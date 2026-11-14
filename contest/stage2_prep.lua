-- Ribbon/scarf preparation using the passed scarf test's selected reader and combo.
local directory = assert(debug.getinfo(1, "S").source:sub(2):gsub("\\", "/"):match("^(.*)/"))
local root = directory .. "/.."
local master = dofile(root .. "/lua/checkpoint_master.lua")
local bridge_io = dofile(root .. "/lua/bridge_io.lua")
local selected = dofile(directory .. "/bag_contest.lua")
local reader = selected.new(memory, emu.gamecode)
local mailbox = bridge_io.new(root .. "/tmp", "checkpoint-")
local command_id, idle = mailbox:previous_id(), false
local adapter = dofile(root .. "/lua/checkpoint_desmume.lua").new({
    pause = function() assert(idle, "A host-confirmed native pause is required") end,
    is_paused = function() return idle end,
})
local api, engine, latest, pending, initial, plan
local stage = "setup"
local observer = {name = "pt-contest-stage2", commands = {}}

function observer.snapshot(frame)
    return {frame = frame, game = {code = emu.gamecode(), supported = emu.gamecode() == "CPUJ"}}
end

local function snapshot(frame)
    local result = reader.snapshot(frame)
    result.target, result.bag_context = plan.target, plan.bag_context
    result.scripted_section_complete = api.state.command_done and api.state.buttons == ""
        and frame == initial.frame + plan.emulator_frames
        and (#plan.inputs == 0 or api.state.stop_reason == "combo_complete")
    return result
end

local function dispatch(action, args)
    local observation
    if action == "state-changed" then
        assert(not engine, "Unexpected load/reset during Stage 2")
        adapter.state_changed()
        api.rebase()
        initial = nil
    elseif action == "inspect" then
        assert(not engine, "Inspect the Stage 2 source before preparing it")
        initial = reader.snapshot(adapter.frame())
        initial.epoch = adapter.epoch()
        observation = initial
        api.capture()
    elseif action == "init" then
        assert(not engine and #args == 3 and initial and initial.frame == adapter.frame()
            and initial.epoch == adapter.epoch(), "Expected the fresh Stage 2 source and input plan")
        plan = assert(loadfile(args[3]))()
        assert(plan.source_species_id == initial.party.species_id
            and plan.expected_item_id >= 0 and plan.expected_item_id <= 536,
            "The preparation plan differs from the first-party source")
        engine = master.new({run_id = args[1], emulator = adapter, reader = {snapshot = snapshot},
            log = master.jsonl(args[2]), definitions = {
                ["stage-2-prep"] = {checks = {
                    {id = "validated_first_party", field = "party.read_valid", expected = true},
                    {id = "same_first_party", field = "party.species_id", expected = plan.source_species_id},
                    {id = "same_ribbon_flags", field = "party.owned_ribbon_bits", expected = initial.party.owned_ribbon_bits},
                    {id = "prepared_held_item", field = "party.held_item_id", expected = plan.expected_item_id},
                    {id = "scripted_section_complete", field = "scripted_section_complete", expected = true},
                }, branches = {pass = "finish"}},
                stopped = {checks = {{id = "host_stop", expected = "continue",
                    evaluate = function(_, context) return false, context.reason end}}, branches = {pass = "finish"}},
            }})
        stage = "prepare"
    elseif action == "visit" then
        assert(engine and stage == "prepare" and not pending, "Expected one completed Stage 2 section")
        api.capture()
        latest = engine:visit("stage-2-prep", {})
        pending = latest.status == "awaiting_save" and latest or nil
        observation, stage = latest.snapshot, "checkpoint"
    elseif action == "complete" then
        assert(pending and #args == 6, "Expected pending saved evidence")
        latest = engine:complete(tonumber(args[2]), {run_id = args[1], visit = tonumber(args[2]),
            frame = tonumber(args[3]), epoch = tonumber(args[4]), path = args[5], emulator_paused = args[6] == "true"})
        pending, observation = nil, latest.snapshot
        stage = latest.proceed and latest.branch or "stopped"
        print("Contest Stage 2: " .. (latest.proceed and "PASS" or "FAIL"))
    elseif action == "abort" then
        local reason = args[1] or "Host stopped contest Stage 2"
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
        error("Unknown Stage 2 checkpoint command: " .. tostring(action))
    end
    local result = {frame = adapter.frame(), epoch = adapter.epoch(), checkpoint = latest,
        observation = observation, next_section = stage}
    if engine and not pending then result.progress = engine:state() end
    return result
end

function observer.attach(bridge)
    api = bridge
    api.state.checkpoint_protocol = 1
    emu.registermenustart(function()
        local parts = mailbox:read_command(command_id)
        if not parts then return end
        command_id = parts[1]
        local args = {}
        for index = 3, #parts do args[#args + 1] = parts[index] end
        assert(not idle, "Reentrant checkpoint request")
        idle, api.state.frame = true, adapter.frame()
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
        api.publish(result.observation)
        mailbox:publish(result, false)
    end)
end
assert(loadfile(root .. "/lua/input_bridge.lua"))(observer)
