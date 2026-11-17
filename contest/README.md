# Contest runner design

See [setup and ownership](../README.md#setup-and-ownership) for the launcher and
prepared game requirements. Standalone prepared tools target JPN Platinum.

## Regional memory readers

JPN and English ROMs select `lua/platinum.lua` address profiles through
the trusted running ROM code (`emu.gamecode`). Party and Bag decoders are shared.

| Region | Local ROM code | Data pointer | Trainer/save pointer |
| --- | --- | --- | --- |
| JPN | `CPUJ` | `0x0210112C` | `0x021BFB94` |
| USA | `CPUE` | `0x02101D2C` | `0x021C0794` |
| EU | `CPUE` | `0x02101D2C` | `0x021C0794` |

The supplied USA/EU ARM9 programs are byte-identical and use the same profile.
Header revision is recorded separately.

Offline checks cover HP encodings, move slots, 20 ribbon flags, scarf positions and
quantities, and stopped unreadable reads. Recorded reader results are in
[`region-memory-results.txt`](region-memory-results.txt). Live Stage 1 evidence
exists for JPN and USA; EU still needs live verification. Offline reader
checks do not establish full regional contest-run coverage.

## Regional timing profiles

`timing_profiles/<ROM-code>.json` owns gameplay defaults, grouped by stage and purpose.
The host identifies the selected ROM, validates its profile once per process, and
passes resolved settings to Lua; the trusted running `emu.gamecode()` must match.
Only `CPUJ.json` (JPN) and `CPUE.json` (English, shared USA/EU) are supplied and accepted.
Prepared standalone tools use `CPUJ.json` and share the main
runner's applied dance metrics and Acting durations.
English Acting uses **74 / 150 game seconds** for rounds 1–3 / final round and results.

| Section | Controls |
| --- | --- |
| `stage1_loading` | Boot wait, Start taps, save loading, prompt dismissal. |
| `stage2_preparation` | Bag/menu navigation and scarf-give confirmation. |
| `stage3_entry` | Enrollment menus and introduction A-mashing. |
| `stage4_dress_up` | Dressing budget, accessory/page/Finish inputs, rendering, A transition, entry polling. |
| `stage5_dancing` | Response holds/offset, feedback wait, demo sampling, overlay settling, B transition. |
| `stage6_acting` | Menus, round/result B-mashing, handoff frames, loaded-state host-time rendering. |
| `standalone_dancing` | Prepared-source first-handoff anchor and display refresh. |
| `diagnostics` | Optional probes, measurement windows, polling, and pointer-sampling bounds. |
| `fixed_dance_metrics` | Pointer speed numerator/denominator, normalized gaps, native cue spacing. |

Suffixes specify units: `*_frames` are integer emulator counts, `*_game_seconds`
convert at **67027965 / 1120380 frames per game second**, and `*_host_seconds` are
wall-clock time. Dance speeds are native pixels per emulator frame; cue offsets are
native pixels. Negative `input_offset_frames` press early. `_help` is explanatory text.

Edit numeric values while retaining schema, game code, and required keys; restart the
caller to load edits. Explicit CLI timing flags override profile defaults, including
allowed zeros. The tables below show JPN defaults; inspect resolved regional values with
`python -B contest/super_contest.py --plan-only --game-code CPUE`.
`python -B contest/super_contest.py --check` validates every profile and runner syntax.
Deadlines, stage order, save slots, and screen coordinates are code-owned contracts.

## Integrated runner design

`super_contest.py` calls `sections.py` / `sections.lua` through one `ContestRuntime`
and persistent `super_contest.lua` dispatcher: **one host caller, controller, Lua
attachment, and checkpoint engine**. `runtime.py` owns speed, deadlines, and cleanup.

Run Stage 1 once, then repeat **2 → 3 → 4 → 5 → 6** for missing ribbons in order:
**Normal → Super → Hyper → Master**, each **Cool → Beauty → Cute → Smart → Tough**.
After either completed result, acknowledge Stage 6 before continuing from the current
overworld. Preserve ribbons, Bag opening/cursor state, the empty-move-slot blacklist,
and the original deadline. Full runs use no quick-loads or stage-by-stage Lua restarts.

Execution is uncapped with fast-forward off and counted emulator-frame inputs.
Finish paused on all 20 ribbons, an unexpected failure, or timeout; terminal cleanup
releases inputs, stops movie mode, and restores 100% speed with the frame limiter on.

## Six-stage chain

| Stage | Action and accepted endpoint | Native save with `debug=1` |
| --- | --- | ---: |
| 1 — Loading | Confirm the hall, then check Fast text speed, first-party eligibility, and Bag readiness. | — |
| 2 — Preparation | Read ribbons; choose the first missing target and equip its scarf when available. | 2 |
| 3 — Contest Entry | Enroll the first Pokémon; run the regional introduction and verify all 5/10/15/20 empty top-screen accessory slots for its rank. | — |
| 4 — Dress-up | Place accessories, finish, and accept a readable Dance 1 demo pointer. | 4 |
| 5 — Dancing | Answer six dances, wait through two lead dances, and complete the Acting transition. | 5 |
| 6 — Acting | Play four rounds, confirm the overworld, and check the target ribbon. | 6 |

Startup and post-contest memory reads use **X → read → X**, with existing menu timings
(**12 held / 90 released emulator frames** by default). Screen checks and accepted
endpoints use the closed-menu overworld. Stage 2 reuses the startup snapshot.
Player save-page 1 starts with Options; its low four bits encode Slow/Normal/Fast as 0/1/2.
Non-Fast or unreadable settings stop at this boundary with the captured evidence.

Planned startup soft-warning series (future implementation): reuse this snapshot and
print each warning once to the Lua console while continuing the run. The first warning
is **fewer than 5 total owned contest accessory copies**; duplicates count, 0–4 warns,
and 5 or more is silent.

Accessory encoding follows [PKHeX's decoder](https://github.com/kwsch/PKHeX/blob/master/PKHeX.Core/Saves/SAV4.cs#L566-L615):
IDs 0–60 have packed four-bit quantities and IDs 61–99 have single-copy flags;
backdrops are separate. Resolve the regional save pointer, then ImageClips save-page
14 (size `0x83C`), with Fashion Case at page offset `0x7F4`.
The shared save-body offset `0x4E38` is not an absolute RAM address.

First-party HP honors the plaintext flag. Unflagged tails validate both raw and
PID-decoded HP, accepting one consistent pair with **0 ≤ current ≤ maximum ≤ 714 HP
points** and maximum > 0. Conflicting pairs abort paused with both values retained.

Stage 2 keeps the same target if its scarf is absent. A failed equip check aborts.
`stage2_bag.py` tracks first/subsequent Bag openings and the remembered cursor;
item positions are refreshed after giving or swapping a scarf.

Gameplay defaults live in the [regional timing profiles](#regional-timing-profiles);
`super_contest.py` owns host deadlines and `--help` lists overrides.

| Setting | Default |
| --- | --- |
| Whole-run / per-visit deadline | 7,200 / 300 host seconds; each visit is capped by remaining whole-run time |
| Dressing grid / budget | 8×8 (also supports 6×6 and 7×7) / 15 game seconds |
| Pickup / drag / release / page-render wait | 4 / 12 / 5 / 30 emulator frames |
| Acting rounds 1–3 / final round and results | 64 / 140 game seconds |
| Main/debug dance input offset | −7 emulator frames |
| Debug saves / dance save-overlay wait | On / 3 host seconds |

Run from the repository root with a fresh run name. `--background` starts the sole
caller; `--wait` waits for its result. Normal checks and retries run locally.

```powershell
python -B contest/super_contest.py --plan-only
python -B contest/super_contest.py --background --run-name full-01 --total-timeout 7200
python -B contest/super_contest.py --wait --run-name full-01
```

## Stage checkpoints and resume

Use the [shared checkpoint protocol](../README.md#runner-planning-and-checkpoints).
Receipts bind run, visit, frame, epoch, and source evidence to a confirmed pause.
The next section resumes that accepted state; each boundary is evaluated once.

- `debug=0` writes **zero quick saves**, including errors and cleanup. `debug=1`
  saves the table's boundaries and stopped diagnostics in temporary slot 9.
  Accepted debug states remain; temporary slot 9 is restored at terminal cleanup.
- Routine pages, cues, feedback, and judge reads use state-free acknowledgements.
  `CheckpointClient.acknowledge(..., slot=None)` records `saved=false`;
  a checkpoint requiring a state cannot accept that receipt.
- Stage 3→4 and Stage 4→5 add **zero handoff/render frames**. Stage 5→6 uses the
  [render preparation](#stage-6-retry-render-preparation) before judge analysis.
- Dance native-save overlays settle for **3 host seconds** with native pause and
  released inputs, adding **zero emulator frames** under the existing deadline.
  Retain fresh before/after captures at the same frame/epoch before cue analysis.

## Stage 4 — Dress-up and transition

Each entry chooses **0–7 initial down-page taps**, completed before accessory analysis.
A tap costs **2 held + 1 released + 30 render frames**; navigation shares the
**15-game-second / 897-frame** dressing budget, anchored to the confirmed Stage 3 entry.
Sample each grid center once per fresh paused page (**64 centers** in the default
**8×8 grid**), then drag its queue with the configured pickup/move/release timings.
Rank limits are **5 / 10 / 15 / 20 accessories**. The deadline reserves the released tail.
Start another page only when its complete tap/release/render cycle fits the remaining
budget; otherwise wait with inputs released to the dressing deadline.

Finish when full, after **14 collection page flips**, or out of time. Initial random
taps are excluded from the flip guard; each collection flip completes its render cycle
before Finish. Then mash A for **40 game seconds / 2,393 frames**
at **10 held / 20 released**. Advance input-free in one-game-second steps until a
fresh capture has the green progress-bar tip and an exact Dance 1 pointer
**8 ≤ x < 128**. This accepted frame/position anchors dancing with **zero speed-probe
frames**. Pixel templates and coordinates belong to `dress_pixels.lua` and `screen_checks.lua`.

## Stage 5 — Dancing

`rank_dance.py` / `.lua` use `dance_timings.fixed_timing(category, rank)` on every
entry. Six responses contain **3 cues each for Normal, 4 for other ranks**.
For entry frame `E`, pointer `p`, speed `v`, and zero-based dance `i`:

```text
M[0] = E + max(1, floor((127 - p) / v))
M[i] = M[0] + i * cycle_frames + floor(i / 2) * extra_switch_frames
```

Lua stops at counted midpoint frames instead of polling running images. Fresh captures
re-anchor later midpoints at `capture_frame + floor((127 - x) / v)`. An early pointer
gets counted input-free `ceil((128 - x) / v)` corrections with fresh captures; the preferred
window is **x=128–129**. An unreadable or non-progressing correction skips the response;
a late capture schedules only inputs that still fit. Corrections use the pointer
read from the paused capture; screenshots need not have identical bytes.
Dance predicates, analysis errors, and callback
failures warn in Lua and continue input-free through the remaining counted response.
Account for every opportunity as executed or skipped, including empty responses;
retain failure captures. Continuations reuse the normal response and handoff actions,
preserving their counted endpoints without repeating completed inputs or waits.

The full runner checks **only Dance 1's first input**: baseline immediately before
the press, then feedback after **2 held / 10 released frames** (press +11 frames).
Excellent recognition uses the bubble's **cyan/yellow/pink palette**, independent
of caption lettering. Absent or stale palettes get frame-by-frame recovery within
the existing eligible feedback window. Each checked executed input has one final
**Excellent or Miss** judgment; recovery samples never add judgments. Every Miss
warns and continues, including across successive contests. All-button tools check
each executed input. Skipped inputs and unreadable cue/midpoint evidence remain
separate from Miss counts. A skipped first input warns and is never replaced by a
later check.

Dances 7–8 are input-free for `2 * cycle_frames + extra_switch_frames`, followed by
the regional B-mashing transition at **10 held / 20 released**. JPN and English use
**18 game seconds / 1,077 emulator frames**.
Stage 6's first prepared capture establishes Acting readiness.

### Rank-selected timing

The selected JSON's `fixed_dance_metrics` section owns these values;
`dance_timings.py` derives the frame schedule. Gaps are emulator frames from
response end **x=248** to the next demo start **x=8**; speeds are native pixels/frame.

| Category / ranks | Speed | Same-leader gap | Leader-change gap |
| --- | ---: | ---: | ---: |
| All Normal/Super | 8/15 | 30 | 269 |
| Cool Hyper/Master | 5/9 | 0 | 216 |
| Beauty Hyper/Master | 1/3 | 0 | 357 |
| Cute Hyper/Master | 2/3 | 24 | 214 |
| Smart Hyper/Master | 7/12 | 29 | 245 |
| Tough Hyper/Master | 5/8 | 28 | 232 |

`cycle_frames = floor(240/v + same_leader_gap)`, `response_frames = floor(120/v)`,
and `extra_switch_frames = leader_change_gap - same_leader_gap`.

### Fixed-profile cue selection

Pair demo/response cues at **120 pixels for Cool/Beauty Hyper/Master; 128 otherwise**.
Native button templates require **80% agreement**.

- Both sides complete: prefer **Demo, Demo, Response, Response**;
  Normal uses **Demo, Response, Response**. These preferences resolve conflicts.
- One side complete: use its positions and fill missing counterparts.
- Both incomplete or either over-counted: keep confirmed agreeing pairs only.
  Excess pairs retain the first two and last two (Normal: first one and last two).
- Skip unresolved, already-due, overlapping, or changed-pixel inputs. Every response
  receives a fresh paused checkpoint and acknowledgement.

## Stage 6 — Acting, results, and retry

After render preparation, the first capture must match **all 15 empty star cells**
exactly and read each available move's category using six exact color probes per slot.
Cache categories for the attempt; exclude the initial empty-slot blacklist.
`acting_pixels.lua` owns the native pixel templates and `acting_plan.lua` owns selection.

Before each round's move inputs, capture all three judges and choose the first with
**exactly 4 stars**, otherwise the fewest stars, breaking ties in judge order.
With 4 stars, prefer the lowest eligible move matching the contest category;
otherwise use the **1 → 2 → 1 → 2** fallback. Exclude empty slots and the last-used
move from round 2 onward; an unavailable fallback uses the lowest available alternatives.
The 2×2 move grid and judge menu remember the last actual selection.

Confirm the judge before repeated B taps at **10 held / 20 released emulator frames**
for the configured game-time segment between rounds and through results. After round
4, confirm the overworld and read the target ribbon using the strict first-party reader.
A miss re-enters Stages 2–6 from the current overworld; every new Acting entry resets
round/move/judge tracking to **1 / 1 / 1** and reads categories again.

After **5 consecutive ribbon misses**, warn once in the Lua console asking whether
the first Pokémon has good contest stats and suitable moves, then keep retrying.
A ribbon win resets the warning streak.

### Stage 6 retry render preparation

- **Direct Stage 5 handoff:** advance **10 input-free emulator frames**, confirm
  native pause, then request a fresh zero-frame capture. Full runs use this on every entry.
- **Standalone entry-state load/retry:** validate the paused source, unpause with
  inputs released for **one full host second**, then confirm native pause, positive
  frame progress, and the same epoch before a fresh zero-frame capture.

Record the preparation method and actual frame/host-time cost under the existing
deadline. Analyze judges only afterward; cached images cannot establish readiness.
Unreadable captures remain paused with evidence.

## Component tools

Use `--help` for tool options and runner `--plan-only` output for source/deadline requirements.
Prepared sources are preserved; temporary native slot 10 is backed up and restored.

| Tool | Purpose |
| --- | --- |
| `stage1_load.py` … `stage6_act.py` | Standalone stages with explicit checkpoint/handoff sources. |
| `dance_master.py` | Fixed-source Cool/Master six-response demonstration; inputs are executed or skipped, with Excellent/Miss feedback and warning-only continuation. Shared offset −7 frames; AVI recording is opt-in with `--avi`. |
| `rank_dance.py` | Main category/rank dance metrics; `--category` selects the prepared target and `--calibrate-speed` enables measurement. Historical source evidence remains in `dance_timings.json`. |
| `debug_contest.py` | All 20 category/rank pairs or `--category` / `--rank` filters; `--loops` repeats. Checks every executed input and reloads its baseline after each contest, including the last. |
| `scarf_tester.py` | Twenty random scarf equips, excluding the currently held scarf on every draw. Verifies the held-item ID and closed menu after each equip; preserves the remembered Bag menu selection and item cursor across cycles. |
| `debug_pointer_speed.py` | Input-free saved-entry pointer measurements using actual elapsed frames and entry reloads. |
| `rank_speed_probe.py` | Prepared-rank speed measurements and counted Dance 1 response trials; `--measure-only` keeps inputs released. |
| `pt_dance_read.py` | Reads native 256×192 top-screen or 256×384 stacked PNG/GD images. |

The fixed demonstration expects native **slot 5** to match
[`states/source-slot-5.ds5`](states/source-slot-5.ds5), at frame **9791**. Install it
with the emulator closed after backing up the existing slot. It requires `--host-timeout`.
Standalone Acting shares the main runner's **64 / 140 game seconds** and uses
entry-state reloads on ribbon misses.
Its prepared budget is supplied explicitly as `--host-timeout 1200` (host seconds).

Debug options include `--measure-only` for input-free observations,
`--measurement-dances 1` for cue-spacing reads, `--midpoint-diagnostics` for single-correction
records, and `--no-timeout` to disable both run timers. Normal completion accounts for
executed plus skipped inputs; it does not imply all feedback was Excellent.

Run the retained scarf tester with `python -B contest/scarf_tester.py --execute`
(or `--plan-only` / `--check`). It loads the prepared battery once, uses shared input
defaults, and finishes paused in the overworld after twenty acknowledged equips.
Each draw excludes the first Pokémon's verified held scarf. Native quick saves/loads
and contest entry are disabled; Bag context follows the current game throughout.
Closed-menu evidence uses matching stable lower-screen Poketch points from the
accepted Stage 1 view.
The current aggregate report is `contest/scarf-test-results.txt`; live receipts and
evidence use a fresh `tmp/contest/<run-name>/` directory. Failed checks retain paused evidence.

## Run folders and cleanup

The main `run_contest.py` launcher creates a fresh timestamped
`tmp/contest/contest-<timestamp>/` folder containing its mailbox, stage visits,
checkpoint receipts, screenshots, and final result. Keep the whole current folder
through success, failure, and interruption.

Startup takes the single-caller lock, opens DeSmuME, attaches Lua, and announces
cleanup in the Lua console. `artifacts.cleanup_run_folders()` then deletes every
older subfolder under `tmp/contest/`, regardless of outcome, before Stage 1 begins.
The current folder and the parent lock file remain. Folder deletion replaces the
main runner's per-file indexing, metadata rewrites, and cleanup after ribbon wins.

Move evidence to `contest/debug/` before the next launch to preserve it. This folder
is outside `tmp/` and has the same location in the working project and release.
Packaging includes its empty placeholder, not local evidence. Prepared component tools still
use recorded per-file artifact ownership for their own cleanup.

## Test statistics and retention

Keep one current aggregate `.txt` report per test/comparison: scope, settings, units,
totals, success/miss/unreadable/skipped counts, and relevant category/rank/input breakdowns.
For midpoint tests include corrections and residual errors; distinguish accepted-window
results from exact pixels and estimated frame errors from measured crossings.
Complete matching tests before comparative conclusions. After analysis and owner exit,
verify the report, clear analyzed scratch/exports, and remove superseded statistics.
Keep run histories out of design documentation. Offline checks do not prove live behavior.
