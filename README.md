# Highlight Clipper Publisher v2

Built by [Miogames](https://github.com/Miogames).

Highlight Clipper turns videos and live streams into highlight clips on Windows. You can
add captions, create vertical videos, and optionally upload or schedule clips on your own YouTube channel.

## Install on Windows

1. On the GitHub repository page, choose **Code → Download ZIP**. Right-click the ZIP, select **Extract All**, and open the extracted folder containing `Start Highlight Clipper.bat`.
2. Open **PowerShell** and run these commands to install Python 3.12 and FFmpeg. The FFmpeg package also provides FFprobe, which the app uses to inspect videos.

   ```powershell
   winget install --id Python.Python.3.12 --exact --source winget
   winget install --id Gyan.FFmpeg --exact --source winget
   ```

3. Close PowerShell and open a new window. Check that all three commands print version information.

   ```powershell
   py -3.12 --version
   ffmpeg -version
   ffprobe -version
   ```

4. In the extracted folder, double-click **Install dependencies.bat**. It downloads the Python packages listed in `requirements.txt`. Wait for **Dependencies installed**.
5. Double-click **Start Highlight Clipper.bat**. Choose a video with audio and press **Start**. When it finishes, click **Open clips folder**. The first captioned run downloads a speech model and needs internet access.

If Windows cannot find `winget`, install or update [Microsoft App Installer](https://learn.microsoft.com/en-us/windows/package-manager/winget/), reopen PowerShell, and retry step 2. If FFmpeg or FFprobe is missing after installation, check that the folder containing both executables is on your Windows **Path**, then reopen the app.

The CPU can run the full local clipping workflow. For faster speech recognition on a supported NVIDIA GPU, see [GPU speech reading](#gpu-speech-reading). API keys are optional for local clipping and captions.

## Optional accounts and API keys

Open **Getting started → Accounts and API keys** for optional features.

| If you want to… | What to set up once | What to do next |
| --- | --- | --- |
| Generate titles with Gemini | Follow **Get a Gemini key**, paste your key, and click **Save key**. | Select **Gemini** under **AI writes title, description and tags**. |
| Upload to your channel | Enable **YouTube Data API v3** and import your Desktop app JSON using the steps below. | Turn on automatic upload. Google asks you to choose your account and channel on first use. |
| See a channel report | Use the same Desktop app JSON and enable **YouTube Analytics API** in that project. | Click **Analyse my channel** and approve analytics access. |
| Research trending videos for AI metadata | Enable **YouTube Data API v3**, create a separate API key in Google Cloud, then paste and save it under **YouTube trend research**. | Select an AI provider. Trend research runs when the AI identifies a topic. |

The application always writes a `youtube_metadata.csv` next to your clips. Use it to review or
manually upload metadata even if direct upload is disabled.

## One-time YouTube connection

1. Create or choose a project in [Google Cloud Console](https://console.cloud.google.com/).
2. Enable **YouTube Data API v3**.
3. Create an OAuth client ID of type **Desktop app**.
4. If prompted, configure the OAuth consent screen. For an external app in testing, add
   the Google account you will sign in with as a test user. Download the Desktop app client's
   JSON. In the Clipper, open **Accounts and API keys → Import Desktop app JSON** and select
   the download. The Clipper saves it under the filename it needs.
5. Start a direct upload and sign in to the channel that owns the content. The app creates its
   private `youtube_token.json` automatically.

## Metadata and AI

- **Gemini/Ollama:** The newer clipper's existing AI options remain available. Gemini needs your
  own `ai_key.txt`; Ollama runs locally. Both generate a draft from sampled video frames and
  spoken text. Review it before publishing.
- **Fallback title:** Used when AI is off or unavailable. Enable the speech hook for a unique,
  truthful title based on the actual clip dialogue.
- **Description, credit, game, and tags:** Add only information that truthfully relates to the
  video. Credit does not replace permission or a licence.
- **Optional trend research:** Put your own YouTube Data API key in `youtube_api_key.txt` only if
  you want the AI to inspect recent, similar Shorts for inspiration. It is instructed not to copy
  titles or tags.
- **Thumbnail:** Choose an accurate JPG/PNG. The same selected image is applied to each clip in
  a run.
- **Playlist:** Paste an existing playlist ID (normally starts with `PL`).
- **Schedule:** Use a future ISO 8601 timestamp that includes its zone, such as
  `2026-11-18T19:00:00+05:30`. Scheduled uploads must be private and can be spaced with the
  minutes-between-clips setting.
- **Made for kids:** Switch it on only if the content is child-directed.

## Command-line example

```powershell
py -3.12 .\highlight_clipper.py .\stream.mp4 --no-ask --top 6 --vertical blur --captions `
  --upload --privacy private --ai gemini --title "Stream highlight" --auto-metadata `
  --game "Valorant" --tags "valorant clutch, ranked gameplay" `
  --publish-at "2026-11-18T19:00:00+05:30" --publish-gap 180
```

## Security and content checks

- This delivery intentionally excludes credentials, API keys, existing clips, and upload logs.
  `.gitignore` prevents accidentally committing `client_secret.json`, `youtube_token.json`,
  `ai_key.txt`, and `youtube_api_key.txt`.
- Upload only clips you own or are permitted to publish.
- New or unverified API projects can have videos locked as private by YouTube. Resolve that in
  YouTube Studio; it is not caused by the clipper.

## New in this version

- **Smart clip picking (video files):** moments are scored by an *excited voice* (louder than the
  surrounding audio, brighter sound, voice-band energy) instead of plain loudness, so long loud
  music or constant gunfire stops winning. Turn off with the switch in *Video options* or
  `--no-smart-pick`. Live mode still uses chat speed.
- **Select facecam area:** in *Clip style*, press **Select facecam area...**, drag a box over your
  face cam (use a video file, or a screenshot of your stream for live mode) and press **Use this
  area**. It is remembered in `facecam_box.txt`. Command line: `--face-box x,y,width,height` in
  percent, e.g. `--face-box 72.9,64.8,23.4,27.8`. Used by the *Gameplay on top + face below* layout;
  blank = automatic face detection as before.
- **Hashtags:** every description ends with up to 3 relevant hashtags (yours from the *Hashtags*
  field first, then the AI's, then the game name) plus `#Shorts` for vertical clips.
  `#Shorts` is no longer added to the title, because YouTube only shows hashtags above the title
  from the title when it has any. Use `--shorts-in-title` to bring the old behaviour back.

## Upload queue (automatic retry)

When YouTube refuses an upload because the channel's upload limit is reached (or the network
drops), the clip is **not lost and not skipped**: it goes into `upload_queue.json` and uploads by
itself later.

- The wait between tries is set by **Retry every (hours)** (default 6). While the limit lasts, new
  clips go straight to the queue instead of wasting tries.
- Every normal run first tries the clips that are due, and **as soon as one upload succeeds, all
  waiting clips are sent too**. In live mode a background check runs every minute.
- **Upload waiting clips now** (in *Speed and output*) tries them immediately. Turn on **Keep
  retrying until all are uploaded** to leave it running until the queue is empty (Stop is safe:
  the queue is saved).
- Command line: `highlight_clipper.py --retry-uploads [--retry-wait] [--retry-hours 6]`.
- Real errors (wrong sign-in, rejected video) are tried 3 times, 24 hours apart, then dropped; the
  clip stays in its folder. A clip whose file was deleted is removed from the queue. If a planned
  publish time has passed by the time of the retry, the clip uploads as a normal private video.
- The queue keeps the title, description, tags and settings each clip had when it was made.

## GPU speech reading

Speech reading (captions, and the speech the AI uses for titles and tags) is the slowest step. With
an NVIDIA graphics card it is typically several times faster.

- It is **automatic**: *Speech reading runs on* is set to "Auto (NVIDIA GPU if it works)". If the
  GPU is missing or its libraries are not installed, the app prints why and continues on the CPU, so
  a run never fails because of the GPU.
- To enable it: double-click **Install GPU speech support.bat** once (adds the NVIDIA libraries
  `nvidia-cublas-cu12` and `nvidia-cudnn-cu12`; needs a recent NVIDIA driver), then press
  **Test GPU speech** in the app. The test says plainly whether it works.
- With a GPU you can raise *Caption accuracy* to `medium` for better Hindi/Hinglish text at a speed
  close to the CPU's `small`.
- Command line: `--speech-device auto|cpu` and `--check-gpu`.

## Channel coach (learns from your own YouTube numbers)

Press **Analyse my channel** (section 7) or run `highlight_clipper.py --coach --ai gemini`.

**One-time setup:** in the same Google Cloud project you use for uploading, open *APIs & Services >
Library*, enable **YouTube Analytics API**, then run the coach. A browser opens once: allow
*View YouTube Analytics reports*. (It uses its own token file, `youtube_analytics_token.json`, so
uploading is never affected.)

What it does:
1. Reads your public videos and each one's **first 7 days** of views, watch time and subscribers.
2. Works out, with plain statistics (no AI), which **days and times** and which **title/length styles**
   did better, and says **"not enough videos"** where the data is too thin to trust. Confidence is
   low under 8 videos, medium under 25.
3. Shows who watches (countries, age/gender, where views come from) when YouTube has that data.
4. With AI on (Gemini is free), writes plain advice: what to upload, when, titles/tags, and 3
   experiments for the next 2 weeks. The AI is told to use only your real numbers and to label each
   point `[data]` or `[general]`.
5. Saves `channel_coach_report.md`. With 8+ videos it also saves `title_lessons.json`; the title
   writer then sees your best and weakest titles and learns the style (never copies the words).

Limits: it cannot predict views or guarantee growth; patterns in small channels are mostly noise;
YouTube's API does not give an hour-by-hour "when your viewers are online" chart, so best times are
learned from your own uploads. The free Gemini tier may use your prompts to improve Google products;
use `--ai ollama` to keep everything on your PC.

## When Gemini is slow or down

- A slow or dropped AI answer (timeout, 503) is now treated like any other busy answer: the app
  switches to another Gemini model, and gives up quickly instead of waiting minutes per clip.
- After a real outage the AI **rests for 10 minutes**, so the next clips are not each made to wait.
- A clip whose title could not be written is **no longer left behind**: it goes into the upload
  queue, and the AI writes its title and tags later (using the speech and settings saved with it);
  then it uploads by itself. The queue panel shows how many are waiting for the AI.
- It still never uploads such a clip with a plain "Stream highlight" title unless you pass
  `--allow-fallback-upload`.
