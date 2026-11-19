"""A failed release gate cannot publish a final ZIP; successful output is the tested candidate."""

from io import BytesIO
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from tests.support.paths import ROOT, temporary_directory
from tests.release import runner as release_runner
from tools import package_contest, prepare_dependencies


def wheel_name(version):
    tag = "cp" + version.replace(".", "")
    return f"pillow-1.0.0-{tag}-{tag}-win_amd64.whl"


def synthetic_wheel():
    content = BytesIO()
    with ZipFile(content, "w") as archive:
        archive.writestr("fixture.txt", "Synthetic archive for wheel-coverage checks only.\n")
    return content.getvalue()


class ReleasePackagingChecks(unittest.TestCase):
    def setUp(self):
        folder = temporary_directory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        changed = patch.object(package_contest, "ROOT", self.root)
        changed.start()
        self.addCleanup(changed.stop)
        for relative in ("contest/debug", "contest/timing_profiles", "host", "lua", "wheels", "tmp"):
            (self.root / relative).mkdir(parents=True)
        (self.root / "readme.md").write_text("# Fixture introduction\n", encoding="utf-8", newline="\n")
        (self.root / "ALGORITHM.md").write_text("# Fixture algorithm\n", encoding="utf-8", newline="\n")
        (self.root / "buyable-poffin-guide.md").write_text("# Fixture Poffin guide\n", encoding="utf-8", newline="\n")
        (self.root / "run_contest.py").write_text('DESMUME_PATH = "private/program.exe"\nROM_PATH = "private/game.nds"\n',
            encoding="utf-8", newline="\n")
        for relative in ("contest/debug/.gitkeep", "contest/region-memory-results.txt",
                "contest/scarf-test-results.txt", "lua/PLATINUM-LICENSE.txt"):
            (self.root / relative).write_text("fixture\n", encoding="utf-8", newline="\n")
        for folder, names in package_contest.RUNTIME_SOURCES.items():
            for name in names:
                path = self.root / folder / name
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.suffix == ".png":
                    path.write_bytes((ROOT / folder / name).read_bytes())
                else:
                    path.write_text("{}\n" if path.suffix == ".json" else "fixture\n", encoding="utf-8", newline="\n")
        (self.root / "contest/dance_timings.json").write_text("{}\n", encoding="utf-8", newline="\n")
        (self.root / "contest/retired_runner.py").write_text("raise RuntimeError('not shipped')\n", encoding="utf-8", newline="\n")
        (self.root / "wheels/fixture.whl").write_bytes(b"synthetic wheel placeholder")
        value = {"requirements": "Pillow\n", "platform": "win_amd64",
            "python_versions": [f"{sys.version_info.major}.{sys.version_info.minor}"], "wheels": ["fixture.whl"]}
        (self.root / "wheels/manifest.json").write_text(json.dumps(value) + "\n", encoding="utf-8", newline="\n")
        self.output = self.root / "tmp/release.zip"

    def test_failed_verification_never_publishes_the_final_archive(self):
        for error, message in ((RuntimeError("release checks failed"), "release checks failed"),
                (None, "requested ZIP was not created")):
            with self.subTest(raised=error is not None), patch("tests.release.verify_release",
                    side_effect=error, return_value={"outcome": "fail"}) as verify:
                with self.assertRaisesRegex(RuntimeError, message):
                    package_contest.build_zip(self.output)
                verify.assert_called_once()
                self.assertFalse(self.output.exists())

    def test_success_publishes_the_exact_verified_candidate_with_bundled_files(self):
        candidates = []

        def verify(candidate, **options):
            candidates.append(candidate.read_bytes())
            return {"outcome": "pass"}

        with patch("tests.release.verify_release", side_effect=verify):
            self.assertEqual(package_contest.build_zip(self.output), self.output)
        self.assertEqual(self.output.read_bytes(), candidates[0])
        with ZipFile(self.output) as archive:
            names = archive.namelist()
            self.assertEqual({Path(name).parts[0] for name in names}, {"run_contest.py", "contest"})
            self.assertIn("contest/wheels/fixture.whl", names)
            self.assertIn("contest/wheels/manifest.json", names)
            self.assertIn("contest/readme.md", names)
            self.assertIn("contest/dress_background.png", names)
            self.assertEqual(archive.read("contest/dress_background.png"),
                (ROOT / "contest/dress_background.png").read_bytes())
            self.assertNotIn("contest/readme.txt", names)
            self.assertNotIn("contest/requirements.txt", names)
            self.assertNotIn("contest/dance_timings.json", names)
            self.assertNotIn("contest/retired_runner.py", names)
            self.assertNotIn("contest/scarf-test-results.txt", names)
            self.assertTrue(release_runner.RUNTIME_MEMBERS.issubset(names))
            self.assertNotIn("private/", archive.read("run_contest.py").decode("utf-8"))


