# Pokémon Auto Play

Windows/DeSmuME automation that collects all **20 Platinum Super Contest ribbons**
on the first party Pokémon. JPN (`CPUJ`) and English (`CPUE`, shared USA/EU) ROMs are accepted.
See [regional memory readers](contest/README.md#regional-memory-readers) for verification coverage.

## Setup and ownership

Requires **Python 3.10+**, Pillow, **DeSmuME 0.9.13 x64**, its matching `lua51.dll`,
and a local Platinum ROM with an existing battery save.

Run setup from the folder containing `requirements.txt` (`contest/` in a release):

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
```

- Put `lua51.dll`, `Battery/`, and `StateSlots/` beside the emulator executable.
  The battery filename must match the ROM stem.
- Set **Options → Text Speed → Fast**, then save on the blue mat facing the middle
  receptionist. The first Pokémon needs
  **HP > 0, Sheen > 0, at least two moves, and must not be an Egg**.
- Double-click [`run_contest.py`](run_contest.py), or run `python -B run_contest.py`
  from the folder containing that file.
  Direct Windows launches hide the console and prefer `pythonw.exe`.
  The launcher prefers the local `.venv` when present. Selected emulator/ROM paths
  are saved in that file; `python -B run_contest.py --choose-paths` selects them again.
- **One controlled emulator, one host caller.** The launcher rejects a second caller,
  silently closes existing DeSmuME instances, and boots the selected ROM fresh.
  Fresh launches use the script lock and bind controls to their newly launched emulator PID.
  The emulator and Lua console open before cleanup removes older run folders and play begins.
  Startup requires no warning acknowledgment; duplicate scripts exit without a popup.
  Warnings, failures, and completion are messages in the Lua console; timeouts log `timeout`.
- Edit gameplay defaults in `contest/timing_profiles/<ROM-code>.json`; see
  [regional timing profiles](contest/README.md#regional-timing-profiles) for sections,
  units, selection, and validation. USA/EU share `CPUE.json`.

## Release layout

Extraction produces two top-level items:

```text
run_contest.py
contest/
  super_contest.py    # runner and other contest modules live directly here
  timing_profiles/   # editable regional gameplay defaults
  debug/             # persistent evidence moved here before another launch
  host/
  lua/
  requirements.txt
  README.md
  DESIGN.md
  .venv/          # created locally after extraction
  tmp/           # created by the runner
```

The release launcher loads its modules and optional `.venv` from `contest/`.
Create the environment after extraction; virtual environments contain local Python paths.
Emulator/ROM selections are initially empty and are remembered in the top-level launcher.
Each launch stores its mailbox, screenshots, receipts, and debug reads in
`contest/tmp/contest/contest-<timestamp>/` in a release. Its whole folder remains through
completion, failure, or interruption. Once the next launch has opened the emulator
and attached Lua, it deletes all older subfolders under `tmp/contest/` before playing.
Move evidence worth keeping into `contest/debug/` before starting another run.
The working project uses the same persistent evidence location. This folder is outside
temporary cleanup; release packaging includes the empty folder only.

## Design

- `host/`: native emulator controls, ownership, save slots, and checkpoint client.
- `lua/`: input bridge, game-independent checkpoint engine, and Platinum memory helpers.
- `contest/`: persistent six-stage runner, game-specific adapters, image readers, and
  timing profiles. See the [contest design](contest/README.md#integrated-runner-design).

Adapters own targets, timing, slots, and screen policy; shared control code stays
game-independent. Regional JSON profiles own timing defaults; `contest/inputs.py`
provides shared input helpers and JPN compatibility constants. Dependencies and
scratch stay project-local (`tmp/` or `.tmp/`); prepared states live in
`contest/states/` and `contest/evidence/`.
See [Poffin plans](buyable-poffin-guide.md) for contest-stat preparation.

## Runner planning and checkpoints

Agree on actions, readable checkpoints, branches, guards with units, the live attempt,
and finish point. Prepared runs use saved defaults; ask before changing shared timings
or retry limits. Distinguish **emulator frames**, **game seconds**, and **host seconds**.

[`lua/checkpoint_master.lua`](lua/checkpoint_master.lua) and
[`host/checkpoints.py`](host/checkpoints.py) enforce:
**pause → fresh zero-frame snapshot → Lua checks → save when required → acknowledge
the final log → proceed/abort**. Evaluate once per visit and share accepted boundaries.
Unreadable checks stay paused with their actual evidence source recorded.

## Paused screenshots

After explicit render preparation, confirm native pause and request a new
`gui.gdscreenshot()` through the paused Lua callback. Capturing adds zero emulator
frames; preparation advances are counted separately. Cached `screen.gd` cannot prove
freshness. See [Acting render preparation](contest/README.md#stage-6-retry-render-preparation).

Host-time save-overlay settling keeps native pause and released inputs. Its fresh
before/after captures must retain the same emulator frame and epoch; see
[dance save policy](contest/README.md#stage-checkpoints-and-resume).

## Verification

```powershell
python -B -m tests
python -B contest/super_contest.py --check
python -B contest/debug_contest.py --check
python -B contest/dance_master.py --check
```

Offline checks use real Lua with simulated emulator callbacks; live behavior requires
live verification. New or expanded tests require an agreed reason.

## Release packaging

Prepare a fresh review layout with `python -B tools/package_contest.py --stage tmp/release-stage`.
At release time, create the archive with
`python -B tools/package_contest.py --zip dist/contest-runner.zip`.
Staging copies the contest modules, runtime resources, and prepared references into `contest/`;
the release launcher starts with empty emulator/ROM selections.

## Attribution

Platinum memory helpers derive from **Real96**, with layout edits by **SexyMalasada**;
the supplied [GPL v3 license](lua/PLATINUM-LICENSE.txt) is retained.
