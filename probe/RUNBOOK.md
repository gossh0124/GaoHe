# GaoHe P0 可行性實測 Runbook

對應規格 `docs/superpowers/specs/2026-11-05-gaohe-skill-connector-design.md` §4.2（六項實測）與 §14（P0 退出條件）。
本原型只量測，不做查核或評分；Cloudflare D1 只存段落 ID（sha256）、計數與 HMAC 後的 IP 前綴，不存正文（§5.1）。

標示「未確認」者為撰寫時無法實際操作核對的介面文字或行為，請以當下畫面為準並在結果表註記。

## 目錄

```text
(repo root)
├─ schemas/normalize-vectors.json   比對鍵與切段測試向量（P2 共用）
└─ probe/
   ├─ RUNBOOK.md              本文件
   ├─ server/                    Cloudflare Worker（gaohe-probe）
   │  ├─ src/                    normalize / anthropicEgress / ip / github / access / auth / metrics / probe / report / retention / provider / index
   │  ├─ migrations/0001_probe.sql
   │  ├─ test/unit, test/integration
   │  ├─ scripts/smoke-local.sh  wrangler dev --local + curl 冒煙測試
   │  └─ wrangler.jsonc
   └─ skill/
      ├─ build-zip.sh         打包 Skill ZIP
      └─ gaohe-probe/            SKILL.md、urls.txt、artifact-check.html
```

## 0. 需要準備

- Node.js 22 與 npm。
- 一個 Cloudflare 帳號（免費方案即可）。
- **兩個 GitHub 帳號**（帳號 A、帳號 B），**建立滿 30 天**（未滿 30 天的帳號可登入，但伺服器不寫入任何資料，§9.1）。
- claude.ai 付費方案（Pro 以上；Free 方案可能只允許 1 個自訂連接器，Team／Enterprise 需由 Owner 加入連接器——皆未確認）。理想上兩個 GitHub 帳號各搭配一個 claude.ai 帳號；只有一個 claude.ai 帳號時，見步驟 9 的替代做法。
- 約 10 個台灣新聞文章網址（步驟 8）。

## 1. 安裝與本機驗證

```bash
cd probe/server
npm install            # .npmrc 已設 legacy-peer-deps=true（npm 10.9 解析 agents 的選用 peer 依賴時會當掉）
npm run typecheck
npm test               # 單元測試（Node）＋整合測試（workerd，含完整 OAuth→MCP→Cloudflare D1 流程）
npm run smoke          # wrangler dev --local + curl：metadata、401、/probe/report
```

全部通過再繼續。

## 2. 登入 Cloudflare

```bash
npx wrangler login
npx wrangler whoami    # 確認帳號
```

## 3. 建立 Cloudflare D1 與 KV

```bash
npx wrangler d1 create gaohe-probe
npx wrangler kv namespace create OAUTH_KV
```

把輸出的 `database_id` 與 KV `id` 分別填入 `server/wrangler.jsonc` 中的佔位值
（`00000000-0000-0000-0000-000000000000` 與 `00000000000000000000000000000000`）。

套用資料表：

```bash
npm run db:migrate:remote
```

## 4. 第一次部署（取得網址）

```bash
npm run deploy
```

記下輸出的網址，例如 `https://gaohe-probe.<你的子網域>.workers.dev`。以下稱為 `$ORIGIN`，連接器網址為 `$ORIGIN/mcp`。
（此時尚未設定 secrets，登入流程還不能用，這是正常的。）

## 5. 建立 GitHub OAuth App

GitHub → Settings → Developer settings → OAuth Apps → New OAuth App：

| 欄位 | 值 |
|---|---|
| Application name | `GaoHe probe (P0)` |
| Homepage URL | `$ORIGIN` |
| Authorization callback URL | `$ORIGIN/callback`（必須完全一致） |
| Enable Device Flow | 不勾 |

建立後記下 **Client ID**，按 Generate a new client secret 取得 **Client secret**。
本服務不要求任何 GitHub scope，只讀取公開的數字 user id 與帳號建立時間，GitHub token 用完即丟、不保存。

## 6. 設定 secrets

```bash
npx wrangler secret put GITHUB_CLIENT_ID        # 貼上 Client ID
npx wrangler secret put GITHUB_CLIENT_SECRET    # 貼上 Client secret
openssl rand -hex 32 | npx wrangler secret put IP_HMAC_KEY
openssl rand -hex 32 | tee report-token.txt | npx wrangler secret put REPORT_TOKEN
# 實測名單：帳號 A、B 的 GitHub 數字 id，以逗號分隔
curl -s https://api.github.com/users/<帳號A的login> | jq .id
curl -s https://api.github.com/users/<帳號B的login> | jq .id
echo '<idA>,<idB>' | npx wrangler secret put PROBE_ALLOWED_GITHUB_IDS
```

