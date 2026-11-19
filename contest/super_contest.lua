-- One persistent native callback set, input dispatcher, and checkpoint engine.
local directory = assert(debug.getinfo(1, "S").source:sub(2):gsub("\\", "/"):match("^(.*)/"))
local root = dofile(directory .. "/resource_root.lua")
local bridge_io = dofile(root .. "/lua/bridge_io.lua")
local combos = dofile(root .. "/lua/input_sequences.lua")
local master = dofile(root .. "/lua/checkpoint_master.lua")
local sections = dofile(directory .. "/sections.lua")
local platinum = dofile(root .. "/lua/platinum.lua")
local memory_boundary = dofile(directory .. "/memory_boundary.lua")
local mailbox_directory = CONTEST_MAILBOX_DIR or root .. "/tmp"
CONTEST_MAILBOX_DIR = nil
local mailbox = bridge_io.new(mailbox_directory)
local checkpoint_mailbox = bridge_io.new(mailbox_directory, "checkpoint-")
local checkpoint_id, idle = checkpoint_mailbox:previous_id(), false
local active, current, config, latest, run_id, work, debug_mode
local last_dance_feedback_visit
local halted = false
local state = {protocol = 1, checkpoint_protocol = 1, bridge = "pt-super-contest",
    game_code = emu.gamecode(), frame = emu.framecount(), phase = "ready", buttons = "", stylus_touch = false,
    command_id = mailbox:previous_id(), command_done = true, error = "", combo_name = "",
    combo_history = setmetatable({}, bridge_io.array_metatable)}
local runtime = {state = state, mailbox_directory = mailbox_directory,
    is_paused = function() return idle end}
local adapter = dofile(root .. "/lua/checkpoint_desmume.lua").new({
    pause = function() assert(idle, "A host-confirmed native pause is required") end,
    is_paused = runtime.is_paused,
})
runtime.adapter = adapter

local stage_feedback = {
    {name = "Loading", checkpoint = "stage-1-load", message = "Fast text speed, position, Bag, and first Pokemon readiness confirmed."},
    {name = "Preparation", checkpoint = "stage-2-prep", message = "Scarf preparation inputs complete."},
    {name = "Contest Entry", checkpoint = "stage-3-enter", message = "Dress-up entry confirmed."},
    {name = "Dress-up", checkpoint = "stage-4-dress", message = "Dancing entry confirmed."},
    {name = "Dancing", checkpoint = "stage-5-dance", message = "Scripted Acting transition complete."},
    {name = "Acting", checkpoint = "stage-6-act", message = "Target ribbon confirmed."},
}
local feedback_stage, feedback_visit, reported_result, collection_reported = 1, 0, nil, false

local function begin_feedback(number, visit)
    assert(stage_feedback[number], "Unknown contest feedback stage")
    if feedback_stage ~= number or feedback_visit ~= visit then reported_result = nil end
    feedback_stage, feedback_visit = number, visit
end

function runtime.fail_stage()
    if reported_result ~= "fail" then
        print(stage_feedback[feedback_stage].name .. ": FAIL")
        reported_result = "fail"
    end
end

function runtime.main_dance_feedback(judgment, section)
    assert(section.stage == 5 and section.feedback_policy == "first_button_only"
        and section.first_feedback_policy == "warn_and_continue" and judgment.dance == 1 and judgment.index == 1
        and (judgment.label == "Excellent" and judgment.fresh_message or judgment.label == "Miss" or judgment.skipped)
        and (not last_dance_feedback_visit or section.section_visit > last_dance_feedback_visit),
        "Account for only the first mirrored input once per main contest")
    last_dance_feedback_visit = section.section_visit
    local label, warning = judgment.label, nil
    if label ~= "Excellent" then
        warning = string.format("Dancing: WARN - First mirrored button: %s (%s - %s); continuing.",
            label, section.target.category, section.target.rank)
    end
    local result = {label = label, warning = warning,
        section_visit = section.section_visit, target = section.target}
    state.dance_first_feedback = result
    if warning then print(warning) end
    return result
end

