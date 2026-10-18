"""Unit tests for the flow.google.com provider's pure helpers."""

import unittest
from datetime import datetime, timezone

from src.services.providers.gflow_utils import (
    GFlowProviderError,
    extract_result_json,
    map_model,
    select_recoverable_video,
)


class GFlowProviderHelperTests(unittest.TestCase):
    def test_model_aliases_map_to_cli_names(self) -> None:
        self.assertEqual(map_model("veo3_fast"), "veo-lite")
        self.assertEqual(map_model("veo31_fast"), "veo-fast")
        self.assertEqual(map_model("omni_flash"), "omni-flash")

    def test_unknown_model_is_rejected_before_submit(self) -> None:
        with self.assertRaises(GFlowProviderError) as raised:
            map_model("unknown")
        self.assertEqual(raised.exception.code, "INVALID_ARGUMENT")

    def test_extracts_plain_json(self) -> None:
        result = extract_result_json('{"status":"ok","media_id":"abc"}')
        self.assertEqual(result["media_id"], "abc")

    def test_extracts_json_after_banner(self) -> None:
        result = extract_result_json('notice\n{"status":"fail","error":{"exit_code":11}}')
        self.assertEqual(result["error"]["exit_code"], 11)

    def test_invalid_output_returns_empty_object(self) -> None:
        self.assertEqual(extract_result_json("not json"), {})

    def test_selects_new_matching_orphan_for_browser_recovery(self) -> None:
        started = datetime(2026, 9, 21, 8, 9, 30, tzinfo=timezone.utc)
        rows = [
            {
                "media_id": "fresh",
                "project_id": "project-1",
                "prompt": "strawberry",
                "created_at": "2026-09-21T08:09:37+00:00",
                "local_path": None,
            },
            {
                "media_id": "old",
                "project_id": "project-1",
                "prompt": "strawberry",
                "created_at": "2026-09-20T08:09:37+00:00",
                "local_path": None,
            },
        ]
        match = select_recoverable_video(
            rows, prompt="strawberry", started_at=started, project_id="project-1"
        )
        self.assertIsNotNone(match)
        self.assertEqual(match["media_id"], "fresh")

    def test_recovery_never_reuses_old_or_different_prompt(self) -> None:
        started = datetime(2026, 9, 21, 8, 9, 30, tzinfo=timezone.utc)
        rows = [
            {
                "media_id": "wrong",
                "project_id": "project-1",
                "prompt": "another prompt",
                "created_at": "2026-09-21T08:09:37+00:00",
                "local_path": None,
            }
        ]
        self.assertIsNone(
            select_recoverable_video(
                rows, prompt="strawberry", started_at=started, project_id="project-1"
            )
        )


if __name__ == "__main__":
    unittest.main()
