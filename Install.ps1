[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$GameDir,
    [string]$Replay
)
$ErrorActionPreference = 'Stop'
$game = (Resolve-Path -LiteralPath $GameDir).Path
$version = 'mods\2.3.1.2'
$helper = 'mods\offhangar-fleet'
$client = Join-Path $game 'win64\WorldOfTanks.exe'
$versionDir = Join-Path $game $version
if (!(Test-Path -LiteralPath $client -PathType Leaf) -or
    !(Test-Path -LiteralPath (Join-Path $versionDir 'offhangar2_lan_2312.wotmod') -PathType Leaf)) {
    throw 'Select the game root containing the EU 2.3.1.2 OffHangar 2.0 LAN installation.'
}
if (Get-Process -Name WorldOfTanks -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $client }) {
    throw 'Close this World of Tanks client before installing.'
}
$modules = 'oh2-replay-compat','oh2-fleet','oh2-fleet-animation','oh2-fleet-hud','oh2-fleet-radio','oh2-fleet-status'
$files = @($modules | ForEach-Object { Join-Path $version ($_+'.wotmod') })
$files += (Join-Path $helper 'export.exe'), (Join-Path $helper 'FleetReplaySettings.exe')
foreach ($file in $files) {
    if (!(Test-Path -LiteralPath (Join-Path $PSScriptRoot $file) -PathType Leaf)) {
        throw 'Extract the complete Windows release ZIP before running Install.ps1.'
    }
}
$seedFile = Join-Path $helper 'bootstrap.dat'
$installedSeed = Join-Path $game $seedFile
$source = if ($Replay) {
    (Resolve-Path -LiteralPath $Replay).Path
} elseif (Test-Path -LiteralPath $installedSeed -PathType Leaf) {
    $installedSeed
} else {
    Join-Path $PSScriptRoot $seedFile
}
if (!(Test-Path -LiteralPath $source -PathType Leaf)) { throw 'Extract the full release ZIP including its replay template.' }
$temp = New-Item -ItemType Directory -Path (Join-Path ([IO.Path]::GetTempPath()) ('offhangar-replay-'+[guid]::NewGuid()))
$rollback = @()
try {
    $prepared = Join-Path $temp.FullName 'bootstrap.dat'
    $exe = Join-Path $PSScriptRoot (Join-Path $helper 'export.exe')
    $job = Start-Process -FilePath $exe -ArgumentList @('--prepare-seed', ('"'+$source+'"'), ('"'+$prepared+'"')) -WindowStyle Hidden -Wait -PassThru
    if ($job.ExitCode -ne 0 -or !(Test-Path -LiteralPath $prepared)) {
        $errorFile = Join-Path $temp.FullName 'bootstrap.error.txt'
        if (Test-Path -LiteralPath $errorFile) { throw (Get-Content -LiteralPath $errorFile -Raw) }
        throw 'Could not prepare this replay template.'
    }
    $null = New-Item -ItemType Directory -Force -Path (Join-Path $game $helper)
    foreach ($file in ($files + $seedFile)) {
        $dst = Join-Path $game $file
        $src = if ($file -eq $seedFile) { $prepared } else { Join-Path $PSScriptRoot $file }
        $backup = $null
        if (Test-Path -LiteralPath $dst) {
            $backup = Join-Path $temp.FullName ([guid]::NewGuid().ToString())
            Copy-Item -LiteralPath $dst -Destination $backup
        }
        $rollback += [pscustomobject]@{ Path=$dst; Backup=$backup }
        Copy-Item -LiteralPath $src -Destination $dst -Force
    }
    Write-Output "Installed OffHangar Replay in $game"
} catch {
    foreach ($entry in $rollback) {
        if ($entry.Backup) { Copy-Item -LiteralPath $entry.Backup -Destination $entry.Path -Force }
        elseif (Test-Path -LiteralPath $entry.Path) { Remove-Item -LiteralPath $entry.Path -Force }
    }
    throw
} finally {
    # Only files created by this invocation live in this freshly generated directory.
    Get-ChildItem -LiteralPath $temp.FullName -File | Remove-Item -Force
    Remove-Item -LiteralPath $temp.FullName
}
