"""Configured paths, paused transport, stale loads, and native slot regressions."""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from host.controller import Controller, ControllerConfig
from host.emulator import DeSmuME
from tests.support.controller import SlotController
from tests.support.paths import temporary_directory


class ConfiguredConnectionChecks(unittest.TestCase):
    def test_emulator_file_paths_use_the_configured_root(self):
        with temporary_directory() as folder:
            root = Path(folder)
            controller = Controller(ControllerConfig(root=root), windows_api=SimpleNamespace())
            session = {"window": 11, "rom": str(root / "game.nds")}
            controller.launch = Mock(return_value=session)
            controller.load_lua = Mock(return_value={})
            controller.screenshot = Mock(return_value={})
            controller.session = Mock(return_value=session)
            controller.set_pause = Mock()
            controller.state_changed = Mock()
            emulator = DeSmuME(controller)

            emulator.launch("game.nds", "lua/input_bridge.lua")
            controller.launch.assert_called_once_with(root / "game.nds")
            controller.load_lua.assert_called_once_with(root / "lua" / "input_bridge.lua")
            emulator.attach("contest/super_contest.lua")
            controller.load_lua.assert_called_with(root / "contest" / "super_contest.lua")
            emulator.screenshot("capture.png")
            controller.screenshot.assert_called_once_with(root / "capture.png")

            save = root / "prepared.dsv"
            save.write_bytes(b"prepared battery save")
            with patch.object(emulator, "stop_movie"), patch("host.rtc.start_recording") as record:
                result = emulator.start_rtc("2077-01-01 00:00:00", "boot.dsm", "prepared.dsv")
            record.assert_called_once()
            self.assertEqual(record.call_args.args[2:], (root / "boot.dsm", save))
            self.assertEqual(result["movie_path"], str(root / "boot.dsm"))
            self.assertEqual(result["sram_path"], str(save))


class PausedCheckpointTransportChecks(unittest.TestCase):
    def setUp(self):
        temporary = temporary_directory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.api = SimpleNamespace(send=Mock(side_effect=self.dispatch), post_menu=Mock())
        self.controller = Controller(ControllerConfig(mailbox_dir=self.work), windows_api=self.api)
        self.controller.session = Mock(return_value={"pid": 7, "window": 11})
        self.controller.set_pause = Mock()
        self.controller.paused = Mock(return_value=True)

    def dispatch(self, window, message, wparam=0, lparam=0, **options):
        if message == 0x211:
            identifier = self.controller.CHECKPOINT_COMMAND.read_text(encoding="utf-8").split("|", 1)[0]
            self.controller.CHECKPOINT_STATUS.write_text(json.dumps({
                "command_id": identifier, "command_done": True, "frame": 80, "epoch": 3,
            }), encoding="utf-8")
        return 0

    def test_paused_rpc_uses_synchronous_menu_callbacks_and_never_resumes(self):
        result = self.controller.checkpoint_command("visit", [])
        self.assertEqual((result["frame"], result["epoch"]), (80, 3))
        self.assertTrue(result["emulator_paused"])
        self.assertEqual([call.args[1] for call in self.api.send.call_args_list], [0x211, 0x212])
        self.api.post_menu.assert_not_called()
        self.assertFalse(self.controller.COMMAND.exists())


class StateSlotChecks(unittest.TestCase):
    def setUp(self):
        temporary = temporary_directory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.controller = SlotController(self.work)
        self.emulator = DeSmuME(self.controller)
        self.baseline = deepcopy(self.controller.observation)

    def test_frozen_load_restores_the_state_and_marks_the_old_publication_stale(self):
        self.emulator.save_state(2)
        self.controller.observation.update(frame=200, held_item_id=261)
        self.controller.publish()
        result = self.emulator.load_state(2, refresh=False)
        self.assertTrue(result["observation_stale"])
        self.assertTrue(result["emulator_paused"])
        self.assertEqual(self.controller.observation["frame"], 100)
        self.assertEqual(self.controller.status()["frame"], 200)
        self.assertEqual(self.controller.bridge_calls, [])

    def test_timestamped_slots_can_be_saved_again_and_loaded_frozen(self):
        for slot in (1, 2, 3, 4, 5, 6):
            with self.subTest(slot=slot):
                first = self.emulator.save_state(slot)
                self.assertEqual(Path(first["path"]).suffix, f".ds{slot}")
                self.assertIn("30-Oct-2026 16:22:16", self.emulator.states.menu("Save", slot)["text"])
                self.assertTrue(self.emulator.states.menu("Save", slot)["text"].endswith(f"\tShift+F{slot}"))
                second = self.emulator.save_state(slot)
                loaded = self.emulator.load_state(slot, refresh=False)
                self.assertEqual(first["path"], second["path"])
                self.assertEqual(loaded["path"], first["path"])
                self.assertTrue(loaded["observation_stale"])
                self.assertTrue(self.controller.is_paused)
                self.assertEqual(self.controller.observation, self.baseline)
        self.assertEqual(self.controller.bridge_calls, [])

    def test_slot_ten_uses_native_zero_label_and_file_for_save_and_load(self):
        first = self.emulator.save_state(10)
        self.assertEqual(Path(first["path"]).name, "Prepared Platinum.ds0")
        self.assertFalse((self.controller.EMULATOR / "StateSlots" / "Prepared Platinum.ds10").exists())
        self.assertIn("/Save State/&0    30-Oct-2026 16:22:16", self.emulator.states.menu("Save", 10)["text"])
        self.emulator.save_state(10)
        loaded = self.emulator.load_state(10, refresh=False)
        self.assertEqual(loaded["path"], first["path"])
        self.assertTrue(loaded["observation_stale"])
        self.assertTrue(self.controller.is_paused)
        self.assertEqual(self.controller.menu_calls, [("Save", 10), ("Save", 10), ("Load", 10)])
        self.assertEqual(self.controller.bridge_calls, [])
        self.assertEqual(self.controller.observation, self.baseline)


if __name__ == "__main__":
    unittest.main(verbosity=2)
