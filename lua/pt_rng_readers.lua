-- Master read-only Japanese Platinum memory snapshot (CPUJ / Lua 5.1 / DeSmuME).
-- Adapted from Real96 / SexyMalasada's references/Pt_RNG_DeSmuMe_SM.lua.
-- Direct calls read the current memory without emulator, input, or history operations.
local source = debug.getinfo(1, "S").source:sub(2):gsub("\\", "/")
local directory = assert(source:match("^(.*)/"), "Cannot locate the Platinum reader folder")
local common = dofile(directory .. "/pt_rng_common.lua")
local M = {}
-- Ordinary Continue prediction from the supplied Platinum reference.
M.load_delay_offset = 21
local floor = math.floor
local array_metatable = {__json_array = true}
local header_address, rtc_address = 0x02FFFE0C, 0x023FFDE8
local party_stride, box_stride, record_size = 0xEC, 0xFF0, 0x88
local roamer_memory_slots = {0, 1, 3, 4, 5}
local fixed_addresses = {
    mt_index = 0x020FFC28, data_pointer = 0x0210112C, delay = 0x021BEAA8,
    rng = 0x021BEF14, mt_state = 0x021BEF18, trainer_pointer = 0x021BFB94,
    battle_temp_rng = 0x027E3634,
}
local data_offsets = {
    party_count = 0xD090, party = 0xD094, weather = 0xE2DA, bike = 0xE304,
    roamers = 0x14FE8, flute = 0x15069, current_box = 0x19F20, boxes = 0x19F24,
    encounter_cooldown = 0x2E834, tile_attribute = 0x30908, party_summary_slot = 0x35558,
    box_slot = 0x4E7EB, box_summary_slot = 0x4E91C, party_slot = 0x4F379, opponents = 0x58E3C,
}
local block_offsets = {
    growth = {0,0,0,0,0,0, 1,1,2,3,2,3, 1,1,2,3,2,3, 1,1,2,3,2,3},
    attack = {1,1,2,3,2,3, 0,0,0,0,0,0, 2,3,1,1,3,2, 2,3,1,1,3,2},
}

function M.array(items)
    return setmetatable(items or {}, array_metatable)
end

function M.capabilities()
    return {
        raw_rng = true, mt_word_and_index = true, rtc = true, trainer_ids = true,
        load_seed_prediction = true,
        player_map = true, menu_selections = true, roamer_read = true, encounter_fields = true,
        pokemon_decode = true, party_read = true, last_party_read = true, opponent_read = true,
        box_read = true, pokemon_field_range_checks = true, pointer_range_checks = true,
        boot_seed_capture = false, pid_advance_tracking = false, mt_advance_tracking = false,
        full_mt_state = false, mt_prediction = false, encounter_prediction = false,
        checksum_validation = false, game_phase_detection = false, extended_pokemon_fields = false,
        daycare_egg_ready = false,
    }
end

function M.limitations()
    return M.array({
        {code = "raw_rng_without_history", message = "Current seeds and delay supply no boot-seed provenance or advance counts."},
        {code = "load_prediction_reference_offset", message = "Predicted delay uses raw delay + 21 for ordinary Continue; the input alignment requires live verification."},
        {code = "mt_partial_state", message = "The reference MT word and index are not a full MT state or predictor."},
        {code = "pointer_ranges_only", message = "Pointer spans are range-checked; in-range memory may still be uninitialized or stale."},
        {code = "pokemon_checksum_unchecked", message = "Field ranges do not validate the stored Pokemon checksum."},
        {code = "encrypted_records_assumed", message = "Record decoding follows the reference's checksum-seeded encrypted layout."},
        {code = "encounter_buffers_only", message = "Opponent records and the temporary battle seed do not prove an active battle."},
        {code = "menu_selection_only", message = "Selection bytes do not establish a menu or game phase."},
        {code = "raw_encounter_fields_only", message = "Tile, cooldown, bike, weather and flute values do not predict the next encounter."},
        {code = "rtc_field_ranges_only", message = "BCD and numeric range checks do not validate the full calendar date."},
        {code = "profile_live_unverified", message = "The CPUJ layout is adapted from the supplied Platinum reference; native verification is pending."},
    })
end

M.get_bits = common.get_bits
M.lcg32 = common.lcg32
M.shiny_xor = common.shiny_xor
M.decode_ivs = common.decode_ivs
M.hidden_power = common.hidden_power

