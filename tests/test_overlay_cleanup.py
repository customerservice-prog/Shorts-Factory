import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from overlay_cleanup import normalize_masks, delogo_filters, suggest_text_masks
from brand_only import brand_full_video, probe_media


class CleanupShapeTests(unittest.TestCase):
    def test_reject_missing_or_large_graphics(self):
        self.assertEqual(normalize_masks("[]"), [])
        with self.assertRaises(ValueError):
            normalize_masks([{"x":5,"y":5,"w":90,"h":20}])
        with self.assertRaises(ValueError):
            normalize_masks([{"x":98,"y":15,"w":10,"h":10}])
        with self.assertRaises(ValueError):
            normalize_masks([{"x":2,"y":3,"w":float("nan"),"h":3}])
        with self.assertRaises(ValueError):
            normalize_masks([{"x":2,"y":3,"w":10,"h":7}]*7)

    def test_percentage_maps_to_original_pixels(self):
        mapped=delogo_filters([{"x":10,"y":10,"w":20,"h":10}],320,600)
        self.assertEqual(len(mapped),1)
        self.assertIn("x=32",mapped[0])
        self.assertIn("y=60",mapped[0])
        self.assertIn("w=64",mapped[0])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),"FFmpeg required")
class CleanupIntegrationTests(unittest.TestCase):
    def test_full_video_graphic_cleanup_and_new_logo(self):
        from PIL import Image, ImageDraw
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            source=root/"original.mp4"
            target=root/"clean-branded.mp4"
            logo=root/"brand.png"
            subprocess.run([
                "ffmpeg","-hide_banner","-loglevel","error","-y",
                "-f","lavfi","-i","color=c=blue:s=320x600:r=12:d=2",
                "-f","lavfi","-i","sine=frequency=440:duration=2",
                "-vf","drawbox=x=38:y=48:w=104:h=60:color=red:t=fill",
                "-c:v","libx264","-pix_fmt","yuv420p","-c:a","aac",
                "-shortest",str(source)
            ],check=True,capture_output=True,timeout=45)
            picture=Image.new("RGBA",(200,75),(0,0,0,0))
            ImageDraw.Draw(picture).rectangle((4,4,196,72),fill=(240,210,50,255))
            picture.save(logo)
            result=brand_full_video(source,target,logo,cleanup_regions=[
                {"x":10,"y":7.5,"w":38,"h":13}
            ])
            self.assertTrue(result["full_length"])
            self.assertEqual(result["cleanup_region_count"],1)
            self.assertFalse(result["cleanup_verified_visual"])
            self.assertTrue(result["logo_added"])
            self.assertAlmostEqual(result["duration"],2,delta=.4)
            self.assertTrue(probe_media(target)["has_audio"])
            self.assertTrue(source.is_file())
            # Check the old overlay area was actually modified by interpolation
            # rather than just ignored by the FFmpeg graph.
            def color(path):
                data=subprocess.run([
                    "ffmpeg","-hide_banner","-loglevel","error",
                    "-ss","1","-i",str(path),
                    "-vf","crop=2:2:75:85,format=rgb24",
                    "-frames:v","1","-f","rawvideo","pipe:1"
                ],check=True,capture_output=True,timeout=25).stdout
                return tuple(data[:3])
            original=color(source)
            cleaned=color(target)
            self.assertGreater(original[0],original[2]+60,(original,cleaned))
            self.assertGreater(cleaned[2],cleaned[0],(original,cleaned))

    def test_suggestions_always_require_review(self):
        # Static generated source with no captions -> no forced removals.
        with tempfile.TemporaryDirectory() as folder:
            original=Path(folder)/"plain.mp4"
            subprocess.run([
                "ffmpeg","-hide_banner","-loglevel","error","-y",
                "-f","lavfi","-i","color=c=black:s=320x480:r=8:d=1",
                "-c:v","libx264","-pix_fmt","yuv420p",str(original)
            ],check=True,capture_output=True,timeout=35)
            report=suggest_text_masks(original,1)
            self.assertTrue(report["requires_confirmation"])
            self.assertIsInstance(report["regions"],list)
            self.assertIn("OCR", report["method"])
            self.assertIn("review_candidates", report)
            self.assertIn("cleanup_difficulty", report)
            self.assertTrue(report["requires_confirmation"])


if __name__=="__main__":
    unittest.main()
