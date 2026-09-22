[CmdletBinding()]
param(
    [string]$Installer,
    [switch]$Quick,
    [switch]$Visible,
    [switch]$KeepStaging
)

$ErrorActionPreference = 'Stop'

if ($env:OS -ne 'Windows_NT') {
    throw 'Windows Sandbox QA can only run on Windows.'
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = (Resolve-Path (Join-Path $scriptDir '..\..')).Path
$sandboxExe = Join-Path $env:WINDIR 'System32\WindowsSandbox.exe'
$windowsEdition = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' -ErrorAction SilentlyContinue).EditionID
if ($windowsEdition -in @('Core', 'CoreN', 'CoreSingleLanguage', 'CoreCountrySpecific')) {
    throw 'Windows Sandbox is not supported on Windows Home. Use Windows Pro/Enterprise/Education or run the clean-install QA on a separate VM/GitHub Actions runner.'
}
if (-not (Test-Path -LiteralPath $sandboxExe)) {
    throw 'Windows Sandbox is not installed. Enable Containers-DisposableClientVM first.'
}

if (-not $Installer) {
    $candidate = Get-ChildItem -Path (Join-Path $repoRoot 'desktop\release') -Filter '*Setup*.exe' -Recurse -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if (-not $candidate) {
        throw 'No Campfire Setup executable was found. Build it first or pass -Installer.'
    }
    $Installer = $candidate.FullName
}
$installerPath = (Resolve-Path -LiteralPath $Installer).Path

$tempRoot = [IO.Path]::GetFullPath($env:TEMP).TrimEnd('\')
$staging = Join-Path $tempRoot ("CampfireSandboxQA-{0}" -f [guid]::NewGuid().ToString('N'))
$resultsRoot = Join-Path $scriptDir 'test-results'
$resultRun = Join-Path $resultsRoot (Get-Date -Format 'yyyyMMdd-HHmmss')
New-Item -ItemType Directory -Path $staging, $resultRun -Force | Out-Null

$sandboxProcess = $null
try {
    Copy-Item -LiteralPath $installerPath -Destination (Join-Path $staging 'Campfire-Setup.exe')
    Copy-Item -LiteralPath (Join-Path $scriptDir 'inside-sandbox.ps1') -Destination $staging

    $quickArg = if ($Quick) { ' -Quick' } else { '' }
    $escapedHostFolder = [Security.SecurityElement]::Escape($staging)
    $logonCommand = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\CampfireQA\inside-sandbox.ps1 -Installer C:\CampfireQA\Campfire-Setup.exe$quickArg"
    $escapedCommand = [Security.SecurityElement]::Escape($logonCommand)
    $config = @"
<Configuration>
  <MappedFolders>
    <MappedFolder>
      <HostFolder>$escapedHostFolder</HostFolder>
      <SandboxFolder>C:\CampfireQA</SandboxFolder>
      <ReadOnly>false</ReadOnly>
    </MappedFolder>
  </MappedFolders>
  <Networking>Enable</Networking>
  <ClipboardRedirection>Disable</ClipboardRedirection>
  <LogonCommand>
    <Command>$escapedCommand</Command>
  </LogonCommand>
</Configuration>
"@
    $configPath = Join-Path $staging 'campfire-qa.wsb'
    Set-Content -LiteralPath $configPath -Value $config -Encoding UTF8

    $startArgs = @{
        FilePath = $sandboxExe
        ArgumentList = @("`"$configPath`"")
        PassThru = $true
    }
    if (-not $Visible) { $startArgs.WindowStyle = 'Hidden' }
    $sandboxProcess = Start-Process @startArgs

    $resultPath = Join-Path $staging 'result.json'
    $timeoutMinutes = if ($Quick) { 10 } else { 45 }
    $deadline = (Get-Date).AddMinutes($timeoutMinutes)
    while (-not (Test-Path -LiteralPath $resultPath) -and (Get-Date) -lt $deadline) {
        if ($sandboxProcess.HasExited) {
            throw "Windows Sandbox exited before producing a result (exit $($sandboxProcess.ExitCode))."
        }
        Start-Sleep -Seconds 2
        $sandboxProcess.Refresh()
    }
    if (-not (Test-Path -LiteralPath $resultPath)) {
        throw "Windows Sandbox QA timed out after $timeoutMinutes minutes."
    }

    foreach ($artifact in @('result.json', 'sandbox.log', 'file-response.json')) {
        $artifactPath = Join-Path $staging $artifact
        if (Test-Path -LiteralPath $artifactPath) {
            Copy-Item -LiteralPath $artifactPath -Destination $resultRun -Force
        }
    }
    $result = Get-Content -Raw -LiteralPath $resultPath | ConvertFrom-Json
    $result | ConvertTo-Json -Depth 10
    Write-Host "QA artifacts: $resultRun"
    if ($result.status -ne 'passed') {
        throw "Windows Sandbox QA failed: $($result.error)"
    }
}
finally {
    if ($sandboxProcess -and -not $sandboxProcess.HasExited) {
        Stop-Process -Id $sandboxProcess.Id -Force -ErrorAction SilentlyContinue
        $sandboxProcess.WaitForExit(10000) | Out-Null
    }
    foreach ($artifact in @('result.json', 'sandbox.log', 'file-response.json')) {
        $artifactPath = Join-Path $staging $artifact
        if (Test-Path -LiteralPath $artifactPath) {
            Copy-Item -LiteralPath $artifactPath -Destination $resultRun -Force -ErrorAction SilentlyContinue
        }
    }
    if (-not $KeepStaging -and (Test-Path -LiteralPath $staging)) {
        $resolvedStaging = [IO.Path]::GetFullPath($staging)
        $allowedPrefix = "$tempRoot\CampfireSandboxQA-"
        if (-not $resolvedStaging.StartsWith($allowedPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove unexpected staging path: $resolvedStaging"
        }
        Remove-Item -LiteralPath $resolvedStaging -Recurse -Force
    }
}
