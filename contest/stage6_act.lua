-- Four pre-move judge captures, Acting inputs, and ribbon-driven slot-5 retries.
local config = ...
assert(type(config) == "table" and config.stage == 6, "Stage 6 configuration is required")
assert(type(config.handoff_render_frames) == "number" and config.handoff_render_frames >= 1
    and config.handoff_render_frames == math.floor(config.handoff_render_frames)
    and type(config.retry_render_host_seconds) == "number" and config.retry_render_host_seconds > 0
    and config.retry_render_host_seconds < math.huge,
    "Stage 6 requires positive configured frame and host-second render windows")
local source = debug.getinfo(1, "S").source:sub(2):gsub("\\", "/")
local directory = assert(source:match("^(.*)/"))
local root = dofile(directory .. "/resource_root.lua")
local master = dofile(root .. "/lua/checkpoint_master.lua")
local bridge_io = dofile(root .. "/lua/bridge_io.lua")
local platinum = dofile(root .. "/lua/platinum.lua")
local pixels = dofile(directory .. "/acting_pixels.lua")
local acting_plan = dofile(directory .. "/acting_plan.lua")
local party_reader = dofile(directory .. "/party_contest.lua").new(memory, emu.gamecode)
local mailbox = bridge_io.new(root .. "/tmp", "checkpoint-")
local command_id, idle = mailbox:previous_id(), false
local adapter = dofile(root .. "/lua/checkpoint_desmume.lua").new({
    pause = function() assert(idle, "A host-confirmed native pause is required") end,
    is_paused = function() return idle end,
})
local api, engine, latest, pending, current_plan, source_party, entry_moves, result_party
local stage, capture_kind = "setup", "source"
local attempt, round_index, completed_rounds = 0, 0, 0
local pause_plan, settled
local readiness_checks = 0
local render_prepared, render_epoch = false, nil
local render_mode, render_end_frame, render_host_seconds
local move_cursor, judge_cursor = 1, 1
local observer = {name = "pt-contest-stage6", commands = {}}

local function game()
    return platinum.game(emu.gamecode())
end

local function release()
    api.finish()
    stylus.set({touch = false})
    api.state.stylus_touch = false
end

local function write_image(path, raw)
    local file = assert(io.open(path, "wb"))
    assert(file:write(raw))
    assert(file:close())
end

local function trial()
    return {attempt = attempt, round = round_index, completed_rounds = completed_rounds,
        move_cursor = move_cursor, judge_cursor = judge_cursor, stage = stage,
        render_preparation_frames = render_prepared and render_end_frame - config.source_frame or 0,
        render_preparation_mode = render_mode, render_preparation_host_seconds = render_host_seconds,
        readiness_checks = readiness_checks, readiness_settle_frames = settled and settled.frames or 0,
        released = api.state.command_done and api.state.buttons == "" and api.state.stylus_touch == false}
end

function observer.snapshot(frame)
    return {frame = frame, game = game(), trial = trial(), target = config.target}
end

function observer.update(frame)
    if stage == "render-retry" then
        -- Keep the host-timed free-running window input-free on every frame.
        release()
    end
end

local function plan_round(frame, stars, acting_order)
    return acting_plan.round(config, round_index, move_cursor, judge_cursor, frame, stars, entry_moves, acting_order)
end

local function checkpoint_snapshot(frame)
    local result = observer.snapshot(frame)
    result.source_context = config.source_context
    local raw = gui.gdscreenshot()
    assert(idle and adapter.frame() == frame, "The fresh paused capture must add zero emulator frames")
    local folder = config.work .. string.format("/attempt-%02d", attempt)
    local path = capture_kind == "round" and folder .. string.format("/round-%d.gd", round_index)
        or capture_kind == "result" and folder .. "/result.gd" or config.work .. "/failure.gd"
    write_image(path, raw)
    result.image = {path = path, frame = frame, epoch = adapter.epoch(),
        source = "fresh_zero_frame_framebuffer", capture_callback = "paused_lua_menu",
        direct_read_frames = 0, retained = true}
    if capture_kind == "round" then
        result.stars = pixels.read_stars(raw)
        result.acting_order = pixels.read_acting_order(raw)
        result.acting_order.frame, result.acting_order.epoch, result.acting_order.path =
            frame, result.image.epoch, result.image.path
        if round_index == 1 and not entry_moves then
            entry_moves = pixels.read_move_categories(raw, config.unavailable_move_slots)
            entry_moves.frame, entry_moves.epoch, entry_moves.path = frame, result.image.epoch, result.image.path
            for _, warning in ipairs(entry_moves.warnings) do print(warning) end
        end
        result.move_categories = entry_moves
        if round_index > 1 then
            result.round_readiness = pixels.read_round_readiness(raw, config.unavailable_move_slots,
                move_cursor, result.stars, result.acting_order)
        end
        if result.stars.readable and result.acting_order.readable and entry_moves then
            result.plan = plan_round(frame, result.stars, result.acting_order)
            for _, warning in ipairs(result.plan.warnings or {}) do print(warning) end
        end
    elseif capture_kind == "result" then
        result.screen = pixels.match_points(raw, config.overworld.points)
        result.screen.expected = "loaded_overworld"
        result.screen.reference = config.overworld.reference
        result.party = result_party
        result.same_first_party = result.party.species_id == source_party.species_id and not result.party.is_egg
        result.target_acquired = result.party.ribbon_flags[config.target.ribbon_bit + 1]
    end
    return result
