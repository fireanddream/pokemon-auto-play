-- Pure Platinum field helpers (Lua 5.1).
-- Adapted from Real96 / SexyMalasada's supplied Platinum reference.
local M = {}
local floor = math.floor

local nature_names = {
    "Hardy", "Lonely", "Brave", "Adamant", "Naughty",
    "Bold", "Docile", "Relaxed", "Impish", "Lax",
    "Timid", "Hasty", "Serious", "Jolly", "Naive",
    "Modest", "Mild", "Quiet", "Bashful", "Rash",
    "Calm", "Gentle", "Sassy", "Careful", "Quirky",
}
local hidden_power_types = {
    "Fighting", "Flying", "Poison", "Ground", "Rock", "Bug", "Ghost", "Steel",
    "Fire", "Water", "Grass", "Electric", "Psychic", "Ice", "Dragon", "Dark",
}
local status_names = {[0] = "None", [8] = "PSN", [16] = "BRN", [32] = "FRZ", [64] = "PAR", [128] = "TOX"}

-- Zero-based tile attributes, preserving the reference's water/encounter bits.
local map_attribute_flags = {
    0, 0, 2, 2, 0, 2, 2, 0, 2, 0, 0, 2, 0, 0, 0, 0,
    3, 3, 3, 1, 1, 3, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0,
    0, 0, 3, 0, 2, 2, 0, 0, 0, 0, 3, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 2, 1, 0, 0, 0, 2, 1, 0, 0, 2, 1, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 2, 2, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
}

function M.nature_name(index)
    return nature_names[index + 1]
end

function M.status_name(value)
    if value > 0 and value < 8 then return "SLP" end
    return status_names[value]
end

function M.tile_flags(attribute)
    return map_attribute_flags[attribute + 1]
end

function M.get_bits(value, shift, width)
    return floor(value / (2 ^ shift)) % (2 ^ width)
end

function M.lcg32(value, multiplier, increment)
    -- Split multiplication so Lua 5.1 doubles retain exact 32-bit results.
    local upper = floor(multiplier / 0x10000) * (value % 0x10000)
        + floor(value / 0x10000) * (multiplier % 0x10000)
    return ((multiplier % 0x10000) * (value % 0x10000)
        + (upper % 0x10000) * 0x10000 + increment) % 0x100000000
end

function M.xor16(left, right)
    local value, mask = 0, 1
    for _ = 1, 16 do
        if left % 2 ~= right % 2 then value = value + mask end
        left, right, mask = floor(left / 2), floor(right / 2), mask * 2
    end
    return value
end

function M.shiny_xor(pid, trainer_id, secret_id)
    return M.xor16(M.xor16(trainer_id, secret_id), M.xor16(pid % 0x10000, floor(pid / 0x10000)))
end

function M.decode_ivs(packed)
    -- Platinum uses this order for both encrypted records and raw roamers.
    return {
        hp = M.get_bits(packed, 0, 5), attack = M.get_bits(packed, 5, 5),
        defense = M.get_bits(packed, 10, 5), speed = M.get_bits(packed, 15, 5),
        sp_attack = M.get_bits(packed, 20, 5), sp_defense = M.get_bits(packed, 25, 5),
    }
end

function M.hidden_power(ivs)
    local stats = {ivs.hp, ivs.attack, ivs.defense, ivs.speed, ivs.sp_attack, ivs.sp_defense}
    local type_bits, power_bits, weight = 0, 0, 1
    for _, iv in ipairs(stats) do
        type_bits = type_bits + (iv % 2) * weight
        power_bits = power_bits + (floor(iv / 2) % 2) * weight
        weight = weight * 2
    end
    local type_index = floor(type_bits * 15 / 63)
    return {type_index = type_index, type = hidden_power_types[type_index + 1], power = floor(power_bits * 40 / 63) + 30}
end

return M
