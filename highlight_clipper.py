"""
Gaming Stream Highlight Clipper (free tools only: Python, numpy, FFmpeg, faster-whisper)

Finds exciting moments from audio loudness (and optionally Twitch chat activity),
then cuts a clip around each one. It ASKS you what you want each time you run it:
how many clips, vertical 9:16 or not, captions yes/no, GPU yes/no.

Setup (once):   pip install numpy faster-whisper yt-dlp chat-downloader
Live mode:      python highlight_clipper.py --live "YOUTUBE_LIVE_LINK"   (Ctrl+C to stop)
Run:            python highlight_clipper.py stream.mp4
Skip questions: python highlight_clipper.py stream.mp4 --no-ask
Or answer on the command line, for example:
    python highlight_clipper.py stream.mp4 --top 8 --vertical --captions --gpu
"""
import argparse
import base64
import csv
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import queue
import random
import urllib.error
import urllib.parse
import urllib.request
import uuid
import collections
import contextlib
from pathlib import Path

import numpy as np

SAMPLE_RATE = 8000  # low sample rate is plenty for loudness and keeps it fast


# ---------- questions ----------

def ask_yes_no(question, default=True):
    hint = "Y/n" if default else "y/N"
    while True:
        ans = input(f"{question} [{hint}]: ").strip().lower()
        if not ans:
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print("  Please type y or n.")


def ask_number(question, default):
    while True:
        ans = input(f"{question} [{default}]: ").strip()
        if not ans:
            return default
        if ans.isdigit() and int(ans) > 0:
            return int(ans)
        print("  Please type a number.")


def ask_choice(question, options, default=1):
    print(question)
    for i, text in enumerate(options, 1):
        print(f"  {i}) {text}")
    while True:
        ans = input(f"Choose [{default}]: ").strip()
        if not ans:
            return default
        if ans.isdigit() and 1 <= int(ans) <= len(options):
            return int(ans)
        print("  Please type one of the numbers.")


# ---------- highlight detection ----------

def audio_loudness(video):
    """Return the loudness (RMS) of every second of the video's audio."""
    cmd = ["ffmpeg", "-v", "error", "-i", video, "-vn", "-ac", "1",
           "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    bytes_per_sec = SAMPLE_RATE * 2  # 16-bit samples
    levels = []
    while True:
        chunk = proc.stdout.read(bytes_per_sec)
        if len(chunk) < bytes_per_sec:
            break
        samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
        levels.append(float(np.sqrt(np.mean(samples * samples))))
    proc.wait()
    return np.array(levels)


def audio_features(video):
    """Per second: loudness, brightness of the sound (spectral centre) and the share of energy
    in the human-voice band. Shouting is louder AND brighter AND voice-heavy; music and game
    explosions are usually not."""
    cmd = ["ffmpeg", "-v", "error", "-i", video, "-vn", "-ac", "1",
           "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    n = SAMPLE_RATE * 2
    freqs = np.fft.rfftfreq(SAMPLE_RATE, 1.0 / SAMPLE_RATE)
    vmask = (freqs >= 300) & (freqs <= 3400)
    window = np.hanning(SAMPLE_RATE)
    rms, cent, voice = [], [], []
    while True:
        chunk = proc.stdout.read(n)
        if len(chunk) < n:
            break
        x = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
        rms.append(float(np.sqrt(np.mean(x * x))))
        spec = np.abs(np.fft.rfft(x * window)) ** 2
        tot = float(spec.sum()) + 1e-9
        cent.append(float((freqs * spec).sum() / tot))
        voice.append(float(spec[vmask].sum() / tot))
    proc.wait()
    return np.array(rms), np.array(cent), np.array(voice)


def rolling_median(x, window=91):
    """Median of the surrounding ~90 seconds: what 'normal' sounds like right here."""
    x = np.asarray(x, dtype=np.float64)
    if len(x) < 5:
        return x.copy()
    w = min(window, len(x))
    w -= (w % 2 == 0)
    pad = w // 2
    view = np.lib.stride_tricks.sliding_window_view(np.pad(x, pad, mode="edge"), w)
    return np.median(view, axis=1)


def excitement_score(rms, cent, voice):
    """0..1 score per second for 'the streamer just got hyped', judged against the
    surrounding audio instead of the whole stream, so loud music or constant gunfire
    no longer looks like a highlight."""
    rms, cent, voice = (np.asarray(a, dtype=np.float64) for a in (rms, cent, voice))
    p50, p90 = np.percentile(rms, 50), np.percentile(rms, 90)
    base = np.maximum(rolling_median(rms), 0.1 * p90 + 1e-6)
    surge = np.maximum(rms - base, 0) / base                      # how much louder than normal
    cbase = np.maximum(rolling_median(cent), 1.0)
    bright = np.maximum(cent - cbase, 0) / cbase                  # sound got higher / sharper
    voice_s = smooth(voice, 5)
    voice_n = np.clip(voice_s / max(np.percentile(voice_s, 90), 1e-6), 0, 1)
    gate = np.clip((rms - p50) / max(p90 - p50, 1e-6), 0, 1)      # must be above usual volume
    raw = 0.65 * normalize(smooth(surge, 3)) + 0.35 * normalize(smooth(bright, 3))
    raw = raw * (0.6 + 0.4 * voice_n) * (0.3 + 0.7 * smooth(gate, 3))
    return normalize(smooth(raw, 5))


def chat_activity(chat_path, length):
    """Count chat messages per second. Reads YouTube chat (yt-dlp .live_chat.json),
    Twitch chat (TwitchDownloader) and chat-downloader files."""
    text = Path(chat_path).read_text(encoding="utf-8")
    times = []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict) and "comments" in data:
        data = data["comments"]
    if isinstance(data, list):  # Twitch or chat-downloader
        for c in data:
            t = c.get("content_offset_seconds", c.get("time_in_seconds"))
            if t is not None:
                times.append(float(t))
    else:  # YouTube live chat from yt-dlp: one JSON object per line
        for line in text.splitlines():
            try:
                r = json.loads(line).get("replayChatItemAction")
            except (json.JSONDecodeError, AttributeError):
                continue
            if r and r.get("videoOffsetTimeMsec") is not None:
                n = sum("addChatItemAction" in a for a in r.get("actions", []))
                times.extend([int(r["videoOffsetTimeMsec"]) / 1000] * n)
    counts = np.zeros(length)
    for t in times:
        if 0 <= int(t) < length:
            counts[int(t)] += 1
    return counts


def smooth(x, window):
    window = min(window, len(x))
    if window == 0:
        return np.asarray(x)
    return np.convolve(x, np.ones(window) / window, mode="same")


def normalize(x):
    hi = np.percentile(x, 99)
    return np.clip(x / hi, 0, 1) if hi > 0 else np.zeros_like(x)


def find_span(score, peak, used, min_len, max_len, pad, max_dip=4):
    """Grow a clip around the peak for as long as the hype lasts (short dips are allowed)."""
    length = len(score)
    thresh = max(0.2, 0.35 * score[peak])

    def grow(step):
        t, last_good, dip = peak, peak, 0
        while 0 <= t + step < length and not used[t + step] and abs(t + step - peak) <= max_len:
            t += step
            if score[t] >= thresh:
                last_good, dip = t, 0
            else:
                dip += 1
                if dip > max_dip:
                    break
        return last_good

    start = max(0, grow(-1) - pad)  # pad = a little setup before and reaction after
    end = min(length, grow(1) + pad)
    if end - start > max_len:  # too long: keep the peak in the first half
        start = max(start, peak - int(0.4 * max_len))
        end = min(length, start + max_len)
    if end - start < min_len:  # too short: widen around the peak
        extra = min_len - (end - start)
        start = max(0, start - extra // 2)
        end = min(length, start + min_len)
        start = max(0, end - min_len)
    return start, end


def pick_clips(score, top, auto, fixed_len, min_len, max_len, pad):
    """Pick the best moments. Returns [(peak_second, score, start, end)] sorted by time."""
    length = len(score)
    used = np.zeros(length, dtype=bool)  # seconds already inside a clip
    clips = []
    for _ in range(top):
        masked = np.where(used, 0, score)
        peak = int(np.argmax(masked))
        if masked[peak] <= 0.1:  # nothing exciting left
            break
        if auto:
            start, end = find_span(score, peak, used, min_len, max_len, pad)
        else:
            start = max(0, peak - fixed_len // 2)
            end = min(length, start + fixed_len)
            start = max(0, end - fixed_len)
        used[max(0, start - 3): end + 3] = True
        clips.append((peak, float(score[peak]), start, end))
    return sorted(clips, key=lambda c: c[2])


def fmt(seconds):
    s = int(seconds)
    return f"{s // 3600:02d}h{s % 3600 // 60:02d}m{s % 60:02d}s"


# ---------- captions (free Whisper model, runs on your PC) ----------

_model = None
_model_key = None
WHISPER = {"device": "auto"}  # auto = NVIDIA GPU when it works, otherwise CPU | cpu = CPU only
_gpu_hint_shown = False


def _add_cuda_dll_dirs():
    """Windows: the pip packages nvidia-cublas-cu12 / nvidia-cudnn-cu12 keep their DLLs in
    site-packages/nvidia/<name>/bin. Make sure the speech engine can find them."""
    if os.name != "nt":
        return
    try:
        import importlib.util
        spec = importlib.util.find_spec("nvidia")
        bases = list(spec.submodule_search_locations or []) if spec else []
    except Exception:
        return
    for base in bases:
        try:
            subs = os.listdir(base)
        except OSError:
            continue
        for sub in subs:
            bindir = os.path.join(base, sub, "bin")
            if os.path.isdir(bindir):
                try:
                    os.add_dll_directory(bindir)
                except (OSError, AttributeError):
                    pass
                if bindir not in os.environ.get("PATH", ""):
                    os.environ["PATH"] = bindir + os.pathsep + os.environ.get("PATH", "")


def _cuda_devices():
    try:
        import ctranslate2
        return int(ctranslate2.get_cuda_device_count())
    except Exception:
        return 0


def _gpu_hint():
    global _gpu_hint_shown
    if not _gpu_hint_shown:
        _gpu_hint_shown = True
        print("      To use your GPU for speech: close the app, double-click "
              "'Install GPU speech support.bat', then press 'Test GPU speech'.")


def get_model(size):
    """The speech model, on the NVIDIA GPU when possible (several times faster), else the CPU."""
    global _model, _model_key
    want = "cpu" if WHISPER["device"] == "cpu" else ("gpu" if _cuda_devices() > 0 else "cpu")
    if _model is not None and _model_key == (size, want):
        return _model
    from faster_whisper import WhisperModel
    _model = None
    if want == "gpu":
        _add_cuda_dll_dirs()
        print(f"Loading caption model '{size}' on your NVIDIA GPU "
              "(the first time it downloads, be patient)...")
        last = None
        for ctype in ("float16", "int8_float16"):  # older cards may not do float16
            try:
                _model = WhisperModel(size, device="cuda", compute_type=ctype)
                break
            except Exception as err:
                last = err
        if _model is None:
            print(f"GPU speech reading is not available ({str(last)[:120]}). Using the CPU instead.")
            _gpu_hint()
            want = "cpu"
    if _model is None:
        print(f"Loading caption model '{size}' on the CPU (the first time it downloads, be patient)...")
        _model = WhisperModel(size, device="cpu", compute_type="int8")
    _model_key = (size, want)
    return _model


def _run_whisper(audio, size, language, prompt):
    """Transcribe; if the GPU breaks halfway (missing NVIDIA libraries), redo it on the CPU."""
    for attempt in (1, 2):
        model = get_model(size)
        try:
            segments, _ = model.transcribe(audio, language=language, word_timestamps=True,
                                           vad_filter=True, initial_prompt=prompt)
            words = []
            for seg in segments:  # the work really happens here, so errors show up here
                for w in seg.words or []:
                    text = w.word.strip().replace("{", "").replace("}", "")
                    if text:
                        words.append((w.start, w.end, text))
            return words
        except Exception as err:
            if attempt == 1 and _model_key and _model_key[1] == "gpu":
                print(f"      GPU speech reading failed ({str(err)[:120]}). "
                      "Switching to the CPU for the rest of this run.")
                _gpu_hint()
                WHISPER["device"] = "cpu"
                continue
            raise


def check_gpu_speech():
    """--check-gpu: say plainly whether the GPU can read speech on this PC."""
    try:
        import ctranslate2
        import faster_whisper
        from faster_whisper import WhisperModel
    except ImportError:
        print("The speech package is missing. Run: python -m pip install faster-whisper")
        return
    print(f"faster-whisper {faster_whisper.__version__}, CTranslate2 {ctranslate2.__version__}")
    n = _cuda_devices()
    if n == 0:
        print("No usable NVIDIA GPU was found (no NVIDIA card, or the driver is too old).\n"
              "Speech reading will use the CPU. Updating the NVIDIA driver may fix this.")
        return
    _add_cuda_dll_dirs()
    print(f"Found {n} NVIDIA GPU(s). Testing a short speech reading on the GPU "
          "(the first time this downloads a small test model)...")
    try:
        t0 = time.time()
        model = WhisperModel("tiny", device="cuda", compute_type="float16")
        segments, _ = model.transcribe(np.zeros(16000 * 5, dtype=np.float32))
        list(segments)
        print(f"GPU speech reading WORKS (test took {time.time() - t0:.1f}s). "
              "The app will use it automatically.")
    except Exception as err:
        print(f"GPU test FAILED: {str(err)[:200]}\n"
              "Fix: close the app and double-click 'Install GPU speech support.bat' "
              "(it adds the NVIDIA libraries), then test again. Until then the CPU is used.")


def transcribe_words(video, start, end, work_dir, size, language):
    """Return [(start, end, word)] for the speech in this clip (times relative to clip)."""
    # Let FFmpeg decode the audio ourselves (16 kHz mono floats) and hand Whisper the raw
    # samples, so it never needs its own audio decoder.
    proc = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(start), "-i", video,
                           "-t", str(end - start), "-vn", "-ac", "1", "-ar", "16000",
                           "-f", "f32le", "-"], capture_output=True, check=True,
                          creationflags=_flags())
    audio = np.frombuffer(proc.stdout, dtype=np.float32)
    if audio.size == 0:
        return []
    prompt = None
    if language == "hinglish":  # Whisper has no Hinglish: use Hindi + a Roman-script hint
        language = "hi"
        prompt = "Bhai ye kya tha yaar, kya clutch tha! Chalo gg, next round dekhte hain."
    return _run_whisper(audio, size, language, prompt)


def ass_time(t):
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def write_ass(words, path, vertical, style):
    """Write a subtitle file. style 1 = big bold 3-word captions, style 2 = small lines."""
    big = style == 1
    per = 3 if big else 7
    width, height = (1080, 1920) if vertical else (1920, 1080)
    size = (84 if vertical else 62) if big else (56 if vertical else 42)
    margin = (520 if vertical else 90) if big else (300 if vertical else 60)
    outline = 6 if big else 3
    text = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {width}\nPlayResY: {height}\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, "
        "Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, "
        "MarginV, Encoding\n"
        f"Style: Default,Arial,{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,"
        f"-1,0,0,0,100,100,0,0,1,{outline},1,2,60,60,{margin},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n")
    for i in range(0, len(words), per):
        group = words[i:i + per]
        line = " ".join(g[2] for g in group)
        if big:
            line = line.upper()
        end = group[-1][1] + 0.4
        if i + per < len(words):
            end = min(end, words[i + per][0])
        text += (f"Dialogue: 0,{ass_time(group[0][0])},{ass_time(end)},Default,,0,0,0,,"
                 f"{line}\n")
    Path(path).write_text(text, encoding="utf-8")


