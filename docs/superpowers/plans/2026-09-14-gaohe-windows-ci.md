# GaoHe Windows Local CI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (\`- [ ]\`) syntax for tracking.

**Goal:** Establish a small, installable GaoHe Python baseline that runs natively on Windows and is automatically tested by GitHub Actions, without adding CD or a local LLM.

**Architecture:** Keep the local runtime thin: a Python package exposes a \`gaohe\` CLI, environment parsing is isolated in \`config.py\`, and a standard-library status page proves the local process can serve HTTP. The Gemini provider remains a later product-layer concern and is represented only by safe configuration fields in this plan. CI installs the package on one Windows runner and runs deterministic pytest tests without API secrets.

**Tech Stack:** Python 3.11+, \`setuptools\`, \`argparse\`, \`dataclasses\`, \`pathlib\`, \`http.server\`, \`pytest\`, GitHub Actions.

**Spec:** \`docs/superpowers/specs/2026-09-14-gaohe-windows-local-ci-design.md\`

## Global Constraints

- Windows 原生 PowerShell 是本機執行環境；不加入 WSL、Docker 或雲端 worker。
- 遠端 LLM 維持 BYOK Gemini；預設 \`gemini-2.5-flash-lite\` 與 \`SEARCH_PROVIDER=none\`。
- 金鑰只存在 \`.env\` 或使用者設定的 secret；不得進 Git 或測試輸出。
- CI 使用單一 \`windows-latest\` runner 與 Python 3.11，不呼叫真實 API。
- CD 暫緩；不建立部署平台、部署 secret 或自動部署 job。
- 目前沒有可恢復的既有遠端 GaoHe 程式碼；新增程式須視為依規格建立的新基線。
- v0／v1／v2 的新聞分析功能不在本計畫內；status UI 不是 v2 同題對讀 UI。
- 每個行為變更遵守 TDD：先寫測試、確認測試以正確原因失敗，再寫最小實作。
- Worktree prerequisite: the planning checkout contains `.worktrees/` in `.gitignore`; Task 4 expands this bootstrap rule into the complete repository ignore list.

---

### Task 1: 建立可安裝 Python package 與 CLI 版本命令

**Files:**
- Create: \`pyproject.toml\`
- Create: \`src/gaohe/__init__.py\`
- Create: \`src/gaohe/cli.py\`
- Test: \`tests/test_cli.py\`

**Interfaces:**
- Produces \`gaohe.__version__: str = "0.1.0"\`.
- Produces \`gaohe.cli.main(argv: Sequence[str] | None = None) -> int\`.
- \`main(["--version"])\` prints \`gaohe 0.1.0\` and returns \`0\`.
- The installed console script is \`gaohe\`.

- [ ] **Step 1: Write the failing CLI test**

```
from gaohe.cli import main


def test_version_command_prints_package_version(capsys):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out == "gaohe 0.1.0\n"
```

- [ ] **Step 2: Run the focused test and verify the expected failure**

Run from the worktree:

```
py -3.11 -m pytest tests/test_cli.py::test_version_command_prints_package_version -q
```

Expected: FAIL because the \`gaohe\` package does not exist yet. If collection fails for a different reason, correct the test setup and rerun until the missing-package failure is observed.

- [ ] **Step 3: Add the minimal package and entry point**

Create \`src/gaohe/__init__.py\`:

```
__version__ = "0.1.0"
```

Create \`src/gaohe/cli.py\`:

```
from collections.abc import Sequence

from . import __version__


def main(argv: Sequence[str] | None = None) -> int:
    if argv is None:
        import sys

        argv = sys.argv[1:]
    if list(argv) == ["--version"]:
        print(f"gaohe {__version__}")
        return 0
    print("usage: gaohe --version")
    return 0
```

Create \`pyproject.toml\`:

```
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "gaohe"
version = "0.1.0"
description = "Taiwan-focused BYOK media post-publication verification tool"
requires-python = ">=3.11"
dependencies = []

[project.optional-dependencies]
dev = ["pytest>=8,<9"]

