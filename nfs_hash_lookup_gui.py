#!/usr/bin/env python3
"""
nfs_hash_lookup_gui.py - resolve a hex hash back to a string using a
wordlist, checking both NFS hash algorithms (Black Box era), with a
side panel to save matches you want to keep for later.

Algorithms from CiPH3R-88/NFS_Hasher_v3.0 (src/main.py), spot-verified
against 8 known real GroupKey pairs already confirmed in this project's
EmitterLibrary work: vlt_hash matched all 8, bin_hash matched none - both
are kept since bin_hash is confirmed used elsewhere in this project
(scenery group names), so which one applies depends on the field you're
resolving, not a project-wide constant.

Usage:
    python nfs_hash_lookup_gui.py
    (pick your wordlist file from the UI, or pass it as an argument:)
    python nfs_hash_lookup_gui.py hashes_main.txt

Wordlist format: one candidate string per line. Blank lines and lines
starting with # are skipped.

Saved list: File > Export Saved... writes the side panel as a
tab-separated file (hash, algorithm, string) for merging into
KnownEffectNames by hand.
"""

import sys
import tkinter as tk
from tkinter import ttk, filedialog, messagebox


# ================= BIN HASH =================
def bin_hash(text: str) -> int:
    data = text.encode("latin-1", errors="ignore")
    h = 0xFFFFFFFF
    for b in data:
        h = (h * 0x21 + b) & 0xFFFFFFFF
    return h


# ================= VLT HASH =================
def _u32(x):
    return x & 0xFFFFFFFF


def _mix1(a, b, c):
    a = _u32((c >> 13) ^ (a - b - c)); b = _u32((a << 8) ^ (b - c - a)); c = _u32((b >> 13) ^ (c - a - b))
    a = _u32((c >> 12) ^ (a - b - c)); b = _u32((a << 16) ^ (b - c - a)); c = _u32((b >> 5) ^ (c - a - b))
    a = _u32((c >> 3) ^ (a - b - c)); b = _u32((a << 10) ^ (b - c - a)); c = _u32((b >> 15) ^ (c - a - b))
    return a, b, c


def _mix2(a, b, c):
    a = _u32((c >> 13) ^ (a - b - c)); b = _u32((a << 8) ^ (b - c - a)); c = _u32((b >> 13) ^ (c - a - b))
    a = _u32((c >> 12) ^ (a - b - c)); b = _u32((a << 16) ^ (b - c - a)); c = _u32((b >> 5) ^ (c - a - b))
    a = _u32((c >> 3) ^ (a - b - c)); b = _u32((a << 10) ^ (b - c - a))
    return _u32((b >> 15) ^ (c - a - b))


def vlt_hash(text: str) -> int:
    if not text:
        return 0
    arr = text.encode("ascii", errors="ignore")
    a = 0x9E3779B9
    b = 0x9E3779B9
    c = 0xABCDEF00
    v1 = 0
    v2 = len(arr)
    while v2 >= 12:
        a = _u32(a + int.from_bytes(arr[v1:v1 + 4], "little"))
        b = _u32(b + int.from_bytes(arr[v1 + 4:v1 + 8], "little"))
        c = _u32(c + int.from_bytes(arr[v1 + 8:v1 + 12], "little"))
        a, b, c = _mix1(a, b, c)
        v1 += 12
        v2 -= 12
    c = _u32(c + len(arr))
    if v2 == 11: c = _u32(c + (arr[v1 + 10] << 24))
    if v2 >= 10: c = _u32(c + (arr[v1 + 9] << 16))
    if v2 >= 9: c = _u32(c + (arr[v1 + 8] << 8))
    if v2 >= 8: b = _u32(b + (arr[v1 + 7] << 24))
    if v2 >= 7: b = _u32(b + (arr[v1 + 6] << 16))
    if v2 >= 6: b = _u32(b + (arr[v1 + 5] << 8))
    if v2 >= 5: b = _u32(b + arr[v1 + 4])
    if v2 >= 4: a = _u32(a + (arr[v1 + 3] << 24))
    if v2 >= 3: a = _u32(a + (arr[v1 + 2] << 16))
    if v2 >= 2: a = _u32(a + (arr[v1 + 1] << 8))
    if v2 >= 1: a = _u32(a + arr[v1])
    return _mix2(a, b, c)


def load_wordlist(path):
    words = []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                words.append(line)
    return words


def build_tables(words):
    bin_table, vlt_table = {}, {}
    for w in words:
        for v in {w, w.lower()}:
            bin_table.setdefault(bin_hash(v), []).append((w, v))
            vlt_table.setdefault(vlt_hash(v), []).append((w, v))
    return bin_table, vlt_table


def parse_hash(s):
    s = s.strip()
    if not s:
        raise ValueError("empty")
    return int(s, 16) if s.lower().startswith("0x") else int(s, 16)


