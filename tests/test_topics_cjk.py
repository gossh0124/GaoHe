import time

import pytest

from gaohe.domain import MAX_QUERY_CHARS, ArticleRevision, article_content_hash
from gaohe.topics import MAX_LABEL_CHARS, MAX_SUMMARY_CHARS, compare_topic, group_revision


def revision(
    revision_id: int,
    url: str,
    title: str,
    text: str,
    fetched_at: str = "2026-09-20T12:00:00Z",
) -> ArticleRevision:
    return ArticleRevision(revision_id, revision_id, url, title, text, article_content_hash(title, text), fetched_at)


EXAMPLE_A = "行政院今天宣布，自下月起開放外籍旅客入境觀光，預計約有500人受惠。"
EXAMPLE_B = "行政院宣布下月起開放外籍旅客入境，估計受惠人數約5000人。"

TOURISM_CNA = (
    "行政院今天召開記者會宣布，自下月1日起全面開放外籍旅客入境觀光，入境後免居家檢疫，改採7天自主健康管理。"
    "行政院發言人表示，疫情趨於穩定，國內疫苗覆蓋率已超過八成，評估後決定分階段恢復國際觀光。"
    "交通部觀光署指出，首階段將先開放免簽國家旅客，旅行團人數上限為每團30人，預計每週可接待約5000名旅客。"
    "觀光署長也說，旅宿業者與旅行業者已完成防疫演練，機場將增設快篩站，確保入境流程順暢。"
)
TOURISM_UDN = (
    "行政院宣布，下月1日起開放外籍旅客入境觀光，並取消入境檢疫，改為7天自主健康管理。"
    "政院發言人指出，考量國內疫苗接種率逾八成且疫情穩定，決定逐步恢復國際旅遊。"
    "交通部觀光署表示，第一階段開放對象為免簽國家旅客，旅行團每團人數上限30人。"
    "觀光署估計，開放後每週將有數千名外籍旅客來台，桃園機場也將增設快篩站分流。"
)
ELECTRICITY = (
    "行政院今天核定經濟部提出的電價調整方案，住宅用電每度電價維持不變，產業用電平均調漲11%。"
    "經濟部長表示，國際燃料價格居高不下，台電累積虧損已超過2000億元，必須合理反映成本。"
    "工商團體對此表示，漲幅高於預期，將衝擊出口產業競爭力，盼政府提供配套措施。"
)
FIRE = (
    "台北市萬華區一處老舊公寓今天凌晨發生火警，台北市消防局獲報後派遣30車、80人到場搶救。"
    "消防局表示，火警共造成3人受傷送醫，起火原因仍待火災調查科鑑定。"
    "台北市政府表示，將加強老舊建物消防安全檢查，並補助住戶裝設住警器。"
)
TYPHOON = (
    "中央氣象署今天上午發布海上颱風警報，颱風中心目前位於鵝鑾鼻東南方海面，以每小時20公里速度向西北前進。"
    "屏東縣政府已成立災害應變中心，要求各鄉鎮完成疏散撤離準備。"
)

ARRIVALS = "行政院今天宣布開放外籍旅客入境觀光，首批旅行團共有{}抵達桃園機場。"
ARRIVALS_PEER = "行政院宣布開放外籍旅客入境觀光後，首批旅行團共{}抵達桃園機場。"


def arrivals(left_phrase: str, right_phrase: str) -> tuple[ArticleRevision, ArticleRevision]:
    left = revision(1, "https://alpha.test/arrivals", "政院開放外籍旅客入境", ARRIVALS.format(left_phrase))
    right = revision(2, "https://bravo.test/arrivals", "外籍旅客入境觀光首發團抵台", ARRIVALS_PEER.format(right_phrase))
    return left, right


# Grouping


