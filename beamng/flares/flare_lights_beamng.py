from collections import defaultdict
import csv
import json
import os
import sys
import tkinter as tk
from tkinter import filedialog, messagebox


def parse_float(val, default=0.0):
    try:
        val_str = str(val).strip()
        return float(val_str) if val_str else default
    except (ValueError, TypeError):
        return default


def generate_beamng_lights():
    # Initialize and hide the base tkinter window
    root = tk.Tk()
    root.withdraw()

    # Open file picker dialog
    tsv_path = filedialog.askopenfilename(
        title="Select TSV Light Data File",
        filetypes=[
            ("TSV files", "*.tsv"),
            ("Text files", "*.txt"),
            ("All files", "*.*"),
        ],
    )

    # Exit if user cancels dialog
    if not tsv_path:
        print("No file selected. Exiting.")
        sys.exit()

    # Define output file path in the same directory as the chosen TSV
    tsv_dir = os.path.dirname(tsv_path)
    output_json_path = os.path.join(tsv_dir, "items.level.json")

    # Track occurrences of each (section, instance) pair
    instance_occurrence = defaultdict(int)
    total_lights_written = 0

    with open(tsv_path, mode="r", encoding="utf-8") as tsv_file, open(
        output_json_path, mode="w", encoding="utf-8"
    ) as out_file:

        reader = csv.DictReader(tsv_file, delimiter="\t")

        for row in reader:
            section = row["section"].strip()
            instance = row["instance"].strip()
            type_name = row["type_name"].strip()
            override_name = (row.get("override_names") or "").strip()

            # Coordinates: Z is offset up by +150
            x = parse_float(row.get("x", 0))
            y = parse_float(row.get("y", 0))
            z = parse_float(row.get("z", 0)) + 150.0

            # Light properties
            is_lamppost = type_name.lower() == "lamppost"
            cast_shadows = True if is_lamppost else False
            radius = 40.0 if is_lamppost else 0.25
            intensity = 25000 if is_lamppost else 5000

            # Count occurrences of this (section, instance)
            instance_occurrence[(section, instance)] += 1
            entry_num = instance_occurrence[(section, instance)]

            # Detect which layers exist with data
            layers_to_export = []
            if (row.get("L0_r") and row.get("L0_r").strip() != "") or (
                row.get("L0_param") and row.get("L0_param").strip() != ""
            ):
                layers_to_export.append("L0")

            if (row.get("L1_r") and row.get("L1_r").strip() != "") or (
                row.get("L1_param") and row.get("L1_param").strip() != ""
            ):
                layers_to_export.append("L1")

            # Fallback if both layer columns are blank in this row
            if not layers_to_export:
                layers_to_export = ["L0"]

            for layer in layers_to_export:
                name = f"generatedLight_{section}_{instance}_{entry_num}_{layer}"

                # Extract RGBA color components
                r = parse_float(row.get(f"{layer}_r"), 1.0)
                g = parse_float(row.get(f"{layer}_g"), 1.0)
                b = parse_float(row.get(f"{layer}_b"), 1.0)
                a = parse_float(row.get(f"{layer}_a"), 1.0)

                light_entry = {
                    "name": name,
                    "class": "PointLight",
                    "__parent": type_name,
                    "position": [round(x, 4), round(y, 4), round(z, 4)],
                    "animate": False,
                    "brightness": 0.397887349,
                    "castShadows": cast_shadows,
                    "color": [round(r, 6), round(g, 6), round(b, 6), round(a, 6)],
                    "intensity": intensity,
                    "radius": radius,
                    "shadowDarkenColor": [0, 0, 0, 0],
                    "texSize": 256,
                    # dynamic fields read by the section streamer
                    "sectionID": section,
                }
                if override_name:
                    light_entry["sectionOverrideName"] = override_name

                out_file.write(json.dumps(light_entry, separators=(",", ":")) + "\n")
                total_lights_written += 1

    # Show completion pop-up
    messagebox.showinfo(
        "Export Complete",
        f"Successfully generated {total_lights_written} lights!\nSaved to:\n{output_json_path}",
    )
    print(f"Export complete -> {output_json_path}")


if __name__ == "__main__":
    generate_beamng_lights()