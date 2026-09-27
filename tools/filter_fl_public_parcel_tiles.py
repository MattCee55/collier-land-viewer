"""Filter selected Florida parcel tiles and rebuild their overview geometry."""

import argparse
import json
import os
import tempfile
from pathlib import Path

from shapely import difference, intersection, make_valid, normalize
from shapely.geometry import mapping, shape
from shapely.ops import unary_union
from shapely.prepared import prep

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
LEE_BONITA_MANAGED_NAMES = (
    "Imperial Flowway",
    "Corkscrew Regional Ecosystem Watershed",
    "Corkscrew Swamp Sanctuary",
    "Corkscrew Regional Mitigation Bank",
)
BATCH_SIZE = 1000
PROXIMITY_BATCH_SIZE = 5000
MANAGED_EDGE_CLEARANCE_DEGREES = 1e-6
OVERLAP_AREA_TOLERANCE = 1e-11
OVERVIEW_SIMPLIFY_TOLERANCE = 0.0005


def parcel_source_id(properties):
    for field in ("SOURCE_OID", "PARCEL_ID", "PARCELID", "PARCEL_NO"):
        value = str(properties.get(field) or "").strip()
        if value:
            return value
    return ""


def valid_parcel_polygon(geometry_data, description):
    geometry = shape(geometry_data)
    geometry_was_invalid = not geometry.is_valid
    if geometry_was_invalid:
        geometry = make_valid(geometry)
    parts = polygon_parts(geometry)
    if not parts:
        return None, geometry_was_invalid
    return valid_polygonal(unary_union(parts), description), geometry_was_invalid


def clip_parcel_geometry(geometry, managed_geometry, prepared_managed_geometry, description):
    if not prepared_managed_geometry.intersects(geometry):
        return geometry
    overlap = intersection(geometry, managed_geometry)
    if overlap.is_empty or overlap.area <= 0:
        return geometry
    remaining_parts = polygon_parts(
        difference(geometry, managed_geometry)
    )
    if not remaining_parts:
        return None
    clipped_geometry = valid_polygonal(
        unary_union(remaining_parts), description + " managed-land subtraction"
    )
    remaining_overlap = intersection(clipped_geometry, managed_geometry).area
    if remaining_overlap > max(OVERLAP_AREA_TOLERANCE, clipped_geometry.area * 1e-12):
        raise ValueError(
            description + " still overlaps managed land by {:.12g}".format(remaining_overlap)
        )
    return clipped_geometry


