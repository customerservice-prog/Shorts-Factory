import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from PIL import Image, ImageDraw
from brand_only import brand_full_video, probe_media


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "FFmpeg required for media verification")
class FullLengthBrandingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.source = root / "original.mp4"
        self.logo = root / "logo.png"
        self.target = root / "branded.mp4"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=blue:s=320x600:r=15:d=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(self.source)
        ], check=True, capture_output=True, timeout=40)
        picture = Image.new("RGBA", (200,100), (0,0,0,0))
        ImageDraw.Draw(picture).rectangle((1,1,199,99),fill=(245,25,35,255))
        picture.save(self.logo)

    def test_full_length_and_audio_preserved(self):
        before = probe_media(self.source)
        technical = brand_full_video(self.source, self.target, self.logo)
        after = probe_media(self.target)
        self.assertEqual((before["width"], before["height"]),
                         (after["width"], after["height"]))
        self.assertTrue(technical["full_length"])
        self.assertTrue(technical["verified"])
        self.assertTrue(after["has_audio"])
        self.assertAlmostEqual(before["duration"], after["duration"], delta=.4)
        self.assertFalse(technical["captions_added"])

    def test_logo_pixel_is_present_in_export(self):
        brand_full_video(self.source,self.target,self.logo)
        def rgb_pixel(path):
            result = subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", "0.5",
                "-i", str(path), "-vf", "crop=2:2:270:30,format=rgb24",
                "-frames:v", "1", "-f", "rawvideo", "pipe:1"
            ], check=True, capture_output=True, timeout=25)
            return tuple(result.stdout[:3])
        original = rgb_pixel(self.source)
        output = rgb_pixel(self.target)
        self.assertGreater(output[0], original[0]+80, (original,output))

    def test_crop_only_changes_picture_not_video_length(self):
        before = probe_media(self.source)
        technical = brand_full_video(self.source,self.target,self.logo,
                                     crop_top=10,crop_bottom=10)
        self.assertTrue(technical["full_length"])
        self.assertLess(technical["height"], before["height"])
        self.assertAlmostEqual(technical["duration"],before["duration"],delta=.4)

    def test_custom_size_and_opacity(self):
        outcome = brand_full_video(self.source, self.target, self.logo,
                                   logo_size=38, logo_opacity=70)
        self.assertTrue(outcome["logo_added"])
        self.assertEqual(outcome["logo_size"], 38)
        self.assertEqual(outcome["logo_opacity"], 70)
        self.assertTrue(outcome["full_length"])

    def test_existing_logo_can_skip_double_overlay(self):
        result = brand_full_video(self.source, self.target, None)
        self.assertFalse(result["logo_added"])
        self.assertEqual(result["logo_size"], 0)
        self.assertTrue(result["full_length"])
        self.assertTrue(result["audio"])


    def test_original_logo_plus_spanish_show_overlay_keeps_timeline(self):
        from show_style import create_follow_banner, create_subtitle_file
        root = Path(self.tmp.name)
        banner = create_follow_banner(root / "banner.png")
        subs = root / "captions.ass"
        count = create_subtitle_file(
            subs,
            [{"start": 0.3, "end": 0.65, "text": "Mira"},
             {"start": 0.7, "end": 1.2, "text": "esto"},
             {"start": 1.25, "end": 1.6, "text": "ahora"}],
            320, 600, style="show"
        )
        before = probe_media(self.source)
        report = brand_full_video(self.source, self.target, self.logo,
                                  subtitle_file=subs, follow_banner=banner)
        after = probe_media(self.target)
        self.assertGreater(count, 0)
        self.assertTrue(report["captions_added"])
        self.assertTrue(report["follow_banner"])
        self.assertTrue(report["logo_added"])
        self.assertTrue(after["has_audio"])
        self.assertAlmostEqual(before["duration"],after["duration"], delta=.4)

    def test_unsafe_settings_rejected(self):
        with self.assertRaises(ValueError):
            brand_full_video(self.source,self.source,self.logo)
        with self.assertRaises(ValueError):
            brand_full_video(self.source,self.target,self.logo,position="outside")
        with self.assertRaises(ValueError):
            brand_full_video(self.source,self.target,self.logo,crop_top=55)
        with self.assertRaises(ValueError):
            brand_full_video(self.source,self.target,self.logo,logo_size=50)
        with self.assertRaises(ValueError):
            brand_full_video(self.source,self.target,self.logo,logo_opacity=40)
