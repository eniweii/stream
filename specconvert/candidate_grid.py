"""
candidate_grid.py

Interactive picker: for every material with no existing normal map, runs
all four diffuse-only height pipelines (pipelines.py), previews the
resulting normal for each as a clickable/hotkey-selectable candidate, and
writes the chosen one (+ AO derived from it) on Approve.

Skip Normal skips AO too for that asset - there is no depth data to derive
it from once normal generation is bypassed.

REF is consulted for exactly one thing: marking flat-surface pixels. Any
REF pixel above BLACK_THRESHOLD forces that pixel's final normal to neutral
flat, regardless of which of the 4 candidates was picked. REF is never used
as an alternate height/normal generation source in this workbench - that
role stays gone.

The lighting preview (mouse-driven relight of the diffuse + composited
normal) is separate from all of the above and never affects export: it
only ever uses diffuse + normal, and reuses REF's raw grayscale as a
specular map when REF exists for the material - no additional data
pulled in, to keep it simple. See compute_relit().

Materials that already have an existing normal map never reach this picker
at all - that case stays fully automatic in normal_ao_generator.py's own
batch logic.

Not self-contained: this module imports directly from normal_ao_generator.py
(height_to_normal, ssao_from_height_normal, grayscale, DetailSettings, DDS
I/O, filename helpers) so all four candidates go through the exact same
downstream math as everything else the tool produces.
"""

import tempfile
import tkinter as tk
from pathlib import Path
from tkinter import scrolledtext

import cv2
import numpy as np
from PIL import Image, ImageTk

import pipelines
from normal_ao_generator import (
    DetailSettings,
    grayscale,
    height_to_normal,
    ssao_from_height_normal,
    save_float_as_dds,
    load_dds_as_float,
    _stem_with_suffix,
    BLACK_THRESHOLD,
    NORMAL_DDS_FORMAT,
    AO_DDS_FORMAT,
)

PREVIEW_SIZE = 220

# Flat surface, packed: (0,0,1) unit-up vector -> (0.5, 0.5, 1.0) in [0,1] space.
FLAT_NORMAL = np.array([0.5, 0.5, 1.0])

CANDIDATE_ORDER = ["1_standard", "2_adaptive", "3_inverted", "4_bevel"]
CANDIDATE_LABELS = {
    "1_standard": "1: Standard",
    "2_adaptive": "2: Adaptive",
    "3_inverted": "3: Inverted",
    "4_bevel": "4: Bevel",
}


def _load_diffuse_rgba(path: str, tmpdir: str):
    """Loads a diffuse file as float64 RGB (same convention as the rest of
    the tool) plus alpha if the source format has one. DDS round-trips
    through texconv via load_dds_as_float, which currently drops alpha -
    so alpha is only available for PNG/TGA-style sources. Pipeline D's
    silhouette falls back to full-frame when alpha is None."""
    if path.lower().endswith(".dds"):
        rgb = load_dds_as_float(path, tmpdir)
        return rgb, None

    img = Image.open(path)
    has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
    arr = np.asarray(img.convert("RGBA")).astype(np.float64) / 255.0
    rgb = arr[..., :3]
    alpha = arr[..., 3] if has_alpha else None
    return rgb, alpha


def load_ref_grayscale(ref_path, target_shape, tmpdir):
    """Raw REF grayscale, resized to match the diffuse if needed. Returns
    None if there's no REF for this material. Two separate uses read this:
    load_flat_mask() thresholds it into a boolean flat-surface mask for
    export, and the lighting preview uses the raw intensity as a specular
    map for the interactive reflection term."""
    if not ref_path:
        return None

    if ref_path.lower().endswith(".dds"):
        ref_rgb = load_dds_as_float(ref_path, tmpdir)
    else:
        ref_rgb = np.asarray(Image.open(ref_path).convert("RGB")).astype(np.float64) / 255.0

    ref_gray = grayscale(ref_rgb)
    if ref_gray.shape != target_shape:
        resized = Image.fromarray((np.clip(ref_gray, 0, 1) * 255).astype(np.uint8)) \
            .resize((target_shape[1], target_shape[0]), Image.NEAREST)
        ref_gray = np.asarray(resized).astype(np.float64) / 255.0

    return ref_gray


