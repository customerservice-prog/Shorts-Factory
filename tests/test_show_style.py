import tempfile
import unittest
from pathlib import Path
from PIL import Image
from show_style import create_follow_banner, create_subtitle_file


class ShowVisualTests(unittest.TestCase):
    def test_follow_banner_has_transparency_and_real_graphics(self):
        with tempfile.TemporaryDirectory() as folder:
            logo = create_follow_banner(Path(folder) / "banner.png")
            with Image.open(logo) as pic:
                self.assertEqual(pic.mode, "RGBA")
                self.assertEqual(pic.size, (950, 145))
                self.assertEqual(pic.getchannel("A").getextrema(), (0, 255))
                self.assertGreater(logo.stat().st_size, 8000)

    def test_time_synced_spanish_quotation(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/"timed.ass"
            words=[
                {"start": .3, "end": .6, "text": "Mira"},
                {"start": .63, "end": .9, "text": "esto"},
                {"start": 1.1, "end": 1.4, "text": "¡qué"},
                {"start": 1.43, "end": 1.8, "text": "duro!"}
            ]
            total=create_subtitle_file(target, words, 660, 1280, style="show")
            content=target.read_text("utf-8")
            self.assertGreaterEqual(total, 1)
            self.assertIn("Mira esto", content)
            self.assertIn("¡qué duro!", content)
            self.assertIn("PlayResX: 660", content)
            self.assertIn("Dialogue:", content)
            self.assertNotIn("La música se vive aquí",content)

    def test_malicious_ass_control_text_removed(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"captions.ass"
            count=create_subtitle_file(path,[{"start":0.1,"end":1,
                "text":r"hello {\pos(0,0)} world"}],1080,1920)
            self.assertEqual(count,1)
            text=path.read_text()
            self.assertNotIn(r"\pos",text)
            self.assertIn("hello pos(0,0) world",text)

    def test_unrecognized_words_not_fabricated(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/"captions.ass"
            self.assertEqual(create_subtitle_file(target,[],660,1280),0)
            self.assertNotIn("Dialogue: 0,",target.read_text())


if __name__=="__main__":
    unittest.main()
