-- Game-independent DeSmuME inputs using the existing controller mailbox.
-- An optional boot hold enforces configured post-reset input guards.
-- An optional observer argument adds game data and on-demand read commands.
local observer = ...
local source = debug.getinfo(1, "S").source:sub(2):gsub("\\", "/")
local script_dir = assert(source:match("^(.*)/"), "Cannot locate the Lua script folder")
local combos = dofile(script_dir .. "/input_sequences.lua")
local bridge_io = dofile(script_dir .. "/bridge_io.lua")
local mailbox = bridge_io.new(script_dir .. "/../tmp")
local apply_buttons = bridge_io.apply_buttons
local valid_buttons = bridge_io.valid_buttons
local array_metatable = bridge_io.array_metatable
local frames_since_reset = emu.framecount()
local last_frame = emu.framecount()
local guard_frames, guard_buttons = 0, {}
local guard_applied = false
local active = nil
local state = {
    protocol = 1, bridge = observer and observer.name or "input", game_code = emu.gamecode(),
    frame = emu.framecount(), frames_since_reset = frames_since_reset,
    phase = "ready", buttons = "", command_id = "", command_done = true,
    error = "", input_guard_frames = 0, input_guard_active = false,
    input_guard_buttons = "", combo_history = setmetatable({}, array_metatable),
}

state.command_id = mailbox:previous_id()

local function publish(observation)
    if observer then state.observation = observation or observer.snapshot(state.frame) end
    mailbox:publish(state)
end

local function capture_screen()
    mailbox:capture_screen()
    state.screen_frame = state.frame
end

local function finish(message)
    if not message and active and active.read then
        local ok, result = pcall(active.read, state.frame)
        if ok then state.read_result = result else message = tostring(result) end
    end
    active = nil
    apply_buttons(nil)
    state.buttons, state.command_done = "", true
    state.error = message or ""
    state.phase = message and "command_error" or "ready"
    state.stop_reason = message and "command_error" or (state.combo_name ~= "" and "combo_complete" or "command_complete")
    state.command_end_frame = state.frame
end

local function stop(message)
    finish(message)
    capture_screen()
    publish()
    emu.pause()
end

local function whole(value, minimum)
    local number = tonumber(value)
    assert(number and number < math.huge and number >= minimum and number == math.floor(number), "Expected whole frame count")
    return number
end

local function begin_step()
    active.step_elapsed = 0
    active.plan.steps[active.step_index].start_frame = emu.framecount()
    state.combo_step = active.step_index
end

local function start_command(parts)
    local action = parts[2]
    state.command_id, state.command_done, state.error = parts[1], false, ""
    state.read_result = nil
    state.last_action, state.command_start_frame = action, emu.framecount()
    state.combo_name, state.combo_step = "", 0
    state.combo_history = setmetatable({}, array_metatable)
    active = {elapsed = 0, total = 1}
    if action == "combo" then
        local arguments = {}
        for index = 4, #parts do arguments[#arguments + 1] = parts[index] end
        active.plan = combos.build(parts[3], arguments, frames_since_reset, guard_frames, valid_buttons)
        active.step_index = 1
        state.combo_name = active.plan.name
        begin_step()
    elseif action == "advance" then
        active.total = whole(parts[3], 1)
    elseif action == "press" then
        assert(frames_since_reset >= guard_frames, "Finish the configured boot hold before ordinary inputs")
        assert(valid_buttons[parts[3]], "Unknown DS button")
        active.button, active.hold = parts[3], whole(parts[4] or 1, 1)
        -- A zero released wait ends at the held-frame boundary; stop() releases
        -- the controls and pauses without executing an additional game frame.
        active.total = active.hold + whole(parts[5] or 1, 0)
    elseif action == "guard-frames" then
        guard_frames = whole(parts[3], 0)
        state.input_guard_frames = guard_frames
    elseif action == "guard-buttons" then
        assert(#parts >= 3, "Supply boot buttons or none")
        guard_buttons = bridge_io.guard_buttons(parts)
        state.input_guard_buttons = table.concat(guard_buttons, ",")
    elseif action == "reset" then
        frames_since_reset, last_frame = 0, 0
        emu.reset()
    elseif observer and observer.commands[action] then
        active.read = observer.prepare(action, parts)
    elseif action ~= "snapshot" then
        error("Unknown input bridge command: " .. tostring(action))
    end
end

local function before_frame()
    if emu.framecount() < last_frame then frames_since_reset = emu.framecount() end
    state.frame, state.frames_since_reset = emu.framecount(), frames_since_reset
    state.input_guard_active = frames_since_reset < guard_frames
    if not active then
        local parts = mailbox:read_command(state.command_id)
        if parts then
            local ok, message = pcall(start_command, parts)
            if not ok then stop(tostring(message)); return end
        end
    end
    local guarded = frames_since_reset < guard_frames
    if guarded then
        apply_buttons(guard_buttons)
    elseif active then
        local input, elapsed = active, active.elapsed
        if active.plan then
            input, elapsed = active.plan.steps[active.step_index], active.step_elapsed
        end
        apply_buttons(input.button and elapsed < input.hold and input.button or nil)
    elseif guard_applied then
        apply_buttons(nil)
    end
    guard_applied = guarded
end

local function after_frame()
    frames_since_reset = frames_since_reset + 1
    last_frame = emu.framecount()
    state.frame, state.frames_since_reset = last_frame, frames_since_reset
    state.input_guard_active = frames_since_reset < guard_frames
    if observer and observer.update then
        local ok, message = pcall(observer.update, state.frame)
        if not ok then stop(tostring(message)); return end
    end
    if active then
        active.elapsed = active.elapsed + 1
        if active.plan then
            active.step_elapsed = active.step_elapsed + 1
            local step = active.plan.steps[active.step_index]
            if active.step_elapsed >= step.total then
                state.combo_history[#state.combo_history + 1] = {
                    action = step.action, start_frame = step.start_frame,
                    end_frame = state.frame, elapsed_frames = active.step_elapsed,
                }
                active.step_index = active.step_index + 1
                if active.step_index > #active.plan.steps then stop(); return end
                begin_step()
            end
        elseif active.elapsed >= active.total then
            stop()
            return
        end
    end
    if observer and state.frame % 15 == 0 then publish() end
end

if observer and observer.attach then
    observer.attach({
        state = state, publish = publish, capture = capture_screen, finish = finish,
        rebase = function()
            frames_since_reset, last_frame = emu.framecount(), emu.framecount()
            state.frame, state.frames_since_reset = last_frame, frames_since_reset
            finish()
        end,
    })
end

emu.registerbefore(before_frame)
emu.registerafter(after_frame)
capture_screen()
publish()
emu.pause()
print("DeSmuME input bridge ready.")
