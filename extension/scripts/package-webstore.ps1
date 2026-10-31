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
$files = Get-ChildItem -LiteralPath $extensionRoot -Recurse -File | ForEach-Object {
  $relativePath = $_.FullName.Substring($extensionRoot.Length).TrimStart('\', '/').Replace('\', '/')
  $topDirectory = ($relativePath -split '/', 2)[0]
  if ($excludedDirectories -contains $topDirectory) { return }
  if ($excludedFiles -contains $_.Name) { return }
  if ($_.Extension -in @('.map', '.zip') -or $_.Name.EndsWith('.test.js')) { return }
  [pscustomobject]@{ Source = $_.FullName; Relative = $relativePath }
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
Write-Host "Included files ($($files.Count)):"
$files.Relative | ForEach-Object { Write-Host "  $_" }
Write-Output $archivePath
