# Run and delete Sautium nodes while testing the Windows install — the
# PowerShell half of scripts/test-node.sh, same three verbs.
#
#   .\scripts\test-node.ps1 run        the installed app against a throwaway data root
#   .\scripts\test-node.ps1 reset      stop that node's PostgreSQL and delete the root
#   .\scripts\test-node.ps1 wipe -Yes  delete the REAL node on this machine
#
# The launcher reads its locations from the profile variables, so the sandbox
# is those variables pointed at one folder: LOCALAPPDATA (data root, the
# app's own clone) and APPDATA (settings, account key). USERPROFILE stays
# real — %USERPROFILE%\.sautium keeps the browser-trusted certificate and the
# model cache, which are shared on purpose — and so does pip's cache, which
# LOCALAPPDATA would otherwise drag along. Ports are shifted off the
# defaults so the test node never claims what the node this machine already
# runs is using.
#
# NOT deleted by any of these: the installed program under
# %LOCALAPPDATA%\Programs\Sautium (the runtime is tooling), pip's cache,
# %USERPROFILE%\.cache\huggingface and the agents' npm prefixes.

param(
    [Parameter(Position = 0)][ValidateSet('run', 'reset', 'wipe')][string]$Verb,
    [switch]$Yes
)

$ErrorActionPreference = 'Stop'

$Sandbox = if ($env:SAUTIUM_TEST_ROOT) { $env:SAUTIUM_TEST_ROOT } else { Join-Path $env:TEMP 'sautium-test' }
$App = if ($env:SAUTIUM_APP) { $env:SAUTIUM_APP } else { Join-Path $env:LOCALAPPDATA 'Programs\Sautium' }
$Launcher = Join-Path $App 'runtime\Sautium.exe'

function Stop-Cluster([string]$DataRoot) {
    # PostgreSQL has to be stopped FIRST. Remove the data directory under a
    # live postmaster and it keeps running against nothing, holding the port
    # for the next test.
    $pgctl = Join-Path $DataRoot 'Sautium\app\pgsql\bin\pg_ctl.exe'
    $pgdata = Join-Path $DataRoot 'Sautium\pgdata'
    if ((Test-Path $pgctl) -and (Test-Path (Join-Path $pgdata 'PG_VERSION'))) {
        & $pgctl -D $pgdata -m fast stop 2>$null | Out-Null
    }
}

function Stop-Under([string]$Root) {
    # The backend and PostgreSQL run from binaries under the data root; the
    # launcher does not (it runs the shared runtime), see below.
    Get-Process | Where-Object { $_.Path -and $_.Path.StartsWith($Root, 'OrdinalIgnoreCase') } |
        Stop-Process -Force -ErrorAction SilentlyContinue
}

function Assert-LauncherQuit {
    # Every installed launcher is Sautium.exe from the same runtime, so a
    # test node's cannot be told from a real one's here. Quit it yourself.
    if (Get-Process -Name 'Sautium' -ErrorAction SilentlyContinue) {
        throw 'Sautium is running — quit it from the tray first (Quit), then rerun.'
    }
}

switch ($Verb) {
    'run' {
        if (-not (Test-Path $Launcher)) { throw "no installed Sautium at $App (set SAUTIUM_APP)" }
        Remove-Item -Recurse -Force $Sandbox -ErrorAction SilentlyContinue
        $local = Join-Path $Sandbox 'local'
        $roaming = Join-Path $Sandbox 'roaming'
        New-Item -ItemType Directory -Force $local, (Join-Path $roaming 'Sautium') | Out-Null
        # WriteAllText with a BOM-less encoding: Set-Content -Encoding UTF8
        # writes a BOM under Windows PowerShell, and the launcher reads JSON.
        $config = @'
{
  "first_run_complete": false,
  "ports": {"postgres": 15488, "web": 18088, "tracker": 18788,
            "media": 8846, "gena": 8847, "p2p_sync": 0}
}
'@
        [System.IO.File]::WriteAllText((Join-Path $roaming 'Sautium\config.json'), $config,
                                       (New-Object System.Text.UTF8Encoding $false))

        $env:PIP_CACHE_DIR = Join-Path $env:LOCALAPPDATA 'pip\Cache'
        $env:LOCALAPPDATA = $local
        $env:APPDATA = $roaming
        Start-Process -FilePath $Launcher -ArgumentList "`"$(Join-Path $App 'bootstrap.py')`"" -WorkingDirectory $App
        Write-Host "test node starting in $Sandbox (web 18088, postgres 15488)"
        Write-Host "logs: $local\Sautium\{bootstrap,launcher,backend}.log"
    }
    'reset' {
        Assert-LauncherQuit
        $local = Join-Path $Sandbox 'local'
        Stop-Under $local
        Stop-Cluster $local
        Remove-Item -Recurse -Force $Sandbox -ErrorAction SilentlyContinue
        Write-Host 'test node deleted'
    }
    'wipe' {
        if (-not $Yes) {
            Write-Error ("This deletes the REAL node on this machine: its database, account key, " +
                         "certificates and settings. Re-run: $PSCommandPath wipe -Yes")
            exit 1
        }
        Get-Process -Name 'Sautium' -ErrorAction SilentlyContinue | Stop-Process -Force
        Stop-Under (Join-Path $env:LOCALAPPDATA 'Sautium')
        Stop-Cluster $env:LOCALAPPDATA
        foreach ($dir in @((Join-Path $env:LOCALAPPDATA 'Sautium'),
                           (Join-Path $env:APPDATA 'Sautium'),
                           (Join-Path $env:USERPROFILE '.sautium'))) {
            # rmdir, not Remove-Item: git's pack files are read-only.
            if (Test-Path $dir) { cmd /c rmdir /s /q "$dir" }
        }
        Write-Host 'node deleted — the next launch starts from the setup wizard'
    }
    default {
        Write-Error "usage: $PSCommandPath run | reset | wipe -Yes"
        exit 1
    }
}
