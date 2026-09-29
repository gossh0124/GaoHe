"""Round-2 topic heuristics: each test reproduces one reviewed false grouping or false difference."""

import pytest

from gaohe.domain import ArticleRevision, article_content_hash
from gaohe.topics import compare_topic, group_revision


def revision(revision_id, url, title, text, fetched_at="2026-09-20T12:00:00Z") -> ArticleRevision:
    return ArticleRevision(revision_id, revision_id, url, title, text, article_content_hash(title, text), fetched_at)


ARRIVALS = "行政院今天宣布開放外籍旅客入境觀光，首批旅行團共有{}抵達桃園機場。"
ARRIVALS_PEER = "行政院宣布開放外籍旅客入境觀光後，首批旅行團共{}抵達桃園機場。"


def arrivals(left_phrase, right_phrase, left_url="https://alpha.test/arrivals", right_url="https://bravo.test/arrivals"):
    left = revision(1, left_url, "政院開放外籍旅客入境", ARRIVALS.format(left_phrase))
    right = revision(2, right_url, "外籍旅客入境觀光首發團抵台", ARRIVALS_PEER.format(right_phrase))
    return left, right


def pair(left_title, left_text, right_title, right_text):
    return (
        revision(1, "https://alpha.test/a", left_title, left_text),
        revision(2, "https://bravo.test/b", right_title, right_text),
    )


# --- finding 13: materiality is a ratio, not the decimal exponent -----------------------------------------------------

@pytest.mark.parametrize(("left_phrase", "right_phrase"), [("9800人", "1萬人"), ("99人", "100人"), ("98人", "102人")])
def test_close_figures_across_a_power_of_ten_are_not_differences(left_phrase, right_phrase):
    assert compare_topic(arrivals(left_phrase, right_phrase)) == []


@pytest.mark.parametrize(("left_phrase", "right_phrase"), [("100人", "900人"), ("1000人", "9999人"), ("300人", "700人")])
def test_figures_far_apart_within_one_power_of_ten_are_differences(left_phrase, right_phrase):
    [candidate] = compare_topic(arrivals(left_phrase, right_phrase))
    assert f"{left_phrase} versus {right_phrase}" in candidate.summary


# --- finding 14: one outlet's subdomains are one medium ----------------------------------------------------------------

@pytest.mark.parametrize(
    ("left_url", "right_url"),
    [("https://news.ltn.com.tw/a", "https://ec.ltn.com.tw/b"), ("https://www.setn.com/a", "https://star.setn.com/b"),
     ("https://udn.com/news/a", "https://money.udn.com/b")],
)
def test_subdomains_of_one_outlet_are_never_cross_media(left_url, right_url):
    left, right = arrivals("300人", "3000人", left_url, right_url)

    group = group_revision(right, (left,))

    assert group is not None and group.confidence == "possible"
    assert compare_topic((left, right)) == []


def test_different_outlets_under_one_country_suffix_are_still_cross_media():
    left, right = arrivals("300人", "3000人", "https://news.ltn.com.tw/a", "https://udn.com/news/b")

    assert group_revision(right, (left,)).confidence == "high"
    assert len(compare_topic((left, right))) == 1


# --- finding 41: identical copies and differently counted things -------------------------------------------------------

FIRE_COPY = "台北市萬華區一處公寓今天凌晨發生火警，台北市消防局表示，火警共造成3人死亡、12人受傷。"


def test_identical_wire_copies_yield_no_difference():
    left = revision(1, "https://alpha.test/fire", "萬華公寓火警", FIRE_COPY)
    right = revision(2, "https://bravo.test/fire", "萬華公寓火警", FIRE_COPY)

    assert group_revision(right, (left,)).confidence == "high"
    assert compare_topic((left, right)) == []


def test_counts_of_different_things_are_not_compared():
    left, right = pair(
        "萬華公寓火警", "台北市消防局表示，萬華公寓火警派遣80人到場搶救，火勢已經撲滅。",
        "萬華公寓火警", "台北市消防局表示，萬華公寓火警造成3名住戶死亡，火勢已經撲滅。",
    )

    assert compare_topic((left, right)) == []


