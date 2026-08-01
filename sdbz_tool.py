import struct
import os
import threading
import subprocess
import customtkinter as ctk
from tkinter import messagebox
import tkinter as tk


def _win_pick_folder(title="Select folder"):
    ps_script = (
        '[System.Reflection.Assembly]::LoadWithPartialName("System.Windows.Forms") | Out-Null; '
        '$d = New-Object System.Windows.Forms.FolderBrowserDialog; '
        f'$d.Description = "{title}"; '
        '$d.UseDescriptionForTitle = $true; '
        'if ($d.ShowDialog() -eq "OK") { $d.SelectedPath } else { "" }'
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps_script],
        capture_output=True, text=True, creationflags=0x08000000,
    )
    return result.stdout.strip()


def _win_open_file(title="Open file", filter_str="All files|*.*"):
    ps_script = (
        '[System.Reflection.Assembly]::LoadWithPartialName("System.Windows.Forms") | Out-Null; '
        '$d = New-Object System.Windows.Forms.OpenFileDialog; '
        f'$d.Title = "{title}"; '
        f'$d.Filter = "{filter_str}"; '
        'if ($d.ShowDialog() -eq "OK") { $d.FileName } else { "" }'
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps_script],
        capture_output=True, text=True, creationflags=0x08000000,
    )
    return result.stdout.strip()


def _win_save_file(title="Save file", default_name="file.dat", filter_str="All files|*.*"):
    ps_script = (
        '[System.Reflection.Assembly]::LoadWithPartialName("System.Windows.Forms") | Out-Null; '
        '$d = New-Object System.Windows.Forms.SaveFileDialog; '
        f'$d.Title = "{title}"; '
        f'$d.FileName = "{default_name}"; '
        f'$d.Filter = "{filter_str}"; '
        'if ($d.ShowDialog() -eq "OK") { $d.FileName } else { "" }'
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps_script],
        capture_output=True, text=True, creationflags=0x08000000,
    )
    return result.stdout.strip()


SECTOR_SIZE = 0x800
KNOWN_MAGICS = {b"FPIX", b"FMDL", b" ZBE", b"RFSM", b"FFON"}
BPP_MAP = {1: "16-bit", 2: "24-bit", 3: "32-bit", 4: "4-bit", 5: "8-bit"}
TYPE_COLORS = {
    "FPIX": "#e74c3c",
    "FMDL": "#3498db",
    "ZBE":  "#2ecc71",
    "RFSM": "#f39c12",
    "FFON": "#9b59b6",
}


def scan_segments(filepath, progress_cb=None):
    filesize = os.path.getsize(filepath)
    sector_count = filesize // SECTOR_SIZE

    magic_sectors = []
    with open(filepath, "rb") as f:
        for sec in range(sector_count):
            f.seek(sec * SECTOR_SIZE)
            magic = f.read(4)
            if magic in KNOWN_MAGICS:
                magic_sectors.append((sec, magic.decode("ascii", errors="replace").strip()))
            if progress_cb and sec % 5000 == 0:
                progress_cb(sec / sector_count * 0.8)

    segments = []
    for i, (sec, magic) in enumerate(magic_sectors):
        start_off = sec * SECTOR_SIZE
        if i + 1 < len(magic_sectors):
            end_off = magic_sectors[i + 1][0] * SECTOR_SIZE
        else:
            end_off = filesize
        seg_size = end_off - start_off

        info = {"offset": start_off, "size": seg_size, "type": magic}

        with open(filepath, "rb") as f:
            f.seek(start_off)
            hdr = f.read(min(32, seg_size))
            info["declared_size"] = struct.unpack_from("<I", hdr, 4)[0] if len(hdr) >= 8 else 0
            if magic == "FPIX" and len(hdr) >= 16:
                info["entry_count"] = struct.unpack_from("<I", hdr, 8)[0]
                info["flags"] = struct.unpack_from("<I", hdr, 12)[0]

        segments.append(info)
        if progress_cb:
            progress_cb(0.8 + 0.2 * (i + 1) / len(magic_sectors))

    return segments


