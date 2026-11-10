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


    def test_authorized_corner_blur_and_logo_export(self):
        from PIL import Image, ImageDraw
        from review import inspect_export
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, output, logo = root/"input.mp4", root/"short.mp4", root/"brand.png"
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=12:duration=4",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)
            ], check=True, capture_output=True, timeout=60)
            image = Image.new("RGBA",(200,85),(0,0,0,0))
            ImageDraw.Draw(image).rectangle((15,15,180,70),fill=(200,20,20,255))
            image.save(logo)
            render_clip(source,output,0,3.2,"left",None,"",logo,False,"top_right")
            data = inspect_export(output,3.2,False)
            self.assertTrue(data["verified"])
            self.assertEqual((data["width"],data["height"]),(1080,1920))


    def test_left_and_right_focus_actually_select_different_halves(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "split.mp4"
            left = root / "left.mp4"
            right = root / "right.mp4"
            # 9:16 split video: red left panel, blue right panel.
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "color=c=red:s=270x960:r=12:d=3.5",
                "-f", "lavfi", "-i", "color=c=blue:s=270x960:r=12:d=3.5",
                "-filter_complex", "[0:v][1:v]hstack=inputs=2[v]",
                "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)
            ], check=True, capture_output=True, timeout=70)
            render_clip(source,left,0,3,"left",None,"",None,False)
            render_clip(source,right,0,3,"right",None,"",None,False)
            def center_pixel(path):
                raw=subprocess.run([
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", "1",
                    "-i", str(path), "-vf", "crop=1:1:540:960,format=rgb24",
                    "-frames:v", "1", "-f", "rawvideo", "pipe:1"
                ], check=True, capture_output=True, timeout=35).stdout
                return tuple(raw[:3])
            l,r=center_pixel(left),center_pixel(right)
            self.assertGreater(l[0],l[2]+60, (l,r))
            self.assertGreater(r[2],r[0]+60, (l,r))


if __name__ == "__main__":
    unittest.main()