local function report_checkpoint(action, record)
    if not record then return end
    if record.decision == "abort" then runtime.fail_stage(); return end
    if action == "complete" and record.status == "complete" and record.proceed
        and record.checkpoint == "stage-6-round" and record.snapshot.trial.round == 1 then
        print("Acting entry: PASS - Judge status, acting order, and move selection ready.")
    end
    local feedback = stage_feedback[feedback_stage]
    if action ~= "complete" or record.status ~= "complete" or not record.proceed
        or record.checkpoint ~= feedback.checkpoint or reported_result then return end
    if record.decision == "miss" then
        if feedback_stage == 5 then
            print("Dancing: WARN - Handoff dance checks missed; continuing to Acting.")
            reported_result = "warning"
            return
        elseif feedback_stage == 6 then
            print("Acting: WARN - Target ribbon not acquired; retrying.")
            reported_result = "warning"
            return
        else runtime.fail_stage(); return end
    end
    print(feedback.name .. ": PASS - " .. feedback.message)
    reported_result = "pass"
    local snapshot = record.snapshot
    if record.checkpoint == "stage-6-act" then
        print("Ribbon acquired: " .. snapshot.target.category .. " - " .. snapshot.target.rank)
    end
    if snapshot.party and snapshot.party.all_ribbons_owned == true and not collection_reported then
        print("All ribbons acquired")
        collection_reported = true
    end
end

local function copy(value)
    if type(value) ~= "table" then return value end
    local result = {}
    for key, item in pairs(value) do result[key] = copy(item) end
    return setmetatable(result, getmetatable(value))
end

local function release()
    bridge_io.apply_buttons(nil)
    stylus.set({touch = false})
    state.buttons, state.stylus_touch = "", false
end
adapter.release = release

local function observation(frame)
    if current and current.observation then return current.observation(frame) end
    return {frame = frame, game = platinum.game(emu.gamecode()),
        active_stage = state.active_stage}
end

local function publish(value)
    state.observation = value or observation(state.frame)
    mailbox:publish(state)
end

local function capture()
    mailbox:capture_screen()
    state.screen_frame = state.frame
end

function runtime.image(path, frame)
    assert(idle and frame == adapter.frame(), "Fresh evidence must be captured from the current paused frame")
    local raw = gui.gdscreenshot()
    local file = assert(io.open(path, "wb"))
    assert(file:write(raw)); assert(file:close())
    capture()
    return {path = path, frame = frame, epoch = adapter.epoch(), source = "fresh_zero_frame_framebuffer",
        capture_callback = "paused_lua_menu", direct_read_frames = 0, retained = true}, raw
end

local function finish(message, reason)
    if message then
        runtime.fail_stage()
        print("Contest stopped: " .. tostring(message))
    end
    active = nil
    release()
    state.command_done, state.error = true, message or ""
    state.phase = message and "command_error" or "ready"
    state.stop_reason = message and "command_error" or reason
        or (state.combo_name ~= "" and "combo_complete" or "command_complete")
    state.command_end_frame = state.frame
end
runtime.api = {state = state, finish = finish, publish = publish, capture = capture}

local function stop(message)
    if current then current.finish(message) end
    finish(message)
    if message then halted = true end
    capture(); publish(); emu.pause()
end

local function identity(record)
    local value = {}
    for _, key in ipairs({"checkpoint", "run_id", "visit", "frame", "epoch"}) do value[key] = record[key] end
    return value
end

local function same_target(a, b)
    if type(a) ~= "table" or type(b) ~= "table" then return false end
    for _, key in ipairs({"category", "rank", "ribbon_bit", "party_slot"}) do
        if a[key] ~= b[key] then return false end
    end
    return true
end

local function stable(record)
    assert(record and record.status == "complete" and record.proceed and record.frame == adapter.frame()
        and record.epoch == adapter.epoch() and state.command_done and state.buttons == "" and not state.stylus_touch,
        "Continue only from the current acknowledged, paused, released boundary")
end

