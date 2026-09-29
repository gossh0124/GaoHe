"""Provider-neutral error and live-check types shared by adapters, the pipeline, the CLI and the UI.

Every message here is shown to people who are not engineers, so it is plain zh-TW, says what to
do next, and never contains a key, a raw response body or a request URL.
"""

from dataclasses import dataclass


# code -> default plain-language message
PROVIDER_ERROR_MESSAGES: dict[str, str] = {
    "auth": "AI 服務拒絕了金鑰。請確認金鑰是否貼對、是否已啟用。",
    "quota": "AI 服務的額度或帳單已用完。請到服務商後台確認額度後再試。",
    "rate_limit": "AI 服務暫時限制請求頻率，稍後會自動再試。",
    "model_not_found": "找不到這個模型名稱。請在設定中改選清單裡的模型。",
    "network": "連不上 AI 服務。請確認網路連線後再試。",
    "timeout": "AI 服務回應逾時，稍後會自動再試。",
    "blocked": "AI 服務因安全政策拒絕處理這段內容；這不代表文章有問題。",
    "invalid_response": "AI 服務回傳的格式無法解讀，這次分析會稍後重試。",
    "unavailable": "AI 服務暫時無法使用，稍後會自動再試。",
    "missing_package": "選用的 AI 服務需要額外套件，請重新執行 setup.cmd 安裝。",
    "config": "AI 服務設定不完整，請重新開啟設定精靈。",
    "search_unavailable": "此 AI 服務的網路搜尋未啟用，證據會維持待查證。",
}

# Codes after which every further call in the same run would fail the same way.
RUN_STOPPING_CODES = frozenset({"auth", "quota", "model_not_found", "missing_package", "config"})
# Codes worth retrying later without spending a job attempt.
TRANSIENT_CODES = frozenset({"rate_limit", "network", "timeout", "unavailable"})


class ProviderError(ValueError):
    """A classified, secret-free failure from an AI or search provider.

    Subclasses ValueError so existing `except ValueError` handling keeps working.
    """

    def __init__(self, code: str, message: str | None = None, *, provider: str = "") -> None:
        if code not in PROVIDER_ERROR_MESSAGES:
            code = "unavailable"
        self.code = code
        self.provider = provider
        self.user_message = message or PROVIDER_ERROR_MESSAGES[code]
        super().__init__(self.user_message)

    @property
    def stops_run(self) -> bool:
        return self.code in RUN_STOPPING_CODES

    @property
    def transient(self) -> bool:
        return self.code in TRANSIENT_CODES


@dataclass(frozen=True)
class LiveCheckItem:
    """One line of `gaohe doctor --live` or the setup wizard's key test."""

    name: str  # e.g. "analysis", "search", "source:<id>"
    ok: bool
    code: str  # "ok" or a PROVIDER_ERROR_MESSAGES key or "skipped"
    message: str  # plain zh-TW
