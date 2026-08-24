#!/usr/bin/env bash
# Run and delete Sautium nodes while testing the macOS app.
#
# Plain `rm -rf` is the whole deletion, but two things around it are easy to
# get wrong by hand:
#
#   - PostgreSQL has to be stopped FIRST. Remove the data directory under a
#     live postmaster and it keeps running against nothing, holding the port
#     for the next test.
#   - On the maintainer's machine the test node and the real one are the SAME
#     paths (~/.config/Sautium, ~/.local/share/Sautium). Wiping to test the
#     install flow would take the real node with it.
#
# So: `run` starts the installed app against a throwaway data root, `reset`
# deletes that, and `wipe` is the deliberate destruction of the real node.
#
# `run` strips LANG/LC_* on purpose — that is what a launch from the Dock
# looks like, and PostgreSQL 18 refuses to start without a locale. Testing
# from a terminal hides that class of bug, which is exactly how it shipped.
#
# NOT deleted by any of these: Homebrew packages, the pip cache,
# ~/.cache/huggingface (models are gigabytes and re-fetching them proves
# nothing) and the codex npm prefix. Those are tooling, not node state.

set -euo pipefail

SANDBOX="${SAUTIUM_TEST_ROOT:-/tmp/sautium-test}"
APP="${SAUTIUM_APP:-/Applications/Sautium.app}"
PG_CTL="$(brew --prefix postgresql@18 2>/dev/null || echo /opt/homebrew/opt/postgresql@18)/bin/pg_ctl"

stop_cluster() {   # $1 = pgdata
    [ -x "$PG_CTL" ] && [ -f "$1/PG_VERSION" ] || return 0
    "$PG_CTL" -D "$1" -m fast stop >/dev/null 2>&1 || true
}

case "${1:-}" in
run)
    rm -rf "$SANDBOX"
    mkdir -p "$SANDBOX/data" "$SANDBOX/config/Sautium"
    # Ports shifted off the defaults: a fresh config would claim the ones the
    # real node on this machine already uses.
    cat > "$SANDBOX/config/Sautium/config.json" <<JSON
{
  "first_run_complete": false,
  "ports": {"postgres": 15488, "web": 18088, "tracker": 18788,
            "media": 8846, "gena": 8847, "p2p_sync": 0}
}
JSON
    env -u LANG -u LC_ALL -u LC_CTYPE open -n \
        --env XDG_DATA_HOME="$SANDBOX/data" \
        --env XDG_CONFIG_HOME="$SANDBOX/config" "$APP"
    echo "test node starting in $SANDBOX (web 18088, postgres 15488)"
    echo "logs: $SANDBOX/data/Sautium/{bootstrap,backend}.log"
    ;;
reset)
    pkill -f "$SANDBOX" 2>/dev/null || true
    sleep 2
    stop_cluster "$SANDBOX/data/Sautium/pgdata"
    rm -rf "$SANDBOX"
    echo "test node deleted"
    ;;
wipe)
    [ "${2:-}" = "--yes" ] || {
        echo "This deletes the REAL node on this machine: its database," >&2
        echo "account key, certificates and settings. Re-run: $0 wipe --yes" >&2
        exit 1
    }
    pkill -f "$APP/Contents/MacOS/Sautium" 2>/dev/null || true
    pkill -f "$HOME/.local/share/Sautium/runtime" 2>/dev/null || true
    sleep 2
    stop_cluster "$HOME/.local/share/Sautium/pgdata"
    rm -rf "$HOME/.config/Sautium" "$HOME/.local/share/Sautium" "$HOME/.sautium" \
           "$HOME/Library/Application Support/Sautium/codex-agent"
    echo "node deleted — the next launch starts from the setup wizard"
    ;;
*)
    echo "usage: $0 run | reset | wipe --yes" >&2
    exit 1
    ;;
esac
