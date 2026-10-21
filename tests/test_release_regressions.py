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

    def test_recent_image_task_has_whole_task_delete_action(self) -> None:
        dashboard = (ROOT / "src" / "static" / "dashboard.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("async function deleteImageTask(imageId, button)", dashboard)
        self.assertIn("Удалить задание", dashboard)
        self.assertIn("method: 'DELETE'", dashboard)
        self.assertIn("Дождитесь завершения генерации", dashboard)
        self.assertIn("_actionInFlight = true", dashboard)
        self.assertIn("_lastImageRenderSignature = null", dashboard)
        self.assertIn("button.dataset.confirmDelete", dashboard)
        self.assertIn("Нажмите ещё раз для удаления", dashboard)

    def test_unchanged_poll_does_not_replace_image_cards(self) -> None:
        dashboard = (ROOT / "src" / "static" / "dashboard.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("imageRenderSignature !== _lastImageRenderSignature", dashboard)
        self.assertIn("_lastImageRenderSignature = imageRenderSignature", dashboard)

    def test_recent_images_are_loaded_ten_at_a_time(self) -> None:
        dashboard = (ROOT / "src" / "static" / "dashboard.html").read_text(
            encoding="utf-8"
        )
        routes = (ROOT / "src" / "api" / "routes.py").read_text(encoding="utf-8")
        self.assertIn("let _imageDisplayLimit = 10", dashboard)
        self.assertIn("_imageDisplayLimit += 10", dashboard)
        self.assertIn("Загрузить ещё 10", dashboard)
        self.assertIn("async def get_dashboard_data(image_limit: int = 10)", routes)
        self.assertIn('"recent_images_has_more"', routes)

    def test_default_result_retention_is_sixty_days(self) -> None:
        config_source = (ROOT / "src" / "core" / "config.py").read_text(
            encoding="utf-8"
        )
        example = (ROOT / "config" / "setting.example.toml").read_text(
            encoding="utf-8"
        )
        self.assertIn('self.get("cleanup", "max_age_days", 60)', config_source)
        self.assertIn("max_age_days = 60", example)

    def test_video_cleanup_uses_supported_model_conversion(self) -> None:
        database = (ROOT / "src" / "core" / "database.py").read_text(
            encoding="utf-8"
        )
        main = (ROOT / "src" / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("VideoDB.from_row", database)
        self.assertIn("VideoDB(**dict(row))", database)
        self.assertIn("Video cleanup failed; continuing with images", main)


if __name__ == "__main__":
    unittest.main()
