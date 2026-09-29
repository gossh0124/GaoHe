# GaoHe Optimization Round (2026-09-29)

> 本文件記錄一輪已完成的優化，而不是待執行的計畫。每個項目依「問題 → 變更 → 測試證據 → 剩餘工作」整理，供下一輪規劃與人工驗收參考。

**Goal:** 把 v0 的監測、分析與本機介面從「單次可跑」提升到「可長期排程執行」：證據必須經過評估才可能形成標註、單篇失敗不拖垮整個佇列、AI 呼叫有每日上限與帳本、所有網址與錯誤訊息都經過同一套遮蔽規則。

**Architecture:** `gaohe.safety` 是唯一的網址驗證與遮蔽入口；`gaohe.storage` 以編號 migration 管理 schema，並提供分析工作狀態機、AI 呼叫帳本、同題分組、人工確認與唯讀 read model；`gaohe.analysis` 負責「抽取 → 取回 → 評估 → 判定」的單篇步驟，`gaohe.policy` 是唯一的可見性規則；新的 `gaohe.pipeline` 把這些步驟組成有預算、有重試、逐篇隔離的佇列；`gaohe.cli` 與 `gaohe.web` 只讀取 read model。

**Tech Stack:** Python 3.11 標準函式庫（sqlite3、http.server、urllib、json、datetime）、既有 pytest 與 ruff；runtime 沒有第三方依賴。

**Spec:** docs/superpowers/specs/2026-09-18-gaohe-media-monitoring-v0-design.md（第 2、4、5、6、9、10、12 節）

## Global Constraints

- 搜尋摘要永遠不是證據；找不到證據、取回失敗、來源失敗都不能變成錯誤判定（規格 2.3、2.4）。
- 不產生整篇文章判定或媒體總分（規格 2.5）。
- API key 與其他憑證不得進入 SQLite、log、錯誤字串、stdout 或 HTML；遮蔽一律經過 `gaohe.safety`（規格 9）。
- runtime 只用標準函式庫；目標平台是 Windows。
- 所有新行為都有 deterministic 測試：時間、sleep、transport 與 provider 都以注入方式替換，CI 不連網、不呼叫真實 AI。
- 本輪沒有以真實 Gemini key、真實新聞來源或真人使用者做過驗收；以下「測試證據」全部來自 fake provider 與 fixture。

---

## 1. 共用安全與領域型別（safety / domain）

- **問題：** 網址驗證、canonical URL 與遮蔽邏輯分散在多個模組，修一處漏一處；Google REST API 以 `?key=` 傳遞金鑰，舊規則只認得 `api_key` 類名稱。
- **變更：** 新增 `gaohe.safety`（`is_http_url`、`is_credential_free_http_url`、`is_source_url`、`canonical_url`、`redact_text`、`redact_url`、`safe_error`），並遮蔽裸 `key=` 參數與 `AIza…` 形狀的金鑰字串。`gaohe.domain` 集中 `ANALYSIS_STATUSES`、`REVIEW_STATUSES`、`ASSESSMENT_RELATIONS`、`EvidenceAssessment` 等共用型別。
- **測試證據：** `tests/test_safety.py`；其他模組的遮蔽測試改為依賴同一組 helper。
- **剩餘工作：** 遮蔽是樣式比對；若 provider 在錯誤訊息中原樣回傳一段不符合任何樣式的金鑰，仍需依賴各 adapter 的固定錯誤訊息（Gemini adapter 已如此處理）。

## 2. 儲存層（storage）

