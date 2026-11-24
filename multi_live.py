"""
Watch several YouTube live streams at once (free). Each streamer gets their own window
and their own folder: C:\\highlight_clipper\\clips\\<streamer name>

Run:  py -3.12 C:\\highlight_clipper\\multi_live.py
Keep this file in the same folder as highlight_clipper.py.
"""
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import highlight_clipper as hc  # noqa: E402  (reuses its question helpers)


def ask_links():
    print("Paste your live links (YouTube, Twitch or Kick), one per line.")
    print("Press Enter on an empty line when done.")
    links = []
    while True:
        line = input(f"  link {len(links) + 1}: ").strip().strip('"')
        if not line:
            if links:
                return links
            print("  Paste at least one link.")
            continue
        for part in re.split(r"[\s,]+", line):
            if not part:
                continue
            if not hc.stream_key(part):
                print(f"  Skipped (not a YouTube, Twitch or Kick live link): {part}")
                continue
            link = hc.watch_link(part)
            if link not in links:
                links.append(link)


def streamer_name(link):
    if hc.platform_of(link) != "youtube":  # Twitch / Kick: the link already has the name
        return hc.channel_of(link)
    try:
        out = subprocess.run(
            [sys.executable, "-m", "yt_dlp", "--no-warnings", "--skip-download",
             "--no-playlist", "--print", "%(channel)s", link],
            capture_output=True, text=True, timeout=60).stdout.strip().splitlines()
        name = re.sub(r"[^\w\- ]", "", out[0]).strip() if out else ""
    except Exception:
        name = ""
    return name or hc.stream_key(link)


def main():
    links = ask_links()
    print()
    flags = ["--no-ask"]
    if hc.ask_yes_no("Make vertical 9:16 clips?"):
        style = hc.ask_choice("Vertical style:", [
            "Full gameplay on a blurred background (recommended)",
            "Zoom into the centre",
            "Follow the face (talking / IRL streams)",
            "Gameplay on top + face close-up below (gaming with a facecam)"])
        flags += ["--vertical", ["blur", "crop", "track", "facecam"][style - 1]]
    if hc.ask_yes_no("Add captions? (slower when many streams make clips at once)"):
        cstyle = hc.ask_choice("Caption style:", ["Big bold words (Shorts style)",
                                                 "Small subtitle lines"])
        lang = input("Speech language (en, hi, hinglish, ...) or Enter for auto: ").strip()
        flags += ["--captions", "--caption-style", str(cstyle)]
        if lang:
            flags += ["--language", lang]
    else:
        flags.append("--no-captions")
    flags.append("--gpu" if hc.ask_yes_no("Use your NVIDIA GPU for faster encoding?")
                 else "--no-gpu")
    if hc.ask_yes_no("Upload finished clips to YouTube automatically?", default=False):
        try:
            import youtube_upload
            youtube_upload.get_service()  # sign in once, before the streams start
        except Exception as err:
            sys.exit(f"YouTube sign-in failed: {err}")
        flags.append("--upload")
        keep = input("Uploaded clips to keep on this PC per streamer (Enter = keep all, "
                     "0 = delete right after upload, 1 = keep only the newest): ").strip()
        if keep.isdigit():
            flags += ["--keep-uploaded", keep]
    else:
        flags.append("--no-upload")
    if hc.ask_yes_no("Let an AI write titles, descriptions and tags for each clip?", default=False):
        which = hc.ask_choice("Which AI?", ["Gemini (free key from Google AI Studio)",
                                            "Ollama (free, runs on your PC)"])
        flags += ["--ai", ["gemini", "ollama"][which - 1]]
    else:
        flags += ["--ai", "none"]
        title = input("Title for every uploaded clip (Enter = 'Stream highlight'): ").strip()
        tags = input("Tags for every clip, separated by commas (Enter = automatic tags): ").strip()
        desc = input("Description (Enter = automatic): ").strip()
        if title:
            flags += ["--title", title]
        if tags:
            flags += ["--tags", tags]
        if desc:
            flags += ["--description", desc]
    sens = hc.ask_choice("How sensitive should hype detection be?", [
        "Only the big moments (fewer clips)", "Balanced (recommended)",
        "Catch more (lots of clips)"], default=2)
    quality = hc.ask_choice("Recording quality (uses internet for every stream at once):", [
        "720p (recommended for several streams)", "1080p"])
    flags += ["--sensitivity", str(sens), "--max-height", "720" if quality == 1 else "1080"]
    buf = input("Folder for temporary recordings, on a drive with lots of free space "
                "(Enter = default): ").strip().strip('"')
    if buf:
        flags += ["--buffer-dir", buf]
    days = input("Delete clips older than how many days? (Enter = never): ").strip()
    if days.isdigit() and int(days) > 0:
        flags += ["--keep-days", days]

    print("\nLooking up streamer names...")
    names = {}
    for link in links:
        name = streamer_name(link)
        if name in names.values():
            name += "_" + hc.stream_key(link)
        names[link] = name

    (HERE / "clips").mkdir(exist_ok=True)
    stops = []
    for link in links:
        stop = Path(tempfile.gettempdir()) / f"hc_stop_{hc.stream_key(link)}.flag"
        stop.unlink(missing_ok=True)
        stops.append(stop)
        env = dict(os.environ, HC_STOP_FILE=str(stop), HC_PAUSE_AT_END="1", PYTHONUTF8="1",
                   HF_HUB_DISABLE_SYMLINKS_WARNING="1")
        out = HERE / "clips" / names[link]
        cmd = [sys.executable, str(HERE / "highlight_clipper.py"), "--live", link,
               "--out", str(out)] + flags
        subprocess.Popen(cmd, cwd=HERE, env=env,
                         creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        print(f"  Started: {names[link]}  ->  {out}")
        time.sleep(3)  # start them one after another so they don't all connect at once

    print("\nEvery streamer now has their own window showing live status.")
    print("Keep THIS window open. You can minimise all of them.")
    input("\nPress Enter here when you want to stop all streams...")
    for stop in stops:
        stop.touch()
    print("Stop signal sent. Each window finishes its last clips, then asks you to press "
          "Enter to close it.")


if __name__ == "__main__":
    main()
