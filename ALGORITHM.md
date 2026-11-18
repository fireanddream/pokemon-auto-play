# Contest runner algorithm

How the runner collects the first party Pokemon's 20 Platinum Super Contest
ribbons. See [readme.md](readme.md) for setup. Timings below are supplied JPN
defaults unless English values are named; regional profiles own editable defaults.

## Integrated runner design

Load the save once, then repeat **Preparation -> Entry -> Dress-up -> Dancing ->
Acting -> Result check**. Select missing ribbons by **Normal -> Super -> Hyper ->
Master**, each **Cool -> Beauty -> Cute -> Smart -> Tough**.

A win advances to the next missing ribbon; a loss retries the same target from
the current overworld. Preserve earned ribbons, Bag menu/cursor positions, and
the initial empty-move-slot list. One caller, emulator, Lua connection, and
checkpoint engine serve the whole run; no quick-loads or Lua restarts.
Finish paused on all 20 ribbons, unexpected failure, or timeout. Release inputs,
stop movie mode, and restore 100% speed with frame limiting enabled.

## Setup and ownership

Use Windows, standard x64 CPython 3.10-3.15, DeSmuME 0.9.13 x64, and its matching
`lua51.dll`. Keep `Battery/` and `StateSlots/` beside the emulator; the `.dsv`
filename must match the ROM stem. Save on the blue mat facing the middle receptionist.
The first Pokemon needs **HP > 0, Sheen > 0, at least two moves, and must not be an Egg**.
See [Poffin plans](buyable-poffin-guide.md) for stat preparation.

The launcher repairs its local `.venv`, installs Pillow from bundled offline wheels,
and remembers paths in `run_contest.py` (`--choose-paths` selects again). It rejects
a second caller, closes existing DeSmuME instances, and boots fresh before attaching
Lua. Gameplay is uncapped; messages appear in Lua and startup details in `tmp/startup.log`.

## Stage 1: Loading and readiness

Load the battery save, dismiss the connection prompt, and verify the closed-menu
hall from a fresh paused image. Use **X -> read party/Bag/options -> X**, with
**12 held / 90 released frames** per menu input. Non-Fast text speed, an ineligible
first Pokemon, or unreadable required data stops here. Stage 2 reuses this snapshot.

Warn once and continue for **fewer than 10 accessory copies** (duplicates count),
**Sheen below 255**, or missing scarves. Count scarves in the Items pocket and the
first Pokemon's held item. An unreadable accessory count warns as unknown.

## Stage 2: Preparation

Read ribbons and select the first missing target. Equip its scarf if available;
otherwise keep the target and continue. Verify an equip/swap, stopping on mismatch.
Track Bag opening/cursor state and refresh item positions after scarf transfers.

## Stage 3: Contest entry

Enroll the first Pokemon for the selected rank/category and tap A through the
regional introduction. Accept a fresh image showing **5 / 10 / 15 / 20 empty
accessory slots** for Normal / Super / Hyper / Master. This frame starts dressing;
the handoff adds zero frames.

## Stage 4: Dress-up and transition

1. Make **0-7 random initial down-page taps**, each costing
   **2 held + 1 released + 30 render frames**, before scanning accessories.
2. Scan each grid center once per fresh paused page: **8x8** by default, also 6x6/7x7.
   Drag detected accessories using **4 pickup / 12 drag / 5 release frames**.
3. Fit navigation and dressing into **20 game seconds / 1,197 frames** from entry.
   Start another page only if its full tap/release/render cycle fits; otherwise
   wait input-free to the deadline. Reserve time to release inputs.
4. Finish at the rank's accessory limit, after **14 collection flips** (excluding
   initial random taps), or at the deadline. Complete each flip's rendering first.
5. Tap A for **40 game seconds / 2,393 frames**, **10 held / 20 released**. Then wait
   input-free in one-game-second steps for a fresh green progress-bar tip and
   Dance 1 pointer at **8 <= x < 128**. This frame/position anchors dancing without a probe.

## Stage 5: Dancing

Answer six dances: **3 cues per response in Normal, 4 otherwise**. Use the profile's
fixed speed and leader gaps, not live speed measurement. For entry frame `E`,
pointer `p`, speed `v`, and zero-based dance `i`, schedule response midpoints:

```text
cycle_frames = floor(240 / v + same_leader_gap)
extra_switch_frames = leader_change_gap - same_leader_gap
response_frames = floor(120 / v)
M[0] = E + max(1, floor((127 - p) / v))
M[i] = M[0] + i * cycle_frames + floor(i / 2) * extra_switch_frames
```

