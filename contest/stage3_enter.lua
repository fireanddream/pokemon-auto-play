-- Contest entry: one input sequence, then one screenshot-reviewed checkpoint.
local source = debug.getinfo(1, "S").source:sub(2):gsub("\\", "/")
local root = assert(source:match("^(.*)/")) .. "/.."
local master = dofile(root .. "/lua/checkpoint_master.lua")
local bridge_io = dofile(root .. "/lua/bridge_io.lua")
local party_reader = dofile(root .. "/contest/party_contest.lua").new(memory, emu.gamecode)
local screen_checks = dofile(root .. "/contest/screen_checks.lua")
local mailbox = bridge_io.new(root .. "/tmp", "checkpoint-")
local command_id = mailbox:previous_id()
local idle = false
local adapter = dofile(root .. "/lua/checkpoint_desmume.lua").new({
    pause = function() assert(idle, "A host-confirmed native pause is required") end,
    is_paused = function() return idle end,
})
local api, engine, latest, pending, captured, screen, target
local sampling, sample_frames, sample_directory
local source_party, enrollment_check
local stage = "setup"
local observer = {name = "pt-contest-stage3", commands = {}}

function observer.snapshot(frame)
    local code = emu.gamecode()
    if screen and captured then
        screen.fresh_visual_binding = captured.frame == frame and captured.epoch == adapter.epoch()
            and captured.raw == gui.gdscreenshot()
        if screen.source == "local_reference_pixels" then
            screen.pixel_check = screen_checks.read(captured.raw, "dress_up_entry")
            screen.confirmed = screen.confirmed and screen.fresh_visual_binding and screen.pixel_check.confirmed
        end
    end
    return {frame = frame,
        game = {code = code, supported = code == "CPUJ", identity_source = "emu.gamecode"},
        target = target, sampling = sampling, source_party = source_party,
        screen = screen or {source = "unreviewed", memory_proven = false}}
end

