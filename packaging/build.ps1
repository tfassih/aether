$ErrorActionPreference = 'Stop'
Push-Location (Join-Path $PSScriptRoot '..')
try {
    $version = (Get-Content VERSION -Raw).Trim()
    if ($version -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid release version.' }
    python -m PyInstaller --clean --noconfirm packaging/aether.spec
    if ($LASTEXITCODE) { throw 'PyInstaller failed.' }
    $release = New-Item -ItemType Directory -Force dist/release
    Copy-Item LICENSE,README.md dist/Aether
    $archive = Join-Path $release "Aether-$version-windows-x64-portable.zip"
    python -m zipfile -c $archive dist/Aether
    if ($LASTEXITCODE) { throw 'Could not create the portable archive.' }
    $iscc = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    $compiler = if ($iscc) { $iscc.Source } else { Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6/ISCC.exe' }
    if (!(Test-Path $compiler)) { throw 'Install Inno Setup 6.3 or newer before building.' }
    & $compiler "/DAppVersion=$version" packaging/setup.iss
    if ($LASTEXITCODE) { throw 'Inno Setup failed.' }
    $installer = Join-Path $release "Aether-$version-windows-x64-setup.exe"
    $hashes = foreach ($file in @($archive, $installer)) {
        $hash = (Get-FileHash -Algorithm SHA256 $file).Hash.ToLowerInvariant()
        "$hash  $(Split-Path $file -Leaf)"
    }
    [IO.File]::WriteAllText((Join-Path $release 'SHA256SUMS.txt'), ($hashes -join "`n") + "`n", [Text.UTF8Encoding]::new($false))
} finally {
    Pop-Location
}
