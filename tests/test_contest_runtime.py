"""Persistent contest transitions, paired retries, counted pages, and owned cleanup."""

from copy import deepcopy
from contextlib import contextmanager, ExitStack, redirect_stdout
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

from contest import dance_response, rank_dance, sections, stage1_load, stage2_prep, stage3_enter, stage4_dress, stage6_act, super_contest
from contest.dance_timings import RANKS, fixed_timing
from contest.messages import abort_message
from contest.pt_dance_read import BUTTONS, CUE_SIDE_OFFSET, EMPTY_PROFILE, FILLED_PROFILES, LEFT_SPRITES, POINTER_SPRITES, RIGHT_SPRITES, _match, layout_for
from contest.runtime import ContestRuntime, TimedController, TimedSlots
from contest.stage2_bag import Stage2BagState
from host.checkpoints import CheckpointStopped, write_json
from host.savestates import StateSlots
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
HEART_ROWS = (".dd.dd..", "dwydrrd.", "dyorrrd.", "dorrrrd.", ".drrrd..", "..drd...", "...d....")
HEART_COLORS = {"d": (181, 0, 0), "w": (255, 255, 255), "y": (255, 132, 24),
                "o": (255, 74, 24), "r": (255, 24, 24)}
MOVE_HEART_ORIGINS = ((59, 240), (187, 240), (59, 336), (187, 336))
PLAYER_PANEL_COLORS = ((255, 239, 134), (255, 207, 24))
OPPONENT_PANEL_COLORS = ((0, 207, 255), (105, 215, 255))


def paint_move_hearts(image, slot, category, hearts):
    x, y = MOVE_HEART_ORIGINS[slot - 1]
    image.paste(MOVE_FILLS[category], (x, y, x + 48, y + 7))
    for cell in range(hearts):
        for dy, row in enumerate(HEART_ROWS):
            for dx, code in enumerate(row):
                if code != ".":
                    image.putpixel((x + cell * 8 + dx, y + dy), HEART_COLORS[code])


def paint_acting_panel(image, position, colors):
    for x in (4, 40, 72):
        for dy, color in zip((36, 42), colors):
            image.putpixel((x, (position - 1) * 48 + dy), color)


