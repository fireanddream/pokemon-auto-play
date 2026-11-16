-- Screenshot-only strip and player-feedback helpers.
local M = {}

local function color(raw, x, y)
    local position = 13 + (y * 256 + x) * 4
    return raw:byte(position, position + 2)
end

function M.bar(raw)
    assert(#raw == 393227 and raw:byte(1) == 255 and raw:byte(2) == 254,
        "Expected a true-color 256 by 384 GD framebuffer")
    local profiles = {
        {{56,255,134},{190,255,255},{56,255,134},{24,207,113}},
        {{207,255,134},{255,255,223},{207,255,134},{174,223,97}},
        {{24,199,24},{97,215,97},{24,199,24},{16,158,16}},
    }
    local empty = {{56,65,81},{134,142,158},{134,142,158},{134,142,158}}
    local function matches(column, profile)
        local numerator, denominator = 0, 0
        for row = 1, 4 do
            for channel = 1, 3 do
                local dark = 255 - profile[row][channel]
                numerator, denominator = numerator + dark * (255 - column[row][channel]), denominator + dark * dark
            end
        end
        local scale = numerator / denominator
        if scale < 0.35 or scale > 1.02 then return false end
        for row = 1, 4 do
            for channel = 1, 3 do
                if math.abs(column[row][channel] - (255 - (255 - profile[row][channel]) * scale)) > 8 then return false end
            end
        end
        return true
    end
    local first, last, first_empty = 256, -1, 248
    for x = 8, 247 do
        local column, filled = {}, false
        for y = 155, 158 do column[#column + 1] = {color(raw, x, y)} end
        for _, profile in ipairs(profiles) do
            if matches(column, profile) then filled = true; break end
        end
        if filled then
            first, last = math.min(first, x), math.max(last, x)
        elseif matches(column, empty) then
            first_empty = math.min(first_empty, x)
        end
    end
    local found = last >= first and last < first_empty
    local white = not found
    if white then
        for y = 0, 160, 32 do
            for x = 0, 224, 32 do
                local r, g, b = color(raw, x, y)
                if r < 248 or g < 248 or b < 248 then white = false end
            end
        end
    end
    return {found = found, first_x = first, last_x = found and first_empty - 1 or -1, white_flash = white,
        position_bounds = found and {last, first_empty - 1} or nil, source = "four_row_fill_and_empty_profiles"}
end

function M.feedback(raw, center)
    -- The rainbow bubble's broad color groups survive different caption lettering.
    local minimum, maximum = math.max(0, center - 36), math.min(255, center + 36)
    local first_x, first_y, rows
    rows = 0
    for y = 0, 49 do
        local cyan, yellow, pink, row_x = 0, 0, 0, nil
        for x = minimum, maximum do
            local r, g, b = color(raw, x, y)
            if r <= 128 and g >= 176 and b >= 176 then
                cyan, row_x = cyan + 1, row_x or x
            elseif r >= 208 and g >= 176 and b <= 128 then
                yellow, row_x = yellow + 1, row_x or x
            elseif r >= 208 and b >= 176 and g <= 176 then
                pink, row_x = pink + 1, row_x or x
            end
        end
        if cyan >= 3 and yellow >= 3 and pink >= 3 then
            first_x, first_y = first_x or row_x, first_y or y
            rows = rows + 1
            if rows >= 2 then
                return {label = "Excellent", x = first_x, y = first_y, matched_rows = rows,
                    source = "excellent_bubble_palette"}
            end
        end
    end
end

return M
