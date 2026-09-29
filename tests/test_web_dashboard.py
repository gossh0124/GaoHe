import colorsys
from html.parser import HTMLParser
from pathlib import Path
import re
import sqlite3

import pytest

from gaohe.config import Settings
from gaohe.domain import Finding
from gaohe.web import (
    EVIDENCE_RELATION_LABELS, EVIDENCE_STATUS_LABELS, FINDING_LABELS, HELP_NO_VERDICT, HELP_SOURCE_FAILURE,
    HELP_UNMARKED, MARK_COLORS, PALETTE, REVIEW_STATUS_LABELS, SEARCH_LEAD_LABEL, SOURCE_ERROR_TEXT,
    STORE_UNAVAILABLE_NOTICE, build_snapshot, render_article, render_status_page, runtime_section,
)


def finding(kind="factual_contradiction", start=0, end=4, evidence_status="retrieved", review="unreviewed", **changes):
    values = dict(
        id=1, revision_id=11, claim_id=None, finding_type=kind, summary="摘要", start=start, end=end,
        status="resolved", evidence_status=evidence_status, visible=True, review_status=review,
    )
    values.update(changes)
    return Finding(**values)


def evidence(**changes):
    item = {
        "url": "https://record.example/doc", "title": "官方紀錄", "provider": "direct",
        "retrieved_at": "2026-09-20T03:00:00Z", "relation": "supports", "status": "retrieved",
        "source_kind": "direct", "rationale": "紀錄與報導一致",
    }
    item.update(changes)
    return item


def full_snapshot():
    return {
        "inbox": [{
            "article_id": 1, "revision_id": 11, "title": "市議會通過預算", "url": "https://news.example/a",
            "source": "範例日報", "published_at": "2026-09-20T01:00:00Z", "updated_at": "2026-09-20T02:00:00Z",
            "text": "市議會通過預算，並稱將改善交通。", "analysis_status": "completed",
            "annotations": (finding(start=0, end=7),), "evidence": [evidence()], "pending_findings": 2,
        }],
        "findings": [{
            "id": 1, "revision_id": 11, "article_id": 1, "article_title": "市議會通過預算",
            "article_url": "https://news.example/a", "source": "範例日報", "finding_type": "unsupported_inference",
            "summary": "把預期效果寫成已證實", "start": 8, "end": 15, "status": "resolved",
            "evidence_status": "insufficient_scope", "visible": True, "review_status": "confirmed",
            "reviewed_at": "2026-09-21T00:00:00Z",
            "evidence": [evidence(relation="contradicts", rationale="交通局報告未支持此說法")],
        }],
        "comparisons": [{
            "id": 5, "label": "市府預算案", "confidence": "high", "status": "active",
            "articles": [
                {"title": "市議會通過預算", "url": "https://news.example/a", "source": "範例日報", "revision_id": 11},
                {"title": "預算三讀", "url": "https://other.example/b", "source": "另一家報", "revision_id": 12},
            ],
        }],
        "sources": [
            {"id": 1, "name": "範例日報", "enabled": True, "status": "ok", "checked_at": "2026-09-20T02:00:00Z",
             "candidates_seen": 3, "error": None},
            {"id": 2, "name": "停刊週報", "enabled": False, "status": "failed", "checked_at": "2026-09-20T02:00:00Z",
             "candidates_seen": 0, "error": "HTTP 403 cookie=raw-error-secret"},
        ],
        "last_run": {
            "started_at": "2026-09-20T02:00:00Z", "finished_at": "2026-09-20T02:01:00Z", "sources_checked": 2,
            "candidates_seen": 3, "revisions_created": 1, "failures": 1,
        },
        "analysis": {"pending": 4, "running": 1, "completed": 9, "failed": 2, "skipped": 3, "unanalyzed": 5},
        "runtime": runtime_section(Settings(llm_provider="gemini", llm_model="gemini-2.5-flash-lite", llm_api_key="k" * 20)),
    }


