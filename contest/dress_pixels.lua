-- One center sample per accessory cell and one rank-dependent heart/pearl sample.
local M = {}

local function rgb(raw, x, y)
    local position = 13 + (y * 256 + x) * 4
    return raw:byte(position, position + 2)
end

local function same(r, g, b, expected)
    return r == expected[1] and g == expected[2] and b == expected[3]
end

function M.read_page(raw, settings, rank)
    assert(type(raw) == "string" and #raw == 393227
        and raw:byte(1) == 255 and raw:byte(2) == 254
        and raw:byte(3) == 1 and raw:byte(4) == 0
        and raw:byte(5) == 1 and raw:byte(6) == 128 and raw:byte(7) == 1,
        "Expected a native true-color 256 by 384 GD framebuffer")
    local last = assert(settings.full_checks[rank], "The dress-up rank has no accessory limit")
    local heart = last.heart_rgb or settings.indicator_rgb.heart
    local pearl = last.pearl_rgb or settings.indicator_rgb.pearl
    local r, g, b = rgb(raw, last.x, last.y)
    local label = same(r, g, b, heart) and "heart"
        or same(r, g, b, pearl) and "pearl" or "unreadable"
    local result = {source = "fresh_zero_frame_accessory_grid_and_rank_marker",
        direct_read_frames = 0, memory_proven = false, rank = rank, limit = last.limit,
        grid_size = settings.grid_size, readable = label ~= "unreadable", full = label == "heart",
        last_indicator = {x = last.x, y = last.y, rgb = {r, g, b}, label = label,
            heart_rgb = heart, pearl_rgb = pearl},
        background_rgb = settings.background_rgb, cells = {}, items = {}}
    for index, center in ipairs(settings.centers) do
        r, g, b = rgb(raw, center.x, center.y + 192)
        local empty = same(r, g, b, settings.background_rgb)
        result.cells[index] = {row = center.row, column = center.column,
            x = center.x, y = center.y, rgb = {r, g, b}, empty = empty}
        if not empty then
            result.items[#result.items + 1] = {x = center.x, y = center.y,
                row = center.row, column = center.column}
        end
    end
    return result
end

return M
