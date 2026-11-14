"""Shared mailbox replay protection and explicit input release through real Lua."""

import json
import unittest

from tests.support.bridge import BridgeClock
from tests.support.lua51 import literal
from tests.support.paths import ROOT


BRIDGES = ("input_bridge.lua",)
BUTTONS = ("debug", "R", "L", "X", "Y", "A", "B", "start", "select", "up", "down", "left", "right", "lid")


class BridgeMailboxChecks(unittest.TestCase):
    def clock(self, script):
        clock = BridgeClock(ROOT / "lua" / script)
        self.addCleanup(clock.close)
        clock.reset_measurement()
        return clock

    def test_attachment_ignores_old_commands_and_completed_ids_do_not_replay(self):
        for script in BRIDGES:
            with self.subTest(script=script):
                clock = self.clock(script)
                initial = json.loads(clock.lua.run(f"""
                    command_text = '900|reset'
                    dofile({literal((ROOT / 'lua' / script).as_posix())})
                    return latest_status
                """))
                self.assertEqual(initial["command_id"], "900")
                clock.reset_measurement()
                self.assertEqual(clock.lua.run("mock_paused = false; tick(); tick(); return tostring(frame)"), "2")
                self.assertEqual(clock.pauses(), 0)
                status = clock.command("advance", 2)
                self.assertEqual(status["frame"], 4)
                self.assertEqual(status["command_id"], "1")
                self.assertEqual(clock.lua.run("mock_paused = false; tick(); tick(); tick(); return tostring(frame)"), "7")
                self.assertEqual(clock.pauses(), 1)

    def test_each_press_and_release_explicitly_clears_unselected_buttons(self):
        for script in BRIDGES:
            with self.subTest(script=script):
                clock = self.clock(script)
                clock.command("advance", 301)
                clock.reset_measurement()
                clock.lua.run(f"""
                    local buttons = {{}}
                    for _, name in ipairs({literal(BUTTONS)}) do buttons[name] = true end
                    joypad.set(buttons)
                    return 'ready'
                """)
                status = clock.command("press", "B", 2, 1)
                self.assertEqual(clock.trace(), ["B", "B", ""])
                self.assertEqual(status["frame"], 304)
                self.assertEqual(status["buttons"], "")
                clock.lua.run(f"""
                    local buttons = joypad.get()
                    for _, name in ipairs({literal(BUTTONS)}) do assert(buttons[name] == false, name) end
                    return 'passed'
                """)


if __name__ == "__main__":
    unittest.main(verbosity=2)
