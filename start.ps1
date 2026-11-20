#Requires -Version 5.1
<#
.SYNOPSIS
  NoNonCents guided start for Windows (PowerShell 5.1 and 7).

.DESCRIPTION
  1. Checks that Docker and Docker Compose are installed and running.
  2. Creates .env from .env.example with fresh random secrets (an existing .env is never changed).
  3. Builds and starts the app and its database with Docker Compose.
  4. Waits until the app answers its health check.
  5. Offers to create the first administrator, unless one already exists.
  6. Prints the address to open.

  Safe to run again at any time. Optional environment variables: WEB_BIND (ADDRESS:PORT, default
  127.0.0.1:8000) and COMPOSE_PROJECT_NAME (default nononcents).

  Run it with:  .\start.ps1     or double-click start.cmd (which allows the script to run).
#>
# Native tools write progress to stderr, which PowerShell 5.1 can turn into errors, so errors are
# handled explicitly through exit codes.
$ErrorActionPreference = 'Continue'
Set-Location -LiteralPath $PSScriptRoot

function Fail([string]$Message) {
    Write-Host "Error: $Message" -ForegroundColor Red
    exit 1
}

# Runs a native command quietly and returns $true when it exits with status 0.
function Test-Native([scriptblock]$Command) {
    $global:LASTEXITCODE = 0
    try { & $Command *> $null } catch { return $false }
    return ($LASTEXITCODE -eq 0)
}

# --- 1. Docker ---------------------------------------------------------------------------------
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Fail 'Docker is not installed. Install Docker Desktop: https://docs.docker.com/get-docker/'
}
if (-not (Test-Native { docker info })) {
    Fail 'Docker is installed but not running. Start Docker Desktop, wait until it says it is running, then run start.ps1 again.'
}
if (-not (Test-Native { docker compose version })) {
    Fail "The 'docker compose' command (Compose v2) is not available. Update Docker Desktop: https://docs.docker.com/compose/install/"
}

# --- 2. .env -----------------------------------------------------------------------------------
function New-RandomString([int]$Length) {
    # Letters and digits only, so the value is safe in .env and in SQL.
    $chars = [char[]]'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $byte = New-Object byte[] 1
    $text = New-Object System.Text.StringBuilder
    while ($text.Length -lt $Length) {
        $rng.GetBytes($byte)
        if ($byte[0] -lt 248) { [void]$text.Append($chars[$byte[0] % 62]) }  # 248 = 4 * 62: no bias
    }
    $rng.Dispose()
    return $text.ToString()
}

function Set-EnvValue([string]$Path, [string]$Key, [string]$Value) {
    $lines = [System.IO.File]::ReadAllLines($Path)
    $found = $false
    for ($i = 0; $i -lt $lines.Length; $i++) {
        if ($lines[$i].StartsWith("$Key=")) { $lines[$i] = "$Key=$Value"; $found = $true }
    }
    if (-not $found) { $lines += "$Key=$Value" }
    # UTF-8 without a byte order mark and with plain line feeds, as Docker Compose expects.
    [System.IO.File]::WriteAllText($Path, (($lines -join "`n") + "`n"), (New-Object System.Text.UTF8Encoding($false)))
}

function Get-EnvValue([string]$Key, [string]$Default) {
    # The shell's value wins, then .env, then the default.
    $fromShell = [Environment]::GetEnvironmentVariable($Key)
    if ($fromShell) { return $fromShell }
    if (Test-Path -LiteralPath '.env') {
        $match = Select-String -LiteralPath '.env' -Pattern "^$Key=(.*)$" | Select-Object -Last 1
        if ($match) {
            $value = $match.Matches[0].Groups[1].Value.Trim('"')
            if ($value) { return $value }
        }
    }
    return $Default
}

if (Test-Path -LiteralPath '.env') {
    Write-Host 'Using your existing .env (not changed).'
} else {
    if (-not (Test-Path -LiteralPath '.env.example')) { Fail '.env.example is missing; run this from the NoNonCents folder.' }
    Write-Host 'Creating .env with new random secrets.'
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
    Set-EnvValue '.env' 'DJANGO_SECRET_KEY' (New-RandomString 64)
    Set-EnvValue '.env' 'POSTGRES_PASSWORD' (New-RandomString 40)
    if ($env:WEB_BIND) { Set-EnvValue '.env' 'WEB_BIND' $env:WEB_BIND }
}

# Folders the containers share with the backup scripts.
New-Item -ItemType Directory -Force -Path 'backups/status', 'backups/requests' | Out-Null

# --- 3. Build and start ------------------------------------------------------------------------
Write-Host 'Building and starting NoNonCents (the first build takes a few minutes)...'
& docker compose up -d --build
if ($LASTEXITCODE -ne 0) { Fail 'docker compose could not start the app. Read the messages above, or run: docker compose logs' }

# --- 4. Wait for the health check --------------------------------------------------------------
$bind = Get-EnvValue 'WEB_BIND' '127.0.0.1:8000'
$split = $bind.LastIndexOf(':')
$hostName = $bind.Substring(0, $split)
$port = $bind.Substring($split + 1)
if (@('', '0.0.0.0', '::', '[::]', '*') -contains $hostName) { $hostName = '127.0.0.1' }
$url = "http://${hostName}:$port"

function Test-Health {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "$url/health/" -TimeoutSec 5
        return ($response.StatusCode -eq 200)
    } catch { return $false }
}

Write-Host "Waiting for the app to answer at $url/health/ ..."
$waited = 0
while (-not (Test-Health)) {
    Start-Sleep -Seconds 2
    $waited += 2
    if ($waited -ge 180) {
        & docker compose logs --tail=40 web
        Fail 'The app did not become healthy within 3 minutes. See the log above (docker compose logs web).'
    }
}
Write-Host 'The app is running.'

# --- 5. First administrator --------------------------------------------------------------------
$hasAdminCode = 'import sys; from django.contrib.auth import get_user_model as g; sys.exit(0 if g().objects.filter(is_superuser=True).exists() else 1)'
$hasAdmin = Test-Native { docker compose exec -T web python manage.py shell -c $hasAdminCode }
$interactive = [Environment]::UserInteractive -and -not [Console]::IsInputRedirected -and -not [Console]::IsOutputRedirected

if ($hasAdmin) {
    Write-Host 'An administrator already exists.'
} elseif ($interactive) {
    $answer = Read-Host 'Create the first administrator now? [Y/n]'
    if ($answer -eq '' -or $answer -match '^[Yy]') {
        Write-Host 'Choose a username and a password of at least 12 characters (an email is optional).'
        & docker compose exec web python manage.py createsuperuser
        if ($LASTEXITCODE -ne 0) {
            Write-Host 'No administrator was created. Run start.ps1 again, or: docker compose exec web python manage.py createsuperuser'
        }
    } else {
        Write-Host 'Skipped. Create one later with: docker compose exec web python manage.py createsuperuser'
    }
} else {
    Write-Host 'No administrator exists yet. Create one with: docker compose exec web python manage.py createsuperuser'
}

# --- 6. Done -----------------------------------------------------------------------------------
Write-Host ''
Write-Host "NoNonCents is ready: $url" -ForegroundColor Green
Write-Host 'Sign in with your administrator account; you will create your first budget profile there.'
Write-Host 'Stop it with: docker compose down      (never add -v: that deletes your data)'
