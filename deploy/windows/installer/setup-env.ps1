<#
.SYNOPSIS
    Builds Jarvis's Python environment. The OpenJarvis setup wizard runs this
    after copying its files; it can be re-run to repair an install.

.DESCRIPTION
    1. Python 3.11 (python.org's installation-free build, checksum- and
       signature-checked, kept inside the install folder).
    2. A virtual environment with the pinned packages: PyTorch with CUDA when
       an NVIDIA GPU is present, the CPU build otherwise.
    3. OpenJarvis and its native extension (wheels shipped with the setup).
    4. Node.js (pinned, checksum-verified) and the MCP filesystem server for
       Jarvis's file tools.
    5. A starter ~/.openjarvis/config.toml and the "jarvis" voice, unless you
       already have them.
    6. With -Brain local: Ollama (signature-checked) and the qwen3.5:4b model.
       -Brain repair (the Start-menu repair shortcut) leaves Ollama alone.

    Everything goes into the install folder except your settings
    (~/.openjarvis) and Ollama, so uninstalling removes the rest.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$App,
    [ValidateSet("local", "groq", "repair")][string]$Brain = "local",
    [string]$Model = "qwen3.5:4b"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is slow with it
$Host.UI.RawUI.WindowTitle = "Installing Jarvis"

$PythonVersion = "3.11.9"
$PythonSha256 = "9283876d58c017e0e846f95b490da3bca0fc0a6ee1134b2870677cfb7eec3c67"
$NodeVersion = "v24.22.2"
$NodeSha256 = "ca1bff5d8ead13c9f2ea404a176db46bd430704d5b5113734049fa365e7799e2"
$OllamaUrl = "https://ollama.com/download/OllamaSetup.exe"

$Setup = Join-Path $App "setup"
$Log = Join-Path $App "install.log"
$Uv = Join-Path $App "bin\uv.exe"
$Venv = Join-Path $App "venv"
$Python = Join-Path $Venv "Scripts\python.exe"
$Home_ = Join-Path $env:USERPROFILE ".openjarvis"

Start-Transcript -Path $Log -Append | Out-Null

function Step([string]$Text) {
    Write-Host ""
    Write-Host "==> $Text" -ForegroundColor Cyan
}

function Invoke-Checked([string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$([IO.Path]::GetFileName($Exe)) $($Arguments -join ' ') failed (exit $LASTEXITCODE)"
    }
}

function Test-NvidiaGpu {
    try {
        $names = (Get-CimInstance Win32_VideoController).Name
        return [bool]($names | Where-Object { $_ -match "NVIDIA" })
    } catch {
        return $false
    }
}

