"""
Small Tkinter viewer for Black Box NFS region files, across games.

Layout:
  File menu      Open region file, Open stream file, Open trough file, Open collision file, Clear
                 stream data, Game (set it before you open a file), Exit
  Settings menu  Road network rotation (0/90/180/270, default 270). It turns
                 the road network nodes only; 270 is what lines them up with
                 the boundaries
  Viewer tab     four layers - Nodes (L5RA traffic nodes), Sections
                 (VisibleSections boundaries and relations), Zones (TrackPath
                 zones and barriers), Troughs (TroughBoundary.bin outlines).
                 "Show" checkboxes turn any combination on. The "Mode" radio
                 buttons pick the ACTIVE layer: it owns the options row below
                 the mode bar and the click selection (a click only picks from
                 the active layer). Layer order (right end of the options row)
                 decides which layer is drawn on top: Raise / Lower move the
                 active layer. Switching mode, layers or order never moves the
                 viewport; only Fit to view and opening a region file do
  Export tab     sections.json (needs the region file and a stream file).
                 The road node export will come here

Sections: yellow polygons (orange with a non-zero elevationHash) labeled with
their letter+number section ID (e.g. "A101"), and their adjacency relations
(magenta lines between section centers) when the selected game's relations
parser succeeds. Click a polygon to inspect its raw fields. Troughs: drivable
areas in green, holes in red.

Group names come from hashes_main.txt through nfs_hash_dictionary.

Pick the game in File > Game before opening a file - see
nfs_region_parser.GAME_PARSERS for what's verified vs. still a hypothesis
per game.

Usage: python3 nfs_region_viewer.py [path/to/file.BUN] [game]
(If no path is given, open one from the File menu. game defaults to None.)
"""
# stream bootstrap: make the shared library folders importable
import pathlib as _pl, sys as _sys
_sys.path[:0] = [str(_pl.Path(__file__).resolve().parents[1] / _d) for _d in ['common', 'region', 'scenery', 'solids_materials']]

import sys
import json
import colorsys
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from nfs_region_parser import load_region_file, GAME_PARSERS, get_label_functions
from export_sections import build_sections, write_sections_json
from nfs_region_common import section_letter as _default_section_letter
from nfs_region_common import format_section_label as _default_format_section_label
from nfs_region_common import section_subsection
from nfs_trackpath import ZONE_TYPES, STREAMER_PREDICTION
from nfs_trough_boundary import load_trough_boundary
from nfs_collision_pack import load_collision_packs, collision_drawables