- **問題：** schema 以臨時 bootstrap 建立，無法可靠升級；分析只有「完成」一種狀態，失敗或中斷的文章不是卡住就是被重複分析；沒有人工確認、AI 呼叫帳本或條件式抓取狀態。
- **變更：** `PRAGMA user_version` 追蹤的編號 migration（目前 `SCHEMA_VERSION = 4`），每步獨立交易並在寫鎖下重新檢查；WAL、`busy_timeout`、寫入使用 `BEGIN IMMEDIATE`。`revision_analysis` 改為工作表（pending／running／completed／failed／skipped、attempts、last_error、provider、model、prompt_version），`list_pending_revisions` 只回傳各文章的目前版本並接手逾時的 running 工作。新增 `llm_calls` 帳本、`assign_topic`／`set_topic_status`、`review_finding`、文章與來源的條件式抓取欄位，以及 `list_findings`、`dashboard_snapshot` read model（網址一律遮蔽）。本套件另加兩個小型唯讀介面：`list_recent_revisions(current_only=True)` 與 `topic_status(topic_id)`。
- **測試證據：** `tests/test_storage_migrations.py`、`test_storage_jobs.py`、`test_storage_fetch_state.py`、`test_storage_read_models.py`、`test_storage_topics_review.py`；`current_only` 與 `topic_status` 由 `tests/test_pipeline.py` 覆蓋。
- **剩餘工作：** 尚無列出已駁回同題群組的 read model（`gaohe topic list` 因此只顯示未駁回的群組）；尚無資料保留期限或清理命令。

## 3. 分析、provider 與可見性規則（analysis / providers / policy）

- **問題：** 模型回傳的字元位置常常算錯，一條錯誤主張就讓整篇分析失敗；取回的頁面沒有經過判讀就直接被當成「矛盾」；可見性規則散落在多處。
- **變更：** 主張改以原文引文定位（`locate_quote`，先精確比對，再忽略空白；重複出現時需指定第幾次），無法定位的主張被捨棄並計入 `rejected_claims`。Gemini 使用 structured output、temperature 0、60 秒逾時，429／5xx 最多重試兩次，公開錯誤訊息不含金鑰或原始回應。新增 `EvidenceAssessor` 協定、`NullEvidenceAssessor` 與 `GeminiEvidenceAssessor`：每一頁證據必須有已知關係、判讀理由與可在頁面中找到的引文，否則維持「未評估」。`gaohe.policy.is_visible` 成為唯一規則：只有經過評估的全文證據可以讓發現可見，任何「支持」都讓事實矛盾維持待人工判讀。
- **測試證據：** `tests/test_quote_anchoring.py`、`test_gemini.py`、`test_assessment.py`、`test_policy.py`、`test_evidence.py`、`test_analysis.py`、`test_providers.py`。
- **剩餘工作：** 搜尋 provider 只有 `none`，因此正式 CLI 目前取不到外部網頁證據；Firecrawl 仍未接入 runtime；prompt 尚未以真實新聞樣本調校。

## 4. 同題分組（topics）

- **問題：** 中文段落沒有空格，整段被當成一個詞，兩家媒體報導同一事件幾乎不會被分在一起；數字比較不認得「千、萬、億」與量詞。
- **變更：** 正規化（NFKC、casefold、臺→台等異體字）後以 CJK 二元組比對，機構與地名以「院、部、市、公司」等後綴錨定；數字解析中文單位與量詞，約略、範圍、序數與日期一律不比較（規格 7.4）；相反詞加入中文詞對並排除否定語境。所有比對都有上限，長文仍在一秒內完成。
- **測試證據：** `tests/test_topics_cjk.py`、`tests/test_topics.py`。
- **剩餘工作：** 規則式分組的召回率與誤判率尚未用真實新聞量測；需要小規模實際監測的資料再調整門檻。

## 5. 本機介面（web）

- **問題：** 頁面以英文為主，沒有讀取實際資料庫的 read model，也缺少 DNS rebinding 等本機防護。
- **變更：** `start_server` 每次 GET / 都讀取 `store.dashboard_snapshot()`，以繁體中文呈現新文章收件匣、發現事項、同題對照、來源狀態、分析佇列、本機設定與說明；三種標註使用低飽和度底色加底線，證據狀態以文字標示；只有可見且未被駁回的發現會在原文上色。伺服器檢查 Host、只接受 GET／HEAD，並加上 CSP、nosniff、no-referrer、no-store。
- **測試證據：** `tests/test_web_dashboard.py`、`test_web_server.py`、`test_web_monitoring.py`，以及本輪新增的 `tests/test_end_to_end.py`（實際 HTTP GET /）。
- **剩餘工作：** `start_server` 使用 IPv4 socket，`gaohe serve --host ::1` 雖通過 CLI 檢查但目前無法綁定；頁面尚未顯示 `DAILY_LLM_CALL_LIMIT` 與今日呼叫量；頁面仍是唯讀，人工確認需使用 CLI。

