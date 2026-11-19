-- Main-run six-response adapter; the persistent dispatcher owns callbacks and saves.
local config, runtime = ...
assert(type(config) == "table" and config.stage == 5 and type(runtime) == "table",
    "Dancing requires the persistent main runtime")
local START, FINISH, OFFSET = "rank-dance-start", "rank-dance-finish", "rank-dance-offset"
local directory = assert(debug.getinfo(1, "S").source:sub(2):gsub("\\", "/"):match("^(.*)/"))
local root = dofile(directory .. "/resource_root.lua")
local bridge_io = dofile(root .. "/lua/bridge_io.lua")
local platinum = dofile(root .. "/lua/platinum.lua")
local json = dofile(root .. "/lua/json.lua")
local pixels = dofile(directory .. "/dance_pixels.lua")
local mailbox_directory = runtime.mailbox_directory
local mailbox = bridge_io.new(mailbox_directory)
local idle = false
local engine, latest, pending, work, current, active, plan = runtime.engine, nil, nil, config.work
local rank = config.target and config.target.rank or config.rank
local cue_count = rank == "Normal" and 3 or 4
local resolved_cue_offset = config.rank_timing and config.rank_timing.cue_side_offset_pixels
local initialized, first_feedback_verified = false, false
local first_feedback, lead_dances, transition, response_events
local inputs, completed, total_inputs, total_judgments, total_excellent = 0, 0, 0, 0, 0
local total_skipped_inputs, response_skips = 0, setmetatable({}, bridge_io.array_metatable)
local retain_judgment, retain_first_skip
local last_feedback, response_complete, halted, captures = nil, false, false, 0
local midpoint_frames, midpoint_source
local judgments = setmetatable({}, bridge_io.array_metatable)
local state, adapter = runtime.state, runtime.adapter

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

local function close_files()
    local file = response_events
    response_events = nil
    if file then
        local ok, closed = pcall(file.close, file)
        if not ok or not closed then warn("Capture stream could not be closed") end
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
    return press ~= nil and current.index == 1 and (press.input_index or index) == 1
end

local function first_judgment_count()
    return first_feedback and first_feedback.skipped and 0 or 1
end

local function timing_origin()
    local anchor = plan.counted_midpoint_anchor
    return anchor and anchor.frame or plan.frame, anchor and anchor.pointer_x or plan.pointer_x
end

local function counted_anchor_valid(frame)
    local anchor = plan and plan.counted_midpoint_anchor
    local accepted = current and current.correction_anchor
    return type(anchor) == "table" and accepted ~= nil
        and anchor.source == accepted.source and anchor.frame == accepted.frame
        and anchor.epoch == accepted.epoch and anchor.epoch == adapter.epoch()
        and anchor.pointer_x == accepted.pointer_x and anchor.pointer_x == plan.pointer_x
        and plan.frame >= anchor.frame + math.ceil((128 - anchor.pointer_x) / plan.speed)
        and frame >= plan.frame or false
end

local function response_timing_ready(frame, bar)
    local pointer = plan and plan.pointer_x
    local bounds = bar and bar.position_bounds
    return type(pointer) == "number" and pointer >= 8 and pointer <= 247
        and bounds ~= nil and bounds[1] <= pointer and pointer <= bounds[2]
        and (pointer >= 128 or counted_anchor_valid(frame)) or false
end

local function completed_count()
    return response_complete and current and math.max(completed, current.index) or completed
end

