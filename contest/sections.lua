-- Callable load/prep/entry/Acting sections; the master owns native callbacks and saves.
local directory = assert(debug.getinfo(1, "S").source:sub(2):gsub("\\", "/"):match("^(.*)/"))
local root = dofile(directory .. "/resource_root.lua")
local bag_reader = dofile(directory .. "/bag_contest.lua").new(memory, emu.gamecode)
local screen_checks = dofile(directory .. "/screen_checks.lua")
local acting_pixels = dofile(directory .. "/acting_pixels.lua")
local acting_plan = dofile(directory .. "/acting_plan.lua")
local platinum = dofile(root .. "/lua/platinum.lua")
local memory_boundary = dofile(directory .. "/memory_boundary.lua")
local M = {}

function M.definitions()
    local function screen_rules(expected)
        return {
            {id = expected, field = "screen.confirmed", expected = true},
            {id = "fresh_framebuffer_binding", field = "screen.fresh_visual_binding", expected = true},
            {id = "review_frame", field = "screen.frame", context = "frame"},
            {id = "review_epoch", field = "screen.epoch", context = "epoch"},
        }
    end
    local readiness = screen_rules("loaded_overworld_screenshot")
    for _, rule in ipairs({
        {id = "validated_first_party", field = "party.read_valid", expected = true},
        {id = "first_not_egg", field = "party.is_egg", expected = false},
        {id = "first_hp", expected = "HP > 0", evaluate = function(snapshot)
            local party = snapshot.party
            return type(party.current_hp) == "number" and party.current_hp > 0,
                {current = party.current_hp, maximum = party.max_hp}
        end},
        {id = "first_sheen", field = "party.sheen", op = "range", expected = {1, 255}},
        {id = "first_moves", field = "party.move_count", op = "range", expected = {2, 4}},
        {id = "readable_text_speed", field = "options.read_valid", expected = true},
        {id = "fast_text_speed", expected = "Fast", evaluate = function(snapshot)
            local options = snapshot.options or {}
            return options.read_valid ~= true or options.text_speed == "Fast", options.text_speed or "Unreadable"
        end},
    }) do readiness[#readiness + 1] = rule end
    return {
        ["stage-1-load"] = {checks = readiness, branches = {pass = "finish"}},
        ["stage-2-prep"] = {checks = {
            {id = "validated_first_party", field = "source_party.read_valid", expected = true},
            {id = "scripted_section_complete", field = "scripted_section_complete", expected = true},
        }, branches = {pass = "finish"}},
        ["stage-3-enter"] = {checks = screen_rules("dress_up_entry_screenshot"), branches = {pass = "finish"}},
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
            branches = {hit = "success", miss = "retry-contest"}},
        stopped = {checks = {{id = "host_stop", expected = "continue",
            evaluate = function(_, context) return false, context.reason end}}, branches = {pass = "finish"}},
    }
end

