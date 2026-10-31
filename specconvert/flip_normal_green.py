#!/usr/bin/env python3
r"""
flip_normal_green.py

Batch-inverts the green (Y) channel of normal-map .dds textures in a folder.
Requires texconv.exe (DirectXTex) on PATH or pass --texconv <path to exe>.

Usage:
    python flip_normal_green.py "C:\path\to\Normal" --out "C:\path\to\Normal_flipped"

Only files matching --pattern (default "*_N.dds") are processed.
Originals are never modified in place; results go to --out.
"""

import argparse
import subprocess
import sys
import tempfile
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

# texconv.exe is expected to sit next to this script, not on PATH
TEXCONV_DEFAULT = Path(__file__).resolve().parent / "texconv.exe"


def invert_green(dds_path: Path) -> None:
    """In-place invert of the green byte of every RGBA texel in an
    uncompressed R8G8B8A8 DDS (single mip level)."""
    data = bytearray(dds_path.read_bytes())
    if data[0:4] != b"DDS ":
        raise ValueError(f"{dds_path.name}: not a DDS file")
    header_size = 148 if data[84:88] == b"DX10" else 128
    for i in range(header_size + 1, len(data), 4):
        data[i] = 255 - data[i]
    dds_path.write_bytes(data)


def run_texconv(texconv: Path, args: list) -> None:
    cmd = [str(texconv), "-nologo", "-y"] + args
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"texconv failed: {' '.join(cmd)}\n{result.stdout}\n{result.stderr}")


def process_file(src: Path, out_dir: Path, texconv: str, tmp_dir: Path) -> None:
    # 1. Decompress to uncompressed top-mip RGBA8 (works regardless of source format)
    run_texconv(texconv, ["-f", "R8G8B8A8_UNORM", "-ft", "dds", "-m", "1",
                           "-o", str(tmp_dir), str(src)])
    decompressed = tmp_dir / (src.stem + ".dds")

    # 2. Invert green channel in place
    invert_green(decompressed)

    # 3. Recompress to BC5_UNORM (normal map format), regenerating full mip chain
    run_texconv(texconv, ["-f", "BC5_UNORM", "-ft", "dds", "-m", "0",
                           "-o", str(out_dir), str(decompressed)])

    decompressed.unlink(missing_ok=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("folder", type=Path, nargs="?", default=None,
                     help="Folder of normal map .dds files (omit to pick via dialog)")
    ap.add_argument("--out", type=Path, default=None,
                     help="Output folder (default: <folder>_flipped)")
    ap.add_argument("--pattern", default="*_N.dds",
                     help="Glob pattern to match files (default: *_N.dds)")
    ap.add_argument("--texconv", type=Path, default=TEXCONV_DEFAULT,
                     help="Path to texconv.exe (default: next to this script)")
    args = ap.parse_args()

    root = tk.Tk()
    root.withdraw()

    folder: Path = args.folder
    if folder is None:
        chosen = filedialog.askdirectory(title="Select folder of normal map .dds files")
        if not chosen:
            print("No folder selected, exiting.")
            return
        folder = Path(chosen)

    if not args.texconv.exists():
        messagebox.showerror("texconv.exe not found",
                              f"Could not find texconv.exe at:\n{args.texconv}\n\n"
                              "Place texconv.exe next to this script, or pass --texconv <path>.")
        return

    out_dir: Path = args.out or folder.parent / (folder.name + "_flipped")
    out_dir.mkdir(parents=True, exist_ok=True)

    files = sorted(folder.glob(args.pattern))
    if not files:
        print(f"No files matched '{args.pattern}' in {folder}")
        return

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        for f in files:
            try:
                process_file(f, out_dir, args.texconv, tmp_dir)
                print(f"OK   {f.name}")
            except Exception as e:
                print(f"FAIL {f.name}: {e}", file=sys.stderr)

    print(f"\nDone. Output: {out_dir}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            tk.Tk().withdraw()
            messagebox.showerror("flip_normal_green.py error", str(e))
        except Exception:
            pass
    finally:
        input("\nPress Enter to exit...")
