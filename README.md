# 稿核 GaoHe

稿核是台灣向、免費、BYOK（使用者自備 API Key）的媒體複驗工具，針對已發布的媒體報導拆解可驗證主張、對照證據與標記可觀察的語言特徵。

它不是官方闢謠平台，也不對 LINE／社群謠言作公共真假判決，不替整篇文章產生單一「官方真理」結論，不對用詞作道德裁決，也不掛政府背書。

## 目前狀態

這個 repo 是依專案規格建立的新基線。先前提到的外部 `berhen5888/GaoHe` repo 與功能分支目前無法取得，因此本次內容不宣稱恢復了既有實作。

目前提供 Windows 原生執行基礎：版本命令、安全設定檢查與本機 status page。v0 的文章分析、v1 的來源擷取／同題聚類，以及 v2 的同題多媒體對讀仍需獨立的產品實作與驗收。

## Windows 原生安裝

需求：Python 3.11 或更新版本與 Git。若 `py` launcher 不在 PATH，請以你安裝的 `python.exe` 取代下方的 `py -3.11`。

```powershell
py -3.11 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

編輯 `.env`，填入自己的 `GOOGLE_API_KEY`。目前免費測試預設為：

```dotenv
LLM_PROVIDER=gemini
GEMINI_MODEL=gemini-2.5-flash-lite
SEARCH_PROVIDER=none
```

API 費用、免費額度與 429 限流由使用者自行承擔；本專案不提供 API 額度，也不把金鑰送進 Git。

## 本機命令

```powershell
& .\.venv\Scripts\python.exe -m gaohe --version
& .\.venv\Scripts\python.exe -m gaohe doctor --env-file .env
& .\.venv\Scripts\python.exe -m gaohe serve --port 8000
```

`doctor` 只顯示 provider、model、搜尋設定、資料目錄與金鑰是否存在，不會顯示金鑰內容。`serve` 預設只綁定 `127.0.0.1`，目前是確認本機 runtime 的 status page，不是 v2 對讀介面。

## 測試與 CI

本機測試：

```powershell
& .\.venv\Scripts\python.exe -m pytest -q
```

GitHub Actions 使用 `windows-latest` 與 Python 3.11，在每次 push／PR 執行安裝與 pytest。CI 使用 deterministic tests，不呼叫 Gemini、Brave 或 Tavily，也不需要 API secret；CI 綠燈不等於新聞事實判斷正確。

## CD

CD 暫緩。尚未選定部署平台、部署憑證、健康檢查或回滾策略，因此 repo 不包含自動部署設定。

## 版控與安全

- 穩定分支是 `main`，功能分支使用 `codex/` 前綴。
- `.env`、`.venv`、本地資料庫、快照與報告輸出均列入 `.gitignore`。
- 真實 API smoke test 只在使用者本機手動選擇執行；測試輸出不可包含 secret。
