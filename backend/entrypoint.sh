#!/bin/bash
set -e

# Update Claude Code CLI if outdated (runs once at container start)
if command -v claude &>/dev/null; then
    CURRENT=$(claude --version 2>/dev/null | head -1 | grep -oP '[\d.]+' || echo "0")
    LATEST=$(npm show @anthropic-ai/claude-code version 2>/dev/null || echo "$CURRENT")
    if [ "$CURRENT" != "$LATEST" ]; then
        echo "Updating Claude Code: $CURRENT → $LATEST"
        npm install -g @anthropic-ai/claude-code@latest --loglevel=warn 2>&1 | tail -1
        echo "Claude Code updated to $(claude --version 2>/dev/null | head -1)"
    else
        echo "Claude Code $CURRENT is up to date"
    fi
fi

exec "$@"
