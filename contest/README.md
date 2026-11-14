# Platinum Super Contest guide

Standalone contest automation using Japanese Platinum (`CPUJ`). Its prepared
**dancing component** reads and answers **six dances: two for each of three opposing
Pokémon**. Success requires four fresh Excellent judgments per dance, **24 in total**.

The [Super Contest master](#super-contest-master) extends this into a full
ribbon-collection loop using the six stage runners.

## Dancing setup and run

Use the repository's [emulator setup and ownership rules](../README.md#setup-and-ownership).
Python 3.10+, Pillow, DeSmuME 0.9.13 x64, `lua51.dll`, and the local Japanese
Platinum ROM are required. Run commands from the repository root.

The prepared native **slot 5** must match [`states/source-slot-5.ds5`](states/source-slot-5.ds5).
To install it, close the emulator, back up its existing slot 5, and copy this state
to `desmume-0.9.13-win64/StateSlots/Pocket Monsters - Platinum (Japan).ds5`.
The runner checks the file before launch and frame **9791** after loading.

Preview without opening the emulator:

```powershell
python -B contest/dance_master.py --plan-only --host-timeout 120
```

Run a fresh demonstration with the agreed **120-host-second** limit:

```powershell
python -B contest/dance_master.py --execute --run-name demo-01 --input-slot 5 --checkpoint-slot 10 --finish-slot 10 --host-timeout 120
```

Choose a fresh `--run-name` each time. The master creates `tmp/contest/<run-name>/`,
launches or uses the owned emulator, loads slot 5, and sets **100% speed**, the
frame limiter on, and fast-forward off. All image reading, input scheduling, and
normal checkpoint decisions run locally without agent monitoring.

It pauses after the sixth response or an unexpected failure; there is no automatic
replay. `--host-timeout` is required and measured in host seconds. Native slot 10
is temporary checkpoint storage and is restored afterward. The source slot is
preserved; run-specific states, images, plans, and `runner-result.json` stay in the
run directory. Finalization releases inputs, stops movie mode, and restores normal speed.

## Optional AVI recording

**Recording is off by default.** Only `--avi PATH` enables it; an ordinary run
opens no AVI dialogs. `--uncapped` independently disables the frame limiter.
Use a fresh run name and output path for each recording:

```powershell
python -B contest/dance_master.py --execute --run-name recording-01 --host-timeout 120 --uncapped --avi recording-01.avi --osd-settle-seconds 3
```

Order: **load ROM → load slot 5 paused → verify the fresh source frame → set run
speed → start AVI → complete six dances → pause and restore 100% → stop AVI**.
The AVI uses uncompressed video and PCM audio. Cleanup also stops the run's
recording after an unexpected abort; native slot restoration follows finalization.
Root AVI/MP4 outputs are local artifacts and are Git-ignored.

- **Native resume:** the AVI dialog automatically resumes emulation when it closes.
  Lua is armed first to stop at the exact two-frame setup endpoint. The host handles
  an early pending pause once, under the existing host deadline. Source validation
  uses a fresh paused RPC rather than cached status.
- **Fading overlays:** native save/record messages and the slot indicator change
  pixels on host time, even while game frames are paused. At uncapped speed these
  can invalidate exact framebuffer checks. `--osd-settle-seconds 3` lets the
  two-second message timer, whole-second expiry, and slot fade finish before each
  demo/response section. This opt-in wait defaults to zero, counts against the
  host deadline, and advances no emulator frames.
- **Video timing:** uncapped execution changes host time, not recorded playback
  speed. The prepared full run spans **2,995 video frames**, about **50.06 seconds**
  at the native rate, including the setup refresh.

### Conversion and validation

Use the original AVI for final exports to avoid an extra lossy encoding pass.
This example keeps the native 256×384 resolution and exports 10 fps with audio:

```powershell
$ffmpeg = "D:\Program Files\yt-dlp_win\ffmpeg.exe"
$ffprobe = "D:\Program Files\yt-dlp_win\ffprobe.exe"
& $ffmpeg -nostdin -n -i "recording-01.avi" -vf "fps=10" -c:v libx264 -preset medium -crf 18 -pix_fmt yuv420p -c:a aac -b:a 192k -movflags +faststart "recording-01-10fps.mp4"
& $ffprobe -v error -count_frames -show_entries "stream=codec_type,width,height,avg_frame_rate,nb_read_frames,duration:format=duration,size" -of json "recording-01-10fps.mp4"
```

Check the AVI with the same probe command using its path: expect 2,995 decoded
video frames. The 10 fps export rounds to **501 frames / 50.1 seconds**, with audio
near the original duration. DeSmuME AVI input can report `Packet too small (16)`
while all expected video frames decode; verify the exported file's full decode,
frame count, and audio duration. A frame-capped stream-copy remux can truncate the
final audio packet, so check both streams after remuxing.

For smaller files at the same resolution and frame rate, raise CRF and use a slower
preset, or try HEVC (`libx265`) / AV1 (`libsvtav1`). Higher CRF trades image quality
for size; CRF values are codec-specific. Smaller-codec starting points are HEVC
`-crf 30 -preset slow` or AV1 `-crf 36 -preset 4`. Reduce AAC to `-b:a 64k`, choose
mono with `-ac 1`, or omit audio with `-an` for further savings.

## Timing and visual checks

The supplied slot-5 state binds `dance_master.py`'s preserved timing schedule:

| Setting | Emulator frames |
| --- | ---: |
| First response boundary after slot 5 | 193 |
| Same-Pokémon response-boundary spacing | 432 |
| Extra spacing when switching Pokémon | 213 |
| Each response half | 216 |
| Button hold / preserved input offset | 2 / −6 |

For zero-based dance `i`, the response boundary is
`9791 + 193 + 432*i + 213*(i//2)`. The six boundaries are **9984, 10416, 11061,
11493, 12138, 12570**; the final endpoint is **12786**.
A cue at demo position `x` is pressed at `boundary + round((x-8)*9/5) - 6`.
`--hold-frames` and `--offset-frames` override those two input settings per run.
`dance_master.DEFAULT_OFFSET_FRAMES` owns the preserved **−6-frame** demo/standalone
default. Main and debug dancing use `dance_timings.DEFAULT_OFFSET_FRAMES = -7`.
A different starting save requires a new timing anchor.

Demo screenshots are sampled every six frames, with final boundary captures.
The master accumulates each dance's actual four cues, checks visual progress
against counted frames, and schedules that response. Button patterns are read
live. Feedback follows the player from **top right → top middle → top left**.

Each demo/response boundary uses the shared paused checkpoint API. Lua binds the
host's pixel evidence to a fresh zero-frame framebuffer, validates the plan or
feedback, then waits for saved-state acknowledgement before proceeding. These
are screenshot checks, not memory-proven game phases.

## Rank-selected timing

[`dance_timings.py`](dance_timings.py) owns the shared `FIXED_METRICS` and **−7-frame**
input offset. `fixed_timing(category, rank)` resolves the same profile for main and debug.
Select **the accepted target's category and rank** on every contest entry, including
full-contest retries; Lua binds both to that target.

| Category / ranks | Pixels / emulator frame | Same-leader gap | Leader-change gap | Cycle | Response |
| --- | ---: | ---: | ---: | ---: | ---: |
| All Normal/Super | 8/15 | 30 | 269 | 480 | 225 |
| Cool Hyper/Master | 5/9 | 0 | 216 | 432 | 216 |
| Beauty Hyper/Master | 1/3 | 0 | 357 | 720 | 360 |
| Cute Hyper/Master | 2/3 | 24 | 214 | 384 | 180 |
| Smart Hyper/Master | 7/12 | 29 | 245 | 440 | 205 |
| Tough Hyper/Master | 5/8 | 28 | 232 | 412 | 192 |

Durations are **emulator frames**. Gaps run from response end **x=248** to the next
demo start **x=8**; one-frame spreads use the lower gap, wider spreads the floored mean.
For speed `v`, `cycle_frames = floor(240/v + dance_gap_frames)`,
`response_frames = floor(120/v)`, and
`extra_switch_frames = leader_gap_frames - dance_gap_frames`.
Input-free Dances 7–8 take `2*cycle_frames + extra_switch_frames`.

The full master anchors this fixed profile at its fresh paused
[Dance 1 demo pointer](#stage-4--dress-up-and-transition), with **0 entry speed-probe
frames**. [`dance_timings.json`](dance_timings.json) retains older rank-only standalone
profiles and compact native facts. Optional `pointer_calibration()` /
`calibrated_timing()` helpers retain their exact 60-frame input-free contract.

[`rank_dance.py`](rank_dance.py) / [`.lua`](rank_dance.lua) account for **three cue
opportunities for Normal, four for every other rank**: **18 / 24 across six responses**.
Main/debug use [fixed spacing and count-aware selection](#fixed-profile-cue-selection),
fresh paused pointer alignment, and explicit skips for unusable inputs. See the
[entry-anchored schedule](#stage-5--dancing). Diagnostic observations retain original
frame numbers/readability flags; unreadable intervals remain part of elapsed time.

### Prepared six-response calibration

Prepare slot 1 for Super/Great or slot 2 for Hyper/Ultra. At **100% speed**, load
paused, run **0.5 host seconds** input-free, pause, and capture the fresh initial
position. `--calibrate-speed` derives a robust rate from readable first-demo
positions, then keeps it fixed while checking **all 24 inputs for Excellent**.
Omitting that flag uses the stored profile; `--pointer-speed` supplies a per-run rate.
Super and Hyper have six-response native coverage. The JSON retains the earlier
Normal result and preserved Master evidence.

```powershell
python -B contest/rank_dance.py --rank Super --input-slot 1 --calibrate-speed --host-timeout 300
python -B contest/rank_dance.py --check
# After gameplay approval and an ownership check; choose a fresh run name:
python -B contest/rank_dance.py --execute --run-name super-six-01 --rank Super --input-slot 1 --calibrate-speed --host-timeout 300
```

Use `--rank Ultra --input-slot 2` for Hyper. The **300-host-second** prepared budget
covers one source's setup and six responses. Source files are preserved; temporary
slot 10 is backed up/restored. Results, frame-indexed readability records, plans,
judgments, and states use a fresh `tmp/contest/<run-name>/`. Failure stops paused
with retained evidence and a review request.

The optional [`rank_speed_probe.py`](rank_speed_probe.py) measures both prepared
slots with **0.5-host-second rendering / 1-host-second measurement** windows.
`--measure-only` stops after measurement; `--inspect-image PATH` reads a saved image.

### Category and rank dance debugging

[`debug_contest.py`](debug_contest.py) runs all **20 combinations**, category-major
(**Cool → Beauty → Cute → Smart → Tough**), then **Normal → Super → Hyper → Master**.
It reads six demonstrations and accounts for the rank's response opportunities:
**18 for Normal, 24 for every other rank; 450 across the complete pass**.
Usable inputs execute; skipped inputs retain their reasons and count toward completion.
`--category` selects that category's four ranks; adding `--rank` selects one combination.
Great/Ultra alias Super/Hyper. `--loops N` repeats the selected sequence N times,
reloading the same run-owned baseline after each contest.

1. Start uncapped from the prepared battery save. After loading, **B held 12 / released
   180 frames** dismisses the recent summary; save that accepted overworld as this run's
   baseline. Its retained file is the reload source. **Native slot 0** (`.ds0`, API
   slot **10**) is backed up/restored and serves as the native save/load transport.
2. Enter the selected contest with the full prepared menu and continuous **50-game-second
   introduction** sequence, then confirm dress-up once. The prepared first Pokémon must
   be eligible for all selected ranks; skip ribbon reads and scarf setup.
3. On confirmed dress-up entry, immediately tap **おわり** at **(230,173), 2 held /
   1 released frames**, then mash **A for 40 game seconds (2,393 frames), 10 held /
   20 released**, continuously in the same command. Only afterward, repeat **60 input-free
   frames → pause → fresh screenshot** until the first readable dance demo.
4. Use fixed measured speed/gaps with entry-anchored midpoint scheduling. Debug captures
   target **x=127 with floored travel**, then use the existing counted input-free adjustment
   for early arrivals. The preferred midpoint window is **x=128–129**. A late capture
   schedules the inputs that still fit; an unreadable pointer or a correction without
   forward progress skips the response using its counted endpoint. Retain a fresh
   zero-frame PNG/GD at every midpoint and adjustment. Every category/rank uses **2 held frames**,
   **10 released frames before feedback**, and the shared **−7-frame offset** (`--offset-frames`).
5. Every input uses the shared scheduled feedback mechanics: a baseline capture on
   the frame before the press, then the first feedback capture after the complete
   **2-held / 10-released** block (**11 emulator frames after the first held frame**).
   Unreadable/stale text is recaptured each frame until fresh feedback, the frame before
   the next input, or the current response end. The final cue always receives its full
   released wait; its recovery deadline is the later of response end and scheduled feedback,
   with any extension ending before the next demo. Retain every recovery image. Record
   Excellent, timing misses, or exhausted unreadable feedback and continue. Inputs whose
   timing or fresh cue evidence is unusable are skipped; a missed feedback window is
   recorded as unreadable. Each executed input receives one feedback result.
6. Acknowledge the sixth response's fresh, released-input completion boundary and
   **reload slot 0 immediately**, including after the final selected contest. An empty
   response waits input-free to its counted endpoint. The pass finishes paused at the baseline.

Main and debug share the [fixed timing table](#rank-selected-timing), fresh-midpoint
re-anchoring, and [count-aware cue selection](#fixed-profile-cue-selection).

Temporary `dance-results.json` indexes all targets, executed/skipped input counts,
conflicts, feedback totals, final completion captures, and baseline reloads. Per-response
plans, judgments, skip reasons, and protected PNG/GD images stay under each Stage 5 visit.
Completion means all opportunities were executed or skipped; it does not imply Excellent feedback.

**Opt-in midpoint diagnostics:** `--midpoint-diagnostics` retains pre/post positions,
signed pixel errors, and estimated frame errors from the unchanged fixed speed.
An early capture receives **one existing counted correction**; any residual early,
late, or unchanged pointer is recorded and the response continues from those actual
pixels. No second correction is applied. Unreadable positions use the counted input-free
skip. Per-dance `midpoint-diagnostic.json` and completed contest records in
`dance-results.json` distinguish the accepted **128–129 window** from exact **x=128**.
When no correction is needed, pre/post reference the same fresh zero-frame capture.

One persistent caller/bridge and the existing **7,200 total / 300 per-stage host-second**
deadlines apply (`--total-timeout`, `--stage-timeout`). All gameplay waits use emulator
frames; no overlay host wait is added. **`--no-timeout` disables both run timers** for a
completion/manual-close pass; closing the emulator ends the host and restores temporary
slot storage without issuing commands to the closed window.

```powershell
python -B contest/debug_contest.py --plan-only
python -B contest/debug_contest.py --plan-only --category Beauty --rank Ultra
python -B contest/debug_contest.py --check
python -B contest/debug_contest.py --background --run-name dance-inputs-20-01 --no-timeout
python -B contest/debug_contest.py --wait --run-name dance-inputs-20-01
python -B contest/debug_contest.py --background --run-name beauty-midpoints-10-01 --category Beauty --loops 10 --midpoint-diagnostics --offset-frames -7 --no-timeout
python -B contest/debug_contest.py --wait --run-name beauty-midpoints-10-01
```

To continue with unvisited targets, add `--skip-joined-from PATH` with a prior
`dance-results.json` or `measurements.json` report; repeat it for multiple reports.
Every non-pending target is excluded, including an interrupted entry.

`--measure-only` retains the input-free measurement pass: 120-frame capture intervals,
readable-pixel speed/gap fits, and a final Pokémon 2 / dance 1 demo capture. Its final
contest stays paused at that capture. `measurements.json` and `samples.json` retain
the original frame numbers, load epochs, uncertainty ranges, and protected images.
Add **`--measurement-dances 1`** for cue-spacing surveys: track every input-free
Dance 1 frame, capture the first response and first readable Dance 2 entry, then
reload the baseline immediately after every target, including the final one.
`observation-frames.jsonl` retains the frame/input audit. The default two-dance
measurement mode accepts `--resume-paused-from RUN_DIRECTORY`, with a matching single
target and retained first-demo evidence ending at the exact current paused framebuffer.

Use a fresh run name under `tmp/contest/<run-name>/`. Only the post-load baseline
saves a state; observation checkpoints use state-free acknowledgements. Terminal
cleanup releases inputs, restores normal speed, and leaves the emulator paused.

### Saved-entry pointer-speed measurement

[`debug_pointer_speed.py`](debug_pointer_speed.py) / [`.lua`](debug_pointer_speed.lua)
reuse debug enrollment and entry checks for **Smart Master, then Tough Master** with
one ROM reset between targets. Each takes **10 distinct random waits** from 20 frames
through 60% of the reference **demo + response** duration, excluding the inter-dance gap.
Save the fresh paused entry once, advance exactly the selected input-free frames,
capture a fresh zero-frame endpoint, acknowledge it, and reload the entry without a
rendering advance. Fit endpoint position against actual elapsed emulator frames with
a free intercept. Unreadable endpoints or a traversal reset abort paused with evidence.
The total host deadline is **1,800 seconds**; success finishes paused at the restored
Tough entry.

`--tough-only` restarts just Tough Master from fresh loading with the same ten-sample
wait window, deadline, and entry-reload policy. Use a fresh run name:

```powershell
python -B contest/debug_pointer_speed.py --plan-only
python -B contest/debug_pointer_speed.py --check
python -B contest/debug_pointer_speed.py --background --run-name pointer-speed-20-01 --total-timeout 1800
python -B contest/debug_pointer_speed.py --wait --run-name pointer-speed-20-01
```

## Test statistics and retention

Use this policy for all contest tests, including future category/rank comparisons.

- Keep **one readable plain-text (`.txt`) findings report** for the current test or
  comparison. Update it as related tests finish; replace superseded statistics.
- State the test scope, repetitions, relevant settings/overrides, and units, then
  aggregate contest/visit/input totals and success, miss, and unreadable counts.
- Break misses down by relevant category/rank and stage/dance/input position.
  Report how many missed and by how much, including error direction and units.
  Label fixed-speed frame estimates as estimates rather than measured crossings.
- For midpoint tests, include before/after position counts, correction attempts,
  correction successes, and remaining early/late misses. Distinguish the accepted
  midpoint window from the exact first pixel.
- Summarize what the statistics support and what remains unresolved. Defer
  comparative or causal/timing judgments until the requested matching tests are
  complete.
- Raw per-input/per-visit records, screenshots, and exports are temporary working
  evidence during execution, resume, and failure review. After analysis is
  complete and scratch has no live owners, verify the text report, clear project
  `tmp/` and `.tmp/`, and delete screenshot ZIPs, raw JSON/CSV exports, and
  superseded statistics. Retain the aggregate report as the results artifact.

## Image reader

### Standalone reader

```powershell
python -B contest/pt_dance_read.py "tmp/dance.png" --dance 1
```

Accepts native **256×192 top-screen or 256×384 stacked PNG/GD** images.
`--details` includes pixel evidence; `--annotate tmp/marked.png` marks detections.
The caller supplies the dance number, 1–6.

Preferred cue sources are **left for 1–2, right for 3–6**, with per-cue fallback
and cross-checks. Conflicts stop by default. The standalone reader's explicit
`--conflict-policy master` can retain disagreement while choosing a readable
preferred source; the preserved demo runner uses the strict policy.

Both button-cue views accept **80%** template agreement, including localized tint
and altered probe pixels. The standalone policy accepts agreeing sides or one
readable side; conflicting readings or two unreadable sides stop paused.
Rank-aware standalone plans prefer readable right markers and bind their evidence
to the fresh paused capture. They resolve a unique mirror offset from two or more
agreeing pairs, fill missing cues from the opposite side, and discard unmatched excess
detections. Genuine paired conflicts still stop. The previous accepted spacing or rank
hint is usable only when consistent with the current cues and required count.
### Fixed-profile cue selection

Main and debug pair cues at **120 native pixels for Cool/Beauty Hyper/Master; 128
for all other combinations**. The shared reader uses these rules:

- With both sides complete, prefer **Demo, Demo, Response, Response**; Normal uses
  **Demo, Response, Response**. Resolve conflicts using those input-position preferences.
- If only one side is complete, use its positions and fill missing counterparts.
- If both sides are incomplete or either has excess detections, use **confirmed
  agreeing pairs only**. Excess pairs keep the first two and last two (Normal: first
  one and last two), with the same side preferences.
- Skip unfilled, already-due, overlapping, or changed-pixel inputs. Each response,
  including an empty one, receives a fresh paused checkpoint and acknowledgement.

The standalone `ProgressTracker` intersects visual frame intervals with counted advances;
`DemoAccumulator` retains observed cues through occlusion. Both reset per dance
and after loading/resetting. Still images give intervals, not universal exact
frame timestamps; unreadable and counted-only evidence remain labeled.

## Files and verification

- `dance_master.py` — complete host runner, timing arithmetic, and image integration.
- `dance_master.lua` — input scheduling, feedback, and paused checkpoints.
- `rank_dance.py` / `.lua`, `dance_timings.py` / `.json` — shared fixed metrics, optional standalone calibration, and persistent Stage 5 sections.
- `debug_contest.py` / `debug_measure.lua` — fixed-metric 20-contest input pass and optional input-free measurements.
- `pt_dance_read.py` — standalone pixel reader and per-dance tracking.
- `runtime.py`, `dance_pixels.lua`, `feedback-templates.lua` — native deadline and feedback helpers.
- [`../host/avi.py`](../host/avi.py) — opt-in native AVI dialogs and recording finalization.
- [`beauty-contest-findings.txt`](beauty-contest-findings.txt) — retained human-readable
  Beauty test statistics and provisional findings.
- [`cue-spacing-findings.txt`](cue-spacing-findings.txt) — retained category/rank cue
  spacing measurements and debug-reader verification.
- [`evidence/completed.ds0`](evidence/completed.ds0) and
  [`evidence/completed.png`](evidence/completed.png) — retained six-response endpoint.

```powershell
python -B contest/dance_master.py --check
python -B -m tests
```

## Six-stage chain

[`super_contest.py`](super_contest.py) runs callable stage actions/readers through
[`sections.py`](sections.py) inside one [`ContestRuntime`](runtime.py) and the
persistent [`super_contest.lua`](super_contest.lua) dispatcher. It runs Stage 1 once,
then repeats Stages 2–6 for each
missing first-party ribbon in category-major order. Each win continues from its
accepted Stage 6 state, retaining acquired ribbons, Bag-opening state, and cursor.
It stops paused when all 20 ribbons are owned, an unexpected failure occurs, or the
shared timeout expires. The final Stage 6 ribbon read can establish completion
without another Stage 2 visit.
The [integration contracts](#integrated-runner-design) give the implementation order
and component ownership.

| Global setting / CLI | Default | Contract |
| --- | ---: | --- |
| `dress_grid` / `--dress-grid` | `6` | One center sample per cell; supports 6×6 or 7×7. |
| `dress_seconds` / `--dress-seconds` | `50` | Automatic game-time window, including initial page navigation and excluding pauses; finish immediately when full. |
| `dress_pickup_frames` / `--dress-pickup-frames` | `4` | Held frames at each queued accessory's source. |
| `dress_drag_frames` / `--dress-drag-frames` | `12` | Held frames moving to the fixed destination. |
| `dress_release_frames` / `--dress-release-frames` | `5` | Released frames after each drag; also the deadline's released-input tail. |
| `dress_page_wait_frames` / `--dress-page-wait-frames` | `30` | Input-free render frames after the next-page tap. |
| `debug` / `--debug` | `0` | `0` saves only the required Acting-entry state in final slot 10; `1` also retains memory-check and stopped diagnostic states. Routine dance boundaries stay state-free in both modes. |
| `--osd-settle-seconds` | `0` | Optional zero-frame host wait before dance sections. |
| `true_retry` / `--true-retry` | `0` | An acknowledged ribbon miss loads accepted slot 10 and repeats Stage 6. With `1`, continue from the current overworld through ribbon/item checks and contest selection in Stages 2–6, without loading a state. |
| `total_timeout_seconds` / `--total-timeout` | `7200` | One whole-run host deadline, including setup, cleanup, and retries. |
| `stage_timeout_seconds` / `--stage-timeout` | `300` | Fresh budget for each stage visit, capped by remaining whole-run time. |
| `acting_round_seconds` / `--acting-round-seconds` | `64` | B-mashing game seconds for Acting rounds 1–3. |
| `acting_final_seconds` / `--acting-final-seconds` | `132` | B-mashing game seconds for Acting round 4 and results. |

Execution is **uncapped across all six stages**, including dressing, with fast-forward
off and counted emulator-frame inputs. Every stage continues from the current accepted
paused state through the same Lua attachment and checkpoint engine. Stage 3→4 and
Stage 4→5 add **zero handoff/render frames**. Stage 5→6 uses ten counted input-free
frames; an actual slot-10 retry uses one full input-free host second before confirmed
native pause and a fresh capture. Only Acting-only ribbon-miss retries load a state.
The [checkpoint policy](#stage-checkpoints-and-resume) selects retained states;
screen-only checks acknowledge frame-bound receipts. Slot 9 holds enabled diagnostic
states and is restored once at terminal cleanup when used.
`--offset-frames` uses the shared main/debug **−7-frame** dancing default.
Acting wait overrides use `--acting-round-seconds` for rounds 1–3 and
`--acting-final-seconds` for round 4, both measured in game seconds.

Stages 1 and 3 accept fresh native pixels against the reviewed hall and Finish-button
profiles in `screen_checks.lua`, with source `local_reference_pixels`. An unreadable
profile aborts paused, retaining `review-request.json` and its image for later review.
Stage 5 saves its completed scripted transition directly. Stage 6's first capture
matches **all 15 empty star slots exactly** and caches **all four move categories**.
Move selection follows the [Acting override and fallback](#stage-6--acting-results-and-retry).
These are pixel/scripted facts, not memory-proven phases. Main dancing uses scheduled
observations and defaults to
**zero overlay-settling seconds**; `--osd-settle-seconds` overrides that host wait per run.

```powershell
python -B contest/super_contest.py --plan-only
python -B contest/super_contest.py --check
python -B contest/super_contest.py --background --run-name full-01 --total-timeout 7200
python -B contest/super_contest.py --wait --run-name full-01
```

`--background` launches the sole host caller; `--wait --run-name full-01` waits
for its terminal result. Normal checks and retries run locally. Artifacts use fresh
`tmp/contest/<run-name>/`, with stage visits under `visits/sN-<visit>/` and shared
receipts/states under `checkpoints/`. Startup clears recorded inactive scratch;
every acknowledged win clears consumed transients before the next target. Durable
facts, handoffs, reference pixels, and removal manifests remain. Terminal cleanup
restores **100% speed, frame limiter on, fast-forward off**. Abort/timeout keeps current
stopped images and checkpoints.
The terminal result records initial/current ribbon counts and every ribbon acquired
during the run, including partial progress at a timeout.

The Lua window reports `Contest selected: <category> - <rank>` during preparation.
Completed stage checkpoints print `<stage name>: PASS - <success message>` using
**Loading, Preparation, Contest Entry, Dress-up, Dancing, Acting**. A ribbon miss or
abort prints `<stage name>: FAIL` once for that visit; diagnostic details stay in the
retained logs. Acknowledged wins print `Ribbon acquired: <category> - <rank>`, and
confirmed collection completion prints `All ribbons acquired`, including an already
complete starting collection. Routine page, dance, and round checkpoints use the logs.

## Super Contest master

**Current status:** Persistent integration and the 20-ribbon loop are implemented.
Offline checks cover shared ownership, direct handoffs, both retry routes, page
budgets, fixed category/rank metrics, first-feedback warning streaks, all debug targets,
optional calibration, scheduled observations, cleanup, and partial
timeout progress. Components and integrated winning transitions have native coverage;
the integrated fixed-metric scheduling path, full-contest retries, and complete collection
require live validation.
Target Japanese Platinum (`CPUJ`), collecting all **20 Sinnoh contest ribbons**
on the first party Pokémon. The user prepares that Pokémon, two suitable Acting
moves in slots **1 and 2**, and a save with the avatar facing a contest receptionist.
Dress-up follows the user-editable [chain settings](#six-stage-chain).

### Integrated runner design

**Implemented in dependency order.** The numbered contracts map to their owners.

#### 1. Establish the shared runtime and lifecycle

`runtime.py` owns one emulator/controller/client, **one Lua window/attachment**, speed,
rendering, movie controls, and terminal cleanup. `super_contest.lua` registers one
native callback set and one game-independent checkpoint engine. Initial ownership
setup stops an inactive prior script before clearing its scratch handles. Normal
stage changes use callable sections without launch/attach, Stop/Run, reset, or loads.
The [chain settings](#six-stage-chain) own speed and deadlines; each visit's budget
includes its retry load and preparation and is capped by the original total deadline.

#### 2. Convert stages into direct, acknowledged sections

`sections.py` reuses stage input plans, dressing helpers, six-response dancing, and
Acting preparation. `sections.lua` supplies load/prep/entry/Acting reads; Stage 4 and
the rank-aware dance Lua adapter expose callable sections inside the same dispatcher.
Flow is **1 once → 2 → 3 → 4 → 5 → 6**, returning from each win to Stage 2 in the
current overworld. Select **Cool → Beauty → Cute → Smart → Tough**, then
**Normal → Super → Hyper → Master** within each category. Share accepted boundary
facts and acknowledge each boundary once per visit. The
[save policy](#stage-checkpoints-and-resume) saves slot 6 for either result only with `debug=1`.

#### 3. Add the two ribbon-miss retry branches

Savestate loads are allowed only after an acknowledged ribbon miss:

| Retry mode | Load | Continue with | Paired context |
| --- | --- | --- | --- |
| Acting-only (`true_retry=0`) | Latest accepted **slot 10** for this contest | Stage 6 | Same target; reset round/move/judge tracking to the Acting-entry values. |
| Full contest (`true_retry=1`) | **None**; current Stage 6 overworld | Stages 2–6 | Fresh ribbon/item reads with current ribbon progress and Bag-opening/cursor state. |

Retain fresh preparation facts after each Stage 2 memory check; save slot 2 only with
`debug=1`. Pair slot 10 with those facts after each Stage 5, including when Stage 2
used a state-free receipt. Preserve slot 10 throughout Acting-only retries. Full-contest
retries re-read ribbons, prepare the required item, and select the contest from the
current state. Only actual slot-10 loads create a new load epoch and restore paired
context. Agree any deliberate retry timing variation; identical replay timings can
repeat a losing result.

#### 4. Make dressing automatic with budgeted initial navigation

Always place accessories automatically, finishing when full or after the configured
50-game-second window. `stage4_dress.py`/`.lua` own the counted page/drag section.

For every dress-up entry, including full-contest retries, choose one fresh random
integer **N from 0–7 inclusive**. Tap the down-page panel **N times before the first
accessory-analysis screenshot or drag**, then use the normal scan/drag sequence.
Zero means no initial taps. Default **2 held + 1 released + 30 render frames** cost
**33 frames per tap / 0–231 frames per entry**, all within the existing 2,991-frame
budget. `initial-navigation.json` records N and its executed cost with the visit.

#### 5. Bind rendering preparation to the actual entry path

`runtime.py` owns rendering policy; `stage6_act.prepare_render()` owns the explicit
entry preparation. Stage 3→4 and Stage 4→5 add **zero render frames**. Direct Stage
5→6 uses ten counted input-free frames. A real slot-10 retry uses **one full host second
unpaused**, released inputs, confirmed native pause, then a fresh zero-frame capture.
`render-preparation.json` records the selected path, measured frames, and pause.

#### 6. Add master-owned transient cleanup

`artifacts.py` implements the two [cleanup hooks](#transient-artifact-cleanup).
`overworld-reference.json` retains reviewed pixel points; `retry-context.json` and
`collection-context.json` retain compact source identities, states, party/Bag facts,
and next-target context. Later visits consume these facts without deleted images or
generated configuration files. Per-win cleanup preserves the current state/deadline.

#### 7. Verify the integrated ownership and transitions

`--check` compiles the host/Lua path. The focused checks in `tests/test_contest_runtime.py`
exercise the real Lua dispatcher with simulated memory/pixels, plus host collection,
speed/deadline ownership, both cleanup hooks, and paused failures. They verify zero
success-path loads, live full-retry preparation, debug save policy, state-free screen
receipts, Acting retry epochs/context, all eight initial page counts, released
deadline tails, native move-fill recognition, cached entry categories and retry reloads,
all four move slots, matching-category overrides and round-based fallbacks,
fixed category/rank profiles, cross-contest first-feedback warnings, skipped first
inputs and fully unreadable dances reaching the counted slot-10 handoff,
optional 60-frame calibration, scheduled midpoint adjustments, diagnostic observation, all 20 measurement
targets with baseline reloads and zero dance inputs/memory reads, skipped-image timing,
rejection of green costume pixels, per-button agreement/fallback, partial/empty debug
responses and unreadable-midpoint recovery through baseline reload, next-target progress
after cleanup, and one terminal native cleanup.

### Stage checkpoints and resume

Every successful **master boundary** ends with:
**pause → fresh zero-frame snapshot and Lua checks → save when required by policy →
acknowledge the final log → remain paused**.
The following stage starts by **unpausing** from that accepted checkpoint;
the master performs these handoffs automatically.

| Stage | Master save slot | Accepted endpoint |
| --- | ---: | --- |
| 1 — Load | — | Loaded overworld; reviewed screen receipt |
| 2 — Prep | 2, `debug=1` only | Target/scarf preparation memory check, or all ribbons already owned |
| 3 — Enter | — | Dress-up entry for the current target; reviewed screen receipt |
| 4 — Dress-up | — | Within Dance 1's demonstration; accepted green pixels and an exact paused pointer |
| 5 — Dancing | 10, always | Completed 15-second B transition; Acting retry source, with readiness checked by Stage 6 |
| 6 — Acting | 6, `debug=1` only | Overworld with the target ribbon result checked in memory |

**Slot 10 is the final native slot**, shown as **0** in DeSmuME and stored as `.ds0`.
It is the only savestate written with `debug=0`, regardless of retry mode. Preserve
it throughout Acting retries and terminal cleanup; replace it after the next Stage 5.
With `debug=1`, slots 2 and 6 also retain their latest accepted memory checks.
A completed contest acknowledges Stage 6 for either ribbon outcome before branching;
with `debug=0` this uses a state-free receipt. All-ribbons completion remains paused.

Accessory pages, cue reads, first-button feedback, judge-star captures, and all routine
full-run dance endpoints use state-free acknowledgements regardless of `debug`.
Unexpected stopped evidence can save temporary slot 9 with `debug=1`. The separate measurement
debug runner saves only its post-load baseline. Taking a screenshot alone never saves a state.

Log-only definitions use `save_state=false` and publish `status="awaiting_ack"`.
`CheckpointClient.acknowledge(..., slot=None)` retains the receipt and completes it
with `saved=false`, binding run/visit/frame/epoch and confirming pause. Lua evaluates
once and acknowledges the final log before authorizing inputs; required state
checkpoints publish `awaiting_save` and cannot accept a state-free receipt.
Stage results always include `checkpoint_receipt`; `save_state` is `null` for a
state-free boundary. Such boundaries use the current-state handoff rather than a reload.

#### Transient artifact cleanup

`owned-artifacts.json` records deletable files. Two master-owned hooks apply:

- **Fresh start:** after confirming ownership and that previous host callers have
  stopped, clear all stale runner-owned screenshots and temporary files from prior
  inactive contest runs before creating the new run's scratch artifacts.
- **Every ribbon win:** first acknowledge Stage 6 (saving slot 6 only with `debug=1`) and retain the ribbon result
  and next-target context, then clear all consumed screenshots and temporary files
  accumulated through that win, including earlier failed attempts. Continue the
  collection loop from the current state under the same deadline.

Registered PNG/GD captures, frame-sequence bytes/indexes, generated configuration,
annotations/contact sheets, and obsolete backups are transient. Preserve durable
states, receipts, compact facts, timing anchors, retry context, reference pixels,
prepared assets, and live Lua/mailbox/session/lock files. Unknown user files stay
outside the owned set. Cleanup manifests under `cleanup/` mark removed paths and
image-retention flags in durable metadata. Abort/timeout retains current stopped
evidence; the next fresh start clears recorded inactive transients. These file hooks
do not reset speed, Lua, game state, or deadlines.

### Stage 1 — Load the game

The following component entry points run standalone. The full master consumes their
actions/readers through the shared runtime described above.

[`stage1_load.py`](stage1_load.py) and [`stage1_load.lua`](stage1_load.lua) reuse the
shared input bridge and checkpoint master. Start with the prepared CPUJ battery
save. The runner launches Platinum or resets the sole controlled Platinum instance.

| Action | Held frames | Released wait frames |
| --- | ---: | ---: |
| Boot wait | — | 600 |
| First Start | 12 | 180 (about 3 seconds) |
| Second Start | 12 | 180 (about 3 seconds) |
| A | 12 | 180 (about 3 seconds) |
| B | 12 | 180 (about 3 seconds) |

The preserved boot and Start timings use the local contest defaults; every press
uses the normal 12-frame hold. A and B each have the requested 180-frame released wait.
B covers the optional Journal. The entire **1,368-frame** sequence runs continuously
with the **frame limiter off**, followed by its only gameplay check: the final paused screenshot.
Waits are emulator frames, independent of accelerated host execution.

```powershell
python -B contest/stage1_load.py --plan-only
python -B contest/stage1_load.py --execute --run-name stage1-01
```

Use a fresh run name. After B's wait, `tmp/contest/<run-name>/review-request.json`
identifies `stage1.png` and its paused frame/epoch. Review that screenshot for the
loaded overworld, then write `review.json` beside it with the request's `run_id`,
`frame`, and `epoch`, plus `verdict` (`pass` or `fail`), `source: "visual_review"`,
and a single-line `note`. The host remains the only emulator caller while waiting.
There are no automatic retries or host timeout.

Lua binds the verdict to that same paused frame/epoch and evaluates the checkpoint
once. A pass saves and acknowledges native **slot 1**. Failure evidence is retained
in the run folder and the previous slot-1 file is restored. `runner-result.json`
reports the terminal verdict. Both outcomes release inputs, stop movie mode,
restore 100% speed with the limiter on, and leave the emulator paused. Screen
readiness is visually reviewed, not inferred from memory fields.

Per-run frame overrides: `--boot-wait-frames`, `--hold-frames`,
`--start-wait-frames`, `--a-wait-frames`, and `--b-wait-frames`.

### Stage 2 — Select a ribbon and prepare its scarf

Read the first party Pokémon's contest-ribbon flags and choose the first missing
ribbon in the master order. If all are present, finish successfully.

| Category order | Required scarf | Down presses from Cool in Stage 3 |
| --- | --- | ---: |
| Cool | Red | 0 |
| Beauty | Blue | 1 |
| Cute | Pink | 2 |
| Smart | Green | 3 |
| Tough | Yellow | 4 |

If the correct scarf is already held, proceed to Stage 3. Otherwise, look up the
required scarf's Bag position and quantity. If absent, keep the current held item
and proceed with the **same target contest**.

If the scarf is available:

1. Open the Bag with **X → Down → Down → A** on its first opening after Stage 1;
   use **X → A** on subsequent openings because the main menu remembers Bag.
2. Navigate from the current highlight to the scarf's current item position.
3. If the Pokémon already holds an item: **A × 2 → wait 2 seconds → A × 4**.
   If it holds nothing: **A × 2 → wait 2 seconds → A × 2**.
4. Press **B → B** after equipping to close the menus completely.
5. Freshly read the first Pokémon's held item. A mismatch with the required scarf
   aborts paused with a message naming the expected scarf and actual held item.

Stage 2 Down taps use **12 held / 30 released frames**, including the first Bag
opening's two Down presses. Other menu taps use **12 held / 90 released frames**;
the explicit wait after A2 is **120 released frames**.

Keep one [`Stage2BagState`](stage2_bag.py) across Stage 2 calls. `opening_buttons()`
plans the input; `record_opened()` records a confirmed opening. Calls that skip
the Bag leave `opened_once` unchanged. A fresh Stage 1 load starts false; restore
the flag with the matching checkpoint context when resuming later.

#### Bag cursor tracking

Use the established cursor tracing:

- **From slot 1: Down presses = item position − 1.** The Bag remembers its cursor;
  from another slot, use target minus current position (Up if negative).
- Taking the final copy removes its entry and shifts following items up one slot.
  More than one copy leaves the entry in place. A returned old item already in the
  Bag merges into its existing stack.

For the **last item in the Bag, with exactly one copy**, record the cursor after
the **complete** give/swap sequence:

| Pokémon's held item before giving | A presses | Cursor afterward |
| --- | --- | --- |
| None | A × 2 + A × 2 | Moves up one slot |
| Old item with no existing Bag copy | A × 2 + A × 4 | Same position; the returned old item is now highlighted as the last item |

Both sequences retain the **2-second wait after the first two A presses**. The
swap row assumes the old item belongs to the **same Bag pocket**. Refresh item
positions and retain the resulting cursor for reopening; other cursor outcomes
remain to be confirmed during Stage 2 lockdown.

### Stage 3 — Enter the selected contest

The prepared avatar is in front of and facing the receptionist.

1. Press **A × 4** to reach rank selection, initially highlighting Normal.
2. Move Down according to the target rank:

   | Ribbon tier / rank | Japanese menu | Down presses |
   | --- | --- | ---: |
   | Base / Normal | ノーマル | 0 |
   | Super | グレート | 1 |
   | Hyper | ウルトラ | 2 |
   | Master | マスター | 3 |

3. Press **A** to reach category selection, initially highlighting Cool.
   Use the category Down count in Stage 2's table.
4. Press **A × 2** to reach Pokémon selection, then **A × 2** to select the first
   party Pokémon.
5. Repeatedly tap **A for 50 seconds** of normal-speed game progress.
   The expected endpoint is the dress-up screen with **about 57 seconds remaining**,
   leaving room for the following stage's 50-game-second automatic window.

[`stage3_enter.py`](stage3_enter.py) and [`stage3_enter.lua`](stage3_enter.lua) run
this section continuously, followed by one paused screenshot review. Menu taps use
the shared **12 held / 90 released** frames; A-mashing uses **10 / 20**. The
50-second segment rounds to **2,991 emulator frames** at the native frame rate.
[`party_contest.lua`](party_contest.lua) freshly reads the first Pokémon's validated
stored data and chooses the first missing Sinnoh contest ribbon in master order.
Omitting `--category` and `--rank` uses that target; supplied values must match it.
All 20 ribbons already owned ends successfully without enrollment.

`sections.custom_enter(runtime)` ports the continuous custom enrollment helper and
checks one fresh paused dress-up endpoint. It is **inert**: the master stage map uses
`sections.enter`, and no normal-run setting or CLI activates the helper.

```powershell
python -B contest/stage3_enter.py --plan-only --host-timeout 120
```

Execution adds `--execute` in place of `--plan-only`, a fresh `--run-name`, and
`--source-checkpoint` pointing to an acknowledged checkpoint receipt. Its retained
state loads through temporary slot 10. The required host deadline covers setup,
inputs, review, and acknowledgement. Review uses the same frame/epoch-bound
`review-request.json` and `review.json` contract as Stage 1. Success retains native
**slot 3**; abort restores its prior file. Both outcomes restore temporary slot 10,
release inputs, restore normal speed, and remain paused with run-specific evidence.

`--enrollment-check` captures rank/category selection, the party-selection prompt,
and dress-up without interrupting the input sequence. The endpoint review adds
observed `selected_rank`, `selected_category`, and `selected_party_slot`; Lua checks
these against the ribbon-derived target. `--background` returns at the review
boundary while the sole host caller waits. Explicit `--ribbon-test` manifests load
derived test states and restore prior slots afterward; they retain separate evidence.

For timing measurement, add `--timing-probe --intro-seconds 76`. Lua captures both
screens at **40 game seconds**, every **5 seconds** afterward, and at the endpoint,
measured from the start of A-mashing. Inputs remain continuous. The probe saves
native GD images, PNGs, a frame-indexed manifest, and `timing-contact-sheet.png`.
It finishes locally with a timing-only checkpoint for later visual review and
restores the previous native slot 3. Its result reports `stage3_accepted: false`.

### Stage 4 — Dress-up and transition

1. The master activates the dressing section at the accepted **current paused Stage
   3 frame** through its existing callbacks, adding **zero handoff/render frames**.
   A standalone source load instead uses **ten input-free render frames**, counted
   within the automatic window.
2. Choose a fresh **0–7** initial down-page tap count and complete its counted
   navigation before the first accessory read or drag. Then allow up to **50 game
   seconds / 2,991 emulator frames** from Stage 3, uncapped in the full master.
   The measurement debug runner skips dressing. Each paused page
   reads one fresh zero-frame framebuffer, samples every grid center once, and checks
   the rank's final indicator. The master acknowledges the page without a state
   (standalone: temporary slot 10), then drags the queue
   continuously with **4 pickup / 12 movement / 5 released frames**. The next-page tap
   uses **2 held / 1 released + 30 input-free render frames**. No drag-success check is
   performed; deadline cuts release the stylus within the window's final five frames.
3. When full or at the counted deadline, tap Finish and use uncapped execution for
   the transition.
   Repeat automatic placement on every contest entry under the shared host deadline.
4. Repeatedly tap **A for 40 seconds** of normal-speed game progress (**2,393 emulator
    frames**), using the existing **10 held / 20 released** cadence.
5. Advance without inputs for **one game second**, pause, and freshly check the
   dance-progress bar's **left tip** in Lua. Repeat locally until it is green or
   the shared host deadline expires. This handles variable entry timing across
   contest ranks. Use the four-row fill shape at **x=8, y=155–158** to distinguish
   the green tip from its white inner line and a flat green background.

The full runner accepts this fresh green-entry capture only with an **exact readable
pointer within Dance 1's demo** (`8 <= x < 128`). Lua binds the pointer and paused
framebuffer before accepting Stage 4. This same frame/position anchors Stage 5's
fixed category/rank profile; entry speed-probe cost is **0 emulator frames**.
Standalone Stage 4 accepts its first green check in **slot 4**. Optional callers can
request the retained exact 60-frame input-free pointer-calibration utility.

[`stage4_dress.py`](stage4_dress.py), [`stage4_dress.lua`](stage4_dress.lua), and
[`dress_pixels.lua`](dress_pixels.lua) implement the local runner with a required
host deadline. Touch coordinates: rectangle **(14,35)–(114,131)**, destination
**(192,80)**, next page **(66,160)**. Empty cells use RGB **(121,247,215)**. The
default grid is **6×6**; `--dress-grid 7` selects 7×7. Normal, Super, and Master sample
two pixels above the final indicator's center: pearl RGB **(8,189,239)**, heart RGB
**(231,0,115)**. Hyper samples the lower heart tip to avoid native save-text overlays:
heart RGB **(156,49,99)**, empty-tip background RGB **(255,115,222)**.

| Rank | Limit | Top-screen indicator sample |
| --- | ---: | --- |
| Normal | 5 | (118,110) |
| Super | 10 | (208,110) |
| Hyper | 15 | (118,136) |
| Master | 20 | (208,128) |

Routine page acknowledgements stay in the JSON logs; the Lua Stage 4 PASS line appears
only at the final Dance 1 handoff. An unreadable indicator aborts paused with retained
evidence. The Finish recommendation
is touchscreen **(230,173)**, **2 held / 1 released frames**. Coordinates and motion
timings have per-run overrides. Dressing/navigation, the A segment, and one-second
advances use counted emulator frames; paused host time is excluded from the dressing
budget and remains within the host deadline.

```powershell
python -B contest/stage4_dress.py --plan-only --host-timeout 120
python -B contest/stage4_dress.py --check
```

Execution replaces `--plan-only` with `--execute`, adds a fresh `--run-name`, and
uses `--source-checkpoint` for the accepted Stage 3 dress-up receipt.
The default standalone source loads through temporary slot 10 and uses the ten-frame
preparation. Standalone `--from-stage3` requires the accepted Stage 3 bridge
to remain attached and paused; it transfers callbacks with zero frame progress and
retains `source-handoff.json`. `--pickup-frames`, `--drag-frames`, `--drag-release-frames`,
and `--page-wait-frames` override motion timing; `--dress-seconds` overrides the game-time
window. Run-specific page images/arrays, counted input results, green-tip facts,
checkpoints, and `dance-handoff.json` stay under `tmp/contest/`.
The handoff identifies the accepted receipt/frame and pointer-derived demo-start and
response-start estimates/ranges; its final `runner-result.json` must pass before use.
The runner requires a host deadline and uses one attempt. It restores
temporary slot 10, releases buttons/stylus, stops movie mode, restores normal speed,
and leaves the emulator paused. An abort restores the previous native slot 4.
Existing loaded-source automatic placement has native coverage on the prepared
Master-rank save; initial random navigation, integrated direct entry, and wider ranks
require live validation.

### Stage 5 — Dancing

The full master's [`sections.py`](sections.py) calls the rank-aware
[`rank_dance.py`](rank_dance.py) / [`.lua`](rank_dance.lua) through its persistent
runtime. Select the [fixed profile](#rank-selected-timing) from `target.category` and
`target.rank`, then continue with **zero handoff frames**.
Stage 4's fresh demo capture supplies entry frame `E` and exact pointer position `p`.
With the profile's fixed pointer speed `v`, favor early arrival at **x=127**:
`M[0] = E + max(1, floor((127 - p) / v))`. For zero-based dance `i = 0..5`, use
`M[i] = M[0] + i*cycle_frames + floor(i/2)*extra_switch_frames`.

Lua stops at these **counted emulator frames**, using scheduled captures rather than
running-frame image polling. Each fresh paused midpoint supplies cues and pointer-based
input alignment using [fixed-profile cue selection](#fixed-profile-cue-selection).
Re-anchor later midpoints at `capture_frame + floor((127 - x)/v)`, applying the
same cycle and leader-switch gaps. If quantization leaves the pointer in the demo at x,
advance `ceil((128-x)/v)` input-free frames and recapture; retain each adjustment and
skip that response if the pointer is unreadable or fails to progress. A late capture
plays only inputs that still fit. `midpoint_timing` records the source/expected frame.
**Dance cue, pointer, input-timing, and feedback failures are non-fatal:** record skips
and continue through the counted response endpoints.

**Check feedback only after Dance 1's first button press.** Capture its fresh baseline
immediately before the press, then capture feedback after **2 held / 10 released frames**
(**11 emulator frames after the first held frame**, before input 2). Re-read stale or
unreadable text frame-by-frame within the bounded feedback window. The state-free
offset checkpoint acknowledges the result or explicit skip:

- **Excellent:** continue silently and reset the Good streak.
- **Good:** the first consecutive Good is silent; from the **second consecutive
  contest** onward, Lua prints `Dancing: WARN` and continues.
- **Failure:** Lua prints `Dancing: WARN`, resets the Good streak, and continues.
- **Unreadable after recovery / skipped first input:** report in Lua, reset the Good
  streak, and continue. A skipped first input is not replaced by a later feedback check.

The streak lives in the persistent main runtime and uses one first judgment per
contest. Acting-only retries do not replay Stage 5. Each demo supplies fresh
rank-specific cues; **no later button feedback is evaluated** by the full runner.

**Dances 7–8 receive no button or stylus inputs.** From Dance 6's counted endpoint,
wait `2*cycle_frames + extra_switch_frames`. Then mash **B for 15 normal-speed
game seconds**, rounded to **897 emulator frames**, using the shared **10 held / 20
released** cadence.

The full runner acknowledges this counted endpoint and saves final slot 10 directly.
Acting readiness shares Stage 6's first judge capture after its render preparation;
there is no separate move-screen check or external review wait at this handoff.

The first result, Good streak, and warning remain in `offset_check` and the visit
results; executed-input feedback uses `judgments.json`, a skipped first input uses
`first-input-result.json`. Completion accounts for **executed + skipped** opportunities
and **one judgment, or zero when input 1 was skipped**. The debug runner uses its
[all-button feedback and baseline-reload completion](#category-and-rank-dance-debugging).

#### Preserved standalone entry

[`stage5_dance.py`](stage5_dance.py) consumes the preserved 216-frame handoff format
through `dance_master.py` / `.lua`:

```powershell
python -B contest/stage5_dance.py --check
python -B contest/stage5_dance.py --plan-only --host-timeout $hostTimeoutSeconds --dance-handoff $danceHandoff
```

Set `$hostTimeoutSeconds` to the agreed host limit and `$danceHandoff` to the accepted
handoff path. Execution replaces `--plan-only` with `--execute` and adds a fresh
`--run-name`. `--uncapped` enables accelerated execution; `--transition-seconds`
overrides the B segment per run. The required host deadline covers setup, all
eight dances, transition, review, and acknowledgement; `run(..., deadline=...)`
also shares an absolute monotonic master deadline. One attempt runs locally.

At Acting entry, review `stage5.png` through the same frame/epoch-bound
`review-request.json` / `review.json` contract as Stage 1. A passing review completes
the shared checkpoint in **slot 5** and writes `acting-handoff.json`. Abort or timeout
retains paused evidence and restores the previous slot 5. Temporary slot 10 is
restored after every outcome; finalization restores normal speed. Run artifacts stay
under `tmp/contest/`. The Stage 4 handoff through the final Acting-entry checkpoint
is live-validated at 100% speed; accelerated operation awaits validation.

### Stage 6 — Acting, results, and retry

After loading the Acting-entry retry state (**slot 10 in the full master; slot 5
standalone**), unpause for **one full host second** with inputs released,
pause again, and take a fresh zero-frame capture for judge analysis. An initial
direct Stage 5 handoff uses **ten input-free emulator frames** instead. Both paths
follow the [render-before-analysis contract](#stage-6-retry-render-preparation).

The **first judge-selection capture of every attempt** must match all **15 empty
star slots at 100%**: all 25 pixels of each 5×5 cell, **375 exact RGB matches**.
A filled or unreadable initial cell aborts paused with that capture. This shares
the first round's normal screenshot and judge read. The same framebuffer identifies
and caches all **four move categories**, requiring six exact fill-color probes per
slot (**24 pixels / zero extra frames**). After acknowledgement, the full runner prints
**`Acting entry: PASS - All 15 empty star slots and four move categories confirmed.`**
before the first move/judge inputs.

The round-based fallback is **1 → 2 → 1 → 2**. From round 2 onward, exclude the move
actually used last round from both matching and fallback selection. If the scheduled
fallback is blocked, use the other move in slots 1/2. A matching-category override affects
only its current round. Initially, move 1 and judge 1 are highlighted; both menus
remember the last actually used slot. Moves use a **2×2 row-major grid**: 1/2 above
3/4. Navigation uses at most one horizontal tap followed by one vertical tap.

| Round | Fallback move | B-mashing after judge confirmation |
| --- | ---: | ---: |
| 1 | 1 | 60 seconds |
| 2 | 2 | 60 seconds |
| 3 | 1 | 60 seconds |
| 4 | 2 | **125 seconds total** |

For each round:

1. Before moving the cursor or selecting a move, pause and capture **all three
   judges**. Read fresh star counts from that same framebuffer: **four captures
   per attempt**, one before each move selection. If any judge has **exactly 4
   stars**, choose the first such judge. Otherwise choose the judge with the
   **fewest stars**, breaking ties by first judge. First means judge order 1, 2, 3.
2. If that judge has **4 stars** and an eligible cached move category matches the **current
   contest category**, use the **lowest-numbered matching move excluding the last-used slot**.
   Otherwise use the current round's unblocked fallback. Navigate from the remembered move slot, then press **A**
   to enter judge selection.
3. Move Left/Right relative to the remembered judge position, then press **A to
   confirm the judge** before starting the B-mashing segment.

All table durations are normal-speed game-time equivalents. After round 4's
125-second segment, expect contest completion and a return to the overworld.
Read the first party Pokémon's target ribbon flag at the Stage 6 checkpoint for
either outcome. The full master saves slot 6 only with `debug=1`; standalone always saves it:

- **Acquired:** report success and finish paused. After checkpoint acknowledgement,
  final logging, and finalization, remove this run's temporary screenshots, including
  earlier misses. Retain states, logs, star counts, move categories, input plans,
  and removal metadata.
- **Missing:** the full master follows its [retry mode](#six-stage-chain). Acting-only
  and standalone retries restore the **accepted entry state paused**, with its paired context,
  reset round/move/judge tracking to **1 / 1 / 1**, clear cached move categories,
  complete the [render-before-analysis sequence](#stage-6-retry-render-preparation), and repeat
  Acting for the same target under the continuing host deadline.

[`stage6_act.py`](stage6_act.py) and [`stage6_act.lua`](stage6_act.lua) reuse the input
bridge, checkpoint master, and validated first-party ribbon reader. **Accelerated
execution is supported with `--uncapped`**, which disables the frame limiter;
fast-forward stays off and all gameplay durations use counted emulator frames.
Finalization restores normal speed. The agreed standalone limit is
**20 minutes / 1,200 host seconds**, shared by setup, all rounds, and every retry;
there is no attempt-count limit by default. An agreed `--max-attempts` limits total
attempts; `--max-attempts 1` stops paused at the first completed result, including a
ribbon miss, retaining slot 6 and its images. The B segments use **3,590 / 3,590 / 3,590 / 7,478
emulator frames**, with the existing **10 held / 20 released** cadence. Menu taps
use **12 held / 90 released** frames, with per-run overrides.
`--round-seconds` overrides the first three B segments; `--final-seconds` overrides
the final segment. Later rounds require readable current star shapes rather than empty slots.

[`acting_pixels.lua`](acting_pixels.lua) checks all five native cells above each
judge: first-cell x positions **84 / 116 / 148**, five-pixel spacing, **y=8–12**.
Empty circles and complete gold/white star shapes must be readable and filled
left-to-right. Unknown star shapes stop paused with images. The return-to-overworld
check uses the reviewed native-image reference.

`acting_pixels.read_move_categories(raw)` requires a native stacked **256×384 GD**
framebuffer and returns the four categories plus per-slot RGB probe evidence. All
six probes must agree on one reviewed move-fill color:

| Category | Fill RGB |
| --- | --- |
| Cool | `(255,41,0)` |
| Beauty | `(41,181,255)` |
| Cute | `(255,99,206)` |
| Smart | `(66,181,41)` |
| Tough | `(247,206,0)` |

Probe x coordinates are **18/108** for left slots and **146/236** for right slots;
y coordinates are **220/244/280** above and **312/336/372** below. Unreadable or
conflicting colors stop paused at entry. Each attempt's `move-categories.json` binds
this read to its first capture's path/frame/epoch. Later rounds reuse those categories;
every retry reads them again after render preparation.

```powershell
python -B contest/stage6_act.py --check
python -B contest/stage6_act.py --plan-only --host-timeout 1200 --uncapped
```

Execution replaces `--plan-only` with `--execute` and adds a fresh `--run-name`,
`--acting-handoff` for Stage 5's accepted `acting-handoff.json`, and
`--overworld-checkpoint` for the reviewed prepared contest-hall receipt. Native
slot 5 must match that retained source. The default entry loads slot 5 paused;
`--from-stage5` instead takes over its current accepted paused endpoint. The Python
API exposes the same choice as `run(..., from_stage5=True)`. Both validate the source
frame and first-party target before their render preparation. Ribbon-miss retries
always reload slot 5 and repeat the one-host-second unpause under the same deadline.
Each pre-move checkpoint uses temporary slot 10; each completed contest saves slot 6
before branching. Slot 10 is restored
at finalization; the latest accepted slot-6 result is retained. Run artifacts live
under `tmp/contest/`; `image-cleanup.json` marks removed image references after success.

#### Stage 6 retry render preparation

**Implemented for initial entry and retries.** After **loading the Acting-entry retry state paused**:

1. Validate the frozen source. Release inputs, confirm native unpause, and run for
   **one full host second** at the selected speed. Check that native pause does not
   interrupt that interval; keep inputs released on every frame.
2. Pause again and confirm native pause. Validate positive emulator-frame progress
   and the unchanged source epoch. The frame count is measured rather than fixed;
   accelerated execution can advance more than 60 frames during the host second.
3. Request a **new `gui.gdscreenshot()` through a paused Lua menu callback**, adding
   zero frames. Convert that capture, then analyze the judge star slots and move
   categories.

An initial **direct Stage 5 handoff** uses the shared input bridge's `advance 10`
command instead of the host-timed window. Every subsequent retry-state reload uses the
one-second path. Both preparations count against the same host deadline.
Each attempt retains `render-preparation.json` with its method, measured endpoints,
host duration where applicable, and confirmed native pause. The round checkpoint
records the fresh capture's zero-frame cost. An earlier frame-completion capture or
conversion of cached `screen.gd` can remain grey despite visible gameplay; see
[screenshot freshness](../README.md#paused-screenshots). Unreadable fresh images stay
paused for review.

[`archive/paused-load-render.zip`](archive/paused-load-render.zip) preserves the
diagnostic `lab.py` and `observe.lua`. Extract them together into a direct child of
project `tmp/`. From a controlled paused slot-5 load with no Lua script attached,
`lab.py baseline` attaches the observer; `lab.py internal` demonstrates the counted
advance and fresh paused capture. That archived comparison uses **five frames**;
the current retry-state preparation uses the host-timed unpause described above.

### Live verification

Use the [runner-planning and checkpoint](../README.md#runner-planning-and-checkpoints)
contracts to agree on
the live attempt and finish point. Validate fixed category/rank profile selection,
scheduled midpoint correction, first-feedback warnings, and dressing/Acting readiness,
Bag state across winning transitions/full retries, host-timed retry-state capture, and complete collection
with success/timeout cleanup. Offline checks do not establish native gameplay behavior.