- `IP_HMAC_KEY` 必須 ≥ 32 bytes；未設定或太短時 `/callback` 回 500「Server misconfigured」（不會消耗登入狀態），`probe_whoami` 回 `SERVER_MISCONFIGURED`。
- `PROBE_ALLOWED_GITHUB_IDS` 為空或未設定時**任何人都無法登入**（fail closed）。不在名單內的 GitHub 帳號在 `/callback` 被拒（回 `access_denied` 給 claude.ai），不寫入任何資料；已發出的 token 在每次工具呼叫時也會再檢查名單。

`report-token.txt` 請自行妥善保存，之後查報表要用；不要提交到 git。

## 7. 線上冒煙測試

```bash
ORIGIN=https://gaohe-probe.<你的子網域>.workers.dev
curl -s $ORIGIN/.well-known/oauth-authorization-server | jq '{issuer, authorization_endpoint, registration_endpoint, client_id_metadata_document_supported}'
curl -s $ORIGIN/.well-known/oauth-protected-resource/mcp | jq
curl -si -X POST $ORIGIN/mcp -H 'Content-Type: application/json' -d '{}' | grep -i -E '^HTTP|^www-authenticate'
# 預期：401，WWW-Authenticate 含 resource_metadata="$ORIGIN/.well-known/oauth-protected-resource/mcp"
```

注意：`/.well-known/oauth-protected-resource`（根路徑、不帶 `/mcp`）在 workers-oauth-provider 1.2.4 會回 404，
受保護資源 metadata 只在 `/.well-known/oauth-protected-resource/mcp` 提供（401 的 WWW-Authenticate 會指向這裡）。

## 8. 準備 Skill

1. 編輯 `probe/skill/gaohe-probe/urls.txt`：
   - 把 10 行 `https://example.invalid/replace-me-NN` 換成實際文章網址（建議中央社、公視、聯合新聞網、自由時報、中時新聞網、報導者、ETtoday、鏡週刊、天下、風傳媒各一篇，選一般文字新聞）。
   - 把 `# probe-origin:` 那一行改成你的 `$ORIGIN`（保留開頭的 `#`）。
   - 三次執行、兩個帳號**必須使用同一份清單**。
2. 打包：

   ```bash
   cd probe/skill && ./build-zip.sh     # 產生 gaohe-probe.zip
   ```

## 9. 在 claude.ai 設定（帳號 A 與帳號 B 各做一次）

1. **啟用程式碼執行**：設定 → 功能（Capabilities）→ 開啟「程式碼執行與檔案建立」（介面名稱未確認）。
2. **上傳 Skill**：設定 → 功能 → Skills → 上傳 `gaohe-probe.zip`，並確認已啟用（介面位置未確認）。
3. **加入連接器**：設定 → 連接器（Connectors）→ 新增自訂連接器（Add custom connector）
   - 名稱：`GaoHe probe`
   - 網址：`$ORIGIN/mcp`
   - 按「連接」→ 出現 GaoHe probe 授權頁（會顯示 claude.ai 的回呼網域）→「允許並以 GitHub 登入」→ GitHub 授權頁 → 回到 claude.ai。
   - 帳號 A 用 GitHub 帳號 A 登入；帳號 B 用 GitHub 帳號 B 登入。
4. 只有一個 claude.ai 帳號時：帳號 A 的三次執行全部完成後，在連接器頁面中斷 GaoHe probe、在瀏覽器登出 GitHub、改以 GitHub 帳號 B 登入 GitHub，再重新連接 GaoHe probe。
   這樣 MCP 來源 IP 與抓取行為仍來自同一個 claude.ai 帳號，請在結果表註記「單一 claude.ai 帳號」。

## 10. 執行實測（2 個帳號 × 3 次）

每次執行都開**新對話**，輸入：

```text
請使用 gaohe-probe Skill 執行 P0 實測，run_label=run1
```

- 依序執行 `run1`、`run2`、`run3`；三次之間建議間隔至少 1 小時（量測時間差造成的版本變化），全部在 48 小時內完成。
- **每帳號每個 UTC 日最多 30 次寫入類呼叫**（§9.1；三個 probe 工具都算，`/callback` 不算）。一次執行約 11 次（10 個網址＋`probe_whoami`），
  所以**同一帳號同一 UTC 日最多跑 2 次**，`run3` 排到下一個 UTC 日（台灣時間早上 8 點換日）。超過時工具回 `DAILY_WRITE_LIMIT`，Skill 會停止。