class ContestClock:
    """Real dispatcher/sections/checkpoints; selected memory and visual inputs are simulated."""

    def __init__(self, rank="Master", *, debug=super_contest.debug):
        profile = fixed_timing("Cool", rank)
        tier = RANKS.index(profile["rank"])
        self.target = {**TARGET, "rank": profile["rank"], "ribbon_bit": tier, "rank_down_presses": tier}
        dressing = stage4_dress.dress_plan(host_timeout=300)["automatic_dressing"]
        self.full_check = dressing["full_checks"][profile["rank"]]
        self.pearl = self.full_check.get("pearl_rgb", dressing["indicator_rgb"]["pearl"])
        self.heart = self.full_check.get("heart_rgb", dressing["indicator_rgb"]["heart"])
        self.clock = BridgeClock(ROOT / "lua" / "input_bridge.lua", game_code="CPUJ")
        self.config_id, self.boundary, self.preparation = 0, None, None
        self.saved_checkpoints, self.log_checkpoints = [], []
        self.work = (ROOT / "tmp" / "offline-contest-runtime").as_posix()
        self.clock.lua.run(f"""
            local real_dofile, real_loadfile, old_open = dofile, loadfile, io.open
            configs, engine_count, registrations, stars_read, move_reads, page_reads = {{}}, 0, {{}}, 0, 0, 0
            bag_snapshots, options_snapshots, accessory_snapshots, options_save = 0, 0, 0, 0x02040000
            party_snapshots = 0
            memory_values[0x02060034], memory_values[0x02060038], memory_values[0x0206003C] = 1, 52, 0x64
            memory_values[0x02060104], memory_values[0x02060108], memory_values[0x0206010C] = 14, 0x83C, 0x5000
            memory_values[0x02045808] = 0x55
            memory_values[0x02040078] = 0x3402
            target, held_item, won, party_sheen = {literal(self.target)}, 0, false, 20
            scarf_quantities = {{[260]=2,[261]=2,[262]=2,[263]=2,[264]=2}}
            native_raw = string.char(255,254,1,0,1,128,1,0,0,0,0)
                .. string.rep(string.char(0,121,247,215), 256 * 384)
            function pixel(x, y, rgb)
                local at = 13 + (y * 256 + x) * 4
                native_raw = native_raw:sub(1, at - 1) .. string.char(unpack(rgb)) .. native_raw:sub(at + 3)
            end
            function paint_acting_moves(categories, hearts)
                local fills, points, origins = {literal(MOVE_FILLS)}, {literal(MOVE_PROBES)}, {literal(MOVE_HEART_ORIGINS)}
                local rows, colors = {literal(HEART_ROWS)}, {literal(HEART_COLORS)}
                hearts = hearts or {{2,2,2,2}}
                for slot, probes in ipairs(points) do
                    local fill, origin = fills[categories[slot]], origins[slot]
                    for _, point in ipairs(probes) do pixel(point[1], point[2], fill) end
                    for dy, row in ipairs(rows) do
                        local parts = {{}}
                        for cell = 1, 6 do
                            for dx = 1, 8 do
                                local code = cell <= hearts[slot] and row:sub(dx, dx) or '.'
                                parts[#parts + 1] = string.char(0, unpack(colors[code] or fill))
                            end
                        end
                        local painted = table.concat(parts)
                        local at = 12 + ((origin[2] + dy - 1) * 256 + origin[1]) * 4
                        native_raw = native_raw:sub(1, at - 1) .. painted .. native_raw:sub(at + #painted)
                    end
                end
            end
            function paint_acting_order(position)
                for row = 1, 4 do
                    local colors = row == position and {literal(PLAYER_PANEL_COLORS)} or {literal(OPPONENT_PANEL_COLORS)}
                    for _, x in ipairs({{4,40,72}}) do
                        pixel(x, (row - 1) * 48 + 36, colors[1])
                        pixel(x, (row - 1) * 48 + 42, colors[2])
                    end
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
                return {{frame=at, party_slot=1, species_id=151, held_item_id=held_item, held_item='Red Scarf',
                    read_valid=true, data_validation_passed=true, is_egg=false, ribbon_flags=flags,
                    current_hp=100, max_hp=100, sheen=party_sheen, move_count=4, moves={{1,2,3,4}},
                    unavailable_move_slots=setmetatable({{}}, {{__json_array=true}}),
                    owned_ribbon_bits=bits, owned_count=count, all_ribbons_owned=false, next_target=next_target}}
            end
            function read_party(at, startup_party)
                party_snapshots = party_snapshots + 1
                if party_unreadable then error('The first Pokemon failed stored-data validation') end
                local value = party(at)
                if startup_party then
                    for _, field in ipairs({{'is_egg','current_hp','max_hp','hp_encoding','sheen',
                        'moves','move_count','unavailable_move_slots'}}) do value[field] = startup_party[field] end
                    value.static_read_frame = startup_party.static_read_frame or startup_party.frame
                    value.read_scope = 'items_and_ribbons'
                else
                    value.static_read_frame, value.read_scope = at, 'startup_readiness'
                end
                return value
            end
            dofile = function(path)
                if path:match('/bag_contest%.lua$') then
                    local options_reader = real_dofile({literal((ROOT / 'lua/platinum.lua').as_posix())})
                    return {{new=function() return {{snapshot=function(at, include_options, startup_party)
                        bag_snapshots = bag_snapshots + 1
                        local items = {{}}
                        for index=1,6 do items[index]={{position=index,item_id=index,quantity=2}} end
                        for id=260,264 do
                            if scarf_quantities[id] then
                                items[#items+1]={{position=#items+1,item_id=id,quantity=scarf_quantities[id]}}
                            end
                        end
                        local options, accessories
                        if include_options then
                            options_snapshots = options_snapshots + 1
                            options = options_reader.read_options(memory, options_save, at)
                            accessory_snapshots = accessory_snapshots + 1
                            accessories = options_reader.read_accessories(memory, options_save, at)
                        end
                        return {{frame=at,game={{code='CPUJ',supported=true}},party=read_party(at, startup_party),
                            bag={{pocket='Items',items=items,count=#items,position_base=1}},
                            options=options,accessories=accessories,access={{direct_read_frames=0,injects_inputs=false}}}}
                    end}} end}}
                elseif path:match('/party_contest%.lua$') then
                    return {{new=function() return {{read_first=read_party}} end}}
                elseif path:match('/screen_checks%.lua$') then
                    local module = real_dofile(path)
                    module.read=function(_, expected)
                        return {{source='local_reference_pixels',confirmed=offline_screen_confirmed ~= false,
                            expected=expected,direct_read_frames=0}}
                    end
                    return module
                elseif path:match('/acting_pixels%.lua$') then
                    local module = real_dofile(path)
                    local read_moves = module.read_move_categories
                    module.read_move_categories = function(raw, unavailable)
                        move_reads=move_reads+1
                        return read_moves(raw, unavailable)
                    end
                    module.read_stars=function() stars_read=stars_read+1
                        local empty=first_judge_capture; first_judge_capture=false
                        acting_read_index=(acting_read_index or 0)+1
                        local counts=acting_star_counts and acting_star_counts[acting_fixture_round or acting_read_index]
                            or ((acting_fixture_round == 1 or empty) and {{0,0,0}} or {{0,4,1}})
                        local readable = acting_judge_unreadable ~= true
                        return {{readable=readable,all_empty=counts[1]==0 and counts[2]==0 and counts[3]==0,
                            counts=counts}}
                    end
                    return module
                elseif path:match('/dance_pixels%.lua$') then
                    return {{bar=function()
                        if rank_unreadable then return {{found=false,first_x=256,last_x=-1,white_flash=true}} end
                        if entry_pointer_x or rank_pointer_override then
                            local x = entry_pointer_x or rank_pointer_override
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
                                    return event.label == 'Excellent' and {{label='Excellent',x=190,y=10,
                                        matched_rows=2,source='excellent_bubble_palette'}} or nil
                                end
                            end
                            return nil
                        end
                        if rank_feedback_due and frame < rank_feedback_due then return nil end
                        return {{label='Excellent',x=190,y=10,matched_rows=2,source='excellent_bubble_palette'}} end}}
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
        profile_path = self.work + "/timing-profile.lua"
        self.clock.lua.run(f"configs[{literal(profile_path)}]={literal(super_contest.load_profile())}; return 'ready'")
        self.call("init-run", "integrated-check", self.work, debug, profile_path)

    def close(self):
        self.clock.close()

    def config(self, value):
        self.config_id += 1
        path = f"{self.work}/config-{self.config_id}.lua"
        self.clock.lua.run(f"configs[{literal(path)}] = {literal(value)}; return 'ready'")
        return path

    def file_bytes(self, path):
        """Pass binary mock captures through the Lua helper's UTF-8 text interface."""
        text = self.clock.lua.run(f"local raw=assert(mock_files[{literal(str(path))}]); "
            "return raw:gsub('[\\128-\\255]', function(c) local byte=c:byte(); "
            "return string.char(192+math.floor(byte/64),128+byte%64) end)")
        return text.encode("latin-1")

    def call(self, action, *args):
        if action in ("round-readiness", "round-visit"):
            self.clock.lua.run(f"acting_fixture_round={int(args[0])}; return 'ready'")
        result = self.clock.checkpoint(action, *args)
        if result.get("error"):
            raise AssertionError(result["error"])
        return result

    def begin(self, stage, plan, **context):
        self.stage = stage
        source = self.boundary
        config = {**plan, "stage": stage, "run_id": "integrated-check", "section_visit": self.config_id + 1,
            "work": f"{self.work}/s{stage}-{self.config_id + 1}", "source_frame": source["frame"] if source else 0,
            "source_context": {key: source[key] for key in ("checkpoint", "run_id", "visit", "frame", "epoch")} if source else None,
            "target": source["snapshot"].get("target", self.target) if source else self.target, **context}
        if stage == 1:
            config["total_emulator_frames"] += 204
        result = self.call("begin-section", self.config(config))
        return config, result

    def acknowledge(self, pending):
        record = pending["checkpoint"]
        saving = record["save_required"]
        slots = {"stage-2-prep": 2, "stage-4-dress": 4, "stage-5-dance": 5, "stage-6-act": 6}
        slot = slots.get(record["checkpoint"], 9)
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
        if self.stage == 1:
            self.read_memory()
        capture = self.call("capture")
        return self.acknowledge(self.call("visit", "pass", "local_reference_pixels", f"{self.work}/review.png", "Native fixture accepted.",
            capture["frame"], capture["epoch"]))

    def read_memory(self):
        self.clock.command("combo", "inputs", "X:12:90")
        result = self.call("inspect")
        self.clock.command("combo", "inputs", "X:12:90")
        return result

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
        if prep["inputs"]:
            self.clock.lua.run(f"""
                local previous, required = held_item, {prep['expected_item_id']}
                scarf_quantities[required] = scarf_quantities[required] - 1
                if scarf_quantities[required] == 0 then scarf_quantities[required] = nil end
                if previous >= 260 and previous <= 264 then
                    scarf_quantities[previous] = (scarf_quantities[previous] or 0) + 1
                end
                held_item = required
                return 'ready'
            """)
        self.acknowledge(self.call("visit"))
        self.preparation = deepcopy(self.boundary)
        return initial, prep

    def enter(self):
        target = self.boundary["snapshot"]["target"]
        plan = stage3_enter.entry_plan(target["category"], target["rank"], host_timeout=300, hold_frames=1, release_frames=1,
            intro_seconds=1 / stage4_dress.FRAME_RATE)
        self.begin(3, plan)
        self.clock.command("combo", "inputs", *plan["inputs"])
        self.review()

    def dress(self, taps, *, finish=False):
        plan = stage4_dress.dress_plan(host_timeout=300, transition_seconds=1 / stage4_dress.FRAME_RATE)
        dressing = plan["automatic_dressing"]
        self.full_check = dressing["full_checks"][self.boundary["snapshot"]["target"]["rank"]]
        self.pearl = self.full_check.get("pearl_rgb", dressing["indicator_rgb"]["pearl"])
        self.heart = self.full_check.get("heart_rgb", dressing["indicator_rgb"]["heart"])
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
            self.clock.lua.run("entry_pointer_x=8; return 'ready'")
            capture = self.call("capture")
            evidence = {"frame": capture["frame"], "epoch": capture["epoch"], "dance": 1,
                "source": "fresh_zero_frame_left_tip_pixels", "path": config["work"] + "/stage4.gd",
                "phase": "demo",
                "pointer": {"status": "exact", "position_bounds": [8, 8]}}
            self.acknowledge(self.call("visit", self.config(evidence)))
            self.clock.lua.run("entry_pointer_x=nil; return 'ready'")
        return config, handoff, progress

    def act(self, *, win=False, rounds=4, move_categories=None, move_hearts=None, star_counts=None,
            acting_positions=None, entry_change=None, after_entry=None, before_round=None, before_result=None,
            before_post_acting=None, finish_results=True):
        plan = stage6_act.acting_plan(host_timeout=300, hold_frames=1, release_frames=1,
                                     round_seconds=1, final_seconds=1)
        self.begin(6, plan, attempt=1, overworld={"points": [[1, 1, 121, 247, 215]],
            "player_points": [[1, 1, 121, 247, 215]],
            "dialogue_top": {"points": [[128, 146, 121, 247, 215]], "bounds": stage6_act.DIALOGUE_TOP_BOUNDS},
            "reference": {"source": "native_fixture"}})
        self.call("begin-attempt")
        self.clock.command("advance", 10)
        self.call("render-prepared")
        categories = move_categories or ["Beauty", "Tough", "Smart", "Cute"]
        hearts = [2, 2, 2, 2] if move_hearts is None else move_hearts
        positions = [1, 1, 1, 1] if acting_positions is None else acting_positions
        self.clock.lua.run(f"paint_acting_moves({literal(categories)}, {literal(hearts)}); "
            f"paint_acting_order({positions[0]}); first_judge_capture=true; acting_judge_unreadable=false; "
            f"acting_star_counts={literal(star_counts)}; acting_read_index=0; return 'ready'")
        if entry_change:
            entry_change(self)
        self.act_records, self.act_snapshots, self.act_readiness = [], [], []
        plans = []
        for number in range(1, rounds + 1):
            if number > 1:
                self.clock.lua.run(f"paint_acting_order({positions[number - 1]}); return 'ready'")
            if before_round:
                before_round(self, number)
            readiness = self.call("round-readiness", number)["observation"]
            self.act_readiness.append(readiness)
            if not readiness["readiness"]["ready"]:
                self.act_snapshots.append({**readiness["readiness"], "image": readiness["image"],
                    "trial": readiness["trial"]})
                raise AssertionError("The next Acting round is not ready for move inputs.")
            self.call("round-ready", number)
            pending = self.call("round-visit", number)
            self.act_records.append(pending["checkpoint"])
            self.act_snapshots.append(pending["checkpoint"]["snapshot"])
            checked = self.acknowledge(pending)
            chosen = checked["checkpoint"]["snapshot"]["plan"]
            plans.append(chosen)
            self.clock.command("combo", "inputs", *chosen["steps"])
            if number == 1 and after_entry:
                after_entry(self)
        self.act_post_acting = []
        if rounds == 4 and finish_results:
            self.clock.lua.run(f"won={literal(win)}; return 'ready'")
            while True:
                if before_post_acting:
                    before_post_acting(self, len(self.act_post_acting) + 1)
                readiness = self.call("post-acting-readiness")["observation"]
                self.act_post_acting.append(readiness)
                self.clock.command("combo", "inputs", *readiness["pause_plan"]["steps"])
                if readiness["readiness"]["ready"]:
                    self.call("post-acting-settled")
                    break
            self.clock.command("combo", "inputs", plan["memory_x_input"])
            if before_result:
                before_result(self)
            self.call("inspect")
            self.clock.command("combo", "inputs", plan["memory_x_input"])
            self.acknowledge(self.call("result-visit"))
        return plans

    def ranked_dance(self, *, bad_feedback=None, early_midpoint_x=None,
                      feedback_delays=None, missing_feedback=(), cue_side_offset_pixels=None, final_response_x=None,
                       midpoint_positions=None, post_fix_pointer_x=None,
                       selected_inputs=None, changed_cues=(), missed_inputs=None, unreadable_midpoints=(), missed_feedback=None,
                       transition_seconds=1 / stage4_dress.FRAME_RATE, changed_paused_framebuffer=False,
                       rejected_plans=(), handoff_faults=(), counted_display_hold=False):
        target = self.boundary["snapshot"]["target"]
        fixed = fixed_timing(target["category"], target["rank"])
        entry_x = self.boundary["snapshot"]["screen"]["pointer"]["position_bounds"][0]
        plan = rank_dance.stage5_plan(rank=target["rank"], source_frame=self.boundary["frame"], host_timeout=300,
            transition_seconds=transition_seconds,
            fixed_timing=fixed, entry_pointer_x=entry_x)
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
            counted_anchor = None
            folder = f"{self.work}/rank-dance-{number}"
            gap = plan["leader_switch_gap_frames"] if number in (3, 5) else 0
            self.call("select-dance", number, folder, gap)
            if gap:
                self.clock.command("wait-gap")
            if number == 1:
                expected_midpoint = plan["midpoint_timing"]["frames"][0]
            else:
                previous = self.rank_plans[-1]
                origin = previous.get("counted_midpoint_anchor") or previous
                anchor = previous["fallback_midpoint_frame"] if previous.get("skip_reason") else origin["frame"] \
                    + math.floor((127 - origin["pointer_x"]) / profile["pixels_per_frame"])
                expected_midpoint = anchor + profile["cycle_frames"] + gap
            self.clock.lua.run("rank_unreadable=false; rank_pointer_override=nil; rank_visual_origin=frame; rank_feedback_due=frame+99999; rank_feedback_events=nil; capture_frames={}; return 'ready'")
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
                if counted_display_hold:
                    counted_anchor = {key: adjustment[key] for key in ("frame", "epoch", "pointer_x")}
                    counted_anchor["source"] = "fresh_paused_midpoint_before_counted_correction"
                if changed_paused_framebuffer:
                    self.clock.lua.run("pixel(240,180,{1,2,3}); return 'ready'")
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
            origin_frame = counted_anchor["frame"] if counted_anchor else midpoint["frame"]
            origin_x = counted_anchor["pointer_x"] if counted_anchor else pointer_x
            endpoint = origin_frame + math.floor((128 - origin_x) / profile["pixels_per_frame"]
                + profile["response_frames"] + 0.5)
            resolved = {"dance": number, "rank": plan["rank"], "frame": midpoint["frame"], "epoch": midpoint["epoch"],
                "pointer_x": pointer_x, "speed": plan["pointer_pixels_per_emulator_frame"],
                "hold_frames": plan["hold_frames"], "offset_frames": plan["offset_frames"],
                "player_center_x": 204 - 80 * ((number - 1) // 2),
                "predicted_end_frame": endpoint,
                "response_end_frame": endpoint,
                "cues": {"right": visible_right, "accepted": accepted}, "pixel_points": [[1, 1, 121, 247, 215]],
                "presses": [{"button": cue["button"], "response_x": cue["x"],
                    "frame": origin_frame + math.floor((cue["x"] - origin_x) / plan["pointer_pixels_per_emulator_frame"] + 0.5)
                        + plan["offset_frames"]}
                    for cue in cues]}
            if counted_anchor:
                resolved["counted_midpoint_anchor"] = counted_anchor
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
                    self.clock.lua.run("rank_unreadable=true; return 'ready'")
                resolved.update(rank_dance.feedback_schedule(resolved,
                    delay_frames=plan["first_feedback_delay_frames"], rank_timing=profile))
            self.rank_plans.append(resolved)
            if number == 1:
                resolved["first_feedback_frame"] = next((press["frame"] + plan["first_feedback_delay_frames"]
                    for index, press in enumerate(resolved["presses"], 1) if press.get("input_index", index) == 1), None)
            submitted = {**resolved, "epoch": resolved["epoch"] + 1} if number in rejected_plans else resolved
            planned = self.call("plan", self.config(submitted))
            recovering = planned.get("recovery_required") is True
            if recovering:
                captured = self.call("capture", "response-recovery")
                skipped = rank_dance.skipped_response({"frame": captured["frame"], "epoch": captured["epoch"],
                    "midpoint_timing": captured["observation"]["midpoint_timing"]}, number, plan, planned["warning"])
                skipped.update(rank_dance.feedback_schedule(skipped,
                    delay_frames=plan["first_feedback_delay_frames"], rank_timing=profile))
                recovered = self.call("recover-response", number, self.config(skipped), planned["warning"])
                resolved = recovered["response_plan"]
                self.rank_plans[-1] = resolved

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
            if recovering:
                if number == 1:
                    checkpoint("rank-dance-offset", midpoint["frame"])
                mirrored = self.clock.command("mirror")
            else:
                mirrored = self.clock.command("mirror")
            if missed is not None:
                self.clock.lua.run("before_frame=saved_before_frame; return 'ready'")
            if missed_window is not None:
                self.clock.lua.run(f"{callback}=saved_feedback_callback; return 'ready'")
            if mirrored.get("error"):
                raise AssertionError(mirrored["error"])
            if number == 1 and not recovering:
                if changed_paused_framebuffer:
                    self.clock.lua.run("pixel(240,181,{4,5,6}); return 'ready'")
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
        faults = set(handoff_faults)
        def handoff_action(action, *args, expected, name):
            if action in faults:
                faults.remove(action)
                self.clock.lua.run("local saved=gui.gdscreenshot; gui.gdscreenshot=function() "
                    "gui.gdscreenshot=saved; error('Handoff capture failed') end; return 'ready'")
            result = self.clock.command(action, *args)
            if result.get("stop_reason") != expected:
                raise dance_response.ReviewNeeded("The handoff capture did not complete.")
            return result

        rank_dance.stage5_handoff(SimpleNamespace(action=handoff_action, command=self.call, record=Mock()), plan)
        self.acknowledge(self.call("visit", "stage-5-dance"))
        return timelines

    def counters(self):
        return json.loads(self.clock.lua.run("return dofile('" + (ROOT / "lua/json.lua").as_posix()
            + "').encode({engines=engine_count,callbacks=registrations,stars=stars_read,moves=move_reads,pages=page_reads,touches=touches})"))


class PersistentLuaChecks(unittest.TestCase):
    def harness(self, rank="Master", *, debug=super_contest.debug):
        harness = ContestClock(rank, debug=debug)
        self.addCleanup(harness.close)
        return harness

    def test_startup_reads_fast_options_with_party_and_reuses_the_same_snapshot(self):
        harness = self.harness(debug=0)
        loading = stage1_load.load_plan(boot_wait_frames=1, hold_frames=1, start_wait_frames=1,
            a_wait_frames=1, b_wait_frames=1)
        harness.begin(1, loading)
        loaded = harness.clock.command("combo", "inputs", *loading["inputs"])
        harness.clock.reset_measurement()
        harness.review()
        snapshot = harness.boundary["snapshot"]
        options = snapshot["options"]
        self.assertTrue(options["read_valid"])
        self.assertEqual((options["text_speed_value"], options["text_speed"]), (2, "Fast"))
        self.assertEqual((options["frame"], snapshot["party"]["frame"], snapshot["image"]["frame"]),
            (loaded["frame"] + 102, loaded["frame"] + 102, loaded["frame"] + 204))
        self.assertEqual(options["direct_read_frames"], 0)
        self.assertTrue(snapshot["accessories"]["read_valid"])
        self.assertEqual(snapshot["accessories"]["total_owned"], 10)
        self.assertEqual(snapshot["accessories"]["frame"], options["frame"])
        self.assertEqual(snapshot["access"]["direct_read_frames"], 0)
        self.assertEqual(harness.clock.trace(), (["X"] * 12 + [""] * 90) * 2)
        harness.clock.reset_measurement()
        harness.begin(2, {})
        initial = harness.call("inspect")["observation"]
        self.assertEqual(initial["options"], options)
        self.assertEqual(initial["accessories"], snapshot["accessories"])
        self.assertEqual(initial["party"], snapshot["party"])
        self.assertEqual(harness.clock.lua.run("return bag_snapshots .. ':' .. options_snapshots .. ':' .. accessory_snapshots"), "1:1:1")
        self.assertEqual(harness.clock.trace(), [])

    def test_host_preparation_accepts_counted_inputs_without_a_post_equip_item_check(self):
        harness = self.harness(debug=0)
        loading = stage1_load.load_plan(boot_wait_frames=1, hold_frames=1, start_wait_frames=1,
            a_wait_frames=1, b_wait_frames=1)
        harness.begin(1, loading)
        harness.clock.command("combo", "inputs", *loading["inputs"])
        harness.review()
        with temporary_directory() as folder:
            runtime = SimpleNamespace(settings=super_contest.chain_plan(debug=0), active_work=Path(folder),
                stage=2, section_visit=2, run_id="integrated-check", stage_deadline=time.monotonic() + 300,
                target=None, bag_context={"opened_once": False, "cursor_position": 1}, json=Mock(),
                start_section=lambda plan: harness.begin(2, plan), command=harness.call,
                lua=lambda name, value: Path(harness.config(value)))

            def action(action, *args, expected, name):
                result = harness.clock.command(action, *args)
                self.assertEqual(result["stop_reason"], expected)
                harness.clock.lua.run("party_unreadable=true; return 'ready'")
                return result

            def acknowledge(pending, *, slot):
                self.assertEqual(slot, 2)
                result = harness.acknowledge(pending)
                runtime.boundary = {"checkpoint": result["checkpoint"], "save_state": None, "receipt": "prep.json"}
                return result

            runtime.action, runtime.acknowledge = action, acknowledge
            result = sections.prep(runtime)
        self.assertEqual(result["outcome"], "pass")
        self.assertEqual(result["planned_item_id"], 260)
        self.assertEqual(result["source_party"]["held_item_id"], 0)
        self.assertNotIn("final_party", result)
        self.assertEqual(runtime.bag_context, {"opened_once": True, "cursor_position": 7})
        self.assertEqual(harness.clock.lua.run("return party_snapshots .. ':' .. bag_snapshots"), "1:1")

    def test_fresh_scarf_order_and_last_copy_cursor_need_no_inventory_prediction_or_guard(self):
        for opened, held, quantity, old_stack, cursor in ((False, 0, 1, False, 1),
                (True, 261, 1, True, 1), (True, 261, 1, False, 2), (True, 0, 2, False, 2)):
            with self.subTest(opened=opened, held=held, quantity=quantity, old_stack=old_stack):
                snapshot = {"party": {"species_id": 151, "held_item_id": held, "next_target": TARGET},
                    "bag": {"items": [{"position": 1, "item_id": held if old_stack else 1, "quantity": 2},
                        {"position": 2, "item_id": 260, "quantity": quantity}]}}
                original = deepcopy(snapshot)
                bag = Stage2BagState(opened)
                plan = stage2_prep.prep_plan(snapshot, bag_state=bag)
                self.assertEqual(plan["bag_context"], {"opened_once": True, "cursor_position": cursor})
                self.assertEqual(tuple(step.split(":")[0] for step in plan["inputs"][:len(bag.opening_buttons())]),
                    bag.opening_buttons())
                self.assertEqual(bag.opened_once, opened)
                self.assertEqual(snapshot, original)
        for held, target, mode in ((260, TARGET, "correct_scarf_held"),
                (0, TARGET, "scarf_unavailable"), (0, None, "all_ribbons_owned")):
            with self.subTest(mode=mode):
                snapshot = {"party": {"species_id": 151, "held_item_id": held, "next_target": target}, "bag": {"items": []}}
                plan = stage2_prep.prep_plan(snapshot, bag_state=Stage2BagState(), cursor=5)
                self.assertEqual((plan["mode"], plan["inputs"]), (mode, []))
                self.assertEqual(plan["bag_context"], {"opened_once": False, "cursor_position": 5})
        harness = self.harness(debug=0)
        harness.clock.lua.run("scarf_quantities[260]=1; return 'ready'")
        harness.prepare_entry()
        before = harness.preparation["snapshot"]
        self.assertEqual(before["party"]["held_item_id"], 0)
        self.assertIn(260, [item["item_id"] for item in before["bag"]["items"]])
        harness.dress(0, finish=True)
        harness.ranked_dance()
        harness.act(win=True, before_result=lambda value: value.clock.lua.run("held_item=264; return 'ready'"))
        fresh = deepcopy(harness.boundary["snapshot"])
        self.assertEqual(fresh["party"]["held_item_id"], 264)
        self.assertNotIn(260, [item["item_id"] for item in fresh["bag"]["items"]])
        self.assertNotIn("item_guard", fresh)
        messages = harness.clock.lua.run("return table.concat(lua_messages, '\\n')")
        self.assertNotIn("item mismatch", messages.lower())
        self.assertNotIn("scarf equip failed", messages.lower())
        initial, plan = harness.prepare()
        self.assertEqual(initial["bag"], fresh["bag"])
        self.assertEqual(plan["target_position"], 7)
        self.assertEqual(plan["mode"], "equip_scarf")
        self.assertEqual(plan["inputs"][:2], ["X:12:90", "A:12:90"])
        self.assertFalse(any(step.startswith(("up:", "down:")) for step in plan["inputs"]))
        self.assertEqual(harness.boundary["snapshot"]["party"]["held_item_id"], 264)
        self.assertEqual(harness.clock.lua.run("return party_snapshots .. ':' .. bag_snapshots"), "2:2")

    def test_unreadable_contest_finish_memory_stays_paused_without_a_fallback_read(self):
        harness = self.harness(debug=0)
        harness.prepare_entry()
        harness.dress(0, finish=True)
        harness.ranked_dance()

        def fail_result_read(value):
            value.clock.lua.run("party_unreadable=true; return 'ready'")
            value.clock.reset_measurement()

        with self.assertRaisesRegex(AssertionError, "failed stored-data validation"):
            harness.act(before_result=fail_result_read)
        frame = int(harness.clock.lua.run("return tostring(frame)"))
        self.assertEqual(harness.clock.trace(), [])
        self.assertEqual(harness.clock.lua.run("return tostring(mock_paused)"), "true")
        stopped = harness.call("abort", "Unreadable contest-finish record")
        self.assertEqual(stopped["frame"], frame)
        self.assertEqual(stopped["checkpoint"]["snapshot"]["image"]["frame"], frame)
        self.assertEqual(harness.clock.lua.run("return party_snapshots .. ':' .. bag_snapshots"), "2:2")

    def test_startup_warnings_print_once_and_continue_from_the_accepted_snapshot(self):
        cases = (("low preparation", "party_sheen=254; memory_values[0x02045808]=9; scarf_quantities[264]=nil", 3),
            ("fully prepared", "party_sheen=255", 0),
            ("missing all scarves", "party_sheen=255; held_item=0; scarf_quantities={}", 1),
            ("unreadable accessories", "party_sheen=255; memory_values[0x02060108]=0", 1))
        for label, change, expected in cases:
            with self.subTest(preparation=label), temporary_directory() as folder:
                harness = self.harness(debug=0)
                harness.clock.lua.run("held_item=260; scarf_quantities[260]=nil; " + change + "; return 'ready'")
                loading = stage1_load.load_plan(boot_wait_frames=1, hold_frames=1, start_wait_frames=1,
                    a_wait_frames=1, b_wait_frames=1)
                harness.begin(1, loading)
                harness.clock.command("combo", "inputs", *loading["inputs"])
                harness.review()
                snapshot = deepcopy(harness.boundary["snapshot"])
                before = harness.clock.trace()
                runtime = SimpleNamespace(settings=super_contest.chain_plan(debug=0), work=Path(folder),
                    active_work=Path(folder), stage=1, section_visit=1, run_id="integrated-check",
                    boundary={"checkpoint": harness.boundary, "save_state": None, "receipt": "startup.json"},
                    controller=SimpleNamespace(log_lua=Mock()), record=Mock())
                with patch("contest.sections.reviewed_inputs", return_value={"screenshot": "stage1.png"}), \
                        patch("contest.stage6_act.overworld_reference_from_checkpoint", return_value={"points": []}):
                    result = sections.load(runtime)
                warnings = result["readiness_warnings"]
                self.assertEqual(len(warnings), expected)
                self.assertEqual([call.args[0] for call in runtime.controller.log_lua.call_args_list], warnings)
                self.assertEqual(result["outcome"], "pass")
                self.assertTrue(result["emulator_paused"] and result["inputs_released"])
                self.assertEqual(harness.clock.trace(), before)
                self.assertEqual(harness.clock.lua.run("return tostring(frame)"), str(harness.boundary["frame"]))
                if label == "low preparation":
                    self.assertIn("Only 9", warnings[0])
                    self.assertIn("254/255", warnings[1])
                    self.assertIn("Yellow Scarf", warnings[2])
                    self.assertNotIn("Red Scarf", warnings[2])
                elif label == "unreadable accessories":
                    self.assertFalse(snapshot["accessories"]["read_valid"])
                    self.assertTrue(snapshot["accessories"]["error"])
                    self.assertIn("could not be read reliably", warnings[0])
                elif label == "missing all scarves":
                    for name in stage2_prep.SCARVES.values():
                        self.assertIn(name, warnings[0])
                initial, prep = harness.prepare(opened_once=False, cursor=1)
                self.assertEqual(initial["accessories"], snapshot["accessories"])
                self.assertEqual(initial["party"], snapshot["party"])
                self.assertEqual(prep["mode"], "scarf_unavailable" if label == "missing all scarves" else "correct_scarf_held")
                self.assertEqual(prep["target"], snapshot["party"]["next_target"])
                self.assertTrue(harness.boundary["proceed"])
                self.assertEqual(runtime.controller.log_lua.call_count, expected)
                self.assertEqual(harness.clock.lua.run("return options_snapshots .. ':' .. accessory_snapshots"), "1:1")
                recorded = [call.kwargs["message"] for call in runtime.record.call_args_list
                    if call.args[0] == "readiness_warning"]
                self.assertEqual(recorded, warnings)

    def test_startup_stops_non_fast_or_unreadable_options_at_the_paused_boundary(self):
        cases = (
            ("Slow", "memory_values[0x02040078]=0x3400", "fast_text_speed"),
            ("Normal", "memory_values[0x02040078]=0x3401", "fast_text_speed"),
            ("undefined value", "memory_values[0x02040078]=0x340F", "readable_text_speed"),
            ("wrong page", "memory_values[0x02060034]=3", "readable_text_speed"),
            ("missing pointer", "options_save=0", "readable_text_speed"),
        )
        for label, change, failed_id in cases:
            with self.subTest(setting=label):
                harness = self.harness(debug=0)
                harness.clock.lua.run(change + "; return 'ready'")
                loading = stage1_load.load_plan(boot_wait_frames=1, hold_frames=1, start_wait_frames=1,
                    a_wait_frames=1, b_wait_frames=1)
                harness.begin(1, loading)
                loaded = harness.clock.command("combo", "inputs", *loading["inputs"])
                harness.read_memory()
                harness.clock.reset_measurement()
                capture = harness.call("capture")
                pending = harness.call("visit", "pass", "local_reference_pixels", f"{harness.work}/review.png",
                    "Native fixture accepted.", capture["frame"], capture["epoch"])
                record = pending["checkpoint"]
                self.assertEqual(record["decision"], "abort")
                self.assertEqual([check["id"] for check in record["checks"] if not check["passed"]], [failed_id])
                snapshot = record["snapshot"]
                self.assertEqual((snapshot["options"]["frame"], snapshot["party"]["frame"], snapshot["image"]["frame"]),
                    (loaded["frame"] + 102, loaded["frame"] + 102, loaded["frame"] + 204))
                self.assertEqual(snapshot["party"]["current_hp"], 100)
                self.assertEqual(snapshot["party"]["move_count"], 4)
                if failed_id == "fast_text_speed":
                    self.assertEqual(snapshot["options"]["text_speed"], label)
                    self.assertIn("Set Options > Text Speed to Fast", abort_message(CheckpointStopped(record)))
                else:
                    self.assertFalse(snapshot["options"]["read_valid"])
                    self.assertTrue(snapshot["options"]["error"])
                    self.assertIn("could not be read reliably", abort_message(CheckpointStopped(record)))
                completed = harness.call("complete", record["run_id"], record["visit"], record["frame"], record["epoch"],
                    f"{harness.work}/checkpoints/options-stop.json", "true", "false")
                self.assertFalse(completed["checkpoint"]["proceed"])
                self.assertEqual(harness.clock.lua.run("return bag_snapshots .. ':' .. options_snapshots"), "1:1")
                self.assertEqual(harness.clock.lua.run("return tostring(mock_paused)"), "true")
                self.assertEqual(harness.clock.trace(), [])

    def test_startup_result_reads_reentry_and_debug_save_ownership_share_one_runtime(self):
        for debug in (0, 1):
            with self.subTest(debug=debug), temporary_directory() as folder:
                work, saved_slots = Path(folder), []
                (work / "StateSlots").mkdir()
                (work / "checkpoints").mkdir()
                harness = self.harness(debug=debug)

                def status():
                    return {**json.loads(harness.clock.lua.run("return latest_status")), "emulator_paused": True}

                controller = SimpleNamespace(ROM=work / "prepared.nds", EMULATOR=work, remaining=lambda: 300,
                    status=status, paused=lambda _: True, session=lambda: {"pid": 7, "window": 11})
                controller.checkpoint_command = lambda action, args, timeout=None: {
                    **harness.call(action, *args), "emulator_paused": True}
                runtime = ContestRuntime("integrated-check", work, super_contest.chain_plan(debug=debug),
                    time.monotonic(), controller=controller)
                slots = {"stage-2-prep": 2, "stage-4-dress": 4, "stage-5-dance": 5, "stage-6-act": 6}

                def acknowledge(pending):
                    result = runtime.acknowledge({**pending, "emulator_paused": True},
                        slot=slots.get(pending["checkpoint"]["checkpoint"], 9))
                    harness.boundary = result["checkpoint"]
                    return result

                def native_save(states, slot, *, expected=None, timeout=20):
                    path = states.path(slot)
                    path.write_bytes(b"DeSmuME SState " + str(expected["frame"]).encode("ascii"))
                    saved_slots.append(slot)
                    return {"slot": slot, "path": str(path), "emulator_paused": True}

                harness.acknowledge = acknowledge
                with patch.object(StateSlots, "save", autospec=True, side_effect=native_save) as native:
                    harness.prepare_entry()
                    startup_party = deepcopy(harness.preparation["snapshot"]["party"])
                    self.assertEqual(harness.clock.lua.run(
                        "return party_snapshots .. ':' .. bag_snapshots .. ':' .. options_snapshots .. ':' .. accessory_snapshots"),
                        "1:1:1:1")
                    self.assertEqual(saved_slots, [2] if debug else [])
                    for win in (False, True):
                        if win:
                            previous = deepcopy(harness.boundary)
                            harness.clock.lua.run("party_unreadable=true; return 'ready'")
                            initial, prep = harness.prepare()
                            self.assertEqual(initial["frame"], previous["frame"])
                            self.assertEqual(initial["party"], previous["snapshot"]["party"])
                            self.assertEqual(initial["bag"], previous["snapshot"]["bag"])
                            self.assertEqual(prep["inputs"], [])
                            self.assertEqual(prep["bag_context"], {"opened_once": True, "cursor_position": 7})
                            self.assertFalse(harness.boundary["snapshot"]["access"]["live_memory_read"])
                            harness.enter()
                            self.assertGreater(harness.boundary["visit"], previous["visit"])
                        expected_reads = 1 + int(win)
                        harness.clock.lua.run("party_unreadable=true; return 'ready'")
                        harness.dress(0, finish=True)
                        harness.ranked_dance()
                        self.assertEqual(saved_slots[-2:], [4, 5] if debug else [])

                        def allow_result_read(value):
                            self.assertEqual(value.clock.lua.run("return party_snapshots .. ':' .. bag_snapshots"),
                                f"{expected_reads}:{expected_reads}")
                            self.assertEqual(value.clock.trace()[-102:], ["X"] * 12 + [""] * 90)
                            value.clock.lua.run("party_unreadable=false; return 'ready'")

                        harness.act(win=win, before_result=allow_result_read)
                        snapshot = harness.boundary["snapshot"]
                        self.assertEqual(harness.boundary["decision"], "hit" if win else "miss")
                        self.assertEqual(harness.boundary["branch"], "success" if win else "retry-contest")
                        self.assertEqual(harness.boundary["epoch"], 0)
                        self.assertEqual(snapshot["trial"]["completed_rounds"], 4)
                        self.assertEqual(harness.boundary["save_required"], bool(debug))
                        self.assertEqual(runtime.boundary["save_state"]["slot"] if debug else runtime.boundary["save_state"],
                            6 if debug else None)
                        self.assertEqual(harness.clock.lua.run(
                            "return party_snapshots .. ':' .. bag_snapshots .. ':' .. options_snapshots .. ':' .. accessory_snapshots"),
                            f"{expected_reads + 1}:{expected_reads + 1}:1:1")
                        read = snapshot["memory_read"]
                        self.assertEqual((read["source"], read["frame"], read["epoch"], read["direct_read_frames"]),
                            ("x_menu_paused_memory", snapshot["party"]["frame"], harness.boundary["epoch"], 0))
                        self.assertEqual(harness.boundary["frame"] - read["frame"], 102)
                        self.assertEqual(snapshot["party"]["static_read_frame"], startup_party["frame"])
                        for field in ("moves", "move_count", "unavailable_move_slots", "current_hp", "max_hp", "sheen"):
                            self.assertEqual(snapshot["party"][field], startup_party[field])
                        self.assertEqual(harness.clock.trace()[-102:], ["X"] * 12 + [""] * 90)
                    self.assertEqual(saved_slots, [2, 4, 5, 6] * 2 if debug else [])
                    self.assertEqual(native.call_count, len(saved_slots))
                self.assertEqual({path.suffix for path in (work / "StateSlots").iterdir()},
                    {".ds2", ".ds4", ".ds5", ".ds6"} if debug else set())
                counts = harness.counters()
                self.assertEqual(counts["engines"], 1)
                self.assertEqual(counts["callbacks"], {"registerbefore": 1, "registerafter": 1, "registermenustart": 1})
                rejected = harness.clock.checkpoint("state-changed", "load", harness.boundary["visit"])
                self.assertIn("Quick-loads are disabled", rejected["error"])
                self.assertEqual((rejected["frame"], rejected["epoch"]), (harness.boundary["frame"], 0))

    def test_acting_handoff_selects_native_slot_5_and_allows_a_state_free_source(self):
        for debug in (0, 1):
            with self.subTest(debug=debug), temporary_directory() as folder:
                work = Path(folder)
                saved = {"retained_path": str(work / "accepted.ds5"), "slot": 5} if debug else None
                checkpoint = {"checkpoint": "stage-5-dance", "frame": 500, "epoch": 0, "snapshot": {}}
                source = {"checkpoint": "stage-4-dress", "frame": 100, "epoch": 0,
                    "snapshot": {"screen": {"pointer": {"status": "exact", "position_bounds": [64, 64]}}}}
                runtime = SimpleNamespace(settings=super_contest.chain_plan(debug=debug), target=TARGET,
                    active_work=work, stage=5, run_id="handoff-check", section_visit=1,
                    boundary={"checkpoint": source, "receipt": str(work / "source.json"), "save_state": None},
                    start_section=Mock(), command=Mock(return_value={"checkpoint": checkpoint}),
                    acknowledge=Mock(), action=Mock(return_value={}), json=Mock())
                runtime.acknowledge.side_effect = lambda *_, **__: setattr(runtime, "boundary",
                    {"checkpoint": checkpoint, "receipt": str(work / "accepted.json"), "save_state": saved})
                last = {"checkpoint": {"snapshot": {"trial": {"total_input_count": 24, "total_excellent_count": 1,
                    "total_skipped_inputs": 0, "total_miss_count": 0}, "offset_check": {}}}}
                with patch("contest.rank_dance.stage5_responses", return_value=([], last)):
                    result = sections.dance(runtime)
                runtime.acknowledge.assert_called_once_with({"checkpoint": checkpoint}, slot=5)
                self.assertEqual(runtime.start_section.call_args.args[0]["source_frame"], 100)
                self.assertEqual(result["checkpoint"], checkpoint)
                runtime.json.assert_not_called()
                self.assertEqual(result["save_state"], saved)

    def test_main_entry_guard_preserves_fast_path_retries_and_retains_timeout_evidence(self):
        self.assertIs(sections.RUNNERS[3], sections.enter)
        self.assertEqual(super_contest.chain_plan()["sections"][2]["callable"], "contest.sections.enter")
        for detections in ((True,), (False, True), (False,)):
            with self.subTest(detections=detections), temporary_directory() as folder:
                folder = Path(folder)
                harness = self.harness()
                loading = stage1_load.load_plan(boot_wait_frames=1, hold_frames=1, start_wait_frames=1,
                    a_wait_frames=1, b_wait_frames=1)
                harness.begin(1, loading)
                harness.clock.command("combo", "inputs", *loading["inputs"])
                harness.review()
                harness.prepare(opened_once=False, cursor=1)
                source = deepcopy(harness.boundary)
                state = {"checks": 0, "host_seconds": 0, "waits": 0}

                def remaining():
                    if state["host_seconds"] >= 2:
                        raise TimeoutError("timeout")
                    return 2 - state["host_seconds"]

                controller = SimpleNamespace(remaining=remaining, live_deadline=2,
                    screenshot=lambda path: Image.new("RGB", (256, 384)).save(path))
                runtime = SimpleNamespace(active_work=folder, settings=super_contest.chain_plan(), stage=3,
                    run_id="integrated-check", target=source["snapshot"]["target"], section_visit=1,
                    boundary={"checkpoint": source, "save_state": None, "receipt": str(folder / "source.json")},
                    json=lambda name, value: write_json(folder / name, value), record=Mock(), check=remaining,
                    controller=controller)
                runtime.review = lambda capture, expected, filename: ContestRuntime.review(runtime, capture, expected, filename=filename)

                def begin(plan):
                    config, handoff = harness.begin(3, plan, work=folder.as_posix())
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

                def command(action, *arguments):
                    if action == "capture":
                        matched = detections[min(state["checks"], len(detections) - 1)]
                        harness.clock.lua.run(f"offline_screen_confirmed={literal(matched)}; return 'ready'")
                        state["checks"] += 1
                    result = harness.call(action, *arguments)
                    image = (result.get("observation") or {}).get("image")
                    if image:
                        Path(image["path"]).write_bytes(harness.file_bytes(image["path"]))
                    return result

                def wait(action, name, *steps, timeout, deadline):
                    self.assertEqual((action, name, timeout, deadline), ("combo", "inputs", None, 2))
                    self.assertEqual(steps, (f"wait:{round(3 * stage3_enter.FRAME_RATE)}",))
                    result = harness.clock.command(action, name, *steps)
                    state["host_seconds"] += 1
                    state["waits"] += 1
                    return result

                runtime.start_section, runtime.action = begin, action
                runtime.command, runtime.acknowledge = command, acknowledge
                runtime.emulator = SimpleNamespace(controller=controller, command=wait)
                harness.clock.reset_measurement()
                baseline = stage3_enter.entry_plan("Cool", "Master", host_timeout=300)["total_emulator_frames"]
                if detections[-1]:
                    result = sections.enter(runtime)
                    self.assertTrue(result["stage3_accepted"])
                    self.assertIsNone(result["save_state"])
                    self.assertEqual(result["screen_evidence"]["source"], "local_reference_pixels")
                    expected_waits = len(detections) - 1
                    self.assertEqual(state["waits"], expected_waits)
                    self.assertEqual(result["input_frames"], baseline + expected_waits * round(3 * stage3_enter.FRAME_RATE))
                    self.assertEqual(len(result["entry_checks"]), len(detections))
                    self.assertEqual(harness.boundary["checkpoint"], "stage-3-enter")
                    self.assertTrue(harness.boundary["snapshot"]["screen"]["fresh_visual_binding"])
                    dressing = stage4_dress.dress_plan(host_timeout=300)
                    _, handoff = harness.begin(4, dressing, initial_page_taps=0)
                    self.assertEqual((handoff["frame"], handoff["handoff"]["emulator_frames"]), (result["frame"], 0))
                else:
                    with self.assertRaises(TimeoutError):
                        sections.enter(runtime)
                    self.assertEqual(harness.boundary["checkpoint"], "stage-2-prep")
                    self.assertEqual((state["checks"], state["waits"], controller.live_deadline), (2, 2, 2))
                    self.assertFalse((folder / "review-request.json").exists())
                checks = json.loads((folder / "dress-entry-checks.json").read_text(encoding="utf-8"))["checks"]
                self.assertEqual(len(checks), state["checks"])
                for checked in checks:
                    path = Path(checked["image"]["path"])
                    self.assertEqual(path.read_bytes(), harness.file_bytes(path.as_posix()))
                    self.assertTrue(path.with_suffix(".png").is_file())
                self.assertEqual(harness.clock.trace()[baseline:], [""] * state["waits"] * round(3 * stage3_enter.FRAME_RATE))
                self.assertEqual(harness.clock.lua.run("return tostring(mock_paused)"), "true")

    def test_dress_entry_mismatch_cannot_skip_the_wait_or_replay_buttons(self):
        for attempt in ("A:1:1", "B:10:20", "capture", "visit"):
            with self.subTest(attempt=attempt):
                harness = self.harness()
                loading = stage1_load.load_plan(boot_wait_frames=1, hold_frames=1, start_wait_frames=1,
                    a_wait_frames=1, b_wait_frames=1)
                harness.begin(1, loading)
                harness.clock.command("combo", "inputs", *loading["inputs"])
                harness.review()
                harness.prepare(opened_once=False, cursor=1)
                plan = stage3_enter.entry_plan("Cool", "Master", host_timeout=300,
                    hold_frames=1, release_frames=1, intro_seconds=1 / stage3_enter.FRAME_RATE)
                harness.begin(3, plan)
                harness.clock.command("combo", "inputs", *plan["inputs"])
                harness.clock.lua.run("offline_screen_confirmed=false; return 'ready'")
                checked = harness.call("capture")
                self.assertFalse(checked["observation"]["automatic_check"]["confirmed"])
                before = checked["frame"]
                harness.clock.reset_measurement()
                if attempt in ("capture", "visit"):
                    rejected = harness.clock.checkpoint(attempt)
                else:
                    rejected = harness.clock.command("combo", "inputs", attempt)
                self.assertIn("error", rejected)
                self.assertEqual(rejected["frame"], before)
                self.assertEqual(harness.clock.trace(), [])
                self.assertEqual(harness.clock.lua.run("return tostring(mock_paused)"), "true")

    def test_held_midpoint_executes_counted_inputs_and_preserves_the_next_dance_anchor(self):
        harness = self.harness(rank="Hyper", debug=0)
        harness.target.update(category="Beauty", ribbon_bit=6, required_scarf_id=261, category_down_presses=1)
        harness.clock.lua.run(f"target={literal(harness.target)}; return 'ready'")
        harness.prepare_entry()
        harness.dress(0, finish=True)
        harness.ranked_dance(early_midpoint_x=127, post_fix_pointer_x=127, counted_display_hold=True)
        trial = harness.boundary["snapshot"]["trial"]
        self.assertEqual((trial["total_input_count"], trial["total_skipped_inputs"]), (24, 0))
        first = harness.rank_plans[0]
        anchor = first["counted_midpoint_anchor"]
        self.assertEqual((first["frame"] - anchor["frame"], first["pointer_x"]), (3, 127))
        self.assertEqual(first["presses"][0]["frame"], anchor["frame"] + 26)
        self.assertEqual(first["response_end_frame"], anchor["frame"] + 363)
        self.assertEqual(harness.rank_midpoints[1]["frame"], anchor["frame"] + 720)
        self.assertEqual(harness.rank_midpoints[1]["source"], "calculated_from_counted_midpoint_and_fixed_metrics")
        checked = harness.rank_cue_trials[0]
        self.assertFalse(checked["midpoint_within_line"])
        self.assertTrue(checked["counted_midpoint_anchor_bound"] and checked["midpoint_policy_met"])
        held = first["presses"][0]["frame"] - first["frame"] - 1
        self.assertEqual(harness.rank_captures[0]["buttons"][held:held + 12], ["A"] * 2 + [""] * 10)

    def test_main_run_uses_fixed_metrics_reanchors_and_hands_off_without_routine_dance_saves(self):
        harness = self.harness(debug=0)
        harness.prepare_entry()
        source_frame = harness.boundary["frame"]
        _, handoff, _ = harness.dress(0, finish=True)
        self.assertEqual((handoff["frame"], handoff["handoff"]["emulator_frames"]), (source_frame, 0))
        entry = harness.boundary["frame"]
        timelines = harness.ranked_dance(early_midpoint_x=126, bad_feedback=(1, 2, "Good"), changed_paused_framebuffer=True)
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
        self.assertEqual(harness.saved_checkpoints, [])
        screens = {"stage-1-load", "stage-3-enter", "stage-4-dress-page", "stage-4-dress",
            "rank-dance-start", "rank-dance-offset", "stage-5-dance", "stage-6-round", "stage-6-act", "stage-2-prep"}
        self.assertTrue(screens.issubset({record["checkpoint"] for record in harness.log_checkpoints}))
        offset = next(record for record in harness.log_checkpoints if record["checkpoint"] == "rank-dance-offset")
        self.assertEqual(offset["decision"], "hit")
        self.assertTrue(offset["snapshot"]["trial"]["first_feedback_bound"])
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
        self.assertEqual(harness.boundary["snapshot"]["target"]["ribbon_bit"], 4)
        self.assertEqual(harness.boundary["epoch"], 0)
        counts = harness.counters()
        self.assertEqual((counts["engines"], counts["stars"]), (1, 8))
        self.assertEqual(counts["callbacks"], {"registerbefore": 1, "registerafter": 1, "registermenustart": 1})
        rejected = harness.clock.checkpoint("state-changed", "load", harness.boundary["visit"])
        self.assertIn("Quick-loads are disabled", rejected["error"])
        self.assertEqual(rejected["epoch"], 0)

    def test_acting_scoring_is_cached_for_four_rounds_and_refreshed_on_contest_reentry(self):
        harness = self.harness()
        harness.prepare_entry()
        harness.dress(0, finish=True)
        harness.ranked_dance()
        categories = ["Beauty", "Tough", "Smart", "Cool"]
        hearts = [5, 2, 4, 3]
        plans = harness.act(move_categories=categories, move_hearts=hearts, acting_positions=[1, 2, 3, 4],
            star_counts=[[0, 0, 0], [0, 4, 4], [0, 1, 2], [0, 0, 0]],
            after_entry=lambda value: value.clock.lua.run("paint_acting_moves({'Cute','Cute','Cute','Cute'}); return 'ready'"))
        self.assertEqual([plan["move"] for plan in plans], [1, 4, 1, 3])
        self.assertEqual([plan["move_hearts"] for plan in plans], [5, 3, 5, 4])
        self.assertEqual([plan["acting_position"] for plan in plans], [1, 2, 3, 4])
        self.assertEqual([plan["previous_move"] for plan in plans], [1, 1, 4, 1])
        self.assertEqual([plan["matching_category_override"] for plan in plans], [False, True, False, False])
        self.assertEqual(plans[1]["menu_buttons"], ["right", "down", "A", "right", "A"])
        self.assertEqual(plans[2]["menu_buttons"], ["left", "up", "A", "left", "A"])
        first = harness.act_snapshots[0]
        for snapshot in harness.act_snapshots:
            moves = snapshot["move_categories"]
            self.assertEqual(moves["categories"], categories)
            self.assertEqual(moves["hearts"], hearts)
            self.assertEqual((moves["frame"], moves["epoch"], moves["path"]),
                (first["image"]["frame"], first["image"]["epoch"], first["image"]["path"]))
            self.assertEqual((moves["matched_pixels"], moves["total_pixels"], moves["direct_read_frames"]), (24, 24, 0))
            order = snapshot["acting_order"]
            self.assertEqual((order["frame"], order["epoch"], order["path"]),
                (snapshot["image"]["frame"], snapshot["image"]["epoch"], snapshot["image"]["path"]))
            self.assertEqual(order["direct_read_frames"], 0)
        self.assertEqual((harness.counters()["stars"], harness.counters()["moves"]), (8, 1))
        original = first["move_categories"]
        harness.prepare()
        harness.enter()
        harness.dress(0, finish=True)
        harness.ranked_dance()
        plans = harness.act(rounds=2, move_categories=["Beauty", "Tough", "Cool", "Cool"])
        fresh = harness.act_snapshots[0]["move_categories"]
        self.assertEqual([plan["move"] for plan in plans], [1, 3])
        self.assertEqual((fresh["epoch"], original["epoch"]), (0, 0))
        self.assertGreater(fresh["frame"], original["frame"])
        self.assertNotEqual(fresh["path"], original["path"])
        self.assertEqual(fresh["frame"], harness.act_snapshots[0]["image"]["frame"])
        self.assertEqual(harness.act_snapshots[1]["move_categories"], fresh)
        self.assertEqual(harness.counters()["moves"], 2)

    def test_unreadable_entry_fill_or_hearts_warn_once_and_reuse_recognized_moves(self):
        for field, change in (("fill", "pixel(18,220,{132,132,132})"),
                ("hearts", "pixel(62,242,{255,41,0})")):
            with self.subTest(field=field):
                harness = self.harness()
                harness.prepare_entry()
                harness.dress(0, finish=True)
                harness.ranked_dance()
                captured = {}

                def obstruct(value):
                    value.clock.lua.run(change + "; return 'ready'")
                    captured["frame"] = int(value.clock.lua.run("return tostring(frame)"))

                plans = harness.act(rounds=2, move_hearts=[6, 1, 4, 3], entry_change=obstruct)
                self.assertEqual([plan["move"] for plan in plans], [3, 4])
                self.assertEqual((harness.counters()["stars"], harness.counters()["moves"]), (4, 1))
                snapshot = harness.act_snapshots[0]
                moves = snapshot["move_categories"]
                self.assertFalse(moves["readable"])
                self.assertEqual(moves["unrecognized_move_slots"], [1])
                self.assertEqual(harness.act_snapshots[1]["move_categories"], moves)
                self.assertEqual(snapshot["image"]["frame"], captured["frame"])
                self.assertEqual(snapshot["trial"]["completed_rounds"], 0)
                self.assertTrue(all(plan["selectable"] and not plan["unrecognized_fallback"] for plan in plans))
                messages = json.loads(harness.clock.lua.run(
                    f"return dofile({literal((ROOT / 'lua/json.lua').as_posix())}).encode(lua_messages)"))
                warnings = [message for message in messages if "Acting: WARN - Move slot " in message]
                self.assertEqual(warnings, moves["warnings"])
                self.assertEqual(len(warnings), 1)
                if field == "fill":
                    self.assertEqual(moves["categories"][0], "unreadable")
                else:
                    self.assertTrue(moves["categories_readable"])
                    self.assertFalse(moves["hearts_readable"])
                    self.assertEqual(moves["hearts"][0], -1)
                    self.assertIn("unrecognized raw hearts", warnings[0])
                self.assertEqual(harness.clock.lua.run("return tostring(mock_paused)"), "true")

    def test_no_recognized_moves_warn_and_fall_back_without_repeating_the_previous_move(self):
        harness = self.harness()
        harness.prepare_entry()
        harness.dress(0, finish=True)
        harness.ranked_dance()

        def obstruct(value):
            points = [probes[0] for probes in MOVE_PROBES]
            value.clock.lua.run(f"for _, point in ipairs({literal(points)}) do "
                "pixel(point[1],point[2],{132,132,132}) end; return 'ready'")

        plans = harness.act(entry_change=obstruct, acting_positions=[1, 2, 3, 4],
            star_counts=[[0, 0, 0], [2, 4, 1], [1, 0, 4], [4, 0, 1]])
        self.assertEqual([plan["move"] for plan in plans], [1, 2, 1, 2])
        self.assertEqual([plan["judge"] for plan in plans], [1, 3, 2, 2])
        self.assertTrue(all(plan["selectable"] and plan["unrecognized_fallback"] for plan in plans))
        self.assertTrue(all(not plan["matching_category_override"] for plan in plans))
        self.assertEqual(harness.act_snapshots[0]["move_categories"]["unrecognized_move_slots"], [1, 2, 3, 4])
        messages = json.loads(harness.clock.lua.run(f"return dofile({literal((ROOT / 'lua/json.lua').as_posix())}).encode(lua_messages)"))
        self.assertEqual(len([message for message in messages if "Acting: WARN - Move slot " in message]), 4)
        self.assertEqual(len([message for message in messages if "No recognized legal move remains" in message]), 4)
        self.assertEqual(harness.clock.lua.run("return tostring(mock_paused)"), "true")

    def test_entry_mismatch_retains_the_actual_paused_capture_before_retry(self):
        harness = self.harness()
        harness.prepare_entry()
        harness.dress(0, finish=True)
        harness.ranked_dance()
        with self.assertRaises(AssertionError):
            harness.act(rounds=1, entry_change=lambda value: value.clock.lua.run("acting_judge_unreadable=true; return 'ready'"))
        snapshot = harness.act_snapshots[0]
        self.assertFalse(snapshot["stars"]["readable"])
        self.assertNotIn("plan", snapshot)
        self.assertEqual(snapshot["trial"]["completed_rounds"], 0)
        self.assertEqual(int(harness.clock.lua.run("return tostring(frame)")), snapshot["image"]["frame"])
        self.assertEqual(harness.clock.lua.run("return tostring(mock_paused)"), "true")
        self.assertEqual(harness.act_readiness[0]["pause_plan"]["phase"], "retry")
        self.assertEqual(harness.boundary["checkpoint"], "stage-5-dance")
        self.assertEqual(harness.counters()["moves"], 0)

    def test_entry_retries_empty_stars_menu_and_order_before_one_cached_paused_plan(self):
        changes = {
            "stars": ("acting_star_counts={{1,0,0}}", "acting_star_counts=nil"),
            "menu": ("pixel(18,244,{132,132,132})", "paint_acting_moves({'Beauty','Tough','Smart','Cute'})"),
            "order": ("pixel(4,42,{132,132,132})", "paint_acting_order(1)"),
        }
        for debug in (0, 1):
            with self.subTest(debug=debug):
                harness = self.harness(debug=debug)
                harness.prepare_entry()
                harness.dress(0, finish=True)
                harness.ranked_dance()
                harness.act(rounds=0)
                saved, logged = len(harness.saved_checkpoints), len(harness.log_checkpoints)
                messages = harness.clock.lua.run("return table.concat(lua_messages, '\\n')")
                harness.clock.reset_measurement()
                for reason, (obstruct, restore) in changes.items():
                    with self.subTest(reason=reason):
                        harness.clock.lua.run(obstruct + "; return 'ready'")
                        before = int(harness.clock.lua.run("return tostring(frame)"))
                        checked = harness.call("round-readiness", 1)
                        sample = checked["observation"]
                        self.assertFalse(sample["readiness"]["ready"])
                        self.assertTrue(sample["readiness"]["entry"])
                        self.assertEqual(sample["image"]["frame"], before)
                        self.assertEqual(sample["pause_plan"]["frames"], round(3 * stage6_act.FRAME_RATE))
                        self.assertEqual((len(harness.saved_checkpoints), len(harness.log_checkpoints)), (saved, logged))
                        self.assertEqual(harness.counters()["moves"], 0)
                        self.assertEqual(harness.clock.lua.run("return party_snapshots .. ':' .. bag_snapshots"), "1:1")
                        self.assertEqual(harness.clock.lua.run("return table.concat(lua_messages, '\\n')"), messages)
                        if reason == "stars":
                            self.assertTrue(sample["readiness"]["stars"]["readable"])
                            self.assertFalse(sample["readiness"]["stars"]["all_empty"])
                        if reason == "menu":
                            self.assertFalse(sample["readiness"]["move_buttons"]["slots"][0]["unavailable"])
                        progressed = harness.clock.command("combo", "inputs", *sample["pause_plan"]["steps"])
                        self.assertEqual(progressed["frame"], sample["pause_plan"]["end_frame"])
                        self.assertEqual(set(harness.clock.trace()), {"", "B"})
                        harness.clock.lua.run(restore + "; return 'ready'")
                ready = harness.call("round-readiness", 1)["observation"]
                self.assertTrue(ready["readiness"]["ready"])
                harness.call("round-ready", 1)
                pending = harness.call("round-visit", 1)
                snapshot = pending["checkpoint"]["snapshot"]
                chosen = harness.acknowledge(pending)["checkpoint"]["snapshot"]["plan"]
                self.assertEqual((snapshot["image"]["frame"], chosen["start_frame"]),
                    (ready["image"]["frame"], ready["image"]["frame"]))
                self.assertEqual(snapshot["trial"]["render_preparation_frames"], 10)
                self.assertEqual(harness.counters()["moves"], 1)
                self.assertNotIn("excluded_previous_move", chosen)
                self.assertEqual((chosen["move"], chosen["judge"]), (1, 1))
                self.assertEqual(harness.clock.lua.run("return tostring(mock_paused)"), "true")
                harness.clock.reset_measurement()
                harness.clock.command("combo", "inputs", *chosen["steps"])
                self.assertEqual(harness.clock.pauses(), 1)
                self.assertEqual(harness.clock.trace()[chosen["pre_selection_frames"]], chosen["menu_buttons"][0])

    def test_later_unreadable_order_blocks_new_move_inputs_without_halting_the_checkpoint_engine(self):
        harness = self.harness()
        harness.prepare_entry()
        harness.dress(0, finish=True)
        harness.ranked_dance()

        def obstruct(value, number):
            if number == 2:
                value.clock.lua.run("pixel(4,90,{132,132,132}); return 'ready'")

        with self.assertRaises(AssertionError):
            harness.act(rounds=2, acting_positions=[1, 2, 3, 4], before_round=obstruct)
        snapshot = harness.act_snapshots[1]
        self.assertFalse(snapshot["acting_order"]["readable"])
        self.assertNotIn("plan", snapshot)
        self.assertEqual(snapshot["trial"]["completed_rounds"], 1)
        self.assertEqual(int(harness.clock.lua.run("return tostring(frame)")), snapshot["image"]["frame"])
        self.assertEqual(harness.clock.lua.run("return tostring(mock_paused)"), "true")
        self.assertEqual(harness.act_readiness[1]["pause_plan"]["phase"], "retry")
        self.assertEqual(harness.boundary["snapshot"]["trial"]["round"], 1)

    def test_pre_round_retries_and_paused_match_are_silent_in_both_debug_modes(self):
        for debug in (0, 1):
            with self.subTest(debug=debug):
                harness = self.harness(debug=debug)
                harness.prepare_entry()
                harness.dress(0, finish=True)
                harness.ranked_dance()
                harness.act(rounds=1)
                before_visit = harness.boundary["visit"]
                before_messages = harness.clock.lua.run("return table.concat(lua_messages, '\\n')")
                harness.clock.reset_measurement()
                harness.clock.lua.run("acting_judge_unreadable=true; pixel(146,244,{132,132,132}); return 'ready'")
                for reason in ("judges", "moves"):
                    before = int(harness.clock.lua.run("return tostring(frame)"))
                    response = harness.call("round-readiness", 2)
                    snapshot = response["observation"]
                    self.assertFalse(snapshot["readiness"]["ready"])
                    self.assertEqual((response["frame"], snapshot["image"]["frame"]), (before, before))
                    self.assertEqual(response["checkpoint"]["visit"], before_visit)
                    self.assertTrue(response["checkpoint"]["proceed"])
                    self.assertEqual(snapshot["pause_plan"]["phase"], "retry")
                    self.assertEqual(snapshot["pause_plan"]["frames"], round(3 * stage4_dress.FRAME_RATE))
                    harness.clock.command("combo", "inputs", *snapshot["pause_plan"]["steps"])
                    if reason == "judges":
                        harness.clock.lua.run("acting_judge_unreadable=false; return 'ready'")
                    else:
                        harness.clock.lua.run("paint_acting_moves({'Beauty','Tough','Smart','Cute'}); "
                            "pixel(18,244,{132,132,132}); return 'ready'")
                ready = harness.call("round-readiness", 2)["observation"]
                self.assertTrue(ready["readiness"]["ready"])
                self.assertEqual(ready["pause_plan"]["phase"], "settle")
                self.assertEqual(ready["pause_plan"]["frames"], round(3 * stage4_dress.FRAME_RATE))
                harness.call("round-ready", 2)
                self.assertEqual(set(harness.clock.trace()), {"", "B"})
                self.assertEqual(harness.clock.lua.run("return table.concat(lua_messages, '\\n')"), before_messages)
                harness.clock.lua.run("paint_acting_order(3); return 'ready'")
                pending = harness.call("round-visit", 2)
                self.assertEqual(pending["checkpoint"]["visit"], before_visit + 1)
                snapshot = pending["checkpoint"]["snapshot"]
                self.assertTrue(snapshot["round_readiness"]["ready"])
                self.assertEqual(snapshot["plan"]["acting_position"], 3)
                self.assertEqual(snapshot["image"]["frame"], ready["image"]["frame"])
                self.assertEqual(snapshot["plan"]["start_frame"], ready["image"]["frame"])
                self.assertEqual(snapshot["trial"]["pre_selection_frames"], round(3 * stage4_dress.FRAME_RATE))
                self.assertEqual(snapshot["plan"]["pre_selection_frames"], round(3 * stage4_dress.FRAME_RATE))
                self.assertEqual(harness.counters()["moves"], 1)

    def test_next_round_cannot_be_visited_without_a_matched_paused_capture(self):
        for number, check_first in product((1, 2), (False, True)):
            with self.subTest(round=number, check_first=check_first):
                harness = self.harness()
                harness.prepare_entry()
                harness.dress(0, finish=True)
                harness.ranked_dance()
                harness.act(rounds=number - 1)
                if check_first:
                    harness.clock.lua.run("acting_judge_unreadable=true; return 'ready'")
                    ready = harness.call("round-readiness", number)["observation"]
                    self.assertFalse(ready["readiness"]["ready"])
                before = int(harness.clock.lua.run("return tostring(frame)"))
                rejected = harness.clock.checkpoint("round-visit", number)
                self.assertIn("error", rejected)
                self.assertEqual(rejected["frame"], before)
                self.assertEqual(harness.clock.lua.run("return tostring(mock_paused)"), "true")

    def test_entry_and_later_rounds_plan_paused_then_execute_one_uninterrupted_combo(self):
        for debug in (0, 1):
            with self.subTest(debug=debug):
                harness = self.harness(debug=debug)
                harness.prepare_entry()
                harness.dress(0, finish=True)
                harness.ranked_dance()
                harness.act(rounds=0)
                for number in (1, 2, 3, 4):
                    with self.subTest(round=number):
                        before = int(harness.clock.lua.run("return tostring(frame)"))
                        ready = harness.call("round-readiness", number)["observation"]
                        self.assertTrue(ready["readiness"]["ready"])
                        harness.call("round-ready", number)
                        pending = harness.call("round-visit", number)
                        snapshot = pending["checkpoint"]["snapshot"]
                        chosen = harness.acknowledge(pending)["checkpoint"]["snapshot"]["plan"]
                        self.assertEqual((snapshot["image"]["frame"], chosen["start_frame"]), (before, before))
                        self.assertEqual(harness.clock.lua.run("return tostring(mock_paused)"), "true")
                        prefix = round(3 * stage6_act.FRAME_RATE)
                        self.assertEqual((chosen["pre_selection_game_seconds"], chosen["pre_selection_frames"]), (3, prefix))
                        harness.clock.reset_measurement()
                        harness.clock.lua.run("capture_frames={}; return 'ready'")
                        executed = harness.clock.command("combo", "inputs", *chosen["steps"])
                        self.assertEqual(executed["stop_reason"], "combo_complete")
                        self.assertEqual(executed["frame"], chosen["end_frame"])
                        self.assertEqual(harness.clock.pauses(), 1)
                        trace = harness.clock.trace()
                        self.assertEqual(set(trace[:prefix]), {"", "B"})
                        self.assertEqual(trace[prefix], chosen["menu_buttons"][0])
                        self.assertEqual(len(trace), chosen["total_frames"])
                        captures = json.loads(harness.clock.lua.run(
                            f"return dofile({literal((ROOT / 'lua/json.lua').as_posix())}).encode(capture_frames)"))
                        self.assertEqual(set(captures), {chosen["end_frame"]})

    def test_matched_b_delay_cannot_run_separately_from_selection(self):
        for number, plan_first in product((1, 2), (False, True)):
            with self.subTest(round=number, plan_first=plan_first):
                harness = self.harness()
                harness.prepare_entry()
                harness.dress(0, finish=True)
                harness.ranked_dance()
                harness.act(rounds=number - 1)
                ready = harness.call("round-readiness", number)["observation"]
                harness.call("round-ready", number)
                if plan_first:
                    harness.acknowledge(harness.call("round-visit", number))
                before = int(harness.clock.lua.run("return tostring(frame)"))
                harness.clock.reset_measurement()
                rejected = harness.clock.command("combo", "inputs", *ready["pause_plan"]["steps"])
                self.assertIn("did not authorize", rejected["error"])
                self.assertEqual(rejected["frame"], before)
                self.assertEqual(harness.clock.trace(), [])
                self.assertEqual(harness.clock.lua.run("return tostring(mock_paused)"), "true")

    def test_multiple_legal_zero_heart_moves_stop_with_a_retained_selection_failure(self):
        harness = self.harness()
        harness.prepare_entry()
        harness.dress(0, finish=True)
        harness.ranked_dance()
        with self.assertRaises(AssertionError):
            harness.act(rounds=2, move_hearts=[6, 0, 0, 0])
        snapshot = harness.act_snapshots[1]
        self.assertTrue(snapshot["move_categories"]["readable"])
        self.assertFalse(snapshot["plan"]["selectable"])
        self.assertEqual(snapshot["plan"]["legal_move_slots"], [2, 3, 4])
        self.assertNotIn("steps", snapshot["plan"])
        self.assertEqual(int(harness.clock.lua.run("return tostring(frame)")), snapshot["image"]["frame"])
        self.assertEqual(harness.clock.lua.run("return tostring(mock_paused)"), "true")

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
                self.assertEqual((trial["total_input_count"], trial["total_judgment_count"], trial["total_miss_count"]),
                    (24, 1, int(missing)))
                self.assertEqual(first["label"], "Miss" if missing else "Excellent")
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
                "rejected_plan": ({"rejected_plans": (1, 6)}, 4 * count),
                "handoff_capture": ({"selected_inputs": {1: []},
                    "handoff_faults": ("lead-dances", "acting-transition")}, 5 * count),
            }
            for name, (options, executed) in cases.items():
                with self.subTest(rank=rank, fault=name):
                    harness = self.harness(rank=rank, debug=0)
                    harness.prepare_entry()
                    harness.dress(0, finish=True)
                    harness.clock.lua.run("lua_messages={}; return 'ready'")
                    harness.ranked_dance(**options, transition_seconds=18)
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
                    self.assertEqual((transition["mash_frames"], transition["end_frame"] - transition["start_frame"]), (1077, 1077))
                    self.assertEqual(harness.boundary["frame"], transition["end_frame"])
                    self.assertEqual(harness.saved_checkpoints, [])
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
                    ("Miss", "missed_feedback_window", 0))
                self.assertEqual(first["image"]["capture_callback"],
                    "before_frame_feedback_recovery" if callback == "before_frame" else "after_frame_feedback")
                self.assertTrue(all(not capture["feedback"] for capture in harness.rank_captures[1:]))

    def test_host_midpoint_analysis_errors_finish_six_responses_and_enter_acting(self):
        with temporary_directory() as folder:
            harness = self.harness(debug=0)
            harness.prepare_entry()
            harness.dress(0, finish=True)
            source = harness.boundary
            plan = rank_dance.stage5_plan(rank="Master", source_frame=source["frame"], host_timeout=300,
                fixed_timing=fixed_timing("Cool", "Master"), entry_pointer_x=source["snapshot"]["screen"]["pointer"]["position_bounds"][0],
                transition_seconds=1 / stage4_dress.FRAME_RATE)
            harness.begin(5, plan)

            def action(action, *args, expected=None, name=None):
                result = harness.clock.command(action, *args)
                if expected and result["stop_reason"] != expected:
                    raise dance_response.ReviewNeeded("The counted dance action was interrupted.")
                return result

            def acknowledge(pending, *, slot):
                self.assertIsNone(slot)
                result = harness.acknowledge(pending)
                return {**result, "host_receipt": str(Path(folder) / "receipt.json"), "host_savestate": None}

            runtime = SimpleNamespace(active_work=Path(folder), settings=super_contest.chain_plan(debug=0),
                boundary={"checkpoint": source, "save_state": None},
                controller=SimpleNamespace(), emulator=SimpleNamespace(), command=harness.call, action=action,
                acknowledge=acknowledge, record=Mock(), json=lambda name, value: write_json(Path(folder) / name, value))
            with patch("contest.rank_dance.prepare_response", side_effect=dance_response.ReviewNeeded("Midpoint analysis failed")):
                responses, last = rank_dance.stage5_responses(runtime, plan)
            trial = last["checkpoint"]["snapshot"]["trial"]
            self.assertEqual((len(responses), trial["total_inputs"], trial["total_skipped_inputs"]), (6, 0, 24))
            self.assertTrue(all(item["input_count"] == 0 for item in responses))
            rank_dance.stage5_handoff(runtime, plan)
            harness.acknowledge(harness.call("visit", "stage-5-dance"))
            self.assertTrue(harness.boundary["proceed"])
            harness.act(rounds=1)
            self.assertEqual(harness.boundary["checkpoint"], "stage-6-round")
            self.assertEqual(harness.counters()["engines"], 1)
            self.assertEqual(harness.saved_checkpoints, [])

    def test_first_feedback_warns_for_each_miss_across_distinct_contests(self):
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
        cases = ("Good", "Good", "Excellent", "Good")
        for index, label in enumerate(cases):
            with self.subTest(contest=index + 1, label=label):
                if index:
                    bag = harness.preparation["snapshot"]["bag_context"]
                    harness.prepare(opened_once=bag["opened_once"], cursor=bag["cursor_position"])
                    target = harness.boundary["snapshot"]["target"]
                    harness.clock.lua.run(f"target={literal(target)}; won=false; return 'ready'")
                    harness.enter()
                seen.append(harness.boundary["snapshot"]["target"]["ribbon_bit"])
                harness.dress(0, finish=True)
                harness.clock.lua.run("lua_messages={}; return 'ready'")
                harness.ranked_dance(bad_feedback=(1, 1, label))
                warned = label != "Excellent"
                expected_label = "Miss" if warned else "Excellent"
                snapshot = harness.boundary["snapshot"]
                first, trial = snapshot["offset_check"], snapshot["trial"]
                self.assertEqual(first["label"], expected_label)
                self.assertNotIn("consecutive_good_contests", first)
                self.assertEqual(trial["total_miss_count"], int(warned))
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
                    self.assertIn("Miss", warnings[0])
                self.assertTrue(all(captures["feedback"] == [] for captures in harness.rank_captures[1:]))
                harness.act(win=True)
                self.assertEqual(harness.boundary["decision"], "hit")
        self.assertEqual(seen, list(range(3, 3 + len(cases))))

    def test_initial_page_navigation_precedes_analysis_and_counts_every_frame(self):
        for taps in (0, 7):
            with self.subTest(taps=taps):
                harness = self.harness()
                harness.prepare_entry()
                source = harness.boundary["frame"]
                plan, handoff, progress = harness.dress(taps)
                self.assertEqual(handoff["handoff"]["emulator_frames"], 0)
                self.assertEqual(progress["page"]["frame"], source + taps * 33)
                self.assertEqual(progress["remaining_frames"], plan["automatic_dressing"]["budget_frames"] - taps * 33)
                navigation = progress["initial_navigation"]
                self.assertEqual((navigation["chosen_taps"], navigation["executed_taps"], navigation["executed_frames"]),
                                 (taps, taps, taps * 33))
                self.assertTrue(navigation["complete"])
                self.assertEqual(harness.counters()["pages"], 1)
                self.assertEqual(len([item for item in harness.counters()["touches"] if (item["x"], item["y"]) == (66, 160)]), taps * 2)

    def test_page_navigation_requires_its_full_render_cycle_before_finish(self):
        cases = ((240, 0, 7, 9, "deadline"), (263, 0, 7, 32, "deadline"), (264, 0, 8, 33, "deadline"),
                 (897, 0, 14, 33, "page_flip_limit"), (897, 7, 14, 33, "page_flip_limit"))
        for budget, initial_taps, expected_taps, last_frames, reason in cases:
            with self.subTest(budget=budget, initial_taps=initial_taps), temporary_directory() as folder:
                harness = self.harness()
                harness.prepare_entry()
                source = harness.boundary["frame"]
                plan = stage4_dress.dress_plan(host_timeout=300,
                    dress_seconds=budget / stage4_dress.FRAME_RATE)
                automatic = plan["automatic_dressing"]
                check = automatic["full_checks"][harness.boundary["snapshot"]["target"]["rank"]]
                pearl = check.get("pearl_rgb", automatic["indicator_rgb"]["pearl"])
                harness.begin(4, plan, initial_page_taps=initial_taps)
                harness.call("init")
                harness.clock.lua.run("for _,center in ipairs(" + literal(automatic["centers"]) + ") do "
                    "pixel(center.x,center.y+192,center.background_rgb) end; "
                    f"pixel({check['x']},{check['y']},{literal(pearl)}); return 'ready'")
                commands = []

                def command(action, **options):
                    result = harness.clock.command(action)
                    commands.append((action, result))
                    return result

                def acknowledge(pending):
                    return {**harness.acknowledge(pending),
                        "host_receipt": str(Path(folder) / f"page-{pending['checkpoint']['visit']}.json")}

                controller = SimpleNamespace(remaining=Mock(), live_deadline=None, screenshot=Mock())
                emulator = SimpleNamespace(controller=controller, command=command)
                events = Mock()
                dressing = stage4_dress.place_accessories(emulator, SimpleNamespace(command=harness.call), plan,
                    Path(folder), source, initial_taps, acknowledge=acknowledge, record=events)
                result, finished = commands[-2][1], commands[-1][1]
                last = dressing["last_input"]
                elapsed = budget if reason == "deadline" else (initial_taps + expected_taps) * 33
                self.assertEqual(result["frame"], source + elapsed)
                self.assertEqual(dressing["end_reason"], reason)
                self.assertEqual(dressing["page_flip_limit_reached"], reason == "page_flip_limit")
                self.assertEqual(dressing["collection_page_taps"], expected_taps)
                self.assertEqual((last["planned_frames"], last["executed_frames"]), (33, last_frames))
                self.assertEqual(last["deadline_truncated"], last_frames < 33)
                page_touches = [item for item in harness.counters()["touches"]
                    if (item["x"], item["y"]) == (66, 160)]
                total_taps = initial_taps + expected_taps
                self.assertEqual((dressing["page_taps"], len(page_touches)), (total_taps, total_taps * 2))
                self.assertFalse(result["stylus_touch"])
                if last_frames < 33:
                    self.assertTrue(all(item["frame"] < source + budget - last_frames for item in page_touches))
                if reason == "page_flip_limit":
                    self.assertEqual(result["stop_reason"], "dress_page_flip_limit_reached")
                    self.assertEqual(harness.counters()["pages"], 14)
                    self.assertEqual(dressing["initial_navigation"]["executed_taps"], initial_taps)
                    self.assertEqual(len([call for call in events.call_args_list
                        if call.args[0] == "dress_page_flip_limit"]), 1)
                self.assertEqual((finished["command_start_frame"], finished["command_end_frame"]),
                                  (source + elapsed, source + elapsed + 3))

    def test_dressing_deadline_releases_a_truncated_drag_before_finish(self):
        harness = self.harness()
        harness.prepare_entry()
        source = harness.boundary["frame"]
        plan = stage4_dress.dress_plan(host_timeout=300,
            dress_seconds=40 / stage4_dress.FRAME_RATE, page_wait_frames=0)
        harness.begin(4, plan, initial_page_taps=0)
        harness.call("init")
        center = plan["automatic_dressing"]["centers"][0]
        harness.clock.lua.run(f"pixel({center['x']},{center['y'] + 192},{{255,0,0}}); return 'ready'")
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

    def test_partial_and_empty_responses_continue_after_a_first_feedback_miss(self):
        for debug in (0, 1):
            with self.subTest(debug=debug):
                harness = self.harness(rank="Normal", debug=debug)
                harness.prepare_entry()
                harness.dress(0, finish=True)
                harness.ranked_dance(selected_inputs={1: [0], 6: []}, missing_feedback=((1, 1),))
                trial = harness.boundary["snapshot"]["trial"]
                self.assertEqual((trial["completed_dances"], trial["total_inputs"], trial["total_skipped_inputs"]), (6, 13, 5))
                self.assertEqual(trial["total_miss_count"], 1)
                self.assertEqual(trial["total_judgment_count"], 1)
                finishes = [item for item in harness.log_checkpoints if item["checkpoint"] == "rank-dance-finish"]
                self.assertEqual([item["frame"] for item in finishes], [item["response_end_frame"] for item in harness.rank_plans])
                self.assertTrue(all(item["proceed"] for item in finishes))