## 6. 分析佇列、CLI、設定與文件（pipeline / cli / config / docs）

- **問題：** `cli.run_pending_analysis` 一次處理所有文章，任何一篇丟出例外就中斷整個排程，失敗的文章沒有狀態也不會重試；AI 呼叫沒有上限也沒有紀錄；同題證據沒有判讀理由，改用新可見性規則後同題發現永遠無法可見（基線中唯一失敗的測試）；同題 context 會把舊版本文章當成對照；CLI 無法查看狀態、列出發現或做人工確認；`serve --host` 可綁定任何介面。
- **變更：**
  - 新增 `gaohe.pipeline.run_pending_analysis(store, analysis, search, fetcher, limit, *, assessor=None, now=None, daily_llm_call_limit=None, max_attempts=3)`，`gaohe.cli` 轉出同名函式。每篇文章先 `mark_analysis_running`，任何步驟的例外只把該篇標為 failed（錯誤訊息為例外類型加上 `safe_error` 後的內容），接著處理下一篇；attempts 與逾時接手沿用 store 的狀態機。
  - 預算以本地日曆日計算（注入的 `now` 自帶時區，未注入時使用電腦本地時間；台灣即 UTC+8 午夜），以 UTC ISO 查詢帳本。開始處理某篇前若已達上限，該篇與其後所有已列出的文章都標為 skipped 並停止；若在某篇的證據評估前達上限，該篇標為 skipped，不消耗 attempt，也不保存半套結果。
  - 分析與評估 provider 以小型 proxy 包裝：每次呼叫都寫入帳本（purpose analysis／assessment、status ok／failed、provider 與 model、輸入與輸出字數估計）。`assess_evidence` 會吞掉評估器的例外，因此 proxy 自己的例外（預算、帳本寫入失敗）另外保存並在評估結束後重新拋出，不會被默默當成「未評估」。`NullEvidenceAssessor` 不算 AI 呼叫。
  - 同題 context 只取各文章目前版本；以「peer 為外層迴圈」分塊計算分組，讓 `gaohe.topics` 的 64 筆文字快取不被洗掉（以本機合成的 20 篇待分析 × 100 篇 peer 量測，分組時間由約 3.5 秒降到約 1.1 秒）。每個 high 或 possible 的 peer 都呼叫 `assign_topic` 保存；只有未被人工駁回的 high peer 會作為 related。
  - 同題差異的 peer 內文（遮蔽後）作為 `related_article` 證據，交給同一個評估器判斷；沒有評估器時一律待補查。比對 peer 網址時兩邊都使用遮蔽後的形式，避免網址帶 token 時永遠無法成立。
  - `save_analysis` 記錄 provider、model、`ANALYSIS_PROMPT_VERSION` 與完成時間。摘要新增 `rejected_claims`、`analyzed`、`failed`、`skipped`，既有鍵的意義不變。
  - CLI：`analyze --pending` 接上 `build_evidence_assessor` 與 `DAILY_LLM_CALL_LIMIT` 並輸出所有摘要鍵；新增 `status`、`finding list [--all] [--limit N]`、`finding review --id N --status confirmed|dismissed [--note]`、`topic list`、`topic review --id N --status active|dismissed`；找不到編號時以狀態 2 結束；`serve` 只接受 `127.0.0.1`、`localhost`、`::1`，埠號被佔用時顯示固定錯誤訊息而非 traceback。所有網址經過 `redact_url`，自由文字經過 `redact_text`。
  - 設定新增 `DAILY_LLM_CALL_LIMIT`（預設 200、正整數，錯誤時與 `POLL_INTERVAL_MINUTES` 相同地拋出 ValueError），並更新 `.env.example`；版本升為 0.2.0。
  - README 更新目前狀態、本機命令、監測契約（證據評估、可見門檻、重試、每日上限）與疑難排解（分析失敗、因預算略過、`gaohe.db-wal` 屬正常）。
