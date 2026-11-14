# Pokémon Auto Play

Standalone **Japanese Platinum (`CPUJ`) Super Contest automation** for Windows
and DeSmuME. The six-stage master collects all 20 contest ribbons on the first
party Pokémon. Prepared dancing runners, category/rank diagnostics, image readers,
states, and retained findings live in [`contest/`](contest/README.md).

## Setup and ownership

Requires **Python 3.10+**, Pillow, **DeSmuME 0.9.13 x64**, `lua51.dll`, and a local
Japanese Platinum ROM with the prepared battery save. Run commands from this root:

```powershell
python -m pip install -r requirements.txt
python -B contest/super_contest.py --plan-only
```

Local runtime layout:

```text
desmume-0.9.13-win64/
  DeSmuME_0.9.13_x64.exe
  lua51.dll
  desmume.ini
  Roms/Pocket Monsters - Platinum (Japan).nds
  Battery/Pocket Monsters - Platinum (Japan).dsv
  StateSlots/
```

Keep emulator data paths in `desmume.ini` inside this project. The emulator,
ROM, battery saves, native slots, and root recordings are local Git-ignored files.
Prepared reference states under `contest/states/` and `contest/evidence/` are tracked.
The standalone dancing demonstration has its own [slot-5 setup](contest/README.md#dancing-setup-and-run).

**One controlled emulator, one host caller.** Check processes and windows before
launching. A running, hung, or terminating DeSmuME instance blocks a replacement;
both its process and windows must disappear first. All runner scratch belongs
under project `tmp/` or `.tmp/`.

## Runner entry points

| Entry point | Purpose |
| --- | --- |
| [`contest/super_contest.py`](contest/super_contest.py) | Persistent six-stage, 20-ribbon collection loop. |
| [`contest/debug_contest.py`](contest/debug_contest.py) | Category/rank dancing checks and input-free measurements. |
| [`contest/debug_pointer_speed.py`](contest/debug_pointer_speed.py) | Saved-entry pointer-speed measurements. |
| [`contest/dance_master.py`](contest/dance_master.py) | Preserved six-response dancing demonstration and optional AVI recording. |
| [`contest/rank_dance.py`](contest/rank_dance.py) | Prepared rank-selected six-response dancing. |
| [`contest/pt_dance_read.py`](contest/pt_dance_read.py) | Standalone screenshot reader. |

Stage runners, settings, execution commands, and retry contracts are documented
in the [contest guide](contest/README.md#six-stage-chain).

## Paused screenshots

A paused state load can leave the display grey. Cached `tmp/screen.gd` pixels
and their frame number do not prove a fresh render. For fresh evidence, finish
the agreed advance, confirm native pause, request a new `gui.gdscreenshot()`
through the paused Lua callback, and convert that capture. The capture adds
zero emulator frames; any render-preparation advance remains explicit and counted.
See [Acting entry and retry rendering](contest/README.md#stage-6-retry-render-preparation).

## Runner planning and checkpoints

Agree on inputs, actions, readable checkpoint facts, branches, guards with units,
the live attempt, and the finish point before new gameplay work. Prepared runs
use their saved defaults unless overrides are requested. Keep timings explicit
and distinguish emulator frames, game seconds, and host seconds.

Use [`lua/checkpoint_master.lua`](lua/checkpoint_master.lua), its adapters, and
[`host/checkpoints.py`](host/checkpoints.py): **pause → fresh zero-frame snapshot
and Lua checks → save when required → acknowledge the final log → proceed or abort**.
Evaluate each boundary once per visit. Screen-only receipts stay state-free;
retry states retain their paired frame/epoch/context. Unreadable boundaries stay
paused with retained evidence. The [integrated contracts](contest/README.md#integrated-runner-design)
and [save policy](contest/README.md#stage-checkpoints-and-resume) own the details.

## Verification

```powershell
python -B -m tests
python -B contest/super_contest.py --check
python -B contest/debug_contest.py --check
python -B contest/dance_master.py --check
```

The offline suite executes real Lua through the local 64-bit `lua51.dll`, using
simulated emulator callbacks and memory. It covers contest integration, input
validation, native-control contracts, and retained-state handling. Offline checks
do not establish live gameplay behavior. New or expanded tests require an agreed
reason; contest statistics follow the [retention policy](contest/README.md#test-statistics-and-retention).

## Shared code and attribution

`host/` owns native controls, `lua/` owns local input/checkpoint and Platinum memory
helpers, and `contest/inputs.py` owns the contest input defaults. These dependencies
are included in this repository. The Platinum memory helpers are adapted from
scripts by **Real96**, with layout edits by **SexyMalasada**; their supplied
[GPL v3 license](lua/PLATINUM-LICENSE.txt) is retained.
