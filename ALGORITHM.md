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
Then display `Total run time: HH:MM:SS` in Lua output and the host log, including
setup, pauses, retries, and cleanup (actual host time, not game time).

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
first Pokemon, or unreadable required data stops here. Stage 2 reuses this snapshot;
startup-only facts (HP, moves, Sheen, options, and position reference) remain cached.

Warn once and continue for **fewer than 10 accessory copies** (duplicates count),
**Sheen below 255**, or missing scarves. Count scarves in the Items pocket and the
first Pokemon's held item. An unreadable accessory count warns as unknown.

## Stage 2: Preparation

Use the fresh X-protected party/Bag snapshot from startup or the last contest finish
to select the first missing ribbon and equip its scarf if available; otherwise keep
the target and continue. Keep the existing cached Bag opening/cursor prediction,
including its last-copy removal adjustment.
Collection preparation does not reread memory after equipping, predict inventory,
or compare items with a cache.

## Stage 3: Contest entry

Enroll the first Pokemon for the selected rank/category and tap A through the
configured regional introduction baseline. The shared visual guard requires a
fresh paused image showing **5 / 10 / 15 / 20 empty accessory slots** for
Normal / Super / Hyper / Master. A mismatch waits input-free for **3 game seconds**
and rechecks through the [shared visual guard](#implementation-boundaries).
A match and the accepted frame's dressing handoff add zero frames.

## Stage 4: Dress-up and transition

1. Make **0-7 random initial down-page taps**, each costing
   **2 held + 1 released + 30 render frames**, before scanning accessories.
2. Scan each full-box grid center once per fresh paused page: **8x8** by default, also
   6x6/7x7. Compare its middle pixel with the matching position in the inferred
   [empty-box reference](contest/dress_background.png), including dotted and edge backgrounds.
   Drag detected accessories using **4 pickup / 12 drag / 5 release frames**.
3. Fit navigation and dressing into **20 game seconds / 1,197 frames** from entry.
   Start another page only if its full tap/release/render cycle fits; otherwise
   wait input-free to the deadline. Reserve time to release inputs.
4. Finish at the rank's accessory limit, after **14 collection flips** (excluding
   initial random taps), or at the deadline. Complete each flip's rendering first.
5. Tap A for **37 game seconds / 2,214 frames** (English **40 / 2,393**),
   **10 held / 20 released**. Check the fresh green progress-bar tip immediately;
   the shared visual guard retries mismatches input-free after **1 game second**.
   A match adds zero frames.
   Require a Dance 1 pointer at **8 <= x < 128**; this frame/position anchors dancing without a probe.

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
- Match button templates once per fresh image at **70% agreement**. Planning and
  evidence reuse that scan. Pair sides at **120 pixels for
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
inputs are separate and never replaced by later checks. Debug runs use the same first-input check.

Leave dances 7-8 input-free for `2 * cycle_frames + extra_switch_frames`, then tap B
for **18 game seconds / 1,077 frames**, **10 held / 20 released**, toward Acting.
Stage 6's fresh prepared capture must still confirm readiness.

## Stage 6: Acting, results, and retry

