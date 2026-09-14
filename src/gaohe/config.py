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
    llm_provider: str = "gemini"
    gemini_model: str = "gemini-2.5-flash-lite"
    search_provider: str = "none"
    data_dir: Path = Path("data")
    google_api_key: str | None = field(default=None, repr=False)

    @property
    def has_api_key(self) -> bool:
        return bool(self.google_api_key)


def load_settings(
    env_file: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    file_values = _read_env_file(Path(".env") if env_file is None else env_file)
    merged = dict(file_values)
    merged.update(dict(os.environ if environ is None else environ))

    def value(name: str, default: str) -> str:
        candidate = merged.get(name, "").strip()
        return candidate or default

    key = merged.get("GOOGLE_API_KEY", "").strip() or None
    return Settings(
        llm_provider=value("LLM_PROVIDER", "gemini"),
        gemini_model=value("GEMINI_MODEL", "gemini-2.5-flash-lite"),
        search_provider=value("SEARCH_PROVIDER", "none"),
        data_dir=Path(value("DATA_DIR", "data")),
        google_api_key=key,
    )
