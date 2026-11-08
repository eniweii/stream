"""Synthetic tests for specconvert/sequence_strips.py (no texconv, no game assets needed)."""
import copy
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

from PIL import Image

SCRIPT = Path(__file__).resolve().parents[1] / "specconvert" / "sequence_strips.py"
SPEC = importlib.util.spec_from_file_location("sequence_strips", SCRIPT)
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)

# the populator imports tkinter (no GUI is built here) and sequence_strips from its own folder
sys.path.insert(0, str(SCRIPT.parent))
for name in ("tkinter", "tkinter.filedialog", "tkinter.scrolledtext", "tkinter.messagebox"):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["tkinter"].filedialog = sys.modules["tkinter.filedialog"]
sys.modules["tkinter"].scrolledtext = sys.modules["tkinter.scrolledtext"]
sys.modules["tkinter"].messagebox = sys.modules["tkinter.messagebox"]
POP_SPEC = importlib.util.spec_from_file_location("materials_populator", SCRIPT.parent / "materials_populator.py")
populator = importlib.util.module_from_spec(POP_SPEC)
POP_SPEC.loader.exec_module(populator)

PREFIX = "/levels/nfsc/assets/textures/"


def fake_texconv(_texconv, args):
    """Stands in for texconv: png output decodes the source image, dds output keeps the png bytes."""
    out_dir = Path(args[args.index("-o") + 1])
    src = Path(args[-1])
    if args[args.index("-ft") + 1] == "png":
        Image.open(src).convert("RGBA").save(out_dir / (src.stem + ".png"))
    else:
        shutil.copy(src, out_dir / (src.stem + ".dds"))


def write_frame(path, colour, size=(8, 4)):
    Image.new("RGBA", size, colour).save(path, format="PNG")


class SequenceStripsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.textures = self.root / "textures"
        self.textures.mkdir()
        self.colours = [(255, 0, 0, 255), (0, 255, 0, 255), (0, 0, 255, 255)]
        frames, emissive = [], []
        for i, colour in enumerate(self.colours):
            name = f"0x0000000{i + 1}_SIGN_ANIM00{i + 1}.dds"
            write_frame(self.textures / name, colour)
            frames.append({"hash": f"0x0000000{i + 1}", "path": PREFIX + name, "emissive": None})
        for i in (0, 2):                                   # frame 2 has no emissive file
            name = f"0x0000000{i + 1}_SIGN_ANIM00{i + 1}_I.dds"
            write_frame(self.textures / name, (255, 255, 255, 255))
            frames[i]["emissive"] = PREFIX + name
        self.anim = {"version": 2, "notes": "n", "animations": {"0xAA": {"name": "SIGN", "fps": 4, "timeBase": "World", "frames": frames}},
                     "materials": {"SIGN_D_WorldShader": [{"texture": "0x00000001", "type": "frame_swap", "animation": "0xAA", "frame": 0, "stage": 0, "emissive": True}],
                                   "OTHER_D": [{"texture": "0x00000002", "type": "frame_swap", "animation": "0xAA", "frame": 1, "stage": 0, "emissive": False}]}}
        self.materials = {"SIGN_D_WorldShader": {"Stages": [{"baseColorMap": frames[0]["path"], "emissiveMap": frames[0]["emissive"], "animFlags": 1, "scrollDir": [1, 0]}]},
                          "OTHER_D": {"Stages": [{"baseColorMap": frames[1]["path"]}]}}
        self.mpath, self.apath = self.root / "main.materials.json", self.root / "material_animations.json"
        self.mpath.write_text(json.dumps(self.materials))
        self.apath.write_text(json.dumps(self.anim))
        self.logs = []

    def tearDown(self):
        self.tmp.cleanup()

    def run_convert(self, **kw):
        strips = mod.convert(str(self.mpath), str(self.textures), runner=fake_texconv, log=self.logs.append, texconv="fake", **kw)
        return strips, json.loads(self.mpath.read_text()), json.loads(self.apath.read_text())

    def test_strip_joins_frames_in_order(self):
        strips, _m, _a = self.run_convert()
        strip = Image.open(self.textures / "animstrips" / Path(strips["0xAA"]["path"]).name)
        self.assertEqual(strip.size, (24, 4))
        self.assertEqual([strip.getpixel((x, 1)) for x in (1, 9, 17)], self.colours)

    def test_stage_fields_keep_the_scroll_flag(self):
        strips, materials, _a = self.run_convert()
        stage = materials["SIGN_D_WorldShader"]["Stages"][0]
        self.assertEqual(stage["animFlags"], 1 | 16 | 8)
        self.assertEqual(stage["sequenceFramePerSec"], 4.0)
        self.assertAlmostEqual(stage["sequenceSegmentSize"], 1 / 3, places=6)
        self.assertEqual(stage["baseColorMap"], strips["0xAA"]["path"])
        self.assertEqual(stage["scrollDir"], [1, 0])
        other = materials["OTHER_D"]["Stages"][0]
        self.assertEqual(other["animFlags"], 24)
        self.assertNotIn("emissiveMap", other)

    def test_emissive_strip_black_cell_for_missing_frame(self):
        strips, materials, _a = self.run_convert()
        self.assertEqual(materials["SIGN_D_WorldShader"]["Stages"][0]["emissiveMap"], strips["0xAA"]["emissive"])
        strip = Image.open(self.textures / "animstrips" / Path(strips["0xAA"]["emissive"]).name)
        self.assertEqual(strip.getpixel((9, 1)), (0, 0, 0, 255))
        self.assertEqual(strip.getpixel((1, 1)), (255, 255, 255, 255))

    def test_animation_json_gets_strip_and_sequence_blocks(self):
        _s, _m, anim = self.run_convert()
        self.assertEqual(anim["version"], 3)
        self.assertEqual(anim["animations"]["0xAA"]["strip"]["cells"], 3)
        seq = anim["materials"]["SIGN_D_WorldShader"][0]["sequence"]
        self.assertEqual((seq["flagsOn"], seq["flagsOff"]), (25, 1))
        self.assertEqual(anim["materials"]["OTHER_D"][0]["sequence"], {"flagsOn": 24, "flagsOff": 0})

    def test_no_scale_and_exclude(self):
        _s, materials, _a = self.run_convert(use_scale=False)
        self.assertEqual(materials["OTHER_D"]["Stages"][0]["animFlags"], 16)
        self.mpath.write_text(json.dumps(self.materials))     # fresh populator output again
        self.apath.write_text(json.dumps(self.anim))
        strips, _m, anim = self.run_convert(exclude=["sign"])
        self.assertEqual(strips, {})
        self.assertNotIn("sequence", anim["materials"]["OTHER_D"][0])

    def test_second_run_changes_nothing(self):
        _s, first, _a = self.run_convert()
        _s, second, _a = self.run_convert()
        self.assertEqual(first, second)

    def test_missing_frame_file_keeps_the_frame_swap(self):
        os.remove(self.textures / "0x00000002_SIGN_ANIM002.dds")
        strips, materials, _a = self.run_convert()
        self.assertEqual(strips, {})
        self.assertEqual(materials["OTHER_D"]["Stages"][0]["baseColorMap"], PREFIX + "0x00000002_SIGN_ANIM002.dds")

    def test_dds_format_from_header(self):
        path = self.root / "x.dds"
        header = bytearray(128)
        header[0:4] = b"DDS "
        header[84:88] = b"DXT1"
        path.write_bytes(bytes(header))
        self.assertEqual(mod.dds_format(path), "BC1_UNORM")
        header[84:88] = b"DXT5"
        path.write_bytes(bytes(header))
        self.assertEqual(mod.dds_format(path), "BC3_UNORM")
        self.assertEqual(mod.dds_format(self.root / "missing.dds"), mod.DEFAULT_FORMAT)