Speed is native pixels per emulator frame; gaps run from response end `x=248` to
next demo start `x=8`. Values live in the profile's `fixed_dance_metrics` section.

### Midpoint capture and cue selection

- Pause at counted midpoints, not by polling running images. Re-anchor at
  `capture_frame + floor((127 - x) / v)`. Correct early captures input-free by
  `ceil((128 - x) / v)`, aiming for **x=128-129**. If a same-epoch correction leaves
  the pixel unchanged, keep its verified pre-correction frame/position for deadlines
  while reading fresh cues. Unreadable/backward corrections skip the response.
- Match button templates at **70% agreement**. Pair sides at **120 pixels for
  Cool/Beauty Hyper/Master; 128 otherwise**. Both sides complete: prefer
  **Demo, Demo, Response, Response** (Normal: **Demo, Response, Response**).
  One complete side fills missing counterparts. Both incomplete or either
  over-counted: keep agreeing pairs only; excess pairs retain first two/last two
  (Normal: first one/last two).
- Convert cues to absolute press frames with **-7-frame offset** (early) and
  **2-frame holds**. Skip unresolved, overdue, overlapping, or changed-pixel inputs.
  Errors warn and continue input-free to the counted endpoint. Record every input
  as executed or skipped, retain failure captures, and never repeat completed actions.

### Feedback and Acting transition

Check only **Dance 1's first input**: baseline before pressing, feedback after
**2 held / 10 released frames** (press +11). Excellent uses cyan/yellow/pink bubble
colors, not text. Recover absent/stale feedback frame-by-frame within its existing
window for one final Excellent/Miss judgment. Misses warn and continue; skipped
inputs are separate and never replaced by later checks. Diagnostic tools can check all inputs.

Leave dances 7-8 input-free for `2 * cycle_frames + extra_switch_frames`, then tap B
for **18 game seconds / 1,077 frames**, **10 held / 20 released**, toward Acting.
Stage 6's fresh prepared capture must still confirm readiness.

## Stage 6: Acting, results, and retry

After render preparation, require **15 empty initial star cells**. Read each move's
contest type and raw hearts from six color probes/heart cells; cache for this
attempt, excluding empty slots. Unknown move data warns once per move. All reads
use pixels/colors, not OCR. Each of four rounds reads three judges and the player's
acting position (1-4) from the same fresh capture:

1. Exclude empty slots, the previous move, and unknown moves. Allow zero hearts
   only when one recognized legal move remains.
2. Acting first/second: if a matching-category move is eligible and a judge has
   **exactly 4 stars**, use the first such judge and highest-heart matching move.
3. Otherwise use the fewest-star judge and highest-heart eligible move.
   Ties prefer judge order and lowest move slot.
4. No recognized legal move: warn and use the first legal slot/fewest-star judge.
   Unreadable judges/order, no legal slot, or recognized but ineligible moves stop paused.
5. Navigate from remembered move/judge selections, confirm, then tap B at
   **10 held / 20 released frames** through the configured round/results segment.

Segments last **67 / 140 game seconds** for JPN rounds 1-3 / final round and results;
English uses **74 / 150**. Selection uses raw hearts, not simulated move effects.

Confirm the closed-menu overworld, use **X -> read -> X** to check the target ribbon,
and acknowledge before returning to Stage 2. Each new Acting entry resets round/
move/judge to **1 / 1 / 1** and rereads moves. After **5 consecutive ribbon misses**,
warn once about stats/moves and continue under the original deadline; a win resets the streak.

### Stage 6 retry render preparation

Use **accepted paused source -> input-free preparation -> confirmed native pause ->
fresh zero-frame capture -> analysis**. Direct handoffs advance **10 emulator frames**;
standalone loads/retries unpause for **one full host second**, confirming positive
frame progress and the same epoch. Count preparation under the existing deadline.
Cached images cannot prove readiness; unreadable required captures stay paused with evidence.

## Runner planning and checkpoints

Agree on actions, readable boundaries, branches, guards with units, live attempt,
and finish point before runner changes. Ask before shared timing or retry/timeout changes.

Once per boundary visit: **pause -> fresh zero-frame snapshot -> Lua checks -> save
if required -> acknowledge final log -> proceed/abort**. The next stage reuses the
accepted boundary. Receipts bind run, visit, frame, epoch (game-state generation),
and actual evidence source to confirmed pause. Unreadable required checks stop paused.
Paused Lua `gui.gdscreenshot()` captures add zero frames; preparation is counted separately.

