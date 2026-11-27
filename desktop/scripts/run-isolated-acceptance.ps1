[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ExecutablePath
)

$ErrorActionPreference = 'Stop'
$profileValue = $env:AGENTDU_DESKTOP_USER_DATA_DIR
if ([string]::IsNullOrWhiteSpace($profileValue)) {
    throw '隔离验收已拒绝启动。请先设置 AGENTDU_DESKTOP_USER_DATA_DIR。'
}
if (-not [System.IO.Path]::IsPathRooted($profileValue)) {
    throw 'AGENTDU_DESKTOP_USER_DATA_DIR 必须是绝对路径。'
}

$resolvedProfile = [System.IO.Path]::GetFullPath($profileValue).TrimEnd('\')
$resolvedExecutable = [System.IO.Path]::GetFullPath($ExecutablePath)
if (-not (Test-Path -LiteralPath $resolvedExecutable -PathType Leaf)) {
    throw "安装版可执行文件不存在。$resolvedExecutable"
}

$installRoot = Split-Path -Parent $resolvedExecutable
$asarPath = Join-Path $installRoot 'resources\app.asar'
$embeddedPython = Join-Path $installRoot 'resources\agentdu-runtime\python\python.exe'
if (-not (Test-Path -LiteralPath $asarPath -PathType Leaf) -or -not (Test-Path -LiteralPath $embeddedPython -PathType Leaf)) {
    throw '目标不是包含 app.asar 和 Embedded Python 的已安装 AgentDU。'
}

$releaseRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\release')).TrimEnd('\')
$releasePrefix = $releaseRoot + '\'
if (-not $resolvedProfile.StartsWith($releasePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw '隔离验收数据目录必须位于 desktop\release 下。'
}

$defaultProfile = [System.IO.Path]::GetFullPath((Join-Path $env:APPDATA 'agentdu-desktop')).TrimEnd('\')
$profilePrefix = $resolvedProfile + '\'
$defaultPrefix = $defaultProfile + '\'
if ($resolvedProfile.Equals($defaultProfile, [System.StringComparison]::OrdinalIgnoreCase) -or
    $resolvedProfile.StartsWith($defaultPrefix, [System.StringComparison]::OrdinalIgnoreCase) -or
    $defaultProfile.StartsWith($profilePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw '隔离验收目录与当前用户 AgentDU profile 重叠，已拒绝启动。'
}

$relativeProfile = [System.IO.Path]::GetRelativePath($releaseRoot, $resolvedProfile)
$currentPath = $releaseRoot
foreach ($segment in $relativeProfile.Split([System.IO.Path]::DirectorySeparatorChar, [System.StringSplitOptions]::RemoveEmptyEntries)) {
    $currentPath = Join-Path $currentPath $segment
    if (Test-Path -LiteralPath $currentPath) {
        $item = Get-Item -LiteralPath $currentPath -Force
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "隔离验收路径包含符号链接或挂载点。$currentPath"
        }
    }
}

$existingInstances = Get-CimInstance Win32_Process -Filter "Name='AgentDU.exe'" |
    Where-Object { $_.ExecutablePath -and [System.IO.Path]::GetFullPath($_.ExecutablePath).Equals($resolvedExecutable, [System.StringComparison]::OrdinalIgnoreCase) }
if ($existingInstances) {
    throw '此安装目录已有 AgentDU 进程。请先正常关闭该安装实例，再运行隔离验收启动器。'
}

if (-not (Test-Path -LiteralPath $resolvedProfile -PathType Container)) {
    New-Item -ItemType Directory -Path $resolvedProfile | Out-Null
}
$markerPath = Join-Path $resolvedProfile '.agentdu-isolated-acceptance'
$markerValue = 'AgentDU isolated acceptance profile v1'
if (Test-Path -LiteralPath $markerPath -PathType Leaf) {
    if ((Get-Content -LiteralPath $markerPath -Raw).Trim() -ne $markerValue) {
        throw '隔离验收目录标记无效，已拒绝启动。'
    }
} else {
    $existingEntries = @(Get-ChildItem -LiteralPath $resolvedProfile -Force)
    if ($existingEntries.Count -gt 0) {
        throw '隔离验收目录已有数据且缺少启动器标记，已拒绝复用。'
    }
    [System.IO.File]::WriteAllText($markerPath, $markerValue, [System.Text.UTF8Encoding]::new($false))
}

$previousProfileValue = $env:AGENTDU_DESKTOP_USER_DATA_DIR
try {
    $env:AGENTDU_DESKTOP_USER_DATA_DIR = $resolvedProfile
    $process = Start-Process -FilePath $resolvedExecutable -WorkingDirectory $installRoot -PassThru
} finally {
    if ($null -eq $previousProfileValue) {
        Remove-Item Env:AGENTDU_DESKTOP_USER_DATA_DIR -ErrorAction SilentlyContinue
    } else {
        $env:AGENTDU_DESKTOP_USER_DATA_DIR = $previousProfileValue
    }
}

[pscustomobject]@{
    ProcessId = $process.Id
    ExecutablePath = $resolvedExecutable
    UserDataDirectory = $resolvedProfile
    IsolationMarker = $markerPath
}
