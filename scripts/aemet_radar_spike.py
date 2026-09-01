#!/usr/bin/env python3
"""Reproducible AEMET radar acquisition/inspection spike.

It deliberately does not publish anything.  AEMET OpenData's first response is
HATEOAS JSON; the temporary ``datos`` and ``metadatos`` URLs are fetched right
away and retained only under the ignored output directory for inspection.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API_ROOT = "https://opendata.aemet.es/opendata/api"
USER_AGENT = "pluja-aemet-live-radar-spike/1.0"


class AemetError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_valid_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def freshness(value: str | None, now: datetime | None = None) -> tuple[str, int | None, str | None]:
    timestamp = parse_valid_time(value)
    if timestamp is None:
        return "UNAVAILABLE", None, None
    age_minutes = round(((now or datetime.now(timezone.utc)) - timestamp).total_seconds() / 60)
    # A radar frame older than 90 minutes is not suitable as current radar.
    return ("FRESH" if -15 <= age_minutes <= 90 else "STALE", age_minutes, timestamp.isoformat().replace("+00:00", "Z"))


def fetch(url: str, *, api_key: str | None = None) -> tuple[bytes, dict[str, str], int]:
    headers = {"Accept": "application/json, image/*;q=0.9, */*;q=0.1", "User-Agent": USER_AGENT}
    if api_key:
        # The OpenAPI security scheme accepts api_key as a header; never place it
        # in a URL that could end up in a report, proxy log or Git artifact.
        headers["api_key"] = api_key
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=35) as response:
            return response.read(), {key.lower(): value for key, value in response.headers.items()}, response.status
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")[:500]
        raise AemetError(f"HTTP {error.code} en {url.split('?')[0]}: {body}") from error
    except URLError as error:
        raise AemetError(f"Error de red en {url.split('?')[0]}: {error.reason}") from error


def read_hateoas(endpoint: str, api_key: str) -> dict[str, Any]:
    raw, _, _ = fetch(f"{API_ROOT}{endpoint}", api_key=api_key)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise AemetError(f"Respuesta HATEOAS no JSON para {endpoint}") from error
    if payload.get("estado") != 200 or not payload.get("datos"):
        raise AemetError(f"AEMET rechazó {endpoint}: estado={payload.get('estado')} descripcion={payload.get('descripcion')}")
    return payload


def extension(content_type: str, data: bytes) -> str:
    if data.startswith(b"\x1f\x8b"):
        return ".tar.gz"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if "png" in content_type:
        return ".png"
    if "gif" in content_type:
        return ".gif"
    if "jpeg" in content_type or "jpg" in content_type:
        return ".jpg"
    return ".bin"


TIFF_TYPES = {1: (1, "B"), 2: (1, "c"), 3: (2, "H"), 4: (4, "I"), 5: (8, "II"), 12: (8, "d")}


def tiff_values(data: bytes, endian: str, offset: int, field_type: int, count: int, raw_value: bytes) -> list[Any]:
    import struct

    width, code = TIFF_TYPES.get(field_type, (0, ""))
    if not width:
        return []
    length = width * count
    raw = raw_value[:length] if length <= 4 else data[offset:offset + length]
    if field_type == 2:
        return [raw.rstrip(b"\0").decode("ascii", errors="replace")]
    if field_type == 5:
        return [struct.unpack(endian + "d" * 0)[0]] if False else [a / b for a, b in struct.iter_unpack(endian + "II", raw)]
    return list(struct.unpack(endian + str(count) + code, raw))


def inspect_geotiff(data: bytes) -> dict[str, Any]:
    import struct

    if data[:2] == b"II":
        endian = "<"
    elif data[:2] == b"MM":
        endian = ">"
    else:
        return {"format": "not TIFF"}
    if len(data) < 8 or struct.unpack(endian + "H", data[2:4])[0] != 42:
        return {"format": "invalid TIFF"}
    directory = struct.unpack(endian + "I", data[4:8])[0]
    entries = struct.unpack(endian + "H", data[directory:directory + 2])[0]
    tags: dict[int, list[Any]] = {}
    for index in range(entries):
        start = directory + 2 + index * 12
        tag, field_type, count, offset = struct.unpack(endian + "HHII", data[start:start + 12])
        tags[tag] = tiff_values(data, endian, offset, field_type, count, data[start + 8:start + 12])
    value = lambda tag, default=None: tags.get(tag, [default])[0]
    scale = tags.get(33550, [])
    tiepoint = tags.get(33922, [])
    bbox = None
    if len(scale) >= 2 and len(tiepoint) >= 6 and value(256) and value(257):
        left, top = tiepoint[3], tiepoint[4]
        bbox = {"west": left, "north": top, "east": left + value(256) * scale[0], "south": top - value(257) * scale[1]}
    palette_entries = len(tags.get(320, [])) // 3
    gdal_metadata = value(42112)
    gdal_items = {}
    if isinstance(gdal_metadata, str):
        for name, item in re.findall(r'<Item name="([^"]+)">(.*?)</Item>', gdal_metadata, flags=re.DOTALL):
            gdal_items[name] = item.strip()
    return {
        "format": "GeoTIFF",
        "width": value(256),
        "height": value(257),
        "bitDepth": value(258),
        "compression": value(259),
        "photometricInterpretation": value(262),
        "channels": value(277),
        "sampleFormat": value(339),
        "indexedPalette": value(262) == 3,
        "paletteEntries": palette_entries,
        "noData": value(42113),
        "pixelScaleDegrees": scale[:2] if scale else None,
        "bbox": bbox,
        "geoKeyDirectory": tags.get(34735),
        "geoAscii": value(34737),
        "gdalItems": gdal_items,
    }


def inspect_container(data: bytes, content_type: str) -> dict[str, Any]:
    if not tarfile.is_tarfile(io.BytesIO(data)):
        return inspect_image(data, content_type)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
        members = [member for member in archive.getmembers() if member.isfile()]
        frames = []
        for member in members:
            if not member.name.lower().endswith((".tif", ".tiff")):
                continue
            content = archive.extractfile(member)
            if content is None:
                continue
            info = inspect_geotiff(content.read())
            timestamp = re.search(r"(\d{12})", member.name)
            frames.append({"name": member.name, "bytes": member.size, "validTime": timestamp.group(1) if timestamp else None, "info": info})
    return {
        "format": "TAR.GZ containing GeoTIFF",
        "mime": content_type or "unknown",
        "bytes": len(data),
        "frameCount": len(frames),
        "frames": frames,
        "latestFrame": max(frames, key=lambda frame: frame["validTime"] or "") if frames else None,
    }


def public_image_summary(image: dict[str, Any]) -> dict[str, Any]:
    """Keep the Actions artifact small and free of temporary provider URLs."""
    latest = image.get("latestFrame") or {}
    latest_info = latest.get("info", image)
    status, age_minutes, valid_time = freshness(latest.get("validTime"))
    items = latest_info.get("gdalItems", {})
    return {
        "format": image.get("format"),
        "mime": image.get("mime"),
        "archiveBytes": image.get("bytes"),
        "frameCount": image.get("frameCount", 1),
        "latestFrame": {
            "name": latest.get("name"),
            "validTime": valid_time,
            "ageMinutes": age_minutes,
            "freshness": status,
            "bytes": latest.get("bytes"),
            "width": latest_info.get("width"),
            "height": latest_info.get("height"),
            "bbox": latest_info.get("bbox"),
            "crs": latest_info.get("geoAscii"),
            "noData": latest_info.get("noData"),
            "indexedPalette": latest_info.get("indexedPalette"),
            "paletteEntries": latest_info.get("paletteEntries"),
            "dataTypeUnits": items.get("DATA_TYPE_UNITS"),
            "hasDocumentedScale": bool(items.get("ESCALA")),
        },
    }


def jpeg_dimensions(data: bytes) -> tuple[int, int, int] | None:
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        index += 2
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            continue
        length = int.from_bytes(data[index:index + 2], "big")
        if length < 2 or index + length > len(data):
            return None
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            return int.from_bytes(data[index + 5:index + 7], "big"), int.from_bytes(data[index + 3:index + 5], "big"), data[index + 7]
        index += length
    return None


def inspect_image(data: bytes, content_type: str) -> dict[str, Any]:
    info: dict[str, Any] = {"mime": content_type or "unknown", "bytes": len(data), "format": "unknown"}
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 33:
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        bit_depth, color_type = data[24], data[25]
        channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
        info.update({"format": "PNG", "width": width, "height": height, "bitDepth": bit_depth, "colorType": color_type, "channels": channels, "hasAlpha": color_type in (4, 6)})
        palette: list[list[int]] = []
        position = 8
        while position + 12 <= len(data):
            length = int.from_bytes(data[position:position + 4], "big")
            chunk = data[position + 4:position + 8]
            value = data[position + 8:position + 8 + length]
            if chunk == b"PLTE":
                palette = [list(value[i:i + 3]) for i in range(0, len(value), 3)]
            if chunk == b"tRNS":
                info["paletteTransparencyEntries"] = len(value)
            position += length + 12
        if palette:
            info["paletteEntries"] = len(palette)
            info["paletteSample"] = palette[:16]
    elif data.startswith((b"GIF87a", b"GIF89a")) and len(data) >= 13:
        packed = data[10]
        info.update({"format": "GIF", "width": int.from_bytes(data[6:8], "little"), "height": int.from_bytes(data[8:10], "little"), "indexed": True, "paletteEntries": 2 ** ((packed & 0x07) + 1) if packed & 0x80 else 0, "hasAlpha": "unknown (GIF transparency extension)"})
    elif data.startswith(b"\xff\xd8\xff"):
        dimensions = jpeg_dimensions(data)
        info.update({"format": "JPEG", "hasAlpha": False})
        if dimensions:
            info.update({"width": dimensions[0], "height": dimensions[1], "channels": dimensions[2]})
    return info


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def try_json(data: bytes) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def safe_headers(headers: dict[str, str]) -> dict[str, str]:
    allowed = ("content-type", "content-length", "last-modified", "date")
    return {key: value for key, value in headers.items() if key in allowed}


def safe_metadata_preview(data: bytes) -> str:
    text = data.decode("latin-1", errors="replace").replace("\x00", " ")
    # Metadata and data links can be temporary HATEOAS URLs; artifacts must not retain them.
    text = re.sub(r"https?://\S+", "<redacted-url>", text)
    return " ".join(text.split())[:800]


def product(name: str, endpoint: str, output: Path, api_key: str) -> dict[str, Any]:
    result: dict[str, Any] = {"name": name, "endpoint": endpoint, "queriedAt": utc_now()}
    try:
        hateoas = read_hateoas(endpoint, api_key)
        # datos/metadatos are short-lived HATEOAS URLs. Do not put them in reports.
        result["hateoas"] = {key: hateoas.get(key) for key in ("estado", "descripcion")}
        data, data_headers, _ = fetch(hateoas["datos"])
        meta, meta_headers, _ = fetch(hateoas["metadatos"])
        image_info = inspect_container(data, data_headers.get("content-type", ""))
        suffix = extension(data_headers.get("content-type", ""), data)
        (output / f"{name}{suffix}").write_bytes(data)
        (output / f"{name}.metadata").write_bytes(meta)
        metadata_json = try_json(meta)
        latest_info = (image_info.get("latestFrame") or {}).get("info", image_info)
        gdal_items = latest_info.get("gdalItems", {})
        if gdal_items.get("DATA_TYPE_UNITS") == "dBZ" and gdal_items.get("ESCALA"):
            intensity = "VIABLE: el GeoTIFF declara dBZ y publica una escala RGBA/intervalos en GDAL metadata; el procesador puede normalizar por esos intervalos documentados."
        elif latest_info.get("indexedPalette"):
            intensity = "PARTIAL: conserva índices y no-data, pero no hay escala dBZ/mm/h documentada en este frame; no convertir colores por parecido visual."
        else:
            intensity = "UNDETERMINED: no hay valores físicos ni escala documentada que permitan convertir colores de forma fiable."
        result.update({
            "ok": True,
            "dataHeaders": safe_headers(data_headers),
            "metadataHeaders": safe_headers(meta_headers),
            "image": public_image_summary(image_info),
            "metadataIsJson": metadata_json is not None,
            "metadataPreview": safe_metadata_preview(meta),
            "localData": f"{name}{suffix}",
            "localMetadata": f"{name}.metadata",
            "intensityAssessment": intensity,
        })
    except AemetError as error:
        result.update({"ok": False, "error": str(error)})
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".spike/aemet-radar"), help="Ignored local inspection directory")
    args = parser.parse_args()
    api_key = os.environ.get("AEMET_API_KEY")
    if not api_key:
        print("ERROR: AEMET_API_KEY no está definida. Exporta el secreto únicamente en el entorno antes de ejecutar este spike.", file=sys.stderr)
        return 2
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    results = [
        product("national", "/red/radar/nacional", output, api_key),
        product("barcelona_ba", "/red/radar/regional/ba", output, api_key),
    ]
    report = {"spikeVersion": 1, "generatedAt": utc_now(), "products": results}
    write_json(output / "report.json", report)
    lines = ["# AEMET radar spike", "", f"Generado: {report['generatedAt']}", ""]
    for item in results:
        lines.append(f"## {item['name']}")
        if item["ok"]:
            image = item["image"]
            latest = image.get("latestFrame") or {}
            lines.extend([f"- Endpoint: `{item['endpoint']}`", f"- Estado: {latest.get('freshness', 'UNAVAILABLE')} · edad: {latest.get('ageMinutes', '—')} min", f"- Formato: {image['format']} · {image.get('mime')}", f"- Frames: {image.get('frameCount', 1)}; más reciente: {latest.get('name', 'n/a')} · {latest.get('validTime', '—')}", f"- Dimensiones: {latest.get('width', '?')}×{latest.get('height', '?')} · archivo: {image.get('archiveBytes', '?')} bytes", f"- Geo: {latest.get('crs', 'sin georreferencia detectada')} · bbox={latest.get('bbox')}", f"- Intensidad: {item['intensityAssessment']}", ""])
        else:
            lines.extend([f"- ERROR: {item['error']}", ""])
    (output / "SUMMARY.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"Spike terminado. Informe: {output / 'SUMMARY.md'}")
    for item in results:
        print(f"{item['name']}: {'OK' if item['ok'] else item['error']}")
    return 0 if all(item["ok"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
