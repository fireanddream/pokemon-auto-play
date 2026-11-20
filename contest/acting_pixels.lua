-- Native Acting judge cells, move fills/hearts, and player panels; no emulator calls.
local M = {}
local empty_rows = {"abbaa", "bbbba", "bbbba", "bbbba", "abbaa"}
local star_rows = {"ab*aa", "b***a", "*****", "b***a", "ab*aa"}
local background = {a = {81, 81, 81}, b = {81, 81, 215}}

local function validate(raw)
    assert(type(raw) == "string" and raw:byte(1) == 255 and raw:byte(2) == 254
        and raw:byte(3) == 1 and raw:byte(4) == 0 and raw:byte(7) == 1,
        "Expected a native true-color 256-pixel-wide GD framebuffer")
    local height = raw:byte(5) * 256 + raw:byte(6)
    assert((height == 192 or height == 384) and #raw == 11 + 256 * height * 4,
        "Expected a complete native top-screen or stacked GD framebuffer")
    return height
end

local function rgb(raw, x, y)
    local position = 13 + (y * 256 + x) * 4
    return raw:byte(position, position + 2)
end

local function same(r, g, b, expected)
    return r == expected[1] and g == expected[2] and b == expected[3]
end

local function star_color(r, g, b)
    -- Gold/brown and bright white are distinct from every empty-cell pixel.
    -- Accept highlight shades only inside the complete observed star silhouette.
    return (r >= 180 and g >= 120 and b <= 130 and r - b >= 70 and g - b >= 50)
        or (r >= 230 and g >= 230 and b >= 200)
end

local function matches(raw, x, rows)
    for dy = 0, 4 do
        for dx = 0, 4 do
            local code = rows[dy + 1]:sub(dx + 1, dx + 1)
            local r, g, b = rgb(raw, x + dx, 8 + dy)
            if code == "*" then
                if not star_color(r, g, b) then return false end
            elseif not same(r, g, b, background[code]) then
                return false
            end
        end
    end
    return true
end

function M.read_stars(raw)
    validate(raw)
    local result = {source = "native_five_cell_star_shapes", readable = true, all_empty = true,
        counts = {}, judges = {}, memory_proven = false, direct_read_frames = 0,
        bounds = {80, 0, 176, 50}}
    for judge = 1, 3 do
        local item = {judge = judge, stars = 0, readable = true, cells = {}}
        local empty_seen = false
        for cell = 1, 5 do
            local x = 84 + (judge - 1) * 32 + (cell - 1) * 5
            local label
            if matches(raw, x, empty_rows) then
                label, empty_seen = "empty", true
            elseif matches(raw, x, star_rows) then
                label = "star"
                item.stars = item.stars + 1
                if empty_seen then item.readable = false end
            else
                label, item.readable = "unreadable", false
            end
            result.all_empty = result.all_empty and label == "empty"
            item.cells[cell] = {cell = cell, x = x, y = 8, label = label}
        end
        result.judges[judge] = item
        result.counts[judge] = item.readable and item.stars or -1
        result.readable = result.readable and item.readable
    end
    return result
end

local move_profiles = {
    {category = "Cool", rgb = {255, 41, 0}},
    {category = "Beauty", rgb = {41, 181, 255}},
    {category = "Cute", rgb = {255, 99, 206}},
    {category = "Smart", rgb = {66, 181, 41}},
    {category = "Tough", rgb = {247, 206, 0}},
}
local move_boxes = {{16, 216, 112, 284}, {144, 216, 240, 284},
                    {16, 308, 112, 376}, {144, 308, 240, 376}}
local heart_positions = {{59, 240}, {187, 240}, {59, 336}, {187, 336}}
local heart_rows = {".dd.dd..", "dwydrrd.", "dyorrrd.", "dorrrrd.",
                    ".drrrd..", "..drd...", "...d...."}
local heart_colors = {d = {181, 0, 0}, w = {255, 255, 255}, y = {255, 132, 24},
                      o = {255, 74, 24}, r = {255, 24, 24}}

local function heart_matches(raw, x, y, fill, filled)
    for dy = 0, 6 do
        for dx = 0, 7 do
            local code = filled and heart_rows[dy + 1]:sub(dx + 1, dx + 1) or "."
            local r, g, b = rgb(raw, x + dx, y + dy)
            if not same(r, g, b, code == "." and fill or heart_colors[code]) then return false end
        end
    end
    return true