def test_example_chinese_pair_groups_high_across_hosts():
    existing = revision(1, "https://alpha.test/tourism", "政院宣布開放外籍旅客入境", EXAMPLE_A)
    incoming = revision(2, "https://bravo.test/tourism", "下月起開放外籍旅客入境觀光", EXAMPLE_B, "2026-09-21T08:00:00Z")

    topic = group_revision(incoming, (existing,))

    assert topic is not None
    assert (topic.confidence, topic.status) == ("high", "active")
    assert "行政院" in topic.label
    assert "開放外籍旅客入境" in topic.label


def test_example_chinese_pair_on_the_same_host_is_only_possible():
    existing = revision(1, "https://alpha.test/tourism", "政院宣布開放外籍旅客入境", EXAMPLE_A)
    incoming = revision(2, "https://alpha.test/tourism-update", "下月起開放外籍旅客入境觀光", EXAMPLE_B)

    topic = group_revision(incoming, (existing,))

    assert topic is not None
    assert (topic.confidence, topic.status) == ("possible", "possible")


def test_example_chinese_pair_outside_the_window_does_not_group():
    existing = revision(1, "https://alpha.test/tourism", "政院宣布開放外籍旅客入境", EXAMPLE_A)
    late = revision(2, "https://bravo.test/tourism", "下月起開放外籍旅客入境觀光", EXAMPLE_B, "2026-09-23T12:00:01Z")

    assert group_revision(late, (existing,)) is None


def test_example_chinese_pair_produces_no_candidate_because_both_counts_are_approximate():
    left = revision(1, "https://alpha.test/tourism", "政院宣布開放外籍旅客入境", EXAMPLE_A)
    right = revision(2, "https://bravo.test/tourism", "下月起開放外籍旅客入境觀光", EXAMPLE_B)

    assert compare_topic((left, right)) == []


def test_long_same_event_chinese_reports_group_high():
    cna = revision(1, "https://cna.test/tourism", "政院拍板 下月起開放外籍旅客入境觀光", TOURISM_CNA)
    udn = revision(2, "https://udn.test/tourism", "觀光解封！外籍旅客下月入境免檢疫", TOURISM_UDN)

    topic = group_revision(udn, (cna,))

    assert topic is not None and topic.confidence == "high"
    assert compare_topic((cna, udn)) == []


@pytest.mark.parametrize(
    ("left_text", "right_text"),
    [
        (TOURISM_CNA, FIRE),
        (TOURISM_CNA, TYPHOON),
        (FIRE, TYPHOON),
        (TOURISM_UDN, ELECTRICITY),
        (TOURISM_CNA, ELECTRICITY),
    ],
)
def test_unrelated_chinese_articles_do_not_group(left_text, right_text):
    left = revision(1, "https://alpha.test/a", "即時新聞", left_text)
    right = revision(2, "https://bravo.test/b", "即時新聞", right_text)

    assert group_revision(right, (left,)) is None
    assert compare_topic((left, right)) == []


def test_chinese_articles_that_only_share_an_official_and_title_are_not_high():
    existing = revision(1, "https://alpha.test/a", "政院長視察", "行政院長卓榮泰今天視察花蓮光復鄉災區，慰問受災民眾。")
    incoming = revision(2, "https://bravo.test/b", "政院長出席", "行政院長卓榮泰今天出席國慶籌備會議，聽取簡報。")

    topic = group_revision(incoming, (existing,))

    assert topic is None or topic.confidence != "high"


def test_chinese_articles_with_matching_titles_but_unrelated_bodies_are_not_high():
    existing = revision(1, "https://alpha.test/a", "行政院宣布開放外籍旅客入境", "颱風逼近，屏東縣政府成立災害應變中心。")
    incoming = revision(2, "https://bravo.test/b", "行政院宣布開放外籍旅客入境", "台北市消防局深夜救出受困住戶。")

    topic = group_revision(incoming, (existing,))

    assert topic is None or topic.confidence != "high"


def test_entity_matches_regardless_of_the_words_before_it():
    existing = revision(1, "https://alpha.test/a", "開放入境", "今天行政院宣布開放外籍旅客入境觀光，旅行團每團上限30人。")
    incoming = revision(2, "https://bravo.test/b", "開放入境", "據報行政院昨天宣布開放外籍旅客入境觀光，旅行團每團上限30人。")

    topic = group_revision(incoming, (existing,))

    assert topic is not None and topic.confidence == "high"
    assert "行政院" in topic.label.split()


