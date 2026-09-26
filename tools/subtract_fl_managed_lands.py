"""Subtract displayed Florida managed-land polygons from parcel overviews."""

import argparse
import json
import os
import tempfile
from pathlib import Path

from shapely import make_valid, normalize
from shapely.geometry import mapping, shape
from shapely.ops import unary_union


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OVERVIEW = ROOT / "fl" / "parcel_overviews.json"
DEFAULT_MANAGED = ROOT / "flma" / "collier_lee_fire.json"
COUNTIES = ("collier", "lee")


def polygon_parts(geometry):
    if geometry.is_empty:
        return []
    if geometry.geom_type == "Polygon":
        return [geometry]
    if hasattr(geometry, "geoms"):
        return [polygon for part in geometry.geoms for polygon in polygon_parts(part)]
    return []


def valid_polygonal(geometry, description):
    if not geometry.is_valid:
        geometry = make_valid(geometry)
    polygons = polygon_parts(geometry)
    if not polygons:
        raise ValueError(description + " contains no polygon geometry")
    result = unary_union(polygons)
    if not result.is_valid:
        result = make_valid(result)
        polygons = polygon_parts(result)
        if not polygons:
            raise ValueError(description + " could not be repaired")
        result = unary_union(polygons)
    if result.is_empty or not result.is_valid:
        raise ValueError(description + " is empty or invalid")
    return result


def load_managed_geometries(path):
    collection = json.loads(path.read_text(encoding="utf-8"))
    if collection.get("type") != "FeatureCollection":
        raise ValueError("Managed-land source is not a FeatureCollection")

    geometries = {county: [] for county in COUNTIES}
    for feature in collection.get("features", []):
        properties = feature.get("properties") or {}
        county = str(properties.get("COUNTY") or "").casefold()
        if county not in geometries:
            continue
        geometry_data = feature.get("geometry")
        if not geometry_data:
            raise ValueError("Managed-land feature is missing geometry")
        geometries[county].append(
            valid_polygonal(shape(geometry_data), county + " managed land")
        )

    result = {}
    for county, items in geometries.items():
        if not items:
            raise ValueError("No managed-land features found for " + county)
        result[county] = valid_polygonal(
            unary_union(items), county + " managed-land union"
        )
    return result, {county: len(items) for county, items in geometries.items()}


def subtract_managed_lands(overview_path, managed_path):
    overview = json.loads(overview_path.read_text(encoding="utf-8"))
    if overview.get("type") != "FeatureCollection":
        raise ValueError("Parcel overview is not a FeatureCollection")

    managed, managed_counts = load_managed_geometries(managed_path)
    found = set()
    for feature in overview.get("features", []):
        properties = feature.setdefault("properties", {})
        county = str(properties.get("COUNTY") or "").casefold()
        if county not in managed:
            continue
        if county in found:
            raise ValueError("Parcel overview has duplicate feature for " + county)
        found.add(county)

        geometry_data = feature.get("geometry")
        if not geometry_data:
            raise ValueError(county + " parcel overview is missing geometry")
        outline = valid_polygonal(shape(geometry_data), county + " parcel overview")
        private = valid_polygonal(
            outline.difference(managed[county]),
            county + " parcel-minus-managed-land result",
        )
        overlap_area = private.intersection(managed[county]).area
        if overlap_area > max(1e-12, outline.area * 1e-12):
            raise ValueError(
                "{} result still overlaps managed land by {:.12g} square degrees".format(
                    county, overlap_area
                )
            )

        removed_area = outline.area - private.area
        properties["kind"] = "parcel-union-minus-managed-land"
        properties["MANAGED_FEATURE_COUNT"] = managed_counts[county]
        feature["geometry"] = mapping(normalize(private))
        print(
            "{}: subtracted {:,} managed features; removed {:.6%} of parcel "
            "outline area; remaining overlap {:.12g}".format(
                county,
                managed_counts[county],
                removed_area / outline.area if outline.area else 0,
                overlap_area,
            ),
            flush=True,
        )

    missing = set(COUNTIES).difference(found)
    if missing:
        raise ValueError("Parcel overview is missing counties: " + ", ".join(sorted(missing)))

    return json.dumps(overview, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n"


def check_no_managed_land_overlap(overview_path, managed_path):
    overview = json.loads(overview_path.read_text(encoding="utf-8"))
    if overview.get("type") != "FeatureCollection":
        raise ValueError("Parcel overview is not a FeatureCollection")

    managed, managed_counts = load_managed_geometries(managed_path)
    found = set()
    for feature in overview.get("features", []):
        properties = feature.get("properties") or {}
        county = str(properties.get("COUNTY") or "").casefold()
        if county not in managed:
            continue
        if county in found:
            raise ValueError("Parcel overview has duplicate feature for " + county)
        found.add(county)
        geometry_data = feature.get("geometry")
        if not geometry_data:
            raise ValueError(county + " parcel overview is missing geometry")
        outline = valid_polygonal(shape(geometry_data), county + " parcel overview")
        overlap_area = outline.intersection(managed[county]).area
        if overlap_area > max(1e-12, outline.area * 1e-12):
            raise ValueError(
                "{} overview overlaps managed land by {:.12g} square degrees".format(
                    county, overlap_area
                )
            )
        print(
            "{}: checked {:,} managed features; remaining overlap {:.12g}".format(
                county, managed_counts[county], overlap_area
            ),
            flush=True,
        )

    missing = set(COUNTIES).difference(found)
    if missing:
        raise ValueError("Parcel overview is missing counties: " + ", ".join(sorted(missing)))


def write_atomic(path, contents):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(contents)
        os.replace(temporary_path, path)
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--overview", type=Path, default=DEFAULT_OVERVIEW)
    parser.add_argument("--managed", type=Path, default=DEFAULT_MANAGED)
    parser.add_argument("--check", action="store_true", help="verify only; do not write")
    args = parser.parse_args()

    if args.check:
        check_no_managed_land_overlap(args.overview, args.managed)
        print(str(args.overview) + " has no managed-land overlap")
        return 0

    generated = subtract_managed_lands(args.overview, args.managed)
    write_atomic(args.overview, generated)
    print("Updated " + str(args.overview), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())