"""Rebuild Florida parcel tiles from county and property-appraiser sources.

Run from the repository root with the workspace Python environment. Parcel
geometries are preserved as individual features and copied into each 0.02-degree
tile touched by their bounds. No parcels are dissolved, simplified, or clipped.
"""

import argparse
import csv
import io
import json
import math
import os
import re
import shutil
import tempfile
import time
import zipfile
from collections import OrderedDict
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

import shapefile


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TILE_ROOT = ROOT / "tiles"
TILE_SIZE = 0.02
BATCH_SIZE = 1000
MAX_OPEN_TILE_FILES = 64
REQUEST_TIMEOUT = 120
REQUEST_ATTEMPTS = 5

COLLIER_GEOMETRY_URL = (
    "https://www.collierappraiser.com/Main_Data/downloadgdfile.asp?"
    "folderName=GIS%20(Shape%20files)&file=parcel_polygon_shape_file.zip"
)
COLLIER_ROLL_URL = (
    "https://www.collierappraiser.com/Main_Data/downloadgdfile.asp?"
    "folderName=INT%20FILES%20(NEW)&file=int_parcels_csv.zip"
)
COUNTY_NAMES = ("collier", "lee")

COUNTY_SOURCES = {
    "lee": {
        "query_url": (
            "https://services2.arcgis.com/LvWGAAhHwbCJ2GMP/arcgis/rest/services/"
            "Lee_County_Parcels/FeatureServer/0/query"
        ),
        "object_id_field": "OBJECTID",
        "source_fields": (
            "STRAP",
            "GISACRES",
            "SITEADDR",
            "SITECITY",
            "SITEZIP",
            "O_NAME",
            "O_ADDR1",
            "O_CITY",
            "O_STATE",
            "O_ZIP",
            "DORCODE",
        ),
        "normalized_fields": {
            "PARCEL_NO": "STRAP",
            "OWN_NAME": "O_NAME",
            "GIS_ACRES": "GISACRES",
            "MAIL_ADDR1": "O_ADDR1",
            "MAIL_CITY": "O_CITY",
            "MAIL_STATE": "O_STATE",
            "MAIL_ZIP": "O_ZIP",
            "DOR_UC": "DORCODE",
        },
    },
}


def request_json(url, parameters):
    request = Request(
        url,
        data=urlencode(parameters).encode("utf-8"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "FloridaParcelTileBuilder/1.0",
        },
    )

    for attempt in range(REQUEST_ATTEMPTS):
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                data = json.loads(response.read().decode("utf-8"))
            if isinstance(data, dict) and data.get("error"):
                raise RuntimeError("ArcGIS error: " + json.dumps(data["error"]))
            return data
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            if attempt + 1 == REQUEST_ATTEMPTS:
                raise RuntimeError("ArcGIS request failed: " + str(error)) from error
            time.sleep(min(30, 2**attempt))

    raise RuntimeError("ArcGIS request failed after retries")


class GoogleDriveDownloadFormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.action = None
        self.fields = {}
        self.in_form = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "form" and self.action is None:
            self.action = attributes.get("action")
            self.in_form = bool(self.action)
        elif tag == "input" and self.in_form:
            name = attributes.get("name")
            value = attributes.get("value")
            if name and value is not None:
                self.fields[name] = value

    def handle_endtag(self, tag):
        if tag == "form":
            self.in_form = False


def download_official_archive(url, destination):
    headers = {"User-Agent": "FloridaParcelTileBuilder/1.0"}
    request = Request(url, headers=headers)
    destination.parent.mkdir(parents=True, exist_ok=True)

    with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        if response.headers.get_content_type() == "text/html":
            parser = GoogleDriveDownloadFormParser()
            parser.feed(response.read(1_000_000).decode("utf-8", errors="replace"))
            if not parser.action or not parser.fields:
                raise RuntimeError("Official parcel download did not return a ZIP file")

            action = urljoin(response.geturl(), parser.action)
            separator = "&" if "?" in action else "?"
            confirmed_request = Request(
                action + separator + urlencode(parser.fields),
                headers=headers,
            )
            with urlopen(confirmed_request, timeout=REQUEST_TIMEOUT) as download:
                if download.headers.get_content_type() == "text/html":
                    raise RuntimeError("Confirmed parcel download returned HTML instead of a ZIP")
                with destination.open("wb") as output:
                    shutil.copyfileobj(download, output)
        else:
            with destination.open("wb") as output:
                shutil.copyfileobj(response, output)

    if not zipfile.is_zipfile(destination):
        raise RuntimeError("Official parcel download is not a valid ZIP file")


