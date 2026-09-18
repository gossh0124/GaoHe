# GaoHe Evidence Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** 在監控核心保存的文章版本上，建立 provider-neutral 的主張抽取、同主題比較、證據檢索與 fail-closed 爭議候選流程；只有滿足證據門檻的候選才進入可視標註。

**Architecture:** AI 只提出主張與候選解釋；規則層決定是否具備可檢驗性與物質性；證據層分開處理搜尋與全文擷取；儲存層保留每一步的來源、狀態與版本。沒有全文證據時，系統保留 pending 或 insufficient_scope，不轉成 false。

**Tech Stack:** Python 3.11、stdlib HTTP/JSON/SQLite、既有 pytest；第一個分析 adapter 可沿用現有 Gemini BYOK 預設，但設定與核心協定不使用 Google-specific 欄位。

**Spec:** docs/superpowers/specs/2026-09-18-gaohe-media-monitoring-v0-design.md

## Global Constraints

- 依賴監控基礎計畫完成後的 ArticleRevision 與 Store；不可另造第二套文章或設定模型。
- 不以關鍵詞直接篩掉文章；關鍵詞只能作為候選提示，不能單獨產生 finding。
- claim extraction、finding candidate、visible finding 三者分離；一般敘述若沒有衝突或重大差異，不得被螢光標註。
- 搜尋結果 snippet、搜尋排名、模型信心分數都不是證據；證據必須有可重取的全文 URL、摘錄、抓取狀態與時間。
- 找不到證據不等於錯誤；來源無法抓取時只允許 pending、retrieval_failed 或 insufficient_scope。
- 約數不可用固定 5% 或 10% 閾值判錯；需保留原文、數值、單位、時間與語意範圍，只有達到 materiality 才升級。
- v0 僅允許三種 visible finding type：factual_contradiction、material_cross_media_difference、unsupported_inference。
- 不評估媒體整體可信度、不給文章總分、不做政治立場分類、不自動產生重要遺漏 finding。
- API 呼叫要可注入 fake provider；CI 不使用真實 API、Firecrawl 或網路。

---

## Task 1: Extend the domain and SQLite schema for claims, evidence, findings, and topics

**Files:**

- Modify: src/gaohe/domain.py
- Modify: src/gaohe/storage.py
- Create: tests/test_analysis_storage.py

**Interfaces:**

    ArticleRevision(id: int, article_id: int, url: str, title: str,
                    text: str, content_hash: str, fetched_at: str)
    Claim(id: int | None, revision_id: int, text: str, start: int, end: int,
          kind: str, materiality: str, extraction_status: str)
    SearchHit(url: str, title: str, snippet: str, source: str,
              published_at: str | None)
    RetrievedPage(url: str, title: str, text: str, retrieved_at: str,
                  status: str, content_hash: str | None)
    Evidence(id: int | None, finding_id: int | None, url: str, title: str,
             excerpt: str, relation: str, status: str, source_kind: str,
             retrieved_at: str | None)
    Finding(id: int | None, revision_id: int, claim_id: int | None,
            finding_type: str, summary: str, start: int, end: int,
            status: str, evidence_status: str, visible: bool)
    TopicGroup(id: int | None, label: str, confidence: str, status: str)

    Store.save_claims(revision_id: int, claims: Sequence[Claim]) -> list[int]
    Store.save_evidence(evidence: Evidence) -> int
    Store.save_finding(finding: Finding) -> int
    Store.link_revision_to_topic(revision_id: int, topic_id: int) -> None
    Store.list_pending_revisions(limit: int = 20) -> list[ArticleRevision]
    Store.list_topic_revisions(topic_id: int) -> list[ArticleRevision]

**Steps:**

- [ ] Add immutable article-revision reads to Store so analysis always uses the exact text and hash captured by monitoring.
- [ ] Create claims, evidence, findings, topics, and topic_articles tables with foreign keys, bounded status values enforced in Python, and uniqueness for one persisted claim span per analysis revision.
- [ ] Store character offsets against the normalized article text and reject spans outside the text length; do not store HTML coordinates in the analysis layer.
- [ ] Persist every evidence retrieval attempt with status and timestamp, including failed retrievals, without storing authorization headers or API keys.
- [ ] Add a migration-safe initialize path that creates missing tables without dropping existing baseline data.
- [ ] Add tests for revision reads, claim spans, evidence status persistence, finding visibility flags, topic links, and repeated initialization.

