[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Installer,
    [switch]$Quick
)

$ErrorActionPreference = 'Stop'
$workDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$resultPath = Join-Path $workDir 'result.json'
$transcriptPath = Join-Path $workDir 'sandbox.log'
$appProcess = $null
$report = [ordered]@{
    status = 'failed'
    mode = if ($Quick) { 'quick' } else { 'full' }
    os = [Environment]::OSVersion.VersionString
    architecture = $env:PROCESSOR_ARCHITECTURE
    installer = $null
    signature = $null
    health = $null
    modelsReady = $false
    promptMasking = $false
    fileMasking = $false
    uninstall = $false
    error = $null
}

function Stop-AppTree {
    param([Diagnostics.Process]$Process)
    if (-not $Process -or $Process.HasExited) { return }
    & taskkill.exe /PID $Process.Id /T /F | Out-Null
}

Start-Transcript -LiteralPath $transcriptPath -Force | Out-Null
try {
    $installerInfo = Get-Item -LiteralPath $Installer
    $report.installer = @{
        name = $installerInfo.Name
        bytes = $installerInfo.Length
        sha256 = (Get-FileHash -LiteralPath $Installer -Algorithm SHA256).Hash
    }

    $install = Start-Process -FilePath $Installer -ArgumentList '/S' -Wait -PassThru
    if ($install.ExitCode -ne 0) { throw "Installer exit code: $($install.ExitCode)" }

    $appExe = Join-Path $env:LOCALAPPDATA 'Programs\Campfire\Campfire.exe'
    $installDeadline = (Get-Date).AddMinutes(3)
    while (-not (Test-Path -LiteralPath $appExe) -and (Get-Date) -lt $installDeadline) {
        Start-Sleep -Seconds 1
    }
    if (-not (Test-Path -LiteralPath $appExe)) { throw "Installed app not found: $appExe" }

    $signature = Get-AuthenticodeSignature -LiteralPath $appExe
    $report.signature = @{ status = [string]$signature.Status; subject = [string]$signature.SignerCertificate.Subject }

    $appProcess = Start-Process -FilePath $appExe -WindowStyle Hidden -PassThru
    $port = $null
    $healthDeadline = (Get-Date).AddMinutes(5)
    while ($null -eq $port -and (Get-Date) -lt $healthDeadline) {
        foreach ($candidate in 48200..48209) {
            try {
                $health = Invoke-RestMethod -Uri "http://127.0.0.1:$candidate/health" -TimeoutSec 2 -UseBasicParsing
                $port = $candidate
                $report.health = $health
                break
            } catch {}
        }
        if ($null -eq $port) { Start-Sleep -Seconds 2 }
    }
    if ($null -eq $port) { throw 'Bundled engine did not become healthy on ports 48200-48209.' }

    if (-not $Quick) {
        $modelsDeadline = (Get-Date).AddMinutes(30)
        while (-not $report.modelsReady -and (Get-Date) -lt $modelsDeadline) {
            try {
                $models = Invoke-RestMethod -Uri "http://127.0.0.1:$port/models/status" -TimeoutSec 5 -UseBasicParsing
                $report.modelsReady = [bool]($models.pii.ready -and $models.injection.ready)
            } catch {}
            if (-not $report.modelsReady) { Start-Sleep -Seconds 10 }
        }
        if (-not $report.modelsReady) { throw 'Models did not become ready within 30 minutes.' }

        $promptSecret = 'ghp_16C7e42F292c6912E7710c838347Ae178B4a'
        $prompt = Invoke-RestMethod -Uri "http://127.0.0.1:$port/jobs/prompt" -Method Post -Body @{ text = "GITHUB_TOKEN=$promptSecret" } -TimeoutSec 420 -UseBasicParsing
        $report.promptMasking = [bool](
            $prompt.done -and
            $prompt.result.scanStatus -eq 'ok' -and
            @($prompt.result.piiItems).Count -gt 0 -and
            -not ([string]$prompt.result.maskedText).Contains($promptSecret)
        )
        if (-not $report.promptMasking) { throw 'Prompt masking verification failed.' }

        $fileSecret = 'ghp_9A8b7C6d5E4f3A2b1C0d9E8f7A6b5C4d3E2f'
        $fixture = Join-Path $workDir 'fixture.html'
        Set-Content -LiteralPath $fixture -Value "<html><body>GITHUB_TOKEN=$fileSecret</body></html>" -Encoding UTF8
        $responsePath = Join-Path $workDir 'file-response.json'
        & curl.exe -sS --fail --max-time 420 `
            -F "file=@$fixture;type=text/html" `
            -F 'mimeType=text/html' `
            -F 'fileName=fixture.html' `
            -F 'userPrompt=Summarize this document' `
            -o $responsePath `
            "http://127.0.0.1:$port/jobs"
        if ($LASTEXITCODE -ne 0) { throw "File API curl exit code: $LASTEXITCODE" }
        $fileResult = Get-Content -Raw -LiteralPath $responsePath | ConvertFrom-Json
        $maskedBytes = if ($fileResult.result.maskedFile.base64) {
            [Convert]::FromBase64String([string]$fileResult.result.maskedFile.base64)
        } else { $null }
        $maskedText = if ($maskedBytes) { [Text.Encoding]::UTF8.GetString($maskedBytes) } else { '' }
        $report.fileMasking = [bool](
            $fileResult.done -and
            $fileResult.result.scanStatus -eq 'ok' -and
            @($fileResult.result.piiItems).Count -gt 0 -and
            $maskedBytes -and
            -not $maskedText.Contains($fileSecret)
        )
        if (-not $report.fileMasking) { throw 'File masking verification failed.' }
    }

    Stop-AppTree -Process $appProcess
    $uninstaller = Join-Path (Split-Path -Parent $appExe) 'Uninstall Campfire.exe'
    if (Test-Path -LiteralPath $uninstaller) {
        $uninstall = Start-Process -FilePath $uninstaller -ArgumentList '/S' -Wait -PassThru
        $report.uninstall = $uninstall.ExitCode -eq 0
    }
    if (-not $report.uninstall) { throw 'Silent uninstall verification failed.' }

    $report.status = 'passed'
} catch {
    $report.error = $_.Exception.Message
} finally {
    Stop-AppTree -Process $appProcess
    $report | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $resultPath -Encoding UTF8
    Stop-Transcript | Out-Null
}

if ($report.status -ne 'passed') { exit 1 }
