# 稿核 GaoHe

稿核是本機優先的媒體「稿後複驗」工具。它不替整篇文章產生單一真假結論，也不是官方闢謠平台。

它不是官方闢謠平台，也不對 LINE／社群謠言作公共真假判決，不替整篇文章產生單一「官方真理」結論，不對用詞作道德裁決，也不掛政府背書。

## 目前狀態

這個 repo 是依專案規格建立的新基線。先前提到的外部 `berhen5888/GaoHe` repo 與功能分支目前無法取得，因此本次內容不宣稱恢復了既有實作。

目前提供 Windows 原生的監測基礎：來源清單、RSS／列表／sitemap 探索、文章 metadata 與內容版本的本機 SQLite 紀錄、一次性監測命令及本機 status page。`analyze --pending` 已提供本機的 provider-neutral 分析、證據狀態與保守同題 context；標註使用者介面與排程仍未實作。

## Windows 原生安裝

需求：Python 3.11 或更新版本與 Git。若 `py` launcher 不在 PATH，請以你安裝的 `python.exe` 取代下方的 `py -3.11`。

```powershell
py -3.11 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

目前的來源監測不會呼叫 AI 或搜尋服務；可先保留 provider 設定空白。日後若加入需要 AI 的功能，再設定 provider-neutral 的 `LLM_PROVIDER`、`LLM_MODEL` 與 `LLM_API_KEY`：

```dotenv
LLM_PROVIDER=
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

監測採 metadata-first：先記錄來源出現的文章 metadata，再在內容變更時建立新的文章 revision。抓取失敗只會留下來源／抓取失敗紀錄，不代表文章有問題，更不是事實判定。來源、文章 metadata、內容版本與檢查結果預設都只留在本機資料目錄的 SQLite 資料庫。

## 測試與 CI

本機測試：

```powershell
& .\.venv\Scripts\python.exe -m pytest -q
```

GitHub Actions 使用 `windows-latest` 與 Python 3.11，在每次 push／PR 執行安裝與 pytest。CI 使用 deterministic tests，不呼叫外部 provider 或搜尋服務，也不需要 API secret；CI 綠燈不等於新聞事實判斷正確。

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