class _Headings(HTMLParser):
    def __init__(self):
        super().__init__()
        self.headings = []
        self._in_h2 = False
        self._english = 0

    def handle_starttag(self, tag, attrs):
        if tag == "h2":
            self._in_h2 = True
            self.headings.append("")
        elif self._in_h2 and ("lang", "en") in attrs:
            self._english += 1

    def handle_endtag(self, tag):
        if tag == "h2":
            self._in_h2 = False
        elif tag == "span" and self._english:
            self._english -= 1

    def handle_data(self, data):
        if self._in_h2 and not self._english:
            self.headings[-1] += data


def test_page_is_zh_tw_with_sections_in_spec_order():
    page = render_status_page(full_snapshot())
    parser = _Headings()
    parser.feed(page)

    assert '<html lang="zh-Hant-TW">' in page
    assert parser.headings == ["新文章收件匣", "發現事項", "同題對照", "來源狀態", "分析佇列", "本機設定", "說明"]


def test_all_labels_are_zh_tw():
    page = render_status_page(full_snapshot())

    for expected in (
        "事實矛盾", "推論超出證據", "已取得證據", "搜尋範圍不足", "來源一致", "找到反向證據", "待人工確認", "已人工確認",
        "同題", "已完成分析", "2 項候選問題待補查", "同題對照：市府預算案（同題）", "正常", "檢查失敗", "已停用",
        "發布：2026-09-20T01:00:00Z", "更新：2026-09-20T02:00:00Z", "人工確認時間：2026-09-21T00:00:00Z",
    ):
        assert expected in page
    for english in ("Factual contradiction", "Pending check", "Evidence retrieved", "No summary", "candidates"):
        assert english not in page


@pytest.mark.parametrize(("labels", "expected"), [
    (FINDING_LABELS, {"factual_contradiction": "事實矛盾", "material_cross_media_difference": "實質跨媒體差異",
                      "unsupported_inference": "推論超出證據"}),
    (EVIDENCE_STATUS_LABELS, {"pending": "待查證", "retrieved": "已取得證據", "retrieval_failed": "取回失敗",
                              "insufficient_scope": "搜尋範圍不足"}),
    (EVIDENCE_RELATION_LABELS, {"supports": "來源一致", "contradicts": "找到反向證據", "context": "背景資料"}),
    (REVIEW_STATUS_LABELS, {"unreviewed": "待人工確認", "confirmed": "已人工確認", "dismissed": "已駁回"}),
])
def test_label_tables_match_the_ui_contract(labels, expected):
    assert labels == expected


@pytest.mark.parametrize(("confidence", "label"), [
    ("high", "同題"), ("possible", "可能同題·待確認"), ("low", "可能同題·待確認"), (None, "可能同題·待確認"),
    (["high"], "可能同題·待確認"),
])
def test_topic_confidence_is_conservative(confidence, label):
    page = render_status_page({"comparisons": [{"label": "主題", "confidence": confidence}]})

    assert f"<strong>主題</strong> <span class='badge'>{label}</span>" in page


def test_rationale_is_shown_under_each_evidence_link():
    page = render_status_page(full_snapshot())

    assert re.search(
        r"<li><a class='evidence-link' href='https://record.example/doc' rel='noreferrer'>官方紀錄</a>[^<]*"
        r"<span class='meta'>[^<]*</span>.*?<p class='rationale'>判讀理由：紀錄與報導一致</p></li>",
        page,
    )
    assert "判讀理由：交通局報告未支持此說法" in page


def test_search_leads_are_never_presented_as_evidence_relations():
    page = render_status_page({"findings": [{
        "finding_type": "factual_contradiction", "summary": "s",
        "evidence": [evidence(source_kind="search", relation="contradicts", title="搜尋結果", rationale=None)],
    }]})

    entry = page[page.index("搜尋結果"):]
    entry = entry[: entry.index("</li>")]
    assert SEARCH_LEAD_LABEL in entry
    assert "找到反向證據" not in entry


