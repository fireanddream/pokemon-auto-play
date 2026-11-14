-- Native CPUJ Acting star cells and move fills; no emulator calls or frame advancement.
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

function M.choose_judge(counts)
    assert(type(counts) == "table" and #counts == 3, "Three readable judge counts are required")
    for _, count in ipairs(counts) do
        assert(type(count) == "number" and count >= 0 and count <= 5 and count == math.floor(count),
            "Judge stars must be whole counts from zero through five")
    end
    for index, count in ipairs(counts) do if count == 4 then return index end end
    local selected = 1
    for index = 2, 3 do if counts[index] < counts[selected] then selected = index end end
    return selected
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
    if result.readable then result.selected_judge = M.choose_judge(result.counts) end
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

function M.read_move_categories(raw)
    assert(validate(raw) == 384, "Move categories require a native stacked 256 by 384 GD framebuffer")
    local result = {source = "native_move_button_fill_rgb", readable = true,
        categories = {}, slots = {}, total_pixels = 0, matched_pixels = 0,
        memory_proven = false, direct_read_frames = 0}
    for slot, box in ipairs(move_boxes) do
        local item = {slot = slot, bounds = box, category = "unreadable", readable = false,
            probes = {}, matched_pixels = 0}
        -- Keep both columns outside the move names, category labels, and heart text.
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
                item.category, item.readable = profile.category, true
            end
        end
        result.slots[slot], result.categories[slot] = item, item.category
        result.total_pixels = result.total_pixels + #item.probes
        result.matched_pixels = result.matched_pixels + item.matched_pixels
        result.readable = result.readable and item.readable
    end
    return result
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
