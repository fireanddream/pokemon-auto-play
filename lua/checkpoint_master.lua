-- Game-independent, pause-first checkpoints. Loading this module has no emulator effects.
local source = debug.getinfo(1, "S").source:sub(2):gsub("\\", "/")
local directory = assert(source:match("^(.*)/"), "Cannot locate checkpoint modules")
local json = dofile(directory .. "/json.lua")
local M = {}

local function text(value)
    return type(value) == "string" and value ~= ""
end

local function whole(value)
    return type(value) == "number" and value >= 0 and value < math.huge and value == math.floor(value)
end

local function copy(value, functions, seen)
    if type(value) ~= "table" then
        local kind = type(value)
        assert(kind == "nil" or kind == "boolean" or kind == "string"
            or (kind == "number" and value == value and math.abs(value) < math.huge)
            or (functions and kind == "function"), "Checkpoint data must be finite, plain values")
        return value
    end
    seen = seen or {}
    assert(not seen[value], "Checkpoint data must not contain cycles")
    seen[value] = true
    local result = {}
    for key, item in pairs(value) do
        assert(type(key) == "string" or whole(key), "Invalid checkpoint data key")
        result[key] = copy(item, functions, seen)
    end
    seen[value] = nil
    local marker = getmetatable(value)
    if type(marker) == "table" and marker.__json_array then setmetatable(result, json.array_metatable) end
    return result
end

local function equal(a, b)
    if type(a) ~= type(b) then return false end
    if type(a) ~= "table" then return a == b end
    for key, value in pairs(a) do if not equal(value, b[key]) then return false end end
    for key in pairs(b) do if a[key] == nil then return false end end
    return true
end

local function field(data, path)
    for key in path:gmatch("[^.]+") do
        if type(data) ~= "table" then return nil end
        data = data[tonumber(key) or key]
    end
    return data
end

