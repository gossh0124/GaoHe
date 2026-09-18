# GaoHe Monitoring Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** 將目前的 Windows-native baseline 擴充成可低成本輪詢、只處理新內容或內容變更、並可在本機 SQLite 中保存文章版本的監控核心。

**Architecture:** 設定層保持 provider-neutral；來源解析、HTTP 抓取、SQLite 儲存、一次性監控執行彼此分離。監控只負責發現與保存，不在本計畫中呼叫 AI 或產生爭議判斷。

**Tech Stack:** Python 3.11、stdlib（urllib、sqlite3、xml.etree、html.parser、dataclasses）、既有 pytest、Windows PowerShell／Task Scheduler 之外的純 Python 核心。

**Spec:** docs/superpowers/specs/2026-09-18-gaohe-media-monitoring-v0-design.md

## Global Constraints

- 保留現有 Windows-native CLI、status page、pytest、windows-latest CI；CD 仍不建立。
- 不新增第三方套件；設定檔與資料檔均以 UTF-8 無 BOM 寫入。
- 不把 API key 寫入 log、SQLite、文章內容或測試輸出；doctor 只能顯示 provider 與已設定／未設定。
- 不用關鍵詞硬篩選文章；來源回傳的新 metadata 先進入候選流程，再由後續分析計畫決定是否值得深入檢驗。
- watch 一次執行必須可重複執行：同一 URL 與相同內容 hash 不新增 revision，也不重複深度處理。
- 網路、解析、SQLite 任一步失敗都要保留可診斷的狀態，但不可因單一來源失敗而中止其他來源。
- 本計畫不實作 AI adapter、搜尋、Firecrawl、爭議標註、排程安裝或發布包；那些工作在後續兩份計畫。
- 每個非平凡 parser、hash、去重與失敗分支至少有一個可執行 pytest 檢查。

---

## Task 1: Replace legacy provider-specific settings with safe provider-neutral settings

**Files:**

- Modify: src/gaohe/config.py
- Modify: src/gaohe/cli.py
- Modify: .env.example
- Modify: tests/test_config.py
- Modify: tests/test_cli.py

**Interfaces:**

    Settings.llm_provider: str
    Settings.llm_model: str
    Settings.llm_api_key: str
    Settings.web_search_provider: str
    Settings.firecrawl_api_key: str
    Settings.data_dir: Path
    Settings.poll_interval_minutes: int
    Settings.database_path: Path
    Settings.has_llm_key: bool
    Settings.has_firecrawl_key: bool

    load_settings(environ: Mapping[str, str] | None = None) -> Settings
    Settings.validate() -> list[str]

**Steps:**

- [ ] Read the existing Settings construction and CLI doctor output, then replace the Google/Gemini field names with the provider-neutral fields above while keeping an explicit compatibility migration for old local environments.
- [ ] Set conservative defaults: llm provider unset, model unset, web search provider set to none, Firecrawl unset, poll interval 60 minutes, and data directory under the current user profile data location.
- [ ] Parse integer settings with a clear validation error for zero, negative, or non-integer poll intervals; do not silently coerce malformed values.
- [ ] Add database_path as a derived path below data_dir and ensure the path is created only by the storage initializer, not during import.
- [ ] Update doctor to report configuration readiness without values or key fragments; include the selected provider names and missing required setup items.
- [ ] Update .env.example and tests to use generic names such as LLM_PROVIDER, LLM_MODEL, LLM_API_KEY, WEB_SEARCH_PROVIDER, and FIRECRAWL_API_KEY.
- [ ] Run the focused config and CLI tests and assert that legacy secret names never appear in doctor output.

**Commit checkpoint:** feat: add provider-neutral GaoHe settings

---

## Task 2: Add the article, source, run, and revision domain model plus SQLite store

**Files:**

- Create: src/gaohe/domain.py
- Create: src/gaohe/storage.py
- Create: tests/test_storage.py

**Interfaces:**

    Source(id: int | None, name: str, feed_url: str, article_url: str | None,
           enabled: bool = True)
    ArticleCandidate(source_id: int, url: str, title: str, published_at: str | None,
                     discovered_at: str, metadata: dict[str, str])
    FetchedArticle(candidate: ArticleCandidate, text: str, fetched_at: str,
                   content_hash: str, fetch_status: str = "ok")
    RunSummary(started_at: str, finished_at: str | None, sources_checked: int,
               candidates_seen: int, revisions_created: int, failures: int)

    Store(path: Path)
    Store.initialize() -> None
    Store.add_source(source: Source) -> int
    Store.list_sources(enabled_only: bool = False) -> list[Source]
    Store.save_fetched_article(article: FetchedArticle) -> tuple[int, bool]
    Store.record_source_check(source_id: int, checked_at: str, status: str,
                              candidates_seen: int, error: str | None) -> None
    Store.record_run(summary: RunSummary) -> int
    Store.latest_content_hash(url: str) -> str | None

**Steps:**

- [ ] Define dataclasses with plain serializable fields and explicit status strings; keep article text separate from metadata so later analysis can reference an immutable revision.
- [ ] Create SQLite tables sources, source_checks, articles, article_revisions, and runs with foreign keys, unique source feed URLs, unique article URLs, and unique article content hashes per URL.
- [ ] Use UTC ISO-8601 strings for persisted timestamps and SHA-256 for content_hash; expose the hash input as normalized article text plus title so identical content is deterministic.
- [ ] Implement Store.initialize with idempotent schema creation and a connection context that commits successful writes and rolls back failed writes.
- [ ] Make save_fetched_article return the revision id and a created flag; unchanged content returns the existing current revision without inserting a duplicate.
- [ ] Record source failures as source_checks rows and keep the error text bounded and free of request headers or secret values.
- [ ] Add tests for schema creation, source CRUD, first revision, unchanged duplicate, changed revision, foreign-key behavior, and rollback after a failed write.
- [ ] Use a temporary database path in tests and close every connection before the test exits.

