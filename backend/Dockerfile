# The release image: NVIDIA's CUDA runtime on Ubuntu 22.04.
#
# Both bases are named by their digests (Docker Hub's indexes): a base that
# moves reaches nodes in a release, as every other pin does. Python is
# CPython built from python.org's source in a stage of its own — the same
# stage as Dockerfile.mac's (a test holds them equal). Ubuntu 22.04's own
# python3.11 is 3.11.0rc1, whose stdlib lacks sys.get_int_max_str_digits:
# torch._dynamo's polyfills reference that symbol at import time, so on rc1
# every transformers model load (CLAP, BGE-M3) crashes. The deadsnakes PPA
# that served a final before had no pin and no second source (IMAGE-BUILD-1).
FROM ubuntu:22.04@sha256:b000e69a50ff940390233025e7a337fb8469b509e2334fa4d018ac30f3e18cdb AS os

# Prevent interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive


# -- CPython, built once per pin ---------------------------------------------
FROM os AS python

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential pkg-config curl ca-certificates xz-utils \
    libssl-dev zlib1g-dev libbz2-dev liblzma-dev libsqlite3-dev libffi-dev uuid-dev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Debian's level of optimisation (PGO + LTO): an interpreter as fast as the
# distributions' (python-build-standalone's ran bytecode 30-45 % slower,
# measured). No readline and no gdbm headers: the interpreter links no GPL
# library. The tests are left out of the install.
ARG CPYTHON_VERSION=3.11.18
ARG CPYTHON_SHA256=9120e0a653868f6e3698d36cdd99e5a58903ec9469e448f21d98f7b16d8fa290
RUN curl -fsSLo /tmp/python.tar.xz \
        https://www.python.org/ftp/python/${CPYTHON_VERSION}/Python-${CPYTHON_VERSION}.tar.xz \
    && echo "${CPYTHON_SHA256}  /tmp/python.tar.xz" | sha256sum -c - \
    && mkdir /tmp/python && tar -xJf /tmp/python.tar.xz -C /tmp/python --strip-components=1 \
    && cd /tmp/python \
    && ./configure --prefix=/usr/local --enable-shared --enable-optimizations --with-lto \
        --enable-loadable-sqlite-extensions --without-ensurepip --enable-option-checking=fatal \
        LDFLAGS=-Wl,-rpath,/usr/local/lib \
    && make -j"$(nproc)" \
    && make install \
    && cd / && rm -rf /tmp/python /tmp/python.tar.xz \
    && find /usr/local/lib/python3.11 -depth -type d -name test -prune -exec rm -rf '{}' + \
    && find /usr/local -name 'libpython*.a' -delete \
    && find /usr/local/lib/python3.11 -name '*.opt-[12].pyc' -delete \
    && find /usr/local/lib -name '*.so*' -type f -exec strip --strip-unneeded '{}' + \
    && ln -s python3 /usr/local/bin/python \
    && /usr/local/bin/python3 -c "import ssl, sqlite3, ctypes, lzma, bz2, zlib, uuid, decimal, hashlib"


# -- the image -----------------------------------------------------------------
FROM nvidia/cuda:12.2.0-runtime-ubuntu22.04@sha256:739e0bde7bafdb2ed9057865f53085539f51cbf8bd6bf719f2e114bab321e70e

# Prevent interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive

# Set Python version
ENV PYTHON_VERSION=3.11

# Install system dependencies. The CUDA base lists NVIDIA's own apt
# repository; this image installs nothing from it, and left in place every
# apt layer would read one more archive nothing here pins.
RUN rm /etc/apt/sources.list.d/cuda-ubuntu2204-x86_64.list \
    && apt-get update && apt-get install -y \
    # The interpreter's own libraries (the python stage built against them),
    # and the MIME table its mimetypes reads (the Web UI's fonts are served
    # by type)
    media-types \
    libssl3 \
    zlib1g \
    libbz2-1.0 \
    liblzma5 \
    libsqlite3-0 \
    libffi8 \
    libuuid1 \
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

# After the packages: a pin's move rebuilds nothing above it
COPY --from=python /usr/local/ /usr/local/

# Every file fetched with curl below names its version and is checked against
# its SHA-256 before it is unpacked or run; a version moves with its digest, in
# a release. (The pip installs further down resolve from PyPI at build time.)

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