def load_collier_tax_roll(archive_path):
    with zipfile.ZipFile(archive_path) as archive:
        csv_files = [
            name
            for name in archive.namelist()
            if name.replace("\\", "/").rsplit("/", 1)[-1].casefold() == "int_parcels.csv"
        ]
        if len(csv_files) != 1:
            raise RuntimeError("Tax-roll ZIP must contain exactly one int_parcels.csv file")

        with archive.open(csv_files[0]) as binary:
            text = io.TextIOWrapper(binary, encoding="utf-8-sig", newline="")
            reader = csv.DictReader(text)
            required_fields = {
                "Folio",
                "OwnerLine1",
                "OwnerLine2",
                "OwnerLine3",
                "OwnerLine4",
                "OwnerLine5",
                "OwnerCity",
                "OwnerState",
                "OwnerZip",
                "OwnerZipPlus4",
                "UseCode",
                "TotalAcres",
            }
            missing_fields = required_fields.difference(reader.fieldnames or ())
            if missing_fields:
                raise RuntimeError(
                    "Tax-roll CSV is missing fields: " + ", ".join(sorted(missing_fields))
                )

            parcels = {}
            for row in reader:
                folio = (row.get("Folio") or "").strip()
                if not folio:
                    continue
                if folio in parcels:
                    raise RuntimeError("Tax-roll CSV contains duplicate Folio " + folio)

                def value(field):
                    field_value = (row.get(field) or "").strip()
                    return field_value or None

                mailing_address = " ".join(
                    field_value
                    for field_value in (value("OwnerLine2"), value("OwnerLine3"), value("OwnerLine4"), value("OwnerLine5"))
                    if field_value
                )
                zip_code = value("OwnerZip")
                zip_plus4 = value("OwnerZipPlus4")
                if zip_code and zip_plus4 and zip_plus4 != "0":
                    zip_code += "-" + zip_plus4

                parcels[folio] = {
                    "PARCELID": folio,
                    "PARCEL_NO": folio,
                    "OWN_NAME": value("OwnerLine1"),
                    "MAIL_ADDR1": mailing_address or None,
                    "MAIL_CITY": value("OwnerCity"),
                    "MAIL_STATE": value("OwnerState"),
                    "MAIL_ZIP": zip_code,
                    "DOR_UC": value("UseCode"),
                    "GIS_ACRES": value("TotalAcres"),
                }

    if not parcels:
        raise RuntimeError("Tax-roll ZIP contains no parcel records")
    return parcels


def resolve_collier_archive(provided_path, url, filename, staging_root):
    if provided_path is not None:
        archive_path = provided_path.resolve()
        if not archive_path.is_file():
            raise RuntimeError("Parcel source archive does not exist: " + str(archive_path))
        return archive_path

    archive_path = staging_root / filename
    print("collier: downloading official " + filename, flush=True)
    download_official_archive(url, archive_path)
    return archive_path


def get_object_ids(source):
    response = request_json(
        source["query_url"],
        {
            "where": "1=1",
            "returnIdsOnly": "true",
            "returnGeometry": "false",
            "f": "json",
        },
    )
    object_ids = response.get("objectIds")
    if not isinstance(object_ids, list) or not object_ids:
        raise RuntimeError("ArcGIS did not return a parcel object ID list")
    if len(set(object_ids)) != len(object_ids):
        raise RuntimeError("ArcGIS returned duplicate parcel object IDs")
    return sorted(object_ids, key=int)


def fetch_feature_batch(source, object_ids, out_fields):
    response = request_json(
        source["query_url"],
        {
            "objectIds": ",".join(str(object_id) for object_id in object_ids),
            "outFields": out_fields,
            "returnGeometry": "true",
            "outSR": "4326",
            "f": "geojson",
        },
    )
    features = response.get("features")
    if not isinstance(features, list):
        raise RuntimeError("ArcGIS parcel query did not return GeoJSON features")
    if response.get("exceededTransferLimit") or len(features) != len(object_ids):
        if len(object_ids) == 1:
            raise RuntimeError(
                "ArcGIS could not return parcel object ID " + str(object_ids[0])
            )
        midpoint = len(object_ids) // 2
        return (
            fetch_feature_batch(source, object_ids[:midpoint], out_fields)
            + fetch_feature_batch(source, object_ids[midpoint:], out_fields)
        )
    return features


