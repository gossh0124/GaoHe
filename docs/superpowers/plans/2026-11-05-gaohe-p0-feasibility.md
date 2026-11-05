# GaoHe P0 可行性實測 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 以最小的探測版 Worker 與 Skill，量測規格 §4.2 的六項可行性前提，產出 P0 通過／不通過的判定（§14）。

**Architecture:** Cloudflare Worker（`probe/server`）同時是 OAuth 2.1 授權伺服器（`@cloudflare/workers-oauth-provider`，上游 GitHub 登入）與無狀態 MCP 伺服器（`agents/mcp/server` 的 `createMcpHandler` ＋ MCP SDK v2）。探測用 Skill（`probe/skill/gaohe-probe`）讓使用者的 Claude 抓取文章並呼叫探測工具；伺服器只存段落 ID（sha256）、計數與 HMAC 後的 IP 前綴，報表端點彙整六項量測。

**Tech Stack:** TypeScript 5.9.3、wrangler 4.158.0、@cloudflare/workers-oauth-provider 1.2.4、agents 0.33.0、@modelcontextprotocol/server 2.4.0、zod 4.6.7、vitest 4.1.12、@cloudflare/vitest-plugin 1.6.0、Cloudflare D1、Workers KV。

## Global Constraints

- 規格：`docs/superpowers/specs/2026-11-05-gaohe-skill-connector-design.md`（已核准）。
- 伺服器不存段落全文（§5.1）；title／byline 只存比對鍵的 sha256。
- 字元位置一律以 Unicode code point 計算，不使用 UTF-16 索引（§6.1）。
- 比對鍵：NFKC、移除空白與 Cf 類字元與變體選擇符、引號／破折號／刪節號變體歸一（§6.1-3）；測試向量在 `schemas/normalize-vectors.json`，P2 共用。
- MCP 請求的 IP 不作為群集訊號；OAuth 瀏覽器 IP 以 HMAC（`IP_HMAC_KEY` ≥ 32 bytes）存 IPv4 /24、IPv6 /48 前綴，保存 90 日（§9.4、§11）。
- 以 GitHub 數字 user id 綁定帳號；不保存 GitHub token；GitHub 帳號未滿 30 天不寫入；每帳號每 UTC 日最多 30 次寫入類呼叫（§9.1）。
- 正文 ≤ 30,000 code point、段落 ≤ 200（§6.1-2）。
- 依賴版本精確鎖定（`probe/server/.npmrc`：`save-exact=true`、`legacy-peer-deps=true`，後者用於繞過 npm 10.9 在 agents 選用 peer 相依的 `edgesOut` 錯誤）。
- CI 不呼叫任何 AI 或外部服務。

## 實作說明

MCP SDK 在 2026 年升為 v2，Cloudflare 的 agents 與 OAuth 套件也同步改版。為了確保本計畫的程式碼可編譯、可執行，Task 1–6 已先以原型實作、經獨立審查（13 項缺陷全數修正）並在 repo 位置重新驗證；程式碼即為 repo 中的檔案，本計畫不重複貼出。各 Task 的「驗證」步驟可隨時重跑。Task 7–9 需要使用者自己的 Cloudflare、GitHub 與 claude.ai 帳號，由使用者依 `probe/RUNBOOK.md` 執行。

---

### Task 1: 比對鍵正規化與切段（含共享測試向量）✅

**Files:**
- Create: `schemas/normalize-vectors.json`（version 2：matchKey 案例、切段案例、段落 ID 等價組）
- Create: `probe/server/src/normalize.ts`
- Test: `probe/server/test/unit/normalize.test.ts`

**Interfaces:**
- Produces: `matchKey(s: string): string`、`splitParagraphs(body: string): string[]`、`paragraphId(p: string): Promise<string>`、`sha256Hex(s: string): Promise<string>`、`codePointLength(s: string): number`

- [x] **Step 1:** 寫測試向量與由向量驅動的測試（全形／半形標點、「」與 “”、——／—／–／⸺／⸻、…與 ...、U+200B／U+FEFF／U+00AD、變體選擇符、𠀋、emoji、ｱ、①、混合空白、VT／FF 換行）。
- [x] **Step 2:** 實作；Cf 字元與變體選擇符在 NFKC 前後各移除一次；ー、一、。。。保留。
- [x] **Step 3: 驗證**

Run: `cd probe/server && npx vitest run --config vitest.config.ts test/unit/normalize.test.ts`
Expected: PASS