# The node's user: the backend and the agent CLIs run as it. The image's
# entrypoint (node_user.py, root's, outside /app — which that user can write)
# hands it the node's folders and drops root, then runs the command.
# codex-agent is the codex working root — its instructions files land there.
RUN useradd -m -s /bin/bash agent \
    && mkdir -p /home/agent/.claude/debug \
    && mkdir -p /home/agent/.claude/projects \
    && echo '{}' > /home/agent/.claude/settings.json \
    && mkdir -p /home/agent/.codex /home/agent/codex-agent /home/agent/.cache \
    && chown -R agent:agent /home/agent/.claude \
        /home/agent/.codex /home/agent/codex-agent /home/agent/.cache
COPY node_user.py /usr/local/lib/sautium/node_user.py
ENTRYPOINT ["python3", "-I", "-S", "/usr/local/lib/sautium/node_user.py"]

# Bootstrap pip for the interpreter (built without ensurepip; the jammy
# python3-pip package would target the system python3.10 instead): pip's own
# wheel, which installs itself.
ARG PIP_WHEEL=pip-26.2.2-py3-none-any.whl
ARG PIP_URL=https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl
ARG PIP_SHA256=fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70
RUN curl -fsSLo /tmp/${PIP_WHEEL} ${PIP_URL} \
    && echo "${PIP_SHA256}  /tmp/${PIP_WHEEL}" | sha256sum -c - \
    && python${PYTHON_VERSION} /tmp/${PIP_WHEEL}/pip install --no-index /tmp/${PIP_WHEEL} \
    && rm /tmp/${PIP_WHEEL} \
    && python${PYTHON_VERSION} -m pip install setuptools==78.1.1 wheel==0.48.0

# Set working directory
WORKDIR /app

# Torch trio in its own layer, pinned to the verified GPU-working set and
# pulled from the cu130 index explicitly. PyPI's linux wheel is a different
# variant (no +cu130 local tag) — a plain `-r requirements.txt` install would
# silently swap the verified build; with the trio pre-installed, the
# `torch==2.12.0` pin in requirements-ml.txt is already satisfied and pip
# leaves it alone. Separate layer = requirements edits don't re-download
# multi-GB torch wheels.
RUN pip install --no-cache-dir \
    torch==2.12.0 torchvision==0.27.0 torchaudio==2.11.0 \
    --index-url https://download.pytorch.org/whl/cu130

# Copy requirements first for better Docker layer caching
COPY requirements.txt requirements-ml.txt ./

# Install Python dependencies: the node's packages and the model stack, in
# one resolution
RUN pip install --no-cache-dir -r requirements.txt -r requirements-ml.txt

# cutlet's Japanese dictionary + tokenizer config (Phase 0a transliteration).
# unidic-lite is sdist-only (no wheel) so it can't live in requirements.txt (the
# launcher installs that with --only-binary=:all:); install it here for full
# Japanese romanization. fugashi/MeCab also needs an mecabrc to exist. Where this
# is absent (launcher), transliterate._japanese degrades to anyascii, not a crash.
RUN pip install --no-cache-dir unidic-lite==1.0.8 && touch /usr/local/etc/mecabrc

# libtorrent for the DHT's peer discovery. Its wheels exist, so a failure
# here is a download's, and an image without it would be a node that
# silently finds no peers: the build fails instead.
RUN pip install --no-cache-dir "libtorrent>=2.0.15"

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

# The node reads no bytecode it cannot write (PI-NODE-2A). /app is the
# checkout's backend/, read-only to the node: a .pyc beside its sources — a
# host interpreter's, or one a node left before that mount — would be read
# for as long as its source keeps its mtime, an update included. Every
# interpreter of the image but node_user.py's (isolated, -I) keeps its cache
# under this prefix instead: the libraries' compiled here, root's, and the
# node's own modules compiled as they are imported, written nowhere. A
# library file that does not compile on 3.11 (torch ships 3.12 syntax among
# its tests) is one nothing imports.
ENV PYTHONPYCACHEPREFIX=/usr/local/lib/sautium/pycache
RUN python -c "import compileall, sys; [compileall.compile_dir(p, quiet=1, workers=0) for p in sys.path if p.startswith('/usr/') and p.endswith(('python3.11', 'site-packages'))]" \
    && test -d /usr/local/lib/sautium/pycache/usr/local/lib/python3.11/site-packages

# Copy application code
COPY . .


# Expose FastAPI port and DHT UDP port
EXPOSE 8000
EXPOSE 19001/udp

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# (a path of its own, whose access line the node leaves out of its log)
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://127.0.0.1:${UVICORN_PORT:-8000}/healthz || exit 1

# Default command (can be overridden in docker-compose.yml)
CMD ["python", "entrypoint.py"]