local function initialize(args)
    assert(not runtime.engine and #args == 4 and (args[3] == "0" or args[3] == "1"),
        "Initialize once with run identity, debug mode, and the resolved regional timing profile")
    run_id, work, debug_mode = args[1], args[2], tonumber(args[3])
    runtime.timing_profile = assert(loadfile(args[4]))()
    assert(runtime.timing_profile.schema == 1 and runtime.timing_profile.game_code == emu.gamecode()
        and type(runtime.timing_profile.sections) == "table",
        "The resolved timing profile must match the trusted running ROM code")
    state.timing_profile_source = runtime.timing_profile.source
    local definitions = sections.definitions()
    local prototypes = {
        assert(loadfile(directory .. "/stage4_dress.lua"))({stage = 4}, runtime),
        assert(loadfile(directory .. "/rank_dance.lua"))({stage = 5}, runtime),
    }
    for _, section in ipairs(prototypes) do
        for name, definition in pairs(section.definitions()) do
            if name ~= "stopped" then
                assert(not definitions[name], "Duplicate integrated checkpoint definition")
                definitions[name] = definition
            end
        end
    end
    local save_policies = {
        ["stage-1-load"] = "screen", ["stage-2-prep"] = "memory", ["stage-3-enter"] = "screen",
        ["stage-4-dress"] = "handoff", ["stage-4-dress-page"] = "screen", ["stage-5-dance"] = "handoff",
        ["stage-6-round"] = "screen", ["stage-6-act"] = "memory",
        ["rank-dance-start"] = "screen", ["rank-dance-offset"] = "screen", ["rank-dance-finish"] = "screen",
        stopped = "debug",
    }
    for name, definition in pairs(definitions) do
        local policy = assert(save_policies[name], "Classify each contest checkpoint's save policy")
        definition.save_state = debug_mode == 1 and (policy == "memory" or policy == "handoff" or policy == "debug")
    end
    state.debug = debug_mode
    runtime.engine = master.new({run_id = run_id, emulator = adapter, definitions = definitions,
        reader = {snapshot = function(frame)
            if current then return current.snapshot(frame) end
            local value = observation(frame)
            value.image = runtime.image(work .. "/failure.gd", frame)
            return value
        end}, log = master.jsonl(work .. "/checkpoint-events.jsonl")})
end

local function begin_section(path)
    assert(runtime.engine and not halted and state.command_done, "The shared runtime must be ready")
    local next_config = assert(loadfile(path))()
    begin_feedback(next_config.stage, next_config.section_visit)
    assert(next_config.run_id == run_id and next_config.source_frame == adapter.frame(),
        "The section must use this runtime and its current paused source frame")
    local number, source = next_config.stage, latest
    if number == 1 then
        assert(not latest and not current, "Run Stage 1 once at fresh setup")
    else
        stable(source)
        local order = {["stage-1-load"] = 2, ["stage-2-prep"] = 3, ["stage-3-enter"] = 4,
            ["stage-4-dress"] = 5, ["stage-5-dance"] = 6}
        local prepare_again = source.checkpoint == "stage-6-act"
            and (source.decision == "hit" or source.decision == "miss")
        local expected = prepare_again and 2 or order[source.checkpoint]
        assert(number ~= 6 or next_config.entry_mode == "stage5_handoff", "Acting entry is a direct Stage 5 handoff")
        assert(number == expected, "The accepted checkpoint did not authorize this stage transition")
        for key, value in pairs(identity(source)) do
            assert(next_config.source_context[key] == value, "The section's source context differs from its accepted boundary")
        end
        if number ~= 2 then
            assert(same_target(next_config.target, source.snapshot.target), "The section changed the accepted contest target")
        end
        if number == 2 then
            next_config.accepted_readiness = copy(source.snapshot)
        end
        next_config.party_context = copy(runtime.party_context)
        if number == 6 then
            assert(runtime.startup_party and runtime.party_context, "Readiness must precede the first Acting entry")
            next_config.startup_party = copy(runtime.startup_party)
        end
        if number == 5 and next_config.rank_timing then
            next_config.accepted_entry_pointer = copy(source.snapshot.screen and source.snapshot.screen.pointer)
        end
    end
    finish(nil, "section_ready")
    state.combo_name, state.combo_history = "", setmetatable({}, bridge_io.array_metatable)
    config = next_config
    if number == 4 then
        assert(config.entry_render_frames == 0 and config.entry_mode == "stage3_handoff",
            "Direct Stage 3 to Stage 4 entry adds zero render frames")
        current = assert(loadfile(directory .. "/stage4_dress.lua"))(config, runtime)
    elseif number == 5 then
        assert(config.refresh_frames == 0, "Direct dancing entry adds zero refresh frames")
        current = assert(loadfile(directory .. "/rank_dance.lua"))(config, runtime)
    else
        current = sections.new(config, runtime)
    end
    state.active_stage, state.section_visit = number, config.section_visit
    return {frame = adapter.frame(), epoch = adapter.epoch(), next_section = "section_ready",
        handoff = {stage = number, source_context = config.source_context,
            emulator_frames = 0, native_lua_attachment = "persistent"}}
