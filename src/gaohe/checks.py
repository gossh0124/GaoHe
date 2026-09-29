"""Check one news article on demand: validate the URL, fetch it, save it, analyze it.

This is what the local page's "查核這篇" box and `gaohe check URL` run. The result is a
job status with a plain zh-TW message, never a verdict on the article: an invalid or
private URL, a page that cannot be read, a provider problem or a budget stop only ever
say what happened and what to do next. The article is saved under the manual source
(單篇查核), so it appears in the inbox like a monitored one but is never polled.
"""

from collections.abc import Mapping
from datetime import datetime
import socket

from .config import Settings
from .domain import ArticleCandidate, CheckOutcome, FetchedArticle, RetrievedPage, article_content_hash
from .errors import PROVIDER_ERROR_MESSAGES, ProviderError
from .safety import Resolver, canonical_url, is_public_http_url, is_source_url
from .storage import Store


INVALID_URL_MESSAGE = "請貼上完整的新聞網址，開頭必須是 http:// 或 https://，中間不能有空白。"
PRIVATE_URL_MESSAGE = "這個網址指向本機或內部網路，GaoHe 只查核公開網站上的新聞。"
FETCH_FAILED_MESSAGE = (
    "無法讀取這個網頁（可能需要登入、被網站阻擋或暫時連不上）。這不代表文章有問題；請稍後再試，或改貼另一個網址。"
)
SETUP_MISSING_MESSAGE = "尚未完成 AI 設定（服務、模型或金鑰）。文章已保存，請先開啟設定精靈，完成後再查核一次。"
SAVED_FOR_RETRY = "文章已保存，修正後可以再查核一次。"
FAILED_MESSAGE = "這次分析沒有完成，GaoHe 會在之後自動重試；這不代表文章有問題。"
BUDGET_MESSAGE = "已達今日 AI 呼叫上限，這篇文章已保存，會在之後自動分析。"
DEFERRED_MESSAGE = "AI 服務暫時無法使用，這篇文章已保存，會在之後自動分析。"
RUNNING_MESSAGE = "這篇文章正在由另一個工作分析，請稍後重新整理頁面。"
QUEUED_MESSAGE = "這篇文章已排入分析佇列，稍後會自動分析。"
_MAX_TITLE_CHARS = 500


def _completed_message(visible: int, pending: int, *, unchanged: bool) -> str:
    parts = ["這篇文章內容沒有變更，以下是先前的查核結果。" if unchanged else "查核完成。"]
    if visible:
        parts.append(f"有 {visible} 處形成標註，請在文章頁查看證據與判讀理由。")
    else:
        parts.append("目前沒有形成標註；這不代表內容已被證實，只代表沒有找到足以標註的證據。")
    if pending:
        parts.append(f"另有 {pending} 項候選問題待補查。")
    return "".join(parts)


def _completed(store: Store, revision_id: int, article_id: int | None, *, unchanged: bool) -> CheckOutcome:
    detail = store.article_detail(revision_id) or {}
    visible = len(detail.get("annotations") or ())
    pending = int(detail.get("pending_findings") or 0)
    return CheckOutcome("completed", _completed_message(visible, pending, unchanged=unchanged), revision_id, article_id)


def _readable(page: object, resolver: Resolver) -> bool:
    """A fetched page counts only when it has text and did not end up on a private address."""
    return (
        isinstance(page, RetrievedPage)
        and page.status == "retrieved"
        and isinstance(page.text, str)
        and bool(page.text.strip())
        and is_public_http_url(page.url, resolver=resolver)
    )


