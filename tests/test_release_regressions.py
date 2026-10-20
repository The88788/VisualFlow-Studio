"""Regression checks for the patched Flow transport and dashboard feedback."""

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReleaseRegressionTests(unittest.TestCase):
    def test_reference_prompt_is_verified_in_editor_not_raw_body(self) -> None:
        patch_source = (ROOT / "scripts" / "patch_gflow_container.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('editor_text = await composer.inner_text()', patch_source)
        self.assertIn('normalized_prompt not in normalized_editor', patch_source)
        self.assertNotIn('escaped_prompt not in body', patch_source)
        self.assertNotIn('request.model, request.prompt', patch_source)

    def test_generation_form_shows_immediate_submission_progress(self) -> None:
        dashboard = (ROOT / "src" / "static" / "dashboard.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("submit.classList.add('is-submitting')", dashboard)
        self.assertIn("Загружаем референсы", dashboard)
        self.assertIn("Задание принято — запускаем Flow", dashboard)
        self.assertIn("submit.classList.remove('is-submitting')", dashboard)


if __name__ == "__main__":
    unittest.main()
