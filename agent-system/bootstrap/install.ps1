# Bob Agent one-liner installer (Windows PowerShell).
#
#   irm https://raw.githubusercontent.com/johan-droid/Bob-Agent/main/agent-system/bootstrap/install.ps1 | iex
#
# Override the download base with $env:BOB_BOOTSTRAP_BASE.
# From a local checkout this script just runs .\bootstrap\bootstrap.py.

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------
function Get-PlatformInfo {
    $info = @{
        OS = "Windows"
        Arch = if ([Environment]::Is64BitOperatingSystem) { "x64" } else { "x86" }
        Edition = (Get-CimInstance Win32_OperatingSystem).Caption
    }
    return $info
}

# ---------------------------------------------------------------------------
# Package manager detection
# ---------------------------------------------------------------------------
function Get-PackageManager {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        return "winget"
    }
    if (Get-Command choco -ErrorAction SilentlyContinue) {
        return "chocolatey"
    }
    if (Get-Command scoop -ErrorAction SilentlyContinue) {
        return "scoop"
    }
    return $null
}

# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------
function Write-ErrorAndExit {
    param([string]$Message)
    Write-Host "[install] ERROR: $Message" -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------------------
# Platform-specific notes
# ---------------------------------------------------------------------------
function Write-PlatformNotes {
    $platform = Get-PlatformInfo
    Write-Host "[install] Platform: Windows ($($platform.Arch))"
    Write-Host "[install] Tips:"
    Write-Host "  - Enable WSL2 for best compatibility: wsl --install"
    Write-Host "  - Docker Desktop with WSL2 backend is recommended"
    Write-Host "  - Run PowerShell as Administrator for system-wide installs"
}

# ---------------------------------------------------------------------------
# Python detection and installation
# ---------------------------------------------------------------------------
function Find-Python {
    # Check for py launcher first (recommended on Windows)
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        try {
            $version = & py -3.12 --version 2>&1
            if ($version -match "Python 3\.(1[2-9]|[2-9][0-9])") {
                return $py.Source
            }
        } catch { }
    }
    # Check for python/python3
    foreach ($cand in @("python", "python3")) {
        $cmd = Get-Command $cand -ErrorAction SilentlyContinue
        if ($cmd) {
            try {
                $version = & $cand --version 2>&1
                if ($version -match "Python 3\.(1[2-9]|[2-9][0-9])") {
                    return $cmd.Source
                }
            } catch { }
        }
    }
    return $null
}

function Install-Python {
    $pm = Get-PackageManager
    Write-Host "[install] No Python 3.12+ found. Attempting installation..." -ForegroundColor Yellow
    
    switch ($pm) {
        "winget" {
            Write-Host "[install] Installing Python via winget..."
            winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
            if ($LASTEXITCODE -ne 0) { throw "winget install failed" }
        }
        "chocolatey" {
            Write-Host "[install] Installing Python via Chocolatey..."
            choco install python -y
            if ($LASTEXITCODE -ne 0) { throw "choco install failed" }
        }
        "scoop" {
            Write-Host "[install] Installing Python via Scoop..."
            scoop install python
            if ($LASTEXITCODE -ne 0) { throw "scoop install failed" }
        }
        default {
            Write-Host "[install] No package manager found. Install Python manually:" -ForegroundColor Red
            Write-Host "  winget:       winget install -e --id Python.Python.3.12" -ForegroundColor Yellow
            Write-Host "  Chocolatey:   choco install python" -ForegroundColor Yellow
            Write-Host "  Scoop:        scoop install python" -ForegroundColor Yellow
            Write-Host "  Download:     https://www.python.org/downloads/windows/" -ForegroundColor Yellow
            return $null
        }
    }
    
    # Refresh PATH after installation
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
    return Find-Python
}

# ---------------------------------------------------------------------------
# Docker detection
# ---------------------------------------------------------------------------
function Test-Docker {
    $docker = Get-Command docker -ErrorAction SilentlyContinue
    if (-not $docker) {
        Write-Host "[install] Docker not found. Docker is needed for sandbox container isolation (optional)." -ForegroundColor Yellow
        Write-Host "  Install options:" -ForegroundColor Yellow
        Write-Host "    1. Docker Desktop: https://www.docker.com/products/docker-desktop/" -ForegroundColor Yellow
        Write-Host "    2. Via winget: winget install -e --id Docker.DockerDesktop" -ForegroundColor Yellow
        Write-Host "    3. Via Chocolatey: choco install docker-desktop -y" -ForegroundColor Yellow
        return $false
    }
    
    # Check if Docker is running
    try {
        $null = docker info 2>&1
        return $true
    } catch {
        Write-Host "[install] Docker found but not running. Start Docker Desktop." -ForegroundColor Yellow
        return $false
    }
}

# ---------------------------------------------------------------------------
# Git detection
# ---------------------------------------------------------------------------
function Test-Git {
    $git = Get-Command git -ErrorAction SilentlyContinue
    if (-not $git) {
        Write-Host "[install] git not found. Install git:" -ForegroundColor Yellow
        Write-Host "  winget:       winget install -e --id Git.Git" -ForegroundColor Yellow
        Write-Host "  Chocolatey:   choco install git -y" -ForegroundColor Yellow
        Write-Host "  Download:     https://git-scm.com/download/win" -ForegroundColor Yellow
        return $false
    }
    return $true
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
Write-PlatformNotes

$Base = if ($env:BOB_BOOTSTRAP_BASE) { $env:BOB_BOOTSTRAP_BASE } else { "https://raw.githubusercontent.com/johan-droid/Bob-Agent/main/agent-system/bootstrap" }

# Check for local checkout
$Local = $null
foreach ($cand in @("bootstrap/bootstrap.py", "agent-system/bootstrap/bootstrap.py")) {
    if (Test-Path $cand) { $Local = $cand; break }
}

if ($Local) {
    Write-Host "[install] using local $Local"
    $py = Find-Python
    if (-not $py) {
        $py = Install-Python
    }
    if (-not $py) {
        Write-ErrorAndExit "No Python found. Install Python 3.12+ and re-run."
    }
    & $py $Local @args
    exit $LASTEXITCODE
}

# Find or install Python
$py = Find-Python
if (-not $py) {
    $py = Install-Python
}
if (-not $py) {
    Write-ErrorAndExit "No Python found. Install Python 3.12+ and re-run."
}

# Check for Docker (warn but don't fail)
Test-Docker | Out-Null

# Check for git (warn but don't fail)
Test-Git | Out-Null

# Download and run bootstrap.py
$tmp = Join-Path ([System.IO.Path]::GetTempPath()) "bob-bootstrap.py"
Write-Host "[install] Downloading bootstrap.py..."
try {
    Invoke-WebRequest -Uri "$Base/bootstrap.py" -OutFile $tmp
} catch {
    Write-ErrorAndExit "Failed to download bootstrap.py: $_"
}

Write-Host "[install] Running bootstrap with Python: $py"
& $py $tmp @args
exit $LASTEXITCODE
