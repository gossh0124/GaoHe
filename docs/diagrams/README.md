# 稿核運作流程圖

以 [Archify](https://github.com/tt-a1i/archify) 繪製，直接用瀏覽器開啟 HTML 即可（可縮放、搜尋節點、匯出圖片）。每個節點標有「來源」，連到 commit `a29ce42` 的實際程式碼行。

| 圖 | 內容 |
|---|---|
| [gaohe-workflow/gaohe-workflow.html](gaohe-workflow/gaohe-workflow.html) | 運作流程：排程或貼網址 → 監測抓取 → 文章版本 → Gemini 抽說法 → Google 搜尋 → 抓來源全文 → 判讀 → 可見性把關 → 本機結果頁，以及失敗重試與停止 |
| [gaohe-lifecycle/gaohe-lifecycle.html](gaohe-lifecycle/gaohe-lifecycle.html) | 一篇文章的分析生命週期：等待分析、分析中、完成、改稿重新分析、本次停止（不扣次數）、失敗重試、失敗 3 次 |

- `candidate.json` 是圖的原始描述，修改後執行 `node <archify>/bin/archify.mjs finalize <type> candidate.json <輸出.html> --repo-root . --quality showcase`。
- `review-2/` 內是最後一次 finalize 的自動檢查紀錄（validate、deliver、check、browser-check 皆通過）。
- 圖中介面文字（按鈕、選單）的繁體中文翻譯由簡體中文自動轉換後人工調整用語，尚未經人工完整校對。