def _outcome(store: Store, summary: Mapping[str, object], revision_id: int, article_id: int | None) -> CheckOutcome:
    from .pipeline import BUDGET_SKIP_REASON

    if summary.get("stopped"):
        code = str(summary.get("stop_code") or "")
        message = PROVIDER_ERROR_MESSAGES.get(code, PROVIDER_ERROR_MESSAGES["unavailable"])
        return CheckOutcome("failed", message + SAVED_FOR_RETRY, revision_id, article_id)
    if summary.get("analyzed"):
        return _completed(store, revision_id, article_id, unchanged=False)
    if summary.get("failed"):
        return CheckOutcome("failed", FAILED_MESSAGE, revision_id, article_id)
    status = store.analysis_status(revision_id) or {}
    if summary.get("skipped"):
        message = BUDGET_MESSAGE if status.get("last_error") == BUDGET_SKIP_REASON else DEFERRED_MESSAGE
        return CheckOutcome("skipped", message, revision_id, article_id)
    if status.get("status") == "completed":
        return _completed(store, revision_id, article_id, unchanged=False)
    message = RUNNING_MESSAGE if status.get("status") == "running" else QUEUED_MESSAGE
    return CheckOutcome("skipped", message, revision_id, article_id)


def check_article_url(
    settings: Settings,
    store: Store,
    url: str,
    *,
    transport=None,
    analysis=None,
    search=None,
    fetcher=None,
    assessor=None,
    now: datetime | None = None,
    resolver: Resolver | None = None,
) -> CheckOutcome:
    """Check one user-supplied article URL and return what happened (never an article verdict).

    Providers that are not injected are built from settings (gaohe.providers is imported
    only then). Checking the same URL again when its text is unchanged and its analysis
    completed returns the existing revision without any new AI call.
    """
    from . import pipeline, providers

    resolve = resolver or socket.getaddrinfo
    moment = now if now is not None else datetime.now().astimezone()
    at = pipeline.utc_iso(moment)
    requested = url.strip() if isinstance(url, str) else ""
    if not is_source_url(requested):
        return CheckOutcome("invalid_url", INVALID_URL_MESSAGE)
    if not is_public_http_url(requested, resolver=resolve):
        return CheckOutcome("invalid_url", PRIVATE_URL_MESSAGE)
    article_url = canonical_url(requested) or requested

    fetcher = fetcher if fetcher is not None else providers.DirectPageFetcher(transport)
    try:
        page = fetcher.fetch(article_url)
    except Exception:
        page = None
    if not _readable(page, resolve):
        return CheckOutcome("fetch_failed", FETCH_FAILED_MESSAGE)

    title = " ".join(page.title.split())[:_MAX_TITLE_CHARS] if isinstance(page.title, str) else ""
    title = title or article_url
    metadata = store.latest_article_metadata(article_url) or {"checked_via": "manual"}
    candidate = ArticleCandidate(store.ensure_manual_source(), article_url, title, None, at, metadata)
    revision_id, created = store.save_fetched_article(
        FetchedArticle(candidate, page.text, at, article_content_hash(title, page.text))
    )
    detail = store.article_detail(revision_id) or {}
    article_id = detail.get("article_id")
    status = store.analysis_status(revision_id) or {}
    if status.get("status") == "completed":
        return _completed(store, revision_id, article_id, unchanged=not created)

    try:
        if None in (analysis, search, assessor) and settings.validate():
            return CheckOutcome("failed", SETUP_MISSING_MESSAGE, revision_id, article_id)
        analysis = analysis if analysis is not None else providers.build_analysis_provider(settings)
        assessor = assessor if assessor is not None else providers.build_evidence_assessor(settings)
        search = search if search is not None else providers.build_search_provider(settings)
    except ProviderError as error:
        return CheckOutcome("failed", error.user_message + SAVED_FOR_RETRY, revision_id, article_id)
    except ValueError:
        return CheckOutcome("failed", PROVIDER_ERROR_MESSAGES["config"] + SAVED_FOR_RETRY, revision_id, article_id)

    if status.get("status") == "failed":
        store.requeue_failed_analyses(at, [revision_id])  # the user explicitly asked for this one again
    summary = pipeline.run_pending_analysis(
        store, analysis, search, fetcher, 1,
        assessor=assessor, now=moment, daily_llm_call_limit=settings.daily_llm_call_limit, revision_ids=[revision_id],
    )
    return _outcome(store, summary, revision_id, article_id)
