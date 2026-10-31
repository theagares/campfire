[CmdletBinding()]
param(
  [string]$OutputDirectory = (Join-Path $PSScriptRoot '..\dist')
)

$ErrorActionPreference = 'Stop'

$extensionRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$manifestPath = Join-Path $extensionRoot 'manifest.json'
$manifest = Get-Content -Raw -Encoding UTF8 -LiteralPath $manifestPath | ConvertFrom-Json
if (-not ($manifest.version -is [string]) -or -not $manifest.version.Trim()) {
  throw 'manifest.json에 유효한 version이 없습니다.'
}

$outputRoot = [IO.Path]::GetFullPath($OutputDirectory)
[IO.Directory]::CreateDirectory($outputRoot) | Out-Null
$archiveName = "Campfire-extension-$($manifest.version).zip"
$archivePath = Join-Path $outputRoot $archiveName

$excludedDirectories = @('tests', 'scripts', 'dist', 'node_modules')
$excludedFiles = @('.DS_Store', 'Thumbs.db')
$repoRoot = Split-Path -Parent $extensionRoot
$git = Get-Command git -ErrorAction SilentlyContinue
$candidateFiles = @()
$usingTrackedFiles = $false

if ($null -ne $git) {
  $trackedPaths = @(& $git.Source -C $repoRoot -c core.quotepath=false ls-files -- extension 2>$null)
  if ($LASTEXITCODE -eq 0) {
    $usingTrackedFiles = $true
    $candidateFiles = @($trackedPaths | ForEach-Object {
      $repoRelativePath = ([string]$_).Trim().Replace('\', '/')
      if (-not $repoRelativePath.StartsWith('extension/')) { return }
      $sourcePath = Join-Path $repoRoot $repoRelativePath.Replace('/', [IO.Path]::DirectorySeparatorChar)
      if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) { return }
      [pscustomobject]@{
        Source = $sourcePath
        Relative = $repoRelativePath.Substring('extension/'.Length)
      }
    })
  }
}

if (-not $usingTrackedFiles) {
  $candidateFiles = @(Get-ChildItem -LiteralPath $extensionRoot -Recurse -File | ForEach-Object {
    $relativePath = $_.FullName.Substring($extensionRoot.Length).TrimStart('\', '/').Replace('\', '/')
    $pathParts = $relativePath -split '/'
    if (@($pathParts | Where-Object { $_.StartsWith('.') -or $_.StartsWith('_') }).Count) { return }
    [pscustomobject]@{ Source = $_.FullName; Relative = $relativePath }
  })
}

$files = $candidateFiles | ForEach-Object {
  $relativePath = $_.Relative
  $topDirectory = ($relativePath -split '/', 2)[0]
  if ($excludedDirectories -contains $topDirectory) { return }
  $fileName = [IO.Path]::GetFileName($relativePath)
  $extension = [IO.Path]::GetExtension($relativePath)
  if ($excludedFiles -contains $fileName) { return }
  if ($extension -in @('.map', '.zip') -or $fileName.EndsWith('.test.js')) { return }
  $_
} | Sort-Object Relative

if (-not ($files.Relative -contains 'manifest.json')) {
  throw '패키지 루트에 manifest.json이 포함되지 않았습니다.'
}

if ([IO.File]::Exists($archivePath)) {
  [IO.File]::Delete($archivePath)
}
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [IO.Compression.ZipFile]::Open($archivePath, [IO.Compression.ZipArchiveMode]::Create)
try {
  foreach ($file in $files) {
    [IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
      $archive,
      $file.Source,
      $file.Relative,
      [IO.Compression.CompressionLevel]::Optimal
    ) | Out-Null
  }
} finally {
  $archive.Dispose()
}

Write-Host "Created: $archivePath"
Write-Host "Version: $($manifest.version)"
Write-Host "Source: $(if ($usingTrackedFiles) { 'git tracked files' } else { 'filesystem fallback' })"
Write-Host "Included files ($($files.Count)):"
$files.Relative | ForEach-Object { Write-Host "  $_" }
Write-Output $archivePath
