import io
import shutil
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw
from cleanup_preview import preview_frame


class ProcessedFramePreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder=Path(self.tmp.name)
        self.logo=self.folder/"bajo.png"
        logo=Image.new("RGBA",(170,80),(0,0,0,0))
        ImageDraw.Draw(logo).rounded_rectangle((1,1,167,78),radius=12,fill=(70,245,30,255))
        logo.save(self.logo)
        image=Image.new("RGB",(500,700),(22,62,205))
        draw=ImageDraw.Draw(image)
        draw.rectangle((60,70,190,150),fill=(235,25,15))
        frame=io.BytesIO()
        image.save(frame,format="JPEG",quality=95)
        self.raw=frame.getvalue()
        self.mask=[{"x":10,"y":9,"w":34,"h":14}]

    @unittest.skipUnless(shutil.which("ffmpeg"),"FFmpeg required for true edited preview")
    def test_changes_old_graphic_and_adds_logo_without_overwriting_source(self):
        data=preview_frame(self.raw,self.mask,self.logo,logo_size=24)
        with Image.open(io.BytesIO(data)) as output:
            self.assertEqual(output.size,(500,700))
            erased=output.convert("RGB").getpixel((110,105))
            self.assertGreater(erased[2],erased[0],erased)
            newlogo=output.convert("RGB").getpixel((410,43))
            self.assertGreater(newlogo[1],newlogo[0]+45,newlogo)
        with Image.open(io.BytesIO(self.raw)) as original:
            red=original.getpixel((110,105))
            self.assertGreater(red[0],red[2]+90,red)
        self.assertGreater(len(data),2000)

    def test_no_cleanup_does_not_claim_old_graphic_removed(self):
        data=preview_frame(self.raw,[],None)
        with Image.open(io.BytesIO(data)) as output:
            red=output.getpixel((110,105))
            self.assertGreater(red[0],red[2]+80,red)

    @unittest.skipUnless(shutil.which("ffmpeg"),"FFmpeg required")
    def test_crop_reduces_height_and_logo_stays_visible(self):
        data=preview_frame(self.raw,self.mask,self.logo,
                           crop_top=10,crop_bottom=10,
                           position="bottom_left",logo_size=24)
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual(image.width,500)
            self.assertAlmostEqual(image.height,560,delta=2)

    def test_bad_image_masks_and_dimensions_are_rejected(self):
        for data in (b"",b"not-an-image",b"\xff\xd8\xff"):
            with self.assertRaises(ValueError):
                preview_frame(data,[],None)
        with self.assertRaises(ValueError):
            preview_frame(self.raw,[{"x":1,"y":1,"w":99,"h":40}],None)
        with self.assertRaises(ValueError):
            preview_frame(self.raw,[],self.logo,logo_size=80)


if __name__=="__main__":
    unittest.main()
