# Permanent test package

Run from the project root:

```powershell
python -B -m tests
python -B -m tests --release
```

The release command runs the complete offline suite with the ZIP's own Pillow
installation, source and packaged main-runner `--check`, syntax/LF checks, archive
layout/private-asset checks, first setup, reuse, missing-package and missing-installer
recovery, and native windowless error/progress checks.
Wheel coverage must include the full CPython 3.10–3.15 release set; environment and
native checks run on the active Python interpreter.

Requirements: Windows, a supported standard x64 CPython build, the prepared local
wheel bundle, a matching local Lua 5.1 DLL, and the retained private fixture described
in [fixtures/README.md](fixtures/README.md). Use `--lua-dll "path/to/lua51.dll"` when
the DLL is outside the default emulator folder.

Release checks install from the bundled wheels with network access blocked. Their
emulator program, ROM header, and battery-save inputs are synthetic. Native UI checks
only observe and dismiss their own test-process windows; a running game is not controlled.
Offline results do not establish live gameplay behavior.

Reports and sanitized logs are written to `tmp/release-checks/<run>/`. Test code and
cropped pixel references stay in this package; private fixture copies stay in the
Git-ignored `tests/fixtures/local/` folder.

Release ZIP creation uses this same gate automatically. See the project's
[release packaging guidance](../ALGORITHM.md#release-packaging).
