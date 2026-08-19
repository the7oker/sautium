<#
.SYNOPSIS
    Installs (or removes) the Sautium master front: Caddy as a Windows service.

.DESCRIPTION
    Behind Docker Desktop the Docker peer surface sees every peer as the bridge
    gateway (user-space port hops rewrite the source address). This front
    terminates the peer TLS on :8801 and forwards plain HTTP + X-Forwarded-For
    to the container's loopback-only upstream 127.0.0.1:18801 (Caddyfile).

    Container side FIRST (WSL shell, repo root), or the port is still taken:
        .env:  P2P_SYNC_PUBLISH=127.0.0.1:18801
               P2P_TRUSTED_FRONT=1
        docker compose up -d backend

    Then, from an elevated PowerShell:  .\install.ps1
    Idempotent and re-runnable:
      1. fetches pinned caddy.exe (v2.11.4) and WinSW (v2.12.0) next to this
         script, verifying SHA-256 (skipped when already present and matching)
      2. deletes the netsh portproxy rule(s) listening on 8801 (the old hop)
      3. refuses if anything else still listens on TCP 8801
      4. installs/starts the "sautium-front" service (sautium-front.xml)
      5. smoke-tests https://localhost:8801/health through the front

    .\install.ps1 -Uninstall  stops and removes the service (binaries stay).
    Going back to the old topology = also re-add the portproxy rule and revert
    the two .env lines - see P2P_NETWORK.md "Master behind a trusted front".
#>
[CmdletBinding()]
param([switch]$Uninstall)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Here = $PSScriptRoot
$ServiceId = 'sautium-front'
$PeerPort = 8801
$UpstreamPort = 18801
$RepoRoot = (Resolve-Path (Join-Path $Here '..\..')).Path

$Caddy = @{
    Url       = 'https://github.com/caddyserver/caddy/releases/download/v2.11.4/caddy_2.11.4_windows_amd64.zip'
    ZipSha256 = '1708333f79e274c7697285afe6d592ab39314e0b131e9ec6bea08ad27df62ebf'
    ExeSha256 = '5cb9ab71e5756ce72840b8234177a2f40c8b4ab47a806b8e841e2b784e9df62b'
    Exe       = Join-Path $Here 'caddy.exe'
}
$WinSW = @{
    Url    = 'https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe'
    Sha256 = '05b82d46ad331cc16bdc00de5c6332c1ef818df8ceefcd49c726553209b3a0da'
    Exe    = Join-Path $Here "$ServiceId.exe"   # WinSW wants <id>.exe next to <id>.xml
}

function Assert-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $admin = ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $admin) { throw 'Run from an elevated PowerShell (services, netsh).' }
}

function Get-Sha256([string]$Path) {
    (Get-FileHash -Algorithm SHA256 -Path $Path).Hash.ToLowerInvariant()
}

function Test-Pinned([string]$Path, [string]$Sha256) {
    (Test-Path $Path) -and ((Get-Sha256 $Path) -eq $Sha256)
}

function Get-Pinned([string]$Url, [string]$Dest, [string]$Sha256) {
    $tmp = "$Dest.download"
    Write-Host "  downloading $Url"
    Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $tmp
    $got = Get-Sha256 $tmp
    if ($got -ne $Sha256) {
        Remove-Item $tmp -Force
        throw "SHA-256 mismatch for $Url`n  expected $Sha256`n  got      $got"
    }
    Move-Item -Force $tmp $Dest
}

function Install-Binaries {
    if (Test-Pinned $Caddy.Exe $Caddy.ExeSha256) {
        Write-Host "  caddy.exe present and pinned"
    } else {
        $zip = Join-Path $Here 'caddy.zip'
        Get-Pinned $Caddy.Url $zip $Caddy.ZipSha256
        $stage = Join-Path $Here 'caddy.unzip'
        if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
        Expand-Archive -Path $zip -DestinationPath $stage
        $exe = Join-Path $stage 'caddy.exe'
        if ((Get-Sha256 $exe) -ne $Caddy.ExeSha256) { throw 'caddy.exe inside the pinned zip does not match its pin' }
        Move-Item -Force $exe $Caddy.Exe
        Remove-Item $stage -Recurse -Force
        Remove-Item $zip -Force
    }
    if (Test-Pinned $WinSW.Exe $WinSW.Sha256) {
        Write-Host "  $ServiceId.exe (WinSW) present and pinned"
    } else {
        Get-Pinned $WinSW.Url $WinSW.Exe $WinSW.Sha256
    }
}