class DressUpPixelChecks(unittest.TestCase):
    counts = {"Normal": 5, "Super": 10, "Hyper": 15, "Master": 20}
    rows = ("abbba", "bcdcb", "bdddb", "bcdcb", "abbba")
    colors = {"a": (132, 82, 198), "b": (8, 189, 239),
              "c": (90, 231, 255), "d": (255, 214, 255)}

    def setUp(self):
        self.lua = Lua51()
        self.addCleanup(self.lua.close)
        self.lua.run(f"screens=dofile({literal((ROOT / 'contest/screen_checks.lua').as_posix())}); "
            f"json=dofile({literal((ROOT / 'lua/json.lua').as_posix())}); return 'ready'")

    def image(self, rank):
        image = Image.new("RGB", (256, 384), (255, 140, 231))
        for slot in range(self.counts[rank]):
            x, y = 46 + slot % 10 * 18, 112 + slot // 10 * 18
            for dy, row in enumerate(self.rows):
                for dx, code in enumerate(row):
                    image.putpixel((x + dx - 2, y + dy - 2), self.colors[code])
        return image

    def read(self, image, rank):
        data = image.convert("RGB").tobytes()
        parts = []
        for color, run in groupby(zip(data[0::3], data[1::3], data[2::3])):
            count = sum(1 for _ in run)
            parts.append(f"string.rep(string.char(0,{color[0]},{color[1]},{color[2]}),{count})")
        height = image.height
        raw = f"string.char(255,254,1,0,{height // 256},{height % 256},1,0,0,0,0)..table.concat({{" + ",".join(parts) + "})"
        return json.loads(self.lua.run(f"return json.encode(screens.read({raw}, 'dress_up_entry', {literal(rank)}))"))

    def test_all_rank_slots_are_readable_without_button_or_header_text(self):
        for rank, count in self.counts.items():
            with self.subTest(rank=rank):
                image = self.image(rank)
                expected = self.read(image, rank)
                image.paste((0, 0, 0), (0, 192, 256, 384))
                image.paste((255, 255, 255), (0, 0, 256, 32))
                result = self.read(image, rank)
                self.assertEqual(result, expected)
                self.assertTrue(result["confirmed"])
                self.assertEqual((result["expected_empty_slots"], result["empty_slot_count"]), (count, count))
                self.assertEqual((result["matched_pixels"], result["expected_pixels"], result["direct_read_frames"]),
                    (count * 25, count * 25, 0))
                self.assertTrue(all(slot["y"] < 192 for slot in result["slots"]))
                self.assertEqual(self.read(image.crop((0, 0, 256, 192)), rank), result)

    def test_filled_or_damaged_required_slots_are_rejected(self):
        for rank, count in self.counts.items():
            last = count - 1
            x, y = 46 + last % 10 * 18, 112 + last // 10 * 18
            for filled in (False, True):
                with self.subTest(rank=rank, filled=filled):
                    image = self.image(rank)
                    if filled:
                        image.paste((231, 0, 115), (x - 2, y - 2, x + 3, y + 3))
                    else:
                        image.putpixel((x, y), (0, 0, 0))
                    result = self.read(image, rank)
                    self.assertFalse(result["confirmed"])
                    self.assertEqual(result["empty_slot_count"], count - 1)

    def test_missing_or_extra_rank_slots_are_rejected(self):
        for actual, expected in product(self.counts, repeat=2):
            if actual == expected:
                continue
            with self.subTest(actual=actual, expected=expected):
                result = self.read(self.image(actual), expected)
                self.assertFalse(result["confirmed"])
                self.assertEqual(result["empty_slot_count"], self.counts[actual])

    def test_a_finish_button_without_empty_slots_does_not_confirm_entry(self):
        image = Image.new("RGB", (256, 384), (81, 81, 81))
        image.paste((255, 101, 150), (194, 350, 249, 376))
        self.assertFalse(self.read(image, "Normal")["confirmed"])


