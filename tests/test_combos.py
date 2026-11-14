"""Reject an invalid shared input plan before any of its inputs execute."""

import unittest

from tests.support.bridge import BridgeClock


class ContinuousComboChecks(unittest.TestCase):
    def setUp(self):
        self.clock = BridgeClock()
        self.addCleanup(self.clock.close)

    def assert_endpoint(self, status: dict, frame: int, reason: str):
        self.assertEqual(status["frame"], frame)
        self.assertEqual(status["command_end_frame"], frame)
        self.assertEqual(status["screen_frame"], frame)
        self.assertEqual(status["stop_reason"], reason)
        self.assertTrue(status["command_done"])
        self.assertEqual(status["buttons"], "")
        self.assertEqual(self.clock.pauses(), 1)

    def test_invalid_later_step_rejects_the_entire_plan_before_any_input(self):
        self.clock.command("advance", 1000)
        self.clock.reset_measurement()
        status = self.clock.command("combo", "inputs", "A:10:20", "B:1:0")
        self.assertTrue(status["error"])
        self.assertEqual(status["combo_history"], [])
        self.assertEqual(self.clock.trace(), [])
        self.assert_endpoint(status, 1000, "command_error")


if __name__ == "__main__":
    unittest.main(verbosity=2)
