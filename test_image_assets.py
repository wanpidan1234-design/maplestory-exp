"""A portable release must read templates from Chinese Windows paths."""
from pathlib import Path
import tempfile
import unittest

import cv2
import numpy as np

from image_assets import read_image


class ImageAssetTests(unittest.TestCase):
    def test_unicode_path_preserves_decoded_pixels(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '冒险岛 独立版' / 'template.png'
            path.parent.mkdir()
            pixels = np.arange(60, dtype=np.uint8).reshape(6, 10)
            ok, encoded = cv2.imencode('.png', pixels)
            self.assertTrue(ok)
            path.write_bytes(encoded.tobytes())
            np.testing.assert_array_equal(read_image(path, cv2.IMREAD_GRAYSCALE), pixels)

    def test_unreadable_assets_keep_missing_template_semantics(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'template.png'
            self.assertIsNone(read_image(path))
            for data in (b'', b'not an image'):
                path.write_bytes(data)
                self.assertIsNone(read_image(path))


if __name__ == '__main__':
    unittest.main()
