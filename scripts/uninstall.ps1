param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectDir,
    [switch]$DeleteConfig,
    [switch]$DeleteData,
    [string]$Confirmation = ""
)

$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $Utf8NoBom
[Console]::OutputEncoding = $Utf8NoBom
$OutputEncoding = $Utf8NoBom
$ErrorActionPreference = "Stop"
$RequiredConfirmation = "DELETE GAOHE DATA"

function Assert-ProjectLocalPath([string]$Path) {
    $resolvedProject = (Resolve-Path -LiteralPath $ProjectDir).Path.TrimEnd('\\') + '\\'
    $resolvedPath = [IO.Path]::GetFullPath($Path)
    if (-not $resolvedPath.StartsWith($resolvedProject, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing a path outside the GaoHe project folder."
    }
    return $resolvedPath
}

function Get-ConfiguredDataDir([string]$EnvFile) {
    $defaultBase = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
    $candidate = Join-Path $defaultBase "GaoHe"
    if (Test-Path -LiteralPath $EnvFile -PathType Leaf) {
        foreach ($line in Get-Content -LiteralPath $EnvFile -Encoding UTF8) {
            if ($line -match '^\s*DATA_DIR\s*=\s*(.+?)\s*$') {
                $candidate = $Matches[1].Trim().Trim('"').Trim("'")
                break
            }
        }
    }
    if (-not [IO.Path]::IsPathRooted($candidate)) {
        $candidate = Join-Path $ProjectDir $candidate
    }
    $resolved = [IO.Path]::GetFullPath($candidate)
    if ($resolved -eq [IO.Path]::GetPathRoot($resolved)) {
        throw "Refusing to delete a filesystem root."
    }
    return $resolved
}

function Assert-AllowedDataPath([string]$Path) {
    $defaultBase = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
    $defaultData = [IO.Path]::GetFullPath((Join-Path $defaultBase "GaoHe"))
    $resolvedProject = $ProjectDir.TrimEnd('\\') + '\\'
    if ($Path -ne $defaultData -and -not $Path.StartsWith($resolvedProject, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing data deletion outside the controlled GaoHe locations."
    }
    $current = $Path
    while ($true) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Refusing to delete a reparse-point path."
            }
        }
        $parent = [IO.Directory]::GetParent($current)
        if ($null -eq $parent -or $parent.FullName -eq $current) { break }
        $current = $parent.FullName
    }
    return $Path
}

$ProjectDir = (Resolve-Path -LiteralPath $ProjectDir).Path
$EnvFile = Assert-ProjectLocalPath (Join-Path $ProjectDir ".env")
$VenvDir = Assert-ProjectLocalPath (Join-Path $ProjectDir ".venv")
$DataDir = Get-ConfiguredDataDir $EnvFile

& schtasks.exe /Delete /TN "GaoHe Watch" /F 2>$null
if ($LASTEXITCODE -eq 0) { Write-Output "Removed GaoHe Watch scheduled task." }
if (Test-Path -LiteralPath $VenvDir) {
    Remove-Item -LiteralPath $VenvDir -Recurse -Force
    Write-Output "Removed project virtual environment."
}

if ($DeleteConfig -or $DeleteData) {
    if ($DeleteConfig) { Write-Output "Resolved configuration path: $EnvFile" }
    if ($DeleteData) { Write-Output "Resolved data path: $DataDir" }
    if ($Confirmation -ne $RequiredConfirmation) {
        Write-Error "Optional deletion was not performed. Re-run with -Confirmation '$RequiredConfirmation'."
        exit 2
    }
    if ($DeleteConfig -and (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
        Remove-Item -LiteralPath $EnvFile -Force
    }
    if ($DeleteData -and (Test-Path -LiteralPath $DataDir -PathType Container)) {
        $AllowedDataDir = Assert-AllowedDataPath $DataDir
        Remove-Item -LiteralPath $AllowedDataDir -Recurse -Force
    }
}
