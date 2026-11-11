import copy
import io
from pathlib import Path
import unittest

from prometheus.data import load_dataset, validate_dataset
from prometheus.ranking import rank_candidates

ROOT = Path(__file__).resolve().parents[1]


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.data = load_dataset(ROOT / "data" / "demo.json")

    def test_demo_rankings_change_by_region(self):
        winners = [rank_candidates(region, self.data["earth"])[0]["site"]["id"]
                   for region in self.data["mars"]]
        self.assertEqual(winners, ["atacama", "cotopaxi", "lanzarote"])

    def test_known_distance_and_identity(self):
        target = self.data["mars"][0]
        result = rank_candidates(target, self.data["earth"])[0]
        self.assertAlmostEqual(result["distance"], (100 / 1000 + 1 / 15 + 5 / 100) / 3)
        self.assertEqual(rank_candidates(target, [target])[0]["distance"], 0)

    def test_tie_does_not_change_distance(self):
        target = self.data["mars"][0]
        sites = [dict(target, name="B"), dict(target, name="A")]
        results = rank_candidates(target, sites)
        self.assertEqual([r["site"]["name"] for r in results], ["A", "B"])
        self.assertEqual([r["distance"] for r in results], [0, 0])

    def test_invalid_numbers_rejected(self):
        for key, value in [("relief_m", -1), ("slope_deg", 91), ("roughness_m", float("nan")),
                           ("lat", 91), ("lon", -181), ("relief_m", True), ("roughness_m", "12")]:
            with self.subTest(key=key, value=value):
                data = copy.deepcopy(self.data)
                data["earth"][0][key] = value
                with self.assertRaises(ValueError):
                    validate_dataset(data)

    def test_missing_data_and_duplicates_rejected(self):
        for change in (lambda d: d.update(mars=[]), lambda d: d["earth"].pop(),
                       lambda d: d["earth"][0].pop("slope_deg"),
                       lambda d: d["earth"][0].update(id=d["mars"][0]["id"])):
            data = copy.deepcopy(self.data)
            change(data)
            with self.assertRaises(ValueError):
                validate_dataset(data)

    def test_external_requires_provenance(self):
        self.data["mode"] = "external"
        with self.assertRaisesRegex(ValueError, "procedencia"):
            validate_dataset(self.data)

    def test_external_contract_with_test_fixture(self):
        # Synthetic validation fixture only; never shipped as scientific input.
        self.data["mode"] = "external"
        for row in self.data["mars"] + self.data["earth"]:
            row.update(source="TEST FIXTURE", product_id="TEST", method="TEST",
                       resolution_m=30, window_m=1000, reference_frame="TEST")
        self.assertEqual(validate_dataset(self.data)["mode"], "external")
        self.data["earth"][0]["window_m"] = 10
        with self.assertRaises(ValueError):
            validate_dataset(self.data)

    def test_invalid_json_and_structure(self):
        for content in ("{", "[]", "null", '{"schema_version": 9}'):
            with self.subTest(content=content), self.assertRaises(ValueError):
                load_dataset(io.StringIO(content))
