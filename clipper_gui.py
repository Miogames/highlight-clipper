"""
Stream Highlight Clipper - desktop window (free: customtkinter).
Setup (once):  py -3.12 -m pip install customtkinter
Run:           py -3.12 clipper_gui.py
Keep this file in the same folder as highlight_clipper.py.
"""
import datetime
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk
from account_setup import AccountSetup

ENGINE = Path(__file__).resolve().with_name("highlight_clipper.py")
REPORT_FILE = Path(__file__).resolve().with_name("channel_coach_report.md")
QUEUE_FILE = Path(__file__).resolve().with_name("upload_queue.json")  # clips waiting for YouTube
FACE_FILE = Path(__file__).resolve().with_name("facecam_box.txt")  # remembers your facecam area
PYTHON = Path(sys.executable)
if PYTHON.name.lower() == "pythonw.exe":  # started without a console: use the normal python
    PYTHON = PYTHON.with_name("python.exe")
PYTHON = str(PYTHON)
VIDEO_TYPES = [("Videos", "*.mp4 *.mkv *.mov *.webm *.ts"), ("All files", "*.*")]
SPEECH_DEVICES = {"Auto (NVIDIA GPU if it works)": "auto", "CPU only": "cpu"}
LANGS = {"Auto-detect": None, "English": "en", "Hindi": "hi", "Hinglish": "hinglish"}
VSTYLES = {"Gameplay on blurred background": "blur", "Zoom into the centre": "crop",
           "Follow the face (talking / IRL streams)": "track",
           "Gameplay on top + face below (gaming with facecam)": "facecam"}
AI_CHOICES = {"Off": "none", "Gemini (free key)": "gemini",
              "Ollama (runs on your PC)": "ollama"}
