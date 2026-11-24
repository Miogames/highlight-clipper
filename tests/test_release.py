"""Run from the source folder: py -3.12 -m unittest discover -s tests -v"""
import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

import account_setup
import highlight_clipper as hc
import youtube_upload


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)

    def test_short_audio_scores(self):
        for seconds in range(1, 12):
            with self.subTest(seconds=seconds):
                signal = np.arange(seconds)
                scores = hc.excitement_score(signal, signal, signal)
                self.assertEqual(scores.shape, (seconds,))
                self.assertTrue(np.isfinite(scores).all())

    def test_key_save_replace_and_environment_precedence(self):
        with patch.object(hc, "APP_DIR", self.folder), patch.dict(os.environ, {"TEST_CLIPPER_KEY": ""}):
            for value in ("first-example", "updated-example"):
                account_setup.save_private(self.folder / "key.txt", value)
                self.assertEqual(hc.read_secret("TEST_CLIPPER_KEY", "key.txt"), value)
            os.environ["TEST_CLIPPER_KEY"] = "environment-example"
            self.assertEqual(hc.read_secret("TEST_CLIPPER_KEY", "key.txt"), "environment-example")
            self.assertEqual(len(list(self.folder.iterdir())), 1)

    def test_oauth_import(self):
        file = self.folder / "client.json"
        client = dict(client_id="sample", client_secret="sample",
                      auth_uri="https://accounts.google.com/o/oauth2/auth",
                      token_uri="https://oauth2.googleapis.com/token")
        file.write_text(json.dumps({"installed": client}))
        self.assertEqual(json.loads(account_setup.desktop_client(file)), {"installed": client})
        for invalid in ("", "[]", '{"web":{}}', '{"installed":{}}'):
            file.write_text(invalid)
            with self.assertRaises(ValueError):
                account_setup.desktop_client(file)

    def test_live_links_and_replay_chat(self):
        for url, key in [("https://youtu.be/abcdefghijk", "abcdefghijk"),
                         ("https://twitch.tv/sample", "twitch_sample"),
                         ("https://kick.com/sample", "kick_sample")]:
            self.assertEqual(hc.stream_key(hc.watch_link(url)), key)
        file = self.folder / "chat.json"
        file.write_text(json.dumps({"comments": [{"content_offset_seconds": 2.4}]}))
        self.assertEqual(hc.chat_activity(file, 4).tolist(), [0, 0, 1, 0])

    def test_private_scheduled_upload(self):
        when = "2099-01-01T00:00:00Z"
        body = youtube_upload.make_body("Example", publish_at=when)
        self.assertEqual(body["status"]["publishAt"], when)
        with self.assertRaises(ValueError):
            youtube_upload.make_body("Example", privacy="public", publish_at=when)

    def test_stop_while_stream_is_offline(self):
        stop = self.folder / "stop.flag"
        argv = ["clipper", "--live", "https://youtu.be/abcdefghijk", "--no-ask",
                "--no-upload", "--out", str(self.folder / "clips")]
        with patch.dict(os.environ, {"HC_STOP_FILE": str(stop)}), patch.object(sys, "argv", argv), \
                patch.object(hc, "LIVE_MODE", False), \
                patch.object(hc, "resolve_live_stream_urls", return_value=([], "offline")) as resolve, \
                patch.object(hc.time, "sleep", side_effect=lambda _: stop.touch()) as wait:
            hc.main()
            resolve.assert_called_once()
            wait.assert_called_once_with(1)

    def test_upload_resumes_after_timeout(self):
        request = Mock()
        request.next_chunk.side_effect = [TimeoutError(), (Mock(progress=lambda: 0.5), None),
                                          (None, {"id": "sample"})]
        with patch("youtube_upload.time.sleep") as delay:
            self.assertEqual(youtube_upload._resumable_upload(request), {"id": "sample"})
            delay.assert_called_once()

    def test_queue_persists_and_retries_once(self):
        clip = self.folder / "example.mp4"
        clip.touch()
        with patch.object(hc, "QUEUE_FILE", self.folder / "queue.json"):
            for _ in range(2):
                hc.enqueue_job({"path": str(clip)}, None, "network", "offline", 3600)
            self.assertEqual(len(hc.load_queue()), 1)
            with patch.object(hc, "_upload_job", return_value="sample") as upload:
                self.assertEqual(hc.process_upload_queue(), 0)
                self.assertEqual(hc.process_upload_queue(force=True), 1)
                upload.assert_called_once()
            self.assertEqual(hc.load_queue(), [])
            self.assertTrue(clip.exists())

    def test_local_clip_from_file_to_manifest(self):
        video = self.folder / "video with spaces.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=160x90:r=10",
                        "-f", "lavfi", "-i", "sine=frequency=440", "-af",
                        "volume=if(between(t\\,2\\,3)\\,1\\,0.05):eval=frame",
                        "-t", "6", "-c:v", "libx264", "-c:a", "aac", str(video)], check=True)
        out = self.folder / "new folder" / "clips"
        subprocess.run([sys.executable, hc.__file__, str(video), "--out", str(out), "--top", "1",
                        "--no-ask", "--no-captions", "--no-upload", "--ai", "none", "--no-gpu"],
                       check=True, capture_output=True)
        with (out / "youtube_metadata.csv").open(encoding="utf-8-sig", newline="") as stream:
            row, = csv.DictReader(stream)
        result = out / row["file"]
        self.assertTrue(result.with_suffix(".txt").is_file())
        info = subprocess.check_output(["ffprobe", "-v", "error", "-show_streams",
                                        "-of", "json", str(result)])
        self.assertEqual({s["codec_type"] for s in json.loads(info)["streams"]}, {"video", "audio"})