try {
    # uv's cache and downloads stay inside the install folder; uv never
    # downloads a Python of its own.
    $cleanCache = $false
    if (-not $env:UV_CACHE_DIR) {
        $env:UV_CACHE_DIR = Join-Path $App "cache"
        $cleanCache = $true
    }
    $env:UV_NO_CONFIG = "1"
    $env:UV_PYTHON_DOWNLOADS = "never"

    Step "Python $PythonVersion"
    # python.org's installation-free build (its NuGet package): a plain
    # folder, nothing in the registry or on PATH. uv's own Python downloads
    # use a directory junction that fails when started from the setup wizard.
    $pythonHome = Join-Path $App "python"
    $basePython = Join-Path $pythonHome "python.exe"
    if (-not (Test-Path $basePython)) {
        $nupkg = Join-Path $env:TEMP "python.$PythonVersion.nupkg"
        Invoke-WebRequest "https://api.nuget.org/v3-flatcontainer/python/$PythonVersion/python.$PythonVersion.nupkg" -OutFile $nupkg -UseBasicParsing
        if ((Get-FileHash $nupkg -Algorithm SHA256).Hash.ToLower() -ne $PythonSha256) {
            throw "The Python download is corrupt (checksum mismatch)."
        }
        $unpack = Join-Path $env:TEMP "python-$PythonVersion-unpack"
        if (Test-Path $unpack) { Remove-Item $unpack -Recurse -Force }
        $zip = [IO.Path]::ChangeExtension($nupkg, ".zip")
        Move-Item $nupkg $zip -Force
        Expand-Archive $zip -DestinationPath $unpack
        $signature = Get-AuthenticodeSignature (Join-Path $unpack "tools\python.exe")
        if ($signature.Status -ne "Valid" -or $signature.SignerCertificate.Subject -notmatch "O=Python Software Foundation") {
            throw "The Python download isn't signed by the Python Software Foundation."
        }
        if (Test-Path $pythonHome) { Remove-Item $pythonHome -Recurse -Force }
        Move-Item (Join-Path $unpack "tools") $pythonHome
        Remove-Item $zip, $unpack -Recurse -Force -ErrorAction SilentlyContinue
    }
    Invoke-Checked $Uv @("venv", $Venv, "--python", $basePython, "--allow-existing")

    $gpu = Test-NvidiaGpu
    $requirements = if ($gpu) { "requirements-cuda.txt" } else { "requirements-cpu.txt" }
    if ($gpu) {
        Step "Packages (NVIDIA GPU found: PyTorch with CUDA, about 3 GB)"
    } else {
        Step "Packages (no NVIDIA GPU: CPU build of PyTorch)"
    }
    # A complete, pinned list: installed as-is, without re-resolving.
    Invoke-Checked $Uv @(
        "pip", "install", "--python", $Python, "--no-deps",
        "--index-strategy", "unsafe-best-match",
        "-r", (Join-Path $Setup $requirements)
    )

    Step "OpenJarvis"
    $wheels = Get-ChildItem (Join-Path $Setup "wheels") -Filter *.whl | ForEach-Object FullName
    Invoke-Checked $Uv (@("pip", "install", "--python", $Python, "--no-deps", "--reinstall") + $wheels)

    Step "Node.js $NodeVersion (for the file tools)"
    $node = Join-Path $App "node"
    if (-not (Test-Path (Join-Path $node "node.exe"))) {
        $zip = Join-Path $env:TEMP "node-$NodeVersion-win-x64.zip"
        Invoke-WebRequest "https://nodejs.org/dist/$NodeVersion/node-$NodeVersion-win-x64.zip" -OutFile $zip -UseBasicParsing
        $hash = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
        if ($hash -ne $NodeSha256) { throw "Node.js download is corrupt (checksum mismatch)." }
        $unpack = Join-Path $env:TEMP "node-$NodeVersion-unpack"
        if (Test-Path $unpack) { Remove-Item $unpack -Recurse -Force }
        Expand-Archive $zip -DestinationPath $unpack
        Move-Item (Join-Path $unpack "node-$NodeVersion-win-x64") $node
        Remove-Item $zip, $unpack -Recurse -Force -ErrorAction SilentlyContinue
    }

    Step "File tools (MCP filesystem server)"
    $env:PATH = "$node;$env:PATH"
    Invoke-Checked (Join-Path $Venv "Scripts\jarvis.exe") @("desktop", "files", "--install")

    Step "Your settings"
    New-Item -ItemType Directory -Force (Join-Path $Home_ "voices") | Out-Null
    $config = Join-Path $Home_ "config.toml"
    if (-not (Test-Path $config)) {
        $text = [IO.File]::ReadAllText((Join-Path $Setup "config.toml"))
        $hasOllama = Test-Path (Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe")
        $engine = if ($Brain -eq "groq" -or ($Brain -eq "repair" -and -not $hasOllama)) { "groq" } else { "ollama" }
        $text = $text.Replace("@ENGINE@", $engine).Replace("@STT@", "faster-whisper")
        if ($gpu) {
            $text = $text.Replace("@WHISPER_MODEL@", "small.en").Replace("@DEVICE@", "cuda").Replace("@COMPUTE@", "int8_float16")
        } else {
            $text = $text.Replace("@WHISPER_MODEL@", "base.en").Replace("@DEVICE@", "cpu").Replace("@COMPUTE@", "int8")
        }
        # UTF-8 without a BOM: TOML readers reject the BOM Windows PowerShell adds.
        [IO.File]::WriteAllText($config, $text, (New-Object Text.UTF8Encoding $false))
        Write-Host "Wrote $config"
    } else {
        Write-Host "Kept your existing $config"
    }
    $voice = Join-Path $Home_ "voices\jarvis.wav"
    if (-not (Test-Path $voice)) {
        Copy-Item (Join-Path $Setup "jarvis.wav") $voice
    }

    if ($Brain -eq "local") {
        Step "Ollama and the $Model model (about 3.4 GB)"
        $ollama = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
        if (-not (Test-Path $ollama)) {
            $installer = Join-Path $env:TEMP "OllamaSetup.exe"
            Invoke-WebRequest $OllamaUrl -OutFile $installer -UseBasicParsing
            $signature = Get-AuthenticodeSignature $installer
            if ($signature.Status -ne "Valid" -or $signature.SignerCertificate.Subject -notmatch "O=Ollama Inc\.") {
                throw "The Ollama download isn't signed by Ollama Inc.; not running it."
            }
            Start-Process $installer -ArgumentList "/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES" -Wait
            Remove-Item $installer -Force -ErrorAction SilentlyContinue
        }
        if (Test-Path $ollama) {
            $running = Get-Process -Name "ollama*" -ErrorAction SilentlyContinue
            if (-not $running) {
                Start-Process $ollama -ArgumentList "serve" -WindowStyle Hidden
                Start-Sleep -Seconds 4
            }
            & $ollama pull $Model
            if ($LASTEXITCODE -ne 0) {
                Write-Warning "Couldn't download $Model now; Jarvis will tell you, or run: ollama pull $Model"
            }
        } else {
            Write-Warning "Ollama didn't install. Get it from ollama.com, or pick Groq in Jarvis's settings."
        }
    }

    if ($cleanCache) {
        Remove-Item $env:UV_CACHE_DIR -Recurse -Force -ErrorAction SilentlyContinue
    }
    Step "Done. Start Jarvis from the Start menu or the desktop."
    Stop-Transcript | Out-Null
    exit 0
} catch {
    Write-Host ""
    Write-Host "Setup failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "The log is at $Log. Run setup again to retry."
    Stop-Transcript | Out-Null
    Read-Host "Press Enter to close"
    exit 1
}
