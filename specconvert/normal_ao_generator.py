"""
Normal + AO generator.

Reads REF masks, diffuse maps, and existing normal maps from three
separate folders, matches them by material name, and generates a
normal map + AO map per material:

    normal exists              -> use as-is, still derive AO
    no normal, REF non-black   -> generate normal from REF
    no normal, no usable REF   -> generate normal from grayscale(diffuse)

Normal generation: AwesomeBump's two-sided cross-product height-to-normal
method (mode_height_to_normal in filters.frag).
AO generation: AwesomeBump's SSAO-style neighbor-sampling method
(mode_occlusion_filter in filters.frag).

Requires texconv.exe on PATH (or set TEXCONV_PATH below) for DDS I/O.
"""
import os
import re
import shutil
import subprocess
import tempfile
import tkinter as tk
from tkinter import filedialog, scrolledtext
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import shift as ndi_shift

# ----------------------------------------------------------------------
# Config - adjust to match your pipeline
# ----------------------------------------------------------------------
TEXCONV_PATH = "C:/Users/ericm/Desktop/texconv.exe"          # full path if not on PATH
NORMAL_DDS_FORMAT = "BC5_UNORM"       # DDS compression for generated normals
AO_DDS_FORMAT = "BC4_UNORM"           # DDS compression for generated AO (single channel)

HEIGHT_TO_NORMAL_DEPTH = 2.0          # AB's own default (conversionHNDepth)
GRAYSCALE_WEIGHTS = (0.333, 0.333, 0.333)  # AB's own default (equal weights, not perceptual luminance)
BLACK_THRESHOLD = 2 / 255.0           # REF max value below this = "all black", treated as no REF

AO_INTENSITY = 1.0
AO_DEPTH = 1.0
AO_BIAS = 1.0
AO_NUM_ITERS = 2                      # (2*n+1)^2 samples per pixel - raise for quality, costs time

# Detail-enhancement tiers, applied to the height source before normal
# generation (ported from AB's Small/Medium Details; AB itself only has
# two tiers here - Large is our extrapolation one step further out,
# following the same size progression).
DETAIL_DEPTH_DEFAULT = 0.1            # AB's own default (shared "Details depth")
DETAIL_SMALL_RADIUS = 3               # AB's own default
DETAIL_MEDIUM_RADII = (15, 20)        # AB's own default (blurredBig, blurredSmall)
DETAIL_LARGE_RADII = (45, 60)         # extrapolated - not in AB, adjust freely

NORMALS_STEP_DEFAULT = 1.0            # AB's own default (1 = unchanged, 0 = flat)

# Groups files across the three folders by material name, ignoring the
# hex prefix and the trailing suffix token (the folder itself already
# tells us the map type, so the suffix value itself doesn't matter).
FILENAME_RE = re.compile(
    r'^0x[0-9A-Fa-f]{8}_(?P<material>.+)_(?P<suffix>[^_.]+)(?:\.(?P<ext>\w+))?$',
    re.IGNORECASE,
)


# ----------------------------------------------------------------------
# Core math - ported from AwesomeBump's filters.frag
# ----------------------------------------------------------------------
def _normalize3(v):
    n = np.linalg.norm(v, axis=-1, keepdims=True)
    n = np.where(n == 0, 1.0, n)
    return v / n


def _cross3(a, b):
    ax, ay, az = a[..., 0], a[..., 1], a[..., 2]
    bx, by, bz = b[..., 0], b[..., 1], b[..., 2]
    return np.stack([ay * bz - az * by, az * bx - ax * bz, ax * by - ay * bx], axis=-1)