After [render preparation](#stage-6-retry-render-preparation), cache each available
move's contest type and raw hearts from six color probes and heart cells on the
accepted first-round frame. Unknown move data warns once per move. Reads use
pixels/colors, not OCR; each round reads three judges and the player's acting
position (1-4) from the same fresh capture:

1. Exclude empty slots, the previous move, and unknown moves. Allow zero hearts
   only when one recognized legal move remains.
2. Acting first/second: if a matching-category move is eligible and a judge has
   **exactly 4 stars**, use the first such judge and highest-heart matching move.
3. Otherwise use the fewest-star judge and highest-heart eligible move.
   Ties prefer judge order and lowest move slot.
4. No recognized legal move: warn and use the first legal slot/fewest-star judge.
   Unreadable judges/order, no legal slot, or recognized but ineligible moves stop paused.
5. Navigate from remembered move/judge selections, confirm, then tap B at
   **10 held / 20 released frames** through the configured base round/results segment.
6. Before every round, including Acting entry, check judges, acting order, and
   selectable move-button fills from a fresh paused image. Entry requires all
   **15 empty star cells**; exclude empty move slots always, and the gray previous
   move only before rounds 2-4.
   The shared visual guard retries mismatches with **3 game seconds of B**.
   On match, keep paused for the fresh round checkpoint and move/judge planning. Resume once for
   **3 more game seconds of B -> selection inputs -> base segment** in one authorized
   combo, with no intervening pause. Polls are silent; first-round messages and
   cached move scoring remain unchanged.

Base segments last **60 / 135 game seconds** for JPN rounds 1-3 / final round and
results; English uses **65 / 140**. Entry reuses the pre-round retry/settle timing settings.
Selection uses raw hearts, not simulated move effects.

After the final/results baseline and before the first X, take fresh zero-frame
paused captures. Together, match the six accepted startup player pixels at
**x=124/130, y=78/84/94** and the reviewed closed-dialogue pixels at
**x=124-130, y=146-148** (top border directly below the player, never text or distant NPCs).
The shared guard retries visual mismatches, including fades, with **3 game seconds
of B**. Once both match, tap B for **3 more game seconds** before X. Polls are silent;
an invalid capture cannot mean dialogue absent.

After every completed contest (win or loss), use **X -> read party/Bag -> X** once,
then confirm the closed-menu overworld and acknowledge before returning to Stage 2.
Read actual held item, Bag quantities/order, and ribbons for the next preparation;
retain full stored-record validation without rereading startup-only facts.
Each new Acting entry resets round/move/judge to **1 / 1 / 1** and refreshes only
move-scoring pixels. After **5 consecutive ribbon misses**,
warn once about stats/moves and continue under the original deadline; a win resets the streak.

### Stage 6 retry render preparation

Use **accepted paused source -> 10 input-free emulator frames -> confirmed native
pause -> fresh zero-frame capture -> analysis**, in the same epoch and under the
existing deadline. Cached images cannot prove readiness; unreadable required
captures stay paused with evidence. Reuse the accepted current-run party context;
missing or inconsistent facts stop without an entry memory read.

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
are not in-game saves or direct memory writes. There is no prepared-state or isolated-stage resume path.

Stage 3->4 and 4->5 add zero handoff/render frames. Before Dance 1, settle the
accepted Stage 4 save overlay for **3 host seconds** paused with inputs released.
Fresh before/after captures must retain the same frame/epoch, adding zero emulator frames.

## Regional memory readers

Trusted ROM code selects JPN `CPUJ` or English `CPUE` (shared USA/EU) profiles;
party/Bag decoding is shared. Header acceptance/offline checks do not prove live coverage.
HP decoding honors the plaintext flag and requires a consistent pair:
**0 <= current <= maximum <= 714, maximum > 0**; conflicts stop paused.
Accessory quantities/flags exclude backdrops.
All contest memory callers use profile Stage 2 X timings, a native paused read with
buttons/stylus released and zero frame progress, then close X before acceptance.
No X-button history is checked. Unreadable reads stay paused at their actual source.

## Regional timing profiles

`contest/timing_profiles/CPUJ.json` and `CPUE.json` own stage and fixed dance
defaults. Validate once per caller against the running ROM. Preserve schema/keys
and restart after edits; CLI timing flags override defaults, including allowed zeros.

Reserved and unused by the collection runner: `standalone_dancing`, `diagnostics`,
`stage4_dress_up.loaded_entry_render_frames`, `stage5_dancing.demo_sample_interval_frames`,
and `stage6_acting.loaded_state_render_host_seconds`. Their `_help` text identifies
them; keys and values remain for the locked schema.

- `*_frames`: integer emulator-frame counts.
- `*_game_seconds`: **67027965 / 1120380 emulator frames per game second**.
- `*_host_seconds`: actual wall-clock time, even while the emulator is paused.
- Whole-run / per-stage-visit deadlines: **7,200 / 300 host seconds**; each visit
  is capped by remaining whole-run time. Retries never reset the whole-run deadline.

Stage order, slots, coordinates, and deadlines stay code-owned. Inspect settings:
`python -B contest/super_contest.py --plan-only --game-code CPUE`.

## Implementation boundaries

`contest/super_contest.py` owns the loop; `runtime.py` owns connection, deadlines,
speed, Lua serialization, paused overlay settling, slot protection, and cleanup.
`sections.py` and the persistent `super_contest.lua` / `sections.lua` dispatcher
serve modular stage helpers. `dance_response.py` owns fresh pointer/cue reads and
fixed-spacing plans; `rank_dance.lua` owns Stage 5 checkpoints, including the Acting handoff.
`memory_boundary.lua` carries accepted party facts; `checks.py` declares sources
for main `--check` and packaging. `host/` owns native controls/receipts; `lua/` owns
the input bridge, checkpoint engine, and memory helpers.
Adapters own targets, timings, slots, and screen policy; shared checkpoints stay game-independent.

`host/guards.py` retries fresh paused visual mismatches under the existing section
deadline. Callers own buttons, retry/matched-settle durations, evidence, and checks.
Polls retain each actual capture without checkpoint visits/saves or deadline resets.
Invalid captures stop paused with evidence; only valid visual mismatches can retry.

### Locked main-runner-only consolidation

Every live run uses the full collection loop, including debugging. Do not add
isolated-stage, prepared-source, scarf-only, dance-only measurement/debug, or
AVI-specific execution paths or compatibility wrappers. Offline tests exercise
retained helpers and actual main-engine boundaries; keep modular helpers, not a monolith.

Regional `fixed_dance_metrics` is the only production dance timing source: no
historical-motion reads, per-contest calibration, live speed fitting, or alternate
observation/feedback modes. The checkpoint, render, input-accounting, save, and
deadline contracts above apply equally to debug runs and retries.

Keep `contest/dance_timings.json` as historical evidence, not a runtime or release
dependency. Preserve prepared states, private evidence, and attribution. Remove tests
solely for retired paths, without keeping obsolete APIs to satisfy them.

## Run folders and cleanup

Keep current mailbox/capture/receipt/results in `tmp/contest/contest-<timestamp>/`
(`contest/tmp/contest/` in releases), including failures. The next launch removes
older run folders after emulator/Lua startup; move useful evidence to `contest/debug/`
first. Scratch/dependencies stay project-local; prepared states/evidence remain private.

## Verification

Run `python -B -m tests` (offline suite) or `python -B -m tests --release` (complete gate).
Use the main runner's `--check`, `--help`, and `--plan-only` entries.
Offline tests use real Lua with simulated emulator callbacks, not live gameplay proof.
`tests/README.md` owns details; new/expanded retained tests need an agreed reason.
Live verification requires a separately agreed main-runner attempt.

## Release packaging

The dependency list is declared in `host/bootstrap.py`.
From source, run `python -B tools/prepare_dependencies.py` (`--cached` uses a complete
cache) for all six Python-version wheels. Stage with
`python -B tools/package_contest.py --stage tmp/release-stage`, or build with
`python -B tools/package_contest.py --zip dist/contest-runner.zip`.
ZIP creation publishes only the candidate passing the complete gate;
`--lua-dll "path/to/lua51.dll"` supplies a matching DLL if needed.

Extraction contains `run_contest.py` and `contest/` (retained modules, profiles, wheels,
`readme.md`, `ALGORITHM.md`, Poffin guide). Paths start empty; `.venv`/`tmp` are
created locally. Emulator files, ROMs, private states/evidence, historical motion data,
old statistics, and debug contents are excluded.

## Test statistics and retention

Keep one current aggregate `.txt` report per comparison: scope, settings, units,
totals, success/miss/unreadable/skipped counts. Finish matching tests before conclusions;
after review/owner exit, verify the report and clear analyzed scratch/obsolete statistics.
Keep superseded reports privately in `contest/debug/archived-reports/`; do not embed
run histories here or rewrite old results as current verification.

## Attribution

Platinum memory helpers derive from **Real96**, with layout edits by **SexyMalasada**.
The supplied [GPL v3 license](lua/PLATINUM-LICENSE.txt) is retained.
