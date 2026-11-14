# Project instructions

Standalone Japanese Platinum (`CPUJ`) contest automation. Follow the user's scope.

## Documentation and ownership

- Root [README.md](README.md) owns setup and shared contracts; [contest/README.md](contest/README.md)
  owns full-run, stage, dancing, and diagnostic details. Keep guidance concise.
- Follow [single-instance ownership](README.md#setup-and-ownership): one controlled
  emulator and one host caller. Scratch stays under project `tmp/` or `.tmp/`.
- Keep runtime dependencies project-local and preserve prepared states and findings.

## Runners and verification

- Agree on actions, readable checkpoints, branches, guards with values/units,
  the live attempt, and the finish point. Prepared runs use saved defaults unless overridden.
- Keep normal progress and retries local to the runner; unexpected stops retain paused evidence.
- Use `lua/checkpoint_master.lua`, its adapters, and `host/checkpoints.py`:
  **pause -> fresh zero-frame snapshot -> Lua checks -> save when required ->
  acknowledge final log -> proceed/abort**. Evaluate once per visit and share boundaries.
- Keep master/client game-independent; adapters own targets, timing, slots, and screen policy.
- Distinguish emulator frames, game seconds, and host seconds. Ask before changing shared
  timing defaults or adding arbitrary retry/timeout limits.
- Unreadable screen checks stay paused with their actual evidence source recorded.
- Use `python -B -m tests` and existing `--check` entry points. New/expanded tests
  require a concrete reason and user agreement. Offline checks do not prove live behavior.

## Contest contracts

- Full runs follow the [integrated runner contracts](contest/README.md#integrated-runner-design).
- All contest tests follow the [statistics and retention policy](contest/README.md#test-statistics-and-retention).
- Stage 5 follows [entry-anchored midpoint scheduling](contest/README.md#stage-5--dancing).
  Follow the [observation and save policy](contest/README.md#stage-checkpoints-and-resume).
- Stage 6 entry and ribbon-miss retries follow the [render-before-analysis sequence](contest/README.md#stage-6-retry-render-preparation):
  accepted Stage 5 handoff or Acting retry-state load paused -> complete agreed input-free
  render preparation -> confirm native pause -> fresh zero-frame capture -> judge analysis.
  An earlier cached screenshot cannot establish readiness.