def height_to_normal(height: np.ndarray, depth: float = HEIGHT_TO_NORMAL_DEPTH) -> np.ndarray:
    """height: (H,W) float [0,1]. Returns (H,W,3) packed normal, float [0,1]. Tiling assumed."""
    H = height.astype(np.float64)
    R0 = H

    Rx_p, Ry_p = np.roll(H, -1, axis=1), np.roll(H, -1, axis=0)
    dRx_p = _normalize3(np.stack([np.ones_like(H), np.zeros_like(H), depth * (Rx_p - R0)], axis=-1))
    dRy_p = _normalize3(np.stack([np.zeros_like(H), np.ones_like(H), depth * (Ry_p - R0)], axis=-1))
    bump = _normalize3(_cross3(dRx_p, dRy_p))

    Rx_m, Ry_m = np.roll(H, 1, axis=1), np.roll(H, 1, axis=0)
    dRx_m = _normalize3(np.stack([-np.ones_like(H), np.zeros_like(H), depth * (Rx_m - R0)], axis=-1))
    dRy_m = _normalize3(np.stack([np.zeros_like(H), -np.ones_like(H), depth * (Ry_m - R0)], axis=-1))
    bump = bump + _normalize3(_cross3(dRx_m, dRy_m))

    bump = _normalize3(bump)
    return np.clip(bump * 0.5 + 0.5, 0.0, 1.0)


def ssao_from_height_normal(height: np.ndarray, normal_rgb: np.ndarray,
                             intensity=AO_INTENSITY, depth=AO_DEPTH,
                             bias=AO_BIAS, num_iters=AO_NUM_ITERS) -> np.ndarray:
    """height: (H,W) float [0,1]. normal_rgb: (H,W,3) float [0,1]. Returns (H,W) AO float [0,1]."""
    h, w = height.shape
    g_scale = 5.0 * depth
    g_bias = 0.05 * bias
    n = _normalize3(normal_rgb * 2.0 - 1.0)
    dx, dy = 1.0 / w, 1.0 / h

    ao_sum = np.zeros((h, w), dtype=np.float64)
    count = 0
    for i in range(-num_iters, num_iters + 1):
        for j in range(-num_iters, num_iters + 1):
            off_x, off_y = i * g_scale, j * g_scale
            h_shift = ndi_shift(height, shift=(-off_y, -off_x), mode='wrap', order=1)
            diff = np.stack([
                off_x * dx * np.ones((h, w)),
                off_y * dy * np.ones((h, w)),
                -g_bias * (h_shift - height),
            ], axis=-1)
            v = _normalize3(diff)
            ao_sum += np.clip(np.sum(n * v, axis=-1), 0.0, None) * intensity
            count += 1

    return np.clip(1.0 - ao_sum / count, 0.0, 1.0)


def grayscale(rgb: np.ndarray, weights=GRAYSCALE_WEIGHTS) -> np.ndarray:
    return rgb[..., 0] * weights[0] + rgb[..., 1] * weights[1] + rgb[..., 2] * weights[2]


def is_all_black(img_float: np.ndarray, threshold=BLACK_THRESHOLD) -> bool:
    return float(np.max(img_float)) < threshold


# ----------------------------------------------------------------------
# Detail enhancement (height, pre-normal-generation) + normal step
# (post-process) - ported from AB's filters.frag
# ----------------------------------------------------------------------
def _gaussian_weight(i, j, w):
    return np.exp(-(i * i + j * j) / (w * w + 1.0))


def _gauss_filter_2d(img: np.ndarray, w: float, radius: int, depth: float) -> np.ndarray:
    """Full windowed 2D gaussian (mirrors GLSL gauss_filter). depth scales sample spacing."""
    total = np.zeros_like(img, dtype=np.float64)
    total_w = 0.0
    for i in range(-radius, radius + 1):
        for j in range(-radius, radius + 1):
            cw = _gaussian_weight(i, j, w)
            total_w += cw
            total += ndi_shift(img, shift=(-depth * j, -depth * i), mode='wrap', order=1) * cw
    return total / total_w


