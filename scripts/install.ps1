# securedoc-gateway 원커맨드 설치 (Windows)
#   irm https://raw.githubusercontent.com/theagares/securedoc-gateway/main/scripts/install.ps1 | iex
#
# git clone 없이 GitHub 저장소를 zip으로 받아 엔진(Python venv)과 데스크탑(Electron)
# 의존성까지 한 번에 설치한다. 실행/패키징은 하지 않고, 마지막에 실행 명령만 안내한다.

$ErrorActionPreference = 'Stop'

$Repo = 'theagares/securedoc-gateway'
$Dest = if ($env:SECUREDOC_INSTALL_DIR) { $env:SECUREDOC_INSTALL_DIR } else { Join-Path $HOME 'securedoc-gateway' }

if (Test-Path $Dest) {
    Write-Host "설치 대상 폴더가 이미 있습니다: $Dest" -ForegroundColor Yellow
    Write-Host '기존 폴더를 지우거나, $env:SECUREDOC_INSTALL_DIR 로 다른 경로를 지정한 뒤 다시 실행하세요.'
    exit 1
}

foreach ($cmd in @('node', 'npm', 'python')) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        Write-Host "필수 프로그램을 찾을 수 없습니다: $cmd" -ForegroundColor Red
        Write-Host 'Node.js 22+ 와 Python 3.10+ 를 먼저 설치하세요.'
        exit 1
    }
}

Write-Host "소스 다운로드 중... ($Repo)"
$zipPath = Join-Path ([System.IO.Path]::GetTempPath()) "securedoc-gateway-$([guid]::NewGuid()).zip"
$extractDir = Join-Path ([System.IO.Path]::GetTempPath()) "securedoc-gateway-$([guid]::NewGuid())"
Invoke-WebRequest -Uri "https://github.com/$Repo/archive/refs/heads/main.zip" -OutFile $zipPath
Expand-Archive -Path $zipPath -DestinationPath $extractDir -Force

$srcRoot = Join-Path $extractDir 'securedoc-gateway-main'
New-Item -ItemType Directory -Path $Dest -Force | Out-Null
Copy-Item -Path (Join-Path $srcRoot '*') -Destination $Dest -Recurse -Force
Remove-Item $zipPath, $extractDir -Recurse -Force

Set-Location $Dest

Write-Host "엔진(Python) 의존성 설치 중..."
python -m venv engine\.venv
& engine\.venv\Scripts\python.exe -m pip install --upgrade pip --quiet
& engine\.venv\Scripts\python.exe -m pip install -e "engine[test]" --quiet

Write-Host "데스크탑 앱(Electron) 의존성 설치 중..."
Push-Location desktop
npm install --no-fund --no-audit
Pop-Location

Write-Host ''
Write-Host "설치 완료: $Dest" -ForegroundColor Green
Write-Host '실행하려면:'
Write-Host "  cd `"$Dest\desktop`""
Write-Host '  npm start'
