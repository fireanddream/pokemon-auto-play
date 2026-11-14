-- Screenshot-only strip, cue, and player-feedback helpers.
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

local function cue_color(r, g, b)
    if r >= 192 and g >= 144 and b < 96 then return "A" end
    if g >= 128 and g >= r + 40 and g >= b + 16 then return "Y" end
    if r >= 48 and b >= 128 and b >= r + 32 and b >= g + 8 then return "B" end
    if r >= 192 and r >= g + 80 and r >= b + 40 then return "X" end
end

function M.cues(raw)
    local found = {}
    for _, button in ipairs({"A", "B", "X", "Y"}) do
        local first, last
        local function complete()
            if first and last - first + 1 == 14 then
                found[#found + 1] = {button = button, x = math.floor((first + last + 1) / 2),
                    left = first, right = last}
            end
            first, last = nil, nil
        end
        for x = 9, 126 do
            local count = 0
            for y = 170, 177 do
                if cue_color(color(raw, x, y)) == button then count = count + 1 end
            end
            if count >= 2 then
                if last and x ~= last + 1 then complete() end
                first, last = first or x, x
            elseif first then
                complete()
            end
        end
        complete()
    end
    table.sort(found, function(a, b) return a.x < b.x end)
    return found
end

function M.feedback(raw, templates, center)
    for _, template in ipairs(templates) do
        local probes = {template.ink[1], template.ink[#template.ink], template.ink[math.floor(#template.ink / 2) + 1]}
        local minimum = math.max(0, center - math.floor(template.width / 2) - 10)
        local maximum = math.min(256 - template.width, center - math.floor(template.width / 2) + 10)
        for y = 0, 50 - template.height do
            for x = minimum, maximum do
                local r, g, b = color(raw, x + probes[1][1], y + probes[1][2])
                if r == g and g == b and r < 248 then
                    local matches = true
                    local function same(point)
                        local a, c, d = color(raw, x + point[1], y + point[2])
                        return a == r and c == r and d == r
                    end
                    for _, point in ipairs(probes) do
                        if not same(point) then matches = false; break end
                    end
                    if matches then
                        for _, point in ipairs(template.ink) do
                            if not same(point) then matches = false; break end
                        end
                    end
                    if matches then
                        for _, point in ipairs(template.empty) do
                            if same(point) then matches = false; break end
                        end
                    end
                    if matches then
                        return {label = template.label, x = x, y = y, text_intensity = r,
                            source = "exact_text_pixel_shape"}
                    end
                end
            end
        end
    end
end

return M
