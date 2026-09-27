import json
import tempfile
import unittest
from pathlib import Path

from build_fl_parcel_tiles import is_fl_public_parcel
from filter_fl_public_parcel_tiles import (
    build_filtered_overview,
    filter_tiles,
    gather_filter_plan,
)
from subtract_fl_managed_lands import load_managed_geometries
from shapely.geometry import box, mapping, shape
from shapely.ops import unary_union
from shapely.prepared import prep


class FloridaParcelSourceEdgeTests(unittest.TestCase):
    def test_south_florida_water_management_owner_alias_is_public(self):
        properties = (
            {
                "OWN_NAME": "SOUTH FLORIDA WATER MANAGEMENT",
                "OWN_ADDR1": "PO BOX 24680",
            },
            {
                "OWN_NAME": "SOUTH FLORIDA WATER MANAGEMENT",
                "OWN_ADDR1": "DISTRICT",
            },
        )
        for parcel_properties in properties:
            with self.subTest(address=parcel_properties["OWN_ADDR1"]):
                self.assertTrue(is_fl_public_parcel(parcel_properties))

        self.assertFalse(
            is_fl_public_parcel(
                {"OWN_NAME": "SOUTH FLORIDA WATER MANAGEMENT LLC"}
            )
        )

    def test_managed_feature_is_assigned_to_each_listed_county(self):
        collection = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"COUNTY": "Collier, Lee"},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [
                                [-81.1, 26.0],
                                [-81.0, 26.0],
                                [-81.0, 26.1],
                                [-81.1, 26.1],
                                [-81.1, 26.0],
                            ]
                        ],
                    },
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "managed.json"
            path.write_text(json.dumps(collection), encoding="utf-8")
            geometries, counts = load_managed_geometries(path)

        self.assertEqual(counts, {"collier": 1, "lee": 1})
        self.assertTrue(geometries["collier"].equals(geometries["lee"]))

    def test_all_private_mode_keeps_full_private_parcels_and_removes_public(self):
        private_geometry = box(-81.01, 26.01, -81.0, 26.02)
        public_geometry = box(-81.04, 26.01, -81.03, 26.02)
        managed_geometry = box(-81.1, 26.0, -80.9, 26.1)
        collection = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "SOURCE_OID": "private-1",
                        "OWN_NAME": "SOUTH FLORIDA WATER MANAGEMENT LLC",
                    },
                    "geometry": mapping(private_geometry),
                },
                {
                    "type": "Feature",
                    "properties": {
                        "SOURCE_OID": "public-1",
                        "OWN_NAME": "SOUTH FLORIDA WATER MANAGEMENT",
                    },
                    "geometry": mapping(public_geometry),
                },
            ],
        }
        counties = ("collier",)
        managed_geometries = {"collier": managed_geometry}
        prepared_managed_geometries = {"collier": prep(managed_geometry)}

        with tempfile.TemporaryDirectory() as directory:
            tile_root = Path(directory)
            tile_dir = tile_root / "collier"
            tile_dir.mkdir()
            (tile_dir / "-4051_1300.json").write_text(
                json.dumps(collection), encoding="utf-8"
            )

            plan = gather_filter_plan(
                tile_root,
                None,
                managed_geometries,
                prepared_managed_geometries,
                counties,
                clip_managed=False,
            )
            filtered = filter_tiles(
                tile_root,
                plan[1],
                plan[2],
                plan[4],
                managed_geometries,
                prepared_managed_geometries,
                counties,
                write_tiles=False,
                clip_managed=False,
            )
            overview = json.loads(
                build_filtered_overview(
                    managed_geometries,
                    {"collier": 1},
                    plan[0],
                    plan[1],
                    plan[2],
                    filtered[6],
                    {"collier": 1},
                    filtered[8],
                    None,
                    counties,
                    clip_managed=False,
                )
            )

        self.assertEqual(plan[0]["collier"], {"public-1"})
        self.assertEqual(filtered[1]["collier"]["public"], {"public-1"})
        self.assertEqual(filtered[7]["collier"], {"private-1"})
        self.assertEqual(filtered[8]["collier"], set())
        self.assertTrue(
            unary_union(filtered[6]["collier"]).equals(private_geometry)
        )
        self.assertEqual(overview["features"][0]["properties"]["kind"], "private-parcel-union")
        self.assertTrue(shape(overview["features"][0]["geometry"]).equals(private_geometry))


if __name__ == "__main__":
    unittest.main()