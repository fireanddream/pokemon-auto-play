"""Persistent contest transitions, paired retries, counted pages, and owned cleanup."""

from copy import deepcopy
from contextlib import redirect_stdout
import io
from itertools import groupby, product
import json
import math
from pathlib import Path
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from contest import debug_contest, rank_dance, rank_speed_probe, sections, stage1_load, stage2_prep, stage3_enter, stage4_dress, stage6_act, super_contest
from contest.dance_timings import RANKS, calibrated_timing, fixed_timing, pointer_calibration, timing_for
from contest.artifacts import OwnedArtifacts, cleanup_stale
from contest.pt_dance_read import BUTTONS, CUE_SIDE_OFFSET, EMPTY_PROFILE, FILLED_PROFILES, LEFT_SPRITES, POINTER_SPRITES, RIGHT_SPRITES, _match, layout_for
from contest.runtime import ContestRuntime, TimedController
from contest.stage2_bag import Stage2BagState
from host.checkpoints import write_json
from tests.support.bridge import BridgeClock
from tests.support.lua51 import Lua51, literal
from tests.support.paths import ROOT, temporary_directory


TARGET = {"category": "Cool", "rank": "Master", "ribbon_bit": 3, "required_scarf_id": 260,
          "category_down_presses": 0, "rank_down_presses": 3, "party_slot": 1}
MOVE_FILLS = {"Cool": (255, 41, 0), "Beauty": (41, 181, 255), "Cute": (255, 99, 206),
              "Smart": (66, 181, 41), "Tough": (247, 206, 0)}
MOVE_BOXES = ((16, 216, 112, 284), (144, 216, 240, 284),
              (16, 308, 112, 376), (144, 308, 240, 376))
MOVE_PROBES = (((18, 220), (18, 244), (18, 280), (108, 220), (108, 244), (108, 280)),
               ((146, 220), (146, 244), (146, 280), (236, 220), (236, 244), (236, 280)),
               ((18, 312), (18, 336), (18, 372), (108, 312), (108, 336), (108, 372)),
               ((146, 312), (146, 336), (146, 372), (236, 312), (236, 336), (236, 372)))


