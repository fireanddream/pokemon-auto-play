"""Startup advisory thresholds and independently encoded Fashion Case memory."""

from copy import deepcopy
import json
import unittest

from contest import sections, stage2_prep
from tests.support.lua51 import Lua51, literal
from tests.support.paths import ROOT


SAVE = 0x02040000
PAGE = SAVE + 0x20024 + 14 * 16
LOCATION = 0x5000
FASHION = SAVE + 0x14 + LOCATION + 0x7F4


class AccessoryMemoryChecks(unittest.TestCase):
    def setUp(self):
        self.lua = Lua51()
        self.addCleanup(self.lua.close)

    def fixture(self, packed=None, *, changes=None):
        values = {PAGE: 14, PAGE + 4: 0x83C, PAGE + 8: LOCATION}
        values.update({FASHION + offset: value for offset, value in (packed or {}).items()})
        values.update(changes or {})
        self.lua.run(f"""
            values = {literal(values)}
            reads = 0
            local function read(address) reads = reads + 1; return values[address] or 0 end
            memory = {{readdwordunsigned=read, readword=read, readbyte=read}}
            platinum = dofile({literal((ROOT / 'lua/platinum.lua').as_posix())})
            return 'ready'
        """)

    def read(self, save=SAVE):
        return json.loads(self.lua.run(f"return dofile({literal((ROOT / 'lua/json.lua').as_posix())})"
            f".encode(platinum.read_accessories(memory, {save}, 77))"))

    def test_packed_copies_single_flags_padding_and_backdrops(self):
        # Five multi-copy IDs own 9/8/7/6/5; five single-copy IDs own one each.
        packed = {0: 0x89, 1: 0x07, 29: 0x60, 30: 0xF5, 31: 0xFF,
            0x20: 0x81, 0x21: 0x01, 0x24: 0xE0, 0x25: 0xFF, 0x26: 0xFF, 0x27: 0xFF}
        packed.update({offset: 0xFF for offset in range(0x28, 0x48)})
        self.fixture(packed)
        result = self.read()
        self.assertTrue(result["read_valid"])
        self.assertEqual((result["total_owned"], result["distinct_owned"]), (40, 10))
        self.assertEqual((result["page_id"], result["page_size"], result["page_location"]), (14, 0x83C, LOCATION))
        self.assertEqual((result["address"], result["save_base"]), (FASHION, SAVE))
        self.assertEqual((result["frame"], result["direct_read_frames"]), (77, 0))

    def test_empty_and_maximum_ownership(self):
        empty = {30: 0xF0, 31: 0xFF, 0x24: 0x80, 0x25: 0xFF, 0x28: 0xFF}
        maximum = {offset: 0x99 for offset in range(30)}
        maximum.update({30: 0xF9, **{offset: 0xFF for offset in range(0x20, 0x28)}})
        for packed, total, distinct in ((empty, 0, 0), (maximum, 588, 100)):
            with self.subTest(total=total):
                self.fixture(packed)
                result = self.read()
                self.assertTrue(result["read_valid"])
                self.assertEqual((result["total_owned"], result["distinct_owned"]), (total, distinct))

    def test_unreadable_data_retains_details_without_inventing_a_count(self):
        cases = ((0, {}), (SAVE + 1, {}), (0x023E0000, {}),
            (SAVE, {PAGE: 3}), (SAVE, {PAGE + 4: 0x83B}),
            (SAVE, {PAGE + 8: LOCATION + 1}), (SAVE, {PAGE + 8: 0x1FF00}),
            (SAVE, {FASHION: 0x0A}), (SAVE, {FASHION: 0xA0}))
        for save, changes in cases:
            with self.subTest(save=save, changes=changes):
                self.fixture(changes=changes)
                result = self.read(save)
                self.assertFalse(result["read_valid"])
                self.assertNotIn("total_owned", result)
                self.assertTrue(result["error"])
                self.assertEqual((result["source"], result["frame"], result["direct_read_frames"]),
                    ("platinum_fashion_case", 77, 0))
        self.fixture()
        self.lua.run("memory.readbyte=function() error('Accessory read failed') end; return 'ready'")
        result = self.read()
        self.assertFalse(result["read_valid"])
        self.assertEqual(result["address"], FASHION)
        self.assertIn("Accessory read failed", result["error"])
        self.assertNotIn("total_owned", result)

    def test_startup_bag_reads_use_the_regional_save_pointer(self):
        for code, pointer in (("CPUJ", 0x021BFB94), ("CPUE", 0x021C0794)):
            with self.subTest(code=code):
                self.fixture({0: 0x55}, changes={pointer: SAVE,
                    SAVE + 0x20024 + 3 * 16: 3, SAVE + 0x20028 + 3 * 16: 1916,
                    SAVE + 0x2002C + 3 * 16: 0x1000,
                    SAVE + 0x20034: 1, SAVE + 0x20038: 52, SAVE + 0x2003C: 0x64,
                    SAVE + 0x78: 2})
                result = json.loads(self.lua.run(f"""
                    local original = dofile
                    dofile = function(path)
                        if path:match('/party_contest%.lua$') then
                            return {{new=function() return {{read_first=function(frame)
                                return {{frame=frame, held_item_id=0, read_valid=true}}
                            end}} end}}
                        end
                        return original(path)
                    end
                    local reader = dofile({literal((ROOT / 'contest/bag_contest.lua').as_posix())})
                        .new(memory, function() return {literal(code)} end)
                    local startup = reader.snapshot(77, true)
                    local later = reader.snapshot(77)
                    dofile = original
                    return dofile({literal((ROOT / 'lua/json.lua').as_posix())})
                        .encode({{startup=startup, later=later}})
                """))
                startup, later = result["startup"], result["later"]
                self.assertEqual(startup["game"]["code"], code)
                self.assertTrue(startup["accessories"]["read_valid"])
                self.assertEqual(startup["accessories"]["total_owned"], 10)
                self.assertEqual(startup["accessories"]["save_base"], SAVE)
                self.assertEqual(startup["options"]["text_speed"], "Fast")
                self.assertEqual(startup["access"]["direct_read_frames"], 0)
                self.assertNotIn("accessories", later)
                self.assertNotIn("options", later)


