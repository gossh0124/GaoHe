"""Deterministic, conservative same-topic comparison helpers.

Text is normalized (NFKC, casefold, a few Traditional Chinese variant forms)
before it is compared. Latin text is split into words as before; runs of CJK
characters are split into character bigrams, because Chinese prose has no
spaces and a whole run is almost never shared verbatim by two outlets.
Organisation and place names are anchored on institutional suffixes (院, 部,
市, 公司 ...), numbers are parsed with Chinese magnitudes (千, 萬, 億) and the
measure word that follows them (人, 輛, 元 ...).

Every heuristic errs toward *not* grouping and *not* flagging: a missed
candidate is a missed hint, a wrong candidate becomes a visible annotation.
Scans are linear in the text length and every pairwise comparison is capped,
so long articles stay cheap.
"""

from array import array
from bisect import bisect_left
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction
from functools import cached_property, lru_cache
import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit

from .domain import MAX_QUERY_CHARS, ArticleRevision, FindingCandidate, TopicGroup
from .safety import is_credential_free_http_url


TOPIC_WINDOW_HOURS = 72
MAX_SUMMARY_CHARS = 500
MAX_LABEL_CHARS = 120

# Share of the smaller article's event vocabulary that two articles must have in
# common. Short texts are governed by the anchor counts; this keeps long Chinese
# articles, which share many incidental bigrams, from grouping by chance.
_HIGH_OVERLAP = Fraction(1, 5)
_POSSIBLE_OVERLAP = Fraction(1, 10)
# Bounds that keep comparisons linear on long articles.
_MAX_CONTEXT_CHARS = 400
_CONTEXT_RADIUS = 200
_MAX_NUMERIC_CLAIMS = 64
_MAX_OPPOSITE_HITS = 16
_MAX_CONTEXT_CHECKS = 2_048
_MAX_ENTITY_PREFIX = 5
# Only this much of a name's prefix masks bigram chains, so a verb the name swallowed still counts.
_CORE_PREFIX = 3
_DOC_CACHE_SIZE = 64

_CJK = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
_CJK_RUN = re.compile(f"[{_CJK}]+")
_WORD = re.compile(r"[a-z][a-z0-9'-]{2,}|[0-9][0-9,]*")
_CAPITALIZED = re.compile(r"\b[A-Z][a-z]{2,}\b")
_SENTENCE_MARK = re.compile(r"[。!?\n\r]|(?<![0-9])\.|\.(?![0-9])")

# Orthographic variants Taiwanese, Hong Kong and older texts use interchangeably,
# plus invisible characters that would otherwise split a CJK run.
_VARIANTS = str.maketrans(
    {
        "臺": "台", "爲": "為", "裏": "裡", "衞": "衛", "綫": "線", "麪": "麵", "啓": "啟",
        "着": "著", "峯": "峰", "羣": "群", "眞": "真", "敎": "教", "衆": "眾", "册": "冊",
        "\u00ad": None, "\u200b": None, "\u200c": None, "\u200d": None, "\u2060": None, "\ufeff": None,
        **{chr(code): None for code in range(0xFE00, 0xFE10)},
    }
)
# NFKC would turn the fullwidth comma into ",", which is a thousands separator in
# numbers; in Chinese prose it only ever separates clauses.
_KEEP_AS_IS = frozenset({"\uff0c"})

_STOPWORDS = {"a", "an", "and", "after", "at", "by", "for", "from", "in", "of", "on", "or", "the", "that", "this", "to", "with", "reported", "reports", "said", "says"}
# High-frequency news bigrams that say nothing about which event is reported.
_STOP_BIGRAMS = frozenset(
    """
    今天 昨天 明天 今日 昨日 明日 今年 去年 明年 今晚 昨晚 日前 近日 近期 目前 現在 當時 當天 當日 稍早 隨後
    之後 之前 以來 以後 以前 未來 過去 上午 下午 中午 晚間 凌晨 早上 晚上 同時 最後 首先 其次 接著 然後
    記者 報導 報道 新聞 消息 本報 外電 綜合 表示 指出 強調 認為 提到 透露 坦言 直言 說明 表明 回應 宣稱 聲稱
    據悉 了解 知道 覺得 希望 我們 你們 他們 她們 大家 自己 對方 雙方 以及 並且 而且 或者 還是 但是 不過 然而
    雖然 因為 所以 因此 如果 若是 即使 儘管 由於 對於 關於 至於 此外 另外 其中 其他 其實 包括 例如 甚至 並未
    尚未 已經 曾經 正在 將會 可能 應該 可以 能夠 需要 必須 是否 沒有 不是 就是 也是 都是 只是 還有 只有 一個
    一些 一起 一直 一定 一樣 這個 那個 這些 那些 這樣 那樣 如何 為何 什麼 怎麼 相關 有關 進行 相當 非常 很多
    許多 更多 所有 各種 部分 方面 情況 問題 時間 時候 方式 工作 重要 主要 一般 持續 繼續 開始 包含 成為
    作為 之間 之中 之一 上述 如此 這次 此次 本次
    """.split()
)
# Grammatical particles and pronouns: a bigram containing one is dropped.
_PARTICLES = frozenset("的了著是在之與及並其這那也都就而但或又很已於為將從該嗎呢吧啊呀們他她我你它一有")
# Characters an entity name never extends leftward across.
_ENTITY_BREAKERS = (_PARTICLES - {"一"}) | frozenset("和被對向到給讓跟")