**Commit checkpoint:** feat: persist GaoHe evidence records

---

## Task 2: Define provider protocols and the first concrete adapters

**Files:**

- Create: src/gaohe/providers.py
- Create: tests/test_providers.py
- Modify: src/gaohe/config.py only if adapter selection needs a validated provider value

**Interfaces:**

    AnalysisProvider.analyze(revision: ArticleRevision,
                             related: Sequence[ArticleRevision]) -> AnalysisResult
    EvidenceSearchProvider.search(query: str, limit: int = 5) -> Sequence[SearchHit]
    PageFetcher.fetch(url: str) -> RetrievedPage

    AnalysisResult(revision_id: int, claims: tuple[Claim, ...],
                   candidates: tuple[FindingCandidate, ...])
    FindingCandidate(claim_id: int | None, finding_type: str, summary: str,
                     start: int, end: int, materiality: str,
                     query: str | None)

**Steps:**

- [ ] Define the protocols and a NullSearchProvider that returns no hits when WEB_SEARCH_PROVIDER is none; keep provider construction small enough that one unsupported provider produces one actionable configuration error.
- [ ] Implement the first live AnalysisProvider using the current Gemini BYOK contract and default model, with provider-neutral input/output types and strict JSON response validation.
- [ ] Implement a direct PageFetcher using the existing HttpTransport and a Firecrawl PageFetcher as an optional fallback selected only when direct retrieval fails and FIRECRAWL_API_KEY is configured.
- [ ] Keep search and scrape separate: EvidenceSearchProvider returns discovery metadata only, while PageFetcher is responsible for fetching page text.
- [ ] Validate every returned URL as HTTP(S), cap response size and excerpt length, normalize provider failures into status values, and never include request headers in raised or printed errors.
- [ ] Make all adapters accept injected transports; test valid JSON, malformed JSON, HTTP failure, timeout, oversized response, invalid URL, absent Firecrawl key, and fallback selection with fake transports.
- [ ] Add a provider matrix test that proves the core pipeline can run with NullSearchProvider and fake AnalysisProvider without network access.

**Commit checkpoint:** feat: add GaoHe provider adapters

---

## Task 3: Add claim extraction and the deterministic materiality gate

**Files:**

- Create: src/gaohe/analysis.py
- Create: tests/test_analysis.py

**Interfaces:**

    extract_claims(revision: ArticleRevision,
                   provider: AnalysisProvider) -> AnalysisResult
    is_material_candidate(claim: Claim, candidate: FindingCandidate,
                          revision: ArticleRevision) -> bool
    allowed_finding_type(value: str) -> bool

**Steps:**

- [ ] Ask the analysis provider for checkable claims and candidate spans, requiring the original claim text and exact character offsets in the response.
- [ ] Reject candidates with invalid spans, empty summaries, unsupported finding types, or no associated claim; store rejected output as non-visible analysis metadata only when it is useful for debugging.
- [ ] Implement the materiality gate for explicit factual contradiction, cross-source difference, and inference beyond stated evidence; ordinary descriptive claims pass through as claims but fail the finding gate.
- [ ] Treat numerical approximations semantically: compare unit, time, entity, and order of magnitude before considering a discrepancy; do not mark near values false solely because they differ.
- [ ] Keep the evidence status independent from highlight color; a candidate can be material and still remain pending because evidence is unavailable.
- [ ] Add tests covering the user example about nearly 500 officials, an explicit number/unit conflict, an attributed statement, a causal inference, a vague opinion, invalid spans, and an unsupported finding type.

**Commit checkpoint:** feat: gate GaoHe finding candidates

---

## Task 4: Retrieve, rank, and validate evidence with fail-closed rules

**Files:**

- Modify: src/gaohe/analysis.py
- Create: tests/test_evidence.py

**Interfaces:**

    retrieve_evidence(candidate: FindingCandidate,
                      search: EvidenceSearchProvider,
                      fetcher: PageFetcher,
                      limit: int = 5) -> list[Evidence]
    resolve_finding(candidate: FindingCandidate,
                    evidence: Sequence[Evidence],
                    related: Sequence[ArticleRevision]) -> Finding
    analyze_revision(revision: ArticleRevision,
                     related: Sequence[ArticleRevision],
                     analysis: AnalysisProvider,
                     search: EvidenceSearchProvider,
                     fetcher: PageFetcher) -> AnalysisResult