def test_evidence_kind_and_status_labels_are_textual():
    page = render_status_page({"inbox": [{"text": "x", "evidence": [
        evidence(source_kind="firecrawl", status="retrieval_failed", relation="context", rationale=""),
    ]}]})

    assert "經 Firecrawl 抓取原始頁面" in page and "取回失敗" in page and "背景資料" in page
    assert "判讀理由" not in page


@pytest.mark.parametrize("rationale", [
    "api_key=abc123", "Bearer abc.def", '{"authorization": "x"}', "token: abc", "password=hunter2",
])
def test_rationale_with_credentials_is_withheld(rationale):
    page = render_status_page({"inbox": [{"text": "x", "evidence": [evidence(rationale=rationale)]}]})

    assert "判讀理由" not in page
    for fragment in ("abc123", "abc.def", "hunter2"):
        assert fragment not in page


def test_long_rationale_is_bounded_and_whitespace_collapsed():
    page = render_status_page({"inbox": [{"text": "x", "evidence": [evidence(rationale="理由\n\n" + "長" * 900)]}]})

    match = re.search(r"判讀理由：([^<]*)</p>", page)
    assert match and match.group(1).startswith("理由 長") and match.group(1).endswith("…")
    assert len(match.group(1)) == 600


def _hex_to_rgb(value):
    value = value.lstrip("#")
    return tuple(int(value[index:index + 2], 16) / 255 for index in (0, 2, 4))


def _luminance(value):
    def channel(component):
        return component / 12.92 if component <= 0.03928 else ((component + 0.055) / 1.055) ** 2.4

    red, green, blue = (channel(component) for component in _hex_to_rgb(value))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(first, second):
    lighter, darker = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


@pytest.mark.parametrize(("kind", "hues"), [
    ("factual_contradiction", ((0, 20), (340, 360))),  # light red
    ("material_cross_media_difference", ((195, 235),)),  # light blue
    ("unsupported_inference", ((255, 290),)),  # light purple
])
def test_annotation_colours_follow_spec_7_3(kind, hues):
    background, underline = MARK_COLORS[kind]
    hue, saturation, value = colorsys.rgb_to_hsv(*_hex_to_rgb(background))
    degrees = hue * 360

    assert any(low <= degrees <= high for low, high in hues)
    assert saturation <= 0.2 and value >= 0.9  # low saturation, light
    assert any(low <= colorsys.rgb_to_hsv(*_hex_to_rgb(underline))[0] * 360 <= high for low, high in hues)
    page = render_status_page({})
    assert f".mark-{kind}{{background:{background};text-decoration-color:{underline}}}" in page
    assert "text-decoration-line:underline" in page


@pytest.mark.parametrize("kind", sorted(MARK_COLORS))
def test_annotation_colours_meet_wcag_aa(kind):
    background, underline = MARK_COLORS[kind]

    assert contrast(PALETTE["text"], background) >= 4.5
    assert contrast(PALETTE["muted"], background) >= 4.5
    assert contrast(underline, background) >= 3  # non-text contrast for the underline


@pytest.mark.parametrize(("foreground", "background"), [
    ("text", "page"), ("text", "surface"), ("muted", "page"), ("muted", "surface"), ("link", "page"),
    ("link", "surface"), ("text", "notice"), ("muted", "notice"),
])
def test_page_text_colours_meet_wcag_aa(foreground, background):
    assert contrast(PALETTE[foreground], PALETTE[background]) >= 4.5


def test_evidence_status_badges_are_not_colour_coded():
    page = render_status_page({})

    for status in EVIDENCE_STATUS_LABELS:
        assert f".badge-{status}" not in page


def test_help_section_explains_what_marks_do_and_do_not_mean():
    page = render_status_page({})
    help_section = page[page.index("id='help'"):]

    for statement in (HELP_UNMARKED, HELP_NO_VERDICT, HELP_SOURCE_FAILURE):
        assert statement in help_section
    assert "未標註的句子不代表已查證" in HELP_UNMARKED
    assert "不對整篇文章下判定" in HELP_NO_VERDICT
    assert "不代表文章本身有問題" in HELP_SOURCE_FAILURE