def test_common_words_ending_in_a_suffix_character_are_not_entities():
    # 社會, 機會 and 全國 end in 會 / 國 but name no organisation, so the pair has no shared entity.
    existing = revision(1, "https://alpha.test/a", "社會觀察", "全國社會各界把握機會，推動長照服務與托育補助。")
    incoming = revision(2, "https://bravo.test/b", "社會觀察", "全國社會各界把握機會，推動長照服務與托育補助。")

    topic = group_revision(incoming, (existing,))

    assert topic is not None and topic.confidence == "possible"


def test_taiwan_variant_characters_normalize_before_comparison():
    existing = revision(1, "https://alpha.test/a", "臺北市政府補助老舊公寓", "臺北市政府宣布補助老舊公寓裝設住警器，首批共有500戶受惠。")
    incoming = revision(2, "https://bravo.test/b", "台北市政府補助老舊公寓", "台北市政府宣布補助老舊公寓裝設住警器，首批共有5000戶受惠。")

    topic = group_revision(incoming, (existing,))
    candidates = compare_topic((existing, incoming))

    assert topic is not None and topic.confidence == "high"
    assert "台北市政府" in topic.label
    assert len(candidates) == 1
    assert existing.text[candidates[0].start:candidates[0].end] == "500戶"


def test_fullwidth_latin_text_groups_with_ascii_text():
    existing = revision(1, "https://alpha.test/a", "Taipei defense forum", "Taipei Defense Ministry forum opens with 1000 delegates.")
    incoming = revision(2, "https://bravo.test/b", "Ｔａｉｐｅｉ security forum", "Ｔａｉｐｅｉ Ｄｅｆｅｎｓｅ Ｍｉｎｉｓｔｒｙ ｆｏｒｕｍ ｏｐｅｎｓ ｗｉｔｈ １０００ ｄｅｌｅｇａｔｅｓ.")

    topic = group_revision(incoming, (existing,))

    assert topic is not None and topic.confidence == "high"


def test_label_is_bounded_and_deterministic():
    title = "行政院宣布開放外籍旅客入境觀光" * 20
    existing = revision(1, "https://alpha.test/a", title, EXAMPLE_A)
    incoming = revision(2, "https://bravo.test/b", title, EXAMPLE_B)

    first = group_revision(incoming, (existing,))
    second = group_revision(incoming, (existing,))

    assert first is not None and second is not None
    assert first.label == second.label
    assert 0 < len(first.label) <= MAX_LABEL_CHARS


# Numbers


def test_exact_chinese_counts_an_order_of_magnitude_apart_produce_one_candidate():
    left, right = arrivals("500人", "5000人")

    candidates = compare_topic((left, right))

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.finding_type == "material_cross_media_difference"
    assert candidate.materiality == "material"
    assert candidate.claim_id is None
    assert candidate.revision_id == left.id
    assert left.text[candidate.start:candidate.end] == "500人"
    assert "500人 versus 5000人" in candidate.summary
    assert "https://alpha.test/arrivals" in candidate.summary
    assert "https://bravo.test/arrivals" in candidate.summary
    assert len(candidate.summary) <= MAX_SUMMARY_CHARS
    assert candidate.query is not None and len(candidate.query) <= MAX_QUERY_CHARS


def test_candidate_is_reported_on_the_first_revision_of_the_pair():
    left, right = arrivals("500人", "5000人")

    candidates = compare_topic((right, left))

    assert len(candidates) == 1
    assert candidates[0].revision_id == right.id
    assert right.text[candidates[0].start:candidates[0].end] == "5000人"