### Task 2: IP 處理與 Anthropic 出口判斷 ✅

**Files:**
- Create: `probe/server/src/anthropicEgress.ts`（`isAnthropicEgress`、`parseIPv4`、`toIPv4`、`expandIPv6`；160.79.104.0/21，需於實測前再核對）
- Create: `probe/server/src/ip.ts`（`ipPrefix`、`hmacHex`、`ipPrefixHmac`、`timingSafeEqualStr`、`MisconfiguredError`）
- Test: `probe/server/test/unit/anthropicEgress.test.ts`（含 RFC 4231 HMAC 向量、IPv4-mapped IPv6）

- [x] **Step 1–2:** 測試先行、實作。
- [x] **Step 3: 驗證** — Run: `npx vitest run --config vitest.config.ts test/unit/anthropicEgress.test.ts` → PASS

### Task 3: GitHub 上游登入與 OAuth 授權伺服器 ✅

**Files:**
- Create: `probe/server/src/github.ts`（授權網址含 PKCE、空 scope；交換 code；讀 `/user` 的數字 id 與 `created_at`；不保存 token）
- Create: `probe/server/src/auth.ts`（`/authorize` 同意頁顯示實際 redirect 網域、`beginUpstream`／`finishUpstream`、`/callback` 以 `completeAuthorization` 寫入 props；失敗時 `server_error`／`access_denied`）
- Create: `probe/server/src/access.ts`（`PROBE_ALLOWED_GITHUB_IDS` 名單、30 天規則、每日 30 次寫入上限）
- Create: `probe/server/src/provider.ts`、`probe/server/src/index.ts`（每個 origin 建立一個 OAuthProvider，resource = `<origin>/mcp`，LRU 上限 4；啟用 CIMD）
- Create: `probe/server/src/env.ts`、`probe/server/src/url.ts`
- Test: `probe/server/test/unit/github.test.ts`、`probe/server/test/unit/url.test.ts`

- [x] **Step 1–2:** 測試先行、實作。
- [x] **Step 3: 驗證** — Run: `npx vitest run --config vitest.config.ts` → 5 files, 118 passed

### Task 4: 探測用 MCP 工具、D1 schema 與量測 ✅

**Files:**
- Create: `probe/server/migrations/0001_probe.sql`（accounts、snapshots、unavailable、ip_observations、metrics）
- Create: `probe/server/src/probe.ts`（`probe_whoami`、`probe_submit_snapshot`、`probe_report_unavailable`；reason 包含 `server_rejected`）
- Create: `probe/server/src/metrics.ts`（記錄每次呼叫的 D1 rows_read／rows_written，含量測本身的成本）
- Create: `probe/server/src/retention.ts`（每日 cron 刪除 90 天前的 ip_observations）
- Create: `probe/server/wrangler.jsonc`（D1 `DB`、KV `OAUTH_KV`、`nodejs_compat`、`global_fetch_strictly_public`、cron `17 3 * * *`）
- Test: `probe/server/test/integration/worker.test.ts`（workerd：註冊 → 同意 → 模擬 GitHub → callback → token → 三個工具寫入 D1 → 報表；偽造／重放 callback 回 400；主模組只匯出 default）

- [x] **Step 1–2:** 測試先行、實作。
- [x] **Step 3: 驗證** — Run: `npx vitest run --config vitest.workers.config.ts` → 1 file, 22 passed

### Task 5: 報表 ✅

**Files:**
- Create: `probe/server/src/report.ts`（`buildReport`、`jaccard`、`stats`；跨帳號與同帳號 Jaccard、以 body_codepoints 中位長度提交為分母的 attestedShare、標題／署名一致率、網域成功率、IP 比例、每工具寫入量、各項 pass 欄位）
- 路由：`GET /probe/report`（`Authorization: Bearer <REPORT_TOKEN>`，常數時間比對）
- Test: `probe/server/test/unit/report.test.ts`（2 帳號 × 3 次含雜訊）

- [x] **Step 1–2:** 測試先行、實作。
- [x] **Step 3: 驗證** — Run: `npx vitest run --config vitest.config.ts test/unit/report.test.ts` → PASS

### Task 6: 探測 Skill、Runbook、冒煙測試與 CI ✅

