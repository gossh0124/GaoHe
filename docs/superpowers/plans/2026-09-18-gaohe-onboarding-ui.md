# GaoHe Onboarding, Local UI, and Scheduler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** 讓沒有開發背景的 Windows 使用者可以用自己的 API key 完成首次設定、指定要監督的媒體、在網站更新後由低成本輪詢觸發分析，並在本機清楚查看文章、證據與標註。

**Architecture:** 以既有本機 status page 為入口；首次設定是同一個 127.0.0.1 wizard；排程是目前使用者的 Windows Task Scheduler 工作，短時間執行 watch 與 pending analysis 後結束。資料與 key 預設保留在本機，解除安裝只移除工具與工作。

**Tech Stack:** Python 3.11 stdlib http.server/html.escape/webbrowser/subprocess、原生 HTML/CSS、PowerShell 5.1、schtasks.exe、既有 pytest 與 Windows CI。

**Spec:** docs/superpowers/specs/2026-09-18-gaohe-media-monitoring-v0-design.md

## Global Constraints

- 僅支援 Windows-native；不安裝 Windows Service、不要求系統管理員權限、不常駐 Python process。
- setup 與 uninstall 必須使用絕對、可解析的專案路徑；不可依賴目前工作目錄或未解析的環境變數。
- setup 只要求一個主 AI provider/key 與一個媒體來源即可完成最小可用設定；web search 與 Firecrawl 都是 optional。
- key 只寫入本機被 gitignore 的設定位置，不顯示完整值、不寫 log、不回傳到 status page 或 SQLite。
- 監控工作預設每 60 分鐘執行一次；只有新文章或同 URL 內容 hash 變更才進入分析。
- 文章呈現必須 escape HTML 並以文章 revision 的字元 offset 產生標註；重疊 span 要有穩定規則，不可破壞原文。
- 顏色使用低飽和度，且每個顏色都搭配文字標籤、圖例與可辨識的 focus/hover 狀態；證據狀態不只靠顏色表達。
- UI 不顯示媒體總分、真假總判決或信任排名；只顯示 finding type、證據狀態、來源與待確認原因。
- 不在 CI 實際建立或刪除 Task Scheduler 工作；所有 Windows API 透過可注入 runner 測試。

---

## Task 1: Replace the status page with a useful local monitoring view

**Files:**

- Modify: src/gaohe/web.py
- Create: tests/test_web_monitoring.py

**Interfaces:**

    render_status_page(snapshot: Mapping[str, object]) -> str
    render_article(text: str, annotations: Sequence[Finding]) -> str
    render_setup_page(state: Mapping[str, object]) -> str
    start_server(settings: Settings, store: Store,
                 host: str = "127.0.0.1", port: int = 0) -> HTTPServer

**Steps:**

- [ ] Keep the existing local server entry point and add four concise sections: new article inbox, findings, same-topic comparison, and source health.
- [ ] Render each article revision with title, source, publication/update time, original text, and links to evidence; escape all user/provider text before inserting HTML.
- [ ] Convert finding spans to marks using a sorted, non-overlapping interval pass; when spans overlap, render the broader source span once and list both finding labels in its detail panel.
- [ ] Use low-saturation semantic colors: muted coral for factual contradiction, muted amber for material cross-media difference, and muted blue for unsupported inference; show the finding name and evidence status beside every mark.
- [ ] Render pending, retrieval_failed, insufficient_scope, and confirmed/contradicted states as text badges with icons or short labels that remain understandable without color.
- [ ] Show source health as last check time, success/failure state, candidate count, and bounded error text; never show request headers, key values, or raw provider payloads.
- [ ] Keep the page dependency-free and readable at normal Windows browser zoom; add visible focus outlines and sufficient text contrast for marks and controls.
- [ ] Add tests for HTML escaping, no-annotation text, each finding color/label, overlapping spans, evidence-state labels, source errors, and absence of key values in rendered HTML.

**Commit checkpoint:** feat: improve GaoHe local monitoring view

---

## Task 2: Add the first-run browser wizard and safe local configuration writes

**Files:**

- Create: src/gaohe/setup_flow.py
- Modify: src/gaohe/cli.py
- Modify: src/gaohe/config.py
- Modify: .gitignore
- Create: tests/test_setup_flow.py

**Interfaces:**

    SetupState(configured: bool, has_llm_key: bool,
               source_count: int, warnings: tuple[str, ...])
    validate_setup_form(form: Mapping[str, str]) -> list[str]
    write_setup_config(form: Mapping[str, str], env_path: Path) -> None
    run_setup_wizard(settings: Settings, store: Store,
                     open_browser: bool = True) -> None

**Steps:**

- [ ] Add a setup route with plain-language fields for AI provider, model, API key, media name, and RSS/list URL; explain that each downloaded user must enter their own key.
- [ ] Make the minimum submit rule exactly one non-empty AI key and one valid HTTP(S) source URL; allow web search provider and Firecrawl to remain disabled.
- [ ] Validate provider/model/key/source values at the boundary, reject whitespace-only values, and show field-level errors without echoing the secret.
- [ ] Write configuration atomically as UTF-8 without BOM to a local ignored environment file; preserve unrelated existing values and never commit or print the new key.
- [ ] Make setup idempotent: reopening the wizard shows masked readiness state, does not duplicate an existing source, and allows replacing the user’s own key.
- [ ] Add a clear first-run message explaining that no cloud account is shared by the repository and that provider quota, terms, and rate limits belong to the user’s account.
- [ ] Add CLI setup entry point that starts the local wizard and optionally opens the default browser; bind only to loopback.
- [ ] Add tests for required fields, invalid URLs, atomic writes, UTF-8 bytes without BOM, preservation of unrelated variables, masked state, duplicate source prevention, and secret-free error output.

