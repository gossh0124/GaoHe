"""Static metadata about the AI services GaoHe can talk to.

This module holds facts only (names, default models, help links, capabilities); the
adapters that actually call each service live in gaohe.providers. The setup wizard,
`gaohe doctor` and the adapters all read this table, so adding a provider starts here.
Model identifiers change over time: each entry records when its list was last checked.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderInfo:
    id: str
    label: str
    default_model: str
    suggested_models: tuple[str, ...]
    key_help_url: str
    needs_base_url: bool = False
    supports_web_search: bool = False
    requires_package: str | None = None
    models_checked_on: str = ""
    notes: str = ""


PROVIDERS: dict[str, ProviderInfo] = {
    "gemini": ProviderInfo(
        id="gemini",
        label="Google Gemini",
        default_model="gemini-2.5-flash",
        suggested_models=("gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.5-flash-lite"),
        key_help_url="https://aistudio.google.com/apikey",
        supports_web_search=True,
        models_checked_on="unverified",
        notes="使用 Google Search grounding 搜尋證據來源。",
    ),
    "anthropic": ProviderInfo(
        id="anthropic",
        label="Anthropic Claude",
        default_model="claude-opus-5-5",
        suggested_models=("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5"),
        key_help_url="https://platform.claude.com/settings/keys",
        supports_web_search=True,
        requires_package="anthropic",
        models_checked_on="2026-09-25",
        notes="透過官方 anthropic 套件呼叫；web search 需在 Claude Console 允許。",
    ),
    "openai": ProviderInfo(
        id="openai",
        label="OpenAI",
        default_model="gpt-5-mini",
        suggested_models=("gpt-5-mini", "gpt-5"),
        key_help_url="https://platform.openai.com/api-keys",
        supports_web_search=True,
        models_checked_on="unverified",
    ),
    "openai_compatible": ProviderInfo(
        id="openai_compatible",
        label="OpenAI 相容端點（Ollama、LM Studio 等）",
        default_model="",
        suggested_models=(),
        key_help_url="",
        needs_base_url=True,
        supports_web_search=False,
        notes="需填寫 LLM_BASE_URL，例如 http://127.0.0.1:11434/v1；沒有內建搜尋，證據需另設搜尋服務。",
    ),
}

WEB_SEARCH_CHOICES = ("auto", "none", "gemini", "anthropic", "openai")


def provider_info(provider_id: str) -> ProviderInfo | None:
    return PROVIDERS.get(provider_id)


def resolve_web_search_provider(web_search_provider: str, llm_provider: str) -> str:
    """Map WEB_SEARCH_PROVIDER=auto to the LLM provider's native search, else 'none'."""
    if web_search_provider != "auto":
        return web_search_provider
    info = PROVIDERS.get(llm_provider)
    return llm_provider if info is not None and info.supports_web_search else "none"