def test_empty_snapshot_shows_zh_tw_empty_states():
    page = render_status_page({"inbox": [], "findings": [], "comparisons": [], "sources": [], "last_run": None})

    for expected in (
        "目前沒有新文章。", "目前沒有可見的發現事項。", "目前沒有至少兩篇文章的同題對照。", "尚未加入或檢查任何來源。",
        "尚未執行過來源監測。", "目前沒有分析佇列資料。", "目前沒有本機設定資料。",
    ):
        assert expected in page


def test_source_errors_are_replaced_by_a_fixed_message():
    page = render_status_page(full_snapshot())

    assert SOURCE_ERROR_TEXT in page
    assert "raw-error-secret" not in page and "HTTP 403" not in page


def test_last_run_and_analysis_queue_are_rendered():
    page = render_status_page(full_snapshot())

    assert "上次監測：開始 2026-09-20T02:00:00Z，結束 2026-09-20T02:01:00Z；檢查來源 2、候選文章 3、新增版本 1、失敗 1。" in page
    for label, count in (("待分析", 4), ("分析中", 1), ("已完成分析", 9), ("分析失敗", 2), ("已略過", 3), ("尚未排入分析", 5)):
        assert f"<tr><th>{label}</th><td>{count}</td></tr>" in page


def test_unfinished_run_and_non_integer_counts_are_safe():
    page = render_status_page({"last_run": {"started_at": "t0", "finished_at": None, "failures": "many"},
                               "analysis": {"pending": True, "running": -1, "completed": "x"}})

    assert "上次監測：開始 t0，結束 尚未完成。" in page
    assert "<tr><th>待分析</th><td>0</td></tr>" in page and "<tr><th>分析中</th><td>0</td></tr>" in page


class _MarkedText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.marked = []
        self._depth = 0
        self._detail = 0

    def handle_starttag(self, tag, attrs):
        if tag == "mark":
            self._depth += 1
            self.marked.append("")
        elif tag == "span" and ("class", "annotation-detail") in attrs:
            self._detail += 1

    def handle_endtag(self, tag):
        if tag == "mark":
            self._depth -= 1
        elif tag == "span" and self._detail:
            self._detail -= 1

    def handle_data(self, data):
        if self._depth and not self._detail:
            self.marked[-1] += data


@pytest.mark.parametrize("annotation", [
    finding(visible=False),
    finding(review="dismissed"),
    finding(kind="ordinary_claim"),
    finding(kind="omission"),
    finding(start="0", end="4"),
    finding(start=True, end=4),
])
def test_only_visible_non_dismissed_v0_findings_colour_text(annotation):
    parser = _MarkedText()
    parser.feed(render_article("abcdef", (annotation,)))

    assert parser.marked == []


def test_visible_finding_colours_only_its_span():
    parser = _MarkedText()
    parser.feed(render_article("abcdef", (finding(start=1, end=3),)))

    assert parser.marked == ["bc"]


def test_render_article_ignores_non_findings_and_non_text():
    assert render_article(None, (object(), "x")) == ""  # type: ignore[arg-type]


def test_findings_list_skips_non_visible_and_unknown_types_but_lists_dismissed():
    page = render_status_page({"findings": [
        {"finding_type": "factual_contradiction", "summary": "hidden-one", "visible": False},
        {"finding_type": "ordinary", "summary": "hidden-two"},
        {"finding_type": ["factual_contradiction"], "summary": "hidden-three"},
        {"finding_type": "material_cross_media_difference", "summary": "被駁回的差異", "review_status": "dismissed",
         "reviewed_at": "2026-09-22"},
    ]})

    for hidden in ("hidden-one", "hidden-two", "hidden-three"):
        assert hidden not in page
    assert "被駁回的差異" in page and "已駁回" in page and "人工確認時間：2026-09-22" in page