# ---------- cutting ----------

# ---------- face tracking (free OpenCV) ----------

_cascade = None


def video_size(src):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                          "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", src],
                         capture_output=True, text=True, timeout=45).stdout
    m = re.search(r"(\d+)x(\d+)", out)  # take the first size, whatever else is printed
    if not m:
        raise RuntimeError("could not read the video size")
    return int(m.group(1)), int(m.group(2))


def find_faces(src, start, end, fps=2):
    """Look for the streamer's face. Returns (W, H, frames_checked, [(t, cx, cy, face_width)])."""
    global _cascade
    import cv2
    if not hasattr(cv2, "CascadeClassifier"):
        raise RuntimeError('needs OpenCV 4: pip install "opencv-python-headless<5"')
    if _cascade is None:
        _cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    W, H = video_size(src)
    aw = 640
    ah = int(round(H * aw / W / 2)) * 2
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(start), "-i", src, "-t", str(end - start), "-an",
         "-vf", f"fps={fps},scale={aw}:{ah},format=gray", "-f", "rawvideo", "-"],
        capture_output=True, check=True, creationflags=_flags()).stdout
    data = np.frombuffer(raw, dtype=np.uint8)
    n = data.size // (aw * ah)
    frames = data[:n * aw * ah].reshape(n, ah, aw)
    found = []
    for i in range(n):
        rects = _cascade.detectMultiScale(cv2.equalizeHist(frames[i]), scaleFactor=1.1,
                                          minNeighbors=6, minSize=(24, 24))
        if len(rects):
            x, y, w, h = max(rects, key=lambda r: r[2] * r[3])  # the biggest face
            found.append((i / fps, (x + w / 2) * W / aw, (y + h / 2) * H / ah, w * W / aw))
    return W, H, n, found


def steady_faces(found, n, W, H):
    """Keep detections near the usual face position; empty if there is no reliable face."""
    if n == 0 or len(found) < max(3, 0.25 * n):
        return []
    mx, my = np.median([f[1] for f in found]), np.median([f[2] for f in found])
    keep = [f for f in found if abs(f[1] - mx) < 0.3 * W and abs(f[2] - my) < 0.3 * H]
    return keep if len(keep) >= max(3, 0.2 * n) else []


LAYOUT = {"split": 0.58, "zoom": 2.2}  # facecam layout: gameplay share of the height, face zoom


def facecam_graph(W, H, faces):
    """Gameplay on top (centre of the screen, fills the area), face close-up below (1080x1920)."""
    top_h = int(round(1920 * LAYOUT["split"] / 2)) * 2
    panel = 1920 - top_h
    if top_h < 400 or panel < 400:
        return None
    # gameplay: the centre of the frame, cropped to the shape of the top area
    gw = min(W, int(H * 1080 / top_h))
    gh = int(gw * top_h / 1080)
    gw, gh = gw // 2 * 2, gh // 2 * 2
    gx, gy = (W - gw) // 2, (H - gh) // 2
    # face: a crop around the face with the shape of the bottom area (zoom = times the face width)
    cx, cy = float(np.median([f[1] for f in faces])), float(np.median([f[2] for f in faces]))
    cw = float(np.median([f[3] for f in faces])) * LAYOUT["zoom"]
    ch = cw * panel / 1080
    k = min(1.0, W / cw, H / ch)
    cw, ch = int(cw * k // 2 * 2), int(ch * k // 2 * 2)
    x = int(min(max(cx - cw / 2, 0), W - cw))
    y = int(min(max(cy - ch * 0.45, 0), H - ch))
    return (f"[0:v]split=2[a][b];[a]crop={gw}:{gh}:{gx}:{gy},scale=1080:{top_h}[top];"
            f"[b]crop={cw}:{ch}:{x}:{y},scale=1080:{panel}[face];[top][face]vstack=inputs=2")


def track_graph(W, H, faces, dur):
    """A 9:16 crop of the full height that follows the face smoothly."""
    cw = int(H * 9 / 16 // 2 * 2)
    if cw >= W:
        return None
    ts = np.array([f[0] for f in faces])
    xs = np.array([f[1] for f in faces])
    smooth_x = np.convolve(np.pad(xs, 2, mode="edge"), np.ones(5) / 5, mode="valid")
    if smooth_x.max() - smooth_x.min() < 0.06 * W:  # barely moves: hold the crop still
        xexpr = str(int(min(max(np.median(xs) - cw / 2, 0), W - cw)))
    else:  # move between a point every 4 seconds (no jittery swinging)
        keys = np.arange(0, dur + 4, 4.0)
        kx = np.clip(np.interp(keys, ts, smooth_x) - cw / 2, 0, W - cw)
        xexpr = f"{kx[-1]:.1f}"
        for i in range(len(keys) - 1, 0, -1):
            t0, t1, x0, x1 = keys[i - 1], keys[i], kx[i - 1], kx[i]
            xexpr = (f"if(lt(t,{t1:g}),{x0:.1f}+({x1:.1f}-{x0:.1f})*(t-{t0:g})/{t1 - t0:g},"
                     f"{xexpr})")
    return f"[0:v]crop={cw}:{H}:x='{xexpr}':y=0,scale=1080:1920"


def parse_face_box(text):
    """'x,y,w,h' in percent of the screen -> (x, y, w, h) as 0..1 fractions, or None if blank."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        x, y, w, h = [float(v) for v in re.split(r"[,\s;]+", text.replace("%", "")) if v != ""]
    except ValueError:
        raise ValueError("the facecam area must be four numbers: x,y,width,height in percent")
    if w < 5 or h < 5 or x < 0 or y < 0 or x + w > 100.5 or y + h > 100.5:
        raise ValueError("the facecam area must be inside the screen and at least 5% wide/high")
    return x / 100, y / 100, w / 100, h / 100


def facecam_box_graph(W, H, box):
    """Like facecam_graph, but the bottom panel shows exactly the area you chose."""
    top_h = int(round(1920 * LAYOUT["split"] / 2)) * 2
    panel = 1920 - top_h
    if top_h < 400 or panel < 400:
        return None
    gw = min(W, int(H * 1080 / top_h))
    gh = int(gw * top_h / 1080)
    gw, gh = gw // 2 * 2, gh // 2 * 2
    gx, gy = (W - gw) // 2, (H - gh) // 2
    bx, by, bw, bh = box[0] * W, box[1] * H, box[2] * W, box[3] * H
    aspect = 1080 / panel
    cw = max(bw, bh * aspect) * 1.06          # contain the whole box, with a little room
    ch = cw / aspect
    if cw > W:
        cw, ch = W, W / aspect
    if ch > H:
        ch, cw = H, H * aspect
    cw, ch = int(cw // 2 * 2), int(ch // 2 * 2)
    x = int(min(max(bx + bw / 2 - cw / 2, 0), W - cw))
    y = int(min(max(by + bh / 2 - ch / 2, 0), H - ch))
    return (f"[0:v]split=2[a][b];[a]crop={gw}:{gh}:{gx}:{gy},scale=1080:{top_h}[top];"
            f"[b]crop={cw}:{ch}:{x}:{y},scale=1080:{panel}[face];[top][face]vstack=inputs=2")


def face_graph(src, start, end, kind):
    """The FFmpeg picture recipe for the face layouts, or None if no face is found."""
    if kind == "facecam" and LAYOUT.get("box"):  # you chose the area: no detection needed
        try:
            W, H = video_size(src)
            return facecam_box_graph(W, H, LAYOUT["box"])
        except Exception as err:
            print(f"      (facecam area failed: {str(err)[:100]})")
            return None
    try:
        W, H, n, found = find_faces(src, start, end)
    except Exception as err:
        print(f"      (face search failed: {str(err)[:100]})")
        return None
    faces = steady_faces(found, n, W, H)
    if not faces:
        return None
    return facecam_graph(W, H, faces) if kind == "facecam" else track_graph(W, H, faces, end - start)


def cut_clip(video, start, end, out_path, precise=False, gpu=False, vertical=None,
             ass_dir=None):
    video, out_path = str(Path(video).resolve()), str(Path(out_path).resolve())
    sub = "ass=caps.ass" if ass_dir else None  # ffmpeg runs inside ass_dir
    graph = None
    if vertical in ("track", "facecam"):  # these need the streamer's face
        graph = face_graph(video, start, end, vertical)
        if graph is None:
            print("      (no face found in this clip - using the blurred background layout)")
            vertical = "blur"
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-i", video,
           "-t", str(end - start)]
    if vertical == "crop":  # zoom into the centre, fills the whole 9:16 frame
        vf = "crop='trunc(ih*9/16/2)*2':ih,scale=1080:1920"
        cmd += ["-vf", vf + ("," + sub if sub else "")]
    elif vertical == "blur" or graph:  # blurred layout, or one of the face layouts
        graph = graph or ("[0:v]split=2[bg][fg];"  # full gameplay in the middle, blur behind
                          "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
                          "crop=1080:1920,boxblur=20:5[bgb];"
                          "[fg]scale=1080:-2[fgs];"
                          "[bgb][fgs]overlay=(W-w)/2:(H-h)/2")
        graph += ("[o];[o]" + sub + "[v]") if sub else "[v]"
        cmd += ["-filter_complex", graph, "-map", "[v]", "-map", "0:a?"]
    elif sub:
        cmd += ["-vf", sub]
    if precise or vertical or sub:  # re-encode
        if gpu:
            cmd += ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "21"]
        else:
            cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "20"]
        cmd += ["-pix_fmt", "yuv420p", "-c:a", "aac"]
    else:  # copy streams: near-instant, start may snap to a keyframe
        cmd += ["-c", "copy"]
    cmd.append(out_path)
    subprocess.run(cmd, check=True, cwd=ass_dir, creationflags=_flags())


# ---------- live mode (YouTube live streams) ----------

LIVE_MODE = False  # set to True while live mode runs


def _flags():
    """Windows: keep clip-making FFmpeg processes alive when Ctrl+C is pressed in live mode."""
    return getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if LIVE_MODE else 0


SEG = 4          # seconds per recorded segment
CALIBRATE = 90   # seconds spent learning the chat's normal speed before clipping
WIN = 10         # chat speed is measured over the last 10 seconds
PRE = 25         # chat reacts late, so every clip starts this many seconds earlier
POST = 12        # extra seconds after the hype ends (the video feed lags behind chat)
CALM = 10        # the hype is over once chat stays calm for this long
KEEP = 1200      # keep the last 20 minutes of recording on disk
# (how many "usual jumps" above normal, minimum ratio over normal, minimum messages)
SENS = {1: (3.0, 1.5, 10), 2: (2.0, 1.3, 6), 3: (1.5, 1.2, 5)}


class ChatMeter:
    """Counts live chat messages (filled by a background thread)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.times = collections.deque()
        self.start = time.time()
        self.total, self.error = 0, None
        self.samples = collections.deque()  # (time, chat speed) taken by the main loop

    def add(self):
        with self.lock:
            self.times.append(time.time())
            self.total += 1

    def rate(self, now):
        with self.lock:
            while self.times and self.times[0] < now - 900:
                self.times.popleft()
            return sum(1 for t in self.times if t > now - WIN)

    def sample(self, now, rate):
        self.samples.append((now, rate))
        while self.samples and self.samples[0][0] < now - 300:
            self.samples.popleft()

    def spread(self, now):
        """How much chat speed usually jumps around (standard deviation, last 5 minutes)."""
        vals = [r for t, r in self.samples if t <= now - WIN]
        return float(np.std(vals)) if len(vals) >= 10 else 0.0

    def normal(self, now):
        """Typical messages per WIN seconds over the last 5 minutes (before right now)."""
        span = min(300, now - self.start) - WIN
        if span < WIN:
            return 0.0
        with self.lock:
            n = sum(1 for t in self.times if now - 300 < t <= now - WIN)
        return n / span * WIN


def video_id(url):
    m = re.search(r"(?:v=|/live/|youtu\.be/|/embed/|/shorts/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else None


def platform_of(url):
    u = url.lower()
    if "twitch.tv" in u:
        return "twitch"
    if "kick.com" in u:
        return "kick"
    return "youtube"


def channel_of(url):
    """Channel name from a Twitch or Kick link."""
    m = re.search(r"(?:twitch\.tv|kick\.com)/(?:popout/|embed/)?([A-Za-z0-9_]+)", url)
    if m and m.group(1).lower() not in ("videos", "video", "directory", "categories", "clips"):
        return m.group(1).lower()
    return None


def stream_key(url):
    """A short unique id for a stream link (None if it isn't a usable live link)."""
    plat = platform_of(url)
    if plat == "youtube":
        return video_id(url)
    ch = channel_of(url)
    return f"{plat}_{ch}" if ch else None


def watch_link(url):
    """Turn any YouTube / Twitch / Kick link style into the plain link the tools understand."""
    plat = platform_of(url)
    if plat != "youtube":
        ch = channel_of(url)
        if not ch:
            return url
        return f"https://www.twitch.tv/{ch}" if plat == "twitch" else f"https://kick.com/{ch}"
    vid = video_id(url)
    return f"https://www.youtube.com/watch?v={vid}" if vid else url


def youtube_chat_reader(url, meter):
    """Read YouTube live chat. Tries chat-downloader first, then pytchat as a backup."""
    errors = []
    try:
        from chat_downloader import ChatDownloader
        for _ in ChatDownloader().get_chat(url):
            meter.add()
        return
    except Exception as err:
        errors.append(f"chat-downloader: {err}")
    try:
        import pytchat
        chat = pytchat.create(video_id=video_id(url), interruptable=False)
        while chat.is_alive():
            for _ in chat.get().sync_items():
                meter.add()
            time.sleep(1)
        return
    except Exception as err:
        errors.append(f"pytchat: {err}")
    meter.error = " | ".join(errors)


def twitch_chat(channel, meter, host="irc.chat.twitch.tv", port=6697, use_ssl=True):
    """Read Twitch chat anonymously over IRC (no login needed). Reconnects if it drops."""
    import socket
    import ssl
    while True:
        try:
            sock = socket.create_connection((host, port), timeout=360)
            if use_ssl:
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
            nick = f"justinfan{random.randint(10000, 99999)}"
            sock.sendall(f"PASS SCHMOOPIIE\r\nNICK {nick}\r\nJOIN #{channel}\r\n".encode())
            meter.error = None
            buf = ""
            while True:
                data = sock.recv(4096)
                if not data:
                    raise ConnectionError("Twitch closed the chat connection")
                buf += data.decode("utf-8", "ignore")
                *lines, buf = buf.split("\r\n")
                for line in lines:
                    if line.startswith("PING"):
                        sock.sendall(b"PONG :tmi.twitch.tv\r\n")
                    elif " PRIVMSG " in line:
                        meter.add()
        except Exception as err:
            meter.error = f"twitch chat: {err}"
            time.sleep(5)


KICK_FALLBACK = ("34bf7a0ff419a2a775b9", "us2")  # last key Kick was known to use


def _find_text(obj, name):
    """Find a text value called `name` anywhere inside parsed JSON."""
    if isinstance(obj, dict):
        if isinstance(obj.get(name), str):
            return obj[name]
        obj = list(obj.values())
    if isinstance(obj, list):
        for item in obj:
            found = _find_text(item, name)
            if found:
                return found
    return None


def kick_pusher_url():
    """Ask Kick which chat server key to use (Kick changes it), like kick.com itself does."""
    key, cluster = KICK_FALLBACK
    try:
        body = json.dumps({"client": {"id": str(uuid.uuid4()), "type": "web"},
                           "capabilities": {"accepted_providers": [{"provider": "pusher"}]}})
        out = subprocess.run(
            ["curl", "-s", "-X", "POST", "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
             "-H", "Content-Type: application/json", "-H", "Accept: application/json",
             "--data-binary", "@-", "https://web.kick.com/api/v1/realtime/connection"],
            input=body, capture_output=True, text=True, timeout=30).stdout
        data = json.loads(out)
        new_key, new_cluster = _find_text(data, "app_key"), _find_text(data, "cluster")
        if (new_key and re.fullmatch(r"[0-9a-f]{20}", new_key)
                and new_cluster and re.fullmatch(r"[a-z0-9]{2,8}", new_cluster)):
            key, cluster = new_key, new_cluster
    except Exception:
        pass  # keep the last known key
    return f"wss://ws-{cluster}.pusher.com/app/{key}?protocol=7&client=js&version=8.4.0&flash=false"


def kick_chatroom_id(channel):
    """Kick's chat room number. Uses curl, which Windows includes."""
    return kick_channel_info(channel)[1]


def kick_channel_info(channel):
    """Return Kick's numeric channel and chatroom IDs for a channel slug."""
    try:
        out = subprocess.run(
            ["curl", "-s", "-L", "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
             "-H", "Accept: application/json", f"https://kick.com/api/v2/channels/{channel}"],
            capture_output=True, text=True, timeout=30).stdout
        data = json.loads(out)
        return int(data["id"]), int(data["chatroom"]["id"])
    except Exception:
        raise RuntimeError(
            "Kick would not give the channel/chat room number. Check that the channel URL is "
            f"correct and live: https://kick.com/{channel}")


def kick_realtime_post(path, body):
    """Call Kick's public realtime negotiation endpoints as a server-side client."""
    raw = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        "https://web.kick.com/api/v1/realtime/" + path.lstrip("/"), data=raw,
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "Origin": "https://kick.com", "User-Agent": "Mozilla/5.0"},
        method="POST")
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def kick_centrifugo_connection(channel_id=None):
    """Negotiate Kick's current Centrifugo URL and an anonymous 45-minute connection token."""
    client_id = str(uuid.uuid4())
    request = {"client": {"id": client_id, "type": "web"}, "capabilities": {
        "accepted_providers": [{"provider": "centrifugo"}, {"provider": "pusher"}]}}
    endpoint = (f"channels/{channel_id}/chat/connection" if channel_id else "connection")
    offered = kick_realtime_post(endpoint, request).get("data", {}).get("connections", [])
    selected = next((item for item in offered if item.get("provider") == "centrifugo"), None)
    if not selected:
        raise RuntimeError("Kick did not offer its Centrifugo chat connection.")
    url = selected.get("credentials", {}).get("url")
    token = kick_realtime_post("auth/connection", {"client_id": client_id}).get("data", {}).get("token")
    if not url or not token:
        raise RuntimeError("Kick did not provide the chat connection details.")
    return url, token


def kick_chat_event(frame):
    """Return a Kick event name from a Centrifugo publication, if this frame is one."""
    push = frame.get("push", {}) if isinstance(frame, dict) else {}
    pub = push.get("pub", {}) if isinstance(push, dict) else {}
    data = pub.get("data", {}) if isinstance(pub, dict) else {}
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            data = {}
    tags = pub.get("tags", {}) if isinstance(pub, dict) else {}
    return data.get("event") or tags.get("event") or ""


def kick_chat(channel, meter, chatroom=None, url=None):
    """Read Kick chat through its current Centrifugo WebSocket and reconnect if it drops."""
    try:
        import websocket
    except ImportError:
        raise RuntimeError("Kick chat needs: py -3.12 -m pip install websocket-client")
    while True:
        try:
            if chatroom:
                room, channel_id = int(chatroom), None
            else:
                channel_id, room = kick_channel_info(channel)
            endpoint, token = (url, None) if url else kick_centrifugo_connection(channel_id)
            if not endpoint:
                raise RuntimeError("Kick did not provide a chat WebSocket address.")
            ws = websocket.create_connection(
                endpoint, timeout=60, origin="https://kick.com",
                header=["User-Agent: Mozilla/5.0"])
            if token:
                ws.send(json.dumps({"id": 1, "connect": {"token": token, "name": "highlight-clipper"}}))
                connected = json.loads(ws.recv())
                if connected.get("error"):
                    raise RuntimeError(f"Kick chat connect refused: {connected['error']}")
            ws.send(json.dumps({"id": 2, "subscribe": {"channel": f"chatrooms.{room}.v2"}}))
            while True:
                raw = ws.recv()
                if not raw:  # the server closed the connection
                    raise ConnectionError("Kick closed the chat connection")
                # Centrifugo may put multiple JSON messages in the same WebSocket frame.
                for line in raw.splitlines():
                    msg = json.loads(line)
                    if msg == {}:  # Centrifugo heartbeat: reply with its required pong frame
                        ws.send("{}")
                        continue
                    if msg.get("error"):
                        raise RuntimeError(f"Kick chat error: {msg['error']}")
                    if kick_chat_event(msg).endswith("ChatMessageEvent"):
                        meter.add()
                meter.error = None
        except Exception as err:
            meter.error = f"kick chat: {err}"
            time.sleep(5)


def chat_reader(url, meter, kick_room=None):
    """Read live chat for whichever platform the link is from."""
    plat = platform_of(url)
    try:
        if plat == "twitch":
            twitch_chat(channel_of(url), meter)
        elif plat == "kick":
            kick_chat(channel_of(url), meter, kick_room)
        else:
            youtube_chat_reader(url, meter)
    except Exception as err:
        meter.error = str(err)


class Recorder:
    """Records the stream into small rolling .ts segments with FFmpeg."""

    def __init__(self, stream_urls, folder):
        self.folder = Path(folder)
        self.csv = self.folder / "segments.csv"
        self.t0 = time.time()
        cmd = ["ffmpeg", "-v", "error"]
        for url in stream_urls:
            if url.startswith("http"):
                cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
            cmd += ["-i", url]
        if len(stream_urls) > 1:  # separate video and audio feeds
            cmd += ["-map", "0:v:0", "-map", "1:a:0"]
        cmd += ["-c", "copy", "-f", "segment", "-segment_time", str(SEG),
                "-reset_timestamps", "1", "-segment_format", "mpegts",
                "-segment_list", str(self.csv), "-segment_list_type", "csv",
                str(self.folder / "seg_%05d.ts")]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    def segments(self):
        segs = []
        if self.csv.is_file():
            for line in self.csv.read_text().splitlines():
                parts = line.split(",")
                if len(parts) == 3:
                    segs.append((parts[0], float(parts[1]), float(parts[2])))
        return segs

    def recorded(self):
        segs = self.segments()
        return segs[-1][2] - segs[0][1] if segs else 0.0

    def cleanup(self):
        segs = self.segments()
        for name, _, end in segs:
            if segs[-1][2] - end > KEEP:
                try:
                    (self.folder / name).unlink()
                except OSError:
                    pass

    def stop(self):
        try:
            self.proc.stdin.write(b"q")
            self.proc.stdin.flush()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.terminate()


def build_clip_source(rec, start, end, tmp):
    """Join the recorded segments covering [start, end] into one file."""
    segs = rec.segments()
    if not segs:
        return None
    base = segs[0][1]
    chosen = [s for s in segs if s[2] - base > start and s[1] - base < end
              and (rec.folder / s[0]).is_file()]
    if not chosen:
        return None
    lst = Path(tmp) / "list.txt"
    lst.write_text("".join(f"file '{(rec.folder / s[0]).resolve().as_posix()}'\n" for s in chosen))
    src = Path(tmp) / "src.ts"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
                    "-i", str(lst), "-c", "copy", str(src)], check=True,
                   creationflags=_flags())
    if not has_video(str(src)):
        raise RuntimeError("the recording has no video picture (audio only)")
    offset = chosen[0][1] - base
    total = chosen[-1][2] - base - offset
    return str(src), max(0, start - offset), min(end - offset, total)


