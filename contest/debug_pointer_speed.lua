-- Saved-entry, input-free pointer samples inside the persistent contest dispatcher.
local directory = assert(debug.getinfo(1, "S").source:sub(2):gsub("\\", "/"):match("^(.*)/"))
local root = dofile(directory .. "/resource_root.lua")
local bridge_io = dofile(root .. "/lua/bridge_io.lua")
local measurement = dofile(directory .. "/debug_measure.lua")
local pixels = dofile(directory .. "/dance_pixels.lua")
local M = {}

local function same_target(a, b)
    if not a or not b then return false end
    for _, key in ipairs({"category", "rank", "party_slot", "category_down_presses", "rank_down_presses"}) do
        if a[key] ~= b[key] then return false end
    end
    return true
end

local function same_identity(a, b)
    if not a or not b then return false end
    for _, key in ipairs({"checkpoint", "run_id", "visit", "frame", "epoch"}) do
        if a[key] ~= b[key] then return false end
    end
    return true
end

function M.definitions(base, runtime)
    local definitions = measurement.definitions(base, runtime, false)
    definitions["stage-4-dress"].save_state = true
    definitions["pointer-speed-sample"] = {save_state = false, branches = {pass = "reload_entry"}, checks = {
        {id = "released_inputs", field = "released", expected = true},
        {id = "fresh_framebuffer", field = "fresh_visual_binding", expected = true},
        {id = "capture_frame", field = "image.frame", context = "frame"},
        {id = "capture_epoch", field = "image.epoch", context = "epoch"},
        {id = "readable_pointer", field = "visual.readable", expected = true},
        {id = "moving_phase", field = "visual.phase", op = "one_of", expected = {"demo", "response"}},
        {id = "saved_start_frame", field = "sampling.start_frame", context = "source_frame"},
        {id = "same_epoch", field = "sampling.start_epoch", context = "epoch"},
        {id = "sample_index", field = "sampling.index", context = "sample_index"},
        {id = "counted_wait", field = "sampling.elapsed_frames", context = "wait_frames"},
        {id = "input_free_wait", field = "sampling.input_free_frames", context = "wait_frames"},
        {id = "no_reset", field = "sampling.reset_seen", expected = false},
        {id = "before_strip_end", field = "sampling.strip_end_seen", expected = false},
        {id = "forward_pointer", expected = "start_x < x < 247", evaluate = function(snapshot, context)
            local x = snapshot.visual.x
            return type(x) == "number" and x > context.start_x and x < 247, x
        end},
    }}
    return definitions
end

