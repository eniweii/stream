"""
Specular -> Metalness/Roughness converter.

Select a folder of DDS specular maps. For each file:
  - roughness = inverted specular (255 - value)
  - metalness = binary mask, threshold = mean + 1 stddev of the image
                (pixel > threshold -> 255, else 0)

Requires texconv.exe (DirectXTex) reachable on PATH or next to this script,
used to unpack DDS -> PNG and repack PNG -> DDS.
"""

import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import numpy as np
from PIL import Image

METAL_CURVE_STRENGTH = 10.0  # higher = more aggressively crushes low tones toward 0

TEXCONV = "texconv.exe"  # assumed on PATH; change to a full path if needed
DDS_OUT_FORMAT = "BC4_UNORM"  # single-channel grayscale DDS format


def find_texconv() -> str:
    local = Path(__file__).parent / "texconv.exe"
    if local.exists():
        return str(local)
    return TEXCONV


def run_texconv(args: list[str], out_dir: Path) -> None:
    cmd = [find_texconv(), "-y", "-o", str(out_dir)] + args
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"texconv failed: {result.stdout}\n{result.stderr}")


def dds_to_png(dds_path: Path, work_dir: Path) -> Path:
    run_texconv(["-ft", "png", str(dds_path)], work_dir)
    return work_dir / (dds_path.stem + ".png")


def png_to_dds(png_path: Path, out_dir: Path) -> None:
    run_texconv(["-ft", "dds", "-f", DDS_OUT_FORMAT, str(png_path)], out_dir)


def process_specular(png_path: Path) -> tuple[Image.Image, Image.Image]:
    img = Image.open(png_path).convert("L")
    arr = np.asarray(img, dtype=np.float32)

    roughness_arr = 255.0 - arr
    roughness_img = Image.fromarray(roughness_arr.astype(np.uint8), mode="L")

    # Log curve: crushes low tones toward 0, keeps high tones near their
    # original value, with a smooth continuous ramp (no hard threshold,
    # no pixelation to blur away).
    x = arr / 255.0
    k = METAL_CURVE_STRENGTH
    y = 1.0 - (np.log1p(k * (1.0 - x)) / np.log1p(k))
    metal_arr = np.clip(y * 255.0, 0, 255).astype(np.uint8)
    metal_img = Image.fromarray(metal_arr, mode="L")

    return roughness_img, metal_img


def convert_folder(input_dir: Path, roughness_dir: Path, metal_dir: Path) -> list[str]:
    roughness_dir.mkdir(parents=True, exist_ok=True)
    metal_dir.mkdir(parents=True, exist_ok=True)
    work_dir = input_dir / "_specconv_tmp"
    work_dir.mkdir(exist_ok=True)

    errors = []
    dds_files = [p for p in input_dir.iterdir() if p.suffix.lower() == ".dds"]

    for dds_path in dds_files:
        try:
            png_path = dds_to_png(dds_path, work_dir)
            roughness_img, metal_img = process_specular(png_path)

            rough_png = work_dir / (dds_path.stem + "_r.png")
            metal_png = work_dir / (dds_path.stem + "_m.png")
            roughness_img.save(rough_png)
            metal_img.save(metal_png)

            png_to_dds(rough_png, roughness_dir)
            png_to_dds(metal_png, metal_dir)

            # texconv names output after the input stem; rename to match source
            (roughness_dir / (rough_png.stem + ".dds")).replace(
                roughness_dir / (dds_path.stem + ".dds")
            )
            (metal_dir / (metal_png.stem + ".dds")).replace(
                metal_dir / (dds_path.stem + ".dds")
            )
        except Exception as e:
            errors.append(f"{dds_path.name}: {e}")

    for f in work_dir.iterdir():
        f.unlink()
    work_dir.rmdir()

    return errors


def main() -> None:
    root = tk.Tk()
    root.withdraw()

    input_dir = filedialog.askdirectory(title="Select folder with specular DDS maps")
    if not input_dir:
        return
    input_dir = Path(input_dir)

    roughness_dir = input_dir / "roughness_out"
    metal_dir = input_dir / "metalness_out"

    errors = convert_folder(input_dir, roughness_dir, metal_dir)

    if errors:
        messagebox.showwarning(
            "Done with errors",
            f"Finished with {len(errors)} error(s):\n\n" + "\n".join(errors),
        )
    else:
        messagebox.showinfo(
            "Done",
            f"Converted files into:\n{roughness_dir}\n{metal_dir}",
        )


if __name__ == "__main__":
    main()