def clip_worker(jobs, rec, args, out_dir):
    """Makes clips in the background so the stream keeps being watched."""
    existing = []
    for clip in Path(out_dir).glob("live_*.mp4"):
        match = re.match(r"live_(\d+)_", clip.name, re.I)
        if match:
            existing.append(int(match.group(1)))
    n = max(existing, default=0)
    while True:
        job = jobs.get()
        if job is None:
            return
        peak, start, end = job
        n += 1
        out_path = out_dir / f"live_{n:02d}_at_{fmt(peak)}.mp4"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                made = build_clip_source(rec, start, end, tmp)
                if not made:
                    print("\n  Skipped a clip: that part was not recorded.")
                    continue
                src, s, e = made
                ass_dir = None
                transcript = ""
                if args.captions or args.auto_metadata or getattr(args, "ai", None):
                    try:
                        words = transcribe_words(src, s, e, tmp, args.model, args.language)
                        transcript = " ".join(w[2] for w in words)
                        if args.captions and words:
                            write_ass(words, Path(tmp) / "caps.ass", bool(args.vertical),
                                      args.caption_style)
                            ass_dir = tmp
                    except Exception as err:
                        what = "Captions" if args.captions else "Speech reading for title/tags"
                        print(f"\n  {what} failed ({str(err)[:120]}). Making the clip without it.")
                try:
                    cut_clip(src, s, e, out_path, args.precise, args.gpu, args.vertical, ass_dir)
                except Exception as err:  # never lose the moment: save a plain clip instead
                    print(f"\n  Fancy version failed ({str(err)[:200]}).\n"
                          "  Saving a plain clip instead.")
                    out_path.unlink(missing_ok=True)
                    cut_clip(src, s, e, out_path)
            print(f"\n  Saved {out_path.name} ({int(e - s)}s)")
            meta = ai_metadata(args, out_path, transcript)  # AI writes title, tags, description first
            upload_if_wanted(args, out_path, meta, transcript)
        except Exception as err:
            out_path.unlink(missing_ok=True)  # no empty 0-byte files left behind
            print(f"\n  Could not make a clip: {err}")


def has_video(src):
    """True if the file or stream has a video picture (not just audio)."""
    if not shutil.which("ffprobe"):
        return True
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                              "-show_entries", "stream=codec_type", "-of", "csv=p=0", src],
                             capture_output=True, text=True, timeout=45).stdout
    except subprocess.TimeoutExpired:
        return True
    return "video" in out