function M.run(runtime)
    local targets, index, base, section, reset_armed = nil, 1, nil, nil, false
    local run = {}
    function run.begin(config, latest)
        assert(not reset_armed, "Complete the armed ROM reset before starting another section")
        if config.stage == 1 then
            if not targets then
                targets = config.measurement_targets
                assert(type(targets) == "table" and ((#targets == 2
                    and targets[1].category == "Smart" and targets[2].category == "Tough"
                    and targets[1].rank == "Master" and targets[2].rank == "Master")
                    or (#targets == 1 and targets[1].category == "Tough" and targets[1].rank == "Master")),
                    "Measure Smart then Tough Master, or restart Tough Master only")
            end
            assert(not base and not latest and same_target(config.target, targets[index]),
                "Load each configured target once from ROM boot")
            base = measurement.run(runtime, false)
            config.measurement_targets = {config.target}
            return base.begin(config, latest)
        end
        assert(base and same_target(config.target, targets[index]), "Keep the selected pointer-speed target")
        if config.stage == 5 then
            assert(latest and latest.checkpoint == "stage-4-dress" and latest.status == "complete" and latest.proceed
                and latest.frame == runtime.adapter.frame() and latest.epoch == runtime.adapter.epoch()
                and same_identity(config.source_context, latest) and same_target(config.target, latest.snapshot.target)
                and latest.save_required and latest.savestate,
                "Sample from the acknowledged saved Dance 1 entry")
            config.entry_observation = latest.snapshot
            section = M.new(config, runtime, latest)
            return section
        end
        return base.begin(config, latest)
    end
    function run.arm(args, latest)
        assert(section, "Create the saved-entry sampling section first")
        return section.arm(args, latest)
    end
    function run.loaded(args)
        assert(section, "Load only the active sampling section's saved entry")
        section.restored(args)
        return true
    end
    function run.arm_reset(args, latest)
        assert(not reset_armed and targets and #targets == 2 and index == 1 and #args == 0 and section and section.finished()
            and latest and latest.status == "complete" and latest.proceed and latest.checkpoint == "pointer-speed-sample"
            and latest.snapshot.sampling.index == 10 and same_target(latest.snapshot.target, targets[index])
            and latest.epoch + 1 == runtime.adapter.epoch(),
            "Reset only after all ten Smart samples and their final saved-entry reload")
        reset_armed = true
        return {frame = runtime.adapter.frame(), epoch = runtime.adapter.epoch(), next_target = targets[2]}
    end
    function run.can_reset() return reset_armed end
    function run.reset()
        assert(reset_armed and index == 1, "Perform the single configured Smart-to-Tough ROM reset")
        index, reset_armed, base, section = 2, false, nil, nil
    end
    return run
end

function M.new(config, runtime, entry)
    local api, adapter, engine = runtime.api, runtime.adapter, runtime.engine
    local mailbox = bridge_io.new(directory .. "/../tmp")
    local sampling, start = config.sampling, config.entry_observation
    assert(sampling.sample_count == 10 and #sampling.wait_frames == sampling.sample_count
        and type(sampling.min_frames) == "number" and sampling.min_frames >= 1
        and sampling.min_frames == math.floor(sampling.min_frames)
        and type(sampling.maximum_fraction) == "number" and sampling.maximum_fraction > 0 and sampling.maximum_fraction < 1
        and sampling.max_frames == math.floor(sampling.dance_frames * sampling.maximum_fraction)
        and start.visual.readable and start.visual.phase == "demo" and start.frame == config.source_frame,
        "Declare ten waits inside the configured minimum and fraction of demo plus response")
    local unique = {}
    for _, frames in ipairs(sampling.wait_frames) do
        assert(type(frames) == "number" and frames == math.floor(frames) and frames >= sampling.min_frames
            and frames <= sampling.max_frames and not unique[frames], "Use the declared distinct integer-frame waits")
        unique[frames] = true
    end
    local section, phase, active, stopped, captured, pending = {}, "ready", nil, nil, nil, nil
    local completed, reloads, armed, latest = 0, 0, false, entry

    function section.observation(frame)
        return {frame = frame, epoch = adapter.epoch(), game = {code = emu.gamecode(), supported = emu.gamecode() == "CPUJ"},
            target = config.target, stage = 5, input_policy = "none",
            released = api.state.command_done and api.state.buttons == "" and not api.state.stylus_touch}
    end
    function section.snapshot(frame)
        assert(runtime.is_paused() and frame == adapter.frame(), "Take a zero-frame paused pointer snapshot")
        if phase == "stopped" then
            local value = section.observation(frame)
            value.image = runtime.image(config.work .. "/failure.gd", frame)
            return value
        end
        assert(captured and captured.frame == frame and captured.epoch == adapter.epoch(), "Bind the current endpoint capture")
        captured.value.fresh_visual_binding = captured.raw == gui.gdscreenshot()
        return captured.value
    end
    function section.finish(message)
        active = nil
        if message then phase = "stopped" end
        api.finish(message)
    end
    function section.arm(args, record)
        assert(not armed and phase == "reload" and record == latest and record.status == "complete" and record.proceed
            and #args == 1 and tonumber(args[1]) == entry.visit and record.frame == adapter.frame()
            and record.epoch == adapter.epoch(), "Reload the saved entry only after an acknowledged endpoint")
        armed = true
        return {frame = adapter.frame(), epoch = adapter.epoch(), source_visit = entry.visit}
    end
    function section.restored(args)
        assert(armed and args[1] == "load" and tonumber(args[2]) == entry.visit and adapter.frame() == entry.frame,
            "Restore the exact saved entry frame without rendering preparation")
        armed, reloads, captured, stopped = false, reloads + 1, nil, nil
        phase = completed == sampling.sample_count and "finish" or "ready"
    end
    function section.finished()
        return phase == "finish" and completed == sampling.sample_count and reloads == completed and adapter.frame() == entry.frame
    end
    function section.checkpoint(action, args)
        local value
        if action == "speed-sample" then
            assert(#args == 0 and phase == "capture" and stopped and not pending and not captured,
                "Capture once after the complete counted wait")
            local frame = adapter.frame()
            local image, raw = runtime.image(config.work .. string.format("/sample-%05d.gd", stopped.index), frame)
            value = section.observation(frame)
            value.image, value.visual = image, measurement.position(raw, config.pointer_templates)
            value.sampling = {index = stopped.index, start_frame = stopped.frame, start_epoch = stopped.epoch,
                wait_frames = stopped.wait, elapsed_frames = frame - stopped.frame, input_free_frames = stopped.elapsed,
                start_x = start.visual.x, reset_seen = stopped.reset_seen, strip_end_seen = stopped.strip_end_seen,
                readable_bar_frames = stopped.readable_bar_frames, unreadable_bar_frames = stopped.unreadable_bar_frames,
                saved_entry_visit = entry.visit}
            captured = {frame = frame, epoch = adapter.epoch(), raw = raw, value = value}
            latest = engine:visit("pointer-speed-sample", {frame = frame, epoch = adapter.epoch(),
                source_frame = entry.frame, start_x = start.visual.x, wait_frames = stopped.wait, sample_index = stopped.index})
            pending = latest.status == "awaiting_ack" and latest or nil
            value, phase = latest.snapshot, "checkpoint"
        elseif action == "complete" then
            assert(pending and #args == 7 and args[7] == "false", "Acknowledge endpoint pixels without another save")
            latest = engine:complete(tonumber(args[2]), {run_id = args[1], visit = tonumber(args[2]),
                frame = tonumber(args[3]), epoch = tonumber(args[4]), path = args[5], emulator_paused = args[6] == "true", saved = false})
            pending, value = nil, latest.snapshot
            if latest.proceed then completed, phase = completed + 1, "reload" else phase = "stopped" end
        elseif action == "abort" then
            local reason = args[1] or "Host stopped the pointer-speed measurement"
            section.finish(reason)
            if pending then latest, pending = engine:cancel(reason), nil
            elseif not engine:state().halted then
                latest = engine:visit("stopped", {reason = reason})
                pending = latest.status == "awaiting_ack" and latest or nil
            end
            value = latest and latest.snapshot
        else
            error("Unknown pointer-speed checkpoint action: " .. tostring(action))
        end
        local result = {frame = adapter.frame(), epoch = adapter.epoch(), checkpoint = latest,
            observation = value, next_section = phase}
        if not pending then result.progress = engine:state() end
        return result
    end
    function section.before()
        if not active then
            local parts = mailbox:read_command(api.state.command_id)
            if parts then
                local frames = tonumber(parts[3])
                assert(parts[2] == "sample" and #parts == 3 and phase == "ready" and not pending and not armed
                    and api.state.command_done and adapter.frame() == entry.frame
                    and frames == sampling.wait_frames[completed + 1], "Run only the next declared input-free frame wait")
                active = {frame = adapter.frame(), epoch = adapter.epoch(), wait = frames, index = completed + 1,
                    elapsed = 0, reset_seen = false, strip_end_seen = false, readable_bar_frames = 0,
                    unreadable_bar_frames = 0, previous_bounds = start.visual.position_bounds}
                api.state.command_id, api.state.command_done, api.state.error = parts[1], false, ""
                api.state.command_start_frame, api.state.last_action, api.state.combo_name = adapter.frame(), "sample", ""
            end
        end
        bridge_io.apply_buttons(nil); stylus.set({touch = false})
        api.state.buttons, api.state.stylus_touch = "", false
    end
    function section.after()
        if active then
            local frame = adapter.frame()
            assert(frame == active.frame + active.elapsed + 1 and adapter.epoch() == active.epoch
                and api.state.buttons == "" and not api.state.stylus_touch, "Count every input-free frame in one load epoch")
            active.elapsed = active.elapsed + 1
            local bar = pixels.bar(gui.gdscreenshot())
            local bounds = bar.found and bar.first_x == 8 and bar.position_bounds
            if bounds then
                active.readable_bar_frames = active.readable_bar_frames + 1
                if active.previous_bounds and bounds[2] < active.previous_bounds[1] then active.reset_seen = true end
                if bounds[1] >= 247 then active.strip_end_seen = true end
                active.previous_bounds = bounds
            else
                active.unreadable_bar_frames = active.unreadable_bar_frames + 1
            end
            if active.elapsed == active.wait then
                stopped, active, phase = active, nil, "capture"
                api.finish(nil, "pointer_speed_wait_complete")
                api.capture(); api.publish(); emu.pause()
                return
            end
        end
        if adapter.frame() % 15 == 0 then api.publish() end
    end
    return section
end

return M