def test_an_opposite_the_peer_also_reports_is_no_difference():
    verdicts = "台北地方法院今天宣判被告甲有罪，全案可上訴。台北地方法院今天宣判被告乙無罪，全案可上訴。"
    left, right = pair("北院宣判", verdicts, "北院宣判", verdicts)

    assert compare_topic((left, right)) == []


# --- finding 42: different events sharing an institution and boilerplate ---------------------------------------------

WANHUA = (
    "台北市萬華區一處老舊公寓今天凌晨發生火警，台北市消防局獲報後派遣30車、80人到場搶救。"
    "消防局表示，火警共造成3人死亡，起火原因仍待火災調查科鑑定。"
)
NEIHU = (
    "台北市內湖區一處工廠今天凌晨發生火警，台北市消防局獲報後派遣25車、60人到場搶救。"
    "消防局表示，火警共造成1人受傷，起火原因仍待火災調查科鑑定。"
)
TYPHOON = "中央氣象署今天上午發布{}颱風海上警報，颱風中心目前位於鵝鑾鼻東南方海面，屏東縣政府已成立災害應變中心。"
CRASH = "國道{}號北上路段今天發生連環車禍，國道公路警察局表示，共有{}車追撞，所幸無人死亡。"


@pytest.mark.parametrize(
    ("left_title", "left_text", "right_title", "right_text"),
    [
        ("北市公寓火警 3死", WANHUA, "北市工廠火警 1傷", NEIHU),
        ("颱風海警發布", TYPHOON.format("山陀兒"), "颱風海警發布", TYPHOON.format("康芮")),
        ("國道連環車禍", CRASH.format(1, 5), "國道連環車禍", CRASH.format(3, 2)),
    ],
)
def test_events_with_different_places_typhoons_or_roads_are_not_high(left_title, left_text, right_title, right_text):
    left, right = pair(left_title, left_text, right_title, right_text)

    group = group_revision(right, (left,))

    assert group is None or group.confidence != "high"
    assert compare_topic((left, right)) == []


def test_the_same_named_event_still_groups_high():
    left, right = pair("颱風海警發布", TYPHOON.format("康芮"), "康芮颱風海警", "今天" + TYPHOON.format("康芮"))

    assert group_revision(right, (left,)).confidence == "high"


def test_figures_are_not_compared_when_titles_share_only_a_name():
    left = revision(1, "https://alpha.test/a", "行政院最新決定", ARRIVALS.format("500人"))
    right = revision(2, "https://bravo.test/b", "行政院今日記者會", ARRIVALS_PEER.format("5000人"))

    assert group_revision(right, (left,)).confidence == "high"
    assert compare_topic((left, right)) == []


# --- finding 44: negation a few characters before the verb, and 與否 ----------------------------------------------------

BUDGET = "立法院今天三讀{}國防特別預算條例，朝野立委在議場外表態。"


@pytest.mark.parametrize(
    ("left_verb", "right_verb"),
    [("未獲通過", "否決"), ("並未獲得通過", "否決"), ("沒有在本會期通過", "否決"), ("難獲通過", "否決"),
     ("恐難通過", "否決"), ("無望通過", "否決"), ("未獲同意", "反對"), ("通過與否仍待觀察的", "否決")],
)
def test_windowed_negation_and_open_outcomes_are_not_opposites(left_verb, right_verb):
    left = revision(1, "https://alpha.test/budget", "國防特別預算條例", BUDGET.format(left_verb))
    right = revision(2, "https://bravo.test/budget", "國防特別預算條例三讀", BUDGET.format(right_verb))

    assert compare_topic((left, right)) == []


# --- finding 45: the opposite verbs must share a subject or object ------------------------------------------------------

