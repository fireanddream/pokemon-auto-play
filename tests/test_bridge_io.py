"""Shared mailbox replay protection and explicit input release through real Lua."""

import json
import unittest

from tests.support.bridge import BridgeClock
from tests.support.lua51 import literal
from tests.support.paths import ROOT


BUTTONS = ("debug", "R", "L", "X", "Y", "A", "B", "start", "select", "up", "down", "left", "right", "lid")


class BridgeMailboxChecks(unittest.TestCase):
    def clock(self):
        clock = BridgeClock()
        self.addCleanup(clock.close)
        clock.reset_measurement()
        return clock

    def test_attachment_ignores_old_commands_and_completed_ids_do_not_replay(self):
        clock = self.clock()
        initial = json.loads(clock.lua.run(f"""
            command_text = '900|reset'
            dofile({literal((ROOT / 'lua/input_bridge.lua').as_posix())})
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
        clock = self.clock()
        clock.lua.run(f"""
            local buttons = {{}}
            for _, name in ipairs({literal(BUTTONS)}) do buttons[name] = true end
            joypad.set(buttons)
            return 'ready'
        """)
        status = clock.command("press", "B", 2, 1)
        self.assertEqual(clock.trace(), ["B", "B", ""])
        self.assertEqual(status["frame"], 3)
        self.assertEqual(status["buttons"], "")
        clock.lua.run(f"""
            local buttons = joypad.get()
            for _, name in ipairs({literal(BUTTONS)}) do assert(buttons[name] == false, name) end
            return 'passed'
        """)

    def test_invalid_later_step_rejects_the_entire_plan_before_any_input(self):
        clock = self.clock()
        clock.command("advance", 8)
        clock.reset_measurement()
        status = clock.command("combo", "inputs", "A:10:20", "B:1:0")
        self.assertTrue(status["error"])
        self.assertEqual(status["combo_history"], [])
        self.assertEqual(clock.trace(), [])
        self.assertEqual((status["frame"], status["command_end_frame"], status["screen_frame"]), (8, 8, 8))
        self.assertEqual(status["stop_reason"], "command_error")
        self.assertTrue(status["command_done"])
        self.assertEqual(status["buttons"], "")
        self.assertEqual(clock.pauses(), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
