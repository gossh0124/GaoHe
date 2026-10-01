"""Classified, secret-free provider failures with plain zh-TW messages for non-experts."""


PROVIDER_ERROR_MESSAGES: dict[str, str] = {
    "auth": "Gemini 拒絕了金鑰。請確認金鑰是否貼對、是否已啟用。",
    "quota": "Gemini 的額度已用完。請到 Google AI Studio 確認額度後再試。",
    "model_not_found": "找不到這個模型名稱。請重新開啟設定精靈改選模型。",
    "rate_limit": "Gemini 暫時限制請求頻率，下次排程會自動繼續。",
    "network": "連不上 Gemini。請確認網路連線，下次排程會自動繼續。",
    "unavailable": "Gemini 暫時無法使用，下次排程會自動繼續。",
    "timeout": "Gemini 回應逾時，這篇稍後會再試。",
    "blocked": "Gemini 因安全政策拒絕處理這段內容；這不代表文章有問題。",
    "invalid_response": "Gemini 回傳的格式無法解讀，這篇稍後會再試。",
    "config": "AI 設定不完整，請重新開啟設定精靈。",
}

# After these every further call in the same run would fail the same way, so the run stops and
# the article stays queued without using up a retry.
RUN_STOPPING_CODES = frozenset({"auth", "quota", "model_not_found", "rate_limit", "network", "unavailable", "config"})


class ProviderError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code if code in PROVIDER_ERROR_MESSAGES else "unavailable"
        self.user_message = PROVIDER_ERROR_MESSAGES[self.code]
        super().__init__(self.user_message)

    @property
    def stops_run(self) -> bool:
        return self.code in RUN_STOPPING_CODES
