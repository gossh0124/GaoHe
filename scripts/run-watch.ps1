param(
    [string]$PythonPath
)

$ProjectDir = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
if (-not $PythonPath) {
    $PythonPath = Join-Path $ProjectDir ".venv\Scripts\python.exe"
}
$PythonPath = [IO.Path]::GetFullPath($PythonPath)

Push-Location $ProjectDir
try {
    & $PythonPath -m gaohe.cli watch --once
    $watchExit = $LASTEXITCODE
    & $PythonPath -m gaohe.cli analyze --pending
    $analysisExit = $LASTEXITCODE
} finally {
    Pop-Location
}

if ($watchExit -ne 0) {
    exit $watchExit
}
exit $analysisExit