class ReleaseWheelCoverageChecks(unittest.TestCase):
    def setUp(self):
        folder = temporary_directory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        with patch.object(release_runner, "ROOT", self.root):
            self.checks = release_runner.ReleaseChecks(output=lambda text: None)

    def make_archive(self, versions, wheel_versions):
        path = self.root / "candidate.zip"
        manifest = {"requirements": "Pillow\n", "platform": "win_amd64", "implementation": "cp",
            "python_versions": list(versions), "wheels": [wheel_name(version) for version in wheel_versions]}
        with ZipFile(path, "x") as archive:
            archive.writestr("run_contest.py", 'DESMUME_PATH = ""\nROM_PATH = ""\nRELEASE = "0.9 pre-release"\n')
            for name in ("readme.md", "ALGORITHM.md", "buyable-poffin-guide.md"):
                archive.writestr("contest/" + name, "# Fixture document\n")
            for name in release_runner.RUNTIME_MEMBERS:
                content = (ROOT / name).read_bytes() if Path(name).suffix == ".png" else (
                    "{}\n" if Path(name).suffix == ".json" else "fixture\n")
                archive.writestr(name, content)
            archive.writestr("contest/wheels/manifest.json", json.dumps(manifest) + "\n")
            for version in wheel_versions:
                archive.writestr("contest/wheels/" + wheel_name(version), synthetic_wheel())
        return path

    def test_manifest_cannot_narrow_the_advertised_release_python_coverage(self):
        archive = self.make_archive(["3.14"], ["3.14"])
        with self.assertRaisesRegex(RuntimeError, "3.10.*3.15"):
            self.checks.inspect_archive(archive)

    def test_full_manifest_still_requires_every_native_pillow_wheel(self):
        versions = prepare_dependencies.PYTHON_VERSIONS
        archive = self.make_archive(versions, [version for version in versions if version != "3.13"])
        with self.assertRaisesRegex(RuntimeError, "CPython 3.13"):
            self.checks.inspect_archive(archive)

    def test_complete_release_coverage_is_recorded(self):
        versions = prepare_dependencies.PYTHON_VERSIONS
        self.checks.inspect_archive(self.make_archive(versions, versions))
        self.assertEqual(self.checks.result["bundled_python_versions"], list(versions))
        self.assertEqual(self.checks.result["bundled_wheels"], [wheel_name(version) for version in versions])

    def test_unlisted_png_is_still_excluded_from_the_release(self):
        versions = prepare_dependencies.PYTHON_VERSIONS
        archive = self.make_archive(versions, versions)
        with ZipFile(archive, "a") as output:
            output.writestr("contest/private-capture.png", b"private capture")
        with self.assertRaisesRegex(RuntimeError, "excluded binary"):
            self.checks.inspect_archive(archive)


class CachedWheelPreparationChecks(unittest.TestCase):
    def setUp(self):
        folder = temporary_directory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.wheels = self.root / "tmp/dependency-preparation/wheels"
        self.wheels.mkdir(parents=True)
        for name, value in (("ROOT", self.root), ("output", lambda text: None)):
            changed = patch.object(prepare_dependencies, name, value)
            changed.start()
            self.addCleanup(changed.stop)
        command = patch.object(prepare_dependencies, "_run_command")
        self.command = command.start()
        self.addCleanup(command.stop)

    def populate(self, versions):
        for version in versions:
            (self.wheels / wheel_name(version)).write_bytes(synthetic_wheel())

    def test_partial_cache_is_preserved_and_rejected_without_downloading(self):
        versions = ("3.10", "3.11", "3.12", "3.14")
        self.populate(versions)
        with self.assertRaisesRegex(RuntimeError, "CPython 3.13"):
            prepare_dependencies.prepare_dependencies(cached=True)
        self.command.assert_not_called()
        self.assertEqual({path.name for path in self.wheels.glob("*.whl")}, {wheel_name(version) for version in versions})
        self.assertFalse((self.root / "wheels").exists())

    def test_complete_cache_is_assembled_without_downloading(self):
        versions = prepare_dependencies.PYTHON_VERSIONS
        self.populate(versions)
        destination = prepare_dependencies.prepare_dependencies(cached=True)
        self.command.assert_not_called()
        self.assertEqual(destination, self.root / "wheels")
        manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["python_versions"], list(versions))
        self.assertEqual(manifest["wheels"], [wheel_name(version) for version in versions])
        self.assertEqual(manifest["requirements"], "Pillow\n")
        self.assertFalse((self.root / "requirements.txt").exists())
        self.assertTrue((self.wheels.parent / "dependencies.json").is_file())

    def test_damaged_cached_wheel_is_reported_before_assembly(self):
        self.populate(prepare_dependencies.PYTHON_VERSIONS)
        broken = self.wheels / wheel_name("3.13")
        broken.write_bytes(b"interrupted wheel download")
        with self.assertRaisesRegex(RuntimeError, broken.name):
            prepare_dependencies.prepare_dependencies(cached=True)
        self.command.assert_not_called()
        self.assertEqual(broken.read_bytes(), b"interrupted wheel download")
        self.assertFalse((self.root / "wheels").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