- 帳號 A、帳號 B 各跑三次，共 6 次、約 60 次 `probe_submit_snapshot`／`probe_report_unavailable`。
- 每次工具呼叫時 claude.ai 會顯示工具核准提示，按允許即可。
- 每次結束時，Skill 會建立「GaoHe 連外測試」artifact：**請把畫面上 fetch() 結果（成功／失敗與訊息）以及 window.claude 那一行記到結果表第 6 項**。
- 記下 Skill 輸出的摘要表（含 githubId、MCP IP 是否為 Anthropic 出口）。

另外在**同一時間**開一個終端機量測 CPU：

```bash
cd probe/server
npx wrangler tail gaohe-probe --format json > tail-run1.jsonl
```

tail 事件中每個請求的 CPU 時間欄位名稱（例如 `cpuTime`）依 wrangler 版本而定（未確認）；若沒有，改用 Cloudflare 儀表板
Workers & Pages → gaohe-probe → Metrics 的 CPU time 百分位數（P50／P99），或 Observability 查詢。
Worker 內的 `Date.now()` 在執行中不前進，因此本原型不在程式內量 CPU。

## 11. 取得報表

```bash
curl -s -H "Authorization: Bearer $(cat report-token.txt)" $ORIGIN/probe/report | jq > report.json
jq '.summary' report.json
jq '.urls[] | {url, accounts, submissions, cross: .crossAccountJaccard.median, within: .withinAccountJaccard.median, attestedShare, titleAgreement, bylineAgreement, pass}' report.json
jq '.domains[] | {domain, successRate, reasons}' report.json
jq '.ip, .metrics' report.json
```

報表欄位說明：

- `crossAccountJaccard`：同一網址、**不同帳號**的每一對提交，段落 ID 集合的 Jaccard（交集／聯集）統計。
- `withinAccountJaccard`：同一帳號不同次執行之間的同一統計。
- `attestedShare`：以 `body_codepoints` 取中位長度提交（偶數份時取較長的那份，`medianSubmissionId`／`medianSubmissionCodepoints`）；
  分子為**該份提交中**被 ≥2 個不同帳號提交過的段落 ID 數，分母為該份提交的段落 ID 數，對應 §6.1-6（只出現在較長提交中的樣板段落不會灌高比例）。
- `titleAgreement`／`bylineAgreement`：標題／署名比對鍵雜湊與最多數相同的提交比例。
- `domains[].successRate`：snapshot ÷（snapshot＋unavailable）；`usable` 為 successRate ≥ 0.5。
  reason=`server_rejected`（頁面有抓到，但伺服器以 `EMPTY_BODY`／`TOO_MANY_PARAGRAPHS` 拒收）列在 `reasons`，但**不計入**成功率的分母，`overallFetchSuccessRate` 亦同。
- `ip.mcp`／`ip.oauth`：MCP 請求與 OAuth 瀏覽器請求中屬於 Anthropic 出口（160.79.104.0/21）的比例。
- `metrics`：每個工具（以及 `callback`，即 `/callback` 的 accounts upsert＋OAuth IP 列）自身 SQL 的 Cloudflare D1 `rows_written`／`rows_read`。
  `*InclInstrumentation` 另加上本原型寫 metrics 列本身的成本（INSERT＋UPDATE，實際量得），是 P0 部署上每次呼叫的真實寫入量，
  `summary.d1RowsWrittenTotalInclInstrumentation` 可與儀表板的 D1 寫入總數對帳。`rows_read` 含每日上限的 COUNT 查詢（掃描 metrics 表，會隨列數成長）。
  **KV 寫入（OAuth token 簽發與 refresh）不在報表內**，必須從 Cloudflare 儀表板 Workers KV → OAUTH_KV 的用量取得。

## 12. 結果表（對應 §4.2 第 1–6 項）