function M.new(config, runtime)
    local api, adapter, engine = runtime.api, runtime.adapter, runtime.engine
    local stage = config.stage == 6 and "setup" or config.stage == 2 and "inspect" or "inputs"
    local latest, pending, captured, screen, initial, prep, current_plan, source_party, entry_moves, result_memory
    local round_index, completed_rounds, move_cursor, judge_cursor = 0, 0, 1, 1
    local pause_plan, settled
    local readiness_checks = 0
    local render_prepared, render_end_frame = false, nil
    local render_epoch, capture_kind = adapter.epoch(), "source"
    local section = {}

    local function game()
        return platinum.game(emu.gamecode())
    end

    local function trial()
        return {stage = stage, attempt = config.attempt, round = round_index,
            completed_rounds = completed_rounds, move_cursor = move_cursor, judge_cursor = judge_cursor,
            render_preparation_frames = render_prepared and render_end_frame - config.source_frame or 0,
            render_preparation_mode = config.entry_mode,
            readiness_checks = readiness_checks, readiness_settle_frames = settled and settled.frames or 0,
            released = api.state.command_done and api.state.buttons == "" and not api.state.stylus_touch}
    end

    local function read_memory(include_readiness, startup_party, scope)
        return memory_boundary.read(api.state, adapter, runtime.is_paused(), scope, function(frame)
            return bag_reader.snapshot(frame, include_readiness, startup_party)
        end)
    end

    function section.observation(frame)
        return {frame = frame, game = game(), trial = trial(), target = config.target,
            source_context = config.source_context, party_context = config.party_context}
    end

    function section.snapshot(frame)
        assert(runtime.is_paused() and adapter.frame() == frame, "Section snapshots require a fresh zero-frame native pause")
        local result = section.observation(frame)
        if capture_kind == "stopped" then
            result.image = runtime.image(config.work .. "/failure.gd", frame)
            return result
        end
        if config.stage == 2 then
            result.party, result.bag, result.memory_read = initial.party, initial.bag, initial.memory_read
            result.access = {source = "accepted_x_menu_memory_before_preparation",
                direct_read_frames = 0, injects_inputs = false, live_memory_read = false}
            result.source_party = initial.party
            result.target, result.bag_context = prep.target, prep.bag_context
            result.scripted_section_complete = api.state.command_done and api.state.buttons == ""
                and frame == initial.frame + prep.emulator_frames
                and (#prep.inputs == 0 or api.state.stop_reason == "combo_complete")
        elseif config.stage == 6 then
            local path = config.work .. (capture_kind == "round" and string.format("/round-%d.gd", round_index)
                or capture_kind == "result" and "/result.gd" or "/failure.gd")
            local raw
            result.image, raw = runtime.image(path, frame)
            if capture_kind == "round" then
                result.stars = acting_pixels.read_stars(raw)
                result.acting_order = acting_pixels.read_acting_order(raw)
                result.acting_order.frame, result.acting_order.epoch, result.acting_order.path =
                    frame, result.image.epoch, result.image.path
                if round_index == 1 and not entry_moves then
                    entry_moves = acting_pixels.read_move_categories(raw, config.unavailable_move_slots)
                    entry_moves.frame, entry_moves.epoch, entry_moves.path = frame, result.image.epoch, result.image.path
                    for _, warning in ipairs(entry_moves.warnings) do print(warning) end
                end
                result.move_categories = entry_moves
                if round_index > 1 then
                    result.round_readiness = acting_pixels.read_round_readiness(raw, config.unavailable_move_slots,
                        move_cursor, result.stars, result.acting_order)
                end
                if result.stars.readable and result.acting_order.readable and entry_moves then
                    result.plan = acting_plan.round(config, round_index, move_cursor, judge_cursor, frame,
                        result.stars, entry_moves, result.acting_order)
                    for _, warning in ipairs(result.plan.warnings or {}) do print(warning) end
                end
            elseif capture_kind == "result" then
                result.screen = acting_pixels.match_points(raw, config.overworld.points)
                result.screen.expected, result.screen.reference = "loaded_overworld", config.overworld.reference
                result.party, result.bag, result.access = result_memory.party, result_memory.bag, result_memory.access
                result.memory_read = result_memory.memory_read
                result.same_first_party = result.party.species_id == source_party.species_id and not result.party.is_egg
                result.target_acquired = result.party.ribbon_flags[config.target.ribbon_bit + 1]
            end
        else
            if screen then
                screen.fresh_visual_binding = captured.frame == frame and captured.epoch == adapter.epoch()
                    and captured.raw == gui.gdscreenshot()
                if screen.source == "local_reference_pixels" then
                    screen.pixel_check = captured.check
                    screen.confirmed = screen.confirmed and screen.fresh_visual_binding and screen.pixel_check.confirmed
                end
            end
            result.screen = screen or {source = "unreviewed", memory_proven = false}
            if config.stage == 1 and screen and screen.confirmed then
                local readiness = initial
                result.party, result.bag, result.access = readiness.party, readiness.bag, readiness.access
                result.options, result.accessories = readiness.options, readiness.accessories
                result.memory_read = readiness.memory_read
                result.image = captured.image
            end
        end
        return result
    end

    local function finish_round()
        assert(stage == "perform-round" and current_plan and api.state.command_done
            and api.state.stop_reason == "combo_complete" and api.state.command_start_frame == current_plan.start_frame
            and adapter.frame() == current_plan.end_frame and #api.state.combo_history == #current_plan.steps,
            "Complete the authorized move, judge confirmation, and B segment before the next boundary")
        completed_rounds = round_index
        move_cursor, judge_cursor = current_plan.move, current_plan.judge
        current_plan, stage = nil, "ready"
    end

    function section.authorize(action)
        if pending or not api.state.command_done then return false end
        if action == "combo" then
            return stage == "inputs" or stage == "prepare" or stage == "perform-round"
                or stage == "round-wait" and pause_plan and adapter.frame() == pause_plan.start_frame
                    and adapter.epoch() == pause_plan.epoch
                or config.stage == 6 and stage == "ready" and completed_rounds == 4
        end
        return action == "advance" and config.stage == 6 and stage == "prepare-render"
            and config.entry_mode == "stage5_handoff"
    end

    function section.checkpoint(action, args)
        local observation
        if action == "inspect" and config.stage == 1 then
            assert(not initial, "Read startup memory once inside the X menu")
            initial = read_memory(true, nil, "startup_readiness")
            observation = initial
        elseif action == "inspect" and config.stage == 6 then
            assert(not result_memory, "Read contest-finish memory once inside the X menu")
            finish_round()
            result_memory = read_memory(false, config.startup_party, "contest_finish_items_and_ribbons")
            observation = result_memory
        elseif action == "inspect" then
            assert(config.stage == 2 and stage == "inspect", "Inspect the current Stage 2 source once")
            initial = assert(config.accepted_readiness, "Preparation requires accepted X-protected source data")
            assert(initial.frame == adapter.frame() and initial.image.epoch == adapter.epoch(),
                "The shared readiness snapshot no longer matches the paused game")
            initial.epoch = adapter.epoch()
            observation = initial
        elseif action == "init-prep" then
            assert(config.stage == 2 and stage == "inspect" and #args == 1 and initial
                and initial.frame == adapter.frame() and initial.epoch == adapter.epoch(),
                "The prep plan requires its fresh paused source")
            prep = assert(loadfile(args[1]))()
            assert(prep.source_species_id == initial.party.species_id and prep.expected_item_id >= 0
                and prep.expected_item_id <= 536, "The prep plan differs from its first-party source")
            config.target, stage = prep.target, "prepare"
        elseif action == "capture" then
            assert((config.stage == 1 or config.stage == 3) and stage == "inputs"
                and api.state.command_done and api.state.stop_reason == "combo_complete"
                and adapter.frame() == config.source_frame + config.total_emulator_frames,
                "Complete the counted section before reviewing its endpoint")
            if config.stage == 1 then
                memory_boundary.closed(initial, api.state, adapter.frame(), adapter.epoch(), runtime.is_paused())
            end
            local image, raw = runtime.image(config.work .. "/stage" .. config.stage .. ".gd", adapter.frame())
            captured = {frame = image.frame, epoch = image.epoch, raw = raw, image = image,
                check = screen_checks.read(raw, config.stage == 1 and "loaded_overworld" or "dress_up_entry",
                    config.target and config.target.rank)}
            observation = section.observation(adapter.frame())
            observation.automatic_check = captured.check
            stage = "review"
        elseif action == "visit" then
            local context = {frame = adapter.frame(), epoch = adapter.epoch()}
            if config.stage == 2 then
                assert(stage == "prepare" and not pending, "Complete the Stage 2 section once")
                context.source_species_id, context.owned_ribbon_bits = prep.source_species_id, initial.party.owned_ribbon_bits
                context.expected_item_id = prep.expected_item_id
            else
                assert(stage == "review" and not pending and #args == 6, "Expected the current endpoint review")
                assert((args[1] == "pass" or args[1] == "fail")
                    and args[2] == "local_reference_pixels"
                    and args[3] ~= "" and args[4] ~= "", "Supply the actual verdict, image, and visual note")
                assert(tonumber(args[5]) == captured.frame and tonumber(args[6]) == captured.epoch
                    and adapter.frame() == captured.frame and adapter.epoch() == captured.epoch,
                    "The reviewed screenshot belongs to a different paused boundary")
                screen = {source = args[2], path = args[3], note = args[4], confirmed = args[1] == "pass",
                    expected = config.stage == 1 and "loaded_overworld" or "dress_up_entry", memory_proven = false,
                    frame = captured.frame, epoch = captured.epoch}
            end
            local name = config.stage == 1 and "stage-1-load" or config.stage == 2 and "stage-2-prep" or "stage-3-enter"
            latest = engine:visit(name, context)
            pending = (latest.status == "awaiting_save" or latest.status == "awaiting_ack") and latest or nil
            observation, stage = latest.snapshot, "checkpoint"
        elseif action == "begin-attempt" then
            assert(config.stage == 6 and stage == "setup" and config.entry_mode == "stage5_handoff"
                and type(config.handoff_render_frames) == "number" and config.handoff_render_frames >= 1
                and config.handoff_render_frames == math.floor(config.handoff_render_frames) and adapter.frame() == config.source_frame,
                "Begin Acting at the current accepted Stage 5 handoff")
            source_party = memory_boundary.party(config.party_context, emu.gamecode(), config.target)
            assert(config.startup_party, "Acting requires the accepted startup party facts")
            config.unavailable_move_slots = config.unavailable_move_slots or config.startup_party.unavailable_move_slots
            api.finish()
            stage = "prepare-render"
            observation = section.observation(adapter.frame())
            observation.source_party = source_party
        elseif action == "render-prepared" then
            assert(config.stage == 6 and round_index == 0 and not render_prepared and adapter.epoch() == render_epoch
                and api.state.command_done and api.state.error == "" and api.state.buttons == "" and not api.state.stylus_touch,
                "Complete input-free rendering and confirm native pause before analysis")
            assert(stage == "prepare-render" and config.entry_mode == "stage5_handoff" and #args == 0
                and api.state.last_action == "advance" and api.state.command_start_frame == config.source_frame
                and api.state.command_end_frame == config.source_frame + config.handoff_render_frames
                and adapter.frame() == config.source_frame + config.handoff_render_frames,
                "A direct Acting handoff needs its configured counted input-free frames")
            render_prepared, render_end_frame, stage = true, adapter.frame(), "ready"
            observation = section.observation(adapter.frame())
        elseif action == "round-readiness" then
            assert(config.stage == 6 and not pending and #args == 1, "Check readiness outside a checkpoint visit")
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
            local path = config.work .. string.format("/round-%d-readiness-%03d.gd", number, readiness_checks)
            local raw
            observation = section.observation(frame)
            observation.image, raw = runtime.image(path, frame)
            observation.readiness = acting_pixels.read_round_readiness(raw, config.unavailable_move_slots, move_cursor)
            observation.readiness.frame, observation.readiness.epoch, observation.readiness.path =
                frame, adapter.epoch(), path
            pause_plan = acting_plan.round_pause(config, number, frame, adapter.epoch(), observation.readiness.ready)
            observation.pause_plan, stage = pause_plan, "round-wait"
        elseif action == "round-settled" then
            assert(config.stage == 6 and not pending and #args == 1 and stage == "round-wait"
                and pause_plan and pause_plan.phase == "settle" and tonumber(args[1]) == pause_plan.round,
                "Complete the ready-screen B delay before the next round visit")
            acting_plan.finish_pause(pause_plan, api.state, adapter.frame(), adapter.epoch())
            settled, pause_plan, stage = pause_plan, nil, "ready"
            observation = section.observation(adapter.frame())
        elseif action == "round-visit" or action == "result-visit" then
            assert(config.stage == 6 and not pending, "Expected one active Acting visit")
            if stage == "perform-round" then finish_round() end
            assert(stage == "ready", "Complete the previous Acting round first")
            if action == "round-visit" then
                assert(round_index < 4 and tonumber(args[1]) == round_index + 1, "Expected the next Acting round")
                if round_index == 0 then
                    assert(render_prepared and adapter.epoch() == render_epoch and adapter.frame() == render_end_frame,
                        "Render preparation must precede the first fresh judge capture")
                else
                    assert(settled and settled.round == round_index + 1 and settled.end_frame == adapter.frame()
                        and settled.epoch == adapter.epoch(), "Finish the post-readiness B delay before move selection")
                end
                round_index, capture_kind = round_index + 1, "round"
            else
                assert(round_index == 4 and completed_rounds == 4, "Complete all four Acting rounds first")
                memory_boundary.closed(result_memory, api.state, adapter.frame(), adapter.epoch(), runtime.is_paused())
                capture_kind = "result"
            end
            latest = engine:visit(capture_kind == "round" and "stage-6-round" or "stage-6-act",
                {frame = adapter.frame(), epoch = adapter.epoch(), round = round_index, attempt = config.attempt})
            pending = (latest.status == "awaiting_save" or latest.status == "awaiting_ack") and latest or nil
            current_plan, observation, stage = latest.snapshot and latest.snapshot.plan, latest.snapshot, "checkpoint"
            settled, readiness_checks = nil, 0
        elseif action == "complete" then
            assert(pending and (#args == 6 or #args == 7), "Expected pending checkpoint evidence")
            latest = engine:complete(tonumber(args[2]), {run_id = args[1], visit = tonumber(args[2]),
                frame = tonumber(args[3]), epoch = tonumber(args[4]), path = args[5], emulator_paused = args[6] == "true",
                saved = args[7] ~= "false"})
            pending, observation = nil, latest.snapshot
            stage = latest.proceed and latest.branch or "stopped"
        elseif action == "abort" then
            local reason = args[1] or "Host stopped the contest section"
            api.finish(reason)
            stage, capture_kind = "stopped", "stopped"
            if pending then
                latest, pending = engine:cancel(reason), nil
            elseif not engine:state().halted then
                latest = engine:visit("stopped", {reason = reason})
                pending = (latest.status == "awaiting_save" or latest.status == "awaiting_ack") and latest or nil
            end
            observation = latest and latest.snapshot
        else
            error("Unknown callable contest section command: " .. tostring(action))
        end
        local result = {frame = adapter.frame(), epoch = adapter.epoch(), checkpoint = latest,
            observation = observation, next_section = stage}
        if not pending then result.progress = engine:state() end
        return result
    end

    section.finish = api.finish
    return section
end

return M