function Remove-PortProxy {
    # Data rows are "<listen addr> <listen port> <connect addr> <connect port>" - locale-proof.
    $lines = netsh interface portproxy show v4tov4 | Where-Object { $_ -match "^\s*\S+\s+$PeerPort\s+\S+\s+\d+\s*$" }
    if (-not $lines) { Write-Host "  no portproxy rule on $PeerPort"; return }
    foreach ($line in $lines) {
        $f = ($line.Trim() -split '\s+')
        Write-Host "  deleting portproxy $($f[0]):$($f[1]) -> $($f[2]):$($f[3])"
        netsh interface portproxy delete v4tov4 listenport=$($f[1]) listenaddress=$($f[0]) | Out-Null
    }
}

function Assert-PortFree {
    # Wildcard/LAN listeners would swallow peer traffic and block Caddy's bind.
    # A loopback-only straggler (WSL's [::1] relay of a port the VM no longer
    # binds) intercepts nothing from outside and coexists with the wildcard
    # bind - report it, do not refuse.
    $listeners = @(Get-NetTCPConnection -LocalPort $PeerPort -State Listen -ErrorAction SilentlyContinue)
    if (-not $listeners) { Write-Host "  TCP $PeerPort is free"; return }
    $hints = @()
    $blocking = $false
    foreach ($l in $listeners) {
        $p = Get-Process -Id $l.OwningProcess -ErrorAction SilentlyContinue
        $name = if ($p) { $p.ProcessName } else { '?' }
        $loopback = $l.LocalAddress -in @('127.0.0.1', '::1')
        Write-Host ("    {0,-18} pid {1,-7} {2}{3}" -f $l.LocalAddress, $l.OwningProcess, $name, $(if ($loopback) { '  (loopback only, tolerated)' } else { '' }))
        if ($loopback) { continue }
        $blocking = $true
        switch -Regex ($name) {
            'com\.docker\.backend' { $hints += "the container still publishes $PeerPort - set P2P_SYNC_PUBLISH=127.0.0.1:$UpstreamPort in .env and run 'docker compose up -d backend' in WSL" }
            'svchost'              { $hints += "a portproxy rule is still there - 'netsh interface portproxy show v4tov4'" }
            'wslrelay'             { $hints += "WSL relays $PeerPort from the VM - something inside WSL still binds it; recreate the container (above)" }
        }
    }
    if ($blocking) { throw ("port $PeerPort is not free.`n  " + (($hints | Select-Object -Unique) -join "`n  ")) }
    Write-Host "  TCP $PeerPort has no wildcard/LAN listener"
}

function Assert-EnvKnobs {
    $envFile = Join-Path $RepoRoot '.env'
    if (-not (Test-Path $envFile)) { Write-Warning ".env not found at $envFile - cannot check the container knobs"; return }
    $text = Get-Content $envFile -Raw
    $missing = @()
    if ($text -notmatch "(?m)^\s*P2P_SYNC_PUBLISH\s*=\s*127\.0\.0\.1:$UpstreamPort\s*$") { $missing += "P2P_SYNC_PUBLISH=127.0.0.1:$UpstreamPort" }
    if ($text -notmatch "(?m)^\s*P2P_TRUSTED_FRONT\s*=\s*1\s*$") { $missing += 'P2P_TRUSTED_FRONT=1' }
    if ($missing) {
        Write-Warning (".env lacks: " + ($missing -join ', ') + " - the container will not trust the front / will not publish on loopback. Add them and 'docker compose up -d backend' (WSL).")
    } else {
        Write-Host "  .env carries the container knobs"
    }
}

function Assert-FirewallRule {
    # A port filter's InstanceID is its rule's Name. Port-based allow rules
    # cover any program, so Caddy needs no rule of its own.
    $rule = $null
    $filters = @(Get-NetFirewallPortFilter -ErrorAction SilentlyContinue |
        Where-Object { $_.Protocol -eq 'TCP' -and "$($_.LocalPort)" -eq "$PeerPort" })
    foreach ($f in $filters) {
        $r = Get-NetFirewallRule -Name $f.InstanceID -ErrorAction SilentlyContinue
        if ($r -and "$($r.Enabled)" -eq 'True' -and "$($r.Direction)" -eq 'Inbound' -and "$($r.Action)" -eq 'Allow') { $rule = $r; break }
    }
    if ($rule) { Write-Host "  inbound firewall allow for TCP $PeerPort`: '$($rule.DisplayName)'" }
    else { Write-Warning "no inbound firewall allow for TCP $PeerPort - New-NetFirewallRule -DisplayName 'Sautium P2P (TCP $PeerPort)' -Direction Inbound -Protocol TCP -LocalPort $PeerPort -Action Allow -Profile Any" }
}