[project.scripts]
gaohe = "gaohe.cli:main"

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 4: Run the focused test and the installed console script**

```
py -3.11 -m pip install -e ".[dev]"
py -3.11 -m pytest tests/test_cli.py::test_version_command_prints_package_version -q
gaohe --version
```

Expected: the focused test passes, the command prints \`gaohe 0.1.0\`, and all commands exit with code \`0\`.

- [ ] **Step 5: Commit the package baseline**

```
git add pyproject.toml src/gaohe tests/test_cli.py
git commit -m "chore: bootstrap Python package"
```

### Task 2: Add safe Windows environment loading and \`doctor\`

**Files:**
- Create: \`src/gaohe/config.py\`
- Modify: \`src/gaohe/cli.py\`
- Test: \`tests/test_config.py\`

**Interfaces:**
- Produces \`gaohe.config.Settings\` with fields \`llm_provider\`, \`gemini_model\`, \`search_provider\`, \`data_dir\`, and a non-repr \`google_api_key\`.
- Produces \`gaohe.config.load_settings(env_file: Path | None = None, environ: Mapping[str, str] | None = None) -> Settings\`.
- \`load_settings\` reads simple \`KEY=VALUE\` lines, never overrides an explicitly supplied environment value, and ignores blank/comment lines.
- \`main(["doctor", "--env-file", path])\` prints only safe configuration summaries and never prints the API key.

- [ ] **Step 1: Write failing configuration tests**

```
from pathlib import Path

from gaohe.config import load_settings


def test_free_test_defaults_do_not_require_a_key(tmp_path: Path):
    settings = load_settings(tmp_path / ".env", {})

    assert settings.llm_provider == "gemini"
    assert settings.gemini_model == "gemini-2.5-flash-lite"
    assert settings.search_provider == "none"
    assert settings.google_api_key is None


def test_explicit_environment_wins_and_key_is_not_repr(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "GEMINI_MODEL=file-model\nGOOGLE_API_KEY=file-secret\n",
        encoding="utf-8",
    )

    settings = load_settings(
        env_file,
        {"GEMINI_MODEL": "env-model", "GOOGLE_API_KEY": "env-secret"},
    )

    assert settings.gemini_model == "env-model"
    assert settings.google_api_key == "env-secret"
    assert "env-secret" not in repr(settings)
```

- [ ] **Step 2: Run the focused tests and verify the expected failure**

```
py -3.11 -m pytest tests/test_config.py -q
```

Expected: FAIL during collection because \`gaohe.config\` does not exist. Fix only test setup errors if present, then rerun until the missing-module failure is observed.

- [ ] **Step 3: Implement the minimal settings loader**

Create \`src/gaohe/config.py\` with a frozen dataclass, \`repr=False\` on the key field, defaults matching the free-test decision, and a parser that accepts only the simple \`.env\` syntax used by \`.env.example\`. Use \`Path\` for \`data_dir\`, defaulting to \`Path("data")\`. When \`environ\` is supplied, copy it before merging file values so caller-owned mappings are not mutated. Apply explicit environment values after file values.

- [ ] **Step 4: Add the \`doctor\` command without exposing secrets**

Extend \`cli.py\` with an \`argparse\` parser and a \`doctor\` subcommand accepting \`--env-file\` (default \`.env\`). Print exactly these safe fields: \`llm_provider\`, \`gemini_model\`, \`search_provider\`, \`data_dir\`, and \`google_api_key=present|missing\`. Return \`0\` for a readable or absent \`.env\`; actual API-key validation belongs to the future provider call.

- [ ] **Step 5: Run configuration and CLI verification**

```
py -3.11 -m pytest tests/test_config.py tests/test_cli.py -q
gaohe doctor --env-file .env.example
```

Expected: all focused tests pass; the doctor output contains \`gemini-2.5-flash-lite\`, \`search_provider=none\`, and \`google_api_key=missing\`, with no secret value.

- [ ] **Step 6: Commit the configuration contract**

```
git add src/gaohe/config.py src/gaohe/cli.py tests/test_config.py tests/test_cli.py
git commit -m "feat: add safe runtime configuration"
```

### Task 3: Add a local-only status UI

**Files:**
- Create: \`src/gaohe/web.py\`
- Modify: \`src/gaohe/cli.py\`
- Test: \`tests/test_web.py\`

**Interfaces:**
- Produces \`gaohe.web.render_status_page(settings: Settings) -> str\`.
- Produces \`gaohe.web.serve(host: str = "127.0.0.1", port: int = 8000, env_file: Path = Path(".env")) -> None\`.
- \`serve\` uses only \`http.server\`, binds to loopback by default, and serves the status page at \`/\`.
- The page identifies the local runtime and displays safe configuration only; it is not the v2 media comparison UI.

- [ ] **Step 1: Write the failing page-rendering test**

```
from pathlib import Path

