-- Prepared Dance 1 measurements and one-response trials through paused checkpoints.
local directory = assert(debug.getinfo(1, "S").source:sub(2):gsub("\\", "/"):match("^(.*)/"))
local root = dofile(directory .. "/resource_root.lua")
local bridge_io = dofile(root .. "/lua/bridge_io.lua")
local master = dofile(root .. "/lua/checkpoint_master.lua")
local json = dofile(root .. "/lua/json.lua")
local pixels = dofile(directory .. "/dance_pixels.lua")
local mailbox = bridge_io.new(root .. "/tmp")
local checkpoint_mailbox = bridge_io.new(root .. "/tmp", "checkpoint-")
local checkpoint_id, idle = checkpoint_mailbox:previous_id(), false
local engine, latest, pending, work, selected, active, plan
local measurement_file, measurement_origin, measurement_last
local demo_bytes, demo_index, demo_offset, demo_origin, demo_last
local response_events, last_feedback, response_complete = nil, nil, false
local inputs, capture_number, halted = 0, 0, false
local judgments = setmetatable({}, bridge_io.array_metatable)
local state = {protocol = 1, checkpoint_protocol = 1, bridge = "pt-contest-rank-speed",
    game_code = emu.gamecode(), frame = emu.framecount(), phase = "ready", buttons = "",
    stylus_touch = false, command_id = mailbox:previous_id(), command_done = true, error = ""}
local adapter = dofile(root .. "/lua/checkpoint_desmume.lua").new({
    pause = function() assert(idle, "Host-confirmed native pause is required") end,
    is_paused = function() return idle end,
})

local function release()
    bridge_io.apply_buttons(nil)
    stylus.set({touch = false})
    state.buttons, state.stylus_touch = "", false
end
adapter.release = release

local function write(path, value, mode)
    local file = assert(io.open(path, mode or "w"))
    assert(file:write(value)); assert(file:close())
end

local function warn(message)
    local text = "Dancing: WARN - " .. tostring(message):gsub("[|\r\n]", " ") .. "; continuing."
    state.dance_warning = text
    print(text)
    return text
end

local function close_files()
    local files = {measurement_file or false, demo_bytes or false, demo_index or false, response_events or false}
    measurement_file, demo_bytes, demo_index, response_events = nil, nil, nil, nil
    for _, file in ipairs(files) do
        if file then
            local ok, closed = pcall(file.close, file)
            if not ok or not closed then warn("Capture stream could not be closed") end
        end
    end
end

local function skipped_count()
    local count = (selected and selected.expected_cues or 0) - #(plan and plan.presses or {})
    for _, press in ipairs(plan and plan.presses or {}) do if press.skipped then count = count + 1 end end
    return count
end

local function skip_press(press, reason)
    if not press.started and not press.skipped then press.skipped, press.skip_reason = true, reason end
end

