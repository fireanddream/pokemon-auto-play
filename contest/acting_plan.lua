-- Pure move/judge and frame-counted B-input planning for both Acting callers.
local M = {}

local categories = {Cool = true, Beauty = true, Cute = true, Smart = true, Tough = true}

local function move_slot(value)
    assert(type(value) == "number" and value >= 1 and value <= 4 and value == math.floor(value),
        "Move slots must be whole numbers from one through four")
    return math.floor((value - 1) / 2), (value - 1) % 2
end

local function choose_move(config, number, move_cursor, stars, move_categories)
    local fallback = config.rounds[number].move
    local last_move = number > 1 and move_cursor or nil
    move_slot(fallback)
    if move_categories then
        assert(type(move_categories) == "table" and #move_categories == 4,
            "Supply all four move categories from the Acting-entry capture")
        assert(config.target and categories[config.target.category], "A supported contest category is required")
        for _, category in ipairs(move_categories) do
            assert(categories[category], "Every move slot needs a recognized contest category")
        end
        if stars.counts[stars.selected_judge] == 4 then
            for slot, category in ipairs(move_categories) do
                if slot ~= last_move and category == config.target.category then return slot, true end
            end
        end
    end
    if fallback == last_move then fallback = fallback == 1 and 2 or 1 end
    return fallback, false
end

function M.round(config, number, move_cursor, judge_cursor, frame, stars, move_categories)
    assert(type(stars) == "table" and stars.readable == true and #stars.counts == 3,
        "Plan each round from three readable judge counts")
    local judge = stars.selected_judge
    assert(type(judge) == "number" and judge >= 1 and judge <= 3 and judge == math.floor(judge),
        "A selected judge from one through three is required")
    local previous_row, previous_column = move_slot(move_cursor)
    local move, override = choose_move(config, number, move_cursor, stars, move_categories)
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
    local cycle = timing.mash_hold_frames + timing.mash_release_frames
    for _ = 1, math.floor(mash / cycle) do
        steps[#steps + 1] = "B:" .. timing.mash_hold_frames .. ":" .. timing.mash_release_frames
    end
    local tail = mash % cycle
    if tail > timing.mash_hold_frames then
        steps[#steps + 1] = "B:" .. timing.mash_hold_frames .. ":" .. (tail - timing.mash_hold_frames)
    elseif tail > 0 then
        steps[#steps + 1] = "wait:" .. tail
    end
    local menu_frames = #menu * (timing.hold_frames + timing.release_frames)
    return {round = number, move = move, judge = judge, previous_move = move_cursor,
        fallback_move = config.rounds[number].move, matching_category_override = override,
        move_category = move_categories and move_categories[move] or nil,
        move_policy = override and "four_star_category_match"
            or move ~= config.rounds[number].move and "alternate_fallback" or "round_1_2_1_2",
        previous_judge = judge_cursor, menu_buttons = menu, steps = steps,
        start_frame = frame, end_frame = frame + menu_frames + mash,
        menu_frames = menu_frames, mash_frames = mash, total_frames = menu_frames + mash,
        judge_confirmed_before_mashing = true}
end

return M