@pytest.mark.parametrize(
    ("left_phrase", "right_phrase", "expected"),
    [
        ("5萬人", "5000人", "5萬人"),
        ("3千人", "3萬人", "3千人"),
        ("2千5百萬元", "250萬元", "2千5百萬元"),
        ("1億2千萬元", "1200萬元", "1億2千萬元"),
        ("1.5萬人", "1500人", "1.5萬人"),
        ("5,000人", "500人", "5,000人"),
        ("500名", "5000人", "500名"),
        ("500位", "5000名", "500位"),
    ],
)
def test_chinese_magnitudes_are_parsed_before_comparing(left_phrase, right_phrase, expected):
    left, right = arrivals(left_phrase, right_phrase)

    candidates = compare_topic((left, right))

    assert len(candidates) == 1
    assert left.text[candidates[0].start:candidates[0].end] == expected


@pytest.mark.parametrize(
    ("left_phrase", "right_phrase"),
    [
        ("1.2萬人", "12000人"),
        ("5萬人", "50000人"),
        ("2千5百萬元", "2500萬元"),
        ("1億2千萬元", "1.2億元"),
        ("450人", "500人"),
        ("500人", "520人"),
        ("500人", "500人"),
    ],
)
def test_equal_values_and_same_magnitude_differences_are_not_candidates(left_phrase, right_phrase):
    left, right = arrivals(left_phrase, right_phrase)

    assert compare_topic((left, right)) == []


@pytest.mark.parametrize(
    ("left_phrase", "right_phrase"),
    [
        ("近500人", "約520人"),
        ("500人左右", "450人"),
        ("近500人", "5000人"),
        ("約500人", "5000人"),
        ("大約500人", "5000人"),
        ("將近500人", "5000人"),
        ("逾500人", "5000人"),
        ("超過500人", "5000人"),
        ("500多人", "5000人"),
        ("500餘人", "5000人"),
        ("500人上下", "5000人"),
        ("500人以上", "5000人"),
        ("5000人", "近500人"),
        ("5000人", "500人左右"),
        ("500至600人", "5000人"),
        ("500人至600人", "5000人"),
        ("500-600人", "5000人"),
        ("5、6人", "500人"),
    ],
)
def test_approximate_and_ranged_chinese_numbers_never_become_candidates(left_phrase, right_phrase):
    left, right = arrivals(left_phrase, right_phrase)

    assert compare_topic((left, right)) == []


@pytest.mark.parametrize(
    ("left_phrase", "right_phrase"),
    [
        ("3個月", "30個月"),
        ("5天", "50天"),
        ("2小時", "20小時"),
        ("10分鐘", "100分鐘"),
        ("2025年", "30000年"),
        ("第5人", "第500人"),
        ("500輛", "5000人"),
        ("500人", "5000人次"),
    ],
)
def test_dates_ordinals_and_different_units_are_not_compared(left_phrase, right_phrase):
    left, right = arrivals(left_phrase, right_phrase)

    assert compare_topic((left, right)) == []


def test_numeric_difference_requires_a_shared_entity_in_the_same_sentence():
    left = revision(1, "https://alpha.test/a", "開放入境", "行政院宣布開放外籍旅客入境觀光。首批旅行團共有500人抵達桃園機場。")
    right = revision(2, "https://bravo.test/b", "開放入境", "行政院宣布開放外籍旅客入境觀光。首批旅行團共有5000人抵達桃園機場。")

    topic = group_revision(right, (left,))

    assert topic is not None and topic.confidence == "high"
    assert compare_topic((left, right)) == []


def test_numeric_difference_requires_two_shared_event_anchors_in_the_same_sentence():
    left = revision(
        1,
        "https://alpha.test/a",
        "開放入境",
        "行政院宣布開放外籍旅客入境觀光，旅行團每團上限30人。行政院統計，首日共有500人入境。",
    )
    right = revision(
        2,
        "https://bravo.test/b",
        "開放入境",
        "行政院宣布開放外籍旅客入境觀光，旅行團每團上限30人。行政院另外編列預算補助5000人。",
    )

    topic = group_revision(right, (left,))

    assert topic is not None and topic.confidence == "high"
    assert compare_topic((left, right)) == []


