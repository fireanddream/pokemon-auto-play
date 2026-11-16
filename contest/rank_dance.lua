-- Rank-selected six-response adapter using the shared checkpoint and pixel helpers.
local config, runtime = ...
config = config or {}
local stage5 = config.stage == 5
local record_feedback = stage5 and config.feedback_policy == "record_all_buttons"
local all_feedback = not stage5 or config.feedback_policy == "all_buttons" or record_feedback
local scheduled = stage5 and config.observation_policy == "scheduled"
local fixed_metrics = stage5 and config.fixed_metrics == true
local midpoint_diagnostics = stage5 and config.midpoint_diagnostics == true
local warn_first_feedback = stage5 and not all_feedback and config.first_feedback_policy == "warn_and_continue"
local scheduled_feedback = record_feedback or warn_first_feedback and fixed_metrics and scheduled
local START = stage5 and "rank-dance-start" or "start"
local FINISH = stage5 and "rank-dance-finish" or "finish"
local OFFSET = "rank-dance-offset"
local directory = assert(debug.getinfo(1, "S").source:sub(2):gsub("\\", "/"):match("^(.*)/"))
local root = dofile(directory .. "/resource_root.lua")
local bridge_io = dofile(root .. "/lua/bridge_io.lua")
local master = dofile(root .. "/lua/checkpoint_master.lua")
local platinum = dofile(root .. "/lua/platinum.lua")
local json = dofile(root .. "/lua/json.lua")
local pixels = dofile(directory .. "/dance_pixels.lua")
local screen_checks = stage5 and dofile(directory .. "/screen_checks.lua") or nil
local mailbox_directory = runtime and runtime.mailbox_directory or root .. "/tmp"
local mailbox = bridge_io.new(mailbox_directory)
local checkpoint_mailbox = bridge_io.new(mailbox_directory, "checkpoint-")
local checkpoint_id, idle = checkpoint_mailbox:previous_id(), false
local engine, latest, pending, work, current, active, plan, rank
local initialized, first_feedback_verified = false, false
local first_feedback, lead_dances, transition, acting_capture, acting_review
local demo_bytes, demo_index, demo_offset, demo_last, response_events
local inputs, completed, total_inputs, total_judgments, total_excellent = 0, 0, 0, 0, 0
local total_skipped_inputs, response_skips = 0, setmetatable({}, bridge_io.array_metatable)
local retain_judgment, retain_first_skip
local last_feedback, response_complete, halted, captures = nil, false, false, 0
local midpoint_frames, midpoint_source
local judgments = setmetatable({}, bridge_io.array_metatable)
local state = runtime and runtime.state or {protocol = 1, checkpoint_protocol = 1, bridge = "pt-rank-dance",
    game_code = emu.gamecode(), frame = emu.framecount(), phase = "ready", buttons = "",
    stylus_touch = false, command_id = mailbox:previous_id(), command_done = true, error = ""}
local adapter = runtime and runtime.adapter or dofile(root .. "/lua/checkpoint_desmume.lua").new({
    pause = function() assert(idle, "Host-confirmed native pause is required") end,
    is_paused = function() return idle end,
})
engine, work = runtime and runtime.engine, config.work
rank = config.target and config.target.rank or config.rank
local cue_count = rank == "Normal" and 3 or 4
local resolved_cue_offset = config.rank_timing and config.rank_timing.cue_side_offset_pixels

local function release()
    bridge_io.apply_buttons(nil); stylus.set({touch = false})
    state.buttons, state.stylus_touch = "", false
end
adapter.release = release

local function warn(message)
    local text = string.format("Dancing: WARN - Dance %d: %s; continuing.",
        current and current.index or completed + 1, tostring(message):gsub("[|\r\n]", " "))
    state.dance_warning = text
    print(text)
    return text
end

local function write(path, value, mode)
    local file = assert(io.open(path, mode or "w"))
    assert(file:write(value)); assert(file:close())
end

local function read(path)
    local file = assert(io.open(path, "rb"))
    local value = assert(file:read("*a")); assert(file:close())
    return value
end

local function close_files()
    local files = {demo_bytes or false, demo_index or false, response_events or false}
    demo_bytes, demo_index, response_events = nil, nil, nil
    for _, file in ipairs(files) do
        if file then
            local ok, closed = pcall(file.close, file)
            if not ok or not closed then warn("Capture stream could not be closed") end
        end
    end
end

local function calculate_midpoints(first)
    local frames = {}
    for index = 0, 5 do
        frames[index + 1] = first + index * config.rank_timing.cycle_frames
            + math.floor(index / 2) * config.rank_timing.extra_switch_frames
    end
    return frames
end

local function planned_count()
    return plan and #plan.presses or 0
end

local function runtime_skip_count()
    local count = 0
    for _, press in ipairs(plan and plan.presses or {}) do
        if press.skipped then count = count + 1 end
    end
    return count
end

local function skip_press(index, reason)
    local press = plan.presses[index]
    if press.started or press.skipped then return end
    press.skipped, press.skip_reason = true, reason
    response_skips[#response_skips + 1] = {index = index, input_index = press.input_index, button = press.button,
        response_x = press.response_x, scheduled_frame = press.frame, frame = adapter.frame(), reason = reason}
end

local function checks_feedback(index)
    local press = plan and plan.presses[index]
    return press ~= nil and (all_feedback or stage5 and current.index == 1
        and (press.input_index or index) == 1)
end

local function first_judgment_count()
    return first_feedback and first_feedback.skipped and 0 or 1
end

local function completed_count()
    return response_complete and current and math.max(completed, current.index) or completed
end

local function observation(frame, raw)
    local code, excellent = emu.gamecode(), 0
    for _, judgment in ipairs(judgments) do
        if judgment.label == "Excellent" then excellent = excellent + 1 end
    end
    return {frame = frame, epoch = adapter.epoch(), game = platinum.game(code),
        bar = raw and pixels.bar(raw), rank = rank, rank_source = stage5 and "accepted_contest_target" or "user_prepared_native_slot",
        target = config.target, source_context = config.source_context, rank_timing = config.rank_timing,
        pointer_calibration = config.pointer_calibration, observation_policy = config.observation_policy,
        rank_profile_matches_target = stage5 and config.rank_timing and config.target
            and config.rank_timing.rank == config.target.rank and rank == config.target.rank
            and (not fixed_metrics or config.rank_timing.category == config.target.category) or false,
        offset_check = first_feedback, lead_dances = lead_dances, transition = transition,
        midpoint_timing = current and {source = current.midpoint_source, expected_frame = current.midpoint_frame,
            entry_frame = config.source_frame, entry_pointer_x = config.midpoint_timing and config.midpoint_timing.entry_pointer_x},
        released = state.buttons == "" and not state.stylus_touch,
        trial = {dance = current and current.index or 0, completed_dances = completed_count(), acknowledged_dances = completed,
            cues_per_dance = cue_count, required_response_inputs = 6 * cue_count,
            inputs = inputs, judgments = judgments, judgment_count = #judgments,
            planned_input_count = planned_count(), runtime_skipped_inputs = runtime_skip_count(),
            skipped_input_count = cue_count - planned_count() + runtime_skip_count(),
            tolerant_inputs = true,
            response_started = current and current.start_acknowledged == true or false,
            total_skipped_inputs = total_skipped_inputs, skipped_inputs = response_skips,
            presses = plan and plan.presses, conflict_count = plan and plan.cues and #(plan.cues.conflicts or {}) or 0,
            excellent_count = excellent, miss_count = #judgments - excellent, complete = response_complete,
            unreadable_demo_frames = current and current.unreadable_demo_frames or 0,
            total_inputs = total_inputs, total_judgments = total_judgments, total_excellent = total_excellent,
            total_input_count = total_inputs, total_judgment_count = total_judgments, total_excellent_count = total_excellent,
            feedback_policy = record_feedback and "record_all_buttons" or all_feedback and "all_buttons" or "first_button_only",
            total_miss_count = total_judgments - total_excellent,
            required_judgments_so_far = all_feedback and total_inputs or first_judgment_count(),
            required_first_feedback_judgments = first_judgment_count(),
            feedback_policy_met = all_feedback and #judgments == inputs
                or not all_feedback and first_feedback_verified and total_judgments == first_judgment_count(),
            first_feedback_verified = first_feedback_verified,
            first_feedback_policy = "warn_and_continue",
            first_feedback_accepted = first_feedback ~= nil,
            released = state.buttons == "" and not state.stylus_touch,
            acting_endpoint_met = transition ~= nil and transition.completed and frame == transition.end_frame,
            at_authorized_midpoint = current ~= nil and current.midpoint_frame ~= nil and frame == current.midpoint_frame,
            sequence_valid = current ~= nil and current.index == completed + 1}}
end

