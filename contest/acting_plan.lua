-- Pure move/judge and frame-counted B-input planning for both Acting callers.
local M = {}

local categories = {Cool = true, Beauty = true, Cute = true, Smart = true, Tough = true}

local function move_slot(value)
    assert(type(value) == "number" and value >= 1 and value <= 4 and value == math.floor(value),
        "Move slots must be whole numbers from one through four")
    return math.floor((value - 1) / 2), (value - 1) % 2
end

local function known_hearts(value)
    return type(value) == "number" and value >= 0 and value <= 6 and value == math.floor(value)
end

local function eligible_moves(config, number, move_cursor, moves)
    local last_move = number > 1 and move_cursor or nil
    assert(type(moves) == "table" and type(moves.categories) == "table" and type(moves.hearts) == "table",
        "Supply move type and raw heart observations from the Acting-entry capture")
    assert(config.target and categories[config.target.category], "A supported contest category is required")
    local blocked, available, legal, recognized, eligible = {}, 0, {}, {}, {}
    for _, slot in ipairs(config.unavailable_move_slots or {}) do
        move_slot(slot)
        blocked[slot] = true
    end
    for slot = 1, 4 do
        if not blocked[slot] then
            available = available + 1
            if slot ~= last_move then
                legal[#legal + 1] = slot
                if categories[moves.categories[slot]] and known_hearts(moves.hearts[slot]) then
                    recognized[#recognized + 1] = slot
                end
            end
        end
    end
    assert(available >= 2, "The first Pokemon needs at least two available moves for Acting")
    for _, slot in ipairs(recognized) do
        if moves.hearts[slot] > 0 or #recognized == 1 then eligible[#eligible + 1] = slot end
    end
    return eligible, legal, recognized, last_move
end

local function strongest(moves, eligible, category)
    local selected
    for _, slot in ipairs(eligible) do
        if (not category or moves.categories[slot] == category)
            and (not selected or moves.hearts[slot] > moves.hearts[selected]) then selected = slot end
    end
    return selected
end

function M.mash_steps(timing, frames)
    assert(type(frames) == "number" and frames >= 1 and frames == math.floor(frames),
        "B tapping requires a positive whole emulator-frame count")
    local steps, cycle = {}, timing.mash_hold_frames + timing.mash_release_frames
    for _ = 1, math.floor(frames / cycle) do
        steps[#steps + 1] = "B:" .. timing.mash_hold_frames .. ":" .. timing.mash_release_frames
    end
    local tail = frames % cycle
    if tail > timing.mash_hold_frames then
        steps[#steps + 1] = "B:" .. timing.mash_hold_frames .. ":" .. (tail - timing.mash_hold_frames)
    elseif tail > 0 then
        steps[#steps + 1] = "wait:" .. tail
    end
    return steps
end

function M.round_pause(config, number, frame, epoch, ready)
    assert(number >= 2 and number <= 4 and number == math.floor(number),
        "Readiness delays apply only between Acting rounds")
    local values = config.between_round_readiness
    local phase = ready and "settle" or "retry"
    local frames = values[phase .. "_frames"]
    return {round = number, phase = phase, game_seconds = values[phase .. "_game_seconds"],
        frames = frames, steps = M.mash_steps(config.timing, frames),
        start_frame = frame, end_frame = frame + frames, epoch = epoch}
end

function M.finish_pause(plan, state, frame, epoch)
    assert(plan and state.command_done and state.error == "" and state.stop_reason == "combo_complete"
        and state.command_start_frame == plan.start_frame and frame == plan.end_frame and epoch == plan.epoch
        and #state.combo_history == #plan.steps and state.buttons == "" and not state.stylus_touch,
        "Complete the configured released B segment before another readiness check or move selection")
end

function M.round(config, number, move_cursor, judge_cursor, frame, stars, moves, acting_order)
    assert(type(stars) == "table" and stars.readable == true and type(stars.counts) == "table" and #stars.counts == 3,
        "Plan each round from three readable judge counts")
    for _, count in ipairs(stars.counts) do
        assert(type(count) == "number" and count >= 0 and count <= 5 and count == math.floor(count),
            "Judge stars must be whole counts from zero through five")
    end
    assert(type(acting_order) == "table" and acting_order.readable == true
        and type(acting_order.position) == "number" and acting_order.position >= 1 and acting_order.position <= 4
        and acting_order.position == math.floor(acting_order.position), "A readable pixel-based acting position is required")
    assert(type(number) == "number" and number >= 1 and number <= 4 and number == math.floor(number),
        "An Acting round from one through four is required")
    assert(type(judge_cursor) == "number" and judge_cursor >= 1 and judge_cursor <= 3
        and judge_cursor == math.floor(judge_cursor), "A judge cursor from one through three is required")
    local previous_row, previous_column = move_slot(move_cursor)
    local eligible, legal, recognized, last_move = eligible_moves(config, number, move_cursor, moves)
    local fallback, override = #recognized == 0, false
    local move = fallback and legal[1] or strongest(moves, eligible)
    if not move then
        return {round = number, selectable = false, acting_position = acting_order.position,
            previous_move = move_cursor, excluded_previous_move = last_move, legal_move_slots = legal,
            selection_error = "Multiple recognized zero-heart moves remain; none is the sole available option"}
    end
    local judge = 1
    for index = 2, 3 do if stars.counts[index] < stars.counts[judge] then judge = index end end
    local matching = not fallback and strongest(moves, eligible, config.target.category)
    if acting_order.position <= 2 and matching then
        for index, count in ipairs(stars.counts) do
            if count == 4 then judge, move, override = index, matching, true; break end
        end
    end
    local row, column = move_slot(move)
    local menu, steps = {}, {}
    if column ~= previous_column then menu[#menu + 1] = column > previous_column and "right" or "left" end
    if row ~= previous_row then menu[#menu + 1] = row > previous_row and "down" or "up" end
    menu[#menu + 1] = "A"
    for _ = 1, math.abs(judge - judge_cursor) do
        menu[#menu + 1] = judge > judge_cursor and "right" or "left"
    end
    menu[#menu + 1] = "A"
    local timing = config.timing
    for _, button in ipairs(menu) do
        steps[#steps + 1] = button .. ":" .. timing.hold_frames .. ":" .. timing.release_frames
    end
    local mash = config.rounds[number].mash_frames
    for _, step in ipairs(M.mash_steps(timing, mash)) do steps[#steps + 1] = step end
    local menu_frames = #menu * (timing.hold_frames + timing.release_frames)
    local total = menu_frames + mash
    if number == 4 then
        -- Open the result-read menu using profile timings, independent of move-input overrides.
        steps[#steps + 1] = config.memory_x_input
        total = total + config.memory_x_frames
    end
    return {round = number, selectable = true, move = move, judge = judge, previous_move = move_cursor,
        excluded_previous_move = last_move, acting_position = acting_order.position,
        eligible_move_slots = fallback and {move} or eligible, recognized_move_slots = recognized,
        matching_category_override = override, unrecognized_fallback = fallback,
        move_category = categories[moves.categories[move]] and moves.categories[move] or "unreadable",
        move_hearts = known_hearts(moves.hearts[move]) and moves.hearts[move] or -1,
        zero_heart_only_option = not fallback and moves.hearts[move] == 0 and #recognized == 1,
        move_policy = fallback and "first_available_unrecognized_fallback"
            or override and "four_star_category_match_most_hearts" or "most_raw_hearts",
        warnings = fallback and {string.format(
            "Acting: WARN - No recognized legal move remains; using first available move slot %d.", move)} or {},
        judge_policy = override and "early_four_stars_matching" or "first_fewest_stars",
        previous_judge = judge_cursor, menu_buttons = menu, steps = steps,
        start_frame = frame, end_frame = frame + total,
        menu_frames = menu_frames, mash_frames = mash, total_frames = total,
        judge_confirmed_before_mashing = true}
end

return M
