-- Contest Stage 1: shared inputs, then one screenshot-reviewed paused checkpoint.
local source = debug.getinfo(1, "S").source:sub(2):gsub("\\", "/")
local directory = assert(source:match("^(.*)/"))
local root = dofile(directory .. "/resource_root.lua")
local master = dofile(root .. "/lua/checkpoint_master.lua")
local bridge_io = dofile(root .. "/lua/bridge_io.lua")
local screen_checks = dofile(directory .. "/screen_checks.lua")
local mailbox = bridge_io.new(root .. "/tmp", "checkpoint-")
local command_id = mailbox:previous_id()
local idle = false
local adapter = dofile(root .. "/lua/checkpoint_desmume.lua").new({
    pause = function() assert(idle, "A host-confirmed native pause is required") end,
    is_paused = function() return idle end,
})
local api, engine, latest, pending, captured, screen
local stage = "boot"
local observer = {name = "pt-contest-stage1", commands = {}}

function observer.snapshot(frame)
    local code = emu.gamecode()
    if screen then
        screen.fresh_visual_binding = captured.frame == frame and captured.epoch == adapter.epoch()
            and captured.raw == gui.gdscreenshot()
        if screen.source == "local_reference_pixels" then
            screen.pixel_check = screen_checks.read(captured.raw, "loaded_overworld")
            screen.confirmed = screen.confirmed and screen.fresh_visual_binding and screen.pixel_check.confirmed
        end
    end
    return {frame = frame,
        game = {code = code, supported = code == "CPUJ", identity_source = "emu.gamecode"},
        screen = screen or {source = "unreviewed", memory_proven = false}}
end

local function packet(observation)
    local result = {frame = adapter.frame(), epoch = adapter.epoch(), checkpoint = latest,
        observation = observation, next_section = stage}
    if engine and not pending then result.progress = engine:state() end
    return result
end

local function dispatch(action, args)
    local observation
    if action == "state-changed" then
        adapter.state_changed()
        api.rebase()
        captured, screen = nil, nil
        assert(not engine, "Unexpected reset/load inside the one-pass contest loader")
    elseif action == "init" then
        assert(not engine and #args == 2, "init needs a run ID and checkpoint log path")
        engine = master.new({run_id = args[1], emulator = adapter, reader = observer,
            log = master.jsonl(args[2]), definitions = {
                ["stage-1-load"] = {checks = {
                    {id = "loaded_overworld_screenshot", field = "screen.confirmed", expected = true},
                    {id = "review_frame", field = "screen.frame", context = "frame"},
                    {id = "review_epoch", field = "screen.epoch", context = "epoch"},
                }, branches = {pass = "finish"}},
                stopped = {checks = {{id = "host_stop", expected = "continue",
                    evaluate = function(_, context) return false, context.reason end}},
                    branches = {pass = "finish"}},
            }})
        stage = "load"
    elseif action == "capture" then
        assert(engine and stage == "load" and api.state.command_done
            and api.state.stop_reason == "combo_complete", "Finish the loading inputs before screenshot review")
        api.capture()
        captured = {frame = adapter.frame(), epoch = adapter.epoch(), raw = gui.gdscreenshot()}
        observation = observer.snapshot(adapter.frame())
        observation.automatic_check = screen_checks.read(captured.raw, "loaded_overworld")
        stage = "review"
    elseif action == "visit" then
        assert(stage == "review" and not pending and #args == 6, "Expected the single endpoint screenshot verdict")
        assert((args[1] == "pass" or args[1] == "fail")
            and (args[2] == "visual_review" or args[2] == "local_reference_pixels")
            and args[3] ~= "" and args[4] ~= "", "Supply the actual screenshot verdict, image, and visual note")
        assert(tonumber(args[5]) == captured.frame and tonumber(args[6]) == captured.epoch
            and adapter.frame() == captured.frame and adapter.epoch() == captured.epoch,
            "The reviewed screenshot no longer belongs to this paused boundary")
        screen = {source = args[2], path = args[3], note = args[4], confirmed = args[1] == "pass",
            expected = "loaded_overworld", memory_proven = false,
            frame = captured.frame, epoch = captured.epoch}
        latest = engine:visit("stage-1-load", {frame = captured.frame, epoch = captured.epoch})
        pending = latest.status == "awaiting_save" and latest or nil
        observation = latest.snapshot
        stage = "checkpoint"
    elseif action == "complete" then
        assert(pending and #args == 6, "Expected pending saved evidence")
        latest = engine:complete(tonumber(args[2]), {run_id = args[1], visit = tonumber(args[2]),
            frame = tonumber(args[3]), epoch = tonumber(args[4]), path = args[5], emulator_paused = args[6] == "true"})
        pending = nil
        stage = latest.proceed and latest.branch or "stopped"
        observation = latest.snapshot
        print("Contest Stage 1: " .. (latest.proceed and "PASS" or "FAIL"))
    elseif action == "abort" then
        local reason = args[1] or "Host stopped contest Stage 1"
        api.finish(reason)
        stage = "stopped"
        if pending then
            latest, pending = engine:cancel(reason), nil
        elseif engine and not engine:state().halted then
            latest = engine:visit("stopped", {reason = reason})
            pending = latest.status == "awaiting_save" and latest or nil
        end
        observation = latest and latest.snapshot
        api.capture()
    else
        error("Unknown contest Stage 1 checkpoint command: " .. tostring(action))
    end
    return packet(observation)
end

function observer.attach(bridge)
    api = bridge
    api.state.checkpoint_protocol = type(emu.registermenustart) == "function" and 1 or 0
    if emu.registermenustart then
        emu.registermenustart(function()
            local parts = mailbox:read_command(command_id)
            if not parts then return end
            command_id = parts[1]
            local args = {}
            for index = 3, #parts do args[#args + 1] = parts[index] end
            assert(not idle, "Reentrant checkpoint request")
            idle = true
            api.state.frame = adapter.frame()
            local ok, result = pcall(dispatch, parts[2], args)
            if not ok then
                local message = tostring(result)
                if pending then latest, pending = engine:cancel(message), nil end
                api.finish(message)
                stage = "stopped"
                result = {frame = adapter.frame(), epoch = adapter.epoch(), error = message}
            end
            idle = false
            result.command_id, result.command_done = command_id, true
            api.state.checkpoint_epoch = adapter.epoch()
            api.state.checkpoint_visit = result.checkpoint and result.checkpoint.visit
            api.state.checkpoint_status = result.checkpoint and result.checkpoint.status
            api.state.observation_stale = false
            api.publish(result.observation or (latest and latest.frame == adapter.frame() and latest.snapshot) or nil)
            mailbox:publish(result, false)
        end)
    end
end

assert(loadfile(root .. "/lua/input_bridge.lua"))(observer)
