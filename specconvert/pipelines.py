"""
pipelines.py

Four diffuse-only height-map generation pipelines for the candidate-selection
workbench. Each function takes a normalized grayscale image (float32, 0-1)
and returns a height map (float32, 0-1) of the same shape.

These produce HEIGHT MAPS ONLY. Height-to-normal conversion is not done here
- feed the output of any pipeline into the existing AB two-sided cross-product
function in normal_ao_generator.py to get the actual normal map. Keeping that
step shared means all four candidates (and the skip-branch check) go through
identical downstream math, so differences in the picker are purely about the
height field, not about two different normal-conversion implementations.

Pipeline A (Standard) and Pipeline C (Inverted) are one function with a flag,
not two separate implementations - they are the same op, mirrored.
"""

import sys

import cv2
import numpy as np
from scipy.ndimage import distance_transform_edt

# Grayscale conversion is intentionally NOT done in this module. The caller
# (candidate_grid.py, wired into normal_ao_generator.py) must pass in a
# height/gray array produced by that module's own grayscale() function -
# AB's equal-weight (0.333, 0.333, 0.333) convention, not perceptual
# luminance - so Pipeline A matches what the current diffuse-fallback
# already produces. Using cv2's built-in grayscale here would silently
# make Pipeline A a different algorithm than the rest of the tool.


def pipeline_standard_or_inverted(gray: np.ndarray, invert: bool = False) -> np.ndarray:
    """Pipeline A (invert=False) / Pipeline C (invert=True).

    Height = luminance directly (A), or 1 - luminance (C). White is always
    the highest point in the result; inverting the input luminance is what
    turns dark-on-light text from carved grooves into raised bumps."""
    if invert:
        return 1.0 - gray
    return gray.copy()


def pipeline_adaptive(gray: np.ndarray, blur_fraction: float = 0.04) -> np.ndarray:
    """Pipeline B (Adaptive Regional-Polarity).

    Standard local-contrast-enhancement technique: blur at a large radius to
    get a "local background baseline", then use the ABSOLUTE deviation from
    that baseline as height. Absolute value is the key step - it makes both
    dark-on-light and light-on-dark detail extrude in the same (+Z)
    direction relative to its own local panel, regardless of which way the
    diffuse colors run. Flat/uniform regions blur to match their own
    baseline and end up flat; any local contrast bumps outward.
    """
    h, w = gray.shape
    short_side = min(h, w)
    sigma = max(1.0, short_side * blur_fraction)

    baseline = cv2.GaussianBlur(gray, (0, 0), sigmaX=sigma, sigmaY=sigma)
    deviation = np.abs(gray - baseline)

    max_dev = float(deviation.max())
    if max_dev > 1e-6:
        deviation = deviation / max_dev

    height = 0.5 + (deviation * 0.5)
    return np.clip(height, 0.0, 1.0).astype(np.float32)


def pipeline_bevel(
    gray: np.ndarray,
    alpha: np.ndarray | None = None,
    bevel_width_fraction: float = 0.05,
    canny_low: int = 50,
    canny_high: int = 150,
) -> np.ndarray:
    """Pipeline D (High-Pass Bevel Field).

    Runs the EDT on two separate boundary sources - the outer silhouette
    (alpha mask, or full-frame if no alpha) and the internal edges (Canny on
    luminance) - then merges by taking the minimum distance to either. This
    gives every internal shape boundary its own bevel, not just the outer
    silhouette, which is the documented approach for pixel-art-style normal
    generation from flat vector graphics.
    """
    h, w = gray.shape
    short_side = min(h, w)
    bevel_px = max(1.0, short_side * bevel_width_fraction)

    if alpha is not None:
        silhouette_mask = (alpha > 0.5).astype(np.uint8)
    else:
        silhouette_mask = np.ones((h, w), dtype=np.uint8)

    # EDT wants distance FROM zero pixels, so measure inward from outside
    # the silhouette (i.e. treat non-silhouette as the "edge").
    edt_silhouette = distance_transform_edt(silhouette_mask)

    gray_u8 = (np.clip(gray, 0.0, 1.0) * 255).astype(np.uint8)
    edges = cv2.Canny(gray_u8, canny_low, canny_high)
    edge_free = (edges == 0).astype(np.uint8)
    edt_edges = distance_transform_edt(edge_free)

    merged = np.minimum(edt_silhouette, edt_edges)
    height = np.clip(merged / bevel_px, 0.0, 1.0).astype(np.float32)

    if alpha is not None:
        height = height * silhouette_mask.astype(np.float32)

    return height


def generate_all_candidates(
    gray: np.ndarray,
    alpha: np.ndarray | None = None,
) -> dict[str, np.ndarray]:
    """Run all four pipelines against a precomputed grayscale height array
    (see module note above re: which grayscale function to use) and return
    height maps keyed by candidate name, in grid order 1-4."""
    return {
        "1_standard": pipeline_standard_or_inverted(gray, invert=False),
        "2_adaptive": pipeline_adaptive(gray),
        "3_inverted": pipeline_standard_or_inverted(gray, invert=True),
        "4_bevel": pipeline_bevel(gray, alpha=alpha),
    }


def _save_height_png(height: np.ndarray, path: str) -> None:
    cv2.imwrite(path, (np.clip(height, 0.0, 1.0) * 255).astype(np.uint8))


def _standalone_grayscale(image_bgr_or_bgra: np.ndarray) -> tuple[np.ndarray, np.ndarray | None]:
    """Test-harness-only loader. Mirrors normal_ao_generator.grayscale()'s
    equal-weight convention so standalone runs match real integration
    output - do not use cv2's built-in grayscale conversion here."""
    if image_bgr_or_bgra.ndim == 3 and image_bgr_or_bgra.shape[2] == 4:
        bgr = image_bgr_or_bgra[:, :, :3].astype(np.float32) / 255.0
        alpha = image_bgr_or_bgra[:, :, 3].astype(np.float32) / 255.0
    else:
        bgr = image_bgr_or_bgra.astype(np.float32) / 255.0
        alpha = None
    gray = (bgr[..., 0] + bgr[..., 1] + bgr[..., 2]) / 3.0
    return gray, alpha


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python pipelines.py <diffuse_image_path> [output_dir]")
        sys.exit(1)

    in_path = sys.argv[1]
    out_dir = sys.argv[2] if len(sys.argv) > 2 else "."

    img = cv2.imread(in_path, cv2.IMREAD_UNCHANGED)
    if img is None:
        print(f"Could not load image: {in_path}")
        sys.exit(1)

    gray, alpha = _standalone_grayscale(img)
    candidates = generate_all_candidates(gray, alpha)
    for name, height_map in candidates.items():
        out_path = f"{out_dir}/candidate_{name}.png"
        _save_height_png(height_map, out_path)
        print(f"Wrote {out_path}")
