#!/usr/bin/env python3
"""Standalone Traffic Node Editor & Exporter for NFS CARP road networks.

Run from the repository root:
    python viewers/traffic_node_editor.py [L5RA.BUN]

The existing nfs_region_viewer.py is not imported or modified. This application
reuses region/nfs_carp_parser.py and region/traffic_corridors.py.
"""
from __future__ import annotations

import copy
import json
import math
import pathlib
import sys
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog, colorchooser

ROOT = pathlib.Path(__file__).resolve().parents[1]
for _folder in ("common", "region", "scenery", "solids_materials", "beamng"):
    _path = str(ROOT / _folder)
    if _path not in sys.path:
        sys.path.insert(0, _path)

from nfs_carp_parser import find_carp_offset, load_road_network_from_carp  # noqa: E402
from traffic_corridors import (  # noqa: E402
    DEFAULT_TYPE_RULES, build_corridors, merged_type_rules, override_key, sample_segment,
    beamng_transform, harmonize_zone_endpoints, merge_connected_corridors,
)
from traffic_node_exporter import write_items_level, write_source_manifest  # noqa: E402
try:  # Use the repository's shared output-path convention.
    from nfs_outputs import out_dir, out_path
    DEFAULT_DATA_DIR = out_dir("traffic_node_editor")
    DEFAULT_OVERRIDES = out_path("traffic_node_editor", "traffic_overrides.json")
except ImportError:  # Allows import/synthetic tests outside a complete repo checkout.
    DEFAULT_DATA_DIR = ROOT / "outputs" / "traffic_node_editor"
    DEFAULT_DATA_DIR.mkdir(parents=True, exist_ok=True)
    DEFAULT_OVERRIDES = DEFAULT_DATA_DIR / "traffic_overrides.json"
TOOL_NAME = "traffic_node_editor"
DEFAULT_PATTERNS = DEFAULT_DATA_DIR / "patterns.json"