function observer.update(frame)
    if not sampling or sampling.complete or stage ~= "enter" then return end
    local index = sampling.count + 1
    local expected = sample_frames[index]
    assert(frame <= expected, "Missed a scheduled timing screenshot")
    if frame ~= expected then return end
    local path = sample_directory .. string.format("/sample-%02d.gd", index)
    local raw = gui.gdscreenshot()
    assert(type(raw) == "string" and #raw > 0, "Timing screenshot is empty")
    local file = assert(io.open(path, "wb"))
    assert(file:write(raw))
    assert(file:close())
    local sample = {path = path, frame = frame, epoch = adapter.epoch()}
    sample[sampling.kind == "enrollment" and "entry_frame_offset" or "intro_frame_offset"] = frame - sampling.start_frame
    sampling.samples[index] = sample
    sampling.count = index
    sampling.complete = index == sampling.expected_count
end

local function packet(observation)
    local result = {frame = adapter.frame(), epoch = adapter.epoch(), checkpoint = latest,
        observation = observation, next_section = stage}
    if engine and not pending then result.progress = engine:state() end
    return result
end

local function dispatch(action, args)
    local observation
    if action == "state-changed" then
        adapter.state_changed()
        api.rebase()
        captured, screen, source_party = nil, nil, nil
        assert(not engine, "Unexpected reset/load during contest entry")
    elseif action == "inspect" then
        assert(not engine, "Source inspection belongs before contest entry")
        source_party = party_reader.read_first(adapter.frame())
        source_party.epoch = adapter.epoch()
        observation = observer.snapshot(adapter.frame())
        api.capture()
    elseif action == "init" then
        assert(not engine and #args == 5, "init needs the run ID, log path, category, rank, and enrollment-check flag")
        assert(source_party and source_party.frame == adapter.frame() and source_party.epoch == adapter.epoch() and source_party.read_valid
            and not source_party.is_egg and source_party.next_target, "A fresh eligible first-party target is required")
        target = source_party.next_target
        assert(target.category == args[3] and target.rank == args[4],
            "The planned contest differs from the first Pokemon's next missing ribbon")
        enrollment_check = args[5] == "true"
        local entry_checks = {
            {id = "dress_up_entry_screenshot", field = "screen.confirmed", expected = true},
            {id = "review_frame", field = "screen.frame", context = "frame"},
            {id = "review_epoch", field = "screen.epoch", context = "epoch"},
        }
        if enrollment_check then
            entry_checks[#entry_checks + 1] = {id = "enrollment_images_complete", field = "sampling.complete", expected = true}
            entry_checks[#entry_checks + 1] = {id = "selected_rank", field = "screen.selected_rank", context = "target.rank"}
            entry_checks[#entry_checks + 1] = {id = "selected_category", field = "screen.selected_category", context = "target.category"}
            entry_checks[#entry_checks + 1] = {id = "selected_party_slot", field = "screen.selected_party_slot", expected = 1}
        end
        engine = master.new({run_id = args[1], emulator = adapter, reader = observer,
            log = master.jsonl(args[2]), definitions = {
                ["stage-3-enter"] = {checks = entry_checks, branches = {pass = "finish"}},
                ["stage-3-timing-probe"] = {checks = {
                    {id = "all_timed_images_written", field = "sampling.complete", expected = true},
                    {id = "timed_image_count", field = "sampling.count", context = "expected_count"},
                }, branches = {pass = "finish"}},
                stopped = {checks = {{id = "host_stop", expected = "continue",
                    evaluate = function(_, context) return false, context.reason end}},
                    branches = {pass = "finish"}},
            }})
        stage = "enter"
    elseif action == "sampling" or action == "enrollment-sampling" then
        assert(engine and stage == "enter" and not sampling and #args >= 3,
            "sampling needs a directory, menu frame count, and introduction frame offsets")
        sample_directory, sample_frames = args[1], {}
        local menu_frames = tonumber(args[2])
        assert(menu_frames and menu_frames >= 0 and menu_frames == math.floor(menu_frames),
            "Supply a whole menu-frame count")
        sampling = {start_frame = adapter.frame() + menu_frames, count = 0,
            kind = action == "enrollment-sampling" and "enrollment" or "timing",
            expected_count = #args - 2, complete = false,
            samples = setmetatable({}, bridge_io.array_metatable)}
        local previous_offset = 0
        for index = 3, #args do
            local offset = tonumber(args[index])
            assert(offset and offset > previous_offset and offset == math.floor(offset),
                "Timing screenshot offsets must be increasing whole frames")
            sample_frames[#sample_frames + 1] = sampling.start_frame + offset
            previous_offset = offset
        end
    elseif action == "probe-visit" then
        assert(engine and stage == "enter" and sampling and sampling.kind == "timing" and not pending
            and api.state.command_done and api.state.stop_reason == "combo_complete",
            "Complete the timing-probe inputs first")
        api.capture()
        screen = {source = "timed_screenshots_pending_review", memory_proven = false,
            expected = "timing_samples_only", phase_confirmed = false}
        latest = engine:visit("stage-3-timing-probe", {expected_count = sampling.expected_count})
        pending = latest.status == "awaiting_save" and latest or nil
        observation, stage = latest.snapshot, "checkpoint"
    elseif action == "capture" then
        assert(engine and stage == "enter" and api.state.command_done
            and api.state.stop_reason == "combo_complete", "Complete the contest-entry inputs first")
        api.capture()
        captured = {frame = adapter.frame(), epoch = adapter.epoch(), raw = gui.gdscreenshot()}
        observation = observer.snapshot(adapter.frame())
        observation.automatic_check = screen_checks.read(captured.raw, "dress_up_entry")
        stage = "review"
    elseif action == "visit" then
        assert(stage == "review" and not pending and #args == (enrollment_check and 9 or 6),
            "Expected the endpoint screenshot verdict and requested enrollment observations")
        assert((args[1] == "pass" or args[1] == "fail")
            and (args[2] == "visual_review" or args[2] == "local_reference_pixels")
            and args[3] ~= "" and args[4] ~= "", "Supply the actual verdict, image, and visual note")
        assert(tonumber(args[5]) == captured.frame and tonumber(args[6]) == captured.epoch
            and adapter.frame() == captured.frame and adapter.epoch() == captured.epoch,
            "The screenshot no longer belongs to this paused boundary")
        screen = {source = args[2], path = args[3], note = args[4], confirmed = args[1] == "pass",
            expected = "dress_up_entry", memory_proven = false,
            frame = captured.frame, epoch = captured.epoch}
        if enrollment_check then
            screen.selected_rank, screen.selected_category, screen.selected_party_slot = args[7], args[8], tonumber(args[9])
        end
        latest = engine:visit("stage-3-enter", {frame = captured.frame, epoch = captured.epoch, target = target})
        pending = latest.status == "awaiting_save" and latest or nil
        observation, stage = latest.snapshot, "checkpoint"
    elseif action == "complete" then
        assert(pending and #args == 6, "Expected pending saved evidence")
        latest = engine:complete(tonumber(args[2]), {run_id = args[1], visit = tonumber(args[2]),
            frame = tonumber(args[3]), epoch = tonumber(args[4]), path = args[5], emulator_paused = args[6] == "true"})
        pending = nil
        stage = latest.proceed and latest.branch or "stopped"
        observation = latest.snapshot
        print((sampling and sampling.kind == "timing" and "Contest timing probe: " or "Contest Stage 3: ")
            .. (latest.proceed and "PASS" or "FAIL"))
    elseif action == "handoff-stage4" then
        assert(#args == 1 and stage == "finish" and not pending and latest
            and latest.checkpoint == "stage-3-enter" and latest.status == "complete" and latest.proceed
            and adapter.frame() == latest.frame and adapter.epoch() == latest.epoch
            and api.state.command_done and api.state.buttons == "",
            "Stage 4 requires the current acknowledged, released Stage 3 boundary")
        local config = assert(loadfile(args[1]))()
        assert(type(config) == "table" and config.stage == 4 and config.entry_mode == "stage3_handoff"
            and config.entry_render_frames == 0 and config.source_frame == latest.frame
            and type(config.source_context) == "table" and type(config.target) == "table",
            "Expected a zero-frame Stage 4 handoff configuration")
        for _, key in ipairs({"checkpoint", "run_id", "visit", "frame", "epoch"}) do
            assert(config.source_context[key] == latest[key], "The Stage 4 source context differs from Stage 3")
        end
        for _, key in ipairs({"category", "rank", "ribbon_bit", "party_slot"}) do
            assert(config.target[key] == latest.snapshot.target[key], "The Stage 4 target differs from Stage 3")
        end
        -- Replace callbacks inside this paused RPC; native Lua restart can advance a frame.
        assert(loadfile(root .. "/contest/stage4_dress.lua"))(config, true)
        assert(adapter.frame() == latest.frame, "The direct dressing handoff advanced emulation")
        stage = "stage4_handoff"
        return {frame = adapter.frame(), epoch = adapter.epoch(), next_section = stage,
            handoff = {source = "paused_stage3_callback", bridge = "pt-contest-stage4",
                source_context = config.source_context, start_frame = latest.frame,
                end_frame = adapter.frame(), emulator_frames = 0}}
    elseif action == "abort" then
        local reason = args[1] or "Host stopped contest Stage 3"
        api.finish(reason)
        stage = "stopped"
        if pending then
            latest, pending = engine:cancel(reason), nil
        elseif engine and not engine:state().halted then
            latest = engine:visit("stopped", {reason = reason})
            pending = latest.status == "awaiting_save" and latest or nil
        end
        observation = latest and latest.snapshot
        api.capture()
    else
        error("Unknown contest Stage 3 checkpoint command: " .. tostring(action))
    end
    return packet(observation)
end

function observer.attach(bridge)
    api = bridge
    api.state.checkpoint_protocol = type(emu.registermenustart) == "function" and 1 or 0
    if emu.registermenustart then
        emu.registermenustart(function()
            local parts = mailbox:read_command(command_id)
            if not parts then return end
            command_id = parts[1]
            local args = {}
            for index = 3, #parts do args[#args + 1] = parts[index] end
            assert(not idle, "Reentrant checkpoint request")
            idle = true
            api.state.frame = adapter.frame()
            local ok, result = pcall(dispatch, parts[2], args)
            if not ok then
                local message = tostring(result)
                if pending then latest, pending = engine:cancel(message), nil end
                api.finish(message)
                stage = "stopped"
                result = {frame = adapter.frame(), epoch = adapter.epoch(), error = message}
            end
            idle = false
            result.command_id, result.command_done = command_id, true
            if not result.handoff then
                api.state.checkpoint_epoch = adapter.epoch()
                api.state.checkpoint_visit = result.checkpoint and result.checkpoint.visit
                api.state.checkpoint_status = result.checkpoint and result.checkpoint.status
                api.state.observation_stale = false
                api.publish(result.observation or (latest and latest.frame == adapter.frame() and latest.snapshot) or nil)
            end
            mailbox:publish(result, false)
        end)
    end
end

assert(loadfile(root .. "/lua/input_bridge.lua"))(observer)
