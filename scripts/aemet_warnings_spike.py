#!/usr/bin/env python3
"""Fetch and normalize the current official AEMET CAP warnings for inspection.

This is a spike only: it writes an ignored local preview, never changes the
published warnings/warnings.json and never exposes AEMET's temporary URLs.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import tarfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from aemet_radar_spike import AemetError, fetch, read_hateoas, safe_headers, safe_metadata_preview, utc_now


def local_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def child_text(parent: ET.Element, name: str) -> str | None:
    for child in parent:
        if local_name(child) == name:
            return child.text.strip() if child.text else None
    return None


def children(parent: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in parent if local_name(child) == name]


def parameter_values(info: ET.Element) -> dict[str, str]:
    values = {}
    for parameter in children(info, "parameter"):
        key, value = child_text(parameter, "valueName"), child_text(parameter, "value")
        if key and value:
            values[key.lower()] = value
    return values


def level(parameters: dict[str, str], severity: str | None) -> str:
    raw = " ".join(parameters.values()).lower()
    if "red" in raw or "rojo" in raw:
        return "RED"
    if "orange" in raw or "naranja" in raw:
        return "ORANGE"
    if "yellow" in raw or "amarillo" in raw:
        return "YELLOW"
    if "green" in raw or "verde" in raw:
        return "GREEN"
    mapping = {"extreme": "RED", "severe": "ORANGE", "moderate": "YELLOW", "minor": "GREEN"}
    return mapping.get((severity or "").lower(), "UNKNOWN")


def phenomena(event: str | None) -> list[str]:
    value = (event or "").lower()
    found = []
    if "torment" in value:
        found.append("THUNDERSTORM")
    if any(token in value for token in ("lluv", "precipit")):
        found.append("RAIN")
    if any(token in value for token in ("viento", "racha")):
        found.append("WIND")
    if any(token in value for token in ("temperatura", "calor")):
        found.append("HEAT")
    if "frío" in value or "frio" in value or "helad" in value:
        found.append("COLD")
    if "nieve" in value or "nevad" in value:
        found.append("SNOW")
    if "costero" in value:
        found.append("COASTAL")
    if "niebla" in value:
        found.append("FOG")
    if "polvo" in value:
        found.append("DUST")
    if "alud" in value:
        found.append("AVALANCHE")
    return found or ["OTHER"]


def selected_info(alert: ET.Element) -> ET.Element | None:
    infos = children(alert, "info")
    return next((info for info in infos if (child_text(info, "language") or "").lower().startswith("es")), infos[0] if infos else None)


def normalize_cap_xml(cap: bytes) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(cap)
    except ET.ParseError as error:
        raise AemetError(f"CAP XML inválido: {error}") from error
    alerts = [root] if local_name(root) == "alert" else [item for item in root.iter() if local_name(item) == "alert"]
    normalized = []
    for alert in alerts:
        identifier = child_text(alert, "identifier") or "unknown"
        info = selected_info(alert)
        if info is None:
            continue
        parameters = parameter_values(info)
        event = child_text(info, "event")
        severity = child_text(info, "severity")
        areas = children(info, "area") or [None]
        for index, area in enumerate(areas):
            polygons = [child_text(item, "polygon") for item in children(area, "polygon")] if area is not None else []
            geocodes = {}
            if area is not None:
                for geocode in children(area, "geocode"):
                    key, value = child_text(geocode, "valueName"), child_text(geocode, "value")
                    if key and value:
                        geocodes[key] = value
            normalized.append({
                "id": f"{identifier}:area-{index}",
                "sourceId": identifier,
                # A combined event such as "Lluvias y tormentas" retains both;
                # the first value is its more operationally significant primary type.
                "phenomenon": phenomena(event)[0],
                "phenomena": phenomena(event),
                "event": event,
                "level": level(parameters, severity),
                "severity": severity,
                "area": child_text(area, "areaDesc") if area is not None else None,
                "geocodes": geocodes or None,
                # CAP polygons use latitude,longitude pairs. Keep source text; no geometry is inferred.
                "polygons": [polygon for polygon in polygons if polygon] or None,
                "startsAt": child_text(info, "onset") or child_text(info, "effective"),
                "endsAt": child_text(info, "expires"),
                "updatedAt": child_text(alert, "sent"),
                "status": child_text(alert, "status"),
                "messageType": child_text(alert, "msgType"),
                "source": "AEMET_OPENDATA_CAP",
            })
    return normalized


def normalize_alerts(payload: bytes, content_type: str | None) -> tuple[str, list[dict[str, Any]]]:
    """Normalize AEMET's CAP payload, which is currently a TAR of CAP 1.2 XML.

    The OpenData metadata declares ``application/x-gtar``.  Keeping TAR support
    here avoids treating a successful official response as malformed XML while
    still accepting a direct CAP XML response if AEMET changes the transport.
    """
    is_tar = "tar" in (content_type or "").lower() or payload.startswith(b"ustar")
    if not is_tar:
        return "CAP XML", normalize_cap_xml(payload)
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as archive:
            members = [member for member in archive.getmembers() if member.isfile() and member.name.lower().endswith(".xml")]
            if not members:
                raise AemetError("El TAR de avisos no contiene CAP XML.")
            warnings: list[dict[str, Any]] = []
            for member in members:
                source = archive.extractfile(member)
                if source is None:
                    continue
                warnings.extend(normalize_cap_xml(source.read()))
            return "TAR con CAP XML v1.2", warnings
    except (tarfile.TarError, OSError) as error:
        raise AemetError(f"TAR de avisos inválido: {error}") from error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--area", default="esp", help="AEMET CAP area, default: esp")
    parser.add_argument("--output", type=Path, default=Path(".spike/aemet-warnings"))
    args = parser.parse_args()
    api_key = os.environ.get("AEMET_API_KEY")
    if not api_key:
        print("ERROR: AEMET_API_KEY no está definida.", file=sys.stderr)
        return 2
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    try:
        hateoas = read_hateoas(f"/avisos_cap/ultimoelaborado/area/{args.area}", api_key)
        cap, cap_headers, _ = fetch(hateoas["datos"])
        metadata, metadata_headers, _ = fetch(hateoas["metadatos"])
        (output / "warnings.cap.xml").write_bytes(cap)
        (output / "warnings.metadata").write_bytes(metadata)
        try:
            payload_format, warnings = normalize_alerts(cap, cap_headers.get("content-type"))
        except AemetError as error:
            diagnostic = {
                "version": 1,
                "source": "AEMET",
                "generatedAt": utc_now(),
                "areaRequest": args.area,
                "format": "UNPARSEABLE",
                "capHeaders": safe_headers(cap_headers),
                "metadataHeaders": safe_headers(metadata_headers),
                "metadataPreview": safe_metadata_preview(metadata),
                "dataPreview": safe_metadata_preview(cap),
                "error": str(error),
            }
            (output / "report.json").write_text(json.dumps(diagnostic, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            (output / "SUMMARY.md").write_text(f"# AEMET warnings spike\n\nERROR: {error}\n\nMIME: {diagnostic['capHeaders'].get('content-type', '—')}\n", encoding="utf-8")
            raise
        preview = {
            "version": 1,
            "source": "AEMET",
            "generatedAt": utc_now(),
            "areaRequest": args.area,
            "format": payload_format,
            "capHeaders": safe_headers(cap_headers),
            "metadataHeaders": safe_headers(metadata_headers),
            "metadataPreview": safe_metadata_preview(metadata),
            "warningCount": len(warnings),
            "warnings": warnings,
        }
        (output / "report.json").write_text(json.dumps(preview, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        by_phenomenon: dict[str, int] = {}
        by_level: dict[str, int] = {}
        with_polygons = 0
        for warning in warnings:
            by_phenomenon[warning["phenomenon"]] = by_phenomenon.get(warning["phenomenon"], 0) + 1
            by_level[warning["level"]] = by_level.get(warning["level"], 0) + 1
            with_polygons += bool(warning["polygons"])
        summary = [
            "# AEMET warnings spike", "", f"Generado: {preview['generatedAt']}",
            f"- Endpoint: `/avisos_cap/ultimoelaborado/area/{args.area}`",
            f"- Formato: {payload_format} · avisos normalizados: {len(warnings)}",
            f"- Fenómenos: {by_phenomenon or 'ninguno'}",
            f"- Niveles: {by_level or 'ninguno'}",
            f"- Avisos con polígono CAP: {with_polygons}/{len(warnings)}",
            f"- Metadata: {preview['metadataPreview'] or '—'}", "",
        ]
        (output / "SUMMARY.md").write_text("\n".join(summary), encoding="utf-8")
        print(f"Warnings spike terminado: {len(warnings)} avisos. Informe: {output / 'SUMMARY.md'}")
        return 0
    except AemetError as error:
        print(f"ERROR AEMET warnings: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
