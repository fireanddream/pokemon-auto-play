-- Regional Platinum layouts from Real96's Pt_RNG_DeSmuMe.lua; dispatch by ROM game code.
local M = {}
local languages = {J = "Japanese", E = "English"}
local addresses = {
    E = {0x02100834, 0x02101D2C, 0x021BF6A8, 0x021BFB14, 0x021BFB18, 0x021C0794},
    J = {0x020FFC28, 0x0210112C, 0x021BEAA8, 0x021BEF14, 0x021BEF18, 0x021BFB94},
}
local names = {"mt_index", "data_pointer", "delay", "rng", "mt_state", "trainer_pointer"}

function M.supported(code)
    return type(code) == "string" and #code == 4 and code:sub(1, 3) == "CPU"
        and languages[code:sub(4)] ~= nil
end

function M.game(code)
    return {code = code, supported = M.supported(code), language = languages[code:sub(4)],
        tested = code == "CPUJ", identity_source = "emu.gamecode"}
end

function M.profile(code)
    assert(M.supported(code), "Japanese (CPUJ) or English (CPUE) Pokemon Platinum is required")
    local source = addresses[code:sub(4)]
    local result = {battle_temp_rng = 0x027E3634}
    for index, name in ipairs(names) do result[name] = source[index] end
    return result, 0
end

function M.read_options(memory_api, save, frame)
    -- PlayerSave starts with Options; text speed is the low four bits (0/1/2).
    -- Layout and encoding: pret/pokeplatinum save_player.h and game_options.h.
    local result = {frame = frame, save_base = save, read_valid = false,
        source = "platinum_player_save", direct_read_frames = 0}
    local ok, message = pcall(function()
        assert(type(save) == "number" and save % 4 == 0
            and save >= 0x02000000 and save + 0x20044 <= 0x02400000,
            "The Options save pointer is outside main RAM")
        local page = save + 0x20024 + 16
        result.page_id = memory_api.readdwordunsigned(page) % 0x100000000
        result.page_size = memory_api.readdwordunsigned(page + 4) % 0x100000000
        result.page_location = memory_api.readdwordunsigned(page + 8) % 0x100000000
        assert(result.page_id == 1 and result.page_size == 52 and result.page_location == 0x64,
            "Unexpected Options save-page layout")
        result.address = save + 0x14 + result.page_location
        result.text_speed_value = memory_api.readword(result.address) % 16
        result.text_speed = ({[0] = "Slow", [1] = "Normal", [2] = "Fast"})[result.text_speed_value]
        assert(result.text_speed, "The text-speed field is outside its defined values")
        result.read_valid = true
    end)
    if not ok then result.error = tostring(message) end
    return result
end

return M
