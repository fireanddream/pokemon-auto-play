# Retained test inputs

- `tests/support/dance-captures.json` stores the cropped reference pixels used by
  the dance checks.
- `tests/support/acting-capture.json` stores the shared non-text fill, heart-strip,
  and player-panel pixels from the four saved Acting screenshots.
- `tests/support/post-acting-capture.json` retains the shared player pixels and
  nearby dialogue top border from three languages, plus the supplied dialogue-absent strip.
- `tests/fixtures/local/completed.png` is a private reference screenshot. The
  fixture helper retains a copy from `contest/evidence/completed.png` when needed.
  Existing retained copies are preserved.
- The Lua checks require a local matching `lua51.dll`; pass `--lua-dll` to the
  test command when it is outside the default emulator folder.

`local/` is Git-ignored. Private inputs, emulator files, and ROMs are excluded from
release ZIPs. Generated test data and reports belong in project-local `tmp/`.
