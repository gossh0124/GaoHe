# 稿核 GaoHe

稿核是本機優先的媒體「稿後複驗」工具。它不替整篇文章產生單一真假結論，也不是官方闢謠平台。

它不是官方闢謠平台，也不對 LINE／社群謠言作公共真假判決，不替整篇文章產生單一「官方真理」結論，不對用詞作道德裁決，也不掛政府背書。

## 目前狀態

這個 repo 是依專案規格建立的新基線。先前提到的外部 `berhen5888/GaoHe` repo 與功能分支目前無法取得，因此本次內容不宣稱恢復了既有實作。

目前提供 Windows 原生的監測基礎：來源清單、RSS／列表／sitemap 探索、文章 metadata 與內容版本的本機 SQLite 紀錄、一次性監測命令、本機 status page、首次設定精靈與使用者層級排程。`analyze --pending` 目前使用 Gemini 分析，提供證據狀態與保守同題 context；其他 AI provider 尚未實作。

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
```

金鑰不會寫入 SQLite，也不應提交到 Git。

## 本機命令

```powershell
& .\.venv\Scripts\python.exe -m gaohe --version
& .\.venv\Scripts\python.exe -m gaohe doctor --env-file .env
& .\.venv\Scripts\python.exe -m gaohe source add --name "Example News" --feed-url "https://example.test/feed.xml" --env-file .env
& .\.venv\Scripts\python.exe -m gaohe source list --env-file .env
& .\.venv\Scripts\python.exe -m gaohe watch --once --env-file .env
& .\.venv\Scripts\python.exe -m gaohe analyze --pending --limit 20 --env-file .env
& .\.venv\Scripts\python.exe -m gaohe serve --port 8000
```

可選擇 `--article-url` 儲存新聞列表網址作為來源 metadata，保留給未來的文章列表探索；目前 `watch --once` 僅監測 `--feed-url`。用 `source disable --id ID` 暫停、`source enable --id ID` 恢復來源。`doctor` 與本機 status page 只顯示金鑰是否存在，不會顯示內容。`serve` 預設只綁定 `127.0.0.1`；開啟後以瀏覽器前往 `http://127.0.0.1:8000/` 檢查本機 runtime。

## 監測契約

監測採 metadata-first：先記錄來源出現的文章 metadata，再在內容變更時建立新的文章 revision。頁面內容有變更時，會建立一個新的 revision，並進入一次 pending analysis cycle；內容 hash 未變時，會略過重複 revision。抓取失敗只會留下來源／抓取失敗紀錄，不代表文章有問題，更不是事實判定。證據不足、搜尋未命中或取回失敗時，finding 仍維持非結論性的狀態；GaoHe 不會替整篇文章下 verdict。來源、文章 metadata、內容版本與檢查結果預設都只留在本機資料目錄的 SQLite 資料庫。

## 疑難排解

- **找不到 Python：** 安裝 Python 3.11+ 後重新執行 `setup.cmd`；若已安裝但 `py` 不可用，使用該 Python 的完整 `python.exe` 路徑執行開發者指令。
- **本機頁面埠號已被使用：** 關閉先前的 `gaohe serve`，或用 `gaohe serve --port 8001 --env-file .env` 改用未佔用埠號，並開啟相同的 `127.0.0.1` 位址。
- **來源 URL 無效：** 設定精靈只接受無帳密、無空白的 HTTP(S) RSS／列表 URL；請改用媒體公開提供的正確網址。
- **來源取回失敗：** 檢查網路、網址與媒體端是否暫時回應失敗；失敗紀錄不表示新聞內容有問題，稍後排程會再嘗試。
- **沒有或不足夠的證據：** 這表示系統沒有取得可追溯、範圍足夠的證據；結果會維持 pending、retrieval failed 或 insufficient scope，而不會變成文章總判決。
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

使用 `gaohe analyze --pending [--limit N]` 處理尚未分析的 revision。它只輸出 claims、candidates、visible findings、pending 與 retrieval failures 的統計，不會輸出文章全文、API key 或整篇 verdict。它會以本機近期 revision 建立保守的高信心同題 context，讓後加入的文章仍可與先前已完成分析的不同來源文章比較。

搜尋與抓取是兩件不同的事：搜尋只提供發現來源的線索，頁面抓取才取得可留存的本文摘錄；搜尋 snippet 不能單獨形成 visible finding。目前 `analyze` CLI 僅支援 `WEB_SEARCH_PROVIDER=none`。`DirectPageFetcher` adapter 已保留，但目前的 `NullSearchProvider` 不會產生 hits，因此正式 CLI 尚無可用搜尋來源觸發直接頁面抓取；跨媒體比較只使用本機已儲存的高信心同題 revision。設為 `firecrawl` 或其他值會安全地以設定不支援結束（exit 2）。Firecrawl adapter 與設定契約僅為後續接線保留，尚未接入 runtime；正式接線前仍須評估使用者自己的 credits、rate limit、403、付費牆、JavaScript、登入與 CAPTCHA 限制。GaoHe 不會繞過登入、付費牆或 CAPTCHA。

每位使用者自行提供 provider key。key 不會提交到 repo，也不會寫入 SQLite 的文章、證據或錯誤資料。CI 僅跑可重現的 fake provider 測試，不連網；CD 目前暫緩。