end

local function dispatch(action, args)
    if action == "init-run" then
        initialize(args)
    elseif action == "state-changed" then
        assert(args[1] == "reset" and not runtime.engine, "Quick-loads are disabled; reset requires initial boot")
        adapter.state_changed()
        state.frame = adapter.frame()
        finish(nil, "state_changed")
        state.observation_stale = true
    elseif action == "begin-section" then
        assert(#args == 1, "Supply the section configuration path")
        return begin_section(args[1])
    elseif action == "terminal" then
        if current then current.finish() end
        finish(nil, "terminal")
    elseif action == "stage-failed" then
        assert(#args == 2, "Supply the failed stage and its visit")
        begin_feedback(tonumber(args[1]), tonumber(args[2]))
        runtime.fail_stage()
    elseif action == "complete" and not current then
        assert(runtime.engine and latest and latest.checkpoint == "stopped" and (#args == 6 or #args == 7),
            "Expected stopped runtime evidence before starting a section")
        latest = runtime.engine:complete(tonumber(args[2]), {run_id = args[1], visit = tonumber(args[2]),
            frame = tonumber(args[3]), epoch = tonumber(args[4]), path = args[5], emulator_paused = args[6] == "true",
            saved = args[7] ~= "false"})
        return {frame = adapter.frame(), epoch = adapter.epoch(), checkpoint = latest, progress = runtime.engine:state()}
    elseif action == "abort" and not current then
        finish(args[1] or "Host stopped the contest runtime")
        if runtime.engine then
            local ok, cancelled = pcall(runtime.engine.cancel, runtime.engine, args[1])
            if ok then latest = cancelled
            elseif not runtime.engine:state().halted then latest = runtime.engine:visit("stopped", {reason = args[1]}) end
        end
        return {frame = adapter.frame(), epoch = adapter.epoch(), checkpoint = latest}
    else
        assert(current, "Start a callable section before issuing its commands")
        local result = current.checkpoint(action, args)
        if action == "complete" and result.checkpoint
            and result.checkpoint.status == "complete" and result.checkpoint.proceed then
            local record = result.checkpoint
            if record.checkpoint == "stage-1-load" then runtime.startup_party = copy(record.snapshot.party) end
            if record.checkpoint == "stage-1-load" or record.checkpoint == "stage-6-act" then
                runtime.party_context = copy(memory_boundary.context(record))
            end
        end
        if action == "init-prep" and not result.error and config.target then
            print("Contest selected: " .. config.target.category .. " - " .. config.target.rank)
        end
        return result
    end
    return {frame = adapter.frame(), epoch = adapter.epoch(), next_section = action}
end

emu.registermenustart(function()
    local parts = checkpoint_mailbox:read_command(checkpoint_id)
    if not parts then return end
    checkpoint_id = parts[1]
    local args = {}
    for index = 3, #parts do args[#args + 1] = parts[index] end
    assert(not idle, "Reentrant persistent checkpoint request")
    idle, state.frame = true, adapter.frame()
    local ok, result = pcall(dispatch, parts[2], args)
    if not ok or result.error then
        local message = not ok and tostring(result) or result.error
        local cancelled_ok, cancelled = false, nil
        if runtime.engine then cancelled_ok, cancelled = pcall(runtime.engine.cancel, runtime.engine, message) end
        if current then current.finish(message) end
        finish(message)
        halted = true
        result = {frame = adapter.frame(), epoch = adapter.epoch(), error = message,
            checkpoint = cancelled_ok and cancelled or type(result) == "table" and result.checkpoint or nil}
    end
    if result.checkpoint then latest = result.checkpoint end
    report_checkpoint(parts[2], result.checkpoint)
    state.checkpoint_epoch = adapter.epoch()
    state.checkpoint_visit = latest and latest.visit
    state.checkpoint_status = latest and latest.status
    if parts[2] ~= "state-changed" then state.observation_stale = false end
    result.command_id, result.command_done = checkpoint_id, true
    publish(result.observation)
    idle = false
    checkpoint_mailbox:publish(result, false)
end)

local function start(parts)
    local action = parts[2]
    state.command_id = parts[1]
    assert(not halted, "The persistent runtime is stopped")
    assert(action == "reset" and not runtime.engine
        or current and current.authorize and current.authorize(action),
        "The active section did not authorize this input action")
    state.command_id, state.command_done, state.error = parts[1], false, ""
    state.last_action, state.command_start_frame = action, state.frame
    state.combo_name, state.combo_step = "", 0
    state.combo_history = setmetatable({}, bridge_io.array_metatable)
    active = {elapsed = 0, total = 1}
    if action == "combo" then
        assert(parts[3] == "inputs", "Contest sections use the prepared explicit input steps")
        local args = {}
        for index = 4, #parts do args[#args + 1] = parts[index] end
        active.plan = combos.build("inputs", args, state.frame, 0, bridge_io.valid_buttons)
        active.step_index, active.step_elapsed = 1, 0
        active.plan.steps[1].start_frame = state.frame
        state.combo_name, state.combo_step = "inputs", 1
    elseif action == "advance" then
        active.total = tonumber(parts[3])
        assert(#parts == 3 and active.total == config.handoff_render_frames, "Use the agreed counted Acting-entry preparation")
    else
        emu.reset()
    end
end

emu.registerbefore(function()
    state.frame = adapter.frame()
    if current and current.before then
        local ok, message = pcall(current.before)
        if not ok then stop(tostring(message)) end
        return
    end
    if not active then
        local parts = mailbox:read_command(state.command_id)
        if parts then
            local ok, message = pcall(start, parts)
            if not ok then stop(tostring(message)); return end
        end
    end
    local input, elapsed = active, active and active.elapsed
    if active and active.plan then input, elapsed = active.plan.steps[active.step_index], active.step_elapsed end
    local button = input and input.button and elapsed < input.hold and input.button or nil
    bridge_io.apply_buttons(button)
    stylus.set({touch = false})
    state.buttons, state.stylus_touch = button or "", false
end)

emu.registerafter(function()
    state.frame = adapter.frame()
    if current and current.after then
        local ok, message = pcall(current.after)
        if not ok then stop(tostring(message)) end
        return
    end
    if active then
        active.elapsed = active.elapsed + 1
        if active.plan then
            active.step_elapsed = active.step_elapsed + 1
            local step = active.plan.steps[active.step_index]
            if active.step_elapsed >= step.total then
                state.combo_history[#state.combo_history + 1] = {action = step.action, start_frame = step.start_frame,
                    end_frame = state.frame, elapsed_frames = active.step_elapsed}
                active.step_index = active.step_index + 1
                if active.step_index > #active.plan.steps then stop(); return end
                active.step_elapsed = 0
                active.plan.steps[active.step_index].start_frame = state.frame
                state.combo_step = active.step_index
            end
        elseif active.elapsed >= active.total then stop(); return end
    end
    if state.frame % 15 == 0 then publish() end
end)

release(); capture(); publish(); emu.pause()
print("Persistent Super Contest runtime ready.")
