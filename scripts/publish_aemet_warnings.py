#!/usr/bin/env python3
"""Publish the official AEMET CAP warning feed and Meteoalerta zones.

The only geographic source is AEMET's official Meteoalerta zone package.  CAP
temporary HATEOAS URLs and raw CAP payloads are never written to the repo.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import shapefile
from pyproj import Transformer

from aemet_radar_spike import AemetError, fetch, read_hateoas, utc_now
from aemet_warnings_spike import normalize_alerts

ZONES_URL = "https://www.aemet.es/documentos/es/eltiempo/prediccion/avisos/plan_meteoalerta/AEMET-meteoalerta-delimitacion-zonas.zip"
ZONE_FIELD = "AEMET-Meteoalerta zona"


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def current_warnings(items: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Keep active or future official alerts, never an alert already expired."""
    return [item for item in items if (ends_at := parse_time(item.get("endsAt"))) is not None and ends_at >= now]


def compact_warning(item: dict[str, Any]) -> dict[str, Any] | None:
    zone_code = (item.get("geocodes") or {}).get(ZONE_FIELD)
    if not zone_code:
        return None
    return {
        "id": item["id"],
        "phenomenon": item["phenomenon"],
        "level": item["level"],
        "area": item.get("area") or "—",
        "startsAt": item.get("startsAt"),
        "endsAt": item.get("endsAt"),
        "updatedAt": item.get("updatedAt"),
        "source": "AEMET_OPENDATA_CAP",
        "zoneCode": zone_code,
    }


def rings(shape: shapefile.Shape, transform: Transformer) -> list[list[list[float]]]:
    points = [list(transform.transform(x, y)) for x, y in shape.points]
    starts = list(shape.parts) + [len(points)]
    return [[[round(lon, 6), round(lat, 6)] for lon, lat in points[starts[index]:starts[index + 1]]] for index in range(len(starts) - 1)]


def zone_features(archive: bytes) -> list[dict[str, Any]]:
    """Read AEMET EPSG:32630 SHP layers and emit WGS84 GeoJSON features."""
    transformer = Transformer.from_crs("EPSG:32630", "EPSG:4326", always_xy=True)
    features: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as directory:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            bundle.extractall(directory)
        for path in Path(directory).rglob("*.shp"):
            reader = shapefile.Reader(str(path), encoding="iso-8859-15")
            fields = [field[0] for field in reader.fields[1:]]
            required = {"COD_Z", "NOM_Z"}
            if not required.issubset(fields):
                raise AemetError(f"Capa de zonas sin atributos oficiales: {path.name}")
            for record in reader.iterShapeRecords():
                properties = dict(zip(fields, record.record))
                zone_code = str(properties["COD_Z"]).strip()
                if not zone_code:
                    continue
                polygon_rings = rings(record.shape, transformer)
                geometry: dict[str, Any]
                # Official layers use multipart polygons; GeoJSON Polygon supports
                # their rings and preserves the AEMET geometry without inference.
                geometry = {"type": "Polygon", "coordinates": polygon_rings}
                features.append({
                    "type": "Feature",
                    "properties": {"zoneCode": zone_code, "name": str(properties["NOM_Z"]).strip()},
                    "geometry": geometry,
                })
            # pyshp keeps its DBF handle open on Windows unless explicitly closed.
            reader.close()
    duplicates = [feature["properties"]["zoneCode"] for feature in features]
    if len(duplicates) != len(set(duplicates)):
        raise AemetError("La cartografía oficial contiene códigos de zona duplicados.")
    if not features:
        raise AemetError("La cartografía oficial no contiene zonas.")
    return features


def point_in_ring(lon: float, lat: float, ring: list[list[float]]) -> bool:
    inside = False
    for index, point in enumerate(ring):
        previous = ring[index - 1]
        x, y = point
        previous_x, previous_y = previous
        if (y > lat) != (previous_y > lat) and lon < (previous_x - x) * (lat - y) / (previous_y - y) + x:
            inside = not inside
    return inside


def zone_for_point(features: list[dict[str, Any]], lon: float, lat: float) -> str | None:
    for feature in features:
        geometry = feature["geometry"]
        if geometry["type"] == "Polygon" and point_in_ring(lon, lat, geometry["coordinates"][0]):
            return feature["properties"]["zoneCode"]
    return None


def download_zones() -> bytes:
    request = Request(ZONES_URL, headers={"User-Agent": "pluja-aemet-live-warnings/1.0"})
    with urlopen(request, timeout=60) as response:
        return response.read()


def main() -> int:
    api_key = os.environ.get("AEMET_API_KEY")
    if not api_key:
        raise SystemExit("ERROR: AEMET_API_KEY no está definida.")
    output = Path("warnings")
    now = datetime.now(timezone.utc)
    try:
        hateoas = read_hateoas("/avisos_cap/ultimoelaborado/area/esp", api_key)
        payload, headers, _ = fetch(hateoas["datos"])
        _, parsed = normalize_alerts(payload, headers.get("content-type"))
        compact = [warning for item in current_warnings(parsed, now) if (warning := compact_warning(item))]
        features = zone_features(download_zones())
        zone_codes = {feature["properties"]["zoneCode"] for feature in features}
        missing = sorted({item["zoneCode"] for item in compact} - zone_codes)
        if missing:
            raise AemetError(f"CAP contiene códigos ausentes de cartografía oficial: {', '.join(missing[:8])}")
        geojson = {"type": "FeatureCollection", "name": "AEMET Meteoalerta zones", "crs": "EPSG:4326", "features": features}
        warnings = {"version": 1, "source": "AEMET_OPENDATA_CAP", "generatedAt": utc_now(), "warnings": compact}
        staging = Path(tempfile.mkdtemp(prefix="aemet-warnings-"))
        try:
            (staging / "warnings.json").write_text(json.dumps(warnings, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
            (staging / "zones.geojson").write_text(json.dumps(geojson, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
            output.mkdir(exist_ok=True)
            shutil.copy2(staging / "warnings.json", output / "warnings.json")
            shutil.copy2(staging / "zones.geojson", output / "zones.geojson")
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        print(f"Published {len(compact)} active/future warnings and {len(features)} official zones.")
        return 0
    except AemetError as error:
        print(f"ERROR AEMET warnings pipeline: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
