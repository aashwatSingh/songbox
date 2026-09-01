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
    & $python -m uvicorn app.main:app --port 8000
} finally {
    Pop-Location
}
