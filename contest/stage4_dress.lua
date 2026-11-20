-- Paused accessory pages, counted stylus drags, Finish, and the Dance 1 handoff.
local config, runtime = ...
assert(type(config) == "table" and config.stage == 4 and type(runtime) == "table",
    "Stage 4 requires the persistent main runtime")
assert(type(stylus) == "table" and type(stylus.set) == "function", "DeSmuME stylus.set is required")
local source = debug.getinfo(1, "S").source:sub(2):gsub("\\", "/")
local directory = assert(source:match("^(.*)/"))
local root = dofile(directory .. "/resource_root.lua")
local bridge_io = dofile(root .. "/lua/bridge_io.lua")
local platinum = dofile(root .. "/lua/platinum.lua")
local dress_pixels = dofile(directory .. "/dress_pixels.lua")
local dance_pixels = dofile(directory .. "/dance_pixels.lua")
local mailbox_directory = runtime.mailbox_directory
local mailbox = bridge_io.new(mailbox_directory)
local idle, active, engine, pending, latest = false, nil, runtime.engine, nil, nil
local initialized = false
local stage, screen, transition, dressing = "setup", nil, nil, nil
local capture_kind, render_prepared = "endpoint", false
local touch_complete, mash_complete = false, false
local state, adapter = runtime.state, runtime.adapter
local read_tip

local function write_file(path, value, mode)
    local file = assert(io.open(path, mode or "w"))
    assert(file:write(value))
    assert(file:close())
end

local function read_file(path)
    local file = assert(io.open(path, "rb"))
    local value = assert(file:read("*a"))
    assert(file:close())
    return value
end

local function game()
    return platinum.game(emu.gamecode())
end

local function release()
    bridge_io.apply_buttons(nil)
    stylus.set({touch = false})
    state.buttons, state.stylus_touch = "", false
end

local function capture()
    mailbox:capture_screen()
    state.screen_frame = state.frame
end

local function pointer_in_strip(raw, x)
    if type(x) ~= "number" or x ~= math.floor(x) or x < 8 or x >= 128 then return false end
    local bar = dance_pixels.bar(raw)
    return bar.found and bar.position_bounds and bar.position_bounds[1] <= x and x <= bar.position_bounds[2] or false
end

local function snapshot(frame)
    local raw = gui.gdscreenshot()
    local trial = {stage = stage, touch_complete = touch_complete, mash_complete = mash_complete,
        render_prepared = render_prepared,
        entry_pointer_valid = false,
        dressing_complete = dressing ~= nil and dressing.complete == true,
        released = state.buttons == "" and state.stylus_touch == false}
    if capture_kind == "dress_page" then
        assert(idle and adapter.frame() == frame, "The accessory snapshot must add zero emulator frames")
        local page = dress_pixels.read_page(raw, config.automatic_dressing, config.target.rank)
        page.cells = setmetatable(page.cells, bridge_io.array_metatable)
        page.items = setmetatable(page.items, bridge_io.array_metatable)
        page.index, page.frame, page.epoch = dressing.pages, frame, adapter.epoch()
        page.path = config.work .. string.format("/dress-page-%03d.gd", page.index)
        write_file(page.path, raw, "wb")
        write_file(mailbox_directory .. "/screen.gd", raw, "wb")
        state.screen_frame = frame
        dressing.page = page
        if page.full then
            dressing.full_detected, dressing.full_frame, dressing.complete = true, frame, true
            dressing.end_frame, dressing.end_reason = frame, "accessory_limit"
            dressing.elapsed_game_seconds = dressing.elapsed_frames / config.automatic_dressing.frame_rate
            trial.dressing_complete = true
        end
        return {frame = frame, game = game(), source_context = config.source_context, party_context = config.party_context,
            target = config.target, dressing = dressing, trial = trial}
    end
    local current_screen = {source = screen and screen.source or "unreviewed", memory_proven = false,
        tip = read_tip(raw), pointer = screen and screen.pointer or nil,
        pointer_source = screen and screen.pointer_source or nil,
        pointer_phase = screen and screen.phase or "unknown", dance = screen and screen.dance or 0,
        frame = screen and screen.frame or frame, epoch = screen and screen.epoch or adapter.epoch(),
        fresh_visual_binding = screen ~= nil and screen.frame == frame and screen.epoch == adapter.epoch()
            and read_file(screen.path) == raw or false}
    local pointer = screen and screen.pointer
    local bounds = pointer and pointer.position_bounds
    trial.entry_pointer_valid = screen and screen.phase == "demo" and pointer and pointer.status == "exact"
        and bounds and bounds[1] == bounds[2] and pointer_in_strip(raw, bounds[1]) or false
    return {frame = frame, game = game(), source_context = config.source_context, party_context = config.party_context, target = config.target,
        screen = current_screen, transition = transition, dressing = dressing, trial = trial}
