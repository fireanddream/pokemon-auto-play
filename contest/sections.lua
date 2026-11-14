-- Callable load/prep/entry/Acting sections; the master owns native callbacks and saves.
local directory = assert(debug.getinfo(1, "S").source:sub(2):gsub("\\", "/"):match("^(.*)/"))
local bag_reader = dofile(directory .. "/bag_contest.lua").new(memory, emu.gamecode)
local party_reader = dofile(directory .. "/party_contest.lua").new(memory, emu.gamecode)
local screen_checks = dofile(directory .. "/screen_checks.lua")
local acting_pixels = dofile(directory .. "/acting_pixels.lua")
local acting_plan = dofile(directory .. "/acting_plan.lua")
local M = {}

function M.definitions(retry_mode)
    local function screen_rules(expected)
        return {
            {id = expected, field = "screen.confirmed", expected = true},
            {id = "fresh_framebuffer_binding", field = "screen.fresh_visual_binding", expected = true},
            {id = "review_frame", field = "screen.frame", context = "frame"},
            {id = "review_epoch", field = "screen.epoch", context = "epoch"},
        }
    end
    return {
        ["stage-1-load"] = {checks = screen_rules("loaded_overworld_screenshot"), branches = {pass = "finish"}},
        ["stage-2-prep"] = {checks = {
            {id = "validated_first_party", field = "party.read_valid", expected = true},
            {id = "same_first_party", field = "party.species_id", context = "source_species_id"},
            {id = "same_ribbon_flags", field = "party.owned_ribbon_bits", context = "owned_ribbon_bits"},
            {id = "prepared_held_item", field = "party.held_item_id", context = "expected_item_id"},
            {id = "scripted_section_complete", field = "scripted_section_complete", expected = true},
        }, branches = {pass = "finish"}},
        ["stage-3-enter"] = {checks = screen_rules("dress_up_entry_screenshot"), branches = {pass = "finish"}},
        ["stage-6-round"] = {checks = {
            {id = "cpuj", field = "game.code", expected = "CPUJ"},
            {id = "released", field = "trial.released", expected = true},
            {id = "round", field = "trial.round", context = "round"},
            {id = "three_readable_judges", field = "stars.readable", expected = true},
            {id = "initial_empty_star_slots", expected = true, evaluate = function(snapshot, context)
                return context.round ~= 1 or snapshot.stars.all_empty == true, snapshot.stars.counts
            end},
            {id = "four_readable_move_categories", field = "move_categories.readable", expected = true},
            {id = "move_category_epoch", field = "move_categories.epoch", context = "epoch"},
            {id = "zero_frame_move_read", field = "move_categories.direct_read_frames", expected = 0},
            {id = "entry_move_capture_binding", expected = true, evaluate = function(snapshot, context)
                local moves = snapshot.move_categories or {}
                return type(moves.frame) == "number" and moves.frame <= context.frame
                    and (context.round ~= 1 or moves.frame == context.frame and moves.path == snapshot.image.path),
                    {frame = moves.frame, path = moves.path}
            end},
            {id = "capture_frame", field = "image.frame", context = "frame"},
            {id = "capture_epoch", field = "image.epoch", context = "epoch"},
        }, branches = {pass = "perform-round"}},
        ["stage-6-act"] = {checks = {
            {id = "cpuj", field = "game.code", expected = "CPUJ"},
            {id = "four_rounds_complete", field = "trial.completed_rounds", expected = 4},
            {id = "released", field = "trial.released", expected = true},
            {id = "overworld", field = "screen.confirmed", expected = true},
            {id = "validated_party", field = "party.data_validation_passed", expected = true},
            {id = "same_first_party", field = "same_first_party", expected = true},
            {id = "capture_frame", field = "image.frame", context = "frame"},
            {id = "capture_epoch", field = "image.epoch", context = "epoch"},
        }, target = {{id = "target_ribbon", field = "target_acquired", expected = true}},
            branches = {hit = "success", miss = retry_mode == 1 and "retry-contest" or "retry-acting"}},
        stopped = {checks = {{id = "host_stop", expected = "continue",
            evaluate = function(_, context) return false, context.reason end}}, branches = {pass = "finish"}},
    }
end

