# 稿核 GaoHe

稿核是本機優先的媒體「稿後複驗」工具。它不替整篇文章產生單一真假結論，也不是官方闢謠平台。

它不是官方闢謠平台，也不對 LINE／社群謠言作公共真假判決，不替整篇文章產生單一「官方真理」結論，不對用詞作道德裁決，也不掛政府背書。

## 目前狀態

這個 repo 是依專案規格建立的新基線。先前提到的外部 `berhen5888/GaoHe` repo 與功能分支目前無法取得，因此本次內容不宣稱恢復了既有實作。

目前版本是 0.2.0，提供 Windows 原生的監測基礎：來源清單、RSS／列表／sitemap 探索、文章 metadata 與內容版本的本機 SQLite 紀錄、一次性監測命令、繁體中文的本機監看頁面、首次設定精靈與使用者層級排程。

`analyze --pending` 目前使用 Gemini：先抽取可查核的主張與候選問題；有全文證據時，再由同一個 Gemini key 逐頁評估證據與主張的關係（支持、矛盾、背景或無關），最後才依保守規則決定是否形成可見標註。搜尋服務尚未接入，所以目前能評估的全文證據只有本機已儲存的同題文章內文；需要外部網頁證據的候選問題會維持待補查。每篇文章的分析是一個獨立工作：失敗只影響該篇，同一篇最多嘗試 3 次；每日 AI 呼叫次數有上限，每次呼叫都記入本機帳本。其他 AI provider 尚未實作。

上述流程目前只以 deterministic fake provider 測試驗證；尚未以真實 Gemini key、真實新聞來源或真人使用者完成驗收，實際的標註品質、誤判率與每日呼叫量仍待小規模實際監測確認。

## 一般 Windows 使用者：GitHub Release ZIP

目前的一般使用者流程如下：

1. 從 GitHub Release 下載並完整解壓縮 ZIP；不需要 Git，但電腦需先安裝 Python 3.11 或更新版本。
2. 雙擊 `setup.cmd`。它會建立（或重用）此資料夾內的 `.venv`，並啟動本機設定精靈。
3. 在精靈輸入**自己的 Gemini API key**、Gemini model 與至少一個媒體 RSS／列表網址。目前僅支援 Gemini；其他 AI provider 是後續工作。每位下載者各自使用自己的帳號、額度與條款，key 僅保留在本機 `.env`。
4. 在解壓縮資料夾開啟 PowerShell，安裝排程、確認狀態，再開啟只綁定本機的監看頁面：

```powershell
& .\.venv\Scripts\python.exe -m gaohe schedule install --env-file .env
& .\.venv\Scripts\python.exe -m gaohe schedule status
& .\.venv\Scripts\python.exe -m gaohe serve --env-file .env
```

`schedule status` 應回報 `installed`；接著以瀏覽器開啟 `http://127.0.0.1:8000/`。排程只在目前登入使用者下定期執行，不建立 Windows Service，也不保存 Windows 密碼。

5. 完成使用後可雙擊 `uninstall.cmd`。預設只移除固定名稱的 `GaoHe Watch` 排程與此專案的 `.venv`，會保留 `.env` 和 SQLite 資料。若確定要刪除設定或資料，請在 PowerShell 明確輸入：

```powershell
.\uninstall.cmd -DeleteConfig -DeleteData -Confirmation "DELETE GAOHE DATA"
```

腳本會先顯示實際解析後的刪除路徑；確認文字不完全相同時，不會刪除設定或資料，並以非零狀態結束。單一檔案 EXE 仍暫緩，待 ZIP 流程取得真實使用者回饋後再評估。

## 開發者安裝

需求：Git 與 Python 3.11 或更新版本。先 clone，再建立虛擬環境與安裝 editable package；若 `py` launcher 不在 PATH，請以你安裝的 `python.exe` 取代下方的 `py -3.11`。

```powershell
git clone https://github.com/<owner>/GaoHe.git
Set-Location GaoHe
py -3.11 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
& .\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q
& .\.venv\Scripts\python.exe -m gaohe doctor --env-file .env
```

