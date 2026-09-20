param(
    [string]$PythonPath,
    [string]$EnvFile
)

$ProjectDir = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
if (-not $PythonPath) {
    $PythonPath = Join-Path $ProjectDir ".venv\Scripts\python.exe"
}
$PythonPath = [IO.Path]::GetFullPath($PythonPath)
if (-not $EnvFile) {
    $EnvFile = Join-Path $ProjectDir ".env"
}
$EnvFile = [IO.Path]::GetFullPath($EnvFile)

Push-Location $ProjectDir
try {
    & $PythonPath -m gaohe.cli watch --once --env-file $EnvFile
    $watchExit = $LASTEXITCODE
    & $PythonPath -m gaohe.cli analyze --pending --env-file $EnvFile
    $analysisExit = $LASTEXITCODE
} finally {
    Pop-Location
}

if ($watchExit -ne 0) {
    exit $watchExit
}
exit $analysisExit
