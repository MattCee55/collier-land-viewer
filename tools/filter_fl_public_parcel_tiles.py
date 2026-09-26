"""Remove recognized Florida public parcels from tiles and overview geometry."""

import argparse
import json
import os
import tempfile
from pathlib import Path

from shapely import make_valid, normalize
from shapely.geometry import mapping, shape
from shapely.ops import unary_union

from build_fl_parcel_tiles import is_fl_public_parcel
from subtract_fl_managed_lands import (
    load_managed_geometries,
    valid_polygonal,
    write_atomic,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TILE_ROOT = ROOT / "tiles"
DEFAULT_OVERVIEW = ROOT / "fl" / "parcel_overviews.json"
DEFAULT_MANAGED = ROOT / "flma" / "collier_lee_fire.json"
COUNTIES = ("collier", "lee")
BATCH_SIZE = 1000


def gather_public_geometries(tile_root):
    parcel_ids = {county: set() for county in COUNTIES}
    geometry_batches = {county: [] for county in COUNTIES}
    batch_unions = {county: [] for county in COUNTIES}
    public_copies = {county: 0 for county in COUNTIES}

    for county in COUNTIES:
        tile_paths = sorted((tile_root / county).glob("*.json"))
        if not tile_paths:
            raise FileNotFoundError("No parcel tiles found for " + county)
        for tile_path in tile_paths:
            collection = json.loads(tile_path.read_text(encoding="utf-8"))
            if collection.get("type") != "FeatureCollection":
                raise ValueError("Invalid GeoJSON tile: " + str(tile_path))
            for feature in collection.get("features", []):
                properties = feature.get("properties") or {}
                if not is_fl_public_parcel(properties):
                    continue
                public_copies[county] += 1
                source_id = str(properties.get("SOURCE_OID") or "").strip()
                if not source_id:
                    raise ValueError("Public parcel missing SOURCE_OID in " + str(tile_path))
                if source_id in parcel_ids[county]:
                    continue
                geometry_data = feature.get("geometry")
                if not geometry_data:
                    raise ValueError("Public parcel missing geometry in " + str(tile_path))
                parcel_ids[county].add(source_id)
                geometry = valid_polygonal(
                    shape(geometry_data), county + " public parcel " + source_id
                )
                geometry_batches[county].append(geometry)
                if len(geometry_batches[county]) >= BATCH_SIZE:
                    batch_unions[county].append(unary_union(geometry_batches[county]))
                    geometry_batches[county].clear()

        if geometry_batches[county]:
            batch_unions[county].append(unary_union(geometry_batches[county]))
            geometry_batches[county].clear()

    public_geometries = {}
    for county in COUNTIES:
        if parcel_ids[county]:
            public_geometries[county] = valid_polygonal(
                unary_union(batch_unions[county]), county + " public-parcel union"
            )
        print(
            "{}: found {:,} public parcels across {:,} tile copies".format(
                county, len(parcel_ids[county]), public_copies[county]
            ),
            flush=True,
        )
    return parcel_ids, public_geometries


def rewrite_tile(path, features):
    collection = {"type": "FeatureCollection", "features": features}
    contents = json.dumps(
        collection,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ) + "\n"
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


def subtract_public_from_overview(overview_path, managed_path, parcel_ids, public_geometries):
    overview = json.loads(overview_path.read_text(encoding="utf-8"))
    if overview.get("type") != "FeatureCollection":
        raise ValueError("Parcel overview is not a FeatureCollection")
    managed_geometries, _ = load_managed_geometries(managed_path)
    found = set()

    for feature in overview.get("features", []):
        properties = feature.setdefault("properties", {})
        county = str(properties.get("COUNTY") or "").casefold()
        if county not in COUNTIES:
            continue
        if county in found:
            raise ValueError("Duplicate overview feature for " + county)
        found.add(county)

        geometry_data = feature.get("geometry")
        if not geometry_data:
            raise ValueError(county + " overview is missing geometry")
        outline = valid_polygonal(shape(geometry_data), county + " parcel overview")
        public_geometry = public_geometries.get(county)
        if public_geometry is not None:
            outline = outline.difference(public_geometry)
        outline = valid_polygonal(outline, county + " private parcel overview")

        public_overlap = (
            outline.intersection(public_geometry).area if public_geometry is not None else 0
        )
        managed_overlap = outline.intersection(managed_geometries[county]).area
        threshold = max(1e-12, outline.area * 1e-12)
        if public_overlap > threshold or managed_overlap > threshold:
            raise ValueError(
                "{} private overview overlaps public parcels ({:.12g}) or managed "
                "lands ({:.12g})".format(county, public_overlap, managed_overlap)
            )

        previous_ids = set(properties.get("PUBLIC_PARCEL_IDS") or [])
        all_public_ids = previous_ids.union(parcel_ids[county])
        source_count = int(
            properties.setdefault(
                "SOURCE_PARCEL_COUNT", int(properties.get("PARCEL_COUNT") or 0)
            )
        )
        properties["PARCEL_COUNT"] = max(0, source_count - len(all_public_ids))
        properties["PUBLIC_PARCELS_REMOVED"] = len(all_public_ids)
        properties["PUBLIC_PARCEL_IDS"] = sorted(all_public_ids)
        properties["kind"] = "parcel-union-minus-managed-and-public-land"
        feature["geometry"] = mapping(normalize(outline))
        print(
            "{} overview: removed {:,} public parcels; public overlap {:.12g}; "
            "managed overlap {:.12g}".format(
                county, len(all_public_ids), public_overlap, managed_overlap
            ),
            flush=True,
        )

    missing = set(COUNTIES).difference(found)
    if missing:
        raise ValueError("Overview missing counties: " + ", ".join(sorted(missing)))
    return json.dumps(overview, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n"


def filter_tiles(tile_root):
    removed_copies = {county: 0 for county in COUNTIES}
    removed_ids = {county: set() for county in COUNTIES}
    for county in COUNTIES:
        tile_paths = sorted((tile_root / county).glob("*.json"))
        for tile_path in tile_paths:
            collection = json.loads(tile_path.read_text(encoding="utf-8"))
            features = collection.get("features")
            if collection.get("type") != "FeatureCollection" or not isinstance(features, list):
                raise ValueError("Invalid GeoJSON tile: " + str(tile_path))
            kept_features = []
            for feature in features:
                properties = feature.get("properties") or {}
                if is_fl_public_parcel(properties):
                    removed_copies[county] += 1
                    source_id = str(properties.get("SOURCE_OID") or "").strip()
                    if not source_id:
                        raise ValueError("Public parcel missing SOURCE_OID in " + str(tile_path))
                    removed_ids[county].add(source_id)
                else:
                    kept_features.append(feature)
            if len(kept_features) != len(features):
                if kept_features:
                    rewrite_tile(tile_path, kept_features)
                else:
                    tile_path.unlink()

    return removed_copies, removed_ids


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tile-root", type=Path, default=DEFAULT_TILE_ROOT)
    parser.add_argument("--overview", type=Path, default=DEFAULT_OVERVIEW)
    parser.add_argument("--managed", type=Path, default=DEFAULT_MANAGED)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report public parcel records without changing tiles or overview",
    )
    args = parser.parse_args()

    parcel_ids, public_geometries = gather_public_geometries(args.tile_root)
    if args.check:
        return 0

    overview_contents = subtract_public_from_overview(
        args.overview, args.managed, parcel_ids, public_geometries
    )
    write_atomic(args.overview, overview_contents)
    tile_copies, tile_ids = filter_tiles(args.tile_root)
    for county in COUNTIES:
        if tile_ids[county] != parcel_ids[county]:
            raise ValueError(
                "{} tile scan found {} unique public IDs, rewrite found {}".format(
                    county, len(parcel_ids[county]), len(tile_ids[county])
                )
            )
        print(
            "{}: removed {:,} public parcel copies from {:,} unique parcels".format(
                county, tile_copies[county], len(tile_ids[county])
            ),
            flush=True,
        )

    print("Updated public parcel exclusions in tiles and overview", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())