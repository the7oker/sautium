# Third-party notices

Sautium itself is licensed under the PolyForm Noncommercial License 1.0.0
(`LICENSE`). It runs on, downloads at install time, or is designed to work
with the third-party components below. Each stays under its own licence;
this file lists them so the notices travel with the code.

Nothing here is modified or statically linked into Sautium: command-line
tools run as separate processes, Python packages are installed by `pip` on
the user's machine, models are downloaded from Hugging Face on first use.

## Carried by the installers, or downloaded by the launcher

| Component | Source | Licence | Notes |
|---|---|---|---|
| CPython 3.12 (python-build-standalone) | github.com/astral-sh/python-build-standalone | PSF-2.0; statically linked components (OpenSSL, Tcl/Tk, libffi, SQLite, zlib, bzip2, xz) under their own licences, see the distribution's `LICENSE.txt` | the launcher's interpreter, carried by the macOS bundle and the Windows installer; on Windows a copy of its `pythonw.exe` runs as `Sautium.exe` with Sautium's icon and version strings, the PSF copyright notice retained |
| MinGit (Git for Windows) | github.com/git-for-windows/git | GPL-2.0-only | carried by the Windows installer; run as a separate process to clone and update the tree |
| PostgreSQL | get.enterprisedb.com (Windows zip) / Homebrew (macOS) | PostgreSQL License | database server |
| pgvector | github.com/andreiramani/pgvector_pgsql_windows / Homebrew | PostgreSQL License | vector index extension |
| Python 3.12 (embeddable) | python.org | PSF-2.0 | the backend's interpreter on Windows |
| FFmpeg ("release-essentials" build) | gyan.dev | GPL-3.0 (this build) | run as a separate process for decoding, slicing and transcoding; sources for the build are published by gyan.dev |
| flac 1.4.3 | xiph.org | flac tool GPL-2.0-or-later; libFLAC BSD-3-Clause | run as a separate process |
| fpcalc (Chromaprint 1.5.1) | acoustid.org/chromaprint | LGPL-2.1-or-later | run as a separate process; fingerprints are computed locally |
| Deno | deno.com | MIT | JavaScript runtime for yt-dlp's player-challenge solver |
| Node.js | nodejs.org | MIT | runtime for the optional AI-agent CLIs |
| yt-dlp (+ yt-dlp-ejs) | PyPI, nightly channel | Unlicense | the built-in YouTube provider |
| PyTorch wheels | download.pytorch.org | BSD-3-Clause | installed by pip |

Optional, user-installed, not redistributed: Claude Code CLI
(`@anthropic-ai/claude-code`, Anthropic's commercial terms) and OpenAI Codex
CLI (Apache-2.0) — the two selectable assistant agents.

Build tooling (not shipped): Inno Setup (Inno Setup License), rcedit (MIT,
edits the resources of the copied `pythonw.exe`).

## Carried in the tree (Web UI)

The one exception to "nothing is vendored": a single-file JavaScript library
the browser loads as-is, because the Web UI has no build step.

| Component | Source | Licence | Notes |
|---|---|---|---|
| TweetNaCl.js 1.0.3 (`nacl-fast.min.js`) | github.com/dchest/tweetnacl-js | Unlicense (public domain) | `backend/static/vendor/nacl-fast.min.js`, unmodified (SHA-256 `3ec535c0…a5131` matches the npm release); the browser side of the boxed credential exchange — X25519 + XSalsa20-Poly1305, Ed25519 verification |

## Models (downloaded from Hugging Face on first use)

| Model | Licence | Use |
|---|---|---|
| `laion/clap-htsat-unfused` | Apache-2.0 | audio embeddings |
| `BAAI/bge-m3` | MIT | multilingual text embeddings |
| `google/madlad400-3b-mt` (CTranslate2 int8 conversion) | Apache-2.0 | query translation |
| `MIT/ast-finetuned-audioset-10-10-0.4593` | BSD-3-Clause | instrument tagging |
| PaSST weights via `hear21passt` | Apache-2.0 | instrument tagging |

## Python packages

### Launcher (`desktop/requirements.txt`, installed by pip into the carried interpreter)

customtkinter (MIT) · pystray (LGPL-3.0) · Pillow (MIT-CMU) · qrcode (BSD) ·
psutil (BSD-3-Clause) · psycopg2-binary (LGPL with exceptions) · cryptography
(Apache-2.0 OR BSD-3-Clause) · aiohttp (Apache-2.0 AND MIT) · libtorrent (BSD) ·
argon2-cffi (MIT) · PyNaCl (Apache-2.0).

pystray and psycopg2-binary are LGPL. They are installed by pip on the
user's machine as separate, replaceable modules (not statically linked);
their sources are at https://github.com/moses-palmer/pystray and
https://github.com/psycopg/psycopg2.

### Backend (`backend/requirements.txt`, installed by pip on the user's machine)

fastapi (MIT) · uvicorn (BSD-3-Clause) · pydantic, pydantic-settings (MIT) ·
SQLAlchemy (MIT) · psycopg2-binary (LGPL with exceptions) · pgvector (MIT) ·
mutagen (GPL-2.0-or-later) · soundfile (BSD-3-Clause) · audioread (MIT) ·
sounddevice (MIT) · async-upnp-client (Apache-2.0) · Pillow (MIT-CMU) ·
imagehash (BSD-2-Clause) · torch, torchvision, torchaudio (BSD-3-Clause) ·
transformers (Apache-2.0) · sentence-transformers (Apache-2.0) · hear21passt
(Apache-2.0) · librosa (ISC) · ctranslate2 (MIT) · anthropic (MIT) · openai
(Apache-2.0) · mcp (MIT) · pylast (Apache-2.0) · lyricsgenius (MIT) ·
argon2-cffi (MIT) · cryptography (Apache-2.0 OR BSD-3-Clause) · python-dotenv
(BSD-3-Clause) · psutil (BSD-3-Clause) · click (BSD-3-Clause) · tqdm (MPL-2.0
AND MIT) · numpy (BSD-3-Clause) · pandas (BSD-3-Clause) · scipy
(BSD-3-Clause) · httpx (BSD-3-Clause) · requests (Apache-2.0) · aiohttp
(Apache-2.0 AND MIT) · python-json-logger (BSD-2-Clause) · pytest (MIT) ·
pytest-asyncio (Apache-2.0) · yt-dlp (Unlicense) · anyascii (ISC) · pypinyin
(MIT) · cutlet (MIT) · koroman (MIT).

mutagen is GPL-2.0-or-later. Sautium imports it for tag reading; it is
installed by pip on the user's machine and is not redistributed with
Sautium. Source: https://github.com/quodlibet/mutagen.

## Services and data

- **Last.fm** — metadata enrichment and scrobbling through the Last.fm API,
  under Last.fm's API terms of service. The application key ships in source,
  as desktop scrobblers do; scrobbling still needs the user's own session
  authorisation.
- **MusicBrainz** — metadata and the optional local database dump (data under
  CC0 / CC BY-NC-SA as published by MetaBrainz); API use follows the
  MusicBrainz rate and user-agent rules.
- **Cover Art Archive** — album art.
- **Deezer public API** — artist images only (no authentication, no audio).
- **Genius** — optional plain-text lyrics fallback with the user's own API
  token; **LRCLIB** — synced lyrics.
- **YouTube** — the built-in streaming preview provider through yt-dlp, a
  terms-of-service matter for the user, not anti-circumvention.
- **Cloudflare Workers** — the project's verification/notary service
  (`worker/`), operated by the maintainer.