local function observation(frame, raw)
    local excellent = 0
    for _, judgment in ipairs(judgments) do
        if judgment.label == "Excellent" then excellent = excellent + 1 end
    end
    return {frame = frame, epoch = adapter.epoch(), game = platinum.game(emu.gamecode()),
        bar = raw and pixels.bar(raw), rank = rank, rank_source = "accepted_contest_target",
        target = config.target, source_context = config.source_context, rank_timing = config.rank_timing,
        party_context = config.party_context, observation_policy = "scheduled",
        rank_profile_matches_target = config.rank_timing and config.target
            and config.rank_timing.rank == config.target.rank and rank == config.target.rank
            and config.rank_timing.category == config.target.category or false,
        offset_check = first_feedback, lead_dances = lead_dances, transition = transition,
        midpoint_timing = current and {source = current.midpoint_source, expected_frame = current.midpoint_frame,
            entry_frame = config.source_frame, entry_pointer_x = config.midpoint_timing and config.midpoint_timing.entry_pointer_x},
        released = state.buttons == "" and not state.stylus_touch,
        trial = {dance = current and current.index or 0, completed_dances = completed_count(), acknowledged_dances = completed,
            cues_per_dance = cue_count, required_response_inputs = 6 * cue_count,
            inputs = inputs, judgments = judgments, judgment_count = #judgments,
            planned_input_count = planned_count(), runtime_skipped_inputs = runtime_skip_count(),
            skipped_input_count = cue_count - planned_count() + runtime_skip_count(), tolerant_inputs = true,
            response_started = current and current.start_acknowledged == true or false,
            total_skipped_inputs = total_skipped_inputs, skipped_inputs = response_skips,
            presses = plan and plan.presses, conflict_count = plan and plan.cues and #(plan.cues.conflicts or {}) or 0,
            excellent_count = excellent, miss_count = #judgments - excellent, complete = response_complete,
            total_inputs = total_inputs, total_judgments = total_judgments, total_excellent = total_excellent,
            total_input_count = total_inputs, total_judgment_count = total_judgments, total_excellent_count = total_excellent,
            feedback_policy = "first_button_only", total_miss_count = total_judgments - total_excellent,
            required_judgments_so_far = first_judgment_count(), required_first_feedback_judgments = first_judgment_count(),
            feedback_policy_met = first_feedback_verified and total_judgments == first_judgment_count(),
            first_feedback_verified = first_feedback_verified, first_feedback_policy = "warn_and_continue",
            first_feedback_accepted = first_feedback ~= nil, released = state.buttons == "" and not state.stylus_touch,
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
    local ready = response_timing_ready(frame, pixels.bar(raw))
    if plan and frame == plan.frame and not response_complete then
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
        and #plan.presses <= cue_count and cues ~= nil and #cues == #plan.presses
    consistent = consistent and cue_side_offset == config.rank_timing.cue_side_offset_pixels
    if plan then
        consistent = consistent and (not plan.counted_midpoint_anchor or counted_anchor_valid(frame))
        local origin_frame, origin_x = timing_origin()
        for index, press in ipairs(plan.presses) do
            local cue = cues and cues[index]
            local due = origin_frame + math.floor((press.response_x - origin_x) / plan.speed + 0.5) + plan.offset_frames
            local following = plan.presses[index + 1] and plan.presses[index + 1].frame or plan.predicted_end_frame
            if not cue or cue.button ~= press.button or cue.response_x ~= press.response_x
                or cue.response_x ~= cue.demo_x + cue_side_offset
                or press.frame ~= due or not press.skipped and (press.frame <= plan.frame
                    or press.frame + plan.hold_frames >= following) then consistent = false end
            local evidence = cue and cue.evidence or {}
            local left, right = evidence.left, evidence.right
            if not left and not right then consistent = false end
            if left and (left.x ~= cue.demo_x or left.button ~= cue.button) then consistent = false end
            if right and (right.x ~= cue.response_x or right.button ~= cue.button) then consistent = false end
        end
        consistent = consistent and plan.cues.side_offset_source == "configured_cue_spacing"
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
    value.trial.response_timing_ready = ready
    value.trial.counted_midpoint_anchor_bound = value.trial.midpoint_pointer_bound and counted_anchor_valid(frame)
    value.trial.midpoint_policy_met = value.trial.midpoint_within_line or value.trial.counted_midpoint_anchor_bound
        or planned_count() == runtime_skip_count() or value.trial.midpoint_pointer_bound and plan.pointer_x >= 128
    value.trial.midpoint_adjustment_count = current and current.midpoint_adjustments or 0
    value.trial.before_first_input = plan ~= nil
    for _, press in ipairs(plan and plan.presses or {}) do
        if not press.skipped and frame >= press.frame then value.trial.before_first_input = false end
    end
    value.trial.at_response_endpoint = plan ~= nil and frame >= plan.response_end_frame
    local first_image = first_feedback and first_feedback.image
    value.trial.first_feedback_bound = first_image ~= nil and first_image.frame == first_feedback.feedback_frame
        and first_image.epoch == adapter.epoch() and first_image.frame <= frame
    value.stop_reason = state.error ~= "" and state.error or "host_stop"
    return value
end

local function common()
    return {{id = "platinum", field = "game.supported", expected = true},
        {id = "released", field = "released", expected = true},
        {id = "frame", field = "frame", context = "frame"},
        {id = "epoch", field = "epoch", context = "epoch"},
        {id = "fresh_capture", field = "image.direct_read_frames", expected = 0},
        {id = "sequence", field = "trial.sequence_valid", expected = true},
        {id = "rank_profile", field = "rank_profile_matches_target", expected = true}}
end

local function definitions()
    local result = {
        [START] = {checks = common(), target = {
            {id = "midpoint", field = "trial.at_authorized_midpoint", expected = true},
            {id = "plan", field = "trial.plan_consistent", expected = true},
            {id = "planned_cues_bound", field = "trial.planned_cues_bound", expected = true},
            {id = "fresh_visual_binding", field = "trial.fresh_visual_binding", expected = true},
            {id = "before_input", field = "trial.before_first_input", expected = true},
            {id = "midpoint_policy", field = "trial.midpoint_policy_met", expected = true},
        }, branches = {hit = "response", miss = "skip_response"}},
        [FINISH] = {checks = common(), target = {
            {id = "response_inputs", expected = true, evaluate = function(snapshot)
                local actual, required = snapshot.trial.inputs,
                    snapshot.trial.planned_input_count - snapshot.trial.runtime_skipped_inputs
                return actual == required, {count = actual, required = required}
            end},
            {id = "offset_verified", field = "trial.first_feedback_verified", expected = true},
            {id = "counted_response_endpoint", field = "trial.at_response_endpoint", expected = true},
            {id = "accounted_inputs", expected = true, evaluate = function(snapshot)
                local trial = snapshot.trial
                return trial.total_inputs + trial.total_skipped_inputs == trial.dance * trial.cues_per_dance
            end},
            {id = "feedback_policy_met", field = "trial.feedback_policy_met", expected = true},
            {id = "verified_feedback_total", expected = true, evaluate = function(snapshot)
                local actual, required = snapshot.trial.total_judgment_count, snapshot.trial.required_judgments_so_far
                return actual == required, {count = actual, required = required}
            end},
            {id = "response_complete", field = "trial.complete", expected = true},
        }, branches = {hit = "next_dance", miss = "next_dance"}},
        [OFFSET] = {checks = common(), target = {
            {id = "first_input_only", expected = true, evaluate = function(snapshot) return snapshot.trial.total_inputs <= 1 end},
            {id = "accepted_feedback", field = "trial.first_feedback_accepted", expected = true},
            {id = "feedback_capture_bound", field = "trial.first_feedback_bound", expected = true},
        }, branches = {hit = "mirror_rest", miss = "mirror_rest"}},
        ["stage-5-dance"] = {checks = {
            {id = "platinum", field = "game.supported", expected = true},
            {id = "released", field = "released", expected = true},
            {id = "frame", field = "frame", context = "frame"},
            {id = "epoch", field = "epoch", context = "epoch"},
            {id = "fresh_capture", field = "image.direct_read_frames", expected = 0},
        }, target = {
            {id = "six_answered_dances", field = "trial.completed_dances", expected = 6},
            {id = "response_input_total", expected = true, evaluate = function(snapshot)
                local trial = snapshot.trial
                return trial.total_input_count + trial.total_skipped_inputs == trial.required_response_inputs,
                    {count = trial.total_input_count, skipped = trial.total_skipped_inputs, required = trial.required_response_inputs}
            end},
            {id = "first_press_feedback", field = "trial.first_feedback_accepted", expected = true},
            {id = "offset_verified", field = "trial.first_feedback_verified", expected = true},
            {id = "single_feedback_check", expected = true, evaluate = function(snapshot)
                return snapshot.trial.total_judgment_count == snapshot.trial.required_first_feedback_judgments
            end},
            {id = "lead_dances_complete", field = "lead_dances.completed", expected = true},
            {id = "lead_input_policy", field = "lead_dances.input_policy", expected = "none"},
            {id = "no_lead_inputs", field = "lead_dances.input_count", expected = 0},
            {id = "B_transition_complete", field = "transition.completed", expected = true},
            {id = "transition_endpoint", field = "trial.acting_endpoint_met", expected = true},
        }, branches = {hit = "finish", miss = "finish"}},
        stopped = {checks = {{id = "host_stop", field = "stop_reason", expected = "continue"}}, branches = {pass = "stopped"}},
    }
    return result
end

local function packet(value)
    local result = {frame = adapter.frame(), epoch = adapter.epoch(), observation = value,
        checkpoint = latest, completed_dances = completed_count(), next_section = state.phase}
    if engine and not pending then result.progress = engine:state() end
    return result
end

local function finish(message, reason, fatal)
    if message and not fatal then warn(message); message, reason = nil, "dance_recovery_ready" end
    if message then runtime.fail_stage() end
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

local function dispatch(action, args)
    if action == "init" then
        assert(engine and not initialized and config.source_frame == adapter.frame(),
            "Initialize from the current accepted Stage 4 frame without advancing")
        local profile = config.rank_timing
        assert(profile and config.target and profile.rank == config.target.rank
            and profile.pixels_per_frame > 0 and profile.cues_per_dance == cue_count
            and config.cues_per_dance == cue_count and config.required_response_inputs == 6 * cue_count,
            "The dance timing profile must match the accepted target rank")
        assert(config.feedback_policy == "first_button_only" and config.first_feedback_policy == "warn_and_continue"
            and config.observation_policy == "scheduled" and config.fixed_metrics == true
            and config.tolerant_inputs == true and config.required_excellent_judgments == 0,
            "Main dancing uses fixed scheduled metrics and first-input feedback without an Excellent requirement")
        assert(runtime.main_dance_feedback, "The persistent runner must account for its first feedback result")
        assert(type(config.feedback_release_frames) == "number" and config.feedback_release_frames >= 1
            and config.feedback_release_frames == math.floor(config.feedback_release_frames)
            and config.first_feedback_delay_frames == config.hold_frames + config.feedback_release_frames - 1,
            "Schedule feedback after the complete held and released input block")
        local speed, short, switch = profile.pixels_per_frame, profile.dance_gap_frames, profile.leader_gap_frames
        local family = (rank == "Normal" or rank == "Super") and "Normal/Super" or config.target.category
        local native = runtime.timing_profile.sections.fixed_dance_metrics[family]
        assert(profile.category == config.target.category and profile.timing_source == "fixed_normalized_contest_measurements"
            and speed == native.pointer_speed_numerator / native.pointer_speed_denominator
            and short == native.same_leader_gap_frames and switch == native.leader_change_gap_frames
            and profile.cue_side_offset_pixels == native.cue_side_offset_pixels
            and short >= 0 and short == math.floor(short) and switch >= short and switch == math.floor(switch)
            and profile.cycle_frames == math.floor(240 / speed + short)
            and profile.response_frames == math.floor(120 / speed) and profile.extra_switch_frames == switch - short,
            "Use the selected regional category/rank's fixed measured speed, cue spacing, and normalized gaps")
        assert(config.transition and config.transition.mash_frames > 0, "The counted Acting transition is required")
        local timing = assert(config.midpoint_timing, "Declare the accepted Stage 4 midpoint anchor")
        local pointer = config.accepted_entry_pointer
        local bounds = pointer and pointer.position_bounds
        assert(timing.entry_frame == config.source_frame and pointer and pointer.status == "exact"
            and bounds and bounds[1] == bounds[2] and timing.entry_pointer_x == bounds[1]
            and type(timing.entry_pointer_x) == "number" and timing.entry_pointer_x >= 8 and timing.entry_pointer_x < 128
            and timing.entry_pointer_x == math.floor(timing.entry_pointer_x),
            "Use the readable pointer from the acknowledged Stage 4 entry")
        assert(timing.source == "accepted_stage4_pointer_and_fixed_metrics" and timing.capture_target_x == 127
            and timing.rounding == "floor" and timing.accepted_pointer_pixels
            and timing.accepted_pointer_pixels[1] == 128 and timing.accepted_pointer_pixels[2] == 129,
            "Midpoint captures must favor early arrival and accept the two native line pixels")
        local first = config.source_frame + math.max(1, math.floor((127 - timing.entry_pointer_x) / speed))
        midpoint_frames = calculate_midpoints(first)
        assert(type(timing.frames) == "table" and #timing.frames == 6, "Calculate all six response midpoints")
        for index = 1, 6 do
            assert(timing.frames[index] == midpoint_frames[index], "The midpoint schedule must use the accepted pointer and fixed timing")
        end
        midpoint_source, initialized = timing.source, true
    elseif action == "select-dance" then
        local index, gap = tonumber(args[1]), tonumber(args[3])
        assert(initialized and engine and not active and not pending and not halted and #args == 3
            and index == completed + 1 and index <= 6, "Select the next authorized dance")
        assert(index == 1 or latest and latest.checkpoint == FINISH and latest.proceed, "Acknowledge the prior response")
        assert(gap == ((index == 3 or index == 5) and config.rank_timing.extra_switch_frames or 0), "Use the selected leader-switch gap")
        current = {index = index, work = args[2], gap = gap, gap_complete = gap == 0, midpoint_adjustments = 0,
            midpoint_frame = midpoint_frames[index], midpoint_source = midpoint_source}
        plan, inputs, response_complete, last_feedback = nil, 0, false, nil
        judgments = setmetatable({}, bridge_io.array_metatable)
        response_skips = setmetatable({}, bridge_io.array_metatable)
    elseif action == "capture" then
        local image, raw = capture(args[1])
        local value = observation(adapter.frame(), raw); value.image = image
        return packet(value)
    elseif action == "retime-midpoint" then
        assert(current and not plan and not active and not pending and not halted
            and current.midpoint_frame == adapter.frame() and state.stop_reason == "demo_midpoint" and #args == 1,
            "Adjust only a fresh calculated midpoint that is still in the demonstration")
        local observed = assert(loadfile(args[1]))()
        assert(observed.frame == adapter.frame() and observed.epoch == adapter.epoch()
            and type(observed.pointer_x) == "number" and observed.pointer_x == math.floor(observed.pointer_x)
            and observed.pointer_x >= 8 and observed.pointer_x < 128,
            "The midpoint adjustment needs a readable paused demo pointer")
        local frames = math.ceil((128 - observed.pointer_x) / config.rank_timing.pixels_per_frame)
        assert(observed.next_frame == observed.frame + frames and frames > 0, "Calculate the remaining pointer travel in emulator frames")
        local bounds = pixels.bar(gui.gdscreenshot()).position_bounds
        assert(bounds and bounds[1] <= observed.pointer_x and observed.pointer_x <= bounds[2],
            "Bind the correction anchor to the fresh paused pointer position")
        current.correction_anchor = {frame = observed.frame, epoch = observed.epoch, pointer_x = observed.pointer_x,
            source = "fresh_paused_midpoint_before_counted_correction"}
        current.midpoint_frame, current.midpoint_source = observed.next_frame, "calculated_from_fresh_demo_pointer"
        current.midpoint_adjustments = current.midpoint_adjustments + 1
    elseif action == "plan" then
        assert(current and not active and not pending and not halted, "Plan one paused response")
        plan = assert(loadfile(args[1]))()
        assert(plan.frame == adapter.frame() and plan.epoch == adapter.epoch() and plan.dance == current.index
            and plan.rank == rank and plan.speed > 0 and #plan.presses <= cue_count, "The plan belongs to this midpoint")
        assert(not plan.counted_midpoint_anchor or counted_anchor_valid(adapter.frame()),
            "Use the same-epoch verified pointer origin after its counted correction")
        assert(#plan.presses == 0 or plan.presses[#plan.presses].frame + plan.hold_frames < plan.response_end_frame,
            "The final input must fit before the counted response endpoint")
        local endpoint
        if plan.skip_reason then
            local anchor = current.midpoint_frame or plan.fallback_midpoint_frame or plan.frame
            current.midpoint_frame = anchor
            assert(#plan.presses == 0 and plan.pointer_x == nil and plan.fallback_midpoint_frame == anchor,
                "An unreadable response uses its counted midpoint for an input-free skip")
            endpoint = math.max(plan.frame + 1, anchor + math.ceil(1 / plan.speed) + config.rank_timing.response_frames)
        else
            local origin_frame, origin_x = timing_origin()
            endpoint = math.max(plan.frame + 1,
                math.floor(origin_frame + (128 - origin_x) / plan.speed + config.rank_timing.response_frames + 0.5))
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
        assert(plan.cues and (plan.cues.side_offset_pixels or resolved_cue_offset) == config.rank_timing.cue_side_offset_pixels,
            "Cue spacing must match the fixed category/rank profile")
        assert(type(plan.feedback_frames) == "table" and #plan.feedback_frames == #plan.presses
            and type(plan.feedback_deadline_frames) == "table" and #plan.feedback_deadline_frames == #plan.presses,
            "Use the shared released-input feedback capture delay")
        local gap = current.index % 2 == 0 and config.rank_timing.leader_gap_frames or config.rank_timing.dance_gap_frames
        assert(plan.next_demo_frame == plan.response_end_frame + gap, "The next demo retains its fixed response-end timing anchor")
        for index, press in ipairs(plan.presses) do
            local following = plan.presses[index + 1]
            local limit = following and math.min(following.frame - 1, plan.response_end_frame)
                or math.max(plan.response_end_frame, plan.feedback_frames[index])
            assert(plan.feedback_frames[index] == press.frame + config.first_feedback_delay_frames
                and plan.feedback_deadline_frames[index] == limit and plan.feedback_frames[index] <= limit
                and (limit <= plan.response_end_frame or limit < plan.next_demo_frame),
                "Bound feedback recovery by the next input or response end, retaining the full final released wait")
        end
        if not plan.skip_reason then
            local origin_frame, origin_x = timing_origin()
            local origin = origin_frame + math.floor((127 - origin_x) / plan.speed)
            for index = current.index + 1, 6 do
                midpoint_frames[index] = origin + (index - current.index) * config.rank_timing.cycle_frames
                    + (math.floor((index - 1) / 2) - math.floor((current.index - 1) / 2)) * config.rank_timing.extra_switch_frames
            end
            midpoint_source = plan.counted_midpoint_anchor and "calculated_from_counted_midpoint_and_fixed_metrics"
                or "calculated_from_fresh_midpoint_and_fixed_metrics"
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
        if current.index == 1 and not first_feedback then retain_first_skip(raw, args[3], "paused_response_recovery") end
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
        assert(args[1] == START or args[1] == FINISH or args[1] == OFFSET or args[1] == "stage-5-dance",
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
        finish(args[1] or "Host stopped dancing", nil, true)
        if pending then latest = engine:cancel(state.error); pending = false
        elseif engine and not engine:state().halted then
            latest = engine:visit("stopped", {frame = adapter.frame(), epoch = adapter.epoch(), reason = state.error})
            pending = latest.status == "awaiting_save" or latest.status == "awaiting_ack"
        end
    elseif action == "terminal" then
        finish(nil, "terminal")
    else
        error("Unknown main dance checkpoint action: " .. tostring(action))
    end
    return packet()
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
        if current.midpoint_frame <= state.frame then
            current.midpoint_overrun_frames = state.frame - current.midpoint_frame
            current.midpoint_frame, current.midpoint_source = state.frame + 1, "late_counted_midpoint"
        end
        assert(current.midpoint_frame > state.frame, "The calculated midpoint must still be ahead of the current frame")
        active = {kind = "demo"}
    elseif parts[2] == "mirror" or parts[2] == "mirror-rest" then
        local expected = parts[2] == "mirror" and START or OFFSET
        assert(plan and latest and latest.proceed and (current.skip_response and current.start_acknowledged
            or latest.checkpoint == expected and latest.frame == state.frame), "Acknowledge the midpoint before responding")
        assert(parts[2] ~= "mirror-rest" or current.skip_response or first_feedback_verified and inputs <= 1,
            "The accepted first-button judgment must authorize the remaining inputs")
        if response_complete then stop(nil, "dance_response_complete"); return end
        response_events = response_events or assert(io.open(current.work .. "/response-events.jsonl",
            (current.skip_response or parts[2] == "mirror-rest") and "a" or "w"))
        active = {kind = "mirror"}
        if not current.skip_response and current.index == 1 and not first_feedback_verified then
            last_feedback = pixels.feedback(gui.gdscreenshot(), plan.player_center_x)
        end
    elseif parts[2] == "lead-dances" then
        assert(completed == 6 and latest and latest.checkpoint == FINISH and latest.proceed,
            "Acknowledge the sixth response before the input-free dances")
        local endpoint = math.max(state.frame + 1, plan.response_end_frame + config.rank_timing.lead_dances_frames)
        lead_dances = lead_dances or {first_dance = 7, last_dance = 8, input_policy = "none", input_count = 0,
            completed = false, start_frame = state.frame, end_frame = endpoint, duration_frames = endpoint - state.frame}
        if lead_dances.completed then stop(nil, "lead_dances_complete"); return end
        active = {kind = "lead", end_frame = math.max(state.frame + 1, lead_dances.end_frame)}
    elseif parts[2] == "acting-transition" then
        assert(lead_dances and lead_dances.completed and (transition or state.frame == lead_dances.end_frame),
            "Complete both input-free dances before the Acting transition")
        transition = transition or {start_frame = state.frame, end_frame = state.frame + config.transition.mash_frames,
            game_seconds = config.transition.game_seconds, mash_frames = config.transition.mash_frames,
            mash_hold_frames = config.transition.mash_hold_frames, mash_release_frames = config.transition.mash_release_frames,
            button = "B", press_count = 0, completed = false}
        if transition.completed then stop(nil, "B_transition_complete"); return end
        active = {kind = "transition", end_frame = math.max(state.frame + 1, transition.end_frame)}
    else
        error("Unknown main dance input action: " .. tostring(parts[2]))
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
        for index, press in ipairs(plan.presses) do
            if not press.started and frame > press.frame then skip_press(index, "missed_input_frame") end
            if checks_feedback(index) and press.started and not press.judgment and state.frame > plan.feedback_deadline_frames[index] then
                retain_judgment(index, nil, gui.gdscreenshot(), false, "missed_feedback_window", "before_frame_feedback_recovery")
            end
            if current.index == 1 and not first_feedback_verified and inputs >= 1 and not checks_feedback(index) and frame == press.frame then
                skip_press(index, "first_feedback_pending")
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
    assert(checks_feedback(index) and press.started and not press.judgment, "Retain one result for the first executed mirrored input")
    local label = fresh and found.label == "Excellent" and "Excellent" or "Miss"
    local path = string.format("%s/judgment-%d-%s.gd", current.work, index, label)
    local judgment = {dance = current.index, index = index, button = press.button, label = label,
        input_frame = press.frame, feedback_frame = state.frame, visible_delay_frames = state.frame - press.frame,
        feedback_x = found and found.x, feedback_y = found and found.y,
        source = found and found.source or "excellent_palette_not_detected", fresh_message = fresh, recovery_reason = reason,
        image = {path = path, frame = state.frame, epoch = adapter.epoch(), source = "input_feedback_framebuffer",
            capture_callback = capture_callback or "after_frame_feedback", direct_read_frames = 0},
        scheduled_feedback_frame = plan.feedback_frames[index], feedback_deadline_frame = plan.feedback_deadline_frames[index],
        feedback_sample_count = press.feedback_sample_count or 0, release_frames_before_initial_capture = config.feedback_release_frames,
        recovered_after_initial_capture = fresh and state.frame > plan.feedback_frames[index]}
    press.judgment = judgment; judgments[#judgments + 1] = judgment
    first_feedback = judgment
    judgment.warning = runtime.main_dance_feedback(judgment, config).warning
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
        image = {path = path, frame = state.frame, epoch = adapter.epoch(), source = "skipped_input_framebuffer",
            capture_callback = capture_callback or "after_frame", direct_read_frames = 0}}
    write(path, raw, "wb")
    judgment.warning = runtime.main_dance_feedback(judgment, config).warning
    first_feedback = judgment
    write(current.work .. "/first-input-result.json", json.encode(judgment) .. "\n")
    assert(response_events:write(json.encode(judgment), "\n"))
end

local function feedback(raw, index)
    if first_feedback_verified or current.index ~= 1 or inputs == 0 then return end
    local press = assert(plan.presses[index], "Capture feedback for the first scheduled input")
    local initial, limit = plan.feedback_frames[index], plan.feedback_deadline_frames[index]
    assert(press.started and not press.judgment and state.frame >= initial and state.frame <= limit,
        "Capture unresolved feedback within its counted recovery window")
    local found = pixels.feedback(raw, plan.player_center_x)
    press.feedback_sample_count = (press.feedback_sample_count or 0) + 1
    local before = press.feedback_before
    local fresh = found ~= nil and (not before or found.label ~= before.label or found.y > before.y + 2)
    if fresh or state.frame == limit then
        retain_judgment(index, found, raw, fresh, not fresh and (found and "stale_message" or "excellent_palette_not_detected") or nil)
    else
        local path = string.format("%s/feedback-recovery-%d-%d.gd", current.work, index, state.frame)
        write(path, raw, "wb")
        assert(response_events:write(json.encode({kind = "feedback_pending", dance = current.index,
            index = index, frame = state.frame, epoch = adapter.epoch(), image = path,
            observed_label = found and found.label, reason = found and "stale_message" or "excellent_palette_not_detected"}), "\n"))
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
            if state.frame >= current.midpoint_frame then
                if state.frame > current.midpoint_frame then
                    warn("The calculated demo midpoint was overrun")
                    current.midpoint_overrun_frames = state.frame - current.midpoint_frame
                    current.midpoint_frame = state.frame
                end
                stop(nil, "demo_midpoint")
            end
            return
        elseif active and active.kind == "mirror" then
            local raw
            if not current.skip_response then
                for index, press in ipairs(plan.presses) do
                    if checks_feedback(index) and not press.skipped and not first_feedback_verified then
                        if state.frame == press.frame - 1 then
                            raw = gui.gdscreenshot()
                            last_feedback = pixels.feedback(raw, plan.player_center_x)
                            write(string.format("%s/feedback-before-%d.gd", current.work, index), raw, "wb")
                        elseif press.started and not press.judgment and state.frame >= plan.feedback_frames[index]
                            and state.frame <= plan.feedback_deadline_frames[index] then
                            raw = gui.gdscreenshot(); feedback(raw, index)
                        elseif press.started and not press.judgment and state.frame > plan.feedback_deadline_frames[index] then
                            raw = gui.gdscreenshot(); retain_judgment(index, nil, raw, false, "missed_feedback_window")
                        end
                    end
                end
            end
            if current.index == 1 and not first_feedback then
                local first_index
                for index, press in ipairs(plan.presses) do
                    if (press.input_index or index) == 1 then first_index = index; break end
                end
                local first = first_index and plan.presses[first_index]
                if not first or first.skipped then
                    raw = raw or gui.gdscreenshot()
                    retain_first_skip(raw, first and first.skip_reason or plan.skip_reason or "first_input_unavailable")
                elseif state.frame >= plan.response_end_frame and first.started and not first.judgment then
                    raw = raw or gui.gdscreenshot(); retain_judgment(first_index, nil, raw, false, "excellent_palette_not_detected")
                end
            end
            if current.index == 1 and first_feedback and not first_feedback_verified then
                local settled, held_until = true, plan.frame
                for _, press in ipairs(plan.presses) do
                    if not press.started and not press.skipped then settled = false end
                    if press.started then held_until = math.max(held_until, press.frame + plan.hold_frames) end
                end
                if settled and state.frame >= math.max(plan.response_end_frame, held_until) then account_response() end
                stop(nil, "offset_feedback_ready", raw); return
            end
            local endpoint = state.frame >= plan.response_end_frame
            local inputs_complete = inputs + runtime_skip_count() == planned_count()
            local held_until = plan.frame
            for _, press in ipairs(plan.presses) do
                if press.started then held_until = math.max(held_until, press.frame + plan.hold_frames) end
            end
            if inputs_complete and first_feedback_verified and state.frame >= held_until and endpoint then
                account_response()
                raw = raw or gui.gdscreenshot()
                write(current.work .. "/response-end.gd", raw, "wb")
                stop(nil, "dance_response_complete", raw); return
            end
        end
    end)
    if not ok then stop(tostring(message)); return end
    if state.frame % 15 == 0 then publish() end
end

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
