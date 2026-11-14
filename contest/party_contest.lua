-- Selected CPUJ party fields and Sinnoh contest ribbons; zero-frame read only.
local source = debug.getinfo(1, "S").source:sub(2):gsub("\\", "/")
local root = assert(source:match("^(.*)/")) .. "/.."
local common = dofile(root .. "/lua/pt_rng_common.lua")
local core = dofile(root .. "/lua/pt_rng_readers.lua")
local M = {}
local categories = {"Cool", "Beauty", "Cute", "Smart", "Tough"}
local ranks = {"Normal", "Super", "Hyper", "Master"}
local orders = {
    "ABCD", "ABDC", "ACBD", "ACDB", "ADBC", "ADCB",
    "BACD", "BADC", "BCAD", "BCDA", "BDAC", "BDCA",
    "CABD", "CADB", "CBAD", "CBDA", "CDAB", "CDBA",
    "DABC", "DACB", "DBAC", "DBCA", "DCAB", "DCBA",
}

function M.new(memory_api, game_code)
    local reader = core.new(memory_api, {game_code = game_code})
    local function read16(address) return memory_api.readword(address) % 0x10000 end
    local function read32(address) return memory_api.readdwordunsigned(address) % 0x100000000 end
    return {read_first = function(frame)
        local addresses, game = reader:addresses()
        assert(game.supported and addresses.party, "CPUJ party memory is unavailable")
        local count = memory_api.readbyte(addresses.party_count)
        assert(count >= 1 and count <= 6, "The first party Pokemon is unavailable")
        local address = addresses.party
        local personality, flags, stored = read32(address), read16(address + 4), read16(address + 6)
        local clear, key, total = {}, stored, 0
        for index = 1, 64 do
            local word = read16(address + 6 + index * 2)
            if common.get_bits(flags, 1, 1) == 0 then
                key = common.lcg32(key, 0x41C64E6D, 0x6073)
                word = common.xor16(word, math.floor(key / 0x10000))
            end
            clear[index], total = word, (total + word) % 0x10000
        end
        assert(total == stored and common.get_bits(flags, 2, 1) == 0,
            "The first Pokemon failed stored-data validation")
        local order = orders[common.get_bits(personality, 13, 5) % 24 + 1]
        local growth = (order:find("A") - 1) * 16
        local attacks = (order:find("B") - 1) * 16
        local ribbon_block = (order:find("C") - 1) * 16
        local result = {frame = frame, party_slot = 1, party_count = count,
            species_id = clear[growth + 1], held_item_id = clear[growth + 2],
            is_egg = common.get_bits(clear[attacks + 10], 14, 1) == 1,
            data_validation_passed = true, read_valid = true,
            owned_ribbon_bits = core.array(), ribbon_flags = core.array(), owned_count = 0,
            source = "first_party_sinnoh_contest_ribbons", direct_read_frames = 0,
            game_phase_proven = false}
        assert(result.species_id >= 1 and result.species_id <= 493
            and result.held_item_id <= 536, "The first Pokemon has invalid selected fields")
        -- Logical block C, byte offset 24: category-major Sinnoh ribbon bits 0..19.
        local bits = clear[ribbon_block + 13] + clear[ribbon_block + 14] * 0x10000
        for bit = 0, 19 do
            local owned = common.get_bits(bits, bit, 1) == 1
            result.ribbon_flags[bit + 1] = owned
            if owned then
                result.owned_count = result.owned_count + 1
                result.owned_ribbon_bits[#result.owned_ribbon_bits + 1] = bit
            elseif not result.next_target then
                local category, rank = math.floor(bit / 4) + 1, bit % 4 + 1
                result.next_target = {category = categories[category], rank = ranks[rank],
                    ribbon_bit = bit, category_down_presses = category - 1, rank_down_presses = rank - 1,
                    required_scarf_id = 259 + category, party_slot = 1,
                    source = "first_party_missing_ribbon", enrollment_proven = false}
            end
        end
        result.all_ribbons_owned = result.owned_count == 20
        return result
    end}
end

return M