class HashLookupApp:
    def __init__(self, root, initial_wordlist=None):
        self.root = root
        root.title("NFS Hash Lookup")
        root.geometry("760x420")
        root.minsize(600, 320)

        self.bin_table = {}
        self.vlt_table = {}
        self.wordlist_path = None
        self.saved = []  # list of (hash_hex, algo, string)

        menubar = tk.Menu(root)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Load wordlist...", command=self.load_wordlist_dialog)
        file_menu.add_command(label="Export saved...", command=self.export_saved)
        menubar.add_cascade(label="File", menu=file_menu)
        root.config(menu=menubar)

        main = ttk.Frame(root, padding=10)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=3)
        main.columnconfigure(1, weight=2)
        main.rowconfigure(2, weight=1)

        # -- left: lookup panel --
        left = ttk.Frame(main)
        left.grid(row=0, column=0, rowspan=3, sticky="nsew", padx=(0, 10))
        left.columnconfigure(1, weight=1)

        self.wordlist_label = ttk.Label(left, text="No wordlist loaded", foreground="#888")
        self.wordlist_label.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        ttk.Label(left, text="Hash:").grid(row=1, column=0, sticky="w")
        self.hash_entry = ttk.Entry(left)
        self.hash_entry.grid(row=1, column=1, sticky="ew", padx=(6, 0))
        self.hash_entry.bind("<Return>", lambda e: self.resolve())

        ttk.Button(left, text="Resolve", command=self.resolve).grid(row=2, column=0, columnspan=2, sticky="ew", pady=8)

        self.result_frame = ttk.Frame(left)
        self.result_frame.grid(row=3, column=0, columnspan=2, sticky="ew")
        self.result_var = tk.StringVar(value="")
        self.result_label = ttk.Label(self.result_frame, textvariable=self.result_var, wraplength=380, justify="left")
        self.result_label.pack(side="left", fill="x", expand=True)
        self.save_btn = ttk.Button(self.result_frame, text="Save >>", command=self.save_current, state="disabled")
        self.save_btn.pack(side="right")

        self._current_match = None  # (hash_hex, algo, string)

        # -- right: saved side panel --
        right = ttk.Frame(main)
        right.grid(row=0, column=1, rowspan=3, sticky="nsew")
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)

        ttk.Label(right, text="Saved matches").grid(row=0, column=0, sticky="w")
        self.saved_list = tk.Listbox(right, activestyle="dotbox")
        self.saved_list.grid(row=1, column=0, sticky="nsew", pady=(4, 4))
        scrollbar = ttk.Scrollbar(right, orient="vertical", command=self.saved_list.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        self.saved_list.configure(yscrollcommand=scrollbar.set)

        btn_row = ttk.Frame(right)
        btn_row.grid(row=2, column=0, sticky="ew")
        ttk.Button(btn_row, text="Remove selected", command=self.remove_selected).pack(side="left")
        ttk.Button(btn_row, text="Export...", command=self.export_saved).pack(side="right")

        if initial_wordlist:
            self.load_wordlist(initial_wordlist)

    def load_wordlist_dialog(self):
        path = filedialog.askopenfilename(title="Choose wordlist file", filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if path:
            self.load_wordlist(path)

    def load_wordlist(self, path):
        try:
            words = load_wordlist(path)
        except OSError as e:
            messagebox.showerror("Failed to load wordlist", str(e))
            return
        self.bin_table, self.vlt_table = build_tables(words)
        self.wordlist_path = path
        self.wordlist_label.config(text=f"{len(words)} words loaded from {path.split('/')[-1]}", foreground="#000")

    def resolve(self):
        raw = self.hash_entry.get()
        try:
            h = parse_hash(raw)
        except ValueError:
            self.result_var.set("Enter a valid hex hash (e.g. 0x378D447E)")
            self.save_btn.config(state="disabled")
            self._current_match = None
            return

        if not self.bin_table and not self.vlt_table:
            self.result_var.set("Load a wordlist first (File > Load wordlist...)")
            self.save_btn.config(state="disabled")
            self._current_match = None
            return

        hits = [(w, "bin_hash") for w, _v in self.bin_table.get(h, [])] + \
               [(w, "vlt_hash") for w, _v in self.vlt_table.get(h, [])]

        hex_str = f"0x{h:08X}"
        if not hits:
            self.result_var.set(f"{hex_str}: not found")
            self.save_btn.config(state="disabled")
            self._current_match = None
        else:
            lines = [f"{hex_str}:"] + [f"  {algo} -> {word}" for word, algo in hits]
            self.result_var.set("\n".join(lines))
            # if multiple hits, save the first one; user can re-save others by re-resolving isn't
            # wired per-hit, but this covers the overwhelmingly common single-match case
            word, algo = hits[0]
            self._current_match = (hex_str, algo, word)
            self.save_btn.config(state="normal")

    def save_current(self):
        if not self._current_match:
            return
        if self._current_match in self.saved:
            return
        self.saved.append(self._current_match)
        h, algo, word = self._current_match
        self.saved_list.insert("end", f"{h}\t{algo}\t{word}")

    def remove_selected(self):
        sel = list(self.saved_list.curselection())
        for idx in reversed(sel):
            self.saved_list.delete(idx)
            del self.saved[idx]

    def export_saved(self):
        if not self.saved:
            messagebox.showinfo("Nothing to export", "No saved matches yet.")
            return
        path = filedialog.asksaveasfilename(title="Export saved matches", defaultextension=".tsv",
                                             filetypes=[("Tab-separated", "*.tsv"), ("All files", "*.*")])
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            f.write("hash\talgorithm\tstring\n")
            for h, algo, word in self.saved:
                f.write(f"{h}\t{algo}\t{word}\n")
        messagebox.showinfo("Exported", f"Saved {len(self.saved)} match(es) to {path}")


def main():
    initial = sys.argv[1] if len(sys.argv) > 1 else None
    root = tk.Tk()
    HashLookupApp(root, initial_wordlist=initial)
    root.mainloop()


if __name__ == "__main__":
    main()