## Stage checkpoints and resume

| Accepted boundary | Native save slot with `debug=1` |
| --- | ---: |
| Stage 2: target/scarf prepared | 2 |
| Stage 4: Dance 1 entry accepted | 4 |
| Stage 5: Acting transition complete | 5 |
| Stage 6: overworld/result checked | 6 |

`debug=0` makes **no quick saves**, even on failure/cleanup. `debug=1` retains accepted
states and restores temporary diagnostic slot 9 at cleanup. Routine checks acknowledge
without saves; these receipts cannot satisfy state-required checkpoints. Savestates
are not in-game saves or direct memory writes. Prepared tools preserve sources and
back up/restore temporary slot 10.

Stage 3->4 and 4->5 add zero handoff/render frames. Dance save overlays settle for
**3 host seconds** paused with inputs released; fresh before/after captures must
retain the same frame/epoch, adding zero emulator frames.

## Regional memory readers

Trusted ROM code selects JPN `CPUJ` or English `CPUE` (shared USA/EU) profiles;
party/Bag decoding is shared. Header acceptance/offline checks do not prove live coverage.
HP decoding honors the plaintext flag and requires a consistent pair:
**0 <= current <= maximum <= 714, maximum > 0**; conflicts stop paused.
Accessory quantities/flags exclude backdrops.

## Regional timing profiles

`contest/timing_profiles/CPUJ.json` and `CPUE.json` own stage, dance, and diagnostic
defaults. Validate once per caller against the running ROM. Preserve schema/keys
and restart after edits; CLI timing flags override defaults, including allowed zeros.

- `*_frames`: integer emulator-frame counts.
- `*_game_seconds`: **67027965 / 1120380 emulator frames per game second**.
- `*_host_seconds`: actual wall-clock time, even while the emulator is paused.
- Whole-run / per-stage-visit deadlines: **7,200 / 300 host seconds**; each visit
  is capped by remaining whole-run time. Retries never reset the whole-run deadline.

Stage order, slots, coordinates, and deadlines stay code-owned. Inspect settings:
`python -B contest/super_contest.py --plan-only --game-code CPUE`.

## Implementation boundaries

`contest/super_contest.py` owns the loop; `runtime.py` owns connection, deadlines,
speed, and cleanup. Stage/dance/Acting helpers own game rules. `host/` owns native
controls/receipts; `lua/` owns the input bridge, checkpoint engine, and memory helpers.
Adapters own targets, timings, slots, and screen policy; shared checkpoints stay game-independent.

## Run folders and cleanup

Keep current mailbox/capture/receipt/results in `tmp/contest/contest-<timestamp>/`
(`contest/tmp/contest/` in releases), including failures. The next launch removes
older run folders after emulator/Lua startup; move useful evidence to `contest/debug/`
first. Scratch/dependencies stay project-local; prepared states/evidence remain private.

## Verification

Run `python -B -m tests` (offline suite) or `python -B -m tests --release` (complete gate).
Use existing runner/component `--check`, `--help`, and `--plan-only` entries.
Offline tests use real Lua with simulated emulator callbacks, not live gameplay proof.
`tests/README.md` owns details; new/expanded retained tests need an agreed reason.

## Release packaging

The dependency list is declared in `host/bootstrap.py`.
From source, run `python -B tools/prepare_dependencies.py` (`--cached` uses a complete
cache) for all six Python-version wheels. Stage with
`python -B tools/package_contest.py --stage tmp/release-stage`, or build with
`python -B tools/package_contest.py --zip dist/contest-runner.zip`.
ZIP creation publishes only the candidate passing the complete gate;
`--lua-dll "path/to/lua51.dll"` supplies a matching DLL if needed.

Extraction contains `run_contest.py` and `contest/` (modules, profiles, wheels,
`readme.md`, `ALGORITHM.md`, Poffin guide). Paths start empty; `.venv`/`tmp` are
created locally. Emulator files, ROMs, private states/evidence, and debug contents are excluded.

## Test statistics and retention

Keep one current aggregate `.txt` report per comparison: scope, settings, units,
totals, success/miss/unreadable/skipped counts. Finish matching tests before conclusions;
after review/owner exit, verify the report and clear analyzed scratch/obsolete statistics.
Do not embed run histories here.

## Attribution

Platinum memory helpers derive from **Real96**, with layout edits by **SexyMalasada**.
The supplied [GPL v3 license](lua/PLATINUM-LICENSE.txt) is retained.