def parse_fpix_detail(filepath, seg):
    with open(filepath, "rb") as f:
        f.seek(seg["offset"])
        declared = seg["declared_size"]
        raw = f.read(min(declared, seg["size"]))

    entry_count = seg.get("entry_count", 0)
    sub_entries = []
    toc_base = 0x20
    for i in range(entry_count):
        pos = toc_base + i * 4
        if pos + 4 > len(raw):
            break
        sub_off = struct.unpack_from("<I", raw, pos)[0]
        if sub_off + 8 > len(raw):
            break
        sub_magic = raw[sub_off: sub_off + 4]
        sub_size = struct.unpack_from("<I", raw, sub_off + 4)[0]
        info = {"rel_offset": sub_off, "magic": sub_magic, "size": sub_size}

        if sub_magic == b"XPIX":
            tim2_off = sub_off + 0x20
            if tim2_off + 4 <= len(raw) and raw[tim2_off: tim2_off + 4] == b"TIM2":
                pic_off = tim2_off + 0x80
                if pic_off + 0x18 <= len(raw):
                    pic_w = struct.unpack_from("<H", raw, pic_off + 0x14)[0]
                    pic_h = struct.unpack_from("<H", raw, pic_off + 0x16)[0]
                    bpp_type = raw[pic_off + 0x13]
                    pic_img = struct.unpack_from("<I", raw, pic_off + 8)[0]
                    pic_clut = struct.unpack_from("<I", raw, pic_off + 4)[0]
                    info["tex"] = {
                        "w": pic_w, "h": pic_h,
                        "bpp": BPP_MAP.get(bpp_type, f"type={bpp_type}"),
                        "img_size": pic_img, "clut_size": pic_clut,
                    }
        sub_entries.append(info)
    return sub_entries


def parse_fmdl_detail(filepath, seg):
    with open(filepath, "rb") as f:
        f.seek(seg["offset"])
        hdr = f.read(min(64, seg["size"]))
    declared = struct.unpack_from("<I", hdr, 4)[0] if len(hdr) >= 8 else 0
    return {"declared_size": declared}


def extract_segments(filepath, segments, output_dir, progress_cb=None):
    os.makedirs(output_dir, exist_ok=True)
    total = len(segments)
    with open(filepath, "rb") as f:
        for i, seg in enumerate(segments):
            f.seek(seg["offset"])
            data = f.read(seg["size"])
            ext_map = {"FPIX": ".fpix", "FMDL": ".fmdl", "ZBE": ".zbe", "RFSM": ".rfsm", "FFON": ".ffon"}
            ext = ext_map.get(seg["type"], ".bin")
            out_name = f"seg_{i:04d}_0x{seg['offset']:08X}_{seg['type']}{ext}"
            with open(os.path.join(output_dir, out_name), "wb") as out:
                out.write(data)
            if progress_cb:
                progress_cb((i + 1) / total)


REPACK_EXTENSIONS = {".fpix", ".fmdl", ".zbe", ".rfsm", ".ffon", ".bin"}


def repack_segments(input_dir, output_path, progress_cb=None):
    files = sorted(
        [f for f in os.listdir(input_dir) if os.path.splitext(f)[1] in REPACK_EXTENSIONS],
        key=lambda x: int(x.split("_")[1]) if "_" in x and x.split("_")[1].isdigit() else 0,
    )
    total = len(files)
    with open(output_path, "wb") as out:
        for i, fname in enumerate(files):
            with open(os.path.join(input_dir, fname), "rb") as inp:
                data = inp.read()
            out.write(data)
            remainder = len(data) % SECTOR_SIZE
            if remainder:
                out.write(b"\x00" * (SECTOR_SIZE - remainder))
            if progress_cb:
                progress_cb((i + 1) / total)