local function observation(frame, raw)
    local code = emu.gamecode()
    local excellent = 0
    for _, judgment in ipairs(judgments) do
        if judgment.label == "Excellent" then excellent = excellent + 1 end
    end
    return {frame = frame, game = {code = code, supported = code == "CPUJ", identity_source = "emu.gamecode"},
        epoch = adapter.epoch(), rank = selected and selected.rank,
        midpoint_timing = selected and selected.midpoint_frame and {expected_frame = selected.midpoint_frame,
            source = "counted_prepared_demo_endpoint"} or nil,
        rank_source = "user_prepared_native_slot", bar = raw and pixels.bar(raw) or nil,
        released = state.buttons == "" and not state.stylus_touch,
         trial = {inputs = inputs, judgments = judgments, judgment_count = #judgments,
            expected_cues = selected and selected.expected_cues or 0, planned_input_count = plan and #plan.presses or 0,
            skipped_input_count = skipped_count(), tolerant_inputs = true,
            response_started = selected and selected.start_acknowledged == true or false,
            excellent_count = excellent, miss_count = #judgments - excellent, complete = response_complete}}
end

local function publish(value)
    state.observation = value or observation(state.frame)
    state.checkpoint_epoch = adapter.epoch()
    state.checkpoint_visit = latest and latest.visit
    state.checkpoint_status = latest and latest.status
    mailbox:publish(state)
end

local function capture(name)
    assert(idle and work, "Fresh captures require a configured, paused host request")
    assert(name:match("^[%w_-]+$"), "Use a simple capture name")
    local frame, raw = adapter.frame(), gui.gdscreenshot()
    local folder = selected and selected.work or work
    local path = folder .. "/" .. name .. ".gd"
    write(path, raw, "wb")
    write(root .. "/tmp/screen.gd", raw, "wb")
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
    capture_number = capture_number + 1
    local image, raw = capture(string.format("checkpoint-%04d", capture_number))
    local value = observation(frame, raw)
    value.image = image
    local consistent = plan ~= nil and #plan.presses <= selected.expected_cues
    if plan then
        for index, press in ipairs(plan.presses) do
            local due = plan.frame + math.floor((press.response_x - plan.pointer_x) / plan.speed + 0.5) + plan.offset_frames
            local following = plan.presses[index + 1] and plan.presses[index + 1].frame or plan.predicted_end_frame
            if press.frame ~= due or not press.skipped and (press.frame <= plan.frame or press.frame + plan.hold_frames >= following) then
                consistent = false
            end
        end
    end
    value.trial.plan_consistent = consistent
    value.trial.expected_cues = selected and selected.expected_cues or 0
    value.trial.before_first_input = plan ~= nil and (#plan.presses == 0 or plan.presses[1].skipped or frame < plan.presses[1].frame)
    value.trial.fresh_visual_binding = plan ~= nil and plan.frame == frame and plan.epoch == adapter.epoch()
        and (#plan.presses == 0 or colors_match(raw, plan.pixel_points)) or false
    return value
end

local function common()
    return {{id = "cpuj", field = "game.code", expected = "CPUJ"},
        {id = "released", field = "released", expected = true},
        {id = "frame", field = "frame", context = "frame"},
        {id = "epoch", field = "epoch", context = "epoch"},
        {id = "fresh_capture", field = "image.direct_read_frames", expected = 0}}
end

local function definitions()
    local start, finish, trial_start, trial_finish = common(), common(), common(), common()
    for _, rules in ipairs({start, finish}) do
        rules[#rules + 1] = {id = "pointer_position", field = "bar.last_x", context = "pointer_x"}
        rules[#rules + 1] = {id = "demo_pixels", field = "bar.last_x", op = "range", expected = {8, 127}}
    end
    finish[#finish + 1] = {id = "forward_motion", expected = true, evaluate = function(value, context)
        return value.frame > context.start_frame and value.bar.last_x > context.start_x,
            {frames = value.frame - context.start_frame, pixels = value.bar.last_x - context.start_x}
    end}
    trial_start[#trial_start + 1] = {id = "midpoint", field = "bar.last_x", op = "range", expected = {128, 247}}
    trial_start[#trial_start + 1] = {id = "plan", field = "trial.plan_consistent", expected = true}
    trial_start[#trial_start + 1] = {id = "fresh_cue_pixels", field = "trial.fresh_visual_binding", expected = true}
    trial_start[#trial_start + 1] = {id = "before_input", field = "trial.before_first_input", expected = true}
    trial_finish[#trial_finish + 1] = {id = "accounted_inputs", expected = true, evaluate = function(value)
        return value.trial.inputs + value.trial.skipped_input_count == value.trial.expected_cues
    end}
    trial_finish[#trial_finish + 1] = {id = "all_feedback", expected = true, evaluate = function(value)
        return value.trial.judgment_count == value.trial.inputs
    end}
    trial_finish[#trial_finish + 1] = {id = "complete_response", field = "trial.complete", expected = true}
    local result = {['measure-start'] = {checks = start, branches = {pass = "measurement"}},
        ['measure-end'] = {checks = finish, branches = {pass = "next_source"}},
        ['trial-start'] = {checks = trial_start, branches = {pass = "response"}},
        ['trial-finish'] = {checks = trial_finish, branches = {pass = "next_trial"}},
        stopped = {checks = {{id = "host_stop", field = "stop_reason", expected = "continue"}}, branches = {pass = "stopped"}}}
    for _, name in ipairs({"trial-start", "trial-finish"}) do
        local definition, targets = result[name], {}
        for index = #common() + 1, #definition.checks do targets[#targets + 1] = definition.checks[index] end
        local branch = definition.branches.pass
        definition.checks, definition.target = common(), targets
        definition.branches = {hit = branch, miss = name == "trial-start" and "skip_response" or branch}
    end
    return result
end

local function packet(value)
    local result = {frame = adapter.frame(), epoch = adapter.epoch(), observation = value,
        checkpoint = latest, next_section = state.phase}
    if engine and not pending then result.progress = engine:state() end
    return result
end

local function finish(message, reason, fatal)
    if message and not fatal and selected and selected.mode == "trial" then
        warn(message); message, reason = nil, "dance_recovery_ready"
    end
    active = nil
    local ok, close_error = pcall(close_files)
    if not ok then warn(close_error) end
    release()
    state.command_done, state.error = true, message or ""
    state.phase, state.stop_reason = message and "stopped" or "ready", message and "command_error" or reason or "command_complete"
    state.command_end_frame = state.frame
    if message then halted = true end
end

local function stop(message, reason, raw)
    finish(message, reason)
    raw = raw or gui.gdscreenshot()
    write(root .. "/tmp/screen.gd", raw, "wb")
    state.screen_frame = state.frame
    publish(observation(state.frame, raw))
    emu.pause()
end

local function retain_measurement(raw, source)
    local sample = {frame = adapter.frame(), epoch = adapter.epoch(), bar = pixels.bar(raw), source = source}
    sample.readable = sample.bar.found
    if not sample.readable then sample.readability_reason = sample.bar.white_flash and "white_flash" or "strip_unreadable" end
    assert(measurement_file:write(json.encode(sample), "\n"))
    measurement_last = sample.frame
end

local function retain_demo(raw, source)
    local sample = {frame = adapter.frame(), epoch = adapter.epoch(), bar = pixels.bar(raw), source = source,
        capture_offset = demo_offset, capture_bytes = #raw}
    assert(demo_bytes:write(raw)); assert(demo_index:write(json.encode(sample), "\n"))
    demo_offset, demo_last = demo_offset + #raw, sample.frame
end

local timing_settings
local retain_miss

local function dispatch(action, args)
    if action == "init-run" then
        assert(not engine and #args == 3, "Initialize the probe once with the resolved timing settings")
        work = args[2]
        timing_settings = assert(loadfile(args[3]))()
        assert(timing_settings.game == emu.gamecode() and timing_settings.demo_sample_interval_frames >= 1
            and timing_settings.demo_sample_interval_frames == math.floor(timing_settings.demo_sample_interval_frames),
            "Use this ROM's positive configured demo sampling interval")
        engine = master.new({run_id = args[1], emulator = adapter, reader = {snapshot = function(frame)
            local value = snapshot(frame)
            value.stop_reason = state.error ~= "" and state.error or "host_stop"
            return value
        end}, definitions = definitions(), log = master.jsonl(work .. "/checkpoint-events.jsonl")})
    elseif action == "state-changed" then
        assert(args[1] == "load" and not pending and not halted, "Only prepared source loads are authorized")
        finish(nil, "source_loaded"); adapter.state_changed()
        plan, inputs, response_complete, last_feedback = nil, 0, false, nil
        judgments = setmetatable({}, bridge_io.array_metatable)
        state.observation_stale = true
    elseif action == "select-source" then
        assert(engine and not active and not pending and not halted and #args == 4, "Select one paused prepared source")
        local expected = tonumber(args[3])
        assert(expected == 3 or expected == 4, "The prepared rank needs three or four cues")
        selected = {rank = args[1], work = args[2], expected_cues = expected, mode = args[4]}
        assert(selected.mode == "measure" or selected.mode == "trial", "Unknown probe phase")
    elseif action == "capture" then
        local image, raw = capture(args[1])
        local value = observation(adapter.frame(), raw)
        value.image = image
        return packet(value)
    elseif action == "measure-begin" then
        assert(selected.mode == "measure" and latest and latest.checkpoint == "measure-start"
            and latest.proceed and not pending and latest.frame == adapter.frame(), "Acknowledge the start capture first")
        measurement_file = assert(io.open(selected.work .. "/pointer-samples.jsonl", "w"))
        measurement_origin = adapter.frame()
        retain_measurement(gui.gdscreenshot(), "fresh_paused_start")
    elseif action == "measure-end" then
        assert(measurement_file, "Begin the timed measurement first")
        retain_measurement(gui.gdscreenshot(), "fresh_paused_end")
        assert(measurement_file:close()); measurement_file = nil
        local image, raw = capture("measurement-end")
        local value = observation(adapter.frame(), raw)
        value.image = image
        return packet(value)
    elseif action == "plan" then
        assert(selected.mode == "trial" and not active and not pending and not halted, "Prepare a paused response")
        plan = assert(loadfile(args[1]))()
        assert(plan.frame == adapter.frame() and plan.epoch == adapter.epoch() and plan.rank == selected.rank
            and plan.speed > 0 and #plan.presses <= selected.expected_cues, "The plan belongs to this source and midpoint")
    elseif action == "recover-response" then
        assert(selected.mode == "trial" and not active and not pending and #args == 2,
            "Continue the trial response from a confirmed pause")
        if not plan then dispatch("plan", {args[1]}) end
        response_events = response_events or assert(io.open(selected.work .. "/response-events.jsonl", "a"))
        for index, press in ipairs(plan.presses) do
            if not press.started then skip_press(press, "dance_error_continuation")
            elseif not press.judgment then retain_miss(index, gui.gdscreenshot(), "paused_response_recovery") end
        end
        selected.skip_response = true
        warn(args[2])
        local image, raw = capture("response-recovery")
        local value = observation(adapter.frame(), raw); value.image = image
        local result = packet(value); result.response_plan = plan
        return result
    elseif action == "warn" then
        assert(#args == 1, "Supply the dance continuation warning")
        warn(args[1])
    elseif action == "visit" then
        assert(not active and not pending, "Finish the action before evaluating its boundary")
        local context = assert(loadfile(args[2]))()
        latest = engine:visit(args[1], context)
        pending = latest.status == "awaiting_save" or latest.status == "awaiting_ack"
        if latest.decision == "miss" then warn(args[1] .. " checks missed") end
    elseif action == "complete" then
        latest = engine:complete(tonumber(args[2]), {run_id = args[1], visit = tonumber(args[2]),
            frame = tonumber(args[3]), epoch = tonumber(args[4]), path = args[5], emulator_paused = args[6] == "true"})
        pending = false
        if latest.proceed and latest.checkpoint == "trial-start" then
            selected.start_acknowledged = true
            if latest.decision == "miss" then
                for _, press in ipairs(plan.presses) do skip_press(press, "midpoint_checkpoint_continuation") end
                selected.skip_response = true
            end
        end
    elseif action == "abort" then
        local reason = args[1] or "Host stopped the probe"
        finish(reason, nil, true)
        if pending then latest = engine:cancel(reason); pending = false
        elseif engine and not engine:state().halted then
            latest = engine:visit("stopped", {frame = adapter.frame(), epoch = adapter.epoch(), reason = reason})
            pending = latest.status == "awaiting_save"
        end
    elseif action == "terminal" then
        finish(nil, "terminal")
    else
        error("Unknown rank-speed checkpoint action: " .. tostring(action))
    end
    return packet()
end

emu.registermenustart(function()
    local parts = checkpoint_mailbox:read_command(checkpoint_id)
    if not parts then return end
    checkpoint_id = parts[1]
    local args = {}
    for index = 3, #parts do args[#args + 1] = parts[index] end
    assert(not idle, "Reentrant paused request")
    idle, state.frame = true, adapter.frame()
    local ok, result = pcall(dispatch, parts[2], args)
    if not ok then
        finish(tostring(result))
        if selected and selected.mode == "trial" and parts[2] ~= "abort" then
            if parts[2] == "plan" then plan = nil end
            result = {frame = adapter.frame(), epoch = adapter.epoch(), warning = tostring(result), recovery_required = true}
        else
            if pending then latest = engine:cancel(tostring(result)); pending = false end
            result = {frame = adapter.frame(), epoch = adapter.epoch(), error = tostring(result), checkpoint = latest}
        end
    end
    if parts[2] ~= "state-changed" then state.observation_stale = false end
    result.command_id, result.command_done = checkpoint_id, true
    publish(result.observation)
    idle = false
    checkpoint_mailbox:publish(result, false)
end)

local function start(parts)
    assert(engine and selected and not halted and not pending and not active, "The probe must be ready")
    state.command_id, state.command_done, state.error = parts[1], false, ""
    state.command_start_frame = state.frame
    if parts[2] == "observe-demo" then
        assert(selected.mode == "trial" and not plan, "Observe the prepared first demonstration")
        demo_bytes = assert(io.open(selected.work .. "/demo-frames.bin", "wb"))
        demo_index = assert(io.open(selected.work .. "/demo-frames.jsonl", "w"))
        demo_offset, demo_origin = 0, state.frame
        active = {kind = "demo"}
        active.end_frame = tonumber(parts[3])
        assert(active.end_frame and active.end_frame > state.frame, "Use the prepared pointer's counted demo endpoint")
        selected.midpoint_frame = active.end_frame
        retain_demo(gui.gdscreenshot(), "current_demo_entry")
    elseif parts[2] == "mirror" then
        assert(plan and latest and latest.proceed and selected.start_acknowledged
            and (selected.skip_response or latest.checkpoint == "trial-start" and latest.frame == state.frame),
            "Acknowledge the midpoint plan before responding")
        if response_complete then stop(nil, "dance_1_response_complete"); return end
        response_events = response_events or assert(io.open(selected.work .. "/response-events.jsonl", selected.skip_response and "a" or "w"))
        active = {kind = "mirror"}
        if not selected.skip_response then last_feedback = pixels.feedback(gui.gdscreenshot(), plan.player_center_x) end
    else
        error("Unknown rank-speed input action: " .. tostring(parts[2]))
    end
end

retain_miss = function(index, raw, capture_callback)
    local press = plan.presses[index]
    assert(press.started and not press.judgment, "Retain one feedback result per executed input")
    local path = string.format("%s/judgment-%d-Miss.gd", selected.work, index)
    local judgment = {index = index, button = press.button, label = "Miss", input_frame = press.frame,
        feedback_frame = state.frame, visible_delay_frames = state.frame - press.frame,
        source = "excellent_palette_not_detected", fresh_message = false, recovery_reason = "excellent_palette_not_detected",
        image = {path = path, frame = state.frame, epoch = adapter.epoch(), source = "input_feedback_framebuffer",
            capture_callback = capture_callback, direct_read_frames = 0}}
    press.judgment = judgment
    judgment.warning = warn(string.format("Input %d feedback: Miss", index))
    judgments[#judgments + 1] = judgment
    write(path, raw, "wb")
    write(selected.work .. "/judgments.json", json.encode(judgments) .. "\n")
    assert(response_events:write(json.encode(judgment), "\n"))
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
    if active and active.kind == "mirror" and not selected.skip_response then
        local frame = state.frame + 1
        for index, press in ipairs(plan.presses) do
            if not press.started and frame > press.frame then skip_press(press, "missed_input_frame") end
            if not press.skipped and not press.started and frame == press.frame then
                for prior_index = 1, index - 1 do
                    local prior = plan.presses[prior_index]
                    if prior.started and not prior.judgment then
                        retain_miss(prior_index, gui.gdscreenshot(), "before_frame_feedback")
                    end
                end
            end
            if not press.skipped and not press.started and frame == press.frame then
                local raw = gui.gdscreenshot()
                last_feedback = pixels.feedback(raw, plan.player_center_x)
                write(string.format("%s/feedback-before-%d.gd", selected.work, index), raw, "wb")
            end
            if not press.skipped and frame >= press.frame and frame < press.frame + plan.hold_frames then button = press.button end
            if not press.skipped and not press.started and frame == press.frame then
                press.started, press.feedback_before = true, last_feedback
                inputs = inputs + 1
                assert(response_events:write(json.encode({kind = "input", index = index,
                    frame = frame, button = press.button}), "\n"))
            end
        end
    end
    bridge_io.apply_buttons(button); stylus.set({touch = false})
    state.buttons, state.stylus_touch = button or "", false
end
emu.registerbefore(function()
    local ok, message = pcall(before_frame)
    if not ok then stop(tostring(message)) end
end)

local function feedback(raw)
    local ready = false
    for _, press in ipairs(plan.presses) do
        if press.started and not press.judgment and state.frame >= press.frame + timing_settings.first_feedback_delay_frames then
            ready = true; break
        end
    end
    if not ready then return end
    local found = pixels.feedback(raw, plan.player_center_x)
    local fresh = found and (not last_feedback or found.label ~= last_feedback.label or found.y > last_feedback.y + 2)
    if fresh then
        local eligible = {}
        for index, press in ipairs(plan.presses) do
            local before = press.feedback_before
            if press.started and not press.judgment and state.frame >= press.frame + timing_settings.first_feedback_delay_frames
                and (not before or found.label ~= before.label or found.y > before.y + 2) then
                eligible[#eligible + 1] = index
            end
        end
        if #eligible > 1 then warn("Feedback cannot be assigned to one pending input"); eligible = {} end
        if #eligible == 1 then
            local index, judgment = eligible[1], {label = found.label, feedback_frame = state.frame,
                feedback_x = found.x, feedback_y = found.y, source = found.source, fresh_message = true}
            local press = plan.presses[index]
            judgment.index, judgment.button, judgment.input_frame = index, press.button, press.frame
            judgment.visible_delay_frames = state.frame - press.frame
            local path = string.format("%s/judgment-%d-Excellent.gd", selected.work, index)
            judgment.image = {path = path, frame = state.frame, epoch = adapter.epoch(), source = "input_feedback_framebuffer",
                capture_callback = "after_frame_feedback", direct_read_frames = 0}
            press.judgment = judgment
            judgments[#judgments + 1] = judgment
            write(path, raw, "wb")
            write(selected.work .. "/judgments.json", json.encode(judgments) .. "\n")
            assert(response_events:write(json.encode(judgment), "\n"))
        end
    end
    last_feedback = found
end

emu.registerafter(function()
    state.frame = adapter.frame()
    local ok, message = pcall(function()
        if measurement_file and state.frame - measurement_last >= timing_settings.demo_sample_interval_frames then
            retain_measurement(gui.gdscreenshot(), "frame_callback")
        end
        if active and active.kind == "demo" then
            local raw = gui.gdscreenshot()
            local bar = pixels.bar(raw)
            if state.frame - demo_last >= timing_settings.demo_sample_interval_frames or bar.found and bar.last_x >= 128 then
                retain_demo(raw, "frame_callback")
            end
            if state.frame >= active.end_frame then stop(nil, "demo_midpoint", raw); return end
        elseif active and active.kind == "mirror" then
            local raw
            if not selected.skip_response then raw = gui.gdscreenshot(); feedback(raw) end
            local held_until = plan.frame
            for _, press in ipairs(plan.presses) do
                if press.started then held_until = math.max(held_until, press.frame + plan.hold_frames) end
            end
            if state.frame >= math.max(plan.response_end_frame, held_until) then
                raw = raw or gui.gdscreenshot()
                for index, press in ipairs(plan.presses) do
                    if not press.started then skip_press(press, "response_endpoint_reached") end
                    if press.started and not press.judgment then retain_miss(index, raw, "after_frame_feedback") end
                end
                if skipped_count() > 0 then warn(string.format("%d/%d inputs skipped", skipped_count(), selected.expected_cues)) end
                response_complete = true
                write(selected.work .. "/response-end.gd", raw, "wb")
                stop(nil, "dance_1_response_complete", raw); return
            end
        end
    end)
    if not ok then stop(tostring(message)); return end
    if state.frame % 15 == 0 then publish() end
end)

release(); mailbox:capture_screen(); publish(); emu.pause()
print("Prepared contest rank-speed probe ready.")
