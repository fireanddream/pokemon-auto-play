-- Game-independent frame plans, validated in full before any inputs execute.
local combos = {}

local function whole(value, minimum, label)
    local number = tonumber(value)
    assert(number and number < math.huge and number >= minimum and number == math.floor(number),
        label .. " requires a whole number of at least " .. minimum)
    return number
end

function combos.build(name, arguments, frames_since_reset, guard_frames, valid_buttons)
    local plan = {name = name, steps = {}, summary_switches = 0}
    local function advance(frames)
        plan.steps[#plan.steps + 1] = {action = "advance", total = frames}
    end
    local function press(button, hold, wait)
        local step = {action = "press", button = button, label = button,
            hold = hold, wait = wait, total = hold + wait}
        plan.steps[#plan.steps + 1] = step
        return step
    end

    if name == "inputs" or name == "guarded-inputs" then
        assert(#arguments > 0, "inputs requires button:hold:release or wait:frames steps")
        if name == "guarded-inputs" then
            local remaining = math.max(0, guard_frames - frames_since_reset)
            if remaining > 0 then advance(remaining) end
        end
        for _, argument in ipairs(arguments) do
            local released = argument:match("^wait:(%d+)$")
            if released then
                advance(whole(released, 1, "Wait frames"))
            else
                local label, hold, wait = argument:match("^([^:]+):(%d+):(%d+)$")
                assert(label, "Input steps use button:hold:release or wait:frames")
                local selected, seen = {}, {}
                for button in (label .. "+"):gmatch("([^+]*)%+") do
                    assert(valid_buttons[button], "Unknown DS button: " .. button)
                    if not seen[button] then
                        selected[#selected + 1] = button
                        seen[button] = true
                    end
                end
                local step = press(selected, whole(hold, 1, "Held frames"), whole(wait, 1, "Released frames"))
                step.label = label
            end
        end
    else
        error("Unknown key combo: " .. tostring(name))
    end

    local progress = frames_since_reset
    for _, step in ipairs(plan.steps) do
        if step.button then
            assert(progress >= guard_frames, "Key combo inputs are blocked during the configured post-reset guard")
        end
        progress = progress + step.total
    end
    return plan
end

return combos