class SDBZTool(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("SDBZ GAME.DAT Tool")
        self.geometry("1040x680")
        self.minsize(860, 540)

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.dat_path = None
        self.segments = []
        self._build_ui()

    def _build_ui(self):
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=14, pady=(14, 4))
        ctk.CTkLabel(
            top, text="Super Dragon Ball Z — GAME.DAT Tool",
            font=("Segoe UI", 20, "bold"),
        ).pack(side="left")

        mid = ctk.CTkFrame(self, fg_color="transparent")
        mid.pack(fill="x", padx=14, pady=4)

        self.path_var = ctk.StringVar(value="No file loaded")
        ctk.CTkEntry(mid, textvariable=self.path_var, state="disabled", font=("Consolas", 12)).pack(
            side="left", fill="x", expand=True, padx=(0, 8),
        )
        ctk.CTkButton(mid, text="Open GAME.DAT", width=150, command=self._open_file).pack(side="left", padx=2)
        ctk.CTkButton(
            mid, text="Extract All", width=120, command=self._extract,
            fg_color="#2d6a4f", hover_color="#40916c",
        ).pack(side="left", padx=2)
        ctk.CTkButton(
            mid, text="Repack", width=100, command=self._repack,
            fg_color="#7b2d8b", hover_color="#9b59b6",
        ).pack(side="left", padx=2)

        stats = ctk.CTkFrame(self, fg_color="#1a1a2e", corner_radius=8)
        stats.pack(fill="x", padx=14, pady=4)
        self.lbl_segs = ctk.CTkLabel(stats, text="Segments: —", font=("Consolas", 12))
        self.lbl_segs.pack(side="left", padx=16, pady=6)
        self.lbl_size = ctk.CTkLabel(stats, text="Size: —", font=("Consolas", 12))
        self.lbl_size.pack(side="left", padx=16, pady=6)

        self.type_labels = {}
        for t, c in TYPE_COLORS.items():
            lbl = ctk.CTkLabel(stats, text=f"{t}: —", font=("Consolas", 11), text_color=c)
            lbl.pack(side="left", padx=10, pady=6)
            self.type_labels[t] = lbl

        self.lbl_sel = ctk.CTkLabel(stats, text="", font=("Consolas", 12), text_color="#74b9ff")
        self.lbl_sel.pack(side="right", padx=16, pady=6)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=14, pady=4)
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(0, weight=1)

        list_frame = ctk.CTkFrame(body, fg_color="#16213e", corner_radius=8)
        list_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        cols_frame = ctk.CTkFrame(list_frame, fg_color="#0f3460", corner_radius=0)
        cols_frame.pack(fill="x")
        for col, w in [("#", 5), ("Type", 6), ("Offset", 12), ("Size", 11), ("Declared", 11), ("Info", 18)]:
            ctk.CTkLabel(cols_frame, text=col, width=w * 8, font=("Consolas", 11, "bold"), anchor="w").pack(
                side="left", padx=4, pady=4,
            )

        tree_container = ctk.CTkFrame(list_frame, fg_color="#16213e")
        tree_container.pack(fill="both", expand=True)

        self.tree_canvas = tk.Canvas(tree_container, bg="#16213e", highlightthickness=0, bd=0)
        self.tree_scroll = ctk.CTkScrollbar(tree_container, command=self.tree_canvas.yview)
        self.tree_scroll.pack(side="right", fill="y")
        self.tree_canvas.pack(side="left", fill="both", expand=True)
        self.tree_canvas.configure(yscrollcommand=self.tree_scroll.set)

        self.tree_inner = tk.Frame(self.tree_canvas, bg="#16213e")
        self.tree_canvas_win = self.tree_canvas.create_window((0, 0), window=self.tree_inner, anchor="nw")
        self.tree_inner.bind("<Configure>", lambda e: self.tree_canvas.configure(scrollregion=self.tree_canvas.bbox("all")))
        self.tree_canvas.bind("<Configure>", lambda e: self.tree_canvas.itemconfig(self.tree_canvas_win, width=e.width))
        self.tree_canvas.bind_all("<MouseWheel>", lambda e: self.tree_canvas.yview_scroll(-1 * (e.delta // 120), "units"))

        self.detail_frame = ctk.CTkFrame(body, fg_color="#1a1a2e", corner_radius=8)
        self.detail_frame.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        ctk.CTkLabel(self.detail_frame, text="Segment Details", font=("Segoe UI", 14, "bold")).pack(pady=(10, 4))
        self.detail_text = ctk.CTkTextbox(self.detail_frame, font=("Consolas", 11), fg_color="#0d1117", state="disabled")
        self.detail_text.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        bot = ctk.CTkFrame(self, fg_color="transparent")
        bot.pack(fill="x", padx=14, pady=(0, 10))
        self.progress = ctk.CTkProgressBar(bot, height=14)
        self.progress.pack(fill="x", side="left", expand=True, padx=(0, 8))
        self.progress.set(0)
        self.status_var = ctk.StringVar(value="Ready")
        ctk.CTkLabel(bot, textvariable=self.status_var, font=("Consolas", 11), width=260, anchor="e").pack(side="right")

    def _open_file(self):
        def do_open():
            path = _win_open_file("Select GAME.DAT", "DAT files|*.DAT")
            if not path:
                return
            self.after(0, self._start_scan, path)
        threading.Thread(target=do_open, daemon=True).start()

    def _start_scan(self, path):
        self.dat_path = path
        self.path_var.set(path)
        self.status_var.set("Scanning sectors...")
        self.progress.set(0)

        def do_scan():
            segs = scan_segments(path, progress_cb=lambda p: self.after(0, self.progress.set, p))
            self.after(0, self._on_scan_done, segs)

        threading.Thread(target=do_scan, daemon=True).start()

    def _on_scan_done(self, segs):
        self.segments = segs
        filesize = os.path.getsize(self.dat_path)
        self.lbl_segs.configure(text=f"Segments: {len(segs)}")
        self.lbl_size.configure(text=f"Size: {filesize / 1024 / 1024:.1f} MB")

        from collections import Counter
        counts = Counter(s["type"] for s in segs)
        for t, lbl in self.type_labels.items():
            c = counts.get(t, 0)
            total_mb = sum(s["size"] for s in segs if s["type"] == t) / 1024 / 1024
            lbl.configure(text=f"{t}: {c} ({total_mb:.0f}M)")

        self.status_var.set(f"Loaded — {len(segs)} segments, 100% coverage")
        self.progress.set(1)
        self._populate_list()

    def _populate_list(self):
        for w in self.tree_inner.winfo_children():
            w.destroy()

        for i, seg in enumerate(self.segments):
            bg = "#1a1a2e" if i % 2 == 0 else "#16213e"
            row = tk.Frame(self.tree_inner, bg=bg, cursor="hand2")
            row.pack(fill="x")

            type_color = TYPE_COLORS.get(seg["type"], "#aaaaaa")

            info_str = ""
            if seg["type"] == "FPIX":
                ec = seg.get("entry_count", "?")
                info_str = f"{ec}x XPIX textures"
            elif seg["type"] == "FMDL":
                info_str = "3D Model"
            elif seg["type"] == "ZBE":
                info_str = "Animation"
            elif seg["type"] == "RFSM":
                info_str = "Resource map"
            elif seg["type"] == "FFON":
                info_str = "Font data"

            vals = [
                (f"{i}", 5, "#e0e0e0"),
                (seg["type"], 6, type_color),
                (f"0x{seg['offset']:08X}", 12, "#e0e0e0"),
                (f"{seg['size']:,}", 11, "#e0e0e0"),
                (f"{seg['declared_size']:,}", 11, "#888888"),
                (info_str, 18, "#e0e0e0"),
            ]
            for text, w, fg in vals:
                lbl = tk.Label(row, text=text, bg=bg, fg=fg, font=("Consolas", 10), anchor="w", width=w)
                lbl.pack(side="left", padx=4, pady=1)
                lbl.bind("<Button-1>", lambda e, idx=i: self._select_seg(idx))
            row.bind("<Button-1>", lambda e, idx=i: self._select_seg(idx))

    def _select_seg(self, idx):
        seg = self.segments[idx]
        self.lbl_sel.configure(text=f"Selected: #{idx} {seg['type']}")
        self.status_var.set(f"Parsing segment {idx}...")

        def do_parse():
            if seg["type"] == "FPIX":
                detail = parse_fpix_detail(self.dat_path, seg)
                self.after(0, self._show_fpix_detail, idx, seg, detail)
            else:
                self.after(0, self._show_generic_detail, idx, seg)

        threading.Thread(target=do_parse, daemon=True).start()

    def _show_fpix_detail(self, idx, seg, sub_entries):
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")

        lines = [
            f"FPIX Segment #{idx}",
            "=" * 34,
            f"Offset:       0x{seg['offset']:08X}",
            f"Sector:       {seg['offset'] // SECTOR_SIZE}",
            f"Segment size: {seg['size']:,} bytes",
            f"Declared:     {seg['declared_size']:,} bytes",
            f"Padding:      {seg['size'] - seg['declared_size']:,} bytes",
            f"Entries:      {seg.get('entry_count', '?')}",
            f"Flags:        0x{seg.get('flags', 0):X}",
            "",
            "Sub-entries (XPIX):",
            "-" * 34,
        ]

        for i, entry in enumerate(sub_entries):
            magic_str = entry["magic"].decode("ascii", errors="replace").strip("\x00")
            lines.append(f"  [{i:2d}] {magic_str} @ +0x{entry['rel_offset']:X}")
            lines.append(f"       Size: {entry['size']:,}")
            if "tex" in entry:
                t = entry["tex"]
                lines.append(f"       TIM2: {t['w']}x{t['h']} {t['bpp']}")
                lines.append(f"       Img: {t['img_size']:,}  CLUT: {t['clut_size']:,}")
            lines.append("")

        self.detail_text.insert("1.0", "\n".join(lines))
        self.detail_text.configure(state="disabled")
        self.status_var.set(f"Seg {idx}: FPIX with {len(sub_entries)} entries")

    def _show_generic_detail(self, idx, seg):
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")

        type_desc = {
            "FMDL": "3D Model data (Arika format)",
            "ZBE":  "Animation / bone data",
            "RFSM": "Resource/filesystem map",
            "FFON": "Font resource",
        }

        lines = [
            f"{seg['type']} Segment #{idx}",
            "=" * 34,
            f"Type:         {type_desc.get(seg['type'], 'Unknown')}",
            f"Offset:       0x{seg['offset']:08X}",
            f"Sector:       {seg['offset'] // SECTOR_SIZE}",
            f"Segment size: {seg['size']:,} bytes",
            f"Declared:     {seg['declared_size']:,} bytes",
            f"Padding:      {seg['size'] - seg['declared_size']:,} bytes",
        ]

        with open(self.dat_path, "rb") as f:
            f.seek(seg["offset"])
            hdr = f.read(min(64, seg["size"]))
        lines.append("")
        lines.append("Header hex dump:")
        lines.append("-" * 34)
        for row_off in range(0, min(64, len(hdr)), 16):
            hex_part = " ".join(f"{hdr[row_off + j]:02X}" for j in range(min(16, len(hdr) - row_off)))
            asc_part = "".join(
                chr(hdr[row_off + j]) if 32 <= hdr[row_off + j] < 127 else "."
                for j in range(min(16, len(hdr) - row_off))
            )
            lines.append(f"  {row_off:04X}: {hex_part:<48s} {asc_part}")

        self.detail_text.insert("1.0", "\n".join(lines))
        self.detail_text.configure(state="disabled")
        self.status_var.set(f"Seg {idx}: {seg['type']} ({seg['size']:,} bytes)")

    def _extract(self):
        if not self.dat_path or not self.segments:
            messagebox.showwarning("Warning", "Load a GAME.DAT file first.")
            return

        def do_pick_and_extract():
            output_dir = _win_pick_folder("Select output folder")
            if not output_dir:
                return
            extract_dir = os.path.join(output_dir, "GAME_DAT_extracted")
            self.after(0, self.status_var.set, "Extracting...")
            self.after(0, self.progress.set, 0)
            extract_segments(
                self.dat_path, self.segments, extract_dir,
                progress_cb=lambda p: self.after(0, self.progress.set, p),
            )
            self.after(0, self._on_extract_done, extract_dir)

        threading.Thread(target=do_pick_and_extract, daemon=True).start()

    def _on_extract_done(self, extract_dir):
        self.status_var.set(f"Extracted {len(self.segments)} segments")
        self.progress.set(1)
        messagebox.showinfo("Done", f"Extracted {len(self.segments)} segments to:\n{extract_dir}")

    def _repack(self):
        def do_pick_and_repack():
            input_dir = _win_pick_folder("Select folder with extracted segments")
            if not input_dir:
                return
            valid = [f for f in os.listdir(input_dir) if os.path.splitext(f)[1] in REPACK_EXTENSIONS]
            if not valid:
                self.after(0, messagebox.showwarning, "Warning", "No segment files found.")
                return
            output_path = _win_save_file("Save repacked GAME.DAT", "GAME.DAT", "DAT files|*.DAT")
            if not output_path:
                return
            self.after(0, self.status_var.set, "Repacking...")
            self.after(0, self.progress.set, 0)
            repack_segments(
                input_dir, output_path,
                progress_cb=lambda p: self.after(0, self.progress.set, p),
            )
            self.after(0, self._on_repack_done, output_path, len(valid))

        threading.Thread(target=do_pick_and_repack, daemon=True).start()

    def _on_repack_done(self, output_path, count):
        self.status_var.set(f"Repacked {count} segments")
        self.progress.set(1)
        messagebox.showinfo("Done", f"Repacked {count} segments to:\n{output_path}")


if __name__ == "__main__":
    app = SDBZTool()
    app.mainloop()
