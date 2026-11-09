import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from pipeline import probe_video, render_clip, write_captions


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg needed")
class ExportIntegrationTests(unittest.TestCase):
    def test_vertical_export_with_captions_and_text_brand(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mp4"
            output = root / "clip.mp4"
            captions = root / "captions.ass"
            command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                       "-f", "lavfi", "-i", "testsrc2=size=480x270:rate=15:duration=4",
                       "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)]
            subprocess.run(command, check=True, capture_output=True, timeout=60)
            duration, audio = probe_video(source)
            self.assertGreater(duration, 3)
            self.assertFalse(audio)
            words = [{"start": 0.4, "end": 0.8, "text": "Amazing"},
                     {"start": 0.8, "end": 1.5, "text": "moment!"}]
            self.assertTrue(write_captions(captions, words, 0, 3.4, "pop"))
            render_clip(source, output, 0, 3.4, "blur", captions, "@studio", None, False)
            self.assertTrue(output.exists())
            self.assertGreater(output.stat().st_size, 2000)
            info = json.loads(subprocess.run(
                ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output)],
                check=True, capture_output=True, text=True, timeout=30
            ).stdout)
            vid = next(x for x in info["streams"] if x["codec_type"] == "video")
            self.assertEqual((vid["width"], vid["height"]), (1080, 1920))


if __name__ == "__main__":
    unittest.main()