SENS = {"Only the big moments": 1, "Balanced (recommended)": 2, "Catch more clips": 3}

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Stream Highlight Clipper")
        self.geometry("1040x760")
        self.minsize(940, 660)
        self.proc, self.live_run = None, False
        self.lines = queue.Queue()
        self.stop_file = Path(tempfile.gettempdir()) / f"hc_stop_{os.getpid()}.flag"
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)
        self.left = ctk.CTkScrollableFrame(self, width=410, label_text="Settings")
        self.left.grid(row=0, column=0, sticky="ns", padx=(14, 7), pady=14)
        self.build_settings()
        self.build_output()
        self.on_mode("Video file")
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(150, self.poll)
        self.queue_tick()

    # ---------- small widget helpers ----------
    def section(self, title):
        box = ctk.CTkFrame(self.left)
        box.pack(fill="x", pady=6, padx=4)
        ctk.CTkLabel(box, text=title, font=ctk.CTkFont(weight="bold")).pack(
            anchor="w", padx=12, pady=(8, 4))
        return box

    def field(self, box, label, default=""):
        ctk.CTkLabel(box, text=label).pack(anchor="w", padx=12)
        entry = ctk.CTkEntry(box)
        entry.insert(0, default)
        entry.pack(fill="x", padx=12, pady=(0, 8))
        return entry

    def pick(self, box, label, values, default):
        ctk.CTkLabel(box, text=label).pack(anchor="w", padx=12)
        menu = ctk.CTkOptionMenu(box, values=values)
        menu.set(default)
        menu.pack(fill="x", padx=12, pady=(0, 8))
        return menu

    def switch(self, box, text, on=True):
        sw = ctk.CTkSwitch(box, text=text)
        if on:
            sw.select()
        sw.pack(anchor="w", padx=12, pady=6)
        return sw

    def browse(self, entry, types):
        path = filedialog.askopenfilename(filetypes=types)
        if path:
            entry.delete(0, "end")
            entry.insert(0, path)

    # ---------- layout ----------
    def build_settings(self):
        b = self.section("Getting started")
        ctk.CTkLabel(b, text="To make local clips, choose a video and press Start.").pack(anchor="w", padx=12)
        ctk.CTkButton(b, text="Accounts and API keys", command=self.open_setup).pack(anchor="w", padx=12, pady=8)
        b = self.section("1. What to clip")
        self.mode = ctk.CTkSegmentedButton(b, values=["Video file", "Live stream"],
                                           command=self.on_mode)
        self.mode.set("Video file")
        self.mode.pack(fill="x", padx=12, pady=6)
        self.src_label = ctk.CTkLabel(b, text="")
        self.src_label.pack(anchor="w", padx=12)
        row = ctk.CTkFrame(b, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(0, 8))
        self.source = ctk.CTkEntry(row)
        self.source.pack(side="left", fill="x", expand=True)
        self.src_browse = ctk.CTkButton(row, text="Browse", width=72,
                                        command=lambda: self.browse(self.source, VIDEO_TYPES))
        self.src_browse.pack(side="left", padx=(6, 0))

        b = self.section("2. Clip style")
        self.vertical = self.switch(b, "Vertical 9:16 (Shorts / Reels / TikTok)")
        self.vstyle = self.pick(b, "Vertical layout", list(VSTYLES),
                                "Gameplay on blurred background")
        self.split = self.field(b, "Gameplay + face layout: gameplay height (% of screen)", "58")
        self.zoom = self.field(b, "Gameplay + face layout: face zoom (smaller = closer)", "2.2")
        self.face_box = self.field(b, "Facecam area: x,y,width,height in % (blank = find the face automatically)")
        if FACE_FILE.is_file():
            self.face_box.insert(0, FACE_FILE.read_text(encoding="utf-8").strip())
        ctk.CTkButton(b, text="Select facecam area...", command=self.pick_facecam
                      ).pack(anchor="w", padx=12, pady=(0, 10))
        self.auto_len = self.switch(b, "Auto length (follows how long the hype lasts)")
        self.min_len = self.field(b, "Shortest clip (seconds)", "15")
        self.max_len = self.field(b, "Longest clip (seconds)", "90")

        b = self.section("3. Captions")
        self.captions = self.switch(b, "Add captions")
        self.cstyle = self.pick(b, "Caption style",
                                ["Big bold words (Shorts style)", "Small subtitle lines"],
                                "Big bold words (Shorts style)")
        self.lang = self.pick(b, "Speech language", list(LANGS), "Auto-detect")
        self.model = self.pick(b, "Caption accuracy (bigger = slower)",
                               ["tiny", "base", "small", "medium"], "small")
        self.speech_dev = self.pick(b, "Speech reading runs on (also used for AI titles and tags)",
                                    list(SPEECH_DEVICES), "Auto (NVIDIA GPU if it works)")
        self.gpu_btn = ctk.CTkButton(b, text="Test GPU speech", command=self.start_gpu_test)
        self.gpu_btn.pack(anchor="w", padx=12, pady=(0, 10))

        b = self.section("4. Speed and output")
        self.gpu = self.switch(b, "Use NVIDIA GPU for encoding", on=False)
        self.out = self.field(b, "Save clips in this folder", "clips")
        self.buffer = self.field(b, "Temporary recording folder (blank = Windows temp folder)")
        self.upload = self.switch(b, "Upload clips to YouTube automatically",
                                  on=ENGINE.with_name("youtube_token.json").is_file())
        self.keep_up = self.field(b, "Uploaded clips to keep on this PC (blank = keep all, "
                                  "0 = delete after upload, 1 = keep only the newest)", "")
        self.keep_days = self.field(b, "Delete clips older than (days, 0 = never)", "0")
        self.ai = self.pick(b, "AI writes title, description and tags", list(AI_CHOICES), "Off")
        self.privacy = self.pick(b, "Upload visibility (YouTube forces private until the "
                                    "project is approved)", ["private", "unlisted", "public"],
                                 "private")
        self.retry_hours = self.field(b, "If YouTube says the upload limit is reached, retry every "
                                         "(hours)", "6")
        self.queue_label = ctk.CTkLabel(b, text="", justify="left", anchor="w", wraplength=360)
        self.queue_label.pack(anchor="w", padx=12, pady=(6, 2))
        self.keep_retrying = self.switch(b, "Keep retrying until all are uploaded (leave this "
                                            "window open)", on=False)
        self.retry_btn = ctk.CTkButton(b, text="Upload waiting clips now", command=self.start_retry)
        self.retry_btn.pack(anchor="w", padx=12, pady=(0, 10))

        b = self.section("5. YouTube metadata")
        self.title_entry = self.field(b, "Title for every clip (same each time; use {n} for the clip number)", "Stream highlight")
        self.auto_metadata = self.switch(b, "When AI is off, make a title hook from the clip's speech", on=False)
        self.game = self.field(b, "Game or topic (only used as relevant metadata)")
        self.tags = self.field(b, "Tags for every clip (comma separated; blank = automatic)")
        self.hashtags = self.field(b, "Hashtags (up to 3, comma separated; blank = automatic)")
        self.description = self.field(b, "Description / call to action (optional)")
        self.credit = self.field(b, "Creator/source credit (when applicable)")
        self.playlist = self.field(b, "Existing YouTube playlist ID (optional)")
        row = ctk.CTkFrame(b, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(0, 8))
        self.thumbnail = ctk.CTkEntry(row, placeholder_text="Optional truthful JPG/PNG thumbnail")
        self.thumbnail.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(row, text="Browse", width=72,
                      command=lambda: self.browse(self.thumbnail,
                                                  [("Images", "*.jpg *.jpeg *.png *.webp"),
                                                   ("All files", "*.*")])
                      ).pack(side="left", padx=(6, 0))

        b = self.section("6. Publishing schedule and audience")
        self.publish_at = self.field(b, "First publish time, e.g. 2026-11-18T19:00:00+05:30 (optional)")
        self.publish_gap = self.field(b, "Minutes between scheduled clips", "180")
        self.made_for_kids = self.switch(b, "This content is made for kids", on=False)

        b = self.section("7. Channel coach (learns from your YouTube numbers)")
        self.niche = self.field(b, "About your channel for the coach (optional), e.g. GTA 6 Hinglish "
                                   "gamer, viewers 16-24 in India")
        ctk.CTkLabel(b, text="Pick Gemini in 'AI writes title, description and tags' to get written "
                             "advice. With AI Off you still get the numbers.", wraplength=360,
                     justify="left", anchor="w", text_color="gray70").pack(anchor="w", padx=12)
        self.coach_label = ctk.CTkLabel(b, text="", anchor="w", justify="left", wraplength=360)
        self.coach_label.pack(anchor="w", padx=12, pady=(6, 2))
        row = ctk.CTkFrame(b, fg_color="transparent")
        row.pack(anchor="w", padx=12, pady=(0, 10))
        self.coach_btn = ctk.CTkButton(row, text="Analyse my channel", command=self.start_coach)
        self.coach_btn.pack(side="left")
        ctk.CTkButton(row, text="Open last report", fg_color="gray30",
                      command=self.open_report).pack(side="left", padx=8)
        self.refresh_coach()

        self.video_box = self.section("Video options")
        self.top = self.field(self.video_box, "How many clips", "10")
        self.fixed = self.field(self.video_box, "Clip length if auto length is off (seconds)", "30")
        self.smart = self.switch(self.video_box, "Smart clip picking (excited voice, not just loud sound)")
        self.chat = self.field(self.video_box, "Chat file (optional, YouTube or Twitch .json)")
        ctk.CTkButton(self.video_box, text="Choose chat file",
                      command=lambda: self.browse(self.chat, [("Chat files", "*.json")])
                      ).pack(anchor="w", padx=12, pady=(0, 10))

        self.live_box = self.section("Live options")
        self.sens = self.pick(self.live_box, "Hype sensitivity", list(SENS),
                              "Balanced (recommended)")

    def open_setup(self):
        dialog = getattr(self, "setup_dialog", None)
        if dialog is None or not dialog.winfo_exists():
            self.setup_dialog = AccountSetup(self, ENGINE.parent)
        self.setup_dialog.lift()
        self.setup_dialog.focus_set()

    def build_output(self):
        right = ctk.CTkFrame(self)
        right.grid(row=0, column=1, sticky="nsew", padx=(7, 14), pady=14)
        right.rowconfigure(2, weight=1)
        right.columnconfigure(0, weight=1)
        header = ctk.CTkFrame(right, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 0))
        ctk.CTkLabel(header, text="Stream Highlight Clipper",
                     font=ctk.CTkFont(size=22, weight="bold")
                     ).pack(anchor="w")
        ctk.CTkButton(header, text="Built by Miogames", fg_color="transparent",
                      text_color="#80bfff", anchor="w", width=110, height=24,
                      command=lambda: webbrowser.open("https://github.com/Miogames")
                      ).pack(anchor="w")
        self.status = ctk.CTkLabel(right, text="Ready.", anchor="w", text_color="gray70")
        self.status.grid(row=1, column=0, sticky="ew", padx=16, pady=(2, 8))
        self.log = ctk.CTkTextbox(right, state="disabled", wrap="word")
        self.log.grid(row=2, column=0, sticky="nsew", padx=16)
        bar = ctk.CTkFrame(right, fg_color="transparent")
        bar.grid(row=3, column=0, sticky="ew", padx=16, pady=14)
        self.start_btn = ctk.CTkButton(bar, text="Start", height=40, command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ctk.CTkButton(bar, text="Stop", height=40, fg_color="#b3402e",
                                      hover_color="#8f3223", state="disabled", command=self.stop)
        self.stop_btn.pack(side="left", padx=8)
        ctk.CTkButton(bar, text="Open clips folder", height=40, fg_color="gray30",
                      command=self.open_folder).pack(side="right")

    def on_mode(self, mode):
        live = mode == "Live stream"
        self.source.delete(0, "end")
        self.src_label.configure(text="Live stream link (YouTube, Twitch or Kick)" if live else "Video file")
        self.src_browse.configure(state="disabled" if live else "normal")
        if live:
            self.video_box.pack_forget()
            self.live_box.pack(fill="x", pady=6, padx=4)
        else:
            self.live_box.pack_forget()
            self.video_box.pack(fill="x", pady=6, padx=4)

    # ---------- facecam area picker ----------
    @staticmethod
    def grab_frame(src, dest):
        """Save one picture (scaled to 540 px high) from a video or image file."""
        for seek in (60, 10, 0):  # a moment into the video; fall back for short files and images
            dest.unlink(missing_ok=True)
            cmd = ["ffmpeg", "-y", "-v", "error"] + (["-ss", str(seek)] if seek else []) + [
                "-i", src, "-frames:v", "1", "-vf", "scale=-2:540", str(dest)]
            try:
                subprocess.run(cmd, capture_output=True, timeout=90,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except Exception:
                pass
            if dest.is_file() and dest.stat().st_size > 0:
                return True
        return False

    @staticmethod
    def box_from_rect(x0, y0, x1, y1, w, h):
        """Dragged rectangle (pixels) -> 'x,y,width,height' in percent, or None if too small."""
        x0, x1 = sorted((max(0, min(x0, w)), max(0, min(x1, w))))
        y0, y1 = sorted((max(0, min(y0, h)), max(0, min(y1, h))))
        if (x1 - x0) < 0.04 * w or (y1 - y0) < 0.04 * h:
            return None
        return f"{x0 / w * 100:.1f},{y0 / h * 100:.1f},{(x1 - x0) / w * 100:.1f},{(y1 - y0) / h * 100:.1f}"

    def pick_facecam(self):
        src = self.source.get().strip().strip('"')
        if self.mode.get() == "Live stream" or not Path(src).is_file():
            src = filedialog.askopenfilename(
                title="Choose a video or a screenshot of your stream",
                filetypes=[("Video or image", "*.mp4 *.mkv *.mov *.webm *.ts *.png *.jpg *.jpeg *.webp"),
                           ("All files", "*.*")])
            if not src:
                return
        frame = Path(tempfile.gettempdir()) / "hc_facecam_frame.png"
        if not self.grab_frame(src, frame):
            messagebox.showerror("Facecam area", "Could not read a picture from that file. "
                                 "Is FFmpeg installed?")
            return
        try:
            photo = tk.PhotoImage(file=str(frame))
        except tk.TclError:
            messagebox.showerror("Facecam area", "Could not show the picture.")
            return
        w, h = photo.width(), photo.height()
        win = ctk.CTkToplevel(self)
        win.title("Select facecam area")
        win.transient(self)
        ctk.CTkLabel(win, text="Drag a box around your face cam, then press 'Use this area'."
                     ).pack(padx=14, pady=(12, 6))
        canvas = tk.Canvas(win, width=w, height=h, highlightthickness=0, cursor="crosshair",
                           bg="black")
        canvas.pack(padx=14)
        canvas.create_image(0, 0, anchor="nw", image=photo)
        canvas.photo = photo  # keep the picture alive
        st = {"start": None, "rect": None, "end": None}
        status = ctk.CTkLabel(win, text="", text_color="gray70")
        status.pack(pady=(6, 0))

        def press(e):
            st["start"], st["end"] = (e.x, e.y), (e.x, e.y)
            if st["rect"]:
                canvas.delete(st["rect"])
            st["rect"] = canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="#ff3b30", width=3)

        def drag(e):
            if st["start"]:
                st["end"] = (max(0, min(e.x, w)), max(0, min(e.y, h)))
                canvas.coords(st["rect"], *st["start"], *st["end"])
                box = self.box_from_rect(*st["start"], *st["end"], w, h)
                status.configure(text=f"Area: {box} %" if box else "Keep dragging...")

        def use():
            box = self.box_from_rect(*(st["start"] or (0, 0)), *(st["end"] or (0, 0)), w, h)
            if not box:
                messagebox.showinfo("Facecam area", "Drag a box around your face cam first.",
                                    parent=win)
                return
            self.face_box.delete(0, "end")
            self.face_box.insert(0, box)
            try:
                FACE_FILE.write_text(box, encoding="utf-8")
            except OSError:
                pass
            self.vertical.select()
            self.vstyle.set("Gameplay on top + face below (gaming with facecam)")
            win.destroy()

        def clear():
            self.face_box.delete(0, "end")
            FACE_FILE.unlink(missing_ok=True)
            win.destroy()

        canvas.bind("<ButtonPress-1>", press)
        canvas.bind("<B1-Motion>", drag)
        row = ctk.CTkFrame(win, fg_color="transparent")
        row.pack(pady=12)
        ctk.CTkButton(row, text="Use this area", command=use).pack(side="left", padx=6)
        ctk.CTkButton(row, text="Auto-detect instead", fg_color="gray30", command=clear
                      ).pack(side="left", padx=6)
        ctk.CTkButton(row, text="Cancel", fg_color="gray30", command=win.destroy
                      ).pack(side="left", padx=6)
        win.after(200, lambda: (win.lift(), win.focus_force()))

    # ---------- building the command ----------
    @staticmethod
    def num(entry, name):
        text = entry.get().strip()
        if not text.isdigit() or int(text) < 1:
            raise ValueError(f"'{name}' must be a whole number.")
        return int(text)

    @staticmethod
    def nonnegative_num(entry, name):
        text = entry.get().strip()
        if not text.isdigit():
            raise ValueError(f"'{name}' must be a whole number of zero or more.")
        return int(text)

    def build_command(self):
        live = self.mode.get() == "Live stream"
        src = self.source.get().strip().strip('"')
        if not src:
            raise ValueError("Paste the YouTube live link first." if live
                             else "Choose a video file first.")
        if not live and not Path(src).is_file():
            raise ValueError("That video file was not found.")
        cmd = [PYTHON, "-u", str(ENGINE)]
        cmd += ["--no-ask", "--live", src] if live else [src, "--no-ask"]
        cmd += ["--out", self.out.get().strip() or "clips",
                "--min-len", str(self.num(self.min_len, "Shortest clip")),
                "--max-len", str(self.num(self.max_len, "Longest clip"))]
        if live:
            cmd += ["--sensitivity", str(SENS[self.sens.get()])]
        else:
            cmd += ["--top", str(self.num(self.top, "How many clips"))]
            cmd.append("--smart-pick" if self.smart.get() else "--no-smart-pick")
            if self.auto_len.get():
                cmd.append("--auto-length")
            else:
                cmd += ["--no-auto-length", "--length", str(self.num(self.fixed, "Clip length"))]
            chat = self.chat.get().strip().strip('"')
            if chat:
                if not Path(chat).is_file():
                    raise ValueError("That chat file was not found.")
                cmd += ["--chat", chat]
        if self.vertical.get():
            cmd += ["--vertical", VSTYLES[self.vstyle.get()]]
            try:
                zoom = float(self.zoom.get().strip().replace(",", "."))
            except ValueError:
                raise ValueError("'Face zoom' must be a number like 2.2.")
            cmd += ["--split", str(self.num(self.split, "Gameplay height")),
                    "--face-zoom", str(zoom)]
            box = self.face_box.get().strip()
            if box and VSTYLES[self.vstyle.get()] == "facecam":
                if len([v for v in re.split(r"[,\s;]+", box.replace("%", "")) if v]) != 4:
                    raise ValueError("'Facecam area' must be four numbers: x,y,width,height "
                                     "(use the Select facecam area button).")
                cmd += ["--face-box", box]
        if self.captions.get():
            cmd += ["--captions", "--model", self.model.get(),
                    "--caption-style", "1" if self.cstyle.get().startswith("Big") else "2"]
            if LANGS[self.lang.get()]:
                cmd += ["--language", LANGS[self.lang.get()]]
        else:
            cmd.append("--no-captions")
            if AI_CHOICES[self.ai.get()] != "none":  # AI reads the speech for titles and tags
                cmd += ["--model", self.model.get()]
                if LANGS[self.lang.get()]:
                    cmd += ["--language", LANGS[self.lang.get()]]
        cmd.append("--gpu" if self.gpu.get() else "--no-gpu")
        if self.buffer.get().strip():
            cmd += ["--buffer-dir", self.buffer.get().strip().strip('"')]
        cmd += ["--ai", AI_CHOICES[self.ai.get()]]
        title = self.title_entry.get().strip()
        if title:
            cmd += ["--title", title]
        if self.auto_metadata.get():
            cmd.append("--auto-metadata")
        for flag, entry in [("--game", self.game), ("--tags", self.tags), ("--hashtags", self.hashtags),
                            ("--description", self.description), ("--credit", self.credit),
                            ("--playlist-id", self.playlist), ("--thumbnail", self.thumbnail),
                            ("--publish-at", self.publish_at)]:
            value = entry.get().strip().strip('"')
            if value:
                cmd += [flag, value]
        cmd += ["--publish-gap", str(self.nonnegative_num(
            self.publish_gap, "Minutes between scheduled clips"))]
        if self.made_for_kids.get():
            cmd.append("--made-for-kids")
        days = self.keep_days.get().strip()
        if not days.isdigit():
            raise ValueError("'Delete clips older than' must be a whole number (0 = never).")
        cmd += ["--keep-days", days]
        keep = self.keep_up.get().strip()
        if keep:
            if not keep.isdigit():
                raise ValueError("'Uploaded clips to keep' must be a whole number, or empty.")
            cmd += ["--keep-uploaded", keep]
        cmd += ["--retry-hours", self.retry_hours_value()]
        cmd += ["--speech-device", SPEECH_DEVICES[self.speech_dev.get()]]
        cmd += (["--upload", "--privacy", self.privacy.get()] if self.upload.get()
                else ["--no-upload"])
        return cmd, live

    def retry_hours_value(self):
        hours = self.retry_hours.get().strip() or "6"
        try:
            if float(hours) <= 0:
                raise ValueError
        except ValueError:
            raise ValueError("'Retry every (hours)' must be a number above 0, for example 6.")
        return hours

    def retry_command(self):
        cmd = [PYTHON, "-u", str(ENGINE), "--retry-uploads", "--no-ask",
               "--retry-hours", self.retry_hours_value()]
        if self.keep_retrying.get():
            cmd.append("--retry-wait")
        return cmd

    def start_gpu_test(self):
        if self.proc:
            return
        if not ENGINE.is_file():
            messagebox.showerror("Missing file",
                                 "Keep clipper_gui.py in the same folder as highlight_clipper.py.")
            return
        self.live_run = False
        self.launch([PYTHON, "-u", str(ENGINE), "--check-gpu", "--no-ask"])

    # ---------- channel coach ----------
    def coach_command(self):
        cmd = [PYTHON, "-u", str(ENGINE), "--coach", "--no-ask",
               "--ai", AI_CHOICES[self.ai.get()]]
        for flag, entry in (("--game", self.game), ("--niche", self.niche)):
            if entry.get().strip():
                cmd += [flag, entry.get().strip()]
        return cmd

    def refresh_coach(self):
        if REPORT_FILE.is_file():
            when = datetime.datetime.fromtimestamp(REPORT_FILE.stat().st_mtime).strftime("%d %b %H:%M")
            self.coach_label.configure(text=f"Last report: {when}")
        else:
            self.coach_label.configure(text="No report yet. The first run asks you to allow "
                                            "'View YouTube Analytics reports' in your browser.")

    def start_coach(self):
        if self.proc:
            return
        if not ENGINE.is_file():
            messagebox.showerror("Missing file",
                                 "Keep clipper_gui.py in the same folder as highlight_clipper.py.")
            return
        self.live_run = False
        self.launch(self.coach_command())

    def open_report(self):
        if not REPORT_FILE.is_file():
            messagebox.showinfo("Channel coach", "There is no report yet. Press 'Analyse my channel'.")
        elif hasattr(os, "startfile"):
            os.startfile(str(REPORT_FILE))  # Windows opens it in your default app
        else:
            messagebox.showinfo("Channel coach", f"The report is saved here:\n{REPORT_FILE}")

    # ---------- upload queue ----------
    def refresh_queue(self):
        try:
            items = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
            items = [e for e in items if isinstance(e, dict)]
        except (OSError, ValueError):
            items = []
        if not items:
            self.queue_label.configure(text="No clips are waiting to upload.")
            return
        nxt = min(e.get("next_try", 0) for e in items)
        when = ("now" if nxt <= datetime.datetime.now().timestamp()
                else datetime.datetime.fromtimestamp(nxt).strftime("%d %b %H:%M"))
        ai_n = sum(1 for e in items if e.get("pending_ai"))
        extra = f" ({ai_n} waiting for the AI to write titles)" if ai_n else ""
        self.queue_label.configure(
            text=f"Waiting to upload: {len(items)} clip(s){extra}. Next automatic try: {when}.")

    def queue_tick(self):
        self.refresh_queue()
        self.after(30000, self.queue_tick)

    def start_retry(self):
        if self.proc:
            return
        if not ENGINE.is_file():
            messagebox.showerror("Missing file",
                                 "Keep clipper_gui.py in the same folder as highlight_clipper.py.")
            return
        try:
            cmd = self.retry_command()
        except ValueError as err:
            messagebox.showerror("Check your settings", str(err))
            return
        self.live_run = bool(self.keep_retrying.get())  # Stop is polite while it waits
        self.launch(cmd)

    # ---------- running ----------
    def start(self):
        if not ENGINE.is_file():
            messagebox.showerror("Missing file",
                                 "Keep clipper_gui.py in the same folder as highlight_clipper.py.")
            return
        try:
            cmd, self.live_run = self.build_command()
        except ValueError as err:
            messagebox.showerror("Check your settings", str(err))
            return
        self.launch(cmd)

    def launch(self, cmd):
        self.stop_file.unlink(missing_ok=True)
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
                   HC_STOP_FILE=str(self.stop_file), HF_HUB_DISABLE_SYMLINKS_WARNING="1")
        self.proc = subprocess.Popen(
            cmd, cwd=ENGINE.parent, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        threading.Thread(target=self.read, args=(self.proc,), daemon=True).start()
        self.start_btn.configure(state="disabled")
        self.retry_btn.configure(state="disabled")
        self.gpu_btn.configure(state="disabled")
        self.coach_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status.configure(text="Working...")

    def read(self, proc):
        for line in proc.stdout:
            self.lines.put(line.rstrip("\r\n"))
        proc.wait()
        self.lines.put(None)

    def poll(self):
        try:
            while True:
                line = self.lines.get_nowait()
                if line is None:
                    self.finished()
                    continue
                text = line.strip()
                if not text:
                    continue
                if text.startswith("Watching |"):
                    self.status.configure(text=text)
                else:
                    self.log.configure(state="normal")
                    self.log.insert("end", line + "\n")
                    self.log.see("end")
                    self.log.configure(state="disabled")
                    self.status.configure(text=text[:130])
        except queue.Empty:
            pass
        self.after(150, self.poll)

    def finished(self):
        code = self.proc.returncode if self.proc else 0
        self.proc = None
        self.start_btn.configure(state="normal")
        self.retry_btn.configure(state="normal")
        self.gpu_btn.configure(state="normal")
        self.coach_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.refresh_queue()
        self.refresh_coach()
        self.status.configure(text="Finished. Click 'Open clips folder' to see your clips."
                              if code == 0 else "Stopped with an error. Read the log above.")

    def stop(self):
        if not self.proc:
            return
        if self.live_run:  # polite stop: the engine finishes the clips in progress
            self.stop_file.touch()
            self.stop_btn.configure(state="disabled")
            self.status.configure(text="Stopping... finishing the last clips (can take a minute).")
            self.after(180000, lambda p=self.proc: self.force_stop(p))
        else:
            self.force_stop(self.proc)

    @staticmethod
    def force_stop(proc):
        if proc and proc.poll() is None:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)

    def open_folder(self):
        out = Path(self.out.get().strip() or "clips")
        out = out if out.is_absolute() else ENGINE.parent / out
        out.mkdir(parents=True, exist_ok=True)
        os.startfile(out)

    def on_close(self):
        self.force_stop(self.proc)
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
