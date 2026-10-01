from contextlib import contextmanager
from http.client import HTTPConnection
from threading import Thread

from gaohe.config import Settings
from gaohe.domain import Finding
from gaohe.web import render_article, render_status_page, start_server

KEY = "AIza-very-secret-key"


def finding(start, end, kind="factual_contradiction", summary="說法不同", visible=True):
    return Finding(1, 1, kind, summary, start, end, "retrieved", visible)


def test_render_article_escapes_text_marks_visible_spans_and_clips():
    html = render_article("<b>甲乙丙丁</b>", [finding(3, 5, summary="<x>"), finding(0, 99, visible=False), finding(5, 2)])
    assert html.startswith("&lt;b&gt;<mark") and "<b>" not in html
    assert html.count("<mark") == 1 and "mark-factual_contradiction" in html and "&lt;x&gt;" in html
    assert render_article("abc", [finding(-5, 2, kind="material_cross_media_difference")]) == "abc"  # removed type never marks


def test_status_page_is_zh_tw_explains_results_and_never_shows_the_key():
    snapshot = {
        "inbox": [{
            "title": "開放觀光", "url": "https://news.example/a?token=%2A%2A%2A", "source": "甲報", "updated_at": "2026-10-01",
            "text": "下月起開放", "analysis_status": "completed", "pending_findings": 1,
            "annotations": [finding(0, 5)],
            "evidence": [{"url": "https://u:p@gov.example/a", "title": "公告", "status": "retrieved", "relation": "contradicts", "rationale": "日期不同"}],
        }],
        "sources": [{"name": "<script>", "enabled": False, "status": "failed", "error": "secret detail", "candidates_seen": 2}],
        "last_run": None,
    }
    page = render_status_page(snapshot, Settings(llm_api_key=KEY))
    assert "<html lang='zh-Hant-TW'>" in page and "怎麼看結果" in page and "不代表已查證" in page
    assert "事實矛盾" in page and "日期不同" in page and "另有 1 項待查證" in page
    assert "href='https://u:p@" not in page  # credentialed links are not linked
    assert "&lt;script&gt;" in page and "secret detail" not in page and "（已停用）" in page
    assert KEY not in page and "AI 金鑰：已設定" in page
    empty = render_status_page({"available": False})
    assert "無法讀取本機資料" in empty and "第一次檢查" in empty


class Store:
    def __init__(self, error=None):
        self.error = error

    def dashboard_snapshot(self):
        if self.error:
            raise self.error
        return {"inbox": [], "sources": [], "last_run": None}


@contextmanager
def server(store):
    instance = start_server(Settings(llm_api_key=KEY), store)
    Thread(target=instance.serve_forever, daemon=True).start()
    try:
        yield instance.server_port
    finally:
        instance.shutdown()
        instance.server_close()


def request(port, path="/", host=None, method="GET"):
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    connection.putrequest(method, path, skip_host=True)
    connection.putheader("Host", host or f"127.0.0.1:{port}")
    connection.endheaders()
    response = connection.getresponse()
    body = response.read().decode("utf-8")
    connection.close()
    return response, body


def test_server_serves_only_loopback_hosts_with_security_headers():
    import sqlite3

    with server(Store()) as port:
        ok, body = request(port)
        assert ok.status == 200 and "稿核 GaoHe" in body and KEY not in body
        assert "default-src 'none'" in ok.getheader("Content-Security-Policy") and ok.getheader("X-Frame-Options") == "DENY"
        assert request(port, host="evil.example")[0].status == 421  # DNS-rebinding guard
        assert request(port, path="/other")[0].status == 404
        assert request(port, method="POST")[0].status == 501
    with server(Store(sqlite3.OperationalError("locked"))) as port:
        response, body = request(port)
        assert response.status == 200 and "無法讀取本機資料" in body and "locked" not in body