目前的來源監測不會呼叫 AI 或搜尋服務；可先保留 provider 設定空白。若要使用現有分析功能，設定目前唯一支援的 Gemini `LLM_PROVIDER`、`LLM_MODEL` 與 `LLM_API_KEY`：

```dotenv
LLM_PROVIDER=gemini
LLM_MODEL=
LLM_API_KEY=
WEB_SEARCH_PROVIDER=none
DAILY_LLM_CALL_LIMIT=200
```

`DAILY_LLM_CALL_LIMIT` 是每天（依電腦的本地時間，台灣即午夜 00:00 起算）最多送出的 AI 呼叫次數，主張抽取與證據評估各算一次，預設 200，必須是正整數。金鑰不會寫入 SQLite，也不應提交到 Git。

## 本機命令

```powershell
& .\.venv\Scripts\python.exe -m gaohe --version
& .\.venv\Scripts\python.exe -m gaohe doctor --env-file .env
& .\.venv\Scripts\python.exe -m gaohe source add --name "Example News" --feed-url "https://example.test/feed.xml" --env-file .env
& .\.venv\Scripts\python.exe -m gaohe source list --env-file .env
& .\.venv\Scripts\python.exe -m gaohe watch --once --env-file .env
& .\.venv\Scripts\python.exe -m gaohe analyze --pending --limit 20 --env-file .env
& .\.venv\Scripts\python.exe -m gaohe status --env-file .env
& .\.venv\Scripts\python.exe -m gaohe finding list --env-file .env
& .\.venv\Scripts\python.exe -m gaohe finding list --all --limit 20 --env-file .env
& .\.venv\Scripts\python.exe -m gaohe finding review --id 3 --status confirmed --note "已對照公報" --env-file .env
& .\.venv\Scripts\python.exe -m gaohe topic list --env-file .env
& .\.venv\Scripts\python.exe -m gaohe topic review --id 2 --status dismissed --env-file .env
& .\.venv\Scripts\python.exe -m gaohe serve --port 8000 --env-file .env
```

可選擇 `--article-url` 儲存新聞列表網址作為來源 metadata，保留給未來的文章列表探索；目前 `watch --once` 僅監測 `--feed-url`。用 `source disable --id ID` 暫停、`source enable --id ID` 恢復來源。

- `analyze --pending` 只輸出統計：`claims`、`candidates`、`visible_findings`、`pending`、`retrieval_failures`、`rejected_claims`（模型提出但無法在原文定位而捨棄的主張）、`analyzed`、`failed`、`skipped`；不輸出文章全文、金鑰或整篇判定。
- `status` 顯示資料庫 schema 版本、分析佇列各狀態數量、今天已用的 AI 呼叫次數與上限、上次監測，以及每個來源最近一次檢查結果；網址與錯誤訊息都先遮蔽帳密與 token。
- `finding list` 預設只列出可見發現；加 `--all` 會一併列出仍待補查的候選問題。每列包含編號、類型、證據狀態、人工確認狀態、文章標題、遮蔽後的網址與摘要。
- `finding review --status confirmed|dismissed` 記錄人工確認或駁回，可加 `--note` 留下備註；找不到編號時以狀態 2 結束。已駁回的發現不會在原文上色。
- `topic list` 列出至少兩篇文章、尚未駁回的同題群組；`topic review --status active|dismissed` 確認或駁回分組。被駁回的組合之後不再作為同題 context，也不會產生跨媒體差異。
- `doctor`、`status` 與本機監看頁面只顯示金鑰是否存在，不會顯示內容。
- `serve` 只接受本機位址：`--host 127.0.0.1`（預設）、`localhost` 或 `::1`；其他位址會直接拒絕並以狀態 2 結束，監看頁面不會開放給區網或網際網路。本機頁面目前尚未支援 IPv6 綁定，使用 `::1` 會顯示 `error: serve unavailable`，請改用預設值。開啟後以瀏覽器前往 `http://127.0.0.1:8000/`。