end

local function initialize()
    engine = master.new({run_id = config.run_id, emulator = adapter,
        reader = {snapshot = checkpoint_snapshot}, log = master.jsonl(config.work .. "/checkpoint-events.jsonl"),
        definitions = {
            ["stage-6-round"] = {checks = {
                {id = "platinum", field = "game.supported", expected = true},
                {id = "released", field = "trial.released", expected = true},
                {id = "round", field = "trial.round", context = "round"},
                {id = "three_readable_judges", field = "stars.readable", expected = true},
                {id = "initial_empty_star_slots", expected = true, evaluate = function(snapshot, context)
                    return context.round ~= 1 or snapshot.stars.all_empty == true, snapshot.stars.counts
                end},
                {id = "move_category_epoch", field = "move_categories.epoch", context = "epoch"},
                {id = "zero_frame_move_read", field = "move_categories.direct_read_frames", expected = 0},
                {id = "entry_move_capture_binding", expected = true, evaluate = function(snapshot, context)
                    local moves = snapshot.move_categories or {}
                    return type(moves.frame) == "number" and moves.frame <= context.frame
                        and (context.round ~= 1 or moves.frame == context.frame and moves.path == snapshot.image.path),
                        {frame = moves.frame, path = moves.path}
                end},
                {id = "readable_acting_order", field = "acting_order.readable", expected = true},
                {id = "acting_order_frame", field = "acting_order.frame", context = "frame"},
                {id = "acting_order_epoch", field = "acting_order.epoch", context = "epoch"},
                {id = "zero_frame_acting_order_read", field = "acting_order.direct_read_frames", expected = 0},
                {id = "acting_order_capture_binding", expected = true, evaluate = function(snapshot)
                    return snapshot.acting_order.path == snapshot.image.path, snapshot.acting_order.path
                end},
                {id = "readable_between_round_menu", expected = true, evaluate = function(snapshot, context)
                    return context.round == 1 or snapshot.round_readiness.ready == true
                end},
                {id = "eligible_move_plan", field = "plan.selectable", expected = true},
                {id = "capture_frame", field = "image.frame", context = "frame"},
                {id = "capture_epoch", field = "image.epoch", context = "epoch"},
            }, branches = {pass = "perform-round"}},
            ["stage-6-act"] = {checks = {
                {id = "platinum", field = "game.supported", expected = true},
                {id = "four_rounds_complete", field = "trial.completed_rounds", expected = 4},
                {id = "released", field = "trial.released", expected = true},
                {id = "overworld", field = "screen.confirmed", expected = true},
                {id = "validated_party", field = "party.data_validation_passed", expected = true},
                {id = "same_first_party", field = "same_first_party", expected = true},
                {id = "capture_frame", field = "image.frame", context = "frame"},
                {id = "capture_epoch", field = "image.epoch", context = "epoch"},
            }, target = {{id = "target_ribbon", field = "target_acquired", expected = true}},
                branches = {hit = "success", miss = "retry-acting"}},
            stopped = {checks = {{id = "host_stop", expected = "continue",
                evaluate = function(_, context) return false, context.reason end}}, branches = {pass = "finish"}},
        }})
end