end

local function read_hearts(raw, slot, fill)
    local result = {readable = true, hearts = 0, cells = {}, source = "native_raw_heart_shapes"}
    local origin = heart_positions[slot]
    local empty_seen = false
    -- The six 8-by-7 cells follow the type capsule, outside all caption lettering.
    for cell = 1, 6 do
        local x, y = origin[1] + (cell - 1) * 8, origin[2]
        local label
        if heart_matches(raw, x, y, fill, false) then
            label, empty_seen = "empty", true
        elseif heart_matches(raw, x, y, fill, true) then
            label = "heart"
            result.hearts = result.hearts + 1
            if empty_seen then result.readable = false end
        else
            label, result.readable = "unreadable", false
        end
        result.cells[cell] = {cell = cell, x = x, y = y, label = label}
    end
    if not result.readable then result.hearts = -1 end
    return result
end

function M.read_move_categories(raw, unavailable_move_slots)
    assert(validate(raw) == 384, "Move types and hearts require a native stacked 256 by 384 GD framebuffer")
    local blocked = {}
    for _, slot in ipairs(unavailable_move_slots or {}) do blocked[slot] = true end
    local result = {source = "native_move_button_fill_rgb_and_raw_heart_shapes", readable = true,
        categories_readable = true, hearts_readable = true,
        categories = {}, hearts = {}, slots = {}, total_pixels = 0, matched_pixels = 0,
        recognized_move_slots = {}, unrecognized_move_slots = {}, warnings = {},
        memory_proven = false, direct_read_frames = 0, unavailable_move_slots = unavailable_move_slots}
    for slot, box in ipairs(move_boxes) do
        local item = {slot = slot, bounds = box, category = "unreadable", hearts = -1, readable = false,
            category_readable = false, hearts_readable = false,
            probes = {}, matched_pixels = 0}
        -- Keep both fill columns outside the move names, type labels, and heart icons.
        if blocked[slot] then
            item.category, item.readable, item.unavailable = "unavailable", true, true
            item.category_readable, item.hearts_readable = true, true
        else
            for _, x in ipairs({box[1] + 2, box[3] - 4}) do
                for _, y in ipairs({box[2] + 4, box[2] + 28, box[4] - 4}) do
                    item.probes[#item.probes + 1] = {x, y, rgb(raw, x, y)}
                end
            end
            for _, profile in ipairs(move_profiles) do
                local matched = 0
                for _, point in ipairs(item.probes) do
                    if same(point[3], point[4], point[5], profile.rgb) then matched = matched + 1 end
                end
                item.matched_pixels = math.max(item.matched_pixels, matched)
                if matched == #item.probes then
                    item.category, item.category_readable = profile.category, true
                    item.raw_hearts = read_hearts(raw, slot, profile.rgb)
                    item.hearts, item.hearts_readable = item.raw_hearts.hearts, item.raw_hearts.readable
                end
            end
            item.readable = item.category_readable and item.hearts_readable
            if item.readable then
                result.recognized_move_slots[#result.recognized_move_slots + 1] = slot
            else
                result.unrecognized_move_slots[#result.unrecognized_move_slots + 1] = slot
                local fields = {}
                if not item.category_readable then fields[#fields + 1] = "type" end
                if not item.hearts_readable then fields[#fields + 1] = "raw hearts" end
                result.warnings[#result.warnings + 1] = string.format(
                    "Acting: WARN - Move slot %d has unrecognized %s; excluding it from scored choices.",
                    slot, table.concat(fields, " and "))
            end
        end
        result.slots[slot], result.categories[slot] = item, item.category
        result.hearts[slot] = item.hearts
        result.total_pixels = result.total_pixels + #item.probes
        result.matched_pixels = result.matched_pixels + item.matched_pixels
        result.categories_readable = result.categories_readable and item.category_readable
        result.hearts_readable = result.hearts_readable and item.hearts_readable
        result.readable = result.readable and item.readable
    end
    return result
end

local panel_profiles = {
    {label = "player", colors = {{255, 239, 134}, {255, 207, 24}}},
    {label = "opponent", colors = {{0, 207, 255}, {105, 215, 255}}},
}

function M.read_acting_order(raw)
    validate(raw)
    local result = {source = "native_player_panel_fill_rgb", readable = true, rows = {},
        player_count = 0, total_pixels = 24, matched_pixels = 0,
        memory_proven = false, direct_read_frames = 0, bounds = {0, 0, 80, 192}}
    local position
    for row = 1, 4 do
        local item = {position = row, label = "unreadable", readable = false, probes = {}, matched_pixels = 0}
        -- Lower panel fills are clear of names, weather icons, and every text line.
        for _, x in ipairs({4, 40, 72}) do
            for _, dy in ipairs({36, 42}) do
                item.probes[#item.probes + 1] = {x, (row - 1) * 48 + dy, rgb(raw, x, (row - 1) * 48 + dy)}
            end
        end
        for _, profile in ipairs(panel_profiles) do
            local matched = 0
            for index, point in ipairs(item.probes) do
                if same(point[3], point[4], point[5], profile.colors[(index - 1) % 2 + 1]) then
                    matched = matched + 1
                end
            end
            item.matched_pixels = math.max(item.matched_pixels, matched)
            if matched == #item.probes then item.label, item.readable = profile.label, true end
        end
        if item.label == "player" then position, result.player_count = row, result.player_count + 1 end
        result.rows[row] = item
        result.matched_pixels = result.matched_pixels + item.matched_pixels
        result.readable = result.readable and item.readable
    end
    result.readable = result.readable and result.player_count == 1
    if result.readable then result.position = position end
    return result
end

function M.read_move_buttons(raw, unavailable_move_slots, previous_move)
    assert(validate(raw) == 384, "Move-menu readiness requires a native stacked GD framebuffer")
    assert(previous_move == nil or type(previous_move) == "number" and previous_move >= 1 and previous_move <= 4
        and previous_move == math.floor(previous_move), "Use no previous move at entry, or a valid slot between rounds")
    local blocked = {}
    if previous_move then blocked[previous_move] = true end
    for _, slot in ipairs(unavailable_move_slots or {}) do blocked[slot] = true end
    local result = {source = "native_selectable_move_button_fills", readable = true,
        slots = {}, available_count = 0, direct_read_frames = 0}
    for slot, box in ipairs(move_boxes) do
        local item = {slot = slot, unavailable = blocked[slot] == true, readable = true, probes = {}}
        if not item.unavailable then
            result.available_count = result.available_count + 1
            -- Lower fill probes establish the menu, not the cached move score/name data.
            for _, x in ipairs({box[1] + 2, box[3] - 4}) do
                for _, y in ipairs({box[2] + 28, box[4] - 4}) do
                    item.probes[#item.probes + 1] = {x, y, rgb(raw, x, y)}
                end
            end
            item.readable = false
            for _, profile in ipairs(move_profiles) do
                local matched = true
                for _, point in ipairs(item.probes) do
                    matched = matched and same(point[3], point[4], point[5], profile.rgb)
                end
                if matched then item.readable = true; break end
            end
        end
        result.slots[slot] = item
        result.readable = result.readable and item.readable
    end
    result.readable = result.readable and result.available_count > 0
    return result
end

function M.read_round_readiness(raw, unavailable_move_slots, previous_move, stars, acting_order)
    stars = stars or M.read_stars(raw)
    acting_order = acting_order or M.read_acting_order(raw)
    local moves = M.read_move_buttons(raw, unavailable_move_slots, previous_move)
    local entry = previous_move == nil
    return {ready = stars.readable and (not entry or stars.all_empty == true) and acting_order.readable and moves.readable,
        entry = entry,
        stars = stars, acting_order = acting_order, move_buttons = moves,
        source = "fresh_judges_order_and_selectable_move_menu", direct_read_frames = 0}
end

function M.match_points(raw, points)
    local height = validate(raw)
    assert(type(points) == "table" and #points > 0, "A reviewed screen reference is required")
    local matched = 0
    for _, point in ipairs(points) do
        assert(point[1] >= 0 and point[1] < 256 and point[2] >= 0 and point[2] < height,
            "The screen reference is outside the native framebuffer")
        local r, g, b = rgb(raw, point[1], point[2])
        if r == point[3] and g == point[4] and b == point[5] then matched = matched + 1 end
    end
    return {confirmed = matched == #points, matched_pixels = matched, expected_pixels = #points,
        source = "fresh_native_pixels_against_reviewed_reference", memory_proven = false}
end

return M