**Files:**
- Create: `probe/skill/gaohe-probe/SKILL.md`、`urls.txt`、`artifact-check.html`；`probe/skill/build-zip.sh`
- Create: `probe/RUNBOOK.md`（部署、claude.ai 設定、2 帳號 × 3 次、報表欄位、§4.2 結果表與門檻、清理）
- Create: `probe/server/scripts/smoke-local.sh`（`wrangler dev --local` ＋ curl）
- Create: `.github/workflows/probe-ci.yml`（ubuntu、Node 22：typecheck、unit＋workerd、smoke）
- Modify: `.gitignore`（忽略 `probe/skill/gaohe-probe.zip`）

- [x] **Step 1: 全部驗證**

```bash
cd probe/server
npm ci
npm run typecheck
npm test
PORT=8811 npm run smoke
npx wrangler deploy --dry-run
```

Expected: typecheck 無錯誤；unit 118 passed、integration 22 passed；smoke 全部 PASS；dry-run 成功（約 1.5 MiB，gzip 約 276 KiB）。

- [x] **Step 2: Commit**

```bash
git add schemas probe .github/workflows/probe-ci.yml .gitignore docs/superpowers/plans/2026-11-05-gaohe-p0-feasibility.md
git commit -m "feat: add GaoHe P0 feasibility probe"
```

### Task 7: 部署探測版（使用者執行）

依 `probe/RUNBOOK.md` 步驟 0–7：

- [ ] 建立 GitHub OAuth App（callback `<ORIGIN>/callback`）。
- [ ] `npx wrangler login`；建立 Cloudflare D1 `gaohe-probe` 與 KV `OAUTH_KV`，把 id 填入 `wrangler.jsonc`。
- [ ] 設定 secrets：`GITHUB_CLIENT_ID`、`GITHUB_CLIENT_SECRET`、`IP_HMAC_KEY`（≥ 32 bytes）、`REPORT_TOKEN`、`PROBE_ALLOWED_GITHUB_IDS`。
- [ ] `npm run db:migrate:remote` 與 `npm run deploy`。
- [ ] 以 curl 確認 `/.well-known/oauth-authorization-server` 200、`POST /mcp` 無 token 時 401 並附 `WWW-Authenticate`。

### Task 8: 執行實測（使用者執行）

依 `probe/RUNBOOK.md` 步驟 8–11：

- [ ] 在 `urls.txt` 填入 10 篇台灣新聞網址，打包 Skill。
- [ ] 帳號 A、B 各自在 claude.ai 啟用程式碼執行、上傳 Skill、加入連接器並以 GitHub 登入。
- [ ] 每帳號執行 run1–run3（同一帳號同一 UTC 日最多 2 次），同時以 `wrangler tail` 或儀表板量 CPU。
- [ ] 記錄每次的 artifact 連外測試結果。
- [ ] `curl` 取得 `report.json`。

### Task 9: 判定與紀錄

**Files:**
- Create: `docs/superpowers/results/2026-11-xx-gaohe-p0-results.md`（以實際日期命名）

- [ ] **Step 1:** 把 RUNBOOK §12 結果表填好（第 1–6 項實測值與通過與否），附上 `report.json` 的 `summary` 區段（只含雜湊與計數）。
- [ ] **Step 2:** 判定：第 2、3 項達可用水準、第 5 項在預算內、第 4 項確認 IP 行為 → P0 通過，進入 P1 計畫；第 2 項不通過 → 依規格 §6.1-4 啟用近似比對並重跑第 2 項，仍不通過則回到設計；第 5 項 CPU 超標 → 改採 Workers Paid 並記錄於規格 §1.1-5。
- [ ] **Step 3:** 依 RUNBOOK §13 清理線上資源。
- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/results/
git commit -m "docs: record GaoHe P0 feasibility results"
```

## Self-Review

- 規格 §4.2 六項：1 → Task 8（實際操作）；2 → Task 1、4、5（`crossAccountJaccard`、`attestedShare`）；3 → Task 4、5（`domains[]`）；4 → Task 2、4（`ip.mcp`／`ip.oauth`）；5 → Task 4、5 與 `wrangler tail`／儀表板（CPU、D1、KV）；6 → Task 6（`artifact-check.html`）。
- §14 P0 退出條件 → Task 9 Step 2。
- 未經實機驗證：真實 GitHub 登入（含 GitHub OAuth App 是否接受 PKCE）、真實 Cloudflare 部署、claude.ai 連接器與 Skill 介面文字——均在 Task 7–8 由使用者實測，RUNBOOK 已標示「未確認」。