function M.new(config, runtime)
    local api, adapter, engine = runtime.api, runtime.adapter, runtime.engine
    local stage = config.stage == 6 and "setup" or config.stage == 2 and "inspect" or "inputs"
    local latest, pending, captured, screen, initial, prep, current_plan, source_party, entry_moves
    local round_index, completed_rounds, move_cursor, judge_cursor = 0, 0, 1, 1
    local render_prepared, render_end_frame, render_host_seconds = false, nil, nil
    local render_epoch, capture_kind = adapter.epoch(), "source"
    local section = {}

    local function game()
        local code = emu.gamecode()
        return {code = code, supported = code == "CPUJ", identity_source = "emu.gamecode"}
    end

    local function trial()
        return {stage = stage, attempt = config.attempt, round = round_index,
            completed_rounds = completed_rounds, move_cursor = move_cursor, judge_cursor = judge_cursor,
            render_preparation_frames = render_prepared and render_end_frame - config.source_frame or 0,
            render_preparation_mode = config.entry_mode, render_preparation_host_seconds = render_host_seconds,
            released = api.state.command_done and api.state.buttons == "" and not api.state.stylus_touch}
    end

    function section.observation(frame)
        return {frame = frame, game = game(), trial = trial(), target = config.target,
            source_context = config.source_context}
    end

    function section.snapshot(frame)
        assert(runtime.is_paused() and adapter.frame() == frame, "Section snapshots require a fresh zero-frame native pause")
        local result = section.observation(frame)
        if capture_kind == "stopped" then
            result.image = runtime.image(config.work .. "/failure.gd", frame)
            return result
        end
        if config.stage == 2 then
            result = bag_reader.snapshot(frame)
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
                if round_index == 1 and not entry_moves then
                    entry_moves = acting_pixels.read_move_categories(raw)
                    entry_moves.frame, entry_moves.epoch, entry_moves.path = frame, result.image.epoch, result.image.path
                end
                result.move_categories = entry_moves
                if result.stars.readable and entry_moves and entry_moves.readable then
                    result.plan = acting_plan.round(config, round_index, move_cursor, judge_cursor, frame,
                        result.stars, entry_moves.categories)
                end
            elseif capture_kind == "result" then
                result.screen = acting_pixels.match_points(raw, config.overworld.points)
                result.screen.expected, result.screen.reference = "loaded_overworld", config.overworld.reference
                result.party = party_reader.read_first(frame)
                result.same_first_party = result.party.species_id == source_party.species_id and not result.party.is_egg
                result.target_acquired = result.party.ribbon_flags[config.target.ribbon_bit + 1]
            end
        else
            if screen then
                screen.fresh_visual_binding = captured.frame == frame and captured.epoch == adapter.epoch()
                    and captured.raw == gui.gdscreenshot()
                if screen.source == "local_reference_pixels" then
                    screen.pixel_check = screen_checks.read(captured.raw, screen.expected)
                    screen.confirmed = screen.confirmed and screen.fresh_visual_binding and screen.pixel_check.confirmed
                end
            end
            result.screen = screen or {source = "unreviewed", memory_proven = false}
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
        end
        return action == "advance" and config.stage == 6 and stage == "prepare-render"
            and config.entry_mode == "stage5_handoff"
    end

    function section.checkpoint(action, args)
        local observation
        if action == "inspect" then
            assert(config.stage == 2 and stage == "inspect", "Inspect the current Stage 2 source once")
            initial = bag_reader.snapshot(adapter.frame())
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
            local image, raw = runtime.image(config.work .. "/stage" .. config.stage .. ".gd", adapter.frame())
            captured = {frame = image.frame, epoch = image.epoch, raw = raw}
            observation = section.observation(adapter.frame())
            observation.automatic_check = screen_checks.read(raw, config.stage == 1 and "loaded_overworld" or "dress_up_entry")
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
                    and (args[2] == "visual_review" or args[2] == "local_reference_pixels")
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
            assert(config.stage == 6 and stage == "setup" and adapter.frame() == config.source_frame,
                "Begin Acting at the accepted Stage 5 handoff or restored retry-state frame")
            source_party = party_reader.read_first(adapter.frame())
            assert(source_party.read_valid and not source_party.is_egg
                and source_party.ribbon_flags[config.target.ribbon_bit + 1] == false,
                "The Acting source must have an eligible first Pokemon missing the target ribbon")
            api.finish()
            stage = "prepare-render"
            observation = section.observation(adapter.frame())
            observation.source_party = source_party
        elseif action == "render-retry-begin" then
            assert(config.stage == 6 and stage == "prepare-render" and config.entry_mode == "load_retry_state"
                and not render_prepared and adapter.frame() == config.source_frame and adapter.epoch() == render_epoch,
                "Start the input-free host-timed window at the restored Acting source")
            api.finish()
            stage = "render-retry"
        elseif action == "render-prepared" then
            assert(config.stage == 6 and round_index == 0 and not render_prepared and adapter.epoch() == render_epoch
                and api.state.command_done and api.state.error == "" and api.state.buttons == "" and not api.state.stylus_touch,
                "Complete input-free rendering and confirm native pause before analysis")
            if config.entry_mode == "load_retry_state" then
                local seconds = tonumber(args[1])
                assert(stage == "render-retry" and #args == 1 and seconds and seconds >= config.retry_render_host_seconds
                    and seconds < math.huge and adapter.frame() > config.source_frame,
                    "An Acting retry needs one full host second and measured frame progress")
                render_host_seconds = seconds
            else
                assert(stage == "prepare-render" and #args == 0 and api.state.last_action == "advance"
                    and api.state.command_start_frame == config.source_frame
                    and api.state.command_end_frame == config.source_frame + config.handoff_render_frames
                    and adapter.frame() == config.source_frame + config.handoff_render_frames,
                    "A direct Acting handoff needs the agreed ten counted input-free frames")
            end
            render_prepared, render_end_frame, stage = true, adapter.frame(), "ready"
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
                end
                round_index, capture_kind = round_index + 1, "round"
            else
                assert(round_index == 4 and completed_rounds == 4, "Complete all four Acting rounds first")
                capture_kind = "result"
            end
            latest = engine:visit(capture_kind == "round" and "stage-6-round" or "stage-6-act",
                {frame = adapter.frame(), epoch = adapter.epoch(), round = round_index, attempt = config.attempt})
            pending = (latest.status == "awaiting_save" or latest.status == "awaiting_ack") and latest or nil
            current_plan, observation, stage = latest.snapshot and latest.snapshot.plan, latest.snapshot, "checkpoint"
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