def _gauss_filter_separable(img: np.ndarray, w: float, radius: int, depth: float) -> np.ndarray:
    """Two-pass separable gaussian (mirrors GLSL gauss_filter_h then gauss_filter_v)."""
    total_h = np.zeros_like(img, dtype=np.float64)
    total_w = 0.0
    for i in range(-radius, radius + 1):
        cw = _gaussian_weight(i, 0, w)
        total_w += cw
        total_h += ndi_shift(img, shift=(0, -depth * i), mode='wrap', order=1) * cw
    pass1 = total_h / total_w

    total_v = np.zeros_like(img, dtype=np.float64)
    total_w = 0.0
    for j in range(-radius, radius + 1):
        cw = _gaussian_weight(0, j, w)
        total_w += cw
        total_v += ndi_shift(pass1, shift=(-depth * j, 0), mode='wrap', order=1) * cw
    return total_v / total_w


def _overlay(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return a * (a + 2 * b * (1 - a))


def small_details_filter(height: np.ndarray, strength: float, detail_depth=DETAIL_DEPTH_DEFAULT,
                          radius=DETAIL_SMALL_RADIUS) -> np.ndarray:
    if strength <= 0:
        return height
    blurred = _gauss_filter_separable(height, w=radius, radius=radius, depth=detail_depth)
    detail = height - blurred
    color_c = 1 + 20 * strength * detail
    return np.clip(_overlay(height, color_c * 0.5), 0.0, 1.0)


def medium_details_filter(height: np.ndarray, strength: float, detail_depth=DETAIL_DEPTH_DEFAULT,
                           radii=DETAIL_MEDIUM_RADII) -> np.ndarray:
    if strength <= 0:
        return height
    r_big, r_small = radii
    blurred_big = _gauss_filter_separable(height, w=r_big, radius=r_big, depth=detail_depth)
    blurred_small = _gauss_filter_2d(height, w=r_small, radius=r_small, depth=detail_depth / 2)
    dog = blurred_small - blurred_big
    color_c = 1 + 20 * strength * dog
    return np.clip(_overlay(height, color_c * 0.5), 0.0, 1.0)


def large_details_filter(height: np.ndarray, strength: float, detail_depth=DETAIL_DEPTH_DEFAULT,
                          radii=DETAIL_LARGE_RADII) -> np.ndarray:
    """Same difference-of-Gaussians pattern as medium, one step further out.
    Not an AB feature - our extrapolation of AB's own small/medium pattern."""
    if strength <= 0:
        return height
    r_big, r_small = radii
    blurred_big = _gauss_filter_separable(height, w=r_big, radius=r_big, depth=detail_depth)
    blurred_small = _gauss_filter_2d(height, w=r_small, radius=r_small, depth=detail_depth / 2)
    dog = blurred_small - blurred_big
    color_c = 1 + 20 * strength * dog
    return np.clip(_overlay(height, color_c * 0.5), 0.0, 1.0)


def normals_step_filter(normal_rgb: np.ndarray, step: float = NORMALS_STEP_DEFAULT) -> np.ndarray:
    """Post-process on a finished (packed) normal. step=1 unchanged, step=0 fully flat."""
    n = 2.0 * (normal_rgb - 0.5)
    n = n.copy()
    n[..., 0] *= step
    n[..., 1] *= step
    n = _normalize3(n)
    return n * 0.5 + 0.5


class DetailSettings:
    """Bundles the adjustable knobs so they can be threaded through the batch/preview
    functions and swapped from the GUI without a long parameter list everywhere."""
    def __init__(self, detail_depth=DETAIL_DEPTH_DEFAULT, small=0.0, medium=0.0, large=0.0,
                 normals_step=NORMALS_STEP_DEFAULT):
        self.detail_depth = detail_depth
        self.small = small
        self.medium = medium
        self.large = large
        self.normals_step = normals_step

    def apply_to_height(self, height: np.ndarray) -> np.ndarray:
        height = small_details_filter(height, self.small, self.detail_depth)
        height = medium_details_filter(height, self.medium, self.detail_depth)
        height = large_details_filter(height, self.large, self.detail_depth)
        return height

    def apply_to_normal(self, normal_rgb: np.ndarray) -> np.ndarray:
        return normals_step_filter(normal_rgb, self.normals_step)


# ----------------------------------------------------------------------
# DDS I/O via texconv.exe
# ----------------------------------------------------------------------
def load_dds_as_float(path: str, tmpdir: str) -> np.ndarray:
    subprocess.run([TEXCONV_PATH, "-ft", "png", "-o", tmpdir, "-y", path],
                   check=True, capture_output=True)
    png_path = Path(tmpdir) / (Path(path).stem + ".png")
    img = Image.open(png_path).convert("RGB")
    return np.asarray(img).astype(np.float64) / 255.0


def save_float_as_dds(img_float: np.ndarray, out_path: str, dds_format: str, tmpdir: str):
    arr8 = np.clip(img_float * 255.0 + 0.5, 0, 255).astype(np.uint8)
    mode = "L" if arr8.ndim == 2 else "RGB"
    png_path = Path(tmpdir) / (Path(out_path).stem + ".png")
    Image.fromarray(arr8, mode=mode).save(png_path)
    out_dir = str(Path(out_path).parent)
    subprocess.run([TEXCONV_PATH, "-ft", "dds", "-f", dds_format, "-o", out_dir, "-y", str(png_path)],
                   check=True, capture_output=True)
    produced = Path(out_dir) / (png_path.stem + ".dds")
    if produced != Path(out_path):
        shutil.move(str(produced), out_path)


# ----------------------------------------------------------------------
# Folder scanning / grouping
# ----------------------------------------------------------------------
def scan_folder(folder: str) -> dict:
    """Returns {material_name: full_path} for every matching file in folder."""
    result = {}
    if not folder:
        return result
    for name in os.listdir(folder):
        m = FILENAME_RE.match(name)
        if m:
            result[m.group("material")] = str(Path(folder) / name)
    return result


# ----------------------------------------------------------------------
# Per-material processing
# ----------------------------------------------------------------------
def _stem_with_suffix(path: str, new_suffix: str) -> str:
    """Swap the trailing _suffix token for new_suffix (e.g. _D -> _N).
    Falls back to appending _new_suffix if the filename has no such token,
    so arbitrary (non-convention) filenames never collide with the source."""
    stem = Path(path).stem
    if re.search(r'_[^_.]+$', stem):
        return re.sub(r'_[^_.]+$', f'_{new_suffix}', stem)
    return f"{stem}_{new_suffix}"


def process_existing_normal(material: str, diffuse_path, normal_path,
                             out_normal_dir: str, out_ao_dir: str, tmpdir: str, log,
                             settings: 'DetailSettings' = None):
    """The one branch that stays fully automatic: normal is left untouched
    on disk (never rewritten), AO is still derived from it. Everything
    without an existing normal goes through the candidate picker instead -
    see run_batch()."""
    settings = settings or DetailSettings()

    normal_rgb = load_dds_as_float(normal_path, tmpdir)
    height_for_ao = grayscale(load_dds_as_float(diffuse_path, tmpdir)) if diffuse_path else grayscale(normal_rgb)
    normal_rgb = settings.apply_to_normal(normal_rgb)
    log(f"{material}: normal already exists, using as-is")

    ao = ssao_from_height_normal(height_for_ao, normal_rgb)
    out_ao_path = str(Path(out_ao_dir) / f"{_stem_with_suffix(normal_path, 'AO')}.dds")
    save_float_as_dds(ao, out_ao_path, AO_DDS_FORMAT, tmpdir)
    log(f"{material}: AO written")


def generate_single_from_diffuse(src_path: str, output_base: str, settings: 'DetailSettings', log):
    """Treats src_path as a diffuse map (same simplified path as the preview) and
    writes real normal + AO DDS files for it - for one-off files outside the
    three-folder batch matching."""
    out_normal_dir = str(Path(output_base) / "generated_normals")
    out_ao_dir = str(Path(output_base) / "generated_occlusion")
    os.makedirs(out_normal_dir, exist_ok=True)
    os.makedirs(out_ao_dir, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmpdir:
        src_rgb = load_dds_as_float(src_path, tmpdir) if src_path.lower().endswith(".dds") else \
            np.asarray(Image.open(src_path).convert("RGB")).astype(np.float64) / 255.0
        height = grayscale(src_rgb)
        height = settings.apply_to_height(height)
        normal_rgb = height_to_normal(height)
        normal_rgb = settings.apply_to_normal(normal_rgb)
        ao = ssao_from_height_normal(height, normal_rgb)

        out_normal_path = str(Path(out_normal_dir) / f"{_stem_with_suffix(src_path, 'N')}.dds")
        out_ao_path = str(Path(out_ao_dir) / f"{_stem_with_suffix(src_path, 'AO')}.dds")
        save_float_as_dds(normal_rgb, out_normal_path, NORMAL_DDS_FORMAT, tmpdir)
        save_float_as_dds(ao, out_ao_path, AO_DDS_FORMAT, tmpdir)
        log(f"Wrote {out_normal_path}")
        log(f"Wrote {out_ao_path}")


def run_batch(ref_folder, diffuse_folder, normal_folder, output_base, log,
              settings: 'DetailSettings' = None, gui_root=None):
    """REF is only consulted for flat-surface marking inside the picker
    (see candidate_grid.load_flat_mask) - it never decides which branch a
    material takes. A material either already has a normal (fully
    automatic, untouched) or it goes through the interactive 4-candidate
    picker, regardless of whether a REF file exists for it.
    gui_root is the Tk widget the picker opens on top of (required to show
    the picker at all; without it, materials needing review are skipped)."""
    settings = settings or DetailSettings()
    ref_files = scan_folder(ref_folder)
    diffuse_files = scan_folder(diffuse_folder)
    normal_files = scan_folder(normal_folder)

    materials = set(diffuse_files) | set(normal_files)
    if not materials:
        log("No matching files found in diffuse or normal folders.")
        return

    out_normal_dir = str(Path(output_base) / "generated_normals")
    out_ao_dir = str(Path(output_base) / "generated_occlusion")
    os.makedirs(out_normal_dir, exist_ok=True)
    os.makedirs(out_ao_dir, exist_ok=True)

    automatic = sorted(m for m in materials if m in normal_files)
    needs_review = sorted(m for m in materials if m not in normal_files and m in diffuse_files)
    unresolvable = sorted(m for m in materials if m not in normal_files and m not in diffuse_files)

    with tempfile.TemporaryDirectory() as tmpdir:
        for material in automatic:
            try:
                process_existing_normal(material, diffuse_files.get(material), normal_files[material],
                                         out_normal_dir, out_ao_dir, tmpdir, log, settings)
            except Exception as e:
                log(f"{material}: ERROR - {e}")

    for material in unresolvable:
        log(f"{material}: SKIPPED - no normal, no diffuse")

    if needs_review:
        log(f"{len(needs_review)} material(s) need review in the candidate picker...")
        if gui_root is None:
            log("No GUI root available - cannot open candidate picker, skipping these materials.")
        else:
            from candidate_grid import CandidatePickerApp  # local import: avoids a circular
                                                             # import at module load time, since
                                                             # candidate_grid imports back from
                                                             # this module
            queue = [(m, diffuse_files[m], ref_files.get(m)) for m in needs_review]
            picker_win = tk.Toplevel(gui_root)
            CandidatePickerApp(picker_win, queue, out_normal_dir, out_ao_dir, settings, log)
            gui_root.wait_window(picker_win)
    else:
        log("No materials need the candidate picker.")

    log("Done.")


# ----------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------
# ----------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------
from PIL import ImageTk

PREVIEW_SIZE = 220


def _to_thumbnail(img_float: np.ndarray) -> "ImageTk.PhotoImage":
    arr8 = np.clip(img_float * 255.0 + 0.5, 0, 255).astype(np.uint8)
    mode = "L" if arr8.ndim == 2 else "RGB"
    pil_img = Image.fromarray(arr8, mode=mode).resize((PREVIEW_SIZE, PREVIEW_SIZE), Image.NEAREST)
    return ImageTk.PhotoImage(pil_img)


class App:
    def __init__(self, root):
        self.root = root
        root.title("Normal + AO Generator")

        self.ref_var = tk.StringVar()
        self.diffuse_var = tk.StringVar()
        self.normal_var = tk.StringVar()
        self.output_var = tk.StringVar(value=os.getcwd())
        self.preview_file_var = tk.StringVar()

        self._folder_row("REF folder:", self.ref_var, 0)
        self._folder_row("Diffuse folder:", self.diffuse_var, 1)
        self._folder_row("Normal folder (existing):", self.normal_var, 2)
        self._folder_row("Output base folder:", self.output_var, 3)

        # ---- settings panel ----
        settings_frame = tk.LabelFrame(root, text="Settings")
        settings_frame.grid(row=4, column=0, columnspan=3, sticky="we", padx=8, pady=8)

        self.detail_depth_var = tk.DoubleVar(value=DETAIL_DEPTH_DEFAULT)
        self.small_var = tk.DoubleVar(value=0.0)
        self.medium_var = tk.DoubleVar(value=0.0)
        self.large_var = tk.DoubleVar(value=0.0)
        self.step_var = tk.DoubleVar(value=NORMALS_STEP_DEFAULT)

        self.small_enabled = tk.BooleanVar(value=False)
        self.medium_enabled = tk.BooleanVar(value=False)
        self.large_enabled = tk.BooleanVar(value=False)
        self.step_enabled = tk.BooleanVar(value=False)

        self._slider_row(settings_frame, "Details depth", self.detail_depth_var, 0.0, 1.0, 0)
        self._slider_row(settings_frame, "Micro detail", self.small_var, 0.0, 1.0, 1, self.small_enabled)
        self._slider_row(settings_frame, "Medium detail", self.medium_var, 0.0, 1.0, 2, self.medium_enabled)
        self._slider_row(settings_frame, "Large detail", self.large_var, 0.0, 1.0, 3, self.large_enabled)
        self._slider_row(settings_frame, "Normal step", self.step_var, -5.0, 5.0, 4, self.step_enabled)

        # ---- preview panel ----
        preview_frame = tk.LabelFrame(root, text="Preview")
        preview_frame.grid(row=5, column=0, columnspan=3, sticky="we", padx=8, pady=8)

        tk.Label(preview_frame, text="Preview file:").grid(row=0, column=0, sticky="e", padx=4)
        tk.Entry(preview_frame, textvariable=self.preview_file_var, width=50).grid(row=0, column=1, padx=4)
        tk.Button(preview_frame, text="Browse",
                  command=lambda: self.preview_file_var.set(filedialog.askopenfilename() or self.preview_file_var.get())
                  ).grid(row=0, column=2, padx=4)
        tk.Button(preview_frame, text="Update Preview", command=self.update_preview
                  ).grid(row=0, column=3, padx=8)
        tk.Button(preview_frame, text="Generate Single", command=self.generate_single
                  ).grid(row=0, column=4, padx=8)

        self.preview_before_label = tk.Label(preview_frame, text="(source)")
        self.preview_before_label.grid(row=1, column=0, columnspan=2, pady=6)
        self.preview_normal_label = tk.Label(preview_frame, text="(normal)")
        self.preview_normal_label.grid(row=1, column=2, pady=6)
        self.preview_ao_label = tk.Label(preview_frame, text="(AO)")
        self.preview_ao_label.grid(row=1, column=3, pady=6)
        self._preview_imgs = []  # keep references so Tk doesn't garbage-collect them

        tk.Button(root, text="Run Batch", command=self.run).grid(row=6, column=0, columnspan=3, pady=8)

        self.log_box = scrolledtext.ScrolledText(root, width=90, height=18)
        self.log_box.grid(row=7, column=0, columnspan=3, padx=8, pady=8)

    def _folder_row(self, label, var, row):
        tk.Label(self.root, text=label).grid(row=row, column=0, sticky="e", padx=4, pady=2)
        tk.Entry(self.root, textvariable=var, width=60).grid(row=row, column=1, padx=4)
        tk.Button(self.root, text="Browse", command=lambda: var.set(filedialog.askdirectory() or var.get())
                  ).grid(row=row, column=2, padx=4)

    def _slider_row(self, parent, label, var, lo, hi, row, enabled_var=None):
        tk.Label(parent, text=label).grid(row=row, column=0, sticky="e", padx=4, pady=2)
        scale = tk.Scale(parent, variable=var, from_=lo, to=hi, resolution=0.01,
                          orient="horizontal", length=250)
        scale.grid(row=row, column=1, padx=4)
        if enabled_var is not None:
            def _toggle(*_):
                scale.configure(state="normal" if enabled_var.get() else "disabled")
            tk.Checkbutton(parent, text="Enable", variable=enabled_var, command=_toggle
                            ).grid(row=row, column=2, padx=4)
            _toggle()

    def current_settings(self) -> DetailSettings:
        return DetailSettings(
            detail_depth=self.detail_depth_var.get(),
            small=self.small_var.get() if self.small_enabled.get() else 0.0,
            medium=self.medium_var.get() if self.medium_enabled.get() else 0.0,
            large=self.large_var.get() if self.large_enabled.get() else 0.0,
            normals_step=self.step_var.get() if self.step_enabled.get() else NORMALS_STEP_DEFAULT,
        )

    def log(self, msg):
        self.log_box.insert(tk.END, msg + "\n")
        self.log_box.see(tk.END)
        self.root.update_idletasks()

    def update_preview(self):
        """Simplified path just for visualizing slider effects on one representative
        file - not the full REF/diffuse/existing-normal batch decision matrix."""
        path = self.preview_file_var.get()
        if not path:
            self.log("Pick a preview file first.")
            return
        settings = self.current_settings()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                src_rgb = load_dds_as_float(path, tmpdir) if path.lower().endswith(".dds") else \
                    np.asarray(Image.open(path).convert("RGB")).astype(np.float64) / 255.0
                height = grayscale(src_rgb)
                height = settings.apply_to_height(height)
                normal_rgb = height_to_normal(height)
                normal_rgb = settings.apply_to_normal(normal_rgb)
                ao = ssao_from_height_normal(height, normal_rgb)

                self._preview_imgs = [_to_thumbnail(src_rgb), _to_thumbnail(normal_rgb), _to_thumbnail(ao)]
                self.preview_before_label.configure(image=self._preview_imgs[0], text="")
                self.preview_normal_label.configure(image=self._preview_imgs[1], text="")
                self.preview_ao_label.configure(image=self._preview_imgs[2], text="")
        except Exception as e:
            self.log(f"Preview failed: {e}")

    def generate_single(self):
        """Writes real normal+AO DDS files for the current preview file (treated
        as a diffuse map), using the current slider settings."""
        path = self.preview_file_var.get()
        if not path:
            self.log("Pick a preview file first.")
            return
        try:
            generate_single_from_diffuse(path, self.output_var.get(), self.current_settings(), self.log)
        except Exception as e:
            self.log(f"Generate single failed: {e}")

    def run(self):
        self.log_box.delete("1.0", tk.END)
        run_batch(self.ref_var.get(), self.diffuse_var.get(), self.normal_var.get(),
                  self.output_var.get(), self.log, self.current_settings(), gui_root=self.root)


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
