"""Filter selected Florida parcel tiles and rebuild their overview geometry."""

import argparse
import json
import os
import tempfile
from pathlib import Path

from shapely import normalize
from shapely.geometry import mapping, shape
from shapely.ops import unary_union

from build_fl_parcel_overviews import dissolve_geometries
from build_fl_parcel_tiles import is_fl_public_parcel
from fl_parcel_proximity import (
    DEFAULT_PUBLIC_DISTANCE_MILES,
    load_public_land_buffers,
    parcel_intersects_public_buffer,
)
from subtract_fl_managed_lands import (
    load_managed_geometries,
    polygon_parts,
    valid_polygonal,
    write_atomic,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TILE_ROOT = ROOT / "tiles"
DEFAULT_OVERVIEW = ROOT / "fl" / "parcel_overviews.json"
DEFAULT_MANAGED = ROOT / "flma" / "collier_lee_fire.json"
COUNTIES = ("collier", "lee")
DEFAULT_COUNTIES = ("collier",)
BATCH_SIZE = 1000
PROXIMITY_BATCH_SIZE = 5000


def gather_filter_plan(tile_root, public_buffers, counties):
    parcel_ids = {county: set() for county in counties}
    public_geometry_batches = {county: [] for county in counties}
    public_batch_unions = {county: [] for county in counties}
    public_copies = {county: 0 for county in counties}
    private_status = {county: {} for county in counties}
    retained_geometry_batches = {county: [] for county in counties}
    retained_batch_unions = {county: [] for county in counties}
    counts = {county: {"retained_unique": 0} for county in counties}

    for county in counties:
        tile_paths = sorted((tile_root / county).glob("*.json"))
        if not tile_paths:
            raise FileNotFoundError("No parcel tiles found for " + county)
        for path_index, tile_path in enumerate(tile_paths, start=1):
            collection = json.loads(tile_path.read_text(encoding="utf-8"))
            features = collection.get("features")
            if collection.get("type") != "FeatureCollection" or not isinstance(features, list):
                raise ValueError("Invalid GeoJSON tile: " + str(tile_path))
            for feature in features:
                properties = feature.get("properties") or {}
                source_id = str(properties.get("SOURCE_OID") or "").strip()
                if not source_id:
                    raise ValueError("Parcel missing SOURCE_OID in " + str(tile_path))
                geometry_data = feature.get("geometry")
                if not geometry_data:
                    raise ValueError("Parcel missing geometry in " + str(tile_path))

                if is_fl_public_parcel(properties):
                    public_copies[county] += 1
                    if source_id in parcel_ids[county]:
                        continue
                    parcel_ids[county].add(source_id)
                    geometry = valid_polygonal(
                        shape(geometry_data), county + " public parcel " + source_id
                    )
                    public_geometry_batches[county].extend(polygon_parts(geometry))
                    if len(public_geometry_batches[county]) >= BATCH_SIZE:
                        public_batch_unions[county].append(
                            unary_union(public_geometry_batches[county])
                        )
                        public_geometry_batches[county].clear()
                    continue

                status = private_status[county].get(source_id)
                if status is None:
                    status = parcel_intersects_public_buffer(
                        geometry_data, public_buffers[county]
                    )
                    private_status[county][source_id] = status
                    geometry = valid_polygonal(
                        shape(geometry_data), county + " private parcel " + source_id
                    )
                    if status:
                        retained_geometry_batches[county].extend(polygon_parts(geometry))
                        counts[county]["retained_unique"] += 1
                        if len(retained_geometry_batches[county]) >= PROXIMITY_BATCH_SIZE:
                            retained_batch_unions[county].append(
                                unary_union(retained_geometry_batches[county])
                            )
                            retained_geometry_batches[county].clear()

            if path_index % 100 == 0 or path_index == len(tile_paths):
                print(
                    "{} scan: {}/{} tiles ({:.0%}), {:,} unique private parcels".format(
                        county,
                        path_index,
                        len(tile_paths),
                        path_index / len(tile_paths),
                        len(private_status[county]),
                    ),
                    flush=True,
                )

        if public_geometry_batches[county]:
            public_batch_unions[county].append(
                unary_union(public_geometry_batches[county])
            )
        if retained_geometry_batches[county]:
            retained_batch_unions[county].append(
                unary_union(retained_geometry_batches[county])
            )

    public_geometries = {}
    for county in counties:
        if parcel_ids[county]:
            public_geometries[county] = valid_polygonal(
                unary_union(public_batch_unions[county]), county + " public-parcel union"
            )
        print(
            "{}: found {:,} public parcels across {:,} tile copies; "
            "{:,} of {:,} unique private parcels within the buffer".format(
                county,
                len(parcel_ids[county]),
                public_copies[county],
                counts[county]["retained_unique"],
                len(private_status[county]),
            ),
            flush=True,
        )
    return (
        parcel_ids,
        public_geometries,
        private_status,
        counts,
        retained_batch_unions,
    )


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


def build_filtered_overview(
    overview_path,
    managed_path,
    parcel_ids,
    public_geometries,
    retained_parcel_geometries,
    retained_parcel_counts,
    distance_miles,
    counties,
):
    overview = json.loads(overview_path.read_text(encoding="utf-8"))
    if overview.get("type") != "FeatureCollection":
        raise ValueError("Parcel overview is not a FeatureCollection")
    managed_geometries, managed_counts = load_managed_geometries(managed_path)
    found = set()

    for feature in overview.get("features", []):
        properties = feature.setdefault("properties", {})
        county = str(properties.get("COUNTY") or "").casefold()
        if county not in counties:
            continue
        if county in found:
            raise ValueError("Duplicate overview feature for " + county)
        found.add(county)

        geometry_data = feature.get("geometry")
        if not geometry_data:
            raise ValueError(county + " overview is missing geometry")
        existing_outline = valid_polygonal(
            shape(geometry_data), county + " existing parcel overview"
        )
        if not retained_parcel_geometries[county]:
            raise ValueError("No retained private parcel geometries for " + county)
        outline = dissolve_geometries(retained_parcel_geometries[county])
        outline = valid_polygonal(
            outline.intersection(existing_outline),
            county + " existing-overview intersection",
        )
        public_geometry = public_geometries.get(county)
        if public_geometry is not None:
            outline = valid_polygonal(
                outline.difference(public_geometry), county + " public-parcel subtraction"
            )
        outline = valid_polygonal(
            outline.difference(managed_geometries[county]),
            county + " managed-land subtraction",
        )

        public_overlap = (
            outline.intersection(public_geometry).area if public_geometry is not None else None
        )
        managed_overlap = outline.intersection(managed_geometries[county]).area
        threshold = max(1e-12, outline.area * 1e-12)
        if public_overlap is not None and public_overlap > threshold:
            raise ValueError(
                "{} private overview overlaps public parcels ({:.12g})".format(
                    county, public_overlap
                )
            )
        if managed_overlap > threshold:
            raise ValueError(
                "{} private overview overlaps managed lands ({:.12g})".format(
                    county, managed_overlap
                )
            )

        previous_ids = set(properties.get("PUBLIC_PARCEL_IDS") or [])
        all_public_ids = previous_ids.union(parcel_ids[county])
        source_count = int(
            properties.setdefault("SOURCE_PARCEL_COUNT", int(properties.get("PARCEL_COUNT") or 0))
        )
        properties["PARCEL_COUNT"] = retained_parcel_counts[county]
        properties["PUBLIC_PARCELS_REMOVED"] = len(all_public_ids)
        properties["PUBLIC_PARCEL_IDS"] = sorted(all_public_ids)
        properties["PRIVATE_PARCELS_REMOVED_BY_DISTANCE"] = max(
            0, source_count - len(all_public_ids) - retained_parcel_counts[county]
        )
        properties["PUBLIC_BUFFER_DISTANCE_MILES"] = distance_miles
        properties["MANAGED_FEATURE_COUNT"] = managed_counts[county]
        properties["kind"] = "parcel-union-near-public-land-minus-managed-and-public-land"
        feature["geometry"] = mapping(normalize(outline))
        public_overlap_status = (
            "{:.12g}".format(public_overlap)
            if public_overlap is not None
            else "preserved by prior overview mask"
        )
        print(
            "{} overview: retained {:,} private parcels; removed {:,} public parcels; "
            "public overlap {}; managed overlap {:.12g}".format(
                county,
                retained_parcel_counts[county],
                len(all_public_ids),
                public_overlap_status,
                managed_overlap,
            ),
            flush=True,
        )

    missing = set(counties).difference(found)
    if missing:
        raise ValueError("Overview missing counties: " + ", ".join(sorted(missing)))
    return json.dumps(overview, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n"


def filter_tiles(tile_root, private_status, counties):
    removed_copies = {
        county: {"public": 0, "distance": 0} for county in counties
    }
    removed_ids = {
        county: {"public": set(), "distance": set()} for county in counties
    }
    for county in counties:
        tile_paths = sorted((tile_root / county).glob("*.json"))
        for path_index, tile_path in enumerate(tile_paths, start=1):
            collection = json.loads(tile_path.read_text(encoding="utf-8"))
            features = collection.get("features")
            if collection.get("type") != "FeatureCollection" or not isinstance(features, list):
                raise ValueError("Invalid GeoJSON tile: " + str(tile_path))
            kept_features = []
            for feature in features:
                properties = feature.get("properties") or {}
                source_id = str(properties.get("SOURCE_OID") or "").strip()
                if not source_id:
                    raise ValueError("Parcel missing SOURCE_OID in " + str(tile_path))
                if is_fl_public_parcel(properties):
                    removed_copies[county]["public"] += 1
                    removed_ids[county]["public"].add(source_id)
                    continue
                status = private_status[county].get(source_id)
                if status is None:
                    raise ValueError(
                        "Private parcel was not classified in " + str(tile_path)
                    )
                if status:
                    kept_features.append(feature)
                else:
                    removed_copies[county]["distance"] += 1
                    removed_ids[county]["distance"].add(source_id)
            if len(kept_features) != len(features):
                if kept_features:
                    rewrite_tile(tile_path, kept_features)
                else:
                    tile_path.unlink()

            if path_index % 100 == 0 or path_index == len(tile_paths):
                print(
                    "{} rewrite: {}/{} tiles ({:.0%})".format(
                        county,
                        path_index,
                        len(tile_paths),
                        path_index / len(tile_paths),
                    ),
                    flush=True,
                )

    return removed_copies, removed_ids


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tile-root", type=Path, default=DEFAULT_TILE_ROOT)
    parser.add_argument("--overview", type=Path, default=DEFAULT_OVERVIEW)
    parser.add_argument("--managed", type=Path, default=DEFAULT_MANAGED)
    parser.add_argument(
        "--counties",
        nargs="+",
        choices=COUNTIES,
        default=DEFAULT_COUNTIES,
        help="county tile sets to filter (default: collier; pass lee explicitly)",
    )
    parser.add_argument(
        "--distance-miles",
        type=float,
        default=DEFAULT_PUBLIC_DISTANCE_MILES,
        help="retain private parcels within this distance of public-managed land",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="scan and validate the filtered data without changing tiles or overview",
    )
    args = parser.parse_args()
    counties = tuple(dict.fromkeys(args.counties))

    public_buffers, public_feature_counts = load_public_land_buffers(
        args.managed,
        counties,
        is_fl_public_parcel,
        args.distance_miles,
    )
    print(
        "Public-managed features: {}; buffer distance: {:.3f} miles".format(
            ", ".join(
                "{} {:,}".format(county, public_feature_counts[county])
                for county in counties
            ),
            args.distance_miles,
        ),
        flush=True,
    )
    (
        parcel_ids,
        public_geometries,
        private_status,
        counts,
        retained_geometry_batches,
    ) = gather_filter_plan(args.tile_root, public_buffers, counties)
    overview_contents = build_filtered_overview(
        args.overview,
        args.managed,
        parcel_ids,
        public_geometries,
        retained_geometry_batches,
        {county: counts[county]["retained_unique"] for county in counties},
        args.distance_miles,
        counties,
    )

    if args.check:
        print("Dry run complete; tiles and overview were not changed", flush=True)
        return 0

    tile_copies, tile_ids = filter_tiles(args.tile_root, private_status, counties)
    for county in counties:
        if tile_ids[county]["public"] != parcel_ids[county]:
            raise ValueError(
                "{} tile scan found {} unique public IDs, rewrite found {}".format(
                    county,
                    len(parcel_ids[county]),
                    len(tile_ids[county]["public"]),
                )
            )
        expected_far_ids = {
            source_id
            for source_id, is_near in private_status[county].items()
            if not is_near
        }
        if tile_ids[county]["distance"] != expected_far_ids:
            raise ValueError(
                "{} proximity scan found {} unique distant IDs, rewrite found {}".format(
                    county,
                    len(expected_far_ids),
                    len(tile_ids[county]["distance"]),
                )
            )
        print(
            "{}: kept {:,}/{:,} unique private parcels; removed {:,} distant copies "
            "and {:,} public copies".format(
                county,
                counts[county]["retained_unique"],
                len(private_status[county]),
                tile_copies[county]["distance"],
                tile_copies[county]["public"],
            ),
            flush=True,
        )

    write_atomic(args.overview, overview_contents)
    print("Updated proximity and public-parcel exclusions in tiles and overview", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())