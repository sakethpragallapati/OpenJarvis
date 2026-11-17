<#
.SYNOPSIS
    Builds the Windows setup wizard: dist\OpenJarvis-Setup-<version>.exe

.DESCRIPTION
    Needs (on the build PC): uv, Rust (cargo) with the MSVC build tools,
    Inno Setup 6, and this repo's .venv (Python 3.11, for maturin and the
    icon).

      .\scripts\windows\build-installer.ps1 -Version 0.1.0

    -UpdateRequirements re-pins deploy\windows\installer\requirements-*.txt
    from the packages installed in .venv (the versions you tested with).

.NOTES
    The setup is small (~10 MB): Python packages, Node.js and the model are
    downloaded on the user's PC by setup-env.ps1.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$RepoUrl = "https://github.com/open-jarvis/openjarvis",
    [switch]$UpdateRequirements
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Installer = Join-Path $Root "deploy\windows\installer"
$Build = Join-Path $Root "build\installer"
$Payload = Join-Path $Build "payload"
$Dist = Join-Path $Root "dist"
$Python = Join-Path $Root ".venv\Scripts\python.exe"

function Invoke-Checked([string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Exe $($Arguments -join ' ') failed ($LASTEXITCODE)" }
}

$uv = (Get-Command uv -ErrorAction Stop).Source
$iscc = @(
    (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 isn't installed (winget install JRSoftware.InnoSetup)." }

if ($UpdateRequirements) {
    Write-Host "==> Re-pinning requirements from .venv"
    New-Item -ItemType Directory -Force $Build | Out-Null
    $constraints = Join-Path $Build "constraints.txt"
    $overrides = Join-Path $Build "overrides.txt"
    (& $uv pip freeze --python $Python) | Where-Object { $_ -notmatch "^-e |^openjarvis-rust @" } |
        Set-Content $constraints -Encoding ascii
    # pyproject's [tool.uv] override-dependencies (chatterbox's old pins).
    @("torch>=2.7", "torchaudio>=2.7", "transformers>=5.2,<6", "numpy>=1.24,<2.3",
      "safetensors>=0.5.3", "diffusers>=0.29", "gradio; sys_platform == 'never'",
      "spacy-pkuseg; sys_platform == 'never'", "pykakasi; sys_platform == 'never'") |
        Set-Content $overrides -Encoding ascii
    $cuda = Join-Path $Installer "requirements-cuda.txt"
    Push-Location $Root
    try {
        Invoke-Checked $uv @("pip", "compile", "pyproject.toml", "--extra", "desktop", "--extra", "voice",
            "--extra", "agent", "--extra", "wakeword", "-c", $constraints, "--override", $overrides,
            "--extra-index-url", "https://download.pytorch.org/whl/cu128", "--index-strategy", "unsafe-best-match",
            "--python-version", "3.11", "--python-platform", "x86_64-pc-windows-msvc",
            "--emit-index-url", "--no-header", "--no-annotate", "-o", $cuda)
    } finally { Pop-Location }
    (Get-Content $cuda) -replace "whl/cu128", "whl/cpu" -replace "^(torch(audio)?==[0-9.]+)\+cu128", '$1+cpu' |
        Set-Content (Join-Path $Installer "requirements-cpu.txt") -Encoding ascii
}

Write-Host "==> Staging $Payload"
if (Test-Path $Payload) { Remove-Item $Payload -Recurse -Force }
$wheels = Join-Path $Payload "setup\wheels"
New-Item -ItemType Directory -Force $wheels, (Join-Path $Payload "bin") | Out-Null

Write-Host "==> OpenJarvis $Version wheel"
$env:SETUPTOOLS_SCM_PRETEND_VERSION = $Version
Push-Location $Root
try {
    Invoke-Checked $uv @("build", "--wheel", "--no-config", "-o", $wheels)
} finally {
    Pop-Location
    Remove-Item Env:SETUPTOOLS_SCM_PRETEND_VERSION
}

Write-Host "==> Native extension (Rust) for Python 3.11"
$maturin = Join-Path $Root ".venv\Scripts\maturin.exe"
Invoke-Checked $maturin @("build", "--release", "-m", (Join-Path $Root "rust\crates\openjarvis-python\Cargo.toml"),
    "-i", $Python, "-o", $wheels)

Write-Host "==> Setup files"
foreach ($name in @("setup-env.ps1", "config.toml", "jarvis.wav", "requirements-cuda.txt", "requirements-cpu.txt")) {
    Copy-Item (Join-Path $Installer $name) (Join-Path $Payload "setup")
}
Copy-Item $uv (Join-Path $Payload "bin\uv.exe")
Copy-Item (Join-Path $Root "LICENSE") (Join-Path $Payload "LICENSE.txt")
Copy-Item (Join-Path $Installer "README.txt") (Join-Path $Payload "README.txt")
$icon = Join-Path $Payload "jarvis.ico"
Invoke-Checked $Python @("-c", "import sys; from openjarvis.hud.install import draw_icon, _ICON_SIZES; draw_icon(256).save(sys.argv[1], format='ICO', sizes=_ICON_SIZES)", $icon)

Write-Host "==> Inno Setup"
New-Item -ItemType Directory -Force $Dist | Out-Null
Invoke-Checked $iscc @("/Q", "/DAppVersion=$Version", "/DPayload=$Payload", "/DRepoUrl=$RepoUrl",
    "/DOutputDir=$Dist", (Join-Path $Installer "OpenJarvis.iss"))

$setup = Join-Path $Dist "OpenJarvis-Setup-$Version.exe"
$hash = (Get-FileHash $setup -Algorithm SHA256).Hash.ToLower()
"$hash  OpenJarvis-Setup-$Version.exe" | Set-Content (Join-Path $Dist "OpenJarvis-Setup-$Version.exe.sha256") -Encoding ascii
Write-Host ""
Write-Host "Built $setup"
Write-Host "SHA256 $hash"