def streamlink_urls(url, height):
    """Twitch / Kick: ask streamlink (handles Kick's protection) for the live stream address."""
    cmd = [sys.executable, "-m", "streamlink_cli", "--stream-sorting-excludes", f">{height}p60",
           "--stream-url", url, "best"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        return None, "streamlink took too long"
    found = res.stdout.split()
    if res.returncode == 0 and found and has_video(found[0]):
        return found[:1], ""
    return None, (res.stderr.strip() or "streamlink found no video picture")[-300:]


def hype_job(state, t0, min_len, max_len):
    start = max(0, state["start"] - t0 - PRE)
    end = state["last_high"] - t0 + POST
    end = min(max(end, start + min_len), start + max_len)
    return (state["peak_t"] - t0, start, end)


def resolve_live_stream_urls(url, height):
    """Return fresh playable stream URLs, or an explanatory error when the channel is offline."""
    plat = platform_of(url)
    stream_urls, last_err = None, ""
    if plat != "youtube":  # Twitch and Kick: streamlink handles their protection first
        stream_urls, last_err = streamlink_urls(url, height)
        choices = () if stream_urls else (f"best[height<={height}]/best", None)
    else:
        choices = (f"bv*[vcodec^=avc1][height<={height}][protocol^=m3u8]+ba[protocol^=m3u8]",
                   f"bv*[vcodec^=avc1][height<={height}]+ba[acodec^=mp4a]",
                   f"best[height<={height}]/best", "bv*[vcodec^=avc1]+ba/b", None)
    for choice in choices:
        cmd = [sys.executable, "-m", "yt_dlp", "-g", "--no-playlist"]
        if choice:
            cmd += ["-f", choice]
        res = subprocess.run(cmd + [url], capture_output=True, text=True)
        urls = res.stdout.split()[:2] if res.returncode == 0 else []
        if len(urls) == 2 and not has_video(urls[0]) and has_video(urls[1]):
            urls.reverse()
        if urls and has_video(urls[0]):
            return urls, ""
        last_err = res.stderr.strip() or "that format has no video picture, only audio"
    return stream_urls, last_err


def run_live(args, meter=None):
    global LIVE_MODE
    LIVE_MODE = True
    plat = platform_of(args.live)
    try:
        if plat == "youtube":
            import yt_dlp  # noqa: F401
            import chat_downloader  # noqa: F401
        else:
            import streamlink  # noqa: F401
            if plat == "kick":
                import websocket  # noqa: F401
    except ImportError:
        sys.exit({"youtube": "Live mode needs two free packages. Run: "
                             "py -3.12 -m pip install yt-dlp chat-downloader",
                  "twitch": "Twitch needs a free package. Run: py -3.12 -m pip install streamlink",
                  "kick": "Kick needs two free packages. Run: "
                          "py -3.12 -m pip install streamlink websocket-client"}[plat])
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    cleanup_old_clips(out_dir, args.keep_days)
    k, floor, min_msgs = SENS[args.sensitivity]
    args.live = watch_link(args.live)

    print("\nWatching for the stream. Press Ctrl+C to stop completely.")
    stop_file = os.environ.get("HC_STOP_FILE")
    while True:
        if stop_file and Path(stop_file).exists():
            print("Stopped before the stream came online.")
            return
        stream_urls, last_err = resolve_live_stream_urls(args.live, args.max_height)
        if stream_urls:
            break
        print("  Stream is offline or reconnecting; trying again in 30 seconds..."
              + (f" ({last_err[-120:]})" if last_err else ""))
        try:
            for _ in range(30):
                if stop_file and Path(stop_file).exists():
                    break
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nStopped before the stream came online.")
            return

    if meter is None:
        meter = ChatMeter()
        threading.Thread(target=chat_reader, args=(args.live, meter, args.kick_chatroom),
                         daemon=True).start()
    rec = Recorder(stream_urls, tempfile.mkdtemp(prefix="hc_live_"))
    jobs = queue.Queue()
    worker = threading.Thread(target=clip_worker, args=(jobs, rec, args, out_dir))
    worker.start()
    global _watcher_started
    if getattr(args, "upload", False) and not _watcher_started:
        _watcher_started = True
        threading.Thread(target=queue_watcher, args=(args,), daemon=True).start()

    if args.live_test:
        print("TEST MODE: recording 40 seconds, then making one clip from it...")
        deadline = time.time() + 150
        try:
            while rec.recorded() < 40 and rec.proc.poll() is None and time.time() < deadline:
                time.sleep(2)
                print(f"\r  recorded {int(rec.recorded())}s", end="", flush=True)
        except KeyboardInterrupt:
            pass
        got = rec.recorded()
        print(f"\n  recorded {int(got)}s. Making the clip...")
        if got > 5:
            jobs.put((got, max(0, got - 30), got))
        else:
            print("  Nothing was recorded. Is the stream live?")
        jobs.put(None)
        worker.join()
        rec.stop()
        shutil.rmtree(rec.folder, ignore_errors=True)
        print(f"\nTest finished. Look in: {out_dir}")
        return

    print(f"Recording and watching chat. Learning normal chat speed for {CALIBRATE}s first.")
    print("Press Ctrl+C to stop.\n")
    state, pending, queued, warned, peak_seen = None, [], 0, False, 0
    stop_requested = False
    disk_low = False
    try:
        while rec.proc.poll() is None:
            time.sleep(2)
            if stop_file and Path(stop_file).exists():
                stop_requested = True
                break
            free_bytes = shutil.disk_usage(rec.folder).free
            if free_bytes < args.min_free_gb * 1024 ** 3:
                disk_low = True
                print(f"\n  Stopping safely: only {free_bytes / 1024 ** 3:.2f} GB is free on the "
                      f"recording drive. Use --buffer-dir on a drive with more space.")
                break
            now = time.time()
            rate, normal = meter.rate(now), meter.normal(now)
            trigger = max(normal * floor, normal + k * meter.spread(now), min_msgs)
            meter.sample(now, rate)
            elapsed = now - rec.t0
            if elapsed >= CALIBRATE:
                peak_seen = max(peak_seen, rate)
            if elapsed >= CALIBRATE:
                if state is None and rate >= trigger:
                    state = {"start": now, "peak": rate, "peak_t": now, "last_high": now,
                             "trigger": trigger}
                    print(f"\n  Hype detected at {fmt(elapsed)}! chat {rate}/{WIN}s (normal ~{normal:.0f})")
                elif state:
                    if rate >= state["peak"]:
                        state["peak"], state["peak_t"] = rate, now
                    if rate >= state["trigger"] * 0.5:
                        state["last_high"] = now
                    too_long = now - state["start"] + PRE + POST >= args.max_len
                    if now - state["last_high"] >= CALM or too_long:
                        pending.append(hype_job(state, rec.t0, args.min_len, args.max_len))
                        state = None
            have = rec.recorded()
            for job in pending[:]:
                if have >= job[2]:  # the whole clip has been recorded
                    jobs.put(job)
                    pending.remove(job)
                    queued += 1
            rec.cleanup()
            if elapsed > 120 and not meter.total and not warned:
                warned = True
                print("\n  No chat messages received yet. Is chat visible on the stream page?"
                      + (f" ({meter.error})" if meter.error else ""))
            chat_txt = ("chat OFF" if meter.error
                        else f"chat {rate:>3}/{WIN}s (needs {trigger:.0f})")
            print(f"\r  Watching | {chat_txt} | normal {normal:4.1f} | top {peak_seen} | "
                  f"recorded {fmt(have)} | clips queued {queued}", end="   ", flush=True)
    except KeyboardInterrupt:
        stop_requested = True
    print("\n\nStopping and finishing the last clips...")
    print(f"  (highest chat speed seen: {peak_seen}/{WIN}s, normal ~{meter.normal(time.time()):.0f})")
    if state:
        pending.append(hype_job(state, rec.t0, args.min_len, args.max_len))
    rec.stop()
    if rec.recorded() == 0:
        print("Nothing was recorded. Is the link a live stream?")
    for job in pending:
        jobs.put(job)
    jobs.put(None)
    worker.join()
    shutil.rmtree(rec.folder, ignore_errors=True)
    if disk_low:
        print(f"Stopped to protect disk space. Clips are in the '{out_dir}' folder.")
        return
    if stop_requested:
        print(f"Stopped. Clips are in the '{out_dir}' folder.")
        return
    print("\nThe video feed ended or disconnected. Keeping the watcher active; it will reconnect "
          "automatically when the channel is live again. Press Ctrl+C to stop.")
    try:
        time.sleep(15)
    except KeyboardInterrupt:
        print(f"\nStopped. Clips are in the '{out_dir}' folder.")
        return
    return run_live(args, meter)


# ---------- AI titles, descriptions and tags ----------

AI_PRESETS = {  # (address, model). Both talk the same standard "chat" format.
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-3.6-flash"),
    "ollama": ("http://localhost:11434/v1", "qwen2.5vl:7b"),
}
YT_API = "https://www.googleapis.com/youtube/v3"
APP_DIR = Path(__file__).resolve().parent


def read_secret(env_name, file_name):
    """A key from an environment variable, or from a small text file next to the app."""
    value = os.environ.get(env_name, "").strip()
    path = APP_DIR / file_name
    if not value and path.is_file():
        value = path.read_text(encoding="utf-8").strip()
    return value


def media_duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True,
                         timeout=45).stdout
    m = re.search(r"\d+(\.\d+)?", out)
    return float(m.group(0)) if m else 0.0


def sample_frames(clip, count=6):
    """Pictures spread evenly through the clip, ready to send to the AI."""
    dur = media_duration(clip)
    frames = []
    for i in range(count):
        img = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{dur * (i + 0.5) / count:.2f}", "-i", str(clip),
             "-frames:v", "1", "-vf", "scale=512:-2", "-q:v", "5", "-f", "image2pipe",
             "-vcodec", "mjpeg", "-"], capture_output=True, creationflags=_flags()).stdout
        if img:
            frames.append(base64.b64encode(img).decode())
    return frames


class AIError(RuntimeError):
    def __init__(self, message, code=None, timeout=False):
        super().__init__(message)
        self.code = code
        self.timeout = timeout


_model_cache = {}
_no_json_mode = {}  # services that rejected response_format