- **測試證據：**
  - `tests/test_pipeline.py`：單篇失敗隔離、attempts 與 `max_attempts`、逾時 running 接手與未逾時者不動、並行完成時不重做、分組例外只影響該篇、peer 外層迴圈的呼叫順序、預算在開始前與評估前達到、本地午夜邊界（UTC 同日但台灣前一天的呼叫不計入）、帳本內容、帳本寫入失敗、Null 評估器不計次、同題分組保存（high／possible）、人工駁回的組合不作為 context、舊版本不作為 peer、同題證據遮蔽且仍可成立、`rejected_claims` 加總。
  - `tests/test_analysis_cli.py`：原本失敗的同題測試改為注入回傳 contradicts 的假評估器並確認可見；新增沒有評估器時同題候選只保存為待補查；CLI 確實傳入評估器與每日上限。
  - `tests/test_cli.py`：`status`、`finding list／review`、`topic list／review` 的輸出、遮蔽、找不到編號與非法 `--limit` 的狀態碼，`serve` 拒絕非本機位址、接受三種本機位址與埠號被佔用；版本與 `pyproject.toml` 一致。
  - `tests/test_config.py`：`DAILY_LLM_CALL_LIMIT` 預設、覆寫、非法值與 `.env.example` 可直接載入。
  - `tests/test_end_to_end.py`：真實 Store、兩個不同主機的中文 RSS、fake transport → `watch_once` → 使用 fake request 的 `GeminiAnalysisProvider`／`GeminiEvidenceAssessor` → `run_pending_analysis` → `start_server`（port 0）→ HTTP GET /。驗證一個已評估的事實矛盾、一個只有搜尋摘要而維持待補查的候選、兩個跨媒體差異都正確呈現（繁中標籤與判讀理由），待補查者只以數量出現；頁面與 CLI 輸出不含金鑰或任何網址 token，資料庫檔案不含金鑰與證據網址的 token（來源與文章網址須原樣保存才能抓取）；另一個情境中一篇文章的 provider 失敗，另一篇仍完成；每日上限用完時兩篇都延後。
  - `tests/test_user_flow.py` 改為檢查繁中區塊標題（新文章收件匣／發現事項／來源狀態）。
  - 全部測試：848 passed、0 failed；`ruff check src tests` 無錯誤（基線為 784 passed、1 failed）。
- **剩餘工作：** 見下方。

---

## 剩餘工作

- **真實環境驗收：** 以使用者自己的 Gemini key 對少量真實媒體做小規模監測（規格 12.4），記錄每日文章量、AI 呼叫次數、分析失敗與人工修正次數，再決定 `DAILY_LLM_CALL_LIMIT` 的預設值與 prompt 調整。
- **搜尋來源：** 接入至少一個實際的搜尋 provider（或 Gemini 原生 web search），否則 provider 提出的事實矛盾候選在正式環境中取不到全文證據，只能維持待補查。
- **監測端：** 條件式抓取（ETag／Last-Modified）與來源解析的改進由平行的 monitor／sources 套件處理，不在本文件範圍；`watch_once` 的簽名維持不變。
- **同題證據摘錄：** peer 內文目前取前 2,000 字作為摘錄；差異若出現在長文後段，評估器看不到而維持待補查。可改為以差異片段為中心擷取。
- **本機介面：** 支援 IPv6 迴路位址、顯示每日上限與今日用量、提供頁面上的人工確認（需另行設計 CSRF 防護）。
- **資料管理：** 列出已駁回的同題群組、資料保留期限與匯出報告。

## 刻意不做

- 不因預算或失敗而產生「無法查證即為錯」的結論；略過與失敗只影響工作狀態。
- 不以單篇或單一媒體的結果計算任何分數或排名。
- 不在 CI 呼叫真實 AI、搜尋或 Firecrawl；不在 CI 建立真正的 Windows 工作排程。