_ENTITY_SUFFIXES = (
    "股份有限公司", "有限公司", "委員會", "基金會", "研究院", "研究所", "事務所", "辦公室",
    "公司", "大學", "銀行", "醫院", "法院", "中心", "協會", "公會", "工會", "集團", "組織", "聯盟",
    "部", "局", "會", "院", "署", "府", "黨", "處", "所", "廳", "市", "縣", "國", "省",
)
_SUFFIXES_BY_LAST: dict[str, tuple[str, ...]] = {}
for _suffix in sorted(_ENTITY_SUFFIXES, key=len, reverse=True):
    _SUFFIXES_BY_LAST[_suffix[-1]] = _SUFFIXES_BY_LAST.get(_suffix[-1], ()) + (_suffix,)
# A suffix character that starts the next word is not a suffix (國家, 所以, 處理 ...).
_SUFFIX_BLOCKING_NEXT = {
    "國": "家際內防營慶旗土產立會",
    "所": "以有謂在得屬需致為",
    "處": "理於分置罰境",
    "部": "分",
    "局": "面勢部",
    "市": "場值況",
    "省": "錢電時力油水",
}
# Common words that merely end in a suffix character.
_NON_ENTITIES = frozenset(
    """
    社會 機會 不會 開會 約會 誤會 體會 理會 學會 都會 將會 也會 還會 就會 才會 只會 仍會 或會 必會 恐會 大會 晚會
    聚會 集會 宴會 酒會 茶會 舞會 盛會 年會 全部 內部 外部 局部 細部 頭部 胸部 腹部 腰部 背部 腿部 臉部 面部 北部
    南部 東部 西部 中部 幹部 總部 本部 分部 結局 格局 僵局 大局 開局 出局 布局 騙局 賭局 飯局 時局 政局 當局 住院
    出院 入院 庭院 寺院 戲院 全國 各國 我國 本國 外國 出國 回國 返國 該國 兩國 他國 大國 強國 小國 鄰國 友國 敵國
    異國 建國 愛國 治國 城市 都市 股市 上市 下市 夜市 超市 門市 樓市 車市 房市 全市 本市 部署 簽署 連署 到處 好處
    壞處 相處 此處 各處 何處 一處 共處 益處 難處 用處 深處 場所 住所 廁所 處所 餐廳 客廳 大廳 政黨 全黨 本黨 本公司
    子公司 母公司 分公司 總公司 新公司 大公司 全省 本省 外省 各省 節省 反省 自省 全縣 本縣 本院 本署 本局 本會
    """.split()
)