class RegionViewerApp(tk.Tk):
    def __init__(self, path=None, game='None'):
        super().__init__()
        self.title("NFS Region Viewer")
        self.geometry("1200x800")

        self.world = None
        self.current_path = None
        self.stream_scenery = None
        self.stream_path = None
        self.scale = 1.0
        self.offset_x = 0.0
        self.offset_y = 0.0
        self.selected_id = None
        self.show_relations = tk.BooleanVar(value=True)
        self.show_labels = tk.BooleanVar(value=True)
        self.dim_nondrivable = tk.BooleanVar(value=False)
        self.game = tk.StringVar(value=game if game in GAME_PARSERS else 'None')
        self.active_mode = tk.StringVar(value='sections')   # which layer owns the options row and the clicks
        self.layer_order = ['sections', 'troughs', 'collision', 'zones', 'nodes']   # bottom to top: the last one is drawn on top
        self.info_open = {}   # section info category -> open (True) or collapsed (False), kept between clicks
        self.layer_vars = {                                  # which layers are drawn
            'nodes': tk.BooleanVar(value=False),
            'sections': tk.BooleanVar(value=True),
            'zones': tk.BooleanVar(value=True),
            'troughs': tk.BooleanVar(value=True),
            'collision': tk.BooleanVar(value=False),
        }
        self.road_rotation = tk.StringVar(value='270')
        self.show_road_width = tk.BooleanVar(value=True)
        self.search_var = tk.StringVar()
        self.highlighted_ids = set()
        self.section_letter = _default_section_letter
        self.format_section_label = _default_format_section_label
        self._drag_start = None
        self._dragged = False
        self._redraw_after_id = None
        self._road_chains = None  # cache: list of node-index lists, invalidated on file load
        self._road_cache = None   # cache: rotated chain points, boxes, widths; rebuilt on a new file or rotation
        self._boundary_boxes = {}  # cache: boundary ID -> (min x, min y, max x, max y), cleared on file load
        self._outline_spacing = {}  # cache: id(point list) -> average segment length, cleared on file load
        self.selected_node_index = None
        self._overlap_clusters = None  # cache: {node_index: (rank, cluster_size)}, invalidated on file load
        self.show_zones = tk.BooleanVar(value=True)
        self.show_barriers = tk.BooleanVar(value=True)
        self.show_zone_labels = tk.BooleanVar(value=True)
        self.visible_zone_types = set(ZONE_TYPES)
        self.selected_zones = set()      # zone.offset values under the last click
        self.selected_barriers = set()   # barrier.offset values near the last click
        self.troughs = None              # TroughBoundary from TroughBoundary.bin, or None
        self.trough_path = None
        self.show_trough_labels = tk.BooleanVar(value=False)
        self.show_trough_holes = tk.BooleanVar(value=True)
        self.selected_troughs = set()    # trough indices under the last click
        self.collision_pieces = None     # article boxes from the stream file's collision packs
        self.collision_walls = None      # barrier edges (wall segments) from the same packs
        self.collision_path = None
        self.show_collision_pieces = tk.BooleanVar(value=True)
        self.show_collision_walls = tk.BooleanVar(value=True)
        self.show_collision_grouped_only = tk.BooleanVar(value=False)
        self.selected_collision = None   # ('piece' or 'wall', index in that list)
        self._collision_lines = []       # wall chains: (flat plane coords, has group, wall ids, box)
        self._collision_line_grid = {}   # grid cell -> indexes of the wall chains that touch it
        self._collision_piece_grid = {}  # grid cell -> indexes of the pieces that touch it

        self._build_ui()

        if path:
            self.load_file(path)

    # ---------- UI ----------
    MODES = [('nodes', 'Nodes'), ('sections', 'Sections'), ('zones', 'Zones'), ('troughs', 'Troughs'),
             ('collision', 'Collision')]
    MODE_TITLES = {'nodes': 'Node info', 'sections': 'Section info',
                   'zones': 'Zone info', 'troughs': 'Trough info', 'collision': 'Collision info'}

    def _build_ui(self):
        self._build_menus()

        self.tabs = ttk.Notebook(self)
        self.tabs.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        viewer_tab = tk.Frame(self.tabs)
        export_tab = tk.Frame(self.tabs)
        self.tabs.add(viewer_tab, text="Viewer")
        self.tabs.add(export_tab, text="Export")

        # Footer first, so the status lines keep their place at the bottom
        footer = tk.Frame(self)
        footer.pack(side=tk.BOTTOM, fill=tk.X)
        self.status_label = tk.Label(footer, text="No file loaded", anchor="w")
        self.status_label.pack(side=tk.TOP, fill=tk.X, padx=6, pady=(2, 0))
        self.stream_status_label = tk.Label(footer, text="No stream file loaded", anchor="w", fg="#888")
        self.stream_status_label.pack(side=tk.TOP, fill=tk.X, padx=6, pady=(0, 0))
        self.trough_status_label = tk.Label(footer, text="No trough file loaded", anchor="w", fg="#888")
        self.trough_status_label.pack(side=tk.TOP, fill=tk.X, padx=6, pady=(0, 0))
        self.collision_status_label = tk.Label(footer, text="No collision file loaded", anchor="w", fg="#888")
        self.collision_status_label.pack(side=tk.TOP, fill=tk.X, padx=6, pady=(0, 0))
        self.perf_label = tk.Label(footer, text="", anchor="e", fg="#888")
        self.perf_label.pack(side=tk.TOP, fill=tk.X, padx=6, pady=(0, 2))

        self._build_viewer_tab(viewer_tab)
        self._build_export_tab(export_tab)
        self._on_mode_change()

    def _build_menus(self):
        menubar = tk.Menu(self, tearoff=False)

        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label="Open region file...", command=self.open_dialog)
        file_menu.add_command(label="Open stream file...", command=self.open_stream_dialog)
        file_menu.add_command(label="Open trough file...", command=self.open_trough_dialog)
        file_menu.add_command(label="Open collision file (stream)...", command=self.open_collision_dialog)
        file_menu.add_separator()
        file_menu.add_command(label="Clear stream data", command=self.clear_stream_data)
        file_menu.add_separator()
        game_menu = tk.Menu(file_menu, tearoff=False)
        for game_name in sorted(GAME_PARSERS.keys()):
            game_menu.add_radiobutton(label=game_name, value=game_name, variable=self.game,
                                       command=self._on_game_change)
        file_menu.add_cascade(label="Game", menu=game_menu)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.destroy)
        menubar.add_cascade(label="File", menu=file_menu)

        settings_menu = tk.Menu(menubar, tearoff=False)
        rotation_menu = tk.Menu(settings_menu, tearoff=False)
        for degrees in ('0', '90', '180', '270'):
            rotation_menu.add_radiobutton(label=degrees, value=degrees, variable=self.road_rotation,
                                           command=self._on_rotation_change)
        settings_menu.add_cascade(label="Road network rotation", menu=rotation_menu)
        menubar.add_cascade(label="Settings", menu=settings_menu)

        self.config(menu=menubar)

    def _build_viewer_tab(self, parent):
        # Mode bar: the active layer (radio buttons) and which layers are drawn (checkboxes)
        modebar = tk.Frame(parent)
        modebar.pack(side=tk.TOP, fill=tk.X)
        tk.Label(modebar, text="Mode:").pack(side=tk.LEFT, padx=(6, 2), pady=4)
        for key, label in self.MODES:
            tk.Radiobutton(modebar, text=label, value=key, variable=self.active_mode,
                            command=self._on_mode_change).pack(side=tk.LEFT, padx=2)
        tk.Label(modebar, text="     Show:").pack(side=tk.LEFT, padx=(12, 2))
        for key, label in self.MODES:
            tk.Checkbutton(modebar, text=label, variable=self.layer_vars[key],
                            command=self.redraw).pack(side=tk.LEFT, padx=2)
        tk.Button(modebar, text="Fit to view", command=self.fit_to_view).pack(side=tk.RIGHT, padx=6, pady=2)

        # Options row: one frame per mode, only the active one is packed
        self.options_holder = tk.Frame(parent, relief=tk.GROOVE, borderwidth=1)
        self.options_holder.pack(side=tk.TOP, fill=tk.X, padx=4, pady=(0, 2))
        self.mode_options = {key: tk.Frame(self.options_holder) for key, _ in self.MODES}

        order_frame = tk.Frame(self.options_holder)
        order_frame.pack(side=tk.RIGHT)
        self.order_label = tk.Label(order_frame, text="", fg="#555")
        self.order_label.pack(side=tk.LEFT, padx=(0, 6))
        tk.Label(order_frame, text="Layer order:").pack(side=tk.LEFT)
        tk.Button(order_frame, text="Raise", command=lambda: self._move_layer(1)).pack(side=tk.LEFT, padx=(4, 2), pady=1)
        tk.Button(order_frame, text="Lower", command=lambda: self._move_layer(-1)).pack(side=tk.LEFT, padx=(0, 6), pady=1)

        nodes_row = self.mode_options['nodes']
        tk.Checkbutton(nodes_row, text="Show road width", variable=self.show_road_width,
                        command=self.redraw).pack(side=tk.LEFT, padx=8, pady=2)
        self.rotation_label = tk.Label(nodes_row, text="", fg="#555")
        self.rotation_label.pack(side=tk.LEFT, padx=8)

        sections_row = self.mode_options['sections']
        tk.Checkbutton(sections_row, text="Show relations", variable=self.show_relations,
                        command=self.redraw).pack(side=tk.LEFT, padx=8, pady=2)
        tk.Checkbutton(sections_row, text="Section labels", variable=self.show_labels,
                        command=self.redraw).pack(side=tk.LEFT, padx=8)
        tk.Checkbutton(sections_row, text="Dim non-drivable", variable=self.dim_nondrivable,
                        command=self.redraw).pack(side=tk.LEFT, padx=8)
        tk.Label(sections_row, text="   Search scenery names:").pack(side=tk.LEFT, padx=(8, 2))
        search_entry = tk.Entry(sections_row, textvariable=self.search_var, width=30)
        search_entry.pack(side=tk.LEFT, padx=(0, 4))
        search_entry.bind("<Return>", lambda e: self._search_scenery())
        tk.Button(sections_row, text="Find", command=self._search_scenery).pack(side=tk.LEFT)
        tk.Button(sections_row, text="Groups...", command=self.open_group_view).pack(side=tk.LEFT, padx=(12, 4))

        zones_row = self.mode_options['zones']
        tk.Checkbutton(zones_row, text="Zone areas", variable=self.show_zones,
                        command=self.redraw).pack(side=tk.LEFT, padx=8, pady=2)
        tk.Checkbutton(zones_row, text="Zone labels", variable=self.show_zone_labels,
                        command=self.redraw).pack(side=tk.LEFT, padx=8)
        tk.Checkbutton(zones_row, text="Barriers", variable=self.show_barriers,
                        command=self.redraw).pack(side=tk.LEFT, padx=8)
        tk.Button(zones_row, text="Zone types...", command=self.open_zone_types_dialog).pack(side=tk.LEFT, padx=8)

        troughs_row = self.mode_options['troughs']
        tk.Checkbutton(troughs_row, text="Trough labels", variable=self.show_trough_labels,
                        command=self.redraw).pack(side=tk.LEFT, padx=8, pady=2)
        tk.Checkbutton(troughs_row, text="Show holes", variable=self.show_trough_holes,
                        command=self.redraw).pack(side=tk.LEFT, padx=8)

        collision_row = self.mode_options['collision']
        tk.Checkbutton(collision_row, text="Pieces (article boxes)", variable=self.show_collision_pieces,
                        command=self.redraw).pack(side=tk.LEFT, padx=8, pady=2)
        tk.Checkbutton(collision_row, text="Walls (barrier edges)", variable=self.show_collision_walls,
                        command=self.redraw).pack(side=tk.LEFT, padx=8)
        tk.Checkbutton(collision_row, text="Only with a group number", variable=self.show_collision_grouped_only,
                        command=self.redraw).pack(side=tk.LEFT, padx=8)

        body = tk.Frame(parent)
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.canvas = tk.Canvas(body, bg="black")
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        info_frame = tk.Frame(body, width=280)
        info_frame.pack(side=tk.RIGHT, fill=tk.Y)
        info_frame.pack_propagate(False)
        self.info_title = tk.Label(info_frame, text="Section info", font=("TkDefaultFont", 11, "bold"))
        self.info_title.pack(anchor="w", padx=8, pady=(8, 0))
        self.info_text = tk.Text(info_frame, wrap="word", state="disabled", height=30)
        self.info_text.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.info_text.tag_configure("category", font=("TkDefaultFont", 10, "bold"), foreground="#1a4f8a")

        self.canvas.bind("<Configure>", lambda e: self.redraw())
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<MouseWheel>", self._on_wheel)        # Windows / macOS
        self.canvas.bind("<Button-4>", lambda e: self._zoom(e, 1.15))   # Linux scroll up
        self.canvas.bind("<Button-5>", lambda e: self._zoom(e, 1 / 1.15))  # Linux scroll down

    def _build_export_tab(self, parent):
        sections_box = tk.LabelFrame(parent, text="sections.json", padx=10, pady=8)
        sections_box.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(10, 4))
        tk.Label(sections_box, anchor="w", justify=tk.LEFT,
                 text="Drivable-section boundary polygons plus each drivable section's related-section\n"
                      "list, for the runtime streamer. Needs a region file and a stream file.").pack(anchor="w")
        tk.Button(sections_box, text="Export sections.json...",
                   command=self.export_sections_json).pack(anchor="w", pady=(6, 0))

        nodes_box = tk.LabelFrame(parent, text="Road nodes", padx=10, pady=8)
        nodes_box.pack(side=tk.TOP, fill=tk.X, padx=10, pady=4)
        tk.Label(nodes_box, anchor="w", justify=tk.LEFT,
                 text="Not written yet. The L5RA traffic node export will be added here.").pack(anchor="w")
        tk.Button(nodes_box, text="Export road nodes...", state="disabled").pack(anchor="w", pady=(6, 0))

    # ---------- Modes and layers ----------
    def _on_mode_change(self):
        """Shows the options row of the active mode and turns that layer on.
        Does not touch the viewport. Clears the old selection so no info from
        another layer stays next to a click in this one."""
        mode = self.active_mode.get()
        for row in self.mode_options.values():
            row.pack_forget()
        self.mode_options[mode].pack(side=tk.LEFT, fill=tk.X)
        self.layer_vars[mode].set(True)
        self.rotation_label.config(text=f"Rotation {self.road_rotation.get()} (Settings > Road network rotation)")
        self.info_title.config(text=self.MODE_TITLES[mode])
        self._update_order_label()
        self.selected_id = None
        self.selected_node_index = None
        self.selected_zones = set()
        self.selected_barriers = set()
        self.selected_troughs = set()
        self.selected_collision = None
        self._show_hint(mode)
        self.redraw()

    def _show_hint(self, mode):
        if mode == 'sections':
            self._show_info(None)
            return
        hints = {
            'nodes': ("No road network in this file." if self.world and not self.world.road_network
                      else "Click a road node to inspect it."),
            'zones': "Click a zone or barrier.",
            'troughs': "Click inside a trough outline.",
            'collision': ("Open the stream file with File > Open collision file." if not self.collision_pieces
                          else "Click a wall or a piece. Orange pieces have strips, red walls have a "
                               "group number, cyan walls have none."),
        }
        self.info_text.config(state="normal")
        self.info_text.delete("1.0", tk.END)
        self.info_text.insert(tk.END, hints[mode])
        self.info_text.config(state="disabled")

    def _move_layer(self, step):
        """Moves the active layer up (step 1) or down (step -1) in the draw
        order. The last layer in layer_order is drawn on top. The viewport
        stays where it is."""
        key = self.active_mode.get()
        index = self.layer_order.index(key)
        new_index = index + step
        if 0 <= new_index < len(self.layer_order):
            self.layer_order[index], self.layer_order[new_index] = (
                self.layer_order[new_index], self.layer_order[index])
            self._update_order_label()
            self.redraw()

    def _update_order_label(self):
        names = dict(self.MODES)
        self.order_label.config(text="bottom > top:  " + "  <  ".join(names[k] for k in self.layer_order))

    def _on_rotation_change(self):
        """Rotation turns the road network nodes only, so the other layers and
        the viewport stay where they are."""
        self.rotation_label.config(text=f"Rotation {self.road_rotation.get()} (Settings > Road network rotation)")
        visible = [key for key, var in self.layer_vars.items() if var.get()]
        if visible == ['nodes']:
            self.fit_to_view()   # the nodes moved and nothing else shows where
        else:
            self.redraw()

    def _on_game_change(self, event=None):
        if self.current_path:
            self.load_file(self.current_path)

    # ---------- File loading ----------
    def open_dialog(self):
        path = filedialog.askopenfilename(
            title="Open NFS region file",
            filetypes=[("Region bundle", "*.bun *.BUN"), ("All files", "*.*")])
        if path:
            self.load_file(path)

    def load_file(self, path):
        try:
            self.world = load_region_file(path, game=self.game.get())
        except Exception as e:
            messagebox.showerror("Failed to load file", str(e))
            return
        self.section_letter, self.format_section_label = get_label_functions(self.game.get())
        self.current_path = path
        self.selected_id = None
        self.selected_node_index = None
        self._road_chains = None
        self._road_cache = None
        self._boundary_boxes = {}
        self._outline_spacing = {}
        self._overlap_clusters = None
        self.selected_zones = set()
        self.selected_barriers = set()
        self._update_status(path)

        # A region file can itself carry scenery override/group data (a real
        # file we tested had 0 instances but 20000+ overrides) - try pulling
        # that in automatically so it doesn't require a separate manual
        # "Open stream file..." pass on the exact same file. Silently skip
        # if this file has none of that data (or isn't in this chunk format
        # at all) rather than bothering the user with an error for what's a
        # normal, expected case.
        try:
            from nfs_stream_scenery import load_stream_scenery
            auto_loaded = load_stream_scenery(path)
            if auto_loaded.sections or auto_loaded.overrides or auto_loaded.groups:
                if self.stream_scenery is None:
                    self.stream_scenery = auto_loaded
                else:
                    self.stream_scenery.merge(auto_loaded)
                self.stream_status_label.config(text=f"Stream: {self.stream_scenery.summary()}", fg="#000000")
        except Exception:
            pass  # not fatal - this is a bonus, not the primary load path

        self._show_hint(self.active_mode.get())
        self.fit_to_view()

    def open_stream_dialog(self):
        path = filedialog.askopenfilename(
            title="Open NFS stream file (e.g. STREAML5RA)",
            filetypes=[("Stream bundle", "*.bun *.BUN"), ("All files", "*.*")])
        if path:
            self.load_stream_file(path)

    def load_stream_file(self, path):
        try:
            from nfs_stream_scenery import load_stream_scenery
            loaded = load_stream_scenery(path)
        except Exception as e:
            messagebox.showerror("Failed to open stream file", str(e))
            return
        if self.stream_scenery is None:
            self.stream_scenery = loaded
            self.stream_path = path
        else:
            # Merge, don't replace - instances and overrides commonly live in
            # different files (e.g. a region file's overrides vs. a stream
            # file's instances), so loading a second file should combine
            # with the first rather than throw it away.
            self.stream_scenery.merge(loaded)
            self.stream_path = f"{self.stream_path} + {path}"
        self.stream_status_label.config(text=f"Stream: {self.stream_scenery.summary()}", fg="#000000")
        if self.active_mode.get() == 'sections':
            self._show_info(self.world.by_id.get(self.selected_id) if self.world else None)

    def open_trough_dialog(self):
        path = filedialog.askopenfilename(
            title="Open TroughBoundary.bin",
            filetypes=[("Trough boundary", "*.bin *.BIN"), ("All files", "*.*")])
        if path:
            self.load_trough_file(path)

    def load_trough_file(self, path):
        try:
            loaded = load_trough_boundary(path)
        except Exception as e:
            messagebox.showerror("Failed to open trough file", str(e))
            return
        self.troughs = loaded
        self.trough_path = path
        self.selected_troughs = set()
        self.trough_status_label.config(text=f"Troughs: {loaded.summary()}", fg="#000000")
        self.redraw()

    def open_collision_dialog(self):
        path = filedialog.askopenfilename(
            title="Open the stream file with the collision packs (e.g. STREAML5RA)",
            filetypes=[("Stream bundle", "*.bun *.BUN"), ("All files", "*.*")])
        if path:
            self.load_collision_file(path)

    def load_collision_file(self, path):
        try:
            packs = load_collision_packs(path)
            pieces, walls = collision_drawables(packs)
        except Exception as e:
            messagebox.showerror("Failed to read collision packs", str(e))
            return
        self.collision_pieces = pieces
        self.collision_walls = walls
        self.collision_path = path
        self.selected_collision = None
        self._build_collision_index()
        self.collision_status_label.config(
            text=f"Collision: {len(packs)} pack(s), {len(pieces)} piece(s) with strips, "
                 f"{len(walls)} wall(s)", fg="#000000")
        self.layer_vars['collision'].set(True)
        self.redraw()

    def clear_stream_data(self):
        self.stream_scenery = None
        self.stream_path = None
        self.stream_status_label.config(text="No stream file loaded", fg="#888")
        if self.active_mode.get() == 'sections':
            self._show_info(self.world.by_id.get(self.selected_id) if self.world else None)

    def _update_status(self, path):
        w = self.world
        drv = f", {len(w.drivable_section_ids)} drivable (LODOffset={w.lod_offset})" \
            if w.drivable_section_ids or w.lod_offset is not None else ""
        rn = f", road network: {len(w.road_network.nodes)} nodes/{len(w.road_network.segments)} segments" \
            if w.road_network else ", no road network in this file"
        tp = self._track_path_status()
        self.status_label.config(
            text=f"{path.split('/')[-1]}  [{self.game.get()}]  |  {len(w.boundaries)} boundaries, "
                 f"{len(w.relations)} relation records, {len(w.elevation)} elevation triangles{drv}{rn}{tp}")

    # ---------- Coordinate transform ----------
    def fit_to_view(self):
        """Fits the layers that are shown. If none of them has points (for
        example only Zones shown), fits everything that has points."""
        if not self.world:
            return
        visible = [key for key, var in self.layer_vars.items() if var.get()]
        xs, ys = self._extent_points(visible)
        if not xs:
            xs, ys = self._extent_points(['sections', 'nodes', 'troughs'])
        if xs:
            self._fit_extent(min(xs), max(xs), min(ys), max(ys))

    def _extent_points(self, layers):
        xs, ys = [], []
        if 'sections' in layers:
            bxs, bys = self._boundary_points()
            xs += bxs
            ys += bys
        if 'nodes' in layers:
            rxs, rys = self._road_points()
            xs += rxs
            ys += rys
        if 'troughs' in layers and self.troughs:
            for polygon in self.troughs.polygons:
                xs += [polygon.bbox_min[0], polygon.bbox_max[0]]
                ys += [polygon.bbox_min[1], polygon.bbox_max[1]]
        if 'collision' in layers and self.collision_pieces:
            # game (x, z) goes to the plane of the section boundaries as (z, -x)
            for piece in self.collision_pieces:
                xs += [piece[7], piece[9]]
                ys += [-piece[6], -piece[8]]
        return xs, ys

    def _boundary_points(self):
        xs, ys = [], []
        for b in self.world.boundaries:
            for (x, y) in b.points:
                xs.append(x)
                ys.append(y)
        return xs, ys

    def _rotate_xy(self, x, y):
        """Applies self.road_rotation (0/90/180/270, degrees) to a 2D point.
        Road network world coords (X, world-Z) don't necessarily share the
        same 2D orientation as VisibleSections boundary points - on at least
        one real Carbon file, the road network came out rotated 90 degrees
        relative to the boundaries. Confirmed default going forward: 270 is
        the rotation that actually matches the boundaries for this game -
        still a user-adjustable dial since this can vary by game or file."""
        deg = int(self.road_rotation.get())
        if deg == 90:
            return -y, x
        if deg == 180:
            return -x, -y
        if deg == 270:
            return y, -x
        return x, y

    def _road_points(self):
        rn = self.world.road_network
        if not rn or not rn.nodes:
            return [], []
        pts = [self._rotate_xy(n.position[0], n.position[2]) for n in rn.nodes]
        return [p[0] for p in pts], [p[1] for p in pts]

    def _fit_extent(self, min_x, max_x, min_y, max_y):
        w = max(max_x - min_x, 1.0)
        h = max(max_y - min_y, 1.0)
        cw = max(self.canvas.winfo_width(), 100)
        ch = max(self.canvas.winfo_height(), 100)
        margin = 0.9
        self.scale = min(cw / w, ch / h) * margin
        # Center the world in the canvas. Y is flipped so +Y (game "north")
        # points up on screen instead of down.
        self.offset_x = cw / 2 - self.scale * (min_x + max_x) / 2
        self.offset_y = ch / 2 + self.scale * (min_y + max_y) / 2
        self.redraw()

    def world_to_canvas(self, x, y):
        return (self.offset_x + x * self.scale, self.offset_y - y * self.scale)

    def canvas_to_world(self, cx, cy):
        return ((cx - self.offset_x) / self.scale, (self.offset_y - cy) / self.scale)

    def _visible_world_bounds(self, margin=1.2):
        """World-space rectangle currently visible on the canvas, expanded by
        `margin` (a screen-size fraction, not a fixed distance) so panning a
        little doesn't immediately reveal un-culled empty space. Used to skip
        building canvas items for geometry that's nowhere near the viewport
        on a big level - the expensive part isn't the math, it's the number
        of canvas.create_* calls.

        World-Y is flipped relative to canvas-Y (world_to_canvas does
        offset_y - y*scale), so the two canvas_to_world() calls below don't
        come back in min/max order - min()/max() them explicitly rather than
        assuming which corner maps to which bound."""
        cw = max(self.canvas.winfo_width(), 100)
        ch = max(self.canvas.winfo_height(), 100)
        pad_x = cw * (margin - 1.0) / 2
        pad_y = ch * (margin - 1.0) / 2
        wx0, wy0 = self.canvas_to_world(-pad_x, -pad_y)
        wx1, wy1 = self.canvas_to_world(cw + pad_x, ch + pad_y)
        return (min(wx0, wx1), min(wy0, wy1), max(wx0, wx1), max(wy0, wy1))

    @staticmethod
    def _bbox_intersects(ax0, ay0, ax1, ay1, bx0, by0, bx1, by1):
        return ax0 <= bx1 and ax1 >= bx0 and ay0 <= by1 and ay1 >= by0

    # ---------- Drawing ----------
    def redraw(self):
        started = time.perf_counter()
        self.canvas.delete("all")
        if not self.world:
            return
        draw = {
            'sections': self._redraw_boundaries,
            'troughs': self._redraw_troughs,
            'collision': self._redraw_collision,
            'zones': self._redraw_track_paths,
            'nodes': self._redraw_road_network,
        }
        timings = []
        for key in self.layer_order:   # bottom first, so the last layer ends up on top
            if self.layer_vars[key].get():
                layer_started = time.perf_counter()
                draw[key]()
                timings.append(f"{key} {(time.perf_counter() - layer_started) * 1000:.0f}")
        self.perf_label.config(text=f"redraw {(time.perf_counter() - started) * 1000:.0f} ms "
                                    f"({', '.join(timings)}), {len(self.canvas.find_all())} canvas items")

    def _node_half_width(self, rn, node):
        """Half of the node's profile total_width(), or None if this node
        has no usable profile (missing/out-of-range profileIndex, or an
        empty profile) - callers should fall back to a bare line in that
        case rather than fabricate a width."""
        if not rn.profiles:
            return None
        idx = node.profileIndex
        if idx < 0 or idx >= len(rn.profiles):
            return None
        profile = rn.profiles[idx]
        if profile.numZones <= 0:
            return None
        return profile.total_width() / 2.0

    def _draw_road_strip_chain(self, rn, chain, canvas_pts, fill_color="#204060"):
        """Ribbon for a whole chain; see _draw_road_strip_run."""
        half_widths = [self._node_half_width(rn, rn.nodes[i]) for i in chain]
        if all(hw is None for hw in half_widths):
            return
        self._draw_road_strip_run([hw if hw is not None else 0.0 for hw in half_widths],
                                  canvas_pts, fill_color)

    def _draw_road_strip_run(self, half_widths, canvas_pts, fill_color="#204060"):
        """Fills one ribbon polygon along the whole chain, using each node's
        profile half-width offset perpendicular to a central-difference
        tangent at that node - the same idea as the centerline's smoothing,
        so the strip's edges bend through the curve instead of kinking at
        every original node like the old per-segment quads did. A missing
        profile at a node (no usable width there) is treated as 0 rather
        than breaking the whole ribbon, so it pinches rather than vanishes.

        Still an approximation: this is the full lane-envelope width (see
        RoadProfile.total_width's docstring), not lane-accurate, and the two
        long edges plus the two end caps are smoothed together as one
        polygon outline, so the ribbon's ends come out slightly rounded
        rather than perfectly flat - a cosmetic side effect of reusing
        Tkinter's built-in polygon smoothing instead of hand-rolling it."""
        n = len(canvas_pts)
        if n < 2:
            return
        left_pts, right_pts = [], []
        for i in range(n):
            if i == 0:
                tx, ty = canvas_pts[1][0] - canvas_pts[0][0], canvas_pts[1][1] - canvas_pts[0][1]
            elif i == n - 1:
                tx, ty = canvas_pts[-1][0] - canvas_pts[-2][0], canvas_pts[-1][1] - canvas_pts[-2][1]
            else:
                tx, ty = canvas_pts[i + 1][0] - canvas_pts[i - 1][0], canvas_pts[i + 1][1] - canvas_pts[i - 1][1]
            tlen = (tx * tx + ty * ty) ** 0.5
            px, py = (-ty / tlen, tx / tlen) if tlen > 1e-6 else (0.0, 0.0)
            half_c = half_widths[i] * self.scale
            cx, cy = canvas_pts[i]
            left_pts.append((cx + px * half_c, cy + py * half_c))
            right_pts.append((cx - px * half_c, cy - py * half_c))

        coords = []
        for pt in left_pts:
            coords.extend(pt)
        for pt in reversed(right_pts):
            coords.extend(pt)
        smooth = 2 < n <= 300
        self.canvas.create_polygon(coords, fill=fill_color, outline="",
                                    smooth=smooth, splinesteps=8 if smooth else 1)

    def _road_geometry(self, rn):
        """Per file and per rotation, kept between redraws: the rotated plane position of every
        node, and for every chain its points, box, half widths and colours. Without it each
        redraw rotated every node, looked up every profile and mixed every colour again."""
        deg = self.road_rotation.get()
        cache = self._road_cache
        if cache is not None and cache['rn'] is rn and cache['deg'] == deg:
            return cache
        if self._road_chains is None:
            self._road_chains = self._trace_road_chains(rn)
        if self._overlap_clusters is None:
            self._overlap_clusters = self._find_overlap_clusters(rn)
        node_xy = [self._rotate_xy(n.position[0], n.position[2]) for n in rn.nodes]
        chains = []
        for chain in self._road_chains:
            xy = [node_xy[i] for i in chain]
            xs, ys = [p[0] for p in xy], [p[1] for p in xy]
            half = [self._node_half_width(rn, rn.nodes[i]) for i in chain]
            if all(h is None for h in half):
                half = None
            else:
                half = [h if h is not None else 0.0 for h in half]
            # A chain that touches an overlap cluster takes the colour of its highest rank, so a
            # whole overpass ramp reads as elevated and not only its end dot.
            overlap_ts = [self._overlap_clusters[i][0] / (self._overlap_clusters[i][1] - 1)
                          for i in chain if i in self._overlap_clusters and self._overlap_clusters[i][1] > 1]
            if overlap_ts:
                line_color = self._lerp_color("#40a0ff", "#ff6040", max(overlap_ts))
                fill_color = self._lerp_color("#204060", "#803010", max(overlap_ts))
            else:
                line_color, fill_color = "#40c0ff", "#204060"
            chains.append((xy, (min(xs), min(ys), max(xs), max(ys)), half, line_color, fill_color))
        cache = {'rn': rn, 'deg': deg, 'node_xy': node_xy, 'chains': chains}
        self._road_cache = cache
        return cache

    def _trace_road_chains(self, rn):
        """Groups connected road segments into maximal polylines (as lists of
        node indices) so a whole chain can be drawn as one smoothed
        create_line instead of one straight create_line per segment - fewer
        canvas items (helps performance on a big network) and lets Tkinter's
        built-in spline smoothing give the network bézier-like curves without
        needing the (currently undecoded - see nfs_carp_parser.RoadSegment's
        raw endHandle/startHandle bytes) real per-segment Bezier handles.

        Chains break at any node whose degree != 2 (endpoints, intersections,
        branches) so junctions stay sharp rather than getting smoothed across.
        A second pass mops up pure loops (every node on the loop has degree
        2, so the first pass never finds a break point to start from)."""
        n = len(rn.nodes)
        adjacency = [[] for _ in range(n)]
        for si, seg in enumerate(rn.segments):
            a, b = seg.nodeStart, seg.nodeEnd
            if 0 <= a < n and 0 <= b < n:
                adjacency[a].append((b, si))
                adjacency[b].append((a, si))

        visited_segments = set()
        chains = []

        def walk(start, first_neighbor, first_seg):
            chain = [start, first_neighbor]
            visited_segments.add(first_seg)
            cur = first_neighbor
            while len(adjacency[cur]) == 2:
                nxt = next((p for p in adjacency[cur] if p[1] not in visited_segments), None)
                if nxt is None:
                    break
                nb, si = nxt
                visited_segments.add(si)
                chain.append(nb)
                cur = nb
                if cur == chain[0]:
                    break  # closed loop reached back through a branch node
            return chain

        for start in range(n):
            if len(adjacency[start]) == 2:
                continue  # only start chains at endpoints/branches, not mid-chain nodes
            for neighbor, si in adjacency[start]:
                if si not in visited_segments:
                    chains.append(walk(start, neighbor, si))

        for si, seg in enumerate(rn.segments):
            if si in visited_segments:
                continue
            a, b = seg.nodeStart, seg.nodeEnd
            if 0 <= a < n and 0 <= b < n:
                chains.append(walk(a, b, si))  # remainder must be pure loops

        return chains

    # Tuning for overlap-cluster detection - world units, not screen pixels.
    # Real segment lengths/profile widths already seen in this project run a
    # few units to a few thousand, so these are a starting guess, not
    # hex-verified - adjust if real overpasses in your files get missed or
    # if unrelated hillside nodes get flagged.
    _OVERLAP_XZ_TOLERANCE = 6.0   # how close in X/Z counts as "same spot"
    _OVERLAP_Y_MIN_GAP = 2.0      # how far apart in Y counts as "different level"

    def _find_overlap_clusters(self, rn):
        """Finds nodes that sit at nearly the same X/Z position as another
        node but at a clearly different height - i.e. one road passing over
        another (overpass/underpass/bridge deck) rather than the same road
        just climbing a grade (which moves in X/Z as it climbs, not staying
        in place). Grid-bucketed by _OVERLAP_XZ_TOLERANCE so this stays
        roughly linear instead of comparing every node against every other
        node on a big network.

        Returns {node_index: (rank, cluster_size)}, rank 0 = lowest Y in
        that cluster. This is a one-shot greedy grouping (not a full
        transitive closure across chains of nearby nodes) - good enough for
        the common 2-level overpass case this is meant to surface, not a
        rigorous clustering algorithm."""
        from collections import defaultdict
        cell = self._OVERLAP_XZ_TOLERANCE
        positions = [n.position for n in rn.nodes]
        buckets = defaultdict(list)
        for i, (x, _, z) in enumerate(positions):
            buckets[(int(x // cell), int(z // cell))].append(i)

        visited = set()
        result = {}
        for i in range(len(positions)):
            if i in visited:
                continue
            xi, yi, zi = positions[i]
            cx, cz = int(xi // cell), int(zi // cell)
            group = [i]
            for dx in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for j in buckets.get((cx + dx, cz + dz), ()):
                        if j == i or j in visited:
                            continue
                        xj, yj, zj = positions[j]
                        if ((xj - xi) ** 2 + (zj - zi) ** 2 <= cell * cell
                                and abs(yj - yi) >= self._OVERLAP_Y_MIN_GAP):
                            group.append(j)
            if len(group) > 1:
                visited.update(group)
                group.sort(key=lambda idx: positions[idx][1])
                n = len(group)
                for rank, idx in enumerate(group):
                    result[idx] = (rank, n)
        return result

    @staticmethod
    def _lerp_color(c1, c2, t):
        """Linear-interpolate two '#rrggbb' hex colors at t in [0, 1]."""
        t = max(0.0, min(1.0, t))
        r1, g1, b1 = int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16)
        r2, g2, b2 = int(c2[1:3], 16), int(c2[3:5], 16), int(c2[5:7], 16)
        r = round(r1 + (r2 - r1) * t)
        g = round(g1 + (g2 - g1) * t)
        b = round(b1 + (b2 - b1) * t)
        return f"#{r:02x}{g:02x}{b:02x}"

    def _redraw_road_network(self):
        rn = self.world.road_network
        if not rn:
            self.canvas.create_text(
                20, 20, anchor="nw", fill="#888",
                text="No road network (CarpWorldGrid) found in this file.")
            return
        geometry = self._road_geometry(rn)
        node_xy = geometry['node_xy']
        vx0, vy0, vx1, vy1 = self._visible_world_bounds()
        show_width = self.show_road_width.get()
        scale, off_x, off_y = self.scale, self.offset_x, self.offset_y

        # Per chain: only the runs of points that are in view (plus the point on each side, so a
        # line still enters and leaves the screen), and no point closer than 2 pixels to the
        # last one. The ribbon uses the same points, so it matches the centre line.
        for xy, (bx0, by0, bx1, by1), half, line_color, fill_color in geometry['chains']:
            if bx1 < vx0 or bx0 > vx1 or by1 < vy0 or by0 > vy1:
                continue  # whole chain is off-screen, skip it entirely
            count = len(xy)
            keep = [False] * count
            for i in range(count - 1):
                ax, ay = xy[i]
                bx, by = xy[i + 1]
                if min(ax, bx) <= vx1 and max(ax, bx) >= vx0 and min(ay, by) <= vy1 and max(ay, by) >= vy0:
                    keep[i] = keep[i + 1] = True
            i = 0
            while i < count:
                if not keep[i]:
                    i += 1
                    continue
                j = i
                while j < count and keep[j]:
                    j += 1
                run = list(range(i, j))   # indexes into the chain
                i = j
                if len(run) < 2:
                    continue
                picked, pts = [], []
                last_x = last_y = None
                for k in run:
                    cx = off_x + xy[k][0] * scale
                    cy = off_y - xy[k][1] * scale
                    if last_x is None or k == run[-1] or abs(cx - last_x) + abs(cy - last_y) >= 2.0:
                        picked.append(k)
                        pts.append((cx, cy))
                        last_x, last_y = cx, cy
                if len(pts) < 2:
                    continue
                if show_width and half is not None:
                    self._draw_road_strip_run([half[k] for k in picked], pts, fill_color)
                flat = [c for pt in pts for c in pt]
                if 2 < len(pts) <= 300:
                    self.canvas.create_line(*flat, fill=line_color, width=2, smooth=True, splinesteps=8)
                else:
                    self.canvas.create_line(*flat, fill=line_color, width=2)

        # Node markers: skip drawing them once there are enough on screen that individual
        # create_oval calls would dominate redraw time - the chain lines already show the
        # network shape at that point. Indices are tracked (not just positions) so the
        # selected node can be found again and always drawn, even past that cap.
        visible = [i for i, (wx, wy) in enumerate(node_xy) if vx0 <= wx <= vx1 and vy0 <= wy <= vy1]
        draw_all = len(visible) <= 2500
        for i in visible:
            overlap = self._overlap_clusters.get(i)
            if not draw_all and i != self.selected_node_index and overlap is None:
                continue  # overlap-cluster nodes stay visible past the cap too - that's the point
            cx, cy = self.world_to_canvas(*node_xy[i])
            selected = (i == self.selected_node_index)
            if selected:
                r, color = 4, "#ffe000"
            elif overlap is not None:
                rank, count = overlap
                # rank 0 (lowest in its cluster) -> cool blue, highest -> warm
                # red/orange, so at any overpass/underpass the color alone
                # says which node is on top without needing to click either.
                t = rank / (count - 1) if count > 1 else 0.0
                r, color = 3, self._lerp_color("#40a0ff", "#ff6040", t)
            else:
                r, color = 2, "#ffffff"
            self.canvas.create_oval(cx - r, cy - r, cx + r, cy + r, fill=color, outline="")

    def _letter_color(self, letter):
        """Deterministic color per section letter, purely to make overlapping/
        adjacent sections visually distinguishable when many are on screen at
        once - not a semantic categorization of any kind."""
        idx = (ord(letter) - ord('A')) % 20
        r, g, b = colorsys.hsv_to_rgb(idx / 20.0, 0.6, 0.95)
        return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"

    def _draw_decimated_line(self, run, color, width, tags=()):
        """One canvas line through the plane points of run: no point closer than 2 pixels to the last
        one is kept (the first and last always are)."""
        scale, off_x, off_y = self.scale, self.offset_x, self.offset_y
        out = []
        last_x = last_y = None
        end = len(run) - 1
        for k, (x, y) in enumerate(run):
            cx = off_x + x * scale
            cy = off_y - y * scale
            if last_x is None or k == end or abs(cx - last_x) + abs(cy - last_y) >= 2.0:
                out.append(cx)
                out.append(cy)
                last_x, last_y = cx, cy
        if len(out) >= 4:
            self.canvas.create_line(*out, fill=color, width=width, tags=tags)

    def _draw_outline(self, points, bounds, color, width=1, closed=True, tags=(), box=None):
        """Draws a polygon or polyline outline of plane points, only where it is in view.
        - box (min x, min y, max x, max y) of the outline, when the caller has it: an outline that
          lies fully in view is drawn in one pass without testing its segments.
        - Dense outlines use every Nth point, N chosen so segments stay about 2 pixels long at
          this zoom (at most 32), because shorter ones cannot be seen.
        - Otherwise a segment counts as in view when its box meets the view bounds; runs of such
          segments become one canvas line each, so a long outline that is mostly off screen costs
          about what its visible part does."""
        n = len(points)
        if n < 2:
            return
        vx0, vy0, vx1, vy1 = bounds
        spacing = self._outline_spacing.get(id(points))
        if spacing is None:
            total = 0.0
            for i in range(n - 1):
                total += abs(points[i + 1][0] - points[i][0]) + abs(points[i + 1][1] - points[i][1])
            spacing = self._outline_spacing[id(points)] = max(total / (n - 1), 1e-6)
        step = int(2.0 / (spacing * self.scale)) if spacing * self.scale < 2.0 else 1
        step = max(1, min(step, 32))
        if step > 1 and n // step >= 4:
            points = points[::step]
            n = len(points)
        if box is not None and vx0 <= box[0] and box[2] <= vx1 and vy0 <= box[1] and box[3] <= vy1:
            self._draw_decimated_line(list(points) + [points[0]] if closed else points, color, width, tags)
            return
        segments = n if closed else n - 1
        i = 0
        while i < segments:
            ax, ay = points[i]
            bx, by = points[(i + 1) % n]
            if not (min(ax, bx) <= vx1 and max(ax, bx) >= vx0 and min(ay, by) <= vy1 and max(ay, by) >= vy0):
                i += 1
                continue
            j = i + 1
            while j < segments:
                ax, ay = points[j]
                bx, by = points[(j + 1) % n]
                if not (min(ax, bx) <= vx1 and max(ax, bx) >= vx0 and min(ay, by) <= vy1 and max(ay, by) >= vy0):
                    break
                j += 1
            self._draw_decimated_line([points[k % n] for k in range(i, j + 1)], color, width, tags)
            i = j

    def _boundary_box(self, b):
        box = self._boundary_boxes.get(b.ID)
        if box is None and b.points:
            xs = [p[0] for p in b.points]
            ys = [p[1] for p in b.points]
            box = self._boundary_boxes[b.ID] = (min(xs), min(ys), max(xs), max(ys))
        return box

    def _redraw_boundaries(self):
        vx0, vy0, vx1, vy1 = self._visible_world_bounds()

        if self.show_relations.get():
            for rel in self.world.relations:
                a = self.world.by_id.get(rel.ID)
                if a is None:
                    continue
                if not (vx0 <= a.pos[0] <= vx1 and vy0 <= a.pos[1] <= vy1):
                    continue  # relation lines are only culled by their origin
                              # point - cheap, and a level-spanning relation
                              # line drawn from an off-screen far end is rare
                ax, ay = self.world_to_canvas(*a.pos)
                for other_id in rel.relatedChunkIDs:
                    b = self.world.by_id.get(other_id)
                    if b is None:
                        continue
                    bx, by = self.world_to_canvas(*b.pos)
                    self.canvas.create_line(ax, ay, bx, by, fill="#c04cff", width=1)

        show_labels = self.show_labels.get()
        bounds = (vx0, vy0, vx1, vy1)
        for b in self.world.boundaries:
            box = self._boundary_box(b)
            if box is None or not self._bbox_intersects(*box, vx0, vy0, vx1, vy1):
                continue
            if len(b.points) < 3:
                continue
            selected = (b.ID == self.selected_id)
            highlighted = (b.ID in self.highlighted_ids)
            if selected:
                outline = "#00e0ff"
            elif highlighted:
                outline = "#ff00c8"
            elif b.elevationHash:
                outline = "#ff9020"
            else:
                outline = self._letter_color(self.section_letter(b.ID))
            if self.dim_nondrivable.get() and not selected and self.world.is_drivable(b.ID) is False:
                outline = "#404040"
            width = 3 if (selected or highlighted) else 1
            self._draw_outline(b.points, bounds, outline, width, True, (f"boundary_{b.ID}",), box)
            if show_labels:
                lx, ly = self.world_to_canvas(*b.pos)
                label = f"{self.section_letter(b.ID)}{b.ID}"
                self.canvas.create_text(lx, ly, text=label, fill=outline,
                                         font=("TkDefaultFont", 8),
                                         tags=(f"boundary_{b.ID}",))

    # ---------- Troughs (TroughBoundary.bin) ----------
    _COLLISION_CELL = 128.0   # world units per grid cell of the collision index
    _COLLISION_CHUNK = 40     # wall segments per drawn chain: long outlines are cut so a view only draws what it needs

    def _build_collision_index(self):
        """Joins walls that follow each other (the end of one is the start of the next, same
        instance and group) into chains of at most _COLLISION_CHUNK segments, so one canvas
        line replaces dozens, and puts the chains and the pieces in a grid so a redraw looks
        only at the cells in view. Points are stored in the plane of the other layers,
        (z, -x) of the game coordinates."""
        cell = self._COLLISION_CELL
        walls = self.collision_walls or []
        lines = []
        state = {'coords': None, 'ids': None, 'key': None}

        def flush():
            coords = state['coords']
            if coords is not None and len(coords) >= 4:
                xs, ys = coords[0::2], coords[1::2]
                lines.append((coords, state['key'][2], state['ids'], (min(xs), min(ys), max(xs), max(ys))))

        for wall_id, w in enumerate(walls):
            ax, ay, bx, by = w[4], -w[3], w[6], -w[5]
            key = (w[0], w[1], bool(w[2]))
            coords = state['coords']
            if (coords is not None and key == state['key'] and len(state['ids']) < self._COLLISION_CHUNK
                    and abs(coords[-2] - ax) < 0.05 and abs(coords[-1] - ay) < 0.05):
                coords.extend((bx, by))
                state['ids'].append(wall_id)
            else:
                flush()
                state['coords'], state['ids'], state['key'] = [ax, ay, bx, by], [wall_id], key
        flush()

        line_grid = {}
        for i, line in enumerate(lines):
            x0, y0, x1, y1 = line[3]
            for gx in range(int(x0 // cell), int(x1 // cell) + 1):
                for gy in range(int(y0 // cell), int(y1 // cell) + 1):
                    line_grid.setdefault((gx, gy), []).append(i)
        piece_grid = {}
        for i, piece in enumerate(self.collision_pieces or []):
            x0, y0, x1, y1 = piece[7], -piece[8], piece[9], -piece[6]
            for gx in range(int(x0 // cell), int(x1 // cell) + 1):
                for gy in range(int(y0 // cell), int(y1 // cell) + 1):
                    piece_grid.setdefault((gx, gy), []).append(i)
        self._collision_lines = lines
        self._collision_line_grid = line_grid
        self._collision_piece_grid = piece_grid

    def _collision_candidates(self, grid, x0, y0, x1, y1):
        """Indexes from the grid cells that touch the world rectangle, each once."""
        cell = self._COLLISION_CELL
        gx0, gx1, gy0, gy1 = int(x0 // cell), int(x1 // cell), int(y0 // cell), int(y1 // cell)
        if (gx1 - gx0 + 1) * (gy1 - gy0 + 1) > len(grid):
            keys = [k for k in grid if gx0 <= k[0] <= gx1 and gy0 <= k[1] <= gy1]
        else:
            keys = [(gx, gy) for gx in range(gx0, gx1 + 1) for gy in range(gy0, gy1 + 1)]
        seen = set()
        for key in keys:
            for i in grid.get(key, ()):
                if i not in seen:
                    seen.add(i)
                    yield i

    def _redraw_collision(self):
        """Pieces are the boxes of articles with strips, walls the barrier edges, drawn as chains.
        Only the grid cells in view are looked at, chains smaller than a pixel are skipped, and
        points closer than 2 pixels to the last one are dropped."""
        if not self.collision_pieces and not self.collision_walls:
            return
        vx0, vy0, vx1, vy1 = self._visible_world_bounds()
        grouped_only = self.show_collision_grouped_only.get()
        scale, off_x, off_y = self.scale, self.offset_x, self.offset_y
        if self.show_collision_pieces.get():
            for i in self._collision_candidates(self._collision_piece_grid, vx0, vy0, vx1, vy1):
                piece = self.collision_pieces[i]
                if grouped_only and not piece[2]:
                    continue
                x0, y0, x1, y1 = piece[7], -piece[8], piece[9], -piece[6]
                if (x1 - x0) * scale < 2 and (y1 - y0) * scale < 2:
                    continue
                if not self._bbox_intersects(x0, y0, x1, y1, vx0, vy0, vx1, vy1):
                    continue
                self.canvas.create_rectangle(off_x + x0 * scale, off_y - y0 * scale,
                                              off_x + x1 * scale, off_y - y1 * scale,
                                              outline="#d08020")
        if self.show_collision_walls.get():
            for i in self._collision_candidates(self._collision_line_grid, vx0, vy0, vx1, vy1):
                coords, has_group, _ids, (x0, y0, x1, y1) = self._collision_lines[i]
                if grouped_only and not has_group:
                    continue
                if (x1 - x0) * scale < 1.5 and (y1 - y0) * scale < 1.5:
                    continue
                if not self._bbox_intersects(x0, y0, x1, y1, vx0, vy0, vx1, vy1):
                    continue
                out = []
                last_x = last_y = None
                count = len(coords)
                # Short segments cannot be seen: take every Nth point so they stay about 2 pixels long.
                spacing = max(x1 - x0, y1 - y0) / len(_ids)
                step = 1 if spacing * scale >= 2.0 else max(1, min(int(2.0 / max(spacing * scale, 1e-9)), len(_ids) // 2 or 1))
                indexes = list(range(0, count, 2 * step))
                if indexes[-1] != count - 2:
                    indexes.append(count - 2)
                for k in indexes:
                    cx = off_x + coords[k] * scale
                    cy = off_y - coords[k + 1] * scale
                    if last_x is None or k == count - 2 or abs(cx - last_x) + abs(cy - last_y) >= 2.0:
                        out.append(cx)
                        out.append(cy)
                        last_x, last_y = cx, cy
                if len(out) >= 4:
                    self.canvas.create_line(*out, fill="#ff4040" if has_group else "#30c0ff")
        selected = self.selected_collision
        if selected and selected[0] == 'wall':
            w = self.collision_walls[selected[1]]
            ax, ay = self.world_to_canvas(w[4], -w[3])
            bx, by = self.world_to_canvas(w[6], -w[5])
            self.canvas.create_line(ax, ay, bx, by, fill="#ffffff", width=3)
        elif selected and selected[0] == 'piece':
            p = self.collision_pieces[selected[1]]
            ax, ay = self.world_to_canvas(p[7], -p[8])
            bx, by = self.world_to_canvas(p[9], -p[6])
            self.canvas.create_rectangle(ax, ay, bx, by, outline="#ffffff", width=2)

    def _select_collision_at(self, cx, cy):
        """Picks the nearest wall within 6 pixels, else the smallest piece box under the click."""
        self.selected_collision = None
        if not self.collision_pieces and not self.collision_walls:
            self._show_hint('collision')
            return
        wx, wy = self.canvas_to_world(cx, cy)
        tolerance = 6.0 / self.scale
        grouped_only = self.show_collision_grouped_only.get()
        best = None
        if self.show_collision_walls.get():
            for i in self._collision_candidates(self._collision_line_grid, wx - tolerance, wy - tolerance,
                                                 wx + tolerance, wy + tolerance):
                coords, has_group, ids, (bx0, by0, bx1, by1) = self._collision_lines[i]
                if grouped_only and not has_group:
                    continue
                if bx0 - tolerance > wx or bx1 + tolerance < wx or by0 - tolerance > wy or by1 + tolerance < wy:
                    continue
                for k, wall_id in enumerate(ids):
                    x0, y0, x1, y1 = coords[2 * k], coords[2 * k + 1], coords[2 * k + 2], coords[2 * k + 3]
                    dx, dy = x1 - x0, y1 - y0
                    length_sq = dx * dx + dy * dy
                    t = 0.0 if length_sq == 0 else max(0.0, min(1.0, ((wx - x0) * dx + (wy - y0) * dy) / length_sq))
                    d = ((wx - (x0 + t * dx)) ** 2 + (wy - (y0 + t * dy)) ** 2) ** 0.5
                    if d <= tolerance and (best is None or d < best[0]):
                        best = (d, 'wall', wall_id)
        if best is None and self.show_collision_pieces.get():
            smallest = None
            for i in self._collision_candidates(self._collision_piece_grid, wx, wy, wx, wy):
                piece = self.collision_pieces[i]
                if grouped_only and not piece[2]:
                    continue
                x0, y0, x1, y1 = piece[7], -piece[8], piece[9], -piece[6]
                if x0 <= wx <= x1 and y0 <= wy <= y1:
                    area = (x1 - x0) * (y1 - y0)
                    if smallest is None or area < smallest[0]:
                        smallest = (area, i)
            if smallest:
                best = (0.0, 'piece', smallest[1])
        if best is None:
            self._show_hint('collision')
            return
        kind, index = best[1], best[2]
        self.selected_collision = (kind, index)
        if kind == 'wall':
            w = self.collision_walls[index]
            length = ((w[5] - w[3]) ** 2 + (w[6] - w[4]) ** 2) ** 0.5
            lines = ["Wall (barrier edge)", f"section {w[0]}, instance {w[1]}",
                     f"group number: {w[2] if w[2] else 'none'}",
                     f"from game x,z ({w[3]:.1f}, {w[4]:.1f}) to ({w[5]:.1f}, {w[6]:.1f})",
                     f"length {length:.1f}, height from {w[7]:.1f} to {w[8]:.1f}"]
        else:
            p = self.collision_pieces[index]
            lines = ["Piece (article with strips)", f"section {p[0]}, instance {p[1]}",
                     f"group number: {p[2] if p[2] else 'none'}",
                     f"strips {p[3]}, triangles {p[5]}, edges {p[4]}",
                     f"box game x {p[6]:.1f} .. {p[8]:.1f}, z {p[7]:.1f} .. {p[9]:.1f}"]
        self.info_text.config(state="normal")
        self.info_text.delete("1.0", tk.END)
        self.info_text.insert(tk.END, "\n".join(lines))
        self.info_text.config(state="disabled")

    def _redraw_troughs(self):
        if not self.troughs:
            return
        vx0, vy0, vx1, vy1 = self._visible_world_bounds()
        show_labels = self.show_trough_labels.get()
        show_holes = self.show_trough_holes.get()
        bounds = (vx0, vy0, vx1, vy1)
        for p in self.troughs.polygons:
            if p.is_hole and not show_holes:
                continue
            if not self._bbox_intersects(p.bbox_min[0], p.bbox_min[1], p.bbox_max[0], p.bbox_max[1],
                                          vx0, vy0, vx1, vy1):
                continue
            if len(p.points) < 3:
                continue
            if p.index in self.selected_troughs:
                color, width = "#ffffff", 3
            elif p.is_hole:
                color, width = "#ff6060", 1
            else:
                color, width = "#30d0a0", 1
            self._draw_outline(p.points, bounds, color, width, True, (),
                               (p.bbox_min[0], p.bbox_min[1], p.bbox_max[0], p.bbox_max[1]))
            if show_labels:
                lx, ly = self.world_to_canvas((p.bbox_min[0] + p.bbox_max[0]) / 2,
                                              (p.bbox_min[1] + p.bbox_max[1]) / 2)
                self.canvas.create_text(lx, ly, text=p.name, fill=color, font=("TkDefaultFont", 7))

    def _append_trough_info(self, cx, cy):
        """Writes the troughs under the click to the info panel. A click inside
        a hole of a trough shows that hole. Sets the highlight set before the
        caller redraws. Returns True when something was found."""
        self.selected_troughs = set()
        if not self.troughs:
            return False
        wx, wy = self.canvas_to_world(cx, cy)
        lines = []
        for p in self.troughs.polygons:
            if p.is_hole:
                continue
            if not (p.bbox_min[0] <= wx <= p.bbox_max[0] and p.bbox_min[1] <= wy <= p.bbox_max[1]):
                continue
            if not _point_in_polygon(wx, wy, p.points):
                continue
            self.selected_troughs.add(p.index)
            line = f"{p.name} (#{p.index}, {len(p.points)} points)"
            for hole in self.troughs.holes_of(p):
                if _point_in_polygon(wx, wy, hole.points):
                    self.selected_troughs.add(hole.index)
                    line += f"\n    inside hole {hole.name} (#{hole.index})"
            lines.append(line)
        self.info_text.config(state="normal")
        self.info_text.delete("1.0", tk.END)
        if lines:
            self.info_text.insert(tk.END, f"troughs here ({len(lines)}):\n" + "\n".join(lines))
        elif not self.troughs.polygons:
            self.info_text.insert(tk.END, "No trough file loaded.")
        else:
            self.info_text.insert(tk.END, "No trough at this point.")
        self.info_text.config(state="disabled")
        return bool(lines)

    # ---------- Track path zones and barriers ----------
    def _track_path_status(self):
        """Status-bar text for the track path layer. Also prints two file
        checks to the console: how many zones sit inside a boundary polygon
        (a check that zones and boundaries share one plane), and how many
        STREAMER_PREDICTION data[0]/data[1] values match a boundary ID."""
        tp = self.world.track_paths
        if not tp:
            return ", no track paths in this file"
        w = self.world
        inside = 0
        for z in tp.zones:
            x, y = z.position
            for b in w.boundaries:
                if (b.boundsMin[0] <= x <= b.boundsMax[0] and b.boundsMin[1] <= y <= b.boundsMax[1]
                        and _point_in_polygon(x, y, b.points)):
                    inside += 1
                    break
        streamers = [z for z in tp.zones if z.type == STREAMER_PREDICTION]
        slots = [v for z in streamers for v in z.data[:2] if v]
        matched = sum(1 for v in slots if v in w.by_id)
        tail_zero = sum(1 for z in streamers if z.data[2] == 0 and z.data[3] == 0)
        print(f"[track paths] {len(tp.zones)} zones, {len(tp.barriers)} barriers; "
              f"{inside}/{len(tp.zones)} zone positions inside a boundary")
        print(f"[track paths] STREAMER_PREDICTION: {len(streamers)} zones, "
              f"{matched}/{len(slots)} data[0..1] values match a boundary ID, "
              f"{tail_zero}/{len(streamers)} have data[2]==data[3]==0")
        return (f", track paths: {len(tp.zones)} zones ({inside} inside a boundary)"
                f"/{len(tp.barriers)} barriers")

    def _zone_color(self, type_id):
        hue = (type_id % 15) / 15.0
        r, g, b = colorsys.hsv_to_rgb(hue, 0.85, 1.0)
        return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"

    def _redraw_track_paths(self):
        tp = self.world.track_paths
        if not tp:
            return
        vx0, vy0, vx1, vy1 = self._visible_world_bounds()
        show_labels = self.show_zone_labels.get()

        if self.show_zones.get():
            for z in tp.zones:
                if z.type not in self.visible_zone_types:
                    continue
                if not self._bbox_intersects(z.bbox_min[0], z.bbox_min[1], z.bbox_max[0], z.bbox_max[1],
                                              vx0, vy0, vx1, vy1):
                    continue
                selected = z.offset in self.selected_zones
                color = "#ffffff" if selected else self._zone_color(z.type)
                width = 3 if selected else 2
                if z.num_points >= 3:
                    self._draw_outline(z.points, (vx0, vy0, vx1, vy1), color, width, True)
                elif z.num_points == 2:
                    a = self.world_to_canvas(*z.points[0])
                    b = self.world_to_canvas(*z.points[1])
                    self.canvas.create_line(*a, *b, fill=color, width=width)
                else:
                    cx, cy = self.world_to_canvas(*z.position)
                    self.canvas.create_oval(cx - 4, cy - 4, cx + 4, cy + 4, outline=color, width=width)
                if show_labels:
                    lx, ly = self.world_to_canvas(*z.position)
                    self.canvas.create_text(lx, ly, text=z.type_name, fill=color,
                                             font=("TkDefaultFont", 7))

        if self.show_barriers.get():
            for br in tp.barriers:
                if not self._bbox_intersects(min(br.p0[0], br.p1[0]), min(br.p0[1], br.p1[1]),
                                              max(br.p0[0], br.p1[0]), max(br.p0[1], br.p1[1]),
                                              vx0, vy0, vx1, vy1):
                    continue
                a = self.world_to_canvas(*br.p0)
                b = self.world_to_canvas(*br.p1)
                if br.offset in self.selected_barriers:
                    color, width = "#ffffff", 4
                elif br.group_hash:
                    color, width = "#ff5050", 2      # group hash set: event-gated barrier
                else:
                    color, width = "#a0a0a0", 2      # static barrier
                dash = () if br.enabled else (4, 3)  # dashed: disabled in the file
                self.canvas.create_line(*a, *b, fill=color, width=width, dash=dash)

    def _distance_to_segment_px(self, px, py, a, b):
        ax, ay = a
        bx, by = b
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy
        t = 0.0 if length2 < 1e-9 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
        qx, qy = ax + t * dx, ay + t * dy
        return ((px - qx) ** 2 + (py - qy) ** 2) ** 0.5

    def _zone_info_lines(self, z):
        lines = [
            f"zone @0x{z.offset:X}: {z.type_name} ({z.type})",
            f"    elevation: {z.elevation:.2f}",
            f"    position: ({z.position[0]:.2f}, {z.position[1]:.2f})  "
            f"direction: ({z.direction[0]:.2f}, {z.direction[1]:.2f})",
            f"    bbox: ({z.bbox_min[0]:.1f}, {z.bbox_min[1]:.1f}) .. ({z.bbox_max[0]:.1f}, {z.bbox_max[1]:.1f})",
            f"    zone_source={z.zone_source} cached_index={z.cached_index} visit_info={z.visit_info}",
            f"    data: {list(z.data)}  points: {z.num_points}",
        ]
        if z.type == STREAMER_PREDICTION:
            for i in (0, 1):
                v = z.data[i]
                if not v:
                    continue
                note = "" if v in self.world.by_id else "  [not in this file]"
                lines.append(f"    data[{i}] = section {self.format_section_label(v)}{note}")
        return lines

    def _select_zone_at(self, cx, cy):
        """Click handler for the Zones mode. Shows the zones under the click
        and the barriers near it."""
        self.info_text.config(state="normal")
        self.info_text.delete("1.0", tk.END)
        self.info_text.config(state="disabled")
        if not self._append_track_path_info(cx, cy, standalone=True):
            self.info_text.config(state="normal")
            self.info_text.insert(tk.END, "No zone or barrier at this point." if self.world.track_paths
                                  else "No track path data in this file.")
            self.info_text.config(state="disabled")
        self.redraw()

    def _append_track_path_info(self, cx, cy, standalone=False):
        """Appends the zones under the click and the barriers near it to the
        info panel, below the section info. Sets the highlight sets before
        the caller redraws."""
        tp = self.world.track_paths
        if not tp:
            return False
        wx, wy = self.canvas_to_world(cx, cy)
        lines = []
        if self.show_zones.get():
            hit_zones = [z for z in tp.zones if z.type in self.visible_zone_types and z.contains(wx, wy)]
            self.selected_zones = {z.offset for z in hit_zones}
            if hit_zones:
                lines.append("")
                lines.append(f"track path zones here ({len(hit_zones)}):")
                for z in hit_zones:
                    lines.extend(self._zone_info_lines(z))
        if self.show_barriers.get():
            hit_barriers = []
            for i, br in enumerate(tp.barriers):
                a = self.world_to_canvas(*br.p0)
                b = self.world_to_canvas(*br.p1)
                if self._distance_to_segment_px(cx, cy, a, b) <= 6:
                    hit_barriers.append((i, br))
            self.selected_barriers = {br.offset for _, br in hit_barriers}
            if hit_barriers:
                lines.append("")
                lines.append(f"track path barriers near click ({len(hit_barriers)}):")
                for i, br in hit_barriers[:10]:
                    name = br.group_name()
                    group = "none (static)" if not br.group_hash else \
                        f"0x{br.group_hash:08X}" + (f" ({name})" if name else "")
                    lines.append(f"barrier #{i} @0x{br.offset:X}")
                    lines.append(f"    enabled={br.enabled} player_barrier={br.player_barrier} "
                                 f"left_handed={br.left_handed}")
                    lines.append(f"    group: {group}")
                if len(hit_barriers) > 10:
                    lines.append(f"...and {len(hit_barriers) - 10} more")
        if lines:
            text = "\n".join(lines)
            self.info_text.config(state="normal")
            self.info_text.insert(tk.END, text.lstrip("\n") if standalone else "\n" + text)
            self.info_text.config(state="disabled")
        return bool(lines)

    def open_zone_types_dialog(self):
        if not self.world or not self.world.track_paths:
            messagebox.showinfo("Zone types", "No track path zones in the loaded file.")
            return
        counts = {}
        for z in self.world.track_paths.zones:
            counts[z.type] = counts.get(z.type, 0) + 1
        win = tk.Toplevel(self)
        win.title("Zone types")
        variables = {}

        def apply():
            self.visible_zone_types = {t for t, v in variables.items() if v.get()}
            self.redraw()

        def set_all(value):
            for v in variables.values():
                v.set(value)
            apply()

        for type_id in sorted(set(ZONE_TYPES) | set(counts)):
            name = ZONE_TYPES.get(type_id, f"UNKNOWN_{type_id}")
            var = tk.BooleanVar(value=type_id in self.visible_zone_types)
            variables[type_id] = var
            tk.Checkbutton(win, text=f"{name} ({counts.get(type_id, 0)})", variable=var,
                            fg=self._zone_color(type_id), command=apply).pack(anchor="w", padx=8)
        row = tk.Frame(win)
        row.pack(fill=tk.X, padx=8, pady=6)
        tk.Button(row, text="All", command=lambda: set_all(True)).pack(side=tk.LEFT, padx=2)
        tk.Button(row, text="None", command=lambda: set_all(False)).pack(side=tk.LEFT, padx=2)

    # ---------- Scenery search ----------
    def _search_scenery(self):
        if self.stream_scenery is None:
            messagebox.showinfo("Search", "Open a stream file first - there's no scenery data loaded yet.")
            return
        needle = self.search_var.get().strip().lower()
        if not needle:
            return

        hits = []
        for section_number, section in self.stream_scenery.sections.items():
            for inst in section.instances:
                name = section.name_for(inst)
                if name and needle in name.lower():
                    hits.append((section_number, inst.instance_number, name))
        hits.sort()

        self.info_text.config(state="normal")
        self.info_text.delete("1.0", tk.END)
        if not hits:
            self.info_text.insert(tk.END, f"No scenery instance names containing {needle!r} found.")
        else:
            # One entry per section, not per instance (a section can have many
            # matching instances with the same name) - and split into two
            # groups, since the two mean very different things: "in this
            # file" you can click and jump to; "not in this file" means that
            # section's geometry/instance data is real, it's just in a
            # different file (region file vs stream file, or a neighboring
            # region file) than whatever's currently loaded.
            by_section = {}
            for section_number, instance_number, name in hits:
                by_section.setdefault(section_number, name)  # first name seen per section

            in_file = sorted(s for s in by_section if self.world is not None and s in self.world.by_id)
            not_in_file = sorted(s for s in by_section if s not in in_file)

            self.info_text.insert(tk.END, f"{len(hits)} instance match(es) in {len(by_section)} "
                                           f"section(s) for {needle!r}\n\n")

            if in_file:
                self.info_text.insert(tk.END, f"In this file - click to jump ({len(in_file)}):\n")
                for section_number in in_file:
                    label = self.format_section_label(section_number)
                    tag = f"search_{section_number}"
                    self.info_text.insert(tk.END, f"  {label}: {by_section[section_number]}\n", (tag,))
                    self.info_text.tag_configure(tag, foreground="#40c0ff", underline=True)
                    self.info_text.tag_bind(tag, "<Button-1>",
                                             lambda e, sn=section_number: self._jump_to_section(sn))
                self.info_text.insert(tk.END, "\n")

            if not_in_file:
                self.info_text.insert(
                    tk.END,
                    f"Not in this file ({len(not_in_file)}) - real sections, just not loaded here "
                    f"(different region/stream file):\n")
                shown_cap = 30
                for section_number in not_in_file[:shown_cap]:
                    label = self.format_section_label(section_number) if self.world else str(section_number)
                    self.info_text.insert(tk.END, f"  {label}: {by_section[section_number]}\n")
                if len(not_in_file) > shown_cap:
                    self.info_text.insert(tk.END, f"  ...and {len(not_in_file) - shown_cap} more\n")
        self.info_text.config(state="disabled")

    def _jump_to_section(self, section_number):
        b = self.world.by_id.get(section_number) if self.world else None
        if b is None:
            return
        self.selected_id = section_number
        self.redraw()
        self._show_info(b)

    def open_group_view(self):
        if self.stream_scenery is None:
            messagebox.showinfo("Groups", "Open a stream/region file first - no group data loaded yet.")
            return

        win = tk.Toplevel(self)
        win.title(f"Groups ({len(self.stream_scenery.groups)})")
        win.geometry("460x520")

        columns = ("key", "overrides", "sections")
        tree = ttk.Treeview(win, columns=columns, show="headings", height=10)
        tree.heading("key", text="Key")
        tree.heading("overrides", text="Overrides")
        tree.heading("sections", text="Sections")
        tree.column("key", width=220)
        tree.column("overrides", width=80, anchor=tk.CENTER)
        tree.column("sections", width=80, anchor=tk.CENTER)
        tree.pack(side=tk.TOP, fill=tk.X)
        vsb = ttk.Scrollbar(win, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        overrides = self.stream_scenery.overrides
        group_sections = {}
        for g in self.stream_scenery.groups:
            group_sections[id(g)] = sorted({overrides[idx].section_number
                                             for idx in g.override_indices if idx < len(overrides)})

        # Most-spread-out groups first - the ones actually worth looking at
        # for a "how does this pattern span the world" question.
        groups_sorted = sorted(self.stream_scenery.groups, key=lambda g: len(group_sections[id(g)]), reverse=True)
        for i, g in enumerate(groups_sorted):
            secs = group_sections[id(g)]
            tree.insert("", tk.END, iid=str(i), values=(g.display_key(), len(g.override_indices), len(secs)))

        detail = tk.Text(win, wrap="word", state="disabled")
        detail.pack(side=tk.BOTTOM, fill=tk.BOTH, expand=True)

        def on_select(_event):
            sel = tree.selection()
            if not sel:
                return
            g = groups_sorted[int(sel[0])]
            self.highlighted_ids = set(group_sections[id(g)])
            self.redraw()

            # Group this group's own overrides by section, so each section
            # heads its own block of the actual objects in it - not just a
            # flat list of section numbers.
            by_section = {}
            for idx in g.override_indices:
                if idx < len(overrides):
                    by_section.setdefault(overrides[idx].section_number, []).append(overrides[idx])

            detail.config(state="normal")
            detail.delete("1.0", tk.END)
            detail.insert(tk.END, f"{g.display_key()} - {len(by_section)} section(s), "
                                   f"{len(g.override_indices)} override(s)\n\n")

            for section_number in sorted(by_section):
                has_boundary = self.world is not None and section_number in self.world.by_id
                if self.world is not None:
                    short = f"{self.section_letter(section_number)}{section_subsection(section_number)}"
                else:
                    short = str(section_number)
                header = f"{short} ({section_number})"

                if has_boundary:
                    tag = f"grouptree_{section_number}"
                    detail.insert(tk.END, header + "\n", (tag,))
                    detail.tag_configure(tag, foreground="#40c0ff", underline=True)
                    detail.tag_bind(tag, "<Button-1>", lambda e, sn=section_number: self._jump_to_section(sn))
                else:
                    detail.insert(tk.END, header + "  [no boundary in this file]\n")

                stream_section = self.stream_scenery.sections.get(section_number)
                for ov in by_section[section_number]:
                    name = None
                    if stream_section is not None and 0 <= ov.instance_number < len(stream_section.instances):
                        name = stream_section.name_for(stream_section.instances[ov.instance_number])
                    object_label = name or f"instance #{ov.instance_number} (no instance data loaded)"
                    detail.insert(tk.END, f"  {object_label}\n")
                    detail.insert(tk.END, f"    Group membership: {g.display_key()}\n")
                detail.insert(tk.END, "\n")

            detail.config(state="disabled")

        tree.bind("<<TreeviewSelect>>", on_select)

        def on_close():
            self.highlighted_ids = set()
            self.redraw()
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", on_close)

    # ---------- Interaction ----------
    def _on_press(self, event):
        self._drag_start = (event.x, event.y)
        self._dragged = False

    def _on_drag(self, event):
        if self._drag_start is None:
            return
        dx = event.x - self._drag_start[0]
        dy = event.y - self._drag_start[1]
        if abs(dx) > 2 or abs(dy) > 2:
            self._dragged = True
        self.offset_x += dx
        self.offset_y += dy
        self._drag_start = (event.x, event.y)
        # Sliding existing canvas items is far cheaper than deleting and
        # recreating everything per mouse-move on a big level - a real
        # redraw() (which also re-culls to the new viewport) is scheduled
        # after interaction settles instead of on every event.
        self.canvas.move("all", dx, dy)
        self._schedule_redraw()

    def _on_release(self, event):
        if not self._dragged:
            self._select_at(event.x, event.y)
        self._drag_start = None

    def _on_wheel(self, event):
        factor = 1.15 if event.delta > 0 else 1 / 1.15
        self._zoom(event, factor)

    def _zoom(self, event, factor):
        if not self.world:
            return
        self.scale *= factor
        # Rescale existing items about the cursor instead of recomputing
        # every point - keeps the point under the cursor fixed exactly like
        # the old per-item recompute did, just without rebuilding anything.
        self.canvas.scale("all", event.x, event.y, factor, factor)
        self.offset_x = event.x + (self.offset_x - event.x) * factor
        self.offset_y = event.y + (self.offset_y - event.y) * factor
        self._schedule_redraw()

    def _schedule_redraw(self, delay_ms=150):
        """Debounced full redraw: move()/scale() keep the view visually
        correct instantly, but only a real redraw() re-culls to the new
        viewport and restores fixed-pixel details (node dot radius, line
        width) that canvas.scale() would otherwise drift away from over
        repeated zooms. Runs once interaction pauses, not on every event."""
        if self._redraw_after_id is not None:
            self.after_cancel(self._redraw_after_id)
        self._redraw_after_id = self.after(delay_ms, self._debounced_redraw)

    def _debounced_redraw(self):
        self._redraw_after_id = None
        self.redraw()

    def _select_at(self, cx, cy):
        """A click only picks from the active mode's layer."""
        if not self.world:
            return
        mode = self.active_mode.get()
        self.selected_zones = set()
        self.selected_barriers = set()
        self.selected_troughs = set()
        if mode == 'nodes':
            node_idx = self._node_at_canvas(cx, cy)
            self.selected_node_index = node_idx
            self.selected_id = None
            if node_idx is not None:
                self._show_node_info(node_idx)
            else:
                self._show_hint('nodes')
        elif mode == 'zones':
            self.selected_id = None
            self.selected_node_index = None
            self._select_zone_at(cx, cy)
            return   # _select_zone_at redraws
        elif mode == 'troughs':
            self.selected_id = None
            self.selected_node_index = None
            self._append_trough_info(cx, cy)
        elif mode == 'collision':
            self.selected_id = None
            self.selected_node_index = None
            self._select_collision_at(cx, cy)
        else:
            wx, wy = self.canvas_to_world(cx, cy)
            hit = None
            for b in self.world.boundaries:
                if _point_in_polygon(wx, wy, b.points):
                    hit = b
                    break
            self.selected_node_index = None
            self.selected_id = hit.ID if hit else None
            self._show_info(hit)
        self.redraw()

    def _node_at_canvas(self, cx, cy, tolerance=8):
        """Nearest road node within `tolerance` screen pixels of a click, or
        None. A pixel tolerance (not a world-space one) so clicking a node
        stays about as forgiving at any zoom level."""
        rn = self.world.road_network
        if not rn or not rn.nodes:
            return None
        best_idx, best_dist2 = None, tolerance * tolerance
        for i, n in enumerate(rn.nodes):
            nx, ny = self.world_to_canvas(*self._rotate_xy(n.position[0], n.position[2]))
            d2 = (nx - cx) ** 2 + (ny - cy) ** 2
            if d2 <= best_dist2:
                best_idx, best_dist2 = i, d2
        return best_idx

    def _show_node_info(self, node_index):
        rn = self.world.road_network
        node = rn.nodes[node_index]
        if self._overlap_clusters is None:  # in case this fires before a redraw ever computed it
            self._overlap_clusters = self._find_overlap_clusters(rn)
        lines = [
            f"Road node #{node_index}  (index field: {node.index})",
            f"position: ({node.position[0]:.2f}, {node.position[1]:.2f}, {node.position[2]:.2f})",
            f"profileIndex: {node.profileIndex}",
            f"numSegments: {node.numSegments}",
        ]
        overlap = self._overlap_clusters.get(node_index)
        if overlap is not None:
            rank, count = overlap
            level = "lowest" if rank == 0 else "highest" if rank == count - 1 else f"level {rank}"
            lines.append(f"overlaps {count - 1} other node(s) at nearly the same X/Z, "
                         f"different height ({level} of {count})")
        connected = [(si, seg) for si, seg in enumerate(rn.segments)
                     if seg.nodeStart == node_index or seg.nodeEnd == node_index]
        lines.append("")
        lines.append(f"connected segments ({len(connected)}):")
        if connected:
            for si, seg in connected:
                other = seg.nodeEnd if seg.nodeStart == node_index else seg.nodeStart
                lines.append(f"    segment #{si} -> node #{other}  (length={seg.length})")
                if 0 <= seg.roadID < len(rn.roads):
                    road = rn.roads[seg.roadID]
                    lines.append(f"        road #{seg.roadID}: scale={road.scale}  "
                                 f"length={road.length}  shortcut={road.shortcut}  "
                                 f"minWidth={road.minWidth}  speechID={road.speechID}")
                elif not rn.roads:
                    lines.append(f"        road #{seg.roadID}: no RNrd data loaded for this file")
                else:
                    lines.append(f"        road #{seg.roadID}: out of range (0..{len(rn.roads) - 1})")
        else:
            lines.append("    (none)")

        lines.append("")
        idx = node.profileIndex
        if not rn.profiles:
            lines.append("profile: no RNpf data loaded for this file")
        elif idx < 0 or idx >= len(rn.profiles):
            lines.append(f"profile: profileIndex {idx} out of range (0..{len(rn.profiles) - 1})")
        else:
            profile = rn.profiles[idx]
            lines.append(f"profile #{idx}: numZones={profile.numZones}  "
                         f"middleZone={profile.middleZone}  "
                         f"total_width={profile.total_width():.2f}")
            lines.append("lanes:")
            for i in range(profile.numZones):
                lines.append(f"    [{i}] type={profile.lane_type(i)}  "
                             f"width={profile.lane_width(i):.2f}  "
                             f"offset={profile.lane_offset(i):.2f}")

        self.info_text.config(state="normal")
        self.info_text.delete("1.0", tk.END)
        self.info_text.insert(tk.END, "\n".join(lines))
        self.info_text.config(state="disabled")

    def export_sections_json(self):
        """Writes sections.json for the runtime streamer: drivable-section
        boundary polygons plus each drivable section's related-section list.
        Only drivable boundaries are ever point-in-polygon tested at runtime
        (per the real NFSMW VisibleSectionManager::FindBoundary/
        FindClosestBoundary, confirmed against decomp source) - non-drivable
        sections are pulled in only via a drivable section's related list,
        never tested directly. A related ID is kept if it has a boundary OR
        stream data (a section with stream data but no boundary is real and
        must stay); only IDs with neither are dropped as non-existent."""
        if self.world is None:
            messagebox.showerror("Export sections.json", "No region file loaded.")
            return
        if self.stream_scenery is None:
            messagebox.showerror(
                "Export sections.json",
                "Load a stream file first (Open stream file...) - the export "
                "needs it to confirm which related section IDs actually have "
                "real data before they're written out.")
            return

        sections, boundaryless_kept = build_sections(self.world, self.stream_scenery)

        if not sections:
            messagebox.showwarning("Export sections.json", "No drivable sections found - nothing to export.")
            return

        path = filedialog.asksaveasfilename(
            title="Export sections.json", defaultextension=".json",
            filetypes=[("JSON", "*.json")])
        if not path:
            return
        write_sections_json(sections, path)
        messagebox.showinfo(
            "Export sections.json",
            f"Wrote {len(sections)} drivable sections to {path}\n"
            f"{len(boundaryless_kept)} related sections have stream data but no boundary (kept).")

    # Section info categories that start collapsed (long lists). Every other
    # category starts open. The choice is kept between clicks in self.info_open.
    INFO_COLLAPSED_BY_DEFAULT = ('scenery', 'overrides')

    def _insert_category(self, key, title, body_lines):
        """Adds a collapsible category to the info panel: a header line that
        toggles its body when clicked. Call with the Text in normal state."""
        is_open = self.info_open.setdefault(key, key not in self.INFO_COLLAPSED_BY_DEFAULT)
        header_tag, body_tag = f"hdr_{key}", f"body_{key}"
        arrow = "\u25bc" if is_open else "\u25b6"
        self.info_text.insert(tk.END, f"{arrow} {title}\n", (header_tag, "category"))
        self.info_text.insert(tk.END, "\n".join(f"    {line}" for line in body_lines) + "\n\n", (body_tag,))
        self.info_text.tag_configure(body_tag, elide=not is_open)
        self.info_text.tag_bind(header_tag, "<Button-1>", lambda e, k=key: self._toggle_category(k))
        self.info_text.tag_bind(header_tag, "<Enter>", lambda e: self.info_text.config(cursor="hand2"))
        self.info_text.tag_bind(header_tag, "<Leave>", lambda e: self.info_text.config(cursor=""))

    def _toggle_category(self, key):
        is_open = not self.info_open.get(key, True)
        self.info_open[key] = is_open
        header_tag = f"hdr_{key}"
        self.info_text.config(state="normal")
        self.info_text.tag_configure(f"body_{key}", elide=not is_open)
        ranges = self.info_text.tag_ranges(header_tag)
        if ranges:
            start = ranges[0]
            self.info_text.replace(start, f"{start}+1c", "\u25bc" if is_open else "\u25b6",
                                   (header_tag, "category"))
        self.info_text.config(state="disabled")
        return "break"

    def _show_info(self, b):
        self.info_text.config(state="normal")
        self.info_text.delete("1.0", tk.END)
        if b is None:
            self.info_text.insert(tk.END, "Click a section to inspect it.")
        else:
            rel = self.world.relations_by_id.get(b.ID)
            visible_ids = getattr(rel, 'visible_related_chunk_ids', rel.relatedChunkIDs if rel else [])
            stale_ids = getattr(rel, 'stale_related_chunk_ids', [])
            drivable = self.world.is_drivable(b.ID)
            drivable_str = {True: "yes", False: "no", None: "unknown"}[drivable]
            lines = [
                f"ID: {b.ID}  {self.format_section_label(b.ID)}",
                f"drivable: {drivable_str}",
                f"ID_over (toggle pair): {b.ID_over}",
                f"type: {b.type}",
                f"type1/type2: {b.type1}/{b.type2}",
                f"elevationHash: 0x{b.elevationHash:08X}",
                f"unk2: {b.unk2}   unk3: {b.unk3}",
                f"pos: ({b.pos[0]:.2f}, {b.pos[1]:.2f})",
                f"boundsMin: ({b.boundsMin[0]:.2f}, {b.boundsMin[1]:.2f})",
                f"boundsMax: ({b.boundsMax[0]:.2f}, {b.boundsMax[1]:.2f})",
            ]
            self.info_text.insert(tk.END, "\n".join(lines) + "\n\n")

            self._insert_category('points', f"Points ({b.numPoints})",
                                  [f"({p[0]:.2f}, {p[1]:.2f})" for p in b.points])

            def missing_reasons(i):
                reasons = []
                if i not in self.world.by_id:
                    reasons.append("no boundary")
                if self.stream_scenery is not None and not self.stream_scenery.has_data_for(i):
                    reasons.append("no stream data")
                return reasons

            present_visible = [i for i in visible_ids if not missing_reasons(i)]
            missing_visible = [i for i in visible_ids if missing_reasons(i)]
            present_stale = [i for i in stale_ids if not missing_reasons(i)]
            missing_stale = [i for i in stale_ids if missing_reasons(i)]

            self._insert_category('related', f"Related sections ({len(present_visible)})",
                                  [self.format_section_label(i) for i in present_visible] or ["(none)"])

            if stale_ids:
                self._insert_category(
                    'stale', f"Possibly stale ({len(present_stale)})",
                    ["unk1 < dataCount - untested hypothesis, see nfs_region_prostreet.py"]
                    + ([self.format_section_label(i) for i in present_stale] or ["(none)"]))

            not_in_file = [(i, False) for i in missing_visible] + [(i, True) for i in missing_stale]
            if not_in_file:
                body = []
                for i, is_stale in not_in_file:
                    reasons = ", ".join(missing_reasons(i))
                    stale_tag = ", stale" if is_stale else ""
                    body.append(f"{self.format_section_label(i)} - {reasons}{stale_tag}")
                self._insert_category('missing', f"Not in this file ({len(not_in_file)})", body)

            if self.stream_scenery is not None:
                stream_section = self.stream_scenery.sections.get(b.ID)
                if stream_section is not None:
                    body = []
                    for inst in stream_section.instances:
                        groups = self.stream_scenery.groups_for(b.ID, inst.instance_number)
                        group_str = (", groups: " + ", ".join(g.display_key() for g in groups)) if groups else ""
                        name = stream_section.name_for(inst)
                        name_str = f" ({name})" if name else ""
                        body.append(f"#{inst.instance_number} guid=0x{inst.scenery_guid:08X}{name_str}{group_str}")
                    self._insert_category('scenery', f"Scenery instances, stream file ({len(stream_section.instances)})",
                                          body or ["(none)"])
                else:
                    self._insert_category('scenery', "Scenery instances, stream file",
                                          ["no instance data for this section"])

                section_overrides = self.stream_scenery.overrides_for_section(b.ID)
                if section_overrides:
                    body = []
                    for _idx, ov, groups in section_overrides:
                        group_str = (", groups: " + ", ".join(g.display_key() for g in groups)) if groups else ""
                        body.append(f"instance #{ov.instance_number}: flags=0x{ov.instance_flags:04X}{group_str}")
                    self._insert_category('overrides', f"Override records ({len(section_overrides)})", body)
        self.info_text.config(state="disabled")


def _point_in_polygon(x, y, points):
    """Standard ray-casting point-in-polygon test."""
    inside = False
    n = len(points)
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        if ((y1 > y) != (y2 > y)) and (x < (x2 - x1) * (y - y1) / (y2 - y1 + 1e-12) + x1):
            inside = not inside
    return inside


if __name__ == '__main__':
    path = sys.argv[1] if len(sys.argv) > 1 else None
    game = sys.argv[2] if len(sys.argv) > 2 else 'None'
    app = RegionViewerApp(path, game)
    app.mainloop()
