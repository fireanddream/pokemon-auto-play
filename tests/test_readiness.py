"""Startup advisories, independently encoded memory, and read-only failure details."""

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


class PartyValidationDiagnosticChecks(unittest.TestCase):
    def setUp(self):
        self.lua = Lua51()
        self.addCleanup(self.lua.close)

    def fixture(self, code, *, actual_plain, flagged_plain, invalid=False, damaged=False):
        data = 0x02080000
        address = data + 0xD094
        words = [0] * 64
        words[0], words[1], words[13] = 330, 260, 255 * 256
        words[16:20] = [49, 98, 225, 432]
        words[44] = 1
        stored = sum(words) % 0x10000
        if damaged:
            words[1] += 1
        encoded, key = [], stored
        for word in words:
            key = (key * 0x41C64E6D + 0x6073) % 0x100000000
            encoded.append(word if actual_plain else word ^ (key >> 16))
        data_pointer, trainer_pointer = {
            "CPUJ": (0x0210112C, 0x021BFB94), "CPUE": (0x02101D2C, 0x021C0794),
        }[code]
        values = {data_pointer: data, trainer_pointer: SAVE, data + 0xD090: 1,
            address: 0, address + 4: 1 + 2 * flagged_plain + 4 * invalid, address + 6: stored,
            address + 0x8E: 30, address + 0x90: 100}
        values.update({address + 8 + index * 2: word for index, word in enumerate(encoded)})
        self.lua.run(f"""
            values = {literal(values)}
            reads = 0
            local function read(address) reads = reads + 1; return values[address] or 0 end
            local function forbidden() error('Diagnostics must not write memory or control the emulator') end
            memory = {{readdwordunsigned=read, readword=read, readbyte=read,
                writebyte=forbidden, writeword=forbidden, writedword=forbidden}}
            emu = {{frameadvance=forbidden, pause=forbidden, unpause=forbidden}}
            joypad = {{set=forbidden}}
            savestate = {{save=forbidden, load=forbidden}}
            reader = dofile({literal((ROOT / 'contest/party_contest.lua').as_posix())})
                .new(memory, function() return {literal(code)} end)
            return 'ready'
        """)

    def read(self):
        return json.loads(self.lua.run(f"""
            local ok, result = pcall(reader.read_first, 77)
            return dofile({literal((ROOT / 'lua/json.lua').as_posix())})
                .encode({{ok=ok, result=result, reads=reads}})
        """))

    def check_failure(self, result, code, *, body_plain, matches, invalid, alternate=False):
        self.assertFalse(result["ok"])
        self.assertEqual(result["reads"], 71)
        error = result["result"]
        for detail in ("The first Pokemon failed stored-data validation", f"game={code}", "frame=77",
                f"body_encoding={'plain' if body_plain else 'encrypted'}", f", checksum_matches={str(matches).lower()}",
                f"invalid_record_flag={str(invalid).lower()}", "hp_plain_flag=true",
                f"alternate_encoding_checksum_matches={str(alternate).lower()}", "direct_read_frames=0"):
            self.assertIn(detail, error)

    def test_valid_plain_and_encrypted_records_keep_the_original_result(self):
        for code in ("CPUJ", "CPUE"):
            for plain in (False, True):
                with self.subTest(code=code, plain=plain):
                    self.fixture(code, actual_plain=plain, flagged_plain=plain)
                    result = self.read()
                    self.assertTrue(result["ok"])
                    self.assertEqual(result["reads"], 73)
                    party = result["result"]
                    self.assertTrue(party["read_valid"] and party["data_validation_passed"])
                    self.assertEqual((party["frame"], party["direct_read_frames"]), (77, 0))
                    self.assertEqual((party["species_id"], party["held_item_id"], party["owned_count"]), (330, 260, 1))
                    self.assertEqual((party["current_hp"], party["max_hp"], party["sheen"]), (30, 100, 255))
                    self.assertEqual(party["moves"], [49, 98, 225, 432])

    def test_checksum_failures_report_the_declared_encoding_without_extra_reads(self):
        for code in ("CPUJ", "CPUE"):
            for plain in (False, True):
                with self.subTest(code=code, plain=plain):
                    self.fixture(code, actual_plain=plain, flagged_plain=plain, damaged=True)
                    self.check_failure(self.read(), code, body_plain=plain, matches=False, invalid=False)

    def test_invalid_record_flag_is_reported_independently_of_the_checksum(self):
        for plain in (False, True):
            for damaged in (False, True):
                with self.subTest(plain=plain, damaged=damaged):
                    self.fixture("CPUE", actual_plain=plain, flagged_plain=plain, invalid=True, damaged=damaged)
                    self.check_failure(self.read(), "CPUE", body_plain=plain, matches=not damaged, invalid=True)

    def test_an_alternate_encoding_match_is_diagnostic_only_and_still_rejected(self):
        for code in ("CPUJ", "CPUE"):
            for plain in (False, True):
                with self.subTest(code=code, actual_plain=plain):
                    self.fixture(code, actual_plain=plain, flagged_plain=not plain)
                    self.check_failure(self.read(), code, body_plain=not plain, matches=False, invalid=False, alternate=True)


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