**Commit checkpoint:** feat: add GaoHe first-run wizard

---

## Task 3: Implement user-level Windows Task Scheduler integration

**Files:**

- Create: src/gaohe/scheduler.py
- Create: scripts/run-watch.ps1
- Modify: src/gaohe/cli.py
- Create: tests/test_scheduler.py

**Interfaces:**

    SchedulerRunner.run(args: Sequence[str]) -> CompletedProcess[str]
    install_task(task_name: str, project_dir: Path, python_path: Path,
                 interval_minutes: int, runner: SchedulerRunner) -> None
    remove_task(task_name: str, runner: SchedulerRunner) -> None
    task_status(task_name: str, runner: SchedulerRunner) -> str

    gaohe schedule install
    gaohe schedule status
    gaohe schedule remove

**Steps:**

- [ ] Build schtasks.exe argument lists without shell=True, using the current user and an absolute PowerShell runner path; keep the task name stable so repeated install updates one task.
- [ ] Schedule at the configured interval with a logged-in user context; do not request elevation, store a password, or create a service.
- [ ] Make scripts/run-watch.ps1 resolve its own project directory, invoke the project interpreter, run gaohe watch --once, then gaohe analyze --pending, and exit with a useful code.
- [ ] Ensure one source failure does not prevent later sources or the analysis command from running; preserve counts in the local run record.
- [ ] Add schedule status output that distinguishes installed, not installed, and query failure without exposing command arguments containing secrets.
- [ ] Add tests for install/update/remove/status command arguments, interval validation, absolute path quoting, nonzero command results, and no shell invocation.
- [ ] Add a local manual check instruction for the user to run schedule status after setup; keep actual scheduler mutation out of automated tests.

**Commit checkpoint:** feat: add Windows scheduled monitoring

---

## Task 4: Add recoverable setup and uninstall scripts for ordinary users

**Files:**

- Create: setup.cmd
- Create: uninstall.cmd
- Create: scripts/setup.ps1
- Create: scripts/uninstall.ps1
- Modify: README.md
- Create: tests/test_release_scripts.py

**Interfaces:**

    setup.cmd
    uninstall.cmd
    scripts/setup.ps1
    scripts/uninstall.ps1

**Steps:**

- [ ] Make setup.cmd locate its own directory, invoke scripts/setup.ps1, create or reuse a project-local .venv, install the local package without adding runtime dependencies, and launch the browser wizard.
- [ ] Make setup.ps1 check for a usable Python 3.11 interpreter, report an actionable install link/message when missing, and stop before modifying files if the check fails.
- [ ] Make uninstall.cmd invoke scripts/uninstall.ps1, remove only the GaoHe scheduled task and project-local virtual environment, and leave the user’s environment file and data directory in place by default.
- [ ] Require an explicit typed confirmation before deleting local configuration or SQLite data; show the exact resolved paths before any optional deletion.
- [ ] Use UTF-8-safe PowerShell output and write generated text files with UTF-8 without BOM; never use repository-wide recursive deletion.
- [ ] Add script tests for path resolution, default data preservation, explicit data-delete confirmation, missing Python, repeated setup, and repeated uninstall.
- [ ] Document developer installation from the repository separately from ordinary-user installation from a GitHub Release ZIP; keep the single-file EXE explicitly deferred.

**Commit checkpoint:** docs: add GaoHe Windows setup and uninstall flow

---

## Task 5: Connect release documentation, accessibility checks, and CI-safe smoke coverage

**Files:**

- Modify: README.md
- Modify: .github/workflows/ci.yml only if the existing workflow needs the new tests
- Create: tests/test_user_flow.py

**Steps:**

- [ ] Document the complete ordinary-user path: download ZIP, run setup.cmd, enter personal key and one media source, confirm schedule status, open the local page, and use uninstall.cmd when finished.
- [ ] Document the developer path: clone, create venv, install editable package, run pytest, run gaohe doctor, run watch --once, and open the local page.
- [ ] Add troubleshooting for missing Python, blocked local port, invalid feed URL, source retrieval failure, missing evidence, provider quota, and Firecrawl optional limits.
- [ ] Explain that a page update triggers one article revision and one pending analysis cycle; unchanged content is skipped by content hash.
- [ ] Add a user-flow test with fake providers and a temporary store that verifies setup, source registration, watch, analyze, status rendering, and safe uninstall planning without real Windows mutation.
- [ ] Keep CI on windows-latest with no API secrets, no network-dependent assertions, no Task Scheduler mutation, and no CD job.
- [ ] Run the full pytest suite and git diff --check.

**Commit checkpoint:** docs: complete GaoHe user flow

---

## Plan-level verification gate

- [ ] From a clean temporary copy, run setup.cmd and confirm the wizard requests a provider-neutral key and one media source.
- [ ] Confirm the resulting environment file is ignored, UTF-8 without BOM, and contains no output-visible secret.
- [ ] Add a fixture source, run the scheduled runner manually, and confirm a new article is visible in the inbox.
- [ ] Change the fixture article body and confirm one new revision and one analysis attempt; repeat without changes and confirm no duplicate revision.
- [ ] Confirm one evidence-backed finding renders with a low-saturation mark, text label, evidence status, and source link.
- [ ] Confirm snippet-only, retrieval-failed, and insufficient-scope cases remain visibly non-conclusive.
- [ ] Run uninstall without the optional data confirmation and verify the scheduled task and venv are removed while local data remains.
- [ ] Run the full Windows CI-equivalent pytest command with no external credentials.

## Deliberate non-goals

- No Windows Service or always-running daemon.
- No automatic browser extension or DOM watcher.
- No single-file EXE until the ZIP/setup flow has real-user feedback.
- No remote account, shared API key, telemetry, or hosted database.