function M.seed_from_delay(rtc, delay)
    assert(rtc.fields_in_range, "Seed prediction requires valid sampled RTC fields")
    assert(type(delay) == "number" and delay >= 0 and delay == math.floor(delay), "Expected a nonnegative delay")
    local high = (rtc.month * rtc.day + rtc.minute + rtc.second) % 0x100
    return (high * 0x1000000 + rtc.hour * 0x10000 + rtc.year - 2000 + delay) % 0x100000000
end

local function whole_index(value, maximum, label)
    assert(type(value) == "number" and value >= 0 and value <= maximum and value == floor(value), label .. " out of range")
    return value
end

local function check_box(box, slot)
    whole_index(box, 17, "Box index")
    whole_index(slot, 29, "Box slot index")
end

local function check_requested(requested)
    assert(type(requested) == "table", "Expected snapshot read selections")
    for key, value in pairs(requested) do
        assert(key == "party" or key == "opponents" or key == "last_party" or key == "box", "Unknown Platinum read: " .. tostring(key))
        if key == "box" then
            assert(type(value) == "table", "Expected box and slot indices")
            for field in pairs(value) do assert(field == "box" or field == "slot", "Unknown box selection field") end
            check_box(value.box, value.slot)
        else
            assert(type(value) == "boolean", "Expected a boolean read selection")
        end
    end
end