local function validate_rules(rules)
    assert(type(rules) == "table" and #rules > 0, "Checkpoint rules must be a nonempty list")
    local names = {}
    for index = 1, #rules do
        local rule = rules[index]
        assert(type(rule) == "table" and text(rule.id) and not names[rule.id], "Rule IDs must be unique")
        names[rule.id] = true
        if rule.evaluate then
            assert(type(rule.evaluate) == "function" and rule.expected ~= nil, "Custom rules need evaluate and expected")
        else
            assert(text(rule.field) and not rule.field:find("%.%.")
                and not rule.field:match("^%.") and not rule.field:match("%.$"), "Rules need a field path")
            local op = rule.op or "eq"
            assert(op == "eq" or op == "range" or op == "one_of", "Unknown checkpoint operator")
            assert(rule.expected ~= nil or text(rule.context), "Rules need expected or context")
            assert(not (rule.expected ~= nil and rule.context), "Choose expected or context")
            if not rule.context and op ~= "eq" then
                assert(type(rule.expected) == "table" and #rule.expected > 0, "Expected a value list")
                if op == "range" then
                    assert(#rule.expected == 2 and type(rule.expected[1]) == "number"
                        and type(rule.expected[2]) == "number" and rule.expected[1] <= rule.expected[2], "Invalid range")
                end
            end
        end
    end
    for key in pairs(rules) do assert(whole(key) and key >= 1 and key <= #rules, "Rules must be a dense list") end
end

local function evaluate(rule, snapshot, context)
    local result = {id = rule.id, field = rule.field, passed = false}
    if rule.evaluate then
        result.expected = copy(rule.expected)
        local passed, actual = rule.evaluate(copy(snapshot), copy(context))
        assert(type(passed) == "boolean", "A custom predicate must return a boolean")
        result.passed, result.actual = passed, copy(actual)
    else
        local actual = field(snapshot, rule.field)
        local expected = rule.expected
        if rule.context then expected = field(context, rule.context) end
        result.actual, result.expected = copy(actual), copy(expected)
        assert(actual ~= nil, "Missing observation field: " .. rule.field)
        assert(expected ~= nil, "Missing expected value: " .. rule.id)
        local op = rule.op or "eq"
        if op == "eq" then
            result.passed = equal(actual, expected)
        elseif op == "range" then
            assert(type(actual) == "number" and type(expected) == "table"
                and type(expected[1]) == "number" and type(expected[2]) == "number", "Range needs numeric values")
            result.passed = actual >= expected[1] and actual <= expected[2]
        else
            assert(type(expected) == "table", "one_of needs a value list")
            for _, value in ipairs(expected) do if equal(actual, value) then result.passed = true end end
        end
    end
    return result
end

-- The directory must already exist. Every write/close failure blocks progress.
function M.jsonl(path)
    assert(text(path), "A checkpoint log path is required")
    return function(record)
        local encoded = json.encode(record)
        local file = assert(io.open(path, "a"))
        local ok, message = file:write(encoded, "\n")
        local closed, close_error = file:close()
        assert(ok, message)
        assert(closed, close_error)
        return true
    end
end

function M.new(options)
    assert(type(options) == "table" and text(options.run_id), "A run_id is required")
    local run_id = options.run_id
    local emulator, reader = options.emulator, options.reader
    assert(type(emulator) == "table" and type(reader) == "table", "Emulator and reader adapters are required")
    for _, name in ipairs({"pause", "release", "frame", "epoch"}) do
        assert(type(emulator[name]) == "function", "Missing emulator operation: " .. name)
    end
    assert(type(reader.snapshot) == "function" and type(options.log) == "function", "Snapshot and log callbacks are required")
    assert(options.save == nil or type(options.save) == "function", "save must be a callback")
    local definitions, limits = copy(options.definitions, true), {}
    assert(type(definitions) == "table" and next(definitions), "Checkpoint definitions are required")
    for id, definition in pairs(definitions) do
        assert(text(id) and type(definition) == "table", "Invalid checkpoint definition")
        assert(definition.read == nil or type(definition.read) == "table", "Read selection must be a table")
        assert(definition.save_state == nil or type(definition.save_state) == "boolean", "save_state must be a boolean")
        validate_rules(definition.checks)
        assert(type(definition.branches) == "table", "Checkpoint branches are required")
        if definition.target then
            validate_rules(definition.target)
            assert(text(definition.branches.hit) and text(definition.branches.miss), "Target branches need hit and miss")
        else
            assert(text(definition.branches.pass), "Checkpoint needs a pass branch")
        end
        if definition.retry then
            local retry = definition.retry
            assert(definition.target and text(retry.key) and whole(retry.max_retries), "Retry guard needs a key and explicit nonnegative maximum")
            assert(limits[retry.key] == nil or limits[retry.key] == retry.max_retries, "Conflicting retry limits")
            limits[retry.key] = retry.max_retries
        end
    end
    local state = copy(options.state or {run_id = run_id, visits = 0, retries = {},
        checkpoints = {}, limits = limits, halted = false})
    assert(state.run_id == run_id and equal(state.limits, limits), "Restored run identity or retry limits differ")
    assert(whole(state.visits) and type(state.retries) == "table" and type(state.checkpoints) == "table"
        and type(state.halted) == "boolean", "Invalid checkpoint state")
    for key, count in pairs(state.retries) do
        assert(limits[key] ~= nil and whole(count) and count <= limits[key], "Invalid restored retry count")
    end
    local engine, pending = {}, nil

    local function stable(record)
        assert(emulator.frame() == record.frame, "Emulator frame changed inside checkpoint")
        assert(emulator.epoch() == record.epoch, "Emulator state changed inside checkpoint")
        if emulator.is_paused then assert(emulator.is_paused() == true, "Emulator is not paused") end
    end

    local function log(record)
        assert(options.log(copy(record)) == true, "Checkpoint log was not acknowledged")
    end

    local function abort(record, message)
        record.status, record.decision, record.proceed, record.branch = "complete", "abort", false, nil
        record.error = tostring(message)
        state.halted, pending = true, nil
        pcall(emulator.pause)
        pcall(emulator.release)
        local ok, failure = pcall(log, record)
        if not ok then record.log_error = tostring(failure) end
        return copy(record)
    end

    function engine:state()
        assert(not pending, "Finish the pending checkpoint before exporting state")
        return copy(state)
    end

    -- complete() acknowledges saved evidence or an explicitly log-only visit.
    -- It does not sample memory or evaluate the same checkpoint a second time.
    function engine:complete(visit, receipt)
        assert(pending, "There is no pending checkpoint")
        local record, definition = pending.record, pending.definition
        local ok, message = pcall(function()
            stable(record)
            assert(visit == record.visit and type(receipt) == "table", "Wrong checkpoint acknowledgement")
            assert(receipt.run_id == record.run_id and receipt.visit == record.visit
                and receipt.frame == record.frame and receipt.epoch == record.epoch, "Stale checkpoint acknowledgement")
            assert(text(receipt.path) and receipt.emulator_paused == true, "Checkpoint receipt needs a retained path and confirmed pause")
            if record.save_required then
                assert(receipt.saved ~= false, "This checkpoint requires saved-state evidence")
            else
                assert(receipt.saved == false, "A log-only checkpoint needs an explicit state-free receipt")
            end
            if record.decision == "abort" and record.save_required then
                for _, checkpoint in pairs(state.checkpoints) do
                    assert(not checkpoint.savestate or receipt.path ~= checkpoint.savestate.path,
                        "Failure evidence must preserve valid checkpoints")
                end
            end
            record.acknowledgement = copy(receipt)
            if record.save_required then record.savestate = copy(receipt) end
            record.status = "complete"
            record.proceed = record.decision ~= "abort"
            log(record)
            stable(record)
        end)
        if not ok then return abort(record, message) end
        if record.proceed then
            if definition.retry and record.decision == "miss" then
                state.retries[definition.retry.key] = record.retries_used + 1
            end
            state.last_valid = {checkpoint = record.checkpoint, visit = record.visit,
                snapshot = copy(record.snapshot), context = copy(record.context),
                acknowledgement = copy(receipt), savestate = copy(record.savestate)}
            state.checkpoints[record.checkpoint] = copy(state.last_valid)
        else
            state.halted = true
        end
        pending = nil
        return copy(record)
    end

    function engine:visit(id, context)
        assert(not pending, "Acknowledge the current checkpoint before another visit")
        assert(not state.halted, "Checkpoint engine halted; investigate before starting a corrected run")
        local definition = assert(definitions[id], "Unknown checkpoint: " .. tostring(id))
        state.visits = state.visits + 1
        local record = {schema = 1, run_id = run_id, checkpoint = id, visit = state.visits,
            status = definition.save_state == false and "awaiting_ack" or "awaiting_save",
            save_required = definition.save_state ~= false, decision = "abort", proceed = false,
            checks = setmetatable({}, json.array_metatable), targets = setmetatable({}, json.array_metatable),
            last_valid = copy(state.last_valid)}
        local ok, message = pcall(function()
            emulator.pause()
            emulator.release()
            record.frame, record.epoch = emulator.frame(), emulator.epoch()
            assert(whole(record.frame) and whole(record.epoch), "Invalid emulator frame or state epoch")
            stable(record)
            record.context = copy(context or {})
            record.snapshot = copy(reader.snapshot(record.frame, copy(definition.read or {})))
            stable(record)
            assert(type(record.snapshot) == "table" and record.snapshot.frame == record.frame, "Snapshot frame is missing or stale")
            assert(type(record.snapshot.game) == "table" and record.snapshot.game.supported == true, "Unsupported game observation")
            local function check(rules, output)
                local passed = true
                for _, rule in ipairs(rules) do
                    local evaluated, result = pcall(evaluate, rule, record.snapshot, record.context)
                    if not evaluated then
                        output[#output + 1] = {id = rule.id, field = rule.field, passed = false, error = tostring(result)}
                        error(result)
                    end
                    output[#output + 1] = result
                    stable(record)
                    passed = passed and result.passed
                end
                return passed
            end
            assert(check(definition.checks, record.checks), "Checkpoint invariant failed")
            if definition.target then
                record.decision = check(definition.target, record.targets) and "hit" or "miss"
            else
                record.decision = "pass"
            end
            if definition.retry then
                local retry = definition.retry
                record.retries_used, record.max_retries = state.retries[retry.key] or 0, retry.max_retries
                record.attempt = record.retries_used + 1
                if record.decision == "miss" and record.retries_used >= retry.max_retries then
                    record.decision, record.error = "abort", "Maximum retry count exhausted"
                end
            end
            record.branch = definition.branches[record.decision]
        end)
        if not ok then
            record.decision, record.branch, record.error = "abort", nil, tostring(message)
            pcall(emulator.pause)
            pcall(emulator.release)
        end
        pending = {record = record, definition = definition}
        local logged, failure = pcall(function() log(record); stable(record) end)
        if not logged then return abort(record, failure) end
        if options.save and record.save_required then
            local saved, receipt = pcall(function()
                stable(record)
                return options.save(copy(record))
            end)
            if not saved then return abort(record, receipt) end
            return self:complete(record.visit, receipt)
        end
        return copy(record)
    end

    function engine:cancel(reason)
        assert(pending, "There is no pending checkpoint")
        return abort(pending.record, reason or "Checkpoint cancelled")
    end

    return engine
end

return M
