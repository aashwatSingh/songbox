# Starts the Songbox API reliably for a live demo: brings up Docker Desktop if it isn't running,
# brings up the Postgres/Redis/MinIO stack, waits for Postgres to actually accept connections
# (not just for the container to exist), applies any pending migrations, then execs uvicorn in the
# foreground so the launcher (Browser pane preview_start) can track it by port.
#
# Uses its own database (songbox_demo, same Postgres instance/container as everything else) rather
# than the shared "songbox" database -- other worktrees on this machine run their own in-progress
# migrations against the shared DB (e.g. an unmerged migration 0010 was found there), and this demo
# must never be broken or blocked by another branch's experimental schema state.

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$apiDir = Join-Path $repoRoot "services\api"
$python = "C:\Users\aashw\AppData\Local\Programs\Python\Python313\python.exe"

$env:DATABASE_URL = "postgresql+psycopg://songbox:songbox@localhost:5433/songbox_demo"
$env:APP_DATABASE_URL = "postgresql+psycopg://songbox_app:songbox_app@localhost:5433/songbox_demo"

# Load secrets from services/api/.env (gitignored -- see .env.example for the format). Nothing else
# in this project reads a .env file; the app itself only ever reads os.environ, so this script is
# the single place that bridges the two. Values already set in the environment win, so a real
# deployment's secret manager is never overridden by a stale local file.
$envFile = Join-Path $apiDir ".env"
if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile) {
        $trimmed = $line.Trim()
        if ($trimmed -eq "" -or $trimmed.StartsWith("#")) { continue }
        $split = $trimmed.IndexOf("=")
        if ($split -lt 1) { continue }
        $name = $trimmed.Substring(0, $split).Trim()
        # Strip optional surrounding quotes; a pasted key often arrives wrapped in them.
        $value = $trimmed.Substring($split + 1).Trim().Trim('"').Trim("'")
        if (-not [Environment]::GetEnvironmentVariable($name)) {
            Set-Item -Path "env:$name" -Value $value
        }
    }
}

# Say plainly what a missing key COSTS, rather than letting it fail silently downstream. Without it
# every AcoustID lookup errors, and the gate's correct-but-confusing response is to HOLD the upload
# at pending_review -- so every single upload looks like it silently did nothing. This warning
# exists because that exact behavior burned real debugging time.
if (-not $env:ACOUSTID_API_KEY) {
    Write-Host ""
    Write-Host "WARNING: ACOUSTID_API_KEY is not set." -ForegroundColor Yellow
    Write-Host "  Every upload will be HELD at pending_review (the gate cannot verify" -ForegroundColor Yellow
    Write-Host "  fingerprints), so auto-processing will never start. Get a key at" -ForegroundColor Yellow
    Write-Host "  https://acoustid.org/new-application and put it in services\api\.env" -ForegroundColor Yellow
    Write-Host "  as ACOUSTID_API_KEY=... (see services\api\.env.example)." -ForegroundColor Yellow
    Write-Host ""
} else {
    Write-Host "ACOUSTID_API_KEY loaded (fingerprint checks enabled)."
}

# Say it out loud on every start. A gate that silently stopped enforcing is far worse than one
# that is noisy about it, and this is exactly the kind of setting that gets turned on for a
# local experiment and then forgotten about on a machine that later serves someone else.
if ($env:SONGBOX_PERSONAL_MODE -and $env:SONGBOX_PERSONAL_MODE -notin @("0", "false", "no", "off")) {
    Write-Host ""
    Write-Host "SONGBOX_PERSONAL_MODE is ON -- the rights gate is NOT enforcing." -ForegroundColor Yellow
    Write-Host "  Every upload passes regardless of what the fingerprint check finds." -ForegroundColor Yellow
    Write-Host "  Intended for a single-user personal install only. Unset it in" -ForegroundColor Yellow
    Write-Host "  services\api\.env before serving anyone else." -ForegroundColor Yellow
    Write-Host ""
}

function Test-DockerRunning {
    docker info *> $null
    return $LASTEXITCODE -eq 0
}

if (-not (Test-DockerRunning)) {
    Write-Host "Docker Desktop isn't running -- starting it..."
    Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    $waited = 0
    while (-not (Test-DockerRunning)) {
        if ($waited -ge 90) {
            Write-Error "Docker Desktop did not come up within 90 seconds."
            exit 1
        }
        Start-Sleep -Seconds 3
        $waited += 3
    }
    Write-Host "Docker Desktop is up."
}

Push-Location $repoRoot
try {
    docker compose up -d
} finally {
    Pop-Location
}

Write-Host "Waiting for Postgres to accept connections..."
$waited = 0
while ($true) {
    docker exec songbox-postgres-1 pg_isready -U songbox *> $null
    if ($LASTEXITCODE -eq 0) { break }
    if ($waited -ge 60) {
        Write-Error "Postgres did not become ready within 60 seconds."
        exit 1
    }
    Start-Sleep -Seconds 2
    $waited += 2
}
Write-Host "Postgres is ready."

Push-Location $apiDir
try {
    & $python -m alembic upgrade head
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Database migration failed."
        exit 1
    }
    # --reload: without it, backend code changes require killing and restarting this whole script
    # to take effect -- a real, repeated source of confusion during development (a new/changed
    # endpoint silently 404s or serves stale behavior until someone remembers to restart). Known
    # tradeoff: once a request is genuinely long-running (the separate/transcribe/package pipeline
    # chain can take minutes on a real song), a file save that triggers a reload mid-request will
    # kill that in-flight request -- standard behavior for any hot-reloading dev server, not worth
    # avoiding --reload over.
    & $python -m uvicorn app.main:app --port 8000 --reload
} finally {
    Pop-Location
}
