"""Run every offline check with ``python -B -m tests`` from the project root."""

import unittest

from tests.support.paths import ROOT


def main() -> int:
    suite = unittest.defaultTestLoader.discover(
        start_dir=str(ROOT / "tests"), top_level_dir=str(ROOT),
    )
    if suite.countTestCases() == 0:
        raise RuntimeError("Offline test discovery found no tests.")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