from gaohe.config import Settings
from gaohe.web import render_status_page


def test_status_page_contains_safe_runtime_state_without_key():
    settings = Settings(
        llm_provider="gemini",
        gemini_model="gemini-2.5-flash-lite",
        search_provider="none",
        data_dir=Path("data"),
        google_api_key="do-not-render",
    )

    page = render_status_page(settings)

    assert "GaoHe local runtime" in page
    assert "gemini-2.5-flash-lite" in page
    assert "do-not-render" not in page
```

- [ ] **Step 2: Run the focused test and verify the expected failure**

```
py -3.11 -m pytest tests/test_web.py -q
```

Expected: FAIL during collection because \`gaohe.web\` does not exist.

- [ ] **Step 3: Implement the standard-library status page and server**

Use \`html.escape\` for displayed values. Implement a \`BaseHTTPRequestHandler\` that returns status \`200\`, \`Content-Type: text/html; charset=utf-8\`, and the rendered page for \`/\`; return \`404\` for other paths. Construct \`ThreadingHTTPServer((host, port), handler)\` and call \`serve_forever()\` until the process receives the normal keyboard interrupt. Do not add a web framework.

- [ ] **Step 4: Add and verify \`gaohe serve\`**

Extend the CLI with \`serve --host\`, \`--port\`, and \`--env-file\`, passing parsed values to \`serve\`.

```
py -3.11 -m pytest tests/test_web.py tests/test_config.py tests/test_cli.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit the local status UI**

```
git add src/gaohe/web.py src/gaohe/cli.py tests/test_web.py
git commit -m "feat: add local status page"
```

### Task 4: Add Windows setup documentation and repository guardrails

**Files:**
- Create: \`.gitignore\`
- Create: \`.env.example\`
- Create: \`README.md\`

- [ ] **Step 1: Add the repository ignore rules**

\`.gitignore\` must include:

```
.env
.venv/
.worktrees/
__pycache__/
*.py[cod]
.pytest_cache/
.mypy_cache/
data/
snapshots/
reports/
*.sqlite
*.sqlite3
```

- [ ] **Step 2: Add the safe environment template**

\`.env.example\` must contain names and free-test defaults only:

```
LLM_PROVIDER=gemini
GEMINI_MODEL=gemini-2.5-flash-lite
SEARCH_PROVIDER=none
GOOGLE_API_KEY=
DATA_DIR=data
```

- [ ] **Step 3: Document the Windows workflow and project boundary**

\`README.md\` must document, in Traditional Chinese, the project positioning as post-publication media verification rather than an official rumor-truth service; BYOK and user-paid API costs; Windows PowerShell setup; \`gaohe --version\`, \`gaohe doctor\`, and \`gaohe serve\`; the free-test defaults; the fact that CI does not call real APIs; the fact that the current code is a new baseline because the earlier remote source was unavailable; and that CD is not configured.

- [ ] **Step 4: Verify repository hygiene**

```
git diff --check
git check-ignore -q .env .venv .worktrees data reports
```

Expected: both commands exit \`0\`; no secret-like value appears in tracked files.

- [ ] **Step 5: Commit repository documentation and guardrails**

```
git add .gitignore .env.example README.md
git commit -m "docs: document Windows setup and repository rules"
```

### Task 5: Add GitHub Actions CI

**Files:**
- Create: \`.github/workflows/ci.yml\`

**Interfaces:**
- Every branch push and every pull request targeting \`main\` runs the \`test\` job.
- The job uses \`windows-latest\`, Python \`3.11\`, project dev extras, and \`python -m pytest -q\`.
- The workflow has \`contents: read\` permission and no API secret.

- [ ] **Step 1: Add the minimal workflow**

Create \`.github/workflows/ci.yml\`:

```
name: CI

