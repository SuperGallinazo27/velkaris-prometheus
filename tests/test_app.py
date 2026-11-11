from pathlib import Path
import unittest

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]


class AppTests(unittest.TestCase):
    def setUp(self):
        self.app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()

    def test_initial_app_has_warning_ranking_and_map(self):
        self.assertEqual(len(self.app.exception), 0)
        self.assertIn("DEMOSTRACIÓN", self.app.warning[0].value)
        self.assertEqual(len(self.app.dataframe[0].value), 3)
        self.assertEqual(len(self.app.get("deck_gl_json_chart")), 1)

    def test_region_selection_recomputes_ranking(self):
        for region, winner in [("jezero", "Atacama, Chile"), ("gale", "Cotopaxi, Ecuador"),
                               ("elysium", "Lanzarote, España")]:
            self.app.sidebar.selectbox[0].select(region).run()
            self.assertEqual(len(self.app.exception), 0)
            self.assertEqual(self.app.dataframe[0].value.iloc[0]["Candidato"], winner)

    def test_candidate_selection_updates_comparison(self):
        self.app.selectbox[0].select("cotopaxi").run()
        self.assertEqual(len(self.app.exception), 0)
        self.assertEqual(self.app.dataframe[1].value.iloc[0]["Tierra"], 1800)

    def test_import_without_file_has_no_results(self):
        self.app.sidebar.radio[0].set_value("Importar descriptores").run()
        self.assertEqual(len(self.app.exception), 0)
        self.assertEqual(len(self.app.dataframe), 0)
        self.assertIn("Todavía no hay datos científicos", self.app.info[0].value)