def test_fullwidth_digits_and_length_changing_characters_keep_spans_on_the_original_text():
    # ㍻ expands to two characters under NFKC, so normalized offsets run one ahead.
    left = revision(1, "https://alpha.test/a", "政院開放外籍旅客入境", "㍻紀念。" + ARRIVALS.format("５００人"))
    right = revision(2, "https://bravo.test/b", "外籍旅客入境觀光首發團抵台", ARRIVALS_PEER.format("5000人"))

    candidates = compare_topic((left, right))

    assert len(candidates) == 1
    assert left.text[candidates[0].start:candidates[0].end] == "５００人"
    assert "５００人 versus 5000人" in candidates[0].summary


def test_combining_marks_before_a_claim_keep_spans_on_the_original_text():
    prefix = "Cafe\u0301 notes. "
    left = revision(1, "https://alpha.test/a", "政院開放外籍旅客入境", prefix + ARRIVALS.format("500人"))
    right = revision(2, "https://bravo.test/b", "外籍旅客入境觀光首發團抵台", ARRIVALS_PEER.format("5000人"))

    candidates = compare_topic((left, right))

    assert len(candidates) == 1
    assert left.text[candidates[0].start:candidates[0].end] == "500人"


EXERCISE = "Taipei Defense Ministry reports {} deployed at the coastal exercise."


def test_english_scale_words_are_parsed_as_magnitudes():
    left = revision(1, "https://alpha.test/a", "Taipei defense forum", EXERCISE.format("2 million troops"))
    right = revision(2, "https://bravo.test/b", "Taipei security forum", EXERCISE.format("200,000 troops"))
    same = revision(3, "https://charlie.test/c", "Taipei security forum", EXERCISE.format("2,000,000 troops"))

    candidates = compare_topic((left, right))

    assert len(candidates) == 1
    assert left.text[candidates[0].start:candidates[0].end] == "2 million troops"
    assert compare_topic((left, same)) == []


@pytest.mark.parametrize(
    "phrase",
    ["between 100 and 150 troops", "at least 100 troops", "100 troops or more", "up to 100 troops", "100 to 150 troops"],
)
def test_english_ranges_and_bounds_are_approximate(phrase):
    left = revision(1, "https://alpha.test/a", "Taipei defense forum", EXERCISE.format(phrase))
    right = revision(2, "https://bravo.test/b", "Taipei security forum", EXERCISE.format("1000 troops"))

    assert compare_topic((left, right)) == []


# Opposites


BUDGET = "立法院今天三讀{}國防特別預算條例，朝野立委在議場外表態。"


def budget_pair(left_verb: str, right_verb: str) -> tuple[ArticleRevision, ArticleRevision]:
    left = revision(1, "https://alpha.test/budget", "國防特別預算條例", BUDGET.format(left_verb))
    right = revision(2, "https://bravo.test/budget", "國防特別預算條例三讀", BUDGET.format(right_verb))
    return left, right


@pytest.mark.parametrize(
    ("left_verb", "right_verb"),
    [("通過", "否決"), ("否決", "通過"), ("批准", "否決"), ("同意", "反對"), ("承認", "否認"), ("確認", "否認")],
)
def test_chinese_opposites_for_the_same_local_event_produce_one_candidate(left_verb, right_verb):
    left, right = budget_pair(left_verb, right_verb)

    candidates = compare_topic((left, right))

    assert len(candidates) == 1
    assert left.text[candidates[0].start:candidates[0].end] == left_verb
    assert f"{left_verb} versus {right_verb}" in candidates[0].summary


