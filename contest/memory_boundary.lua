-- Callers open X with profile timings, read paused, then close X before acceptance.
local M = {}
local party_checkpoints = {['stage-1-load'] = true, ['stage-2-prep'] = true,
    ['stage-3-enter'] = true, ['stage-6-act'] = true}

local function whole(value, minimum, maximum)
    return type(value) == "number" and value >= minimum and value < math.huge
        and value == math.floor(value) and (not maximum or value <= maximum)
end

function M.read(state, adapter, paused, scope, read)
    local frame, epoch = adapter.frame(), adapter.epoch()
    assert(paused and state.command_done and state.error == "" and state.buttons == "" and not state.stylus_touch,
        "Memory reads require a native paused boundary with inputs released")
    local result = read(frame)
    assert(adapter.frame() == frame and adapter.epoch() == epoch, "Memory reads must add zero emulator frames")
    result.epoch = epoch
    result.memory_read = {frame = frame, epoch = epoch, source = "x_menu_paused_memory", scope = scope, direct_read_frames = 0}
    return result
end

function M.closed(snapshot, state, frame, epoch, paused)
    assert(snapshot and snapshot.epoch == epoch and paused and state.command_done and state.error == ""
        and state.buttons == "" and not state.stylus_touch and state.stop_reason == "combo_complete"
        and state.command_start_frame == snapshot.frame and state.command_end_frame == frame and frame > snapshot.frame,
        "Close X after the single paused memory read before accepting the closed-menu boundary")
end

function M.context(record)
    local snapshot = assert(record.snapshot, "An accepted party checkpoint is required")
    local accepted = {}
    for _, key in ipairs({"checkpoint", "run_id", "visit", "frame", "epoch", "status", "proceed"}) do
        accepted[key] = record[key]
    end
    return {party = snapshot.party or snapshot.source_party, memory_read = snapshot.memory_read,
        game_code = snapshot.game.code, accepted_at = accepted}
end

function M.party(context, game_code, target)
    assert(type(context) == "table" and type(context.party) == "table"
        and type(context.memory_read) == "table" and type(context.accepted_at) == "table",
        "Acting requires saved, accepted X-protected first-party readiness; memory cannot be read at Acting entry")
    local party, read, accepted = context.party, context.memory_read, context.accepted_at
    assert(context.game_code == game_code and (game_code == "CPUJ" or game_code == "CPUE")
        and party_checkpoints[accepted.checkpoint]
        and accepted.status == "complete" and accepted.proceed == true
        and type(accepted.run_id) == "string" and accepted.run_id ~= "" and whole(accepted.visit, 1)
        and whole(accepted.frame, 0) and whole(accepted.epoch, 0)
        and whole(read.frame, 0) and whole(read.epoch, 0)
        and read.source == "x_menu_paused_memory" and read.direct_read_frames == 0
        and read.frame == party.frame and read.epoch == accepted.epoch and read.frame <= accepted.frame,
        "The party handoff does not identify its accepted X-pause memory read")
    assert(party.read_valid == true and party.data_validation_passed == true
        and party.party_slot == 1 and party.is_egg == false
        and whole(party.species_id, 1, 493) and whole(party.held_item_id, 0, 536)
        and whole(party.current_hp, 1, 714) and whole(party.max_hp, 1, 714) and party.current_hp <= party.max_hp
        and whole(party.sheen, 1, 255) and whole(party.move_count, 2, 4)
        and type(party.moves) == "table" and #party.moves == 4
        and type(party.unavailable_move_slots) == "table"
        and type(party.ribbon_flags) == "table" and #party.ribbon_flags == 20,
        "The accepted first-party readiness facts are invalid or inconsistent")
    local empty, count = 0, 0
    for slot = 1, 4 do
        local move = party.moves[slot]
        assert(whole(move, 0, 467), "The accepted move facts are inconsistent")
        if move == 0 then
            empty = empty + 1
            assert(party.unavailable_move_slots[empty] == slot, "The accepted empty-move-slot list is invalid")
        else
            count = count + 1
        end
    end
    assert(empty == #party.unavailable_move_slots and count == party.move_count,
        "The accepted move facts are inconsistent")
    for bit = 1, 20 do
        assert(type(party.ribbon_flags[bit]) == "boolean", "The accepted ribbon flags are invalid")
    end
    assert(type(target) == "table" and target.party_slot == 1 and whole(target.ribbon_bit, 0, 19)
        and party.ribbon_flags[target.ribbon_bit + 1] == false,
        "The accepted first Pokemon must have at least two moves and be missing the target ribbon")
    return party
end

return M
