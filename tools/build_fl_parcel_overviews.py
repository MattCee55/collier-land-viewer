"""Build dissolved, low-zoom overviews from Florida parcel tiles.

Each source parcel is repeated across the tiles it touches. This builder
deduplicates those copies by SOURCE_OID, dissolves adjoining parcel boundaries,
and writes one simplified MultiPolygon feature per county for zoomed-out views.
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
DEFAULT_TILE_ROOT = ROOT / "tiles"
DEFAULT_OUTPUT_PATH = ROOT / "fl" / "parcel_overviews.json"
COUNTIES = ("collier", "lee")
BATCH_SIZE = 5000
SIMPLIFY_TOLERANCE = 0.0001
PUBLIC_OWNER_FIELDS = (
    "OWN_NAME",
    "O_NAME",
    "NAME1",
    "OWNER",
    "OWNER_NAME",
    "OWNERNAME",
)
PUBLIC_OWNER_ADDRESS_FIELDS = ("OWN_ADDR1", "O_ADDR1", "MAIL_ADDR1")
PUBLIC_OWNER_TERMS = (
    "TITF",
    "TIITF",
    "TRUSTEES OF THE INTERNAL IMPROVEMENT TRUST FUND",
    "ST OF FL",
    "STATE OF FL",
    "STATE OF FLORIDA",
    "DEP OF STATE LANDS",
    "FL DEPT OF",
    "FLORIDA DEPT OF",
    "FL DEPARTMENT OF",
    "FLORIDA DEPARTMENT OF",
    "FDOT",
    "FDEP",
    "FLORIDA FISH AND WILDLIFE",
    "FISH AND WILDLIFE CONSERVATION COMMISSION",
    "UNITED STATES",
    "U S GOVERNMENT",
    "US GOVERNMENT",
    "U S GOVT",
    "US GOVT",
    "FEDERAL GOVERNMENT",
    "NATIONAL PARK SERVICE",
    "NATIONAL PARKS SERVICE",
    "UNITED STATES POSTAL SERVICE",
    "US DEPARTMENT OF",
    "U S DEPARTMENT OF",
    "US DEPT OF",
    "U S DEPT OF",
    "US FISH AND WILDLIFE",
    "U S FISH AND WILDLIFE",
    "ARMY CORPS OF ENGINEERS",
    "BUREAU OF LAND MANAGEMENT",
    "FOREST SERVICE",
)


def polygon_parts(geometry):
    if geometry.is_empty:
        return []
    if geometry.geom_type == "Polygon":
        return [geometry]
    if hasattr(geometry, "geoms"):
        return [polygon for part in geometry.geoms for polygon in polygon_parts(part)]
    return []


def is_florida_public_parcel(properties):
    owner_values = [str(properties.get(field) or "") for field in PUBLIC_OWNER_FIELDS]
    other_values = [
        str(properties.get(field) or "") for field in PUBLIC_OWNER_ADDRESS_FIELDS
    ]
    normalize_owner = lambda values: " " + re.sub(
        r"[^A-Z0-9]+", " ", " ".join(values).upper()
    ).strip() + " "
    owner = normalize_owner(owner_values)
    normalized = normalize_owner(owner_values + other_values)
    if owner == " USA ":
        return True
    federal_agencies = (
        " NATIONAL PARK SERVICE ",
        " NATIONAL PARKS SERVICE ",
        " US DEPARTMENT OF ",
        " U S DEPARTMENT OF ",
        " US DEPT OF ",
        " U S DEPT OF ",
        " US FISH AND WILDLIFE ",
        " U S FISH AND WILDLIFE ",
        " ARMY CORPS OF ENGINEERS ",
        " BUREAU OF LAND MANAGEMENT ",
        " FOREST SERVICE ",
    )
    if " USA " in owner and any(term in normalized for term in federal_agencies):
        return True
    return any(
        " " + term + " " in normalized
        for term in PUBLIC_OWNER_TERMS
    )


def dissolve_geometries(geometries, simplify_tolerance=SIMPLIFY_TOLERANCE):
    merged = unary_union(geometries)
    if not merged.is_valid:
        merged = make_valid(merged)
    polygons = polygon_parts(merged)
    if not polygons:
        raise ValueError("Dissolve produced no polygon geometry")

    merged = unary_union(polygons)
    if simplify_tolerance:
        merged = merged.simplify(simplify_tolerance, preserve_topology=True)
    merged = normalize(merged)
    if merged.is_empty or not merged.is_valid:
        raise ValueError("Dissolve produced invalid polygon geometry")
    return merged


def build_county_overview(county, tile_root):
    tile_paths = sorted((tile_root / county).glob("*.json"))
    if not tile_paths:
        raise FileNotFoundError("No parcel tiles found for " + county)

    parcel_ids = set()
    excluded_public_ids = set()
    geometry_batch = []
    dissolved_batches = []
    repaired_count = 0

    for tile_path in tile_paths:
        collection = json.loads(tile_path.read_text(encoding="utf-8"))
        if collection.get("type") != "FeatureCollection":
            raise ValueError("Invalid GeoJSON tile: " + str(tile_path))

        for feature in collection.get("features", []):
            properties = feature.get("properties") or {}
            source_id = str(properties.get("SOURCE_OID") or "").strip()
            if not source_id:
                raise ValueError("Parcel is missing SOURCE_OID in " + str(tile_path))
            if source_id in parcel_ids or source_id in excluded_public_ids:
                continue
            if is_florida_public_parcel(properties):
                excluded_public_ids.add(source_id)
                continue
            parcel_ids.add(source_id)

            geometry_data = feature.get("geometry")
            if not geometry_data:
                raise ValueError("Parcel is missing geometry in " + str(tile_path))
            geometry = shape(geometry_data)
            if not geometry.is_valid:
                geometry = make_valid(geometry)
                repaired_count += 1
            parts = polygon_parts(geometry)
            if not parts:
                raise ValueError("Parcel has no polygon geometry in " + str(tile_path))
            geometry_batch.extend(parts)

            if len(geometry_batch) >= BATCH_SIZE:
                dissolved_batches.append(unary_union(geometry_batch))
                geometry_batch.clear()

    if geometry_batch:
        dissolved_batches.append(unary_union(geometry_batch))
    if not parcel_ids or not dissolved_batches:
        raise ValueError("No parcel polygons found for " + county)

    merged = dissolve_geometries(dissolved_batches)
    return {
        "type": "Feature",
        "properties": {
            "COUNTY": county,
            "PARCEL_COUNT": len(parcel_ids),
            "kind": "parcel-union",
        },
        "geometry": mapping(merged),
    }, repaired_count, len(parcel_ids), len(polygon_parts(merged)), len(excluded_public_ids)


def build_overviews(tile_root=DEFAULT_TILE_ROOT):
    features = []
    for county in COUNTIES:
        (
            feature,
            repaired_count,
            parcel_count,
            area_count,
            excluded_public_count,
        ) = build_county_overview(county, tile_root)
        features.append(feature)
        print(
            "{}: {:,} unique parcels dissolved into {:,} connected areas; "
            "excluded {:,} public parcels; {} repaired inputs".format(
                county,
                parcel_count,
                area_count,
                excluded_public_count,
                repaired_count,
            ),
            flush=True,
        )
    data = {"type": "FeatureCollection", "features": features}
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tile-root",
        type=Path,
        default=DEFAULT_TILE_ROOT,
        help="directory containing collier and lee parcel tile folders",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help="destination for the merged overview FeatureCollection",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if the overview GeoJSON is missing or out of date",
    )
    args = parser.parse_args()
    generated = build_overviews(args.tile_root)

    if args.check:
        if not args.output.is_file() or args.output.read_text(encoding="utf-8") != generated:
            print(str(args.output) + " is missing or out of date", file=sys.stderr)
            return 1
        print(str(args.output) + " is up to date")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(generated, encoding="utf-8", newline="\n")
    print("Wrote {} ({} bytes)".format(args.output, args.output.stat().st_size))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())