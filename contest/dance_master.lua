-- Six-response adapter, with the full Stage 5 input-free tail and Acting handoff.
local config, runtime = ...
assert(type(config) == "table" and type(config.dances) == "table", "The dance master configuration is required")
local stage5 = config.stage == 5
assert(#config.dances == (stage5 and 8 or 6), "Expected six responses or the eight-dance Stage 5 schedule")
if stage5 then
    assert(type(stylus) == "table" and type(stylus.set) == "function", "DeSmuME stylus.set is required")
    assert(type(config.transition) == "table" and config.transition.mash_frames > 0,
        "The Acting transition timing is required")
end
local refresh_frames = config.refresh_frames
local cue_count = config.cues_per_dance or 4
local source = debug.getinfo(1, "S").source:sub(2):gsub("\\", "/")
local directory = assert(source:match("^(.*)/"))
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
local checkpoint_id = checkpoint_mailbox:previous_id()
local idle, active, engine, pending, latest = false, nil, runtime and runtime.engine, nil, nil
local initialized = false
local current, origin, last_sample, last_feedback, demo_initial
local frames_file, samples_file, byte_offset = nil, nil, 0
local stage, completed_dances, input_count, total_inputs, total_judgments, total_excellent = "unconfigured", 0, 0, 0, 0, 0
local lead_dances, transition, acting_review
local feedback_failure
local first_feedback, first_feedback_verified = nil, false
local total_skipped_inputs, capture_number = 0, 0
local judgments = setmetatable({}, bridge_io.array_metatable)
local adapter = runtime and runtime.adapter or dofile(root .. "/lua/checkpoint_desmume.lua").new({
    pause = function() assert(idle, "Host-confirmed native pause is required") end,
    is_paused = function() return idle end,
})
local state = runtime and runtime.state or {protocol = 1, checkpoint_protocol = 1, bridge = stage5 and "pt-contest-stage5" or "pt-dance-master",
    game_code = emu.gamecode(), frame = emu.framecount(), phase = "ready", buttons = "",
    stylus_touch = false, error = "", command_id = mailbox:previous_id(), command_done = true}

local function release()
    bridge_io.apply_buttons(nil)
    if stage5 then stylus.set({touch = false}) end
    state.buttons, state.stylus_touch = "", false
end

local function game()
    return platinum.game(emu.gamecode())
end

local function write_file(path, value, mode)
    local file = assert(io.open(path, mode or "w"))
    assert(file:write(value))
    assert(file:close())
end

local function read_file(path)
    local file = assert(io.open(path, "rb"))
    local value = assert(file:read("*a"))
    assert(file:close())
    return value
end

local function warn(message)
    local text = string.format("Dancing: WARN - Dance %d: %s; continuing.",
        current and current.index or completed_dances + 1, tostring(message):gsub("[|\r\n]", " "))
    state.dance_warning = text
    print(text)
    return text
end

local function skipped_count()
    local count = cue_count - #(current and current.presses or {})
    for _, press in ipairs(current and current.presses or {}) do
        if press.skipped then count = count + 1 end
    end
    return count
end

local function skip_press(press, reason)
    if not press.started and not press.skipped then press.skipped, press.skip_reason = true, reason end
end

local function account_response()
    if not current.response_complete then
        total_skipped_inputs = total_skipped_inputs + cue_count - input_count
        if input_count < cue_count then warn(string.format("%d/%d inputs skipped", cue_count - input_count, cue_count)) end
    end
    current.response_complete, current.response_finish_frame = true, state.frame
end

local function completed_count()
    return current and current.response_complete and math.max(completed_dances, current.index) or completed_dances
end

local function snapshot(frame)
    local raw = gui.gdscreenshot()
    local dance = current or {index = 0, presses = {}}
    local visual = dance.visual_evidence or {}
    local cues, presses = visual.buttons or {}, dance.presses
    local consistent, excellent = #cues == #presses and #presses <= cue_count, 0
    for index, cue in ipairs(cues) do
        local press = presses[index]
        local due = dance.boundary_frame + math.floor((cue.demo_x - 8) * dance.frames_per_pixel + 0.5) + config.offset_frames
        local following = presses[index + 1] and presses[index + 1].frame or dance.end_frame
        if not press or press.button ~= cue.button or press.demo_x ~= cue.demo_x or press.frame ~= due
            or not press.skipped and (press.frame <= dance.boundary_frame or press.frame + config.hold_frames >= following) then
            consistent = false
        end
    end
    for _, judgment in ipairs(judgments) do
        if judgment.label == "Excellent" then excellent = excellent + 1 end
    end
    local bound = false
    if visual.frame == frame and frame == dance.boundary_frame and dance.work then
        local readable, boundary = pcall(read_file, dance.work .. "/boundary.gd")
        bound = readable and boundary == raw
    end
    local range = visual.tracked_progress and visual.tracked_progress.frame_range or {}
    local start_range = dance.demo_start_frame_range or {dance.demo_start_frame, dance.demo_start_frame}
    local expected_range = {}
    if #start_range == 2 and dance.boundary_frame then
        expected_range = {dance.boundary_frame - start_range[2], dance.boundary_frame - start_range[1]}
    end
    local timeline_confirmed = visual.timeline_confirmed == true and #range == 2 and #expected_range == 2
        and math.max(range[1], expected_range[1]) <= math.min(range[2], expected_range[2])
    local offset_now = stage5 and stage == "offset-check" and first_feedback
        and pixels.feedback(raw, dance.player_center_x) or nil
    local first_feedback_current = offset_now ~= nil and first_feedback.feedback_frame == frame
        and offset_now.label == first_feedback.label
    local screen = {source = "fresh_framebuffer_bound_to_host_pixel_reader_and_counted_demo_captures",
        memory_proven = false, judgment_source = "per_press_excellent_palette_and_new_message_onset"}
    if acting_review then
        screen = {source = acting_review.source, expected = "acting_entry",
            confirmed = acting_review.verdict == "pass", note = acting_review.note, memory_proven = false,
            frame = acting_review.frame, epoch = acting_review.epoch,
            fresh_visual_binding = acting_review.frame == frame and acting_review.epoch == adapter.epoch()
                 and read_file(config.work .. "/stage5.gd") == raw or false}
        if acting_review.source == "local_reference_pixels" then
            screen.pixel_check = screen_checks.read(raw, "acting_entry")
            screen.confirmed = screen.confirmed and screen.fresh_visual_binding and screen.pixel_check.confirmed
        end
    end
    capture_number = capture_number + 1
    local path = string.format("%s/checkpoint-%04d.gd", dance.work or config.work, capture_number)
    write_file(path, raw, "wb")
    local first_image = first_feedback and first_feedback.image
    return {frame = frame, epoch = adapter.epoch(), game = game(), bar = pixels.bar(raw), cues = cues, screen = screen,
        released = state.buttons == "" and (not stage5 or not state.stylus_touch),
        image = {path = path, frame = frame, epoch = adapter.epoch(), source = "fresh_zero_frame_framebuffer",
            capture_callback = "paused_lua_menu", direct_read_frames = 0},
        source_context = config.source_context, target = config.target, timing_anchor = config.timing_anchor,
        lead_dances = lead_dances, transition = transition, feedback_failure = feedback_failure,
        offset_check = first_feedback,
        trial = {dance_index = dance.index, stage = stage, input_count = input_count,
            cues_per_dance = cue_count, required_response_inputs = 6 * cue_count, tolerant_inputs = true,
            planned_input_count = #presses, skipped_input_count = skipped_count(), total_skipped_inputs = total_skipped_inputs,
            response_started = dance.start_acknowledged == true, response_complete = dance.response_complete == true,
            total_input_count = total_inputs, total_excellent_count = total_excellent,
            total_miss_count = total_judgments - total_excellent,
            total_judgment_count = total_judgments, first_feedback_verified = first_feedback_verified,
             first_feedback_current = first_feedback_current,
             first_feedback_accepted = first_feedback ~= nil,
             first_feedback_bound = first_image ~= nil and first_image.frame == first_feedback.feedback_frame
                 and first_image.epoch == adapter.epoch() and first_image.frame <= frame,
             required_first_feedback_judgments = first_feedback and first_feedback.skipped and 0 or 1,
             completed_dances = completed_count(), acknowledged_dances = completed_dances,
             released = state.buttons == "" and (not stage5 or not state.stylus_touch),
            endpoint_met = frame == dance.end_frame, at_boundary = frame == dance.boundary_frame,
            acting_endpoint_met = stage5 and frame == config.end_frame or false,
            before_first_input = #presses == 0 or presses[1].skipped or frame < presses[1].frame,
            cues_consistent = consistent, cue_count = #cues,
            visual_complete = visual.complete == true, fresh_visual_binding = bound,
            timeline_confirmed = timeline_confirmed, configured_demo_frame_range = expected_range,
            observed_demo_frame_range = range,
            visual_source = visual.source, conflict_policy = visual.conflict_policy or "unreadable",
            sequence_valid = dance.index == completed_dances + 1,
            cumulative_complete = total_inputs + total_skipped_inputs == dance.index * cue_count,
            judgment_count = #judgments, excellent_count = excellent, miss_count = #judgments - excellent, judgments = judgments,
            last_sample_frame = last_sample and last_sample.frame or frame}}
end

local function capture()
    mailbox:capture_screen()
    state.screen_frame = state.frame
end

local function publish(observation)
    state.observation = observation or {frame = state.frame, game = game(),
        lead_dances = lead_dances, transition = transition, feedback_failure = feedback_failure,
        offset_check = first_feedback,
        trial = {stage = stage, dance_index = current and current.index or 0,
            completed_dances = completed_count(), acknowledged_dances = completed_dances, input_count = input_count,
            total_input_count = total_inputs, total_excellent_count = total_excellent,
             total_miss_count = total_judgments - total_excellent,
             total_skipped_inputs = total_skipped_inputs,
            total_judgment_count = total_judgments, first_feedback_verified = first_feedback_verified}}
    mailbox:publish(state)
end

local function finish(message, reason, fatal)
    if message and not fatal then warn(message); message, reason = nil, "dance_recovery_ready" end
    if message and runtime then runtime.fail_stage() end
    active = nil
    release()
    state.buttons, state.error, state.command_done = "", message or "", true
    state.phase = message and "command_error" or "ready"
    state.stop_reason = message and "command_error" or reason or "command_complete"
    state.command_end_frame = state.frame
    for _, file in ipairs({frames_file or false, samples_file or false}) do
        if file then
            local ok, closed = pcall(file.close, file)
            if not ok or not closed then warn("Capture stream could not be closed") end
        end
    end
    frames_file, samples_file = nil, nil
end

local function stop(message, reason)
    finish(message, reason)
    capture()
    publish()
    emu.pause()
end

local function retain_miss(index, raw, capture_callback)
    local press = current.presses[index]
    assert(press.started and not press.judgment, "Retain one feedback result per executed input")
    local path = string.format("%s/judgment-%d-Miss.gd", current.work, index)
    local judgment = {dance = current.index, index = index, button = press.button, label = "Miss",
        input_frame = press.frame, feedback_frame = state.frame, visible_delay_frames = state.frame - press.frame,
        source = "excellent_palette_not_detected", fresh_message = false, recovery_reason = "excellent_palette_not_detected",
        image = {path = path, frame = state.frame, epoch = adapter.epoch(), source = "input_feedback_framebuffer",
            capture_callback = capture_callback, direct_read_frames = 0}}
    press.judgment = judgment
    judgments[#judgments + 1] = judgment
    total_judgments = total_judgments + 1
    judgment.warning = warn(string.format("Input %d feedback: Miss", index))
    if stage5 then
        first_feedback = judgment
        feedback_failure = {dance = current.index, frame = state.frame, label = "Miss", feedback = judgment,
            source = "our_pokemon_first_button_feedback_pixels", message = "Excellent first-button feedback missed"}
        write_file(current.work .. "/non-excellent.json", json.encode(feedback_failure) .. "\n")
    end
    write_file(path, raw, "wb")
    write_file(current.work .. "/judgments.json", json.encode(judgments) .. "\n")
end

local function retain_first_skip(raw, reason, capture_callback)
    local path = current.work .. "/first-input-skipped.gd"
    first_feedback = {dance = 1, index = 1, label = "Skipped", skipped = true, fresh_message = false,
        feedback_frame = state.frame, recovery_reason = reason, source = "skipped_first_input",
        image = {path = path, frame = state.frame, epoch = adapter.epoch(), source = "skipped_input_framebuffer",
            capture_callback = capture_callback or "after_frame", direct_read_frames = 0}, warning = warn("First mirrored input skipped")}
    write_file(path, raw, "wb")
    write_file(current.work .. "/first-input-result.json", json.encode(first_feedback) .. "\n")
end

local function retain(frame, input_event, raw, capture_source)
    local paused_capture = raw ~= nil
    raw = raw or gui.gdscreenshot()
    local sample = {frame = frame, offset = frame - origin, bar = pixels.bar(raw),
        applied_button = state.buttons, input_event = input_event,
        source = capture_source or (paused_capture and "fresh_paused_demo_entry" or "frame_callback"),
        capture_offset = byte_offset, capture_bytes = #raw}
    if active.kind == "mirror" then
        local delay, should_read = config.hold_frames + config.feedback_release_frames - 1, false
        for index, press in ipairs(current.presses) do
            if (not stage5 or current.index == 1 and index == 1) and press.started and not press.judgment
                and frame >= press.frame + delay then should_read = true; break end
        end
        local feedback = should_read and pixels.feedback(raw, current.player_center_x) or nil
        sample.feedback = feedback
        local fresh = feedback and (not last_feedback or feedback.label ~= last_feedback.label
            or feedback.y > last_feedback.y + 2)
        if fresh then
            local eligible = {}
            for index, press in ipairs(current.presses) do
                local prior = press.feedback_before
                if (not stage5 or index == 1 and current.index == 1) and press.started and not press.judgment
                    and frame >= press.frame + delay
                    and (not prior or feedback.label ~= prior.label or feedback.y > prior.y + 2) then
                    eligible[#eligible + 1] = index
                end
            end
            if #eligible > 1 then warn("Feedback cannot be assigned to one pending input"); eligible = {} end
            if #eligible == 1 then
                local index = eligible[1]
                local press = current.presses[index]
                local judgment = {dance = current.index, index = index, button = press.button,
                    input_frame = press.frame, feedback_frame = frame, visible_delay_frames = frame - press.frame,
                    label = feedback.label, feedback_x = feedback.x, feedback_y = feedback.y,
                    source = feedback.source, fresh_message = true,
                    image = {path = string.format("%s/judgment-%d-Excellent.gd", current.work, index),
                        frame = frame, epoch = adapter.epoch(), source = "input_feedback_framebuffer",
                        capture_callback = "after_frame_feedback", direct_read_frames = 0}}
                press.judgment = judgment
                judgments[#judgments + 1] = judgment
                total_judgments = total_judgments + 1
                if feedback.label == "Excellent" then total_excellent = total_excellent + 1 end
                if stage5 then first_feedback = judgment end
                sample.new_judgment = judgment
                write_file(string.format("%s/judgment-%d-%s.gd", current.work, index, feedback.label), raw, "wb")
                write_file(current.work .. "/judgments.json", json.encode(judgments) .. "\n")
            end
        end
        if should_read then last_feedback = feedback end
    end
    assert(frames_file:write(raw))
    assert(samples_file:write(json.encode(sample), "\n"))
    byte_offset = byte_offset + #raw
    last_sample = sample
    return raw
end

local function packet(observation)
    local result = {frame = adapter.frame(), epoch = adapter.epoch(), checkpoint = latest,
        observation = observation, next_section = stage, completed_dances = completed_count()}
    if engine and not pending then result.progress = engine:state() end
    return result
end

local function definitions()
    local result = {
        start = {checks = {
            {id = "platinum", field = "game.supported", expected = true},
            {id = "sequence", field = "trial.sequence_valid", expected = true},
            {id = "configured_boundary", field = "trial.at_boundary", expected = true},
            {id = "rank_cues", field = "trial.cue_count", expected = cue_count},
            {id = "visual_complete", field = "trial.visual_complete", expected = true},
            {id = "cue_conflict_policy", field = "trial.conflict_policy", expected = "error"},
            {id = "fresh_framebuffer_binding", field = "trial.fresh_visual_binding", expected = true},
            {id = "counted_visual_timing", field = "trial.timeline_confirmed", expected = true},
            {id = "cues_and_timing_match_plan", field = "trial.cues_consistent", expected = true},
            {id = "before_first_input", field = "trial.before_first_input", expected = true},
        }, branches = {pass = "mirror"}},
        ["offset-check"] = {checks = {
            {id = "platinum", field = "game.supported", expected = true},
            {id = "first_input_only", field = "trial.total_input_count", expected = 1},
            {id = "first_feedback_accepted", field = "trial.first_feedback_accepted", expected = true},
            {id = "feedback_capture_bound", field = "trial.first_feedback_bound", expected = true},
            {id = "released", field = "trial.released", expected = true},
        }, branches = {pass = "mirror-rest"}},
        finish = {checks = {{id = "platinum", field = "game.supported", expected = true}},
            target = {{id = "six_dances", field = "trial.dance_index", expected = 6}},
            branches = {hit = stage5 and "lead-dances" or "success", miss = "next-dance"}},
        ["stage-5-dance"] = {checks = {
            {id = "platinum", field = "game.supported", expected = true},
            {id = "six_answered_dances", field = "trial.completed_dances", expected = 6},
            {id = "response_input_total", expected = true, evaluate = function(snapshot)
                local actual, required = snapshot.trial.total_input_count, snapshot.trial.required_response_inputs or 6 * cue_count
                local skipped = snapshot.trial.tolerant_inputs and snapshot.trial.total_skipped_inputs or 0
                return actual + skipped == required, {count = actual, skipped = skipped, required = required}
            end},
            {id = "first_press_feedback", expected = true, evaluate = function(snapshot)
                local first = snapshot.offset_check
                if runtime and snapshot.rank_timing then
                    return snapshot.trial.first_feedback_accepted == true, first and first.label
                end
                return first ~= nil, first and first.label
            end},
            {id = "offset_verified", field = "trial.first_feedback_verified", expected = true},
            {id = "single_feedback_check", expected = true, evaluate = function(snapshot)
                return snapshot.trial.total_judgment_count == (snapshot.trial.required_first_feedback_judgments or 1)
            end},
            {id = "lead_dances_complete", field = "lead_dances.completed", expected = true},
            {id = "lead_input_policy", field = "lead_dances.input_policy", expected = "none"},
            {id = "no_lead_inputs", field = "lead_dances.input_count", expected = 0},
            {id = "B_transition_complete", field = "transition.completed", expected = true},
            {id = "transition_endpoint", field = "trial.acting_endpoint_met", expected = true},
            {id = "released", field = "trial.released", expected = true},
        }, branches = {pass = "finish"}},
        stopped = {checks = {{id = "host_stop", expected = "continue",
            evaluate = function(_, context) return false, context.reason end}}, branches = {pass = "review"}},
    }
    if not runtime then
        local checks = result["stage-5-dance"].checks
        for _, check in ipairs({
            {id = "acting_review_source", field = "screen.source", op = "one_of",
                expected = {"visual_review", "local_reference_pixels"}},
            {id = "acting_entry", field = "screen.expected", expected = "acting_entry"},
            {id = "acting_confirmed", field = "screen.confirmed", expected = true},
            {id = "fresh_framebuffer_binding", field = "screen.fresh_visual_binding", expected = true},
            {id = "review_frame", field = "screen.frame", context = "frame"},
            {id = "review_epoch", field = "screen.epoch", context = "epoch"},
        }) do checks[#checks + 1] = check end
    end
    local function common()
        return {{id = "platinum", field = "game.supported", expected = true},
            {id = "released", field = "released", expected = true},
            {id = "frame", field = "frame", context = "frame"},
            {id = "epoch", field = "epoch", context = "epoch"},
            {id = "fresh_capture", field = "image.direct_read_frames", expected = 0}}
    end
    for _, name in ipairs({"start", "offset-check", "stage-5-dance"}) do
        local definition, targets, checks = result[name], {}, common()
        for _, check in ipairs(definition.checks) do
            if check.id:match("^acting_") or check.id:match("^review_") or check.id == "fresh_framebuffer_binding" and name == "stage-5-dance" then
                checks[#checks + 1] = check
            elseif check.id ~= "platinum" and check.id ~= "released" then targets[#targets + 1] = check end
        end
        local branch = definition.branches.pass
        definition.checks, definition.target = checks, targets
        definition.branches = {hit = branch, miss = name == "start" and "skip-response" or branch}
    end
    -- Finish always advances the six-response schedule; its measured facts remain in the snapshot.
    result.finish.checks = common()
    return result
end

local function dispatch(action, args)
    local observation
    if action == "state-changed" then
        assert(not initialized, "Reload requires a fresh dance section")
        adapter.state_changed()
        state.frame = adapter.frame()
        finish()
    elseif action == "inspect" then
        observation = {frame = adapter.frame(), game = game()}
    elseif action == "arm-avi-refresh" then
        assert(not initialized and not active and adapter.frame() == config.source_frame,
            "AVI recording must start from the paused prepared source")
        assert(type(refresh_frames) == "number" and refresh_frames >= 1 and refresh_frames == math.floor(refresh_frames),
            "Use the configured fixed-source display refresh frames")
        active = {kind = "advance", elapsed = 0, total = refresh_frames}
        state.last_action, state.command_start_frame = "avi-refresh", adapter.frame()
        state.command_done, state.error, state.phase = false, "", "running"
        state.command_end_frame, state.stop_reason = nil, nil
    elseif action == "init" then
        assert(type(refresh_frames) == "number" and refresh_frames >= 0 and refresh_frames == math.floor(refresh_frames)
            and (cue_count == 3 or cue_count == 4), "Declare counted refresh frames and the rank's cue count")
        assert(not stage5 or type(config.feedback_release_frames) == "number" and config.feedback_release_frames >= 1
            and config.feedback_release_frames == math.floor(config.feedback_release_frames),
            "Use the configured released-input feedback wait")
        assert(not initialized and adapter.frame() == config.source_frame + refresh_frames,
            "Expected the accepted source and its configured refresh frames")
        engine = engine or master.new({run_id = config.run_id, emulator = adapter, reader = {snapshot = snapshot},
            log = master.jsonl(config.work .. "/checkpoint-events.jsonl"), definitions = definitions()})
        initialized = true
        stage = "next-dance"
    elseif action == "begin-dance" then
        local index = tonumber(args[1])
        assert(engine and not pending and not active and stage == "next-dance"
            and index == completed_dances + 1 and index <= 6, "The next dance is not authorized")
        current = config.dances[index]
        if adapter.frame() >= current.boundary_frame then warn("The next response boundary has passed") end
        input_count, last_sample, last_feedback = 0, nil, nil
        judgments = setmetatable({}, bridge_io.array_metatable)
        demo_initial = nil
        if stage5 and index == 1 and config.source_image then
            local image = config.source_image
            assert(image.frame == adapter.frame() and image.frame == config.source_context.frame
                and image.origin_epoch == config.source_context.epoch and image.origin_run_id == config.source_context.run_id,
                "The initial demo image must belong to the restored Stage 4 checkpoint")
            demo_initial = {frame = adapter.frame(), epoch = adapter.epoch(), raw = read_file(image.path), source = image.source}
        elseif stage5 and adapter.frame() >= current.demo_start_frame then
            demo_initial = {frame = adapter.frame(), epoch = adapter.epoch(), raw = gui.gdscreenshot()}
        end
        stage = "demo"
    elseif action == "plan" then
        assert(current and stage == "read-demo" and not active, "A captured demo is required")
        local plan = assert(loadfile(current.work .. "/plan.lua"))()
        assert(plan.dance == current.index and plan.boundary_frame == current.boundary_frame
            and plan.end_frame == current.end_frame and plan.hold_frames == config.hold_frames
            and plan.offset_frames == config.offset_frames, "The visual plan belongs to a different dance or timing")
        current.presses, current.visual_evidence = plan.presses, plan.visual_evidence
        stage = "start"
    elseif action == "capture" then
        assert(idle and #args == 1 and args[1]:match("^[%w_-]+$"), "Capture the configured paused evidence")
        observation = snapshot(adapter.frame())
        local path = (current and current.work or config.work) .. "/" .. args[1] .. ".gd"
        write_file(path, gui.gdscreenshot(), "wb")
        observation.image.path = path
        capture()
    elseif action == "recover-response" then
        assert(current and not active and not pending and #args == 2, "Continue the selected response from a confirmed pause")
        local plan = assert(loadfile(args[1]))()
        assert(plan.dance == current.index and plan.boundary_frame == current.boundary_frame and plan.end_frame == current.end_frame,
            "The input-free continuation belongs to this dance")
        if not current.visual_evidence then current.presses, current.visual_evidence = plan.presses, plan.visual_evidence end
        for index, press in ipairs(current.presses) do
            if not press.started then skip_press(press, "dance_error_continuation")
            elseif not press.judgment and (not stage5 or current.index == 1 and index == 1) then
                retain_miss(index, gui.gdscreenshot(), "paused_response_recovery")
            end
        end
        if stage5 and current.index == 1 and not first_feedback then
            retain_first_skip(gui.gdscreenshot(), args[2], "paused_response_recovery")
        end
        current.skip_response = true
        stage = not current.start_acknowledged and "start"
            or stage5 and current.index == 1 and not first_feedback_verified and "offset-check" or "mirror"
        warn(args[2])
        local result = packet(snapshot(adapter.frame()))
        result.response_plan = {presses = current.presses, visual_evidence = current.visual_evidence}
        return result
    elseif action == "warn" then
        assert(#args == 1, "Supply the dance continuation warning")
        warn(args[1])
    elseif action == "prime" then
        assert(stage == "mirror" and not active, "Priming requires the saved paused start")
        local selected
        for _, press in ipairs(current.presses) do
            if not press.started and not press.skipped and not current.skip_response
                and adapter.frame() + 1 == press.frame then selected = press.button end
        end
        bridge_io.apply_buttons(selected)
        state.buttons = selected or ""
    elseif action == "capture-acting" then
        assert(stage5 and stage == "acting-review" and not active and not pending,
            "Complete all eight dances and the B transition first")
        write_file(config.work .. "/stage5.gd", gui.gdscreenshot(), "wb")
        capture()
        observation = snapshot(adapter.frame())
        observation.automatic_check = screen_checks.read(gui.gdscreenshot(), "acting_entry")
    elseif action == "review-acting" then
        assert(stage5 and stage == "acting-review" and not active and not pending and #args == 1,
            "Expected the paused Acting-entry review")
        local review = assert(loadfile(args[1]))()
        assert(type(review) == "table" and review.frame == adapter.frame() and review.epoch == adapter.epoch()
            and review.run_id == config.run_id
            and (review.source == "visual_review" or review.source == "local_reference_pixels")
            and (review.verdict == "pass" or review.verdict == "fail"),
            "The Acting review belongs to a different paused boundary or source")
        assert(type(review.note) == "string" and review.note:match("%S") and not review.note:find("[|\r\n]"),
            "The Acting review needs a single-line visual explanation")
        acting_review, stage = review, "stage5-checkpoint"
    elseif action == "visit" then
        assert(engine and not pending and #args == 1, "Expected one checkpoint visit")
        assert((args[1] == "start" and stage == "start")
            or (stage5 and args[1] == "offset-check" and stage == "offset-check" and not active)
            or (args[1] == "finish" and stage == "mirror" and not active)
            or (stage5 and args[1] == "stage-5-dance" and not active
                and (stage == "stage5-checkpoint" or runtime and stage == "acting-review")),
            "Boundary is not ready")
        latest = engine:visit(args[1], {frame = adapter.frame(), epoch = adapter.epoch()})
        pending = (latest.status == "awaiting_save" or latest.status == "awaiting_ack") and latest or nil
        observation = latest.snapshot
        if latest.decision == "miss" and args[1] ~= "finish" then warn(args[1] .. " checks missed") end
        capture()
    elseif action == "complete" then
        assert(pending and (#args == 6 or #args == 7), "Expected pending checkpoint evidence")
        latest = engine:complete(tonumber(args[2]), {run_id = args[1], visit = tonumber(args[2]),
            frame = tonumber(args[3]), epoch = tonumber(args[4]), path = args[5], emulator_paused = args[6] == "true",
            saved = args[7] ~= "false"})
        pending, observation = nil, latest.snapshot
        stage = latest.proceed and latest.branch or "stopped"
        if latest.proceed and latest.checkpoint == "start" then
            current.start_acknowledged = true
            if latest.decision == "miss" then
                for _, press in ipairs(current.presses) do skip_press(press, "midpoint_checkpoint_continuation") end
                current.skip_response, stage = true, "mirror"
            end
            if current.skip_response and stage5 and current.index == 1 and first_feedback and not first_feedback_verified then
                stage = "offset-check"
            end
        end
        if latest.proceed and latest.checkpoint == "offset-check" then first_feedback_verified = true end
        if latest.proceed and latest.checkpoint == "finish" then completed_dances = current.index end
        if not runtime and latest.checkpoint == "stage-5-dance" then
            print("Contest Stage 5: " .. (latest.proceed and "PASS" or "FAIL"))
        end
    elseif action == "abort" then
        stage = "stopped"
        local reason = args[1] or "Host stopped the full dance run"
        finish(reason, nil, true)
        if pending then
            latest, pending = engine:cancel(reason), nil
        elseif engine and not engine:state().halted then
            latest = engine:visit("stopped", {reason = reason})
            pending = (latest.status == "awaiting_save" or latest.status == "awaiting_ack") and latest or nil
        end
        observation = latest and latest.snapshot
        capture()
    else
        error("Unknown dance-master checkpoint command: " .. tostring(action))
    end
    return packet(observation)
end

local function checkpoint_callback()
    local parts = checkpoint_mailbox:read_command(checkpoint_id)
    if not parts then return end
    checkpoint_id = parts[1]
    local args = {}
    for index = 3, #parts do args[#args + 1] = parts[index] end
    assert(not idle, "Reentrant paused callback")
    idle, state.frame = true, adapter.frame()
    local ok, result = pcall(dispatch, parts[2], args)
    if not ok then
        local message = tostring(result)
        if pending then latest, pending = engine:cancel(message), nil end
        finish(message)
        result = {frame = adapter.frame(), epoch = adapter.epoch(), warning = message, recovery_required = true}
    end
    idle = false
    result.command_id, result.command_done = checkpoint_id, true
    state.checkpoint_epoch = adapter.epoch()
    state.checkpoint_visit = result.checkpoint and result.checkpoint.visit
    state.checkpoint_status = result.checkpoint and result.checkpoint.status
    state.observation_stale = false
    publish(result.observation)
    checkpoint_mailbox:publish(result, false)
end

local function start(parts)
    state.command_id, state.command_done, state.error = parts[1], false, ""
    assert(not pending, "A saved checkpoint must authorize the next dance action")
    state.last_action, state.command_start_frame = parts[2], state.frame
    active = {kind = parts[2], elapsed = 0}
    if parts[2] == "advance" then
        active.total = tonumber(parts[3])
        assert(not initialized and #parts == 3 and active.total == refresh_frames and active.total > 0,
            "Only the counted setup refresh is allowed")
        assert(state.frame >= config.source_frame and state.frame < config.source_frame + active.total,
            "The setup refresh must start before its absolute endpoint")
    elseif parts[2] == "mirror" or parts[2] == "mirror-rest" or parts[2] == "observe-demo" then
        assert(#parts == 2 and current, "Expected one configured dance section")
        if parts[2] == "mirror" then
            assert(stage == "mirror" and current.start_acknowledged, "A saved start checkpoint is required")
        elseif parts[2] == "mirror-rest" then
            assert(stage5 and stage == "mirror-rest" and first_feedback_verified and input_count <= 1,
                "The first-button checkpoint must authorize the remaining inputs")
            active.kind, stage = "mirror", "mirror"
        else
            assert(stage == "demo" and state.frame < current.boundary_frame, "The next demonstration is not armed")
        end
        if active.kind == "mirror" and current.response_complete then stop(nil, "mirror_complete"); return end
        if active.kind == "mirror" and current.skip_response then return end
        origin, byte_offset = state.frame, 0
        local prefix = parts[2] == "mirror" and "frames" or parts[2] == "mirror-rest" and "rest-frames" or "demo-frames"
        frames_file = assert(io.open(current.work .. "/" .. prefix .. ".bin", "wb"))
        samples_file = assert(io.open(current.work .. "/" .. prefix .. ".jsonl", "w"))
        if parts[2] == "observe-demo" and demo_initial then
            assert(demo_initial.frame == state.frame and demo_initial.epoch == adapter.epoch(),
                "The paused demonstration entry changed before observation started")
            retain(demo_initial.frame, nil, demo_initial.raw, demo_initial.source)
            demo_initial = nil
        elseif parts[2] == "mirror" or parts[2] == "mirror-rest" or state.frame >= current.demo_start_frame then
            retain(state.frame)
        end
    elseif parts[2] == "lead-dances" then
        assert(stage5 and #parts == 2 and engine and not pending and completed_dances == 6,
            "The acknowledged sixth response must authorize Dances 7 and 8")
        if lead_dances and lead_dances.completed then stop(nil, "lead_dances_complete"); return end
        assert(stage == "lead-dances", "The input-free dances must be authorized")
        assert(config.dances[7].input_policy == "none" and config.dances[8].input_policy == "none",
            "Dances 7 and 8 require no inputs")
        lead_dances = lead_dances or {first_dance = 7, last_dance = 8, input_policy = "none", completed = false,
            start_frame = state.frame, end_frame = config.dances[8].end_frame,
            duration_frames = config.dances[8].end_frame - state.frame,
            input_count_before = total_inputs, input_count = 0, dances = {config.dances[7], config.dances[8]}}
    elseif parts[2] == "acting-transition" then
        assert(stage5 and #parts == 2 and engine and not pending and lead_dances and lead_dances.completed,
            "Complete the input-free Dances 7 and 8 first")
        if transition and transition.completed then stop(nil, "B_transition_complete"); return end
        assert(stage == "acting-transition", "The Acting transition must be authorized")
        active.total = config.transition.mash_frames
        transition = transition or {start_frame = state.frame, end_frame = state.frame + active.total,
            game_seconds = config.transition.game_seconds, mash_frames = active.total,
            mash_hold_frames = config.transition.mash_hold_frames,
            mash_release_frames = config.transition.mash_release_frames, button = "B", press_count = 0,
            completed = false}
        active.elapsed = state.frame - transition.start_frame
    else
        error("Unknown dance-master action: " .. tostring(parts[2]))
    end
end

local function before_frame()
    state.frame = emu.framecount()
    if not active then
        local parts = mailbox:read_command(state.command_id)
        if parts then
            local ok, message = pcall(start, parts)
            if not ok then stop(tostring(message)); return end
        end
    end
    local selected, event
    if active and active.kind == "mirror" then
        local frame = state.frame + 1
        if stage5 and current.index == 1 and not first_feedback and not first_feedback_verified and current.presses[2]
            and frame >= current.presses[2].frame and current.presses[1].started and not current.presses[1].judgment then
            retain_miss(1, gui.gdscreenshot(), "before_frame_feedback")
            stage = "offset-check"
            stop(nil, "offset_feedback_ready")
            return
        end
        for index, press in ipairs(current.presses) do
            if not press.started and frame > press.frame then skip_press(press, "missed_input_frame") end
            if not stage5 and not current.skip_response and not press.skipped and not press.started and frame == press.frame then
                for prior_index = 1, index - 1 do
                    local prior = current.presses[prior_index]
                    if prior.started and not prior.judgment then
                        retain_miss(prior_index, gui.gdscreenshot(), "before_frame_feedback")
                    end
                end
            end
            if not press.skipped and not press.started and not current.skip_response and frame == press.frame
                and (not stage5 or current.index == 1 and index == 1) then
                local raw = gui.gdscreenshot()
                last_feedback = pixels.feedback(raw, current.player_center_x)
                write_file(string.format("%s/feedback-before-%d.gd", current.work, index), raw, "wb")
            end
            if not press.skipped and not current.skip_response and frame >= press.frame and frame < press.frame + config.hold_frames then selected = press.button end
            if not press.skipped and not press.started and not current.skip_response and frame == press.frame then
                input_count, total_inputs = input_count + 1, total_inputs + 1
                press.started, press.feedback_before = true, last_feedback
                event = {index = index, button = press.button, frame = frame, kind = "down"}
            elseif frame == press.frame + config.hold_frames then
                event = {index = index, button = press.button, frame = frame, kind = "up"}
            end
        end
        active.input_event = event
    elseif active and active.kind == "acting-transition" then
        local cycle = transition.mash_hold_frames + transition.mash_release_frames
        if active.elapsed % cycle < transition.mash_hold_frames then selected = "B" end
        if active.elapsed % cycle == 0 then transition.press_count = transition.press_count + 1 end
    end
    bridge_io.apply_buttons(selected)
    state.buttons = selected or ""
    if stage5 then
        stylus.set({touch = false})
        state.stylus_touch = false
    end
end

local function after_frame()
    state.frame = emu.framecount()
    if active then
        active.elapsed = active.elapsed + 1
        if active.kind == "mirror" then
            if not current.skip_response and (not stage5 or not first_feedback_verified
                or active.input_event or state.frame >= current.end_frame) then
                local ok, message = pcall(retain, state.frame, active.input_event)
                if not ok then stop(tostring(message)); return end
            end
            if stage5 and current.index == 1 and not first_feedback then
                local first = current.presses[1]
                if not first or first.skipped then retain_first_skip(gui.gdscreenshot(), "first_input_unavailable")
                elseif state.frame >= current.end_frame and first.started and not first.judgment then
                    retain_miss(1, gui.gdscreenshot(), "after_frame_feedback")
                end
            end
            if stage5 and current.index == 1 and first_feedback and not first_feedback_verified then
                if state.frame >= current.end_frame then account_response() end
                stage = "offset-check"
                stop(nil, "offset_feedback_ready")
                return
            end
            if state.frame >= current.end_frame then
                if not stage5 then
                    for index, press in ipairs(current.presses) do
                        if press.started and not press.judgment then
                            retain_miss(index, gui.gdscreenshot(), "after_frame_feedback")
                        end
                    end
                end
                account_response()
                stop(nil, "mirror_complete"); return
            end
        elseif active.kind == "observe-demo" then
            local ok, message = pcall(function()
                if state.frame >= current.demo_start_frame and ((state.frame - current.demo_start_frame) % current.sample_frames == 0
                    or state.frame >= current.boundary_frame - 1) then
                    local raw = retain(state.frame)
                    if state.frame == current.boundary_frame then write_file(current.work .. "/boundary.gd", raw, "wb") end
                end
                if state.frame > current.boundary_frame then
                    warn("The response boundary was overrun")
                    write_file(current.work .. "/boundary.gd", gui.gdscreenshot(), "wb")
                end
            end)
            if not ok then stop(tostring(message)); return end
            if state.frame >= current.boundary_frame then
                stage = "read-demo"
                stop(nil, "demo_complete")
                return
            end
        elseif active.kind == "lead-dances" then
            if state.frame >= lead_dances.end_frame then
                if state.frame ~= lead_dances.end_frame then warn("The eighth dance endpoint was overrun") end
                lead_dances.completed, lead_dances.input_count_after = true, total_inputs
                lead_dances.input_count = total_inputs - lead_dances.input_count_before
                stage = "acting-transition"
                stop(nil, "lead_dances_complete")
                return
            end
        elseif active.kind == "acting-transition" then
            if state.frame >= transition.end_frame then
                if state.frame ~= transition.end_frame then warn("The Acting transition endpoint was overrun") end
                transition.completed, transition.actual_frames = true, state.frame - transition.start_frame
                stage = "acting-review"
                stop(nil, "B_transition_complete")
                return
            end
        elseif state.frame >= config.source_frame + active.total then
            -- Include AVI's native resume in the configured counted source boundary.
            stop(state.frame ~= config.source_frame + active.total and "The setup refresh boundary was overrun" or nil)
            return
        end
    end
    if state.frame % 15 == 0 then publish() end
end

if runtime then
    return {definitions = definitions, snapshot = snapshot,
        before = function()
            local ok, message = pcall(before_frame)
            if not ok then stop(tostring(message)) end
        end,
        after = function()
            local ok, message = pcall(after_frame)
            if not ok then stop(tostring(message)) end
        end,
        finish = finish, checkpoint = function(action, args)
            idle, state.frame = true, adapter.frame()
            local ok, result = pcall(dispatch, action, args)
            idle = false
            if not ok then
                local message = tostring(result)
                if pending then latest, pending = engine:cancel(message), nil end
                finish(message)
                return {frame = adapter.frame(), epoch = adapter.epoch(), checkpoint = latest, warning = message, recovery_required = true}
            end
            return result
        end}
end

emu.registermenustart(checkpoint_callback)
emu.registerbefore(function()
    local ok, message = pcall(before_frame)
    if not ok then stop(tostring(message)) end
end)
emu.registerafter(function()
    local ok, message = pcall(after_frame)
    if not ok then stop(tostring(message)) end
end)

if stage5 then release() end
capture()
publish()
emu.pause()
print(stage5 and "Local contest Stage 5 bridge ready." or "Local six-dance master ready.")
