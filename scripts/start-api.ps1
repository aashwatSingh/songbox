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
$personalMode = $env:SONGBOX_PERSONAL_MODE -and `
    $env:SONGBOX_PERSONAL_MODE -notin @("0", "false", "no", "off")
if ($personalMode) {
    Write-Host ""
    Write-Host "SONGBOX_PERSONAL_MODE is ON -- the rights gate is NOT enforcing." -ForegroundColor Yellow
    Write-Host "  Every upload passes regardless of what the fingerprint check finds." -ForegroundColor Yellow
    Write-Host "  Intended for a single-user personal install only. Unset it in" -ForegroundColor Yellow
    Write-Host "  services\api\.env before serving anyone else." -ForegroundColor Yellow
    Write-Host ""
}

# See the bind-address comment further down for the full reasoning. Checked here, next to the
# personal-mode warning, because the two settings are only dangerous *together*: an unenforced
# rights gate behind a loopback-only socket serves exactly one machine, which is the documented
# single-user case. The same gate behind a wide-open socket is the "serving more than one person"
# state CLAUDE.md forbids, and 0.0.0.0 makes that true of every interface at once.
$bindHost = if ($env:SONGBOX_BIND_HOST) { $env:SONGBOX_BIND_HOST } else { "127.0.0.1" }
if ($personalMode -and $bindHost -eq "0.0.0.0") {
    Write-Host ""
    Write-Host "REFUSING TO START: SONGBOX_BIND_HOST=0.0.0.0 with the rights gate unenforced." -ForegroundColor Red
    Write-Host "  0.0.0.0 listens on every interface, so the only thing keeping this off" -ForegroundColor Red
    Write-Host "  whatever network you are on is a Windows Firewall profile classification" -ForegroundColor Red
    Write-Host "  that nothing here can verify. Behind it: unauthenticated signup and a gate" -ForegroundColor Red
    Write-Host "  that passes every upload." -ForegroundColor Red
    Write-Host "  Set SONGBOX_BIND_HOST to this machine's Tailscale address (tailscale ip -4)," -ForegroundColor Red
    Write-Host "  or unset SONGBOX_PERSONAL_MODE." -ForegroundColor Red
    Write-Host ""
    exit 1
}

function Test-DockerRunning {
    # Wrapped in try/catch, not a bare call: with $ErrorActionPreference = "Stop" (set at the top
    # of this script), PowerShell 5.1 wraps a native command's stderr output in a terminating
    # NativeCommandError the instant that stream is redirected at all -- even redirected to $null,
    # even though the command's own actual exit code is all this function cares about. Confirmed
    # for real: with Docker Desktop not yet running, `docker info` writes its "cannot connect"
    # message to stderr, and that turned into an uncaught exception that silently killed this
    # entire script before it ever reached the Docker-Desktop-auto-start logic below -- the exact
    # case that logic exists to handle. The bug was latent through every earlier run this session
    # only because Docker already happened to be running each time.
    try {
        docker info *> $null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
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
    # Same try/catch reasoning as Test-DockerRunning above: `docker exec` writes to stderr (not
    # just a nonzero exit code) when the target container doesn't exist yet or isn't running --
    # a real possibility on the very first iteration here, right after `docker compose up -d`
    # returns but before the container has actually started. With $ErrorActionPreference = "Stop"
    # that stderr write becomes a terminating exception, exactly like the Docker-Desktop check.
    try {
        docker exec songbox-postgres-1 pg_isready -U songbox *> $null
    } catch {
        $LASTEXITCODE = 1
    }
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
    # Bind address. uvicorn's own default is 127.0.0.1 (loopback only) -- confirmed via `netstat`,
    # the socket was bound to 127.0.0.1:8000 rather than 0.0.0.0:8000, so the kernel refused every
    # non-loopback connection, including Tailscale's. That default is kept here, because it is the
    # right one for plain local dev and fails closed.
    #
    # To reach this API from your own other devices, set SONGBOX_BIND_HOST to THIS machine's
    # Tailscale address (`tailscale ip -4`). Deliberately not 0.0.0.0: binding one specific
    # address means the kernel will not accept a connection arriving on the Wi-Fi adapter at all,
    # so reachability is a property of this process rather than of Windows Firewall's opinion
    # about the current network. Windows stores that opinion per network *profile*, not per
    # adapter -- so with 0.0.0.0 the day this laptop joins a network Windows classifies Private,
    # ports open to that whole LAN, where unauthenticated signup plus SONGBOX_PERSONAL_MODE's
    # unenforced rights gate are waiting. One narrower bind removes that entire failure mode.
    #
    # Tradeoff, deliberate: while bound to the Tailscale address, http://localhost:8000 no longer
    # answers on this machine -- use the Tailscale address from here too (it works locally). If
    # Tailscale is down, the bind fails immediately and loudly rather than quietly falling back to
    # something more exposed.
    Write-Host "Binding API to $bindHost`:8000"
    #
    # --reload: without it, backend code changes require killing and restarting this whole script
    # to take effect -- a real, repeated source of confusion during development (a new/changed
    # endpoint silently 404s or serves stale behavior until someone remembers to restart). Known
    # tradeoff: once a request is genuinely long-running (the separate/transcribe/package pipeline
    # chain can take minutes on a real song), a file save that triggers a reload mid-request will
    # kill that in-flight request -- standard behavior for any hot-reloading dev server, not worth
    # avoiding --reload over.
    & $python -m uvicorn app.main:app --host $bindHost --port 8000 --reload
} finally {
    Pop-Location
}
