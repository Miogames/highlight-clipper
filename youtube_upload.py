"""Official YouTube Data API publishing helpers for Highlight Clipper.

Setup (once):
  1. Create an OAuth *Desktop app* client in Google Cloud with YouTube Data API v3 enabled.
  2. Save the downloaded OAuth file as ``client_secret.json`` next to this file.
  3. ``py -3.12 -m pip install google-api-python-client google-auth-oauthlib``

The first upload opens a Google sign-in page. ``youtube_token.json`` is created locally
after permission is granted. Both JSON files are private credentials; never share them
or commit them to source control.
"""
from __future__ import annotations

import mimetypes
import json
import random
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
# ``youtube`` is needed for optional playlist insertion; ``youtube.upload`` covers uploads.
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]
SECRETS = HERE / "client_secret.json"
TOKEN = HERE / "youtube_token.json"
RETRIABLE_STATUS_CODES = {500, 502, 503, 504}


def get_service():
    """Open the OAuth sign-in flow when needed and return a YouTube API service."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if TOKEN.is_file():
        try:
            saved = json.loads(TOKEN.read_text(encoding="utf-8"))
            # A token issued to the old upload-only version needs one fresh consent for playlists.
            if set(SCOPES).issubset(set(saved.get("scopes", []))):
                creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
        except (OSError, ValueError, TypeError):
            creds = None
    if creds and not creds.valid and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:
            creds = None
    if not creds or not creds.valid:
        if not SECRETS.is_file():
            raise RuntimeError(
                f"OAuth file not found: {SECRETS.name}. See README.md for the one-time setup."
            )
        creds = InstalledAppFlow.from_client_secrets_file(str(SECRETS), SCOPES).run_local_server(port=0)
    TOKEN.write_text(creds.to_json(), encoding="utf-8")
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def clean_title(value: str) -> str:
    """Return a valid, readable title within YouTube's 100-character limit."""
    title = " ".join((value or "").replace("<", "").replace(">", "").split())
    return title[:100].rstrip() or "Stream highlight"


def clean_tags(values) -> list[str]:
    """Remove duplicates and cap tags so metadata remains relevant and API-safe."""
    result, seen, total = [], set(), 0
    for value in values or []:
        tag = " ".join(str(value).replace("<", "").replace(">", "").split())[:60]
        key = tag.casefold()
        if not tag or key in seen:
            continue
        # YouTube limits a video's combined tags to 500 characters.
        extra = len(tag) + (1 if result else 0)
        if total + extra > 480:
            break
        result.append(tag)
        seen.add(key)
        total += extra
    return result


def make_body(title, description="", privacy="private", *, tags=None, category="20",
              publish_at=None, made_for_kids=False):
    """Build a compliant video resource for ``videos.insert``."""
    if publish_at and privacy != "private":
        raise ValueError("Scheduled uploads must use private visibility until the publish time.")
    status = {
        "privacyStatus": privacy,
        "selfDeclaredMadeForKids": bool(made_for_kids),
        "embeddable": True,
        "publicStatsViewable": True,
    }
    if publish_at:
        status["publishAt"] = publish_at
    return {
        "snippet": {
            "title": clean_title(title),
            "description": (description or "")[:4900],
            "categoryId": str(category or "20"),
            "tags": clean_tags(tags),
        },
        "status": status,
    }


def _resumable_upload(request):
    """Resume transiently failed uploads instead of discarding a finished clip."""
    from googleapiclient.errors import HttpError

    response = None
    retries = 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                print(f"      upload {int(status.progress() * 100):3d}%", end="\r", flush=True)
            retries = 0
        except HttpError as error:
            if error.resp.status not in RETRIABLE_STATUS_CODES:
                raise
            retries += 1
        except (ConnectionError, TimeoutError, OSError):
            retries += 1
        if retries:
            if retries > 6:
                raise RuntimeError("Upload retry limit reached; the clip remains saved locally.")
            delay = min(60, (2 ** retries) + random.random())
            print(f"      upload paused; retrying in {delay:.0f}s ({retries}/6)")
            time.sleep(delay)
    print(" " * 32, end="\r", flush=True)
    return response


def upload_clip(service, path, title, description="", privacy="private", *, tags=None,
                category="20", publish_at=None, made_for_kids=False, playlist_id=None,
                thumbnail=None):
    """Upload one video and optionally add it to a playlist and set its thumbnail.

    Returns the newly created video ID. A thumbnail is optional because a frame grab or
    generic image is often worse than a deliberately designed, truthful thumbnail.
    """
    from googleapiclient.http import MediaFileUpload

    path = Path(path)
    mime = mimetypes.guess_type(path.name)[0] or "video/mp4"
    media = MediaFileUpload(str(path), mimetype=mime, chunksize=8 * 1024 * 1024, resumable=True)
    request = service.videos().insert(
        part="snippet,status",
        body=make_body(title, description, privacy, tags=tags, category=category,
                       publish_at=publish_at, made_for_kids=made_for_kids),
        media_body=media,
    )
    response = _resumable_upload(request)
    video_id = response["id"]

    if thumbnail:
        try:
            thumbnail_path = Path(thumbnail)
            if not thumbnail_path.is_file():
                raise FileNotFoundError(f"Thumbnail file not found: {thumbnail_path}")
            image_mime = mimetypes.guess_type(thumbnail_path.name)[0] or "image/jpeg"
            image = MediaFileUpload(str(thumbnail_path), mimetype=image_mime, resumable=False)
            service.thumbnails().set(videoId=video_id, media_body=image).execute()
        except Exception as error:
            print(f"      Uploaded, but the thumbnail was not set: {str(error)[:160]}")

    if playlist_id:
        try:
            service.playlistItems().insert(
                part="snippet",
                body={"snippet": {"playlistId": playlist_id,
                                  "resourceId": {"kind": "youtube#video", "videoId": video_id}}},
            ).execute()
        except Exception as error:
            print(f"      Uploaded, but was not added to the playlist: {str(error)[:160]}")
    return video_id