# Numbers: Arabic digits, optional Chinese magnitudes or an English scale word,
# then a measure word. Only measure words listed here form a comparable claim.
_CJK_MAGNITUDES = {"百": 100, "千": 1_000, "萬": 10**4, "億": 10**8, "兆": 10**12}
_SCALES = {"thousand": 10**3, "million": 10**6, "billion": 10**9, "trillion": 10**12}
_CJK_UNITS = (
    "平方公里", "人次", "公里", "公尺", "公分", "公斤", "公噸", "公頃", "毫米", "英里", "海里",
    "美元", "日圓", "日元", "歐元", "港元", "港幣", "人民幣", "新台幣", "台幣",
    "人", "名", "位", "元", "件", "例", "起", "座", "戶", "輛", "架", "艘", "次", "家", "所", "間", "棟",
    "隻", "頭", "條", "項", "份", "張", "台", "部", "枚", "發", "顆", "票", "席", "倍", "成", "歲", "噸",
    "坪", "度", "級", "個",
)
_CJK_DATE_UNITS = (
    "個月", "個小時", "個星期", "個禮拜", "小時", "分鐘", "星期", "禮拜", "週年", "年度", "世紀",
    "年", "月", "日", "時", "點", "分", "秒", "週", "周", "天", "號", "季", "晚",
)
_LATIN_DATE_UNITS = {
    "am", "pm", "day", "days", "hour", "hours", "minute", "minutes", "second", "seconds", "week", "weeks", "month", "months",
    "year", "years", "decade", "decades", "century", "centuries", "st", "nd", "rd", "th",
}
_DATE_UNITS = frozenset(_CJK_DATE_UNITS) | _LATIN_DATE_UNITS
_NON_UNITS = frozenset(_STOPWORDS | {"is", "are", "was", "were", "be", "as", "per", "than"})
# Classifiers that all count people; outlets use them interchangeably.
_PERSON_UNITS = frozenset({"人", "名", "位"})
_UNIT_ALTERNATION = "|".join(re.escape(unit) for unit in sorted(_CJK_UNITS + _CJK_DATE_UNITS, key=len, reverse=True))
_VALUE = r"[0-9](?:[0-9]|,(?=[0-9]))*(?:\.[0-9]+)?"
_NUMBER = re.compile(
    rf"(?<![0-9a-z_.,百千萬億兆])(?P<value>{_VALUE}(?:[百千萬億兆]+(?:{_VALUE}[百千萬億兆]+)*)?)"
    r"(?:[ \t]*(?P<scale>thousand|million|billion|trillion)\b)?"
    r"(?P<infix>[多餘許來])?"
    rf"[ \t]*(?P<unit>%|{_UNIT_ALTERNATION}|[a-z]+)?"
)
_CHUNK = re.compile(rf"({_VALUE})([百千萬億兆]*)")
_APPROX_BEFORE = re.compile(
    r"(?:\b(?:about|around|approximately|roughly|nearly|almost|over|under|some|estimated"
    r"|more than|less than|fewer than|at least|at most|up to)"
    r"|約莫|大約|約略|將近|接近|近乎|大概|超過|至少|不到|不足|未滿|上看|約|近|逾)(?:有|達|為)?[ \t]*$"
)
_APPROX_AFTER = re.compile(r"[ \t]*(?:左右|上下|以上|以下|之譜|出頭|開外|不等|or so\b|or more\b|or fewer\b|or less\b|\+)")
# Both ends of a range or list are approximate: 500至600人, 500人至600人, 5、6人, between 500 and 600.
_RANGE_BEFORE = re.compile(
    rf"[0-9][百千萬億兆多餘]*[ \t]*(?:{_UNIT_ALTERNATION}|%)?[ \t]*(?:至|到|~|〜|-|–|—|、|或|\bto|\bor|\band)[ \t]*$"
)
_RANGE_AFTER_VALUE = re.compile(r"[ \t]*(?:至|到|~|〜|-|–|—|、|或|to\b|or\b|and\b)[ \t]*[0-9]")
_RANGE_AFTER_UNIT = re.compile(r"[ \t]*(?:至|到|~|〜|-|–|—|、|或|to\b|or\b)[ \t]*[0-9]")

_OPPOSITES = (
    ("approved", "rejected"), ("opened", "closed"), ("confirmed", "denied"), ("arrested", "released"),
    ("通過", "否決"), ("批准", "否決"), ("同意", "反對"), ("承認", "否認"), ("確認", "否認"),
    ("上漲", "下跌"), ("增加", "減少"), ("開放", "關閉"), ("逮捕", "釋放"), ("有罪", "無罪"),
)
_OPPOSITE_WORDS = frozenset(word for pair in _OPPOSITES for word in pair)
_LATIN_NEGATION = re.compile(r"(?:\bnot|\bnever|\bno|n't)(?:\s+[a-z]+){0,2}\s+$")
# Negation, modality or repetition right before a Chinese verb changes what it asserts.
_CJK_NEGATION = re.compile(
    r"(?:不|未|沒|無|非|否|別|勿|莫|沒有|未能|無法|不再|不予|不會|不能|不得|不宜|難以|尚未|並未|拒絕|是否|能否|會否"
    r"|考慮|研議|研擬|研究|評估|規劃|討論|爭取|推動|可能|要求|呼籲|反對|重新|再度|暫緩|延後|延緩|停止|取消"
    r"|計畫|計劃|預計|預定|打算|希望|準備|有望|即將|擬|將|恐|盼|若|應)\s*$"
)
# A following character that turns the verb into a noun or adjective (開放式, 同意書 ...).
_CJK_OPPOSITE_BLOCKING_NEXT = frozenset("率性式者黨書權派票案")


