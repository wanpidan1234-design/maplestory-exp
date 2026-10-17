"""Actual OCR smoke on generated Chinese text; not an in-game accuracy claim."""
from pathlib import Path
import unittest

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from quiz_bank import QuizEntry, match_question
from quiz_helper import QuizOCR


class RealQuizOCRTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.font_path = Path('C:/Windows/Fonts/msyh.ttc')
        if not cls.font_path.exists():
            raise unittest.SkipTest('Chinese test font not installed')
        cls.engine = QuizOCR()

    def test_multiline_question_and_options_are_recognized(self):
        image = Image.new('RGB', (1040, 280), '#ffffff')
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype(str(self.font_path), 30)
        draw.multiline_text((24, 20), '以下哪个地方不是射手村的地图？\n1. 废弃都市\n2. 射手村公园\n3. 蘑菇公园',
                            fill='#202020', font=font, spacing=18)
        text, confidence = self.engine.recognize(np.asarray(image)[:, :, ::-1].copy())
        self.assertIn('射手村', text)
        self.assertIn('废弃都市', text)
        entry = QuizEntry('generated', '以下哪个地方不是射手村的地图？', '废弃都市')
        result = match_question(text, confidence=confidence, entries=(entry,))
        self.assertEqual(result.status, 'matched', (text, confidence, result))
        self.assertEqual(result.entry.answer, '废弃都市')

    def test_blank_image_never_returns_a_confirmed_answer(self):
        text, confidence = self.engine.recognize(np.full((240, 800, 3), 255, dtype=np.uint8))
        entry = QuizEntry('generated', '以下哪个地方不是射手村的地图？', '废弃都市')
        self.assertNotEqual(match_question(text, confidence=confidence, entries=(entry,)).status, 'matched')


if __name__ == '__main__':
    unittest.main()
