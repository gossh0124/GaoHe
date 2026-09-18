import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


def _read_env_file(path: Path | None) -> dict[str, str]:
    if path is None or not path.is_file():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key.strip():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


@dataclass(frozen=True)
class Settings:
    llm_provider: str = ""
    llm_model: str = ""
    llm_api_key: str = field(default="", repr=False)
    web_search_provider: str = "none"
    firecrawl_api_key: str = field(default="", repr=False)
    data_dir: Path = field(default_factory=lambda: Path.home() / "AppData" / "Local" / "GaoHe")
    poll_interval_minutes: int = 60

    @property
    def database_path(self) -> Path:
        return self.data_dir / "gaohe.db"

    @property
    def has_llm_key(self) -> bool:
        return bool(self.llm_api_key)

    @property
    def has_firecrawl_key(self) -> bool:
        return bool(self.firecrawl_api_key)

    def validate(self) -> list[str]:
        missing: list[str] = []
        if not self.llm_provider:
            missing.append("LLM_PROVIDER is required")
        if not self.llm_model:
            missing.append("LLM_MODEL is required")
        if not self.has_llm_key:
            missing.append("LLM_API_KEY is required")
        if self.web_search_provider == "firecrawl" and not self.has_firecrawl_key:
            missing.append("FIRECRAWL_API_KEY is required when WEB_SEARCH_PROVIDER=firecrawl")
        return missing


def load_settings(
    env_file: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    file_values = _read_env_file(Path(".env") if env_file is None else env_file)
    merged = dict(file_values)
    merged.update(dict(os.environ if environ is None else environ))

    def value(name: str, default: str, legacy_name: str | None = None) -> str:
        candidate = merged.get(name, "").strip()
        if not candidate and legacy_name:
            candidate = merged.get(legacy_name, "").strip()
        return candidate or default

    interval_value = value("POLL_INTERVAL_MINUTES", "60")
    try:
        poll_interval_minutes = int(interval_value)
    except ValueError as error:
        raise ValueError("POLL_INTERVAL_MINUTES must be a positive integer") from error
    if poll_interval_minutes <= 0:
        raise ValueError("POLL_INTERVAL_MINUTES must be a positive integer")

    data_dir = value(
        "DATA_DIR",
        str(Path(merged.get("LOCALAPPDATA", "").strip() or Path.home() / "AppData" / "Local") / "GaoHe"),
    )
    return Settings(
        llm_provider=value("LLM_PROVIDER", ""),
        llm_model=value("LLM_MODEL", "", "GEMINI_MODEL"),
        llm_api_key=value("LLM_API_KEY", "", "GOOGLE_API_KEY"),
        web_search_provider=value("WEB_SEARCH_PROVIDER", "none", "SEARCH_PROVIDER"),
        firecrawl_api_key=value("FIRECRAWL_API_KEY", ""),
        data_dir=Path(data_dir),
        poll_interval_minutes=poll_interval_minutes,
    )
