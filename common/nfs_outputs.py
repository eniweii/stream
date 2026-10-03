"""Shared output-folder helper.

Rule for every tool in this repo: generated files go to
<repo root>/outputs/<tool-name>/. Git ignores outputs/ (local only).
A tool may still accept an explicit path on the command line.
"""
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
OUTPUTS = REPO_ROOT / "outputs"


def out_dir(tool_name):
    """Return outputs/<tool_name>/ and create it if needed."""
    d = OUTPUTS / tool_name
    d.mkdir(parents=True, exist_ok=True)
    return d


def out_path(tool_name, filename):
    """Return outputs/<tool_name>/<filename>. The folder is created."""
    return out_dir(tool_name) / filename