## 監測契約

監測採 metadata-first：先記錄來源出現的文章 metadata，再在內容變更時建立新的文章 revision。頁面內容有變更時，會建立一個新的 revision，並進入一次 pending analysis cycle；內容 hash 未變時，會略過重複 revision。只有每篇文章目前的版本會被分析或拿來做同題比較，舊版本不會。抓取失敗只會留下來源／抓取失敗紀錄，不代表文章有問題，更不是事實判定。

分析依序是：主張抽取 → 候選問題 → 取回全文證據 → 證據評估 → 決定是否可見。

- **證據評估：** 每一頁取回的全文（或同題文章的內文）都會連同主張交給 AI 判讀，AI 必須引用該頁的原文片段並說明理由；引文在頁面中找不到、理由空白或判讀失敗，該頁就視為「未評估」。
- **可見標註門檻：** 只有經過評估、有判讀理由、可回溯網址的全文證據，才可能讓候選問題形成原文上的可見標註。搜尋摘要永遠只是線索；找不到證據、取回失敗、來源失敗或未評估，都只會維持待補查，不會變成錯誤判定。只要有任何來源支持原文，事實矛盾就維持待人工判讀。
- **失敗與重試：** 某篇文章分析時任何一步出錯，只會把該篇標為分析失敗並記下已遮蔽的錯誤原因，其他文章照常處理；失敗的文章會在之後的排程中再試，同一篇最多嘗試 3 次。中途被中斷而停在「分析中」超過 60 分鐘的工作，下次會自動接手。
- **每日呼叫上限：** 每次主張抽取與證據評估都記入本機帳本（只記時間、provider、模型、用途、字數與成敗，不記 prompt 或金鑰）。當天呼叫次數達到 `DAILY_LLM_CALL_LIMIT` 時，尚未完成的文章會標為「已略過」並保留到隔天，不消耗重試次數，也不會留下半套結果。

GaoHe 不會替整篇文章下 verdict，也不產生媒體總分。來源、文章 metadata、內容版本、分析結果與檢查紀錄預設都只留在本機資料目錄的 SQLite 資料庫。

## 疑難排解

- **找不到 Python：** 安裝 Python 3.11+ 後重新執行 `setup.cmd`；若已安裝但 `py` 不可用，使用該 Python 的完整 `python.exe` 路徑執行開發者指令。
- **本機頁面埠號已被使用：** 關閉先前的 `gaohe serve`，或用 `gaohe serve --port 8001 --env-file .env` 改用未佔用埠號，並開啟相同的 `127.0.0.1` 位址。
- **來源 URL 無效：** 設定精靈只接受無帳密、無空白的 HTTP(S) RSS／列表 URL；請改用媒體公開提供的正確網址。
- **來源取回失敗：** 檢查網路、網址與媒體端是否暫時回應失敗；失敗紀錄不表示新聞內容有問題，稍後排程會再嘗試。
- **沒有或不足夠的證據：** 這表示系統沒有取得可追溯、範圍足夠的證據；結果會維持 pending、retrieval failed 或 insufficient scope，而不會變成文章總判決。
- **文章顯示「分析失敗」：** 用 `gaohe status --env-file .env` 查看各狀態數量；失敗原因已遮蔽，常見是 AI 服務逾時、額度用盡或回應格式不符。之後的排程會自動重試，同一篇最多嘗試 3 次；用完後會停在分析失敗，不再消耗額度。
- **文章顯示「已略過」：** 通常是當天 AI 呼叫已達 `DAILY_LLM_CALL_LIMIT`。這些文章會在隔天（本地時間午夜後）的排程繼續分析，不算失敗；若每天都不夠用，可在 `.env` 調高上限，但請先確認自己的 AI 帳戶額度與費用。
- **資料目錄出現 `gaohe.db-wal`、`gaohe.db-shm`：** 這是 SQLite 的 WAL 模式檔案，讓排程監測與本機頁面可以同時讀寫，屬正常現象。請不要手動刪除；需要備份時，先關閉 `gaohe serve` 並等排程結束，再連同這兩個檔案一起複製。
- **Gemini 額度或錯誤：** 確認自己的 key、模型、帳戶額度、rate limit 與供應商條款；GaoHe 不共用 repository 的帳戶或額度。
- **Firecrawl：** Firecrawl 目前尚未接入 runtime，不能作為現行分析的搜尋／抓取來源。未來若啟用，仍是使用者自己的 credits 與 rate limits，且可能遇到 403、付費牆、JavaScript、登入或 CAPTCHA；GaoHe 不會繞過這些限制。

