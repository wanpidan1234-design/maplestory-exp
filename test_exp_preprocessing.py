"""EXP preprocessing must retain geometry and same-frame confidence checks."""
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from experience_ocr import ExperienceOCR


class AbsolutePreprocessingTests(unittest.TestCase):
    def setUp(self):
        self.ocr=ExperienceOCR.__new__(ExperienceOCR)
        self.ocr.last_diagnostics={'candidates':[]}
        self.frame=np.zeros((25,110,3),dtype=np.uint8)
        for x in range(8,85,11):
            self.frame[5:18,x:x+8]=255

    def test_wide_variants_keep_color_and_complete_foreground(self):
        variants=self.ocr._absolute_variants(self.frame)
        self.assertIs(variants[0],self.frame)
        for variant,factor,inverted in zip(variants[1:],(3,4),(True,False)):
            mask=255-variant if inverted else variant
            reduced=mask[::factor,::factor]
            x,y,w,h=cv2.boundingRect(cv2.findNonZero(reduced))
            self.assertGreater(x,0)
            self.assertGreater(y,0)
            np.testing.assert_array_equal(reduced[y:y+h,x:x+w],self.frame[5:18,8:82,0])

    def test_short_foreground_retains_calibrated_variants(self):
        small=self.frame[:,:32]
        expected=self.ocr.variants(small)
        for actual,wanted in zip(self.ocr._absolute_variants(small),expected):
            np.testing.assert_array_equal(actual,wanted)

    def test_million_boundary_keeps_same_vote_rules(self):
        for value in (999999,1000000,1000001,1234567,99999999999999):
            with self.subTest(value=value):
                with patch.object(self.ocr,'recognize_exp',return_value=(str(value),.85)):
                    self.assertEqual(self.ocr._absolute(self.frame,[]),(value,None))

    def test_strong_conflict_is_not_outvoted_by_preprocessed_views(self):
        for answers in ([('900905',.85),('800905',.80),('900905',.85)],
                        [('1000000',.85),('1000001',.85),('1000001',.85)]):
            with patch.object(self.ocr,'recognize_exp',side_effect=answers) as infer:
                self.assertEqual(self.ocr._absolute(self.frame,[]),(None,'absolute_conflict'))
                self.assertEqual(infer.call_count,3)

    def test_weak_spaced_vote_requires_two_new_qualified_views(self):
        for score,expected in ((.74,None),(.75,901449)):
            answers=[('901 449',.73),('901449',score),('901449',score)]
            with patch.object(self.ocr,'recognize_exp',side_effect=answers):
                self.assertEqual(self.ocr._absolute(self.frame,[])[0],expected)


if __name__=='__main__':
    unittest.main()