local function fields_in_range(errors, label, value, minimum, maximum)
    if value < minimum or value > maximum then errors[#errors + 1] = label end
end

local function ram_span(address, size, alignment)
    return type(address) == "number" and address == floor(address) and address % alignment == 0
        and address >= 0x02000000 and address + size <= 0x02400000
end

local function unavailable(pointer)
    return {available = false, reason = pointer .. "_pointer_out_of_range"}
end

local function set_shiny(record, trainer, reference)
    record.shiny_reference = reference
    record.shiny_available = trainer.tid ~= nil and trainer.sid ~= nil
    if record.shiny_available then
        record.shiny_xor = M.shiny_xor(record.pid_value, trainer.tid, trainer.sid)
        record.is_shiny = record.shiny_xor < 8
    end
end

local function decode_bcd(value)
    local units, tens = value % 16, floor(value / 16)
    if units > 9 or tens > 9 then return nil end
    return tens * 10 + units
end

function M.new(memory_api, options)
    options = options or {}
    assert(options.game_code == nil or type(options.game_code) == "function", "Game identity must use a trusted ROM-code provider")
    assert(type(memory_api) == "table", "A memory reader table is required")
    assert(type(memory_api.readdwordunsigned) == "function" and type(memory_api.readword) == "function"
        and type(memory_api.readbyte) == "function", "Expected DeSmuME-style 32/16/8-bit readers")
    local function read32(address) return memory_api.readdwordunsigned(address) % 0x100000000 end
    local function read16(address) return memory_api.readword(address) % 0x10000 end
    local function read8(address) return memory_api.readbyte(address) % 0x100 end
    local reader = {}

    function reader:identify()
        local memory_header = read32(header_address)
        local header = memory_header
        if options.game_code then
            local code = options.game_code()
            assert(type(code) == "string" and #code == 4, "Expected a four-byte ROM game code")
            header = 0
            for index = 1, 4 do header = header + code:byte(index) * 2 ^ ((index - 1) * 8) end
        end
        local bytes, printable = {}, true
        for index = 0, 3 do
            local byte = M.get_bits(header, index * 8, 8)
            bytes[index + 1] = string.char(byte)
            printable = printable and byte >= 32 and byte <= 126
        end
        local supported = header == 0x4A555043
        return {
            code = printable and table.concat(bytes) or "Unknown", header = string.format("%08X", header),
            version = supported and "Platinum" or "Unknown", language = supported and "JPN" or "Unknown",
            supported = supported, required_code = "CPUJ",
            identity_source = options.game_code and "emulator_rom_header" or "memory_header",
            memory_header = options.game_code and string.format("%08X", memory_header) or nil,
            profile_source = "references/Pt_RNG_DeSmuMe_SM.lua",
        }
    end

    local function profile(game)
        assert(game.supported, "Japanese Platinum (CPUJ) required; observed " .. game.code)
        local addresses = {}
        for name, address in pairs(fixed_addresses) do addresses[name] = address end
        return addresses
    end

    local function context(game)
        local addresses = profile(game)
        local data_base, trainer_base = read32(addresses.data_pointer), read32(addresses.trainer_pointer)
        local data_size = data_offsets.opponents + party_stride + record_size
        local pointers = {
            data = {address = addresses.data_pointer, value = data_base, in_range = ram_span(data_base, data_size, 4)},
            trainer = {address = addresses.trainer_pointer, value = trainer_base, in_range = ram_span(trainer_base, 0x1296, 4)},
        }
        if pointers.data.in_range then
            for name, offset in pairs(data_offsets) do addresses[name] = data_base + offset end
        end
        if pointers.trainer.in_range then
            addresses.trainer_ids, addresses.player_map = trainer_base + 0x8C, trainer_base + 0x1294
        end
        return {addresses = addresses, game = game, pointers = pointers}
    end

    function reader:addresses()
        local current = context(self:identify())
        return current.addresses, current.game, current.pointers
    end

    local function trainer_record(current)
        local address = current.addresses.trainer_ids
        if not address then return unavailable("trainer") end
        local ids = read32(address)
        return {available = true, address = address, tid = ids % 0x10000, sid = floor(ids / 0x10000)}
    end

    function reader:read_trainer()
        return trainer_record(context(self:identify()))
    end

    function reader:read_delay()
        return read32(profile(self:identify()).delay)
    end

    function reader:read_rtc()
        local raw = {
            year = read8(rtc_address), month = read8(rtc_address + 1), day = read8(rtc_address + 2),
            hour = read8(rtc_address + 4), minute = read8(rtc_address + 5), second = read8(rtc_address + 6),
        }
        local rtc = {raw = raw, encoding = "BCD", fields_in_range = true}
        for name, value in pairs(raw) do
            rtc[name] = decode_bcd(name == "hour" and value % 0x40 or value)
        end
        if rtc.year then rtc.year = 2000 + rtc.year end
        local ranges = {year = {2000, 2099}, month = {1, 12}, day = {1, 31}, hour = {0, 23}, minute = {0, 59}, second = {0, 59}}
        for name, bounds in pairs(ranges) do
            if rtc[name] == nil or rtc[name] < bounds[1] or rtc[name] > bounds[2] then rtc.fields_in_range = false end
        end
        return rtc
    end

    local function player_record(current)
        local address = current.addresses.player_map
        if not address then return unavailable("trainer") end
        return {available = true, address = address, map_index = read16(address)}
    end

    local function menu_record(current)
        local addresses = current.addresses
        if not addresses.current_box then return unavailable("data") end
        return {
            available = true, phase_known = false, current_box_index = read8(addresses.current_box),
            box_selected_slot_index = read8(addresses.box_slot), box_summary_slot_index = read8(addresses.box_summary_slot),
            party_selected_slot_index = read8(addresses.party_slot), party_summary_slot_index = read8(addresses.party_summary_slot),
        }
    end

    local function encounter_record(current)
        local addresses = current.addresses
        if not addresses.tile_attribute then return unavailable("data") end
        local attribute, bike = read8(addresses.tile_attribute), read8(addresses.bike)
        local flags = common.tile_flags(attribute)
        return {
            available = true, tile_attribute = attribute, tile_flags = flags,
            is_encounter_tile = M.get_bits(flags, 1, 1) == 1, is_water_tile = flags % 2 == 1,
            is_long_grass = attribute == 3, is_cave_tile = attribute == 8,
            cooldown_steps = read8(addresses.encounter_cooldown), bike_flag = bike, is_on_bike = bike == 1,
            weather_index = read8(addresses.weather), flute_flag = read8(addresses.flute),
        }
    end

    local function roamer_record(current, slot, trainer, player)
        local memory_slot = roamer_memory_slots[slot + 1]
        if not current.addresses.roamers then
            local result = unavailable("data")
            result.slot_index, result.memory_slot_index = slot, memory_slot
            return result
        end
        local address = current.addresses.roamers + memory_slot * 0x14
        local flag = read8(address + 0x12)
        local record = {
            available = true, address = address, slot_index = slot, memory_slot_index = memory_slot,
            active_flag = flag, active = flag == 1, field_errors = M.array(),
        }
        fields_in_range(record.field_errors, "active_flag", flag, 0, 1)
        if record.active then
            local pid = read32(address + 8)
            record.map_index = read16(address)
            if player.available then record.same_location_as_player = record.map_index == player.map_index end
            record.pid, record.pid_value = string.format("%08X", pid), pid
            record.nature_index, record.nature = pid % 25, common.nature_name(pid % 25)
            record.ivs = M.decode_ivs(read32(address + 4))
            record.hidden_power = M.hidden_power(record.ivs)
            record.species_id, record.hp = read16(address + 0xC), read16(address + 0xE)
            record.level, record.status_code = read8(address + 0x10), read8(address + 0x11)
            record.status = common.status_name(record.status_code)
            set_shiny(record, trainer, "current_trainer")
            fields_in_range(record.field_errors, "species_id", record.species_id, 1, 493)
            fields_in_range(record.field_errors, "level", record.level, 1, 100)
            if record.status == nil then record.field_errors[#record.field_errors + 1] = "status_code" end
        end
        record.fields_in_range = #record.field_errors == 0
        return record
    end

    local function roamer_records(current, trainer, player)
        local result = current.addresses.roamers and {available = true} or unavailable("data")
        result.slots = M.array()
        for slot = 0, 4 do result.slots[#result.slots + 1] = roamer_record(current, slot, trainer, player) end
        return result
    end

    function reader:read_roamer(slot)
        whole_index(slot, 4, "Roamer slot index")
        local current = context(self:identify())
        return roamer_record(current, slot, trainer_record(current), player_record(current))
    end

    function reader:read_roamers()
        local current = context(self:identify())
        return roamer_records(current, trainer_record(current), player_record(current))
    end

    local function decrypt_block(encrypted, checksum, offset)
        local prng = checksum
        for _ = 1, offset do prng = M.lcg32(prng, 0x5F748241, 0xCBA72510) end
        local words = {}
        for index = 1, 16 do
            prng = M.lcg32(prng, 0x41C64E6D, 0x6073)
            words[index] = common.xor16(encrypted[offset * 16 + index], floor(prng / 0x10000))
        end
        return words
    end

    function reader:read_pokemon(address, shiny_trainer)
        assert(ram_span(address, record_size, 2), "Pokemon record address out of range")
        local pid, header_flags, checksum = read32(address), read16(address + 4), read16(address + 6)
        local encrypted, empty = {}, pid == 0 and header_flags == 0 and checksum == 0
        for index = 1, 64 do
            encrypted[index] = read16(address + 6 + index * 2)
            empty = empty and encrypted[index] == 0
        end
        local record = {
            available = true, address = address, present = not empty, pid = string.format("%08X", pid), pid_value = pid,
            header_flags = header_flags, checksum = checksum, checksum_validated = false,
            validation_status = empty and "empty" or "unchecked",
        }
        if empty then return record end
        local order = M.get_bits(pid, 13, 5) % 24
        local growth = decrypt_block(encrypted, checksum, block_offsets.growth[order + 1])
        local attacks = decrypt_block(encrypted, checksum, block_offsets.attack[order + 1])
        record.block_order = order
        record.species_id, record.item_id = growth[1], growth[2]
        record.ot = {tid = growth[3], sid = growth[4]}
        record.ability_id = M.get_bits(growth[7], 8, 8)
        record.nature_index, record.nature = pid % 25, common.nature_name(pid % 25)
        record.moves = M.array({attacks[1], attacks[2], attacks[3], attacks[4]})
        record.pp = M.array({attacks[5] % 0x100, floor(attacks[5] / 0x100), attacks[6] % 0x100, floor(attacks[6] / 0x100)})
        local packed_ivs = attacks[9] + attacks[10] * 0x10000
        record.ivs, record.is_egg = M.decode_ivs(packed_ivs), M.get_bits(packed_ivs, 30, 1) == 1
        record.hidden_power = M.hidden_power(record.ivs)
        set_shiny(record, shiny_trainer or record.ot, shiny_trainer and "current_trainer" or "original_trainer")
        record.field_errors = M.array()
        fields_in_range(record.field_errors, "species_id", record.species_id, 1, 493)
        fields_in_range(record.field_errors, "item_id", record.item_id, 0, 536)
        fields_in_range(record.field_errors, "ability_id", record.ability_id, 1, 123)
        for index = 1, 4 do
            fields_in_range(record.field_errors, "moves[" .. index .. "]", record.moves[index], 0, 467)
            fields_in_range(record.field_errors, "pp[" .. index .. "]", record.pp[index], 0, 99)
        end
        record.fields_in_range = #record.field_errors == 0
        if not record.fields_in_range then record.validation_status = "invalid_fields" end
        return record
    end

    local function party_records(current, count)
        if not current.addresses.party then
            local result = unavailable("data")
            result.slots = M.array()
            return result
        end
        count = count or read8(current.addresses.party_count)
        local result = {available = true, count = count, count_in_range = count <= 6, slots = M.array()}
        if result.count_in_range then
            for slot = 0, count - 1 do
                local record = reader:read_pokemon(current.addresses.party + slot * party_stride)
                record.slot_index = slot
                result.slots[#result.slots + 1] = record
            end
        end
        return result
    end

    function reader:read_party()
        return party_records(context(self:identify()))
    end

    local function last_party_record(current, count, party)
        if not current.addresses.party then return unavailable("data") end
        count = count or read8(current.addresses.party_count)
        local result = {available = true, count = count, count_in_range = count <= 6, present = false}
        if not result.count_in_range then
            result.available, result.reason = false, "party_count_out_of_range"
        elseif count > 0 then
            result.pokemon = party and party.slots[count] or reader:read_pokemon(current.addresses.party + (count - 1) * party_stride)
            result.pokemon.slot_index = count - 1
            result.present = result.pokemon.present
        end
        return result
    end

    function reader:read_last_party()
        return last_party_record(context(self:identify()))
    end

    local function box_record(current, box, slot)
        local result
        if current.addresses.boxes then
            result = reader:read_pokemon(current.addresses.boxes + box * box_stride + slot * record_size)
        else
            result = unavailable("data")
        end
        result.box_index, result.slot_index = box, slot
        return result
    end

    function reader:box_address(box, slot)
        check_box(box, slot)
        local addresses = self:addresses()
        if addresses.boxes then return addresses.boxes + box * box_stride + slot * record_size end
        return nil
    end

    function reader:read_box(box, slot)
        check_box(box, slot)
        return box_record(context(self:identify()), box, slot)
    end

    local function opponent_records(current, trainer)
        local result = current.addresses.opponents and {available = true} or unavailable("data")
        result.battle_active_known, result.capture = false, M.array()
        if result.available then
            for slot = 0, 1 do
                local record = reader:read_pokemon(current.addresses.opponents + slot * party_stride, trainer)
                record.slot_index = slot
                result.capture[#result.capture + 1] = record
            end
        end
        return result
    end

    function reader:read_opponents()
        local current = context(self:identify())
        return opponent_records(current, trainer_record(current))
    end

    function reader:snapshot(frame, requested)
        requested = requested or {}
        check_requested(requested)
        local game = self:identify()
        local result = {
            schema = 1, frame = frame, game = game,
            capabilities = M.capabilities(), limitations = M.limitations(),
            access = {mode = "direct_reader", direct_read_frames = 0, injects_inputs = false, pauses = false},
        }
        if not game.supported then return result end
        -- Resolve each pointer once per snapshot, including its selected record reads.
        local current = context(game)
        local addresses = current.addresses
        result.addresses, result.pointers = addresses, current.pointers
        local seed, battle_seed = read32(addresses.rng), read32(addresses.battle_temp_rng)
        local mt_index = read32(addresses.mt_index)
        result.rng = {
            current_seed = string.format("%08X", seed), current_seed_value = seed,
            mt_state_word = read32(addresses.mt_state), mt_index = mt_index, mt_index_in_range = mt_index <= 624,
            delay = read32(addresses.delay), battle_temp_seed = string.format("%08X", battle_seed), battle_temp_seed_value = battle_seed,
        }
        result.rtc, result.trainer, result.player = self:read_rtc(), trainer_record(current), player_record(current)
        result.rng.predicted_delay_offset = M.load_delay_offset
        result.rng.predicted_delay = result.rng.delay + M.load_delay_offset
        result.rng.prediction_available = result.rtc.fields_in_range
        if result.rng.prediction_available then
            local predicted = M.seed_from_delay(result.rtc, result.rng.predicted_delay)
            result.rng.predicted_seed_value = predicted
            result.rng.predicted_seed = string.format("%08X", predicted)
        end
        result.menu, result.encounter = menu_record(current), encounter_record(current)
        result.party_available = current.pointers.data.in_range
        if result.party_available then
            result.party_count = read8(addresses.party_count)
            result.party_count_in_range = result.party_count <= 6
        end
        result.roamers = roamer_records(current, result.trainer, result.player)
        if requested.party then result.party = party_records(current, result.party_count) end
        if requested.last_party then result.last_party = last_party_record(current, result.party_count, result.party) end
        if requested.opponents then result.opponents = opponent_records(current, result.trainer) end
        if requested.box then result.box = box_record(current, requested.box.box, requested.box.slot) end
        return result
    end

    return reader
end

return M
