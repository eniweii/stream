"""Synthetic tests for the light pack -> BeamNG exporter (no game assets needed)."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "beamng" / "lights" / "light_pack_beamng.py"
SPEC = importlib.util.spec_from_file_location("light_pack_beamng", SCRIPT)
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


def row(section="101", index="3", name="LIGHT_A", size="2.5"):
    return {"pack_section": section, "index": index, "name": name, "r": "0.2", "g": "0.4", "b": "0.6",
            "world_x": "1.5", "world_y": "2.5", "world_z": "3.5", "size": size, "intensity": "9", "far_end": "7"}


class LightPackBuilderTest(unittest.TestCase):
    def build(self, rows, **kw):
        with tempfile.TemporaryDirectory() as tmp:
            result = mod.build_outputs(rows, tmp, **kw)
            lines = Path(result["items_path"]).read_text(encoding="utf-8").splitlines()
            return result, [json.loads(l) for l in lines]

    def test_light_fields(self):
        result, lights = self.build([row()])
        light = lights[0]
        self.assertEqual(light["name"], "LIGHT_A_101_3")
        self.assertEqual(light["class"], "PointLight")
        self.assertEqual(light["position"], [1.5, 2.5, 153.5])
        self.assertEqual(light["color"], [0.2, 0.4, 0.6, 1])
        self.assertEqual(light["brightness"], 0.397887349)
        self.assertEqual(light["sectionID"], "101")
        self.assertEqual(light["radius"], 2.5)
        self.assertNotIn("intensity", light)

    def test_names_unique_per_pack_and_index(self):
        result, lights = self.build([row(index="0"), row(index="1"), row(section="102", index="0")])
        self.assertEqual(len({l["name"] for l in lights}), 3)
        self.assertEqual(result["duplicates"], [])

    def test_empty_name_and_fixed_radius(self):
        _, lights = self.build([row(name="")], radius_from_size=False, fixed_radius=1.0)
        self.assertEqual(lights[0]["name"], "light_101_3")
        self.assertEqual(lights[0]["radius"], 1.0)


if __name__ == "__main__":
    unittest.main()