@pytest.mark.parametrize(
    ("left_text", "right_text"),
    [
        ("數位發展部今天宣布，政府開放資料平台今年新增資料集，民眾可免費下載使用。",
         "數位發展部今天宣布，舊版政府資料平台將於年底關閉，民眾可免費下載使用。"),
        ("衛福部統計，今年長照床位增加，偏鄉仍不足。", "衛福部統計，今年長照人力減少，偏鄉仍不足。"),
        ("國防部指出，解放軍逮捕兩名台籍人士，情勢緊張。", "國防部指出，解放軍近日釋放和緩訊號，情勢緊張。"),
    ],
)
def test_opposite_words_about_different_things_are_not_differences(left_text, right_text):
    left, right = pair("即時新聞", left_text, "即時新聞", right_text)

    assert compare_topic((left, right)) == []


# --- finding 46: publication time, not fetch time, sets the window ------------------------------------------------------

def test_articles_published_weeks_apart_do_not_group_even_when_fetched_together():
    left, right = arrivals("500人", "5000人")
    published = {1: "2026-08-07T01:00:00Z", 2: "2026-09-03T01:00:00Z"}

    assert group_revision(right, (left,)) is not None  # fetched together
    assert group_revision(right, (left,), event_times=published) is None
    assert compare_topic((left, right), event_times=published) == []
    assert group_revision(right, (left,), event_times={1: "2026-09-20T01:00:00Z"}) is not None


# --- finding 47: more bound and estimate words, and ranks -----------------------------------------------------------------

@pytest.mark.parametrize(
    ("left_phrase", "right_phrase"),
    [("突破500人", "5000人"), ("高達500人", "5000人"), ("多達500人", "5000人"), ("不下500人", "5000人"),
     ("最多30人", "300人"), ("預估500人", "5000人"), ("估計500人", "5000人"), ("低於500人", "5000人"),
     ("前10名", "前100名")],
)
def test_bounds_estimates_and_ranks_are_not_exact_figures(left_phrase, right_phrase):
    assert compare_topic(arrivals(left_phrase, right_phrase)) == []


@pytest.mark.parametrize("phrase", ["close to 100 troops", "as many as 100 troops", "upwards of 100 troops"])
def test_english_bound_phrases_are_approximate(phrase):
    exercise = "Taipei Defense Ministry reports {} deployed at the coastal exercise."
    left = revision(1, "https://alpha.test/a", "Taipei defense forum", exercise.format(phrase))
    right = revision(2, "https://bravo.test/b", "Taipei security forum", exercise.format("1000 troops"))

    assert compare_topic((left, right)) == []


# --- finding 48: readable labels -------------------------------------------------------------------------------------------

def test_labels_keep_whole_names_and_drop_number_fragments():
    body = "立法院三讀通過國防特別預算條例。行政院感謝立法院支持，國道公路警察局也將加強執勤。"
    left, right = pair("即時新聞 花蓮6.2地震", body, "即時新聞 花蓮6.2地震後", body + "朝野立委在議場外表態。")

    group = group_revision(right, (left,))

    tokens = group.label.split()
    assert "立法院" in tokens and "感謝立法院" not in tokens
    assert "國道公路警察局" in tokens and "道公路警察局" not in tokens
    assert not any(token.isdigit() for token in tokens)
    assert "即時新" not in tokens


# --- finding 49: query-string article ids ---------------------------------------------------------------------------------

def test_articles_identified_by_query_string_are_different_articles():
    left, right = arrivals(
        "500人", "5000人", "https://www.setn.com/News.aspx?NewsID=1512345", "https://www.setn.com/News.aspx?NewsID=1512346",
    )

    group = group_revision(right, (left,))

    assert group is not None and group.confidence == "possible"


def test_summaries_cite_the_identifying_query_but_no_secret():
    left, right = arrivals(
        "500人", "5000人", "https://www.setn.com/News.aspx?NewsID=1512345&token=abc#top", "https://bravo.test/arrivals",
    )

    [candidate] = compare_topic((left, right))

    assert "https://www.setn.com/News.aspx?NewsID=1512345 " in candidate.summary
    assert "token" not in candidate.summary and "abc" not in candidate.summary
