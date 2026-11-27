$ErrorActionPreference = 'Stop'
$os = Get-CimInstance Win32_OperatingSystem
Write-Host "$($os.Caption) build $($os.BuildNumber) ($($os.OSArchitecture))"
if ($os.ProductType -ne 1 -or [int]$os.BuildNumber -lt 22000) { throw 'A Windows 11 client is required for this check.' }

$release = (Resolve-Path "$PSScriptRoot/../dist/release").Path
$testRoot = New-Item -ItemType Directory (Join-Path ([IO.Path]::GetTempPath()) "Aether QA $([guid]::NewGuid())")
$env:AETHER_DATA_DIR = Join-Path $testRoot 'User data'
$env:AETHER_SMOKE_SOURCE = Join-Path $testRoot 'Offline games'
$env:RAWG_API_KEY = ''
$fixture = New-Item -ItemType Directory -Force (Join-Path $env:AETHER_SMOKE_SOURCE 'Game Library Probe')
[IO.File]::WriteAllBytes((Join-Path $fixture 'Probe.exe'), [byte[]](0,1,2,3))

function Test-PackagedUi([string]$exe, [string]$name) {
    $report = Join-Path $testRoot "$name.json"
    $process = Start-Process -FilePath $exe -ArgumentList "--smoke-test `"$report`"" -PassThru
    try {
        if (!$process.WaitForExit(150000)) { throw "Timed out waiting for $name native window." }
        if (!(Test-Path $report)) { throw "$name exited before reporting the native UI result (exit $($process.ExitCode))." }
        $result = Get-Content $report -Raw | ConvertFrom-Json
        Write-Host ($result | ConvertTo-Json -Compress)
        if (!$result.ok -or !$result.library -or $result.engine -notmatch 'Edg/') { throw "$name did not pass its WebView2 check." }
        if ($process.ExitCode -ne 0) { throw "$name exited with code $($process.ExitCode)." }
    } finally {
        if (!$process.HasExited) { taskkill /PID $process.Id /T /F }
        $log = Join-Path $env:AETHER_DATA_DIR 'aether.log'
        if (Test-Path $log) { Get-Content $log -Tail 60 }
    }
}
foreach ($line in Get-Content (Join-Path $release 'SHA256SUMS.txt')) {
    $expected, $name = $line -split '  ', 2
    if ((Get-FileHash -Algorithm SHA256 (Join-Path $release $name)).Hash -ne $expected) { throw "Checksum failed for $name" }
}
$zip = (Get-ChildItem $release -Filter '*-portable.zip').FullName
Expand-Archive -LiteralPath $zip -DestinationPath (Join-Path $testRoot 'Portable')
Test-PackagedUi (Join-Path $testRoot 'Portable/Aether/Aether.exe') 'portable'
$setup = (Get-ChildItem $release -Filter '*-setup.exe').FullName
$installed = Join-Path $testRoot 'Installed'
$result = Start-Process -Wait -PassThru $setup -ArgumentList "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /DIR=`"$installed`""
if ($result.ExitCode) { throw "Installer failed with $($result.ExitCode)." }
Test-PackagedUi (Join-Path $installed 'Aether.exe') 'installed'
$uninstall = (Get-ChildItem $installed -Filter 'unins*.exe' | Select-Object -First 1).FullName
$result = Start-Process -Wait -PassThru $uninstall -ArgumentList '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART'
if ($result.ExitCode -or !(Test-Path (Join-Path $env:AETHER_DATA_DIR 'library.sqlite3'))) { throw 'Uninstall or data preservation check failed.' }
