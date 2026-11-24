"""Optional, local credential setup for Highlight Clipper."""
import json
import os
import tempfile
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk


def save_private(path, text):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".clipper-setup-", delete=False) as tmp:
        pending = Path(tmp.name)
        try:
            tmp.write(text)
        except OSError:
            tmp.close()
            pending.unlink(missing_ok=True)
            raise
    try:
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


def desktop_client(source):
    data = json.loads(source.read_text(encoding="utf-8-sig"))
    client = data.get("installed") if isinstance(data, dict) else None
    if not isinstance(client, dict) or not all(
        isinstance(client.get(k), str) and client[k].strip()
        for k in ("client_id", "client_secret", "auth_uri", "token_uri")
    ):
        raise ValueError("Expected a Desktop app OAuth client.")
    if (client["auth_uri"] not in ("https://accounts.google.com/o/oauth2/auth",
                                   "https://accounts.google.com/o/oauth2/v2/auth")
            or client["token_uri"] != "https://oauth2.googleapis.com/token"):
        raise ValueError("Expected Google sign-in endpoints.")
    return json.dumps({"installed": client}, indent=2)


class AccountSetup(ctk.CTkToplevel):
    def __init__(self, parent, folder):
        super().__init__(parent)
        self.folder = folder
        self.title("Accounts and API keys")
        self.geometry("570x600")
        self.transient(parent)
        panel = ctk.CTkScrollableFrame(self)
        panel.pack(fill="both", expand=True, padx=12, pady=12)
        self.note(panel, "Clipping and captions work without an account. Set up only the features you want.")
        self.heading(panel, "YouTube uploads and channel reports")
        self.note(panel, "Enable YouTube Data API v3 in Google Cloud. For reports, also enable YouTube Analytics API. Create a Desktop app OAuth client and download its JSON. Google will ask you to choose your channel the first time you use the feature.")
        self.link(panel, "Open Google Cloud credentials", "https://console.cloud.google.com/apis/credentials")
        self.oauth = ctk.CTkButton(panel, text="Import Desktop app JSON", command=self.import_client)
        self.oauth.pack(anchor="w", padx=10, pady=5)
        if (folder / "client_secret.json").is_file():
            self.oauth.configure(text="Desktop app JSON on file", state="disabled")
        self.key_row(panel, "Gemini titles and descriptions", "Get a Gemini key",
                     "https://aistudio.google.com/apikey", "GEMINI_API_KEY", "ai_key.txt")
        self.key_row(panel, "YouTube trend research", "Create a YouTube Data API v3 key",
                     "https://console.cloud.google.com/apis/credentials", "YOUTUBE_API_KEY", "youtube_api_key.txt")
        self.note(panel, "Keys are saved as local text files in this app folder. Share the clean source ZIP when sharing the app.")
        ctk.CTkButton(self, text="Done", command=self.destroy).pack(pady=(0, 12))

    def heading(self, panel, text):
        ctk.CTkLabel(panel, text=text, font=ctk.CTkFont(weight="bold")).pack(anchor="w", padx=10, pady=(12, 0))

    def note(self, panel, text):
        ctk.CTkLabel(panel, text=text, wraplength=460, justify="left").pack(anchor="w", padx=10, pady=4)

    def link(self, panel, text, url):
        ctk.CTkButton(panel, text=text, fg_color="gray32", command=lambda: webbrowser.open(url)).pack(anchor="w", padx=10)

    def import_client(self):
        source = filedialog.askopenfilename(parent=self, filetypes=[("Google Desktop app JSON", "*.json")])
        if not source:
            return
        try:
            text = desktop_client(Path(source))
            save_private(self.folder / "client_secret.json", text)
        except ValueError:
            messagebox.showerror("Choose the Desktop app JSON", "Download the OAuth client JSON for a Desktop app from Google Cloud and select it here.", parent=self)
            return
        except OSError:
            messagebox.showerror("Import failed", "Check that you can read the selected file and write to the app folder.", parent=self)
            return
        self.oauth.configure(text="Desktop app JSON on file", state="disabled")

    def key_row(self, panel, title, link_label, url, env, filename):
        self.heading(panel, title)
        if os.environ.get(env, "").strip():
            self.note(panel, f"Using the key from {env}.")
            return
        self.link(panel, link_label, url)
        row = ctk.CTkFrame(panel, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=6)
        path = self.folder / filename
        entry = ctk.CTkEntry(row, show="*", placeholder_text="Paste a replacement key" if path.is_file() else "Paste your key")
        entry.pack(side="left", fill="x", expand=True)

        def save():
            key = entry.get().strip()
            if not key or any(c.isspace() for c in key):
                messagebox.showerror(title, "Paste one key with no spaces.", parent=self)
                return
            try:
                save_private(path, key)
            except OSError:
                messagebox.showerror(title, "Could not save the key. Check that the app folder is writable.", parent=self)
                return
            entry.delete(0, "end")
            entry.configure(placeholder_text="Paste a replacement key")
            messagebox.showinfo(title, "Key saved on this PC.", parent=self)

        ctk.CTkButton(row, text="Save key", width=85, command=save).pack(side="left", padx=(6, 0))
