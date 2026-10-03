"""
3D viewer - a separate tool from nfs_region_viewer.py, built on the same
underlying data (nfs_region_parser, nfs_stream_scenery, nfs_solid_reader).

Streams geometry in based on camera position, the same way the game's own
section system does: find which boundary polygon the camera is standing in,
render that section plus its related (visible-neighbor) sections, and
nothing else. A compact 2D minimap runs alongside the 3D canvas showing the
current section and camera position, so it's always clear "what section am
I in" while flying around - the whole point of building this on top of the
existing region/stream readers rather than a one-off.

Carbon/World09 only for now, same as nfs_solid_reader.py.

Controls: WASD to move, arrow keys to look, Space/Shift for up/down.

This is a first pass - flat/unlit shading (a single directional light dotted
against each triangle's normal, no textures), no z-buffer (painter's
algorithm, back-to-front by average depth), no collision. Good enough to
confirm real geometry renders in the right place with the right streaming
behavior; not a polished renderer.
"""
import math
import time
import tkinter as tk
from tkinter import filedialog, messagebox

from nfs_region_parser import load_region_file, get_label_functions
from nfs_stream_scenery import load_stream_scenery
from nfs_solid_reader import read_object_pack, OBJECT_PACK_CHUNK
from nfs_region_common import walk_chunks


def load_solids(path):
    """Scans a whole file for ObjectPackChunk containers and returns a dict
    of hash -> SolidObject. Same file the stream scenery came from, in
    principle - walk_chunks recurses into everything so it doesn't matter
    where exactly this chunk sits."""
    data = open(path, 'rb').read()
    solids = {}
    for _offset, raw_id, _id_hex, length, _is_container, payload_start in walk_chunks(data):
        if int.from_bytes(raw_id, 'little') == OBJECT_PACK_CHUNK:
            for obj in read_object_pack(data, payload_start, length):
                if obj.hash is not None:
                    solids[obj.hash] = obj
    return solids


def point_in_polygon(x, y, points):
    """Standard ray-casting point-in-polygon test."""
    inside = False
    n = len(points)
    if n < 3:
        return False
    x1, y1 = points[-1]
    for x2, y2 in points:
        if ((y1 > y) != (y2 > y)) and (x < (x2 - x1) * (y - y1) / (y2 - y1 + 1e-12) + x1):
            inside = not inside
        x1, y1 = x2, y2
    return inside


