from pathlib import Path


ROOT = Path(__file__).parents[1]


def _text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_cmd_launchers_resolve_their_own_directory_and_propagate_exit_code():
    for filename, script in (("setup.cmd", "setup.ps1"), ("uninstall.cmd", "uninstall.ps1")):
        text = _text(filename)
        assert "%~dp0" in text
        assert f"scripts\\{script}" in text
        assert '-ProjectDir "%PROJECT_DIR%"' in text
        assert "exit /b %ERRORLEVEL%" in text


def test_setup_script_gates_python_before_creating_a_venv_and_launches_wizard():
    text = _text("scripts/setup.ps1")

    assert "Get-Command py" in text
    assert '& py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"' in text
    assert '$PythonArguments = @("-3")' in text
    assert "py -3.11" not in text
    assert "Get-Command python" in text
    assert "https://www.python.org/downloads/windows/" in text
    assert text.index("if ($null -eq $PythonExecutable)") < text.index("-m venv")
    assert 'Join-Path $ProjectDir ".venv"' in text
    assert 'Join-Path $VenvDir "Scripts\\python.exe"' in text
    assert "-m pip install --no-deps -e" in text
    assert "-m gaohe setup --env-file $EnvFile" in text
    assert "if (-not (Test-Path -LiteralPath $VenvPython" in text


def test_release_scripts_use_utf8_without_bom_and_do_not_emit_secrets():
    for filename in ("scripts/setup.ps1", "scripts/uninstall.ps1"):
        text = _text(filename)
        assert "System.Text.UTF8Encoding($false)" in text
        assert "$OutputEncoding = $Utf8NoBom" in text
        assert "LLM_API_KEY" not in text
        assert "FIRECRAWL_API_KEY" not in text


def test_uninstall_is_idempotent_and_preserves_env_and_data_by_default():
    text = _text("scripts/uninstall.ps1")

    assert '"GaoHe Watch"' in text
    assert 'schtasks.exe /Delete /TN "GaoHe Watch" /F' in text
    assert "if (Test-Path -LiteralPath $VenvDir)" in text
    assert "Remove-Item -LiteralPath $VenvDir -Recurse -Force" in text
    assert "DeleteConfig" in text and "DeleteData" in text
    assert "if ($DeleteConfig)" in text
    assert "if ($DeleteData)" in text


def test_uninstall_requires_typed_confirmation_before_optional_deletion():
    text = _text("scripts/uninstall.ps1")

    assert '"DELETE GAOHE DATA"' in text
    assert "Confirmation" in text
    assert "Resolved configuration path:" in text
    assert "Resolved data path:" in text
    assert text.index("if ($Confirmation -ne $RequiredConfirmation)") < text.index("Remove-Item -LiteralPath $EnvFile")
    assert text.index("if ($Confirmation -ne $RequiredConfirmation)") < text.index("Assert-AllowedDataPath $DataDir")
    assert text.index("Assert-AllowedDataPath $DataDir") < text.index("Remove-Item -LiteralPath $AllowedDataDir")


def test_uninstall_never_targets_the_repository_or_unresolved_broad_paths():
    text = _text("scripts/uninstall.ps1")

    assert "Assert-ProjectLocalPath" in text
    assert "Resolve-Path -LiteralPath $ProjectDir" in text
    assert "Remove-Item -LiteralPath $ProjectDir -Recurse" not in text
    assert "Remove-Item -Recurse -Force $ProjectDir" not in text


def test_uninstall_optional_data_deletion_is_limited_to_controlled_paths_and_not_reparse_points():
    text = _text("scripts/uninstall.ps1")

    assert "Assert-AllowedDataPath" in text
    assert "LocalApplicationData" in text
    assert "StartsWith($resolvedProject" in text
    assert "ReparsePoint" in text
    assert "Assert-AllowedDataPath $DataDir" in text