class ContestClock:
    """Real dispatcher/sections/checkpoints; selected memory and visual inputs are simulated."""

    def __init__(self, retry_mode=0, rank="Master", observation_policy="scheduled", *, debug=super_contest.debug):
        profile = timing_for(rank)
        tier = RANKS.index(profile["rank"])
        self.target = {**TARGET, "rank": profile["rank"], "ribbon_bit": tier, "rank_down_presses": tier}
        dressing = stage4_dress.dress_plan(host_timeout=300)["automatic_dressing"]
        self.full_check = dressing["full_checks"][profile["rank"]]
        self.pearl = self.full_check.get("pearl_rgb", dressing["indicator_rgb"]["pearl"])
        self.heart = self.full_check.get("heart_rgb", dressing["indicator_rgb"]["heart"])
        self.clock = BridgeClock(ROOT / "lua" / "input_bridge.lua", game_code="CPUJ")
        self.config_id, self.boundary, self.sources = 0, None, {}
        self.saved_checkpoints, self.log_checkpoints = [], []
        self.observation_policy = observation_policy
        self.work = (ROOT / "tmp" / "offline-contest-runtime").as_posix()
        self.clock.lua.run(f"""
            local real_dofile, real_loadfile, old_open = dofile, loadfile, io.open
            configs, engine_count, registrations, stars_read, move_reads, page_reads = {{}}, 0, {{}}, 0, 0, 0
            target, held_item, won = {literal(self.target)}, 0, false
            native_raw = string.char(255,254,1,0,1,128,1,0,0,0,0)
                .. string.rep(string.char(0,121,247,215), 256 * 384)
            function pixel(x, y, rgb)
                local at = 13 + (y * 256 + x) * 4
                native_raw = native_raw:sub(1, at - 1) .. string.char(unpack(rgb)) .. native_raw:sub(at + 3)
            end
            function paint_acting_moves(categories)
                local fills, points = {literal(MOVE_FILLS)}, {literal(MOVE_PROBES)}
                for slot, probes in ipairs(points) do
                    for _, point in ipairs(probes) do pixel(point[1], point[2], fills[categories[slot]]) end
                end
            end
            pixel({self.full_check['x']}, {self.full_check['y']}, {literal(self.pearl)})
            pixel(8,155,{{24,199,24}}); pixel(8,156,{{97,215,97}})
            pixel(8,157,{{24,199,24}}); pixel(8,158,{{16,158,16}})
            capture_frames, feedback_read_frames, lua_messages = {{}}, {{}}, {{}}
            print = function(message) lua_messages[#lua_messages + 1] = tostring(message) end
            gui.gdscreenshot = function() capture_frames[#capture_frames + 1] = frame; return native_raw end
            touches = {{}}
            stylus = {{set = function(value)
                if value.touch then touches[#touches + 1] = {{frame = frame + 1, x = value.x, y = value.y}} end
            end}}
            for _, name in ipairs({{'registerbefore','registerafter','registermenustart'}}) do
                local original = emu[name]
                emu[name] = function(callback)
                    registrations[name] = (registrations[name] or 0) + 1
                    original(callback)
                end
            end
            function party(at)
                local flags, bits, count = {{}}, {{}}, 0
                for bit = 0, 19 do
                    flags[bit + 1] = bit < {tier} or won and bit == {tier}
                    if flags[bit + 1] then bits[#bits + 1] = bit; count = count + 1 end
                end
                local next_target = won and {{category='Beauty', rank='Normal', ribbon_bit=4,
                    required_scarf_id=261, category_down_presses=1, rank_down_presses=0, party_slot=1}} or target
                return {{frame=at, species_id=151, held_item_id=held_item, held_item='Red Scarf',
                    read_valid=true, data_validation_passed=true, is_egg=false, ribbon_flags=flags,
                    owned_ribbon_bits=bits, owned_count=count, all_ribbons_owned=false, next_target=next_target}}
            end
            dofile = function(path)
                if path:match('/bag_contest%.lua$') then
                    return {{new=function() return {{snapshot=function(at)
                        local items = {{}}
                        for index=1,6 do items[index]={{position=index,item_id=index,quantity=2}} end
                        for index=0,4 do items[7+index]={{position=7+index,item_id=260+index,quantity=2}} end
                        return {{frame=at,game={{code='CPUJ',supported=true}},party=party(at),bag={{items=items}}}}
                    end}} end}}
                elseif path:match('/party_contest%.lua$') then
                    return {{new=function() return {{read_first=function(at) return party(at) end}} end}}
                elseif path:match('/screen_checks%.lua$') then
                    return {{read=function(_, expected) return {{source='local_reference_pixels',
                        confirmed=offline_screen_confirmed ~= false,expected=expected}} end}}
                elseif path:match('/acting_pixels%.lua$') then
                    local module = real_dofile(path)
                    local read_moves = module.read_move_categories
                    module.read_move_categories = function(raw)
                        move_reads=move_reads+1
                        return read_moves(raw)
                    end
                    module.read_stars=function() stars_read=stars_read+1
                        local empty=first_judge_capture; first_judge_capture=false
                        acting_read_index=(acting_read_index or 0)+1
                        local counts=acting_star_counts and acting_star_counts[acting_read_index]
                            or (empty and {{0,0,0}} or {{0,4,1}})
                        return {{readable=true,all_empty=counts[1]==0 and counts[2]==0 and counts[3]==0,
                            counts=counts,selected_judge=module.choose_judge(counts)}}
                    end
                    module.match_points=function() return {{confirmed=true}} end
                    return module
                elseif path:match('/dance_pixels%.lua$') then
                    return {{bar=function()
                        if measurement_pointer_bounds then
                            return {{found=true,first_x=8,last_x=measurement_pointer_bounds[1],
                                position_bounds=measurement_pointer_bounds,white_flash=false}}
                        end
                        if measurement_unreadable then return {{found=false,first_x=256,last_x=-1,white_flash=true}} end
                        if calibration_pointer_x or rank_pointer_override then
                            local x = calibration_pointer_x or rank_pointer_override
                            return {{found=true,first_x=8,last_x=x,position_bounds={{x,x}},white_flash=false}}
                        end
                        if not rank_visual_origin then return {{}} end
                        if frame > rank_visual_origin and frame <= rank_visual_origin + 2 then
                            return {{found=false,first_x=256,last_x=-1,white_flash=true}}
                        end
                        local x = frame == rank_visual_origin and 64 or 128
                        return {{found=true,first_x=8,last_x=x,position_bounds={{x,x}},white_flash=false}}
                    end, feedback=function()
                        feedback_read_frames[#feedback_read_frames + 1] = frame
                        if rank_feedback_events then
                            for _, event in ipairs(rank_feedback_events) do
                                if frame >= event.frame and frame <= event.end_frame then
                                    return {{label=event.label,x=190,y=10,text_intensity=255,source='offline_text_shape'}}
                                end
                            end
                            return nil
                        end
                        if rank_feedback_due and frame < rank_feedback_due then return nil end
                        return {{label='Excellent',x=190,y=10,text_intensity=255,source='offline_text_shape'}} end}}
                end
                local module = real_dofile(path)
                if path:match('/checkpoint_master%.lua$') then
                    local original = module.new
                    module.new=function(options) engine_count=engine_count+1; return original(options) end
                elseif path:match('/dress_pixels%.lua$') then
                    local original=module.read_page
                    module.read_page=function(...) page_reads=page_reads+1; return original(...) end
                end
                return module
            end
            loadfile=function(path)
                if configs[path] then return function() return configs[path] end end
                return real_loadfile(path)
            end
            io.open=function(path, mode)
                if mode=='rb' then
                    if not mock_files[path] then return nil end
                    return {{read=function() return mock_files[path] end,close=function() return true end}}
                end
                return old_open(path, mode == 'ab' and 'a' or mode)
            end
            dofile({literal((ROOT / 'contest/super_contest.lua').as_posix())})
            return 'ready'
        """)
        self.call("init-run", "integrated-check", self.work, retry_mode,
            0 if observation_policy in ("measurement", "debug_inputs") else debug, observation_policy)

    def close(self):
        self.clock.close()

    def config(self, value):
        self.config_id += 1
        path = f"{self.work}/config-{self.config_id}.lua"
        self.clock.lua.run(f"configs[{literal(path)}] = {literal(value)}; return 'ready'")
        return path

    def call(self, action, *args):
        result = self.clock.checkpoint(action, *args)
        if result.get("error"):
            raise AssertionError(result["error"])
        return result

    def begin(self, stage, plan, **context):
        source = self.boundary
        config = {**plan, "stage": stage, "run_id": "integrated-check", "section_visit": self.config_id + 1,
            "work": f"{self.work}/s{stage}-{self.config_id + 1}", "source_frame": source["frame"] if source else 0,
            "source_context": {key: source[key] for key in ("checkpoint", "run_id", "visit", "frame", "epoch")} if source else None,
            "target": source["snapshot"].get("target", self.target) if source else self.target, **context}
        result = self.call("begin-section", self.config(config))
        return config, result

    def acknowledge(self, pending):
        record = pending["checkpoint"]
        saving = record["save_required"]
        slots = {"stage-2-prep": 2, "stage-5-dance": 10, "stage-6-act": 6}
        slot = slots.get(record["checkpoint"], 10 if self.observation_policy in ("measurement", "debug_inputs") else 9)
        suffix = f"ds{slot % 10}" if saving else "json"
        arguments = [record["run_id"], record["visit"], record["frame"], record["epoch"],
            f"{self.work}/checkpoints/visit-{record['visit']}.{suffix}", "true"]
        if not saving:
            arguments.append("false")
        accepted = self.call("complete", *arguments)
        self.boundary = accepted["checkpoint"]
        if not self.boundary["proceed"]:
            raise AssertionError(self.boundary.get("error", "Checkpoint rejected"))
        (self.saved_checkpoints if saving else self.log_checkpoints).append(deepcopy(self.boundary))
        return accepted

    def review(self):
        capture = self.call("capture")
        return self.acknowledge(self.call("visit", "pass", "visual_review", f"{self.work}/review.png", "Native fixture accepted.",
            capture["frame"], capture["epoch"]))

    def prepare_entry(self):
        load = stage1_load.load_plan(boot_wait_frames=1, hold_frames=1, start_wait_frames=1, a_wait_frames=1, b_wait_frames=1)
        self.begin(1, load)
        self.clock.command("combo", "inputs", *load["inputs"])
        self.review()
        self.prepare(opened_once=False, cursor=1)
        self.enter()

    def prepare(self, *, opened_once=True, cursor=7):
        self.begin(2, {})
        initial = self.call("inspect")["observation"]
        prep = stage2_prep.prep_plan(initial, bag_state=Stage2BagState(opened_once), cursor=cursor)
        self.call("init-prep", self.config(prep))
        if prep["inputs"]:
            self.clock.command("combo", "inputs", *prep["inputs"])
        self.clock.lua.run(f"held_item={prep['expected_item_id']}; return 'ready'")
        self.acknowledge(self.call("visit"))
        self.retain(2)
        return initial, prep

    def retain(self, slot):
        source = self.boundary
        prepared = source if slot == 2 else self.sources[2]
        context = {"target": source["snapshot"]["target"], "bag_context": deepcopy(prepared["snapshot"]["bag_context"]),
            "source_party": self.clock.lua.run("return dofile('" + (ROOT / "lua/json.lua").as_posix() + "').encode(party(frame))")}
        context["source_party"] = json.loads(context["source_party"])
        self.call("retain-context", slot, self.config(context))
        self.sources[slot] = deepcopy(source)

    def enter(self):
        target = self.boundary["snapshot"]["target"]
        plan = stage3_enter.entry_plan(target["category"], target["rank"], host_timeout=300, hold_frames=1, release_frames=1,
            intro_seconds=1 / stage4_dress.FRAME_RATE)
        self.begin(3, plan)
        self.clock.command("combo", "inputs", *plan["inputs"])
        self.review()

    def dress(self, taps, *, finish=False, calibrate=False, change_calibration=None):
        plan = stage4_dress.dress_plan(host_timeout=300, from_stage3=True, transition_seconds=1 / stage4_dress.FRAME_RATE)
        dressing = plan["automatic_dressing"]
        self.full_check = dressing["full_checks"][self.boundary["snapshot"]["target"]["rank"]]
        self.pearl = self.full_check.get("pearl_rgb", dressing["indicator_rgb"]["pearl"])
        self.heart = self.full_check.get("heart_rgb", dressing["indicator_rgb"]["heart"])
        if calibrate:
            plan["pointer_calibration_frames"] = 60
        else:
            plan["require_demo_pointer"] = True
        self.calibration_advance, self.calibration_inputs = None, []
        config, handoff = self.begin(4, plan, initial_page_taps=taps)
        self.call("init")
        if taps:
            self.clock.command("dress-initial-pages")
        if finish:
            self.clock.lua.run(f"pixel({self.full_check['x']},{self.full_check['y']},{literal(self.heart)}); return 'ready'")
        pending = self.call("dress-page-visit")
        progress = pending["observation"]["dressing"]
        self.acknowledge(pending)
        if finish:
            self.clock.command("finish-dressing")
            self.clock.command("dress-transition")
            self.clock.command("observe-frames", 60)
            self.call("check-dance")
            self.clock.lua.run("calibration_pointer_x=8; return 'ready'")
            capture = self.call("capture")
            calibration, pointer_x = None, 8
            if calibrate:
                first = {"frame": capture["frame"], "epoch": capture["epoch"], "pointer_x": 8,
                    "path": config["work"] + "/pointer-calibration-start.gd", "source": "offline_pointer_fixture"}
                self.clock.lua.run(f"mock_files[{literal(first['path'])}] = native_raw; return 'ready'")
                self.call("calibration-start", self.config(first))
                self.clock.reset_measurement()
                self.calibration_advance = self.clock.command("calibrate-pointer")
                self.calibration_inputs = self.clock.trace()
                self.clock.lua.run("calibration_pointer_x=28; return 'ready'")
                capture = self.call("capture")
                last = {**first, "frame": capture["frame"], "pointer_x": 28, "path": config["work"] + "/stage4.gd"}
                calibration, pointer_x = pointer_calibration(first, last), 28
                if change_calibration:
                    change_calibration(calibration)
            evidence = {"frame": capture["frame"], "epoch": capture["epoch"], "dance": 1,
                "source": "fresh_zero_frame_left_tip_pixels", "path": config["work"] + "/stage4.gd",
                "phase": "demo", "frame_range": [0, 0], "pointer_calibration": calibration,
                "pointer": {"status": "exact", "position_bounds": [pointer_x, pointer_x]}}
            self.acknowledge(self.call("visit", self.config(evidence)))
            self.clock.lua.run("calibration_pointer_x=nil; return 'ready'")
        return config, handoff, progress

    def act(self, *, win=False, loaded=False, rounds=4, move_categories=None, star_counts=None,
            entry_change=None, after_entry=None):
        plan = stage6_act.acting_plan(host_timeout=300, hold_frames=1, release_frames=1, from_stage5=not loaded,
                                    round_seconds=1, final_seconds=1, source_slot=10)
        self.begin(6, plan, attempt=1, overworld={"points": [[1, 1, 0, 0, 0]], "reference": {"source": "native_fixture"}})
        self.call("begin-attempt")
        if loaded:
            self.call("render-retry-begin")
            self.clock.lua.run("mock_paused=false; for index=1,120 do tick() end; mock_paused=true; return 'ready'")
            self.call("render-prepared", 1)
        else:
            self.clock.command("advance", 10)
            self.call("render-prepared")
        categories = move_categories or ["Beauty", "Tough", "Smart", "Cute"]
        self.clock.lua.run(f"paint_acting_moves({literal(categories)}); first_judge_capture=true; "
            f"acting_star_counts={literal(star_counts)}; acting_read_index=0; return 'ready'")
        if entry_change:
            entry_change(self)
        self.act_records, self.act_snapshots = [], []
        plans = []
        for number in range(1, rounds + 1):
            pending = self.call("round-visit", number)
            self.act_records.append(pending["checkpoint"])
            self.act_snapshots.append(pending["checkpoint"]["snapshot"])
            checked = self.acknowledge(pending)
            chosen = checked["checkpoint"]["snapshot"]["plan"]
            plans.append(chosen)
            self.clock.command("combo", "inputs", *chosen["steps"])
            if number == 1 and after_entry:
                after_entry(self)
        if rounds == 4:
            self.clock.lua.run(f"won={literal(win)}; return 'ready'")
            self.acknowledge(self.call("result-visit"))
        return plans

    def ranked_dance(self, *, feedback_policy="first_button_only", bad_feedback=None, early_midpoint_x=None,
                      feedback_delays=None, missing_feedback=(), cue_side_offset_pixels=None, final_response_x=None,
                      midpoint_diagnostics=False, midpoint_positions=None, post_fix_pointer_x=None,
                      selected_inputs=None, changed_cues=(), missed_inputs=None, unreadable_midpoints=(), missed_feedback=None,
                      transition_seconds=1 / stage4_dress.FRAME_RATE):
        recording = feedback_policy == "record_all_buttons"
        target = self.boundary["snapshot"]["target"]
        calibration = None if recording else self.boundary["snapshot"].get("pointer_calibration")
        fixed = debug_contest.fixed_timing(target["category"], target["rank"]) if recording else (
            fixed_timing(target["category"], target["rank"]) if calibration is None and self.observation_policy == "scheduled" else None)
        entry_x = self.boundary["snapshot"]["visual"]["x"] if recording else self.boundary["snapshot"]["screen"]["pointer"]["position_bounds"][0]
        plan = rank_dance.stage5_plan(rank=target["rank"], source_frame=self.boundary["frame"], host_timeout=300,
            transition_seconds=transition_seconds, feedback_policy=feedback_policy,
            offset_frames=debug_contest.DEFAULT_OFFSET_FRAMES if recording else rank_dance.DEFAULT_OFFSET_FRAMES,
            pointer_calibration=calibration, fixed_timing=fixed,
            entry_pointer_x=entry_x,
            first_feedback_policy="warn_and_continue" if fixed is not None and not recording else "require_excellent",
            observation_policy="scheduled" if recording else self.observation_policy,
            midpoint_diagnostics=midpoint_diagnostics)
        profile = plan["rank_timing"]
        tolerant = plan["tolerant_inputs"]
        self.begin(5, plan)
        self.call("init")
        timelines = []
        self.rank_cue_trials = []
        self.rank_midpoints = []
        self.rank_plans = []
        self.rank_captures = []
        for number in range(1, 7):
            folder = f"{self.work}/rank-dance-{number}"
            gap = plan["leader_switch_gap_frames"] if number in (3, 5) else 0
            self.call("select-dance", number, folder, gap)
            if gap:
                self.clock.command("wait-gap")
            if number == 1:
                expected_midpoint = plan["midpoint_timing"]["frames"][0]
            elif tolerant:
                previous = self.rank_plans[-1]
                anchor = previous["fallback_midpoint_frame"] if previous.get("skip_reason") else previous["frame"] \
                    + math.floor((127 - previous["pointer_x"]) / profile["pixels_per_frame"])
                expected_midpoint = anchor + profile["cycle_frames"] + gap
            else:
                first = self.rank_midpoints[0]["frame"]
                expected_midpoint = first + (number - 1) * profile["cycle_frames"] \
                    + ((number - 1) // 2) * profile["extra_switch_frames"]
            self.clock.lua.run("measurement_unreadable=false; rank_pointer_override=nil; rank_visual_origin=frame; rank_feedback_due=frame+99999; rank_feedback_events=nil; capture_frames={}; return 'ready'")
            observed = self.clock.command("observe-demo")
            pointer_x = (midpoint_positions or {}).get(number, 128)
            if midpoint_positions:
                self.clock.lua.run(f"rank_pointer_override={pointer_x}; return 'ready'")
            midpoint = self.call("capture", "midpoint")
            if number == 1 and early_midpoint_x is not None:
                self.clock.lua.run(f"rank_pointer_override={early_midpoint_x}; return 'ready'")
                delta = math.ceil((128 - early_midpoint_x) / profile["pixels_per_frame"])
                adjustment = {"frame": midpoint["frame"], "epoch": midpoint["epoch"], "pointer_x": early_midpoint_x,
                    "next_frame": midpoint["frame"] + delta, "path": midpoint["observation"]["image"]["path"]}
                self.call("retime-midpoint", self.config(adjustment))
                observed = self.clock.command("observe-demo")
                pointer_x = 128 if post_fix_pointer_x is None else post_fix_pointer_x
                self.clock.lua.run(f"rank_pointer_override={literal(post_fix_pointer_x)}; return 'ready'")
                midpoint = self.call("capture", "midpoint")
                expected_midpoint += delta
            if observed["frame"] != expected_midpoint:
                raise AssertionError("Visual pixels changed the counted midpoint schedule")
            self.rank_midpoints.append({"frame": midpoint["frame"], **midpoint["observation"]["midpoint_timing"]})
            captures = {"demo": json.loads(self.clock.lua.run("return dofile('" + (ROOT / "lua/json.lua").as_posix()
                + "').encode(capture_frames)"))}
            index = self.clock.lua.run(f"return mock_files[{literal(folder + '/demo-frames.jsonl')}] or ''")
            timelines.append([json.loads(line) for line in index.splitlines()])
            buttons, positions = (("B", "B", "B"), (160, 224, 240)) if profile["rank"] == "Normal" \
                else (("A", "B", "X", "Y"), (138, 158, 178, 198))
            if final_response_x is not None:
                positions = (*positions[:-1], final_response_x)
            cues = [{"button": button, "x": x} for button, x in zip(buttons, positions)]
            accepted = [{"button": cue["button"], "demo_x": cue["x"] - profile["cue_side_offset_pixels"], "response_x": cue["x"],
                "source_side": "right", "fallback": False, "cross_checked": True, "position_source": "native_right_marker"}
                for cue in cues]
            visible_right = cues
            if number == 3:
                accepted[2].update(source_side="left", fallback=True, cross_checked=False,
                                   position_source="mirrored_readable_left_cue")
                visible_right = cues[:2] + cues[3:]
            endpoint = midpoint["frame"] + math.floor((128 - pointer_x) / profile["pixels_per_frame"]
                + profile["response_frames"] + 0.5)
            resolved = {"dance": number, "rank": plan["rank"], "frame": midpoint["frame"], "epoch": midpoint["epoch"],
                "pointer_x": pointer_x, "speed": plan["pointer_pixels_per_emulator_frame"],
                "hold_frames": plan["hold_frames"], "offset_frames": plan["offset_frames"],
                "player_center_x": 204 - 80 * ((number - 1) // 2),
                "predicted_end_frame": endpoint,
                "response_end_frame": endpoint,
                "cues": {"right": visible_right, "accepted": accepted}, "pixel_points": [[1, 1, 121, 247, 215]],
                "presses": [{"button": cue["button"], "response_x": cue["x"],
                    "frame": midpoint["frame"] + math.floor((cue["x"] - pointer_x) / plan["pointer_pixels_per_emulator_frame"] + 0.5)
                        + plan["offset_frames"]}
                    for cue in cues]}
            if tolerant:
                offset = cue_side_offset_pixels or profile["cue_side_offset_pixels"]
                for index, cue in enumerate(accepted, 1):
                    resolved["presses"][index - 1]["input_index"] = index
                    cue["demo_x"] = cue["response_x"] - offset
                    cue["pixel_points"] = [[1, 1, 121, 247, 215]]
                    cue["evidence"] = {"left": {"x": cue["demo_x"], "button": cue["button"]}}
                    if cue["source_side"] == "right":
                        cue["evidence"]["right"] = {"x": cue["response_x"], "button": cue["button"]}
                resolved["cues"].update(side_offset_pixels=offset, side_offset_source="configured_cue_spacing", conflicts=[])
                selected = (selected_inputs or {}).get(number, list(range(len(cues))))
                resolved["presses"] = [resolved["presses"][index] for index in selected]
                resolved["cues"]["accepted"] = [accepted[index] for index in selected]
                resolved.update(expected_inputs=profile["cues_per_dance"],
                    skipped_input_count=profile["cues_per_dance"] - len(selected), skipped_inputs=[])
                resolved["cues"]["unresolved_cues"] = resolved["skipped_input_count"]
                for index, cue in enumerate(resolved["cues"]["accepted"], 1):
                    if (number, index) in changed_cues:
                        cue["pixel_points"] = [[1, 1, 0, 0, 0]]
                if number in unreadable_midpoints:
                    resolved = rank_dance.skipped_response({"frame": midpoint["frame"], "epoch": midpoint["epoch"],
                        "midpoint_timing": midpoint["observation"]["midpoint_timing"]}, number, plan, "unreadable_midpoint")
                    self.clock.lua.run("measurement_unreadable=true; return 'ready'")
                resolved.update(rank_dance.feedback_schedule(resolved,
                    delay_frames=plan["first_feedback_delay_frames"], rank_timing=profile))
            self.rank_plans.append(resolved)
            if self.observation_policy == "scheduled" and number == 1 and not recording:
                resolved["first_feedback_frame"] = next((press["frame"] + plan["first_feedback_delay_frames"]
                    for index, press in enumerate(resolved["presses"], 1) if press.get("input_index", index) == 1), None)
            self.call("plan", self.config(resolved))

            def checkpoint(name, frame):
                return self.acknowledge(self.call("visit", name, self.config({"frame": frame, "epoch": midpoint["epoch"]})))

            started = checkpoint("rank-dance-start", midpoint["frame"])
            self.rank_cue_trials.append(started["checkpoint"]["snapshot"]["trial"])
            events = []
            for index, press in enumerate(resolved["presses"], 1):
                label = bad_feedback[2] if bad_feedback is not None and bad_feedback[:2] == (number, index) else "Excellent"
                if label is not None and (number, index) not in missing_feedback:
                    delay = (feedback_delays or {}).get((number, index), 1)
                    events.append({"frame": press["frame"] + delay,
                        "end_frame": press["frame"] + delay + max(plan["first_feedback_delay_frames"], 5), "label": label})
            self.clock.lua.run(f"rank_feedback_events={literal(events)}; capture_frames={{}}; feedback_read_frames={{}}; return 'ready'")
            missed = (missed_inputs or {}).get(number)
            if missed is not None:
                due = resolved["presses"][missed]["frame"]
                self.clock.lua.run(f"""
                    saved_before_frame=before_frame
                    before_frame=function()
                        if frame == {due - 1} then frame=frame+1; mock_trace[#mock_trace+1]='' end
                        saved_before_frame()
                    end
                    return 'ready'
                """)
            missed_window = (missed_feedback or {}).get(number)
            if missed_window is not None:
                index, callback = missed_window
                initial = resolved["feedback_frames"][index]
                limit = resolved["feedback_deadline_frames"][index]
                trigger = initial - 1 if callback == "before_frame" else initial
                self.clock.lua.run(f"""
                    saved_feedback_callback={callback}
                    {callback}=function()
                        if frame == {trigger} then
                            for skipped=frame+1,{limit + 1} do mock_trace[#mock_trace+1]='' end
                            frame={limit + 1}
                        end
                        saved_feedback_callback()
                    end
                    return 'ready'
                """)
            mirrored = self.clock.command("mirror")
            if missed is not None:
                self.clock.lua.run("before_frame=saved_before_frame; return 'ready'")
            if missed_window is not None:
                self.clock.lua.run(f"{callback}=saved_feedback_callback; return 'ready'")
            if mirrored.get("error"):
                raise AssertionError(mirrored["error"])
            if number == 1 and not recording:
                checkpoint("rank-dance-offset", mirrored["frame"])
                mirrored = self.clock.command("mirror-rest")
                if mirrored.get("error"):
                    raise AssertionError(mirrored["error"])
            captures["response"] = json.loads(self.clock.lua.run("return dofile('" + (ROOT / "lua/json.lua").as_posix()
                + "').encode(capture_frames)"))
            captures["feedback"] = json.loads(self.clock.lua.run("return dofile('" + (ROOT / "lua/json.lua").as_posix()
                + "').encode(feedback_read_frames)")) or []
            captures["buttons"] = self.clock.trace()[-(mirrored["frame"] - midpoint["frame"]):]
            self.rank_captures.append(captures)
            checkpoint("rank-dance-finish", mirrored["frame"])
        if feedback_policy in ("all_buttons", "record_all_buttons"):
            return timelines
        self.clock.command("lead-dances")
        self.clock.command("acting-transition")
        self.acknowledge(self.call("visit", "stage-5-dance"))
        self.retain(10)
        return timelines

    def retry(self, slot):
        source = self.sources[slot]
        armed = self.call("arm-retry", slot, source["visit"])
        self.clock.lua.run(f"frame={source['frame']}; won=false; return 'loaded'")
        loaded = self.call("state-changed", "load", source["visit"])
        self.boundary = deepcopy(source)
        return armed, loaded

    def counters(self):
        return json.loads(self.clock.lua.run("return dofile('" + (ROOT / "lua/json.lua").as_posix()
            + "').encode({engines=engine_count,callbacks=registrations,stars=stars_read,moves=move_reads,pages=page_reads,touches=touches})"))


class PersistentLuaChecks(unittest.TestCase):
    def harness(self, mode=0, rank="Master", observation_policy="scheduled", *, debug=super_contest.debug):
        harness = ContestClock(mode, rank, observation_policy, debug=debug)
        self.addCleanup(harness.close)
        return harness

    def test_custom_entry_is_inert_and_requires_a_fresh_confirmed_endpoint(self):
        self.assertIs(sections.RUNNERS[3], sections.enter)
        self.assertEqual(super_contest.chain_plan()["sections"][2]["callable"], "contest.sections.enter")
        for confirmed in (True, False):
            with self.subTest(confirmed=confirmed), temporary_directory() as folder:
                harness = self.harness()
                loading = stage1_load.load_plan(boot_wait_frames=1, hold_frames=1, start_wait_frames=1,
                    a_wait_frames=1, b_wait_frames=1)
                harness.begin(1, loading)
                harness.clock.command("combo", "inputs", *loading["inputs"])
                harness.review()
                harness.prepare(opened_once=False, cursor=1)
                source = deepcopy(harness.boundary)
                runtime = SimpleNamespace(active_work=Path(folder), settings=super_contest.chain_plan(), stage=3,
                    run_id="integrated-check", target=source["snapshot"]["target"], section_visit=1,
                    boundary={"checkpoint": source, "save_state": None, "receipt": str(Path(folder) / "source.json")},
                    register=lambda path, **_: path, json=lambda name, value: write_json(Path(folder) / name, value),
                    controller=SimpleNamespace(screenshot=lambda path: Image.new("RGB", (256, 384)).save(path)))

                def begin(plan):
                    config, handoff = harness.begin(3, plan)
                    runtime.section_visit = config["section_visit"]
                    return config, handoff

                def action(action, *args, expected, **_):
                    result = harness.clock.command(action, *args)
                    self.assertEqual(result["stop_reason"], expected)
                    return result

                def acknowledge(pending, *, slot):
                    self.assertEqual(slot, 3)
                    self.assertFalse(pending["checkpoint"]["save_required"])
                    result = harness.acknowledge(pending)
                    runtime.boundary = {"checkpoint": result["checkpoint"], "save_state": None,
                        "receipt": str(Path(folder) / "entry.json")}
                    return result

                runtime.start_section, runtime.action = begin, action
                runtime.command, runtime.acknowledge = harness.call, acknowledge
                harness.clock.lua.run(f"offline_screen_confirmed={literal(confirmed)}; return 'ready'")
                if confirmed:
                    result = sections.custom_enter(runtime)
                    self.assertTrue(result["stage3_accepted"])
                    self.assertIsNone(result["save_state"])
                    self.assertEqual(result["dress_up_capture"]["direct_read_frames"], 0)
                    self.assertEqual(result["input_frames"], stage3_enter.entry_plan("Cool", "Master", host_timeout=300)["total_emulator_frames"])
                    self.assertEqual(harness.boundary["checkpoint"], "stage-3-enter")
                    self.assertTrue(harness.boundary["snapshot"]["screen"]["fresh_visual_binding"])
                else:
                    with self.assertRaisesRegex(RuntimeError, "Continuous enrollment did not reach dress-up"):
                        sections.custom_enter(runtime)
                    self.assertEqual(harness.boundary["checkpoint"], "stage-2-prep")
                    request = json.loads((Path(folder) / "review-request.json").read_text(encoding="utf-8"))
                    self.assertFalse(request["screen"]["confirmed"])
                    self.assertTrue(Path(request["image"]).is_file())
                self.assertEqual(harness.clock.lua.run("return tostring(mock_paused)"), "true")

    def test_main_run_uses_fixed_metrics_reanchors_and_hands_off_without_routine_dance_saves(self):
        harness = self.harness()
        harness.prepare_entry()
        source_frame = harness.boundary["frame"]
        _, handoff, _ = harness.dress(0, finish=True)
        self.assertEqual((handoff["frame"], handoff["handoff"]["emulator_frames"]), (source_frame, 0))
        self.assertEqual(harness.calibration_inputs, [])
        self.assertIsNone(harness.calibration_advance)
        entry = harness.boundary["frame"]
        timelines = harness.ranked_dance(early_midpoint_x=126, bad_feedback=(1, 2, "Good"))
        snapshot = harness.boundary["snapshot"]
        timing, trial = snapshot["rank_timing"], snapshot["trial"]
        self.assertEqual((timing["response_frames"], timing["cycle_frames"], timing["extra_switch_frames"]), (216, 432, 216))
        self.assertEqual(timing["timing_source"], "fixed_normalized_contest_measurements")
        self.assertEqual((trial["completed_dances"], trial["total_input_count"], trial["total_judgment_count"],
            trial["total_excellent_count"]), (6, 24, 1, 1))
        self.assertEqual(harness.rank_midpoints[0]["frame"], entry + 218)
        self.assertEqual(harness.rank_midpoints[0]["source"], "calculated_from_fresh_demo_pointer")
        self.assertEqual([item["frame"] - harness.rank_midpoints[0]["frame"] for item in harness.rank_midpoints],
                          [0, 430, 1076, 1506, 2152, 2582])
        self.assertTrue(all(item["source"] == "calculated_from_fresh_midpoint_and_fixed_metrics" for item in harness.rank_midpoints[1:]))
        self.assertTrue(all(not records for records in timelines))
        fallback = harness.rank_cue_trials[2]["cue_sources"][2]
        self.assertEqual((fallback["button"], fallback["source_side"], fallback["fallback"]), ("X", "left", True))
        for number, (captures, plan) in enumerate(zip(harness.rank_captures, harness.rank_plans), 1):
            self.assertEqual(set(captures["demo"]), {plan["frame"], plan["frame"] - 4} if number == 1 else {plan["frame"]})
            allowed = {plan["response_end_frame"]}
            if number == 1:
                allowed.update((plan["frame"], plan["presses"][0]["frame"] - 1, plan["first_feedback_frame"]))
                self.assertEqual(set(captures["feedback"]), {plan["frame"], plan["presses"][0]["frame"] - 1,
                    plan["first_feedback_frame"]})
            else:
                self.assertEqual(captures["feedback"], [])
            self.assertLessEqual(set(captures["response"]), allowed)
        plans = harness.act(win=True)
        self.assertEqual([plan["move"] for plan in plans], [1, 2, 1, 2])
        self.assertEqual(harness.boundary["decision"], "hit")
        self.assertEqual(harness.boundary["snapshot"]["party"]["owned_ribbon_bits"], [0, 1, 2, 3])
        self.assertEqual([record["checkpoint"] for record in harness.saved_checkpoints],
                         ["stage-5-dance"])
        self.assertTrue(harness.sources[10]["savestate"]["path"].endswith(".ds0"))
        screens = {"stage-1-load", "stage-3-enter", "stage-4-dress-page", "stage-4-dress",
            "rank-dance-start", "rank-dance-offset", "stage-6-round"}
        self.assertTrue(screens.issubset({record["checkpoint"] for record in harness.log_checkpoints}))
        self.assertTrue(all("savestate" not in record for record in harness.log_checkpoints))
        before = harness.boundary["frame"]
        _, next_handoff = harness.begin(2, {})
        self.assertEqual(next_handoff["frame"], before)
        initial = harness.call("inspect")["observation"]
        prep = stage2_prep.prep_plan(initial, bag_state=Stage2BagState(True), cursor=7)
        harness.call("init-prep", harness.config(prep))
        if prep["inputs"]:
            harness.clock.command("combo", "inputs", *prep["inputs"])
        harness.clock.lua.run(f"held_item={prep['expected_item_id']}; return 'ready'")
        harness.acknowledge(harness.call("visit"))
        harness.retain(2)
        self.assertEqual(harness.boundary["snapshot"]["target"]["ribbon_bit"], 4)
        self.assertEqual(harness.boundary["epoch"], 0)
        counts = harness.counters()
        self.assertEqual((counts["engines"], counts["stars"]), (1, 4))
        self.assertEqual(counts["callbacks"], {"registerbefore": 1, "registerafter": 1, "registermenustart": 1})
        rejected = harness.clock.checkpoint("state-changed", "load", harness.boundary["visit"])
        self.assertIn("acknowledged ribbon miss", rejected["error"])
        self.assertEqual(rejected["epoch"], 0)

    def test_acting_categories_are_cached_and_overrides_use_the_actual_move_cursor(self):
        harness = self.harness()
        harness.prepare_entry()
        harness.dress(0, finish=True)
        harness.ranked_dance()
        categories = ["Beauty", "Tough", "Smart", "Cool"]
        plans = harness.act(move_categories=categories,
            star_counts=[[0, 0, 0], [0, 4, 4], [0, 1, 2], [0, 0, 0]],
            after_entry=lambda value: value.clock.lua.run("paint_acting_moves({'Cute','Cute','Cute','Cute'}); return 'ready'"))
        self.assertEqual([plan["move"] for plan in plans], [1, 4, 1, 2])
        self.assertEqual([plan["previous_move"] for plan in plans], [1, 1, 4, 1])
        self.assertEqual([plan["matching_category_override"] for plan in plans], [False, True, False, False])
        self.assertEqual(plans[1]["menu_buttons"], ["right", "down", "A", "right", "A"])
        self.assertEqual(plans[2]["menu_buttons"], ["left", "up", "A", "left", "A"])
        first = harness.act_snapshots[0]
        for snapshot in harness.act_snapshots:
            moves = snapshot["move_categories"]
            self.assertEqual(moves["categories"], categories)
            self.assertEqual((moves["frame"], moves["epoch"], moves["path"]),
                (first["image"]["frame"], first["image"]["epoch"], first["image"]["path"]))
            self.assertEqual((moves["matched_pixels"], moves["total_pixels"], moves["direct_read_frames"]), (24, 24, 0))
        self.assertEqual((harness.counters()["stars"], harness.counters()["moves"]), (4, 1))

    def test_round_four_skips_last_used_move_with_entry_categories_cached(self):
        harness = self.harness()
        harness.prepare_entry()
        harness.dress(0, finish=True)
        harness.ranked_dance()
        categories = ["Cool", "Cool", "Cool", "Beauty"]
        plans = harness.act(move_categories=categories,
            star_counts=[[0, 0, 0], [1, 2, 0], [0, 1, 2], [0, 0, 4]],
            after_entry=lambda value: value.clock.lua.run("paint_acting_moves({'Cute','Cute','Cute','Cute'}); return 'ready'"))
        self.assertEqual([plan["move"] for plan in plans], [1, 2, 1, 2])
        self.assertEqual([plan["previous_move"] for plan in plans], [1, 1, 2, 1])
        self.assertTrue(plans[3]["matching_category_override"])
        self.assertEqual(plans[3]["menu_buttons"], ["right", "A", "right", "right", "A"])
        first = harness.act_snapshots[0]["move_categories"]
        self.assertEqual(first["categories"], categories)
        self.assertTrue(all(snapshot["move_categories"] == first for snapshot in harness.act_snapshots))
        self.assertEqual((harness.counters()["stars"], harness.counters()["moves"]), (4, 1))

    def test_acting_retry_reads_new_categories_at_its_fresh_entry_epoch(self):
        harness = self.harness()
        harness.prepare_entry()
        harness.dress(0, finish=True)
        harness.ranked_dance()
        harness.act(move_categories=["Beauty", "Tough", "Smart", "Cool"])
        original = harness.act_snapshots[0]["move_categories"]
        harness.retry(10)
        plans = harness.act(loaded=True, rounds=2, move_categories=["Beauty", "Tough", "Cool", "Cool"])
        fresh = harness.act_snapshots[0]["move_categories"]
        self.assertEqual([plan["move"] for plan in plans], [1, 3])
        self.assertEqual((fresh["epoch"], original["epoch"]), (1, 0))
        self.assertNotEqual(fresh["path"], original["path"])
        self.assertEqual(fresh["frame"], harness.act_snapshots[0]["image"]["frame"])
        self.assertEqual(harness.act_snapshots[1]["move_categories"], fresh)
        self.assertEqual(harness.counters()["moves"], 2)

    def test_unreadable_entry_move_fill_stops_paused_before_selection(self):
        harness = self.harness()
        harness.prepare_entry()
        harness.dress(0, finish=True)
        harness.ranked_dance()
        captured = {}

        def obstruct(value):
            value.clock.lua.run("pixel(18,220,{132,132,132}); return 'ready'")
            captured["frame"] = int(value.clock.lua.run("return tostring(frame)"))

        with self.assertRaises(AssertionError):
            harness.act(rounds=1, entry_change=obstruct)
        self.assertEqual(int(harness.clock.lua.run("return tostring(frame)")), captured["frame"])
        self.assertEqual(harness.clock.lua.run("return tostring(mock_paused)"), "true")
        self.assertEqual((harness.counters()["stars"], harness.counters()["moves"]), (1, 1))
        snapshot = harness.act_snapshots[0]
        self.assertFalse(snapshot["move_categories"]["readable"])
        self.assertEqual(snapshot["move_categories"]["categories"][0], "unreadable")
        self.assertNotIn("plan", snapshot)
        self.assertEqual(snapshot["trial"]["completed_rounds"], 0)

    def test_changed_calibration_cannot_be_acknowledged(self):
        harness = self.harness()
        harness.prepare_entry()
        with self.assertRaisesRegex(AssertionError, "Checkpoint invariant failed"):
            harness.dress(0, finish=True, calibrate=True, change_calibration=lambda value: value.update(pixels_per_frame=0.5))
        self.assertEqual(harness.clock.lua.run("return tostring(mock_paused)"), "true")
        self.assertNotIn("stage-4-dress", [item["checkpoint"] for item in harness.log_checkpoints])

    def test_scheduled_first_feedback_recovers_or_expires_and_continues_without_later_checks(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                harness = self.harness()
                harness.prepare_entry()
                harness.dress(0, finish=True)
                harness.ranked_dance(feedback_delays={(1, 1): 14}, missing_feedback=((1, 1),) if missing else (),
                    bad_feedback=(1, 2, "Miss"))
                snapshot = harness.boundary["snapshot"]
                first, trial = snapshot["offset_check"], snapshot["trial"]
                self.assertEqual(harness.boundary["checkpoint"], "stage-5-dance")
                self.assertEqual((trial["total_input_count"], trial["total_judgment_count"], trial["total_unreadable_feedback"]),
                    (24, 1, int(missing)))
                self.assertEqual(first["label"], "Unreadable" if missing else "Excellent")
                self.assertEqual(first["feedback_frame"], first["feedback_deadline_frame"] if missing else first["input_frame"] + 14)
                self.assertLess(first["feedback_frame"], harness.rank_plans[0]["presses"][1]["frame"])
                self.assertEqual(first["feedback_sample_count"], first["visible_delay_frames"] - 10)
                self.assertEqual(first["fresh_message"], not missing)
                self.assertTrue(all(not captures["feedback"] for captures in harness.rank_captures[1:]))
                self.assertTrue(trial["released"])

    def test_main_skipped_first_inputs_and_unreadable_dances_reach_the_counted_handoff(self):
        for rank in ("Normal", "Master"):
            count = 3 if rank == "Normal" else 4
            cases = {
                "partial": ({"selected_inputs": {1: list(range(1, count)), 6: []},
                    "changed_cues": ((2, 2),), "missed_inputs": {4: 1}}, 5 * count - 3),
                "changed_first": ({"changed_cues": ((1, 1),)}, 6 * count - 1),
                "missed_first": ({"missed_inputs": {1: 0}}, 6 * count - 1),
                "unreadable": ({"unreadable_midpoints": tuple(range(1, 7))}, 0),
            }
            for name, (options, executed) in cases.items():
                with self.subTest(rank=rank, fault=name):
                    harness = self.harness(rank=rank)
                    harness.prepare_entry()
                    harness.dress(0, finish=True)
                    harness.clock.lua.run("lua_messages={}; return 'ready'")
                    harness.ranked_dance(**options, transition_seconds=15)
                    snapshot = harness.boundary["snapshot"]
                    trial, first = snapshot["trial"], snapshot["offset_check"]
                    self.assertEqual(harness.boundary["checkpoint"], "stage-5-dance")
                    self.assertEqual((trial["completed_dances"], trial["total_inputs"], trial["total_skipped_inputs"]),
                        (6, executed, 6 * count - executed))
                    self.assertEqual((trial["total_judgment_count"], trial["required_first_feedback_judgments"]), (0, 0))
                    self.assertEqual((first["dance"], first["index"], first["label"]), (1, 1, "Skipped"))
                    self.assertTrue(first["skipped"] and trial["first_feedback_verified"])
                    self.assertFalse(first["fresh_message"])
                    finishes = [item for item in harness.log_checkpoints if item["checkpoint"] == "rank-dance-finish"]
                    self.assertEqual([item["frame"] for item in finishes],
                        [item["response_end_frame"] for item in harness.rank_plans])
                    lead, transition = snapshot["lead_dances"], snapshot["transition"]
                    self.assertEqual(lead["start_frame"], finishes[-1]["frame"])
                    self.assertEqual(lead["duration_frames"], snapshot["rank_timing"]["lead_dances_frames"])
                    self.assertEqual((lead["input_policy"], lead["input_count"]), ("none", 0))
                    self.assertEqual(transition["start_frame"], lead["end_frame"])
                    self.assertEqual((transition["mash_frames"], transition["end_frame"] - transition["start_frame"]), (897, 897))
                    self.assertEqual(harness.boundary["frame"], transition["end_frame"])
                    self.assertTrue(harness.sources[10]["savestate"]["path"].endswith(".ds0"))
                    self.assertTrue(all(not capture["feedback"] for capture in harness.rank_captures[1:]))
                    if not executed:
                        self.assertTrue(all(not button for capture in harness.rank_captures for button in capture["buttons"]))
                    messages = json.loads(harness.clock.lua.run("return dofile('" + (ROOT / "lua/json.lua").as_posix()
                        + "').encode(lua_messages)"))
                    self.assertNotIn("Dancing: FAIL", messages)
                    self.assertIn(first["warning"], messages)
                    harness.act(rounds=1)
                    self.assertEqual(harness.boundary["checkpoint"], "stage-6-round")

    def test_main_missed_first_feedback_windows_continue_with_one_accounted_judgment(self):
        for callback in ("before_frame", "after_frame"):
            with self.subTest(callback=callback):
                harness = self.harness()
                harness.prepare_entry()
                harness.dress(0, finish=True)
                harness.ranked_dance(missed_feedback={1: (0, callback)})
                snapshot = harness.boundary["snapshot"]
                trial, first = snapshot["trial"], snapshot["offset_check"]
                self.assertEqual(harness.boundary["checkpoint"], "stage-5-dance")
                self.assertEqual((trial["total_inputs"], trial["total_skipped_inputs"], trial["total_judgment_count"]), (23, 1, 1))
                self.assertEqual((first["label"], first["recovery_reason"], first["feedback_sample_count"]),
                    ("Unreadable", "missed_feedback_window", 0))
                self.assertEqual(first["image"]["capture_callback"],
                    "before_frame_feedback_recovery" if callback == "before_frame" else "after_frame_feedback")
                self.assertTrue(all(not capture["feedback"] for capture in harness.rank_captures[1:]))

    def test_first_feedback_warnings_continue_and_good_streak_spans_distinct_contests(self):
        harness = self.harness()
        harness.clock.lua.run("""
            local original_party = party
            local owned = {[1]=true,[2]=true,[3]=true}
            party = function(at)
                local value = original_party(at)
                if won then owned[target.ribbon_bit + 1] = true end
                local flags, bits, next_target = {}, {}, nil
                local categories, ranks = {'Cool','Beauty','Cute','Smart','Tough'}, {'Normal','Super','Hyper','Master'}
                for bit = 0,19 do
                    flags[bit + 1] = owned[bit + 1] == true
                    if flags[bit + 1] then bits[#bits + 1] = bit
                    elseif not next_target then
                        local category, rank = math.floor(bit / 4), bit % 4
                        next_target = {category=categories[category + 1],rank=ranks[rank + 1],ribbon_bit=bit,
                            required_scarf_id=260+category,category_down_presses=category,rank_down_presses=rank,party_slot=1}
                    end
                end
                value.ribbon_flags, value.owned_ribbon_bits, value.owned_count = flags, bits, #bits
                value.next_target, value.all_ribbons_owned = next_target, next_target == nil
                return value
            end
            return 'ready'
        """)
        harness.prepare_entry()
        seen = []
        cases = (("Good", 1, False), ("Good", 2, True), ("Good", 3, True), ("Excellent", 0, False),
                 ("Good", 1, False), ("Miss", 0, True), ("Good", 1, False), (None, 0, True),
                 ("Good", 1, False), ("Good", 2, True))
        for index, (label, streak, warned) in enumerate(cases):
            with self.subTest(contest=index + 1, label=label):
                if index:
                    bag = harness.sources[2]["snapshot"]["bag_context"]
                    harness.prepare(opened_once=bag["opened_once"], cursor=bag["cursor_position"])
                    target = harness.boundary["snapshot"]["target"]
                    harness.clock.lua.run(f"target={literal(target)}; won=false; return 'ready'")
                    harness.enter()
                seen.append(harness.boundary["snapshot"]["target"]["ribbon_bit"])
                harness.dress(0, finish=True)
                harness.clock.lua.run("lua_messages={}; return 'ready'")
                harness.ranked_dance(bad_feedback=(1, 1, label))
                label = label or "Unreadable"
                snapshot = harness.boundary["snapshot"]
                first, trial = snapshot["offset_check"], snapshot["trial"]
                self.assertEqual((first["label"], first["consecutive_good_contests"]), (label, streak))
                self.assertEqual((trial["completed_dances"], trial["total_judgment_count"]), (6, 1))
                self.assertTrue(trial["first_feedback_verified"] and trial["first_feedback_accepted"])
                self.assertEqual(harness.boundary["checkpoint"], "stage-5-dance")
                messages = json.loads(harness.clock.lua.run("return dofile('" + (ROOT / "lua/json.lua").as_posix()
                    + "').encode(lua_messages)"))
                warnings = [message for message in messages if message.startswith("Dancing: WARN -")]
                self.assertEqual(len(warnings), int(warned))
                self.assertNotIn("Dancing: FAIL", messages)
                if warned:
                    self.assertEqual(first["warning"], warnings[0])
                    self.assertIn(f"{streak} consecutive contests" if label == "Good" else label, warnings[0])
                self.assertTrue(all(captures["feedback"] == [] for captures in harness.rank_captures[1:]))
                harness.act(win=True)
                self.assertEqual(harness.boundary["decision"], "hit")
        self.assertEqual(seen, list(range(3, 13)))

    def test_retry_routes_preserve_context_and_reset_acting_cursors(self):
        for mode in (0, 1):
            with self.subTest(mode=mode):
                harness = self.harness(mode, debug=mode)
                harness.prepare_entry()
                harness.dress(7, finish=True)
                harness.ranked_dance()
                harness.act()
                self.assertEqual(harness.boundary["decision"], "miss")
                if mode == 0:
                    armed, loaded = harness.retry(10)
                    self.assertEqual((armed["retry_slot"], loaded["epoch"], loaded["frame"]), (10, 1, harness.sources[10]["frame"]))
                    self.assertEqual(armed["context"]["bag_context"], {"opened_once": True, "cursor_position": 7})
                    self.assertEqual(armed["context"]["source_party"]["owned_ribbon_bits"], [0, 1, 2])
                    first = harness.act(loaded=True, rounds=1)[0]
                    self.assertEqual((first["previous_move"], first["previous_judge"]), (1, 1))
                    self.assertEqual(first["menu_buttons"], ["A", "A"])
                    snapshot = harness.boundary["snapshot"]
                    self.assertEqual(snapshot["trial"]["render_preparation_frames"], 120)
                    self.assertEqual(snapshot["image"]["direct_read_frames"], 0)
                else:
                    self.assertEqual(harness.boundary["branch"], "retry-contest")
                    current_frame = harness.boundary["frame"]
                    previous_visit = harness.boundary["visit"]
                    initial, prep = harness.prepare()
                    self.assertEqual(initial["frame"], current_frame)
                    self.assertEqual(initial["party"]["owned_ribbon_bits"], [0, 1, 2])
                    self.assertEqual(prep["bag_context"], {"opened_once": True, "cursor_position": 7})
                    self.assertEqual(prep["inputs"], [])
                    harness.enter()
                    self.assertGreater(harness.boundary["visit"], previous_visit)
                    self.assertEqual(harness.boundary["epoch"], 0)
                    rejected = harness.clock.checkpoint("arm-retry", 10, harness.sources[10]["visit"])
                    self.assertIn("Acting-only", rejected["error"])
                    self.assertEqual((rejected["frame"], rejected["epoch"]), (harness.boundary["frame"], 0))
                self.assertEqual(harness.counters()["engines"], 1)
                self.assertEqual(harness.counters()["callbacks"], {"registerbefore": 1, "registerafter": 1, "registermenustart": 1})

    def test_initial_page_navigation_precedes_analysis_and_counts_every_frame(self):
        for taps in (0, 7):
            with self.subTest(taps=taps):
                harness = self.harness()
                harness.prepare_entry()
                source = harness.boundary["frame"]
                _, handoff, progress = harness.dress(taps)
                self.assertEqual(handoff["handoff"]["emulator_frames"], 0)
                self.assertEqual(progress["page"]["frame"], source + taps * 33)
                self.assertEqual(progress["remaining_frames"], 2991 - taps * 33)
                navigation = progress["initial_navigation"]
                self.assertEqual((navigation["chosen_taps"], navigation["executed_taps"], navigation["executed_frames"]),
                                 (taps, taps, taps * 33))
                self.assertTrue(navigation["complete"])
                self.assertEqual(harness.counters()["pages"], 1)
                self.assertEqual(len([item for item in harness.counters()["touches"] if (item["x"], item["y"]) == (66, 160)]), taps * 2)

    def test_dressing_deadline_releases_a_truncated_drag_before_finish(self):
        harness = self.harness()
        harness.prepare_entry()
        source = harness.boundary["frame"]
        plan = stage4_dress.dress_plan(host_timeout=300, from_stage3=True,
            dress_seconds=40 / stage4_dress.FRAME_RATE, page_wait_frames=0)
        harness.begin(4, plan, initial_page_taps=0)
        harness.call("init")
        harness.clock.lua.run("pixel(22,235,{255,0,0}); return 'ready'")
        for expected in ("dress_page_complete", "dress_countdown_complete"):
            harness.acknowledge(harness.call("dress-page-visit"))
            result = harness.clock.command("dress-page-inputs")
            self.assertEqual(result["stop_reason"], expected)
        self.assertEqual(result["frame"], source + 40)
        self.assertEqual(result["observation"]["dressing"]["completed_drag_inputs"], 1)
        self.assertFalse(result["stylus_touch"])
        held = [item["frame"] for item in harness.counters()["touches"]]
        self.assertLessEqual(max(held), source + 35)
        finished = harness.clock.command("finish-dressing")
        self.assertEqual((finished["command_start_frame"], finished["command_end_frame"]), (source + 40, source + 43))

    def test_debug_checks_every_fresh_excellent_and_stops_after_six_responses(self):
        harness = self.harness(rank="Normal", observation_policy="diagnostic", debug=1)
        harness.prepare_entry()
        harness.dress(0, finish=True)
        timelines = harness.ranked_dance(feedback_policy="all_buttons")
        snapshot = harness.boundary["snapshot"]
        trial = snapshot["trial"]
        self.assertEqual(harness.boundary["checkpoint"], "rank-dance-finish")
        self.assertEqual((trial["dance"], trial["total_input_count"], trial["total_judgment_count"],
            trial["total_excellent_count"]), (6, 18, 18, 18))
        self.assertEqual(snapshot["rank_timing"]["cue_side_offset_pixels"], 128)
        self.assertEqual(trial["feedback_policy"], "all_buttons")
        self.assertTrue(trial["feedback_policy_met"])
        self.assertEqual((snapshot["offset_check"]["dance"], snapshot["offset_check"]["index"]), (1, 1))
        for records in timelines:
            self.assertEqual([item["frame"] - records[0]["frame"] for item in records[:3]], [0, 1, 2])
            self.assertEqual([item["readable"] for item in records[:3]], [True, False, False])
        endpoints = [record for record in harness.saved_checkpoints if record["checkpoint"] == "rank-dance-finish"]
        self.assertEqual(len(endpoints), 6)
        self.assertTrue(all(judgment["fresh_message"] and judgment["label"] == "Excellent"
            for record in endpoints for judgment in record["snapshot"]["trial"]["judgments"]))
        self.assertNotIn("stage-5-dance", [record["checkpoint"] for record in harness.saved_checkpoints])
        self.assertEqual(harness.counters()["stars"], 0)
        self.assertEqual(harness.clock.lua.run("return tostring(mock_paused)"), "true")

    def test_debug_later_bad_or_unreadable_feedback_stops_paused(self):
        for index, label in ((2, "Good"), (3, None)):
            with self.subTest(index=index, label=label):
                harness = self.harness(rank="Normal", observation_policy="diagnostic", debug=1)
                harness.prepare_entry()
                harness.dress(0, finish=True)
                message = "calibrate button press timing" if label else "Fresh .*feedback is unreadable"
                with self.assertRaisesRegex(AssertionError, message):
                    harness.ranked_dance(feedback_policy="all_buttons", bad_feedback=(1, index, label))
                stopped = harness.clock.checkpoint("capture", "failure")
                trial = stopped["observation"]["trial"]
                self.assertEqual((trial["total_input_count"], trial["total_judgment_count"], trial["total_excellent_count"]),
                                 (index, index if label else index - 1, index - 1))
                resolved = harness.rank_plans[-1]
                expected_frame = resolved["presses"][index - 1]["frame"] + 1 if label else \
                    resolved["presses"][index]["frame"] - 1 if index < 3 else resolved["response_end_frame"]
                self.assertEqual(stopped["frame"], expected_frame)
                self.assertTrue(stopped["observation"]["released"])
                self.assertEqual(harness.clock.lua.run("return tostring(mock_paused)"), "true")
                if label:
                    failure = json.loads(harness.clock.file_text("non-excellent.json"))
                    self.assertEqual((failure["feedback"]["dance"], failure["feedback"]["index"], failure["source"]),
                        (1, index, "fresh_mirror_button_feedback_pixels"))


class ActingPixelChecks(unittest.TestCase):
    def setUp(self):
        self.lua = Lua51()
        self.addCleanup(self.lua.close)
        self.lua.run(f"pixels=dofile({literal((ROOT / 'contest/acting_pixels.lua').as_posix())}); "
            f"json=dofile({literal((ROOT / 'lua/json.lua').as_posix())}); return 'ready'")

    def image(self, categories):
        image = Image.new("RGB", (256, 384), (81, 81, 81))
        for box, category in zip(MOVE_BOXES, categories):
            image.paste(MOVE_FILLS[category], box)
        return image

    def read(self, image):
        data = image.convert("RGB").tobytes()
        parts = []
        for color, run in groupby(zip(data[0::3], data[1::3], data[2::3])):
            count = sum(1 for _ in run)
            parts.append(f"string.rep(string.char(0,{color[0]},{color[1]},{color[2]}),{count})")
        height = image.height
        raw = f"string.char(255,254,1,0,{height // 256},{height % 256},1,0,0,0,0)..table.concat({{" + ",".join(parts) + "})"
        return json.loads(self.lua.run(f"return json.encode(pixels.read_move_categories({raw}))"))

    def test_all_five_native_move_fills_are_readable_in_every_slot(self):
        categories = list(stage3_enter.CATEGORIES)
        for first in range(5):
            expected = [categories[(first + slot) % 5] for slot in range(4)]
            with self.subTest(categories=expected):
                result = self.read(self.image(expected))
                self.assertTrue(result["readable"])
                self.assertEqual(result["categories"], expected)
                self.assertEqual((result["matched_pixels"], result["total_pixels"], result["direct_read_frames"]), (24, 24, 0))

    def test_unknown_or_conflicting_fills_and_top_only_images_are_rejected(self):
        categories = ["Beauty", "Tough", "Smart", "Cute"]
        for color in ((132, 132, 132), MOVE_FILLS["Cool"]):
            with self.subTest(color=color):
                image = self.image(categories)
                image.putpixel((18, 220), color)
                result = self.read(image)
                self.assertFalse(result["readable"])
                self.assertEqual(result["categories"], ["unreadable", *categories[1:]])
                self.assertEqual(result["slots"][0]["matched_pixels"], 5)
        image = self.image(categories)
        image.paste((89, 142, 231), MOVE_BOXES[0])
        self.assertFalse(self.read(image)["readable"])
        with self.assertRaisesRegex(AssertionError, "stacked"):
            self.read(image.crop((0, 0, 256, 192)))


class ActingMoveChecks(unittest.TestCase):
    def setUp(self):
        self.clock = BridgeClock(ROOT / "lua" / "input_bridge.lua", game_code="CPUJ")
        self.addCleanup(self.clock.close)
        self.clock.lua.run(f"acting_plan=dofile({literal((ROOT / 'contest/acting_plan.lua').as_posix())}); "
            f"acting_pixels=dofile({literal((ROOT / 'contest/acting_pixels.lua').as_posix())}); "
            f"json=dofile({literal((ROOT / 'lua/json.lua').as_posix())}); return 'ready'")
        self.config = {**stage6_act.acting_plan(host_timeout=300, hold_frames=1, release_frames=1,
            round_seconds=1 / stage4_dress.FRAME_RATE, final_seconds=1 / stage4_dress.FRAME_RATE),
            "target": TARGET}

    def plan(self, number, previous_move, previous_judge, counts, move_categories=None, *, category="Cool"):
        config = {**self.config, "target": {**TARGET, "category": category}}
        return json.loads(self.clock.lua.run(f"local counts={literal(counts)}; "
            "local stars={readable=true,counts=counts,selected_judge=acting_pixels.choose_judge(counts)}; "
            f"return json.encode(acting_plan.round({literal(config)}, {number}, {previous_move}, "
            f"{previous_judge}, frame, stars, {literal(move_categories)}))"))

    def perform(self, plan, menu):
        self.assertEqual(plan["menu_buttons"], menu)
        self.clock.reset_measurement()
        executed = self.clock.command("combo", "inputs", *plan["steps"])
        expected = [value for button in menu for value in (button, "")] + [""]
        self.assertEqual(self.clock.trace(), expected)
        self.assertEqual(executed["command_start_frame"], plan["start_frame"])
        self.assertEqual(executed["command_end_frame"], plan["end_frame"])
        self.assertEqual(plan["total_frames"], len(expected))

    def test_four_slot_navigation_executes_minimal_cardinal_paths(self):
        paths = {
            1: {1: [], 2: ["right"], 3: ["down"], 4: ["right", "down"]},
            2: {1: ["left"], 2: [], 3: ["left", "down"], 4: ["down"]},
            3: {1: ["up"], 2: ["right", "up"], 3: [], 4: ["right"]},
            4: {1: ["left", "up"], 2: ["up"], 3: ["left"], 4: []},
        }
        for previous, destinations in paths.items():
            for move, directions in destinations.items():
                if move == previous:
                    continue
                with self.subTest(previous=previous, move=move):
                    categories = ["Beauty"] * 4
                    categories[move - 1] = "Cool"
                    plan = self.plan(2, previous, 2, [0, 4, 0], categories)
                    self.assertEqual((plan["previous_move"], plan["move"]), (previous, move))
                    self.assertTrue(plan["matching_category_override"])
                    self.perform(plan, directions + ["A", "A"])

    def test_override_requires_four_stars_and_uses_the_lowest_matching_slot(self):
        for category in stage3_enter.CATEGORIES:
            with self.subTest(category=category):
                other = "Beauty" if category != "Beauty" else "Cool"
                plan = self.plan(2, 1, 1, [0, 4, 4], [other, other, category, category], category=category)
                self.assertEqual((plan["move"], plan["judge"], plan["fallback_move"]), (3, 2, 2))
                self.assertEqual(plan["move_category"], category)
                self.assertEqual(plan["move_policy"], "four_star_category_match")
                self.assertTrue(plan["matching_category_override"])
        cases = (([0, 4, 1], ["Beauty", "Tough", "Smart", "Cute"]),
                 ([0, 3, 1], ["Beauty", "Tough", "Cool", "Cool"]),
                 ([0, 5, 1], ["Beauty", "Tough", "Cool", "Cool"]),
                 ([0, 4, 1], None))
        for counts, categories in cases:
            with self.subTest(counts=counts, categories=categories):
                plan = self.plan(2, 1, 1, counts, categories)
                self.assertEqual((plan["move"], plan["fallback_move"]), (2, 2))
                self.assertFalse(plan["matching_category_override"])
                self.assertEqual(plan["move_policy"], "round_1_2_1_2")

    def test_last_used_move_is_excluded_from_matching_and_fallback_choices(self):
        cases = (
            (4, 1, [0, 0, 4], ["Cool", "Cool", "Cool", "Beauty"], 2, True,
             ["right", "A", "right", "right", "A"]),
            (3, 4, [0, 4, 0], ["Beauty", "Tough", "Cool", "Cool"], 3, True,
             ["left", "A", "right", "A"]),
            (4, 3, [0, 4, 0], ["Beauty", "Tough", "Cool", "Cute"], 2, False,
             ["right", "up", "A", "right", "A"]),
            (3, 1, [0, 0, 4], ["Cool", "Beauty", "Smart", "Cute"], 2, False,
             ["right", "A", "right", "right", "A"]),
            (2, 2, [0, 3, 1], None, 1, False, ["left", "A", "A"]),
        )
        for number, previous, counts, categories, move, override, menu in cases:
            with self.subTest(round=number, previous_move=previous, categories=categories):
                plan = self.plan(number, previous, 1, counts, categories)
                self.assertEqual(plan["move"], move)
                self.assertNotEqual(plan["move"], previous)
                self.assertEqual(plan["matching_category_override"], override)
                self.assertEqual(plan["fallback_move"], 1 if number % 2 else 2)
                if not override and move != plan["fallback_move"]:
                    self.assertEqual(plan["move_policy"], "alternate_fallback")
                self.perform(plan, menu)

    def test_overrides_keep_the_original_fallback_rounds_and_actual_cursor(self):
        cases = (
            (["Beauty", "Tough", "Smart", "Cool"], [1, 4, 1, 2],
             [[0, 0, 0], [0, 4, 4], [0, 1, 2], [0, 0, 0]],
             [["A", "A"], ["right", "down", "A", "right", "A"],
              ["left", "up", "A", "left", "A"], ["right", "A", "A"]]),
            (["Beauty", "Tough", "Cool", "Cool"], [1, 3, 1, 3],
             [[0, 0, 0], [0, 4, 4], [0, 1, 2], [4, 0, 1]],
             [["A", "A"], ["down", "A", "right", "A"],
              ["up", "A", "left", "A"], ["down", "A", "A"]]),
        )
        for categories, moves, stars, menus in cases:
            previous_move, previous_judge = 1, 1
            for number, (move, counts, menu) in enumerate(zip(moves, stars, menus), 1):
                with self.subTest(categories=categories, round=number):
                    plan = self.plan(number, previous_move, previous_judge, counts, categories)
                    self.assertEqual(plan["move"], move)
                    self.assertEqual(plan["fallback_move"], 1 if number % 2 else 2)
                    self.assertEqual((plan["previous_move"], plan["previous_judge"]), (previous_move, previous_judge))
                    self.perform(plan, menu)
                    previous_move, previous_judge = plan["move"], plan["judge"]


class RankTimingChecks(unittest.TestCase):
    def test_main_fixed_profiles_cover_all_twenty_targets_and_keep_debug_recording_opt_in(self):
        planned = super_contest.chain_plan()
        self.assertEqual(planned["dance_pointer_calibration_frames"], 0)
        self.assertEqual(list(planned["dance_timings"]), list(stage3_enter.CATEGORIES))
        expected = {
            "Normal/Super": (8 / 15, 480, 239, 225, 1199),
            "Cool": (5 / 9, 432, 216, 216, 1080),
            "Beauty": (1 / 3, 720, 357, 360, 1797),
            "Cute": (2 / 3, 384, 190, 180, 958),
            "Smart": (7 / 12, 440, 216, 205, 1096),
            "Tough": (5 / 8, 412, 204, 192, 1028),
        }
        self.assertEqual(planned["dance_offset_frames"], -7)
        expected_spacing = {"Cool": (128, 128, 120, 120), "Beauty": (128, 128, 120, 120),
            "Cute": (128, 128, 128, 128), "Smart": (128, 128, 128, 128), "Tough": (128, 128, 128, 128)}
        for category in stage3_enter.CATEGORIES:
            for rank in RANKS:
                with self.subTest(category=category, rank=rank):
                    profile = planned["dance_timings"][category][rank]
                    family = "Normal/Super" if rank in ("Normal", "Super") else category
                    self.assertEqual(tuple(profile[key] for key in ("pixels_per_frame", "cycle_frames",
                        "extra_switch_frames", "response_frames", "lead_dances_frames")), expected[family])
                    self.assertEqual((profile["category"], profile["rank"], profile["cues_per_dance"]),
                        (category, rank, 3 if rank == "Normal" else 4))
                    response = rank_dance.stage5_plan(rank=rank, source_frame=1000, host_timeout=300,
                        fixed_timing=profile, entry_pointer_x=64, observation_policy="scheduled",
                        first_feedback_policy=planned["dance_first_feedback_policy"])
                    self.assertTrue(response["fixed_metrics"])
                    self.assertTrue(response["tolerant_inputs"])
                    self.assertFalse(response["debug_inputs"])
                    self.assertEqual(response["feedback_policy"], "first_button_only")
                    self.assertEqual(response["required_excellent_judgments"], 0)
                    debug_profile = debug_contest.fixed_timing(category, rank)
                    self.assertEqual(debug_profile, profile)
                    self.assertEqual(profile["cue_side_offset_pixels"], expected_spacing[category][RANKS.index(rank)])

    def test_stage4_host_calibrates_before_the_shared_final_handoff(self):
        with temporary_directory() as folder:
            work, clock, captures = Path(folder), {"frame": 900}, []
            plan = stage4_dress.dress_plan(host_timeout=300, from_stage3=True,
                transition_seconds=1 / stage4_dress.FRAME_RATE)
            plan["pointer_calibration_frames"] = 60
            controller = SimpleNamespace(live_deadline=None, remaining=lambda: None, screenshot=Mock())

            def action(name, *args, **_):
                start = clock["frame"]
                frames, reason = {"dress-transition": (1, "A_mash_complete"),
                    "observe-frames": (args[0] if args else 0, "observation_interval_complete"),
                    "calibrate-pointer": (60, "pointer_calibration_complete")}[name]
                clock["frame"] += frames
                return {"stop_reason": reason, "command_start_frame": start, "command_end_frame": clock["frame"]}

            def command(name, *args):
                if name == "capture":
                    captures.append(clock["frame"])
                    (work / "stage4.gd").write_bytes(b"first pixels" if len(captures) == 1 else b"final pixels")
                return {"frame": clock["frame"], "epoch": 2, "observation": {"tip": {"green": True},
                    "transition": {"start_frame": 900, "total_frames": clock["frame"] - 900}}}

            def reading(x, nominal):
                return {"source": "fixture_pointer", "pointer": {"status": "exact", "position_bounds": [x, x]},
                    "progress": {"phase": "demo", "frame_range": [nominal, nominal + 2], "nominal_frame": nominal}}

            emulator = SimpleNamespace(controller=controller, command=Mock(side_effect=action))
            client = SimpleNamespace(command=Mock(side_effect=command))
            with patch("contest.stage4_dress.stacked_gd", return_value=object()), \
                    patch("contest.stage4_dress.pointer_timing", side_effect=[reading(8, 0), reading(28, 36), reading(28, 60)]):
                landing, pending = stage4_dress.locate_dance_entry(emulator, client, plan, work,
                    record=Mock(), rank_timing=timing_for("Hyper"))
            self.assertEqual(captures, [961, 1021])
            self.assertEqual([call.args[0] for call in client.command.call_args_list],
                             ["check-dance", "capture", "calibration-start", "capture", "visit"])
            self.assertEqual((landing["frame"], pending["frame"], landing["no_input_wait_frames"]), (1021, 1021, 120))
            self.assertEqual(landing["pointer_calibration"]["pixels_per_frame"], 1 / 3)
            self.assertEqual(landing["rank_timing"]["response_frames"], 360)
            self.assertEqual(landing["timing_anchor"]["response_start_frame"], 1321)
            self.assertEqual((work / "pointer-calibration-start.gd").read_bytes(), b"first pixels")
            self.assertEqual((work / "stage4.gd").read_bytes(), b"final pixels")

    def test_calibration_replaces_travel_time_and_keeps_fixed_gap_estimates(self):
        first = {"frame": 1000, "epoch": 2, "pointer_x": 8}
        last = {"frame": 1060, "epoch": 2, "pointer_x": 28}
        measured = pointer_calibration(first, last)
        for rank, response, cycle, switch in (("Normal", 374, 746, 241), ("Super", 375, 748, 241),
                ("Hyper", 360, 720, 213), ("Master", 360, 720, 213)):
            with self.subTest(rank=rank):
                profile = calibrated_timing(rank, measured)
                self.assertEqual((profile["pixels_per_frame"], profile["response_frames"], profile["cycle_frames"],
                    profile["extra_switch_frames"]), (1 / 3, response, cycle, switch))
                self.assertFalse(profile["fixed_timing_estimates"]["measured_stop_gaps"])
                self.assertEqual(profile["lead_dances_frames"], 2 * cycle + switch)
                self.assertEqual(profile["stored_timing"]["cycle_frames"], timing_for(rank)["cycle_frames"])
        plan = rank_dance.stage5_plan(rank="Hyper", source_frame=1060, host_timeout=300,
            pointer_calibration=measured, observation_policy="scheduled")
        self.assertEqual(plan["midpoint_timing"]["frames"], [1360, 2080, 3013, 3733, 4666, 5386])
        with self.assertRaisesRegex(ValueError, "accepted pointer calibration endpoint"):
            rank_dance.stage5_plan(rank="Hyper", source_frame=1000, host_timeout=300, pointer_calibration=measured)
        for changed in ({"frame": 1061}, {"epoch": 3}, {"pointer_x": 128}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                pointer_calibration(first, {**last, **changed})

    def test_debug_measurement_targets_cover_all_categories_and_ranks_without_preparation(self):
        default = debug_contest.debug_plan(measure_only=True)
        input_default = debug_contest.debug_plan()
        self.assertEqual(input_default["required_response_inputs"], 450)
        self.assertEqual((default["target"]["category"], default["target"]["rank"]), ("Cool", "Normal"))
        self.assertEqual([(target["category"], target["rank"]) for target in default["targets"]],
            [(category, rank) for category in stage3_enter.CATEGORIES for rank in RANKS])
        self.assertEqual([item["stage"] for item in default["sections"]], [1, 3, 4, 5])
        self.assertEqual((default["entry_poll"]["wait_frames"], default["capture_wait_frames"]), (60, 120))
        self.assertFalse(default["dressing"]["enabled"])
        self.assertFalse(default["dance_button_recognition"] or default["dance_inputs"] or default["dance_memory_snapshots"])
        self.assertEqual(timing_for("Great"), timing_for("Super"))
        self.assertEqual(timing_for("Ultra"), timing_for("Hyper"))
        for category_index, category in enumerate(stage3_enter.CATEGORIES):
            for rank_index, rank in enumerate(RANKS):
                with self.subTest(category=category, rank=rank):
                    planned = debug_contest.debug_plan(category=category, rank=rank, measure_only=True)
                    target = planned["target"]
                    entry = stage3_enter.entry_plan(category, rank, host_timeout=300)
                    self.assertEqual((target["category_down_presses"], target["rank_down_presses"]),
                                     (entry["category_down_presses"], entry["rank_down_presses"]))
                    self.assertEqual(planned["targets"], [default["targets"][category_index * 4 + rank_index]])
                    self.assertNotIn("ribbon_bit", target)
                    self.assertNotIn("required_scarf_id", target)
                    self.assertNotIn("rank_timing", planned)
                    inputs = debug_contest.debug_plan(category=category, rank=rank)
                    self.assertEqual(inputs["input_timing"], {"hold_frames": 2, "release_frames": 10, "offset_frames": -7})
                    self.assertEqual(inputs["feedback"]["delay_frames"], 11)
                    response = rank_dance.stage5_plan(rank=rank, source_frame=1000, host_timeout=300,
                        fixed_timing=inputs["fixed_timings"][0], entry_pointer_x=64,
                        offset_frames=inputs["input_timing"]["offset_frames"],
                        feedback_policy="record_all_buttons", observation_policy="scheduled")
                    self.assertEqual((response["hold_frames"], response["feedback_release_frames"],
                        response["offset_frames"], response["first_feedback_delay_frames"]), (2, 10, -7, 11))

    def test_debug_capture_anchors_favor_early_arrival_for_every_speed_family(self):
        cases = (("Cool", "Normal", 1118), ("Cool", "Master", 1113), ("Beauty", "Hyper", 1189),
            ("Cute", "Master", 1094), ("Smart", "Hyper", 1108), ("Tough", "Master", 1100))
        for category, rank, first in cases:
            with self.subTest(category=category, rank=rank):
                profile = debug_contest.fixed_timing(category, rank)
                plan = rank_dance.stage5_plan(rank=rank, source_frame=1000, host_timeout=300,
                    fixed_timing=profile, entry_pointer_x=64, feedback_policy="record_all_buttons",
                    observation_policy="scheduled", offset_frames=debug_contest.DEFAULT_OFFSET_FRAMES)
                timing = plan["midpoint_timing"]
                self.assertEqual(timing["frames"][0], first)
                self.assertEqual(timing["accepted_pointer_pixels"], [128, 129])
                self.assertLessEqual(64 + (first - 1000) * profile["pixels_per_frame"], 127)
                self.assertEqual(len(timing["frames"]), 6)
                near_line = rank_dance.stage5_plan(rank=rank, source_frame=1000, host_timeout=300,
                    fixed_timing=profile, entry_pointer_x=127, feedback_policy="record_all_buttons",
                    observation_policy="scheduled")
                self.assertGreater(near_line["midpoint_timing"]["frames"][0], 1000)

    def test_beauty_midpoint_diagnostic_plan_repeats_four_ranks_ten_times(self):
        planned = debug_contest.debug_plan(category="Beauty", loops=10, midpoint_diagnostics=True, no_timeout=True)
        self.assertEqual((planned["loop_count"], planned["total_contests"], planned["required_midpoint_visits"],
            planned["required_response_inputs"]), (10, 40, 240, 900))
        self.assertEqual([(target["debug_loop"], target["category"], target["rank"]) for target in planned["targets"]],
            [(loop, "Beauty", rank) for loop in range(1, 11) for rank in RANKS])
        self.assertEqual(planned["input_timing"], {"hold_frames": 2, "release_frames": 10, "offset_frames": -7})
        self.assertFalse(planned["host_timeouts_enabled"])
        self.assertEqual(planned["midpoint_policy"]["early_policy"], "one_counted_input_free_adjustment")
        self.assertEqual(planned["midpoint_policy"]["post_fix_policy"], "record_and_continue")
        for rank in RANKS:
            profile = debug_contest.fixed_timing("Beauty", rank)
            base = dict(rank=rank, source_frame=1000, host_timeout=300, fixed_timing=profile,
                entry_pointer_x=64, feedback_policy="record_all_buttons", observation_policy="scheduled", offset_frames=-7)
            normal, diagnostic = rank_dance.stage5_plan(**base), rank_dance.stage5_plan(**base, midpoint_diagnostics=True)
            self.assertEqual({key: value for key, value in diagnostic.items() if key != "midpoint_diagnostics"},
                {key: value for key, value in normal.items() if key != "midpoint_diagnostics"})
        with self.assertRaisesRegex(ValueError, "input debug pass"):
            debug_contest.debug_plan(measure_only=True, midpoint_diagnostics=True)
        with self.assertRaisesRegex(ValueError, "input debug runner"):
            rank_dance.stage5_plan(rank="Hyper", source_frame=1000, host_timeout=300, midpoint_diagnostics=True)
        for count in (0, -1, True, 1.5):
            with self.subTest(count=count), self.assertRaisesRegex(ValueError, "loop count"):
                debug_contest.debug_plan(loops=count)


class DanceMeasurementChecks(unittest.TestCase):
    def loaded(self, *, inputs=False, settings=None):
        if settings is None:
            settings = debug_contest.debug_plan(category="Cute", rank="Hyper") if inputs else debug_contest.debug_plan(measure_only=True)
        harness = ContestClock(rank=settings["target"]["rank"], observation_policy="debug_inputs" if inputs else "measurement")
        harness.target = settings["target"]
        self.addCleanup(harness.close)
        harness.clock.lua.run("mock_memory_reads=0; return 'ready'")
        loading = stage1_load.load_plan()
        harness.begin(1, loading, measurement_targets=settings["targets"], target=settings["target"])
        harness.clock.command("combo", "inputs", *loading["inputs"])
        harness.review()
        self.assertEqual(harness.clock.trace()[-192:], ["B"] * 12 + [""] * 180)
        self.assertEqual(harness.boundary["frame"], loading["total_emulator_frames"])
        return harness, settings

    def enter_dance(self, harness, settings, target, *, begin_measurement=True):
        entry = stage3_enter.entry_plan(target["category"], target["rank"], host_timeout=300)
        harness.begin(3, entry, target=target)
        entered = harness.clock.command("combo", "inputs", *entry["inputs"])
        self.assertEqual(entered["stop_reason"], "debug_entry_complete")
        self.assertEqual(entered["command_end_frame"] - entered["command_start_frame"], entry["total_emulator_frames"])
        harness.call("capture")
        harness.acknowledge(harness.call("visit"))
        source = harness.boundary["frame"]
        harness.begin(4, {"entry_poll": settings["entry_poll"], "stylus": settings["dressing"]["finish"],
            "transition": settings["transition"]})
        harness.clock.reset_measurement()
        transitioned = harness.clock.command("finish-dress")
        self.assertEqual(transitioned["stop_reason"], "debug_transition_complete")
        self.assertEqual(transitioned["command_end_frame"] - source, 3 + 2393)
        self.assertEqual(harness.clock.trace()[:3], [""] * 3)
        self.assertEqual(harness.clock.trace().count("A"), 800)
        self.assertEqual(harness.counters()["touches"][-2:],
            [{"frame": source + 1, "x": 230, "y": 173}, {"frame": source + 2, "x": 230, "y": 173}])
        harness.clock.lua.run("measurement_unreadable=true; return 'ready'")
        harness.clock.reset_measurement()
        harness.clock.command("wait-entry")
        self.assertEqual(harness.clock.trace(), [""] * 60)
        harness.call("capture")
        harness.clock.reset_measurement()
        harness.clock.command("wait-entry")
        self.assertEqual(harness.clock.trace(), [""] * 60)
        harness.clock.lua.run("measurement_unreadable=false; rank_pointer_override=30; return 'ready'")
        harness.call("capture")
        harness.acknowledge(harness.call("visit"))
        first = harness.boundary["snapshot"]
        if begin_measurement:
            harness.begin(5, {"capture_wait_frames": settings["capture_wait_frames"]})
        harness.clock.reset_measurement()
        return first

    def sample(self, harness, x):
        bounds = x if isinstance(x, list) else None
        pointer = x if isinstance(x, int) else 30
        harness.clock.lua.run(f"measurement_pointer_bounds={literal(bounds)}; "
            f"measurement_unreadable={literal(x is None)}; rank_pointer_override={pointer}; return 'ready'")
        result = harness.clock.command("observe")
        self.assertEqual(result["stop_reason"], "debug_wait_complete")
        self.assertEqual(result["frame"] - result["command_start_frame"], 120)
        pending = harness.call("measurement-visit")
        self.assertFalse(pending["checkpoint"]["save_required"])
        self.assertNotIn("party", pending["checkpoint"]["snapshot"])
        return pending

    def test_twenty_contests_reload_only_the_new_baseline_and_finish_on_the_final_demo(self):
        harness, settings = self.loaded()
        baseline = deepcopy(harness.boundary)
        for index, target in enumerate(settings["targets"]):
            with self.subTest(target=target):
                if index:
                    harness.call("arm-measurement-reload", baseline["visit"])
                    harness.clock.lua.run(f"frame={baseline['frame']}; return 'loaded'")
                    loaded = harness.call("state-changed", "load", baseline["visit"])
                    self.assertEqual((loaded["frame"], loaded["epoch"]), (baseline["frame"], index))
                    harness.boundary = deepcopy(baseline)
                first = self.enter_dance(harness, settings, target)
                samples = [first]
                positions = ([97, 143], 150, 210, 30, 90, 150, 210, 247, None, 30) if target["rank"] == "Hyper" \
                    else (90, None, 210, 30, 90, 150, 210, 247, None, 30)
                for x in positions:
                    pending = self.sample(harness, x)
                    samples.append(pending["checkpoint"]["snapshot"])
                    harness.acknowledge(pending)
                visual = harness.boundary["snapshot"]["visual"]
                self.assertEqual((visual["pokemon"], visual["dance"], visual["phase"]), (2, 1, "demo"))
                self.assertTrue(visual["finish_confirmed"])
                self.assertEqual(harness.boundary["checkpoint"], "dance-measurement-finish")
                self.assertEqual(harness.boundary["frame"] - first["frame"], 1200)
                self.assertEqual(harness.clock.trace(), [""] * 1200)
                metrics = debug_contest.summarize_measurements(samples)
                self.assertEqual(metrics["pointer_speed"]["pixels_per_frame"], 0.5)
                self.assertEqual(metrics["dance_1_to_dance_2"]["estimate_frames"], 0)
                self.assertEqual(metrics["pokemon_1_to_pokemon_2"]["estimate_frames"], 240)
                skipped_offset = 120 if target["rank"] == "Hyper" else 240
                self.assertEqual(metrics["skipped_frames"], [first["frame"] + skipped_offset, first["frame"] + 1080])
                self.assertFalse(any(sample["visual"].get("finish_confirmed") for sample in samples[:-1]))
        self.assertEqual([record["checkpoint"] for record in harness.saved_checkpoints], ["stage-1-load"])
        self.assertEqual(harness.clock.lua.run("return tostring(mock_memory_reads)"), "0")
        counters = harness.counters()
        self.assertEqual((counters["engines"], counters["pages"], counters["stars"]), (1, 0, 0))
        self.assertEqual(counters["callbacks"], {"registerbefore": 1, "registerafter": 1, "registermenustart": 1})
        final = harness.boundary["frame"]
        rejected = harness.clock.checkpoint("arm-measurement-reload", baseline["visit"])
        self.assertIn("another target pending", rejected["error"])
        self.assertEqual(rejected["frame"], final)

    def test_ambiguous_numbering_and_dance_inputs_cannot_advance_the_capture_sequence(self):
        for interrupt in ("ambiguous", "input"):
            with self.subTest(interrupt=interrupt):
                harness, settings = self.loaded()
                first = self.enter_dance(harness, settings, settings["target"])
                if interrupt == "ambiguous":
                    samples = [first]
                    for x in (90, None, None, None, 30):
                        pending = self.sample(harness, x)
                        samples.append(pending["checkpoint"]["snapshot"])
                        harness.acknowledge(pending)
                    visual = samples[-1]["visual"]
                    self.assertEqual(visual["sequence_status"], "ambiguous")
                    self.assertFalse(visual.get("finish_confirmed"))
                    self.assertIn(samples[-1]["frame"], debug_contest.summarize_measurements(samples)["skipped_frames"])
                    before = samples[-1]["frame"]
                else:
                    before = first["frame"]
                result = harness.clock.command("observe" if interrupt == "ambiguous" else "mirror")
                self.assertIn("next input-free observation interval" if interrupt == "ambiguous"
                    else "disabled in measurement mode", result["error"])
                self.assertEqual(result["frame"], before)
                self.assertTrue(all(not buttons for buttons in harness.clock.trace()))

    def test_input_feedback_recovers_or_expires_without_moving_the_remaining_inputs(self):
        for missing in ((), ((1, 2), (6, 4))):
            with self.subTest(missing=missing):
                harness, settings = self.loaded(inputs=True)
                baseline = deepcopy(harness.boundary)
                self.enter_dance(harness, settings, settings["target"], begin_measurement=False)
                harness.ranked_dance(feedback_policy="record_all_buttons", bad_feedback=(2, 2, "Good"),
                    feedback_delays={(1, 1): 14, (6, 4): 9}, missing_feedback=missing,
                    cue_side_offset_pixels=128, final_response_x=248)
                trial = harness.boundary["snapshot"]["trial"]
                self.assertEqual((trial["total_input_count"], trial["total_judgment_count"], trial["total_timing_misses"]), (24, 24, 1))
                self.assertEqual(trial["total_unreadable_feedback"], len(missing))
                self.assertEqual(trial["cue_side_offset_pixels"], 128)
                self.assertTrue(trial["final_feedback_bound"] and trial["at_feedback_endpoint"])
                finishes = [value for value in harness.log_checkpoints if value["checkpoint"] == "rank-dance-finish"]
                for number, (result, planned) in enumerate(zip(finishes, harness.rank_plans), 1):
                    captures = harness.rank_captures[number - 1]
                    for judgment, press in zip(result["snapshot"]["trial"]["judgments"], planned["presses"]):
                        self.assertEqual(judgment["input_frame"], press["frame"])
                        first_held = press["frame"] - planned["frame"] - 1
                        self.assertEqual(captures["buttons"][first_held:first_held + 12], [press["button"]] * 2 + [""] * 10)
                        self.assertIn(press["frame"] + 11, captures["response"])
                        self.assertEqual(judgment["release_frames_before_initial_capture"], 10)
                        if (number, judgment["index"]) in missing:
                            self.assertEqual(judgment["label"], "Unreadable")
                            self.assertEqual(judgment["feedback_frame"], judgment["feedback_deadline_frame"])
                first = finishes[0]["snapshot"]["trial"]["judgments"][0]
                self.assertEqual((first["scheduled_feedback_frame"] - first["input_frame"], first["visible_delay_frames"]), (11, 14))
                self.assertTrue(first["recovered_after_initial_capture"])
                self.assertEqual(first["feedback_sample_count"], 4)
                for result, planned in zip(finishes, harness.rank_plans):
                    final = result["snapshot"]["trial"]["judgments"][-1]
                    self.assertEqual(final["feedback_frame"], planned["presses"][-1]["frame"] + 11)
                    self.assertEqual(final["feedback_frame"], planned["response_end_frame"] + 4)
                    self.assertLess(final["feedback_frame"], planned["next_demo_frame"])
                if not missing:
                    self.assertEqual(trial["judgments"][-1]["visible_delay_frames"], 11)
                    self.assertFalse(trial["judgments"][-1]["recovered_after_initial_capture"])
                harness.call("arm-measurement-reload", baseline["visit"])
                harness.clock.lua.run(f"frame={baseline['frame']}; return 'loaded'")
                loaded = harness.call("state-changed", "load", baseline["visit"])
                self.assertEqual((loaded["frame"], loaded["epoch"]), (baseline["frame"], 1))
                self.assertEqual(harness.clock.lua.run("return tostring(mock_paused)"), "true")

    def test_debug_first_good_stays_record_only_without_main_warning_state(self):
        harness, settings = self.loaded(inputs=True)
        self.enter_dance(harness, settings, settings["target"], begin_measurement=False)
        harness.clock.lua.run("lua_messages={}; return 'ready'")
        harness.ranked_dance(feedback_policy="record_all_buttons", bad_feedback=(1, 1, "Good"))
        snapshot = harness.boundary["snapshot"]
        trial = snapshot["trial"]
        self.assertEqual((trial["total_input_count"], trial["total_judgment_count"], trial["total_excellent_count"]), (24, 24, 23))
        self.assertEqual(snapshot["offset_check"]["label"], "Good")
        self.assertNotIn("consecutive_good_contests", snapshot["offset_check"])
        messages = json.loads(harness.clock.lua.run("return dofile('" + (ROOT / "lua/json.lua").as_posix()
            + "').encode(lua_messages)"))
        self.assertFalse(any("Dancing: WARN" in message for message in messages))
        status = json.loads(harness.clock.lua.run("return latest_status"))
        self.assertNotIn("dance_first_feedback", status)

    def test_debug_checkpoints_skip_residual_early_inputs_and_continue_late_responses(self):
        for diagnostic in (False, True):
            with self.subTest(diagnostic=diagnostic):
                settings = debug_contest.debug_plan(category="Beauty", rank="Hyper", midpoint_diagnostics=diagnostic)
                harness, settings = self.loaded(inputs=True, settings=settings)
                baseline = deepcopy(harness.boundary)
                self.enter_dance(harness, settings, settings["target"], begin_measurement=False)
                arguments = dict(feedback_policy="record_all_buttons", midpoint_diagnostics=diagnostic,
                    early_midpoint_x=127, post_fix_pointer_x=127, midpoint_positions={2: 130, 3: 129})
                harness.ranked_dance(**arguments)
                trial = harness.boundary["snapshot"]["trial"]
                expected = 24 if diagnostic else 20
                self.assertEqual((trial["total_input_count"], trial["total_judgment_count"]), (expected, expected))
                self.assertEqual(trial["total_skipped_inputs"], 24 - expected)
                starts = harness.rank_cue_trials
                self.assertFalse(starts[0]["midpoint_within_line"] or starts[1]["midpoint_within_line"])
                self.assertTrue(starts[2]["midpoint_within_line"])
                self.assertTrue(all(item["midpoint_policy_met"] for item in starts))
                self.assertEqual(starts[0]["midpoint_adjustment_count"], 1)
                self.assertEqual([item["pointer_x"] for item in harness.rank_plans], [127, 130, 129, 128, 128, 128])
                harness.call("arm-measurement-reload", baseline["visit"])
                harness.clock.lua.run(f"frame={baseline['frame']}; return 'loaded'")
                loaded = harness.call("state-changed", "load", baseline["visit"])
                self.assertEqual((loaded["frame"], loaded["epoch"]), (baseline["frame"], 1))

    def test_debug_partial_empty_changed_and_missed_inputs_finish_and_reload(self):
        for rank, unreadable in (("Normal", ()), ("Master", ()), ("Normal", (1, 6)), ("Master", (1, 6))):
            with self.subTest(rank=rank, unreadable=unreadable):
                settings = debug_contest.debug_plan(category="Smart", rank=rank)
                harness, settings = self.loaded(inputs=True, settings=settings)
                baseline = deepcopy(harness.boundary)
                self.enter_dance(harness, settings, settings["target"], begin_measurement=False)
                count = 3 if rank == "Normal" else 4
                harness.ranked_dance(feedback_policy="record_all_buttons", selected_inputs={1: [0], 6: []},
                    changed_cues=((2, 2),), missed_inputs={4: 1}, missing_feedback=((3, 1),), unreadable_midpoints=unreadable)
                trial = harness.boundary["snapshot"]["trial"]
                expected = 4 * count - 1 - bool(unreadable)
                self.assertEqual((trial["total_inputs"], trial["total_judgment_count"]), (expected, expected))
                self.assertEqual(trial["total_skipped_inputs"], 6 * count - expected)
                self.assertEqual(trial["total_unreadable_feedback"], 1)
                self.assertEqual(trial["judgments"], [])
                self.assertTrue(trial["debug_finish_bound"])
                self.assertFalse(trial["final_feedback_bound"])
                finishes = [item["snapshot"]["trial"] for item in harness.log_checkpoints if item["checkpoint"] == "rank-dance-finish"]
                self.assertEqual([item["inputs"] for item in finishes], [0 if unreadable else 1, count - 1, count, count - 1, count, 0])
                self.assertEqual(finishes[1]["skipped_inputs"][0]["reason"], "cue_pixels_changed_before_input")
                self.assertEqual(finishes[3]["skipped_inputs"][0]["reason"], "missed_input_frame")
                for number in unreadable:
                    self.assertEqual(finishes[number - 1]["skipped_inputs"], [{"reason": "unreadable_midpoint", "count": count}])
                    self.assertTrue(all(not buttons for buttons in harness.rank_captures[number - 1]["buttons"]))
                harness.call("arm-measurement-reload", baseline["visit"])
                harness.clock.lua.run(f"frame={baseline['frame']}; return 'loaded'")
                loaded = harness.call("state-changed", "load", baseline["visit"])
                self.assertEqual((loaded["frame"], loaded["epoch"]), (baseline["frame"], 1))

    def test_debug_missed_feedback_windows_record_the_actual_callback_and_continue(self):
        for callback in ("before_frame", "after_frame"):
            with self.subTest(callback=callback):
                settings = debug_contest.debug_plan(category="Smart", rank="Master")
                harness, settings = self.loaded(inputs=True, settings=settings)
                baseline = deepcopy(harness.boundary)
                self.enter_dance(harness, settings, settings["target"], begin_measurement=False)
                harness.ranked_dance(feedback_policy="record_all_buttons", missed_feedback={6: (3, callback)})
                trial = harness.boundary["snapshot"]["trial"]
                self.assertEqual((trial["total_inputs"], trial["total_judgment_count"], trial["total_skipped_inputs"]), (24, 24, 0))
                self.assertEqual((trial["total_excellent_count"], trial["total_unreadable_feedback"]), (23, 1))
                final = trial["judgments"][-1]
                self.assertEqual(final["recovery_reason"], "missed_feedback_window")
                self.assertEqual(final["feedback_frame"], final["feedback_deadline_frame"] + 1)
                self.assertEqual(final["feedback_sample_count"], 0)
                self.assertEqual(final["image"]["capture_callback"],
                    "before_frame_feedback_recovery" if callback == "before_frame" else "after_frame_feedback")
                self.assertEqual(trial["final_feedback_bound"], callback == "after_frame")
                self.assertTrue(trial["debug_finish_bound"])
                harness.call("arm-measurement-reload", baseline["visit"])
                harness.clock.lua.run(f"frame={baseline['frame']}; return 'loaded'")
                loaded = harness.call("state-changed", "load", baseline["visit"])
                self.assertEqual((loaded["frame"], loaded["epoch"]), (baseline["frame"], 1))

    def test_repeated_debug_targets_cross_the_loop_boundary_after_the_final_feedback_reload(self):
        settings = debug_contest.debug_plan(category="Beauty", rank="Normal", loops=2, midpoint_diagnostics=True)
        harness, settings = self.loaded(inputs=True, settings=settings)
        baseline = deepcopy(harness.boundary)
        for index, target in enumerate(settings["targets"]):
            self.enter_dance(harness, settings, target, begin_measurement=False)
            selected = {number: [] for number in range(1, 7)} if index else None
            harness.ranked_dance(feedback_policy="record_all_buttons", midpoint_diagnostics=True, selected_inputs=selected)
            self.assertEqual(harness.boundary["snapshot"]["target"]["debug_loop"], index + 1)
            trial = harness.boundary["snapshot"]["trial"]
            self.assertEqual((trial["total_inputs"], trial["total_judgment_count"], trial["total_skipped_inputs"]),
                (0, 0, 18) if index else (18, 18, 0))
            if index:
                self.assertEqual(trial["judgments"], [])
                self.assertTrue(trial["debug_finish_bound"])
                self.assertFalse(trial["final_feedback_bound"])
                self.assertTrue(all(not button for capture in harness.rank_captures for button in capture["buttons"]))
            harness.call("arm-measurement-reload", baseline["visit"])
            harness.clock.lua.run(f"frame={baseline['frame']}; return 'loaded'")
            loaded = harness.call("state-changed", "load", baseline["visit"])
            self.assertEqual((loaded["frame"], loaded["epoch"]), (baseline["frame"], index + 1))
            harness.boundary = deepcopy(baseline)
        self.assertEqual([record["checkpoint"] for record in harness.saved_checkpoints], ["stage-1-load"])
        self.assertEqual(harness.counters()["engines"], 1)
        all_targets = debug_contest.debug_plan(category="Beauty", loops=10, midpoint_diagnostics=True)
        self.loaded(inputs=True, settings=all_targets)


class StripFallbackChecks(unittest.TestCase):
    def test_green_costume_pixels_do_not_extend_the_four_row_strip(self):
        lua = Lua51()
        self.addCleanup(lua.close)
        result = json.loads(lua.run(f"""
            local pixels = dofile({literal((ROOT / 'contest/dance_pixels.lua').as_posix())})
            local json = dofile({literal((ROOT / 'lua/json.lua').as_posix())})
            local results = {{}}
            for _, endpoint in ipairs({{50, 128}}) do
                local raw = string.char(255,254,1,0,1,128,1,0,0,0,0) .. string.rep(string.char(0,0,0,0), 256 * 384)
                local function pixel(x, y, rgb)
                    local at = 13 + (y * 256 + x) * 4
                    raw = raw:sub(1, at - 1) .. string.char(unpack(rgb)) .. raw:sub(at + 3)
                end
                local filled = {{{{24,199,24}},{{97,215,97}},{{24,199,24}},{{16,158,16}}}}
                local empty = {{{{56,65,81}},{{134,142,158}},{{134,142,158}},{{134,142,158}}}}
                for x = 8, 247 do
                    for y = 155, 158 do pixel(x, y, (x <= endpoint and filled or empty)[y - 154]) end
                end
                for x = 144, 153 do
                    pixel(x,155,{{93,239,77}}); pixel(x,156,{{93,239,77}}); pixel(x,157,{{12,158,12}})
                end
                results[#results + 1] = pixels.bar(raw)
            end
            return json.encode(results)
        """))
        self.assertEqual([item["last_x"] for item in result], [50, 128])
        self.assertEqual([item["position_bounds"] for item in result], [[50, 50], [128, 128]])
        self.assertTrue(all(item["found"] for item in result))


class CueAgreementChecks(unittest.TestCase):
    centers = (28, 58, 88, 118)
    midpoint = {"frame": 1000, "epoch": 2, "position": {"x": 128, "phase": "response"}}

    def image(self, *, left=BUTTONS, right=BUTTONS, side_offset=CUE_SIDE_OFFSET):
        image = Image.new("RGB", (256, 192), (0, 0, 0))
        pixels = image.load()
        for side, buttons in (("left", left), ("right", right)):
            sprites = LEFT_SPRITES if side == "left" else RIGHT_SPRITES
            y = 168 if side == "left" else layout_for(3)["right_marker_y"] - 8
            for center, button in zip(self.centers, buttons):
                if button is None:
                    continue
                x = center - 8 + (side_offset if side == "right" else 0)
                for dx, dy, color in sprites[button].points:
                    pixels[x + dx, y + dy] = color
        return image

    def plan(self, image, *, samples=(), read_frame=None):
        return rank_speed_probe.response_plan(self.midpoint, image, samples, read_frame, rank="Master",
            expected_cues=4, speed=timing_for("Master")["pixels_per_frame"], hold_frames=2, offset_frames=-8, dance=3)

    def debug_capture(self, folder, name, frame, x):
        image = self.image()
        pixels = image.load()
        if x is not None:
            for column in range(8, 248):
                profile = FILLED_PROFILES[0] if column <= x else EMPTY_PROFILE
                for row, color in enumerate(profile, 155):
                    pixels[column, row] = color
            for dx, dy, color in POINTER_SPRITES[0].points:
                pixels[x - 7 + dx, 134 + dy] = color
        stacked = Image.new("RGB", (256, 384))
        stacked.paste(image)
        raw = b"\xff\xfe\x01\x00\x01\x80\x01\x00\x00\x00\x00" \
            + Image.merge("RGBA", (Image.new("L", stacked.size), *stacked.split())).tobytes()
        path = folder / f"{name}.gd"
        path.write_bytes(raw)
        return {"frame": frame, "epoch": 2, "emulator_paused": True, "observation": {"image": {
            "path": str(path), "frame": frame, "epoch": 2, "capture_callback": "paused_lua_menu", "direct_read_frames": 0}}}

    def test_main_and_debug_midpoints_preserve_early_captures_and_accept_both_line_pixels(self):
        for main, positions in product((False, True), (((1000, 128),), ((1000, 129),), ((1000, 126), (1004, 128)))):
            with self.subTest(main=main, positions=positions), temporary_directory() as folder:
                work, samples = Path(folder), iter(positions)

                def command(action, *args):
                    return self.debug_capture(work, args[0], *next(samples)) if action == "capture" else {}

                client, record = SimpleNamespace(command=Mock(side_effect=command)), Mock()
                emulator = SimpleNamespace(controller=SimpleNamespace(live_deadline=None),
                    command=Mock(return_value={"stop_reason": "demo_midpoint"}))
                plan = rank_dance.stage5_plan(rank="Master", source_frame=700, host_timeout=300,
                    fixed_timing=debug_contest.fixed_timing("Cool", "Master"), entry_pointer_x=64,
                    offset_frames=-7, feedback_policy="first_button_only" if main else "record_all_buttons",
                    first_feedback_policy="warn_and_continue" if main else "require_excellent", observation_policy="scheduled")
                midpoint, planned, _ = rank_dance.prepare_response(emulator, client, work, 3, plan, record=record)
                self.assertEqual(planned["pointer_x"], positions[-1][1])
                self.assertEqual(midpoint["midpoint_status"], "on_line")
                captured = json.loads((work / "midpoint-captures.json").read_text(encoding="utf-8"))["captures"]
                self.assertEqual([(item["frame"], item["position"]["x"]) for item in captured], list(positions))
                for index, item in enumerate(captured, 1):
                    self.assertTrue((work / f"midpoint-{index:02d}.png").is_file())
                    self.assertTrue(Path(item["image"]["path"]).is_file())
                    self.assertEqual(item["image"]["direct_read_frames"], 0)
                if len(positions) > 1:
                    adjustments = json.loads((work / "midpoint-adjustments.json").read_text(encoding="utf-8"))["adjustments"]
                    self.assertEqual(adjustments[0]["next_frame"] - adjustments[0]["frame"], 4)
                    self.assertTrue((work / "midpoint-adjustment-01.png").is_file())

    def test_main_and_debug_late_unreadable_or_stalled_midpoints_install_a_continuation(self):
        cases = ((((1000, 130),), 4), (((1000, 230),), 1), (((1000, None),), 0), (((1000, 127), (1002, 127)), 0))
        for main, (positions, count) in product((False, True), cases):
            with self.subTest(main=main, positions=positions), temporary_directory() as folder:
                work, samples = Path(folder), iter(positions)
                client = SimpleNamespace(command=Mock(side_effect=lambda action, name:
                    self.debug_capture(work, name, *next(samples)) if action == "capture" else {}))
                emulator = SimpleNamespace(controller=SimpleNamespace(live_deadline=None),
                    command=Mock(return_value={"stop_reason": "demo_midpoint"}))
                plan = rank_dance.stage5_plan(rank="Master", source_frame=700, host_timeout=300,
                    fixed_timing=debug_contest.fixed_timing("Cool", "Master"), entry_pointer_x=64,
                    offset_frames=-7, feedback_policy="first_button_only" if main else "record_all_buttons",
                    first_feedback_policy="warn_and_continue" if main else "require_excellent", observation_policy="scheduled")
                midpoint, planned, reading = rank_dance.prepare_response(emulator, client, work, 3, plan, record=Mock())
                self.assertEqual(len(planned["presses"]), count)
                self.assertEqual(planned["skipped_input_count"], 4 - count)
                self.assertTrue(Path(midpoint["png"]).is_file())
                self.assertGreater(planned["response_end_frame"], midpoint["frame"])
                self.assertEqual(client.command.call_args_list[-1].args[0], "plan")
                self.assertEqual(reading["unreadable_frames"], [1000] if positions[0][1] is None else [])
                if count == 0:
                    self.assertEqual(planned["source"], "counted_midpoint_input_free_skip")
                    self.assertEqual(planned["feedback_frames"], [])

    def test_diagnostic_midpoints_record_one_existing_correction_and_continue_off_center(self):
        cases = ((127, 127, "still_early"), (126, 127, "still_early"), (127, 128, "fixed"),
            (127, 129, "fixed"), (127, 130, "overshot"), (130, None, "late_uncorrected"), (129, None, "not_needed"))
        for pre_x, post_x, outcome in cases:
            with self.subTest(pre_x=pre_x, post_x=post_x), temporary_directory() as folder:
                work = Path(folder)
                frames = (128 - pre_x) * 3 if pre_x < 128 else 0
                positions = [(1000, pre_x)] + ([(1000 + frames, post_x)] if post_x is not None else [])
                samples = iter(positions)

                def command(action, *args):
                    return self.debug_capture(work, args[0], *next(samples)) if action == "capture" else {}

                client = SimpleNamespace(command=Mock(side_effect=command))
                emulator = SimpleNamespace(controller=SimpleNamespace(live_deadline=None),
                    command=Mock(return_value={"stop_reason": "demo_midpoint"}))
                plan = rank_dance.stage5_plan(rank="Hyper", source_frame=700, host_timeout=300,
                    fixed_timing=debug_contest.fixed_timing("Beauty", "Hyper"), entry_pointer_x=64,
                    offset_frames=-7, feedback_policy="record_all_buttons", observation_policy="scheduled", midpoint_diagnostics=True)
                midpoint, planned, _ = rank_dance.prepare_response(emulator, client, work, 3, plan, record=Mock())
                diagnostic = json.loads((work / "midpoint-diagnostic.json").read_text(encoding="utf-8"))
                self.assertEqual(diagnostic["correction_outcome"], outcome)
                self.assertEqual(diagnostic["correction_count"], int(post_x is not None))
                self.assertEqual(diagnostic["correction_frames"], frames)
                final_x = pre_x if post_x is None else post_x
                self.assertEqual((planned["pointer_x"], midpoint["position"]["x"]), (final_x, final_x))
                self.assertEqual(diagnostic["post"]["estimated_first_pixel_error_frames"], (final_x - 128) * 3)
                self.assertEqual(diagnostic["post_off_first_pixel"], final_x != 128)
                self.assertEqual(diagnostic["post_off_line"], not 128 <= final_x <= 129)
                self.assertEqual(diagnostic["fixed_to_line"], pre_x < 128 and 128 <= final_x <= 129)
                self.assertEqual(diagnostic["post_capture_reused"], post_x is None)
                actions = [call.args[0] for call in client.command.call_args_list]
                self.assertEqual(actions.count("retime-midpoint"), int(post_x is not None))
                self.assertEqual(actions[-1], "plan")
                for captured in (diagnostic["pre"], diagnostic["post"]):
                    self.assertEqual(captured["direct_read_frames"], 0)
                    self.assertTrue(Path(captured["image"]).is_file() and Path(captured["raw_image"]).is_file())

    def test_missing_views_resolve_per_button_and_install_the_plan_silently(self):
        image = self.image(left=("A", None, "X", "Y"), right=("A", "B", None, "Y"))
        with temporary_directory() as folder:
            work = Path(folder)
            image.save(work / "midpoint.png")
            measured = pointer_calibration({"frame": 640, "epoch": 2, "pointer_x": 8},
                {"frame": 700, "epoch": 2, "pointer_x": 28})
            midpoint = {**self.midpoint, "image": {"path": str(work / "midpoint.png")}}
            emulator = SimpleNamespace(controller=SimpleNamespace(live_deadline=None, remaining=lambda: None),
                command=Mock(return_value={"stop_reason": "demo_midpoint"}))
            client, record, output = Mock(), Mock(), io.StringIO()
            options = rank_dance.stage5_plan(rank="Master", source_frame=700, host_timeout=300,
                pointer_calibration=measured, observation_policy="scheduled")
            with patch("contest.rank_dance.capture_result", return_value=midpoint), redirect_stdout(output):
                _, planned, reading = rank_dance.prepare_response(emulator, client, work, 3, options, record=record)
            client.command.assert_called_with("plan", (work / "response-plan.lua").as_posix())
            self.assertEqual(output.getvalue(), "")
            self.assertFalse((work / "demo-frames.bin").exists())
            self.assertFalse(reading["continuous_demo_observation"])
        self.assertEqual([press["button"] for press in planned["presses"]], list(BUTTONS))
        accepted = planned["cues"]["accepted"]
        self.assertEqual([cue["source_side"] for cue in accepted], ["right", "right", "left", "right"])
        self.assertEqual([cue["cross_checked"] for cue in accepted], [True, False, False, True])
        self.assertEqual(accepted[2]["position_source"], "mirrored_readable_left_cue")
        self.assertEqual(planned["presses"][2]["response_x"], 208)
        self.assertFalse(any(50 <= x < 66 and 168 <= y < 184 for x, y, *_ in planned["pixel_points"]))
        self.assertFalse(any(200 <= x < 216 and 168 <= y < 184 for x, y, *_ in planned["pixel_points"]))

    def test_conflicting_or_missing_fresh_cues_require_review(self):
        old = self.image()
        samples = [{"frame": 999, "epoch": 2, "readable": True}]
        cases = ((self.image(right=("B", "B", "X", "Y")), "readable cue sides disagree"),
                 (self.image(left=("A", "B", None, "Y"), right=("A", "B", None, "Y")), "expected 4 readable cue pairs; found 3"))
        for fresh, message in cases:
            with self.subTest(message=message), patch("contest.rank_speed_probe.gd_image", side_effect=lambda image: image):
                with self.assertRaisesRegex(rank_speed_probe.ReviewNeeded, message):
                    self.plan(fresh, samples=samples, read_frame=lambda sample: old)

    def test_live_128_pixel_pairs_override_the_rank_hint_and_fill_opposite_missing_cues(self):
        self.centers = (48, 64, 80, 112)
        image = self.image(left=("A", "A", None, "X"), right=("A", "A", "B", None), side_offset=128)
        planned = self.plan(image)
        self.assertEqual(planned["cues"]["side_offset_pixels"], 128)
        self.assertEqual(planned["cues"]["side_offset_source"], "fresh_paired_cue_centers")
        self.assertEqual([(press["button"], press["response_x"]) for press in planned["presses"]],
                         [("A", 176), ("A", 192), ("B", 208), ("X", 240)])
        self.assertEqual([cue["source_side"] for cue in planned["cues"]["accepted"]], ["right", "right", "right", "left"])

    def test_unpaired_extras_are_discarded_only_from_an_over_counted_side(self):
        left = [{"x": x, "button": button} for x, button in zip(self.centers, BUTTONS)]
        right = [{**cue, "x": cue["x"] + 120} for cue in left]
        for extra_left, extra_right in (([{"x": 8, "button": "B"}], []),
                ([], [{"x": 133, "button": "X"}]), ([{"x": 8, "button": "B"}], [{"x": 133, "button": "X"}])):
            with self.subTest(left=extra_left, right=extra_right):
                paired = rank_speed_probe.merge_cues(left + extra_left, right + extra_right, expected_cues=4, dance=3)
                self.assertEqual([(cue["demo_x"], cue["button"]) for cue in paired["accepted"]], list(zip(self.centers, BUTTONS)))
                self.assertEqual(len(paired["discarded_cues"]), len(extra_left) + len(extra_right))

    def test_debug_conflict_preferences_missing_sides_and_confirmed_only_partial_reads(self):
        cases = (
            (4, BUTTONS, BUTTONS, list(BUTTONS)),
            (4, BUTTONS, ("B", "X", "Y", "A"), ["A", "B", "Y", "A"]),
            (4, ("A", None, "X", "Y"), BUTTONS, list(BUTTONS)),
            (4, BUTTONS, ("A", "B", None, "Y"), list(BUTTONS)),
            (4, ("A", None, "X", "Y"), ("A", "B", None, "Y"), ["A", "Y"]),
            (4, ("A", None, None, None), (None, "B", None, None), []),
            (3, ("A", "B", "X"), ("X", "Y", "B"), ["A", "Y", "B"]),
            (3, ("A", None, "X"), ("A", "B", None), ["A"]),
        )
        for count, left, right, expected in cases:
            with self.subTest(count=count, left=left, right=right):
                offset = 128 if count == 3 else 120
                planned = rank_speed_probe.response_plan(self.midpoint, self.image(left=left, right=right, side_offset=offset),
                    [], None, rank="Normal" if count == 3 else "Master", expected_cues=count,
                    speed=0.5, hold_frames=2, offset_frames=-7, dance=3, side_offset_pixels=offset,
                    fixed_spacing=True, tolerant_inputs=True)
                self.assertEqual([press["button"] for press in planned["presses"]], expected)
                self.assertEqual(planned["skipped_input_count"], count - len(expected))
                for cue in planned["cues"]["accepted"]:
                    self.assertTrue(all(reading["button"] == cue["button"] for reading in cue["evidence"].values()))
                    self.assertEqual(cue["response_x"] - cue["demo_x"], offset)
                    self.assertTrue(cue["pixel_points"])

    def test_debug_excess_pairs_use_demo_prefix_and_response_suffix(self):
        positions = (16, 32, 48, 64, 80, 112)
        left = [{"x": x, "button": button} for x, button in zip(positions, ("A", "B", "X", "Y", "A", "B"))]
        right = [{**cue, "x": cue["x"] + 128} for cue in left]
        for count in (3, 4):
            for a, b, expected in ((left, right, list(positions[:count - 2] + positions[-2:])),
                    (left, right[:count], list(positions[:count])),
                    (left[:count], right, list(positions[:count])), (left, right[:1], [16])):
                with self.subTest(count=count, left=len(a), right=len(b)):
                    selected = rank_speed_probe.select_cues(a, b, expected_cues=count, side_offset_pixels=128)
                    self.assertEqual([cue["demo_x"] for cue in selected["accepted"]], expected)
                    self.assertTrue(all(cue["cross_checked"] for cue in selected["accepted"]))
                    if len(expected) == count:
                        self.assertEqual([cue["source_side"] for cue in selected["accepted"]],
                            ["left"] * (count - 2) + ["right"] * 2)

    def test_debug_feedback_prunes_overlaps_and_accepts_an_empty_response(self):
        profile = debug_contest.fixed_timing("Cool", "Master")
        for frames, expected in (([1010, 1015, 1050, 1095], [1010, 1050]), ([], [])):
            with self.subTest(frames=frames):
                planned = {"dance": 1, "frame": 1000, "response_end_frame": 1100, "hold_frames": 2,
                    "presses": [{"frame": frame} for frame in frames], "expected_inputs": 4, "skipped_inputs": [],
                    "cues": {"accepted": [{"pixel_points": [[index, 1, 1, 2, 3]]} for index in range(len(frames))]}}
                feedback = rank_dance.feedback_schedule(planned, delay_frames=11, rank_timing=profile, tolerant_inputs=True)
                self.assertEqual([press["frame"] for press in planned["presses"]], expected)
                self.assertEqual(planned["skipped_input_count"], 4 - len(expected))
                self.assertEqual(feedback["feedback_frames"], [frame + 11 for frame in expected])

    def damaged_cue(self, side, *, extra_miss=0):
        sprite = (LEFT_SPRITES if side == "left" else RIGHT_SPRITES)["A"]
        image = Image.new("RGB", (256, 192), (0, 0, 0))
        pixels = image.load()
        for dx, dy, color in sprite.points:
            pixels[dx, dy] = color
        offsets = list(dict.fromkeys([(dx, dy) for dx, dy, _ in sprite.probes] +
                                    [(dx, dy) for dx, dy, _ in sprite.points]))
        for dx, dy in offsets[:len(sprite.points) // 5 + extra_miss]:
            pixels[dx, dy] = (0, 0, 0)
        return pixels, sprite

    def test_cue_match_threshold_accepts_80_percent_and_rejects_lower(self):
        for side in ("left", "right"):
            with self.subTest(side=side):
                pixels, sprite = self.damaged_cue(side)
                matched = _match(pixels, 0, 0, sprite)
                self.assertIsNotNone(matched)
                self.assertGreaterEqual(matched["match_fraction"], 0.80)
                pixels, sprite = self.damaged_cue(side, extra_miss=1)
                self.assertIsNone(_match(pixels, 0, 0, sprite))


class ArtifactOwnershipChecks(unittest.TestCase):
    def test_win_cleanup_preserves_durable_references_states_and_unknown_user_files(self):
        with temporary_directory() as folder:
            work = Path(folder)
            artifacts = OwnedArtifacts(work, "owned-check", 123)
            capture, sequence, config = work / "stage1.png", work / "frames.bin", work / "section-config.lua"
            live, user = work / "attach.lua", work / "user-photo.png"
            for path in (capture, sequence, config, live, user):
                path.write_bytes(b"owned fixture")
            user_folder = work / "user-tools"
            user_folder.mkdir()
            (user_folder / "stage4.png").write_bytes(b"user image")
            (work / "checkpoints").mkdir()
            state = work / "checkpoints" / "accepted.ds6"
            state.write_bytes(b"DeSmuME SState retained")
            receipt = work / "checkpoints" / "accepted.json"
            points = {"points": [[88, 44, 77, 81, 142]], "reference": {"path": str(capture),
                "checkpoint": str(receipt), "frame": 80, "epoch": 1}}
            write_json(work / "overworld-reference.json", points)
            write_json(work / "runner-result.json", {"screen": {"path": str(capture), "retained": True}, "save_state": str(state)})
            write_json(work / "user-data.json", {"path": str(capture)})
            write_json(user_folder / "runner-result.json", {"path": str(capture)})
            artifacts.register_tree(work)
            artifacts.register(live, kind="generated_configuration", protected=True)
            manifest = artifacts.cleanup("win-01")
            self.assertFalse(any(path.exists() for path in (capture, sequence, config)))
            self.assertTrue(all(path.exists() for path in (state, live, user)))
            self.assertTrue((user_folder / "stage4.png").exists())
            result = json.loads((work / "runner-result.json").read_text(encoding="utf-8"))
            self.assertFalse(result["screen"]["retained"])
            self.assertFalse(result["screen"]["artifact_retention"]["path"]["retained"])
            self.assertEqual(result["screen"]["artifact_retention"]["path"]["manifest"], str(manifest))
            self.assertEqual(json.loads((work / "overworld-reference.json").read_text(encoding="utf-8"))["points"], points["points"])
            previous = {"frame": 80, "epoch": 1, "status": "complete", "proceed": True, "snapshot": {"screen": {"confirmed": True,
                "expected": "loaded_overworld", "source": "visual_review", "frame": 80, "epoch": 1, "path": str(capture)}}}
            with patch("contest.stage6_act.accepted_source", return_value=(receipt, state, previous)):
                self.assertEqual(stage6_act.overworld_reference(receipt)["points"], points["points"])
            self.assertEqual(json.loads((work / "user-data.json").read_text(encoding="utf-8")), {"path": str(capture)})
            self.assertEqual(json.loads((user_folder / "runner-result.json").read_text(encoding="utf-8")), {"path": str(capture)})

    def test_startup_checks_all_hosts_before_deleting_and_keeps_live_lua(self):
        with temporary_directory() as folder:
            parent = Path(folder)
            old, busy, legacy, foreign = (parent / name for name in ("old", "busy", "legacy", "foreign"))
            for work in (old, busy, legacy, foreign):
                work.mkdir()
                (work / "stage4.png").write_bytes(b"capture")
            old_artifacts = OwnedArtifacts(old, "old", 101)
            old_artifacts.register_tree(old)
            OwnedArtifacts(busy, "busy", 102).register_tree(busy)
            live_lua = old / "attach.lua"
            live_lua.write_text("return {}\n", encoding="utf-8")
            old_artifacts.register(live_lua, kind="generated_configuration")
            old_backup = old / "previous-slot-9.ds9"
            old_backup.write_bytes(b"restored backup")
            old_artifacts.register(old_backup, kind="slot_backup", protected=True)
            write_json(old / "runner-result.json", {"temporary_slot_9_restored": True, "cleanup_errors": []})
            unfinished = old / "visits" / "s5-001" / "dance-01"
            unfinished.mkdir(parents=True)
            unindexed = unfinished / "demo-frames.bin"
            unindexed.write_bytes(b"last capture before host exit")
            write_json(legacy / "runner-setup.json", {"run_id": "legacy", "host_pid": 103, "stage": 4, "work_dir": str(legacy)})
            write_json(legacy / "runner-result.json", {"outcome": "fail", "cleanup_errors": []})
            (legacy / "previous-slot-10.ds0").write_bytes(b"obsolete backup")
            with self.assertRaisesRegex(RuntimeError, "still active"):
                cleanup_stale(parent, current_pid=999, process_alive=lambda pid: pid == 102)
            self.assertTrue((old / "stage4.png").exists())
            facts = cleanup_stale(parent, current_pid=999, protected=[live_lua], process_alive=lambda _: False)
            self.assertEqual(facts["removed_files"], 6)
            self.assertTrue(live_lua.exists() and (foreign / "stage4.png").exists())
            self.assertFalse(unindexed.exists())
            self.assertFalse((legacy / "previous-slot-10.ds0").exists())


class MasterLifecycleChecks(unittest.TestCase):
    def runtime(self, work, settings):
        controller = SimpleNamespace(live_deadline=None, remaining=lambda: 300, session=lambda: {"pid": 7, "window": 11},
            set_pause=Mock(), screenshot=Mock(), paused=lambda _: True,
            status=lambda: {"frame": 100, "buttons": "", "stylus_touch": False})
        runtime = ContestRuntime("lifecycle-check", work, settings, time.monotonic(), controller=controller)
        runtime.emulator = Mock()
        runtime.emulator.set_frame_limit.side_effect = lambda enabled: {"frame_limit": enabled}
        runtime.client = Mock()
        runtime.owned = runtime.attached = True
        return runtime

    def test_unlimited_debug_run_has_no_whole_run_or_stage_deadline(self):
        settings = debug_contest.debug_plan(no_timeout=True)
        self.assertFalse(settings["host_timeouts_enabled"])
        self.assertTrue(debug_contest.debug_plan()["host_timeouts_enabled"])
        with temporary_directory() as folder:
            runtime = self.runtime(Path(folder), settings)
            self.assertIsNone(runtime.total_deadline)
            runtime.begin_visit(5, 1)
            self.assertIsNone(runtime.stage_deadline)
            self.assertIsNone(runtime.controller.live_deadline)
            runtime.action("observe-demo")
            runtime.emulator.command.assert_called_once_with("observe-demo", timeout=None, deadline=None)

    def test_unlimited_poll_detects_a_closed_emulator(self):
        alive, windows = Mock(return_value=True), Mock(return_value=True)
        controller = TimedController(windows_api=SimpleNamespace(process_alive=alive,
            USER32=SimpleNamespace(IsWindow=windows)))
        controller.live_session = {"pid": 7, "window": 11, "lua_window": 12}
        self.assertTrue(controller.waiting(None))
        alive.return_value = False
        with self.assertRaisesRegex(SystemExit, "emulator was closed"):
            controller.waiting(None)

    def test_manual_emulator_close_restores_slot_storage_without_native_commands(self):
        with temporary_directory() as folder:
            work = Path(folder)
            previous, native = work / "previous.ds0", work / "native.ds0"
            previous.write_bytes(b"original slot")
            native.write_bytes(b"run baseline")
            runtime = self.runtime(work, debug_contest.debug_plan(no_timeout=True))
            runtime.originals[10] = previous
            runtime.emulator.states.path.return_value = native
            result = runtime.finalize(error="The controlled emulator was closed.", emulator_closed=True)
            self.assertEqual(native.read_bytes(), previous.read_bytes())
            self.assertEqual(result["errors"], [])
            self.assertTrue(result["emulator_closed"] and result["temporary_slot_10_restored"])
            self.assertIsNone(result["emulator_paused"])
            runtime.controller.set_pause.assert_not_called()
            runtime.client.command.assert_not_called()
            runtime.emulator.stop_movie.assert_not_called()
            self.assertEqual(runtime.lifecycle["terminal_cleanups"], 1)

    def test_main_and_debug_speed_policies_and_terminal_cleanup(self):
        for runner in ("main", "debug"):
            with self.subTest(runner=runner), temporary_directory() as folder:
                diagnostic = runner == "debug"
                settings = debug_contest.debug_plan() if diagnostic else super_contest.chain_plan()
                runtime = self.runtime(Path(folder), settings)
                for phase in ("run", "run", "dressing", "dressing", "run"):
                    runtime.set_speed(phase)
                self.assertEqual([call.args[0] for call in runtime.emulator.set_frame_limit.call_args_list], [False, False, False])
                self.assertEqual(settings["native_osd_settle_host_seconds"], 0)
                runtime.emulator.restore_normal_speed.assert_not_called()
                client = runtime.client
                runtime.total_deadline = time.monotonic() + 10
                runtime.begin_visit(1, 1)
                self.assertEqual(runtime.stage_deadline, runtime.total_deadline)
                self.assertIs(runtime.client, client)
                if diagnostic:
                    runtime.protect_slot = Mock()
                    runtime.client.stop.side_effect = RuntimeError("capture failed")
                result = runtime.finalize(error="stopped" if diagnostic else None)
                if diagnostic:
                    self.assertIn("capture failed", result["errors"])
                else:
                    runtime.client.command.assert_called_once_with("terminal")
                    self.assertEqual(result["errors"], [])
                runtime.emulator.stop_movie.assert_called_once()
                runtime.emulator.restore_normal_speed.assert_called_once()
                with self.assertRaisesRegex(RuntimeError, "only once"):
                    runtime.finalize()


class CollectionLoopChecks(unittest.TestCase):
    def collection(self, parent, mode, *, debug=super_contest.debug, stop=False):
        settings = super_contest.chain_plan(debug=debug, true_retry=mode)
        instances = []

        class LocalRuntime:
            def __init__(self, run_id, work, settings, started):
                self.run_id, self.work, self.settings = run_id, work, settings
                self.total_deadline = started + settings["total_timeout_seconds"]
                self.controller = SimpleNamespace(live_deadline=self.total_deadline, session=lambda: {"pid": 7})
                self.originals, self.accepted_slots, self.sources = {}, {}, {}
                self.bag_context = {"opened_once": True, "cursor_position": 7}
                self.owned_bits, self.loads, self.captures = set(range(18)), [], []
                self.preparations = []
                self.target, self.boundary, self.party = None, None, None
                self.frame, self.epoch, self.checkpoints, self.finalizations = 100, 0, 0, 0
                self.missed, self.active_work = False, work
                self.overworld = {"points": [[88, 44, 77, 81, 142]]}
                instances.append(self)

            def confirm_ownership(self):
                return None

            def prepare_fresh_ownership(self, session):
                assert session is None
                assert not self.work.exists() and old_capture.exists()

            def setup(self):
                assert not old_capture.exists()
                assert (self.work / "startup-cleanup.json").is_file()
                self.json("overworld-reference.json", self.overworld)
                (self.work / "user-photo.png").write_bytes(b"user work")

            def check(self):
                return self.controller.live_deadline - time.monotonic()

            def begin_visit(self, stage, visit):
                self.stage, self.stage_started = stage, time.monotonic()
                self.stage_deadline = min(self.total_deadline, self.stage_started + self.settings["stage_timeout_seconds"])
                self.controller.live_deadline = self.stage_deadline
                self.active_work = self.work / "visits" / f"s{stage}-{visit:03d}"
                self.active_work.mkdir(parents=True)
                return self.active_work

            def json(self, name, value):
                write_json(self.active_work / name, value)

            def retry(self, slot):
                assert self.boundary["checkpoint"]["decision"] == "miss"
                assert self.controller.live_deadline == self.stage_deadline
                saved = self.sources[slot]
                self.boundary = deepcopy(saved["boundary"])
                self.target, self.owned_bits = deepcopy(saved["target"]), set(saved["bits"])
                self.bag_context = deepcopy(saved["bag"])
                self.frame, self.epoch = self.boundary["checkpoint"]["frame"], self.epoch + 1
                self.loads.append({"slot": slot, "stage": self.stage, "ribbons": sorted(self.owned_bits)})

            def emit(self, stage, *, decision="pass"):
                self.frame, self.checkpoints = self.frame + 10, self.checkpoints + 1
                missing = next((bit for bit in range(20) if bit not in self.owned_bits), None)
                next_target = None if missing is None else {"category": "Tough", "rank": "Hyper" if missing == 18 else "Master",
                    "ribbon_bit": missing, "party_slot": 1}
                self.party = {"owned_count": len(self.owned_bits), "all_ribbons_owned": missing is None,
                    "owned_ribbon_bits": sorted(self.owned_bits), "next_target": next_target}
                names = {1: "stage-1-load", 2: "stage-2-prep", 3: "stage-3-enter", 4: "stage-4-dress", 5: "stage-5-dance", 6: "stage-6-act"}
                checkpoint = {"checkpoint": names[stage], "visit": self.checkpoints, "run_id": self.run_id,
                    "frame": self.frame, "epoch": self.epoch, "status": "complete", "proceed": True, "decision": decision,
                    "save_required": stage == 5 or self.settings["debug"] == 1 and stage in (2, 6),
                    "snapshot": {"target": self.target, "party": self.party}}
                slot = 10 if stage == 5 else stage
                state = self.work / "checkpoints" / f"{self.checkpoints:04d}-stage-{stage}.ds{slot % 10}"
                saved = None
                if checkpoint["save_required"]:
                    state.write_bytes(b"DeSmuME SState accepted")
                    saved = {"retained_path": str(state), "slot": slot}
                    self.accepted_slots[slot] = state
                receipt = state.with_suffix(".json")
                write_json(receipt, {"completed": checkpoint, "host_savestate": saved})
                self.boundary = {"checkpoint": checkpoint, "save_state": saved, "receipt": str(receipt)}
                if stage in (2, 5):
                    self.sources[slot] = {"boundary": deepcopy(self.boundary), "target": deepcopy(self.target),
                        "bits": sorted(self.owned_bits), "bag": deepcopy(self.bag_context)}
                image = self.active_work / f"stage{stage}.png"
                image.write_bytes(b"consumed capture")
                self.captures.append(image)
                return {"stage": stage, "outcome": "miss" if decision == "miss" else "pass", "checkpoint": checkpoint,
                    "checkpoint_receipt": str(receipt), "save_state": saved,
                    "frame": self.frame, "target": self.target, "screenshot": str(image),
                    "ribbon_acquired": decision == "hit", "completed_attempts": 1, "final_party": self.party}

            def finalize(self, *, error=None):
                self.finalizations += 1
                return {"errors": [], "frame": self.frame, "emulator_paused": True, "inputs_released": True,
                    "lifecycle": {"lua_attachments": 1, "checkpoint_engines": 1, "terminal_cleanups": self.finalizations,
                        "retry_loads": self.loads}}

        def section(runtime, **_):
            if runtime.stage == 2:
                runtime.preparations.append(deepcopy(runtime.boundary["checkpoint"]))
                if len(runtime.owned_bits) > 18:
                    assert all(not image.exists() for image in runtime.captures)
                    assert all(state.exists() for state in runtime.accepted_slots.values())
                    assert runtime.overworld["points"] == [[88, 44, 77, 81, 142]]
                bit = next(bit for bit in range(20) if bit not in runtime.owned_bits)
                runtime.target = {"category": "Tough", "rank": "Hyper" if bit == 18 else "Master", "ribbon_bit": bit, "party_slot": 1}
            if runtime.stage != 6:
                return runtime.emit(runtime.stage)
            if stop:
                (runtime.active_work / "failure.png").write_bytes(b"stopped evidence")
                raise TimeoutError("Configured host deadline reached")
            if not runtime.missed:
                runtime.missed = True
                return runtime.emit(6, decision="miss")
            runtime.owned_bits.add(runtime.target["ribbon_bit"])
            return runtime.emit(6, decision="hit")

        (parent / "tmp" / "contest").mkdir(parents=True)
        old_work = parent / "tmp" / "contest" / "old-run"
        old_work.mkdir()
        old_capture = old_work / "stage3.png"
        old_capture.write_bytes(b"stale capture")
        old_artifacts = OwnedArtifacts(old_work, "old-run", 999999)
        old_artifacts.register_tree(old_work)
        with patch("contest.super_contest.ROOT", parent), patch("contest.super_contest.ContestRuntime", LocalRuntime), \
                patch.dict("contest.sections.RUNNERS", {stage: section for stage in range(1, 7)}, clear=True), \
                patch("builtins.print"):
            result = super_contest._run("collection-check", settings, time.monotonic())
        return result, instances[0]

    def test_collection_routes_misses_and_removes_consumed_scratch_before_next_target(self):
        for mode, debug in ((0, 1), (1, 0)):
            with self.subTest(mode=mode, debug=debug), temporary_directory() as folder:
                result, runtime = self.collection(Path(folder), mode, debug=debug)
                self.assertEqual(result["outcome"], "pass")
                self.assertEqual([item["ribbon_bit"] for item in result["ribbons_acquired"]], [18, 19])
                self.assertEqual((result["initial_owned_ribbon_count"], result["owned_ribbon_count"]), (18, 20))
                self.assertEqual([(item["slot"], item["stage"]) for item in runtime.loads], [] if mode else [(10, 6)])
                if mode:
                    retry_source = runtime.preparations[1]
                    self.assertEqual((retry_source["checkpoint"], retry_source["decision"], retry_source["epoch"]), ("stage-6-act", "miss", 0))
                    self.assertEqual(retry_source["snapshot"]["party"]["owned_ribbon_bits"], list(range(18)))
                    self.assertEqual([item["stage"] for item in result["visits"]], [1] + [2, 3, 4, 5, 6] * 3)
                else:
                    self.assertEqual(runtime.loads[0]["ribbons"], list(range(18)))
                self.assertEqual(runtime.finalizations, 1)
                self.assertFalse(any(path.exists() for path in runtime.captures))
                self.assertTrue((runtime.work / "user-photo.png").exists())
                self.assertEqual(len(result["cleanup_manifests"]), 3)

    def test_timeout_keeps_stopped_images_and_latest_accepted_state(self):
        with temporary_directory() as folder:
            result, runtime = self.collection(Path(folder), 0, stop=True)
            self.assertEqual(result["outcome"], "timeout")
            self.assertEqual(result["ribbons_acquired"], [])
            self.assertEqual(runtime.finalizations, 1)
            self.assertTrue((runtime.active_work / "failure.png").exists())
            self.assertTrue(Path(result["last_save_state"]).exists())
            self.assertEqual(len(result["cleanup_manifests"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