class Viewer3D(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("NFS 3D Viewer (streaming)")
        self.geometry("1200x700")

        self.world = None
        self.stream_scenery = None
        self.solids = {}          # hash -> SolidObject
        self.section_letter = None
        self.format_section_label = None

        self.cam_x, self.cam_y, self.cam_z = 0.0, 5.0, 0.0
        self.yaw, self.pitch = 0.0, 0.0
        self.keys_down = set()
        self.move_speed = 20.0    # world units/sec
        self.look_speed = 1.2     # radians/sec

        self.current_section = None
        self.highlighted_ids = set()
        self.highlight_group = None  # set of (section_number, instance_number) for group-view highlighting
        self.cull_backfaces = True
        self.cull_flip = False

        self._build_ui()
        self.bind("<KeyPress>", self._on_key_down)
        self.bind("<KeyRelease>", self._on_key_up)

        self._last_tick = time.time()
        self.after(16, self._tick)

    # ---------- UI ----------
    def _build_ui(self):
        toolbar = tk.Frame(self)
        toolbar.pack(side=tk.TOP, fill=tk.X)
        tk.Button(toolbar, text="Open region file...", command=self.open_region_dialog).pack(side=tk.LEFT, padx=4, pady=4)
        tk.Button(toolbar, text="Open stream file...", command=self.open_stream_dialog).pack(side=tk.LEFT, padx=4, pady=4)
        tk.Button(toolbar, text="Guess/highlight group...", command=self._prompt_group).pack(side=tk.LEFT, padx=4, pady=4)
        self.status_label = tk.Label(toolbar, text="No data loaded", anchor="w")
        self.status_label.pack(side=tk.LEFT, padx=12)

        body = tk.Frame(self)
        body.pack(fill=tk.BOTH, expand=True)

        self.canvas3d = tk.Canvas(body, bg="#0a0a12")
        self.canvas3d.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.canvas3d.focus_set()

        # Compact 2D minimap, always visible alongside the 3D view.
        self.minimap = tk.Canvas(body, width=260, bg="#000000", highlightthickness=0)
        self.minimap.pack(side=tk.RIGHT, fill=tk.Y)

    def open_region_dialog(self):
        path = filedialog.askopenfilename(title="Open region file (e.g. L5RA)")
        if not path:
            return
        try:
            self.world = load_region_file(path, game='carbon')
        except Exception as e:
            messagebox.showerror("Failed to load region file", str(e))
            return
        self.section_letter, self.format_section_label = get_label_functions('carbon')
        self._update_status()

    def open_stream_dialog(self):
        path = filedialog.askopenfilename(title="Open stream file")
        if not path:
            return
        try:
            loaded = load_stream_scenery(path)
        except Exception as e:
            messagebox.showerror("Failed to load stream scenery", str(e))
            return
        if self.stream_scenery is None:
            self.stream_scenery = loaded
        else:
            self.stream_scenery.merge(loaded)

        try:
            new_solids = load_solids(path)
            self.solids.update(new_solids)
        except Exception as e:
            print(f"[nfs_3d_viewer] no geometry found in this file (not fatal): {e}")

        self._update_status()

    def _update_status(self):
        parts = []
        if self.world is not None:
            parts.append(f"{len(self.world.boundaries)} boundaries")
        if self.stream_scenery is not None:
            parts.append(self.stream_scenery.summary())
        parts.append(f"{len(self.solids)} solid(s) with geometry loaded")
        self.status_label.config(text=" | ".join(parts))

    def _prompt_group(self):
        if self.stream_scenery is None:
            messagebox.showinfo("Groups", "Open a stream file first.")
            return
        win = tk.Toplevel(self)
        win.title("Highlight group")
        tk.Label(win, text="Group name to test:").pack(side=tk.LEFT, padx=4, pady=8)
        var = tk.StringVar()
        entry = tk.Entry(win, textvariable=var, width=28)
        entry.pack(side=tk.LEFT, padx=4)

        def apply():
            matches = self.stream_scenery.groups_matching_name(var.get().strip())
            if not matches:
                messagebox.showinfo("Highlight group", "No loaded group has that name/key.")
                return
            pairs = set()
            for g in matches:
                for idx in g.override_indices:
                    if idx < len(self.stream_scenery.overrides):
                        ov = self.stream_scenery.overrides[idx]
                        pairs.add((ov.section_number, ov.instance_number))
            self.highlight_group = pairs
            win.destroy()

        tk.Button(win, text="Highlight", command=apply).pack(side=tk.LEFT, padx=4)
        entry.bind("<Return>", lambda e: apply())

    # ---------- Input ----------
    def _on_key_down(self, event):
        self.keys_down.add(event.keysym)
        if event.keysym == 'c':
            self.cull_backfaces = not self.cull_backfaces
        elif event.keysym == 'v':
            self.cull_flip = not self.cull_flip

    def _on_key_up(self, event):
        self.keys_down.discard(event.keysym)

    def _tick(self):
        now = time.time()
        dt = min(now - self._last_tick, 0.1)
        self._last_tick = now
        self._update_camera(dt)
        self._redraw()
        self.after(16, self._tick)

    def _update_camera(self, dt):
        keys = self.keys_down
        if 'Left' in keys:
            self.yaw -= self.look_speed * dt
        if 'Right' in keys:
            self.yaw += self.look_speed * dt
        if 'Up' in keys:
            self.pitch = min(1.5, self.pitch + self.look_speed * dt)
        if 'Down' in keys:
            self.pitch = max(-1.5, self.pitch - self.look_speed * dt)

        forward = (math.sin(self.yaw) * math.cos(self.pitch),
                   math.sin(self.pitch),
                   math.cos(self.yaw) * math.cos(self.pitch))
        right = (math.cos(self.yaw), 0.0, -math.sin(self.yaw))

        move = self.move_speed * dt
        if 'w' in keys:
            self.cam_x += forward[0] * move; self.cam_y += forward[1] * move; self.cam_z += forward[2] * move
        if 's' in keys:
            self.cam_x -= forward[0] * move; self.cam_y -= forward[1] * move; self.cam_z -= forward[2] * move
        if 'd' in keys:
            self.cam_x += right[0] * move; self.cam_z += right[2] * move
        if 'a' in keys:
            self.cam_x -= right[0] * move; self.cam_z -= right[2] * move
        if 'space' in keys:
            self.cam_y += move
        if 'Shift_L' in keys or 'Shift_R' in keys:
            self.cam_y -= move

    # ---------- Streaming ----------
    def _current_section(self):
        if self.world is None:
            return None
        for b in self.world.boundaries:
            if point_in_polygon(self.cam_x, self.cam_z, b.points):
                return b.ID
        return None

    def _streamed_sections(self):
        """Current section plus its related (visible-neighbor) sections -
        same streaming rule discussed for the BeamNG side: current + related,
        nothing else loaded."""
        current = self._current_section()
        self.current_section = current
        if current is None or self.world is None:
            return set()
        sections = {current}
        rel = self.world.relations_by_id.get(current)
        if rel is not None:
            sections.update(getattr(rel, 'visible_related_chunk_ids', rel.relatedChunkIDs))
        return sections

    # ---------- 3D projection ----------
    def _world_to_view(self, wx, wy, wz):
        dx, dy, dz = wx - self.cam_x, wy - self.cam_y, wz - self.cam_z
        cy, sy = math.cos(self.yaw), math.sin(self.yaw)
        x1 = dx * cy - dz * sy
        z1 = dx * sy + dz * cy
        cp, sp = math.cos(self.pitch), math.sin(self.pitch)
        y2 = dy * cp - z1 * sp
        z2 = dy * sp + z1 * cp
        return x1, y2, z2

    def _project(self, wx, wy, wz, w, h, focal):
        vx, vy, vz = self._world_to_view(wx, wy, wz)
        if vz <= 0.5:
            return None
        sx = w / 2 + (vx / vz) * focal
        sy = h / 2 - (vy / vz) * focal
        return sx, sy, vz

    @staticmethod
    def _rotate_local(local, rotation):
        r0, r1, r2 = rotation
        x, y, z = local
        return (x * r0[0] + y * r1[0] + z * r2[0],
                x * r0[1] + y * r1[1] + z * r2[1],
                x * r0[2] + y * r1[2] + z * r2[2])

    # ---------- Rendering ----------
    def _redraw(self):
        w = self.canvas3d.winfo_width() or 800
        h = self.canvas3d.winfo_height() or 600
        focal = (w / 2) / math.tan(math.radians(70) / 2)

        self.canvas3d.delete("all")
        sections = self._streamed_sections()

        tris = []  # (avg_depth, [(sx,sy),...], shade)
        light_dir = (0.4, 0.8, 0.3)
        light_len = math.sqrt(sum(c * c for c in light_dir))
        light_dir = tuple(c / light_len for c in light_dir)

        if self.stream_scenery is not None:
            for section_number in sections:
                stream_section = self.stream_scenery.sections.get(section_number)
                if stream_section is None:
                    continue
                for inst in stream_section.instances:
                    info = (stream_section.infos[inst.info_index]
                            if 0 <= inst.info_index < len(stream_section.infos) else None)
                    if info is None or info.solid_key not in self.solids:
                        continue
                    solid = self.solids[info.solid_key]
                    is_highlighted = (self.highlight_group is not None and
                                       (section_number, inst.instance_number) in self.highlight_group)

                    for vset in solid.vertex_sets:
                        for i in range(0, len(vset) - 2, 3):
                            tri_screen = []
                            depths = []
                            world_pts = []
                            for vtx in (vset[i], vset[i + 1], vset[i + 2]):
                                wpos = self._rotate_local(vtx.position, inst.rotation)
                                wx = inst.position[0] + wpos[0]
                                wy = inst.position[1] + wpos[1]
                                wz = inst.position[2] + wpos[2]
                                world_pts.append((wx, wy, wz))
                                proj = self._project(wx, wy, wz, w, h, focal)
                                if proj is None:
                                    tri_screen = None
                                    break
                                tri_screen.append((proj[0], proj[1]))
                                depths.append(proj[2])
                            if tri_screen is None:
                                continue

                            if self.cull_backfaces:
                                signed_area = ((tri_screen[1][0] - tri_screen[0][0]) * (tri_screen[2][1] - tri_screen[0][1])
                                               - (tri_screen[2][0] - tri_screen[0][0]) * (tri_screen[1][1] - tri_screen[0][1]))
                                front_facing = signed_area < 0
                                if self.cull_flip:
                                    front_facing = not front_facing
                                if not front_facing:
                                    continue

                            ax = world_pts[1][0] - world_pts[0][0]
                            ay = world_pts[1][1] - world_pts[0][1]
                            az = world_pts[1][2] - world_pts[0][2]
                            bx = world_pts[2][0] - world_pts[0][0]
                            by = world_pts[2][1] - world_pts[0][1]
                            bz = world_pts[2][2] - world_pts[0][2]
                            nx = ay * bz - az * by
                            ny = az * bx - ax * bz
                            nz = ax * by - ay * bx
                            nlen = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
                            shade = max(0.15, abs((nx * light_dir[0] + ny * light_dir[1] + nz * light_dir[2]) / nlen))

                            if is_highlighted:
                                color = f"#{int(255 * shade):02x}{int(20 * shade):02x}{int(180 * shade):02x}"
                            else:
                                g = int(160 * shade)
                                color = f"#{g:02x}{g:02x}{g:02x}"

                            tris.append((sum(depths) / 3, tri_screen, color))

        tris.sort(key=lambda t: t[0], reverse=True)  # painter's algorithm: far first
        for _depth, pts, color in tris:
            flat = [c for p in pts for c in p]
            self.canvas3d.create_polygon(flat, fill=color, outline="")

        self.canvas3d.create_text(
            8, 8, anchor="nw", fill="#8f8",
            text=f"pos=({self.cam_x:.1f}, {self.cam_y:.1f}, {self.cam_z:.1f})  "
                 f"yaw={math.degrees(self.yaw):.0f} pitch={math.degrees(self.pitch):.0f}\n"
                 f"section: {self.format_section_label(self.current_section) if self.format_section_label and self.current_section else self.current_section}\n"
                 f"streamed sections: {len(sections)}   triangles drawn: {len(tris)}\n"
                 f"backface culling: {'on' if self.cull_backfaces else 'off'} (C to toggle, V to flip winding)")

        self._redraw_minimap(sections)

    def _redraw_minimap(self, streamed_sections):
        self.minimap.delete("all")
        if self.world is None or not self.world.boundaries:
            self.minimap.create_text(10, 10, anchor="nw", fill="#666", text="No region file loaded")
            return

        mm_w = self.minimap.winfo_width() or 260
        mm_h = self.minimap.winfo_height() or 700
        xs = [p[0] for b in self.world.boundaries for p in b.points] or [0, 1]
        zs = [p[1] for b in self.world.boundaries for p in b.points] or [0, 1]
        min_x, max_x = min(xs), max(xs)
        min_z, max_z = min(zs), max(zs)
        span_x = max(max_x - min_x, 1.0)
        span_z = max(max_z - min_z, 1.0)
        scale = min(mm_w / span_x, mm_h / span_z) * 0.9

        def to_mm(x, z):
            return ((x - min_x) * scale + 10, (z - min_z) * scale + 10)

        for b in self.world.boundaries:
            if len(b.points) < 3:
                continue
            coords = [c for p in b.points for c in to_mm(*p)]
            if b.ID == self.current_section:
                color = "#00e0ff"
            elif b.ID in streamed_sections:
                color = "#40a040"
            else:
                color = "#333"
            self.minimap.create_polygon(coords, outline=color, fill="", width=2 if b.ID == self.current_section else 1)

        cx, cy = to_mm(self.cam_x, self.cam_z)
        self.minimap.create_oval(cx - 4, cy - 4, cx + 4, cy + 4, fill="#ff0", outline="")
        dx = math.sin(self.yaw) * 12
        dz = math.cos(self.yaw) * 12
        self.minimap.create_line(cx, cy, cx + dx, cy + dz, fill="#ff0", width=2)


if __name__ == '__main__':
    Viewer3D().mainloop()
