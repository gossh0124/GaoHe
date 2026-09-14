# GaoHe Windows 原生執行與 GitHub CI 設計

- 日期：2026-09-14
- 狀態：待審閱
- 範圍：本機執行、GitHub 版控、CI；CD 暫緩

## 1. 問題本質

核心問題不是把 LLM 搬到本機，而是讓 GaoHe 不依賴雲端 worker 額度，能在 Windows 原生環境被安裝、測試與操作，同時保留 BYOK 的 Gemini API 供應方式。

成功條件：

1. 程式碼、變更歷史與 CI 設定都在 public GitHub repo `gossh0124/GaoHe`。
2. Windows PowerShell 能建立隔離 Python 環境並執行 GaoHe CLI／本機 UI。
3. Gemini 金鑰只存在本機 `.env` 或明確設定的 CI secret，不進 Git、不出現在 log。
4. 每次 push 或 PR 都能在不使用真實 API 金鑰的情況下完成自動測試。
5. 專案文件清楚區分「本機程式執行」、「遠端 Gemini 推理」與「尚未配置的部署」。

目前已知狀態：本機資料夾原先只有空的 Git repo；先前貼文提到的 `berhen5888/GaoHe` 與功能分支無法取得。因此後續程式碼若重新建立，必須標示為依規格建立的新基線，不得宣稱恢復了既有遠端實作。

## 2. 第一性原理

### 不可省略的事實

- 使用者需要的是本機可執行的工作流程，不是本機模型推理。
- LLM 呼叫仍需透過 Gemini 網路 API，因此 API 額度與雲端 worker 額度是兩個不同限制。
- CI 若呼叫真實 API，會消耗額度並需要暴露 secret；單元與整合測試可以用假 provider 驗證主要流程。
- CD 必須有明確主機、部署憑證與可驗收的健康檢查；目前沒有這些條件。

### 推理鏈

`不依賴雲端 worker` → `原始碼放入 GitHub` → `Windows venv 執行` → `Gemini provider 維持 BYOK` → `本地資料保存` → `CI 用離線測試證明可安裝與運行`。

因此本次只建立 CI，不建立沒有部署目標的 CD；也不加入 Docker、本機 LLM、跨平台矩陣或自動模型路由。

## 3. 範圍

### 本次包含

- public repo `gossh0124/GaoHe`，穩定分支命名為 `main`。
- Windows 原生 PowerShell 的開發與試跑文件。
- Python 3.11+ 的可安裝專案結構與開發測試依賴。
- `.env.example`、`.gitignore` 與不含金鑰的設定說明。
- GitHub Actions CI：安裝專案並執行 pytest。
- v0／v1／v2 的完成度分開回報；CI 綠燈不等於新聞複驗正確或 v2 已完成。

### 本次不包含

- Ollama、llama.cpp 或其他本機 LLM。
- Docker、WSL、雲端 worker 或自動部署。
- 真實 API 呼叫的必要 CI job。
- 未確認 robots／授權前的全文爬蟲。
- 對整篇文章產生單一真偽判決。

## 4. 執行設計

本機使用 Windows 原生 PowerShell：

1. 以 `py -3.11 -m venv .venv` 建立專案隔離環境。
2. 以 `.venv\Scripts\Activate.ps1` 啟用環境。
3. 以 editable install 安裝專案與測試依賴。
4. 複製 `.env.example` 為 `.env`，由使用者填入 Gemini 金鑰。
5. 開發預設使用 `gemini-2.5-flash-lite` 與 `SEARCH_PROVIDER=none`，符合目前免費測試決策。
6. CLI 與 UI 僅綁定本機 loopback；資料庫、快照與輸出檔留在本機且列入忽略規則。

業務邏輯只依賴 provider 介面，不直接讀取特定廠商 SDK。provider 層負責 API 錯誤、429／退避與回應解析；缺少金鑰時在信任邊界明確失敗，錯誤訊息不得包含 secret。

## 5. 版控設計

- GitHub repo：`https://github.com/gossh0124/GaoHe`。
- `main`：可重現、可通過 CI 的穩定基線。
- 功能工作使用 `codex/` 前綴分支，完成後透過 PR 合併。
- `.gitignore` 至少排除 `.env`、`.venv/`、Python cache、pytest cache、本地資料庫、快照與報告輸出。
- 第一個程式基線提交前，先確認 `git diff --check`、金鑰掃描式檢查與測試結果；不得把先前外部 worker 的未驗證成果當作本輪證據。

## 6. CI 設計

新增 `.github/workflows/ci.yml`：

- 觸發：所有分支 push，以及 targeting `main` 的 pull request。
- runner：單一 `windows-latest`，與使用者的 Windows 原生環境一致。
- Python：3.11。
- 步驟：checkout → setup Python → pip install 專案與 dev extras → `python -m pytest -q`。
- 權限：`contents: read`。
- 不注入 Gemini、Brave 或 Tavily secret；測試 provider 使用 deterministic fake／fixture。
- CI 失敗即阻止「可合併」判定，但不宣稱它證明外部新聞來源、API 額度或事實判斷品質。

不加入 lint、coverage gate、矩陣測試或第三方 action，除非測試量或維護需求證明它們有必要。

## 7. 驗收證據

### 本機

- `python --version` 顯示 3.11 或以上。
- 測試命令在乾淨 venv 通過。
- 不含 API 金鑰的 sample／fake-provider 流程可產生結構化輸出。
- 真實 Gemini smoke test 僅由使用者在本機手動選擇執行，並記錄 provider、model、prompt version、原文 hash 與抓取時間。

### GitHub

- repo 可公開瀏覽，`main` 與功能分支歷史可追溯。
- PR／push 會觸發 CI，且至少有一次成功 run 的連結與 commit SHA。
- repo 內容沒有 `.env`、金鑰或本地資料庫。

### 尚未宣稱

- 沒有搜尋金鑰時，事實層大量 `insufficient` 是預期限制，不是 CI 問題。
- CI 綠燈不代表 v1 擷取、同題聚類、v2 對讀 UI 或新聞事實判斷已完成。
- CD 暫緩，直到選定部署平台、secret 管理方式、健康檢查與回滾策略。

