-- Prepared Dance 1 measurements and one-response trials through paused checkpoints.
local directory = assert(debug.getinfo(1, "S").source:sub(2):gsub("\\", "/"):match("^(.*)/"))
local root = directory .. "/.."
local bridge_io = dofile(root .. "/lua/bridge_io.lua")
local master = dofile(root .. "/lua/checkpoint_master.lua")
local json = dofile(root .. "/lua/json.lua")
local pixels = dofile(directory .. "/dance_pixels.lua")
local templates = dofile(directory .. "/feedback-templates.lua")
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

local function close_files()
    for _, file in ipairs({measurement_file or false, demo_bytes or false,
            demo_index or false, response_events or false}) do
        if file then assert(file:close()) end
    end
    measurement_file, demo_bytes, demo_index, response_events = nil, nil, nil, nil
end

local function observation(frame, raw)
    local code = emu.gamecode()
    local excellent = 0
    for _, judgment in ipairs(judgments) do
        if judgment.label == "Excellent" then excellent = excellent + 1 end
    end
    return {frame = frame, game = {code = code, supported = code == "CPUJ", identity_source = "emu.gamecode"},
        epoch = adapter.epoch(), rank = selected and selected.rank,
        rank_source = "user_prepared_native_slot", bar = raw and pixels.bar(raw) or nil,
        released = state.buttons == "" and not state.stylus_touch,
        trial = {inputs = inputs, judgments = judgments, judgment_count = #judgments,
            excellent_count = excellent, complete = response_complete}}
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
    value.image, value.cues = image, pixels.cues(raw)
    local consistent = plan ~= nil and #plan.presses == selected.expected_cues
    if plan then
        for index, press in ipairs(plan.presses) do
            local due = plan.frame + math.floor((press.response_x - plan.pointer_x) / plan.speed + 0.5) + plan.offset_frames
            local following = plan.presses[index + 1] and plan.presses[index + 1].frame or plan.predicted_end_frame
            if press.frame ~= due or press.frame <= plan.frame or press.frame + plan.hold_frames >= following then
                consistent = false
            end
        end
    end
    value.trial.plan_consistent = consistent
    value.trial.expected_cues = selected and selected.expected_cues or 0
    value.trial.before_first_input = plan ~= nil and frame < plan.presses[1].frame
    value.trial.fresh_visual_binding = plan ~= nil and plan.frame == frame and plan.epoch == adapter.epoch()
        and colors_match(raw, plan.pixel_points) or false
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
    trial_finish[#trial_finish + 1] = {id = "all_inputs", field = "trial.inputs", context = "expected_cues"}
    trial_finish[#trial_finish + 1] = {id = "all_feedback", field = "trial.judgment_count", context = "expected_cues"}
    trial_finish[#trial_finish + 1] = {id = "all_excellent", field = "trial.excellent_count", context = "expected_cues"}
    trial_finish[#trial_finish + 1] = {id = "complete_response", field = "trial.complete", expected = true}
    return {['measure-start'] = {checks = start, branches = {pass = "measurement"}},
        ['measure-end'] = {checks = finish, branches = {pass = "next_source"}},
        ['trial-start'] = {checks = trial_start, branches = {pass = "response"}},
        ['trial-finish'] = {checks = trial_finish, branches = {pass = "next_trial"}},
        stopped = {checks = {{id = "host_stop", field = "stop_reason", expected = "continue"}}, branches = {pass = "stopped"}}}
end

local function packet(value)
    local result = {frame = adapter.frame(), epoch = adapter.epoch(), observation = value,
        checkpoint = latest, next_section = state.phase}
    if engine and not pending then result.progress = engine:state() end
    return result
end

local function finish(message, reason)
    active = nil
    close_files(); release()
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

local function dispatch(action, args)
    if action == "init-run" then
        assert(not engine and #args == 2, "Initialize the probe once")
        work = args[2]
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
            and plan.speed > 0 and #plan.presses == selected.expected_cues, "The plan belongs to this source and midpoint")
    elseif action == "visit" then
        assert(not active and not pending, "Finish the action before evaluating its boundary")
        local context = assert(loadfile(args[2]))()
        latest = engine:visit(args[1], context)
        pending = latest.status == "awaiting_save"
    elseif action == "complete" then
        latest = engine:complete(tonumber(args[2]), {run_id = args[1], visit = tonumber(args[2]),
            frame = tonumber(args[3]), epoch = tonumber(args[4]), path = args[5], emulator_paused = args[6] == "true"})
        pending = false
    elseif action == "abort" then
        local reason = args[1] or "Host stopped the probe"
        finish(reason)
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
        if pending then latest = engine:cancel(tostring(result)); pending = false end
        result = {frame = adapter.frame(), epoch = adapter.epoch(), error = tostring(result), checkpoint = latest}
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
        retain_demo(gui.gdscreenshot(), "current_demo_entry")
    elseif parts[2] == "mirror" then
        assert(plan and latest and latest.checkpoint == "trial-start" and latest.proceed
            and latest.frame == state.frame, "Acknowledge the midpoint plan before responding")
        response_events = assert(io.open(selected.work .. "/response-events.jsonl", "w"))
        active = {kind = "mirror"}
        last_feedback = pixels.feedback(gui.gdscreenshot(), templates, plan.player_center_x)
    else
        error("Unknown rank-speed input action: " .. tostring(parts[2]))
    end
end

emu.registerbefore(function()
    state.frame = adapter.frame()
    if not active then
        local parts = mailbox:read_command(state.command_id)
        if parts then
            local ok, message = pcall(start, parts)
            if not ok then stop(tostring(message)); return end
        end
    end
    local button
    if active and active.kind == "mirror" then
        local frame = state.frame + 1
        for index, press in ipairs(plan.presses) do
            if frame == press.frame and index > 1 and not plan.presses[index - 1].judgment then
                stop("Fresh feedback is unreadable before the next response input"); return
            end
            if frame >= press.frame and frame < press.frame + plan.hold_frames then button = press.button end
            if frame == press.frame then
                press.started, press.feedback_before = true, last_feedback
                inputs = inputs + 1
                assert(response_events:write(json.encode({kind = "input", index = index,
                    frame = frame, button = press.button}), "\n"))
            end
        end
    end
    bridge_io.apply_buttons(button); stylus.set({touch = false})
    state.buttons, state.stylus_touch = button or "", false
end)

local function feedback(raw)
    local found = pixels.feedback(raw, templates, plan.player_center_x)
    local fresh = found and (not last_feedback or found.label ~= last_feedback.label or found.y > last_feedback.y + 2)
    if fresh then
        local eligible = {}
        for index, press in ipairs(plan.presses) do
            local before = press.feedback_before
            if press.started and not press.judgment and state.frame > press.frame
                and (not before or found.label ~= before.label or found.y > before.y + 2) then
                eligible[#eligible + 1] = index
            end
        end
        assert(#eligible <= 1, "Feedback belongs to multiple response inputs")
        if #eligible == 1 then
            local index, judgment = eligible[1], {label = found.label, feedback_frame = state.frame,
                text_x = found.x, text_y = found.y, source = found.source, fresh_message = true}
            local press = plan.presses[index]
            judgment.index, judgment.button, judgment.input_frame = index, press.button, press.frame
            judgment.visible_delay_frames = state.frame - press.frame
            press.judgment = judgment
            judgments[#judgments + 1] = judgment
            write(string.format("%s/judgment-%d-%s.gd", selected.work, index, found.label), raw, "wb")
            write(selected.work .. "/judgments.json", json.encode(judgments) .. "\n")
            assert(response_events:write(json.encode(judgment), "\n"))
            assert(found.label == "Excellent", "Dance 1 timing needs review: " .. found.label)
        end
    end
    last_feedback = found
end

emu.registerafter(function()
    state.frame = adapter.frame()
    local ok, message = pcall(function()
        if measurement_file and state.frame - measurement_last >= 6 then
            retain_measurement(gui.gdscreenshot(), "frame_callback")
        end
        if active and active.kind == "demo" then
            local raw = gui.gdscreenshot()
            local bar = pixels.bar(raw)
            if state.frame - demo_last >= 6 or bar.found and bar.last_x >= 128 then
                retain_demo(raw, "frame_callback")
            end
            assert(bar.found, "The prepared demonstration strip is unreadable")
            if bar.last_x >= 128 then stop(nil, "demo_midpoint", raw); return end
        elseif active and active.kind == "mirror" then
            local raw = gui.gdscreenshot()
            feedback(raw)
            local bar = pixels.bar(raw)
            if inputs == #plan.presses and #judgments == #plan.presses
                and state.frame >= plan.presses[#plan.presses].frame + plan.hold_frames
                and (not bar.found or bar.last_x < 128) then
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
