"""First-run recovery, one-installer ownership, and windowless startup diagnostics."""

from contextlib import nullcontext, redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

from host import bootstrap
import run_contest
from tests.support.paths import temporary_directory


class SetupProcess:
    """Stand in for Python/venv/pip while the real marker and file lock are used."""

    def __init__(self, root):
        self.root = root
        self.compatible = self.installed = self.pip_ready = False
        self.installations = self.creations = self.pip_repairs = 0
        self.failure = None

    def probe(self, python, code, environment, directory):
        if not python.is_file():
            return False
        if "sys.version_info" in code:
            return self.compatible
        return self.pip_ready if "import pip" in code else self.installed

    def run(self, arguments, environment, output, label):
        self.environment = environment
        if "venv" in arguments:
            self.creations += 1
            directory = Path(arguments[-1])
            python = directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_bytes(b"test interpreter")
            self.compatible = True
            self.pip_ready = True
        elif "ensurepip" in arguments:
            self.pip_repairs += 1
            self.pip_ready = True
        else:
            self.installations += 1
            self.install_arguments = arguments
            if not self.pip_ready:
                raise RuntimeError("The local package installer is missing.")
            if self.failure is not None:
                error, self.failure = self.failure, None
                raise error
            self.installed = True


class EnvironmentSetupChecks(unittest.TestCase):
    def setUp(self):
        folder = temporary_directory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.wheels = self.root / "wheels"
        self.wheels.mkdir()
        (self.wheels / "fixture.whl").write_bytes(b"test wheel path")
        self.write_bundle(bootstrap.dependency_requirements())
        self.process = SetupProcess(self.root)
        for name, replacement in (("_probe", self.process.probe), ("_run_command", self.process.run)):
            patched = patch.object(bootstrap, name, side_effect=replacement)
            patched.start()
            self.addCleanup(patched.stop)
        if os.name == "nt":
            progress = patch("host.windows.setup_console", side_effect=lambda quiet: nullcontext())
            progress.start()
            self.addCleanup(progress.stop)
        self.messages = []

    def write_bundle(self, requirements):
        value = {"requirements": requirements, "platform": "win_amd64",
            "python_versions": [f"{sys.version_info.major}.{sys.version_info.minor}"], "wheels": ["fixture.whl"]}
        (self.wheels / "manifest.json").write_text(json.dumps(value) + "\n", encoding="utf-8", newline="\n")

    def setup(self):
        return bootstrap.ensure_environment(self.root, quiet=True, output=self.messages.append)

    def test_first_setup_is_local_and_successful_environment_is_reused(self):
        directory = self.setup()
        self.assertEqual(directory, self.root / ".venv")
        self.assertEqual((self.process.creations, self.process.installations), (1, 1))
        self.assertEqual(Path(self.process.environment["TEMP"]), self.root / "tmp")
        self.assertEqual(Path(self.process.environment["TMPDIR"]), self.root / "tmp")
        self.assertIn("--no-index", self.process.install_arguments)
        self.assertIn("Pillow", self.process.install_arguments)
        self.assertNotIn("-r", self.process.install_arguments)
        self.assertFalse((self.root / "requirements.txt").exists())
        index = self.process.install_arguments.index("--find-links")
        self.assertEqual(self.process.install_arguments[index + 1], self.wheels)
        self.setup()
        self.assertEqual((self.process.creations, self.process.installations), (1, 1))

    def test_failed_or_interrupted_install_releases_lock_and_next_launch_repairs_it(self):
        for error in (RuntimeError("installation failed"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                self.process.failure = error
                with self.assertRaises(type(error)):
                    self.setup()
                self.assertFalse((self.root / ".venv/environment-ready.json").exists())
        self.setup()
        self.assertEqual((self.process.creations, self.process.installations), (1, 3))
        self.assertTrue(self.process.installed)
        self.assertTrue((self.root / ".venv/environment-ready.json").is_file())

    def test_interrupted_environment_creation_repairs_a_missing_package_installer(self):
        python = self.root / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
        python.parent.mkdir(parents=True)
        python.write_bytes(b"test interpreter")
        self.process.compatible = True
        self.setup()
        self.assertEqual(self.process.creations, 0)
        self.assertEqual(self.process.pip_repairs, 1)
        self.assertEqual(self.process.installations, 1)
        self.assertTrue((self.root / ".venv/environment-ready.json").is_file())

    def test_missing_package_invalidates_an_existing_ready_environment(self):
        self.setup()
        self.process.installed = False
        self.setup()
        self.assertEqual(self.process.creations, 1)
        self.assertEqual(self.process.installations, 2)

    def test_changed_requirements_are_installed_before_readiness_is_recorded(self):
        self.setup()
        with patch.object(bootstrap, "DEPENDENCIES", ("Pillow>=1",)):
            self.write_bundle(bootstrap.dependency_requirements())
            self.setup()
        self.assertEqual(self.process.installations, 2)
        marker = json.loads((self.root / ".venv/environment-ready.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["requirements"], "Pillow>=1\n")

    def test_missing_bundle_fails_without_creating_or_downloading_an_environment(self):
        (self.wheels / "fixture.whl").unlink()
        with self.assertRaisesRegex(FileNotFoundError, "complete release ZIP"):
            self.setup()
        self.assertEqual((self.process.creations, self.process.installations), (0, 0))

    def test_changed_requirements_reject_a_stale_bundle_without_online_fallback(self):
        with patch.object(bootstrap, "DEPENDENCIES", ("Pillow>=1",)):
            with self.assertRaisesRegex(RuntimeError, "updated wheel bundle"):
                self.setup()
        self.assertEqual((self.process.creations, self.process.installations), (0, 0))

    def test_second_installer_cannot_start_while_setup_lock_is_owned(self):
        scratch = self.root / "tmp"
        scratch.mkdir()
        with bootstrap.setup_lock(scratch):
            with self.assertRaises(bootstrap.EnvironmentBusy):
                self.setup()
        self.assertEqual(self.process.creations, 0)
        self.assertEqual(self.process.installations, 0)


class LauncherDiagnosticsChecks(unittest.TestCase):
    def setUp(self):
        folder = temporary_directory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        changed = patch.object(run_contest, "MODULE_ROOT", self.root)
        changed.start()
        self.addCleanup(changed.stop)
        popup = patch.object(run_contest, "show_error_dialog")
        self.popup = popup.start()
        self.addCleanup(popup.stop)

    def test_windowless_error_has_a_visible_message_and_redacted_local_traceback(self):
        private = "b" * 64
        try:
            raise RuntimeError("dependency failure " + private)
        except RuntimeError as error:
            with patch.object(sys, "stdout", None):
                run_contest.report_startup_error(error)
        self.popup.assert_called_once()
        message = self.popup.call_args.args[0]
        log = (self.root / "tmp/startup.log").read_text(encoding="utf-8")
        self.assertIn("dependency failure", message)
        self.assertIn("Traceback", log)
        self.assertIn("startup.log", message)
        self.assertNotIn(private, message)
        self.assertNotIn(private, log)

    def test_log_write_failure_still_reports_an_actionable_error(self):
        with patch.object(run_contest, "append_startup_log", side_effect=PermissionError()), redirect_stdout(io.StringIO()):
            run_contest.report_startup_error(RuntimeError("setup failed"))
        self.popup.assert_called_once()
        self.assertIn("writable folder", self.popup.call_args.args[0])

    def test_invalid_launcher_option_is_reported_without_running_gameplay(self):
        with redirect_stdout(io.StringIO()):
            code = run_contest.main(["--unknown-option"])
        self.assertEqual(code, 1)
        self.popup.assert_called_once()
        self.assertIn("--help", self.popup.call_args.args[0])

    def test_help_is_available_without_environment_creation(self):
        with patch.object(bootstrap, "ensure_environment") as setup, redirect_stdout(io.StringIO()) as output:
            code = run_contest.entry_point(["--help"])
        self.assertEqual(code, 0)
        self.assertTrue(output.getvalue())
        setup.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows launcher relaunch")
    def test_windowless_relaunch_uses_local_pythonw_and_preserves_arguments(self):
        python = self.root / ".venv/Scripts/pythonw.exe"
        python.parent.mkdir(parents=True)
        python.write_bytes(b"test interpreter")
        with patch("host.windows.hide_private_console", return_value=True), \
                patch.object(bootstrap, "ensure_environment", return_value=self.root / ".venv"), \
                patch.object(run_contest.subprocess, "Popen", return_value=Mock()) as launch:
            code = run_contest.entry_point(["--check"])
        self.assertEqual(code, 0)
        arguments = launch.call_args.args[0]
        self.assertEqual(arguments, [str(python), "-B", str(run_contest.SCRIPT), "--check"])
        self.assertEqual(launch.call_args.kwargs["env"]["AUTOPLAY_WINDOWLESS"], "1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
