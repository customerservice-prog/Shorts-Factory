import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from review import risk_review

class MonetizationRiskTests(unittest.TestCase):
    def job(self, platform="facebook_reels", **updates):
        base = {"target_platform": platform, "rights": "unknown", "audio_rights": "unknown",
                "originality": "minimal", "filename": "Artist - Official Video.mp4",
                "captions": "pop", "layout": "blur", "rights_evidence": ""}
        base.update(updates)
        return base
    def clips(self, length=44):
        return [{"file":"clip_01.mp4","start":0,"end":length,"length":length,
                 "captions":True,"layout":"blur","highlight_score":70,
                 "technical":{"verified":True}}]
    def test_reused_music_content_is_high_risk_for_facebook(self):
        review=risk_review(self.job(),self.clips(),[],"es",100,True)
        self.assertEqual(review["status"],"high_risk")
        self.assertFalse(review["monetization_guaranteed"])
        self.assertEqual(review["clip_reviews"][0]["status"],"manual_review_required")
    def test_instagram_is_a_separate_platform(self):
        review=risk_review(self.job("instagram_reels"),self.clips(),[],"es",100,True)
        titles=[x["title"] for x in review["source_findings"]]
        self.assertIn("Instagram monetization is feature/account specific",titles)
    def test_tiktok_duration_flag(self):
        review=risk_review(self.job("tiktok_rewards"),self.clips(),[],"es",100,True)
        self.assertTrue(any(x["level"]=="high" for x in review["clip_reviews"][0]["checks"]))
    def test_owned_content_is_still_not_certified(self):
        review=risk_review(self.job("youtube_shorts",rights="owned",
                  audio_rights="owned",originality="original",filename="My video.mp4"),
                  self.clips(),[],"en",100,True)
        self.assertFalse(review["monetization_guaranteed"])
        self.assertEqual(review["status"],"manual_review_required")

class BrandLogoTests(unittest.TestCase):
    def test_bajo_mundo_logo_preserves_visible_artwork_and_removes_white_matte(self):
        from PIL import Image
        import branding
        with tempfile.TemporaryDirectory() as temporary:
            target=Path(temporary)
            with patch.object(branding,"BRAND_DIR",target),patch.object(branding,"DEFAULT_LOGO",target/"logo.png"):
                logo=branding.prepare_default_logo()
                self.assertTrue(logo.exists())
                with Image.open(logo) as image:
                    self.assertEqual(image.mode,"RGBA")
                    alpha=image.getchannel("A")
                    smallest,largest=alpha.getextrema()
                    self.assertEqual(smallest,0)
                    self.assertGreater(largest,200)

    def test_studio_watermark_is_transparent_and_readable_size(self):
        from PIL import Image
        import branding
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            with patch.object(branding, "BRAND_DIR", folder), patch.object(
                branding, "STUDIO_LOGO", folder / "studio.png"
            ):
                asset = branding.brand_path("bajo-mundo-studio")
                self.assertTrue(asset.exists())
                with Image.open(asset) as image:
                    self.assertEqual(image.mode, "RGBA")
                    self.assertEqual(image.size, (1000, 340))
                    self.assertEqual(image.getchannel("A").getextrema(), (0, 255))
                    self.assertGreater(asset.stat().st_size, 7000)
        brands = branding.list_brands()
        self.assertEqual(brands[0]["id"], "bajo-mundo")
        self.assertEqual(brands[1]["id"], "bajo-mundo-studio")

if __name__=="__main__":
    unittest.main()
