-- Items-pocket reader promoted from the passed local scarf test.
local directory = assert(debug.getinfo(1, "S").source:sub(2):gsub("\\", "/"):match("^(.*)/"))
local core = dofile(directory .. "/../lua/pt_rng_readers.lua")
local party = dofile(directory .. "/party_contest.lua")
local names = {[0] = "None", [260] = "Red Scarf", [261] = "Blue Scarf",
    [262] = "Pink Scarf", [263] = "Green Scarf", [264] = "Yellow Scarf"}
local M = {}
function M.item_name(id) return names[id] or ("Item " .. tostring(id)) end

function M.new(memory_api, game_code)
    local reader = core.new(memory_api, {game_code = game_code})
    local first = party.new(memory_api, game_code)
    local function read16(address) return memory_api.readword(address) % 0x10000 end
    local function read32(address) return memory_api.readdwordunsigned(address) % 0x100000000 end
    local function span(address, size)
        assert(address % 4 == 0 and address >= 0x02000000 and address + size <= 0x02400000,
            "Selected memory span is outside CPUJ main RAM")
    end
    return {snapshot = function(frame)
        local _, game, pointers = reader:addresses()
        assert(game.supported, "CPUJ Bag memory is unavailable")
        local pokemon = first.read_first(frame)
        pokemon.held_item = M.item_name(pokemon.held_item_id)
        local save = pointers.trainer.value
        span(save, 0x20064)
        local page = save + 0x20024 + 3 * 16
        assert(read32(page) == 3 and read32(page + 4) == 1916, "Unexpected Bag save-page layout")
        local address = save + 0x14 + read32(page + 8)
        span(address, 1916)
        local items, empty, seen = core.array(), false, {}
        for index = 0, 164 do
            local id, quantity = read16(address + index * 4), read16(address + index * 4 + 2)
            if id == 0 then
                assert(quantity == 0, "An empty Bag slot has a quantity")
                empty = true
            else
                assert(not empty and not seen[id] and id <= 467 and quantity >= 1 and quantity <= 999,
                    "Bag item order or fields are invalid")
                seen[id] = true
                items[#items + 1] = {position = index + 1, item_id = id, quantity = quantity}
            end
        end
        return {frame = frame, game = {code = game.code, supported = game.supported,
            identity_source = game.identity_source}, party = pokemon,
            bag = {pocket = "Items", items = items, count = #items, position_base = 1},
            access = {direct_read_frames = 0, injects_inputs = false},
            screen = {source = "prepared_save_and_scripted_inputs", memory_proven = false}}
    end}
end
return M
