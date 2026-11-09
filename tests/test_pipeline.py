import tempfile
import unittest
from pathlib import Path

from pipeline import ass_time, clip_title, score_window, select_clips, write_captions


class PipelineTests(unittest.TestCase):
    def test_timing(self):
        self.assertEqual(ass_time(62.25), "0:01:02.25")
        self.assertEqual(ass_time(-1), "0:00:00.00")

    def test_no_speech_fallback(self):
        clips = select_clips([], 200, 3, 45)
        self.assertEqual(len(clips), 3)
        self.assertTrue(all(c["end"] > c["start"] for c in clips))

    def test_title_from_words(self):
        title = clip_title([{"text": "hello"}, {"text": "world."}], 1)
        self.assertEqual(title, "Hello world")

    def test_score_ranking(self):
        words = [{"start": 0, "end": 1, "text": "Why"},
                 {"start": 1, "end": 2, "text": "money?"},
                 {"start": 2, "end": 3, "text": "Listen!"}]
        self.assertGreater(score_window(words, 0, 3, 20), 35)

    def test_subtitle_file(self):
        words = [{"start": 1.0, "end": 1.5, "text": "Hello"},
                 {"start": 1.5, "end": 2.0, "text": "world!"}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "captions.ass"
            self.assertTrue(write_captions(path, words, 0, 5, "pop"))
            result = path.read_text()
            self.assertIn("Dialogue:", result)
            self.assertIn("HELLO", result)
            self.assertIn(r"{\k", result)


if __name__ == "__main__":
    unittest.main()