| # | 項目 | 量測方式 | 通過門檻 | 實測值 | 結果 |
|---|---|---|---|---|---|
| 1 | Skill＋自訂遠端連接器可用；同一次執行中 Skill 能呼叫連接器工具與內建網頁抓取 | 步驟 9–10 實際操作；記錄方案（Pro／Max／Team）、網頁版或 Desktop | 兩個帳號 6 次執行都完成，且有 snapshot 寫入 | | ☐ 通過 ☐ 不通過 |
| 2 | 比對鍵後段落精確相符率 | `report.json` 的 `urls[].crossAccountJaccard.median`、`urls[].attestedShare`、`summary.urlsPassing` | 每個網址：跨帳號 Jaccard 中位數 ≥ 0.8 **且** ≥ 60% 段落被 ≥2 個帳號提交；**10 個網址中 ≥ 7 個**達標（`summary.item2ParagraphMatchPass`） | 達標網址數：＿／10 | ☐ 通過 ☐ 不通過（→ 啟用 §6.1-4 近似比對或回到設計） |
| 3 | 各新聞網域抓取成功率 | `report.json` 的 `domains[]`、`summary.usableDomains`、`summary.overallFetchSuccessRate` | **≥ 70% 的網域**可用（該網域 successRate ≥ 0.5；`summary.item3FetchPass`） | 可用網域：＿／＿；整體成功率：＿ | ☐ 通過 ☐ 不通過 |
| 4 | MCP 請求來源 IP 是否為 Anthropic 出口 | `report.json` 的 `ip.mcp.share`、`ip.oauth.share`；Skill 摘要的 `mcpRequestIpIsAnthropic` | 預期「是」（`ip.mcp.share` 接近 1、`ip.oauth.share` 為 0）→ 確認 §9.4 只用 OAuth 瀏覽器 IP 作群集訊號。若「否」，回到 §9.4 重新評估 | mcp：＿；oauth：＿ | ☐ 符合預期 ☐ 不符 |
| 5 | CPU 時間、Cloudflare D1 讀寫列數、KV 寫入數 | `wrangler tail`／儀表板 CPU P95；`report.json` 的 `metrics`；儀表板 Cloudflare D1 與 KV 用量 | 每個工具 **p95 CPU < 10 ms**（免費方案上限）；否則註記需要 Workers Paid（約 US$5／月）。**每次 snapshot 的 rows_written ≤ 5**（`summary.item5RowsWrittenPass`）。另記錄 6 次執行合計的 Cloudflare D1 讀寫列數與 KV 寫入數，換算每日 100 個帳號時是否仍在免費額度內 | CPU p95：whoami＿／snapshot＿／unavailable＿ ms；rows_written max：＿；KV 寫入：＿ | ☐ 免費方案可行 ☐ 需 Workers Paid |
| 6 | artifact 能否呼叫外部 API 或觸發新對話 | 每次執行最後的「GaoHe 連外測試」artifact 畫面 | 只記錄是／否（預期「否」，報告改由 Skill 預先嵌入資料，§10.2） | fetch()：☐ 成功 ☐ 失敗（訊息：＿）；window.claude：＿ | ☐ 可連外 ☐ 不可連外 |

本機整合測試量到的參考值（Miniflare 本機 Cloudflare D1，非線上）：`probe_submit_snapshot` 每次 rows_written = 2（資料列＋TEXT 主鍵索引），
`probe_whoami` 與 `probe_report_unavailable` 各 2，`callback` 3；metrics 列本身每次另加 3（INSERT 2＋UPDATE 1），
所以每次工具呼叫在 P0 部署上實際寫 5 列、每次登入 6 列。第 5 項門檻（≤ 5）以工具自身的 `item5RowsWrittenPerSnapshotMax` 判定
（正式版沒有 metrics 表）；另把 `item5RowsWrittenPerSnapshotMaxInclInstrumentation` 與 KV 寫入數記入實測值。線上數值以 `report.json` 為準。

P0 退出條件（§14）：第 2、3 項達可用水準，第 5 項在預算內，第 4 項確認 IP 行為。

## 13. 實測後清理

```bash
npx wrangler delete gaohe-probe
npx wrangler d1 delete gaohe-probe
npx wrangler kv namespace delete --binding OAUTH_KV
```

並在 GitHub 刪除 OAuth App、在 claude.ai 移除連接器與 Skill。匯出的 `report.json` 只含雜湊與計數，可保留作為 P0 紀錄。

## 附註

- Anthropic 出口 IP 範圍 `160.79.104.0/21` 取自 Anthropic 公布的 IP 清單，實測前請再核對一次（`server/src/anthropicEgress.ts`）。只判斷 IPv4。
- §9.1 規則已執行：GitHub 帳號未滿 30 天時可登入與呼叫工具，但不寫入 Cloudflare D1（工具回 `written: false`、`not_written_reason: ACCOUNT_TOO_NEW`）；
  每帳號每 UTC 日最多 30 次寫入類呼叫；另加 P0 專用的 `PROBE_ALLOWED_GITHUB_IDS` 名單。
- §11 IP 訊號保存 90 日：Worker 的每日 cron（`wrangler.jsonc` 的 `triggers.crons`，UTC 03:17）刪除 90 天前的 `ip_observations`。
  實測後依第 13 步刪除整個資料庫。
- 比對鍵也把 ⸺ ⸻ 視為破折號、« » ‹ › 視為引號，VT／FF 視為換行；〃（同上符號）與 ″（雙撇號）不視為引號。
- 段落切分規則：以換行切分、去頭尾空白、丟棄比對鍵為空的行；比對鍵少於 20 個 code point 的段落併入下一段（最後一段過短則併入前一段）。測試向量見 `schemas/normalize-vectors.json`。
