"""Synthetic tests for the overrideNames table of sections.json (no game files needed)."""
import json
import pathlib
import sys
import tempfile
import unittest

_root = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(_root / d) for d in ("common", "region", "scenery", "solids_materials")]

import export_sections   # noqa: E402


class FakeGroup:
    def __init__(self, key, name):
        self.key, self.name = key, name


class FakeScenery:
    def __init__(self, groups):
        self.groups = groups


class OverrideNamesTests(unittest.TestCase):
    def test_only_named_groups_are_written_with_the_hex_format_of_the_tsv(self):
        scenery = FakeScenery([FakeGroup(0x454C22D2, "SCENERY_GROUP_DOOR"), FakeGroup(0x1111AAAA, None),
                               FakeGroup(0x0000BEEF, "SHORT")])
        self.assertEqual(export_sections.build_override_names(scenery),
                         {"0x0000BEEF": "SHORT", "0x454C22D2": "SCENERY_GROUP_DOOR"})

    def test_sections_json_carries_the_table_only_when_there_is_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "sections.json"
            export_sections.write_sections_json([], path, [], {"0x454C22D2": "NAME"})
            self.assertEqual(json.loads(path.read_text())["overrideNames"], {"0x454C22D2": "NAME"})
            export_sections.write_sections_json([], path, [], {})
            self.assertNotIn("overrideNames", json.loads(path.read_text()))


if __name__ == "__main__":
    unittest.main()