def geometry_bounds(geometry):
    if not geometry or geometry.get("type") not in ("Polygon", "MultiPolygon"):
        raise ValueError("Parcel is missing a polygon geometry")

    coordinates = geometry.get("coordinates")
    if not coordinates:
        raise ValueError("Parcel has empty polygon coordinates")

    minimum_x = math.inf
    minimum_y = math.inf
    maximum_x = -math.inf
    maximum_y = -math.inf
    stack = [coordinates]

    while stack:
        value = stack.pop()
        if (
            isinstance(value, (list, tuple))
            and len(value) >= 2
            and isinstance(value[0], (int, float))
            and isinstance(value[1], (int, float))
        ):
            x, y = value[0], value[1]
            if not math.isfinite(x) or not math.isfinite(y):
                raise ValueError("Parcel geometry contains a non-finite coordinate")
            minimum_x = min(minimum_x, x)
            minimum_y = min(minimum_y, y)
            maximum_x = max(maximum_x, x)
            maximum_y = max(maximum_y, y)
        elif isinstance(value, (list, tuple)):
            stack.extend(value)

    if not math.isfinite(minimum_x):
        raise ValueError("Parcel has no coordinates")
    if not (-90 <= minimum_x <= maximum_x <= -70 and 20 <= minimum_y <= maximum_y <= 35):
        raise ValueError("Parcel coordinates are outside the Florida longitude/latitude range")

    return minimum_x, minimum_y, maximum_x, maximum_y


def tile_indices(bounds):
    minimum_x, minimum_y, maximum_x, maximum_y = bounds
    minimum_tx = math.floor(minimum_x / TILE_SIZE)
    minimum_ty = math.floor(minimum_y / TILE_SIZE)
    maximum_tx = math.floor(maximum_x / TILE_SIZE)
    maximum_ty = math.floor(maximum_y / TILE_SIZE)
    tile_count = (maximum_tx - minimum_tx + 1) * (maximum_ty - minimum_ty + 1)
    if tile_count > 10000:
        raise ValueError("Parcel geometry spans an unexpectedly large number of tiles")
    for tx in range(minimum_tx, maximum_tx + 1):
        for ty in range(minimum_ty, maximum_ty + 1):
            yield tx, ty


class TileWriter:
    def __init__(self, output_dir):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.handles = OrderedDict()
        self.counts = {}
        self.paths = {}

    def write(self, tile, serialized_feature):
        handle = self.handles.pop(tile, None)
        if handle is None:
            if len(self.handles) >= MAX_OPEN_TILE_FILES:
                _, oldest_handle = self.handles.popitem(last=False)
                oldest_handle.close()

            tx, ty = tile
            path = self.output_dir / (str(tx) + "_" + str(ty) + ".json")
            is_new = tile not in self.counts
            handle = path.open(
                "w" if is_new else "a",
                encoding="utf-8",
                newline="\n",
            )
            self.paths[tile] = path
            if is_new:
                handle.write('{"type":"FeatureCollection","features":[')
                self.counts[tile] = 0
        self.handles[tile] = handle

        if self.counts[tile]:
            handle.write(",")
        handle.write(serialized_feature)
        self.counts[tile] += 1

    def close_handles(self):
        while self.handles:
            _, handle = self.handles.popitem()
            handle.close()

    def finalize(self):
        self.close_handles()
        for path in self.paths.values():
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write("]}\n")


