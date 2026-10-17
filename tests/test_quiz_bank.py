"""Exact matches and visibly tentative answers for wording/OCR differences."""
import math
import unittest

from quiz_bank import QuizEntry, load_bank, match_question, normalize


class QuizMatchingTests(unittest.TestCase):
    def setUp(self):
        self.entry = QuizEntry(
            'fixture-location', '以下哪个地方不是射手村的地图？', '废弃都市',
            aliases=('下列哪个地方不属于射手村的地图？',), source='fixture://local')
        self.entries = (self.entry,)

    def assert_rejected(self, text, **kwargs):
        result = match_question(text, entries=self.entries, **kwargs)
        self.assertNotEqual(result.status, 'matched', (text, result))

    def test_exact_question_and_alias(self):
        for text in (self.entry.question, *self.entry.aliases):
            with self.subTest(text=text):
                result = match_question(text, entries=self.entries)
                self.assertEqual(result.status, 'matched')
                self.assertEqual(result.entry.id, self.entry.id)
                self.assertEqual(result.entry.answer, '废弃都市')

    def test_punctuation_whitespace_and_wrapping(self):
        for text in ('以下哪个地方\n不是射手村的地图?',
                     '  以下 哪个 地方不是射手村的地图 ？  ',
                     '以下哪个地方不是射手村的地图？\n1. 废弃都市\n2. 射手村公园'):
            with self.subTest(text=text):
                self.assertEqual(match_question(text, entries=self.entries).status, 'matched')

    def test_normalization_preserves_semantic_characters(self):
        self.assertEqual(normalize(' １２３，射 手\n村？'), normalize('123射手村'))
        self.assertNotEqual(normalize('是射手村'), normalize('不是射手村'))
        self.assertNotEqual(normalize('需要30个'), normalize('需要50个'))

    def test_single_nonsemantic_ocr_typo(self):
        result = match_question('以下哪个地万不是射手村的地图？', entries=self.entries)
        self.assertEqual(result.status, 'matched')
        self.assertEqual(result.entry.id, self.entry.id)

    def test_missing_negative_is_not_same_question(self):
        self.assert_rejected('以下哪个地方是射手村的地图？')

    def test_changed_number_is_not_same_question(self):
        entry = QuizEntry('fixture-number', '进行任务时需要收集30个树枝的是哪位村民？', '玛亚')
        result = match_question('进行任务时需要收集50个树枝的是哪位村民？', entries=(entry,))
        self.assertNotEqual(result.status, 'matched')

    def test_partial_unrelated_and_blank_are_rejected(self):
        for text in ('', '射手村', '以下哪个', '今天天气不错，继续升级吧', '？？？'):
            with self.subTest(text=text):
                self.assert_rejected(text)

    def test_low_or_invalid_confidence_is_rejected(self):
        for confidence in (0.0, 0.3, -1.0, math.nan, math.inf):
            with self.subTest(confidence=confidence):
                self.assert_rejected(self.entry.question, confidence=confidence)

    def test_duplicate_question_conflicting_answers_is_ambiguous(self):
        other = QuizEntry('fixture-conflict', self.entry.question, '魔法密林')
        result = match_question(self.entry.question, entries=(self.entry, other))
        self.assertEqual(result.status, 'uncertain')

    def test_sensitive_answer_requires_visible_option(self):
        entry = QuizEntry('fixture-sensitive', '转职考验中的秘密道具叫什么名字？', '黑暗水晶',
                          requires_answer_visible=True)
        self.assertNotEqual(match_question(entry.question, entries=(entry,)).status, 'matched')
        result = match_question(entry.question + '\n1. 黑暗水晶\n2. 魔法石', entries=(entry,))
        self.assertEqual(result.status, 'matched')

    def test_explicit_empty_bank_does_not_fall_back_to_default(self):
        self.assertNotEqual(match_question(self.entry.question, entries=()).status, 'matched')

    def test_reworded_stem_returns_reference_answer(self):
        result = match_question('请问有哪些地方并不属于射手村的地图范围呢？', entries=self.entries)
        self.assertEqual(result.entry, self.entry)
        self.assertEqual(result.status, 'uncertain')

    def test_low_confidence_retains_reference_with_warning(self):
        result = match_question(self.entry.question, confidence=.55, entries=self.entries)
        self.assertEqual(result.entry, self.entry)
        self.assertEqual(result.status, 'uncertain')
        self.assertIn('识别文字', result.reason)

    def test_conflicting_answers_are_both_shown(self):
        other = QuizEntry('conflict', self.entry.question, '魔法密林')
        result = match_question(self.entry.question, entries=(self.entry, other))
        self.assertEqual(result.status, 'uncertain')
        self.assertEqual({result.entry.answer, *[row.answer for row in result.alternatives]},
                         {'废弃都市', '魔法密林'})

    def test_missing_sensitive_option_still_gives_reference(self):
        entry = QuizEntry('sensitive', self.entry.question, '废弃都市', requires_answer_visible=True)
        result = match_question(entry.question, entries=(entry,))
        self.assertEqual(result.entry, entry)
        self.assertEqual(result.status, 'uncertain')

    def test_unrelated_text_has_no_suggestion(self):
        result = match_question('今天天气不错，继续升级吧', entries=self.entries)
        self.assertIsNone(result.entry)

    def test_only_numbered_options_do_not_create_suggestion(self):
        result = match_question('1. 废弃都市\n2. 射手村公园\n3. 魔法密林', entries=self.entries)
        self.assertIsNone(result.entry)


