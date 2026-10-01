# 稿核 GaoHe

稿核是本機優先的新聞「稿後複驗」工具：監測固定媒體或查核單篇新聞網址，用 Gemini 找出值得查核的說法、以 Google 搜尋找證據來源、抓取原始頁面全文判讀，只在證據明確時於原文標註。

它不是官方闢謠平台，不對整篇文章下真假判決，不提供媒體分數，也不處理 LINE／社群轉傳訊息。

## 運作方式

```text
媒體 RSS／列表 ──(排程每小時)──> 新文章或內容變更 ─┐
貼上單篇網址 ──(gaohe check)──────────────────────┤
                                                  v
Gemini 抽出可查核的說法 -> Google 搜尋找來源 -> 抓取來源全文 -> Gemini 判讀關係
                                                  v
                     只有「可追溯的全文證據」才形成標註 -> 本機頁面 http://127.0.0.1:8000/
```

可見標註只有兩種：

- **事實矛盾**（淡紅）：可取得的原始頁面直接與文章說法衝突，且沒有來源支持原說法。
- **推論超出證據**（淡紫）：文章把推測或因果寫成定論，至少兩個獨立網站都只支持背後的事實。

搜尋摘要永遠不是證據；抓不到頁面、找不到證據、來源網站失敗，都只會是「待查證」，不代表文章有問題。未標註的句子也不代表已被證實。

## 給一般 Windows 使用者

需要 Python 3.11 以上與一把 Gemini API 金鑰。

1. 下載並完整解壓縮 ZIP。
2. 雙擊 `setup.cmd`：它會建立 `.venv` 並開啟設定精靈。貼上 Gemini 金鑰（模型已有預設值）與一個媒體 RSS／列表網址。
3. 在解壓縮資料夾開啟 PowerShell，安裝排程並開啟本機頁面：

```powershell
& .\.venv\Scripts\python.exe -m gaohe schedule install --env-file .env
& .\.venv\Scripts\python.exe -m gaohe serve --env-file .env
```

4. 想查核某一篇新聞：

```powershell
& .\.venv\Scripts\python.exe -m gaohe check "https://新聞網址" --env-file .env
```

5. 不再使用時雙擊 `uninstall.cmd`；預設保留 `.env` 與資料。要一併刪除：
   `.\uninstall.cmd -DeleteConfig -DeleteData -Confirmation "DELETE GAOHE DATA"`

## 指令

| 指令 | 用途 |
|---|---|
| `gaohe setup` | 開啟本機設定精靈 |
| `gaohe doctor` | 檢查設定是否完整（不顯示金鑰） |
| `gaohe source add --name N --feed-url URL` / `list` / `enable --id` / `disable --id` | 管理監測來源 |
| `gaohe watch --once` | 檢查一次所有來源 |
| `gaohe analyze --pending [--limit 20]` | 分析尚未分析的文章；金鑰、額度或連線問題會停下並以代碼 3 結束 |
| `gaohe check URL` | 立即查核一篇新聞 |
| `gaohe serve [--port 8000]` | 開啟只綁定本機的結果頁 |
| `gaohe schedule install` / `status` / `remove` | 管理 Windows 工作排程（每 `POLL_INTERVAL_MINUTES` 分鐘執行 watch + analyze） |

設定（`.env`）：`LLM_PROVIDER=gemini`、`LLM_MODEL`、`LLM_API_KEY`、`WEB_SEARCH_PROVIDER`（`gemini` 或 `none`）、`DATA_DIR`、`POLL_INTERVAL_MINUTES`。

## 監測規則

- 新出現的文章抓一次；發布 48 小時內的文章，每 3 小時重抓一次以發現改稿；內容沒變不會重複分析。
- 每個來源每次最多抓 30 篇，同一網站的請求間隔至少 1 秒。
- 只抓公開網址：指向本機或內部網路（含轉址）的連結一律拒絕。
- 分析失敗的文章最多重試 3 次；金鑰錯誤、額度用完、限流或斷線會讓整次分析停下，文章保留在佇列，不消耗重試次數。

## 資料與隱私

- 金鑰只存在本機 `.env`，不寫入資料庫、頁面或錯誤訊息。
- 文章、證據與結果存在 `DATA_DIR\gaohe.db`（SQLite，會一併產生 `gaohe.db-wal`、`gaohe.db-shm`）。
- 文章內容與查核問題會送到 Gemini；找證據時 Gemini 會使用 Google 搜尋。
- 若出現「資料庫格式不相容」，代表資料庫來自舊版測試：備份後刪除 `gaohe.db` 即可。

## 開發

```powershell
py -3.11 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
& .\.venv\Scripts\python.exe -m pytest -q
& .\.venv\Scripts\python.exe -m ruff check src tests
```

模組：`monitor`（輪詢來源）→ `storage`（SQLite）→ `pipeline`（分析佇列）→ `analysis`（抽說法、找證據、判斷是否可見）→ `providers`（Gemini 與頁面抓取）；`checks`（單篇查核）、`web`（結果頁）、`setup_flow`（設定精靈）、`safety`（網址驗證與遮罩）。

CI 在 Windows 上執行 lint 與 deterministic 測試，不呼叫真實 Gemini，也不需要金鑰。測試通過不代表新聞判讀正確；真實 Gemini 與真實網站尚未經過實際驗收。
