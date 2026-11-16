-- Twenty continuous scarf swaps; fresh held-item and closed-menu checks per cycle.
local directory = assert(debug.getinfo(1, "S").source:sub(2):gsub("\\", "/"):match("^(.*)/"))
local sections = dofile(directory .. "/sections.lua")
local screens = dofile(directory .. "/screen_checks.lua")
local pixels = dofile(directory .. "/acting_pixels.lua")
local M = {}

-- Stable Poketch panel edges, away from animated app content and clock digits.
local menu_probes = {{16,208}, {32,208}, {192,208}, {208,208}, {16,236}, {208,236},
    {16,328}, {208,328}, {16,364}, {32,364}, {192,364}, {208,364}}

local function same_identity(context, record)
    if not context or not record then return false end
    for _, key in ipairs({"checkpoint", "run_id", "visit", "frame", "epoch"}) do
        if context[key] ~= record[key] then return false end
    end
    return true
end

local function scarf_rule()
    return {id = "required_scarf_held", expected = true, evaluate = function(snapshot)
        local party, target = snapshot.party or {}, snapshot.target or {}
        return party.held_item_id == target.required_scarf_id,
            {expected_item_id = target.required_scarf_id, actual_item_id = party.held_item_id}
    end}
end

function M.definitions(base)
    local definitions = {}
    for _, name in ipairs({"stage-1-load", "stage-2-prep", "stopped"}) do
        definitions[name] = base[name]
        definitions[name].save_state = false
    end
    local prep = definitions["stage-2-prep"].checks
    prep[#prep + 1] = scarf_rule()
    for _, rule in ipairs({
        {id = "overworld_pixels", field = "screen.confirmed", expected = true},
        {id = "closed_menu_pixels", field = "menu_closed.confirmed", expected = true},
        {id = "capture_frame", field = "image.frame", context = "frame"},
        {id = "capture_epoch", field = "image.epoch", context = "epoch"},
    }) do prep[#prep + 1] = rule end
    return definitions
end

function M.run(runtime)
    local started, count, menu_reference = false, 0, nil
    local run = {}

    function run.begin(config, latest)
        local adapter, number = runtime.adapter, config.stage
        if number == 1 then
            assert(not started and not latest and config.scarf_cycles == 20,
                "Load once for the twenty-cycle scarf test")
            started = true
        else
            assert(started and number == 2 and count < 20 and menu_reference
                and latest and latest.status == "complete" and latest.proceed
                and latest.frame == adapter.frame() and latest.epoch == adapter.epoch()
                and same_identity(config.source_context, latest)
                and latest.checkpoint == (count == 0 and "stage-1-load" or "stage-2-prep"),
                "Continue scarf preparation from the current acknowledged paused overworld")
            local target = config.target or {}
            local positions = {Cool = 0, Beauty = 1, Cute = 2, Smart = 3, Tough = 4}
            local position = positions[target.category]
            assert(position and target.party_slot == 1 and target.scarf_cycle == count + 1
                and target.required_scarf_id == 260 + position
                and target.required_scarf_id ~= latest.snapshot.party.held_item_id,
                "Choose a random category whose scarf is not currently held")
            count = count + 1
        end
        local section = sections.new(config, runtime)
        local snapshot = section.snapshot
        function section.snapshot(frame)
            local result = snapshot(frame)
            if number == 1 and result.screen and result.screen.confirmed and result.image then
                local raw, points = gui.gdscreenshot(), {}
                for _, probe in ipairs(menu_probes) do
                    local at = 13 + (probe[2] * 256 + probe[1]) * 4
                    local r, g, b = raw:byte(at, at + 2)
                    points[#points + 1] = {probe[1], probe[2], r, g, b}
                end
                menu_reference = {points = points, image = result.image,
                    source = "accepted_stage1_closed_menu_framebuffer"}
                result.menu_exit_reference = menu_reference
            elseif number == 2 and result.party then
                local raw
                result.image, raw = runtime.image(config.work .. "/stage2-end.gd", frame)
                result.screen = screens.read(raw, "loaded_overworld")
                result.menu_closed = pixels.match_points(raw, menu_reference.points)
                result.menu_closed.source = "accepted_stage1_closed_menu_pixels"
                result.menu_closed.reference = menu_reference
                result.menu_closed.direct_read_frames = 0
            end
            return result
        end
        return section
    end
    return run
end

return M
