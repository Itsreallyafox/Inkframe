<#
.SYNOPSIS
    Build the native dither library on the Windows desktop with MSVC:
    inkframe\render\dither.dll (gitignored).

.DESCRIPTION
    Locates Visual Studio with vswhere, runs vcvars64.bat in a cmd shell and
    compiles inkframe\render\dither.c with
        cl /nologo /O2 /W3 /std:c11 /LD /Fe:inkframe\render\dither.dll inkframe\render\dither.c
    then deletes the .obj/.exp/.lib leftovers and prints the DLL path.
    The Pi builds libdither.so itself (install.sh build_native, or
    scripts/build_dither.sh); this script is for desktop tests only.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_dither.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$renderDir = Join-Path $root 'inkframe\render'
$src = Join-Path $renderDir 'dither.c'
$dll = Join-Path $renderDir 'dither.dll'
if (-not (Test-Path $src)) { throw "missing $src" }

$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
if (-not (Test-Path $vswhere)) { throw "vswhere.exe not found at $vswhere; install Visual Studio 2022 with the C++ workload" }
$vsPath = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vsPath) { throw 'vswhere found no Visual Studio with the x64 C++ tools' }
$vcvars = Join-Path $vsPath 'VC\Auxiliary\Build\vcvars64.bat'
if (-not (Test-Path $vcvars)) { throw "missing $vcvars" }

# A throw-away batch file avoids PowerShell 5.1's native-argument quoting.
# vcvars64.bat itself calls vswhere, so put it on PATH first.  The paths reach
# cmd.exe through environment variables, which are Unicode end to end, so the
# batch file itself is pure ASCII and a repo or Visual Studio path with
# non-ASCII characters is not mangled by the console code page.
$bat = Join-Path ([System.IO.Path]::GetTempPath()) ('inkframe-build-dither-' + [guid]::NewGuid().ToString('N') + '.cmd')
$lines = @(
    '@echo off',
    'set "PATH=%INKFRAME_VSWHERE_DIR%;%PATH%"',
    'call "%INKFRAME_VCVARS%" >nul || exit /b 1',
    'cd /d "%INKFRAME_ROOT%" || exit /b 1',
    'cl /nologo /O2 /W3 /std:c11 /LD /Fe:inkframe\render\dither.dll /Fo:inkframe\render\dither.obj inkframe\render\dither.c || exit /b 1'
)
Set-Content -Path $bat -Value $lines -Encoding ASCII
$env:INKFRAME_VSWHERE_DIR = Split-Path -Parent $vswhere
$env:INKFRAME_VCVARS = $vcvars
$env:INKFRAME_ROOT = $root
try {
    & cmd.exe /d /c $bat
    $code = $LASTEXITCODE
}
finally {
    Remove-Item -Force -ErrorAction SilentlyContinue $bat
    foreach ($name in @('INKFRAME_VSWHERE_DIR', 'INKFRAME_VCVARS', 'INKFRAME_ROOT')) {
        Remove-Item -Force -ErrorAction SilentlyContinue ('Env:' + $name)
    }
    foreach ($ext in @('obj', 'exp', 'lib')) {
        Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $renderDir ('dither.' + $ext))
        Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $root ('dither.' + $ext))
    }
}
if ($code -ne 0) { throw "cl failed with exit code $code" }
if (-not (Test-Path $dll)) { throw "cl reported success but $dll is missing" }
Write-Host ('built ' + $dll)