class BundledBankTests(unittest.TestCase):
    def test_live_alpha_squad_wording_returns_answer(self):
        result = match_question('第一个问题。下面中不是阿尔法部队的队员是谁？\n'
                                '1. 比特中士\n2. 查理中士\n3. 培斯上尉\n4. 伊吉上等兵\n5. 巴伯下士')
        self.assertIsNotNone(result.entry)
        self.assertEqual(result.entry.answer, '比特中士')

    def test_reworded_bundled_questions_return_expected_reference(self):
        for text, answer in (
                ('下列哪一位NPC在魔法密林看不到？', '露尔'),
                ('以下哪种怪物的等级最高？', '斧木妖'),
                ('哪个任务能够重复执行？', '艾温的玻璃鞋'),
                ('魔法密林汉斯手里拿的球是什么颜色？', '蓝色'),
                ('以下哪个不是阿尔法部队成员？', '比特中士')):
            with self.subTest(text=text):
                result = match_question(text)
                self.assertIsNotNone(result.entry, result)
                self.assertEqual(result.entry.answer, answer)

    def test_bank_has_unique_ids_and_attributable_answers(self):
        entries = load_bank()
        self.assertGreaterEqual(len(entries), 30)
        self.assertEqual(len({entry.id for entry in entries}), len(entries))
        for entry in entries:
            with self.subTest(id=entry.id):
                self.assertTrue(entry.question.strip())
                self.assertTrue(entry.answer.strip())
                self.assertTrue(entry.source.startswith('https://'))

    def test_all_bundled_questions_round_trip_with_visible_answer(self):
        entries = load_bank()
        for entry in entries:
            with self.subTest(id=entry.id):
                result = match_question(entry.question + '\n1. ' + entry.answer, entries=entries)
                self.assertEqual(result.status, 'matched', result)
                self.assertEqual(result.entry.answer, entry.answer)

    def test_bundled_negation_loss_never_confirms_original_answer(self):
        entries = load_bank()
        for entry in entries:
            for negative in ('不', '没', '无'):
                if negative not in entry.question:
                    continue
                with self.subTest(id=entry.id, missing=negative):
                    changed = entry.question.replace(negative, '', 1)
                    result = match_question(changed + '\n1. ' + entry.answer, entries=entries)
                    self.assertFalse(result.status == 'matched' and result.entry.id == entry.id,
                                     (changed, result))


if __name__ == '__main__':
    unittest.main()