## 測試與 CI

本機測試：

```powershell
& .\.venv\Scripts\python.exe -m pytest -q
```

GitHub Actions 使用 `windows-latest` 與 Python 3.11，在每次 push／PR 執行安裝與 pytest。CI 使用 deterministic tests，不呼叫外部 provider 或搜尋服務、不建立真正的 Task Scheduler 工作，也不需要 API secret；CI 綠燈不等於新聞事實判斷正確。上述安裝精靈與排程尚未完成真人瀏覽器／Task Scheduler 操作驗收；CI 不包含 CD 工作。

## CD

CD 暫緩。尚未選定部署平台、部署憑證、健康檢查或回滾策略，因此 repo 不包含自動部署設定。

## 版控與安全

- 穩定分支是 `main`，功能分支使用 `codex/` 前綴。
- `.env`、`.venv`、本地資料庫、快照與報告輸出均列入 `.gitignore`。
- 真實 API smoke test 只在使用者本機手動選擇執行；測試輸出不可包含 secret。
# GaoHe

## 稿後複驗的證據模型

GaoHe 只處理已擷取的新聞文本，不產生整篇文章的真假判決或媒體評分。結果分成三層：

- `claims` 是從原文擷取、帶有精確文字位置的可檢視陳述。
- `candidates` 是值得查核的提案，尚未代表錯誤。
- `visible findings` 必須有可追溯的全文證據與明確關係；沒有搜尋結果、抓取失敗或證據範圍不足時，均維持非可見狀態。

使用 `gaohe analyze --pending [--limit N]` 處理尚未分析的 revision。它只輸出 claims、candidates、visible findings、pending、retrieval failures、rejected claims 與 analyzed／failed／skipped 的統計，不會輸出文章全文、API key 或整篇 verdict。它會以本機近期各文章的目前版本建立保守的高信心同題 context，讓後加入的文章仍可與先前已完成分析的不同來源文章比較；同題分組（含「可能同題」）會存入本機，供 `gaohe topic list` 與人工確認。同題差異只是候選問題：另一篇文章的內文要經過證據評估、確認兩者確實衝突，才會形成實質跨媒體差異標註；沒有評估時一律維持待補查。

搜尋與抓取是兩件不同的事：搜尋只提供發現來源的線索，頁面抓取才取得可留存的本文摘錄；搜尋 snippet 不能單獨形成 visible finding。目前 `analyze` CLI 僅支援 `WEB_SEARCH_PROVIDER=none`。`DirectPageFetcher` adapter 已保留，但目前的 `NullSearchProvider` 不會產生 hits，因此正式 CLI 尚無可用搜尋來源觸發直接頁面抓取；跨媒體比較只使用本機已儲存、未被人工駁回的高信心同題 revision。設為 `firecrawl` 或其他值會安全地以設定不支援結束（exit 2）。Firecrawl adapter 與設定契約僅為後續接線保留，尚未接入 runtime；正式接線前仍須評估使用者自己的 credits、rate limit、403、付費牆、JavaScript、登入與 CAPTCHA 限制。GaoHe 不會繞過登入、付費牆或 CAPTCHA。

每位使用者自行提供 provider key。key 不會提交到 repo，也不會寫入 SQLite 的文章、證據或錯誤資料。CI 僅跑可重現的 fake provider 測試，不連網；CD 目前暫緩。
