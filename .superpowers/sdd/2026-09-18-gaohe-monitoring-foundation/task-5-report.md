# Task 5 報告：來源管理命令與監測契約

## 完成範圍

- 新增 `gaohe source add --name NAME --feed-url URL [--article-url URL]`。
- 新增 `gaohe source list`、`gaohe source disable --id ID`、`gaohe source enable --id ID`。
- `source` 與 `watch --once` 都從同一個 `Settings.database_path` 初始化同一種 SQLite `Store`。
- CLI 邊界拒絕空白名稱及沒有 host 的非 HTTP(S) feed/article URL，錯誤回傳碼為 2 且訊息可直接修正輸入。
- `Store.set_source_enabled()` 只更新目標來源；不存在的 ID 回傳可處理的 CLI 錯誤。
- README 移除 Gemini/Google 專屬設定，改為 provider-neutral 的延後設定說明、來源到 `watch --once` 的本機流程、status page 入口與目前監測邊界。
- README 明確記錄 metadata-first、內容變更建立 revision、抓取失敗不是事實發現，以及預設資料只留本機 SQLite。

## 測試與證據

1. TDD red：`tests/test_sources_cli.py` 在實作前執行，3 項測試皆因 `source` 子命令不存在而失敗。
2. TDD green：新增命令後重跑 `tests/test_sources_cli.py`，結果 `3 passed`。
3. 完整測試：`& .\.venv\Scripts\python.exe -m pytest -q --basetemp .pytest-tmp`，結果 `52 passed in 1.02s`。
4. 離線 smoke：以臨時目錄、真實 `Store`/`watch_once` 與本機 `FixtureTransport` 執行；第一次建立 1 個 revision、第二次未變更建立 0 個、修改文章本文後再建立 1 個，輸出 `fixture smoke: revisions=1,0,1`。沒有真實網路要求。
5. `git diff --check` 結束碼為 0。
6. 修改與新增的文字檔均檢查為 UTF-8 無 BOM；敏感字串掃描未發現 API key、Bearer 或 Authorization 值。

## 自我檢閱

- 新測試使用真實 Settings、SQLite Store 與 CLI，覆蓋新增/列出、持久化啟用切換、輸入驗證與清單不輸出測試用 key。
- 沒有加入 AI、搜尋、Firecrawl、UI、scheduler、外部網路呼叫或新相依套件。
- 沒有 push；僅建立本機 commit。

## 已知邊界

- `source list` 是 CLI 清單，不是來源健康或文章瀏覽介面；目前 status page 也仍是 runtime 狀態頁。
- 來源 URL 應使用公開的 feed/list URL；不要把 access token 放入 URL，因為 URL 是監測資料的一部分。

## Fix round 1：CLI URL 輸出遮罩

### 修改檔案

- `src/gaohe/cli.py`：在 `source list` 輸出邊界以標準庫 URL 解析遮罩 user-info，並遮罩 `authorization`、`cookie`、`token`、`secret`、`password`、`session`、`api-key` 與 `api_key` 的 query value；host、path 與非敏感 query 參數保持可辨識。
- `tests/test_sources_cli.py`：以暫存資料目錄建立含 user-info、`token` 與 `api_key` 的 feed/article URL，驗證輸出不含秘密且保留安全 host/path/參數。
- `.superpowers/sdd/2026-09-18-gaohe-monitoring-foundation/task-5-report.md`：追加本輪修補證據。

### 命令輸出

1. TDD red：`& .\.venv\Scripts\python.exe -m pytest -q tests/test_sources_cli.py --basetemp .pytest-tmp`，結果 `1 failed, 3 passed in 0.33s`；失敗輸出包含未遮罩的 `token:secret`。
2. Focused：`& .\.venv\Scripts\python.exe -m pytest -q tests/test_sources_cli.py --basetemp .pytest-tmp`，結果 `4 passed in 0.20s`。
3. 完整：`& .\.venv\Scripts\python.exe -m pytest -q --basetemp .pytest-tmp`，結果 `53 passed in 1.76s`。
4. `git diff --check`，結束碼 `0`。
