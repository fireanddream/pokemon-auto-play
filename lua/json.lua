-- Shared data-only JSON encoding for the controller bridges (Lua 5.1).
local array_metatable = {__json_array = true}
local escapes = {
    ['"'] = '\\"', ['\\'] = '\\\\', ['\b'] = '\\b', ['\f'] = '\\f',
    ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t',
}

local function json_string(value)
    local text = tostring(value):gsub('[%z\1-\31\\"]', function(character)
        return escapes[character] or string.format("\\u%04X", character:byte())
    end)
    return '"' .. text .. '"'
end

local function encode(value)
    local kind = type(value)
    if kind == "table" then
        local metatable = getmetatable(value)
        if metatable == array_metatable or (type(metatable) == "table" and metatable.__json_array) or value[1] ~= nil then
            local items = {}
            for _, item in ipairs(value) do items[#items + 1] = encode(item) end
            return "[" .. table.concat(items, ",") .. "]"
        end
        local keys, fields = {}, {}
        for key in pairs(value) do keys[#keys + 1] = key end
        table.sort(keys)
        for _, key in ipairs(keys) do fields[#fields + 1] = json_string(key) .. ":" .. encode(value[key]) end
        return "{" .. table.concat(fields, ",") .. "}"
    elseif kind == "nil" then
        return "null"
    elseif kind == "number" then
        assert(value == value and value ~= math.huge and value ~= -math.huge, "JSON numbers must be finite")
        return tostring(value)
    elseif kind == "boolean" then
        return tostring(value)
    end
    return json_string(value)
end

return {encode = encode, array_metatable = array_metatable}
