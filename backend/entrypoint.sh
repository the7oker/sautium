#!/bin/bash
set -e

# Ensure the bind-mounted CLI-agent session storage is writable by the
# non-root user that actually runs the agents. UIDs usually match (host
# 1000, container 1000), but host_uid != agent_uid would silently
# fail the first session save.
if [ -d /home/agent/.claude/projects ]; then
    chown -R agent:agent /home/agent/.claude/projects 2>/dev/null || true
fi
# The ~/.codex bind mount: when the host never ran codex, Docker
# creates the dir root-owned and the demoted CLI can't write auth.json.
# Chowning just the root is enough — codex creates everything else.
if [ -d /home/agent/.codex ]; then
    chown agent:agent /home/agent/.codex 2>/dev/null || true
fi

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

# Update Codex CLI if outdated (runs once at container start)
if command -v codex &>/dev/null; then
    CURRENT=$(codex --version 2>/dev/null | head -1 | grep -oP '[\d.]+' || echo "0")
    LATEST=$(npm show @openai/codex version 2>/dev/null || echo "$CURRENT")
    if [ "$CURRENT" != "$LATEST" ]; then
        echo "Updating Codex: $CURRENT → $LATEST"
        npm install -g @openai/codex@latest --loglevel=warn 2>&1 | tail -1
        echo "Codex updated to $(codex --version 2>/dev/null | head -1)"
    else
        echo "Codex $CURRENT is up to date"
    fi
fi

exec "$@"
