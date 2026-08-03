# Build the Docker image and save the deploy tarball.
#
#   .\build.ps1            build the image, smoke-test it, write movie-filter.tar.gz
#   .\build.ps1 -NoTarball  build and smoke-test only (fast; skips the ~3 min save)
#   .\build.ps1 -SkipTest   build and save without starting a container
#
# The tarball is how the image reaches Unraid: the server has no source tree to build
# from, so the image is built here and copied over. See docker-compose.deploy.yml.

[CmdletBinding()]
param(
    [switch]$NoTarball,
    [switch]$SkipTest,
    [string]$Tag     = "movie-filter:latest",
    [string]$Tarball = "movie-filter.tar.gz",
    # Spare host port for the smoke test. Only needs to be free on this machine.
    [int]$TestPort   = 18000
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$started = Get-Date

# docker writes harmless warnings ("No blkio throttle...") to stderr, which Windows
# PowerShell turns into a terminating NativeCommandError under $ErrorActionPreference
# = Stop. Run the quiet calls through here and judge them by exit code instead.
#
# Takes one array so docker's own flags are never bound as PowerShell parameters:
# passing `-p` through a [string[]]$Args splat binds it to the common -PipelineVariable
# and fails before docker is ever called.
function Invoke-Quiet([string[]]$Cmd) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Cmd[0] @($Cmd[1..($Cmd.Count - 1)]) 2>&1 | Out-Null
        return $LASTEXITCODE
    } finally { $ErrorActionPreference = $old }
}

# Same stderr problem as above, but for commands whose output should stay visible.
# docker build writes its progress to stderr, so this only bites when the caller pipes
# the script's output — `.\build.ps1` alone would appear to work.
function Invoke-Loud([string[]]$Cmd) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Cmd[0] @($Cmd[1..($Cmd.Count - 1)]) 2>&1 | ForEach-Object { Write-Host $_ }
        return $LASTEXITCODE
    } finally { $ErrorActionPreference = $old }
}

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "    $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "    $msg" -ForegroundColor Yellow }

# --- docker engine ----------------------------------------------------------------
# Docker Desktop exits without a running engine rather than starting one on demand, so
# check first: the failure otherwise surfaces as an opaque named-pipe error.
Step "Checking Docker"
if ((Invoke-Quiet @('docker','info')) -ne 0) {
    Warn "Docker engine is not running. Starting Docker Desktop..."
    $exe = "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe"
    if (-not (Test-Path $exe)) { throw "Docker Desktop not found at $exe. Start Docker manually and re-run." }
    Start-Process $exe
    $deadline = (Get-Date).AddMinutes(5)
    $ready = $false
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 5
        if ((Invoke-Quiet @('docker','info')) -eq 0) { $ready = $true; break }
    }
    if (-not $ready) { throw "Docker did not become ready within 5 minutes." }
}
Ok "engine ready"

# --- warn on uncommitted work -----------------------------------------------------
# The build uses the working tree, not HEAD, so what ships can differ from what is
# pushed. Not an error — deploying to test an in-progress change is normal here.
if (Get-Command git -ErrorAction SilentlyContinue) {
    $dirty = git status --porcelain 2>$null | Where-Object { $_ -notmatch '\.tar\.gz' }
    if ($dirty) {
        Warn "Working tree has uncommitted changes; the image will contain them:"
        $dirty | Select-Object -First 10 | ForEach-Object { Warn "  $_" }
    }
}

# --- build ------------------------------------------------------------------------
Step "Building $Tag"
if ((Invoke-Loud @('docker','build','-t',$Tag,'.')) -ne 0) { throw "docker build failed" }
$size = docker images $Tag --format "{{.Size}}" | Select-Object -First 1
Ok "built ($size)"

# --- smoke test -------------------------------------------------------------------
# Catches an image that builds but cannot serve: a bad import or a missing dependency
# would otherwise only show up on the server.
if (-not $SkipTest) {
    Step "Smoke test"
    $name = "movie-filter-smoke"
    Invoke-Quiet @('docker','rm','-f',$name) | Out-Null
    if ((Invoke-Quiet @('docker','run','-d','--name',$name,'-p',"${TestPort}:8000",$Tag)) -ne 0) {
        throw "could not start the smoke container"
    }

    try {
        $health = $null
        foreach ($i in 1..30) {
            Start-Sleep -Seconds 4
            try {
                $health = Invoke-RestMethod -Uri "http://localhost:$TestPort/api/health" -TimeoutSec 5
                break
            } catch { }
        }
        if (-not $health) {
            Invoke-Loud @('docker','logs','--tail','30',$name) | Out-Null
            throw "the container never answered /api/health on port $TestPort"
        }
        Ok "health: device=$($health.device) gpu_ok=$($health.gpu_ok)"
        # gpu_ok is false here by design: this runs without --gpus, so CPU fallback is
        # the correct answer. The GPU only matters on the server.
        Ok "(gpu_ok=false is expected locally - no --gpus flag)"
    } finally {
        Invoke-Quiet @('docker','rm','-f',$name) | Out-Null
    }
}

# --- tarball ----------------------------------------------------------------------
if (-not $NoTarball) {
    Step "Saving $Tarball (~2-3 min)"
    # bash gives a single pipeline to gzip; PowerShell's pipe would corrupt the stream by
    # decoding it as text, so shell out rather than piping here.
    $bash = "$env:ProgramFiles\Git\bin\bash.exe"
    if (Test-Path $bash) {
        if ((Invoke-Loud @($bash,'-lc',"docker save $Tag | gzip > $Tarball")) -ne 0) {
            throw "docker save failed"
        }
    } else {
        Warn "Git bash not found; saving uncompressed .tar instead"
        $Tarball = $Tarball -replace '\.gz$', ''
        if ((Invoke-Loud @('docker','save','-o',$Tarball,$Tag)) -ne 0) {
            throw "docker save failed"
        }
    }
    $mb = [math]::Round((Get-Item $Tarball).Length / 1MB, 1)
    Ok "$Tarball ($mb MB)"
}

$mins = [math]::Round(((Get-Date) - $started).TotalMinutes, 1)
Write-Host "`nDone in $mins min." -ForegroundColor Green
if (-not $NoTarball) {
    Write-Host "Copy $Tarball to the server, then there:" -ForegroundColor Gray
    Write-Host "  gunzip -c $Tarball | docker load" -ForegroundColor Gray
    Write-Host "  docker compose -f docker-compose.deploy.yml up -d" -ForegroundColor Gray
}
