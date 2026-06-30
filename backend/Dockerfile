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

# Node.js 22 (for Claude Code CLI + MCP servers)
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && npm install -g @modelcontextprotocol/server-postgres \
    && npm cache clean --force

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]

# Non-root user for Claude Code (--dangerously-skip-permissions requires non-root)
RUN useradd -m -s /bin/bash claudeuser \
    && mkdir -p /home/claudeuser/.claude/debug \
    && mkdir -p /home/claudeuser/.claude/projects \
    && echo '{}' > /home/claudeuser/.claude/settings.json \
    && chown -R claudeuser:claudeuser /home/claudeuser/.claude

# Set Python 3.11 as default
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python${PYTHON_VERSION} 1 \
    && update-alternatives --install /usr/bin/python python /usr/bin/python${PYTHON_VERSION} 1

# Bootstrap pip for the deadsnakes interpreter (it ships without pip; the jammy
# python3-pip package would target the system python3.10 instead).
RUN curl -fsSL https://bootstrap.pypa.io/get-pip.py | python${PYTHON_VERSION} \
    && python${PYTHON_VERSION} -m pip install --upgrade pip setuptools wheel

# Set working directory
WORKDIR /app

# Copy requirements first for better Docker layer caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# fugashi/MeCab (cutlet's Japanese tokenizer, Phase 0a transliteration) needs an
# rc file to exist even though the dictionary ships via unidic-lite.
RUN touch /usr/local/etc/mecabrc

# Optional: libtorrent for P2P DHT peer discovery (graceful degradation if unavailable)
RUN pip install --no-cache-dir libtorrent || echo "WARNING: libtorrent not available — DHT peer discovery disabled"

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
