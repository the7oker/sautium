# Use NVIDIA CUDA base image with Ubuntu 22.04
FROM nvidia/cuda:12.2.0-runtime-ubuntu22.04

# Prevent interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive

# Set Python version
ENV PYTHON_VERSION=3.11

# Install system dependencies.
# deadsnakes PPA is mandatory: Ubuntu 22.04's stock python3.11 package is frozen
# at 3.11.0rc1, whose stdlib lacks sys.get_int_max_str_digits. torch._dynamo's
# polyfills reference that symbol at import time, so on rc1 every transformers
# model load (CLAP, BGE-M3) crashes — silently killing all embedding generation.
# deadsnakes ships a proper 3.11.x final.
RUN apt-get update && apt-get install -y software-properties-common \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get update && apt-get install -y \
    # Python (final, from deadsnakes — not jammy's rc1)
    python${PYTHON_VERSION} \
    python${PYTHON_VERSION}-dev \
    python${PYTHON_VERSION}-venv \
    python${PYTHON_VERSION}-distutils \
    # Audio processing libraries
    ffmpeg \
    libsndfile1 \
    libsndfile1-dev \
    # Chromaprint fpcalc — acoustic fingerprint for signed-record content-address
    libchromaprint-tools \
    # Build tools
    build-essential \
    git \
    wget \
    curl \
    # PostgreSQL client
    postgresql-client \
    # Cleanup
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Node.js 22 (for Claude Code / Codex CLIs + MCP servers)
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && npm install -g @openai/codex \
    && npm install -g @modelcontextprotocol/server-postgres \
    && npm cache clean --force

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]

# Non-root user for the CLI agents (Claude Code refuses
# --dangerously-skip-permissions as root; codex runs demoted the same
# way). codex-dj is the codex working root — AGENTS.md lands there.
RUN useradd -m -s /bin/bash agent \
    && mkdir -p /home/agent/.claude/debug \
    && mkdir -p /home/agent/.claude/projects \
    && echo '{}' > /home/agent/.claude/settings.json \
    && mkdir -p /home/agent/.codex /home/agent/codex-dj \
    && chown -R agent:agent /home/agent/.claude \
        /home/agent/.codex /home/agent/codex-dj

# Set Python 3.11 as default
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python${PYTHON_VERSION} 1 \
    && update-alternatives --install /usr/bin/python python /usr/bin/python${PYTHON_VERSION} 1

# Bootstrap pip for the deadsnakes interpreter (it ships without pip; the jammy
# python3-pip package would target the system python3.10 instead).
RUN curl -fsSL https://bootstrap.pypa.io/get-pip.py | python${PYTHON_VERSION} \
    && python${PYTHON_VERSION} -m pip install --upgrade pip setuptools wheel

# Set working directory
WORKDIR /app

# Torch trio in its own layer, pinned to the verified GPU-working set and
# pulled from the cu130 index explicitly. PyPI's linux wheel is a different
# variant (no +cu130 local tag) — a plain `-r requirements.txt` install would
# silently swap the verified build; with the trio pre-installed, the
# `torch==2.12.0` pin in requirements.txt is already satisfied and pip
# leaves it alone. Separate layer = requirements.txt edits don't re-download
# multi-GB torch wheels.
RUN pip install --no-cache-dir \
    torch==2.12.0 torchvision==0.27.0 torchaudio==2.11.0 \
    --index-url https://download.pytorch.org/whl/cu130

# Copy requirements first for better Docker layer caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# cutlet's Japanese dictionary + tokenizer config (Phase 0a transliteration).
# unidic-lite is sdist-only (no wheel) so it can't live in requirements.txt (the
# launcher installs that with --only-binary=:all:); install it here for full
# Japanese romanization. fugashi/MeCab also needs an mecabrc to exist. Where this
# is absent (launcher), transliterate._japanese degrades to anyascii, not a crash.
RUN pip install --no-cache-dir unidic-lite && touch /usr/local/etc/mecabrc

# Optional: libtorrent for P2P DHT peer discovery (graceful degradation if unavailable)
RUN pip install --no-cache-dir libtorrent || echo "WARNING: libtorrent not available — DHT peer discovery disabled"

# PyNaCl for the peer-chat surface (E2E Box, mirrors the launcher's chat
# stack). Standalone late layer so it never busts the pip-install cache.
RUN pip install --no-cache-dir pynacl

# yt-dlp — the core YouTube streaming-preview provider runs it as `-m yt_dlp`
# on this same interpreter (streaming/youtube.py), and the launcher installs
# it into its own the same way (desktop/db_init.py). NIGHTLY channel (--pre):
# upstream calls stable "often stale and prone to external breakage" and
# recommends nightly for regular users — the 2026-08 android_vr 403 was
# stable-only. This layer is only the offline baseline: entrypoint.py re-runs
# the install at every container start so a long-lived image keeps up with
# YouTube. [default] brings yt-dlp-ejs, the player-challenge solver; deno is
# the sandboxed runtime it runs in — node is in the image too, but the solver
# executes YouTube's own JS, and deno gives it no permissions where node would
# give it ours. Late standalone layer: a version bump must not re-run the pip
# install above (which already pulled stable from requirements.txt — hence -U).
RUN pip install --no-cache-dir -U --pre "yt-dlp[default]" \
    && curl -fsSL https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip \
         -o /tmp/deno.zip \
    && python -c "import zipfile; zipfile.ZipFile('/tmp/deno.zip').extractall('/usr/local/bin')" \
    && chmod +x /usr/local/bin/deno && rm /tmp/deno.zip \
    && deno --version && yt-dlp --version

# Parallel bzip2 — fast MusicBrainz dump decompression (mb_dump_load). Kept in a
# late, standalone layer so it never busts the expensive pip-install cache above.
RUN apt-get update && apt-get install -y lbzip2 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Copy application code
COPY . .

# Create cache directory for models
RUN mkdir -p /root/.cache

# Expose FastAPI port and DHT UDP port
EXPOSE 8000
EXPOSE 19001/udp

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Health check (HTTPS, self-signed — -k skips cert verification)
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f -k https://localhost:8000/ || exit 1

# Default command (can be overridden in docker-compose.yml)
CMD ["python", "entrypoint.py"]
