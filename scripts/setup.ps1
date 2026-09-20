param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectDir
)

$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $Utf8NoBom
[Console]::OutputEncoding = $Utf8NoBom
$OutputEncoding = $Utf8NoBom
$ErrorActionPreference = "Stop"

$ProjectDir = [IO.Path]::GetFullPath($ProjectDir)
if (-not (Test-Path -LiteralPath $ProjectDir -PathType Container)) {
    Write-Error "GaoHe project folder was not found. Extract the Release ZIP completely and run setup.cmd again."
    exit 2
}

$PythonExecutable = $null
$PythonArguments = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3.11 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
    if ($LASTEXITCODE -eq 0) {
        $PythonExecutable = "py"
        $PythonArguments = @("-3.11")
    }
}
if ($null -eq $PythonExecutable -and (Get-Command python -ErrorAction SilentlyContinue)) {
    & python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
    if ($LASTEXITCODE -eq 0) {
        $PythonExecutable = "python"
    }
}
if ($null -eq $PythonExecutable) {
    Write-Error "Python 3.11 or newer is required. Install it from https://www.python.org/downloads/windows/ and select 'Add python.exe to PATH', then run setup.cmd again."
    exit 2
}

$VenvDir = [IO.Path]::GetFullPath((Join-Path $ProjectDir ".venv"))
$VenvPython = [IO.Path]::GetFullPath((Join-Path $VenvDir "Scripts\python.exe"))
$EnvFile = [IO.Path]::GetFullPath((Join-Path $ProjectDir ".env"))
if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    & $PythonExecutable @PythonArguments -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

& $VenvPython -m pip install --no-deps -e $ProjectDir
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Push-Location $ProjectDir
try {
    & $VenvPython -m gaohe setup --env-file $EnvFile
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
