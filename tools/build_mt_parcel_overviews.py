"""Build the Montana private-parcel overview GeoJSON from local source tiles.

From the repository root, install tools/requirements-parcel-overview.txt, then
run this script. Use --check to verify that the generated file is up to date.
"""

import argparse
import json
import re
import sys
from pathlib import Path

from shapely import make_valid, normalize
from shapely.geometry import mapping, shape
from shapely.ops import unary_union


ROOT = Path(__file__).resolve().parents[1]
PARCEL_TILE_DIR = ROOT / "tiles" / "mt"
DISTRICT_DIR = ROOT / "mt"
OUTPUT_PATH = DISTRICT_DIR / "parcel_overviews.json"
DISTRICTS = ("270", "321")
SIMPLIFY_TOLERANCE = 0.00002
PUBLIC_OWNER_TERMS = (
    "UNITED STATES",
    "FOREST SERVICE",
    "STATE OF MONTANA",
    "BUREAU OF",
    "MONTANA DEPT",
    "MONTANA DEPARTMENT",
    "MONTANA STATE",
    "COUNTY OF",
    "DNRC",
    "FISH WILDLIFE",
    "SCHOOL DIST",
    "CITY OF",
    "TOWN OF",
    "NATIONAL PARK SERVICE",
)


def is_public_owner(properties):
    owner = (properties or {}).get("OwnerName") or ""
    name = re.sub(r"[^A-Z0-9]+", " ", str(owner).upper()).strip()
    if re.search(r"(^| )USA( |$)|(^| )U S A( |$)|\bCOUNTY$", name):
        return True
    return any(term in name for term in PUBLIC_OWNER_TERMS)


def polygon_parts(geometry):
    if geometry.is_empty:
        return []
    if geometry.geom_type == "Polygon":
        return [geometry]
    if hasattr(geometry, "geoms"):
        return [polygon for part in geometry.geoms for polygon in polygon_parts(part)]
    return []


def read_private_parcels():
    geometries = {district: [] for district in DISTRICTS}
    parcel_ids = {district: set() for district in DISTRICTS}
    repaired = {district: 0 for district in DISTRICTS}
    tile_paths = sorted(PARCEL_TILE_DIR.glob("*.json"))
    if not tile_paths:
        raise FileNotFoundError("No local Montana parcel tiles found in " + str(PARCEL_TILE_DIR))

    for tile_path in tile_paths:
        collection = json.loads(tile_path.read_text(encoding="utf-8"))
        for feature in collection.get("features", []):
            properties = feature.get("properties") or {}
            district = str(properties.get("HD", ""))
            parcel_id = str(properties.get("PARCELID", ""))
            if district not in geometries or not parcel_id or is_public_owner(properties):
                continue
            if not feature.get("geometry"):
                raise ValueError("Parcel without geometry in " + str(tile_path))

            geometry = shape(feature["geometry"])
            if not geometry.is_valid:
                geometry = make_valid(geometry)
                repaired[district] += 1
            geometries[district].extend(polygon_parts(geometry))
            parcel_ids[district].add(parcel_id)

    return geometries, parcel_ids, repaired


def build_overviews():
    parcel_geometries, parcel_ids, repaired = read_private_parcels()
    features = []

    for district in DISTRICTS:
        district_path = DISTRICT_DIR / ("hd" + district + ".json")
        collection = json.loads(district_path.read_text(encoding="utf-8"))
        boundary = shape(collection["features"][0]["geometry"])
        if not boundary.is_valid:
            boundary = make_valid(boundary)

        merged = unary_union(parcel_geometries[district]).intersection(boundary)
        merged = unary_union(polygon_parts(make_valid(merged)))
        merged = normalize(merged.simplify(SIMPLIFY_TOLERANCE, preserve_topology=True))
        if merged.is_empty or not merged.is_valid:
            raise ValueError("Invalid dissolved geometry for HD " + district)

        features.append({
            "type": "Feature",
            "properties": {"HD": district, "kind": "private-parcel-union"},
            "geometry": mapping(merged),
        })
        print(
            "HD {}: {} private parcel IDs, {} merged areas, {} repaired inputs".format(
                district, len(parcel_ids[district]), len(polygon_parts(merged)), repaired[district]
            )
        )

    data = {"type": "FeatureCollection", "features": features}
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the overview GeoJSON is stale")
    args = parser.parse_args()
    generated = build_overviews()

    if args.check:
        if not OUTPUT_PATH.is_file() or OUTPUT_PATH.read_text(encoding="utf-8") != generated:
            print(str(OUTPUT_PATH) + " is missing or out of date", file=sys.stderr)
            return 1
        print(str(OUTPUT_PATH) + " is up to date")
        return 0

    OUTPUT_PATH.write_text(generated, encoding="utf-8", newline="\n")
    print("Wrote {} ({} bytes)".format(OUTPUT_PATH, OUTPUT_PATH.stat().st_size))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())