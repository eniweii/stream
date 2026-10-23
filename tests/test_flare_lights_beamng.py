"""Synthetic tests for the Carbon flare -> BeamNG exporter (no game assets needed)."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "beamng" / "flares" / "flare_lights_beamng.py"
SPEC = importlib.util.spec_from_file_location("flare_lights_beamng", SCRIPT)
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


def row(section="101", instance="42", tint="00000000", type_name="catseye_blue",
        params0="catseye_blue_inner", tex0="HEADLIGHTCATSEYE", params1="", tex1="",
        blink="", cycle=""):
    r = {
        "section": section, "instance": instance, "type_name": type_name,
        "x": "1.5", "y": "2.5", "z": "3.5", "tint": tint, "override_names": "groupA",
        "blink_timer": blink, "blink_cycle_s": cycle,
    }
    for layer, params, texture, color in [
        ("L0", params0, tex0, (0.2, 0.3, 0.4, 0.8)),
        ("L1", params1, tex1, (1, 1, 1, 1)),
    ]:
        r[f"{layer}_params"] = params
        r[f"{layer}_texture"] = texture
        for k, v in zip(("r", "g", "b", "a"), color):
            r[f"{layer}_{k}"] = str(v)
        r[f"{layer}_MinScale"] = "2.0"
        r[f"{layer}_Maxscale"] = "4.0"
        r[f"{layer}_MinSize"] = "2"
        r[f"{layer}_MaxSize"] = "8"
        r[f"{layer}_Power"] = "1"
    return r


class FlareBuilderTest(unittest.TestCase):
    def test_flare_datablock_and_light_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = mod.build_outputs([row(), row()], tmp, texture_size=(512, 256))
            blocks = json.loads(Path(out["datablocks_path"]).read_text())
            lights = [json.loads(x) for x in Path(out["items_path"]).read_text().splitlines()]
            self.assertEqual(out["lights"], 2)
            self.assertEqual(out["flare_datablocks"], 1)
            flare = next(v for v in blocks.values() if v["class"] == "LightFlareData")
            self.assertEqual(flare["elementRect"], ["0 0 512 256"])
            self.assertEqual(flare["elementDist"], [0.0])
            self.assertEqual(flare["elementUseLightColor"], [True])
            self.assertEqual(flare["overallScale"], 3.0)
            self.assertEqual(lights[0]["flareType"], flare["name"])
            self.assertEqual(lights[0]["flareScale"], 1.0)
            self.assertEqual(lights[0]["position"], [1.5, 2.5, 153.5])
            self.assertEqual(lights[0]["sectionOverrideName"], "groupA")
            self.assertNotEqual(lights[0]["name"], lights[1]["name"])

    def test_marker_tint_overrides_vault_color(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = mod.build_outputs([row(tint="FF8040FF")], tmp, texture_size=(256, 256))
            light = json.loads(Path(out["items_path"]).read_text().splitlines()[0])
            self.assertEqual(light["color"], [1.0, round(128 / 255, 6), round(64 / 255, 6), 1.0])

    def test_blink_anim_is_deduplicated_and_shared_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = [
                row(type_name="blinking_amber", params0="blink_entry", blink="amber", cycle="0.8"),
                row(section="102", instance="43", type_name="blinking_amber",
                    params0="blink_entry", blink="amber", cycle="0.8"),
            ]
            out = mod.build_outputs(rows, tmp, texture_size=(256, 256))
            blocks = json.loads(Path(out["datablocks_path"]).read_text())
            animations = [v for v in blocks.values() if v["class"] == "LightAnimData"]
            lights = [json.loads(x) for x in Path(out["items_path"]).read_text().splitlines()]
            self.assertEqual(len(animations), 1)
            self.assertEqual(animations[0]["brightnessPeriod"], 0.8)
            self.assertEqual(animations[0]["brightnessKeys"], "aza")
            self.assertFalse(animations[0]["brightnessSmooth"])
            self.assertTrue(all(light["animate"] for light in lights))
            self.assertTrue(all(light["animationType"] == animations[0]["name"] for light in lights))
            self.assertTrue(all(light["animationPhase"] == 0.0 for light in lights))


    def test_existing_level_asset_supplies_actual_name_extension_and_dimensions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            assets = root / "levels" / "nfsc" / "Assets" / "Flares"
            assets.mkdir(parents=True)
            # Minimal TGA header: type 2 (uncompressed RGB), width=512, height=128.
            header = bytearray(18)
            header[2] = 2
            header[12:14] = (512).to_bytes(2, "little")
            header[14:16] = (128).to_bytes(2, "little")
            (assets / "HeAdLiGhTcAtSeYe.TGA").write_bytes(header)
            out_dir = root / "out"
            with patch.object(mod, "repo_root", return_value=root):
                out = mod.build_outputs([row()], out_dir)
            blocks = json.loads(Path(out["datablocks_path"]).read_text())
            flare = next(v for v in blocks.values() if v["class"] == "LightFlareData")
            self.assertEqual(flare["flareTexture"], "levels/nfsc/Assets/Flares/HeAdLiGhTcAtSeYe.TGA")
            self.assertEqual(flare["elementRect"], ["0 0 512 128"])
            self.assertFalse(any("no source image found" in warning for warning in out["warnings"]))

    def test_unmapped_layers_skip_instead_of_emitting_light_without_flare(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = mod.build_outputs([row(params0="", tex0="")], tmp)
            self.assertEqual(out["lights"], 0)
            self.assertEqual(out["skipped_rows"], 1)
            self.assertEqual(json.loads(Path(out["datablocks_path"]).read_text()), {})


if __name__ == "__main__":
    unittest.main()