local function finish_round()
    assert(stage == "perform-round" and current_plan and api.state.command_done
        and api.state.stop_reason == "combo_complete"
        and api.state.command_start_frame == current_plan.start_frame
        and adapter.frame() == current_plan.end_frame
        and #api.state.combo_history == #current_plan.steps,
        "The authorized move, judge confirmation, and B segment must finish before the next boundary")
    completed_rounds = round_index
    move_cursor, judge_cursor = current_plan.move, current_plan.judge
    current_plan, stage = nil, "ready"
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
        assert(not pending and (stage == "setup" or stage == "retry-acting") and args[1] == "load",
            "Only an acknowledged ribbon miss may reload the accepted Acting source")
        adapter.state_changed()
        api.rebase()
        release()
        round_index, completed_rounds, move_cursor, judge_cursor = 0, 0, 1, 1
        pause_plan, settled, readiness_checks = nil, nil, 0
        render_prepared, render_epoch = false, nil
        render_mode, render_end_frame, render_host_seconds = nil, nil, nil
        current_plan, source_party, entry_moves, result_party, stage = nil, nil, nil, nil, "loaded"
    elseif action == "begin-attempt" then
        assert((stage == "loaded" or (stage == "setup" and config.entry_mode == "stage5_handoff"))
            and adapter.frame() == config.source_frame,
            "Expected the accepted Stage 5 handoff or loaded slot-5 Acting-entry frame")
        render_mode = stage == "loaded" and "load_retry_state" or "stage5_handoff"
        source_party = party_reader.read_first(adapter.frame())
        assert(source_party.read_valid and not source_party.is_egg
            and source_party.ribbon_flags[config.target.ribbon_bit + 1] == false,
            "The accepted Acting source must have an eligible first Pokemon missing the target ribbon")
        config.unavailable_move_slots = config.unavailable_move_slots or source_party.unavailable_move_slots
        release()
        if not engine then initialize() end
        render_prepared, render_epoch = false, adapter.epoch()
        render_end_frame, render_host_seconds, entry_moves = nil, nil, nil
        attempt, stage = attempt + 1, "prepare-render"
        pause_plan, settled, readiness_checks = nil, nil, 0
        observation = observer.snapshot(adapter.frame())
        observation.source_party, observation.source_context = source_party, config.source_context
    elseif action == "render-retry-begin" then
        assert(#args == 0 and stage == "prepare-render" and render_mode == "load_retry_state"
            and not render_prepared and round_index == 0 and adapter.epoch() == render_epoch
            and adapter.frame() == config.source_frame and not pending,
            "The host-timed render window must begin at the paused slot-5 source")
        release()
        stage = "render-retry"
        observation = observer.snapshot(adapter.frame())
    elseif action == "render-prepared" then
        assert(round_index == 0 and not render_prepared
            and adapter.epoch() == render_epoch and api.state.command_done and api.state.error == ""
            and api.state.buttons == "" and api.state.stylus_touch == false,
            "Complete input-free render preparation and confirm native pause before Acting analysis")
        if render_mode == "load_retry_state" then
            local elapsed = tonumber(args[1])
            assert(stage == "render-retry" and #args == 1 and elapsed
                and elapsed >= config.retry_render_host_seconds and elapsed < math.huge
                and adapter.frame() > config.source_frame,
                "Run one full host second after slot-5 loading, then pause with measured frame progress")
            render_host_seconds = elapsed
        else
            assert(render_mode == "stage5_handoff" and stage == "prepare-render" and #args == 0
                and api.state.last_action == "advance" and api.state.stop_reason == "command_complete"
                and api.state.command_start_frame == config.source_frame
                and api.state.command_end_frame == config.source_frame + config.handoff_render_frames
                and adapter.frame() == config.source_frame + config.handoff_render_frames,
                "Advance ten input-free frames at the accepted Stage 5 handoff")
        end
        render_end_frame = adapter.frame()
        render_prepared, stage = true, "ready"
        observation = observer.snapshot(adapter.frame())
    elseif action == "round-readiness" then
        assert(engine and not pending and #args == 1, "Check readiness outside a checkpoint visit")
        if stage == "perform-round" then finish_round() end
        if stage == "round-wait" then
            assert(pause_plan.phase == "retry", "Finish the ready-screen B delay before move selection")
            acting_plan.finish_pause(pause_plan, api.state, adapter.frame(), adapter.epoch())
            pause_plan, stage = nil, "ready"
        end
        local number = tonumber(args[1])
        assert(stage == "ready" and round_index >= 1 and round_index < 4
            and completed_rounds == round_index and number == round_index + 1 and not settled,
            "Check only the next round after its base B segment")
        readiness_checks = readiness_checks + 1
        local frame = adapter.frame()
        local path = config.work .. string.format("/attempt-%02d/round-%d-readiness-%03d.gd", attempt, number, readiness_checks)
        local raw = gui.gdscreenshot()
        assert(idle and adapter.frame() == frame, "A readiness capture must add zero emulator frames")
        write_image(path, raw)
        observation = observer.snapshot(frame)
        observation.image = {path = path, frame = frame, epoch = adapter.epoch(), source = "fresh_zero_frame_framebuffer",
            capture_callback = "paused_lua_menu", direct_read_frames = 0, retained = true}
        observation.readiness = pixels.read_round_readiness(raw, config.unavailable_move_slots, move_cursor)
        observation.readiness.frame, observation.readiness.epoch, observation.readiness.path = frame, adapter.epoch(), path
        pause_plan = acting_plan.round_pause(config, number, frame, adapter.epoch(), observation.readiness.ready)
        observation.pause_plan, stage = pause_plan, "round-wait"
    elseif action == "round-settled" then
        assert(engine and not pending and #args == 1 and stage == "round-wait" and pause_plan
            and pause_plan.phase == "settle" and tonumber(args[1]) == pause_plan.round,
            "Complete the ready-screen B delay before the next round visit")
        acting_plan.finish_pause(pause_plan, api.state, adapter.frame(), adapter.epoch())
        settled, pause_plan, stage = pause_plan, nil, "ready"
        observation = observer.snapshot(adapter.frame())
    elseif action == "inspect" then
        finish_round()
        result_party = party_reader.read_first(adapter.frame())
        observation = {frame = adapter.frame(), party = result_party}
    elseif action == "round-visit" or action == "result-visit" then
        assert(engine and not pending, "Expected one active Stage 6 attempt")
        if stage == "perform-round" then finish_round() end
        assert(stage == "ready", "The prior round has not completed")
        if action == "round-visit" then
            assert(round_index < 4 and tonumber(args[1]) == round_index + 1, "Expected the next Acting round")
            if round_index == 0 then
                assert(api.state.command_done and render_prepared
                    and adapter.epoch() == render_epoch
                    and adapter.frame() == render_end_frame,
                    "Complete render preparation before the first fresh judge capture")
            else
                assert(settled and settled.round == round_index + 1 and settled.end_frame == adapter.frame()
                    and settled.epoch == adapter.epoch(), "Finish the post-readiness B delay before move selection")
            end
            round_index, capture_kind = round_index + 1, "round"
        else
            assert(round_index == 4 and completed_rounds == 4, "Complete all four Acting rounds first")
            capture_kind = "result"
        end
        latest = engine:visit(capture_kind == "round" and "stage-6-round" or "stage-6-act",
            {frame = adapter.frame(), epoch = adapter.epoch(), round = round_index, attempt = attempt})
        pending = latest.status == "awaiting_save" and latest or nil
        current_plan = latest.snapshot and latest.snapshot.plan
        observation, stage = latest.snapshot, "checkpoint"
        settled, readiness_checks = nil, 0
        api.capture()
    elseif action == "complete" then
        assert(pending and #args == 6, "Expected pending saved evidence")
        latest = engine:complete(tonumber(args[2]), {run_id = args[1], visit = tonumber(args[2]),
            frame = tonumber(args[3]), epoch = tonumber(args[4]), path = args[5], emulator_paused = args[6] == "true"})
        pending, observation = nil, latest.snapshot
        stage = latest.proceed and latest.branch or "stopped"
    elseif action == "abort" then
        local reason = args[1] or "Host stopped contest Stage 6"
        release()
        stage, capture_kind = "stopped", "stopped"
        if pending then
            latest, pending = engine:cancel(reason), nil
        elseif engine and not engine:state().halted then
            latest = engine:visit("stopped", {reason = reason})
            pending = latest.status == "awaiting_save" and latest or nil
        end
        observation = latest and latest.snapshot
        api.capture()
    else
        error("Unknown Stage 6 checkpoint command: " .. tostring(action))
    end
    return packet(observation)
end

function observer.attach(bridge)
    api = bridge
    api.state.checkpoint_protocol = 1
    stylus.set({touch = false})
    api.state.stylus_touch = false
    emu.registermenustart(function()
        local parts = mailbox:read_command(command_id)
        if not parts then return end
        command_id = parts[1]
        local args = {}
        for index = 3, #parts do args[#args + 1] = parts[index] end
        assert(not idle, "Reentrant paused checkpoint request")
        idle, api.state.frame = true, adapter.frame()
        local ok, result = pcall(dispatch, parts[2], args)
        if not ok then
            local message = tostring(result)
            if pending then latest, pending = engine:cancel(message), nil end
            release()
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