class PopulatorTandemTest(unittest.TestCase):
    """Populate animations followed by the strip step, twice: the second run has to end in the same state."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.textures = Path(self.tmp.name)
        hashes = ["0x00000001", "0x00000002", "0x00000003"]
        self.disk = {}
        for i, h in enumerate(hashes):
            name = f"{h}_SIGN_ANIM00{i + 1}.dds"
            write_frame(self.textures / name, (60 * i, 10, 10, 255))
            self.disk[h] = str(self.textures / name)
        self.dictionary = {"textures": {h: {} for h in hashes},
                           "animations": {"0xAA": {"name": "SIGN", "fps": 4, "timeBase": "World", "frames": hashes}}}
        self.materials = {"SIGN_D_WorldShader": {"Stages": [{"baseColorMap": PREFIX + "0x00000001_SIGN_ANIM001.dds"}]}}

    def tearDown(self):
        self.tmp.cleanup()

    def run_once(self, materials, old_lua, use_strips=True, use_scale=True):
        log = []
        restored = mod.revert_strips(materials, old_lua, log.append) if old_lua else 0
        changes, lua = populator.populate_animations(materials, self.dictionary, self.disk, str(self.textures), PREFIX, log=log.append)
        if use_strips:
            mod.build_and_apply(materials, lua, str(self.textures), PREFIX, "fake", None, use_scale, log.append, fake_texconv)
        return lua, restored

    def test_populate_then_strips(self):
        lua, _ = self.run_once(self.materials, None)
        stage = self.materials["SIGN_D_WorldShader"]["Stages"][0]
        self.assertTrue(stage["baseColorMap"].startswith(PREFIX + "animstrips/ANIMSTRIP_SIGN_"))
        self.assertEqual(stage["animFlags"], 24)
        self.assertIn("strip", lua["animations"]["0xAA"])
        self.assertEqual(lua["materials"]["SIGN_D_WorldShader"][0]["sequence"], {"flagsOn": 24, "flagsOff": 0})

    def test_rerun_on_converted_files_gives_the_same_result(self):
        first_lua, _ = self.run_once(self.materials, None)
        first_materials = copy.deepcopy(self.materials)
        second_lua, restored = self.run_once(self.materials, json.loads(json.dumps(first_lua)))
        self.assertEqual(restored, 1)
        self.assertEqual(self.materials, first_materials)
        self.assertEqual(second_lua, first_lua)

    def test_rerun_with_strips_off_restores_the_frame_swap_state(self):
        first_lua, _ = self.run_once(self.materials, None)
        plain = {"SIGN_D_WorldShader": {"Stages": [{"baseColorMap": PREFIX + "0x00000001_SIGN_ANIM001.dds"}]}}
        self.run_once(self.materials, json.loads(json.dumps(first_lua)), use_strips=False)
        self.assertEqual(self.materials, plain)

    def test_scale_switch(self):
        self.run_once(self.materials, None, use_scale=False)
        self.assertEqual(self.materials["SIGN_D_WorldShader"]["Stages"][0]["animFlags"], 16)


if __name__ == "__main__":
    unittest.main()
