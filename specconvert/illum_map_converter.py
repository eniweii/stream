import re
import subprocess
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import numpy as np
from PIL import Image

TEXCONV = "texconv.exe"  # assumed on PATH or placed next to this script
DDS_OUT_FORMAT = "BC7_UNORM_SRGB"

# ----------------- TUNING PARAMETERS -----------------
CURVE_EDGE0 = 10.0  # REF values at/below this map to black
CURVE_EDGE1 = 42.0  # REF values at/above this map to fully masked

# Controls how bright the diffuse remains in the emissive map (0.0 to 1.0).
# Keeps it dimmed down to prevent the in-game shader/bloom from blowing out.
EMISSIVE_SCALE = 0.5
# -----------------------------------------------------

# Regex to strip hash prefix and any trailing standard suffix (_D, _REF, etc.)
# so we match purely on the core asset name regardless of suffix.
STRIP_RE = re.compile(
    r"^(?:0x[0-9a-fA-F]{8}_)?(?P<core>.+?)(?:_(?:REF|AO|[DNMROASIE]))?$",
    re.IGNORECASE,
)
HASH_RE = re.compile(r"^(0x[0-9a-fA-F]{8})", re.IGNORECASE)


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


def get_core_name(filename_stem: str) -> str:
    m = STRIP_RE.match(filename_stem)
    return m.group("core").upper() if m else filename_stem.upper()


def get_hash_prefix(filename_stem: str) -> str:
    m = HASH_RE.match(filename_stem)
    return m.group(1) if m else ""


def ref_weight(ref_png: Path) -> np.ndarray:
    img = Image.open(ref_png).convert("L")
    arr = np.asarray(img, dtype=np.float32)
    t = np.clip((arr - CURVE_EDGE0) / (CURVE_EDGE1 - CURVE_EDGE0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def make_illum(diffuse_png: Path, ref_png: Path) -> Image.Image:
    diffuse = Image.open(diffuse_png).convert("RGB")
    weight = ref_weight(ref_png)

    diffuse_arr = np.asarray(diffuse, dtype=np.float32)

    # Match dimensions if they differ
    if weight.shape != diffuse_arr.shape[:2]:
        weight_img = Image.fromarray((weight * 255.0).astype(np.uint8), mode="L")
        weight_img = weight_img.resize((diffuse.width, diffuse.height), Image.BILINEAR)
        weight = np.asarray(weight_img, dtype=np.float32) / 255.0

    # Multiply diffuse RGB by the REF mask weight and darken by EMISSIVE_SCALE
    illum_arr = diffuse_arr * weight[:, :, None] * EMISSIVE_SCALE
    illum_arr = np.clip(illum_arr, 0.0, 255.0).astype(np.uint8)
    return Image.fromarray(illum_arr, mode="RGB")


def convert_folders(
    diffuse_dir: Path, ref_dir: Path, illum_dir: Path
) -> tuple[list[str], list[str]]:
    illum_dir.mkdir(parents=True, exist_ok=True)
    work_dir = diffuse_dir / "_illumconv_tmp"
    work_dir.mkdir(exist_ok=True)

    errors = []
    created = []

    # Map core asset name -> Path
    diffuse_map: dict[str, Path] = {}
    for p in diffuse_dir.glob("*.dds"):
        core = get_core_name(p.stem)
        diffuse_map[core] = p

    ref_map: dict[str, Path] = {}
    for p in ref_dir.glob("*.dds"):
        core = get_core_name(p.stem)
        ref_map[core] = p

    if not diffuse_map:
        errors.append(f"No .dds files found in Diffuse folder: {diffuse_dir}")
    if not ref_map:
        errors.append(f"No .dds files found in REF folder: {ref_dir}")

    # Process pairs matched by core name
    for core, ref_path in ref_map.items():
        diffuse_path = diffuse_map.get(core)
        if not diffuse_path:
            errors.append(f"No matching diffuse found for REF file: '{ref_path.name}'")
            continue

        try:
            # Unpack each to uniquely named PNGs so they NEVER overwrite each other
            diff_png = work_dir / f"{diffuse_path.stem}_DIFFUSE_TMP.png"
            ref_png = work_dir / f"{ref_path.stem}_REF_TMP.png"

            run_texconv(["-ft", "png", "-o", str(work_dir), str(diffuse_path)], work_dir)
            (work_dir / f"{diffuse_path.stem}.png").replace(diff_png)

            run_texconv(["-ft", "png", "-o", str(work_dir), str(ref_path)], work_dir)
            (work_dir / f"{ref_path.stem}.png").replace(ref_png)

            # Generate the combined emissive texture
            illum_img = make_illum(diff_png, ref_png)

            # Build output filename: [Hash]_[Core]_I.dds
            h = get_hash_prefix(diffuse_path.stem)
            out_name = f"{h}_{core}_I" if h else f"{core}_I"

            illum_png = work_dir / f"{out_name}.png"
            illum_img.save(illum_png)

            # Repack to DDS
            run_texconv(["-ft", "dds", "-f", DDS_OUT_FORMAT, str(illum_png)], illum_dir)
            created.append(f"{out_name}.dds")

        except Exception as e:
            errors.append(f"{diffuse_path.name} / {ref_path.name}: {e}")

    # Clean temporary work directory
    for f in work_dir.iterdir():
        f.unlink()
    work_dir.rmdir()

    return created, errors


def main() -> None:
    root = tk.Tk()
    root.withdraw()

    diffuse_dir = filedialog.askdirectory(title="1. Select folder with DIFFUSE DDS maps")
    if not diffuse_dir:
        return
    diffuse_dir = Path(diffuse_dir)

    ref_dir = filedialog.askdirectory(title="2. Select folder with REF DDS maps")
    if not ref_dir:
        return
    ref_dir = Path(ref_dir)

    illum_dir = diffuse_dir / "illum_out"

    created, errors = convert_folders(diffuse_dir, ref_dir, illum_dir)

    summary = f"Created {len(created)} file(s) in:\n{illum_dir}\n\n" + "\n".join(created)
    if errors:
        summary += "\n\n" + f"{len(errors)} error/skip(s):\n" + "\n".join(errors)
        messagebox.showwarning("Done with warnings", summary)
    else:
        messagebox.showinfo("Done", summary)


if __name__ == "__main__":
    main()