end

local function publish(observation)
    state.observation = observation or {frame = state.frame, game = game(), transition = transition, dressing = dressing,
        trial = {stage = stage, touch_complete = touch_complete, mash_complete = mash_complete}}
    mailbox:publish(state)
end

local function finish(message, reason)
    if message then runtime.fail_stage() end
    active = nil
    release()
    state.error, state.command_done = message or "", true
    state.phase = message and "command_error" or "ready"
    state.stop_reason = message and "command_error" or reason or "command_complete"
    state.command_end_frame = state.frame
end

local function stop(message, reason)
    finish(message, reason)
    capture()
    publish()
    emu.pause()
end

-- The left tip uses the existing four-row fill profiles. Its bright inner row
-- is part of the shape, so a white line or flat green background is insufficient.
local filled = {
    [string.char(56,255,134,190,255,255,56,255,134,24,207,113)] = true,
    [string.char(207,255,134,255,255,223,207,255,134,174,223,97)] = true,
    [string.char(24,199,24,97,215,97,24,199,24,16,158,16)] = true,
}
read_tip = function(raw)
    assert(#raw == 393227 and raw:byte(1) == 255 and raw:byte(2) == 254,
        "Expected a true-color 256 by 384 GD framebuffer")
    local values, profile = {}, {}
    for _, y in ipairs(config.dance_check.tip_rows) do
        local position = 13 + (y * 256 + config.dance_check.tip_x) * 4
        values[#values + 1] = raw:sub(position, position + 2)
        profile[#profile + 1] = {raw:byte(position, position + 2)}
    end
    return {source = "left_tip_four_row_fill_profiles", x = config.dance_check.tip_x,
        rows = config.dance_check.tip_rows, rgb_profile = profile, green = filled[table.concat(values)] == true}
end

local function packet(observation)
    local result = {frame = adapter.frame(), epoch = adapter.epoch(), checkpoint = latest,
        observation = observation, next_section = stage}
    if engine and not pending then result.progress = engine:state() end
    return result
end

local function definitions()
    return {
        ["stage-4-dress"] = {checks = {
            {id = "finish_tap_complete", field = "trial.touch_complete", expected = true},
            {id = "dressing_complete", field = "trial.dressing_complete", expected = true},
            {id = "A_mashing_complete", field = "trial.mash_complete", expected = true},
            {id = "released", field = "trial.released", expected = true},
            {id = "pixel_reader", field = "screen.source", expected = "fresh_zero_frame_left_tip_pixels"},
            {id = "fresh_framebuffer_binding", field = "screen.fresh_visual_binding", expected = true},
            {id = "review_frame", field = "screen.frame", context = "frame"},
            {id = "review_epoch", field = "screen.epoch", context = "epoch"},
            {id = "dance_1", field = "screen.dance", expected = 1},
            {id = "green_progress_tip", field = "screen.tip.green", expected = true},
            {id = "demo_pointer", field = "trial.entry_pointer_valid", expected = true},
        }, branches = {pass = "finish"}},
        ["stage-4-dress-page"] = {checks = {
            {id = "released", field = "trial.released", expected = true},
            {id = "render_prepared", field = "trial.render_prepared", expected = true},
            {id = "readable_rank_indicator", field = "dressing.page.readable", expected = true},
            {id = "capture_frame", field = "dressing.page.frame", context = "frame"},
            {id = "capture_epoch", field = "dressing.page.epoch", context = "epoch"},
        }, target = {{id = "accessory_limit", field = "dressing.page.full", expected = true}},
            branches = {hit = "finish-dress-early", miss = "drag-dress-page"}},
        stopped = {checks = {{id = "host_stop", expected = "continue",
            evaluate = function(_, context) return false, context.reason end}}, branches = {pass = "finish"}},
    }
end

local function dispatch(action, args)
    local observation
    if action == "init" then
        assert(not initialized and #args == 0 and adapter.frame() == config.source_frame,
            "Expected the accepted current dress-up boundary")
        assert(engine and config.entry_mode == "stage3_handoff" and config.entry_render_frames == 0,
            "Direct Stage 3 to Stage 4 entry uses the main engine and adds zero frames")
        assert(config.target and config.automatic_dressing.full_checks[config.target.rank],
            "Automatic dressing requires the accepted contest rank")
        local taps = config.initial_page_taps
        assert(type(taps) == "number" and taps >= 0 and taps <= 7 and taps == math.floor(taps),
            "Choose the initial accessory page once, from zero through seven taps")
        local page = config.automatic_dressing.next_page
        local page_flip_limit = config.automatic_dressing.page_flip_limit
        assert(page_flip_limit == nil or type(page_flip_limit) == "number" and page_flip_limit >= 1
            and page_flip_limit == math.floor(page_flip_limit), "The collection page-flip limit must be a positive whole count")
        local initial_frames = taps * (page.hold_frames + page.release_frames + page.wait_frames)
        assert(config.entry_render_frames + initial_frames + config.automatic_dressing.motion.release_frames
            <= config.automatic_dressing.budget_frames, "Initial navigation must fit inside the dressing budget")
        dressing = {start_frame = adapter.frame(), epoch = adapter.epoch(),
            budget_frames = config.automatic_dressing.budget_frames,
            elapsed_frames = 0, remaining_frames = config.automatic_dressing.budget_frames,
            pages = 0, attempted_drags = 0, completed_drag_inputs = 0, page_taps = 0, collection_page_taps = 0,
            page_flip_limit = page_flip_limit, page_flip_limit_reached = false,
            full_detected = false, complete = false, render_preparation_frames = config.entry_render_frames,
            initial_navigation = {chosen_taps = taps, executed_taps = 0, planned_frames = initial_frames,
                executed_frames = 0, complete = taps == 0}}
        initialized = true
        render_prepared = true
        stage = taps > 0 and "initial-pages" or "dressing"
    elseif action == "dress-page-visit" then
        assert(engine and stage == "dressing" and render_prepared and dressing.initial_navigation.complete
            and not active and not pending and dressing and dressing.remaining_frames > 0
            and adapter.epoch() == dressing.epoch
            and adapter.frame() == dressing.start_frame + dressing.elapsed_frames,
            "Expected the next paused accessory page within the counted dressing window")
        dressing.pages, capture_kind = dressing.pages + 1, "dress_page"
        latest = engine:visit("stage-4-dress-page", {frame = adapter.frame(), epoch = adapter.epoch()})
        capture_kind = "endpoint"
        pending = (latest.status == "awaiting_save" or latest.status == "awaiting_ack") and latest or nil
        observation, stage = latest.snapshot, "checkpoint"
    elseif action == "check-dance" then
        assert(engine and stage == "observe" and mash_complete and not active and not pending,
            "Complete A-mashing and pause before checking dancing entry")
        local tip = read_tip(gui.gdscreenshot())
        tip.frame, tip.epoch = adapter.frame(), adapter.epoch()
        transition.poll_count = transition.poll_count + 1
        transition.total_frames = adapter.frame() - transition.start_frame
        observation = {frame = adapter.frame(), game = game(), tip = tip, transition = transition}
        if tip.green then
            transition.candidate_frame, stage = adapter.frame(), "dance_candidate"
        end
    elseif action == "capture" then
        assert(engine and stage == "dance_candidate" and not active and not pending,
            "A paused dancing candidate is required")
        write_file(config.work .. "/stage4.gd", gui.gdscreenshot(), "wb")
        capture()
        observation = snapshot(adapter.frame())
        observation.tip = observation.screen.tip
    elseif action == "visit" then
        assert(engine and stage == "dance_candidate" and not active and not pending and #args == 1,
            "Expected the paused endpoint pixel evidence")
        screen = assert(loadfile(args[1]))()
        assert(type(screen) == "table" and screen.frame == adapter.frame() and screen.epoch == adapter.epoch(),
            "The endpoint evidence belongs to a different paused boundary")
        latest = engine:visit("stage-4-dress", {frame = adapter.frame(), epoch = adapter.epoch()})
        pending = (latest.status == "awaiting_save" or latest.status == "awaiting_ack") and latest or nil
        observation, stage = latest.snapshot, "checkpoint"
        capture()
    elseif action == "complete" then
        assert(pending and (#args == 6 or #args == 7), "Expected pending checkpoint evidence")
        latest = engine:complete(tonumber(args[2]), {run_id = args[1], visit = tonumber(args[2]),
            frame = tonumber(args[3]), epoch = tonumber(args[4]), path = args[5], emulator_paused = args[6] == "true",
            saved = args[7] ~= "false"})
        pending, observation = nil, latest.snapshot
        stage = latest.proceed and latest.branch or "stopped"
    elseif action == "abort" then
        local reason = args[1] or "Host stopped contest Stage 4"
        finish(reason)
        stage, capture_kind = "stopped", "endpoint"
        if pending then
            latest, pending = engine:cancel(reason), nil
        elseif engine and not engine:state().halted then
            latest = engine:visit("stopped", {reason = reason})
            pending = (latest.status == "awaiting_save" or latest.status == "awaiting_ack") and latest or nil
        end
        observation = latest and latest.snapshot
        capture()
    else
        error("Unknown Stage 4 checkpoint command: " .. tostring(action))
    end
    return packet(observation)
end

local function start(parts)
    state.command_id = parts[1]
    assert(engine and initialized and not pending, "Expected one authorized Stage 4 action")
    state.command_id, state.command_done, state.error = parts[1], false, ""
    state.last_action, state.command_start_frame = parts[2], state.frame
    state.phase, state.stop_reason, state.command_end_frame = "running", nil, nil
    active = {kind = parts[2], elapsed = 0}
    if active.kind == "dress-initial-pages" then
        local navigation = dressing.initial_navigation
        assert(#parts == 2 and stage == "initial-pages" and render_prepared and dressing.pages == 0
            and not navigation.complete and navigation.chosen_taps > 0,
            "Initial page taps must precede the first accessory read or drag")
        active.total = navigation.planned_frames
        navigation.start_frame = state.frame
    elseif active.kind == "dress-page-inputs" then
        assert(#parts == 2 and stage == "drag-dress-page" and dressing and dressing.remaining_frames > 0
            and dressing.page.frame == state.frame and dressing.page.epoch == adapter.epoch(),
            "Only the acknowledged accessory page can authorize its queued inputs")
        local settings = config.automatic_dressing
        active.items = dressing.page.items
        active.drag_total = #active.items * settings.motion.cycle_frames
        active.planned = active.drag_total + settings.next_page.hold_frames
            + settings.next_page.release_frames + settings.next_page.wait_frames
        active.total = math.min(active.planned, dressing.remaining_frames)
        active.started_items, active.interrupted_items = {}, {}
        dressing.last_input = {kind = active.kind, page = dressing.page.index, start_frame = state.frame,
            queued_drags = #active.items, planned_frames = active.planned, scheduled_frames = active.total,
            deadline_truncated = active.total < active.planned}
    elseif active.kind == "finish-dressing" then
        assert(#parts == 2 and not touch_complete and render_prepared
            and dressing.complete and state.frame == dressing.end_frame
            and (stage == "dress_deadline" or stage == "finish-dress-early" and dressing.full_detected
                or stage == "dress_page_limit" and dressing.page_flip_limit_reached),
            "Reach the accessory limit, page-flip limit, or dressing deadline before the Finish tap")
        active.total = config.stylus.hold_frames + config.stylus.release_frames
    elseif active.kind == "dress-transition" then
        assert(#parts == 2 and stage == "transition" and touch_complete and not mash_complete, "Complete the Finish tap first")
        active.total = config.timing.mash_frames
        transition = {start_frame = state.frame, mash_frames = active.total, total_frames = 0, poll_count = 0}
    elseif active.kind == "observe-frames" then
        local frames = tonumber(parts[3])
        assert(#parts == 3 and stage == "observe" and mash_complete and frames and frames > 0
            and frames == math.floor(frames), "Expected a whole observation frame count")
        active.total = frames
    else
        error("Unknown Stage 4 input action: " .. tostring(parts[2]))
    end
end

local function page_touch()
    local settings, elapsed = config.automatic_dressing, active.elapsed
    local motion = settings.motion
    -- The final released frames belong to the window, even if it cuts a drag short.
    local released_tail = dressing.remaining_frames <= motion.release_frames
    if elapsed < active.drag_total then
        local index = math.floor(elapsed / motion.cycle_frames) + 1
        local phase = elapsed % motion.cycle_frames
        local item = active.items[index]
        if phase < motion.pickup_frames + motion.drag_frames and not released_tail then
            if phase == 0 then
                active.started_items[index] = true
                dressing.attempted_drags = dressing.attempted_drags + 1
            end
            local fraction = phase < motion.pickup_frames and 0
                or (phase - motion.pickup_frames + 1) / motion.drag_frames
            stylus.set({x = math.floor(item.x + (settings.destination.x - item.x) * fraction + 0.5),
                y = math.floor(item.y + (settings.destination.y - item.y) * fraction + 0.5), touch = true})
            state.stylus_touch = true
            return
        elseif released_tail and phase < motion.pickup_frames + motion.drag_frames
            and active.started_items[index] then
            active.interrupted_items[index] = true
        end
    else
        local phase = elapsed - active.drag_total
        if phase < settings.next_page.hold_frames and not released_tail
            and not dressing.last_input.deadline_truncated then
            if phase == 0 then
                dressing.page_taps = dressing.page_taps + 1
                dressing.collection_page_taps = dressing.collection_page_taps + 1
            end
            stylus.set({x = settings.next_page.x, y = settings.next_page.y, touch = true})
            state.stylus_touch = true
            return
        end
    end
    stylus.set({touch = false})
    state.stylus_touch = false
end

local function before_frame()
    state.frame = emu.framecount()
    if not active then
        local parts = mailbox:read_command(state.command_id)
        if parts then
            local ok, message = pcall(start, parts)
            if not ok then stop(tostring(message)); return end
        end
    end
    if not active then release(); return end
    local selected
    if active.kind == "dress-initial-pages" then
        local page = config.automatic_dressing.next_page
        local cycle = page.hold_frames + page.release_frames + page.wait_frames
        local phase = active.elapsed % cycle
        local touched = phase < page.hold_frames
        if phase == 0 then
            dressing.initial_navigation.executed_taps = dressing.initial_navigation.executed_taps + 1
            dressing.page_taps = dressing.page_taps + 1
        end
        stylus.set({x = page.x, y = page.y, touch = touched})
        state.stylus_touch = touched
    elseif active.kind == "dress-page-inputs" then
        page_touch()
    elseif active.kind == "finish-dressing" then
        local touched = active.elapsed < config.stylus.hold_frames
        stylus.set({x = config.stylus.x, y = config.stylus.y, touch = touched})
        state.stylus_touch = touched
    elseif active.kind == "dress-transition" then
        local cycle = config.timing.mash_hold_frames + config.timing.mash_release_frames
        if active.elapsed < config.timing.mash_frames and active.elapsed % cycle < config.timing.mash_hold_frames then
            selected = "A"
        end
    else
        stylus.set({touch = false})
        state.stylus_touch = false
    end
    bridge_io.apply_buttons(selected)
    state.buttons = selected or ""
end

local function after_frame()
    state.frame = emu.framecount()
    if active then
        active.elapsed = active.elapsed + 1
        if active.kind == "dress-page-inputs" then
            local cycle = config.automatic_dressing.motion.cycle_frames
            if active.elapsed <= active.drag_total and active.elapsed % cycle == 0 then
                local index = active.elapsed / cycle
                if active.started_items[index] and not active.interrupted_items[index] then
                    dressing.completed_drag_inputs = dressing.completed_drag_inputs + 1
                end
            end
        end
        if dressing and (active.kind == "dress-initial-pages"
            or active.kind == "dress-page-inputs") then
            dressing.elapsed_frames = state.frame - dressing.start_frame
            dressing.remaining_frames = dressing.budget_frames - dressing.elapsed_frames
            assert(dressing.remaining_frames >= 0, "The dressing countdown exceeded its frame budget")
            if dressing.remaining_frames == 0 then
                dressing.complete, dressing.end_frame, stage = true, state.frame, "dress_deadline"
                dressing.end_reason = "deadline"
                dressing.elapsed_game_seconds = dressing.elapsed_frames / config.automatic_dressing.frame_rate
                if dressing.last_input then
                    dressing.last_input.end_frame, dressing.last_input.executed_frames = state.frame, active.elapsed
                end
                stop(nil, "dress_countdown_complete")
                return
            end
        end
        if active.kind == "dress-initial-pages" and active.elapsed >= active.total then
            local navigation = dressing.initial_navigation
            assert(navigation.executed_taps == navigation.chosen_taps, "The initial page count changed during navigation")
            navigation.complete, navigation.end_frame, navigation.executed_frames = true, state.frame, active.elapsed
            stage = "dressing"
            stop(nil, "dress_initial_pages_complete")
            return
        elseif active.kind == "dress-page-inputs" and active.elapsed >= active.total then
            dressing.last_input.end_frame, dressing.last_input.executed_frames = state.frame, active.elapsed
            if dressing.page_flip_limit and dressing.collection_page_taps >= dressing.page_flip_limit then
                dressing.complete, dressing.end_frame, stage = true, state.frame, "dress_page_limit"
                dressing.page_flip_limit_reached, dressing.end_reason = true, "page_flip_limit"
                dressing.elapsed_game_seconds = dressing.elapsed_frames / config.automatic_dressing.frame_rate
                stop(nil, "dress_page_flip_limit_reached")
                return
            end
            stage = "dressing"
            stop(nil, "dress_page_complete")
            return
        elseif active.kind == "finish-dressing" and active.elapsed >= active.total then
            touch_complete, stage = true, "transition"
            stop(nil, "finish_tap_complete")
            return
        elseif active.kind == "dress-transition" and active.elapsed >= active.total then
            mash_complete, stage = true, "observe"
            transition.mash_end_frame, transition.total_frames = state.frame, state.frame - transition.start_frame
            stop(nil, "A_mash_complete")
            return
        elseif active.kind == "observe-frames" and active.elapsed >= active.total then
            transition.total_frames = state.frame - transition.start_frame
            stop(nil, "observation_interval_complete")
            return
        end
    end
    if state.frame % 15 == 0 then publish() end
end

return {definitions = definitions, snapshot = snapshot, before = before_frame, after = after_frame,
        finish = finish, checkpoint = function(action, args)
            idle, state.frame = true, adapter.frame()
            local ok, result = pcall(dispatch, action, args)
            idle = false
            if not ok then
                local message = tostring(result)
                if pending then latest, pending = engine:cancel(message), nil end
                finish(message)
                stage = "stopped"
                return {frame = adapter.frame(), epoch = adapter.epoch(), checkpoint = latest, error = message}
            end
            return result
        end}