**Steps:**

- [ ] Generate one bounded query per material candidate from the claim and source context; do not send the whole article or secrets to a search provider.
- [ ] Deduplicate search hits by canonical URL, fetch only the bounded top results, and retain title, URL, excerpt, source kind, and retrieval status.
- [ ] Mark snippets as discovery-only; a snippet alone can never move a finding to visible.
- [ ] For factual contradiction, require an explicit conflicting full-text passage with a retrievable URL; for cross-media difference, require two article revisions in the same high-confidence topic with materially different checkable claims; for unsupported inference, require evidence that limits or contradicts the inference rather than mere absence of evidence.
- [ ] Map failed retrieval to retrieval_failed or pending and map no useful full text to insufficient_scope; never map either state to false or visible contradiction.
- [ ] Save evidence and findings in one transaction per revision so a partial analysis cannot appear as a complete visible result.
- [ ] Add tests for supported, contradicted, contextual, snippet-only, no-result, timeout, Firecrawl fallback, and mixed-source cases.

**Commit checkpoint:** feat: add fail-closed evidence resolution

---

## Task 5: Group same-topic articles conservatively and compare only verified peers

**Files:**

- Create: src/gaohe/topics.py
- Create: tests/test_topics.py

**Interfaces:**

    group_revision(revision: ArticleRevision,
                   existing: Sequence[ArticleRevision]) -> TopicGroup | None
    compare_topic(revisions: Sequence[ArticleRevision]) -> list[FindingCandidate]

**Steps:**

- [ ] Normalize titles, dates, named-entity-like tokens, and event terms with stdlib tokenization; use a bounded time window and exact/shared anchors as the initial candidate grouping signal.
- [ ] Require multiple independent anchors for high-confidence grouping; place uncertain matches in possible or ungrouped state instead of forcing a comparison.
- [ ] Compare only claims from high-confidence same-topic revisions and retain each media source URL beside the differing claim.
- [ ] Keep comparison asymmetric details visible: publication time, update time if known, source kind, and whether one article has only a missing detail rather than a contradiction.
- [ ] Add deterministic tests for same event/different wording, unrelated articles sharing a person name, updated article revisions, uncertain grouping, and material versus harmless wording differences.

**Commit checkpoint:** feat: add conservative topic comparison

---

## Task 6: Expose pending analysis and document the evidence contract

**Files:**

- Modify: src/gaohe/cli.py
- Modify: README.md
- Create: tests/test_analysis_cli.py

**Interfaces:**

    gaohe analyze --pending [--limit N]

**Steps:**

- [ ] Add an analyze --pending command that loads newly created revisions, groups possible peers, runs the injected/provider-selected pipeline, and prints counts for claims, candidates, visible findings, pending items, and retrieval failures.
- [ ] Ensure the command exits successfully for ordinary no-finding results and partial source retrieval failures; exit nonzero only for invalid configuration or storage failure.
- [ ] Document the three-layer result model: claims are extracted text, candidates are proposed checks, and visible findings require evidence.
- [ ] Document that search and scrape are different operations, Firecrawl is optional, provider quotas and terms apply to each user’s own key, and no key is included in repository data.
- [ ] Add CLI tests using fake providers and a temporary database; verify that a no-evidence article produces no visible finding.
- [ ] Run the full pytest suite and a local fixture smoke test covering one new article through pending analysis.

**Commit checkpoint:** docs: document GaoHe evidence pipeline

---

## Plan-level verification gate

- [ ] Run analysis on the nearly 500 officials fixture and confirm it remains an ordinary claim unless a material conflicting source is supplied.
- [ ] Run a numeric contradiction fixture and confirm the visible finding includes the exact article span, evidence URL, excerpt, evidence status, and finding type.
- [ ] Run a snippet-only and retrieval-failure fixture and confirm both remain non-visible.
- [ ] Run two high-confidence same-topic articles and confirm a material cross-media difference can be stored without producing a media-wide score.
- [ ] Confirm all provider tests run without network and no secret value is present in test output or persisted rows.
- [ ] Run the full test suite and git diff --check.

## Deliberate non-goals

- No automatic whole-article verdict.
- No fixed numerical tolerance that declares approximate reporting false.
- No search-engine scraping implementation in the core; provider adapters remain replaceable.
- No automatic important-omission detection.