class AccessoryGridPixelChecks(unittest.TestCase):
    def setUp(self):
        self.lua = Lua51()
        self.addCleanup(self.lua.close)
        self.lua.run(f"pixels=dofile({literal((ROOT / 'contest/dress_pixels.lua').as_posix())}); "
            f"json=dofile({literal((ROOT / 'lua/json.lua').as_posix())}); return 'ready'")

    def background(self):
        green, pale, white = (121, 247, 215), (182, 255, 239), (223, 255, 255)
        image = Image.new("RGB", (116, 127), (121, 81, 113))
        for x in (0, 115):
            image.putpixel((x, 0), (255, 56, 174))
        for box in ((5, 2, 111, 3), (4, 3, 112, 4), (5, 124, 111, 125)):
            image.paste(white, box)
        image.paste(green, (4, 4, 112, 124))
        edges = {20: (white, pale), 21: (pale, white), 22: (green, pale), 23: (pale, green),
                 136: (pale, green), 137: (green, pale), 138: (pale, white), 139: (white, pale)}
        for y, colors in edges.items():
            for x in range(10, 118):
                image.putpixel((x - 6, y - 16), colors[x % 2])
        dot = ((None, pale, pale, None), (pale, white, white, pale),
               (pale, white, white, pale), (None, pale, pale, None))
        for left in range(14, 111, 8):
            for dy, row in enumerate(dot):
                for dx, color in enumerate(row):
                    if color is not None:
                        image.putpixel((left + dx - 6, 25 + dy - 16), color)
        return image

    def image(self, settings):
        image = Image.new("RGB", (256, 384), (0, 0, 0))
        with self.background() as background:
            image.paste(background, (6, 208))
        check = settings["full_checks"]["Master"]
        image.putpixel((check["x"], check["y"]), tuple(settings["indicator_rgb"]["pearl"]))
        return image

    def read(self, image, settings):
        data = image.convert("RGB").tobytes()
        parts = []
        for color, run in groupby(zip(data[0::3], data[1::3], data[2::3])):
            count = sum(1 for _ in run)
            parts.append(f"string.rep(string.char(0,{color[0]},{color[1]},{color[2]}),{count})")
        raw = "string.char(255,254,1,0,1,128,1,0,0,0,0)..table.concat({" + ",".join(parts) + "})"
        return json.loads(self.lua.run(f"local page=pixels.read_page({raw},{literal(settings)},'Master'); "
            "page.cells=setmetatable(page.cells,json.array_metatable); "
            "page.items=setmetatable(page.items,json.array_metatable); return json.encode(page)"))

    def test_reference_contains_the_entire_clean_box_not_only_grid_samples(self):
        with stage4_dress.read_background() as actual, self.background() as expected:
            self.assertEqual((actual.mode, actual.size), ("RGB", (116, 127)))
            self.assertEqual(actual.info["touchscreen_origin"], "6,16")
            self.assertEqual(actual.tobytes(), expected.tobytes())

    def test_full_box_centers_match_background_for_every_supported_grid(self):
        corners = {6: ((16, 27), (112, 132)), 7: ((14, 25), (114, 134)), 8: ((13, 24), (115, 135))}
        for grid, (first, last) in corners.items():
            with self.subTest(grid=grid):
                settings = stage4_dress.dress_plan(host_timeout=300, dress_grid=grid)["automatic_dressing"]
                centers = settings["centers"]
                self.assertEqual(settings["rectangle"], {"left": 6, "top": 16, "right": 122, "bottom": 143})
                self.assertEqual(len(centers), grid * grid)
                self.assertEqual((centers[0]["x"], centers[0]["y"]), first)
                self.assertEqual((centers[-1]["x"], centers[-1]["y"]), last)
                with self.background() as background:
                    for center in centers:
                        self.assertEqual(center["background_rgb"],
                            list(background.getpixel((center["x"] - 6, center["y"] - 16))))
                page = self.read(self.image(settings), settings)
                self.assertEqual(page["items"], [])
                self.assertEqual(len(page["cells"]), grid * grid)
                self.assertTrue(all(cell["empty"] for cell in page["cells"]))
                self.assertTrue(page["readable"])
                self.assertFalse(page["full"])
                self.assertEqual(page["direct_read_frames"], 0)
                self.assertEqual(page["background_reference"], settings["background_reference"])

    def test_only_changed_center_pixels_queue_drags_including_dots_and_edges(self):
        for grid in (6, 7, 8):
            with self.subTest(grid=grid):
                settings = stage4_dress.dress_plan(host_timeout=300, dress_grid=grid)["automatic_dressing"]
                centers = settings["centers"]
                first = next((center for center in centers if center["background_rgb"] != [121, 247, 215]), centers[0])
                edge = centers[-1]
                image = self.image(settings)
                color = (121, 247, 215) if first["background_rgb"] != [121, 247, 215] else (255, 0, 0)
                image.putpixel((first["x"], first["y"] + 192), color)
                image.putpixel((edge["x"], edge["y"] + 192), (255, 0, 0))
                neighbor = (first["x"] + 1, first["y"])
                self.assertNotIn(neighbor, {(center["x"], center["y"]) for center in centers})
                image.putpixel((neighbor[0], neighbor[1] + 192), (255, 0, 0))
                page = self.read(image, settings)
                self.assertEqual(page["items"], [{key: center[key] for key in ("x", "y", "row", "column")}
                    for center in (first, edge)])
                self.assertEqual(sum(not cell["empty"] for cell in page["cells"]), 2)
                self.assertEqual(page["direct_read_frames"], 0)

    def test_missing_or_wrong_native_background_is_rejected(self):
        with temporary_directory() as folder:
            path = Path(folder) / "background.png"
            with patch.object(stage4_dress, "DRESS_BACKGROUND", path):
                with self.assertRaises(FileNotFoundError):
                    stage4_dress.dress_plan(host_timeout=300)
                for mode, size in (("RGB", (116, 126)), ("RGBA", (116, 127))):
                    with self.subTest(mode=mode, size=size):
                        Image.new(mode, size).save(path)
                        with self.assertRaisesRegex(ValueError, "RGB PNG"):
                            stage4_dress.dress_plan(host_timeout=300)