**Commit checkpoint:** feat: add SQLite monitoring store

---

## Task 3: Implement source discovery and article extraction with stdlib-only parsers

**Files:**

- Create: src/gaohe/sources.py
- Create: tests/test_sources.py

**Interfaces:**

    HttpResponse(status: int, url: str, headers: Mapping[str, str], body: bytes)
    HttpTransport.fetch(url: str, timeout_seconds: float = 20.0) -> HttpResponse
    UrllibTransport.fetch(url: str, timeout_seconds: float = 20.0) -> HttpResponse
    parse_feed(body: bytes, source_url: str) -> list[ArticleCandidate]
    parse_sitemap(body: bytes, source_url: str) -> list[ArticleCandidate]
    parse_html_list(body: bytes, source_url: str) -> list[ArticleCandidate]
    extract_article_text(body: bytes, content_type: str | None = None) -> str

**Steps:**

- [ ] Implement UrllibTransport with a bounded timeout, a descriptive User-Agent, UTF-8/declared-charset decoding, and response objects that preserve status and final URL without preserving authorization headers.
- [ ] Parse RSS and Atom title, link, publication time, and source metadata with XML namespaces handled explicitly; ignore entries without an absolute HTTP(S) article URL.
- [ ] Parse sitemap url/lastmod entries and simple HTML lists using stdlib HTMLParser; preserve discovery metadata without assuming a media-specific DOM.
- [ ] Extract visible article text from paragraphs, headings, list items, and article/main containers while dropping script, style, navigation, and form content; normalize whitespace deterministically.
- [ ] Enforce bounded response bytes and candidate count so a malformed or unexpectedly large page cannot consume unbounded memory.
- [ ] Return empty candidates for unsupported or invalid payloads and let the monitor record the source failure/status rather than raising through the whole run.
- [ ] Add fixture-based tests for RSS, Atom, sitemap, HTML list, charset decoding, relative links, malformed XML, text cleanup, byte limits, and candidate limits.

**Commit checkpoint:** feat: add stdlib source discovery

---

## Task 4: Build the idempotent one-shot monitor and CLI command

**Files:**

- Create: src/gaohe/monitor.py
- Modify: src/gaohe/cli.py
- Modify: src/gaohe/__main__.py only if command dispatch requires it
- Create: tests/test_monitor.py

**Interfaces:**

    watch_once(settings: Settings, store: Store,
               transport: HttpTransport,
               now: datetime | None = None) -> RunSummary

    gaohe watch --once

**Steps:**

- [ ] Load enabled sources, fetch each source feed/list, parse candidates, fetch each new or potentially changed article, and save only new revisions.
- [ ] Compare the candidate URL with the stored latest hash; skip article-body fetch when the source supplies a trustworthy unchanged lastmod/etag value, otherwise fetch and hash the body.
- [ ] Keep source processing independent: catch transport, parsing, and article extraction failures per source or article, record the failure, and continue.
- [ ] Persist one RunSummary row even when every source fails; return a nonzero CLI exit code only for invalid configuration or database initialization failure, not for a partial source failure.
- [ ] Make watch --once print a compact UTF-8-safe summary containing counts only: checked, candidates, revisions, failures.
- [ ] Inject transport and clock in tests; never access the network from pytest.
- [ ] Add tests for no sources, new article, unchanged article, changed article, mixed source success/failure, malformed feed, and repeated invocation.

**Commit checkpoint:** feat: add one-shot GaoHe monitor

---

## Task 5: Add source management commands and document the monitoring contract

**Files:**

- Modify: src/gaohe/cli.py
- Modify: README.md
- Create: tests/test_sources_cli.py

**Interfaces:**

    gaohe source add --name NAME --feed-url URL [--article-url URL]
    gaohe source list
    gaohe source disable --id ID
    gaohe source enable --id ID

**Steps:**

- [ ] Add source add/list/enable/disable commands using the same Settings and Store path as watch --once.
- [ ] Validate only HTTP(S) source URLs and non-empty names at the CLI boundary; show a concise actionable error for invalid input.
- [ ] Document the first usable local path: configure a provider-neutral key later, add one RSS/feed source, run watch --once, and inspect the local status page.
- [ ] State that monitoring is metadata-first, content changes create revisions, retrieval failures are not factual findings, and all data remains local by default.
- [ ] Add command tests with a temporary data directory and assert that list output never includes secrets.
- [ ] Run the complete existing and new pytest suite, then run a manual temporary-directory smoke test using a local fixture transport.

**Commit checkpoint:** docs: document GaoHe monitoring foundation

---

## Plan-level verification gate

- [ ] Create a temporary data directory and initialize an empty store.
- [ ] Add one fixture source and run watch --once with a fake transport.
- [ ] Verify one article revision is created on first discovery, zero revisions on the second unchanged run, and one new revision after changing article text.
- [ ] Verify a failing second source is recorded while the first source still succeeds.
- [ ] Run the full test suite and git diff --check.
- [ ] Confirm no key value, authorization header, or response body is printed by tests or CLI summaries.

## Deliberate non-goals

- No background Windows service.
- No scheduler installation.
- No AI analysis, web search, Firecrawl, topic grouping, finding severity, or annotation rendering.
- No single-file executable packaging.
