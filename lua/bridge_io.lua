-- Shared controller mailbox and DS button I/O. Bridges own scheduling and observations.
local source = debug.getinfo(1, "S").source:sub(2):gsub("\\", "/")
local script_dir = assert(source:match("^(.*)/"), "Cannot locate the Lua bridge folder")
local json = dofile(script_dir .. "/json.lua")
local M = {array_metatable = json.array_metatable, valid_buttons = {}}
local button_names = {"debug", "R", "L", "X", "Y", "A", "B", "start", "select", "up", "down", "left", "right", "lid"}
for _, name in ipairs(button_names) do M.valid_buttons[name] = true end

function M.guard_buttons(parts)
    local selected, seen = {}, {}
    if not (#parts == 3 and parts[3] == "none") then
        for index = 3, #parts do
            local name = parts[index]
            assert(M.valid_buttons[name], "Unknown DS button")
            if not seen[name] then
                selected[#selected + 1] = name
                seen[name] = true
            end
        end
    end
    return selected
end

function M.apply_buttons(selected)
    local buttons = {}
    for _, name in ipairs(button_names) do buttons[name] = false end
    if type(selected) == "table" then
        for _, name in ipairs(selected) do buttons[name] = true end
    elseif selected then
        buttons[selected] = true
    end
    joypad.set(buttons)
end

function M.pressed_buttons(input)
    local pressed = {}
    for _, name in ipairs(button_names) do
        if input[name] then pressed[#pressed + 1] = name end
    end
    return table.concat(pressed, ",")
end

local function read_line(path)
    local file = io.open(path, "r")
    if not file then return nil end
    local line = file:read("*l")
    file:close()
    return line
end

local function write_json(path, state, mode)
    local file = assert(io.open(path, mode))
    file:write(json.encode(state), "\n")
    file:close()
end

function M.new(directory, prefix)
    local command_path = directory .. "/" .. (prefix or "") .. "command.txt"
    local status_path = directory .. "/" .. (prefix or "") .. "status.json"
    local events_path = directory .. "/" .. (prefix and prefix .. "events.jsonl" or "seed-events.jsonl")
    local screen_path = directory .. "/screen.gd"
    local mailbox = {}

    function mailbox:previous_id()
        return (read_line(command_path) or ""):match("^(%d+)|") or ""
    end

    function mailbox:read_command(previous_id)
        local line = read_line(command_path)
        if not line then return nil end
        local parts = {}
        -- Preserve empty fields, including a trailing empty argument.
        for value in (line .. "|"):gmatch("([^|]*)|") do parts[#parts + 1] = value end
        if parts[1] ~= previous_id then return parts end
    end

    function mailbox:publish(state, event)
        write_json(status_path, state, "w")
        if event then write_json(events_path, state, "a") end
    end

    function mailbox:capture_screen()
        local file = assert(io.open(screen_path, "wb"))
        file:write(gui.gdscreenshot())
        file:close()
    end

    return mailbox
end

return M