@lru_cache(maxsize=8192)
def _fold(cluster: str) -> tuple[str, str]:
    """Return the cased and folded normal forms of one grapheme cluster, equal in length."""
    if cluster in _KEEP_AS_IS:
        return cluster, cluster
    cased = unicodedata.normalize("NFKC", cluster).translate(_VARIANTS)
    folded = cased.casefold()
    return (cased if len(cased) == len(folded) else folded), folded


def _normalize(text: str) -> tuple[str, str, list[int] | None, list[int] | None]:
    """Return folded text, cased text and, when lengths changed, maps back to the original."""
    if text.isascii():
        return text.lower(), text, None, None
    cased_parts: list[str] = []
    folded_parts: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    identity = True
    index, length = 0, len(text)
    while index < length:
        end = index + 1
        while end < length and unicodedata.category(text[end]).startswith("M"):
            end += 1
        cased, folded = _fold(text[index:end])
        identity = identity and len(folded) == end - index
        cased_parts.append(cased)
        folded_parts.append(folded)
        starts.extend([index] * len(folded))
        ends.extend([end] * len(folded))
        index = end
    if identity:
        return "".join(folded_parts), "".join(cased_parts), None, None
    return "".join(folded_parts), "".join(cased_parts), starts, ends


def _kept_bigram(bigram: str) -> bool:
    return bigram not in _STOP_BIGRAMS and bigram[0] not in _PARTICLES and bigram[1] not in _PARTICLES


def _breaks_entity(norm: str, position: int, run_start: int) -> bool:
    if norm[position] in _ENTITY_BREAKERS:
        return True
    return position > run_start and norm[position - 1:position + 1] in _STOP_BIGRAMS


def _cjk_entities(norm: str, run_start: int, run_end: int, core: bytearray, found: list[tuple[int, int, str]]) -> None:
    """Collect suffix-anchored names in one CJK run, e.g. 政院 and 行政院 for 今天行政院."""
    for last in range(run_start + 1, run_end):
        candidates = _SUFFIXES_BY_LAST.get(norm[last])
        if not candidates:
            continue
        stop = last + 1
        suffix = next((item for item in candidates if stop - len(item) >= run_start and norm.startswith(item, stop - len(item))), None)
        if suffix is None or stop - len(suffix) == run_start:
            continue
        if stop < run_end and norm[stop] in _SUFFIX_BLOCKING_NEXT.get(suffix, ""):
            continue
        first = stop - len(suffix) - 1
        if norm[first:stop] in _NON_ENTITIES:
            continue
        begin = None
        for position in range(first, max(run_start, first - _MAX_ENTITY_PREFIX + 1) - 1, -1):
            if _breaks_entity(norm, position, run_start):
                break
            found.append((position, stop, norm[position:stop]))
            begin = position
        if begin is not None:
            begin = max(begin, first - _CORE_PREFIX + 1)
            core[begin:stop] = b"\x01" * (stop - begin)


def _select(items: Sequence[tuple], starts: Sequence[int], begin: int, end: int) -> list[tuple]:
    """Return the (start, end, ...) items, sorted by start, that lie inside [begin, end)."""
    selected = []
    index = bisect_left(starts, begin)
    while index < len(items) and items[index][0] < end:
        if items[index][1] <= end:
            selected.append(items[index])
        index += 1
    return selected


class _Region:
    """Tokens of one span of a normalized document; bigrams are produced on demand."""

    def __init__(self, doc: "_Doc", start: int, end: int) -> None:
        self.start, self.end = start, end
        self.core = doc.core
        self._norm = doc.norm
        self._positions = doc.bigram_positions
        # A bigram at position p fits the span when p + 2 <= end.
        self._low, self._high = bisect_left(self._positions, start), bisect_left(self._positions, end - 1)
        self.words = tuple(_select(doc.words, doc.word_starts, start, end))
        self.numbers = frozenset(item[2] for item in _select(doc.numbers, doc.number_starts, start, end))
        self.english_entities = frozenset(item[2] for item in _select(doc.english, doc.english_starts, start, end))
        self.cjk_entities = frozenset(item[2] for item in _select(doc.cjk, doc.cjk_starts, start, end))
        self.entities = (self.english_entities - _STOPWORDS) | self.cjk_entities

    @property
    def bigrams(self) -> Iterator[tuple[int, str]]:
        norm, positions = self._norm, self._positions
        for index in range(self._low, self._high):
            position = positions[index]
            yield position, norm[position:position + 2]

    @cached_property
    def bigram_set(self) -> frozenset[str]:
        return frozenset(value for _, value in self.bigrams)


