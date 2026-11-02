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

# Every download below names its version and is checked against its SHA-256
# before it is unpacked or run; a version moves with its digest, in a release.

# Node.js 22, the official build (for the Claude Code / Codex CLIs)
ARG NODE_VERSION=22.23.2
ARG NODE_SHA256=d60acfe00a2932254bb0ad20e01b0d74397a0875595de719654b214f4b03f307
RUN curl -fsSLo /tmp/node.tar.xz \
        https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz \
    && echo "${NODE_SHA256}  /tmp/node.tar.xz" | sha256sum -c - \
    && tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 --no-same-owner \
    && rm /tmp/node.tar.xz && node --version

# The agent CLIs at the exact versions and integrity hashes their lockfiles
# name (agents/*/package-lock.json). They move with a release — a new
# lockfile and a rebuilt image — never at a container's start, and the CLI's
# own updater stays off: the node's user cannot write here.
COPY agents/ /opt/agents/
RUN npm ci --prefix /opt/agents/claude-code --no-audit --no-fund \
    && npm ci --prefix /opt/agents/codex --no-audit --no-fund \
    && npm cache clean --force
ENV PATH=/opt/agents/claude-code/node_modules/.bin:/opt/agents/codex/node_modules/.bin:$PATH
ENV DISABLE_AUTOUPDATER=1

# The node's user: the backend and the agent CLIs run as it (entrypoint.py
# hands it the node's folders and drops root before anything else runs).
# codex-agent is the codex working root — its instructions file lands there.
RUN useradd -m -s /bin/bash agent \
    && mkdir -p /home/agent/.claude/debug \
    && mkdir -p /home/agent/.claude/projects \
    && echo '{}' > /home/agent/.claude/settings.json \
    && mkdir -p /home/agent/.codex /home/agent/codex-agent /home/agent/.cache \
    && chown -R agent:agent /home/agent/.claude \
        /home/agent/.codex /home/agent/codex-agent /home/agent/.cache

# Set Python 3.11 as default
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python${PYTHON_VERSION} 1 \
    && update-alternatives --install /usr/bin/python python /usr/bin/python${PYTHON_VERSION} 1

# Bootstrap pip for the deadsnakes interpreter (it ships without pip; the jammy
# python3-pip package would target the system python3.10 instead): pip's own
# wheel, which installs itself.
ARG PIP_WHEEL=pip-26.2.2-py3-none-any.whl
ARG PIP_URL=https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl
ARG PIP_SHA256=fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70
RUN curl -fsSLo /tmp/${PIP_WHEEL} ${PIP_URL} \
    && echo "${PIP_SHA256}  /tmp/${PIP_WHEEL}" | sha256sum -c - \
    && python${PYTHON_VERSION} /tmp/${PIP_WHEEL}/pip install --no-index /tmp/${PIP_WHEEL} \
    && rm /tmp/${PIP_WHEEL} \
    && python${PYTHON_VERSION} -m pip install setuptools==78.1.0 wheel==0.48.0

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
RUN pip install --no-cache-dir "libtorrent>=2.0.15" || echo "WARNING: libtorrent not available — DHT peer discovery disabled"

# PyNaCl for the peer-chat surface (E2E Box, mirrors the launcher's chat
# stack). Standalone late layer so it never busts the pip-install cache.
RUN pip install --no-cache-dir "PyNaCl>=1.6.2"

# deno: the sandboxed JS runtime yt-dlp solves YouTube's player challenges in
# — node is in the image too, but the solver executes YouTube's own JS, and
# deno gives it no permissions where node would give it ours. yt-dlp itself is
# not in the image: the node installs its stable build into its cache and
# keeps it current (streaming/ytdlp_release.py).
ARG DENO_VERSION=2.9.8
ARG DENO_SHA256=e890975bc2549807bbcc0b584ffafda72cc7c93349dd99c80483dd784f6c4609
RUN curl -fsSLo /tmp/deno.zip \
         https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-x86_64-unknown-linux-gnu.zip \
    && echo "${DENO_SHA256}  /tmp/deno.zip" | sha256sum -c - \
    && python -c "import zipfile; zipfile.ZipFile('/tmp/deno.zip').extractall('/usr/local/bin')" \
    && chmod +x /usr/local/bin/deno && rm /tmp/deno.zip \
    && deno --version

# Parallel bzip2 — fast MusicBrainz dump decompression (mb_dump_load). Kept in a
# late, standalone layer so it never busts the expensive pip-install cache above.
RUN apt-get update && apt-get install -y lbzip2 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# PostgreSQL 18 client — pg_dump / pg_restore for node backups (backup.py).
# jammy's postgresql-client is the 14 series, which refuses to dump an 18
# server ("server version mismatch"), so the tools come from PGDG. Same
# late standalone layer as lbzip2; PG_BIN pins the 18 binaries for the
# backend whatever pg_wrapper would pick.
# The repository's key is checked like any download (its fingerprint is
# B97B 0AFC AA1A 47F0 44F2 44A0 7FCC 7D46 ACCC 4CF8).
ARG PGDG_SIGNER_SHA256=0144068502a1eddd2a0280ede10ef607d1ec592ce819940991203941564e8e76
RUN apt-get update && apt-get install -y --no-install-recommends gnupg ca-certificates \
    && curl -fsSLo /tmp/pgdg.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    && echo "${PGDG_SIGNER_SHA256}  /tmp/pgdg.asc" | sha256sum -c - \
    && gpg --dearmor -o /usr/share/keyrings/postgresql.gpg /tmp/pgdg.asc && rm /tmp/pgdg.asc \
    && echo "deb [signed-by=/usr/share/keyrings/postgresql.gpg] https://apt.postgresql.org/pub/repos/apt jammy-pgdg main" \
         > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update && apt-get install -y --no-install-recommends postgresql-client-18 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
ENV PG_BIN=/usr/lib/postgresql/18/bin

# Copy application code
COPY . .


# Expose FastAPI port and DHT UDP port
EXPOSE 8000
EXPOSE 19001/udp

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/ || exit 1

# Default command (can be overridden in docker-compose.yml)
CMD ["python", "entrypoint.py"]
