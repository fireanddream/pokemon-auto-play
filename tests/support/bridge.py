"""Mock emulator callbacks and memory while executing the actual Lua bridges."""

from __future__ import annotations

import json

from tests.support.lua51 import Lua51, literal
from tests.support.paths import ROOT


class BridgeClock:
    """Only the emulator and memory are mocked; all command execution is real Lua."""

    def __init__(self, script=None, game_code: str = "CPUJ"):
        self.lua = Lua51()
        self.lua.run("""
            memory_values, command_text, frame, latest_status = {}, nil, 0, ''
            checkpoint_text, latest_checkpoint_status = nil, ''
            mock_memory_reads = 0
            mock_game_code = 'CPUJ'
            mock_paused, mock_pause_count, mock_trace, mock_events = false, 0, {}, {}
            local buttons = {}
            mock_files = {}
            io.open = function(path, mode)
                if mode == 'r' then
                    if path:match('checkpoint%-command%.txt$') then
                        if not checkpoint_text then return nil end
                        return {read = function() return checkpoint_text end, close = function() return true end}
                    end
                    if not path:match('command%.txt$') or not command_text then return nil end
                    return {read = function() return command_text end, close = function() end}
                end
                local content = mode == 'a' and (mock_files[path] or '') or ''
                return {
                    write = function(self, ...)
                        for index = 1, select('#', ...) do content = content .. tostring(select(index, ...)) end
                        mock_files[path] = content
                        if path:match('checkpoint%-status%.json$') then latest_checkpoint_status = content
                        elseif path:match('status%.json$') then latest_status = content end
                        return self
                    end,
                    close = function() return true end,
                }
            end
            local function read(address) mock_memory_reads = mock_memory_reads + 1; return memory_values[address] or 0 end
            memory = {readdwordunsigned = read, readword = read, readbyte = read}
            emu = {
                gamecode = function() return mock_game_code end,
                framecount = function() return frame end,
                reset = function()
                    memory_values, frame, buttons = {}, 0, {}
                    local header = 0
                    for index = 1, 4 do header = header + mock_game_code:byte(index) * 2 ^ ((index - 1) * 8) end
                    memory_values[0x02FFFE0C] = header
                end,
                pause = function() mock_paused = true; mock_pause_count = mock_pause_count + 1 end,
                registerbefore = function(callback) before_frame = callback end,
                registerafter = function(callback) after_frame = callback end,
                registermenustart = function(callback) menu_start = callback end,
            }
            joypad = {get = function() return buttons end, set = function(value) buttons = value end}
            gui = {gdscreenshot = function() return 'mock framebuffer' end, register = function() end}
            print = function() end
            function tick()
                before_frame()
                if mock_paused then return end
                frame = frame + 1
                memory_values[0x02FFFC3C] = frame
                for address, value in pairs(mock_events[frame] or {}) do memory_values[address] = value end
                local pressed = {}
                for name, held in pairs(buttons) do if held then pressed[#pressed + 1] = name end end
                table.sort(pressed)
                mock_trace[#mock_trace + 1] = table.concat(pressed, '+')
                after_frame()
            end
            local command_number = 0
            function execute(action, arguments, limit)
                command_number = command_number + 1
                command_text = command_number .. '|' .. action
                for _, argument in ipairs(arguments) do command_text = command_text .. '|' .. argument end
                mock_paused = false
                local count = 0
                while not mock_paused and count < limit do tick(); count = count + 1 end
                assert(mock_paused, 'Command did not stop within the frame limit')
                return latest_status
            end
            function checkpoint_command(action, arguments)
                command_number = command_number + 1
                checkpoint_text = command_number .. '|' .. action
                for _, argument in ipairs(arguments) do checkpoint_text = checkpoint_text .. '|' .. argument end
                mock_paused = true
                local start_frame = frame
                menu_start()
                assert(mock_paused and frame == start_frame, 'Paused RPC advanced or resumed the game')
                return latest_checkpoint_status
            end
            return 'ready'
        """)
        self.lua.run(f"mock_game_code = {literal(game_code)}; return 'ready'")
        script = script if script is not None else ROOT / "lua" / "input_bridge.lua"
        self.lua.run(f"dofile({literal(script.as_posix())}); return latest_status")

    def close(self):
        self.lua.close()

    def events(self, changes: dict):
        self.lua.run(f"mock_events = {literal(changes)}; return 'ready'")

    def command(self, action: str, *arguments: object, limit: int = 10000) -> dict:
        return json.loads(self.lua.run(f"return execute({literal(action)}, {literal([str(item) for item in arguments])}, {limit})"))

    def checkpoint(self, action: str, *arguments: object) -> dict:
        return json.loads(self.lua.run(f"return checkpoint_command({literal(action)}, {literal([str(item) for item in arguments])})"))

    def reset_measurement(self):
        self.lua.run("mock_trace, mock_pause_count, mock_events = {}, 0, {}; return 'ready'")

    def trace(self) -> list[str]:
        count, content = self.lua.run("return #mock_trace .. '\\n' .. table.concat(mock_trace, '\\n')").split("\n", 1)
        return content.split("\n") if int(count) else []

    def pauses(self) -> int:
        return int(self.lua.run("return tostring(mock_pause_count)"))

    def file_text(self, name: str) -> str:
        return self.lua.run(f"""
            local name = {literal(name)}
            for path, content in pairs(mock_files) do
                if path:sub(-#name) == name then return content end
            end
            return ''
        """)


def tap(button: str, hold: int, released: int) -> list[str]:
    return [button] * hold + [""] * released