def test_article_links_and_topic_badges_use_safe_urls_only():
    snapshot = {"inbox": [{"title": "標題", "url": "javascript:alert(1)", "text": "x", "revision_id": 3}],
                "comparisons": [{"label": "議題<x>", "confidence": "possible",
                                 "articles": [{"title": "t", "url": "https://a.example/?session=zz-secret", "revision_id": 3}]}]}
    page = render_status_page(snapshot)

    assert "javascript:" not in page and "zz-secret" not in page
    assert "同題對照：議題&lt;x&gt;（可能同題·待確認）" in page


def test_pending_findings_badge_ignores_non_counts():
    for value in (True, "3", -1, 0, None):
        page = render_status_page({"inbox": [{"text": "x", "pending_findings": value}]})
        assert "候選問題待補查" not in page


HOSTILE = "<script>alert('x')</script>"


@pytest.mark.parametrize("snapshot", [
    {"inbox": [{"title": HOSTILE, "source": HOSTILE, "text": HOSTILE, "published_at": HOSTILE, "analysis_status": HOSTILE,
                "updated_at": HOSTILE}]},
    {"findings": [{"finding_type": "factual_contradiction", "summary": HOSTILE, "article_title": HOSTILE, "source": HOSTILE,
                   "evidence_status": HOSTILE, "review_status": HOSTILE, "reviewed_at": HOSTILE}]},
    {"comparisons": [{"label": HOSTILE, "confidence": HOSTILE, "articles": [{"title": HOSTILE, "source": HOSTILE}]}]},
    {"sources": [{"name": HOSTILE, "status": HOSTILE, "checked_at": HOSTILE, "error": HOSTILE}]},
    {"last_run": {"started_at": HOSTILE, "finished_at": HOSTILE}},
    {"runtime": {"llm_model": HOSTILE, "data_dir": HOSTILE, "llm_api_key": HOSTILE}},
    {"inbox": [{"text": "abc", "annotations": (finding(summary=HOSTILE, evidence_status=HOSTILE, start=0, end=2),)}]},
    {"inbox": [{"text": "x", "evidence": [evidence(title=HOSTILE, provider=HOSTILE, rationale=HOSTILE,
                                                    retrieved_at=HOSTILE)]}]},
])
def test_every_snapshot_value_is_escaped(snapshot):
    page = render_status_page(snapshot)

    assert "<script>" not in page
    assert "badge-<" not in page and "mark-<" not in page


@pytest.mark.parametrize("snapshot", [
    {}, [], "text", None, {"inbox": None}, {"inbox": [{}]}, {"inbox": [None, 3, "x"]}, {"findings": [{}]},
    {"findings": [None]}, {"comparisons": [{"articles": None}]}, {"comparisons": [{"articles": [None, {}]}]},
    {"sources": [{}]}, {"sources": [{"status": {"a": 1}, "enabled": "no", "candidates_seen": "x"}]},
    {"last_run": {}}, {"last_run": "yesterday"}, {"analysis": {}}, {"analysis": []}, {"runtime": None},
    {"runtime": {"llm_api_key": ["present"]}}, {"inbox": [{"annotations": "abc", "text": 5}]},
    {"inbox": [{"annotations": [object(), finding(start=-5, end=99)], "text": None}]},
    {"inbox": [{"text": "x", "evidence": [None, {}, {"url": None}, {"url": ["x"]}, {"url": "http://[::1"}]}]},
    {"inbox": [{"text": "x", "evidence": [evidence(relation=["x"], status={"y": 1}, source_kind=["z"], rationale=7)]}]},
    {"findings": [{"finding_type": "unsupported_inference", "review_status": ["x"], "evidence_status": ["y"],
                   "visible": "yes", "evidence": "none"}]},
    {"comparisons": [{"confidence": {"high": 1}, "label": None, "articles": [{"revision_id": [1]}]}]},
    {"data_unavailable": "yes"},
])
def test_missing_or_malformed_values_never_crash_rendering(snapshot):
    page = render_status_page(snapshot)

    assert "稿核 GaoHe 本機監測" in page and page.endswith("</html>")


