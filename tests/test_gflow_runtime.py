"""Tests for safe Chrome profile marker recovery."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.services.providers.gflow_runtime import prepare_profile


class GFlowRuntimeTests(unittest.TestCase):
    @unittest.skipIf(
        os.name == "nt",
        "Chrome singleton recovery uses Linux symlinks in the production container",
    )
    def test_removes_stale_singleton_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
                os.symlink("old-container", profile / name)

            with patch("src.services.providers.gflow_runtime.Path.iterdir", return_value=[]):
                prepare_profile(profile)

            for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
                self.assertFalse(os.path.lexists(profile / name))


if __name__ == "__main__":
    unittest.main()
