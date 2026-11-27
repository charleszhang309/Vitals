[CmdletBinding()]
param([switch]$SidecarOnly)

$ErrorActionPreference = 'Stop'
$desktop = Split-Path -Parent $PSScriptRoot
$project = Split-Path -Parent $desktop
$python = Join-Path $project '.venv/Scripts/python.exe'
$cargoHome = Join-Path $env:USERPROFILE '.cargo/bin'
if (Test-Path -LiteralPath $cargoHome) { $env:Path = "$cargoHome;$env:Path" }
$npm = (Get-Command npm.cmd -ErrorAction Stop).Source
$rustc = (Get-Command rustc.exe -ErrorAction Stop).Source
if (!(Test-Path -LiteralPath $python)) { throw 'Create the project .venv and install backend/requirements.txt first.' }

function Invoke-Step([string]$Program, [string[]]$Arguments) {
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program exited with code $LASTEXITCODE." }
}

$target = (& $rustc -vV | Select-String '^host: ').Line -replace '^host: ', ''
if ($target -notin @('x86_64-pc-windows-msvc', 'aarch64-pc-windows-msvc')) {
    throw 'Use a native x64 or ARM64 Windows MSVC Rust toolchain.'
}
$architecture = (& $python -c "import platform; print(platform.machine())").Trim().ToUpperInvariant()
if (($target.StartsWith('x86_64') -and $architecture -ne 'AMD64') -or
    ($target.StartsWith('aarch64') -and $architecture -ne 'ARM64')) {
    throw 'Python architecture must match the native Rust toolchain.'
}

$build = Join-Path $desktop '.build'
$static = Join-Path $build 'static'
$binaries = Join-Path $desktop 'src-tauri/binaries'
$runtime = Join-Path $binaries 'vitals-hub-runtime'
New-Item -ItemType Directory -Force -Path $build, $binaries | Out-Null
Push-Location (Join-Path $project 'frontend')
try {
    Invoke-Step $npm @('ci')
    Invoke-Step $npm @('run', 'build', '--', '--outDir', $static)
} finally { Pop-Location }

Push-Location $project
try {
    Invoke-Step $python @('-m', 'pip', 'install', 'pyinstaller==6.22.3', '-r', 'backend/requirements.txt')
    $frozen = Join-Path $build 'frozen'
    # CalDAV loads its client through a lazy import that PyInstaller cannot detect.
    Invoke-Step $python @(
        '-m', 'PyInstaller', '--noconfirm', '--onedir', '--console', '--name', 'vitals-hub', '--paths', $project,
        '--distpath', $frozen, '--workpath', (Join-Path $build 'work'), '--specpath', $build,
        '--add-data', "$static;backend/static", '--add-data', "$(Join-Path $project 'backend/agent.md');backend",
        '--add-data', "$(Join-Path $project 'plugins/obs-studio/manifest.json');plugins/obs-studio",
        '--add-data', "$(Join-Path $project 'plugins/obs-studio/plugin.py');plugins/obs-studio",
        '--add-data', "$(Join-Path $project 'plugins/davinci-resolve/manifest.json');plugins/davinci-resolve",
        '--add-data', "$(Join-Path $project 'plugins/davinci-resolve/plugin.py');plugins/davinci-resolve",
        '--collect-submodules', 'uvicorn', '--collect-submodules', 'winrt', '--collect-submodules', 'pycaw',
        '--hidden-import', 'caldav.davclient',
        (Join-Path $PSScriptRoot 'sidecar_entry.py')
    )
    $sidecar = Join-Path $frozen 'vitals-hub'
    Copy-Item -LiteralPath (Join-Path $sidecar 'vitals-hub.exe') -Destination (Join-Path $binaries "vitals-hub-$target.exe") -Force
    if (Test-Path -LiteralPath $runtime) {
        $resolvedRuntime = (Resolve-Path -LiteralPath $runtime).Path
        $expectedRuntime = Join-Path (Resolve-Path -LiteralPath $binaries).Path 'vitals-hub-runtime'
        if ($resolvedRuntime -ne $expectedRuntime -or (Get-Item -LiteralPath $runtime).LinkType) {
            throw 'Unexpected sidecar runtime directory; remove it manually.'
        }
        Remove-Item -LiteralPath $resolvedRuntime -Recurse -Force
    }
    Copy-Item -LiteralPath (Join-Path $sidecar '_internal') -Destination $runtime -Recurse
} finally { Pop-Location }

if (!$SidecarOnly) {
    Push-Location $desktop
    try {
        Invoke-Step $npm @('ci')
        Invoke-Step $npm @('run', 'tauri', '--', 'build', '--', '--locked')
    } finally { Pop-Location }
}
