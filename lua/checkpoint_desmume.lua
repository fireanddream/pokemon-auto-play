-- DeSmuME mechanics only. Construction neither resets nor advances the emulator.
local source = debug.getinfo(1, "S").source:sub(2):gsub("\\", "/")
local directory = assert(source:match("^(.*)/"))
local buttons = dofile(directory .. "/bridge_io.lua").valid_buttons
local M = {}

function M.new(options)
    options = options or {}
    local emulator, pad = options.emu or emu, options.joypad or joypad
    assert(type(emulator) == "table" and type(emulator.pause) == "function"
        and type(emulator.framecount) == "function", "DeSmuME pause/frame APIs are required")
    assert(type(pad) == "table" and type(pad.set) == "function", "DeSmuME joypad API is required")
    assert(options.is_paused == nil or type(options.is_paused) == "function", "is_paused must be a callback")
    assert(options.pause == nil or type(options.pause) == "function", "pause must be a callback")
    local epoch = 0
    return {
        -- Native emu.pause() yields until resume. A paused host callback supplies
        -- a non-yielding pause confirmation instead, before any memory is read.
        pause = options.pause or function() emulator.pause() end,
        release = function()
            local released = {}
            for name in pairs(buttons) do released[name] = false end
            pad.set(released)
        end,
        frame = function() return emulator.framecount() end,
        epoch = function() return epoch end,
        is_paused = options.is_paused,
        -- Call for every load/reset, including loads returning to the same frame.
        state_changed = function() epoch = epoch + 1 end,
    }
end

return M