class ActingPixelChecks(unittest.TestCase):
    def setUp(self):
        self.lua = Lua51()
        self.addCleanup(self.lua.close)
        self.lua.run(f"pixels=dofile({literal((ROOT / 'contest/acting_pixels.lua').as_posix())}); "
            f"json=dofile({literal((ROOT / 'lua/json.lua').as_posix())}); return 'ready'")

    def image(self, categories, hearts=(3, 2, 2, 2), acting_position=1):
        image = Image.new("RGB", (256, 384), (81, 81, 81))
        for slot, (box, category, count) in enumerate(zip(MOVE_BOXES, categories, hearts), 1):
            image.paste(MOVE_FILLS[category], box)
            paint_move_hearts(image, slot, category, count)
        for position in range(1, 5):
            paint_acting_panel(image, position,
                PLAYER_PANEL_COLORS if position == acting_position else OPPONENT_PANEL_COLORS)
        return image

    def read(self, image, *, order=False, unavailable=(), buttons=False, previous_move=1, readiness=False):
        data = image.convert("RGB").tobytes()
        parts = []
        for color, run in groupby(zip(data[0::3], data[1::3], data[2::3])):
            count = sum(1 for _ in run)
            parts.append(f"string.rep(string.char(0,{color[0]},{color[1]},{color[2]}),{count})")
        height = image.height
        raw = f"string.char(255,254,1,0,{height // 256},{height % 256},1,0,0,0,0)..table.concat({{" + ",".join(parts) + "})"
        call = f"pixels.read_round_readiness({raw}, {literal(unavailable)}, {literal(previous_move)})" if readiness else (
            f"pixels.read_acting_order({raw})" if order else (
            f"pixels.read_move_buttons({raw}, {literal(unavailable)}, {literal(previous_move)})" if buttons else
            f"pixels.read_move_categories({raw}, {literal(unavailable)})"))
        return json.loads(self.lua.run(f"return json.encode({call})"))

    def test_entry_requires_fifteen_empty_stars_and_does_not_skip_the_first_move(self):
        image = self.image(["Cool", "Beauty", "Cute", "Smart"])
        colors = {"a": (81, 81, 81), "b": (81, 81, 215), "*": (255, 207, 24)}
        empty = ("abbaa", "bbbba", "bbbba", "bbbba", "abbaa")
        for judge in range(3):
            for cell in range(5):
                for dy, row in enumerate(empty):
                    for dx, code in enumerate(row):
                        image.putpixel((84 + judge * 32 + cell * 5 + dx, 8 + dy), colors[code])
        ready = self.read(image, readiness=True, previous_move=None)
        self.assertTrue(ready["ready"] and ready["entry"] and ready["stars"]["all_empty"])
        self.assertEqual(ready["move_buttons"]["available_count"], 4)
        first_move_mismatch = image.copy()
        first_move_mismatch.putpixel((18, 244), (132, 132, 132))
        self.assertFalse(self.read(first_move_mismatch, readiness=True, previous_move=None)["ready"])
        self.assertTrue(self.read(first_move_mismatch, readiness=True, previous_move=1)["ready"])
        self.assertTrue(self.read(first_move_mismatch, readiness=True, previous_move=None, unavailable=(1,))["ready"])
        nonempty = image.copy()
        for dy, row in enumerate(("ab*aa", "b***a", "*****", "b***a", "ab*aa")):
            for dx, code in enumerate(row):
                nonempty.putpixel((84 + dx, 8 + dy), colors[code])
        wrong_entry = self.read(nonempty, readiness=True, previous_move=None)
        self.assertTrue(wrong_entry["stars"]["readable"])
        self.assertFalse(wrong_entry["ready"])
        self.assertTrue(self.read(nonempty, readiness=True, previous_move=1)["ready"])
        self.assertFalse(self.read(Image.new("RGB", (256, 384)), readiness=True, previous_move=None)["ready"])

    def test_all_five_native_move_fills_are_readable_in_every_slot(self):
        categories = list(stage3_enter.CATEGORIES)
        for first in range(5):
            expected = [categories[(first + slot) % 5] for slot in range(4)]
            hearts = [(first + slot) % 7 for slot in range(4)]
            with self.subTest(categories=expected):
                result = self.read(self.image(expected, hearts))
                self.assertTrue(result["readable"])
                self.assertEqual(result["categories"], expected)
                self.assertEqual(result["hearts"], hearts)
                self.assertEqual((result["matched_pixels"], result["total_pixels"], result["direct_read_frames"]), (24, 24, 0))

    def test_retained_four_language_pixels_read_native_hearts_and_acting_order(self):
        reference = json.loads((ROOT / "tests/support/acting-capture.json").read_text(encoding="utf-8"))
        self.assertEqual(reference["sources"], ["Japan", "Europe", "USA", "Chinese"])
        image = Image.new("RGB", (256, 384), (81, 81, 81))
        for x, y, r, g, b in reference["move_fill_points"] + reference["acting_order_points"]:
            image.putpixel((x, y), (r, g, b))
        for strip in reference["heart_strips"]:
            x, y = strip["origin"]
            self.assertEqual(len(strip["rows"]), 7)
            for dy, row in enumerate(strip["rows"]):
                self.assertEqual(len(row), 48)
                for dx, code in enumerate(row):
                    image.putpixel((x + dx, y + dy), tuple(reference["palette"][code]))
        moves, order = self.read(image), self.read(image, order=True)
        self.assertTrue(moves["readable"])
        self.assertEqual(moves["categories"], reference["move_types"])
        self.assertEqual(moves["hearts"], reference["move_hearts"])
        self.assertTrue(order["readable"])
        self.assertEqual(order["position"], reference["acting_position"])

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
                self.assertEqual(result["unrecognized_move_slots"], [1])
                self.assertEqual(result["recognized_move_slots"], [2, 3, 4])
                self.assertEqual(len(result["warnings"]), 1)
        image = self.image(categories)
        image.paste((89, 142, 231), MOVE_BOXES[0])
        self.assertFalse(self.read(image)["readable"])
        with self.assertRaisesRegex(AssertionError, "stacked"):
            self.read(image.crop((0, 0, 256, 192)))

    def test_native_heart_counts_ignore_move_names_type_letters_and_effect_text(self):
        categories, hearts = ["Cool", "Beauty", "Cute", "Smart"], [6, 0, 3, 4]
        image = self.image(categories, hearts)
        expected = self.read(image)
        for box, (heart_x, heart_y) in zip(MOVE_BOXES, MOVE_HEART_ORIGINS):
            image.paste((255, 255, 255), (box[0] + 4, box[1] + 1, box[2] - 7, box[1] + 18))
            image.paste((0, 0, 0), (box[0] + 4, heart_y - 8, heart_x, heart_y + 7))
            image.paste((255, 255, 255), (box[0] + 4, heart_y + 9, box[2] - 7, box[3] - 7))
        self.assertEqual(self.read(image), expected)
        self.assertTrue(expected["hearts_readable"])
        self.assertEqual(expected["hearts"], hearts)

    def test_damaged_or_gapped_heart_cells_are_not_silently_counted_as_zero(self):
        categories = ["Cool", "Beauty", "Cute", "Smart"]
        for damage in ("partial", "gap", "unknown_empty_cell"):
            with self.subTest(damage=damage):
                image = self.image(categories)
                if damage == "partial":
                    image.putpixel((62, 242), MOVE_FILLS["Cool"])
                elif damage == "gap":
                    image.paste(MOVE_FILLS["Cool"], (67, 240, 75, 247))
                else:
                    image.putpixel((91, 242), (132, 132, 132))
                result = self.read(image)
                self.assertTrue(result["categories_readable"])
                self.assertFalse(result["hearts_readable"])
                self.assertFalse(result["readable"])
                self.assertEqual(result["hearts"][0], -1)
                self.assertEqual(len(result["warnings"]), 1)
                self.assertIn("unrecognized raw hearts", result["warnings"][0])

    def test_blacklisted_slots_need_neither_move_fill_nor_heart_pixels(self):
        image = self.image(["Cool", "Beauty", "Cute", "Smart"])
        for box in MOVE_BOXES[2:]:
            image.paste((0, 0, 0), box)
        result = self.read(image, unavailable=(3, 4))
        self.assertTrue(result["readable"])
        self.assertEqual(result["categories"], ["Cool", "Beauty", "unavailable", "unavailable"])
        self.assertEqual(result["hearts"], [3, 2, -1, -1])
        self.assertEqual(len(result["warnings"]), 0)

    def test_all_four_acting_positions_use_panel_colors_without_any_names(self):
        for position in range(1, 5):
            with self.subTest(position=position):
                image = self.image(["Cool", "Beauty", "Cute", "Smart"], acting_position=position)
                for row in range(4):
                    image.paste((255, 255, 255), (0, row * 48, 80, row * 48 + 32))
                result = self.read(image, order=True)
                self.assertTrue(result["readable"])
                self.assertEqual((result["position"], result["player_count"], result["direct_read_frames"]), (position, 1, 0))
                self.assertEqual(result["source"], "native_player_panel_fill_rgb")
                self.assertEqual((result["matched_pixels"], result["total_pixels"]), (24, 24))
                self.assertEqual(self.read(image.crop((0, 0, 256, 192)), order=True), result)

    def test_unknown_conflicting_missing_and_multiple_player_panels_are_rejected(self):
        for damage in ("unknown", "conflicting", "missing", "multiple"):
            with self.subTest(damage=damage):
                image = self.image(["Cool", "Beauty", "Cute", "Smart"])
                if damage == "unknown":
                    image.putpixel((4, 42), (132, 132, 132))
                elif damage == "conflicting":
                    image.putpixel((4, 42), OPPONENT_PANEL_COLORS[1])
                elif damage == "missing":
                    paint_acting_panel(image, 1, OPPONENT_PANEL_COLORS)
                else:
                    paint_acting_panel(image, 2, PLAYER_PANEL_COLORS)
                result = self.read(image, order=True)
                self.assertFalse(result["readable"])
                self.assertNotIn("position", result)


    def test_between_round_menu_checks_exclude_gray_previous_and_empty_slots(self):
        for previous in range(1, 5):
            with self.subTest(previous=previous):
                image = self.image(["Cool", "Beauty", "Cute", "Smart"])
                empty = previous % 4 + 1
                for slot in (previous, empty):
                    image.paste((132, 132, 132), MOVE_BOXES[slot - 1])
                result = self.read(image, buttons=True, previous_move=previous, unavailable=(empty,))
                self.assertTrue(result["readable"])
                self.assertEqual((result["available_count"], result["direct_read_frames"]), (2, 0))
                self.assertTrue(result["slots"][previous - 1]["unavailable"])
                self.assertTrue(result["slots"][empty - 1]["unavailable"])

    def test_missing_move_menu_or_conflicting_selectable_fill_is_not_ready(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                image = self.image(["Cool", "Beauty", "Cute", "Smart"])
                if missing:
                    image.paste((255, 101, 150), (0, 192, 256, 384))
                else:
                    image.putpixel((146, 244), MOVE_FILLS["Cool"])
                self.assertFalse(self.read(image, buttons=True)["readable"])
        with self.assertRaisesRegex(AssertionError, "stacked"):
            self.read(image.crop((0, 0, 256, 192)), buttons=True)

    def test_move_menu_readiness_does_not_replace_cached_scoring_reads(self):
        image = self.image(["Cool", "Beauty", "Cute", "Smart"])
        for probes in MOVE_PROBES:
            image.putpixel(probes[0], (132, 132, 132))
        image.putpixel((62, 242), MOVE_FILLS["Cool"])
        self.assertFalse(self.read(image)["readable"])
        self.assertTrue(self.read(image, buttons=True)["readable"])


class ActingReadinessLoopChecks(unittest.TestCase):
    def loop(self, folder, detections, *, deadline=100, round_number=2):
        state = {"frame": 1000, "host_seconds": 0, "checks": 0, "ready_rounds": []}
        timing = stage6_act.acting_plan(host_timeout=300)["between_round_readiness"]

        def remaining():
            if state["host_seconds"] >= deadline:
                raise TimeoutError("timeout")
            return deadline - state["host_seconds"]

        controller = SimpleNamespace(live_deadline=deadline, remaining=Mock(side_effect=remaining))

        def command(action, number):
            if action == "round-ready":
                state["ready_rounds"].append(number)
                return {}
            self.assertEqual((action, number), ("round-readiness", round_number))
            state["checks"] += 1
            ready = detections[min(state["checks"] - 1, len(detections) - 1)]
            phase = "settle" if ready else "retry"
            frames = timing[phase + "_frames"]
            cycle, tail = divmod(frames, 30)
            steps = ["B:10:20"] * cycle + ([f"B:10:{tail - 10}"] if tail > 10 else [f"wait:{tail}"] if tail else [])
            path = folder / f"round-{round_number}-readiness-{state['checks']:03d}.gd"
            path.write_bytes(bytes((255, 254, 1, 0, 1, 128, 1, 0, 0, 0, 0)) + bytes((0, 81, 81, 81)) * (256 * 384))
            return {"observation": {"image": {"path": str(path), "frame": state["frame"], "epoch": 0,
                "source": "fresh_zero_frame_framebuffer", "direct_read_frames": 0}, "readiness": {"ready": ready},
                "pause_plan": {"phase": phase, "steps": steps, "end_frame": state["frame"] + frames}}}

        def advance(action, name, *steps, timeout, deadline):
            self.assertEqual((action, name, timeout, deadline), ("combo", "inputs", None, controller.live_deadline))
            for step in steps:
                values = step.split(":")
                self.assertIn(values[0], ("B", "wait"))
                state["frame"] += sum(map(int, values[1:]))
            state["host_seconds"] += 1
            return {"frame": state["frame"], "stop_reason": "combo_complete"}

        client = SimpleNamespace(command=Mock(side_effect=command))
        emulator = SimpleNamespace(controller=controller, command=Mock(side_effect=advance))
        return state, emulator, client

    def test_three_second_retries_then_paused_match_are_silent_and_retain_each_capture(self):
        with temporary_directory() as folder:
            folder = Path(folder)
            state, emulator, client = self.loop(folder, [False, False, True])
            output = io.StringIO()
            with redirect_stdout(output):
                checks = stage6_act.wait_round_ready(emulator, client, folder, 2)
            self.assertEqual(output.getvalue(), "")
            self.assertEqual([check["pause_plan"]["phase"] for check in checks], ["retry", "retry", "settle"])
            self.assertEqual(state["ready_rounds"], [2])
            self.assertEqual(state["frame"], 1000 + 2 * round(3 * stage4_dress.FRAME_RATE))
            self.assertEqual(emulator.command.call_count, 2)
            self.assertEqual(len(list(folder.glob("round-2-readiness-*.gd"))), 3)
            self.assertEqual(len(list(folder.glob("round-2-readiness-*.png"))), 3)
            for check in checks:
                path = Path(check["image"]["path"])
                self.assertTrue(path.is_file() and path.with_suffix(".png").is_file())
                saved = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
                expected_end = check["image"]["frame"] if check["readiness"]["ready"] else check["pause_plan"]["end_frame"]
                self.assertEqual(saved["end_frame"], expected_end)

    def test_timeout_retains_unreadable_evidence_without_a_round_visit_or_deadline_reset(self):
        with temporary_directory() as folder:
            folder = Path(folder)
            state, emulator, client = self.loop(folder, [False], deadline=2)
            output = io.StringIO()
            with redirect_stdout(output), self.assertRaises(TimeoutError):
                stage6_act.wait_round_ready(emulator, client, folder, 2)
            self.assertEqual(output.getvalue(), "")
            self.assertEqual((state["checks"], state["ready_rounds"], emulator.controller.live_deadline), (2, [], 2))
            self.assertEqual(len(list(folder.glob("round-2-readiness-*.gd"))), 2)
            self.assertEqual(len(list(folder.glob("round-2-readiness-*.png"))), 2)
            self.assertTrue(all(call.args[0] == "round-readiness" for call in client.command.call_args_list))
            last = json.loads((folder / "round-2-readiness-002.json").read_text(encoding="utf-8"))
            self.assertFalse(last["readiness"]["ready"])

    def test_match_defers_b_delay_without_advancing_the_paused_frame(self):
        with temporary_directory() as folder:
            folder = Path(folder)
            state, emulator, client = self.loop(folder, [True], deadline=1)
            checks = stage6_act.wait_round_ready(emulator, client, folder, 2)
            self.assertEqual(state["ready_rounds"], [2])
            self.assertEqual((state["frame"], checks[0]["end_frame"]), (1000, 1000))
            self.assertEqual(checks[0]["pause_plan"]["end_frame"], 1000 + round(3 * stage6_act.FRAME_RATE))
            emulator.command.assert_not_called()

    def test_deadline_after_ready_observation_does_not_authorize_the_round(self):
        with temporary_directory() as folder:
            folder = Path(folder)
            state, emulator, client = self.loop(folder, [True], deadline=1)
            observe = client.command.side_effect

            def expire(action, number):
                result = observe(action, number)
                state["host_seconds"] = 1
                return result

            client.command.side_effect = expire
            with self.assertRaises(TimeoutError):
                stage6_act.wait_round_ready(emulator, client, folder, 2)
            self.assertEqual(state["ready_rounds"], [])
            self.assertEqual(state["frame"], 1000)
            emulator.command.assert_not_called()

    def test_entry_reuses_the_same_three_second_guard_as_later_rounds(self):
        for number in (1, 2, 3, 4):
            with self.subTest(round=number), temporary_directory() as folder:
                folder = Path(folder)
                state, emulator, client = self.loop(folder, [False, True], round_number=number)
                checks = stage6_act.wait_round_ready(emulator, client, folder, number)
                self.assertEqual(state["ready_rounds"], [number])
                self.assertEqual(state["frame"], 1000 + round(3 * stage6_act.FRAME_RATE))
                self.assertEqual(emulator.command.call_count, 1)
                self.assertEqual(checks[-1]["end_frame"], checks[-1]["image"]["frame"])

    def test_invalid_rounds_have_no_readiness_side_effects(self):
        for number in (0, 5):
            with self.subTest(round=number), temporary_directory() as folder:
                folder = Path(folder)
                state, emulator, client = self.loop(folder, [True])
                with self.assertRaises(ValueError):
                    stage6_act.wait_round_ready(emulator, client, folder, number)
                client.command.assert_not_called()
                emulator.command.assert_not_called()

    def test_main_host_checks_entry_before_planning_and_does_not_select_on_timeout(self):
        with temporary_directory() as folder:
            folder = Path(folder)
            runtime = SimpleNamespace(settings=super_contest.chain_plan(debug=0), active_work=folder,
                overworld={}, unavailable_move_slots=[], party={}, emulator=Mock(), start_section=Mock(),
                command=Mock(return_value={}), action=Mock(), acknowledge=Mock(), json=Mock(), record=Mock())
            with patch.object(stage6_act, "prepare_render", return_value={}), \
                    patch.object(stage6_act, "wait_round_ready", side_effect=TimeoutError("timeout")) as guard:
                with self.assertRaises(TimeoutError):
                    sections.act(runtime, attempt=1)
            guard.assert_called_once_with(runtime.emulator, runtime, folder, 1)
            self.assertEqual([call.args for call in runtime.command.call_args_list], [("begin-attempt",)])
            runtime.action.assert_not_called()
            runtime.acknowledge.assert_not_called()


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

    def plan(self, number, previous_move, previous_judge, counts, move_categories=None, *, category="Cool",
             hearts=(2, 2, 2, 2), acting_position=1, unavailable_move_slots=()):
        config = {**self.config, "target": {**TARGET, "category": category},
            "unavailable_move_slots": unavailable_move_slots}
        moves = {"readable": True, "categories": move_categories or ["Beauty", "Tough", "Smart", "Cute"], "hearts": hearts}
        order = {"readable": True, "position": acting_position}
        return json.loads(self.clock.lua.run(f"local counts={literal(counts)}; "
            "local stars={readable=true,counts=counts}; "
            f"return json.encode(acting_plan.round({literal(config)}, {number}, {previous_move}, "
            f"{previous_judge}, frame, stars, {literal(moves)}, {literal(order)}))"))

    def perform(self, plan, menu):
        self.assertEqual(plan["menu_buttons"], menu)
        self.clock.reset_measurement()
        executed = self.clock.command("combo", "inputs", *plan["steps"])
        prefix_frames = self.config["between_round_readiness"]["settle_frames"]
        hold, release = self.config["timing"]["mash_hold_frames"], self.config["timing"]["mash_release_frames"]
        cycles, tail = divmod(prefix_frames, hold + release)
        prefix = (["B"] * hold + [""] * release) * cycles
        prefix += ["B"] * hold + [""] * (tail - hold) if tail > hold else [""] * tail
        expected = prefix + [value for button in menu for value in (button, "")] + [""]
        self.assertEqual(plan["pre_selection_frames"], prefix_frames)
        self.assertEqual(self.clock.trace(), expected)
        self.assertEqual(executed["command_start_frame"], plan["start_frame"])
        self.assertEqual(executed["command_end_frame"], plan["end_frame"])
        self.assertEqual(plan["total_frames"], len(expected))

    def test_regional_bases_and_shared_retry_settle_durations(self):
        for code, base, final in (("CPUJ", 60, 135), ("CPUE", 65, 140)):
            with self.subTest(code=code):
                plan = stage6_act.acting_plan(host_timeout=300,
                    timing_profile=super_contest.load_profile(code))
                self.assertEqual([round_["mash_game_seconds"] for round_ in plan["rounds"]], [base, base, base, final])
                values = plan["between_round_readiness"]
                self.assertEqual((values["retry_game_seconds"], values["settle_game_seconds"]), (3, 3))
                self.assertEqual(values["messages"], "silent")
                self.assertEqual(values["matched_sequence"], "plan_paused_then_b_delay_and_selection_in_one_combo")
                post = plan["post_acting_readiness"]
                self.assertEqual((post["retry_game_seconds"], post["settle_game_seconds"]), (3, 3))
                self.assertEqual((post["before"], post["deadline"]),
                    ("first_result_memory_x", "existing_section_host_deadline"))
                self.assertEqual(super_contest.chain_plan(game_code=code)["acting_result_readiness"], post)
                for ready, seconds in ((False, 3), (True, 3)):
                    pause = json.loads(self.clock.lua.run(f"return json.encode(acting_plan.round_pause({literal(plan)}, "
                        f"2, frame, 0, {literal(ready)}))"))
                    self.assertEqual(pause["frames"], round(seconds * stage4_dress.FRAME_RATE))
                    self.clock.reset_measurement()
                    self.clock.command("combo", "inputs", *pause["steps"])
                    self.assertEqual(len(self.clock.trace()), pause["frames"])
                    self.assertEqual(set(self.clock.trace()), {"", "B"})

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

    def test_early_four_star_override_uses_the_strongest_matching_move(self):
        for category, position in product(stage3_enter.CATEGORIES, (1, 2)):
            with self.subTest(category=category, position=position):
                other = "Beauty" if category != "Beauty" else "Cool"
                move_types = [other, other, category, category]
                plan = self.plan(2, 1, 1, [0, 4, 4], move_types, category=category,
                    hearts=(6, 6, 2, 4), acting_position=position)
                self.assertEqual((plan["move"], plan["judge"], plan["move_hearts"]), (4, 2, 4))
                self.assertEqual(plan["move_category"], category)
                self.assertEqual(plan["move_policy"], "four_star_category_match_most_hearts")
                self.assertTrue(plan["matching_category_override"])
                tied = self.plan(2, 1, 1, [0, 4, 4], move_types, category=category,
                    hearts=(6, 6, 4, 4), acting_position=position)
                self.assertEqual((tied["move"], tied["judge"]), (3, 2))

    def test_fallback_uses_fewest_judge_and_strongest_eligible_move(self):
        cases = (
            ([0, 4, 1], ["Beauty", "Tough", "Cool", "Cool"], 3, 1),
            ([0, 4, 1], ["Beauty", "Tough", "Cool", "Cool"], 4, 1),
            ([2, 3, 1], ["Beauty", "Tough", "Cool", "Cool"], 1, 3),
            ([2, 5, 1], ["Beauty", "Tough", "Cool", "Cool"], 2, 3),
            ([0, 4, 1], ["Beauty", "Tough", "Smart", "Cute"], 1, 1),
            ([0, 4, 1], ["Cool", "Tough", "Smart", "Cute"], 2, 1),
        )
        for counts, move_types, position, judge in cases:
            with self.subTest(counts=counts, position=position, categories=move_types):
                plan = self.plan(2, 1, 1, counts, move_types, hearts=(6, 5, 2, 4), acting_position=position)
                self.assertEqual((plan["move"], plan["judge"], plan["move_hearts"]), (2, judge, 5))
                self.assertFalse(plan["matching_category_override"])
                self.assertEqual(plan["move_policy"], "most_raw_hearts")
        tied = self.plan(2, 1, 3, [0, 0, 4], hearts=(6, 4, 4, 4), acting_position=4)
        self.assertEqual((tied["move"], tied["judge"]), (2, 1))

    def test_last_used_move_is_always_excluded_from_both_picking_paths(self):
        for previous, position in product(range(1, 5), (1, 4)):
            with self.subTest(previous=previous, position=position):
                hearts = [1, 2, 3, 4]
                hearts[previous - 1] = 6
                plan = self.plan(2, previous, 1, [0, 4, 1], ["Cool"] * 4,
                    hearts=hearts, acting_position=position)
                expected = max((slot for slot in range(1, 5) if slot != previous), key=lambda slot: hearts[slot - 1])
                self.assertEqual(plan["move"], expected)
                self.assertNotEqual(plan["move"], previous)
                self.assertNotIn(previous, plan["eligible_move_slots"])
                self.assertEqual(plan["judge"], 2 if position == 1 else 1)
        first = self.plan(1, 1, 1, [0, 0, 0], hearts=(6, 2, 2, 2))
        self.assertEqual(first["move"], 1)

    def test_zero_heart_moves_are_allowed_only_as_the_sole_legal_move(self):
        plan = self.plan(2, 1, 1, [0, 4, 1], ["Beauty", "Tough", "Cool", "Cute"], hearts=(6, 2, 0, 4))
        self.assertEqual((plan["move"], plan["judge"]), (4, 1))
        self.assertNotIn(3, plan["eligible_move_slots"])
        self.assertFalse(plan["matching_category_override"])
        only = self.plan(2, 1, 1, [0, 4, 1], ["Cool", "Beauty", "unavailable", "unavailable"],
            hearts=(6, 0, -1, -1), unavailable_move_slots=(3, 4))
        self.assertEqual((only["move"], only["move_hearts"], only["judge"]), (2, 0, 1))
        self.assertTrue(only["zero_heart_only_option"])
        self.assertEqual(only["eligible_move_slots"], [2])
        only_recognized = self.plan(2, 1, 1, [0, 4, 1], ["Cool", "Beauty", "unreadable", "unreadable"],
            hearts=(6, 0, -1, -1))
        self.assertEqual((only_recognized["move"], only_recognized["move_hearts"]), (2, 0))
        self.assertTrue(only_recognized["zero_heart_only_option"])
        self.assertFalse(only_recognized["unrecognized_fallback"])
        for number, hearts in ((1, (0, 0, 0, 0)), (2, (6, 0, 0, 0))):
            rejected = self.plan(number, 1, 1, [0, 0, 0], hearts=hearts)
            self.assertFalse(rejected["selectable"])
            self.assertNotIn("steps", rejected)

    def test_unknown_strength_is_discarded_but_order_remains_mandatory(self):
        for value in (-1, 7, 1.5, "2", None):
            with self.subTest(hearts=value):
                plan = self.plan(2, 1, 1, [0, 4, 1], hearts=(6, value, 3, 4))
                self.assertEqual((plan["move"], plan["move_hearts"]), (4, 4))
                self.assertEqual(plan["recognized_move_slots"], [3, 4])
                self.assertFalse(plan["unrecognized_fallback"])
        missing = self.plan(2, 1, 1, [0, 4, 1], hearts=(6, 2, 3))
        self.assertEqual((missing["move"], missing["move_hearts"]), (3, 3))
        for position in (None, 0, 5, 1.5, "1"):
            with self.subTest(position=position), self.assertRaises(AssertionError):
                self.plan(2, 1, 1, [0, 4, 1], acting_position=position)

    def test_unrecognized_types_are_discarded_and_first_available_fallback_is_always_legal(self):
        move_types = ["Cool", "unreadable", "Cool", "Beauty"]
        for position, expected, judge in ((1, 3, 2), (4, 4, 1)):
            plan = self.plan(2, 1, 1, [0, 4, 1], move_types, hearts=(6, 6, 2, 5), acting_position=position)
            self.assertEqual((plan["move"], plan["judge"]), (expected, judge))
            self.assertEqual(plan["recognized_move_slots"], [3, 4])
        for previous in range(1, 5):
            with self.subTest(previous=previous):
                plan = self.plan(2, previous, 1, [2, 4, 1], ["unreadable"] * 4, hearts=(-1, -1, -1, -1))
                self.assertTrue(plan["unrecognized_fallback"])
                self.assertEqual(plan["move"], 2 if previous == 1 else 1)
                self.assertNotEqual(plan["move"], previous)
                self.assertEqual(plan["judge"], 3)
                self.assertEqual(len(plan["warnings"]), 1)
        only_previous_recognized = self.plan(2, 3, 1, [0, 4, 1],
            ["unreadable", "unreadable", "Cool", "unreadable"], hearts=(-1, -1, 6, -1))
        self.assertEqual((only_previous_recognized["move"], only_previous_recognized["judge"]), (1, 1))
        self.assertTrue(only_previous_recognized["unrecognized_fallback"])
        blocked = self.plan(2, 1, 1, [0, 4, 1], ["unreadable"] * 4,
            hearts=(-1, -1, -1, -1), unavailable_move_slots=(2, 4))
        self.assertEqual(blocked["move"], 3)

    def test_raw_heart_choices_keep_actual_move_and_judge_cursors_across_rounds(self):
        categories, hearts = ["Beauty", "Tough", "Smart", "Cool"], (5, 2, 4, 3)
        moves, positions = [1, 4, 1, 3], [1, 2, 3, 4]
        stars = [[0, 0, 0], [0, 4, 4], [0, 1, 2], [4, 0, 1]]
        menus = [["A", "A"], ["right", "down", "A", "right", "A"],
            ["left", "up", "A", "left", "A"], ["down", "A", "right", "A"]]
        previous_move, previous_judge = 1, 1
        for number, (move, position, counts, menu) in enumerate(zip(moves, positions, stars, menus), 1):
            with self.subTest(round=number):
                plan = self.plan(number, previous_move, previous_judge, counts, categories,
                    hearts=hearts, acting_position=position)
                self.assertEqual(plan["move"], move)
                self.assertEqual((plan["previous_move"], plan["previous_judge"]), (previous_move, previous_judge))
                self.perform(plan, menu)
                previous_move, previous_judge = plan["move"], plan["judge"]


class RankTimingChecks(unittest.TestCase):
    def test_main_fixed_profiles_cover_all_twenty_targets_and_check_only_first_feedback(self):
        planned = super_contest.chain_plan()
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
                        fixed_timing=profile, entry_pointer_x=64)
                    self.assertTrue(response["fixed_metrics"])
                    self.assertTrue(response["tolerant_inputs"])
                    self.assertEqual(response["feedback_policy"], "first_button_only")
                    self.assertEqual(response["required_excellent_judgments"], 0)
                    self.assertEqual(profile["cue_side_offset_pixels"], expected_spacing[category][RANKS.index(rank)])

    def test_main_capture_anchors_favor_early_arrival_for_every_speed_family(self):
        cases = (("Cool", "Normal", 1118), ("Cool", "Master", 1113), ("Beauty", "Hyper", 1189),
            ("Cute", "Master", 1094), ("Smart", "Hyper", 1108), ("Tough", "Master", 1100))
        for category, rank, first in cases:
            with self.subTest(category=category, rank=rank):
                profile = fixed_timing(category, rank)
                plan = rank_dance.stage5_plan(rank=rank, source_frame=1000, host_timeout=300,
                    fixed_timing=profile, entry_pointer_x=64)
                timing = plan["midpoint_timing"]
                self.assertEqual(timing["frames"][0], first)
                self.assertEqual(timing["accepted_pointer_pixels"], [128, 129])
                self.assertLessEqual(64 + (first - 1000) * profile["pixels_per_frame"], 127)
                self.assertEqual(len(timing["frames"]), 6)
                near_line = rank_dance.stage5_plan(rank=rank, source_frame=1000, host_timeout=300,
                    fixed_timing=profile, entry_pointer_x=127)
                self.assertGreater(near_line["midpoint_timing"]["frames"][0], 1000)


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
        return {"frame": frame, "epoch": 2, "emulator_paused": True, "observation": {"released": True, "image": {
            "path": str(path), "frame": frame, "epoch": 2, "source": "fresh_zero_frame_framebuffer",
            "capture_callback": "paused_lua_menu", "direct_read_frames": 0}}}

    def test_main_midpoints_preserve_early_captures_and_accept_both_line_pixels(self):
        for positions in (((1000, 128),), ((1000, 129),), ((1000, 126), (1004, 128))):
            with self.subTest(positions=positions), temporary_directory() as folder:
                work, samples = Path(folder), iter(positions)

                def command(action, *args):
                    return self.debug_capture(work, args[0], *next(samples)) if action == "capture" else {}

                client, record = SimpleNamespace(command=Mock(side_effect=command)), Mock()
                emulator = SimpleNamespace(controller=SimpleNamespace(live_deadline=None),
                    command=Mock(return_value={"stop_reason": "demo_midpoint"}))
                plan = rank_dance.stage5_plan(rank="Master", source_frame=700, host_timeout=300,
                    fixed_timing=fixed_timing("Cool", "Master"), entry_pointer_x=64, offset_frames=-7)
                with patch.object(rank_dance, "cue_positions", wraps=dance_response.cue_positions) as scan, \
                        patch.object(dance_response, "cue_positions", side_effect=AssertionError("Do not scan the same image twice")):
                    midpoint, planned, _ = rank_dance.prepare_response(emulator, client, work, 3, plan, record=record)
                self.assertEqual(scan.call_count, 2)
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
                    self.assertEqual(adjustments[0]["path"], captured[0]["image"]["path"])
                    self.assertTrue(Path(adjustments[0]["path"]).with_suffix(".png").is_file())

    def test_held_midpoint_keeps_original_deadlines_and_only_skips_expired_inputs(self):
        for elapsed in (3, 100):
            with self.subTest(elapsed=elapsed), temporary_directory() as folder:
                work, samples = Path(folder), iter(((1000, 127), (1000 + elapsed, 127)))
                client = SimpleNamespace(command=Mock(side_effect=lambda action, name:
                    self.debug_capture(work, name, *next(samples)) if action == "capture" else {}))
                emulator = SimpleNamespace(controller=SimpleNamespace(live_deadline=None),
                    command=Mock(return_value={"stop_reason": "demo_midpoint"}))
                plan = rank_dance.stage5_plan(rank="Hyper", source_frame=700, host_timeout=300,
                    fixed_timing=fixed_timing("Beauty", "Hyper"), entry_pointer_x=64)
                midpoint, planned, _ = rank_dance.prepare_response(emulator, client, work, 3, plan, record=Mock())
                expected = [1056, 1146, 1236, 1326]
                self.assertEqual([press["frame"] for press in planned["presses"]],
                    [frame for frame in expected if frame > midpoint["frame"]])
                self.assertEqual(planned["skipped_input_count"], int(elapsed == 100))
                self.assertEqual((planned["frame"], planned["pointer_x"], planned["response_end_frame"]),
                    (1000 + elapsed, 127, 1363))
                self.assertEqual(planned["counted_midpoint_anchor"], {"frame": 1000, "epoch": 2, "pointer_x": 127,
                    "source": "fresh_paused_midpoint_before_counted_correction"})
                self.assertEqual(sum(call.args[0] == "retime-midpoint" for call in client.command.call_args_list), 1)
                if elapsed == 100:
                    self.assertEqual(planned["skipped_inputs"][0]["reason"], "input_already_due")

    def test_main_late_unreadable_or_invalid_corrections_install_a_continuation(self):
        cases = ((((1000, 130),), 4, False), (((1000, 230),), 1, False), (((1000, None),), 0, False),
            (((1000, 127), (1002, 127)), 4, False), (((1000, 127), (1001, 127)), 0, False),
            (((1000, 127), (1002, 126)), 0, False), (((1000, 127),), 0, True))
        for positions, count, rejected in cases:
            with self.subTest(positions=positions), temporary_directory() as folder:
                work, samples = Path(folder), iter(positions)
                client = SimpleNamespace(command=Mock(side_effect=lambda action, name:
                    self.debug_capture(work, name, *next(samples)) if action == "capture" else
                    {"recovery_required": True, "warning": "Midpoint pixels changed"} if rejected and action == "retime-midpoint" else {}))
                emulator = SimpleNamespace(controller=SimpleNamespace(live_deadline=None),
                    command=Mock(return_value={"stop_reason": "demo_midpoint"}))
                plan = rank_dance.stage5_plan(rank="Master", source_frame=700, host_timeout=300,
                    fixed_timing=fixed_timing("Cool", "Master"), entry_pointer_x=64, offset_frames=-7)
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
                if rejected:
                    adjustment = json.loads((work / "midpoint-adjustments.json").read_text(encoding="utf-8"))["adjustments"][0]
                    self.assertFalse(adjustment["accepted"])
                    self.assertEqual(emulator.command.call_count, 1)

    def test_incomplete_views_keep_confirmed_pairs_and_install_the_plan_silently(self):
        image = self.image(left=("A", None, "X", "Y"), right=("A", "B", None, "Y"))
        with temporary_directory() as folder:
            work = Path(folder)
            image.save(work / "midpoint-source.png")
            midpoint = {**self.midpoint, "image": {"path": str(work / "midpoint-source.png")},
                "png": str(work / "midpoint-source.png")}
            emulator = SimpleNamespace(controller=SimpleNamespace(live_deadline=None, remaining=lambda: None),
                command=Mock(return_value={"stop_reason": "demo_midpoint"}))
            client, record, output = Mock(command=Mock(return_value={})), Mock(), io.StringIO()
            options = rank_dance.stage5_plan(rank="Master", source_frame=700, host_timeout=300,
                fixed_timing=fixed_timing("Cool", "Master"), entry_pointer_x=64)
            with patch("contest.rank_dance.capture_result", return_value=midpoint), redirect_stdout(output):
                _, planned, reading = rank_dance.prepare_response(emulator, client, work, 3, options, record=record)
            client.command.assert_called_with("plan", (work / "response-plan.lua").as_posix())
            self.assertEqual(output.getvalue(), "")
            self.assertFalse((work / "demo-frames.bin").exists())
            self.assertFalse(reading["continuous_demo_observation"])
        self.assertEqual([press["button"] for press in planned["presses"]], ["A", "Y"])
        self.assertEqual(planned["skipped_input_count"], 2)
        accepted = planned["cues"]["accepted"]
        self.assertEqual([cue["source_side"] for cue in accepted], ["left", "left"])
        self.assertTrue(all(cue["cross_checked"] for cue in accepted))
        self.assertFalse(any(50 <= x < 66 and 168 <= y < 184 for x, y, *_ in planned["pixel_points"]))
        self.assertFalse(any(200 <= x < 216 and 168 <= y < 184 for x, y, *_ in planned["pixel_points"]))

    def test_main_conflict_preferences_missing_sides_and_confirmed_only_partial_reads(self):
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
                planned = dance_response.response_plan(self.midpoint, self.image(left=left, right=right, side_offset=offset),
                    rank="Normal" if count == 3 else "Master", expected_cues=count,
                    speed=0.5, hold_frames=2, offset_frames=-7, dance=3, side_offset_pixels=offset)
                self.assertEqual([press["button"] for press in planned["presses"]], expected)
                self.assertEqual(planned["skipped_input_count"], count - len(expected))
                for cue in planned["cues"]["accepted"]:
                    self.assertTrue(all(reading["button"] == cue["button"] for reading in cue["evidence"].values()))
                    self.assertEqual(cue["response_x"] - cue["demo_x"], offset)
                    self.assertTrue(cue["pixel_points"])

    def test_main_excess_pairs_use_demo_prefix_and_response_suffix(self):
        positions = (16, 32, 48, 64, 80, 112)
        left = [{"x": x, "button": button} for x, button in zip(positions, ("A", "B", "X", "Y", "A", "B"))]
        right = [{**cue, "x": cue["x"] + 128} for cue in left]
        for count in (3, 4):
            for a, b, expected in ((left, right, list(positions[:count - 2] + positions[-2:])),
                    (left, right[:count], list(positions[:count])),
                    (left[:count], right, list(positions[:count])), (left, right[:1], [16])):
                with self.subTest(count=count, left=len(a), right=len(b)):
                    selected = dance_response.select_cues(a, b, expected_cues=count, side_offset_pixels=128)
                    self.assertEqual([cue["demo_x"] for cue in selected["accepted"]], expected)
                    self.assertTrue(all(cue["cross_checked"] for cue in selected["accepted"]))
                    if len(expected) == count:
                        self.assertEqual([cue["source_side"] for cue in selected["accepted"]],
                            ["left"] * (count - 2) + ["right"] * 2)

    def test_main_feedback_prunes_overlaps_and_accepts_an_empty_response(self):
        profile = fixed_timing("Cool", "Master")
        for frames, expected in (([1010, 1015, 1050, 1095], [1010, 1050]), ([], [])):
            with self.subTest(frames=frames):
                planned = {"dance": 1, "frame": 1000, "response_end_frame": 1100, "hold_frames": 2,
                    "presses": [{"frame": frame} for frame in frames], "expected_inputs": 4, "skipped_inputs": [],
                    "cues": {"accepted": [{"pixel_points": [[index, 1, 1, 2, 3]]} for index in range(len(frames))]}}
                feedback = rank_dance.feedback_schedule(planned, delay_frames=11, rank_timing=profile)
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
        for dx, dy in offsets[:len(sprite.points) * 3 // 10 + extra_miss]:
            pixels[dx, dy] = (0, 0, 0)
        return pixels, sprite

    def test_cue_match_threshold_accepts_70_percent_and_rejects_lower(self):
        for side in ("left", "right"):
            with self.subTest(side=side):
                pixels, sprite = self.damaged_cue(side)
                matched = _match(pixels, 0, 0, sprite)
                self.assertIsNotNone(matched)
                self.assertGreaterEqual(matched["match_fraction"], 0.70)
                pixels, sprite = self.damaged_cue(side, extra_miss=1)
                self.assertIsNone(_match(pixels, 0, 0, sprite))


class MasterLifecycleChecks(unittest.TestCase):
    def runtime(self, work, settings):
        controller = SimpleNamespace(ROM=ROOT / "prepared.nds", live_deadline=None, remaining=lambda: 300, session=lambda: {"pid": 7, "window": 11},
            set_pause=Mock(), screenshot=Mock(), paused=lambda _: True,
            status=lambda: {"frame": 100, "buttons": "", "stylus_touch": False})
        runtime = ContestRuntime("lifecycle-check", work, settings, time.monotonic(), controller=controller)
        runtime.emulator = Mock()
        runtime.emulator.set_frame_limit.side_effect = lambda enabled: {"frame_limit": enabled}
        runtime.client = Mock()
        runtime.owned = runtime.attached = True
        return runtime

    def test_main_disabled_saves_and_all_quick_loads_are_blocked_before_native_io(self):
        for debug in (0, 1):
            with self.subTest(debug=debug), temporary_directory() as folder:
                controller = SimpleNamespace(remaining=Mock(return_value=300), live_deadline=None)
                runtime = ContestRuntime("guard-check", Path(folder), super_contest.chain_plan(debug=debug),
                    time.monotonic(), controller=controller)
                self.assertIsInstance(runtime.emulator.states, TimedSlots)
                with patch.object(StateSlots, "save") as save, patch.object(StateSlots, "load") as load:
                    with self.assertRaisesRegex(RuntimeError, "Quick-loads are disabled"):
                        runtime.emulator.states.load(5)
                    if debug == 0:
                        with self.assertRaisesRegex(RuntimeError, "Quick saves are disabled"):
                            runtime.emulator.states.save(6)
                        runtime.client = Mock()
                        with self.assertRaisesRegex(RuntimeError, "Quick saves are disabled"):
                            runtime.acknowledge({"checkpoint": {"save_required": True}}, slot=6)
                        runtime.client.acknowledge.assert_not_called()
                        self.assertEqual(runtime.originals, {})
                    save.assert_not_called()
                    load.assert_not_called()
                    controller.remaining.assert_not_called()

    def test_main_debug_zero_cleanup_does_not_save_on_success_failure_timeout_or_close(self):
        for error, closed in ((None, False), ("stopped", False), ("timeout", False), ("emulator closed", True)):
            with self.subTest(error=error, closed=closed), temporary_directory() as folder:
                runtime = self.runtime(Path(folder), super_contest.chain_plan(debug=0))
                result = runtime.finalize(error=error, emulator_closed=closed)
                self.assertEqual(result["errors"], [])
                self.assertEqual(runtime.originals, {})
                runtime.emulator.states.save.assert_not_called()
                runtime.emulator.states.load.assert_not_called()
                runtime.emulator.states.path.assert_not_called()
                if error and not closed:
                    self.assertIsNone(runtime.client.stop.call_args.kwargs["slot"])

    def test_poll_detects_a_closed_emulator(self):
        alive, windows = Mock(return_value=True), Mock(return_value=True)
        controller = TimedController(windows_api=SimpleNamespace(process_alive=alive,
            USER32=SimpleNamespace(IsWindow=windows)))
        controller.live_session = {"pid": 7, "window": 11, "lua_window": 12}
        deadline = time.monotonic() + 300
        self.assertTrue(controller.waiting(deadline))
        alive.return_value = False
        with self.assertRaisesRegex(SystemExit, "emulator was closed"):
            controller.waiting(deadline)

    def test_manual_emulator_close_restores_slot_storage_without_native_commands(self):
        with temporary_directory() as folder:
            work = Path(folder)
            previous, native = work / "previous.ds9", work / "native.ds9"
            previous.write_bytes(b"original slot")
            native.write_bytes(b"run baseline")
            runtime = self.runtime(work, super_contest.chain_plan(debug=1))
            runtime.originals[9] = previous
            runtime.emulator.states.path.return_value = native
            result = runtime.finalize(error="The controlled emulator was closed.", emulator_closed=True)
            self.assertEqual(native.read_bytes(), previous.read_bytes())
            self.assertEqual(result["errors"], [])
            self.assertTrue(result["emulator_closed"] and result["temporary_slot_9_restored"])
            self.assertIsNone(result["emulator_paused"])
            runtime.controller.set_pause.assert_not_called()
            runtime.client.command.assert_not_called()
            runtime.emulator.stop_movie.assert_not_called()
            self.assertEqual(runtime.lifecycle["terminal_cleanups"], 1)

    def test_main_and_debug_speed_policies_and_terminal_cleanup(self):
        for debug in (0, 1):
            with self.subTest(debug=debug), temporary_directory() as folder:
                diagnostic = debug == 1
                settings = super_contest.chain_plan(debug=debug)
                runtime = self.runtime(Path(folder), settings)
                for phase in ("run", "run", "dressing", "dressing", "run"):
                    runtime.set_speed(phase)
                self.assertEqual([call.args[0] for call in runtime.emulator.set_frame_limit.call_args_list], [False, False, False])
                self.assertEqual(settings["native_osd_settle_host_seconds"], 3)
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
    def collection(self, parent, *, debug=super_contest.debug, stop=False, ribbon_results=None):
        settings = super_contest.chain_plan(debug=debug)
        instances = []
        ribbon_results = None if ribbon_results is None else iter(ribbon_results)

        class LocalRuntime:
            def __init__(self, run_id, work, settings, started):
                self.run_id, self.work, self.settings = run_id, work, settings
                self.total_deadline = started + settings["total_timeout_seconds"]
                self.controller = SimpleNamespace(live_deadline=self.total_deadline, session=lambda: {"pid": 7},
                    log_lua=self.log_lua)
                self.originals, self.accepted_slots = {}, {}
                self.bag_context = {"opened_once": True, "cursor_position": 7}
                self.unavailable_move_slots = [2, 4]
                self.owned_bits, self.saves, self.captures = set(range(18)), [], []
                self.preparations = []
                self.target, self.boundary, self.party = None, None, None
                self.frame, self.epoch, self.checkpoints, self.finalizations = 100, 0, 0, 0
                self.missed, self.active_work = False, work
                self.overworld = {"points": [[88, 44, 77, 81, 142]]}
                self.lua_ready, self.startup_messages = False, []
                self.folder_exclusions = (persistent_evidence, other_scratch, lock_file)
                instances.append(self)

            def confirm_ownership(self):
                return None

            def prepare_fresh_ownership(self, session):
                assert session is None
                assert not self.work.exists() and old_capture.exists()

            def setup(self):
                assert all(folder.is_dir() for folder in older_runs)
                assert not (self.work / "startup-cleanup.json").exists()
                self.json("overworld-reference.json", self.overworld)
                (self.work / "user-photo.png").write_bytes(b"user work")
                self.lua_ready = True

            def log_lua(self, text):
                assert self.lua_ready
                if not self.startup_messages:
                    assert all(folder.is_dir() for folder in older_runs)
                else:
                    assert not any(folder.exists() for folder in older_runs)
                self.startup_messages.append(text)

            def check(self):
                return self.controller.live_deadline - time.monotonic()

            def begin_visit(self, stage, visit):
                assert not any(folder.exists() for folder in older_runs)
                assert all(path.is_file() for path in self.folder_exclusions)
                self.stage, self.stage_started = stage, time.monotonic()
                self.stage_deadline = min(self.total_deadline, self.stage_started + self.settings["stage_timeout_seconds"])
                self.controller.live_deadline = self.stage_deadline
                self.active_work = self.work / "visits" / f"s{stage}-{visit:03d}"
                self.active_work.mkdir(parents=True)
                return self.active_work

            def json(self, name, value):
                write_json(self.active_work / name, value)

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
                    "save_required": self.settings["debug"] == 1 and stage in (2, 4, 5, 6),
                    "snapshot": {"target": self.target, "party": self.party}}
                slot = stage
                state = self.work / "checkpoints" / f"{self.checkpoints:04d}-stage-{stage}.ds{slot % 10}"
                saved = None
                if checkpoint["save_required"]:
                    state.write_bytes(b"DeSmuME SState accepted")
                    saved = {"retained_path": str(state), "slot": slot}
                    self.accepted_slots[slot] = state
                    self.saves.append({"slot": slot, "decision": decision, "frame": self.frame})
                receipt = state.with_suffix(".json")
                write_json(receipt, {"completed": checkpoint, "host_savestate": saved})
                self.boundary = {"checkpoint": checkpoint, "save_state": saved, "receipt": str(receipt)}
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
                    "lifecycle": {"lua_attachments": 1, "checkpoint_engines": 1, "terminal_cleanups": self.finalizations}}

        def section(runtime, **_):
            if runtime.stage == 2:
                runtime.preparations.append(deepcopy(runtime.boundary["checkpoint"]))
                if len(runtime.owned_bits) > 18:
                    assert all(image.exists() for image in runtime.captures)
                    assert all(state.exists() for state in runtime.accepted_slots.values())
                    assert runtime.overworld["points"] == [[88, 44, 77, 81, 142]]
                bit = next(bit for bit in range(20) if bit not in runtime.owned_bits)
                runtime.target = {"category": "Tough", "rank": "Hyper" if bit == 18 else "Master", "ribbon_bit": bit, "party_slot": 1}
            if runtime.stage != 6:
                return runtime.emit(runtime.stage)
            if stop:
                (runtime.active_work / "failure.png").write_bytes(b"stopped evidence")
                raise TimeoutError("Configured host deadline reached")
            if ribbon_results is None:
                acquired = runtime.missed
                runtime.missed = True
            else:
                acquired = next(ribbon_results)
            if acquired:
                runtime.owned_bits.add(runtime.target["ribbon_bit"])
            return runtime.emit(6, decision="hit" if acquired else "miss")

        (parent / "tmp" / "contest").mkdir(parents=True)
        old_work = parent / "tmp" / "contest" / "old-run"
        older_runs = (old_work, old_work.with_name("failed-run"), old_work.with_name("interrupted-run"))
        for folder in older_runs:
            folder.mkdir()
            (folder / "stage3.png").write_bytes(b"stale capture")
        old_capture = old_work / "stage3.png"
        write_json(old_work / "runner-result.json", {"outcome": "pass"})
        write_json(older_runs[1] / "runner-result.json", {"outcome": "fail"})
        (older_runs[2] / "runner-result.json").write_text("{", encoding="utf-8")
        persistent_evidence = parent / "contest" / "debug" / "kept-run" / "stage3.png"
        persistent_evidence.parent.mkdir(parents=True)
        persistent_evidence.write_bytes(b"persistent evidence")
        other_scratch = parent / "tmp" / "other-tool" / "capture.png"
        other_scratch.parent.mkdir()
        other_scratch.write_bytes(b"other scratch")
        lock_file = old_work.parent / "active-host.lock"
        lock_file.write_bytes(b"0")
        with patch("contest.super_contest.ROOT", parent), patch("contest.super_contest.ContestRuntime", LocalRuntime), \
                patch("contest.artifacts.ROOT", parent), \
                patch.dict("contest.sections.RUNNERS", {stage: section for stage in range(1, 7)}, clear=True), \
                patch("builtins.print"):
            result = super_contest._run("collection-check", settings, time.monotonic())
        return result, instances[0]

    def test_collection_routes_misses_and_keeps_current_evidence_through_success(self):
        for debug in (0, 1):
            with self.subTest(debug=debug), temporary_directory() as folder:
                result, runtime = self.collection(Path(folder), debug=debug)
                self.assertEqual(result["outcome"], "pass")
                self.assertEqual([item["ribbon_bit"] for item in result["ribbons_acquired"]], [18, 19])
                self.assertEqual((result["initial_owned_ribbon_count"], result["owned_ribbon_count"]), (18, 20))
                self.assertEqual(runtime.epoch, 0)
                retry_source = runtime.preparations[1]
                self.assertEqual((retry_source["checkpoint"], retry_source["decision"], retry_source["epoch"]), ("stage-6-act", "miss", 0))
                self.assertEqual(retry_source["snapshot"]["party"]["owned_ribbon_bits"], list(range(18)))
                self.assertEqual([item["stage"] for item in result["visits"]], [1] + [2, 3, 4, 5, 6] * 3)
                self.assertEqual((result["contest_entries"], result["completed_acting_attempts"]), (3, 3))
                self.assertEqual([saved["slot"] for saved in runtime.saves], [2, 4, 5, 6] * 3 if debug else [])
                self.assertEqual([saved["decision"] for saved in runtime.saves if saved["slot"] == 6],
                    ["miss", "hit", "hit"] if debug else [])
                context = json.loads((runtime.work / "collection-context.json").read_text(encoding="utf-8"))
                self.assertEqual(context["unavailable_move_slots"], [2, 4])
                self.assertEqual(runtime.finalizations, 1)
                self.assertTrue(all(path.exists() for path in runtime.captures))
                self.assertTrue((runtime.work / "user-photo.png").exists())
                self.assertTrue(all(path.is_file() for path in runtime.folder_exclusions))
                self.assertEqual(len(result["cleanup_manifests"]), 1)
                cleanup = json.loads(Path(result["cleanup_manifests"][0]).read_text(encoding="utf-8"))
                self.assertEqual(cleanup["status"], "complete")
                self.assertEqual(cleanup["removed_folders"], ["failed-run", "interrupted-run", "old-run"])
                self.assertEqual(len(runtime.startup_messages), 3)

    def test_five_ribbon_misses_warn_once_per_streak_and_keep_retrying(self):
        attempts = [False] * 6 + [True] + [False] * 6 + [True]
        with temporary_directory() as folder:
            result, runtime = self.collection(Path(folder), ribbon_results=attempts)
            self.assertEqual(result["outcome"], "pass")
            self.assertTrue(result["all_ribbons_owned"])
            self.assertEqual(result["completed_acting_attempts"], len(attempts))
            self.assertEqual([item["ribbon_bit"] for item in result["ribbons_acquired"]], [18, 19])
            self.assertEqual(result["consecutive_ribbon_misses"], 0)
            events = [json.loads(line) for line in
                (runtime.work / "runner-events.jsonl").read_text(encoding="utf-8").splitlines()]
            results = [event for event in events if event["phase"] == "ribbon_result"]
            self.assertEqual([event["acquired"] for event in results], attempts)
            self.assertEqual([event["consecutive_ribbon_misses"] for event in results],
                [1, 2, 3, 4, 5, 6, 0, 1, 2, 3, 4, 5, 6, 0])
            warnings = [event for event in events if event["phase"] == "ribbon_miss_warning"]
            self.assertEqual([event["completed_acting_attempts"] for event in warnings], [5, 12])
            self.assertEqual([event["consecutive_ribbon_misses"] for event in warnings], [5, 5])
            messages = [text for text in runtime.startup_messages if text.startswith("Contest: WARN -")]
            self.assertEqual(messages, [event["message"] for event in warnings])
            for message in messages:
                self.assertIn("5 consecutive contests", message)
                self.assertIn("good contest stats and suitable moves?", message)
                self.assertIn("Retrying automatically.", message)
            self.assertEqual(len([event for event in events if event["phase"] == "retry_contest"]), 12)
            context = json.loads((runtime.work / "collection-context.json").read_text(encoding="utf-8"))
            self.assertEqual(context["consecutive_ribbon_misses"], 0)
            self.assertEqual(runtime.finalizations, 1)

    def test_timeout_keeps_stopped_images_and_debug_only_accepted_states(self):
        for debug in (0, 1):
            with self.subTest(debug=debug), temporary_directory() as folder:
                result, runtime = self.collection(Path(folder), debug=debug, stop=True)
                self.assertEqual((result["outcome"], result["error"]), ("timeout", "timeout"))
                self.assertEqual(result["ribbons_acquired"], [])
                self.assertEqual(runtime.finalizations, 1)
                self.assertTrue((runtime.active_work / "failure.png").exists())
                self.assertTrue(all(path.exists() for path in runtime.captures))
                self.assertTrue(all(path.is_file() for path in runtime.folder_exclusions))
                if debug:
                    self.assertTrue(Path(result["last_save_state"]).exists())
                    self.assertEqual(runtime.saves[-1]["slot"], 5)
                else:
                    self.assertIsNone(result["last_save_state"])
                    self.assertEqual(runtime.saves, [])
                self.assertEqual(len(result["cleanup_manifests"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