class TrafficNodeEditor(tk.Tk):
    def __init__(self, initial_path=None):
        super().__init__()
        self.title("NFS Carbon - Traffic Node Editor & Exporter")
        self.geometry("1500x920")
        self.minsize(1100, 700)
        self.configure(bg="#dedede")

        self.world_path = None
        self.network = None
        self.preview = None
        self.corridors = []
        self.corridor_by_key = {}
        self.corridor_boxes = {}
        self.selected_key = None
        self.scale = 0.1
        self.offset_x = 0.0
        self.offset_y = 0.0
        self._drag = None
        self._dragged = False
        self._redraw_job = None
        self._dirty = False
        self._rules_dirty = False
        self.status_var = tk.StringVar(value="Open a region .BUN containing a CARP world grid.")
        self.perf_var = tk.StringVar(value="Ready")
        self.filter_var = tk.StringVar(value="All")
        self.mode_var = tk.StringVar(value="zones")
        self.vertical_offset_var = tk.StringVar(value="150")
        self.material_var = tk.StringVar(value="road_invisible")
        self.texture_length_var = tk.StringVar(value="8")
        self.render_priority_var = tk.StringVar(value="0")
        self.over_objects_var = tk.BooleanVar(value=True)
        # CARP decision segments carry actual curved turning movements. Include
        # eligible ones by default; users can disable them for comparison.
        self.include_decision_connectors_var = tk.BooleanVar(value=True)

        self.layer_vars = {
            "source": tk.BooleanVar(value=True),
            "corridors": tk.BooleanVar(value=True),
            "arrows": tk.BooleanVar(value=True),
            "junctions": tk.BooleanVar(value=True),
            "labels": tk.BooleanVar(value=False),
        }
        self.state_data = {
            "schema_version": 1,
            "source_file": None,
            "vertical_offset": 150.0,
            "type_rules": {},
            "overrides": {},
            "patterns": [],
            "include_decision_connectors": True,
            "over_objects": True,
        }
        self.type_rules = merged_type_rules()
        self.override_path = DEFAULT_OVERRIDES
        self._load_default_state()
        self._build_menu()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Control-s>", lambda _e: self.save_overrides())
        if initial_path:
            self.after(100, lambda: self.load_world(initial_path))

    # ---------- UI ----------
    def _build_menu(self):
        menu = tk.Menu(self)
        file_menu = tk.Menu(menu, tearoff=False)
        file_menu.add_command(label="Open CARP / region .BUN...", command=self.open_world)
        file_menu.add_separator()
        file_menu.add_command(label="Load overrides...", command=self.load_overrides_dialog)
        file_menu.add_command(label="Save overrides", command=self.save_overrides)
        file_menu.add_separator()
        file_menu.add_command(label="Export BeamNG DecalRoads...", command=self.export_dialog)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._on_close)
        menu.add_cascade(label="File", menu=file_menu)

        view_menu = tk.Menu(menu, tearoff=False)
        view_menu.add_command(label="Fit network to view", command=self.fit_to_view)
        view_menu.add_command(label="Regenerate preview", command=self.rebuild_preview)
        view_menu.add_separator()
        view_menu.add_checkbutton(label="Source graph", variable=self.layer_vars["source"], command=self.redraw)
        view_menu.add_checkbutton(label="AI corridors", variable=self.layer_vars["corridors"], command=self.redraw)
        view_menu.add_checkbutton(label="Lane arrows", variable=self.layer_vars["arrows"], command=self.redraw)
        view_menu.add_checkbutton(label="Junction nodes", variable=self.layer_vars["junctions"], command=self.redraw)
        menu.add_cascade(label="View", menu=view_menu)

        tools_menu = tk.Menu(menu, tearoff=False)
        tools_menu.add_command(label="Rebuild and validate", command=self.validate_network)
        tools_menu.add_command(label="Set vertical offset...", command=self.set_vertical_offset)
        tools_menu.add_command(label="Manage reusable patterns...", command=self.manage_patterns)
        menu.add_cascade(label="Tools", menu=tools_menu)
        menu.add_command(label="Help", command=self.show_help)
        self.config(menu=menu)

    def _build_ui(self):
        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill=tk.BOTH, expand=True, padx=5, pady=(5, 2))
        self.editor_tab = ttk.Frame(self.tabs)
        self.rules_tab = ttk.Frame(self.tabs)
        self.export_tab = ttk.Frame(self.tabs)
        self.tabs.add(self.editor_tab, text="Traffic Editor")
        self.tabs.add(self.rules_tab, text="Rules & Patterns")
        self.tabs.add(self.export_tab, text="Exporter")
        self._build_editor_tab()
        self._build_rules_tab()
        self._build_export_tab()
        footer = ttk.Frame(self)
        footer.pack(fill=tk.X, padx=6, pady=(0, 5))
        ttk.Label(footer, textvariable=self.status_var, anchor="w").pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(footer, textvariable=self.perf_var, anchor="e").pack(side=tk.RIGHT)

    def _build_editor_tab(self):
        controls = ttk.Frame(self.editor_tab, padding=(5, 4))
        controls.pack(fill=tk.X)
        ttk.Label(controls, text="Mode:").pack(side=tk.LEFT, padx=(2, 5))
        for value, label in (("zones", "Zones"), ("corridors", "Corridors"), ("junctions", "Junctions")):
            ttk.Radiobutton(controls, text=label, value=value, variable=self.mode_var,
                            command=self.redraw).pack(side=tk.LEFT, padx=4)
        ttk.Separator(controls, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)
        for key, label in (("source", "Source graph"), ("corridors", "AI corridors"),
                           ("arrows", "Lane arrows"), ("junctions", "Junctions")):
            ttk.Checkbutton(controls, text=label, variable=self.layer_vars[key],
                            command=self.redraw).pack(side=tk.LEFT, padx=3)
        ttk.Button(controls, text="Regenerate preview", command=self.rebuild_preview).pack(side=tk.RIGHT, padx=3)
        ttk.Button(controls, text="Fit to view", command=self.fit_to_view).pack(side=tk.RIGHT, padx=3)
        ttk.Label(controls, text="Filter:").pack(side=tk.RIGHT, padx=(8, 2))
        self.filter_combo = ttk.Combobox(controls, textvariable=self.filter_var, state="readonly", width=15,
                                         values=("All",))
        self.filter_combo.pack(side=tk.RIGHT, padx=2)
        self.filter_combo.bind("<<ComboboxSelected>>", lambda _e: self.redraw())

        body = ttk.Frame(self.editor_tab)
        body.pack(fill=tk.BOTH, expand=True)
        self.left_panel = ttk.Frame(body, width=225, padding=5)
        self.left_panel.pack(side=tk.LEFT, fill=tk.Y)
        self.left_panel.pack_propagate(False)
        self.canvas = tk.Canvas(body, bg="#090b0e", highlightthickness=1, highlightbackground="#888")
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 4))
        self.right_panel = ttk.Frame(body, width=300, padding=5)
        self.right_panel.pack(side=tk.RIGHT, fill=tk.Y)
        self.right_panel.pack_propagate(False)
        self._build_left_panel()
        self._build_inspector()
        self.canvas.bind("<Configure>", lambda _e: self.redraw())
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Button-4>", lambda e: self._zoom(e, 1.15))
        self.canvas.bind("<Button-5>", lambda e: self._zoom(e, 1 / 1.15))

    def _build_left_panel(self):
        layers = ttk.LabelFrame(self.left_panel, text="Layers", padding=5)
        layers.pack(fill=tk.X, pady=(0, 6))
        for key, label in (("source", "Source nodes / segments"), ("corridors", "Derived corridors"),
                           ("arrows", "Direction arrows"), ("junctions", "Branch nodes"),
                           ("labels", "Node/zone labels")):
            ttk.Checkbutton(layers, text=label, variable=self.layer_vars[key],
                            command=self.redraw).pack(anchor="w", pady=1)

        rules_box = ttk.LabelFrame(self.left_panel, text="Classification mapping", padding=4)
        rules_box.pack(fill=tk.X, pady=(0, 6))
        self.mini_rules = ttk.Treeview(rules_box, columns=("class", "drv"), show="tree headings", height=8)
        self.mini_rules.heading("#0", text="Type")
        self.mini_rules.heading("class", text="Class")
        self.mini_rules.heading("drv", text="Drv.")
        self.mini_rules.column("#0", width=45, stretch=False)
        self.mini_rules.column("class", width=100)
        self.mini_rules.column("drv", width=43, anchor=tk.E, stretch=False)
        self.mini_rules.pack(fill=tk.X)
        self.mini_rules.bind("<<TreeviewSelect>>", self._on_rule_selected)
        ttk.Button(rules_box, text="Edit selected type...", command=self.edit_selected_rule).pack(fill=tk.X, pady=(4, 0))

        pattern_box = ttk.LabelFrame(self.left_panel, text="Patterns", padding=5)
        pattern_box.pack(fill=tk.X, pady=(0, 6))
        self.pattern_count_var = tk.StringVar(value="0 reusable patterns")
        ttk.Label(pattern_box, textvariable=self.pattern_count_var).pack(anchor="w")
        ttk.Button(pattern_box, text="Save selected as pattern...", command=self.save_selected_as_pattern).pack(fill=tk.X, pady=(5, 2))
        ttk.Button(pattern_box, text="Manage patterns...", command=self.manage_patterns).pack(fill=tk.X)

        quick = ttk.LabelFrame(self.left_panel, text="Quick actions", padding=5)
        quick.pack(fill=tk.X)
        ttk.Button(quick, text="Rebuild network", command=self.rebuild_preview).pack(fill=tk.X, pady=2)
        ttk.Button(quick, text="Validate", command=self.validate_network).pack(fill=tk.X, pady=2)
        ttk.Button(quick, text="Save overrides", command=self.save_overrides).pack(fill=tk.X, pady=2)
        ttk.Button(quick, text="Export to BeamNG", command=self.export_dialog).pack(fill=tk.X, pady=2)
        self._refresh_rules_table()
        self._refresh_pattern_count()

    def _build_inspector(self):
        box = ttk.LabelFrame(self.right_panel, text="Selected corridor", padding=6)
        box.pack(fill=tk.X, pady=(0, 6))
        self.inspector_vars = {key: tk.StringVar() for key in (
            "segment", "start_node", "end_node", "profiles", "zone", "source_type", "width",
            "offset", "direction", "drivability", "width_scale", "offset_delta", "classification",
            "lanes_left", "lanes_right")}
        rows = [
            ("Source segment", "segment", False), ("Start node", "start_node", False),
            ("End node", "end_node", False), ("Profiles", "profiles", False),
            ("Zone index", "zone", False), ("Source type", "source_type", False),
            ("Width", "width", False), ("Source offset", "offset", False),
        ]
        self._inspector_combo = {}
        row_i = 0
        for label, key, _ in rows:
            ttk.Label(box, text=label + ":").grid(row=row_i, column=0, sticky="w", pady=2, padx=(0, 5))
            ttk.Label(box, textvariable=self.inspector_vars[key]).grid(row=row_i, column=1, sticky="ew", pady=2)
            row_i += 1
        ttk.Separator(box, orient=tk.HORIZONTAL).grid(row=row_i, column=0, columnspan=2, sticky="ew", pady=5)
        row_i += 1
        ttk.Label(box, text="Classification:").grid(row=row_i, column=0, sticky="w", pady=2)
        self.class_combo = ttk.Combobox(box, textvariable=self.inspector_vars["classification"], state="readonly", width=17)
        self.class_combo.grid(row=row_i, column=1, sticky="ew", pady=2)
        row_i += 1
        ttk.Label(box, text="Drivability:").grid(row=row_i, column=0, sticky="w", pady=2)
        ttk.Entry(box, textvariable=self.inspector_vars["drivability"], width=10).grid(row=row_i, column=1, sticky="ew", pady=2)
        row_i += 1
        ttk.Label(box, text="Width scale:").grid(row=row_i, column=0, sticky="w", pady=2)
        ttk.Entry(box, textvariable=self.inspector_vars["width_scale"], width=10).grid(row=row_i, column=1, sticky="ew", pady=2)
        row_i += 1
        ttk.Label(box, text="Offset delta (m):").grid(row=row_i, column=0, sticky="w", pady=2)
        ttk.Entry(box, textvariable=self.inspector_vars["offset_delta"], width=10).grid(row=row_i, column=1, sticky="ew", pady=2)
        row_i += 1
        ttk.Label(box, text="Direction:").grid(row=row_i, column=0, sticky="w", pady=2)
        self.dir_combo = ttk.Combobox(box, textvariable=self.inspector_vars["direction"], state="readonly", values=("forward", "reverse"), width=12)
        self.dir_combo.grid(row=row_i, column=1, sticky="ew", pady=2)
        row_i += 1
        for label, key in (("Lanes left", "lanes_left"), ("Lanes right", "lanes_right")):
            ttk.Label(box, text=label + ":").grid(row=row_i, column=0, sticky="w", pady=2)
            ttk.Entry(box, textvariable=self.inspector_vars[key], width=10).grid(row=row_i, column=1, sticky="ew", pady=2)
            row_i += 1
        self.one_way_var = tk.BooleanVar(value=True)
        self.auto_junction_var = tk.BooleanVar(value=True)
        self.auto_lanes_var = tk.BooleanVar(value=False)
        self.excluded_var = tk.BooleanVar(value=False)
        self.force_export_var = tk.BooleanVar(value=False)
        for var, label in ((self.one_way_var, "One way"), (self.auto_junction_var, "Auto junction"),
                           (self.auto_lanes_var, "Auto lanes"), (self.excluded_var, "Exclude from export"),
                           (self.force_export_var, "Force export (override source flags)")):
            ttk.Checkbutton(box, text=label, variable=var).grid(row=row_i, column=0, columnspan=2, sticky="w", pady=1)
            row_i += 1
        buttons = ttk.Frame(box)
        buttons.grid(row=row_i, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(buttons, text="Apply override", command=self.apply_override).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 3))
        ttk.Button(buttons, text="Reset", command=self.reset_override).pack(side=tk.LEFT, expand=True, fill=tk.X)
        box.columnconfigure(1, weight=1)

        hist = ttk.LabelFrame(self.right_panel, text="Adjustment log", padding=4)
        hist.pack(fill=tk.BOTH, expand=True)
        cols = ("n", "target", "operation", "value")
        self.history = ttk.Treeview(hist, columns=cols, show="headings", height=8)
        for c, title, width in (("n", "#", 32), ("target", "Target", 95), ("operation", "Operation", 90), ("value", "Value", 72)):
            self.history.heading(c, text=title)
            self.history.column(c, width=width, stretch=(c != "n"))
        self.history.pack(fill=tk.BOTH, expand=True)
        hist_buttons = ttk.Frame(hist)
        hist_buttons.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(hist_buttons, text="Undo last", command=self.undo_last).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(hist_buttons, text="Save overrides", command=self.save_overrides).pack(side=tk.LEFT)

    def _build_rules_tab(self):
        outer = ttk.Frame(self.rules_tab, padding=8)
        outer.pack(fill=tk.BOTH, expand=True)
        ttk.Label(outer, text="Source profile-zone type mappings. Change meanings as the Carbon data is verified; unknown types stay excluded by default.").pack(anchor="w", pady=(0, 6))
        columns = ("type", "label", "drivability", "export", "color")
        self.rules_tree = ttk.Treeview(outer, columns=columns, show="headings", height=18)
        for c, label, width in (("type", "Type ID", 80), ("label", "Classification", 260),
                                ("drivability", "Drivability", 120), ("export", "Export", 90), ("color", "Color", 120)):
            self.rules_tree.heading(c, text=label)
            self.rules_tree.column(c, width=width, anchor=tk.W if c in ("label", "color") else tk.CENTER)
        self.rules_tree.pack(fill=tk.BOTH, expand=True)
        self.rules_tree.bind("<Double-1>", lambda _e: self.edit_selected_rule())
        row = ttk.Frame(outer)
        row.pack(fill=tk.X, pady=6)
        ttk.Button(row, text="Edit selected...", command=self.edit_selected_rule).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(row, text="Restore defaults", command=self.restore_default_rules).pack(side=tk.LEFT)
        ttk.Label(outer, text="Rules apply to newly derived corridors. Per-corridor overrides take precedence; reusable patterns apply before local overrides.", foreground="#555").pack(anchor="w")
        self._refresh_rules_table()

    def _build_export_tab(self):
        outer = ttk.Frame(self.export_tab, padding=10)
        outer.pack(fill=tk.BOTH, expand=True)
        input_box = ttk.LabelFrame(outer, text="BeamNG export settings", padding=8)
        input_box.pack(fill=tk.X)
        settings = [
            ("DecalRoad material", self.material_var, "road_invisible"),
            ("Texture length", self.texture_length_var, "8"),
            ("Render priority", self.render_priority_var, "0"),
            ("World vertical offset", self.vertical_offset_var, "150"),
        ]
        for r, (label, var, _default) in enumerate(settings):
            ttk.Label(input_box, text=label + ":").grid(row=r, column=0, sticky="w", padx=(0, 8), pady=3)
            ttk.Entry(input_box, textvariable=var, width=24).grid(row=r, column=1, sticky="w", pady=3)
        ttk.Checkbutton(input_box, text="Project road strips over existing objects (overObjects)",
                        variable=self.over_objects_var, command=self._on_export_option_change).grid(
                            row=len(settings), column=0, columnspan=2, sticky="w", pady=3)
        ttk.Checkbutton(input_box, text="Include eligible CARP decision connectors (junction turning paths)",
                        variable=self.include_decision_connectors_var,
                        command=self._on_decision_connector_change).grid(
                            row=5, column=0, columnspan=2, sticky="w", pady=3)
        ttk.Label(input_box, text="Export uses the same per-zone curves shown in Zones mode. All sampled points along a continuous lane are stored as nodes of one DecalRoad (not one object per point); compatible pieces are joined by source node/profile-zone identity, including where CARP road IDs change at a simple continuation. Decision connectors remain separate curved DecalRoads with autoJunction enabled. Width comes from each zone node. NFS height is corrected by the configured +Z offset during serialization.", wraplength=900).grid(row=6, column=0, columnspan=2, sticky="w", pady=(5, 0))
        action_row = ttk.Frame(outer)
        action_row.pack(fill=tk.X, pady=8)
        ttk.Button(action_row, text="Rebuild preview", command=self.rebuild_preview).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(action_row, text="Validate network", command=self.validate_network).pack(side=tk.LEFT, padx=5)
        ttk.Button(action_row, text="Export items.level.json...", command=self.export_dialog).pack(side=tk.LEFT, padx=5)
        summary_box = ttk.LabelFrame(outer, text="Network and export diagnostics", padding=5)
        summary_box.pack(fill=tk.BOTH, expand=True)
        self.report_text = tk.Text(summary_box, wrap="none", height=20, font=("Consolas", 9))
        yscroll = ttk.Scrollbar(summary_box, orient=tk.VERTICAL, command=self.report_text.yview)
        self.report_text.configure(yscrollcommand=yscroll.set)
        yscroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.report_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._set_report("Open a region .BUN file containing a CARP world grid to begin.")

    # ---------- Source and state ----------
    def _load_default_state(self):
        if DEFAULT_OVERRIDES.exists():
            try:
                data = json.loads(DEFAULT_OVERRIDES.read_text(encoding="utf-8"))
                self.state_data.update({k: data[k] for k in ("vertical_offset", "type_rules", "overrides", "patterns", "include_decision_connectors", "over_objects") if k in data})
                self.type_rules = merged_type_rules(self.state_data.get("type_rules"))
                self.vertical_offset_var.set(str(self.state_data.get("vertical_offset", 150.0)))
                self.include_decision_connectors_var.set(bool(self.state_data.get("include_decision_connectors", True)))
                self.over_objects_var.set(bool(self.state_data.get("over_objects", True)))
                self.override_path = DEFAULT_OVERRIDES
            except (OSError, ValueError, TypeError) as exc:
                print(f"[traffic_node_editor] Could not load default overrides: {exc}")

    def open_world(self):
        path = filedialog.askopenfilename(title="Open NFS region / CARP file", filetypes=[("BUN files", "*.BUN *.bun"), ("All files", "*.*")])
        if path:
            self.load_world(path)

    def load_world(self, path):
        try:
            path = pathlib.Path(path)
            print(f"[traffic_node_editor] Loading CARP road network: {path}")
            data = path.read_bytes()
            offset = find_carp_offset(data)
            if offset is None:
                raise ValueError("No CARP world grid chunk was found. Select the main region/world .BUN, not STREAM*.BUN.")
            network = load_road_network_from_carp(data, offset)
            if network is None or not network.nodes or not network.segments or not network.profiles:
                raise ValueError("The CARP block was found but contained no usable road network records.")
            self.world_path = path
            self.network = network
            self.state_data["source_file"] = str(path)
            self.selected_key = None
            self._dirty = False
            print(f"[traffic_node_editor] Parsed {len(network.nodes)} nodes, {len(network.segments)} segments, {len(network.profiles)} profiles.")
            self.rebuild_preview(fit=True)
        except Exception as exc:
            messagebox.showerror("Open CARP road network", str(exc))
            print(f"[traffic_node_editor] ERROR: {exc}")

    def _rebuild(self):
        if not self.network:
            self.preview = None
            self.corridors = []
            self.export_roads = []
            self.corridor_by_key = {}
            self.corridor_boxes = {}
            self.merge_warnings = []
            return
        try:
            vertical_offset = float(self.vertical_offset_var.get())
        except ValueError:
            vertical_offset = float(self.state_data.get("vertical_offset", 150.0))
            self.vertical_offset_var.set(str(vertical_offset))
        self.state_data["vertical_offset"] = vertical_offset
        self.type_rules = merged_type_rules(self.state_data.get("type_rules"))
        self.preview = build_corridors(self.network, self.type_rules, self.state_data.get("overrides", {}),
                                       patterns=self.state_data.get("patterns", []),
                                       vertical_offset=vertical_offset, curve_steps=10,
                                       include_decision_connectors=self.include_decision_connectors_var.get())
        # The exact Zone-mode geometry is authoritative for export. Harmonize
        # endpoints by (CARP source node, raw profile-zone ID) before rendering
        # or stitching, so visible zones, DecalRoad nodes, and autoJunctions agree.
        endpoint_report = harmonize_zone_endpoints(self.preview["corridors"])
        self.corridors = self.preview["corridors"]
        self.zone_endpoint_report = endpoint_report
        self.export_roads = merge_connected_corridors(self.corridors)
        self.merge_warnings = []  # ambiguous source-graph joins intentionally stay split
        self.corridor_by_key = {c["source_key"]: c for c in self.corridors}
        if hasattr(self, "filter_combo"):
            labels = sorted({c["classification"] for c in self.corridors})
            values = ["All"] + labels
            self.filter_combo.configure(values=values)
            if self.filter_var.get() not in values:
                self.filter_var.set("All")
        self.corridor_boxes = {}
        for corridor in self.corridors:
            pts = corridor["nodes"]
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            self.corridor_boxes[corridor["source_key"]] = (min(xs), min(ys), max(xs), max(ys))
        self._refresh_pattern_count()
        self._update_report()

    def rebuild_preview(self, fit=False):
        if not self.network:
            return
        started = time.perf_counter()
        self._rebuild()
        if fit or self.scale <= 0:
            self.fit_to_view()
        else:
            self.redraw()
        self._refresh_rules_table()
        self._sync_inspector()
        exportable = sum(1 for c in self.corridors if c.get("export", False))
        self.status_var.set(
            f"{self.world_path.name}: {len(self.network.nodes):,} nodes, "
            f"{len(self.network.segments):,} segments, {len(self.corridors):,} zone corridors "
            f"({exportable:,} exportable) -> {len(self.export_roads):,} continuous DecalRoads")
        self.perf_var.set(f"Preview built in {(time.perf_counter()-started):.2f}s")
        print(f"[traffic_node_editor] Preview: {len(self.corridors)} corridors; counts={self.preview['derived_counts']}; warnings={len(self.preview['warnings'])}")

    # ---------- Canvas / geometry ----------
    def _canvas_xy(self, x, y):
        return self.offset_x + x * self.scale, self.offset_y - y * self.scale

    def _world_xy(self, cx, cy):
        return (cx - self.offset_x) / max(self.scale, 1e-9), (self.offset_y - cy) / max(self.scale, 1e-9)

    def fit_to_view(self):
        if not self.network or not self.network.nodes:
            return
        pts = [(n.position[2], -n.position[0]) for n in self.network.nodes]
        x0, x1 = min(p[0] for p in pts), max(p[0] for p in pts)
        y0, y1 = min(p[1] for p in pts), max(p[1] for p in pts)
        w, h = max(1.0, x1-x0), max(1.0, y1-y0)
        cw, ch = max(100, self.canvas.winfo_width()), max(100, self.canvas.winfo_height())
        self.scale = min(cw / w, ch / h) * 0.92
        self.offset_x = cw / 2 - self.scale * (x0+x1) / 2
        self.offset_y = ch / 2 + self.scale * (y0+y1) / 2
        self.redraw()

    @staticmethod
    def _line_distance(px, py, ax, ay, bx, by):
        dx, dy = bx-ax, by-ay
        denom = dx*dx + dy*dy
        t = 0.0 if denom <= 1e-12 else max(0.0, min(1.0, ((px-ax)*dx+(py-ay)*dy)/denom))
        qx, qy = ax+t*dx, ay+t*dy
        return math.hypot(px-qx, py-qy)

    def _visible_bounds(self):
        x0, y1 = self._world_xy(0, 0)
        x1, y0 = self._world_xy(self.canvas.winfo_width(), self.canvas.winfo_height())
        return min(x0,x1), min(y0,y1), max(x0,x1), max(y0,y1)

    @staticmethod
    def _bbox_intersects(a, b):
        return a[0] <= b[2] and a[2] >= b[0] and a[1] <= b[3] and a[3] >= b[1]

    def redraw(self):
        if not hasattr(self, "canvas"):
            return
        started = time.perf_counter()
        self.canvas.delete("all")
        if not self.network or not self.preview:
            self.canvas.create_text(15, 15, anchor="nw", fill="#b4b4b4", text="Open a region .BUN containing CARP data.")
            return
        vx0, vy0, vx1, vy1 = self._visible_bounds()
        visible_box = (vx0, vy0, vx1, vy1)
        mode = self.mode_var.get()

        if self.layer_vars["source"].get():
            # The source centerlines are an orientation reference beneath the corridors.
            for seg in self.network.segments:
                a, b = int(seg.nodeStart), int(seg.nodeEnd)
                if not (0 <= a < len(self.network.nodes) and 0 <= b < len(self.network.nodes)):
                    continue
                source_samples = sample_segment(self.network, seg, 8)
                source_pts = [beamng_transform(p, 0.0)[:2] for p in source_samples]
                sx = [p[0] for p in source_pts]; sy = [p[1] for p in source_pts]
                if max(sx) < vx0 or min(sx) > vx1 or max(sy) < vy0 or min(sy) > vy1:
                    continue
                # This is accurate source-curve context, not Tk canvas spline smoothing.
                flat_source = [v for p in (self._canvas_xy(x, y) for x, y in source_pts) for v in p]
                self.canvas.create_line(*flat_source, fill="#303941", width=1, tags=("source",))

        if self.layer_vars["corridors"].get():
            for c in self.corridors:
                if mode == "junctions":
                    continue
                if mode == "corridors" and not c["export"]:
                    continue
                if self.filter_var.get() != "All" and c["classification"] != self.filter_var.get():
                    continue
                box = self.corridor_boxes.get(c["source_key"])
                if not box or not self._bbox_intersects(box, visible_box):
                    continue
                pts = c["nodes"]
                canvas_pts = [self._canvas_xy(p[0], p[1]) for p in pts]
                flat = [v for p in canvas_pts for v in p]
                # At full-city zoom keep every corridor visible, but capped to 1-8 px.
                width_m = max(c["width_start"], c["width_end"])
                line_width = max(1, min(8, width_m * self.scale))
                color = c["color"] if (c["export"] or mode == "zones") else "#59616a"
                if c["source_key"] == self.selected_key:
                    self.canvas.create_line(*flat, fill="#f4f4f4", width=line_width+3, tags=("selected_underlay",))
                self.canvas.create_line(*flat, fill=color, width=line_width, tags=("corridor", c["source_key"]))
                if self.layer_vars["arrows"].get() and self.scale > 0.018 and len(canvas_pts) >= 3:
                    mid = len(canvas_pts) // 2
                    p0, p1 = canvas_pts[max(0, mid-1)], canvas_pts[min(len(canvas_pts)-1, mid+1)]
                    if math.hypot(p1[0]-p0[0], p1[1]-p0[1]) > 13:
                        self.canvas.create_line(*p0, *p1, fill="#eaf6ff", width=1, arrow=tk.LAST,
                                                 arrowshape=(7, 8, 3), tags=("arrow",))
                if self.layer_vars["labels"].get() and self.scale > 0.05 and len(canvas_pts) > 2:
                    midp = canvas_pts[len(canvas_pts)//2]
                    self.canvas.create_text(midp[0], midp[1], text=f"{c['segment_index']}:{c['zone_index']}", fill="#eeeeee", font=("TkDefaultFont", 7))

        if self.layer_vars["junctions"].get():
            for ni, node in enumerate(self.network.nodes):
                if int(node.numSegments) < 3:
                    continue
                x, y = node.position[2], -node.position[0]
                if not (vx0 <= x <= vx1 and vy0 <= y <= vy1):
                    continue
                cx, cy = self._canvas_xy(x, y)
                self.canvas.create_oval(cx-3, cy-3, cx+3, cy+3, fill="#f1a33c", outline="#171717")
                if self.layer_vars["labels"].get() and self.scale > 0.05:
                    self.canvas.create_text(cx+5, cy-5, anchor="sw", fill="#ffd18a", text=str(ni), font=("TkDefaultFont", 7))

        # Small unobtrusive map legend.
        lx, ly = 10, max(10, self.canvas.winfo_height() - 105)
        self.canvas.create_rectangle(lx, ly, lx+185, ly+95, fill="#090b0e", outline="#50565c")
        self.canvas.create_text(lx+8, ly+7, anchor="nw", fill="#d9d9d9", text="Corridor legend", font=("TkDefaultFont", 8, "bold"))
        legend = [("#31b77a", "Preferred traffic"), ("#a879c9", "Median / low priority"),
                  ("#d99a32", "Shoulder / fallback"), ("#59616a", "Excluded / unmapped")]
        for idx, (color, label) in enumerate(legend):
            yy = ly+27+idx*16
            self.canvas.create_line(lx+8, yy, lx+28, yy, fill=color, width=4)
            self.canvas.create_text(lx+34, yy, anchor="w", fill="#dddddd", text=label, font=("TkDefaultFont", 8))
        self.perf_var.set(f"Draw {time.perf_counter()-started:.2f}s | {len(self.canvas.find_all()):,} items")

    def _hit_corridor(self, cx, cy, tolerance=8.0):
        wx, wy = self._world_xy(cx, cy)
        margin = tolerance / max(self.scale, 1e-9)
        query = (wx-margin, wy-margin, wx+margin, wy+margin)
        best_key, best_dist = None, tolerance
        for c in self.corridors:
            box = self.corridor_boxes.get(c["source_key"])
            if not box or not self._bbox_intersects(box, query):
                continue
            pts = c["nodes"]
            screen = [self._canvas_xy(p[0], p[1]) for p in pts]
            for i in range(len(screen)-1):
                d = self._line_distance(cx, cy, *screen[i], *screen[i+1])
                if d < best_dist:
                    best_dist, best_key = d, c["source_key"]
        return best_key

    def _on_press(self, event):
        self._drag = (event.x, event.y)
        self._dragged = False

    def _on_drag(self, event):
        if self._drag is None:
            return
        dx, dy = event.x-self._drag[0], event.y-self._drag[1]
        if abs(dx) > 2 or abs(dy) > 2:
            self._dragged = True
        self.offset_x += dx
        self.offset_y += dy
        self._drag = (event.x, event.y)
        self.canvas.move("all", dx, dy)
        self._schedule_redraw(120)

    def _on_release(self, event):
        if not self._dragged:
            key = self._hit_corridor(event.x, event.y)
            if key:
                self.selected_key = key
                self._sync_inspector()
                self.redraw()
            else:
                self.selected_key = None
                self._sync_inspector()
                self.redraw()
        self._drag = None

    def _on_wheel(self, event):
        self._zoom(event, 1.15 if event.delta > 0 else 1/1.15)

    def _zoom(self, event, factor):
        if not self.network:
            return
        self.scale *= factor
        self.offset_x = event.x + (self.offset_x-event.x)*factor
        self.offset_y = event.y + (self.offset_y-event.y)*factor
        self.canvas.scale("all", event.x, event.y, factor, factor)
        self._schedule_redraw(120)

    def _schedule_redraw(self, delay_ms=150):
        if self._redraw_job is not None:
            self.after_cancel(self._redraw_job)
        self._redraw_job = self.after(delay_ms, self._debounced_redraw)

    def _debounced_redraw(self):
        self._redraw_job = None
        self.redraw()

    # ---------- Inspector / adjustments ----------
    def _sync_inspector(self):
        c = self.corridor_by_key.get(self.selected_key) if self.selected_key else None
        if not c:
            for v in self.inspector_vars.values():
                v.set("")
            self.class_combo.configure(values=sorted({r["label"] for r in self.type_rules.values()}))
            self.one_way_var.set(True); self.auto_junction_var.set(True); self.auto_lanes_var.set(False); self.excluded_var.set(False); self.force_export_var.set(False)
            return
        self.inspector_vars["segment"].set(f"{c['segment_index']} (source {c['source_segment_id']})")
        self.inspector_vars["start_node"].set(str(c["node_start"]))
        self.inspector_vars["end_node"].set(str(c["node_end"]))
        self.inspector_vars["profiles"].set(f"{c['profile_start']} -> {c['profile_end']}")
        self.inspector_vars["zone"].set(str(c["zone_index"]))
        self.inspector_vars["source_type"].set(f"{c['source_type']} -> {c['source_type_end']}")
        self.inspector_vars["width"].set(f"{c['width_start']:.2f} -> {c['width_end']:.2f} m")
        self.inspector_vars["offset"].set(f"{c['offset_start']:+.2f} -> {c['offset_end']:+.2f} m")
        self.inspector_vars["classification"].set(c["classification"])
        self.inspector_vars["drivability"].set(f"{c['drivability']:.3f}")
        self.inspector_vars["width_scale"].set(str(c.get("override", {}).get("width_scale", 1.0)))
        self.inspector_vars["offset_delta"].set(str(c.get("override", {}).get("offset_delta", 0.0)))
        self.inspector_vars["direction"].set(c["direction"])
        self.inspector_vars["lanes_left"].set(str(c["lanes_left"]))
        self.inspector_vars["lanes_right"].set(str(c["lanes_right"]))
        self.one_way_var.set(c["one_way"])
        self.auto_junction_var.set(c["auto_junction"])
        self.auto_lanes_var.set(c["auto_lanes"])
        self.excluded_var.set(c["excluded"])
        self.force_export_var.set(c.get("force_export", False))
        self.class_combo.configure(values=sorted({r["label"] for r in self.type_rules.values()}))
        self._refresh_history()

    def apply_override(self):
        if not self.selected_key:
            messagebox.showinfo("Apply override", "Select a corridor first.")
            return
        try:
            drivability = float(self.inspector_vars["drivability"].get())
            width_scale = float(self.inspector_vars["width_scale"].get())
            offset_delta = float(self.inspector_vars["offset_delta"].get())
            lanes_left = int(self.inspector_vars["lanes_left"].get())
            lanes_right = int(self.inspector_vars["lanes_right"].get())
            if not math.isfinite(drivability) or not math.isfinite(width_scale) or not math.isfinite(offset_delta):
                raise ValueError("Numeric fields must be finite.")
            if width_scale <= 0 or lanes_left < 0 or lanes_right < 0:
                raise ValueError("Width scale must be positive and lane counts cannot be negative.")
        except ValueError as exc:
            messagebox.showerror("Invalid override", str(exc))
            return
        old = copy.deepcopy(self.state_data["overrides"].get(self.selected_key, {}))
        classification = self.inspector_vars["classification"].get().strip()
        self.state_data["overrides"][self.selected_key] = {
            "classification": classification or self.corridor_by_key[self.selected_key]["classification"],
            "drivability": drivability, "width_scale": width_scale, "offset_delta": offset_delta,
            "direction": self.inspector_vars["direction"].get(),
            "one_way": self.one_way_var.get(), "auto_junction": self.auto_junction_var.get(),
            "auto_lanes": self.auto_lanes_var.get(), "lanes_left": lanes_left,
            "lanes_right": lanes_right, "excluded": self.excluded_var.get(),
            "force_export": self.force_export_var.get(),
        }
        self._log_adjustment(self.selected_key, "apply override", f"drv={drivability:g}", old,
                             self.state_data["overrides"][self.selected_key])
        self._dirty = True
        self.rebuild_preview()

    def reset_override(self):
        if not self.selected_key:
            return
        old = self.state_data["overrides"].pop(self.selected_key, None)
        if old is not None:
            self._log_adjustment(self.selected_key, "reset override", "defaults", old, None)
            self._dirty = True
            self.rebuild_preview()

    def _log_adjustment(self, target, operation, value, old, new, kind=None):
        log_path = DEFAULT_DATA_DIR / "adjustment_log.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if kind is None:
            kind = "override" if "override" in operation else ("pattern_add" if operation == "save reusable pattern" else "other")
        row = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "target": target,
               "operation": operation, "value": value, "before": old, "after": new, "kind": kind}
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n")
        self._refresh_history()

    def _refresh_history(self):
        for item in self.history.get_children():
            self.history.delete(item)
        log_path = DEFAULT_DATA_DIR / "adjustment_log.jsonl"
        if not log_path.exists():
            return
        try:
            rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except (OSError, ValueError):
            return
        for i, row in enumerate(rows[-100:]):
            self.history.insert("", tk.END, values=(f"{len(rows)-len(rows[-100:])+i+1:03d}",
                                                       str(row.get("target", ""))[-24:], row.get("operation", ""), row.get("value", "")))

    def undo_last(self):
        log_path = DEFAULT_DATA_DIR / "adjustment_log.jsonl"
        if not log_path.exists():
            return
        try:
            rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            if not rows:
                return
            last = rows[-1]
            target, kind = last.get("target"), last.get("kind", "other")
            before, after = last.get("before"), last.get("after")
            if kind == "override":
                if before is None:
                    self.state_data["overrides"].pop(target, None)
                else:
                    self.state_data["overrides"][target] = before
                self._log_adjustment(target, "undo " + str(last.get("operation")), "inverse", after, before, kind="override")
            elif kind == "type_rule":
                type_id = str(target).partition(":")[2]
                if before is None:
                    self.type_rules.pop(type_id, None)
                else:
                    self.type_rules[type_id] = before
                self.state_data["type_rules"] = copy.deepcopy(self.type_rules)
                self._log_adjustment(target, "undo " + str(last.get("operation")), "inverse", after, before, kind="type_rule")
            elif kind == "type_rules_all":
                self.type_rules = merged_type_rules(before or {})
                self.state_data["type_rules"] = copy.deepcopy(self.type_rules)
                self._log_adjustment(target, "undo " + str(last.get("operation")), "inverse", after, before, kind="type_rules_all")
            elif kind == "vertical_offset":
                self.state_data["vertical_offset"] = float(before)
                self.vertical_offset_var.set(str(before))
                self._log_adjustment(target, "undo " + str(last.get("operation")), "inverse", after, before, kind="vertical_offset")
            elif kind == "pattern_add":
                pattern = after
                patterns = self.state_data.get("patterns", [])
                for i in range(len(patterns)-1, -1, -1):
                    if patterns[i] == pattern:
                        patterns.pop(i); break
                self._log_adjustment(target, "undo pattern add", "remove pattern", pattern, None, kind="pattern_remove")
            elif kind == "pattern_remove":
                if before is not None:
                    self.state_data.setdefault("patterns", []).append(before)
                self._log_adjustment(target, "undo pattern remove", "restore pattern", None, before, kind="pattern_add")
            elif kind == "pattern_edit":
                pattern_name = str(target).partition(":")[2]
                patterns = self.state_data.setdefault("patterns", [])
                for i in range(len(patterns)-1, -1, -1):
                    if patterns[i].get("name") == pattern_name:
                        patterns[i] = before
                        break
                self._log_adjustment(target, "undo pattern edit", "restore pattern", after, before, kind="pattern_edit")
            else:
                messagebox.showinfo("Undo last", "The most recent log entry is informational or cannot be inverted automatically.")
                return
            self._dirty = True
            self.rebuild_preview()
        except (OSError, ValueError, TypeError) as exc:
            messagebox.showerror("Undo last", str(exc))

    # ---------- Rules and reusable patterns ----------
    def _refresh_rules_table(self):
        if not hasattr(self, "rules_tree"):
            return
        for tree in (self.rules_tree, self.mini_rules):
            if not tree.winfo_exists():
                continue
            for item in tree.get_children():
                tree.delete(item)
            for key, rule in sorted(self.type_rules.items(), key=lambda kv: int(kv[0])):
                if tree is self.rules_tree:
                    tree.insert("", tk.END, iid=key, values=(key, rule.get("label", f"Type {key}"),
                               f"{float(rule.get('drivability', -1.0)):.3f}", "yes" if rule.get("export", False) else "no", rule.get("color", "#777777")))
                else:
                    tree.insert("", tk.END, iid=key, text=key, values=(rule.get("label", f"Type {key}"),
                               f"{float(rule.get('drivability', -1.0)):.2f}"))

    def _on_rule_selected(self, _event=None):
        sel = self.mini_rules.selection()
        if sel and hasattr(self, "rules_tree"):
            self.rules_tree.selection_set(sel[0])

    def edit_selected_rule(self):
        tree = self.rules_tree if self.tabs.index(self.tabs.select()) == 1 else self.mini_rules
        sel = tree.selection()
        if not sel:
            messagebox.showinfo("Classification mapping", "Select a source type first.")
            return
        type_id = str(sel[0])
        rule = dict(self.type_rules[type_id])
        win = tk.Toplevel(self)
        win.title(f"Edit profile type {type_id}")
        win.transient(self); win.grab_set()
        frm = ttk.Frame(win, padding=10); frm.pack(fill=tk.BOTH, expand=True)
        label_var = tk.StringVar(value=str(rule.get("label", "")))
        drv_var = tk.StringVar(value=str(rule.get("drivability", -1.0)))
        export_var = tk.BooleanVar(value=bool(rule.get("export", False)))
        color_var = tk.StringVar(value=str(rule.get("color", "#777777")))
        ttk.Label(frm, text=f"Source type {type_id}").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        ttk.Label(frm, text="Classification label:").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Entry(frm, textvariable=label_var, width=28).grid(row=1, column=1, sticky="ew", pady=3)
        ttk.Label(frm, text="Drivability:").grid(row=2, column=0, sticky="w", pady=3)
        ttk.Entry(frm, textvariable=drv_var, width=12).grid(row=2, column=1, sticky="w", pady=3)
        ttk.Checkbutton(frm, text="Export as a DecalRoad when drivability > 0", variable=export_var).grid(row=3, column=0, columnspan=2, sticky="w", pady=3)
        ttk.Label(frm, text="Preview color:").grid(row=4, column=0, sticky="w", pady=3)
        color_row = ttk.Frame(frm); color_row.grid(row=4, column=1, sticky="ew")
        ttk.Entry(color_row, textvariable=color_var, width=12).pack(side=tk.LEFT)
        def choose_color():
            result = colorchooser.askcolor(color=color_var.get(), parent=win)
            if result and result[1]: color_var.set(result[1])
        ttk.Button(color_row, text="Choose...", command=choose_color).pack(side=tk.LEFT, padx=3)
        def apply():
            try:
                drv = float(drv_var.get())
                if not math.isfinite(drv): raise ValueError("Drivability must be finite.")
                color = color_var.get().strip()
                if len(color) != 7 or not color.startswith("#"): raise ValueError("Color must be #RRGGBB.")
                previous_rule = copy.deepcopy(self.type_rules[type_id])
                self.type_rules[type_id] = {"label": label_var.get().strip() or f"Type {type_id}",
                    "drivability": drv, "export": export_var.get(), "color": color}
                self.state_data["type_rules"] = copy.deepcopy(self.type_rules)
                self._log_adjustment(f"type:{type_id}", "update type rule", f"type {type_id}", previous_rule, copy.deepcopy(self.type_rules[type_id]), kind="type_rule")
                self._dirty = True; self._rules_dirty = True
                win.destroy(); self.rebuild_preview()
            except ValueError as exc:
                messagebox.showerror("Invalid rule", str(exc), parent=win)
        buttons = ttk.Frame(frm); buttons.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(buttons, text="Apply", command=apply).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(buttons, text="Cancel", command=win.destroy).pack(side=tk.LEFT)
        win.columnconfigure(0, weight=1); win.columnconfigure(1, weight=2)

    def restore_default_rules(self):
        if not messagebox.askyesno("Restore classification defaults", "Replace all type mappings with the built-in defaults?"):
            return
        previous_rules = copy.deepcopy(self.type_rules)
        self.type_rules = merged_type_rules()
        self.state_data["type_rules"] = copy.deepcopy(self.type_rules)
        self._log_adjustment("type_rules", "restore default mappings", "defaults", previous_rules, copy.deepcopy(self.type_rules), kind="type_rules_all")
        self._dirty = True; self._rules_dirty = True
        self.rebuild_preview()

    def _refresh_pattern_count(self):
        if hasattr(self, "pattern_count_var"):
            patterns = self.state_data.get("patterns", [])
            self.pattern_count_var.set(f"{len(patterns)} reusable pattern(s) ({sum(1 for p in patterns if p.get('enabled', True))} enabled)")

    def save_selected_as_pattern(self):
        if not self.selected_key or self.selected_key not in self.corridor_by_key:
            messagebox.showinfo("Save pattern", "Select a corridor first.")
            return
        local = self.state_data["overrides"].get(self.selected_key)
        if not local:
            messagebox.showinfo("Save pattern", "Apply a local override to this corridor first, then save it as a reusable pattern.")
            return
        c = self.corridor_by_key[self.selected_key]
        name = simpledialog.askstring("Save reusable pattern", "Pattern name:", parent=self)
        if not name:
            return
        pa = self.network.profiles[c["profile_start"]]
        pb = self.network.profiles[c["profile_end"]]
        pattern = {
            "name": name.strip(), "enabled": True,
            "match": {"zone_index": c["zone_index"], "start_type": c["source_type"],
                      "end_type": c["source_type_end"], "start_zone_count": int(pa.numZones),
                      "end_zone_count": int(pb.numZones),
                      "min_start_width": max(0.0, c["source_width_start"] - 0.05),
                      "max_start_width": c["source_width_start"] + 0.05},
            "overrides": copy.deepcopy(local),
        }
        self.state_data["patterns"].append(pattern)
        self._log_adjustment(self.selected_key, "save reusable pattern", name.strip(), None, pattern, kind="pattern_add")
        self._dirty = True
        self.rebuild_preview()
        messagebox.showinfo("Save reusable pattern", f"Pattern '{name}' saved. It will match similar source profiles.")

    def manage_patterns(self):
        win = tk.Toplevel(self); win.title("Reusable traffic patterns"); win.geometry("760x390")
        win.transient(self)
        cols = ("enabled", "name", "zone", "type", "matching")
        tree = ttk.Treeview(win, columns=cols, show="headings", height=12)
        for col, title, width in (("enabled", "Enabled", 60), ("name", "Name", 180), ("zone", "Zone", 55),
                                  ("type", "Type", 90), ("matching", "Match signature", 330)):
            tree.heading(col, text=title); tree.column(col, width=width)
        tree.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        def refresh():
            for i in tree.get_children(): tree.delete(i)
            for i, p in enumerate(self.state_data.get("patterns", [])):
                m = p.get("match", {})
                tree.insert("", tk.END, iid=str(i), values=("yes" if p.get("enabled", True) else "no", p.get("name", ""),
                    m.get("zone_index", "*"), f"{m.get('start_type','*')}->{m.get('end_type','*')}",
                    f"zones {m.get('start_zone_count','*')}->{m.get('end_zone_count','*')}, width {m.get('min_start_width','*')}..{m.get('max_start_width','*')}"))
        def toggle():
            sel=tree.selection()
            if not sel:return
            idx=int(sel[0]); p=self.state_data["patterns"][idx]; before=copy.deepcopy(p); p["enabled"]=not p.get("enabled", True)
            self._log_adjustment(f"pattern:{p.get('name','')}", "toggle pattern", str(p["enabled"]), before, copy.deepcopy(p), kind="pattern_edit")
            self._dirty=True; refresh(); self.rebuild_preview()
        def remove():
            sel=tree.selection()
            if not sel:return
            idx=int(sel[0]); p=self.state_data["patterns"][idx]
            if messagebox.askyesno("Remove pattern", f"Remove '{p.get('name','')}'?", parent=win):
                removed = self.state_data["patterns"].pop(idx)
                self._log_adjustment(f"pattern:{p.get('name','')}", "remove pattern", "removed", removed, None, kind="pattern_remove")
                self._dirty=True; refresh(); self.rebuild_preview()
        row=ttk.Frame(win);row.pack(fill=tk.X,padx=8,pady=(0,8))
        ttk.Button(row,text="Enable/disable",command=toggle).pack(side=tk.LEFT,padx=(0,4))
        ttk.Button(row,text="Remove selected",command=remove).pack(side=tk.LEFT,padx=4)
        ttk.Button(row,text="Close",command=win.destroy).pack(side=tk.RIGHT)
        refresh()

    # ---------- Save/load, validation, export ----------
    def save_overrides(self):
        path = pathlib.Path(self.override_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.state_data["type_rules"] = copy.deepcopy(self.type_rules)
        self.state_data["source_file"] = str(self.world_path) if self.world_path else self.state_data.get("source_file")
        self.state_data["include_decision_connectors"] = self.include_decision_connectors_var.get()
        self.state_data["over_objects"] = self.over_objects_var.get()
        try:
            self.state_data["vertical_offset"] = float(self.vertical_offset_var.get())
        except ValueError:
            self.state_data["vertical_offset"] = 150.0
        path.write_text(json.dumps(self.state_data, indent=2, ensure_ascii=False), encoding="utf-8")
        self._dirty = False; self._rules_dirty = False
        self.status_var.set(f"Saved overrides: {path}")
        print(f"[traffic_node_editor] Saved {len(self.state_data['overrides'])} local overrides and {len(self.state_data['patterns'])} patterns -> {path}")

    def load_overrides_dialog(self):
        path = filedialog.askopenfilename(title="Load traffic overrides", filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if path:
            try:
                data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
                self.state_data.update({k: data[k] for k in ("vertical_offset", "type_rules", "overrides", "patterns", "include_decision_connectors", "over_objects") if k in data})
                self.type_rules = merged_type_rules(self.state_data.get("type_rules"))
                self.vertical_offset_var.set(str(self.state_data.get("vertical_offset", 150.0)))
                self.include_decision_connectors_var.set(bool(self.state_data.get("include_decision_connectors", True)))
                self.over_objects_var.set(bool(self.state_data.get("over_objects", True)))
                self.override_path = pathlib.Path(path)
                self._dirty = False
                self.rebuild_preview()
                self.status_var.set(f"Loaded overrides: {path}")
            except (OSError, ValueError, TypeError) as exc:
                messagebox.showerror("Load overrides", str(exc))

    def _on_export_option_change(self):
        """Persist export toggles with the same override file as classification rules."""
        self.state_data["over_objects"] = self.over_objects_var.get()
        self._dirty = True
        self.status_var.set(f"Export setting changed: overObjects={self.over_objects_var.get()} (save overrides to persist)")

    def _on_decision_connector_change(self):
        self.state_data["include_decision_connectors"] = self.include_decision_connectors_var.get()
        self._dirty = True
        self.rebuild_preview()

    def set_vertical_offset(self):
        current = self.vertical_offset_var.get()
        value = simpledialog.askfloat("World vertical offset", "Add this vertical offset to NFS source Y coordinates:", initialvalue=float(current or 150), parent=self)
        if value is not None:
            old = self.state_data.get("vertical_offset", 150.0)
            self.vertical_offset_var.set(str(value)); self.state_data["vertical_offset"] = value
            self._log_adjustment("world:vertical_offset", "change vertical offset", str(value), old, value, kind="vertical_offset")
            self._dirty = True; self.rebuild_preview()

    def validate_network(self):
        if not self.preview:
            messagebox.showinfo("Validate network", "Open a CARP file first.")
            return
        report = self._format_report()
        self._set_report(report)
        warnings = self.preview["warnings"]
        self.status_var.set(f"Validation complete: {len(warnings)} warning(s), {len(self.export_roads):,} continuous DecalRoad chains")
        print("[traffic_node_editor] Validation report:\n" + report)
        messagebox.showinfo("Validation complete", f"{len(warnings)} warning(s)\n{sum(1 for c in self.corridors if c.get('export', False)):,} source zone corridors reduce to {len(self.export_roads):,} continuous DecalRoads. See Exporter tab for diagnostics.")

    def _format_report(self):
        if not self.preview:
            return "No network loaded."
        p = self.preview
        lines = ["TRAFFIC NODE EDITOR - NETWORK REPORT", "="*54]
        if self.world_path: lines.append(f"Source: {self.world_path}")
        lines.append(f"Coordinate mapping: {p['coordinate_mapping']}")
        lines.append(f"Vertical offset: +{p['vertical_offset']} m (applied to DecalRoad node Z at serialization; not baked into preview data)")
        lines.append("")
        lines.append("Source counts:")
        for k,v in p["network_counts"].items(): lines.append(f"  {k}: {v:,}")
        lines.append("Derived counts:")
        for k,v in p["derived_counts"].items(): lines.append(f"  {k}: {v:,}")
        lines.append(f"  active patterns: {sum(1 for x in self.state_data.get('patterns', []) if x.get('enabled', True))}")
        lines.append(f"  local overrides: {len(self.state_data.get('overrides', {}))}")
        lines.append(f"Source zone corridors: {len(self.corridors):,}")
        lines.append(f"Exportable zone corridors: {sum(1 for c in self.corridors if c.get('export', False)):,}")
        lines.append(f"Continuous DecalRoad chains: {len(getattr(self, 'export_roads', [])):,}")
        lines.append(f"Zone endpoint harmonization: {getattr(self, 'zone_endpoint_report', {})}")
        lines.append(f"Geometry source: Zones-mode profile-zone nodes; node widths are inherited from zone widths")
        lines.append(f"overObjects: {self.over_objects_var.get()}")
        lines.append(f"Decision connectors included: {self.include_decision_connectors_var.get()}")
        lines.append("")
        lines.append(f"Warnings ({len(p['warnings'])}):")
        for warning in p["warnings"][:1000]:
            lines.append(f"  [{warning.get('kind')}] segment={warning.get('segment','?')} zone={warning.get('zone','?')}: {warning.get('message','')}")
        if len(p["warnings"]) > 1000: lines.append(f"  ... {len(p['warnings'])-1000} more")
        lines.append("")
        lines.append("Exporter note: DecalRoad lane offset/orientation and elevation must be checked in BeamNG on a small district before full-city use.")
        return "\n".join(lines)

    def _update_report(self):
        if hasattr(self, "report_text"):
            self._set_report(self._format_report())

    def _set_report(self, value):
        if not hasattr(self, "report_text"):
            return
        self.report_text.configure(state=tk.NORMAL)
        self.report_text.delete("1.0", tk.END)
        self.report_text.insert("1.0", value)
        self.report_text.configure(state=tk.DISABLED)

    def export_dialog(self):
        if not self.preview:
            messagebox.showinfo("Export to BeamNG", "Open a CARP file first.")
            return
        try:
            material = self.material_var.get().strip() or "road_invisible"
            texture_length = float(self.texture_length_var.get())
            priority = int(self.render_priority_var.get())
            if not math.isfinite(texture_length) or texture_length <= 0:
                raise ValueError("Texture length must be positive and finite.")
            vertical_offset = float(self.vertical_offset_var.get())
            if vertical_offset != self.preview["vertical_offset"]:
                self.rebuild_preview()
        except ValueError as exc:
            messagebox.showerror("Export settings", str(exc)); return
        path = filedialog.asksaveasfilename(title="Export BeamNG items.level.json", defaultextension=".jsonl",
                    initialfile="traffic_roads.items.level.json", filetypes=[("BeamNG line-delimited JSON", "*.json *.jsonl"), ("All files", "*.*")])
        if not path:
            return
        try:
            count = write_items_level(self.export_roads, path, material=material,
                                      texture_length=texture_length, render_priority=priority,
                                      over_objects=self.over_objects_var.get(),
                                      vertical_offset=vertical_offset)
            manifest_path = pathlib.Path(path).with_name(pathlib.Path(path).stem + ".source_manifest.json")
            write_source_manifest(self.export_roads, manifest_path, vertical_offset=vertical_offset)
            report_path = pathlib.Path(path).with_name(pathlib.Path(path).stem + ".report.json")
            report = {"source_file": str(self.world_path) if self.world_path else None,
                      "network_counts": self.preview["network_counts"],
                      "derived_counts": self.preview["derived_counts"],
                      "source_zone_corridors": len(self.corridors),
                      "exportable_zone_corridors": sum(1 for c in self.corridors if c.get("export", False)),
                      "continuous_road_chains": len(self.export_roads),
                      "exported_decalroads": count, "warning_count": len(self.preview["warnings"]),
                      "warnings": self.preview["warnings"], "material": material,
                      "overObjects": self.over_objects_var.get(),
                      "include_decision_connectors": self.include_decision_connectors_var.get(),
                      "vertical_offset": vertical_offset,
                      "vertical_offset_applied_during_serialization": True,
                      "zone_endpoint_harmonization": self.zone_endpoint_report,
                      "roads_have_node_arrays": True}
            report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
            self.status_var.set(f"Exported {count:,} continuous DecalRoads to {path}")
            print(f"[traffic_node_editor] Exported {count} DecalRoads -> {path}")
            print(f"[traffic_node_editor] Source manifest -> {manifest_path}")
            print(f"[traffic_node_editor] Export report -> {report_path}")
            messagebox.showinfo("Export complete", f"Wrote {count:,} continuous DecalRoad objects from {sum(1 for c in self.corridors if c.get('export', False)):,} exportable Zones-mode zone paths.\n\nEach DecalRoad contains its full ordered node array with per-node width.\nZ offset applied during export: +{vertical_offset:g} m\noverObjects: {self.over_objects_var.get()}\nRoads: {path}\nSource manifest: {manifest_path}\nDiagnostics: {report_path}\n\nTest lane direction, lateral positioning, connector coverage and autoJunction in BeamNG on a small area before full-level use.")
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc))
            print(f"[traffic_node_editor] Export ERROR: {exc}")

    def show_help(self):
        messagebox.showinfo("Traffic Node Editor", "Standalone CARP road-network editor.\n\n1. Open the main region/world .BUN with a CARP road network (e.g. L5RA.BUN), not STREAM*.BUN.\n2. Inspect colored profile-zone corridors and select one to edit.\n3. Adjust type mappings under Rules & Patterns.\n4. Save local overrides and reusable patterns.\n5. Export from Zones-mode profile geometry. All sampled points for a continuous lane are the node array of one DecalRoad; widths are stored per node. CARP turn connectors remain full curved DecalRoads and share harmonized endpoints with road approaches. The exporter applies the configured Z offset (default +150 m) exactly once. `overObjects` controls projection over existing meshes.\n\nValidate height, lane width, oneWay and autoJunction in BeamNG on a small district before full-level export.")

    def _on_close(self):
        if self._dirty:
            ans = messagebox.askyesnocancel("Unsaved changes", "Traffic editor changes have not been saved. Save overrides before exiting?")
            if ans is None:
                return
            if ans:
                self.save_overrides()
        self.destroy()


def main():
    app = TrafficNodeEditor(sys.argv[1] if len(sys.argv) > 1 else None)
    app.mainloop()


if __name__ == "__main__":
    main()
