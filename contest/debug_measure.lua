-- Debug enrollment/baseline adapter; optional measurement reads use framebuffer pixels.
local directory = assert(debug.getinfo(1, "S").source:sub(2):gsub("\\", "/"):match("^(.*)/"))
local root = dofile(directory .. "/resource_root.lua")
local bridge_io = dofile(root .. "/lua/bridge_io.lua")
local combos = dofile(root .. "/lua/input_sequences.lua")
local pixels = dofile(directory .. "/dance_pixels.lua")
local screens = dofile(directory .. "/screen_checks.lua")
local json = dofile(root .. "/lua/json.lua")
local M = {}

local function fresh_rules()
    return {
        {id = "released_inputs", field = "released", expected = true},
        {id = "fresh_framebuffer", field = "fresh_visual_binding", expected = true},
        {id = "capture_frame", field = "image.frame", context = "frame"},
        {id = "capture_epoch", field = "image.epoch", context = "epoch"},
    }
end

function M.definitions(base, runtime, inputs_mode)
    local definitions = {["stage-1-load"] = base["stage-1-load"], stopped = base.stopped}
    definitions["stage-1-load"].save_state = true
    definitions.stopped.save_state = false
    for _, name in ipairs({"stage-3-enter", "stage-4-dress", "dance-measurement", "dance-measurement-finish"}) do
        definitions[name] = {checks = fresh_rules(), save_state = false, branches = {pass = "finish"}}
    end
    local entry = definitions["stage-3-enter"].checks
    entry[#entry + 1] = {id = "dress_up_pixels", field = "screen.confirmed", expected = true}
    for _, name in ipairs({"stage-4-dress", "dance-measurement-finish"}) do
        local rules = definitions[name].checks
        rules[#rules + 1] = {id = "readable_demo", field = "visual.phase", expected = "demo"}
        rules[#rules + 1] = {id = "readable_position", field = "visual.readable", expected = true}
        rules[#rules + 1] = name == "stage-4-dress"
            and {id = "known_sweep", field = "visual.sweep", expected = 1}
            or {id = "known_sweep", field = "visual.sweep", context = "finish_sweep"}
        rules[#rules + 1] = {id = "visual_sequence", field = "visual.sequence_status", expected = "confirmed"}
    end
    if inputs_mode then
        local responses = assert(loadfile(directory .. "/rank_dance.lua"))({stage = 5,
            feedback_policy = "record_all_buttons", debug_inputs = true}, runtime)
        for name, definition in pairs(responses.definitions()) do
            if name ~= "stopped" then
                definition.save_state = false
                definitions[name] = definition
            end
        end
    end
    return definitions
end

local function identity_matches(context, record)
    if not context or not record then return false end
    for _, key in ipairs({"checkpoint", "run_id", "visit", "frame", "epoch"}) do
        if context[key] ~= record[key] then return false end
    end
    return true
end

local function same_target(a, b)
    if not a or not b then return false end
    for _, key in ipairs({"category", "rank", "party_slot", "category_down_presses", "rank_down_presses", "debug_loop"}) do
        if a[key] ~= b[key] then return false end
    end
    return true
end

local position

function M.run(runtime, inputs_mode)
    local targets, baseline, restored, armed, index = nil, nil, false, false, 1
    local measurement_dances = 2
    local run = {}
    function run.begin(config, latest)
        local number, adapter = config.stage, runtime.adapter
        assert(not armed, "Finish the armed baseline reload before starting a section")
        if number == 5 and config.resume_history then
            assert(not targets and not latest and #config.resume_history > 0,
                "Adopt retained entry evidence only at the start of a paused single-contest continuation")
            local history, previous_frame, last_raw = {}, nil, nil
            for _, sample in ipairs(config.resume_history) do
                assert(sample.frame <= config.source_frame and (not previous_frame
                    or sample.frame > previous_frame and sample.frame - previous_frame <= config.capture_wait_frames),
                    "Retained entry frames must be ordered and cover the current pause without a long gap")
                local file = assert(io.open(sample.image.path, "rb"))
                local raw = file:read("*a"); file:close()
                sample.visual = position(raw, config.pointer_templates)
                if #history > 0 or sample.visual.readable and sample.visual.phase == "demo" then
                    if #history == 0 then
                        sample.visual.sweep, sample.visual.pokemon, sample.visual.dance = 1, 1, 1
                    end
                    history[#history + 1] = sample
                end
                previous_frame, last_raw = sample.frame, raw
            end
            assert(#history > 0 and previous_frame == adapter.frame() and last_raw == gui.gdscreenshot(),
                "Resume from a retained first demo through the exact current paused framebuffer")
            config.resume_history, config.entry_observation = history, history[1]
            targets = {config.target}
            return M.new(config, runtime)
        end
        if number == 1 then
            assert(not targets and not latest, "Create the measurement baseline once after fresh loading")
            measurement_dances = config.measurement_dances or 2
            assert(measurement_dances == 1 or measurement_dances == 2, "Measure one or two dances")
            targets = config.measurement_targets
            assert(type(targets) == "table" and #targets > 0, "Supply the ordered debug targets")
            local categories, ranks, previous = {"Cool", "Beauty", "Cute", "Smart", "Tough"}, {"Normal", "Super", "Hyper", "Master"}, -1
            local loop_size = 0
            for _, target in ipairs(targets) do
                if (target.debug_loop or 1) ~= 1 then break end
                loop_size = loop_size + 1
            end
            assert(loop_size > 0 and #targets % loop_size == 0, "Supply complete repetitions of the selected debug targets")
            for index, target in ipairs(targets) do
                local position = (index - 1) % loop_size + 1
                if position == 1 then previous = -1 end
                local category, rank = target.category_down_presses, target.rank_down_presses
                assert(type(category) == "number" and type(rank) == "number" and category % 1 == 0 and rank % 1 == 0
                    and category >= 0 and category < 5 and rank >= 0 and rank < 4
                    and categories[category + 1] == target.category and ranks[rank + 1] == target.rank
                    and target.party_slot == 1 and category * 4 + rank > previous
                    and (target.debug_loop or 1) == math.floor((index - 1) / loop_size) + 1
                    and category == targets[position].category_down_presses and rank == targets[position].rank_down_presses,
                    "Debug targets must be unique and ordered within each declared repeated loop")
                previous = category * 4 + rank
            end
            return nil
        end
        local source = restored and baseline or latest
        assert(source and source.status == "complete" and source.proceed and source.frame == adapter.frame()
            and (restored or source.epoch == adapter.epoch()) and identity_matches(config.source_context, source),
            "Continue from the acknowledged current boundary or this run's restored baseline")
        local expected = ({["stage-1-load"] = 3, ["stage-3-enter"] = 4, ["stage-4-dress"] = 5})[source.checkpoint]
        assert(number == expected and same_target(config.target, targets[index]), "Enter the next untested measurement target")
        if number == 3 then
            baseline = baseline or source
        else
            assert(same_target(config.target, source.snapshot.target), "The measurement target changed during its contest")
        end
        if number == 5 then
            config.entry_observation = source.snapshot
            if not inputs_mode then config.measurement_dances = measurement_dances end
        end
        restored = false
        if number == 5 and inputs_mode then
            assert(config.debug_inputs and config.feedback_policy == "record_all_buttons"
                and config.observation_policy == "scheduled"
                and config.midpoint_timing.entry_pointer_x == source.snapshot.visual.x,
                "Debug responses must use fixed metrics and the accepted first-demo pointer")
            return assert(loadfile(directory .. "/rank_dance.lua"))(config, runtime)
        end
        return M.new(config, runtime)
    end
    function run.arm(args, latest)
        local trial = latest and latest.snapshot and latest.snapshot.trial
        local input_finish = inputs_mode and latest and latest.checkpoint == "rank-dance-finish"
            and trial and trial.dance == 6 and trial.total_inputs + trial.total_skipped_inputs == trial.required_response_inputs
            and trial.total_judgment_count == trial.total_inputs and trial.debug_finish_bound
        local measurement_finish = not inputs_mode and latest and latest.checkpoint == "dance-measurement-finish"
        assert(not armed and not restored and baseline and #args == 1 and tonumber(args[1]) == baseline.visit
            and (input_finish or measurement_finish) and latest.status == "complete"
            and latest.proceed and latest.frame == runtime.adapter.frame() and latest.epoch == runtime.adapter.epoch()
            and same_target(latest.snapshot.target, targets[index]) and (inputs_mode or measurement_dances == 1 or index < #targets),
            "Reload after an acknowledged final response, or a final measurement capture with another target pending")
        armed = true
        return {frame = runtime.adapter.frame(), epoch = runtime.adapter.epoch(), source_visit = baseline.visit}
    end
    function run.loaded(args)
        assert(args[1] == "load" and armed and tonumber(args[2]) == baseline.visit
            and runtime.adapter.frame() == baseline.frame, "The measurement load must restore its armed post-load baseline")
        armed, restored, index = false, true, index + 1
    end
    return run
end

local function pointer_position(raw, bounds, templates)
    if not templates then return nil end
    local function color(x, y)
        local at = 13 + (y * 256 + x) * 4
        return {raw:byte(at, at + 2)}
    end
    local function matches(x, y, sprite)
        local numerator, denominator = 0, 0
        for _, point in ipairs(sprite.probes) do
            local actual = color(x + point[1], y + point[2])
            for channel = 1, 3 do
                local dark = 255 - point[3][channel]
                numerator, denominator = numerator + dark * (255 - actual[channel]), denominator + dark * dark
            end
        end
        local scale = denominator > 0 and numerator / denominator or 1
        if scale < 0.35 or scale > 1.02 then return false end
        local function near(point)
            local actual = color(x + point[1], y + point[2])
            for channel = 1, 3 do
                if math.abs(actual[channel] - (255 - (255 - point[3][channel]) * scale)) > 8 then return false end
            end
            return true
        end
        for _, point in ipairs(sprite.probes) do if not near(point) then return false end end
        local count = 0
        for _, point in ipairs(sprite.points) do if near(point) then count = count + 1 end end
        return count / #sprite.points >= sprite.minimum_match_fraction
    end
    local found
    for y = 116, 153 do
        for x = bounds[1], bounds[2] do
            local actual = color(x, y)
            local r, g, b = actual[1], actual[2], actual[3]
            local candidates = {}
            if y < 146 and r >= 240 and r - g >= 35 and b - g >= 30 then
                candidates[#candidates + 1] = {templates.full, y}
            end
            if y >= 124 and r >= 200 and r - g >= 60 and b - g >= 14 and r - b >= 40 then
                candidates[#candidates + 1] = {templates.lower, y - 8}
            end
            for _, candidate in ipairs(candidates) do
                for _, sprite in ipairs(candidate[1]) do
                    if matches(x - 7, candidate[2], sprite) then
                        if found and found ~= x then return nil end
                        found = x
                    end
                end
            end
        end
    end
    return found
end

position = function(raw, templates)
    local bar = pixels.bar(raw)
    local bounds = bar.found and bar.first_x == 8 and bar.position_bounds
    local grid_end
    if bounds and bounds[1] < bounds[2] then
        for x = bounds[1] + 1, bounds[2] do
            local mark = (x % 30 == 8 or x % 30 == 9) and {255, 255, 223}
                or x % 10 == 8 and {182, 190, 190}
            if mark then
                local matched = true
                for y = 155, 158 do
                    local at = 13 + (y * 256 + x) * 4
                    local r, g, b = raw:byte(at, at + 2)
                    if r ~= mark[1] or g ~= mark[2] or b ~= mark[3] then matched = false; break end
                end
                if matched then bounds, grid_end = {bounds[1], x - 1}, x; break end
            end
        end
    end
    local pointer = bounds and bounds[1] < bounds[2] and pointer_position(raw, bounds, templates)
    if pointer then bounds = {pointer, pointer} end
    local exact = bounds and bounds[1] == bounds[2] or false
    local phase = bounds and (bounds[2] < 128 and "demo" or bounds[1] >= 128 and "response") or "unknown"
    return {readable = exact, x = exact and bounds[1] or nil, position_bounds = bounds,
        phase = phase or "unknown", source = pointer and "four_row_strip_and_pink_pointer_sprite"
            or grid_end and "four_row_fill_empty_and_grid_profiles" or "four_row_fill_and_empty_profiles",
        first_empty_grid_x = grid_end,
        sequence_status = exact and "confirmed" or "unreadable", reason = not exact and "unreadable_or_ambiguous_strip" or nil}, bar.found
end

local function tracker(entry, interval, finish_sweep)
    local previous, sweep, response_seen, speed_upper = entry, 1, false, nil
    return function(frame, visual)
        local bounds = visual.position_bounds
        if not bounds then return end
        local old = previous.visual
        local old_bounds = old.position_bounds
        local elapsed = frame - previous.frame
        if elapsed > interval and (not speed_upper or elapsed * speed_upper >= 240) then
            if visual.readable then
                visual.sequence_status, visual.reason = "ambiguous", "Unreadable interval could hide an additional complete sweep."
            end
            return
        end
        local reset = bounds[2] < old_bounds[1]
        if reset then
            if old_bounds[1] < 128 or bounds[2] >= 128 or not response_seen then
                if visual.readable then
                    visual.sequence_status, visual.reason = "ambiguous", "The visible reset lacks the preceding response."
                end
                return
            end
            sweep, response_seen = sweep + 1, false
        elseif not visual.readable and bounds[1] <= old_bounds[2] then
            return
        elseif visual.readable and old.readable and visual.phase == old.phase
            and visual.x > old.x and old.x < 247 and visual.x < 247 then
            speed_upper = math.max(speed_upper or 0, (visual.x - old.x + 1) / elapsed)
        end
        if bounds[1] >= 128 then response_seen = true end
        visual.sweep, visual.pokemon, visual.dance = sweep, math.floor((sweep - 1) / 2) + 1, (sweep - 1) % 2 + 1
        visual.completed_sweeps = sweep - 1
        visual.sequence_source = "first_demo_then_readable_response_to_demo_resets"
        visual.sequence_support = not visual.readable and (reset and "bounded_response_to_demo_reset" or "bounded_forward_progress") or nil
        visual.finish_confirmed = visual.readable and sweep == (finish_sweep or 3) and visual.phase == "demo"
        previous = {frame = frame, visual = visual}
    end
end

function M.new(config, runtime)
    local api, adapter, engine = runtime.api, runtime.adapter, runtime.engine
    local mailbox = bridge_io.new(directory .. "/../tmp")
    local section, captured, latest, pending, active = {}, nil, nil, nil, nil
    local phase = config.stage == 3 and "menu" or config.stage == 4 and "finish-dress" or "observe"
    local captures, dance_seen, last_sample_frame = 0, false, config.source_frame
    local finish_sweep = (config.measurement_dances or 2) + 1
    local single_dance = config.stage == 5 and config.measurement_dances == 1
    local track = config.stage == 5 and tracker(config.entry_observation, config.capture_wait_frames, finish_sweep)
    local observed, observed_frames, first_response_frame, trace = nil, 0, nil, nil
    if single_dance then
        trace = assert(io.open(config.work .. "/observation-frames.jsonl", "w"))
    end
    if config.resume_history then
        for index = 2, #config.resume_history do
            local sample = config.resume_history[index]
            track(sample.frame, sample.visual)
            assert(sample.visual.sequence_status ~= "ambiguous", "The retained entry images do not establish a unique sweep count")
        end
        phase = "resume"
    end

    function section.observation(frame)
        return {frame = frame, epoch = adapter.epoch(), game = {code = emu.gamecode(), supported = emu.gamecode() == "CPUJ"},
            target = config.target, stage = config.stage, input_policy = config.stage == 5 and "none" or "scripted_menu",
            released = api.state.command_done and api.state.buttons == "" and not api.state.stylus_touch}
    end

    local function capture(resuming)
        local frame = adapter.frame()
        assert(not active and not pending and api.state.command_done and (not captured or captured.frame ~= frame),
            "Take one fresh capture after each completed input or observation interval")
        captures = captures + 1
        local image, raw = runtime.image(config.work .. string.format("/sample-%05d.gd", captures), frame)
        local value = section.observation(frame)
        value.image = image
        if config.stage == 3 then
            value.screen = screens.read(raw, "dress_up_entry")
        else
            local seen
            value.visual, seen = position(raw, config.pointer_templates)
            dance_seen = dance_seen or seen
            value.dance_seen = dance_seen
            if config.stage == 4 then
                value.visual.sweep, value.visual.pokemon, value.visual.dance = 1, 1, 1
            elseif single_dance then
                assert(observed and observed.frame == frame and observed.raw == raw
                    and frame > last_sample_frame and frame - last_sample_frame <= config.capture_wait_frames
                    and observed_frames == frame - config.source_frame,
                    "Bind the fresh paused survey capture to continuous input-free frame checks")
                value.visual = observed.visual
                value.survey = {observed_frames = observed_frames, input_free_frames = observed_frames,
                    dance_input_count = 0, dance_stylus_count = 0, first_response_frame = first_response_frame,
                    frame_by_frame_tracking = true, first_dance_2_frame = value.visual.finish_confirmed and frame or nil}
                last_sample_frame = frame
            else
                assert(resuming and frame == config.source_frame or frame == last_sample_frame + config.capture_wait_frames,
                    "Keep the exact two-second emulator-frame capture cadence after the verified paused resume")
                track(frame, value.visual)
                last_sample_frame = frame
            end
        end
        captured = {frame = frame, epoch = adapter.epoch(), raw = raw, value = value}
        return value
    end

    function section.snapshot(frame)
        assert(runtime.is_paused() and adapter.frame() == frame, "Measurement checkpoints require a native zero-frame pause")
        if phase == "stopped" then
            local value = section.observation(frame)
            value.image = runtime.image(config.work .. "/failure.gd", frame)
            return value
        end
        assert(captured and captured.frame == frame and captured.epoch == adapter.epoch(), "Use the current captured pixels")
        captured.value.fresh_visual_binding = captured.raw == gui.gdscreenshot()
        return captured.value
    end

    function section.finish(message)
        active = nil
        if trace then trace:close(); trace = nil end
        if message then phase = "stopped" end
        api.finish(message)
    end

    function section.checkpoint(action, args)
        local value
        if action == "capture" then
            assert((config.stage == 3 or config.stage == 4) and phase == "poll",
                "Complete enrollment, or Finish plus the full A mash and an input-free entry interval, before capturing")
            value = capture()
        elseif action == "visit" or action == "measurement-visit" or action == "resume-visit" then
            assert(not pending, "Acknowledge the current measurement boundary first")
            local name
            if config.stage == 5 then
                local resuming = action == "resume-visit" and phase == "resume" and config.resume_history
                assert(resuming or action == "measurement-visit" and phase == "capture", "Complete the next input-free interval")
                value = capture(resuming)
                name = value.visual.finish_confirmed and "dance-measurement-finish" or "dance-measurement"
            else
                assert(action == "visit" and phase == "poll" and captured, "Capture the section's readable endpoint first")
                name = config.stage == 3 and "stage-3-enter" or "stage-4-dress"
            end
            latest = engine:visit(name, {frame = adapter.frame(), epoch = adapter.epoch(), finish_sweep = finish_sweep})
            pending = (latest.status == "awaiting_ack" or latest.status == "awaiting_save") and latest or nil
            value, phase = latest.snapshot, "checkpoint"
        elseif action == "complete" then
            local entry_save = config.stage == 4 and config.retain_entry_state == true
            assert(pending and (entry_save and #args == 6 or not entry_save and #args == 7 and args[7] == "false"),
                "Save only a declared measurement entry; other screen boundaries acknowledge pixels")
            latest = engine:complete(tonumber(args[2]), {run_id = args[1], visit = tonumber(args[2]),
                frame = tonumber(args[3]), epoch = tonumber(args[4]), path = args[5], emulator_paused = args[6] == "true", saved = entry_save})
            pending, value = nil, latest.snapshot
            phase = latest.proceed and latest.checkpoint == "dance-measurement"
                and latest.snapshot.visual.sequence_status ~= "ambiguous" and "observe" or "finish"
        elseif action == "abort" then
            local reason = args[1] or "Host stopped the dance measurement"
            section.finish(reason)
            if pending then latest, pending = engine:cancel(reason), nil
            elseif not engine:state().halted then
                latest = engine:visit("stopped", {reason = reason})
                pending = latest.status == "awaiting_ack" and latest or nil
            end
            value = latest and latest.snapshot
        else
            error("Unknown measurement checkpoint command: " .. tostring(action))
        end
        local result = {frame = adapter.frame(), epoch = adapter.epoch(), checkpoint = latest, observation = value, next_section = phase}
        if action == "resume-visit" then result.recovered_samples = config.resume_history end
        if not pending then result.progress = engine:state() end
        return result
    end

    local function start(parts)
        local action, steps = parts[2], {}
        assert(not pending and api.state.command_done, "Acknowledge the paused evidence before advancing")
        if action == "combo" then
            assert(config.stage == 3 and phase == "menu" and parts[3] == "inputs" and #parts == #config.inputs + 3,
                "Only the prepared continuous enrollment sequence is allowed")
            for index, step in ipairs(config.inputs) do assert(parts[index + 3] == step, "The entry inputs changed"); steps[index] = step end
        elseif action == "finish-dress" then
            assert(config.stage == 4 and phase == "finish-dress" and #parts == 2, "Tap Finish immediately at dress-up entry")
            local touch = config.stylus
            active = combos.build("inputs", config.transition.inputs, adapter.frame(), 0, bridge_io.valid_buttons)
            local total = 0
            for _, step in ipairs(active.steps) do
                assert(step.action == "press" and #step.button == 1 and step.button[1] == "A",
                    "The prepared dress-up transition permits only continuous A mashing")
                total = total + step.total
            end
            assert(total == config.transition.mash_frames, "Complete the exact agreed transition frame count")
            table.insert(active.steps, 1, {hold = touch.hold_frames,
                total = touch.hold_frames + touch.release_frames, touch = touch})
            active.reason, active.next_phase = "debug_transition_complete", "entry-wait"
        elseif action == "wait-entry" then
            assert(config.stage == 4 and (phase == "entry-wait" or phase == "poll" and captured) and #parts == 2,
                "One-second input-free checks start only after Finish and the complete A-mashing transition")
            steps = {"wait:" .. config.entry_poll.wait_frames}
        elseif action == "observe" then
            assert(config.stage == 5 and phase == "observe" and #parts == 2, "Dancing permits only the next input-free observation interval")
            steps = {"wait:" .. config.capture_wait_frames}
        else
            error("Button recognition and dance inputs are disabled in measurement mode")
        end
        if not active then
            active = combos.build("inputs", steps, adapter.frame(), 0, bridge_io.valid_buttons)
            active.reason = action == "combo" and "debug_entry_complete" or "debug_wait_complete"
        end
        active.index, active.elapsed = 1, 0
        api.state.command_id, api.state.command_done, api.state.error = parts[1], false, ""
        api.state.command_start_frame, api.state.last_action = adapter.frame(), action
        api.state.combo_name = ""
    end

    function section.before()
        if not active then
            local parts = mailbox:read_command(api.state.command_id)
            if parts then
                api.state.command_id = parts[1]
                start(parts)
            end
        end
        local step = active and active.steps[active.index]
        local held = step and step.hold and active.elapsed < step.hold
        local button = held and step.button or nil
        local touch = held and step.touch
        if config.stage == 5 then
            assert(not button and not touch, "Dance buttons and stylus inputs are disabled during measurement")
        end
        bridge_io.apply_buttons(button)
        stylus.set(touch and {x = touch.x, y = touch.y, touch = true} or {touch = false})
        api.state.buttons, api.state.stylus_touch = button and step.label or "", touch and true or false
    end

    function section.after()
        if active then
            if single_dance then
                local frame, raw = adapter.frame(), gui.gdscreenshot()
                assert(frame == config.source_frame + observed_frames + 1,
                    "Check every emulator frame in the one-dance survey")
                assert(api.state.buttons == "" and not api.state.stylus_touch,
                    "The one-dance survey must remain input-free")
                local visual = position(raw, config.pointer_templates)
                track(frame, visual)
                observed_frames = observed_frames + 1
                observed = {frame = frame, raw = raw, visual = visual}
                trace:write(json.encode({frame = frame, epoch = adapter.epoch(), visual = visual,
                    buttons = api.state.buttons, stylus_touch = api.state.stylus_touch}), "\n")
                local midpoint = not first_response_frame and visual.readable
                    and visual.sweep == 1 and visual.phase == "response"
                if midpoint then first_response_frame = frame end
                if midpoint or visual.finish_confirmed or visual.sequence_status == "ambiguous" then
                    trace:flush()
                    if visual.finish_confirmed then trace:close(); trace = nil end
                    active, phase = nil, "capture"
                    api.finish(nil, "debug_wait_complete")
                    api.capture(); api.publish(); emu.pause()
                    return
                end
            end
            active.elapsed = active.elapsed + 1
            if active.elapsed >= active.steps[active.index].total then
                active.index, active.elapsed = active.index + 1, 0
                if active.index > #active.steps then
                    local reason, next_phase = active.reason, active.next_phase
                    active = nil
                    phase = next_phase or (config.stage == 5 and "capture" or "poll")
                    if trace then trace:flush() end
                    api.finish(nil, reason)
                    api.capture(); api.publish(); emu.pause()
                    return
                end
            end
        end
        if adapter.frame() % 15 == 0 then api.publish() end
    end
    return section
end

M.position = position
return M