def load_flat_mask(ref_gray):
    """REF marks flat-surface pixels: any REF pixel brighter than
    BLACK_THRESHOLD means "this pixel is a flat surface" and its final
    normal gets forced neutral, regardless of which candidate was picked.
    Black REF pixels (or no REF at all) leave the candidate's own normal
    untouched there. Takes the already-loaded grayscale (see
    load_ref_grayscale) rather than a path, so callers that also need the
    raw intensity (the lighting preview) don't load REF twice."""
    if ref_gray is None:
        return None
    return ref_gray > BLACK_THRESHOLD


def compute_relit(diffuse_rgb: np.ndarray, normal_rgb: np.ndarray, ref_gray,
                   light_xy: tuple, ambient: float = 0.15, shininess: float = 32.0) -> np.ndarray:
    """Interactive lighting-preview only - never used in the actual export
    path. Only diffuse + normal are required, deliberately no more data
    than that to keep this simple. If REF exists for this material, its raw
    grayscale is reused as a specular map for the reflection term; if not,
    the preview is diffuse lighting only, no specular term at all.

    light_xy: (lx, ly) in [-1, 1], typically driven by mouse position over
    the preview canvas. Light Z is derived so the light vector stays unit
    length (a hemisphere in front of the surface)."""
    n = normal_rgb * 2.0 - 1.0
    n = n / np.clip(np.linalg.norm(n, axis=-1, keepdims=True), 1e-6, None)

    lx, ly = light_xy
    lz_sq = 1.0 - lx * lx - ly * ly
    lz = np.sqrt(lz_sq) if lz_sq > 0 else 0.0
    light = np.array([lx, ly, lz], dtype=np.float64)
    light = light / (np.linalg.norm(light) + 1e-6)

    ndotl = np.clip(np.sum(n * light, axis=-1), 0.0, 1.0)
    result = diffuse_rgb * (ambient + (1.0 - ambient) * ndotl[..., None])

    if ref_gray is not None:
        view = np.array([0.0, 0.0, 1.0])
        half = light + view
        half = half / (np.linalg.norm(half) + 1e-6)
        ndoth = np.clip(np.sum(n * half, axis=-1), 0.0, 1.0)
        spec = ref_gray * (ndoth ** shininess)
        result = result + spec[..., None]

    return np.clip(result, 0.0, 1.0)


def build_candidates(diffuse_rgb: np.ndarray, alpha, settings: DetailSettings,
                      flat_mask=None) -> dict:
    """Returns {candidate_name: {"height": h, "normal": n}} for all 4
    pipelines, with the same detail settings (Micro/Medium/Large/Step)
    applied the same way the rest of the tool applies them, and the REF
    flat-surface mask (if any) baked into every candidate's normal so the
    override is visible in the preview before Approve, not just at export."""
    gray = grayscale(diffuse_rgb)
    heights = pipelines.generate_all_candidates(gray, alpha)

    results = {}
    for name, height in heights.items():
        height = settings.apply_to_height(height)
        normal = height_to_normal(height)
        normal = settings.apply_to_normal(normal)
        if flat_mask is not None:
            normal = normal.copy()
            normal[flat_mask] = FLAT_NORMAL
        results[name] = {"height": height, "normal": normal}
    return results


def apply_roi_overrides(base: np.ndarray, candidates: dict, regions: list, feather_px: int = 3) -> np.ndarray:
    """base: the master-selected candidate's array (height OR normal - this
    works on either since both are just per-pixel arrays of the same
    shape). regions: list of {"bbox": (x0,y0,x1,y1), "pipeline": name}.
    Each region blends in that pipeline's own array within the box,
    feathered at the edges per the spec (a soft Gaussian falloff, not a
    hard cut) so stitched boundaries don't show a seam. Regions are applied
    in order - later regions composite on top of earlier ones."""
    if not regions:
        return base

    h, w = base.shape[:2]
    result = base.copy()
    for region in regions:
        x0, y0, x1, y1 = region["bbox"]
        pipeline_arr = candidates[region["pipeline"]]["height" if base.ndim == 2 else "normal"]

        mask = np.zeros((h, w), dtype=np.float32)
        mask[y0:y1, x0:x1] = 1.0
        if feather_px > 0:
            k = feather_px * 2 + 1
            mask = cv2.GaussianBlur(mask, (k, k), sigmaX=feather_px)

        if result.ndim == 3:
            mask = mask[..., None]
        result = result * (1.0 - mask) + pipeline_arr * mask

    return result


def _renormalize_normal(normal_rgb: np.ndarray) -> np.ndarray:
    """Blending two packed normals linearly (for the ROI feather) leaves a
    non-unit vector in the transition band. Unpack, renormalize, repack."""
    n = normal_rgb * 2.0 - 1.0
    length = np.linalg.norm(n, axis=-1, keepdims=True)
    length = np.where(length == 0, 1.0, length)
    n = n / length
    return n * 0.5 + 0.5