function Get-ServiceOrNull { Get-Service -Name $ServiceId -ErrorAction SilentlyContinue }

function Wait-ServiceState([string]$State, [int]$Seconds = 15) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    do {
        $svc = Get-ServiceOrNull
        if ($svc -and "$($svc.Status)" -eq $State) { return $true }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    return $false
}

function Invoke-WinSW([string]$Verb) {
    & $WinSW.Exe $Verb | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "WinSW '$Verb' failed (exit $LASTEXITCODE) - see $Here\$ServiceId.wrapper.log" }
}

function Test-Front {
    $out = (& curl.exe -sk --max-time 8 -o - -w '%{http_code}' "https://localhost:$PeerPort/health" 2>$null) -join "`n"
    $code = if ($out) { $out.Substring([Math]::Max(0, $out.Length - 3)) } else { '' }
    $body = if ($out -and $out.Length -gt 3) { $out.Substring(0, $out.Length - 3) } else { '' }
    if ($code -eq '200' -and $body -match 'sautium-peer') {
        Write-Host "  https://localhost:$PeerPort/health -> 200 sautium-peer (front + upstream OK)"
    } elseif ($code -eq '502') {
        Write-Warning "front answers, upstream 127.0.0.1:$UpstreamPort does not (502) - is the container up with P2P_SYNC_PUBLISH=127.0.0.1:$UpstreamPort?"
    } else {
        Write-Warning "unexpected answer from the front: http $code - check $Here\$ServiceId.err.log"
    }
}

function Invoke-Main {
    Assert-Admin

    if ($Uninstall) {
        Write-Host "Removing the $ServiceId service"
        $svc = Get-ServiceOrNull
        if (-not $svc) { Write-Host "  not installed"; return }
        if (-not (Test-Path $WinSW.Exe)) { throw "$($WinSW.Exe) missing - cannot drive the service; use 'sc.exe delete $ServiceId' by hand" }
        if ("$($svc.Status)" -ne 'Stopped') { Invoke-WinSW stop; [void](Wait-ServiceState 'Stopped') }
        Invoke-WinSW uninstall
        Write-Host "  removed. The portproxy rule was NOT re-added; the container is still on loopback until .env is reverted."
        return
    }

    Write-Host "Sautium master front -> $Here"
    Write-Host "[1/5] binaries"
    Install-Binaries
    Write-Host "[2/5] container knobs"
    Assert-EnvKnobs
    Write-Host "[3/5] old hop"
    Remove-PortProxy
    $svc = Get-ServiceOrNull
    if ($svc -and "$($svc.Status)" -ne 'Stopped') {
        Write-Host "  stopping the running $ServiceId before the port check"
        Invoke-WinSW stop
        if (-not (Wait-ServiceState 'Stopped')) { throw "$ServiceId did not stop" }
    }
    Assert-PortFree
    Write-Host "[4/5] service"
    if (-not (Get-ServiceOrNull)) { Invoke-WinSW install; Write-Host "  installed" }
    Invoke-WinSW start
    if (-not (Wait-ServiceState 'Running')) { throw "$ServiceId did not reach Running - see $Here\$ServiceId.wrapper.log and $ServiceId.err.log" }
    Write-Host "  running (auto-start, restart on failure)"
    Assert-FirewallRule
    Write-Host "[5/5] smoke test"
    Start-Sleep -Seconds 2
    Test-Front
    Write-Host ""
    Write-Host "Done. Verify real peer addresses land on the master (WSL):"
    Write-Host "  docker exec sautium-postgres psql -U musicai -d music_ai -c ""SELECT addr, count(*) FROM p2p_contact_events WHERE ts > now() - interval '15 min' GROUP BY 1"""
    Write-Host "  (one token per distinct client; the bridge-gateway token 219df670-... must stop growing)"
}

# Transcript next to the script: the run is usually launched elevated from
# another session (UAC), whose console nobody reads back.
Start-Transcript -Path (Join-Path $Here 'install.log') -Append | Out-Null
$failed = $false
try {
    Invoke-Main
} catch {
    Write-Host "FAILED: $($_.Exception.Message)" -ForegroundColor Red
    $failed = $true
} finally {
    Stop-Transcript | Out-Null
}
if ($failed) { exit 1 }