def is_fl_public_parcel(properties):
    owner_fields = (
        "OWN_NAME",
        "O_NAME",
        "NAME1",
        "OWNER",
        "OWNER_NAME",
        "OWNERNAME",
        "Owner1",
    )
    address_fields = ("OWN_ADDR1", "O_ADDR1", "MAIL_ADDR1")
    owners = [str(properties.get(field) or "") for field in owner_fields]
    addresses = [str(properties.get(field) or "") for field in address_fields]

    def normalize(values):
        return " " + re.sub(r"[^A-Z0-9]+", " ", " ".join(values).upper()).strip() + " "

    owner = normalize(owners)
    combined = normalize(owners + addresses)
    if owner == " USA ":
        return True
    if any(term in owner for term in (
        " SOUTH FL WATER MGMT DIST ",
        " SOUTH FLORIDA WATER MANAGEMENT DISTRICT ",
    )):
        return True

    public_terms = (
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
    federal_agencies = (
        "NATIONAL PARK SERVICE",
        "NATIONAL PARKS SERVICE",
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
    if " USA " in owner and any(" " + term + " " in combined for term in federal_agencies):
        return True
    return any(" " + term + " " in combined for term in public_terms)


def normalized_properties(attributes, source):
    properties = dict(attributes)
    for target_field, source_field in source["normalized_fields"].items():
        properties[target_field] = attributes.get(source_field)
    properties["SOURCE_OID"] = attributes.get(source["object_id_field"])
    return properties


def validate_tiles(output_dir, expected_count):
    tile_paths = sorted(output_dir.glob("*.json"))
    if not tile_paths:
        raise RuntimeError("No parcel tile files were generated in " + str(output_dir))

    unique_ids = set()
    copied_feature_count = 0
    total_bytes = 0

    for tile_path in tile_paths:
        collection = json.loads(tile_path.read_text(encoding="utf-8"))
        if collection.get("type") != "FeatureCollection":
            raise RuntimeError("Invalid GeoJSON tile: " + str(tile_path))
        features = collection.get("features")
        if not isinstance(features, list) or not features:
            raise RuntimeError("Empty or invalid GeoJSON tile: " + str(tile_path))

        for feature in features:
            properties = feature.get("properties") or {}
            source_id = properties.get("SOURCE_OID")
            geometry = feature.get("geometry") or {}
            if source_id is None:
                raise RuntimeError("Parcel tile is missing its source object ID: " + str(tile_path))
            if geometry.get("type") not in ("Polygon", "MultiPolygon") or not geometry.get("coordinates"):
                raise RuntimeError("Parcel tile contains an invalid polygon: " + str(tile_path))
            unique_ids.add(str(source_id))

        copied_feature_count += len(features)
        total_bytes += tile_path.stat().st_size

    if len(unique_ids) != expected_count:
        raise RuntimeError(
            "Tile validation found {} unique source parcels; expected {}".format(
                len(unique_ids), expected_count
            )
        )
    if copied_feature_count < expected_count:
        raise RuntimeError("Tile validation found fewer features than the source count")

    return len(tile_paths), copied_feature_count, total_bytes


def build_county(county, source, staging_root):
    object_ids = get_object_ids(source)
    expected_count = len(object_ids)
    print("{}: downloading {:,} county parcel features".format(county, expected_count), flush=True)

    out_fields = ",".join(
        (source["object_id_field"],) + source["source_fields"]
    )
    writer = TileWriter(staging_root / county)
    downloaded = 0
    excluded_public = 0
    try:
        for offset in range(0, expected_count, BATCH_SIZE):
            batch_ids = object_ids[offset : offset + BATCH_SIZE]
            features = fetch_feature_batch(source, batch_ids, out_fields)
            for feature in features:
                geometry = feature.get("geometry")
                bounds = geometry_bounds(geometry)
                attributes = feature.get("properties") or {}
                output_feature = dict(feature)
                output_feature["properties"] = normalized_properties(attributes, source)
                if is_fl_public_parcel(output_feature["properties"]):
                    excluded_public += 1
                    downloaded += 1
                    continue
                serialized_feature = json.dumps(
                    output_feature,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
                for tile in tile_indices(bounds):
                    writer.write(tile, serialized_feature)
                downloaded += 1

            batch_number = offset // BATCH_SIZE + 1
            if batch_number % 20 == 0 or offset + len(batch_ids) >= expected_count:
                print(
                    "{}: downloaded {:,}/{:,} features across {:,} tiles".format(
                        county, downloaded, expected_count, len(writer.counts)
                    ),
                    flush=True,
                )
    except BaseException:
        writer.close_handles()
        raise

    writer.finalize()
    if downloaded != expected_count:
        raise RuntimeError(
            "{}: downloaded {} features; expected {}".format(
                county, downloaded, expected_count
            )
        )

    tile_count, copied_count, total_bytes = validate_tiles(
        staging_root / county,
        expected_count - excluded_public,
    )
    print(
        "{}: validated {:,} private parcels; excluded {:,} public records in {:,} tiles "
        "({:,} tile features, {:,} bytes)".format(
            county,
            expected_count - excluded_public,
            excluded_public,
            tile_count,
            copied_count,
            total_bytes,
        ),
        flush=True,
    )


def build_collier(staging_root, geometry_archive_path=None, roll_archive_path=None):
    geometry_archive_path = resolve_collier_archive(
        geometry_archive_path,
        COLLIER_GEOMETRY_URL,
        "collier-parcel-polygons.zip",
        staging_root,
    )
    roll_archive_path = resolve_collier_archive(
        roll_archive_path,
        COLLIER_ROLL_URL,
        "collier-parcel-roll.zip",
        staging_root,
    )
    tax_roll = load_collier_tax_roll(roll_archive_path)
    print("collier: loaded {:,} official tax-roll records".format(len(tax_roll)), flush=True)

    with zipfile.ZipFile(geometry_archive_path) as archive:
        members = {
            name.replace("\\", "/").rsplit("/", 1)[-1].casefold(): name
            for name in archive.namelist()
        }
        component_names = {
            extension: members.get("parcel_polygon" + extension)
            for extension in (".shp", ".shx", ".dbf", ".prj")
        }
        if any(name is None for name in component_names.values()):
            raise RuntimeError("Official Collier geometry ZIP is missing shapefile components")

        projection = archive.read(component_names[".prj"]).decode("ascii", errors="ignore").upper()
        if not any(token in projection for token in ("WGS_1984", "WGS 84", "WGS84")):
            raise RuntimeError("Official Collier parcel shapefile is not in WGS84 coordinates")

        with archive.open(component_names[".shp"]) as shp, archive.open(
            component_names[".shx"]
        ) as shx, archive.open(component_names[".dbf"]) as dbf:
            reader = shapefile.Reader(shp=shp, shx=shx, dbf=dbf, encoding="latin1")
            source_fields = (
                "FLN",
                "SECTION_",
                "TOWNSHIP",
                "RANGE",
                "CALC_ACRE",
                "PARCELTYPE",
                "GlobalID",
            )
            available_fields = {field[0] for field in reader.fields[1:]}
            missing_fields = set(source_fields).difference(available_fields)
            if missing_fields:
                raise RuntimeError(
                    "Official Collier shapefile is missing fields: "
                    + ", ".join(sorted(missing_fields))
                )
            if reader.shapeType != shapefile.POLYGON:
                raise RuntimeError("Official Collier shapefile does not contain polygon geometry")

            expected_count = reader.numRecords
            output_dir = staging_root / "collier"
            writer = TileWriter(output_dir)
            downloaded = 0
            processed = 0
            excluded_public = 0
            matched_features = 0
            unmatched_features = 0
            unique_flns = set()
            try:
                records = reader.iterRecords(fields=source_fields)
                for record, source_geometry in zip(records, reader.iterShapes()):
                    properties = dict(zip(source_fields, record))
                    source_id = str(properties.get("GlobalID") or "").strip()
                    if not source_id:
                        raise RuntimeError("Official Collier feature is missing its GlobalID")

                    folio = str(properties.get("FLN") or "").strip()
                    if folio:
                        unique_flns.add(folio)
                    geometry = source_geometry.__geo_interface__
                    bounds = geometry_bounds(geometry)
                    properties["SOURCE_OID"] = source_id
                    properties["PARCELID"] = folio or None
                    properties["PARCEL_NO"] = folio or None
                    properties["GIS_ACRES"] = properties.get("CALC_ACRE")

                    tax_record = tax_roll.get(folio)
                    if tax_record:
                        properties.update(tax_record)
                        matched_features += 1
                    else:
                        unmatched_features += 1

                    processed += 1
                    if is_fl_public_parcel(properties):
                        excluded_public += 1
                        if processed % 20000 == 0 or processed == expected_count:
                            print(
                                "collier: processed {:,}/{:,} polygons; excluded {:,} public parcels".format(
                                    processed, expected_count, excluded_public
                                ),
                                flush=True,
                            )
                        continue

                    output_feature = {
                        "type": "Feature",
                        "geometry": geometry,
                        "properties": properties,
                    }
                    serialized_feature = json.dumps(
                        output_feature,
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                    )
                    for tile in tile_indices(bounds):
                        writer.write(tile, serialized_feature)
                    downloaded += 1

                    if processed % 20000 == 0 or processed == expected_count:
                        print(
                            "collier: processed {:,}/{:,} polygons; wrote {:,}, "
                            "excluded {:,} public across {:,} tiles".format(
                                processed,
                                expected_count,
                                downloaded,
                                excluded_public,
                                len(writer.counts),
                            ),
                            flush=True,
                        )
            except BaseException:
                writer.close_handles()
                raise

            writer.finalize()

    if downloaded + excluded_public != expected_count:
        raise RuntimeError(
            "collier: processed {} polygons; expected {}".format(
                downloaded + excluded_public, expected_count
            )
        )

    matched_flns = sum(1 for folio in unique_flns if folio in tax_roll)
    match_ratio = matched_flns / len(unique_flns) if unique_flns else 0
    if match_ratio < 0.95:
        raise RuntimeError(
            "collier: only {:.1%} of parcel FLNs matched the official tax roll".format(
                match_ratio
            )
        )
    print(
        "collier: matched {:,} of {:,} distinct FLNs to tax-roll owners; "
        "{: ,} polygon records lack a roll match".format(
            matched_flns, len(unique_flns), unmatched_features
        ),
        flush=True,
    )

    tile_count, copied_count, total_bytes = validate_tiles(
        output_dir, expected_count - excluded_public
    )
    print(
        "collier: validated {:,} private polygons; excluded {:,} public parcels in {:,} tiles "
        "({:,} tile features, {:,} bytes)".format(
            expected_count - excluded_public,
            excluded_public,
            tile_count,
            copied_count,
            total_bytes,
        ),
        flush=True,
    )


def remove_path(path):
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def install_staged_counties(staging_root, tile_root, counties):
    backed_up = []
    installed = []
    try:
        for county in counties:
            target = tile_root / county
            staged = staging_root / county
            backup = staging_root / ("previous-" + county)
            if target.exists():
                os.replace(target, backup)
                backed_up.append(county)
            os.replace(staged, target)
            installed.append(county)
    except BaseException:
        for county in reversed(installed):
            remove_path(tile_root / county)
        for county in reversed(backed_up):
            backup = staging_root / ("previous-" + county)
            if backup.exists():
                os.replace(backup, tile_root / county)
        raise

    for county in backed_up:
        shutil.rmtree(staging_root / ("previous-" + county), ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--counties",
        nargs="+",
        choices=COUNTY_NAMES,
        default=list(COUNTY_NAMES),
        help="county tile sets to replace (default: collier lee)",
    )
    parser.add_argument(
        "--collier-geometry-zip",
        type=Path,
        help="use a local official Collier parcel polygon ZIP instead of downloading it",
    )
    parser.add_argument(
        "--collier-roll-zip",
        type=Path,
        help="use a local official Collier parcel roll ZIP instead of downloading it",
    )
    parser.add_argument(
        "--tile-root",
        type=Path,
        default=DEFAULT_TILE_ROOT,
        help="tile directory containing the county subfolders",
    )
    args = parser.parse_args()
    tile_root = args.tile_root.resolve()
    tile_root.mkdir(parents=True, exist_ok=True)
    counties = list(dict.fromkeys(args.counties))
    staging_root = Path(
        tempfile.mkdtemp(prefix=".florida-parcels-stage-", dir=str(tile_root))
    )

    try:
        for county in counties:
            if county == "collier":
                build_collier(
                    staging_root,
                    args.collier_geometry_zip,
                    args.collier_roll_zip,
                )
            else:
                build_county(county, COUNTY_SOURCES[county], staging_root)
        install_staged_counties(staging_root, tile_root, counties)
    except BaseException:
        if not any(staging_root.glob("previous-*")):
            shutil.rmtree(staging_root, ignore_errors=True)
        else:
            print("Previous tile folders were retained at " + str(staging_root))
        raise
    else:
        shutil.rmtree(staging_root, ignore_errors=True)
        print("Installed Florida parcel tiles for: " + ", ".join(counties))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())