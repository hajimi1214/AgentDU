$ErrorActionPreference = "Stop"

$desktopRoot = Split-Path -Parent $PSScriptRoot
$repositoryRoot = Split-Path -Parent $desktopRoot
$buildRoot = Join-Path $desktopRoot "build"
$cacheRoot = Join-Path $buildRoot "cache"
$runtimeRoot = Join-Path $buildRoot "runtime"
$pythonRoot = Join-Path $runtimeRoot "python"
$archivePath = Join-Path $cacheRoot "python-3.14.10-embed-amd64.zip"
$pythonUrl = "https://www.python.org/ftp/python/3.14.10/python-3.14.10-embed-amd64.zip"
$expectedSha256 = "90905e64a840557d8c1bc4f5b74db3d7ee85e3ffb217611cac1c7813acea4159"

New-Item -ItemType Directory -Path $cacheRoot -Force | Out-Null
if (-not (Test-Path -LiteralPath $archivePath -PathType Leaf)) {
    Invoke-WebRequest -Uri $pythonUrl -OutFile $archivePath
}

$hashAlgorithm = [System.Security.Cryptography.SHA256]::Create()
$archiveStream = [System.IO.File]::OpenRead($archivePath)
try {
    $actualSha256 = -join ($hashAlgorithm.ComputeHash($archiveStream) | ForEach-Object { $_.ToString("x2") })
} finally {
    $archiveStream.Dispose()
    $hashAlgorithm.Dispose()
}
if ($actualSha256 -ne $expectedSha256) {
    throw "Python embeddable package SHA-256 mismatch. Expected $expectedSha256, got $actualSha256."
}

if (Test-Path -LiteralPath $runtimeRoot) {
    $resolvedBuildRoot = [System.IO.Path]::GetFullPath($buildRoot).TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
    $resolvedRuntimeRoot = [System.IO.Path]::GetFullPath($runtimeRoot)
    if (-not $resolvedRuntimeRoot.StartsWith($resolvedBuildRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to replace a runtime directory outside the desktop build directory."
    }
    Remove-Item -LiteralPath $resolvedRuntimeRoot -Recurse -Force
}

New-Item -ItemType Directory -Path $pythonRoot -Force | Out-Null
Expand-Archive -LiteralPath $archivePath -DestinationPath $pythonRoot -Force

$pythonPathFile = Join-Path $pythonRoot "python314._pth"
if (-not (Test-Path -LiteralPath $pythonPathFile -PathType Leaf)) {
    throw "Python embeddable package did not contain python314._pth."
}
$pythonPathLines = @(Get-Content -LiteralPath $pythonPathFile)
if ($pythonPathLines -notcontains ".") {
    throw "Python embeddable package does not include its isolated application directory."
}

$agentduSource = Join-Path $repositoryRoot "src\agentdu"
if (-not (Test-Path -LiteralPath $agentduSource -PathType Container)) {
    throw "AgentDU Runtime package was not found at $agentduSource."
}
$agentduDestination = Join-Path $pythonRoot "agentdu"
New-Item -ItemType Directory -Path $agentduDestination -Force | Out-Null
foreach ($sourceItem in Get-ChildItem -LiteralPath $agentduSource -Force) {
    Copy-Item -LiteralPath $sourceItem.FullName -Destination $agentduDestination -Recurse -Force
}
Get-ChildItem -LiteralPath $agentduDestination -Directory -Filter "__pycache__" -Recurse |
    Remove-Item -Recurse -Force

$pythonExecutable = Join-Path $pythonRoot "python.exe"
$probeOutput = & $pythonExecutable -B -c "import agentdu, sys, tomllib; from agentdu.desktop_host import RuntimeHost; print(sys.version.split()[0]); print(agentdu.__file__); print(sys.flags.isolated)"
if ($LASTEXITCODE -ne 0) {
    throw "The bundled Python Runtime could not import the AgentDU Runtime Host."
}
if ([System.IO.Path]::GetFullPath($probeOutput[1]) -ne [System.IO.Path]::GetFullPath((Join-Path $agentduDestination "__init__.py"))) {
    throw "The bundled Python Runtime loaded AgentDU from an unexpected path."
}
if ($probeOutput[-1] -ne "1") {
    throw "The bundled Python Runtime did not start in isolated mode."
}

$manifest = [ordered]@{
    pythonVersion = "3.14.10"
    architecture = "amd64"
    source = $pythonUrl
    sha256 = $actualSha256
    isolatedPathConfiguration = $true
    runtimePackage = "agentdu"
}
$manifest | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runtimeRoot "runtime-manifest.json") -Encoding utf8
Write-Output "Prepared isolated AgentDU Runtime with Python $($probeOutput[0]) ($actualSha256)."
