"""Run every offline check with ``python -B -m tests`` from the project root."""

import argparse
import os
from pathlib import Path
import unittest

from tests.support.paths import ROOT


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", action="store_true", help="Run the complete offline release gate, including fresh ZIP and native UI checks.")
    parser.add_argument("--lua-dll", type=Path, help="Use this matching local Lua 5.1 DLL for the test package.")
    args = parser.parse_args(argv)
    if args.lua_dll is not None:
        os.environ["AUTOPLAY_TEST_LUA_DLL"] = str(args.lua_dll.resolve())
    if args.release:
        from tests.release import verify_release
        from tests.release.runner import safe_output

        try:
            verify_release(lua_dll=args.lua_dll)
        except (OSError, ValueError, RuntimeError) as error:
            safe_output(error)
            return 1
        return 0
    suite = unittest.defaultTestLoader.discover(
        start_dir=str(ROOT / "tests"), top_level_dir=str(ROOT),
    )
    if suite.countTestCases() == 0:
        raise RuntimeError("Offline test discovery found no tests.")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
