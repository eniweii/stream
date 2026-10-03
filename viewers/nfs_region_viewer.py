"""
Small Tkinter viewer for Black Box NFS region files, across games.

Shows VisibleSections boundary polygons (yellow, or orange if they carry a
non-zero elevationHash) labeled with their letter+number section ID (e.g.
"A101"), and their adjacency relations (magenta lines between section
centers) when the selected game's relations parser succeeds. Click a polygon
to inspect its raw fields, including related-section labels.

Pick the game from the dropdown before opening a file - see
nfs_region_parser.GAME_PARSERS for what's verified vs. still a hypothesis
per game.

Usage: python3 nfs_region_viewer.py [path/to/file.BUN] [game]
(If no path is given, a file-open dialog appears. game defaults to prostreet.)
"""
# stream bootstrap: make the shared library folders importable
import pathlib as _pl, sys as _sys
_sys.path[:0] = [str(_pl.Path(__file__).resolve().parents[1] / _d) for _d in ['common', 'region', 'scenery', 'solids_materials']]

import sys
import json
import colorsys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from nfs_region_parser import load_region_file, GAME_PARSERS, get_label_functions
from export_sections import build_sections, write_sections_json
from nfs_region_common import section_letter as _default_section_letter
from nfs_region_common import format_section_label as _default_format_section_label
from nfs_region_common import section_subsection
from nfs_trackpath import ZONE_TYPES, STREAMER_PREDICTION


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
        self.view_mode = tk.StringVar(value='boundaries')
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
        self.selected_node_index = None
        self._overlap_clusters = None  # cache: {node_index: (rank, cluster_size)}, invalidated on file load
        self.show_zones = tk.BooleanVar(value=True)
        self.show_barriers = tk.BooleanVar(value=True)
        self.show_zone_labels = tk.BooleanVar(value=True)
        self.zone_select_mode = tk.BooleanVar(value=False)
        self.visible_zone_types = set(ZONE_TYPES)
        self.selected_zones = set()      # zone.offset values under the last click
        self.selected_barriers = set()   # barrier.offset values near the last click

        self._build_ui()

        if path:
            self.load_file(path)

    # ---------- UI ----------
    def _build_ui(self):
        toolbar = tk.Frame(self)
        toolbar.pack(side=tk.TOP, fill=tk.X)

        tk.Button(toolbar, text="Open...", command=self.open_dialog).pack(side=tk.LEFT, padx=4, pady=4)
        tk.Button(toolbar, text="Open stream file...", command=self.open_stream_dialog).pack(side=tk.LEFT, padx=4, pady=4)
        tk.Button(toolbar, text="Clear stream data", command=self.clear_stream_data).pack(side=tk.LEFT, padx=4, pady=4)
        tk.Button(toolbar, text="Groups...", command=self.open_group_view).pack(side=tk.LEFT, padx=4, pady=4)
        tk.Button(toolbar, text="Export sections.json...", command=self.export_sections_json).pack(side=tk.LEFT, padx=4, pady=4)
        tk.Button(toolbar, text="Fit to view", command=self.fit_to_view).pack(side=tk.LEFT, padx=4, pady=4)

        tk.Label(toolbar, text="View:").pack(side=tk.LEFT, padx=(12, 2))
        view_combo = ttk.Combobox(toolbar, textvariable=self.view_mode, state="readonly",
                                   width=14, values=['boundaries', 'road network', 'combined'])
        view_combo.pack(side=tk.LEFT, padx=(0, 8))
        view_combo.bind("<<ComboboxSelected>>", lambda e: self.fit_to_view())

        tk.Label(toolbar, text="Rotate:").pack(side=tk.LEFT, padx=(12, 2))
        rotate_combo = ttk.Combobox(toolbar, textvariable=self.road_rotation, state="readonly",
                                     width=4, values=['0', '90', '180', '270'])
        rotate_combo.pack(side=tk.LEFT, padx=(0, 8))
        rotate_combo.bind("<<ComboboxSelected>>", lambda e: self.fit_to_view())

        tk.Label(toolbar, text="Game:").pack(side=tk.LEFT, padx=(12, 2))
        game_combo = ttk.Combobox(toolbar, textvariable=self.game, state="readonly",
                                   width=12, values=sorted(GAME_PARSERS.keys()))
        game_combo.pack(side=tk.LEFT, padx=(0, 8))
        game_combo.bind("<<ComboboxSelected>>", self._on_game_change)

        tk.Checkbutton(toolbar, text="Show relations", variable=self.show_relations,
                        command=self.redraw).pack(side=tk.LEFT, padx=8)
        tk.Checkbutton(toolbar, text="Section labels", variable=self.show_labels,
                        command=self.redraw).pack(side=tk.LEFT, padx=8)
        tk.Checkbutton(toolbar, text="Dim non-drivable", variable=self.dim_nondrivable,
                        command=self.redraw).pack(side=tk.LEFT, padx=8)
        tk.Checkbutton(toolbar, text="Show road width", variable=self.show_road_width,
                        command=self.redraw).pack(side=tk.LEFT, padx=8)
        search_bar = tk.Frame(self)
        search_bar.pack(side=tk.TOP, fill=tk.X)
        tk.Label(search_bar, text="Search scenery names:").pack(side=tk.LEFT, padx=(4, 2), pady=2)
        search_entry = tk.Entry(search_bar, textvariable=self.search_var, width=30)
        search_entry.pack(side=tk.LEFT, padx=(0, 4))
        search_entry.bind("<Return>", lambda e: self._search_scenery())
        tk.Button(search_bar, text="Find", command=self._search_scenery).pack(side=tk.LEFT)

        tk.Label(search_bar, text="   Guess group name:").pack(side=tk.LEFT, padx=(8, 2))
        self.group_guess_var = tk.StringVar()
        guess_entry = tk.Entry(search_bar, textvariable=self.group_guess_var, width=24)
        guess_entry.pack(side=tk.LEFT)
        guess_entry.bind("<Return>", lambda e: self._guess_group_name())
        tk.Button(search_bar, text="Test", command=self._guess_group_name).pack(side=tk.LEFT)

        tk.Checkbutton(search_bar, text="Zones", variable=self.show_zones,
                        command=self.redraw).pack(side=tk.LEFT, padx=(16, 2))
        tk.Checkbutton(search_bar, text="Zone labels", variable=self.show_zone_labels,
                        command=self.redraw).pack(side=tk.LEFT, padx=2)
        tk.Checkbutton(search_bar, text="Barriers", variable=self.show_barriers,
                        command=self.redraw).pack(side=tk.LEFT, padx=2)
        tk.Checkbutton(search_bar, text="Zone select mode", variable=self.zone_select_mode,
                        command=self._on_zone_select_toggle).pack(side=tk.LEFT, padx=2)
        tk.Button(search_bar, text="Zone types...", command=self.open_zone_types_dialog).pack(side=tk.LEFT, padx=4)

        body = tk.Frame(self)
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.canvas = tk.Canvas(body, bg="black")
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        info_frame = tk.Frame(body, width=280)
        info_frame.pack(side=tk.RIGHT, fill=tk.Y)
        info_frame.pack_propagate(False)
        tk.Label(info_frame, text="Section info", font=("TkDefaultFont", 11, "bold")).pack(
            anchor="w", padx=8, pady=(8, 0))
        self.info_text = tk.Text(info_frame, wrap="word", state="disabled", height=30)
        self.info_text.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        self.canvas.bind("<Configure>", lambda e: self.redraw())
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<MouseWheel>", self._on_wheel)        # Windows / macOS
        self.canvas.bind("<Button-4>", lambda e: self._zoom(e, 1.15))   # Linux scroll up
        self.canvas.bind("<Button-5>", lambda e: self._zoom(e, 1 / 1.15))  # Linux scroll down

        footer = tk.Frame(self)
        footer.pack(side=tk.BOTTOM, fill=tk.X)
        self.status_label = tk.Label(footer, text="No file loaded", anchor="w")
        self.status_label.pack(side=tk.TOP, fill=tk.X, padx=6, pady=(2, 0))
        self.stream_status_label = tk.Label(footer, text="No stream file loaded", anchor="w", fg="#888")
        self.stream_status_label.pack(side=tk.TOP, fill=tk.X, padx=6, pady=(0, 2))

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

        self._show_info(None)
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
        self._show_info(self.world.by_id.get(self.selected_id) if self.world else None)

    def clear_stream_data(self):
        self.stream_scenery = None
        self.stream_path = None
        self.stream_status_label.config(text="No stream file loaded", fg="#888")
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
        if not self.world:
            return
        mode = self.view_mode.get()
        if mode == 'road network':
            self._fit_to_road_network()
        elif mode == 'combined':
            self._fit_to_combined()
        else:
            self._fit_to_boundaries()

    def _fit_to_boundaries(self):
        if not self.world.boundaries:
            return
        xs, ys = self._boundary_points()
        if not xs:
            return
        self._fit_extent(min(xs), max(xs), min(ys), max(ys))

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

    def _fit_to_road_network(self):
        xs, ys = self._road_points()
        if not xs:
            return
        self._fit_extent(min(xs), max(xs), min(ys), max(ys))

    def _road_points(self):
        rn = self.world.road_network
        if not rn or not rn.nodes:
            return [], []
        pts = [self._rotate_xy(n.position[0], n.position[2]) for n in rn.nodes]
        return [p[0] for p in pts], [p[1] for p in pts]

    def _fit_to_combined(self):
        bxs, bys = self._boundary_points()
        rxs, rys = self._road_points()
        xs, ys = bxs + rxs, bys + rys
        if not xs:
            return
        self._fit_extent(min(xs), max(xs), min(ys), max(ys))

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
        self.canvas.delete("all")
        if not self.world:
            return
        mode = self.view_mode.get()
        if mode == 'road network':
            self._redraw_road_network()
        elif mode == 'combined':
            self._redraw_boundaries()
            self._redraw_track_paths()
            self._redraw_road_network()
        else:
            self._redraw_boundaries()
            self._redraw_track_paths()

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
        half_widths = [self._node_half_width(rn, rn.nodes[i]) for i in chain]
        if all(hw is None for hw in half_widths):
            return
        half_widths = [hw if hw is not None else 0.0 for hw in half_widths]
        n = len(canvas_pts)
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
        smooth = n > 2
        self.canvas.create_polygon(coords, fill=fill_color, outline="",
                                    smooth=smooth, splinesteps=12 if smooth else 1)

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
        if self._road_chains is None:
            self._road_chains = self._trace_road_chains(rn)
        if self._overlap_clusters is None:
            self._overlap_clusters = self._find_overlap_clusters(rn)

        vx0, vy0, vx1, vy1 = self._visible_world_bounds()
        show_width = self.show_road_width.get()
        nodes = rn.nodes

        # Road-width strips are drawn per whole chain (one ribbon polygon,
        # see _draw_road_strip_chain) so they curve the same way the
        # centerline does, then the centerline itself on top of that.
        for chain in self._road_chains:
            pts_world = [self._rotate_xy(nodes[i].position[0], nodes[i].position[2]) for i in chain]
            cxs, cys = [p[0] for p in pts_world], [p[1] for p in pts_world]
            if max(cxs) < vx0 or min(cxs) > vx1 or max(cys) < vy0 or min(cys) > vy1:
                continue  # whole chain is off-screen, skip it entirely
            canvas_pts = [self.world_to_canvas(x, y) for x, y in pts_world]

            # If any node on this chain is part of an overlap cluster, color
            # the whole chain (fill + centerline) with the same blue->orange
            # gradient used on the node markers, by that node's rank - so an
            # entire overpass ramp reads as "elevated" at a glance instead of
            # only its endpoint dots. When a chain touches more than one
            # overlap node (rare), the highest rank found wins, since that's
            # the more attention-worthy end of the ambiguity.
            overlap_ts = [self._overlap_clusters[i][0] / (self._overlap_clusters[i][1] - 1)
                          for i in chain if i in self._overlap_clusters and self._overlap_clusters[i][1] > 1]
            if overlap_ts:
                line_color = self._lerp_color("#40a0ff", "#ff6040", max(overlap_ts))
                fill_color = self._lerp_color("#204060", "#803010", max(overlap_ts))
            else:
                line_color, fill_color = "#40c0ff", "#204060"

            if show_width:
                self._draw_road_strip_chain(rn, chain, canvas_pts, fill_color)
            flat = [c for pt in canvas_pts for c in pt]
            if len(chain) > 2:
                self.canvas.create_line(*flat, fill=line_color, width=2,
                                         smooth=True, splinesteps=12)
            else:
                self.canvas.create_line(*flat, fill=line_color, width=2)

        # Node markers: skip drawing them once there are enough on screen
        # that individual create_oval calls would dominate redraw time - the
        # smoothed chain lines already show the network shape at that point.
        # Indices are tracked (not just positions) so the selected node can
        # be found again and always drawn, even past that cap.
        visible = []
        for i, n in enumerate(nodes):
            wx, wy = self._rotate_xy(n.position[0], n.position[2])
            if vx0 <= wx <= vx1 and vy0 <= wy <= vy1:
                visible.append(i)
        draw_all = len(visible) <= 4000
        for i in visible:
            overlap = self._overlap_clusters.get(i)
            if not draw_all and i != self.selected_node_index and overlap is None:
                continue  # overlap-cluster nodes stay visible past the cap too - that's the point
            cx, cy = self.world_to_canvas(*self._rotate_xy(nodes[i].position[0], nodes[i].position[2]))
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
        for b in self.world.boundaries:
            xs = [p[0] for p in b.points]
            ys = [p[1] for p in b.points]
            if not xs or not self._bbox_intersects(min(xs), min(ys), max(xs), max(ys),
                                                     vx0, vy0, vx1, vy1):
                continue
            coords = []
            for (x, y) in b.points:
                cx, cy = self.world_to_canvas(x, y)
                coords.extend([cx, cy])
            if len(coords) < 6:
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
            self.canvas.create_polygon(coords, outline=outline, fill="", width=width,
                                        tags=(f"boundary_{b.ID}",))
            if show_labels:
                lx, ly = self.world_to_canvas(*b.pos)
                label = f"{self.section_letter(b.ID)}{b.ID}"
                self.canvas.create_text(lx, ly, text=label, fill=outline,
                                         font=("TkDefaultFont", 8),
                                         tags=(f"boundary_{b.ID}",))

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
                    coords = []
                    for (x, y) in z.points:
                        coords.extend(self.world_to_canvas(x, y))
                    self.canvas.create_polygon(coords, outline=color, fill="", width=width)
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

    def _on_zone_select_toggle(self):
        """Clears the selection and the info panel when the mode changes, so
        old section info does not stay next to a zone click (or the reverse)."""
        self.selected_id = None
        self.selected_zones = set()
        self.selected_barriers = set()
        if self.world:
            text = ("Zone select mode: click a zone or barrier." if self.zone_select_mode.get()
                    else "Click a section to inspect it.")
            self.info_text.config(state="normal")
            self.info_text.delete("1.0", tk.END)
            self.info_text.insert(tk.END, text)
            self.info_text.config(state="disabled")
        self.redraw()

    def _select_zone_at(self, cx, cy, mode):
        """Click handler for zone select mode. Road nodes are tested first by
        the caller, so they stay selectable in combined view. No section is
        selected here."""
        self.selected_id = None
        self.selected_node_index = None
        self.info_text.config(state="normal")
        self.info_text.delete("1.0", tk.END)
        if mode == 'road network':
            self.info_text.insert(tk.END, "Zones and barriers are not drawn in road network view. "
                                           "Use the boundaries or combined view.")
            self.info_text.config(state="disabled")
        else:
            self.info_text.config(state="disabled")
            if not self._append_track_path_info(cx, cy, standalone=True):
                self.info_text.config(state="normal")
                self.info_text.insert(tk.END, "No zone or barrier at this point.")
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

    def _guess_group_name(self):
        if self.stream_scenery is None:
            messagebox.showinfo("Guess group name", "Open a stream/region file first - no group data loaded yet.")
            return
        candidate = self.group_guess_var.get().strip()
        if not candidate:
            return

        from nfs_hashing import bin_hash
        target = bin_hash(candidate)
        matches = self.stream_scenery.groups_matching_name(candidate)

        self.info_text.config(state="normal")
        self.info_text.delete("1.0", tk.END)
        self.info_text.insert(tk.END, f"{candidate!r} hashes to 0x{target:08X}\n\n")
        if matches:
            self.info_text.insert(tk.END, f"Match! {len(matches)} loaded group(s) have this key:\n")
            for g in matches:
                sections = sorted({self.stream_scenery.overrides[idx].section_number
                                    for idx in g.override_indices if idx < len(self.stream_scenery.overrides)})
                section_labels = ", ".join(self.format_section_label(s) if self.world else str(s)
                                            for s in sections) or "(none)"
                self.info_text.insert(tk.END, f"  group_number={g.group_number}, "
                                               f"{len(g.override_indices)} override(s), sections: {section_labels}\n")
        else:
            self.info_text.insert(tk.END, "No loaded group has this key - not a real name for this file "
                                           "(or the group it belongs to isn't loaded).")
        self.info_text.config(state="disabled")

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
        if not self.world:
            return
        mode = self.view_mode.get()
        self.selected_zones = set()
        self.selected_barriers = set()
        if mode in ('road network', 'combined') and self.world.road_network:
            node_idx = self._node_at_canvas(cx, cy)
            if node_idx is not None:
                self.selected_node_index = node_idx
                self.selected_id = None
                self._show_node_info(node_idx)
                self.redraw()
                return
        if self.zone_select_mode.get():
            self._select_zone_at(cx, cy, mode)
            return
        wx, wy = self.canvas_to_world(cx, cy)
        hit = None
        for b in self.world.boundaries:
            if _point_in_polygon(wx, wy, b.points):
                hit = b
                break
        self.selected_node_index = None
        self.selected_id = hit.ID if hit else None
        self._show_info(hit)
        if mode != 'road network':
            self._append_track_path_info(cx, cy)
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
                f"points ({b.numPoints}):",
            ]
            for p in b.points:
                lines.append(f"    ({p[0]:.2f}, {p[1]:.2f})")
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

            lines.append("")
            lines.append(f"related section IDs ({len(present_visible)}):")
            if present_visible:
                for i in present_visible:
                    lines.append(f"    {self.format_section_label(i)}")
            else:
                lines.append("    (none)")

            if stale_ids:
                lines.append("")
                lines.append(f"possibly stale, unk1 < dataCount ({len(present_stale)}) - "
                              f"untested hypothesis, see nfs_region_prostreet.py:")
                if present_stale:
                    for i in present_stale:
                        lines.append(f"    {self.format_section_label(i)}")
                else:
                    lines.append("    (none)")

            not_in_file = [(i, False) for i in missing_visible] + [(i, True) for i in missing_stale]
            if not_in_file:
                lines.append("")
                lines.append(f"not in this file ({len(not_in_file)}):")
                for i, is_stale in not_in_file:
                    reasons = ", ".join(missing_reasons(i))
                    stale_tag = ", stale" if is_stale else ""
                    lines.append(f"    {self.format_section_label(i)} - {reasons}{stale_tag}")

            if self.stream_scenery is not None:
                lines.append("")
                stream_section = self.stream_scenery.sections.get(b.ID)
                if stream_section is not None:
                    lines.append(f"scenery (stream file): {len(stream_section.instances)} instance(s)")
                    for inst in stream_section.instances:
                        groups = self.stream_scenery.groups_for(b.ID, inst.instance_number)
                        group_str = (", groups: " + ", ".join(g.display_key() for g in groups)) if groups else ""
                        name = stream_section.name_for(inst)
                        name_str = f" ({name})" if name else ""
                        lines.append(f"    #{inst.instance_number} guid=0x{inst.scenery_guid:08X}{name_str}{group_str}")
                else:
                    lines.append("scenery (stream file): no instance data for this section")

                section_overrides = self.stream_scenery.overrides_for_section(b.ID)
                if section_overrides:
                    lines.append(f"override records for this section ({len(section_overrides)}):")
                    for _idx, ov, groups in section_overrides:
                        group_str = (", groups: " + ", ".join(g.display_key() for g in groups)) if groups else ""
                        lines.append(f"    instance #{ov.instance_number}: "
                                     f"flags=0x{ov.instance_flags:04X}{group_str}")

            self.info_text.insert(tk.END, "\n".join(lines))
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
