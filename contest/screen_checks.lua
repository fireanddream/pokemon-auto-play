-- Compact profiles from accepted native screenshots; no memory-proven menu claims.
local directory = assert(debug.getinfo(1, "S").source:sub(2):gsub("\\", "/"):match("^(.*)/"))
local pixels = dofile(directory .. "/acting_pixels.lua")
local slot_counts = {Normal = 5, Super = 10, Hyper = 15, Master = 20}
local empty_slot_rows = {"abbba", "bcdcb", "bdddb", "bcdcb", "abbba"}
local empty_slot_rgb = {a = {132,82,198}, b = {8,189,239},
    c = {90,231,255}, d = {255,214,255}}
local profiles = {
    loaded_overworld = {reference = "Pocket Monsters - Platinum (Japan)__583.png", points = {
        -- Six central receptionist pixels, then six player pixels; exact native RGB.
        {124,54,158,93,109}, {130,54,158,93,109}, {124,60,231,134,158},
        {130,60,158,93,109}, {124,66,142,174,239}, {130,66,142,174,239},
        {124,78,223,109,125}, {130,78,223,109,125}, {124,84,190,60,77},
        {130,84,190,60,77}, {124,94,207,207,223}, {130,94,207,207,223},
    }},
}

local function read_empty_slots(raw, rank)
    local limit = assert(slot_counts[rank], "A supported contest rank is required for dress-up entry")
    local result = {confirmed = true, rank = rank, expected_empty_slots = limit,
        empty_slot_count = 0, expected_pixels = limit * 25, matched_pixels = 0,
        memory_proven = false, slots = {},
        template = {rows = empty_slot_rows, rgb = empty_slot_rgb}}
    for slot = 1, 20 do
        local x = 46 + ((slot - 1) % 10) * 18
        local y = 112 + math.floor((slot - 1) / 10) * 18
        local points = {}
        for dy, row in ipairs(empty_slot_rows) do
            for dx = 1, #row do
                local color = empty_slot_rgb[row:sub(dx, dx)]
                points[#points + 1] = {x + dx - 3, y + dy - 3, color[1], color[2], color[3]}
            end
        end
        local checked = pixels.match_points(raw, points)
        local required = slot <= limit
        result.slots[slot] = {slot = slot, x = x, y = y, expected_empty = required,
            empty = checked.confirmed, matched_pixels = checked.matched_pixels}
        if checked.confirmed then result.empty_slot_count = result.empty_slot_count + 1 end
        if required then
            result.matched_pixels = result.matched_pixels + checked.matched_pixels
            result.confirmed = result.confirmed and checked.confirmed
        end
    end
    result.confirmed = result.confirmed and result.empty_slot_count == limit
    return result
end

local M = {}
function M.read(raw, expected, rank)
    local result, reference
    if expected == "dress_up_entry" then
        result, reference = read_empty_slots(raw, rank), "native_top_screen_empty_accessory_slots"
    else
        local profile = assert(profiles[expected], "No reviewed profile for this boundary")
        result, reference = pixels.match_points(raw, profile.points), profile.reference
        result.points = profile.points
    end
    result.source, result.expected, result.reference = "local_reference_pixels", expected, reference
    result.direct_read_frames = 0
    return result
end

function M.post_acting(raw, overworld)
    assert(type(overworld) == "table" and type(overworld.player_points) == "table" and #overworld.player_points > 0
        and type(overworld.dialogue_top) == "table" and type(overworld.dialogue_top.points) == "table"
        and #overworld.dialogue_top.points > 0, "Use the accepted startup position and closed-dialogue tip reference")
    local position = pixels.match_points(raw, overworld.player_points)
    local dialogue_closed = pixels.match_points(raw, overworld.dialogue_top.points)
    dialogue_closed.bounds = overworld.dialogue_top.bounds
    return {ready = position.confirmed and dialogue_closed.confirmed,
        position = position, dialogue_closed = dialogue_closed, reference = overworld.reference,
        source = "accepted_startup_player_and_reviewed_closed_dialogue_top",
        direct_read_frames = 0, memory_proven = false}
end
return M