def test_runtime_section_reports_key_presence_without_the_key():
    settings = Settings(llm_provider="gemini", llm_model="m", llm_api_key="AIza-really-secret-value", data_dir=Path("d"))
    runtime = runtime_section(settings)
    page = render_status_page(settings)

    assert runtime["llm_api_key"] == "present" and "firecrawl_api_key" not in runtime
    assert "AIza-really-secret-value" not in repr(runtime) and "AIza-really-secret-value" not in page
    assert "已設定 <span class='meta' lang='en'>present</span>" in page
    assert "<code>LLM_API_KEY</code>" in page and "<code>DATA_DIR</code>" in page
    missing = render_status_page(Settings())
    assert "未設定 <span class='meta' lang='en'>missing</span>" in missing


def test_runtime_values_that_contain_a_configured_key_are_masked():
    key = "AIza-pasted-into-the-wrong-field"
    settings = Settings(llm_provider="gemini", llm_model=key, llm_api_key=key, data_dir=Path(f"C:/{key}/GaoHe"),
                        web_search_provider="firecrawl", firecrawl_api_key="fc-secret-1234567")
    page = render_status_page(settings)

    assert key not in page and "fc-secret-1234567" not in page
    assert page.count("（已隱藏）") == 2
    assert "Firecrawl API 金鑰" in page


def test_short_key_is_masked_only_on_exact_match():
    runtime = runtime_section(Settings(llm_model="abc", llm_api_key="abc", data_dir=Path("abcdef")))

    assert runtime["llm_model"] == "（已隱藏）"
    assert runtime["data_dir"] == "abcdef"


def test_snapshot_runtime_key_field_never_echoes_unknown_values():
    page = render_status_page({"runtime": {"LLM API key": "AIza-legacy-leak", "llm_api_key": "AIza-new-leak"}})

    assert "AIza" not in page and "狀態不明" in page


def test_legacy_runtime_keys_still_render():
    page = render_status_page({"runtime": {"LLM provider": "gemini", "LLM model": "m1", "Data directory": "d1"}})

    assert "<td>gemini</td>" in page and "<td>m1</td>" in page and "<td>d1</td>" in page


class FakeStore:
    def __init__(self, result=None, error=None):
        self.result = result if result is not None else {"inbox": [], "runtime": {"llm_api_key": "leak"}}
        self.error = error

    def dashboard_snapshot(self):
        if self.error:
            raise self.error
        return self.result


def test_build_snapshot_merges_runtime_from_settings():
    settings = Settings(llm_provider="gemini", llm_api_key="secret-key-value")
    snapshot = build_snapshot(settings, FakeStore())

    assert snapshot["inbox"] == [] and snapshot["data_unavailable"] is False
    assert snapshot["runtime"] == runtime_section(settings)


@pytest.mark.parametrize("error", [sqlite3.OperationalError("locked"), OSError("io"), ValueError("bad")])
def test_build_snapshot_turns_store_errors_into_a_runtime_only_notice(error):
    snapshot = build_snapshot(Settings(), FakeStore(error=error))
    page = render_status_page(snapshot)

    assert snapshot == {"runtime": runtime_section(Settings()), "data_unavailable": True}
    assert STORE_UNAVAILABLE_NOTICE in page and "role='status'" in page
    assert "locked" not in page


def test_build_snapshot_does_not_swallow_programming_errors():
    with pytest.raises(RuntimeError):
        build_snapshot(Settings(), FakeStore(error=RuntimeError("bug")))


def test_settings_only_page_remains_backward_compatible():
    page = render_status_page(Settings(llm_provider="gemini", data_dir=Path("data")))

    assert "目前沒有新文章。" in page and "id='runtime'" in page and STORE_UNAVAILABLE_NOTICE not in page
