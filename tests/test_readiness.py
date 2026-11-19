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

    def fixture(self, code, *, actual_plain, flagged_plain, invalid=False, damaged=False,
                held=260, ribbon_bits=(0,), moves=(49, 98, 225, 432), sheen=255, hp=(30, 100), encrypted_prefix=None):
        data = 0x02080000
        address = data + 0xD094
        words = [0] * 64
        words[0], words[1], words[13] = 330, held, sheen * 256
        words[16:20] = moves
        bits = sum(1 << bit for bit in ribbon_bits)
        words[44], words[45] = bits & 0xFFFF, bits >> 16
        stored = sum(words) % 0x10000
        if damaged:
            words[1] += 1
        encoded, key = [], stored
        for word in words:
            key = (key * 0x41C64E6D + 0x6073) % 0x100000000
            encoded.append(word if actual_plain else word ^ (key >> 16))
        if encrypted_prefix is not None:
            encoded[encrypted_prefix:] = words[encrypted_prefix:]
        data_pointer, trainer_pointer = {
            "CPUJ": (0x0210112C, 0x021BFB94), "CPUE": (0x02101D2C, 0x021C0794),
        }[code]
        values = {data_pointer: data, trainer_pointer: SAVE, data + 0xD090: 1,
            address: 0, address + 4: 1 + 2 * flagged_plain + 4 * invalid, address + 6: stored,
            address + 0x8E: hp[0], address + 0x90: hp[1]}
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

    def read(self, *, startup_party=None, frame=77):
        return json.loads(self.lua.run(f"""
            local ok, result = pcall(reader.read_first, {frame}, {literal(startup_party)})
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

    def test_contest_finish_reads_current_items_ribbons_but_reuses_startup_only_facts(self):
        for code in ("CPUJ", "CPUE"):
            for plain in (False, True):
                with self.subTest(code=code, plain=plain):
                    self.fixture(code, actual_plain=plain, flagged_plain=plain)
                    startup = self.read()["result"]
                    self.fixture(code, actual_plain=plain, flagged_plain=plain, held=261,
                        ribbon_bits=(0, 4), moves=(0, 0, 0, 0), sheen=0, hp=(0, 0))
                    result = self.read(startup_party=startup, frame=88)
                    self.assertTrue(result["ok"])
                    self.assertEqual(result["reads"], 71)
                    party = result["result"]
                    self.assertEqual((party["held_item_id"], party["owned_ribbon_bits"]), (261, [0, 4]))
                    self.assertEqual((party["frame"], party["static_read_frame"], party["read_scope"]), (88, 77, "items_and_ribbons"))
                    for field in ("is_egg", "current_hp", "max_hp", "hp_encoding", "sheen", "moves", "move_count", "unavailable_move_slots"):
                        self.assertEqual(party[field], startup[field])

    def test_startup_facts_cannot_bypass_a_partially_encrypted_contest_finish_record(self):
        for code in ("CPUJ", "CPUE"):
            with self.subTest(code=code):
                self.fixture(code, actual_plain=False, flagged_plain=False)
                startup = self.read()["result"]
                self.fixture(code, actual_plain=False, flagged_plain=False, held=261, encrypted_prefix=41)
                self.check_failure(self.read(startup_party=startup), code, body_plain=False, matches=False, invalid=False)

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


class AcceptedPartyHandoffChecks(unittest.TestCase):
    def setUp(self):
        self.lua = Lua51()
        self.addCleanup(self.lua.close)
        self.lua.run(f"boundary=dofile({literal((ROOT / 'contest/memory_boundary.lua').as_posix())}); "
            f"json=dofile({literal((ROOT / 'lua/json.lua').as_posix())}); return 'ready'")

    def context(self, code="CPUJ"):
        return {"game_code": code,
            "party": {"frame": 77, "read_valid": True, "data_validation_passed": True, "party_slot": 1,
                "species_id": 330, "held_item_id": 260, "is_egg": False, "current_hp": 30, "max_hp": 100,
                "sheen": 255, "moves": [49, 0, 432, 0], "move_count": 2, "unavailable_move_slots": [2, 4],
                "ribbon_flags": [False] * 20},
            "memory_read": {"frame": 77, "epoch": 2, "source": "x_menu_paused_memory", "direct_read_frames": 0},
            "accepted_at": {"checkpoint": "stage-1-load", "run_id": "party-handoff-check", "visit": 1,
                "frame": 281, "epoch": 2, "status": "complete", "proceed": True}}

    def lua_party(self, context, target):
        return json.loads(self.lua.run(f"local ok, result=pcall(boundary.party, {literal(context)}, 'CPUJ', {literal(target)}); "
            "return json.encode({ok=ok, result=result})"))

    def test_acknowledged_party_facts_are_reused_without_entry_memory_reads(self):
        for code in ("CPUJ", "CPUE"):
            with self.subTest(code=code):
                context = self.context(code)
                record = {**context["accepted_at"], "snapshot": {"game": {"code": code},
                    "source_party": context["party"], "memory_read": context["memory_read"]}}
                result = json.loads(self.lua.run(f"local record={literal(record)}; local accepted=boundary.context(record); "
                    f"local party=boundary.party(accepted, {literal(code)}, {{party_slot=1,ribbon_bit=3}}); "
                    "return json.encode({context=accepted,party=party,record=record})"))
                self.assertEqual(result["context"], context)
                self.assertEqual(result["party"], context["party"])
                self.assertEqual(result["record"], record)
        context = self.context()
        self.assertEqual(self.lua_party(context, {"party_slot": 1, "ribbon_bit": 3})["result"], context["party"])

    def test_missing_unaccepted_or_inconsistent_party_facts_are_rejected_by_main_boundary(self):
        changes = (("game_code", "CPUE"), ("party", None), ("memory_read", None), ("accepted_at", None),
            ("accepted_at.checkpoint", "stage-5-dance"), ("accepted_at.status", "awaiting_ack"),
            ("accepted_at.proceed", False), ("accepted_at.run_id", ""), ("accepted_at.visit", 0),
            ("accepted_at.frame", 76), ("accepted_at.epoch", 3), ("memory_read.frame", 78),
            ("memory_read.source", "unprotected_memory"), ("memory_read.direct_read_frames", 1),
            ("memory_read.direct_read_frames", False),
            ("party.read_valid", False), ("party.data_validation_passed", False), ("party.party_slot", 2),
            ("party.party_slot", True),
            ("party.species_id", 0), ("party.held_item_id", 537), ("party.is_egg", True),
            ("party.current_hp", 0), ("party.current_hp", True), ("party.current_hp", 101),
            ("party.max_hp", 715), ("party.sheen", 0), ("party.moves", [49, 0, 432]),
            ("party.moves", [49, 0, 468, 0]), ("party.move_count", 3),
            ("party.unavailable_move_slots", []), ("party.unavailable_move_slots", [4, 2]),
            ("party.unavailable_move_slots", [2, 2]), ("party.ribbon_flags", [False] * 19),
            ("party.ribbon_flags", [0] + [False] * 19))
        target = {"party_slot": 1, "ribbon_bit": 3}
        for path, value in ((None, None), *changes):
            with self.subTest(field=path, value=value):
                context = self.context() if path else None
                if path:
                    fields, parent = path.split("."), context
                    for field in fields[:-1]:
                        parent = parent[field]
                    parent[fields[-1]] = value
                self.assertFalse(self.lua_party(context, target)["ok"])
        for target in ({"party_slot": 2, "ribbon_bit": 3}, {"party_slot": True, "ribbon_bit": 3}, {"party_slot": 1, "ribbon_bit": 20},
                {"party_slot": 1, "ribbon_bit": True}):
            with self.subTest(target=target):
                self.assertFalse(self.lua_party(self.context(), target)["ok"])


class MemoryBoundaryChecks(unittest.TestCase):
    def setUp(self):
        self.lua = Lua51()
        self.addCleanup(self.lua.close)
        self.lua.run(f"boundary=dofile({literal((ROOT / 'contest/memory_boundary.lua').as_posix())}); "
            f"json=dofile({literal((ROOT / 'lua/json.lua').as_posix())}); "
            "frame, epoch, reads=77, 2, 0; adapter={frame=function() return frame end, epoch=function() return epoch end}; "
            "return 'ready'")
        self.state = {"command_done": True, "error": "", "buttons": "", "stylus_touch": False}

    def test_memory_reads_add_zero_frames_and_close_acceptance_needs_no_button_history(self):
        result = json.loads(self.lua.run(f"result=boundary.read({literal(self.state)}, adapter, true, 'test_party', "
            "function(at) reads=reads+1; return {frame=at} end); return json.encode(result)"))
        self.assertEqual(result, {"frame": 77, "epoch": 2, "memory_read": {"frame": 77, "epoch": 2,
            "source": "x_menu_paused_memory", "scope": "test_party", "direct_read_frames": 0}})
        closed = {**self.state, "stop_reason": "combo_complete", "command_start_frame": 77, "command_end_frame": 179}
        self.assertEqual(self.lua.run(f"frame=179; boundary.closed(result, {literal(closed)}, frame, epoch, true); "
            "return reads .. ':' .. frame"), "1:179")

    def test_running_held_or_changed_reads_fail_without_a_retry_or_extra_frames(self):
        cases = ((False, self.state, ""), (True, {**self.state, "command_done": False}, ""),
            (True, {**self.state, "error": "unreadable"}, ""), (True, {**self.state, "buttons": "A"}, ""),
            (True, {**self.state, "stylus_touch": True}, ""), (True, self.state, "frame=frame+1"),
            (True, self.state, "epoch=epoch+1"), (True, self.state, "error('Unreadable party')"))
        for paused, state, change in cases:
            with self.subTest(paused=paused, state=state, change=change):
                changed = change + "; " if change else ""
                result = json.loads(self.lua.run(f"frame, epoch, reads=77, 2, 0; local ok, result=pcall(boundary.read, "
                    f"{literal(state)}, adapter, {literal(paused)}, 'test_party', function(at) reads=reads+1; "
                    f"{changed}return {{frame=at}} end); return json.encode({{ok=ok, reads=reads, frame=frame}})"))
                self.assertFalse(result["ok"])
                self.assertEqual(result["reads"], int(bool(change)))
                self.assertEqual(result["frame"], 78 if change == "frame=frame+1" else 77)
        closed = {**self.state, "stop_reason": "combo_complete", "command_start_frame": 77, "command_end_frame": 179}
        for frame, epoch, state in ((77, 2, closed), (179, 3, closed),
                (179, 2, {**closed, "command_start_frame": 78}), (179, 2, {**closed, "buttons": "X"})):
            with self.subTest(closed_frame=frame, epoch=epoch, state=state):
                self.assertEqual(self.lua.run(f"return tostring(pcall(boundary.closed, {{frame=77, epoch=2}}, "
                    f"{literal(state)}, {frame}, {epoch}, true))"), "false")


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
