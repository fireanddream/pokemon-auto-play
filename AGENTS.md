# Project instructions

Platinum contest automation. Follow the user's scope; region acceptance is separate from live verification.

## Ownership and design

- [readme.md](readme.md) owns the user introduction/setup; [ALGORITHM.md](ALGORITHM.md)
  owns runner steps/shared contracts. Keep guidance concise and current; omit run histories.
- Keep source and Markdown files LF-only, following `.gitattributes`; mixed LF/CRLF
  triggers IDLE warnings. Verify Python-file line endings after edits. Code that rewrites
  source files must explicitly write LF (e.g. `newline="\n"` in Python text writes).
- One controlled emulator and one host caller. Keep dependencies and scratch project-local
  (`tmp/` or `.tmp/`); preserve prepared states and established design facts.
- Keep checkpoint master/client game-independent; adapters own targets, timing, slots, and screen policy.

## Runners and verification

- Follow [planning and checkpoints](ALGORITHM.md#runner-planning-and-checkpoints): agree on
  actions, readable boundaries, branches, guards with units, live attempt, and finish point.
  Prepared runs use saved defaults; ask before shared timing changes or new retry/timeout limits.
- Keep progress/retries local. Use the shared pause/capture/check/save/acknowledge protocol
  once per boundary visit. Unreadable checks retain paused evidence and its actual source.
- Distinguish emulator frames, game seconds, and host seconds. Use `python -B -m tests`
  and existing `--check` entries; new/expanded tests need an agreed reason.
  Offline checks do not prove live behavior.

## Contest contracts

- Follow the [integrated design](ALGORITHM.md#integrated-runner-design),
  [save policy](ALGORITHM.md#stage-checkpoints-and-resume), and
  [test retention policy](ALGORITHM.md#test-statistics-and-retention).
- Stage 5 uses [entry-anchored midpoint scheduling](ALGORITHM.md#stage-5-dancing).
- Stage 6 follows [render-before-analysis](ALGORITHM.md#stage-6-retry-render-preparation):
  accepted paused source -> agreed input-free preparation -> confirmed native pause ->
  fresh zero-frame capture -> judge analysis. Cached screenshots cannot establish readiness.