class ReadinessWarningChecks(unittest.TestCase):
    def snapshot(self, *, copies=10, sheen=255, held=0, inventory=None):
        if inventory is None:
            inventory = {item_id: 1 for item_id in stage2_prep.SCARVES}
        return {"party": {"sheen": sheen, "held_item_id": held},
            "accessories": {"read_valid": True, "total_owned": copies},
            "bag": {"items": [{"item_id": item_id, "quantity": quantity} for item_id, quantity in inventory.items()]}}

    def test_strict_thresholds_and_independent_warnings(self):
        cases = ((0, 255, ("accessory",)), (9, 255, ("accessory",)), (10, 255, ()),
            (588, 255, ()), (10, 0, ("Sheen",)), (10, 1, ("Sheen",)),
            (10, 254, ("Sheen",)), (9, 254, ("accessory", "Sheen")))
        for copies, sheen, expected in cases:
            with self.subTest(copies=copies, sheen=sheen):
                snapshot = self.snapshot(copies=copies, sheen=sheen)
                original = deepcopy(snapshot)
                warnings = sections.readiness_warnings(snapshot)
                self.assertEqual(len(warnings), len(expected))
                for warning, field in zip(warnings, expected):
                    self.assertIn(field, warning)
                    self.assertIn("Continuing.", warning)
                self.assertEqual(snapshot, original)

    def test_each_first_held_scarf_counts_but_other_party_slots_do_not(self):
        for held, name in stage2_prep.SCARVES.items():
            with self.subTest(item=name):
                inventory = {item_id: 999 for item_id in stage2_prep.SCARVES if item_id != held}
                self.assertEqual(sections.readiness_warnings(self.snapshot(held=held, inventory=inventory)), [])
                snapshot = self.snapshot(inventory=inventory)
                snapshot["other_party"] = [{"held_item_id": held}]
                warnings = sections.readiness_warnings(snapshot)
                self.assertEqual(len(warnings), 1)
                self.assertIn(name, warnings[0])
                for other in inventory:
                    self.assertNotIn(stage2_prep.SCARVES[other], warnings[0])

    def test_missing_colors_are_combined_and_zero_quantity_is_not_ownership(self):
        warnings = sections.readiness_warnings(self.snapshot(held=260, inventory={260: 0, 261: 1, 264: 0}))
        self.assertEqual(len(warnings), 1)
        self.assertIn("Pink Scarf, Green Scarf, Yellow Scarf", warnings[0])
        self.assertNotIn("Red Scarf", warnings[0])
        self.assertNotIn("Blue Scarf", warnings[0])
        warning = sections.readiness_warnings(self.snapshot(held=1, inventory={}))[0]
        for name in stage2_prep.SCARVES.values():
            self.assertIn(name, warning)

    def test_unreadable_accessories_are_unknown_instead_of_zero(self):
        snapshot = self.snapshot()
        snapshot["accessories"] = {"read_valid": False, "total_owned": 0, "error": "Unreadable page"}
        warnings = sections.readiness_warnings(snapshot)
        self.assertEqual(len(warnings), 1)
        self.assertIn("could not be read reliably", warnings[0])
        self.assertNotIn("Only 0", warnings[0])
        self.assertEqual(snapshot["accessories"]["error"], "Unreadable page")


if __name__ == "__main__":
    unittest.main()