@pytest.mark.parametrize(
    ("left_text", "right_text"),
    [
        ("經濟部統計，今年電價上漲，工業用電受衝擊。", "經濟部統計，今年電價下跌，工業用電受衝擊。"),
        ("交通部宣布下月起開放外籍旅客入境觀光。", "交通部宣布下月起關閉外籍旅客入境觀光。"),
        ("台北地方法院今天宣判被告有罪，全案可上訴。", "台北地方法院今天宣判被告無罪，全案可上訴。"),
        ("台北市警察局今天逮捕涉案男子，全案移送地檢署。", "台北市警察局今天釋放涉案男子，全案移送地檢署。"),
        ("衛福部統計，今年長照床位增加，偏鄉仍不足。", "衛福部統計，今年長照床位減少，偏鄉仍不足。"),
    ],
)
def test_extended_chinese_opposite_pairs(left_text, right_text):
    left = revision(1, "https://alpha.test/a", "即時新聞", left_text)
    right = revision(2, "https://bravo.test/b", "即時新聞", right_text)

    assert len(compare_topic((left, right))) == 1


@pytest.mark.parametrize(
    ("left_verb", "right_verb"),
    [
        ("未通過", "否決"),
        ("沒有通過", "否決"),
        ("通過", "未否決"),
        ("不同意", "反對"),
        ("尚未確認", "否認"),
        ("可能通過", "否決"),
        ("將通過", "否決"),
        ("是否通過", "否決"),
    ],
)
def test_negated_or_hedged_chinese_verbs_are_not_opposites(left_verb, right_verb):
    left, right = budget_pair(left_verb, right_verb)

    assert compare_topic((left, right)) == []


def test_chinese_reopening_and_adjectival_forms_are_not_opposites():
    reopened = revision(1, "https://alpha.test/a", "即時新聞", "交通部宣布下月起重新開放外籍旅客入境觀光。")
    closed = revision(2, "https://bravo.test/b", "即時新聞", "交通部宣布下月起關閉外籍旅客入境觀光。")
    open_style = revision(3, "https://charlie.test/c", "即時新聞", "交通部宣布下月起開放式管理外籍旅客入境觀光。")

    assert compare_topic((reopened, closed)) == []
    assert compare_topic((open_style, closed)) == []


def test_sentences_mentioning_both_poles_are_not_opposites():
    left = revision(1, "https://alpha.test/a", "國防特別預算條例", "立法院今天三讀通過國防特別預算條例，並否決在野黨版本。")
    right = revision(2, "https://bravo.test/b", "國防特別預算條例三讀", "立法院今天三讀否決國防特別預算條例，朝野立委在議場外表態。")

    assert compare_topic((left, right)) == []


def test_chinese_opposites_in_sentences_without_a_shared_entity_are_not_compared():
    left = revision(1, "https://alpha.test/a", "國防特別預算條例", "立法院今天審查國防特別預算條例。會議最後通過。")
    right = revision(2, "https://bravo.test/b", "國防特別預算條例", "立法院今天審查國防特別預算條例。會議最後否決。")

    assert group_revision(right, (left,)) is not None
    assert compare_topic((left, right)) == []


def test_chinese_opposites_need_two_shared_event_anchors():
    left = revision(1, "https://alpha.test/a", "國防特別預算條例", "國防特別預算條例審查順利。立法院通過。")
    right = revision(2, "https://bravo.test/b", "國防特別預算條例", "國防特別預算條例審查順利。立法院否決。")

    assert compare_topic((left, right)) == []


def test_negated_english_verbs_are_not_opposites():
    left = revision(1, "https://alpha.test/a", "Taipei defense forum", "Taipei Defense Ministry has not approved the coastal permit.")
    right = revision(2, "https://bravo.test/b", "Taipei security forum", "Taipei Defense Ministry rejected the coastal permit.")

    assert compare_topic((left, right)) == []


def test_english_sentences_mentioning_both_poles_are_not_opposites():
    both = "Taipei Defense Ministry approved the coastal permit and rejected the harbor plan."
    left = revision(1, "https://alpha.test/a", "Taipei defense forum", both)
    right = revision(2, "https://bravo.test/b", "Taipei security forum", "Taipei Defense Ministry rejected the coastal permit.")

    assert compare_topic((left, right)) == []


# Safety and bounds


