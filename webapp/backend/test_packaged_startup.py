"""Check library imports with read-only application resources."""
import errno
import runpy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


class PackagedStartupTests(unittest.TestCase):
    def test_import_populator_without_creating_output_directories(self):
        populator = Path(__file__).resolve().parents[2] / "populator"
        with patch.object(sys, "path", [str(populator), *sys.path]):
            with patch.object(Path, "mkdir", side_effect=OSError(errno.EROFS, "Read-only application resources")):
                runpy.run_path(str(populator / "populate.py"), run_name="packaged_import_check")


if __name__ == "__main__":
    unittest.main()