local function publish(value)
    state.observation = value or observation(state.frame)
    state.checkpoint_epoch = adapter.epoch()
    state.checkpoint_visit = latest and latest.visit
    state.checkpoint_status = latest and latest.status
    mailbox:publish(state)
end

local function capture(name, folder)
    assert(idle and work and name:match("^[%w_-]+$"), "A configured paused capture is required")
    local frame, raw = adapter.frame(), gui.gdscreenshot()
    local path = (folder or current and current.work or work) .. "/" .. name .. ".gd"
    write(path, raw, "wb"); write(mailbox_directory .. "/screen.gd", raw, "wb")
    state.screen_frame = frame
    return {path = path, frame = frame, epoch = adapter.epoch(), source = "fresh_zero_frame_framebuffer",
        capture_callback = "paused_lua_menu", direct_read_frames = 0, retained = true}, raw
end

local function colors_match(raw, points)
    if type(points) ~= "table" or #points == 0 then return false end
    for _, point in ipairs(points) do
        local position = 13 + (point[2] * 256 + point[1]) * 4
        local r, g, b = raw:byte(position, position + 2)
        if r ~= point[3] or g ~= point[4] or b ~= point[5] then return false end
    end
    return true
end

local function snapshot(frame)
    captures = captures + 1
    local image, raw = capture(string.format("checkpoint-%04d", captures))
    if plan and frame == plan.frame and not response_complete then
        local bar = pixels.bar(raw)
        local bounds = bar and bar.position_bounds
        local pointer = plan.pointer_x
        local ready = pointer and pointer >= (midpoint_diagnostics and 8 or 128) and pointer <= 247
            and bounds and bounds[1] <= pointer and pointer <= bounds[2]
        for index, cue in ipairs(plan.cues.accepted) do
            if not ready then skip_press(index, "midpoint_position_unreadable_or_early")
            elseif not colors_match(raw, cue.pixel_points or plan.pixel_points) then
                skip_press(index, "cue_pixels_changed_before_input")
            end
        end
    end
    local cues = plan and plan.cues and plan.cues.accepted
    local cue_side_offset = plan and plan.cues and plan.cues.side_offset_pixels or resolved_cue_offset
    local value, consistent = observation(frame, raw), plan ~= nil
        and #plan.presses <= cue_count
        and cues ~= nil and #cues == #plan.presses
    local visible_pairs = 0
    consistent = consistent and type(cue_side_offset) == "number" and cue_side_offset > 0
        and cue_side_offset < 256 and cue_side_offset == math.floor(cue_side_offset)
        and (not fixed_metrics or cue_side_offset == config.rank_timing.cue_side_offset_pixels)
    if plan then
        for index, press in ipairs(plan.presses) do
            local cue = cues and cues[index]
            local due = plan.frame + math.floor((press.response_x - plan.pointer_x) / plan.speed + 0.5) + plan.offset_frames
            local following = plan.presses[index + 1] and plan.presses[index + 1].frame or plan.predicted_end_frame
            if not cue or cue.button ~= press.button or cue.response_x ~= press.response_x
                or cue.response_x ~= cue.demo_x + cue_side_offset
                or press.frame ~= due or not press.skipped and (press.frame <= plan.frame
                    or press.frame + plan.hold_frames >= following) then
                consistent = false
            end
            if plan.cues and plan.cues.side_offset_pixels then
                local evidence = cue and cue.evidence or {}
                local left, right = evidence.left, evidence.right
                if not left and not right then consistent = false end
                if left and (left.x ~= cue.demo_x or left.button ~= cue.button) then consistent = false end
                if right and (right.x ~= cue.response_x or right.button ~= cue.button) then consistent = false end
                if left and right and right.x - left.x == cue_side_offset then visible_pairs = visible_pairs + 1 end
            end
        end
        if plan.cues and plan.cues.side_offset_pixels then
            local source = plan.cues.side_offset_source
            consistent = consistent and (source == "fresh_paired_cue_centers" and visible_pairs >= 2
                or source == "configured_cue_spacing" and cue_side_offset == resolved_cue_offset)
        end
    end
    value.image = image
    value.trial.plan_consistent = consistent
    value.trial.rank_cues_visible = cues ~= nil and #cues == cue_count and colors_match(raw, plan.pixel_points) or false
    value.trial.planned_cues_bound = plan ~= nil
    for index, cue in ipairs(cues or {}) do
        if not plan.presses[index].skipped and not colors_match(raw, cue.pixel_points or plan.pixel_points) then
            value.trial.planned_cues_bound = false
        end
    end
    value.trial.cue_sources = setmetatable({}, bridge_io.array_metatable)
    value.trial.cue_side_offset_pixels = cue_side_offset
    for _, cue in ipairs(cues or {}) do
        value.trial.cue_sources[#value.trial.cue_sources + 1] = {demo_x = cue.demo_x, button = cue.button,
            source_side = cue.source_side, fallback = cue.fallback, cross_checked = cue.cross_checked,
            position_source = cue.position_source}
    end
    value.trial.fresh_visual_binding = plan ~= nil and plan.frame == frame and plan.epoch == adapter.epoch()
    local bounds = value.bar and value.bar.position_bounds
    value.trial.midpoint_within_line = plan ~= nil and type(plan.pointer_x) == "number" and plan.pointer_x >= 128 and plan.pointer_x <= 129
         and bounds ~= nil and bounds[1] <= plan.pointer_x and plan.pointer_x <= bounds[2] or false
    value.trial.midpoint_pointer_bound = plan ~= nil and type(plan.pointer_x) == "number" and plan.pointer_x >= 8 and plan.pointer_x <= 247
        and plan.pointer_x == math.floor(plan.pointer_x) and bounds ~= nil
        and bounds[1] <= plan.pointer_x and plan.pointer_x <= bounds[2] or false
    value.trial.midpoint_diagnostics = midpoint_diagnostics
    value.trial.midpoint_policy_met = value.trial.midpoint_within_line
        or midpoint_diagnostics and value.trial.midpoint_pointer_bound
        or (planned_count() == runtime_skip_count()
            or value.trial.midpoint_pointer_bound and plan.pointer_x >= 128)
    value.trial.midpoint_adjustment_count = current and current.midpoint_adjustments or 0
    value.trial.before_first_input = plan ~= nil
    for _, press in ipairs(plan and plan.presses or {}) do
        if not press.skipped and frame >= press.frame then value.trial.before_first_input = false end
    end
    value.trial.at_response_endpoint = plan ~= nil and frame >= plan.response_end_frame
    if record_feedback then
        local final = judgments[#judgments]
        value.trial.at_feedback_endpoint = plan ~= nil and plan.feedback_frames ~= nil
            and final ~= nil and final.index == planned_count() and frame == final.feedback_frame
            and frame >= plan.feedback_frames[final.index] and frame <= plan.feedback_deadline_frames[final.index]
        value.trial.final_feedback_bound = final ~= nil and final.feedback_frame == frame
            and final.image.epoch == adapter.epoch()
        value.trial.debug_finish_bound = response_complete and current.response_finish_frame == frame
    end
    local offset_now = not warn_first_feedback and first_feedback and first_feedback.feedback_frame == frame
        and pixels.feedback(raw, plan.player_center_x) or nil
    value.trial.first_feedback_current = offset_now ~= nil and offset_now.label == first_feedback.label
    local first_image = first_feedback and first_feedback.image
    value.trial.first_feedback_bound = first_image ~= nil and first_image.frame == first_feedback.feedback_frame
        and first_image.epoch == adapter.epoch() and first_image.frame <= frame
    if acting_review then
        local bound = acting_capture and acting_capture.frame == frame and acting_capture.epoch == adapter.epoch() or false
        value.screen = {source = acting_review.source, expected = "acting_entry", frame = acting_review.frame,
            epoch = acting_review.epoch, note = acting_review.note, memory_proven = false,
            confirmed = acting_review.verdict == "pass", fresh_visual_binding = bound}
        if acting_review.source == "local_reference_pixels" then
            value.screen.pixel_check = screen_checks.read(raw, "acting_entry")
            value.screen.confirmed = value.screen.confirmed and bound and value.screen.pixel_check.confirmed
        end
    end
    value.stop_reason = state.error ~= "" and state.error or "host_stop"
    return value
end

local function common()
    local checks = {{id = "platinum", field = "game.supported", expected = true},
        {id = "released", field = "released", expected = true},
        {id = "frame", field = "frame", context = "frame"},
        {id = "epoch", field = "epoch", context = "epoch"},
        {id = "fresh_capture", field = "image.direct_read_frames", expected = 0},
        {id = "sequence", field = "trial.sequence_valid", expected = true}}
    if stage5 then checks[#checks + 1] = {id = "rank_profile", field = "rank_profile_matches_target", expected = true} end
    return checks
end

local function definitions()
    local function count_check(id, field)
        return {id = id, expected = true, evaluate = function(snapshot)
            local actual, required = snapshot.trial[field],
                snapshot.trial.planned_input_count - snapshot.trial.runtime_skipped_inputs
            return actual == required, {count = actual, required = required}
        end}
    end
    local start, finish = common(), common()
    start[#start + 1] = stage5
        and {id = "midpoint", field = "trial.at_authorized_midpoint", expected = true}
        or {id = "midpoint", field = "bar.last_x", op = "range", expected = {128, 247}}
    start[#start + 1] = {id = "plan", field = "trial.plan_consistent", expected = true}
    start[#start + 1] = {id = "planned_cues_bound", field = "trial.planned_cues_bound", expected = true}
    start[#start + 1] = {id = "fresh_visual_binding", field = "trial.fresh_visual_binding", expected = true}
    start[#start + 1] = {id = "before_input", field = "trial.before_first_input", expected = true}
    start[#start + 1] = {id = "midpoint_policy", field = "trial.midpoint_policy_met", expected = true}
    finish[#finish + 1] = count_check("response_inputs", "inputs")
    if stage5 then
        if record_feedback then
            finish[#finish + 1] = {id = "debug_finish_frame", field = "trial.debug_finish_bound", expected = true}
        else
            finish[#finish + 1] = {id = "offset_verified", field = "trial.first_feedback_verified", expected = true}
            finish[#finish + 1] = {id = "counted_response_endpoint", field = "trial.at_response_endpoint", expected = true}
        end
        finish[#finish + 1] = {id = "accounted_inputs", expected = true, evaluate = function(snapshot)
            local trial = snapshot.trial
            return trial.total_inputs + trial.total_skipped_inputs == trial.dance * trial.cues_per_dance
        end}
        finish[#finish + 1] = {id = "feedback_policy_met", field = "trial.feedback_policy_met", expected = true}
        finish[#finish + 1] = {id = "verified_feedback_total", expected = true, evaluate = function(snapshot)
            local actual, required = snapshot.trial.total_judgment_count, snapshot.trial.required_judgments_so_far
            return actual == required, {count = actual, required = required}
        end}
    else
        finish[#finish + 1] = count_check("response_judgments", "judgment_count")
    end
    finish[#finish + 1] = {id = "response_complete", field = "trial.complete", expected = true}
    local result = {[START] = {checks = start, branches = {pass = "response"}},
        [FINISH] = {checks = finish, branches = {pass = "next_dance"}},
        stopped = {checks = {{id = "host_stop", field = "stop_reason", expected = "continue"}}, branches = {pass = "stopped"}}}
    if stage5 and not record_feedback then
        local offset = common()
        offset[#offset + 1] = {id = "first_input_only", expected = true, evaluate = function(snapshot)
            return snapshot.trial.total_inputs <= 1
        end}
        offset[#offset + 1] = {id = "accepted_feedback", field = "trial.first_feedback_accepted", expected = true}
        offset[#offset + 1] = {id = "feedback_capture_bound", field = "trial.first_feedback_bound", expected = true}
        result[OFFSET] = {checks = offset, branches = {pass = "mirror_rest"}}
    end
    -- Dance predicates always select a continuation; shared pause/identity checks stay invariant.
    for name, definition in pairs(result) do
        if name ~= "stopped" then
            local targets, invariant_count = {}, #common()
            for index = invariant_count + 1, #definition.checks do
                targets[#targets + 1] = definition.checks[index]
            end
            definition.checks, definition.target = common(), targets
            local branch = definition.branches.pass
            definition.branches = {hit = branch, miss = name == START and "skip_response" or branch}
        end
    end
    return result
end

local function packet(value)
    local result = {frame = adapter.frame(), epoch = adapter.epoch(), observation = value,
        checkpoint = latest, completed_dances = completed_count(), next_section = state.phase}
    if engine and not pending then result.progress = engine:state() end
    return result
end

local function finish(message, reason, fatal)
    if message and not fatal then
        warn(message)
        message, reason = nil, "dance_recovery_ready"
    end
    if message and runtime then runtime.fail_stage() end
    active = nil
    local ok, close_error = pcall(close_files)
    if not ok then warn(close_error) end
    release()
    state.command_done, state.error = true, message or ""
    state.phase, state.stop_reason = message and "stopped" or "ready", message and "command_error" or reason or "command_complete"
    state.command_end_frame = state.frame
    if message then halted = true end
end

local function account_response()
    if not response_complete then
        total_skipped_inputs = total_skipped_inputs + cue_count - inputs
        if inputs < cue_count then warn(string.format("%d/%d inputs skipped", cue_count - inputs, cue_count)) end
    end
    response_complete, current.response_finish_frame = true, adapter.frame()
end

local function stop(message, reason, raw)
    finish(message, reason)
    raw = raw or gui.gdscreenshot()
    write(mailbox_directory .. "/screen.gd", raw, "wb"); state.screen_frame = state.frame
    publish(observation(state.frame, raw)); emu.pause()
end

local function retain_demo(raw, source, readable)
    readable = readable ~= false
    local sample = {frame = adapter.frame(), epoch = adapter.epoch(), bar = pixels.bar(raw), source = source,
        readable = readable, capture_offset = demo_offset, capture_bytes = readable and #raw or 0}
    if readable then
        assert(demo_bytes:write(raw))
        demo_offset, demo_last = demo_offset + #raw, sample.frame
    else
        sample.readability_reason = sample.bar.white_flash and "white_flash" or "strip_unreadable"
        sample.timing_source = "counted_emulator_frame_without_a_readable_position"
    end
    assert(demo_index:write(json.encode(sample), "\n"))
end

local function dispatch(action, args)
    if action == "init-run" then
        assert(not runtime and not engine and #args == 4, "Initialize once with the resolved timing settings")
        work, rank = args[2], args[3]
        assert(rank == "Normal" or rank == "Super" or rank == "Hyper" or rank == "Master", "Select a supported contest rank")
        config = assert(loadfile(args[4]))()
        assert(config.rank == rank and config.rank_timing and config.game == emu.gamecode(),
            "Prepared timing settings must match this ROM and rank")
        cue_count = config.cues_per_dance
        resolved_cue_offset = config.rank_timing.cue_side_offset_pixels
        assert(config.demo_sample_interval_frames >= 1
            and config.demo_sample_interval_frames == math.floor(config.demo_sample_interval_frames),
            "Use a positive configured demo sampling interval")
        engine = master.new({run_id = args[1], emulator = adapter, reader = {snapshot = snapshot},
            definitions = definitions(), log = master.jsonl(work .. "/checkpoint-events.jsonl")})
        initialized = true
    elseif action == "init" then
        assert(stage5 and runtime and engine and not initialized and config.source_frame == adapter.frame(),
            "Initialize from the current accepted Stage 4 frame without advancing")
        assert(config.rank_timing and config.target and config.rank_timing.rank == config.target.rank
            and config.rank_timing.pixels_per_frame > 0 and config.rank_timing.cues_per_dance == cue_count
            and config.cues_per_dance == cue_count and config.required_response_inputs == 6 * cue_count,
            "The dance timing profile must match the accepted target rank")
        assert(type(config.demo_sample_interval_frames) == "number" and config.demo_sample_interval_frames >= 1
            and config.demo_sample_interval_frames == math.floor(config.demo_sample_interval_frames),
            "Use a positive configured demo sampling interval")
        assert((config.feedback_policy == "first_button_only" or config.feedback_policy == "all_buttons" or record_feedback)
            and config.required_excellent_judgments == 0,
            "Dance feedback is recorded without requiring an Excellent result")
        assert(config.first_feedback_policy == "warn_and_continue", "Dance feedback must continue after a miss")
        assert(not warn_first_feedback or runtime.main_dance_feedback,
            "The persistent runner must account for its first feedback result")
        assert(config.observation_policy == "scheduled" or config.observation_policy == "diagnostic",
            "Declare scheduled or diagnostic dance observations")
        assert(not scheduled or record_feedback or (fixed_metrics or config.pointer_calibration) and not all_feedback,
            "Scheduled dancing requires fixed metrics or per-contest calibration and first-button feedback")
        assert(not scheduled or type(config.feedback_release_frames) == "number"
            and config.feedback_release_frames >= 1 and config.feedback_release_frames == math.floor(config.feedback_release_frames)
            and config.first_feedback_delay_frames == config.hold_frames + config.feedback_release_frames - 1,
            "Schedule feedback after the complete held and released input block")
        assert((config.debug_inputs == true) == record_feedback,
            "Recording-only feedback belongs to the fixed-metric debug runner")
        assert(config.tolerant_inputs == true, "Every dance mode must use count-aware continuation")
        assert(fixed_metrics == (config.rank_timing.timing_source == "fixed_normalized_contest_measurements"),
            "Declare whether the selected profile uses fixed measured metrics")
        if fixed_metrics then
            local profile = config.rank_timing
            local speed, short, switch = profile.pixels_per_frame, profile.dance_gap_frames, profile.leader_gap_frames
            assert(scheduled and not config.pointer_calibration and profile.category == config.target.category
                and profile.timing_source == "fixed_normalized_contest_measurements"
                and short >= 0 and short == math.floor(short) and switch >= short and switch == math.floor(switch)
                and profile.cycle_frames == math.floor(240 / speed + short)
                and profile.response_frames == math.floor(120 / speed) and profile.extra_switch_frames == switch - short,
                "Use the selected category/rank's fixed measured speed and normalized gaps")
        end
        assert(not scheduled_feedback or fixed_metrics and scheduled, "Scheduled feedback requires fixed scheduled metrics")
        assert(config.midpoint_diagnostics == nil or type(config.midpoint_diagnostics) == "boolean",
            "Declare off-center midpoint diagnostics with a boolean")
        assert(not midpoint_diagnostics or record_feedback and fixed_metrics and scheduled,
            "Off-center continuation belongs to fixed-metric scheduled input debugging")
        local measured, accepted = config.pointer_calibration, config.accepted_pointer_calibration
        if measured then
            assert(accepted and accepted.source == measured.source and measured.source == "fresh_paused_pointer_displacement"
                and measured.input_policy == "none" and measured.emulator_frames == config.calibration_window_frames,
                "Use the pointer calibration from the acknowledged Stage 4 checkpoint")
            for _, name in ipairs({"start", "end"}) do
                for _, field in ipairs({"frame", "epoch", "pointer_x", "path", "source"}) do
                    assert(measured[name][field] == accepted[name][field], "The accepted calibration endpoints changed")
                end
            end
            local first, last = measured.start, measured["end"]
            local displacement = last.pointer_x - first.pointer_x
            assert(last.frame == config.source_frame and last.epoch == config.source_context.epoch
                and last.frame - first.frame == config.calibration_window_frames and last.epoch == first.epoch
                and first.pointer_x >= 8 and first.pointer_x < last.pointer_x and last.pointer_x < 128
                and first.pointer_x == math.floor(first.pointer_x) and last.pointer_x == math.floor(last.pointer_x)
                and measured.pointer_pixels == displacement and measured.pixels_per_frame == displacement / config.calibration_window_frames
                and measured.pixels_per_frame == accepted.pixels_per_frame
                and config.rank_timing.pixels_per_frame == measured.pixels_per_frame,
                "The dance speed must use this contest's counted pointer displacement")
            local profile, fixed = config.rank_timing, config.rank_timing.fixed_timing_estimates
            local stored = assert(profile.stored_timing, "Retain the fixed-gap calculation source")
            local old_half = math.floor(120 / stored.pixels_per_frame + 0.5)
            local half = math.floor(120 / measured.pixels_per_frame + 0.5)
            assert(fixed and fixed.cycle_extra_frames == stored.cycle_frames - 2 * old_half
                and fixed.response_tail_frames == stored.response_frames - old_half
                and fixed.leader_switch_frames == stored.extra_switch_frames
                and profile.extra_switch_frames == stored.extra_switch_frames
                and profile.cycle_frames == 2 * half + fixed.cycle_extra_frames
                and profile.response_frames == half + fixed.response_tail_frames
                and profile.lead_dances_frames == 2 * profile.cycle_frames + stored.extra_switch_frames,
                "Calibrated travel must preserve the separate fixed timing estimates")
        else
            assert(not accepted, "The accepted per-contest calibration cannot be discarded")
        end
        assert(config.transition and config.transition.mash_frames > 0, "The counted Acting transition is required")
        local timing = assert(config.midpoint_timing, "Declare the Stage 4 midpoint anchor or visual fallback")
        assert(timing.entry_frame == config.source_frame, "The midpoint anchor belongs to the accepted Stage 4 frame")
        if fixed_metrics and not record_feedback then
            local pointer = config.accepted_entry_pointer
            local bounds = pointer and pointer.position_bounds
            assert(pointer and pointer.status == "exact" and bounds and bounds[1] == bounds[2]
                and timing.entry_pointer_x == bounds[1], "Use the readable pointer from the acknowledged Stage 4 entry")
        end
        if timing.frames then
            local expected_source = fixed_metrics and "accepted_stage4_pointer_and_fixed_metrics"
                or measured and "accepted_stage4_pointer_calibration" or "accepted_stage4_pointer_and_rank_timing"
            assert(timing.source == expected_source and type(timing.entry_pointer_x) == "number"
                and timing.entry_pointer_x >= 8 and timing.entry_pointer_x < 128
                and timing.entry_pointer_x == math.floor(timing.entry_pointer_x), "Use the accepted Dance 1 demo pointer")
            assert(not measured or timing.entry_pointer_x == measured["end"].pointer_x,
                "The midpoint anchor must use the calibrated endpoint position")
            if scheduled_feedback then
                assert(timing.capture_target_x == 127 and timing.rounding == "floor"
                    and timing.accepted_pointer_pixels and timing.accepted_pointer_pixels[1] == 128
                    and timing.accepted_pointer_pixels[2] == 129,
                    "Midpoint captures must favor early arrival and accept the two native line pixels")
            end
            local travel = ((scheduled_feedback and 127 or 128) - timing.entry_pointer_x) / config.rank_timing.pixels_per_frame
            local first = config.source_frame + (scheduled_feedback and math.max(1, math.floor(travel)) or math.floor(travel + 0.5))
            midpoint_frames = calculate_midpoints(first)
            assert(#timing.frames == 6, "Calculate all six response midpoints")
            for index = 1, 6 do
                assert(timing.frames[index] == midpoint_frames[index], "The midpoint schedule must use the accepted pointer and rank timing")
            end
            midpoint_source = timing.source
        else
            assert(timing.source == "visual_midpoint_fallback" and timing.entry_pointer_x == nil,
                "Visual midpoint fallback requires an unreadable entry pointer")
        end
        initialized = true
    elseif action == "state-changed" then
        assert(not runtime and args[1] == "load" and not current and completed == 0 and not pending and not halted,
            "Only the initial prepared source load is allowed")
        finish(nil, "source_loaded"); adapter.state_changed(); state.observation_stale = true
    elseif action == "anchor-midpoints" then
        assert(not stage5 and initialized and not current and not pending and #args == 1,
            "Anchor the prepared schedule once from its fresh paused entry")
        local observed = assert(loadfile(args[1]))()
        local bar = pixels.bar(gui.gdscreenshot())
        local bounds = bar.position_bounds
        local pointer = observed.pointer_x
        local readable = type(pointer) == "number" and pointer >= 8 and pointer <= 247
            and bounds and bounds[1] <= pointer and pointer <= bounds[2]
        assert(observed.frame == adapter.frame() and observed.epoch == adapter.epoch()
            and observed.speed > 0 and (pointer == nil or readable),
            "The prepared midpoint anchor needs the current paused frame and its readable pointer or unreadable marker")
        local first = observed.frame + (readable and math.max(1, math.ceil((128 - pointer) / observed.speed))
            or config.rank_timing.response_frames)
        assert(observed.first_midpoint_frame == first, "Use the prepared pointer's counted remaining travel")
        config.pointer_pixels_per_emulator_frame = observed.speed
        midpoint_frames = calculate_midpoints(first)
        midpoint_source = readable and "fresh_prepared_pointer_and_rank_timing" or "counted_prepared_demo_budget"
    elseif action == "select-dance" then
        local index, gap = tonumber(args[1]), tonumber(args[3])
        assert(initialized and engine and not active and not pending and not halted and #args == 3
            and index == completed + 1 and index <= 6, "Select the next authorized dance")
        assert(index == 1 or latest and latest.checkpoint == FINISH and latest.proceed, "Acknowledge the prior response")
        local expected_gap = config.rank_timing.extra_switch_frames
        assert(gap == ((index == 3 or index == 5) and expected_gap or 0), "Use the selected leader-switch gap")
        current = {index = index, work = args[2], gap = gap, gap_complete = gap == 0, unreadable_demo_frames = 0,
            midpoint_adjustments = 0,
            midpoint_frame = midpoint_frames and midpoint_frames[index],
            midpoint_source = midpoint_source or "visual_midpoint_fallback"}
        plan, inputs, response_complete, last_feedback = nil, 0, false, nil
        judgments = setmetatable({}, bridge_io.array_metatable)
        response_skips = setmetatable({}, bridge_io.array_metatable)
    elseif action == "capture" then
        local image, raw = capture(args[1])
        local value = observation(adapter.frame(), raw); value.image = image
        return packet(value)
    elseif action == "retime-midpoint" then
        assert((not stage5 and midpoint_frames or config.pointer_calibration or fixed_metrics) and current and not plan and not active and not pending and not halted
            and current.midpoint_frame == adapter.frame() and state.stop_reason == "demo_midpoint" and #args == 1,
            "Adjust only a fresh calculated midpoint that is still in the demonstration")
        assert(not midpoint_diagnostics or current.midpoint_adjustments == 0,
            "Midpoint diagnostics permit one existing counted correction per visit")
        local observed = assert(loadfile(args[1]))()
        assert(observed.frame == adapter.frame() and observed.epoch == adapter.epoch()
            and type(observed.pointer_x) == "number"
            and observed.pointer_x == math.floor(observed.pointer_x) and observed.pointer_x >= 8 and observed.pointer_x < 128,
            "The midpoint adjustment needs a readable paused demo pointer")
        local speed = not stage5 and config.pointer_pixels_per_emulator_frame or config.rank_timing.pixels_per_frame
        local frames = math.ceil((128 - observed.pointer_x) / speed)
        assert(observed.next_frame == observed.frame + frames and frames > 0,
            "Calculate the remaining pointer travel in emulator frames")
        current.midpoint_frame, current.midpoint_source = observed.next_frame, "calculated_from_fresh_demo_pointer"
        current.midpoint_adjustments = current.midpoint_adjustments + 1
    elseif action == "plan" then
        assert(current and not active and not pending and not halted, "Plan one paused response")
        plan = assert(loadfile(args[1]))()
        assert(plan.frame == adapter.frame() and plan.epoch == adapter.epoch() and plan.dance == current.index
            and plan.rank == rank and plan.speed > 0 and #plan.presses <= cue_count, "The plan belongs to this midpoint")
        assert(not plan.response_end_frame or #plan.presses == 0
            or plan.presses[#plan.presses].frame + plan.hold_frames < plan.response_end_frame,
            "The final input must fit before the counted response endpoint")
        if stage5 then
            local endpoint
            if plan.skip_reason then
                local anchor = current.midpoint_frame or plan.fallback_midpoint_frame or plan.frame
                current.midpoint_frame = anchor
                assert(#plan.presses == 0 and plan.pointer_x == nil
                    and plan.fallback_midpoint_frame == anchor,
                    "An unreadable response uses its counted midpoint for an input-free skip")
                endpoint = math.max(plan.frame + 1, anchor + math.ceil(1 / plan.speed)
                    + config.rank_timing.response_frames)
            else
                endpoint = math.floor(plan.frame + (128 - plan.pointer_x) / plan.speed + config.rank_timing.response_frames + 0.5)
                endpoint = math.max(plan.frame + 1, endpoint)
            end
            assert(plan.speed == config.rank_timing.pixels_per_frame and plan.response_end_frame == endpoint
                and plan.hold_frames == config.hold_frames and plan.offset_frames == config.offset_frames,
                 "The response must use the selected rank timing and counted endpoint")
            setmetatable(plan.presses, bridge_io.array_metatable)
            assert(plan.expected_inputs == cue_count and plan.skipped_input_count == cue_count - #plan.presses,
                "Account for every cue opportunity as planned or skipped")
            for _, skipped in ipairs(plan.skipped_inputs or {}) do response_skips[#response_skips + 1] = skipped end
            if not plan.skip_reason and (plan.cues.unresolved_cues or 0) > 0 then
                response_skips[#response_skips + 1] = {reason = "unresolved_cues", count = plan.cues.unresolved_cues}
            end
            if scheduled_feedback then
                assert(plan.cues and (plan.cues.side_offset_pixels or resolved_cue_offset)
                    == config.rank_timing.cue_side_offset_pixels,
                    "Cue spacing must match the fixed category/rank profile")
                assert(type(config.first_feedback_delay_frames) == "number"
                    and config.first_feedback_delay_frames == math.floor(config.first_feedback_delay_frames)
                    and config.first_feedback_delay_frames >= plan.hold_frames
                    and type(plan.feedback_frames) == "table" and #plan.feedback_frames == #plan.presses
                    and type(plan.feedback_deadline_frames) == "table" and #plan.feedback_deadline_frames == #plan.presses,
                     "Use the shared released-input feedback capture delay")
                local gap = current.index % 2 == 0 and config.rank_timing.leader_gap_frames or config.rank_timing.dance_gap_frames
                assert(plan.next_demo_frame == plan.response_end_frame + gap,
                    "The next demo retains its fixed response-end timing anchor")
                for index, press in ipairs(plan.presses) do
                    local following = plan.presses[index + 1]
                    local limit = following and math.min(following.frame - 1, plan.response_end_frame)
                        or math.max(plan.response_end_frame, plan.feedback_frames[index])
                    assert(plan.feedback_frames[index] == press.frame + config.first_feedback_delay_frames
                        and plan.feedback_deadline_frames[index] == limit and plan.feedback_frames[index] <= limit
                        and (limit <= plan.response_end_frame or limit < plan.next_demo_frame),
                        "Bound feedback recovery by the next input or response end, retaining the full final released wait")
                end
            elseif scheduled and current.index == 1 then
                assert(type(config.first_feedback_delay_frames) == "number"
                    and config.first_feedback_delay_frames == math.floor(config.first_feedback_delay_frames)
                    and config.first_feedback_delay_frames >= plan.hold_frames
                    and (#plan.presses == 0 or plan.first_feedback_frame == plan.presses[1].frame + config.first_feedback_delay_frames)
                    and (#plan.presses < 2 or plan.first_feedback_frame < plan.presses[2].frame),
                    "Schedule the released-input feedback capture before the second button")
            end
            if (config.pointer_calibration or fixed_metrics) and not plan.skip_reason then
                local origin = scheduled_feedback and plan.frame + math.floor((127 - plan.pointer_x) / plan.speed)
                    or plan.response_end_frame - config.rank_timing.response_frames
                for index = current.index + 1, 6 do
                    midpoint_frames[index] = origin + (index - current.index) * config.rank_timing.cycle_frames
                        + (math.floor((index - 1) / 2) - math.floor((current.index - 1) / 2)) * config.rank_timing.extra_switch_frames
                end
                midpoint_source = fixed_metrics and "calculated_from_fresh_midpoint_and_fixed_metrics"
                    or "calculated_from_fresh_midpoint_and_calibration"
            end
        end
        if not stage5 and midpoint_frames and not plan.skip_reason then
            config.pointer_pixels_per_emulator_frame = plan.speed
            local origin = plan.response_end_frame - config.rank_timing.response_frames
            for index = current.index + 1, 6 do
                midpoint_frames[index] = origin + (index - current.index) * config.rank_timing.cycle_frames
                    + (math.floor((index - 1) / 2) - math.floor((current.index - 1) / 2)) * config.rank_timing.extra_switch_frames
            end
            midpoint_source = "calculated_from_fresh_prepared_midpoint_and_rank_timing"
        end
    elseif action == "recover-response" then
        assert(current and not active and not pending and #args == 3
            and current.index == completed + 1 and current.index == tonumber(args[1]),
            "Continue the selected response from a confirmed pause")
        if not plan then dispatch("plan", {args[2]}) end
        local image, raw = capture("response-recovery")
        current.skip_response = true
        warn(args[3])
        response_events = response_events or assert(io.open(current.work .. "/response-events.jsonl", "a"))
        for index, press in ipairs(plan.presses) do
            if not press.started then skip_press(index, "dance_error_continuation")
            elseif not press.judgment and checks_feedback(index) then
                retain_judgment(index, nil, raw, false, "dance_error_continuation", "paused_response_recovery")
            end
        end
        if stage5 and not record_feedback and current.index == 1 and not first_feedback then
            retain_first_skip(raw, args[3], "paused_response_recovery")
        end
        finish(nil, "dance_recovery_ready")
        local value = observation(adapter.frame(), raw)
        value.image = image
        local result = packet(value)
        result.response_plan = plan
        return result
    elseif action == "warn" then
        assert(#args == 1, "Supply the dance continuation warning")
        warn(args[1])
    elseif action == "visit" then
        assert(not active and not pending and not halted, "The boundary must be ready")
        assert(args[1] == START or args[1] == FINISH or stage5 and (args[1] == OFFSET or args[1] == "stage-5-dance"),
            "Select a dance or Acting-entry checkpoint")
        local context = args[2] and assert(loadfile(args[2]))() or {frame = adapter.frame(), epoch = adapter.epoch()}
        latest = engine:visit(args[1], context)
        pending = latest.status == "awaiting_save" or latest.status == "awaiting_ack"
        if latest.decision == "miss" then
            local failed = {}
            for _, target in ipairs(latest.targets) do if not target.passed then failed[#failed + 1] = target.id end end
            warn(args[1] .. ": " .. table.concat(failed, ", "))
        end
    elseif action == "complete" then
        assert(pending and (#args == 6 or #args == 7), "Complete the pending visit once")
        latest = engine:complete(tonumber(args[2]), {run_id = args[1], visit = tonumber(args[2]),
            frame = tonumber(args[3]), epoch = tonumber(args[4]), path = args[5], emulator_paused = args[6] == "true",
            saved = args[7] ~= "false"})
        pending = false
        if latest.proceed and latest.checkpoint == START then
            current.start_acknowledged = true
            resolved_cue_offset = plan.cues.side_offset_pixels or resolved_cue_offset
            if latest.decision == "miss" then
                for index in ipairs(plan.presses) do skip_press(index, "midpoint_checkpoint_continuation") end
            end
        end
        if latest.proceed and latest.checkpoint == FINISH then completed = current.index end
        if latest.proceed and latest.checkpoint == OFFSET then first_feedback_verified = true end
    elseif action == "abort" then
        finish(args[1] or "Host stopped the six-dance attempt", nil, true)
        if pending then latest = engine:cancel(state.error); pending = false
        elseif engine and not engine:state().halted then
            latest = engine:visit("stopped", {frame = adapter.frame(), epoch = adapter.epoch(), reason = state.error})
            pending = latest.status == "awaiting_save" or latest.status == "awaiting_ack"
        end
    elseif action == "capture-acting" then
        assert(stage5 and completed == 6 and transition and transition.completed and not active and not pending,
            "Complete six responses, both input-free dances, and the B transition")
        local image, raw = capture("stage5", work)
        acting_capture = {frame = adapter.frame(), epoch = adapter.epoch()}
        local value = observation(adapter.frame(), raw)
        value.image, value.automatic_check = image, screen_checks.read(raw, "acting_entry")
        return packet(value)
    elseif action == "review-acting" then
        assert(stage5 and acting_capture and not active and not pending, "Capture the paused Acting entry first")
        local review = assert(loadfile(args[1]))()
        assert(review.run_id == config.run_id and review.frame == adapter.frame() and review.epoch == adapter.epoch()
            and (review.source == "visual_review" or review.source == "local_reference_pixels")
            and (review.verdict == "pass" or review.verdict == "fail"), "The Acting review belongs to this paused frame")
        assert(type(review.note) == "string" and review.note:match("%S") and not review.note:find("[|\r\n]"),
            "The review needs a single-line explanation")
        acting_review = review
    elseif action == "terminal" then
        finish(nil, "terminal")
    else
        error("Unknown six-dance checkpoint action: " .. tostring(action))
    end
    return packet()
end

local function checkpoint_callback()
    local parts = checkpoint_mailbox:read_command(checkpoint_id)
    if not parts then return end
    checkpoint_id = parts[1]
    local args = {}; for index = 3, #parts do args[#args + 1] = parts[index] end
    assert(not idle, "Reentrant paused request")
    idle, state.frame = true, adapter.frame()
    local ok, result = pcall(dispatch, parts[2], args)
    if not ok then
        local message = tostring(result)
        finish(message)
        if parts[2] ~= "abort" and parts[2] ~= "terminal" then
            if parts[2] == "plan" then plan = nil end
            result = {frame = adapter.frame(), epoch = adapter.epoch(), warning = message, recovery_required = true}
        else
            if pending then latest = engine:cancel(message); pending = false end
            result = {frame = adapter.frame(), epoch = adapter.epoch(), error = message, checkpoint = latest}
        end
    end
    if parts[2] ~= "state-changed" then state.observation_stale = false end
    result.command_id, result.command_done = checkpoint_id, true
    publish(result.observation); idle = false; checkpoint_mailbox:publish(result, false)
end

local function start(parts)
    assert(engine and current and not halted and not pending and not active, "The selected dance must be ready")
    state.command_id, state.command_done, state.error = parts[1], false, ""
    state.command_start_frame = state.frame
    if parts[2] == "wait-gap" then
        assert(not current.gap_complete and not plan, "Wait once for the agreed leader switch")
        active = {kind = "gap", end_frame = state.frame + current.gap}
    elseif parts[2] == "observe-demo" then
        assert(current.gap_complete and not plan, "Observe the selected demonstration")
        if current.midpoint_frame and current.midpoint_frame <= state.frame then
            current.midpoint_overrun_frames = state.frame - current.midpoint_frame
            current.midpoint_frame = state.frame + 1
            current.midpoint_source = "late_counted_midpoint"
        end
        assert(not current.midpoint_frame or current.midpoint_frame > state.frame,
            "The calculated midpoint must still be ahead of the current frame")
        if scheduled then
            assert(current.midpoint_frame, "Scheduled observation requires a counted midpoint")
            active = {kind = "demo"}
            return
        end
        local continuing = current.demo_started == true
        demo_bytes = assert(io.open(current.work .. "/demo-frames.bin", continuing and "ab" or "wb"))
        demo_index = assert(io.open(current.work .. "/demo-frames.jsonl", continuing and "a" or "w"))
        if not continuing then demo_offset, demo_last = 0, state.frame end
        local raw = gui.gdscreenshot()
        local source = "paused_demo_entry"
        if not continuing and stage5 and current.index == 1 and config.source_image then
            local image = config.source_image
            assert(image.frame == state.frame and image.origin_epoch == config.source_context.epoch
                and image.origin_run_id == config.source_context.run_id, "The source image must belong to the accepted Stage 4 frame")
            raw, source = read(image.path), "accepted_stage4_checkpoint_framebuffer"
        end
        local bar = pixels.bar(raw)
        active = {kind = "demo", seen_demo = current.midpoint_frame ~= nil or bar.found and bar.last_x < 128}
        if active.seen_demo and not continuing then retain_demo(raw, source, bar.found) end
        current.demo_started = true
    elseif parts[2] == "mirror" or parts[2] == "mirror-rest" then
        local expected = parts[2] == "mirror" and START or OFFSET
        assert(plan and latest and latest.proceed and (current.skip_response and current.start_acknowledged
            or latest.checkpoint == expected and latest.frame == state.frame),
            "Acknowledge the midpoint before responding")
        assert(parts[2] ~= "mirror-rest" or current.skip_response or stage5 and not record_feedback and first_feedback_verified
            and inputs <= 1,
            "The accepted first-button judgment must authorize the remaining inputs")
        if response_complete then stop(nil, "dance_response_complete"); return end
        response_events = response_events or assert(io.open(current.work .. "/response-events.jsonl",
            (current.skip_response or parts[2] == "mirror-rest") and "a" or "w"))
        active = {kind = "mirror"}
        if not current.skip_response and (not scheduled or record_feedback or current.index == 1 and not first_feedback_verified) then
            last_feedback = pixels.feedback(gui.gdscreenshot(), plan.player_center_x)
        end
    elseif parts[2] == "lead-dances" then
        assert(stage5 and not record_feedback and completed == 6 and latest and latest.checkpoint == FINISH and latest.proceed,
            "Acknowledge the sixth response before the input-free dances")
        local endpoint = math.max(state.frame + 1, plan.response_end_frame + config.rank_timing.lead_dances_frames)
        lead_dances = lead_dances or {first_dance = 7, last_dance = 8, input_policy = "none", input_count = 0,
            completed = false, start_frame = state.frame, end_frame = endpoint, duration_frames = endpoint - state.frame}
        if lead_dances.completed then stop(nil, "lead_dances_complete"); return end
        active = {kind = "lead", end_frame = math.max(state.frame + 1, lead_dances.end_frame)}
    elseif parts[2] == "acting-transition" then
        assert(stage5 and not record_feedback and lead_dances and lead_dances.completed
            and (transition or state.frame == lead_dances.end_frame),
            "Complete both input-free dances before the Acting transition")
        transition = transition or {start_frame = state.frame, end_frame = state.frame + config.transition.mash_frames,
            game_seconds = config.transition.game_seconds, mash_frames = config.transition.mash_frames,
            mash_hold_frames = config.transition.mash_hold_frames, mash_release_frames = config.transition.mash_release_frames,
            button = "B", press_count = 0, completed = false}
        if transition.completed then stop(nil, "B_transition_complete"); return end
        active = {kind = "transition", end_frame = math.max(state.frame + 1, transition.end_frame)}
    else
        error("Unknown six-dance input action: " .. tostring(parts[2]))
    end
end

local function before_frame()
    state.frame = adapter.frame()
    if not active then
        local parts = mailbox:read_command(state.command_id)
        if parts then
            local ok, message = pcall(start, parts)
            if not ok then stop(tostring(message)); return end
        end
    end
    local button
    if active and active.kind == "mirror" and not current.skip_response then
        local frame = state.frame + 1
        if stage5 and not record_feedback and not scheduled_feedback and not first_feedback_verified
            and not first_feedback and plan.presses[2] and frame >= plan.presses[2].frame
            and plan.presses[1].started and not plan.presses[1].judgment then
            retain_judgment(1, nil, gui.gdscreenshot(), false, "excellent_palette_not_detected", "before_frame_feedback")
            stop(nil, "offset_feedback_ready"); return
        end
        for index, press in ipairs(plan.presses) do
            if not press.started and frame > press.frame then skip_press(index, "missed_input_frame") end
            if scheduled_feedback and checks_feedback(index) and press.started and not press.judgment
                and state.frame > plan.feedback_deadline_frames[index] then
                retain_judgment(index, nil, gui.gdscreenshot(), false, "missed_feedback_window", "before_frame_feedback_recovery")
            end
            if warn_first_feedback and current.index == 1 and not first_feedback_verified and inputs >= 1
                and not checks_feedback(index) and frame == press.frame then
                skip_press(index, "first_feedback_pending")
            end
            if not scheduled_feedback and all_feedback and not press.skipped and not press.started and frame == press.frame then
                for prior_index = 1, index - 1 do
                    local prior = plan.presses[prior_index]
                    if prior.started and not prior.judgment then
                        retain_judgment(prior_index, nil, gui.gdscreenshot(), false, "excellent_palette_not_detected", "before_frame_feedback")
                    end
                end
            end
            if not scheduled and checks_feedback(index) and not press.skipped and not press.started and frame == press.frame then
                local raw = gui.gdscreenshot()
                last_feedback = pixels.feedback(raw, plan.player_center_x)
                write(string.format("%s/feedback-before-%d.gd", current.work, index), raw, "wb")
            end
            if not press.skipped and frame >= press.frame and frame < press.frame + plan.hold_frames then button = press.button end
            if not press.skipped and not press.started and frame == press.frame then
                press.started, press.feedback_before = true, last_feedback
                inputs, total_inputs = inputs + 1, total_inputs + 1
                assert(response_events:write(json.encode({kind = "input", dance = current.index,
                    index = index, frame = frame, button = button}), "\n"))
            end
        end
    elseif active and active.kind == "transition" then
        local elapsed = state.frame - transition.start_frame
        local cycle = transition.mash_hold_frames + transition.mash_release_frames
        if elapsed % cycle < transition.mash_hold_frames then button = "B" end
        if elapsed % cycle == 0 then transition.press_count = transition.press_count + 1 end
    end
    bridge_io.apply_buttons(button); stylus.set({touch = false})
    state.buttons, state.stylus_touch = button or "", false
end

retain_judgment = function(index, found, raw, fresh, reason, capture_callback)
    local press = plan.presses[index]
    assert(press.started and not press.judgment, "Retain one feedback result per executed input")
    local label = fresh and found.label == "Excellent" and "Excellent" or "Miss"
    local path = string.format("%s/judgment-%d-%s.gd", current.work, index, label)
    local judgment = {dance = current.index, index = index, button = press.button, label = label,
        input_frame = press.frame, feedback_frame = state.frame, visible_delay_frames = state.frame - press.frame,
        feedback_x = found and found.x, feedback_y = found and found.y,
        source = found and found.source or "excellent_palette_not_detected", fresh_message = fresh, recovery_reason = reason,
        image = {path = path, frame = state.frame, epoch = adapter.epoch(),
            source = "input_feedback_framebuffer", capture_callback = capture_callback or "after_frame_feedback", direct_read_frames = 0}}
    if scheduled_feedback then
        judgment.scheduled_feedback_frame = plan.feedback_frames[index]
        judgment.feedback_deadline_frame = plan.feedback_deadline_frames[index]
        judgment.feedback_sample_count = press.feedback_sample_count or 0
        judgment.release_frames_before_initial_capture = config.feedback_release_frames
        judgment.recovered_after_initial_capture = fresh and state.frame > plan.feedback_frames[index]
    end
    press.judgment = judgment; judgments[#judgments + 1] = judgment
    if stage5 and current.index == 1 and index == 1 then
        first_feedback = judgment
        if warn_first_feedback then
            local result = runtime.main_dance_feedback(judgment, config)
            judgment.warning = result.warning
        end
    end
    if label == "Miss" and not (warn_first_feedback and current.index == 1 and index == 1) then
        judgment.warning = warn(string.format("Input %d feedback: Miss", index))
    end
    total_judgments = total_judgments + 1
    if label == "Excellent" then total_excellent = total_excellent + 1 end
    write(path, raw, "wb")
    write(current.work .. "/judgments.json", json.encode(judgments) .. "\n")
    assert(response_events:write(json.encode(judgment), "\n"))
    return judgment
end

retain_first_skip = function(raw, reason, capture_callback)
    local path = current.work .. "/first-input-skipped.gd"
    local judgment = {dance = 1, index = 1, label = "Skipped", skipped = true, fresh_message = false,
        feedback_frame = state.frame, recovery_reason = reason, source = "skipped_first_input",
        image = {path = path, frame = state.frame, epoch = adapter.epoch(),
            source = "skipped_input_framebuffer", capture_callback = capture_callback or "after_frame", direct_read_frames = 0}}
    write(path, raw, "wb")
    if warn_first_feedback then
        judgment.warning = runtime.main_dance_feedback(judgment, config).warning
    else
        judgment.warning = warn("First mirrored input skipped")
    end
    first_feedback = judgment
    write(current.work .. "/first-input-result.json", json.encode(judgment) .. "\n")
    assert(response_events:write(json.encode(judgment), "\n"))
end

local function feedback(raw, scheduled_index)
    if stage5 and not all_feedback and (first_feedback_verified or current.index ~= 1 or inputs == 0) then return end
    local delay = config.first_feedback_delay_frames or config.hold_frames + config.feedback_release_frames - 1
    if not scheduled_feedback then
        local ready = false
        for index, press in ipairs(plan.presses) do
            if checks_feedback(index) and press.started and not press.judgment and state.frame >= press.frame + delay then
                ready = true; break
            end
        end
        if not ready then return end
    end
    local found = pixels.feedback(raw, plan.player_center_x)
    if scheduled_feedback then
        local press = assert(plan.presses[scheduled_index], "Capture feedback for one scheduled input")
        local initial, limit = plan.feedback_frames[scheduled_index], plan.feedback_deadline_frames[scheduled_index]
        assert(press.started and not press.judgment and state.frame >= initial and state.frame <= limit,
            "Capture unresolved feedback within its counted recovery window")
        press.feedback_sample_count = (press.feedback_sample_count or 0) + 1
        local before = press.feedback_before
        local fresh = found ~= nil and (not before or found.label ~= before.label or found.y > before.y + 2)
        if fresh or state.frame == limit then
            retain_judgment(scheduled_index, found, raw, fresh,
                not fresh and (found and "stale_message" or "excellent_palette_not_detected") or nil)
        else
            local path = string.format("%s/feedback-recovery-%d-%d.gd", current.work, scheduled_index, state.frame)
            write(path, raw, "wb")
            assert(response_events:write(json.encode({kind = "feedback_pending", dance = current.index,
                index = scheduled_index, frame = state.frame, epoch = adapter.epoch(), image = path,
                observed_label = found and found.label, reason = found and "stale_message" or "excellent_palette_not_detected"}), "\n"))
        end
        last_feedback = found
        return
    end
    local fresh = found and (not last_feedback or found.label ~= last_feedback.label or found.y > last_feedback.y + 2)
    if fresh then
        local eligible = {}
        for index, press in ipairs(plan.presses) do
            local before = press.feedback_before
            if checks_feedback(index) and press.started and not press.judgment and state.frame >= press.frame + delay
                and (not before or found.label ~= before.label or found.y > before.y + 2) then
                eligible[#eligible + 1] = index
            end
        end
        if #eligible > 1 then warn("Feedback cannot be assigned to one pending input"); eligible = {} end
        if #eligible == 1 then
            local index = eligible[1]
            retain_judgment(index, found, raw, true)
            if stage5 and all_feedback then
                print(string.format("Dancing feedback: dance %d, button %d/%d: Excellent", current.index, index, cue_count))
            end
        end
    end
    last_feedback = found
end

local function after_frame()
    state.frame = adapter.frame()
    local ok, message = pcall(function()
        if active and active.kind == "gap" then
            if state.frame >= active.end_frame then
                if state.frame ~= active.end_frame then warn("The counted leader-switch gap was overrun") end
                current.gap_complete = true; stop(nil, "leader_switch_gap_complete"); return
            end
        elseif active and active.kind == "lead" then
            if state.frame >= active.end_frame then
                if state.frame ~= active.end_frame then
                    warn("The input-free dance endpoint was overrun")
                    lead_dances.planned_end_frame, lead_dances.end_frame = active.end_frame, state.frame
                end
                lead_dances.completed = true; stop(nil, "lead_dances_complete"); return
            end
        elseif active and active.kind == "transition" then
            if state.frame >= active.end_frame then
                if state.frame ~= active.end_frame then
                    warn("The Acting transition endpoint was overrun")
                    transition.planned_end_frame, transition.end_frame = active.end_frame, state.frame
                end
                transition.completed = true; stop(nil, "B_transition_complete"); return
            end
        elseif active and active.kind == "demo" then
            if scheduled then
                if state.frame >= current.midpoint_frame then
                    if state.frame > current.midpoint_frame then
                        warn("The calculated demo midpoint was overrun")
                        current.midpoint_overrun_frames = state.frame - current.midpoint_frame
                        current.midpoint_frame = state.frame
                    end
                    stop(nil, "demo_midpoint")
                end
                return
            end
            local raw = gui.gdscreenshot()
            local bar = pixels.bar(raw)
            local calculated = current.midpoint_frame ~= nil
            local at_midpoint = calculated and state.frame >= current.midpoint_frame
            if not active.seen_demo and bar.found and bar.last_x < 128 then active.seen_demo = true end
            if not bar.found then
                current.unreadable_demo_frames = current.unreadable_demo_frames + 1
                retain_demo(raw, "unreadable_demo_frame", false)
            elseif active.seen_demo then
                if state.frame - demo_last >= config.demo_sample_interval_frames or at_midpoint or not calculated and bar.last_x >= 128 then
                    retain_demo(raw, "frame_callback")
                end
                if not calculated and bar.last_x >= 128 then
                    current.midpoint_frame = state.frame
                    if stage5 then
                        assert(current.index == 1, "Only the initial unreadable entry anchor uses visual fallback")
                        midpoint_frames = calculate_midpoints(state.frame)
                        midpoint_source = "calculated_from_visual_fallback_anchor"
                    end
                    stop(nil, "demo_midpoint", raw); return
                end
            end
            if at_midpoint then
                if state.frame > current.midpoint_frame then
                    warn("The calculated demo midpoint was overrun")
                    current.midpoint_overrun_frames = state.frame - current.midpoint_frame
                    current.midpoint_frame = state.frame
                end
                stop(nil, "demo_midpoint", raw); return
            end
        elseif active and active.kind == "mirror" then
            local raw
            if scheduled and not current.skip_response then
                if scheduled_feedback then
                    for index, press in ipairs(plan.presses) do
                        if checks_feedback(index) and not press.skipped and not first_feedback_verified then
                            if state.frame == press.frame - 1 then
                                raw = gui.gdscreenshot()
                                last_feedback = pixels.feedback(raw, plan.player_center_x)
                                write(string.format("%s/feedback-before-%d.gd", current.work, index), raw, "wb")
                            elseif press.started and not press.judgment and state.frame >= plan.feedback_frames[index]
                                and state.frame <= plan.feedback_deadline_frames[index] then
                                raw = gui.gdscreenshot()
                                feedback(raw, index)
                            elseif press.started and not press.judgment and state.frame > plan.feedback_deadline_frames[index] then
                                raw = gui.gdscreenshot()
                                retain_judgment(index, nil, raw, false, "missed_feedback_window")
                            end
                        end
                    end
                    if warn_first_feedback and current.index == 1 and not first_feedback then
                        local first
                        for index, press in ipairs(plan.presses) do
                            if checks_feedback(index) then first = press; break end
                        end
                        if not first or first.skipped then
                            raw = raw or gui.gdscreenshot()
                            retain_first_skip(raw, first and first.skip_reason or plan.skip_reason or "first_input_unavailable")
                        end
                    end
                elseif current.index == 1 and not first_feedback_verified then
                    if plan.presses[1] and inputs == 0 and state.frame == plan.presses[1].frame - 1 then
                        raw = gui.gdscreenshot()
                        last_feedback = pixels.feedback(raw, plan.player_center_x)
                        write(current.work .. "/feedback-before.gd", raw, "wb")
                    elseif state.frame == plan.first_feedback_frame and plan.presses[1].started and not plan.presses[1].judgment then
                        raw = gui.gdscreenshot()
                        feedback(raw)
                        if not first_feedback then
                            retain_judgment(1, nil, raw, false, "excellent_palette_not_detected")
                        end
                    end
                end
            elseif not scheduled and not current.skip_response then
                raw = gui.gdscreenshot()
                feedback(raw)
            end
            if stage5 and not record_feedback and current.index == 1 and not first_feedback then
                local first_index
                for index, press in ipairs(plan.presses) do
                    if (press.input_index or index) == 1 then first_index = index; break end
                end
                local first = first_index and plan.presses[first_index]
                if not first or first.skipped then
                    raw = raw or gui.gdscreenshot()
                    retain_first_skip(raw, first and first.skip_reason or plan.skip_reason or "first_input_unavailable")
                elseif state.frame >= plan.response_end_frame and first.started and not first.judgment then
                    raw = raw or gui.gdscreenshot()
                    retain_judgment(first_index, nil, raw, false, "excellent_palette_not_detected")
                end
            end
            if record_feedback then
                local settled, held_until = true, plan.frame
                for _, press in ipairs(plan.presses) do
                    if not press.started and not press.skipped then settled = false end
                    if press.started then held_until = math.max(held_until, press.frame + plan.hold_frames) end
                end
                local final = judgments[#judgments]
                local endpoint = not current.skip_response and final and final.feedback_frame or plan.response_end_frame
                if settled and #judgments == inputs and state.frame >= math.max(endpoint, held_until) then
                    account_response()
                    raw = raw or gui.gdscreenshot()
                    write(current.work .. "/response-end.gd", raw, "wb")
                    stop(nil, "dance_response_complete", raw)
                end
                return
            end
            if stage5 and not record_feedback and current.index == 1 and first_feedback and not first_feedback_verified then
                local settled, held_until = true, plan.frame
                for _, press in ipairs(plan.presses) do
                    if not press.started and not press.skipped then settled = false end
                    if press.started then held_until = math.max(held_until, press.frame + plan.hold_frames) end
                end
                if settled and state.frame >= math.max(plan.response_end_frame, held_until) then account_response() end
                stop(nil, "offset_feedback_ready", raw); return
            end
            local counted = plan.response_end_frame ~= nil
            local end_frame = plan.response_end_frame
            local endpoint = counted and state.frame >= end_frame
            if not counted then
                local bar = pixels.bar(raw)
                endpoint = not bar.found or bar.last_x < 128
            end
            local feedback_complete = all_feedback and #judgments == inputs
                or not all_feedback and first_feedback_verified
            if all_feedback and counted and endpoint and not feedback_complete then
                for index, press in ipairs(plan.presses) do
                    if press.started and not press.judgment then
                        raw = raw or gui.gdscreenshot()
                        retain_judgment(index, nil, raw, false, "excellent_palette_not_detected")
                    end
                end
                feedback_complete = #judgments == inputs
            end
            local inputs_complete = inputs + runtime_skip_count() == planned_count()
            local held_until = plan.frame
            for _, press in ipairs(plan.presses) do
                if press.started then held_until = math.max(held_until, press.frame + plan.hold_frames) end
            end
            if inputs_complete and feedback_complete and state.frame >= held_until and endpoint then
                account_response()
                raw = raw or gui.gdscreenshot()
                response_complete = true; write(current.work .. "/response-end.gd", raw, "wb")
                stop(nil, "dance_response_complete", raw); return
            end
        end
    end)
    if not ok then stop(tostring(message)); return end
    if state.frame % 15 == 0 then publish() end
end

if runtime then
    return {definitions = definitions, snapshot = snapshot, observation = observation,
        before = function()
            local ok, message = pcall(before_frame)
            if not ok then stop(tostring(message)) end
        end,
        after = after_frame, finish = finish,
        checkpoint = function(action, args)
            idle, state.frame = true, adapter.frame()
            local ok, result = pcall(dispatch, action, args)
            idle = false
            if not ok then
                local message = tostring(result)
                if action ~= "abort" and action ~= "terminal" then
                    if action == "plan" then plan = nil end
                    finish(message, action)
                    local value = packet()
                    value.warning, value.recovery_required = state.dance_warning, true
                    return value
                end
                if pending then latest = engine:cancel(message); pending = false end
                finish(message)
                return {frame = adapter.frame(), epoch = adapter.epoch(), checkpoint = latest, error = message}
            end
            return result
        end}
end

emu.registermenustart(checkpoint_callback)
emu.registerbefore(function()
    local ok, message = pcall(before_frame)
    if not ok then stop(tostring(message)) end
end)
emu.registerafter(after_frame)

release(); mailbox:capture_screen(); publish(); emu.pause()
print("Rank-selected six-dance adapter ready.")