class CandidatePickerApp:
    """Walks a queue of (material, diffuse_path, ref_path_or_None) tuples.
    For each, shows the 4 candidates, waits for a hotkey/click selection +
    Approve (or Skip), writes output, and advances. Materials with an
    existing normal must be filtered out of the queue before constructing
    this class - they don't belong in this workflow at all.

    Every image panel (diffuse, composite, lighting, and the 4 candidate
    thumbnails) is a resizable Canvas rather than a fixed-size Label: the
    whole window grid has weighted rows/columns, so dragging the window
    edge grows every panel instead of leaving dead space. Each canvas
    redraws its own image at the new size on <Configure>, debounced ~100ms
    so a resize drag doesn't trigger a redraw storm across six live images
    at once."""

    RESIZE_DEBOUNCE_MS = 100

    def __init__(self, root, queue, out_normal_dir, out_ao_dir,
                 settings: DetailSettings, log):
        self.root = root
        self.queue = queue  # list of (material, diffuse_path, ref_path_or_None)
        self.index = 0
        self.out_normal_dir = out_normal_dir
        self.out_ao_dir = out_ao_dir
        self.settings = settings
        self.log = log

        self.selected = None
        self.candidates = {}
        self.diffuse_rgb = None
        self.thumbnails = []  # kept for compatibility with older call sites; unused by the canvases below

        self.regions = []           # [{"bbox": (x0,y0,x1,y1), "pipeline": name}, ...] for current asset, image-space coords
        self.roi_draw_mode = False
        self._pending_bbox = None   # image-space bbox awaiting a 1-4 press to assign its pipeline
        self._drag_start = None
        self._drag_rect_id = None

        self.ref_gray = None                   # raw REF grayscale for current asset, or None
        self._current_composite_normal = None  # last composited normal, used by the lighting preview
        self._light_arr = None                 # last relit array, redrawn on lighting-canvas resize

        # Resize/redraw bookkeeping: each resizable canvas maps to a
        # zero-arg callable returning its current source array (or None).
        self._canvas_sources = {}
        self._resize_jobs = {}
        self._photo_refs = {}

        root.title("Normal Candidate Picker")
        root.grid_columnconfigure((0, 1, 2, 3), weight=1)
        root.grid_rowconfigure(1, weight=2)  # diffuse / composite / lighting row
        root.grid_rowconfigure(2, weight=1)  # 4-candidate row
        root.grid_rowconfigure(4, weight=1)  # log box

        self.status_label = tk.Label(root, text="", font=("Segoe UI", 11, "bold"))
        self.status_label.grid(row=0, column=0, columnspan=4, pady=(8, 4))

        self.diffuse_canvas = tk.Canvas(root, width=PREVIEW_SIZE, height=PREVIEW_SIZE,
                                         highlightthickness=1, highlightbackground="gray")
        self.diffuse_canvas.grid(row=1, column=0, columnspan=2, padx=6, pady=6, sticky="nsew")
        self._register_resizable(self.diffuse_canvas, lambda: self.diffuse_rgb)

        composite_frame = tk.Frame(root)
        composite_frame.grid(row=1, column=2, padx=6, pady=6, sticky="nsew")
        tk.Label(composite_frame, text="Composite normal (master + ROI)").pack(side="top")
        self.composite_canvas = tk.Canvas(composite_frame, width=PREVIEW_SIZE, height=PREVIEW_SIZE,
                                           highlightthickness=1, highlightbackground="gray")
        self.composite_canvas.pack(side="top", fill="both", expand=True)
        self._register_resizable(self.composite_canvas, lambda: self._current_composite_normal)

        lighting_frame = tk.Frame(root)
        lighting_frame.grid(row=1, column=3, padx=6, pady=6, sticky="nsew")
        tk.Label(lighting_frame, text="Lighting preview (move mouse)").pack(side="top")
        self.light_canvas = tk.Canvas(lighting_frame, width=PREVIEW_SIZE, height=PREVIEW_SIZE,
                                       highlightthickness=1, highlightbackground="gray")
        self.light_canvas.pack(side="top", fill="both", expand=True)
        self.light_canvas.bind("<Motion>", self._on_light_move)
        self._register_resizable(self.light_canvas, lambda: self._light_arr)

        self.diffuse_canvas.bind("<ButtonPress-1>", self._on_drag_start)
        self.diffuse_canvas.bind("<B1-Motion>", self._on_drag_move)
        self.diffuse_canvas.bind("<ButtonRelease-1>", self._on_drag_end)

        self.candidate_frames = {}
        self.candidate_canvases = {}
        for i, name in enumerate(CANDIDATE_ORDER):
            frame = tk.Frame(root, bd=2, relief="groove")
            frame.grid(row=2, column=i, padx=6, pady=6, sticky="nsew")
            tk.Label(frame, text=CANDIDATE_LABELS[name]).pack(side="top")
            canvas = tk.Canvas(frame, width=PREVIEW_SIZE // 2, height=PREVIEW_SIZE // 2,
                                highlightthickness=0)
            canvas.pack(side="top", fill="both", expand=True)
            tk.Button(frame, text=f"Select {CANDIDATE_LABELS[name]}",
                      command=lambda n=name: self.select(n)).pack(side="bottom", pady=4)
            self.candidate_frames[name] = frame
            self.candidate_canvases[name] = canvas
            self._register_resizable(canvas, lambda n=name: self.candidates.get(n, {}).get("normal"))

        action_frame = tk.Frame(root)
        action_frame.grid(row=3, column=0, columnspan=4, pady=10)
        tk.Button(action_frame, text="<- Previous (P)", command=self.previous).pack(side="left", padx=6)
        tk.Button(action_frame, text="Skip Normal (S)", command=self.skip).pack(side="left", padx=6)
        tk.Button(action_frame, text="Approve & Save (Space)", command=self.approve).pack(side="left", padx=6)
        self.roi_button = tk.Button(action_frame, text="Draw Region Override (R)", command=self.toggle_roi_draw_mode)
        self.roi_button.pack(side="left", padx=6)
        tk.Button(action_frame, text="Close", command=root.destroy).pack(side="left", padx=(24, 6))

        self.log_box = scrolledtext.ScrolledText(root, width=100, height=10)
        self.log_box.grid(row=4, column=0, columnspan=4, padx=8, pady=8, sticky="nsew")

        for digit, name in zip("1234", CANDIDATE_ORDER):
            root.bind(digit, lambda e, n=name: self.select(n))
        for key in ("s", "S", "x", "X"):
            root.bind(key, lambda e: self.skip())
        for key in ("p", "P", "<Left>"):
            root.bind(key, lambda e: self.previous())
        for key in ("<space>", "<Return>"):
            root.bind(key, lambda e: self.approve())
        for key in ("r", "R"):
            root.bind(key, lambda e: self.toggle_roi_draw_mode())

        self.load_current()

    def _log(self, msg):
        self.log_box.insert(tk.END, msg + "\n")
        self.log_box.see(tk.END)
        self.log(msg)

    # ---- resizable-canvas machinery -----------------------------------

    def _register_resizable(self, canvas, get_array):
        """get_array: zero-arg callable returning the current array (or
        None) for this canvas. Registers it and binds <Configure> so the
        canvas redraws itself at its new size whenever the window resizes,
        debounced so a resize drag doesn't fire a redraw on every pixel of
        movement."""
        self._canvas_sources[canvas] = get_array
        canvas.bind("<Configure>", lambda e, c=canvas: self._schedule_redraw(c))

    def _schedule_redraw(self, canvas):
        job = self._resize_jobs.pop(canvas, None)
        if job is not None:
            self.root.after_cancel(job)
        self._resize_jobs[canvas] = self.root.after(
            self.RESIZE_DEBOUNCE_MS, lambda: self._redraw_canvas(canvas))

    def _redraw_canvas(self, canvas):
        self._resize_jobs.pop(canvas, None)
        get_array = self._canvas_sources.get(canvas)
        if get_array is None:
            return
        arr = get_array()
        canvas.delete("img")
        if arr is None:
            return

        w, h = canvas.winfo_width(), canvas.winfo_height()
        if w < 2 or h < 2:
            return

        arr8 = np.clip(arr * 255.0 + 0.5, 0, 255).astype(np.uint8)
        mode = "L" if arr8.ndim == 2 else "RGB"
        photo = ImageTk.PhotoImage(Image.fromarray(arr8, mode=mode).resize((w, h), Image.NEAREST))
        self._photo_refs[canvas] = photo  # keep alive - tkinter drops GC'd PhotoImages
        canvas.create_image(0, 0, anchor="nw", image=photo, tags="img")

        if canvas is self.diffuse_canvas:
            self._redraw_region_overlays()

    def _redraw_all(self):
        for canvas in list(self._canvas_sources):
            self._redraw_canvas(canvas)

    # ---- ROI drawing / overlay mapping ---------------------------------

    def _canvas_to_image_coords(self, cx, cy):
        """Maps a point in the diffuse canvas's CURRENT pixel space back to
        original image pixels, using the canvas's live size rather than a
        fixed constant - correct at any window size, not just the default."""
        h, w = self.diffuse_rgb.shape[:2]
        cw = max(self.diffuse_canvas.winfo_width(), 1)
        ch = max(self.diffuse_canvas.winfo_height(), 1)
        x = int(np.clip(cx * (w / cw), 0, w))
        y = int(np.clip(cy * (h / ch), 0, h))
        return x, y

    def _image_to_canvas_coords(self, ix, iy):
        h, w = self.diffuse_rgb.shape[:2]
        cw = self.diffuse_canvas.winfo_width()
        ch = self.diffuse_canvas.winfo_height()
        return ix * (cw / w), iy * (ch / h)

    def _redraw_region_overlays(self):
        """Redraws every already-assigned region's outline in the diffuse
        canvas's current coordinate space. Called after every diffuse-image
        redraw (including resizes) so overlays stay aligned with their
        actual image-space bbox instead of drifting when the window
        resizes."""
        self.diffuse_canvas.delete("roi")
        for region in self.regions:
            x0, y0, x1, y1 = region["bbox"]
            cx0, cy0 = self._image_to_canvas_coords(x0, y0)
            cx1, cy1 = self._image_to_canvas_coords(x1, y1)
            self.diffuse_canvas.create_rectangle(cx0, cy0, cx1, cy1, outline="orange", width=2, tags="roi")

    # ---- asset loading ---------------------------------------------------

    def load_current(self):
        if self.index >= len(self.queue):
            self.status_label.configure(text="All assets reviewed.")
            self.diffuse_rgb = None
            self._current_composite_normal = None
            self._light_arr = None
            self.candidates = {}
            self._redraw_all()
            self.diffuse_canvas.delete("roi")
            self._log("Done - all assets reviewed.")
            return

        material, diffuse_path, ref_path = self.queue[self.index]
        self.status_label.configure(text=f"Asset {self.index + 1} / {len(self.queue)}: {material}")

        with tempfile.TemporaryDirectory() as tmpdir:
            self.diffuse_rgb, alpha = _load_diffuse_rgba(diffuse_path, tmpdir)
            self.ref_gray = load_ref_grayscale(ref_path, self.diffuse_rgb.shape[:2], tmpdir)
        flat_mask = load_flat_mask(self.ref_gray)

        self.candidates = build_candidates(self.diffuse_rgb, alpha, self.settings, flat_mask)

        self.selected = None
        self.regions = []
        self.roi_draw_mode = False
        self._pending_bbox = None
        self._current_composite_normal = None
        self._light_arr = None

        for frame in self.candidate_frames.values():
            frame.configure(relief="groove")

        self._redraw_all()
        self._log(f"{material}: showing 4 candidates")

    # ---- selection / ROI assignment --------------------------------------

    def select(self, name):
        if not self.candidates:
            return

        if self._pending_bbox is not None:
            self.regions.append({"bbox": self._pending_bbox, "pipeline": name})
            self._log(f"Region {self._pending_bbox}: assigned {CANDIDATE_LABELS[name]}")
            self._pending_bbox = None
            self._redraw_region_overlays()
            self._refresh_composite()
            return

        self.selected = name
        for n, frame in self.candidate_frames.items():
            frame.configure(relief="solid" if n == name else "groove")
        self._log(f"Selected candidate: {CANDIDATE_LABELS[name]}")
        self._refresh_composite()

    def toggle_roi_draw_mode(self):
        if not self.candidates:
            return
        self.roi_draw_mode = not self.roi_draw_mode
        self.roi_button.configure(relief="sunken" if self.roi_draw_mode else "raised")
        self._log("Region draw mode ON - drag a box on the diffuse image, then press 1-4"
                   if self.roi_draw_mode else "Region draw mode off")

    def _on_drag_start(self, event):
        if not self.roi_draw_mode:
            return
        self._drag_start = (event.x, event.y)
        if self._drag_rect_id is not None:
            self.diffuse_canvas.delete(self._drag_rect_id)
        self._drag_rect_id = self.diffuse_canvas.create_rectangle(
            event.x, event.y, event.x, event.y, outline="yellow", width=2, dash=(4, 2))

    def _on_drag_move(self, event):
        if not self.roi_draw_mode or self._drag_start is None:
            return
        x0, y0 = self._drag_start
        self.diffuse_canvas.coords(self._drag_rect_id, x0, y0, event.x, event.y)

    def _on_drag_end(self, event):
        if not self.roi_draw_mode or self._drag_start is None:
            return
        x0c, y0c = self._drag_start
        x1c, y1c = event.x, event.y
        self._drag_start = None
        self.roi_draw_mode = False
        self.roi_button.configure(relief="raised")

        img_x0, img_y0 = self._canvas_to_image_coords(min(x0c, x1c), min(y0c, y1c))
        img_x1, img_y1 = self._canvas_to_image_coords(max(x0c, x1c), max(y0c, y1c))
        if self._drag_rect_id is not None:
            self.diffuse_canvas.delete(self._drag_rect_id)
            self._drag_rect_id = None

        if img_x1 - img_x0 < 2 or img_y1 - img_y0 < 2:
            self._log("Region too small, ignored.")
            return

        self._pending_bbox = (img_x0, img_y0, img_x1, img_y1)
        cx0, cy0 = self._image_to_canvas_coords(img_x0, img_y0)
        cx1, cy1 = self._image_to_canvas_coords(img_x1, img_y1)
        self.diffuse_canvas.create_rectangle(cx0, cy0, cx1, cy1, outline="yellow", width=2, tags="pending_roi")
        self._log(f"Region drawn {self._pending_bbox} - press 1-4 to assign its pipeline.")

    # ---- composite / lighting preview -----------------------------------

    def _refresh_composite(self):
        if self.selected is None:
            self._current_composite_normal = None
            self._light_arr = None
            self._redraw_canvas(self.composite_canvas)
            self._redraw_canvas(self.light_canvas)
            return
        base_normal = self.candidates[self.selected]["normal"]
        composite_normal = apply_roi_overrides(base_normal, self.candidates, self.regions)
        composite_normal = _renormalize_normal(composite_normal)
        self._current_composite_normal = composite_normal
        self._redraw_canvas(self.composite_canvas)
        self._update_lighting(0.0, 0.0)  # default: light straight-on

    def _on_light_move(self, event):
        if self._current_composite_normal is None:
            return
        w = max(self.light_canvas.winfo_width(), 1)
        h = max(self.light_canvas.winfo_height(), 1)
        lx = float(np.clip((event.x / w) * 2 - 1, -1, 1))
        ly = float(np.clip(-((event.y / h) * 2 - 1), -1, 1))  # invert y: up on screen = up
        self._update_lighting(lx, ly)

    def _update_lighting(self, lx, ly):
        self._light_arr = compute_relit(self.diffuse_rgb, self._current_composite_normal, self.ref_gray, (lx, ly))
        self._redraw_canvas(self.light_canvas)

    # ---- queue navigation -------------------------------------------------

    def skip(self):
        if self.index >= len(self.queue):
            return
        material, _, _ = self.queue[self.index]
        self._log(f"{material}: SKIPPED (no normal, no AO written)")
        self.index += 1
        self.load_current()

    def approve(self):
        if self.index >= len(self.queue):
            return
        if self.selected is None:
            self._log("Pick a candidate (1-4) before approving.")
            return

        material, diffuse_path, _ref_path = self.queue[self.index]
        chosen = self.candidates[self.selected]
        height = apply_roi_overrides(chosen["height"], self.candidates, self.regions)
        normal_rgb = apply_roi_overrides(chosen["normal"], self.candidates, self.regions)
        normal_rgb = _renormalize_normal(normal_rgb)
        ao = ssao_from_height_normal(height, normal_rgb)

        with tempfile.TemporaryDirectory() as tmpdir:
            out_normal_path = str(Path(self.out_normal_dir) / f"{_stem_with_suffix(diffuse_path, 'N')}.dds")
            out_ao_path = str(Path(self.out_ao_dir) / f"{_stem_with_suffix(diffuse_path, 'AO')}.dds")
            save_float_as_dds(normal_rgb, out_normal_path, NORMAL_DDS_FORMAT, tmpdir)
            save_float_as_dds(ao, out_ao_path, AO_DDS_FORMAT, tmpdir)

        self._log(f"{material}: wrote normal ({CANDIDATE_LABELS[self.selected]}) + AO")
        self.index += 1
        self.load_current()

    def previous(self):
        if self.index == 0:
            self._log("Already at the first asset.")
            return
        self.index -= 1
        self.load_current()
