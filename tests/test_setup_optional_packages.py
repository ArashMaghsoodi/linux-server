import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "setup_optional_packages.py"
SETUP_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "setup.sh"
SPEC = importlib.util.spec_from_file_location("setup_optional_packages", SCRIPT_PATH)
setup_optional_packages = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup_optional_packages)


class SetupOptionalPackagesTests(unittest.TestCase):
    def test_setup_does_not_use_python312_before_installing_it(self):
        setup_script = SETUP_SCRIPT_PATH.read_text(encoding="utf-8")
        python_install_offset = setup_script.index("==> Installing Python 3.12")

        self.assertNotIn("python3.12", setup_script[:python_install_offset])

    def test_failed_optional_package_is_recorded_and_later_packages_are_attempted(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            warnings_path = Path(temporary_directory) / "setup-warnings.txt"
            with patch.object(
                setup_optional_packages.subprocess,
                "run",
                side_effect=[
                    subprocess.CompletedProcess([], 100),
                    subprocess.CompletedProcess([], 0),
                ],
            ) as run:
                warnings = setup_optional_packages.install_optional_packages(
                    ["fastfetch", "tree"],
                    warnings_path,
                )

            self.assertEqual(
                run.call_args_list,
                [
                    call(["sudo", "apt-get", "install", "-y", "fastfetch"], check=False),
                    call(["sudo", "apt-get", "install", "-y", "tree"], check=False),
                ],
            )
            self.assertEqual(
                warnings,
                ["Optional package 'fastfetch' installation failed (apt-get exit 100)."],
            )
            self.assertEqual(
                warnings_path.read_text(encoding="utf-8").splitlines(),
                warnings,
            )

    def test_unavailable_installer_is_reported_without_blocking_other_packages(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            warnings_path = Path(temporary_directory) / "setup-warnings.txt"
            with patch.object(
                setup_optional_packages.subprocess,
                "run",
                side_effect=[
                    FileNotFoundError("sudo not found"),
                    subprocess.CompletedProcess([], 0),
                ],
            ) as run:
                warnings = setup_optional_packages.install_optional_packages(
                    ["htop", "tree"],
                    warnings_path,
                )

            self.assertEqual(run.call_count, 2)
            self.assertEqual(
                warnings,
                ["Optional package 'htop' installation failed (sudo not found)."],
            )
            self.assertEqual(
                warnings_path.read_text(encoding="utf-8").splitlines(),
                warnings,
            )


if __name__ == "__main__":
    unittest.main()
