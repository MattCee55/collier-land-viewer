"""Build public-land proximity buffers for Florida parcel filtering."""

import json
import math
from pathlib import Path

from pyproj import Transformer
from shapely import make_valid
from shapely.geometry import shape
from shapely.ops import transform, unary_union
from shapely.prepared import prep


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANAGED_PATH = ROOT / "flma" / "collier_lee_fire.json"
DEFAULT_PUBLIC_DISTANCE_MILES = 0.25
METERS_PER_MILE = 1609.344
TO_UTM = Transformer.from_crs("EPSG:4326", "EPSG:26917", always_xy=True).transform
FROM_UTM = Transformer.from_crs("EPSG:26917", "EPSG:4326", always_xy=True).transform


def polygon_parts(geometry):
    if geometry.is_empty:
        return []
    if geometry.geom_type == "Polygon":
        return [geometry]
    if hasattr(geometry, "geoms"):
        return [polygon for part in geometry.geoms for polygon in polygon_parts(part)]
    return []


def has_state_or_federal_ownership_type(properties):
    owner_types = {
        value.strip().upper()
        for value in str(properties.get("OWNERTYPES") or "").split(",")
    }
    return any(
        value in ("S", "F") or value.startswith("F-")
        for value in owner_types
    )


def load_public_land_buffers(
    managed_path=DEFAULT_MANAGED_PATH,
    counties=("collier", "lee"),
    distance_miles=DEFAULT_PUBLIC_DISTANCE_MILES,
):
    if not math.isfinite(distance_miles) or distance_miles <= 0:
        raise ValueError("Public-land buffer distance must be positive")

    collection = json.loads(Path(managed_path).read_text(encoding="utf-8"))
    if collection.get("type") != "FeatureCollection":
        raise ValueError("Managed-land source is not a FeatureCollection")

    county_names = tuple(str(county).casefold() for county in counties)
    geometries = {county: [] for county in county_names}
    feature_counts = {county: 0 for county in county_names}
    for feature in collection.get("features", []):
        properties = feature.get("properties") or {}
        feature_counties = {
            value.strip().casefold()
            for value in str(properties.get("COUNTY") or "").split(",")
        }
        matching_counties = feature_counties.intersection(county_names)
        if not matching_counties:
            continue
        if not has_state_or_federal_ownership_type(properties):
            continue

        geometry_data = feature.get("geometry")
        if not geometry_data:
            raise ValueError("Public managed-land feature is missing geometry")
        geometry = shape(geometry_data)
        if not geometry.is_valid:
            geometry = make_valid(geometry)
        parts = polygon_parts(geometry)
        if not parts:
            raise ValueError("Public managed-land feature contains no polygon geometry")
        projected = transform(TO_UTM, unary_union(parts))
        for county in matching_counties:
            geometries[county].append(projected)
            feature_counts[county] += 1

    buffers = {}
    radius_meters = distance_miles * METERS_PER_MILE
    for county, public_geometries in geometries.items():
        if not public_geometries:
            raise ValueError("No public managed-land features found for " + county)
        public_union = unary_union(public_geometries)
        metric_buffer = public_union.buffer(radius_meters, quad_segs=16)
        geographic_buffer = transform(FROM_UTM, metric_buffer)
        if geographic_buffer.is_empty or not geographic_buffer.is_valid:
            raise ValueError("Invalid public-land buffer for " + county)
        buffers[county] = prep(geographic_buffer)
    return buffers, feature_counts


def parcel_intersects_public_buffer(geometry_data, public_buffer):
    if not geometry_data:
        raise ValueError("Parcel feature is missing geometry")
    geometry = shape(geometry_data)
    if geometry.is_empty:
        return False
    if not geometry.is_valid:
        geometry = make_valid(geometry)
    return public_buffer.intersects(geometry)