@dataclass(frozen=True)
class _NumericClaim:
    start: int
    end: int
    value: Decimal
    unit: str
    latin_unit: bool


class _Doc:
    """A normalized text with lazily computed, cached comparison features."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.norm, cased, self._starts, self._ends = _normalize(text)
        norm = self.norm
        self.words: list[tuple[int, int, str]] = []
        self.numbers: list[tuple[int, int, str]] = []
        for match in _WORD.finditer(norm):
            target = self.numbers if match.group(0)[0].isdigit() else self.words
            target.append((match.start(), match.end(), match.group(0)))
        self.english = [(match.start(), match.end(), match.group(0).casefold()) for match in _CAPITALIZED.finditer(cased)]
        self.core = bytearray(len(norm) + 1)
        self.bigram_positions = array("i")
        cjk: list[tuple[int, int, str]] = []
        for run in _CJK_RUN.finditer(norm):
            self.bigram_positions.extend(
                position for position in range(run.start(), run.end() - 1) if _kept_bigram(norm[position:position + 2])
            )
            _cjk_entities(norm, run.start(), run.end(), self.core, cjk)
        self.cjk = sorted(cjk)
        self.word_starts = [item[0] for item in self.words]
        self.number_starts = [item[0] for item in self.numbers]
        self.english_starts = [item[0] for item in self.english]
        self.cjk_starts = [item[0] for item in self.cjk]
        self.marks = [match.start() for match in _SENTENCE_MARK.finditer(norm)]
        self._regions: dict[tuple[int, int], _Region] = {}
        self._claims: list[_NumericClaim] | None = None
        self._hits: dict[str, list[tuple[int, int]]] = {}

    @property
    def whole(self) -> _Region:
        return self.region(0, len(self.norm))

    def region(self, start: int, end: int) -> _Region:
        region = self._regions.get((start, end))
        if region is None:
            region = self._regions[(start, end)] = _Region(self, start, end)
        return region

    def original_span(self, start: int, end: int) -> tuple[int, int]:
        if self._starts is None or self._ends is None:
            return start, end
        return self._starts[start], self._ends[end - 1]

    def context(self, start: int, end: int) -> _Region:
        """Return the sentence around a span, clipped to a window for run-on text."""
        index = bisect_left(self.marks, start)
        left = self.marks[index - 1] + 1 if index else 0
        right_index = bisect_left(self.marks, end)
        right = self.marks[right_index] if right_index < len(self.marks) else len(self.norm)
        if right - left > _MAX_CONTEXT_CHARS:
            left, right = max(left, start - _CONTEXT_RADIUS), min(right, end + _CONTEXT_RADIUS)
        return self.region(left, right)

    def numeric_claims(self) -> list[_NumericClaim]:
        if self._claims is None:
            self._claims = _numeric_claims(self.norm)
        return self._claims

    def opposite_hits(self, word: str) -> list[tuple[int, int]]:
        if word not in self._hits:
            self._hits[word] = _opposite_hits(self.norm, word)
        return self._hits[word]


@lru_cache(maxsize=_DOC_CACHE_SIZE)
def _doc(text: str) -> _Doc:
    return _Doc(text)


def _overlaps(start: int, end: int, spans: Iterable[tuple[int, int]]) -> bool:
    return any(start < span_end and span_start < end for span_start, span_end in spans)


def _latin_events(
    regions: Sequence[_Region], skip_values: frozenset[str] = frozenset(), skip_spans: Sequence[tuple[int, int]] = ()
) -> set[str]:
    entities = frozenset().union(*(region.english_entities for region in regions))
    values = {value for region in regions for start, end, value in region.words if not _overlaps(start, end, skip_spans)}
    return values - entities - _STOPWORDS - skip_values


def _kept_bigrams(
    region: _Region, skip_values: frozenset[str], skip_spans: Sequence[tuple[int, int]]
) -> list[tuple[int, str]]:
    if not skip_values and not skip_spans:
        return list(region.bigrams)
    return [
        (position, value) for position, value in region.bigrams
        if value not in skip_values and not _overlaps(position, position + 2, skip_spans)
    ]


def _segments(bigrams: Iterable[tuple[int, str]], shared: set[str], core: bytearray) -> int:
    """Count maximal chains of shared bigrams that reach outside an entity name.

    A copied phrase such as a name and title yields many bigrams but is one
    anchor, and a chain made only of an entity is already counted as an entity.
    """
    count = 0
    previous = -2
    anchored = False
    for position, value in bigrams:
        if value not in shared:
            continue
        if position != previous + 1:
            count += anchored
            anchored = False
        anchored = anchored or not (core[position] or core[position + 1])
        previous = position
    return count + anchored


def _anchor_count(
    left: Sequence[_Region],
    right: Sequence[_Region],
    *,
    skip_values: frozenset[str] = frozenset(),
    left_skip: Sequence[tuple[int, int]] = (),
    right_skip: Sequence[tuple[int, int]] = (),
) -> int:
    """Count independent shared event anchors: Latin words plus CJK bigram chains."""
    words = _latin_events(left, skip_values, left_skip) & _latin_events(right, skip_values, right_skip)
    left_runs = [(region, _kept_bigrams(region, skip_values, left_skip)) for region in left]
    right_runs = [(region, _kept_bigrams(region, skip_values, right_skip)) for region in right]
    left_values = {value for _, bigrams in left_runs for _, value in bigrams}
    shared = {value for _, bigrams in right_runs for _, value in bigrams if value in left_values}
    if not shared:
        return len(words)
    left_chains = sum(_segments(bigrams, shared, region.core) for region, bigrams in left_runs)
    right_chains = sum(_segments(bigrams, shared, region.core) for region, bigrams in right_runs)
    return len(words) + min(left_chains, right_chains)


def _vocabulary(regions: Sequence[_Region]) -> set[str]:
    return _latin_events(regions) | {value for region in regions for value in region.bigram_set}


def _overlap(left: Sequence[_Region], right: Sequence[_Region]) -> Fraction:
    """Overlap coefficient of the two event vocabularies."""
    left_vocabulary, right_vocabulary = _vocabulary(left), _vocabulary(right)
    smaller = min(len(left_vocabulary), len(right_vocabulary))
    return Fraction(len(left_vocabulary & right_vocabulary), smaller) if smaller else Fraction(0)


def _entities(regions: Sequence[_Region]) -> frozenset[str]:
    return frozenset().union(*(region.entities for region in regions))


def _views(revision: ArticleRevision) -> tuple[tuple[_Region, _Region], tuple[_Region]]:
    title, body = _doc(revision.title).whole, _doc(revision.text).whole
    return (title, body), (body,)


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").casefold()
    except ValueError:
        return ""


def _safe_url(url: str) -> str:
    if not is_credential_free_http_url(url):
        return ""
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(), parsed.path, "", ""))[:200]


def _when(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _within_window(left: ArticleRevision, right: ArticleRevision) -> bool:
    left_when, right_when = _when(left.fetched_at), _when(right.fetched_at)
    return left_when is not None and right_when is not None and abs((left_when - right_when).total_seconds()) <= TOPIC_WINDOW_HOURS * 3600


def _pair_confidence(left: ArticleRevision, right: ArticleRevision) -> str | None:
    left_url, right_url = _safe_url(left.url), _safe_url(right.url)
    if not left_url or not right_url or left_url == right_url or not _within_window(left, right):
        return None
    left_all, left_body = _views(left)
    right_all, right_body = _views(right)
    shared_events = _anchor_count(left_all, right_all)
    if not shared_events:
        return None
    if (
        _host(left_url) != _host(right_url)
        and _entities(left_all) & _entities(right_all)
        and shared_events >= 2
        and _anchor_count(left_body, right_body) >= 2
        and _overlap(left_body, right_body) >= _HIGH_OVERLAP
    ):
        return "high"
    if _overlap(left_all, right_all) >= _POSSIBLE_OVERLAP:
        return "possible"
    return None


def _chain_texts(region: _Region, shared: frozenset[str], norm: str) -> set[str]:
    """Return the text of each maximal chain of shared bigrams, e.g. 開放外籍旅客."""
    texts: set[str] = set()
    begin = previous = None
    for position, value in region.bigrams:
        if value not in shared:
            continue
        if previous is not None and position == previous + 1:
            previous = position
            continue
        if begin is not None and previous is not None:
            texts.add(norm[begin:previous + 2])
        begin = previous = position
    if begin is not None and previous is not None:
        texts.add(norm[begin:previous + 2])
    return texts


def _label(left: ArticleRevision, right: ArticleRevision) -> str:
    left_doc, right_doc = _doc(left.title), _doc(right.title)
    left_title, right_title = left_doc.whole, right_doc.whole
    items = ({value for *_, value in left_title.words} | left_title.numbers) & (
        {value for *_, value in right_title.words} | right_title.numbers
    )
    items |= _chain_texts(left_title, left_title.bigram_set & right_title.bigram_set, left_doc.norm)
    left_all, _ = _views(left)
    right_all, _ = _views(right)
    items |= frozenset().union(*(region.english_entities | region.cjk_entities for region in left_all)) & frozenset().union(
        *(region.english_entities | region.cjk_entities for region in right_all)
    )
    # Prefer 行政院 over its fragment 政院, and a whole shared phrase over its pieces.
    cjk_items = {item for item in items if _CJK_RUN.search(item)}
    chosen: list[str] = []
    for item in sorted(items):
        if item in cjk_items and any(item != other and item in other for other in cjk_items):
            continue
        chosen.append(item)
        if len(chosen) == 5:
            break
    return " ".join(chosen)[:MAX_LABEL_CHARS] or "possible topic"


def group_revision(revision: ArticleRevision, existing: Sequence[ArticleRevision]) -> TopicGroup | None:
    """Return the best deterministic grouping signal without forcing uncertain peers."""
    possible: ArticleRevision | None = None
    for item in existing:
        confidence = _pair_confidence(revision, item)
        if confidence == "high":
            return TopicGroup(None, _label(revision, item), "high", "active")
        if confidence == "possible" and possible is None:
            possible = item
    return TopicGroup(None, _label(revision, possible), "possible", "possible") if possible else None


def _parse_value(raw: str, scale: str | None) -> Decimal:
    """Parse 5,000 / 1.5萬 / 2千5百萬 / 1億2千萬 into an exact value."""
    total = Decimal(0)
    section = Decimal(0)
    for digits, magnitudes in _CHUNK.findall(raw):
        small = large = 1
        for character in magnitudes:
            if character in "百千":
                small *= _CJK_MAGNITUDES[character]
            else:
                large *= _CJK_MAGNITUDES[character]
        section += Decimal(digits.replace(",", "")) * small
        if large > 1:
            total += section * large
            section = Decimal(0)
    total += section
    return total * _SCALES[scale] if scale else total


def _numeric_claims(norm: str) -> list[_NumericClaim]:
    """Exact, unit-bearing numbers; approximate, ranged, ordinal and date numbers are left out."""
    claims: list[_NumericClaim] = []
    for match in _NUMBER.finditer(norm):
        unit = match.group("unit")
        if not unit or unit in _DATE_UNITS or unit in _NON_UNITS or match.group("infix"):
            continue
        before = norm[max(0, match.start() - 16):match.start()]
        if before.endswith("第") or _APPROX_BEFORE.search(before) or _RANGE_BEFORE.search(before):
            continue
        value_end = match.end("scale") if match.group("scale") else match.end("value")
        if (
            _APPROX_AFTER.match(norm, match.end())
            or _RANGE_AFTER_VALUE.match(norm, value_end)
            or _RANGE_AFTER_UNIT.match(norm, match.end())
        ):
            continue
        value = _parse_value(match.group("value"), match.group("scale"))
        if not value:
            continue
        latin = unit.isascii() and unit != "%"
        claims.append(_NumericClaim(match.start(), match.end(), value, "人" if unit in _PERSON_UNITS else unit, latin))
        if len(claims) >= _MAX_NUMERIC_CLAIMS:
            break
    return claims


def _find_all(text: str, word: str) -> Iterable[int]:
    position = text.find(word)
    while position != -1:
        yield position
        position = text.find(word, position + len(word))


def _opposite_hits(norm: str, word: str) -> list[tuple[int, int]]:
    """Occurrences of an assertion word that are not negated, hedged or part of a larger word."""
    hits: list[tuple[int, int]] = []
    latin = word.isascii()
    starts = (match.start() for match in re.finditer(rf"\b{re.escape(word)}\b", norm)) if latin else _find_all(norm, word)
    for start in starts:
        end = start + len(word)
        before = norm[max(0, start - 40):start]
        if latin:
            negated = _LATIN_NEGATION.search(before) is not None
        else:
            negated = _CJK_NEGATION.search(before) is not None or norm[end:end + 1] in _CJK_OPPOSITE_BLOCKING_NEXT
        if negated:
            continue
        hits.append((start, end))
        if len(hits) >= _MAX_OPPOSITE_HITS:
            break
    return hits


def _mentions(doc: _Doc, region: _Region, word: str) -> bool:
    text = doc.norm[region.start:region.end]
    if word.isascii():
        return re.search(rf"\b{re.escape(word)}\b", text) is not None
    return word in text


class _Budget:
    """A fixed number of sentence-pair comparisons for one pair of articles."""

    def __init__(self, remaining: int) -> None:
        self.remaining = remaining

    def take(self) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


def _phrase(doc: _Doc, start: int, end: int) -> tuple[int, int, str]:
    original_start, original_end = doc.original_span(start, end)
    return original_start, original_end, " ".join(doc.text[original_start:original_end].split())


def _numeric_difference(left: _Doc, right: _Doc, budget: _Budget) -> tuple[int, int, str, str] | None:
    peers_by_unit: dict[str, list[_NumericClaim]] = {}
    for claim in right.numeric_claims():
        peers_by_unit.setdefault(claim.unit, []).append(claim)
    for claim in left.numeric_claims():
        peers = peers_by_unit.get(claim.unit)
        if not peers:
            continue
        left_context = left.context(claim.start, claim.end)
        skip_values = frozenset({claim.unit}) if claim.latin_unit else frozenset()
        for other in peers:
            if other.value == claim.value or other.value.adjusted() == claim.value.adjusted():
                continue
            if not budget.take():
                return None
            right_context = right.context(other.start, other.end)
            if not left_context.entities & right_context.entities:
                continue
            anchors = _anchor_count(
                (left_context,),
                (right_context,),
                skip_values=skip_values,
                left_skip=((claim.start, claim.end),),
                right_skip=((other.start, other.end),),
            )
            if anchors >= 2:
                start, end, phrase = _phrase(left, claim.start, claim.end)
                return start, end, phrase, _phrase(right, other.start, other.end)[2]
    return None


def _opposite_difference(left: _Doc, right: _Doc, budget: _Budget) -> tuple[int, int, str, str] | None:
    for first, second in _OPPOSITES:
        for left_word, right_word in ((first, second), (second, first)):
            left_hits, right_hits = left.opposite_hits(left_word), right.opposite_hits(right_word)
            if not left_hits or not right_hits:
                continue
            # Chinese bigrams are weaker anchors than words, so Chinese needs two.
            required = 1 if left_word.isascii() else 2
            for left_start, left_end in left_hits:
                left_context = left.context(left_start, left_end)
                if _mentions(left, left_context, right_word):
                    continue
                for right_start, right_end in right_hits:
                    if not budget.take():
                        return None
                    right_context = right.context(right_start, right_end)
                    if _mentions(right, right_context, left_word) or not left_context.entities & right_context.entities:
                        continue
                    anchors = _anchor_count(
                        (left_context,),
                        (right_context,),
                        skip_values=_OPPOSITE_WORDS,
                        left_skip=((left_start, left_end),),
                        right_skip=((right_start, right_end),),
                    )
                    if anchors >= required:
                        start, end, phrase = _phrase(left, left_start, left_end)
                        return start, end, phrase, _phrase(right, right_start, right_end)[2]
    return None


def _candidate(left: ArticleRevision, right: ArticleRevision, difference: tuple[int, int, str, str]) -> FindingCandidate:
    start, end, left_claim, right_claim = difference
    left_url, right_url = _safe_url(left.url), _safe_url(right.url)
    detail = f"{left_claim} versus {right_claim}"
    sources = f"{left_url} ({_host(left.url)}, {left.fetched_at[:32]}) vs {right_url} ({_host(right.url)}, {right.fetched_at[:32]})"
    summary = f"Material cross-media difference: {detail}; sources: {sources}"[:MAX_SUMMARY_CHARS]
    query = f"cross-media difference {detail}; sources: {sources}"[:MAX_QUERY_CHARS]
    return FindingCandidate(None, "material_cross_media_difference", summary, start, end, "material", query, left.id)


def compare_topic(revisions: Sequence[ArticleRevision]) -> list[FindingCandidate]:
    """Return only material, checkable candidates between high-confidence peers."""
    candidates: list[FindingCandidate] = []
    seen: set[tuple[int, int, int]] = set()
    for index, left in enumerate(revisions):
        for right in revisions[index + 1:]:
            if _pair_confidence(left, right) != "high":
                continue
            left_doc, right_doc = _doc(left.text), _doc(right.text)
            budget = _Budget(_MAX_CONTEXT_CHECKS)
            difference = _numeric_difference(left_doc, right_doc, budget) or _opposite_difference(left_doc, right_doc, budget)
            if difference is None:
                continue
            key = (left.id, difference[0], right.id)
            if key not in seen:
                seen.add(key)
                candidates.append(_candidate(left, right, difference))
    return candidates
