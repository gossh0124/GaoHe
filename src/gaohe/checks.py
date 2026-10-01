"""Check one news article on demand (`gaohe check URL`): validate, fetch, save, analyze.

The result is a plain zh-TW message about what happened, never a verdict on the article.
The article is saved under the manual source (單篇查核): it appears on the local page like a
monitored one but is never polled.
"""

import socket

from .config import Settings
from .domain import ArticleCandidate, CheckOutcome, FetchedArticle, article_content_hash
from .errors import PROVIDER_ERROR_MESSAGES, ProviderError
from .pipeline import run_pending_analysis, utc_now
from .safety import Resolver, canonical_url, is_public_http_url, is_source_url
from .storage import Store


INVALID_URL = "請貼上完整的新聞網址，開頭必須是 http:// 或 https://，中間不能有空白。"
PRIVATE_URL = "這個網址指向本機或內部網路，稿核只查核公開網站上的新聞。"
FETCH_FAILED = "無法讀取這個網頁（可能需要登入、被網站阻擋或暫時連不上）。這不代表文章有問題；請稍後再試。"
SETUP_MISSING = "尚未完成 AI 設定。文章已保存，請先執行設定精靈，完成後再查核一次。"
FAILED = "這次分析沒有完成，文章已保存，之後排程會自動重試；這不代表文章有問題。"


def _completed_message(visible: int, pending: int, unchanged: bool) -> str:
    parts = ["這篇文章內容沒有變更，以下是先前的查核結果。" if unchanged else "查核完成。"]
    if visible:
        parts.append(f"有 {visible} 處形成標註，請在本機頁面查看證據與判讀理由。")
    else:
        parts.append("目前沒有形成標註；這不代表內容已被證實，只代表沒有找到足以標註的證據。")
    if pending:
        parts.append(f"另有 {pending} 項候選問題待查證。")
    return "".join(parts)


def check_article_url(
    settings: Settings,
    store: Store,
    url: str,
    *,
    analysis=None,
    search=None,
    fetcher=None,
    assessor=None,
    resolver: Resolver | None = None,
) -> CheckOutcome:
    """Providers that are not injected are built from settings."""
    from . import providers

    resolve = resolver or socket.getaddrinfo
    requested = url.strip() if isinstance(url, str) else ""
    if not is_source_url(requested):
        return CheckOutcome("invalid_url", INVALID_URL)
    if not is_public_http_url(requested, resolver=resolve):
        return CheckOutcome("invalid_url", PRIVATE_URL)
    article_url = canonical_url(requested) or requested

    fetcher = fetcher if fetcher is not None else providers.DirectPageFetcher()
    try:
        page = fetcher.fetch(article_url)
    except Exception:
        page = None
    if page is None or page.status != "retrieved" or not page.text.strip() or not is_public_http_url(page.url, resolver=resolve):
        return CheckOutcome("fetch_failed", FETCH_FAILED)

    at = utc_now()
    title = " ".join(page.title.split())[:500] or article_url
    candidate = ArticleCandidate(store.ensure_manual_source(), article_url, title, None, at, {"checked_via": "manual"})
    revision_id, created = store.save_fetched_article(FetchedArticle(candidate, page.text, at, article_content_hash(title, page.text)))
    status = store.analysis_status(revision_id) or {}
    if status.get("status") == "completed":
        return CheckOutcome("completed", _completed_message(*store.finding_counts(revision_id), unchanged=not created), revision_id)

    if None in (analysis, search, assessor) and settings.validate():
        return CheckOutcome("failed", SETUP_MISSING, revision_id)
    try:
        analysis = analysis or providers.build_analysis_provider(settings)
        assessor = assessor or providers.build_evidence_assessor(settings)
        search = search or providers.build_search_provider(settings)
    except ProviderError as error:
        return CheckOutcome("failed", error.user_message, revision_id)

    store.reset_analysis(revision_id)  # the user explicitly asked for this one again
    summary = run_pending_analysis(store, analysis, search, fetcher, assessor, 1, revision_ids=[revision_id])
    if summary["stopped"]:
        message = PROVIDER_ERROR_MESSAGES.get(str(summary["stop_code"]), PROVIDER_ERROR_MESSAGES["unavailable"])
        return CheckOutcome("failed", message + "文章已保存，修正後可以再查核一次。", revision_id)
    if not summary["analyzed"]:
        return CheckOutcome("failed", FAILED, revision_id)
    return CheckOutcome("completed", _completed_message(*store.finding_counts(revision_id), unchanged=False), revision_id)