def gather_filter_plan(
    tile_root,
    public_buffers,
    managed_geometries,
    prepared_managed_geometries,
    counties,
):
    parcel_ids = {county: set() for county in counties}
    public_copies = {county: 0 for county in counties}
    private_status = {county: {} for county in counties}
    invalid_ids = {county: set() for county in counties}
    invalid_copy_ids = {county: set() for county in counties}
    valid_ids = {county: set() for county in counties}
    private_geometry_ids = {county: set() for county in counties}
    managed_only_ids = {county: set() for county in counties}
    counts = {county: {"near_unique": 0} for county in counties}

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
                source_id = parcel_source_id(properties)
                if not source_id:
                    raise ValueError("Parcel missing SOURCE_OID in " + str(tile_path))
                if is_fl_public_parcel(properties):
                    public_copies[county] += 1
                    parcel_ids[county].add(source_id)
                    continue
                geometry_data = feature.get("geometry")
                if not geometry_data:
                    raise ValueError("Parcel missing geometry in " + str(tile_path))

                geometry, _ = valid_parcel_polygon(
                    geometry_data, county + " private parcel " + source_id
                )
                if geometry is None:
                    invalid_ids[county].add(source_id)
                    invalid_copy_ids[county].add(source_id)
                    private_status[county].setdefault(source_id, False)
                    continue

                valid_ids[county].add(source_id)
                private_geometry = clip_parcel_geometry(
                    geometry,
                    managed_geometries[county],
                    prepared_managed_geometries[county],
                    county + " private parcel " + source_id,
                )
                if private_geometry is None:
                    private_status[county].setdefault(source_id, False)
                    continue

                private_geometry_ids[county].add(source_id)
                is_near = public_buffers is None or parcel_intersects_public_buffer(
                    mapping(private_geometry), public_buffers[county]
                )
                private_status[county][source_id] = (
                    private_status[county].get(source_id, False) or is_near
                )

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

        invalid_ids[county].difference_update(valid_ids[county])
        managed_only_ids[county] = (
            valid_ids[county] - private_geometry_ids[county]
        )
        counts[county]["near_unique"] = sum(private_status[county].values())

    for county in counties:
        print(
            "{}: found {:,} public parcels across {:,} tile copies; "
            "{:,} of {:,} unique private parcels intersect the buffer".format(
                county,
                len(parcel_ids[county]),
                public_copies[county],
                counts[county]["near_unique"],
                len(private_status[county]),
            ),
            flush=True,
        )
    return (
        parcel_ids,
        private_status,
        invalid_ids,
        invalid_copy_ids,
        managed_only_ids,
        counts,
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
    managed_geometries,
    managed_counts,
    parcel_ids,
    private_status,
    invalid_ids,
    retained_parcel_geometries,
    retained_parcel_counts,
    managed_removed_ids,
    distance_miles,
    counties,
):
    features = []
    for county in counties:
        if not retained_parcel_geometries[county]:
            raise ValueError("No retained private parcel geometries for " + county)
        outline = dissolve_geometries(
            retained_parcel_geometries[county],
            simplify_tolerance=OVERVIEW_SIMPLIFY_TOLERANCE,
        )
        outline = valid_polygonal(
            difference(outline, managed_geometries[county]),
            county + " managed-land subtraction from overview",
        )
        managed_overlap = intersection(outline, managed_geometries[county]).area
        threshold = max(OVERLAP_AREA_TOLERANCE, outline.area * 1e-12)
        if managed_overlap > threshold:
            raise ValueError(
                "{} private overview overlaps managed lands ({:.12g})".format(
                    county, managed_overlap
                )
            )

        source_count = len(private_status[county]) + len(parcel_ids[county])
        managed_removed_count = len(managed_removed_ids[county])
        properties = {
            "COUNTY": county,
            "PARCEL_COUNT": retained_parcel_counts[county],
            "SOURCE_PARCEL_COUNT": source_count,
            "PUBLIC_PARCELS_REMOVED": len(parcel_ids[county]),
            "PRIVATE_PARCELS_REMOVED_BY_MANAGED_LAND": managed_removed_count,
            "PRIVATE_PARCELS_REMOVED_INVALID_GEOMETRY": len(invalid_ids[county]),
            "PRIVATE_PARCELS_REMOVED_BY_DISTANCE": max(
                0,
                source_count
                - len(parcel_ids[county])
                - retained_parcel_counts[county]
                - managed_removed_count
                - len(invalid_ids[county]),
            ),
            "MANAGED_FEATURE_COUNT": managed_counts[county],
            "kind": (
                "parcel-union-near-state-federal-land-minus-managed"
                if distance_miles is not None
                else "parcel-union-minus-managed"
            ),
        }
        if distance_miles is not None:
            properties["PUBLIC_BUFFER_DISTANCE_MILES"] = distance_miles
        features.append(
            {
                "type": "Feature",
                "properties": properties,
                "geometry": mapping(normalize(outline)),
            }
        )
        print(
            "{} overview: retained {:,} private parcels; removed {:,} public parcels; "
            "removed {:,} managed-only parcels; managed overlap {:.12g}".format(
                county,
                retained_parcel_counts[county],
                len(parcel_ids[county]),
                len(managed_removed_ids[county]),
                managed_overlap,
            ),
            flush=True,
        )

    overview = {"type": "FeatureCollection", "features": features}
    return json.dumps(overview, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n"


def filter_tiles(
    tile_root,
    private_status,
    invalid_ids,
    managed_only_ids,
    managed_geometries,
    prepared_managed_geometries,
    counties,
    write_tiles,
):
    removed_copies = {
        county: {"public": 0, "distance": 0, "managed": 0, "clipped": 0, "invalid": 0}
        for county in counties
    }
    removed_ids = {
        county: {
            "public": set(),
            "distance": set(),
            "managed": set(),
            "clipped": set(),
            "invalid": set(),
        }
        for county in counties
    }
    repaired_copies = {county: 0 for county in counties}
    repaired_ids = {county: set() for county in counties}
    clipped_copies = {county: 0 for county in counties}
    clipped_ids = {county: set() for county in counties}
    retained_geometry_batches = {county: [] for county in counties}
    retained_geometry_batch = {county: [] for county in counties}
    retained_ids = {county: set() for county in counties}
    managed_removed_ids = {county: set() for county in counties}
    for county in counties:
        tile_paths = sorted((tile_root / county).glob("*.json"))
        for path_index, tile_path in enumerate(tile_paths, start=1):
            collection = json.loads(tile_path.read_text(encoding="utf-8"))
            features = collection.get("features")
            if collection.get("type") != "FeatureCollection" or not isinstance(features, list):
                raise ValueError("Invalid GeoJSON tile: " + str(tile_path))
            kept_features = []
            tile_changed = False
            for feature in features:
                properties = feature.get("properties") or {}
                source_id = parcel_source_id(properties)
                if not source_id:
                    raise ValueError("Parcel missing SOURCE_OID in " + str(tile_path))
                if is_fl_public_parcel(properties):
                    removed_copies[county]["public"] += 1
                    removed_ids[county]["public"].add(source_id)
                    tile_changed = True
                    continue

                geometry_data = feature.get("geometry")
                if not geometry_data:
                    raise ValueError("Parcel missing geometry in " + str(tile_path))
                geometry, geometry_was_invalid = valid_parcel_polygon(
                    geometry_data, county + " private parcel " + source_id
                )
                if geometry is None:
                    removed_copies[county]["invalid"] += 1
                    removed_ids[county]["invalid"].add(source_id)
                    tile_changed = True
                    continue

                status = private_status[county].get(source_id)
                if status is None:
                    raise ValueError(
                        "Private parcel was not classified in " + str(tile_path)
                    )
                if not status and source_id not in managed_only_ids[county]:
                    removed_copies[county]["distance"] += 1
                    removed_ids[county]["distance"].add(source_id)
                    tile_changed = True
                    continue

                clipped_geometry = clip_parcel_geometry(
                    geometry,
                    managed_geometries[county],
                    prepared_managed_geometries[county],
                    county + " private parcel " + source_id,
                )
                if clipped_geometry is None:
                    removed_copies[county]["managed"] += 1
                    removed_ids[county]["managed"].add(source_id)
                    tile_changed = True
                    continue
                if not status:
                    removed_copies[county]["distance"] += 1
                    removed_ids[county]["distance"].add(source_id)
                    tile_changed = True
                    continue

                if geometry_was_invalid:
                    repaired_copies[county] += 1
                    repaired_ids[county].add(source_id)
                if clipped_geometry is not geometry:
                    feature["geometry"] = mapping(normalize(clipped_geometry))
                    clipped_copies[county] += 1
                    clipped_ids[county].add(source_id)
                    tile_changed = True
                elif geometry_was_invalid:
                    feature["geometry"] = mapping(normalize(geometry))
                    tile_changed = True

                kept_features.append(feature)
                retained_ids[county].add(source_id)
                retained_geometry_batch[county].extend(
                    polygon_parts(clipped_geometry)
                )
                if len(retained_geometry_batch[county]) >= PROXIMITY_BATCH_SIZE:
                    retained_geometry_batches[county].append(
                        unary_union(retained_geometry_batch[county])
                    )
                    retained_geometry_batch[county].clear()
            if tile_changed and write_tiles:
                if kept_features:
                    rewrite_tile(tile_path, kept_features)
                else:
                    tile_path.unlink()
            if path_index % 100 == 0 or path_index == len(tile_paths):
                print(
                    "{} {}: {}/{} tiles ({:.0%})".format(
                        county,
                        "rewrite" if write_tiles else "check",
                        path_index,
                        len(tile_paths),
                        path_index / len(tile_paths),
                    ),
                    flush=True,
                )

        if retained_geometry_batch[county]:
            retained_geometry_batches[county].append(
                unary_union(retained_geometry_batch[county])
            )
        managed_removed_ids[county] = (
            removed_ids[county]["managed"]
            - retained_ids[county]
            - invalid_ids[county]
        )

    return (
        removed_copies,
        removed_ids,
        repaired_copies,
        repaired_ids,
        clipped_copies,
        clipped_ids,
        retained_geometry_batches,
        retained_ids,
        managed_removed_ids,
    )


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
        "--all-private",
        action="store_true",
        help="retain all nonpublic parcels without proximity filtering",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="scan and validate the filtered data without changing tiles or overview",
    )
    args = parser.parse_args()
    counties = tuple(dict.fromkeys(args.counties))
    distance_miles = None if args.all_private else args.distance_miles
    managed_geometries, managed_counts = load_managed_geometries(args.managed)
    managed_clip_geometries = {
        county: valid_polygonal(
            geometry.buffer(MANAGED_EDGE_CLEARANCE_DEGREES),
            county + " managed-land clearance boundary",
        )
        for county, geometry in managed_geometries.items()
    }
    prepared_managed_geometries = {
        county: prep(managed_clip_geometries[county]) for county in counties
    }
    if distance_miles is None:
        public_buffers = None
        print("Private-parcel proximity filter disabled", flush=True)
    else:
        public_buffers, public_feature_counts = load_public_land_buffers(
            args.managed,
            counties,
            distance_miles,
            managed_names_by_county={"lee": LEE_BONITA_MANAGED_NAMES},
        )
        print(
            "Public-managed features: {}; buffer distance: {:.3f} miles".format(
                ", ".join(
                    "{} {:,}".format(county, public_feature_counts[county])
                    for county in counties
                ),
                distance_miles,
            ),
            flush=True,
        )
    (
        parcel_ids,
        private_status,
        invalid_ids,
        invalid_copy_ids,
        managed_only_ids,
        counts,
    ) = gather_filter_plan(
        args.tile_root,
        public_buffers,
        managed_clip_geometries,
        prepared_managed_geometries,
        counties,
    )
    (
        tile_copies,
        tile_ids,
        repaired_copies,
        repaired_ids,
        clipped_copies,
        clipped_ids,
        retained_geometry_batches,
        retained_ids,
        managed_removed_ids,
    ) = filter_tiles(
        args.tile_root,
        private_status,
        invalid_ids,
        managed_only_ids,
        managed_clip_geometries,
        prepared_managed_geometries,
        counties,
        write_tiles=not args.check,
    )
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
            and source_id not in invalid_ids[county]
            and source_id not in managed_only_ids[county]
        }
        if tile_ids[county]["distance"] != expected_far_ids:
            raise ValueError(
                "{} proximity scan found {} unique distant IDs, rewrite found {}".format(
                    county,
                    len(expected_far_ids),
                    len(tile_ids[county]["distance"]),
                )
            )
        if tile_ids[county]["invalid"] != invalid_copy_ids[county]:
            raise ValueError(
                "{} scan found {} invalid parcel IDs, rewrite found {}".format(
                    county,
                    len(invalid_copy_ids[county]),
                    len(tile_ids[county]["invalid"]),
                )
            )
        expected_retained_ids = {
            source_id
            for source_id, is_near in private_status[county].items()
            if is_near
        } - managed_removed_ids[county]
        if retained_ids[county] != expected_retained_ids:
            raise ValueError(
                "{} scan found {} retained parcels, rewrite found {}".format(
                    county,
                    len(expected_retained_ids),
                    len(retained_ids[county]),
                )
            )
        print(
            "{}: retained {:,}/{:,} unique private parcels; clipped {:,} tile copies; "
            "removed {:,} distant copies, {:,} fully managed copies, {:,} public copies, "
            "and {:,} invalid copies; repaired {:,} geometry copies".format(
                county,
                len(retained_ids[county]),
                len(private_status[county]),
                clipped_copies[county],
                tile_copies[county]["distance"],
                tile_copies[county]["managed"],
                tile_copies[county]["public"],
                tile_copies[county]["invalid"],
                repaired_copies[county],
            ),
            flush=True,
        )

    overview_contents = build_filtered_overview(
        managed_clip_geometries,
        managed_counts,
        parcel_ids,
        private_status,
        invalid_ids,
        retained_geometry_batches,
        {county: len(retained_ids[county]) for county in counties},
        managed_removed_ids,
        distance_miles,
        counties,
    )

    if args.check:
        print("Dry run complete; tiles and overview were not changed", flush=True)
        return 0

    write_atomic(args.overview, overview_contents)
    if distance_miles is None:
        print("Updated public-parcel exclusions and managed-land clipping", flush=True)
    else:
        print(
            "Updated proximity, public-parcel exclusions, and managed-land clipping",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())