def test_chinese_candidates_never_carry_url_secrets():
    left = revision(1, "https://alpha.test/arrivals?token=secret#api_key=hidden", "政院開放外籍旅客入境", ARRIVALS.format("500人"))
    right = revision(2, "https://bravo.test/arrivals?session=abc", "外籍旅客入境觀光首發團抵台", ARRIVALS_PEER.format("5000人"))
    credentialed = revision(3, "https://user:secret@charlie.test/arrivals", "外籍旅客入境觀光首發團抵台", ARRIVALS_PEER.format("5000人"))

    candidates = compare_topic((left, right))

    assert len(candidates) == 1
    text = candidates[0].summary + (candidates[0].query or "")
    for secret in ("secret", "hidden", "token", "api_key", "session", "abc"):
        assert secret not in text
    assert compare_topic((left, credentialed)) == []


def test_results_are_deterministic_across_repeated_calls():
    left, right = arrivals("500人", "5000人")

    assert compare_topic((left, right)) == compare_topic((left, right))
    assert group_revision(right, (left,)) == group_revision(right, (left,))


def _long_pair() -> tuple[ArticleRevision, ArticleRevision]:
    lead = "行政院宣布開放外籍旅客入境觀光，首批旅行團抵達桃園機場，立法院通過相關預算。"
    left_body = lead + "".join(f"行政院補助{100 + index % 900}人，立法院通過。" for index in range(1_300))
    right_body = lead + "".join(f"行政院統計{1_000 + index % 9_000}人，立法院否決。" for index in range(1_300))
    left = revision(1, "https://alpha.test/long", "開放外籍旅客入境", left_body[:20_000])
    right = revision(2, "https://bravo.test/long", "開放外籍旅客入境", right_body[:20_000])
    return left, right


def test_long_texts_stay_bounded(monkeypatch):
    import gaohe.topics as topics

    left, right = _long_pair()
    assert len(left.text) == len(right.text) == 20_000
    calls = 0
    original = topics._anchor_count

    def counting(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(topics, "_anchor_count", counting)
    topics._doc.cache_clear()

    assert group_revision(right, (left,)) is not None
    assert compare_topic((left, right)) == []
    # Two whole-article comparisons per grouping check, then at most the fixed budget of sentence checks.
    assert calls <= 4 + topics._MAX_CONTEXT_CHECKS


def test_opposite_heavy_long_texts_stay_bounded(monkeypatch):
    import gaohe.topics as topics

    lead = "行政院宣布開放外籍旅客入境觀光，首批旅行團抵達桃園機場。"
    left = revision(1, "https://alpha.test/long", "開放外籍旅客入境", (lead + "行政院通過。" * 4_000)[:20_000])
    right = revision(2, "https://bravo.test/long", "開放外籍旅客入境", (lead + "行政院否決。" * 4_000)[:20_000])
    calls = 0
    original = topics._anchor_count

    def counting(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(topics, "_anchor_count", counting)
    topics._doc.cache_clear()

    assert group_revision(right, (left,)) is not None
    assert compare_topic((left, right)) == []
    assert 4 < calls <= 4 + topics._MAX_OPPOSITE_HITS**2


def test_a_difference_after_long_shared_filler_is_still_found():
    filler = "交通部宣布颱風期間暫停部分渡輪航班，旅客出發前應留意最新資訊。" * 350
    left = revision(1, "https://alpha.test/long", "政院開放外籍旅客入境", filler + ARRIVALS.format("500人"))
    right = revision(2, "https://bravo.test/long", "外籍旅客入境觀光首發團抵台", filler + ARRIVALS_PEER.format("5000人"))

    candidates = compare_topic((left, right))

    assert len(left.text) > 10_000
    assert len(candidates) == 1
    assert left.text[candidates[0].start:candidates[0].end] == "500人"


def test_long_texts_run_fast():
    left, right = _long_pair()
    unpunctuated = revision(3, "https://charlie.test/long", "開放外籍旅客入境", ("行政院通過補助500人" * 2_000)[:20_000])

    started = time.perf_counter()
    compare_topic((left, right, unpunctuated))
    group_revision(unpunctuated, (left, right))
    elapsed = time.perf_counter() - started

    assert elapsed < 5
