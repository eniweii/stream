"""
REF -> binary mask converter.

Select a folder of REF DDS maps. For each file:
  - any pixel above a near-zero threshold becomes full white (255)
  - anything else (black) stays black
No blur, no dilation - binarizing gives clear boundaries on its own,
even from low-resolution/blocky source REF maps.

Requires texconv.exe (DirectXTex) reachable on PATH or next to this script,
used to unpack DDS -> PNG and repack PNG -> DDS.
"""

import subprocess
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import numpy as np
from PIL import Image

TEXCONV = "texconv.exe"  # assumed on PATH; change to a full path if needed
DDS_OUT_FORMAT = "BC4_UNORM"  # single-channel grayscale DDS format
CURVE_EDGE0 = 10.0  # input values at/below this map to fully black (no mask)
CURVE_EDGE1 = 42.0  # input values at/above this map to fully white (full mask)


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


def process_ref(png_path: Path) -> tuple[Image.Image, Image.Image]:
    # Smoothstep curve: values at/below CURVE_EDGE0 go fully black, at/above
    # CURVE_EDGE1 go fully white, with a smooth S-shaped ramp between - gives
    # soft, anti-aliased mask edges instead of a hard binary cutoff.
    img = Image.open(png_path).convert("L")
    arr = np.asarray(img, dtype=np.float32)
    t = np.clip((arr - CURVE_EDGE0) / (CURVE_EDGE1 - CURVE_EDGE0), 0.0, 1.0)
    y = t * t * (3.0 - 2.0 * t)
    mask_arr = (y * 255.0).astype(np.uint8)
    roughness_arr = 255 - mask_arr
    mask_img = Image.fromarray(mask_arr, mode="L")
    roughness_img = Image.fromarray(roughness_arr, mode="L")
    return mask_img, roughness_img


def convert_folder(input_dir: Path, mask_dir: Path, roughness_dir: Path) -> list[str]:
    mask_dir.mkdir(parents=True, exist_ok=True)
    roughness_dir.mkdir(parents=True, exist_ok=True)
    work_dir = input_dir / "_refconv_tmp"
    work_dir.mkdir(exist_ok=True)

    errors = []
    dds_files = [p for p in input_dir.iterdir() if p.suffix.lower() == ".dds"]

    for dds_path in dds_files:
        try:
            png_path = dds_to_png(dds_path, work_dir)
            mask_img, roughness_img = process_ref(png_path)

            mask_png = work_dir / (dds_path.stem + "_mask.png")
            rough_png = work_dir / (dds_path.stem + "_r.png")
            mask_img.save(mask_png)
            roughness_img.save(rough_png)

            png_to_dds(mask_png, mask_dir)
            png_to_dds(rough_png, roughness_dir)

            # texconv names output after the input stem; rename to match source
            (mask_dir / (mask_png.stem + ".dds")).replace(
                mask_dir / (dds_path.stem + ".dds")
            )
            (roughness_dir / (rough_png.stem + ".dds")).replace(
                roughness_dir / (dds_path.stem + ".dds")
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

    input_dir = filedialog.askdirectory(title="Select folder with REF DDS maps")
    if not input_dir:
        return
    input_dir = Path(input_dir)

    mask_dir = input_dir / "ref_mask_out"
    roughness_dir = input_dir / "ref_roughness_out"

    errors = convert_folder(input_dir, mask_dir, roughness_dir)

    if errors:
        messagebox.showwarning(
            "Done with errors",
            f"Finished with {len(errors)} error(s):\n\n" + "\n".join(errors),
        )
    else:
        messagebox.showinfo("Done", f"Converted files into:\n{mask_dir}\n{roughness_dir}")


if __name__ == "__main__":
    main()
