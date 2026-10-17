"""Offline exact matching and ranked suggestions for the CN classic quiz.

No option numbers are inferred: the game may shuffle them. Community answers
are references, not a claim that every live client question has been verified.
"""
from dataclasses import dataclass
from collections import Counter
from difflib import SequenceMatcher
import json
import math
from pathlib import Path
import re
import unicodedata


@dataclass(frozen=True)
class QuizEntry:
    id: str
    question: str
    answer: str
    aliases: tuple[str, ...] = ()
    source: str = ''
    requires_answer_visible: bool = False


@dataclass(frozen=True)
class QuizMatch:
    status: str
    entry: QuizEntry | None = None
    score: float = 0.0
    reason: str = ''
    alternatives: tuple[QuizEntry, ...] = ()


def normalize(text):
    return re.sub(r'[^a-z0-9\u4e00-\u9fff]', '',
                  unicodedata.normalize('NFKC', text).lower())


def load_bank(path=None):
    path = Path(path) if path else Path(__file__).parent / 'assets/third-job-quiz-cn.json'
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    if data.get('schema_version') != 1 or not data.get('entries'):
        raise ValueError('题库格式无效')
    entries = tuple(QuizEntry(**dict(row, aliases=tuple(row.get('aliases', ()))))
                    for row in data['entries'])
    if (len({row.id for row in entries}) != len(entries)
            or any(not row.question.strip() or not row.answer.strip() for row in entries)):
        raise ValueError('题库存在空题目、空答案或重复编号')
    return entries


_CRITICAL = ('不', '没', '无', '最', '高', '低', '正确', '错误')


def _compatible(expected, observed):
    # A single lost negative or changed number is not a harmless OCR typo.
    return (all(expected.count(word) == observed.count(word) for word in _CRITICAL)
            and re.findall(r'\d+', expected) == re.findall(r'\d+', observed))


def _best_question_span(question, text):
    """Compare the entire stem with a similarly sized span, never keywords only."""
    if len(question) < 8 or len(text) < len(question) * .85:
        return 0.0
    best = 0.0
    # At most two edit operations. This deliberately rejects heavily rewritten
    # stems until a verified alias is added, instead of guessing an answer.
    for length in range(max(8, len(question)-2), len(question)+3):
        for start in range(max(0, len(text)-length+1)):
            span = text[start:start+length]
            if not _compatible(question, span):
                continue
            score = SequenceMatcher(None, question, span, autojunk=False).ratio()
            best = max(best, score)
    return best


def answer_visible(answer, text):
    # Source pairs sometimes use HP/MP while the live localized text uses
    # 血量/魔量. These are answer-only replacements, never stem normalization.
    def clean(value):
        value = normalize(value).replace('hp', '血量').replace('mp', '魔量')
        return value.replace('降低', '减少').replace('回复', '恢复').replace('飘飘猪', '漂漂猪')
    return clean(answer) in clean(text)


def _fuzzy_text(text):
    """Collapse common wording differences, without deleting polarity/numbers."""
    text = normalize(text)
    for old, new in (('阿尔法部队的队员', '阿尔法队员'),
                     ('阿尔法部队', '阿尔法'), ('不属于', '不是'),
                     ('不能看到', '看不到'), ('不能见到', '看不到'),
                     ('看不见', '看不到'), ('见不到', '看不到'),
                     ('没有出现', '看不到'), ('掉落', '爆出'),
                     ('战利品', '物品'), ('级别', '等级'), ('回复', '恢复')):
        text = text.replace(old, new)
    return re.sub(r'第[一二三四五六七八九十0-9]+[个道]?(?:问题|题)|'
                  r'下面中|下列|以下|下面|哪一个|哪个|什么|是谁|是多少|请问', '', text)


def _question_text(text):
    # Numbered options must not become question evidence. Keep wrapped stems.
    text = re.split(r'[?？]', text, maxsplit=1)[0]
    return '\n'.join(line for line in text.splitlines()
                     if not re.match(r'^\s*[1-9a-eA-E][.、．:：)）]\s*', line))


def _fuzzy_score(question, observed):
    if len(question) < 4 or len(observed) < 4:
        return 0.0
    matcher = SequenceMatcher(None, question, observed, autojunk=False)
    # Anchor windows on shared text; avoid a quadratic scan over a large crop.
    starts = {0}
    for block in matcher.get_matching_blocks():
        if block.size:
            starts.update((max(0, block.b-block.a), block.b))
    best = 0.0
    for start in starts:
        for length in {len(question), round(len(question)*.7), round(len(question)*1.3)}:
            span = observed[start:start+length]
            common = sum((Counter(question) & Counter(span)).values())
            if common < 4 or common / len(question) < .45:
                continue
            sequence = SequenceMatcher(None, question, span, autojunk=False).ratio()
            overlap = 2 * common / (len(question)+len(span))
            best = max(best, .75*sequence + .25*overlap)
    return best


def _suggest(text, confidence, entries):
    observed = _fuzzy_text(_question_text(text))
    candidates = []
    for entry in entries:
        score = max(_fuzzy_score(_fuzzy_text(stem), observed)
                    for stem in (entry.question, *entry.aliases))
        if score >= .52:
            # Options only break close ties; they cannot create a candidate.
            score = min(1.0, score + (.04 if answer_visible(entry.answer, text) else 0))
            candidates.append((score, entry))
    candidates.sort(key=lambda item: item[0], reverse=True)
    if not candidates:
        return QuizMatch('no_match', reason='未找到相近题目，请框入完整题干或手动查题')
    score, entry = candidates[0]
    alternatives = tuple(row for value, row in candidates[1:]
                         if score-value <= .10 and row.answer != entry.answer)[:2]
    reason = '近似匹配 · 请核对题干、正反问法、数字和选项'
    if alternatives:
        reason = '多条题目相近 · 已列出候选答案，请按题干核对'
    elif confidence < .80:
        reason = '识别文字有疑点 · 已给出最相近题目的参考答案'
    elif entry.requires_answer_visible and not answer_visible(entry.answer, text):
        reason = '相近题库参考 · 尚未在选项中识别到答案，请核对版本'
    return QuizMatch('uncertain', entry, score, reason, alternatives)


def match_question(text, confidence=1.0, entries=None):
    if not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or confidence <= 0:
        return QuizMatch('uncertain', reason='文字识别不清，请保持题目完整可见')
    if len(text) > 1800:
        return QuizMatch('uncertain', reason='识别区域过大，请只框选题目与选项')
    normalized = normalize(text)
    if len(normalized) < 8:
        return QuizMatch('no_match', reason='等待完整题目')
    entries = tuple(entries) if entries is not None else load_bank()
    if confidence < .80:
        return _suggest(text, confidence, entries)
    candidates = []
    for entry in entries:
        score = max(_best_question_span(normalize(stem), normalized)
                    for stem in (entry.question, *entry.aliases))
        candidates.append((score, entry))
    candidates.sort(key=lambda item: item[0], reverse=True)
    if not candidates or candidates[0][0] < .92:
        return _suggest(text, confidence, entries)
    score, entry = candidates[0]
    # Same-stem variants must never be broken by file order.
    different = [item for item in candidates[1:] if item[1].answer != entry.answer]
    if different and score - different[0][0] < .055:
        return _suggest(text, confidence, entries)
    if entry.requires_answer_visible and not answer_visible(entry.answer, text):
        return _suggest(text, confidence, entries)
    return QuizMatch('matched', entry, score, '题库参考，请按答案文字核对选项')