def _chat_once(base, key, model, prompt, frames):
    content = [{"type": "text", "text": prompt}] + [
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b}} for b in frames]
    payload = {"model": model, "temperature": 0.7,
               "messages": [{"role": "user", "content": content}]}
    if not _no_json_mode.get(base):
        payload["response_format"] = {"type": "json_object"}
    body = json.dumps(payload).encode()
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.load(resp)["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as err:
        detail = err.read()[:300].decode("utf-8", "ignore")
        if err.code == 400 and "response_format" in detail and not _no_json_mode.get(base):
            _no_json_mode[base] = True  # this service cannot force JSON: ask again without it
            return _chat_once(base, key, model, prompt, frames)
        raise AIError(f"the AI service said {err.code}: {detail[:200]}", err.code)
    except (TimeoutError, urllib.error.URLError, ConnectionError, OSError) as err:
        slow = isinstance(err, TimeoutError) or "timed out" in str(err).lower()
        raise AIError("the AI service did not answer in time" if slow
                      else f"could not reach the AI service ({str(err)[:80]})", 504, timeout=slow)


def _chat_with_retry(base, key, model, prompt, frames):
    """Retry only temporary AI service outages before giving up on metadata."""
    delays = (4, 8, 16)
    for attempt, delay in enumerate(delays, 1):
        try:
            return _chat_once(base, key, model, prompt, frames)
        except AIError as err:
            if err.code not in (429, 500, 502, 503, 504) or err.timeout:
                raise  # a slow answer is not retried on the same model: switch model instead
            print(f"      AI temporarily unavailable ({err.code}); retrying in {delay}s "
                  f"({attempt}/{len(delays)})...")
            time.sleep(delay)
    return _chat_once(base, key, model, prompt, frames)


def rank_gemini_models(model_ids):
    """Regular 'flash' Gemini models, newest first (names change as Google retires old ones)."""
    ranked = []
    for mid in model_ids:
        name = mid.split("/")[-1]
        m = re.fullmatch(r"gemini-(\d+(?:\.\d+)?)-flash(-preview(?:-[\d-]+)?)?", name)
        if m:
            ranked.append(((float(m.group(1)), 0 if m.group(2) else 1), name))
    return [name for _, name in sorted(ranked, reverse=True)]


def list_models(base, key):
    req = urllib.request.Request(base.rstrip("/") + "/models",
                                 headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return [m["id"] for m in json.load(resp).get("data", [])]


_BUSY = (429, 500, 502, 503, 504)


def ai_chat(args, prompt, frames):
    base, default = AI_PRESETS[args.ai]
    base = getattr(args, "ai_url", None) or base
    fixed = getattr(args, "ai_model", None)
    key = read_secret("GEMINI_API_KEY", "ai_key.txt") if args.ai == "gemini" else "ollama"
    model = fixed or _model_cache.get(args.ai) or default
    try:
        reply = _chat_with_retry(base, key, model, prompt, frames)
    except AIError as err:
        # model retired (400/404) or overloaded (503 etc.): try other Gemini models
        if err.code not in (400, 404) + _BUSY or fixed or args.ai != "gemini":
            raise
        reply = None
        try:
            others = [m for m in rank_gemini_models(list_models(base, key)) if m != model][:4]
        except Exception:
            others = []
        for extra in ("gemini-flash-lite-latest", "gemini-2.5-flash", "gemini-2.0-flash"):
            if extra != model and extra not in others:
                others.append(extra)
        busy_fails = 0
        for other in others[:5]:
            try:
                reply = _chat_once(base, key, other, prompt, frames)
            except AIError as err2:
                if err2.code not in (400, 404) + _BUSY:
                    raise
                busy_fails += err2.code in _BUSY
                if busy_fails >= 3:  # Google is clearly struggling: stop wasting minutes
                    break
                continue
            print(f"      (switched the AI model to {other})")
            model = other
            break
        if reply is None:
            raise err
    _model_cache[args.ai] = model
    return reply


def parse_json_block(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("the AI did not answer in the expected format")
    return json.loads(m.group(0))


def trending_examples(topic, key):
    """Popular recent short videos about the topic: (titles, most used tags)."""
    since = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def get(path, **params):
        url = f"{YT_API}/{path}?" + urllib.parse.urlencode({"key": key, **params})
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.load(resp)

    found = get("search", part="snippet", type="video", videoDuration="short", order="viewCount",
                maxResults=10, publishedAfter=since, q=f"{topic} #shorts")
    ids = ",".join(i["id"]["videoId"] for i in found.get("items", []))
    if not ids:
        return [], []
    vids = get("videos", part="snippet", id=ids).get("items", [])
    tags = collections.Counter(t.lower() for v in vids for t in v["snippet"].get("tags", []))
    return [v["snippet"]["title"] for v in vids][:8], [t for t, _ in tags.most_common(15)]


_BAD_TITLES = re.compile(
    r"^(insane|crazy|wow|omg|epic|unbelievable|amazing|best|funny|stream highlight|highlight|"
    r"clip|gameplay|gaming)[\s!.?:-]*(clutch|moment|clip|play|gameplay|highlight)?[\s!.?]*$", re.I)
_recent_titles = []  # titles already made in this run, so clips don't all sound the same
_FILLER_TAGS = {"gaming", "video", "youtube", "shorts", "short", "viral", "fyp", "trending",
                "funny", "highlights", "clip", "clips", "gameplay"}


def _creator(args):
    """Creator name: the Credit field, else the Title field (unless it is the default)."""
    name = (getattr(args, "credit", "") or "").strip()
    if not name:
        t = (getattr(args, "title", "") or "").replace("{n}", "").strip(" -:|")
        if t and t.lower() != "stream highlight":
            name = t
    return name


def _title_ok(title):
    t = title.strip()
    if not (18 <= len(t) <= 70) or _BAD_TITLES.match(t):
        return False
    letters = [c for c in t if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.6:  # SHOUTING TITLES
        return False
    if t.casefold() in (x.casefold() for x in _recent_titles):
        return False
    return True


def _pick_title(raw):
    """Choose the best of the AI's candidate titles instead of trusting a single guess."""
    def plain(x):
        return re.sub(r"[<>]", "", re.sub(r"<[^>]*>", "", str(x))).strip().strip('"\'')
    cands = [plain(t) for t in (raw.get("titles") or []) if str(t).strip()]
    if raw.get("title"):
        cands.append(plain(raw["title"]))
    cands = [re.sub(r"\s*#shorts\s*$", "", c, flags=re.I).strip() for c in cands]
    for c in cands:
        if _title_ok(c):
            return c[:100]
    for c in cands:  # nothing perfect: take the first usable one rather than failing
        if c:
            return c[:100]
    return ""


def _smart_tags(raw_tags, args, topic=""):
    """Specific tags first, the game name guaranteed, generic filler last and limited."""
    specific, generic, seen = [], [], set()
    game = (getattr(args, "game", "") or "").strip()
    creator = _creator(args)
    for tag in ([game] if game else []) + ([creator] if creator else []) + list(raw_tags or []):
        tag = re.sub(r"[<>,#]", "", str(tag)).strip()[:60]
        key = tag.casefold()
        if not tag or key in seen:
            continue
        seen.add(key)
        (generic if key in _FILLER_TAGS else specific).append(tag)
    return (specific + generic[:3])[:15]


def clean_meta(raw, args):
    """Make the AI's answer safe for YouTube (length limits, no angle brackets)."""
    def plain(text):  # remove HTML-like tags first, then any stray angle brackets
        return re.sub(r"[<>]", "", re.sub(r"<[^>]*>", "", str(text))).strip()

    title = _pick_title(raw)
    if not title:
        raise ValueError("the AI gave no title")
    desc = re.sub(r"[ \t]*#\w+", "", plain(raw.get("description", ""))).strip()
    creator = _creator(args)
    if creator:
        if creator.casefold() not in title.casefold() and len(title) + 3 + len(creator) <= 70:
            title = f"{title} | {creator}"
        if creator.casefold() not in desc.casefold():
            desc = (desc + f"\n\nCreator: {creator}").strip()
    tags, seen, total = [], set(), 0
    for tag in _smart_tags(raw.get("tags"), args, raw.get("topic", "")):
        if tag.lower() not in seen and total + len(tag) < 400:
            seen.add(tag.lower())
            tags.append(tag)
            total += len(tag) + 1
    if getattr(args, "live", None) and args.live not in desc:
        desc += f"\n\nClipped from: {args.live}"
    return {"title": title, "description": desc, "tags": tags,
            "hashtags": [h for h in (raw.get("hashtags") or []) if str(h).strip()]}


# ---------- channel coach: learn from your own YouTube numbers ----------

ANALYTICS_TOKEN = APP_DIR / "youtube_analytics_token.json"
ANALYTICS_SCOPES = ["https://www.googleapis.com/auth/yt-analytics.readonly",
                    "https://www.googleapis.com/auth/youtube.readonly"]
COACH_REPORT = APP_DIR / "channel_coach_report.md"
TITLE_LESSONS = APP_DIR / "title_lessons.json"
_EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")


class CoachError(Exception):
    """A problem the user can fix; the message says how."""


def _coach_http_problem(err):
    text = str(err).lower()
    if "accessnotconfigured" in text or "has not been used" in text or "is disabled" in text:
        return CoachError("The 'YouTube Analytics API' is not switched on for your Google Cloud "
                          "project. Open console.cloud.google.com > APIs & Services > Library, "
                          "search 'YouTube Analytics API', press Enable (same project as your "
                          "upload setup), wait a minute and try again.")
    if "insufficient" in text or "forbidden" in text or "403" in text:
        return CoachError("YouTube did not allow reading analytics for this account. Delete "
                          "youtube_analytics_token.json and run again, and approve the "
                          "'View YouTube Analytics reports' permission.")
    return None


def get_analytics_services():
    """Sign in (own token file, so uploading is never affected) -> (data API, analytics API)."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        raise CoachError("A package is missing. Run: python -m pip install google-api-python-client "
                         "google-auth-oauthlib")
    creds = None
    if ANALYTICS_TOKEN.is_file():
        try:
            saved = json.loads(ANALYTICS_TOKEN.read_text(encoding="utf-8"))
            if set(ANALYTICS_SCOPES).issubset(set(saved.get("scopes", []))):
                creds = Credentials.from_authorized_user_file(str(ANALYTICS_TOKEN), ANALYTICS_SCOPES)
        except (OSError, ValueError, TypeError):
            creds = None
    if creds and not creds.valid and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:
            creds = None
    if not creds or not creds.valid:
        secrets = APP_DIR / "client_secret.json"
        if not secrets.is_file():
            raise CoachError("client_secret.json not found. Use the same Google setup as for "
                             "uploading (see README.md).")
        print("A browser window opens: sign in and allow 'View YouTube Analytics reports'.")
        creds = InstalledAppFlow.from_client_secrets_file(str(secrets), ANALYTICS_SCOPES
                                                          ).run_local_server(port=0)
    ANALYTICS_TOKEN.write_text(creds.to_json(), encoding="utf-8")
    return (build("youtube", "v3", credentials=creds, cache_discovery=False),
            build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False))


def _iso_seconds(text):
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", text or "")
    if not m:
        return 0
    d, h, mi, sec = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + sec


def collect_channel_data(yt, an, max_videos=80, today=None):
    """Your public videos with their first-week numbers, plus who watches (when available)."""
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    try:
        chan = (yt.channels().list(part="snippet,contentDetails", mine=True).execute().get("items")
                or [None])[0]
        if not chan:
            raise CoachError("No YouTube channel was found for this Google account.")
        uploads = chan["contentDetails"]["relatedPlaylists"]["uploads"]
        ids, token = [], None
        while len(ids) < max_videos:
            r = yt.playlistItems().list(part="contentDetails", playlistId=uploads, maxResults=50,
                                        pageToken=token).execute()
            ids += [i["contentDetails"]["videoId"] for i in r.get("items", [])]
            token = r.get("nextPageToken")
            if not token:
                break
        ids = ids[:max_videos]
        videos = []
        for k in range(0, len(ids), 50):
            r = yt.videos().list(part="snippet,contentDetails,statistics,status",
                                 id=",".join(ids[k:k + 50])).execute()
            for v in r.get("items", []):
                if v.get("status", {}).get("privacyStatus") != "public":
                    continue
                sn = v["snippet"]
                videos.append({"id": v["id"], "title": sn.get("title", ""),
                               "published": datetime.datetime.fromisoformat(
                                   sn["publishedAt"].replace("Z", "+00:00")),
                               "duration_s": _iso_seconds(v["contentDetails"].get("duration")),
                               "total_views": int(v.get("statistics", {}).get("viewCount", 0)),
                               "views7": None})
        for v in videos:  # first 7 days of each video that is at least 4 days old
            start = v["published"].date()
            if (today - start).days < 4:
                continue
            end = min(start + datetime.timedelta(days=6), today - datetime.timedelta(days=1))
            r = an.reports().query(ids="channel==MINE", startDate=str(start), endDate=str(end),
                                   metrics="views,estimatedMinutesWatched,averageViewPercentage,"
                                           "subscribersGained", filters=f"video=={v['id']}").execute()
            row = (r.get("rows") or [[0, 0, 0, 0]])[0]
            v.update(views7=int(row[0]), minutes7=float(row[1]), avg_pct7=float(row[2]),
                     subs7=int(row[3]))
    except CoachError:
        raise
    except Exception as err:
        problem = _coach_http_problem(err)
        if problem:
            raise problem
        raise
    audience = {}
    start90 = str(today - datetime.timedelta(days=90))
    for name, kw in (("age_gender", dict(metrics="viewerPercentage", dimensions="ageGroup,gender",
                                         sort="-viewerPercentage", maxResults=6)),
                     ("countries", dict(metrics="views", dimensions="country", sort="-views",
                                        maxResults=5)),
                     ("traffic", dict(metrics="views", dimensions="insightTrafficSourceType",
                                      sort="-views", maxResults=6))):
        try:  # small channels often have no demographic data yet: skip quietly
            r = an.reports().query(ids="channel==MINE", startDate=start90, endDate=str(today),
                                   **kw).execute()
            audience[name] = [list(row) for row in r.get("rows", [])]
        except Exception:
            audience[name] = []
    return {"channel": chan["snippet"].get("title", ""), "videos": videos, "audience": audience}


def _daypart(h):
    return ("morning 05-12" if 5 <= h < 12 else "afternoon 12-17" if 12 <= h < 17
            else "evening 17-22" if 17 <= h < 22 else "night 22-05")


def _group(videos, keyfn, min_n):
    import statistics
    groups = {}
    for v in videos:
        groups.setdefault(keyfn(v), []).append(v["views7"])
    overall = statistics.median([v["views7"] for v in videos]) or 1
    rows = [{"group": k, "n": len(x), "median_views7": statistics.median(x),
             "lift": round(statistics.median(x) / overall, 2), "reliable": len(x) >= min_n}
            for k, x in groups.items()]
    return sorted(rows, key=lambda r: -r["median_views7"])


def _strip_names(title, ignore):
    """Remove the game's name (e.g. 'GTA 6') so its digits do not count as 'a number in the title'."""
    for name in ignore or []:
        name = (name or "").strip()
        if name:
            title = re.sub(re.escape(name).replace(r"\ ", r"\s*"), " ", title, flags=re.I)
    return title


def analyse(videos, ignore=()):
    """Plain statistics, no AI: what performs better on this channel, and how sure we can be."""
    import statistics
    vs = [dict(v) for v in videos if v.get("views7") is not None]
    n = len(vs)
    out = {"mature_videos": n, "confidence": "low" if n < 8 else "medium" if n < 25 else "good",
           "timezone": time.tzname[0]}
    if n == 0:
        return out
    for v in vs:
        v["local"] = v["published"].astimezone()
    out["median_views7"] = statistics.median([v["views7"] for v in vs])
    out["weekday"] = _group(vs, lambda v: v["local"].strftime("%A"), 3)
    out["daypart"] = _group(vs, lambda v: _daypart(v["local"].hour), 3)

    def binary(label, test):
        yes = [v for v in vs if test(v)]
        no = [v for v in vs if not test(v)]
        if len(yes) < 4 or len(no) < 4:
            return {"feature": label, "reliable": False, "n_with": len(yes), "n_without": len(no)}
        a = statistics.median([v["views7"] for v in yes])
        b = statistics.median([v["views7"] for v in no])
        return {"feature": label, "reliable": True, "n_with": len(yes), "n_without": len(no),
                "median_with": a, "median_without": b, "lift": round(a / (b or 1), 2)}
    out["features"] = [
        binary("title has a number", lambda v: bool(re.search(r"\d", _strip_names(v["title"], ignore)))),
        binary("title is a question", lambda v: "?" in v["title"]),
        binary("title has an emoji", lambda v: bool(_EMOJI.search(v["title"]))),
        binary("title is longer than 45 characters", lambda v: len(v["title"]) > 45),
        binary("video is longer than 30 seconds", lambda v: v["duration_s"] > 30)]
    ranked = sorted(vs, key=lambda v: -v["views7"])
    k = 3 if n < 10 else 5
    def short(v):
        return {"title": v["title"], "views7": v["views7"], "duration_s": v["duration_s"],
                "avg_view_pct": round(v.get("avg_pct7", 0)), "subs7": v.get("subs7", 0),
                "posted": v["local"].strftime("%a %H:%M")}
    out["best"] = [short(v) for v in ranked[:k]]
    out["worst"] = [short(v) for v in ranked[-k:]] if n >= 2 * k else []
    return out


def render_stats(summary, data=None):
    n = summary["mature_videos"]
    lines = [f"# Channel report", "",
             f"Videos with a full first week of data: **{n}** (confidence: **{summary['confidence']}**)."]
    if n == 0:
        lines.append("\nNo video is old enough yet (a video needs about 4 days). Come back later.")
        return "\n".join(lines)
    lines += [f"Typical first-week views: **{int(summary['median_views7'])}**. "
              f"Times below are in your PC time zone ({summary['timezone']}).", ""]
    for title, key in (("Best days to post", "weekday"), ("Best time of day", "daypart")):
        lines.append(f"## {title}")
        for r in summary[key]:
            note = "" if r["reliable"] else "  (not enough videos to trust this)"
            lines.append(f"- {r['group']}: {int(r['median_views7'])} views, {r['n']} video(s), "
                         f"{r['lift']}x typical{note}")
        lines.append("")
    lines.append("## What in the title or length changes results")
    for f in summary["features"]:
        if f["reliable"]:
            lines.append(f"- {f['feature']}: {int(f['median_with'])} vs {int(f['median_without'])} "
                         f"views ({f['lift']}x)")
        else:
            lines.append(f"- {f['feature']}: not enough videos to compare yet")
    lines += ["", "## Best videos (first week)"]
    lines += [f"- {b['title']}: {b['views7']} views, posted {b['posted']}" for b in summary["best"]]
    if summary["worst"]:
        lines += ["", "## Weakest videos"]
        lines += [f"- {b['title']}: {b['views7']} views" for b in summary["worst"]]
    aud = (data or {}).get("audience") or {}
    if any(aud.values()):
        lines += ["", "## Who watches (last 90 days)"]
        if aud.get("countries"):
            lines.append("- Top countries: " + ", ".join(f"{c[0]} ({int(c[1])})" for c in aud["countries"]))
        if aud.get("age_gender"):
            lines.append("- Age and gender: " + ", ".join(f"{a[0]} {a[1]} {a[2]}%" for a in aud["age_gender"]))
        if aud.get("traffic"):
            lines.append("- Where views come from: " + ", ".join(f"{t[0]} ({int(t[1])})" for t in aud["traffic"]))
    lines += ["", "_These are patterns in your own videos, not proof. A title style can look good only "
              "because it was posted at a better time, or on a lucky day. Test one thing at a time._"]
    return "\n".join(lines)


def coach_prompt(summary, data, args):
    niche = (getattr(args, "niche", "") or "").strip()
    game = (getattr(args, "game", "") or "").strip()
    facts = json.dumps({"summary": summary, "audience": (data or {}).get("audience", {})},
                       ensure_ascii=False, default=str)[:12000]
    return (
        "You are a careful YouTube growth coach for a gaming streamer who posts clips and Shorts.\n"
        f"Channel: {(data or {}).get('channel', '')}. Game/niche: {game or 'not given'}. "
        f"Notes from the creator: {niche or 'none'}.\n"
        "Below are REAL numbers from the channel (JSON). Rules:\n"
        "- Use ONLY these numbers. Never invent statistics, views or percentages.\n"
        "- Where 'reliable' is false, or confidence is 'low', say there is not enough data and do "
        "not draw a conclusion from it.\n"
        "- Start each advice item with [data] if it comes from these numbers, or [general] if it is "
        "normal best practice. Be honest that nothing guarantees views.\n"
        "- Write in very simple English, short sentences, at most 5 items per list.\n\n"
        f"DATA:\n{facts}\n\n"
        'Reply with ONLY this JSON: {"headline": "one sentence", "what_to_upload": ["..."], '
        '"when_to_upload": ["..."], "titles_and_tags": ["..."], "audience": ["..."], '
        '"experiments": ["3 concrete things to test over the next 2 weeks"], "cautions": ["..."]}')


def render_coach(advice):
    names = [("what_to_upload", "What to upload"), ("when_to_upload", "When to upload"),
             ("titles_and_tags", "Titles and tags"), ("audience", "Your audience"),
             ("experiments", "Experiments for the next 2 weeks"), ("cautions", "Be careful")]
    lines = ["# Coach advice", "", f"**{advice.get('headline', '')}**", ""]
    for key, label in names:
        items = [str(i) for i in (advice.get(key) or []) if str(i).strip()]
        if items:
            lines += [f"## {label}"] + [f"- {i}" for i in items] + [""]
    return "\n".join(lines)


def save_title_lessons(summary):
    """Feed this channel's best and worst titles back into the title writer (not when data is thin)."""
    if summary.get("confidence") == "low" or not summary.get("best"):
        return False
    TITLE_LESSONS.write_text(json.dumps({
        "best": [b["title"] for b in summary["best"]],
        "worst": [b["title"] for b in summary.get("worst", [])],
        "updated": time.time()}, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def run_coach(args):
    """--coach: read your channel's numbers, find patterns, and (with AI) write the advice."""
    try:
        yt, an = get_analytics_services()
        print("Reading your channel (this can take a minute)...")
        data = collect_channel_data(yt, an)
    except CoachError as err:
        print(f"Channel coach: {err}")
        return
    summary = analyse(data["videos"], ignore=[getattr(args, "game", "")])
    report = render_stats(summary, data)
    print("\n" + report + "\n")
    advice_text = ""
    if getattr(args, "ai", None) in (None, "none"):
        advice_text = ("\n_Turn on the AI (Gemini) in the app to get written advice: what to upload, "
                       "when, and what to test._\n")
        print(advice_text.strip())
    elif summary["mature_videos"] == 0:
        advice_text = ""
    else:
        try:
            print(f"Asking the AI ({args.ai}) to explain these numbers...")
            advice = parse_json_block(ai_chat(args, coach_prompt(summary, data, args), []))
            advice_text = "\n" + render_coach(advice) + "\n"
            print(advice_text)
        except Exception as err:
            advice_text = f"\n_The AI could not write advice this time ({str(err)[:120]}). The numbers above still apply._\n"
            print(advice_text.strip())
    COACH_REPORT.write_text(report + "\n" + advice_text, encoding="utf-8")
    print(f"Saved: {COACH_REPORT.name}")
    if save_title_lessons(summary):
        print("The title writer will now learn from your best and weakest titles.")
    elif summary["mature_videos"]:
        print("Not enough videos yet for the title writer to learn from (it needs at least 8).")


def _title_lessons_text():
    try:
        d = json.loads(TITLE_LESSONS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    best, worst = d.get("best") or [], d.get("worst") or []
    if not best:
        return ""
    text = ("THIS CHANNEL'S OWN RESULTS (learn the style, never copy the words)\n"
            "- Titles with the most first-week views:\n"
            + "\n".join(f"  * {t}" for t in best[:4]) + "\n")
    if worst:
        text += "- Weakest titles (avoid this style):\n" + "\n".join(f"  * {t}" for t in worst[:4]) + "\n"
    return text + "\n"


def _build_prompt(args, talk):
    game = (getattr(args, "game", "") or "").strip()
    channel = (getattr(args, "title", "") or "").strip()
    avoid = "\n".join(f"- {t}" for t in _recent_titles[-8:]) or "(none yet)"
    context = [f"Game/topic given by the creator: {game}" if game else
               "Game: not given. Work it out from the pictures (HUD, menus, characters)."]
    if channel and channel.lower() != "stream highlight":
        context.append(f"Channel / series name: {channel}")
    creator = _creator(args)
    if creator:
        context.append(f"Creator of the video: {creator}. Mention them naturally in the "
                       "description (e.g. 'Clip from {creator}'s stream'); do not put the name in the titles, it is added automatically.".replace("{creator}", creator))
    return (
        "You are an expert YouTube Shorts title and SEO writer for gaming streamers.\n"
        "The images are frames, in order, from ONE short stream clip. Look at them carefully: "
        "read the on-screen text, HUD, kill feed, scoreboard and menus to learn the game, the "
        "map/mode and exactly what happens.\n"
        + "\n".join(context) + "\n"
        f"What the streamer says in the clip (may be Hindi/Hinglish; understand it): {talk}\n\n"
        + _title_lessons_text() +
        "TITLE RULES\n"
        "- Write 5 DIFFERENT title candidates, 30-65 characters each, in the language and "
        "style the streamer speaks (Hinglish in Roman letters is fine if they speak Hinglish).\n"
        "- Each must name the SPECIFIC thing that happens (what, against whom, how: e.g. "
        "'1v4 Ace with a Sheriff on Ascent') and open with the strongest keyword or the payoff, "
        "not with filler.\n"
        "- Include the game name in at least 2 candidates. Use a number, a stake or a question "
        "when it is true. Max one emoji, none is also fine.\n"
        "- NEVER: all caps, clickbait that is not shown in the clip, 'Insane clutch!' style "
        "generic titles, vague words alone (epic, crazy, wow, funny), or hashtags.\n"
        "- Do not reuse the wording of these earlier titles:\n" + avoid + "\n\n"
        "TAGS RULES\n"
        "- 15 tags, no #, most specific first: game name, character/weapon/map/mode seen, the "
        "action (e.g. 'valorant ace', '1v4 clutch'), streamer-language search phrases "
        "(also Hinglish spellings if relevant), then 2-3 broad ones. Each 1-4 words.\n"
        "- Only things actually visible or said. No unrelated trending tags.\n\n"
        "HASHTAGS RULES\n"
        "- Exactly 3 hashtag words (no #, no spaces): the game, the specific topic, and one "
        "broad searchable word. Example: Valorant, ValorantAce, Gaming.\n\n"
        "DESCRIPTION RULES\n"
        "- 2 short sentences: what happens, then a hook to watch/subscribe. Put the game name and "
        "main keywords naturally in the first sentence.\n\n"
        'Reply with ONLY this JSON: {"topic": "game + what happens, max 8 words", '
        '"titles": ["...", "...", "...", "...", "..."], "description": "...", "tags": ["..."], '
        '"hashtags": ["...", "...", "..."]}')


_ai_block_until = 0.0


def ai_metadata(args, clip_path, transcript=""):
    """Let the AI watch the clip and write its title, description and tags. None if it can't."""
    global _ai_block_until
    if not getattr(args, "ai", None):
        return None
    if time.time() < _ai_block_until:  # it just failed: do not make every clip wait for it
        print(f"      (AI is resting until about {clock(_ai_block_until)} after repeated failures)")
        return None
    try:
        frames = sample_frames(clip_path, 8)
        if not frames:
            raise RuntimeError("could not read pictures from the clip")
        talk = transcript.strip()[:2000] or "(nothing was said)"
        base_prompt = _build_prompt(args, talk)
        raw = None
        last_err = None
        for _ in range(2):  # one retry if the answer is not usable JSON / has no title
            try:
                raw = parse_json_block(ai_chat(args, base_prompt, frames))
                if not _pick_title(raw):
                    raise ValueError("the AI gave no title")
                break
            except (ValueError, json.JSONDecodeError) as err:
                last_err = err
                raw = None
        if raw is None:
            raise last_err or RuntimeError("no usable AI answer")
        key = read_secret("YOUTUBE_API_KEY", "youtube_api_key.txt")
        if key and raw.get("topic"):
            try:
                titles, tags = trending_examples(raw["topic"], key)
            except Exception as err:
                titles, tags = [], []
                code = getattr(err, "code", None)
                if code in (400, 401, 403):
                    why = ("the YouTube API key was rejected. Put a real YouTube Data API v3 key "
                           "in youtube_api_key.txt (it must NOT be the Gemini key) and enable "
                           "'YouTube Data API v3' for it in Google Cloud Console")
                else:
                    why = str(err)[:100]
                print(f"      (trending check skipped: {why})")
            if titles:
                ask = (base_prompt + "\n\nPopular recent Shorts about this topic (inspiration for "
                       "keywords and style only, do NOT copy them):\n"
                       + "\n".join(f"- {t}" for t in titles)
                       + "\nTags they use: " + ", ".join(tags)
                       + "\nNow write the final, ORIGINAL, honest version in the same JSON format.")
                try:
                    better = parse_json_block(ai_chat(args, ask, frames))
                    if _pick_title(better):
                        raw = {**raw, **better}
                except Exception as err:
                    print(f"      (trending step failed, keeping the first answer: {str(err)[:100]})")
        meta = clean_meta(raw, args)
        _recent_titles.append(meta["title"])
        print(f"      AI title: {meta['title']}")
        print(f"      AI tags : {', '.join(meta['tags'][:8])}{'...' if len(meta['tags']) > 8 else ''}")
        return meta
    except Exception as err:
        print(f"      AI could not write the title ({str(err)[:160]}).")
        if isinstance(err, AIError) and (err.timeout or err.code in _BUSY):
            _ai_block_until = time.time() + 600  # try again in 10 minutes, not on every clip
        return None


def check_ai(args):
    """Stop early, with help, if the chosen AI is not ready."""
    if args.ai == "gemini" and not read_secret("GEMINI_API_KEY", "ai_key.txt"):
        sys.exit("Gemini needs a free key. Get one at https://aistudio.google.com and save it in a "
                 f"file named ai_key.txt inside {APP_DIR} (just the key, nothing else).")
    if args.ai == "ollama":
        try:
            base = (args.ai_url or AI_PRESETS["ollama"][0]).rstrip("/")
            urllib.request.urlopen(base + "/models", timeout=5).read()
        except Exception:
            sys.exit("Ollama is not running. Install it from ollama.com, open it, then run:\n"
                     "    ollama pull qwen2.5vl:7b")
    yt_key, ai_key = read_secret("YOUTUBE_API_KEY", "youtube_api_key.txt"), read_secret("GEMINI_API_KEY", "ai_key.txt")
    if yt_key and yt_key == ai_key:
        print("Warning: youtube_api_key.txt contains the same key as ai_key.txt. A Gemini key does "
              "not work for YouTube, so the trending check will be skipped. Use a separate "
              "YouTube Data API v3 key (or delete youtube_api_key.txt).")
    if not yt_key:
        print("Note: no youtube_api_key.txt found, so the AI will not check trending videos "
              "(optional).")


_yt = {}
_uploaded = []  # clips that uploaded successfully, oldest first


def _csv_values(value):
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def _clip_number(path):
    match = re.search(r"(?:clip|live)_(\d+)", Path(path).stem, re.I)
    return int(match.group(1)) if match else 1


def _spoken_hook(transcript):
    """A short title hook made only from words actually spoken in the clip."""
    text = re.sub(r"\s+", " ", (transcript or "")).strip(" -–—.,;:!?")
    if not text:
        return ""
    pieces = re.split(r"(?<=[.!?])\s+", text)
    sentence = next((piece for piece in pieces if len(piece.split()) >= 4), pieces[0])
    return " ".join(sentence.split()[:10]).strip(" -–—.,;:")[:62].rstrip()


_GENERIC_HASHTAGS = {"shorts", "short", "viral", "fyp", "trending", "video", "youtube"}


def _hashtag(text):
    """'GTA 6' -> '#GTA6'. Letters, digits and underscores only (any language)."""
    t = re.sub(r"[^\w]", "", str(text), flags=re.UNICODE).strip("_")
    return "#" + t if t and not t.isdigit() and len(t) <= 30 else ""


def build_hashtags(args, ai_hashtags=(), tags=()):
    """Exactly up to 3 hashtags: yours first, then the AI's, then the game and best tags."""
    pools = [_csv_values(getattr(args, "hashtags", "")), list(ai_hashtags or []),
             [getattr(args, "game", "")], list(tags or [])[:6], ["Gaming"]]
    out, seen = [], set()
    for pool in pools:
        for item in pool:
            tag = _hashtag(item)
            key = tag.casefold()
            if not tag or key in seen or key[1:] in _GENERIC_HASHTAGS:
                continue
            seen.add(key)
            out.append(tag)
            if len(out) == 3:
                return out
    return out


def _clean_tag_list(values):
    tags, seen, total = [], set(), 0
    for value in values:
        tag = re.sub(r"[<>,#]", "", str(value)).strip()[:60]
        key = tag.casefold()
        if not tag or key in seen:
            continue
        extra = len(tag) + (1 if tags else 0)
        if total + extra > 480:  # YouTube's combined tag limit is 500 characters
            break
        tags.append(tag)
        seen.add(key)
        total += extra
    return tags


def metadata_for_clip(args, path, meta=None, transcript=""):
    """Combine optional AI output with truthful creator-provided publishing metadata."""
    if meta:
        title = str(meta.get("title", "")).strip()
        description = str(meta.get("description", "")).strip()
        tags = list(meta.get("tags") or [])
    else:
        number = _clip_number(path)
        raw_title = (args.title or "Stream highlight").strip() or "Stream highlight"
        title = raw_title.replace("{n}", str(number))
        hook = _spoken_hook(transcript) if args.auto_metadata else ""
        if hook:
            title = f"{title}: {hook}"
        description, tags = "", []  # the title is used exactly as typed (add {n} for numbers)

    title = " ".join(title.replace("<", "").replace(">", "").split())
    if args.vertical and getattr(args, "shorts_in_title", False) and "#shorts" not in title.casefold():
        suffix = " #Shorts"
        title = title[:100 - len(suffix)].rstrip() + suffix
    title = title[:100].rstrip() or "Stream highlight"

    additions = []
    if args.description.strip() and args.description.strip() not in description:
        additions.append(args.description.strip())
    if args.credit.strip() and args.credit.strip() not in description:
        additions.append(f"Credit: {args.credit.strip()}")
    if args.live and args.live not in description:
        additions.append(f"Source: {args.live}")
    if additions:
        description = "\n\n".join(part for part in (description, *additions) if part)
    ai_hashtags = list(meta.get("hashtags") or []) if meta else []
    tag_pool = list(tags) + _csv_values(args.tags)
    hashtags = build_hashtags(args, ai_hashtags, tag_pool)
    # keep the user's own hashtags out of the duplicate check, then put ours last
    have = {h.casefold() for h in re.findall(r"#\w+", description)}
    line = [h for h in hashtags if h.casefold() not in have]
    if args.vertical and "#shorts" not in have:
        line.append("#Shorts")
    if line:
        description = (description + "\n\n" + " ".join(line)).strip()

    typed = _csv_values(args.tags)
    tags += typed
    if not typed:  # tags you typed are used exactly as typed; the automatic ones fill in only if you typed none
        if args.game.strip():
            tags.append(args.game.strip())
        tags += ["gaming", "stream highlight"]
        if args.vertical:
            tags.append("shorts")
    return {"title": title, "description": description[:4900], "tags": _clean_tag_list(tags)}


def publish_time_for_clip(args):
    """Return the scheduled time for this clip in UTC, or None for immediate publishing."""
    if not getattr(args, "_publish_start", None):
        return None
    index = getattr(args, "_publish_count", 0)
    args._publish_count = index + 1
    value = args._publish_start + datetime.timedelta(minutes=args.publish_gap * index)
    return value.isoformat().replace("+00:00", "Z")


def cleanup_old_clips(folder, days):
    """Delete this app's clips (live_* / clip_*) and their .txt notes older than `days` days."""
    if not days or days <= 0:
        return
    limit = time.time() - days * 86400
    for f in Path(folder).glob("*"):
        try:
            if (f.name.startswith(("live_", "clip_")) and f.suffix.lower() in (".mp4", ".txt")
                    and f.stat().st_mtime < limit):
                f.unlink()
        except OSError:
            pass


def log_for_upload(args, path, metadata, publish_at):
    """Always write an upload-review manifest, even when direct uploading is switched off."""
    try:
        tags = ", ".join(metadata["tags"])
        csv_path = Path(path).parent / "youtube_metadata.csv"
        new = not csv_path.exists()
        with open(csv_path, "a", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            if new:
                writer.writerow(["file", "title", "description", "tags", "publish_at", "privacy"])
            writer.writerow([Path(path).name, metadata["title"],
                             metadata["description"].replace("\n", " "), tags,
                             publish_at or "", args.privacy])
        Path(path).with_suffix(".txt").write_text(
            f"{metadata['title']}\n\n{metadata['description']}\n\nTags: {tags}\n"
            f"Publish at: {publish_at or 'immediately'}\n", encoding="utf-8")
    except Exception:
        pass


# ---------- upload queue: clips wait here when YouTube says "limit reached" ----------

QUEUE_FILE = APP_DIR / "upload_queue.json"
_queue_mem_lock = threading.RLock()   # threads of this program
_process_lock = threading.Lock()      # only one retry pass at a time
_upload_block_until = 0.0             # after a limit error, new clips go straight to the queue
_watcher_started = False
_LIMIT_WORDS = ("uploadlimitexceeded", "dailylimitexceeded", "quotaexceeded", "ratelimitexceeded",
                "userratelimitexceeded", "exceeded the number of videos")


def clock(ts):
    return time.strftime("%d %b %H:%M", time.localtime(ts))


@contextlib.contextmanager
def _queue_guard():
    """Several copies of the app (e.g. multi-live) may touch the queue: take a file lock."""
    lock = QUEUE_FILE.with_suffix(".lock")
    owned = False
    with _queue_mem_lock:
        deadline = time.time() + 10
        while True:
            try:
                os.close(os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                owned = True
                break
            except FileExistsError:
                try:
                    if time.time() - lock.stat().st_mtime > 30:  # left behind by a crash
                        lock.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                if time.time() > deadline:
                    break
                time.sleep(0.05)
            except OSError:
                break
        try:
            yield
        finally:
            if owned:
                lock.unlink(missing_ok=True)


def load_queue():
    try:
        data = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [e for e in data if isinstance(e, dict) and e.get("id")] if isinstance(data, list) else []


def _save_queue(items):
    tmp = QUEUE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, QUEUE_FILE)


def queue_edit(fn):
    """Read, change and save the queue as one safe step."""
    with _queue_guard():
        items = load_queue()
        result = fn(items)
        _save_queue(items)
        return result


def classify_upload_error(err):
    """('limit' | 'temporary' | 'fatal', short reason)."""
    status = getattr(getattr(err, "resp", None), "status", None)
    raw = getattr(err, "content", b"")
    raw = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else str(raw)
    text = f"{err} {raw}".lower()
    if any(w in text for w in _LIMIT_WORDS):
        return "limit", "YouTube says the upload limit for this channel is reached"
    if isinstance(err, FileNotFoundError):
        return "fatal", "the clip file is missing"
    if (status in (500, 502, 503, 504) or isinstance(err, (ConnectionError, TimeoutError, OSError))
            or "retry limit reached" in text or "timed out" in text):
        return "temporary", "a temporary network or YouTube problem"
    return "fatal", str(err)[:160]


def enqueue_job(job, keep, kind, why, delay):
    entry = {**job, "keep": keep, "attempts": 0, "fatal": 0, "queued_at": time.time(),
             "next_try": time.time() + delay, "last_error": why, "kind": kind}

    def add(items):
        for old in items:  # the same clip is never queued twice
            if old.get("path") == job["path"]:
                old.update(next_try=entry["next_try"], last_error=why, kind=kind)
                return
        entry["id"] = uuid.uuid4().hex
        items.append(entry)
    queue_edit(add)


def _resolve_pending_ai(entry, force):
    """Write the title/tags for a clip that was waiting for the AI. None if the AI is still down."""
    global _ai_block_until
    if force:
        _ai_block_until = 0.0
    import types
    ma = types.SimpleNamespace(**(entry.get("meta_args") or {}))
    transcript = entry.get("transcript", "")
    meta = ai_metadata(ma, entry["path"], transcript)
    if meta is None:
        return None
    md = metadata_for_clip(ma, entry["path"], meta, transcript)
    try:  # refresh the review note next to the clip
        Path(entry["path"]).with_suffix(".txt").write_text(
            f"{md['title']}\n\n{md['description']}\n\nTags: {', '.join(md['tags'])}\n", encoding="utf-8")
    except OSError:
        pass
    return {"title": md["title"], "description": md["description"], "tags": md["tags"],
            "pending_ai": False}


def _upload_job(job):
    import youtube_upload
    tid = threading.get_ident()
    if tid not in _yt:  # one YouTube connection per thread
        _yt[tid] = youtube_upload.get_service()
    return youtube_upload.upload_clip(
        _yt[tid], job["path"], job["title"], job["description"], job["privacy"],
        tags=job.get("tags"), category=job.get("category", "20"), publish_at=job.get("publish_at"),
        made_for_kids=bool(job.get("made_for_kids")), playlist_id=job.get("playlist_id") or None,
        thumbnail=job.get("thumbnail") or None)


def _after_upload(keep, path):
    """Delete older uploaded clips from this PC when the user asked for that."""
    if keep is None:
        return
    _uploaded.append(Path(path))
    while len(_uploaded) > keep:
        old = _uploaded.pop(0)
        try:
            old.unlink(missing_ok=True)
            print(f"      (deleted {old.name} to free space)")
        except OSError as err:
            print(f"      (could not delete {old.name}: {err})")


def handle_upload_failure(args, job, keep, err):
    """Queue the clip for later when the failure is a limit or temporary problem."""
    global _upload_block_until
    kind, why = classify_upload_error(err)
    if kind == "fatal":
        print(f"  Upload failed, the clip is still saved: {str(err)[:250]}")
        return
    hours = max(0.25, float(getattr(args, "retry_hours", 6.0) or 6.0))
    delay = hours * 3600 if kind == "limit" else 900
    if kind == "limit":
        _upload_block_until = time.time() + delay
    enqueue_job(job, keep, kind, why, delay)
    print(f"  Upload postponed: {why}.\n"
          f"  The clip is saved and queued. It will upload automatically (next try about "
          f"{clock(time.time() + delay)}).")


def process_upload_queue(args=None, force=False):
    """Try the queued uploads that are due. Returns how many were uploaded."""
    global _upload_block_until
    if not _process_lock.acquire(blocking=False):
        return 0
    done = 0
    try:
        hours = max(0.25, float(getattr(args, "retry_hours", 6.0) or 6.0))
        if force:
            _upload_block_until = 0.0
        ai_failed = False
        for entry in sorted(load_queue(), key=lambda e: e.get("queued_at", 0)):
            if not force and entry.get("next_try", 0) > time.time():
                continue
            if entry.get("pending_ai") and ai_failed:
                continue
            eid, path = entry["id"], Path(entry["path"])
            if not path.is_file():
                print(f"  Queue: {path.name} no longer exists, so it was removed from the queue.")
                queue_edit(lambda items, i=eid: items.__setitem__(slice(None), [e for e in items if e["id"] != i]))
                continue
            job = dict(entry)
            if entry.get("pending_ai"):
                print(f"  Queue: asking the AI for the title of {path.name}...")
                got = _resolve_pending_ai(entry, force)
                if got is None:
                    ai_failed = True
                    again = time.time() + 600
                    print(f"  Queue: the AI is still unavailable. Next try about {clock(again)}.")
                    queue_edit(lambda items, n=again: [
                        x.update(next_try=n, last_error="waiting for the AI")
                        for x in items if x.get("pending_ai")])
                    continue
                job.update(got)
                queue_edit(lambda items, i=eid, g=got: [x.update(g) for x in items if x["id"] == i])
            pa = job.get("publish_at")
            if pa:
                try:
                    when = datetime.datetime.fromisoformat(pa.replace("Z", "+00:00"))
                    if when <= datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=10):
                        print(f"  Queue: the planned publish time for {path.name} has passed; "
                              "uploading it as a normal private upload instead.")
                        job["publish_at"] = None
                except ValueError:
                    job["publish_at"] = None
            print(f"  Queue: uploading {path.name} (try {entry.get('attempts', 0) + 1})...")
            try:
                vid = _upload_job(job)
            except Exception as err:
                kind, why = classify_upload_error(err)
                attempts = entry.get("attempts", 0) + 1
                if kind == "fatal":
                    fatal = entry.get("fatal", 0) + 1
                    if fatal >= 3:
                        print(f"  Queue: giving up on {path.name}: {why}. The clip is still in its folder.")
                        queue_edit(lambda items, i=eid: items.__setitem__(slice(None), [e for e in items if e["id"] != i]))
                    else:
                        print(f"  Queue: {path.name} failed: {why}. Will try again in 24 hours.")
                        queue_edit(lambda items, i=eid, f=fatal, w=why, a=attempts: [
                            x.update(fatal=f, last_error=w, attempts=a, next_try=time.time() + 86400)
                            for x in items if x["id"] == i])
                    continue
                delay = hours * 3600 if kind == "limit" else min(7200, 900 * 2 ** min(attempts, 3))
                nxt = time.time() + delay
                if kind == "limit":  # every other clip would hit the same wall: wait for all of them
                    _upload_block_until = nxt
                    print(f"  Queue: {why}. Next try about {clock(nxt)}.")
                    queue_edit(lambda items, n=nxt, w=why: [
                        x.update(next_try=max(x.get("next_try", 0), n), last_error=w) for x in items])
                    queue_edit(lambda items, i=eid, a=attempts: [
                        x.update(attempts=a) for x in items if x["id"] == i])
                    break
                print(f"  Queue: {path.name} failed ({why}). Next try about {clock(nxt)}.")
                queue_edit(lambda items, i=eid, n=nxt, w=why, a=attempts: [
                    x.update(next_try=n, last_error=w, attempts=a) for x in items if x["id"] == i])
                continue
            print(f"  Queue: uploaded {path.name}: https://youtu.be/{vid}")
            queue_edit(lambda items, i=eid: items.__setitem__(slice(None), [e for e in items if e["id"] != i]))
            done += 1
            _after_upload(entry.get("keep"), path)
        return done
    finally:
        _process_lock.release()


def queue_summary():
    items = load_queue()
    if not items:
        return ""
    nxt = min(e.get("next_try", 0) for e in items)
    return f"{len(items)} clip(s) waiting to upload; next try about {clock(nxt)}"


def queue_watcher(args):
    """Live mode: every minute, retry queued uploads whose time has come."""
    while True:
        time.sleep(60)
        try:
            if any(e.get("next_try", 0) <= time.time() for e in load_queue()):
                process_upload_queue(args)
        except Exception as err:
            print(f"\n  (upload queue check failed: {str(err)[:120]})")


def run_retry_uploads(args):
    """--retry-uploads: upload what is waiting now; with --retry-wait keep going until empty."""
    items = load_queue()
    if not items:
        print("No uploads are waiting.")
        return
    print(f"{len(items)} upload(s) waiting. Trying now...")
    stop_file = os.environ.get("HC_STOP_FILE")
    force = True
    while True:
        process_upload_queue(args, force=force)
        force = False
        left = load_queue()
        if not left:
            print("All waiting uploads are done.")
            return
        nxt = min(e.get("next_try", 0) for e in left)
        if not getattr(args, "retry_wait", False):
            print(f"{len(left)} clip(s) still waiting. Next try about {clock(nxt)}. "
                  "Turn on 'Keep retrying' to let the app do this by itself.")
            return
        print(f"Waiting until about {clock(nxt)} ({len(left)} clip(s) left). Press Stop to quit; "
              "the queue is saved.")
        while time.time() < nxt:
            if stop_file and Path(stop_file).exists():
                print("Stopped. The remaining clips stay in the queue.")
                return
            time.sleep(5)


def upload_if_wanted(args, path, meta=None, transcript=""):
    """Upload one clip through the official API; preserve it locally if something fails."""
    metadata = metadata_for_clip(args, path, meta, transcript)
    publish_at = publish_time_for_clip(args)
    log_for_upload(args, path, metadata, publish_at)
    if getattr(args, "ai", None) and meta is None and not getattr(args, "allow_fallback_upload", False):
        if not getattr(args, "upload", False):
            print("  AI metadata is unavailable. The clip is saved (uploading is off).")
            return
        keep = getattr(args, "keep_uploaded", None)
        if keep is None and getattr(args, "delete_after_upload", False):
            keep = 0
        simple = (str, int, float, bool, type(None))
        job = {"path": str(Path(path).resolve()), "title": metadata["title"],
               "description": metadata["description"], "tags": metadata["tags"],
               "privacy": args.privacy, "category": args.category, "publish_at": publish_at,
               "made_for_kids": bool(args.made_for_kids), "playlist_id": args.playlist_id or "",
               "thumbnail": args.thumbnail or "", "pending_ai": True,
               "transcript": (transcript or "")[:4000],
               "meta_args": {k: v for k, v in vars(args).items() if isinstance(v, simple)}}
        enqueue_job(job, keep, "ai", "waiting for the AI to write the title", 600)
        print("  The AI is unavailable right now. The clip is saved and queued: its title and tags "
              "will be written later and it uploads automatically.")
        return
    if not getattr(args, "upload", False):
        return
    keep = getattr(args, "keep_uploaded", None)  # how many uploaded clips stay on this PC
    if keep is None and getattr(args, "delete_after_upload", False):
        keep = 0
    job = {"path": str(Path(path).resolve()), "title": metadata["title"],
           "description": metadata["description"], "privacy": args.privacy,
           "tags": metadata["tags"], "category": args.category, "publish_at": publish_at,
           "made_for_kids": bool(args.made_for_kids), "playlist_id": args.playlist_id or "",
           "thumbnail": args.thumbnail or ""}
    if time.time() < _upload_block_until:  # the limit was hit a moment ago: do not waste a try
        enqueue_job(job, keep, "limit", "YouTube upload limit reached earlier",
                    max(60, _upload_block_until - time.time()))
        print(f"  Queued for later (YouTube limit): {Path(path).name}")
        return
    try:
        vid = _upload_job(job)
        when = f", scheduled {publish_at}" if publish_at else ""
        print(f"  Uploaded to YouTube ({args.privacy}{when}): https://youtu.be/{vid}")
        _after_upload(keep, path)
    except Exception as err:
        handle_upload_failure(args, job, keep, err)
        return
    if load_queue():  # YouTube accepted a clip, so the limit is over: send the waiting ones too
        print("  Upload queue: YouTube is accepting uploads again, sending the waiting clips...")
        process_upload_queue(args, force=True)

def main():
    p = argparse.ArgumentParser(description="Auto-cut highlights from a gaming stream.")
    p.add_argument("video", nargs="?", help="path to the stream video (e.g. stream.mp4)")
    p.add_argument("--live", help="YouTube live stream link: watch chat and clip it live")
    p.add_argument("--sensitivity", type=int, choices=[1, 2, 3],
                   help="live mode: 1 = only big moments, 2 = balanced, 3 = catch more")
    p.add_argument("--chat", help="Twitch chat JSON from TwitchDownloader")
    p.add_argument("--top", type=int, help="number of clips")
    p.add_argument("--auto-length", action=argparse.BooleanOptionalAction,
                   help="clip length follows how long the hype lasts")
    p.add_argument("--length", type=int, help="fixed clip length in seconds (with --no-auto-length)")
    p.add_argument("--min-len", type=int, default=15, help="shortest auto clip, seconds")
    p.add_argument("--max-len", type=int, default=90, help="longest auto clip, seconds")
    p.add_argument("--pad", type=int, default=5,
                   help="extra seconds of setup before and reaction after the hype")
    p.add_argument("--out", default="clips", help="output folder (default 'clips')")
    p.add_argument("--vertical", nargs="?", const="blur",
                   choices=["blur", "crop", "track", "facecam", "none"],
                   help="9:16 clips: 'blur' = full frame on blurred background, 'crop' = zoom, "
                        "'track' = follow the face, 'facecam' = gameplay on top + face below")
    p.add_argument("--captions", action=argparse.BooleanOptionalAction, help="burn in captions")
    p.add_argument("--caption-style", type=int, choices=[1, 2],
                   help="1 = big bold word captions, 2 = small normal lines")
    p.add_argument("--language", help="speech language code, e.g. en or hi (default: auto)")
    p.add_argument("--model", default="small", help="Whisper size: tiny/base/small/medium")
    p.add_argument("--gpu", action=argparse.BooleanOptionalAction, help="NVIDIA GPU encoding")
    p.add_argument("--precise", action="store_true", help="re-encode for exact cuts")
    p.add_argument("--upload", action=argparse.BooleanOptionalAction,
                   help="upload every finished clip to your YouTube channel")
    p.add_argument("--privacy", choices=["private", "unlisted", "public"], default="private",
                   help="visibility of uploaded clips (default private)")
    p.add_argument("--title", default="Stream highlight",
                   help="title for every uploaded clip, used exactly as typed; use {n} for the clip number")
    p.add_argument("--auto-metadata", action=argparse.BooleanOptionalAction, default=False,
                   help="when AI is off, derive a short truthful title hook from spoken words")
    p.add_argument("--description", default="", help="optional call to action / required description text")
    p.add_argument("--credit", default="", help="creator/source credit to include when applicable")
    p.add_argument("--game", default="", help="game/topic name; only used as relevant metadata")
    p.add_argument("--tags", default="", help="tags for every uploaded clip, separated by commas (used exactly as typed)")
    p.add_argument("--category", default="20", help="YouTube category ID; 20 is Gaming")
    p.add_argument("--playlist-id", default="", help="add each upload to this existing playlist ID")
    p.add_argument("--thumbnail", default="", help="optional truthful JPG/PNG thumbnail for each upload")
    p.add_argument("--publish-at", default="",
                   help="schedule first upload: ISO 8601 with zone, e.g. 2026-11-18T19:00:00+05:30")
    p.add_argument("--publish-gap", type=int, default=180,
                   help="minutes between scheduled uploads (default 180)")
    p.add_argument("--made-for-kids", action=argparse.BooleanOptionalAction, default=False,
                   help="declare child-directed content when that is true")
    p.add_argument("--buffer-dir", help="folder for temporary recordings (pick a drive with lots of free space)")
    p.add_argument("--min-free-gb", type=float, default=4.0,
                   help="stop safely before the recording drive has less free space than this (default 4 GB)")
    p.add_argument("--keep-uploaded", type=int,
                   help="keep only the newest N uploaded clips on this PC and delete older uploaded ones "
                        "(0 = delete each right after it uploads)")
    p.add_argument("--delete-after-upload", action="store_true",
                   help="delete each clip from this PC once it has uploaded successfully")
    p.add_argument("--keep-days", type=int, default=0,
                   help="delete clips older than this many days (0 = never)")
    p.add_argument("--ai", choices=["gemini", "ollama", "none"],
                   help="let an AI watch each clip and write its title, description and tags")
    p.add_argument("--allow-fallback-upload", action="store_true",
                   help="with --ai: upload using fallback metadata if the AI stays unavailable")
    p.add_argument("--ai-model", help="override the AI model name")
    p.add_argument("--ai-url", help="override the AI service address")
    p.add_argument("--split", type=int, default=58,
                   help="facecam layout: percent of the screen height used by the gameplay (default 58)")
    p.add_argument("--face-zoom", type=float, default=2.2,
                   help="facecam layout: how much around the face to show; smaller = closer (default 2.2)")
    p.add_argument("--smart-pick", action=argparse.BooleanOptionalAction, default=True,
                   help="video files: find moments by excited voice vs the surrounding audio "
                        "(use --no-smart-pick for the old plain-loudness method)")
    p.add_argument("--face-box", default="",
                   help="facecam layout: your face cam area as x,y,width,height in percent of the "
                        "screen, e.g. 72,60,26,36 (blank = find the face automatically)")
    p.add_argument("--hashtags", default="",
                   help="up to 3 hashtags for the description, comma separated (blank = automatic)")
    p.add_argument("--shorts-in-title", action="store_true",
                   help="also add #Shorts to the title (this hides description hashtags above the title)")
    p.add_argument("--coach", action="store_true",
                   help="read your channel's analytics and write advice (what/when to upload), then exit")
    p.add_argument("--niche", default="",
                   help="notes for the coach, e.g. 'GTA 6 Hinglish gamer, viewers 16-24 in India'")
    p.add_argument("--speech-device", choices=["auto", "cpu"], default="auto",
                   help="where speech reading runs: auto = NVIDIA GPU when it works (much faster), "
                        "cpu = always the processor")
    p.add_argument("--check-gpu", action="store_true",
                   help="test whether GPU speech reading works on this PC, then exit")
    p.add_argument("--retry-hours", type=float, default=6.0,
                   help="when YouTube says the upload limit is reached, wait this many hours before "
                        "retrying the queued clips (default 6)")
    p.add_argument("--retry-uploads", action="store_true",
                   help="do nothing but upload the clips waiting in upload_queue.json, then exit")
    p.add_argument("--retry-wait", action="store_true",
                   help="with --retry-uploads: keep retrying until every waiting clip is uploaded")
    p.add_argument("--max-height", type=int, default=1080,
                   help="live mode: tallest video to record (720 saves internet when watching many streams)")
    p.add_argument("--live-test", action="store_true",
                   help="with --live: record 40 seconds and make one clip right away (no hype needed)")
    p.add_argument("--kick-chatroom", type=int,
                   help="Kick only: chat room number, if the app cannot look it up itself")
    p.add_argument("--test-chat", metavar="LINK",
                   help="check for 20 seconds that live chat can be read (YouTube, Twitch or Kick link)")
    p.add_argument("--no-ask", action="store_true", help="skip the questions")
    args = p.parse_args()
    if args.min_free_gb < 0:
        sys.exit("--min-free-gb cannot be negative.")
    if args.publish_gap < 0:
        sys.exit("--publish-gap cannot be negative.")
    args._publish_start = None
    args._publish_count = 0
    if args.publish_at:
        try:
            scheduled = datetime.datetime.fromisoformat(args.publish_at.strip().replace("Z", "+00:00"))
        except ValueError:
            sys.exit("--publish-at must be ISO 8601 with a time zone, e.g. 2026-11-18T19:00:00+05:30")
        if scheduled.tzinfo is None:
            sys.exit("--publish-at needs a time zone, such as +05:30.")
        args._publish_start = scheduled.astimezone(datetime.timezone.utc)
        if args._publish_start <= datetime.datetime.now(datetime.timezone.utc):
            sys.exit("--publish-at must be in the future. Leave it blank for an immediate upload.")
        if args.privacy != "private":
            sys.exit("Scheduled uploads must use --privacy private until their publish time.")
    if args.thumbnail and not Path(args.thumbnail).is_file():
        sys.exit(f"Thumbnail file not found: {args.thumbnail}")
    if args.buffer_dir:  # all temporary files go there instead of the C: drive
        Path(args.buffer_dir).mkdir(parents=True, exist_ok=True)
        tempfile.tempdir = str(Path(args.buffer_dir).resolve())
    LAYOUT["split"] = min(max(args.split, 30), 80) / 100
    LAYOUT["zoom"] = min(max(args.face_zoom, 1.2), 8.0)
    try:
        LAYOUT["box"] = parse_face_box(args.face_box)
    except ValueError as err:
        sys.exit(f"--face-box: {err}")
    if not Path(args.out).is_absolute():  # relative folders live next to this script
        args.out = str(Path(__file__).resolve().parent / args.out)

    WHISPER["device"] = args.speech_device
    if args.check_gpu:
        check_gpu_speech()
        return
    if args.coach:
        run_coach(args)
        return
    if args.retry_hours <= 0:
        sys.exit("--retry-hours must be more than 0.")
    if args.retry_uploads:
        run_retry_uploads(args)
        return

    if args.test_chat:
        meter = ChatMeter()
        threading.Thread(target=chat_reader,
                         args=(watch_link(args.test_chat), meter, args.kick_chatroom),
                         daemon=True).start()
        print("Listening to chat for 20 seconds...")
        for i in range(20):
            time.sleep(1)
            print(f"\r  {i + 1:>2}s  chat messages received: {meter.total}", end="", flush=True)
        if meter.total:
            print("\n\nChat works! Live mode will be able to see the hype.")
        else:
            print("\n\nNo chat messages received. " + (meter.error or
                  "Check that chat is visible on the stream page."))
        return

    if not all(shutil.which(tool) for tool in ("ffmpeg", "ffprobe")):
        sys.exit("Clipping needs ffmpeg and ffprobe. Install FFmpeg with 'winget install Gyan.FFmpeg', then reopen the app.")
    ask = not args.no_ask
    if not args.video and not args.live:
        if not ask:
            sys.exit("Give a video file, or --live with a YouTube link.")
        mode = ask_choice("What do you want to do?", [
            "Cut highlights from a downloaded video",
            "Watch a live YouTube stream and clip it automatically"])
        if mode == 2:
            args.live = input("Paste the YouTube live link: ").strip().strip('"')
        else:
            args.video = input("Video file name or path: ").strip().strip('"')
    if args.live:
        args.top, args.auto_length = 10, True  # not used in live mode
    elif not Path(args.video).is_file():
        sys.exit(f"Video not found: {args.video}")

    # ----- ask the questions (only the ones not already given on the command line) -----
    print()
    if args.top is None:
        args.top = ask_number("How many clips do you want?", 10) if ask else 10
    if args.auto_length is None:
        args.auto_length = ask_yes_no("Auto clip length (longer when the hype lasts longer)?") if ask else True
    if not args.auto_length and args.length is None:
        args.length = ask_number("Clip length in seconds?", 30) if ask else 30
    if args.vertical == "none":
        args.vertical = None
    elif args.vertical is None and ask:
        if ask_yes_no("Make vertical 9:16 clips (Shorts / Reels / TikTok)?"):
            choice = ask_choice("Vertical style:", [
                "Full gameplay on a blurred background (recommended)",
                "Zoom into the centre (fills the screen, cuts the sides)",
                "Follow the face (talking / IRL streams)",
                "Gameplay on top + face close-up below (gaming with a facecam)"])
            args.vertical = ["blur", "crop", "track", "facecam"][choice - 1]
    if args.captions is None:
        args.captions = ask and ask_yes_no("Add captions?")
    if args.captions or args.auto_metadata or getattr(args, "ai", None) not in (None, "none"):
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            sys.exit("Captions and automatic title hooks need a free package. Run: python -m pip install faster-whisper")
        if args.captions and args.caption_style is None:
            args.caption_style = 1
            if ask:
                args.caption_style = ask_choice("Caption style:", [
                    "Big bold words, 3 at a time (Shorts style)",
                    "Small normal subtitle lines"])
        if args.language is None and ask and (args.captions or args.auto_metadata):
            args.language = input("Speech language (en, hi, hinglish, ...) or Enter for auto: ").strip() or None
    if args.gpu is None:
        args.gpu = ask and ask_yes_no("Use your NVIDIA GPU for faster encoding?")
    if args.upload is None:
        args.upload = ask and ask_yes_no("Upload finished clips to YouTube automatically?",
                                         default=False)
    if args.ai is None and ask:
        if ask_yes_no("Let an AI watch each clip and write its title, description and tags?",
                      default=False):
            which = ask_choice("Which AI?", ["Gemini (free key from Google AI Studio)",
                                             "Ollama (free, runs on your own PC)"])
            args.ai = ["gemini", "ollama"][which - 1]
    if args.ai == "none":
        args.ai = None
    if not args.chat and ask and not args.live:
        auto = Path(args.video).with_suffix("").with_suffix(".live_chat.json")
        if auto.is_file() and ask_yes_no(f"Found chat file '{auto.name}'. Use it?"):
            args.chat = str(auto)
        else:
            path = input("Chat file (YouTube or Twitch .json) to use? Press Enter to skip: ").strip().strip('"')
            args.chat = path or None
    if args.chat and not Path(args.chat).is_file():
        sys.exit(f"Chat file not found: {args.chat}")

    if args.ai:
        check_ai(args)

    if args.vertical in ("track", "facecam") and not (args.vertical == "facecam" and LAYOUT.get("box")):
        try:
            import cv2
            if not hasattr(cv2, "CascadeClassifier"):  # OpenCV 5 removed the face finder
                raise ImportError
        except ImportError:
            sys.exit("Face layouts need OpenCV 4 (not 5). Run: "
                     "py -3.12 -m pip install \"opencv-python-headless<5\"")

    if args.upload:
        try:
            import youtube_upload
            youtube_upload.get_service()  # sign in now (opens your browser the first time)
        except Exception as err:
            sys.exit(f"YouTube sign-in failed: {err}")
        print("Note: YouTube can lock uploads from unverified API projects as private. "
              "Every clip is also listed in youtube_metadata.csv for review or manual upload.")
        waiting = queue_summary()
        if waiting:
            print(f"Upload queue: {waiting}.")
            if process_upload_queue(args):
                print("Upload queue: " + (queue_summary() or "empty now") + ".")

    if args.live:
        if args.sensitivity is None:
            args.sensitivity = 2
            if ask:
                args.sensitivity = ask_choice("How sensitive should hype detection be?", [
                    "Only the big moments (fewer clips)",
                    "Balanced (recommended)",
                    "Catch more (lots of clips)"], default=2)
        run_live(args)
        return

    # ----- find the highlights -----
    print("\nAnalysing audio (long streams take a few minutes)...")
    if args.smart_pick:
        rms, cent, voice = audio_features(args.video)
        length = len(rms)
        if length == 0:
            sys.exit("Could not read any audio from that video.")
        print("  Smart picking: looking for loud + excited voice, not just loud sound.")
        audio_score = excitement_score(rms, cent, voice)
    else:
        loud = audio_loudness(args.video)
        length = len(loud)
        if length == 0:
            sys.exit("Could not read any audio from that video.")
        audio_score = normalize(smooth(np.maximum(loud - np.median(loud), 0), 5))
    if args.chat:
        print("Reading chat log...")
        counts = chat_activity(args.chat, length)
        if counts.sum() == 0:
            print("  No chat messages found in that file, using audio only.")
            score = audio_score
        else:
            score = 0.5 * audio_score + 0.5 * normalize(smooth(counts, 10))
    else:
        score = audio_score

    clips = pick_clips(score, args.top, args.auto_length, args.length,
                       args.min_len, args.max_len, args.pad)
    if not clips:
        sys.exit("No highlights found. Try a video with louder moments or add a chat file.")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\nFound {len(clips)} highlights. Making clips...")
    for i, (t, s, start, end) in enumerate(clips, 1):
        out_path = out_dir / f"clip_{i:02d}_at_{fmt(t)}.mp4"
        print(f"  {i:2d}. {fmt(t)}  score {s:.2f}  {end - start}s long  ->  {out_path.name}")
        with tempfile.TemporaryDirectory() as tmp:
            ass_dir = None
            transcript = ""
            words = []
            if args.captions or args.auto_metadata or getattr(args, "ai", None):
                try:
                    words = transcribe_words(args.video, start, end, tmp, args.model, args.language)
                    transcript = " ".join(w[2] for w in words)
                except Exception as err:
                    if args.captions:
                        raise
                    print(f"      (automatic title hook skipped: {str(err)[:120]})")
            if args.captions:
                if words:
                    write_ass(words, Path(tmp) / "caps.ass", bool(args.vertical),
                              args.caption_style)
                    ass_dir = tmp
                else:
                    print("      (no speech found in this clip, skipping captions)")
            cut_clip(args.video, start, end, out_path, args.precise, args.gpu,
                     args.vertical, ass_dir)
            meta = ai_metadata(args, out_path, transcript)
            upload_if_wanted(args, out_path, meta, transcript)
            cleanup_old_clips(out_path.parent, getattr(args, "keep_days", 0))

    if args.upload and load_queue():
        print("\nUpload queue: " + queue_summary() + ". Trying the ones that are due...")
        process_upload_queue(args)
        if load_queue():
            print("Upload queue: " + queue_summary() + ". They upload by themselves on the next run, "
                  "or press 'Upload waiting clips now'.")
    print(f"\nDone! Clips are in the '{out_dir}' folder.")


if __name__ == "__main__":
    pause = os.environ.get("HC_PAUSE_AT_END")  # set by multi_live.py so windows stay open
    try:
        main()
    except SystemExit as e:
        if isinstance(e.code, str):
            print(e.code)  # show the message before the pause
            e.code = 1
        if pause:
            input("\nPress Enter to close this window...")
        raise
    else:
        if pause:
            input("\nPress Enter to close this window...")