on:
  push:
  pull_request:
    branches: [main]

permissions:
  contents: read

jobs:
  test:
    runs-on: windows-latest
    steps:
      - name: Check out source
        uses: actions/checkout@v4
      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: pip
      - name: Install package and test dependencies
        run: python -m pip install -e ".[dev]"
      - name: Run tests
        run: python -m pytest -q
```

- [ ] **Step 2: Check the workflow and full local suite**

```
git diff --check
py -3.11 -m pytest -q
```

Expected: no whitespace errors and the complete local suite passes. If a YAML linter is available in the environment, run it; otherwise use the GitHub Actions run as the authoritative workflow syntax check.

- [ ] **Step 3: Commit the CI workflow**

```
git add .github/workflows/ci.yml
git commit -m "ci: run Python tests on Windows"
```

### Task 6: Verify the actual Windows path and synchronize the feature branch

**Files:**
- Modify: none beyond the files above

- [ ] **Step 1: Create the isolated execution worktree**

From the clean planning checkout, confirm \`.worktrees/\` is ignored, then create the feature worktree and branch:

```
git check-ignore -q .worktrees
git worktree add '.worktrees\\gaohe-windows-ci' -b 'codex/gaohe-windows-ci' main
```

Run all remaining commands from \`C:\\Users\\berhe\\Documents\\ChatGPT\\GaoHe\\.worktrees\\gaohe-windows-ci\`.

- [ ] **Step 2: Recreate the local environment from scratch**

```
py -3.11 -m venv .venv
& .\\.venv\\Scripts\\python.exe -m pip install -e ".[dev]"
& .\\.venv\\Scripts\\python.exe -m pytest -q
& .\\.venv\\Scripts\\python.exe -m gaohe --version
& .\\.venv\\Scripts\\python.exe -m gaohe doctor --env-file .env.example
```

Expected: install exits \`0\`, the full suite passes, version output is \`gaohe 0.1.0\`, doctor shows the free-test defaults and \`google_api_key=missing\` without exposing a value.

- [ ] **Step 3: Exercise the local HTTP path**

Start the server using \`Start-Process -WindowStyle Hidden\`, save the returned process id, request \`http://127.0.0.1:8765/\` with \`Invoke-WebRequest\`, assert status \`200\` and the \`GaoHe local runtime\` marker, then stop only that saved process id. Record the response status and body marker in the handoff; do not leave a server process running.

- [ ] **Step 4: Run final local hygiene checks**

```
git diff --check
git status --short
git ls-files | Select-String -Pattern '(^|/)(\\.env|\\.venv|data/|snapshots/|reports/)' -CaseSensitive:$false
```

Expected: no diff-check output, only intended tracked files, and no output from the secret/local-artifact scan.

- [ ] **Step 5: Push the feature branch and verify GitHub Actions**

```
git push -u origin codex/gaohe-windows-ci
gh run list --repo gossh0124/GaoHe --branch codex/gaohe-windows-ci --limit 1
gh run watch <run-id> --repo gossh0124/GaoHe --exit-status
```

Expected: the pushed commit is visible on GitHub and the Windows CI run exits successfully. If the run fails, stop and fix the reported failure before reporting completion; do not merge the branch automatically.

- [ ] **Step 6: Report the evidence boundary**

Report the feature branch, commit SHAs, local pytest count, local CLI and HTTP smoke results, and GitHub Actions run URL. Explicitly state that CD and the v0/v1/v2 media-analysis pipeline remain outside this plan.
