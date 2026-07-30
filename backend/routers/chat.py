"""
AI DJ chat with persistent history and feedback.

Sessions, messages stored in PostgreSQL. Feedback endpoint for debugging
recommendation quality. Supports multiple LLM providers.

The send-message endpoint streams the response as Server-Sent Events:
text deltas as the model generates, an optional `blocks` event after
the trailing `[DJ_BLOCKS]` marker is parsed and hydrated, and a final
`done` event with metadata. The router owns the marker contract:
providers emit raw `TextDelta`s, the router runs them through
`BlocksFilter` so prose flows live to the UI while the structured
payload is redirected into a separate event.
"""

import json
import logging
import queue
import threading
from typing import Iterator, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from config import settings
from db_pool import db_query as _db_query, db_query_one as _db_query_one, db_execute as _db_execute
from entity_hydration import hydrate_artists, hydrate_albums, hydrate_tracks
from providers.base import StreamDone, StreamEvent, TextDelta, ToolStart
from tools.block_parser import (
    BlocksFilter,
    extract_blocks_with_fallback,
    parse_blocks_payload,
    strip_blocks_marker,
)
from tools.track_parser import strip_tracks_marker

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])



# ---------------------------------------------------------------------------
# Player context (for AI DJ awareness of what's playing)
# ---------------------------------------------------------------------------

def _get_player_context() -> Optional[str]:
    """Current playback state, straight from the player router.

    Output-agnostic: get_status() reports whatever backend is active, so this
    is right whether the sound is going to HQPlayer, a renderer, a local
    device or the browser."""
    try:
        from routers.player import get_status, get_playlist

        from routers.settings import _read

        status = get_status()
        parts = []

        # Which output is chosen, because it decides which tools work at all:
        # the hqplayer_* family commands the HQPlayer device and refuses when
        # the sound is going elsewhere. Without this the agent has to guess,
        # and it guesses HQPlayer.
        #
        # Read from settings when the status carries none — nothing is attached
        # while a renderer is asleep or a browser tab is closed, and "which
        # output did the user pick" still has an answer. This used to return
        # nothing at all in that case, leaving the agent blind precisely when
        # it most needed to explain why playback would not start.
        out = status.get("output") or {}
        otype = out.get("type") or _read("output.type") or "none"
        label = out.get("label")
        attached = status.get("state") not in ("disconnected", None)
        parts.append(
            f"Active audio output: {otype}{f' ({label})' if label else ''}"
            f"{'' if attached else ' — not currently attached'}. "
            + ("HQPlayer transport/DSP tools are available."
               if otype == "hqplayer" else
               "hqplayer_* tools will refuse — use play_track / play_album / "
               "play_similar / add_to_queue, which follow this output."))
        if not attached:
            return "\n".join(parts)

        # Now playing
        state = status.get("state", "unknown")
        song = status.get("song")
        artist = status.get("artist")
        album = status.get("album")
        genre = status.get("genre")

        if song:
            np = f"Now playing ({state}): \"{song}\" by {artist or 'Unknown'}"
            if album:
                np += f" | Album: {album}"
            if genre:
                np += f" | Genre: {genre}"
            pos = status.get("position_formatted", "")
            length = status.get("length_formatted", "")
            if pos and length:
                np += f" | Position: {pos}/{length}"
            parts.append(np)
        else:
            parts.append(f"Player state: {state} (no track loaded)")

        # Playlist
        try:
            pl = get_playlist()
            pl_tracks = pl.get("tracks", [])
            if pl_tracks:
                current_idx = status.get("track_index")
                playlist_lines = [f"Playlist ({len(pl_tracks)} tracks):"]
                for t in pl_tracks:
                    marker = " >>> " if t.get("index") == (current_idx - 1 if current_idx else -1) else "     "
                    tid = f" [ID:{t['id']}]" if t.get("id") else ""
                    playlist_lines.append(f"{marker}{t.get('artist', '?')} - {t.get('title', '?')}{tid}")
                parts.append("\n".join(playlist_lines))
        except Exception:
            pass

        return "\n".join(parts)

    except Exception as e:
        logger.debug(f"Failed to get player context: {e}")
        return None


# ---------------------------------------------------------------------------
# Claude Code DJ integration
# ---------------------------------------------------------------------------

def _get_claude_session_id(session_id: int) -> Optional[str]:
    """Get Claude Code session ID mapped to our chat session."""
    row = _db_query_one(
        "SELECT claude_session_id FROM chat_sessions WHERE id = %(id)s",
        {"id": session_id},
    )
    return row["claude_session_id"] if row and row.get("claude_session_id") else None


def _save_claude_session_id(session_id: int, claude_sid: str):
    """Save Claude Code session ID for continuity."""
    _db_execute(
        "UPDATE chat_sessions SET claude_session_id = %(csid)s WHERE id = %(id)s",
        {"csid": claude_sid, "id": session_id},
    )


# ---------------------------------------------------------------------------
# Block hydration
# ---------------------------------------------------------------------------

def _hydrate_blocks(blocks: list[dict]) -> list[dict]:
    """Resolve each block's items to full tiles (cover_id, names,
    year, track_count) using the shared `entity_hydration` module —
    same data shape Discovery's renderers expect, so the chat UI can
    use the same `renderArtistRow` / `renderAlbumRow` /
    `renderTrackList` helpers.

    Items with IDs the database doesn't recognize are dropped. Empty
    blocks are dropped from the output entirely.
    """
    out: list[dict] = []
    for b in blocks:
        kind = b.get("kind")
        items = b.get("items") or []
        if not items:
            continue
        if kind == "artist":
            ids = [it.get("artist_id") for it in items if it.get("artist_id")]
            hydrated = hydrate_artists(ids)
        elif kind == "album":
            ids = [it.get("album_id") for it in items if it.get("album_id")]
            hydrated = hydrate_albums(ids)
        elif kind == "tracks":
            ids = [it.get("id") for it in items if it.get("id")]
            hydrated = hydrate_tracks(ids)
        else:
            continue
        if not hydrated:
            logger.info(
                f"Block kind={kind}: all {len(items)} items dropped during hydration "
                f"(unknown IDs?)"
            )
            continue
        if len(hydrated) < len(items):
            logger.info(
                f"Block kind={kind}: {len(items)} → {len(hydrated)} items after hydration"
            )
        out.append({"kind": kind, "items": hydrated})
    return out


def _tracks_from_blocks(blocks: list[dict]) -> list[dict]:
    """Pull the hydrated tracks out of a block list — kept for
    backward compatibility with the existing /api/chat response shape
    and with old `tracks_data` rendering on cached messages.
    """
    for b in blocks:
        if b.get("kind") == "tracks":
            return list(b.get("items") or [])
    return []


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class CreateSessionRequest(BaseModel):
    title: Optional[str] = None


class ChatMessageRequest(BaseModel):
    message: str
    model: Optional[str] = None
    provider: Optional[str] = None


class FeedbackRequest(BaseModel):
    is_not_relevant: bool = True
    comment: Optional[str] = None


# ---------------------------------------------------------------------------
# Providers endpoint
# ---------------------------------------------------------------------------

@router.get("/providers")
async def list_providers():
    """List available LLM providers and their models."""
    from providers import available_providers
    return available_providers()


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

@router.get("/sessions")
async def list_sessions(limit: int = 50):
    """List chat sessions, newest first.

    Each row includes a `preview` (first user message, truncated)
    and `last_model` (provider:model of the most recent assistant
    reply) — surfaced in the chat-list view so users can recognise
    a session by content + the model that answered, not just title.
    """
    limit = min(limit, 200)
    rows = _db_query("""
        SELECT s.id, s.title, s.created_at, s.updated_at,
               COUNT(m.id) as message_count,
               (SELECT LEFT(cm.content, 160)
                FROM chat_messages cm
                WHERE cm.session_id = s.id AND cm.role = 'user'
                ORDER BY cm.id ASC
                LIMIT 1) AS preview,
               (SELECT cm.model
                FROM chat_messages cm
                WHERE cm.session_id = s.id
                  AND cm.role = 'assistant'
                  AND cm.model IS NOT NULL
                ORDER BY cm.id DESC
                LIMIT 1) AS last_model
        FROM chat_sessions s
        LEFT JOIN chat_messages m ON m.session_id = s.id
        GROUP BY s.id
        ORDER BY s.updated_at DESC
        LIMIT %(limit)s
    """, {"limit": limit})
    return rows


@router.post("/sessions")
async def create_session(req: CreateSessionRequest = None):
    """Create a new chat session."""
    title = (req.title if req and req.title else None)
    row = _db_execute("""
        INSERT INTO chat_sessions (title) VALUES (%(title)s)
        RETURNING id, title, created_at
    """, {"title": title})
    return row


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: int):
    """Delete a session and all its messages (CASCADE)."""
    existing = _db_query_one(
        "SELECT id FROM chat_sessions WHERE id = %(id)s", {"id": session_id}
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Session not found")
    _db_execute("DELETE FROM chat_sessions WHERE id = %(id)s", {"id": session_id})
    return {"ok": True}


@router.get("/sessions/{session_id}/messages")
async def get_messages(session_id: int):
    """Get all messages in a session, plus whether a reply is being
    generated right now — the client uses the flag to reattach to the
    live stream after a page reload."""
    existing = _db_query_one(
        "SELECT id FROM chat_sessions WHERE id = %(id)s", {"id": session_id}
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Session not found")
    rows = _db_query("""
        SELECT id, role, content, tracks_data, blocks_data, model,
               is_not_relevant, feedback_comment, created_at
        FROM chat_messages
        WHERE session_id = %(sid)s
        ORDER BY id
    """, {"sid": session_id})
    with _active_runs_lock:
        generating = session_id in _active_runs
    return {"messages": rows, "generating": generating}


# ---------------------------------------------------------------------------
# Send message (streaming chat endpoint)
# ---------------------------------------------------------------------------

def _format_sse(event: str, data: dict) -> str:
    """Encode one SSE message. `default=str` lets us pass datetimes
    straight through without extra serialization."""
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


# ---------------------------------------------------------------------------
# Live generation registry (stream survives page reloads)
# ---------------------------------------------------------------------------

_RUN_CLOSED = object()
KEEPALIVE_INTERVAL_SEC = 15.0


class _LiveRun:
    """One in-flight assistant generation for a chat session.

    The producer thread publishes rendered SSE strings here; any number
    of consumers — the original POST response and reattach clients that
    arrive after a page reload — subscribe and get a full replay of
    everything emitted so far, then live events. The run outlives the
    HTTP connection that started it, so a dropped client never kills
    the generation.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._history: list[str] = []
        self._subscribers: list[queue.Queue] = []
        self._closed = False

    def publish(self, sse: str):
        with self._lock:
            self._history.append(sse)
            for q in self._subscribers:
                q.put(sse)

    def close(self):
        with self._lock:
            self._closed = True
            for q in self._subscribers:
                q.put(_RUN_CLOSED)
            self._subscribers.clear()

    def subscribe(self) -> "queue.Queue":
        q: "queue.Queue" = queue.Queue()
        with self._lock:
            for sse in self._history:
                q.put(sse)
            if self._closed:
                q.put(_RUN_CLOSED)
            else:
                self._subscribers.append(q)
        return q

    def unsubscribe(self, q: "queue.Queue"):
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)


_active_runs: dict[int, _LiveRun] = {}
_active_runs_lock = threading.Lock()


def _run_event_stream(run: _LiveRun) -> Iterator[str]:
    """SSE feed for one subscriber: replayed history, then live events,
    with keepalive comments while the producer is inside a long tool
    call so proxies/browsers don't drop the idle connection."""
    q = run.subscribe()
    try:
        while True:
            try:
                item = q.get(timeout=KEEPALIVE_INTERVAL_SEC)
            except queue.Empty:
                yield ": keepalive\n\n"
                continue
            if item is _RUN_CLOSED:
                break
            yield item
    finally:
        run.unsubscribe(q)


# Maximum characters in an AI-generated session title. The chat-list
# row and the chat-view title bar both ellipsize at ~30-35 chars on
# the 360px reference; 60 here gives some headroom for languages
# (e.g. Ukrainian) where fewer words = more characters per word.
_TITLE_MAX_CHARS = 60


_TITLE_PROMPT_TEMPLATE = (
    "Summarise this chat exchange into a concise chat title — "
    "ideally 3-5 words, max 8 — in the same language as the user. "
    "No quotes, no trailing punctuation, no phrases like 'Chat about'. "
    "Output ONLY the title text.\n\n"
    "User:\n{u}\n\n"
    "Assistant:\n{a}"
)


def _clean_title(raw: str) -> Optional[str]:
    """Trim quotes/punctuation/whitespace; cap to `_TITLE_MAX_CHARS`."""
    if not raw:
        return None
    title = raw.strip().strip('"\'').rstrip('.!?,;:')
    if not title:
        return None
    # Sometimes the model echoes the prompt structure or returns
    # multiple lines. Take the first non-empty line.
    line = next((l.strip() for l in title.splitlines() if l.strip()), title)
    line = line.strip().strip('"\'').rstrip('.!?,;:')
    if not line:
        return None
    if len(line) > _TITLE_MAX_CHARS:
        line = line[:_TITLE_MAX_CHARS - 1].rstrip() + '…'
    return line


def _title_via_provider(provider_name: str, prompt: str) -> Optional[str]:
    """Run the title prompt through any SDK-based provider via the
    shared `BaseProvider.chat()` interface. The DJ tool registry is
    still passed in the request (provider implementations always
    attach it), but a pure-summarisation prompt won't trigger tool
    use — single round-trip, ~500-1500ms depending on provider.
    Picks the smallest available model so even a slow provider
    answers quickly."""
    from providers import get_provider
    provider = get_provider(provider_name)
    if provider is None:
        return None
    try:
        models = provider.models()
        small_model = models[-1] if models else None
        result = provider.chat(
            message=prompt,
            system_prompt=(
                "You produce short chat titles. Output the title text only "
                "— no quotes, no preamble, no explanation, no tool calls."
            ),
            model=small_model,
        )
        return _clean_title(result.answer or "")
    except NotImplementedError:
        # ClaudeCodeProvider.chat() is a sentinel — caller has its own
        # subprocess path.
        return None
    except Exception as e:
        logger.debug(f"Title gen via {provider_name} failed: {e}")
        return None


def _title_via_claude_code(prompt: str) -> Optional[str]:
    """Fallback: spawn the Claude Code CLI in a bare one-shot mode —
    no `--mcp-config`, no tool definitions, no resumable session. This
    is ~10s faster than reusing `call_claude_code` because the CLI
    skips loading the entire DJ MCP server. Demote to `claudeuser`
    is preserved (Linux/Docker path) so `--dangerously-skip-permissions`
    is accepted, and `ANTHROPIC_API_KEY` is stripped from the env so
    the CLI uses the user's Claude subscription rather than the
    pay-as-you-go API account. ~3-5s total.
    """
    from providers import get_provider as _get_provider
    if _get_provider("claude_code") is None:
        return None
    try:
        from claude_code_runner import _resolve_claude_executable, CLAUDE_USER
        import subprocess
        import sys
        import os

        claude_exe = _resolve_claude_executable()
        if claude_exe is None:
            return None

        env = os.environ.copy()
        env.pop("ANTHROPIC_API_KEY", None)

        # `text=True` without `encoding=` falls back to the platform's
        # ANSI codepage on Windows (cp1251 for many Ukrainian/Russian
        # locales), which mangles UTF-8 output from the CLI. Force
        # UTF-8 + errors="replace" so we get printable glyphs even on
        # the rare line that includes something un-decodable.
        kwargs = dict(
            capture_output=True, text=True, timeout=20, env=env,
            encoding="utf-8", errors="replace",
        )
        if sys.platform != "win32" and sys.platform != "darwin":
            # Linux/Docker — same demote path as call_claude_code.
            import pwd

            pw = pwd.getpwnam(CLAUDE_USER)

            def demote():
                os.setgid(pw.pw_gid)
                os.setuid(pw.pw_uid)

            kwargs["preexec_fn"] = demote
            env["HOME"] = pw.pw_dir

        cmd = [
            claude_exe,
            "-p", prompt,
            "--output-format", "text",
            "--model", "haiku",
            "--dangerously-skip-permissions",
        ]
        result = subprocess.run(cmd, **kwargs)
        if result.returncode != 0:
            logger.debug(
                f"Claude Code title gen rc={result.returncode}: "
                f"{(result.stderr or '')[:200]}"
            )
            return None
        return _clean_title(result.stdout or "")
    except Exception as e:
        logger.debug(f"Claude Code title gen failed: {e}")
        return None


def _generate_session_title(
    first_user: str, first_assistant: str, provider_name: str,
) -> Optional[str]:
    """Summarise the first exchange into a short chat title via the
    SAME provider that just answered. ChatGPT user → ChatGPT title;
    Groq user → Groq title; Claude Code user → Claude Code title
    (subprocess). This keeps billing/credentials consistent — if the
    main chat works, title gen works.

    Runs synchronously in the request thread right after the assistant
    reply is persisted. Returns None if the provider call fails;
    caller keeps the truncated-message fallback already on the
    session. Output is hard-trimmed to `_TITLE_MAX_CHARS` because
    list rows can't fit anything longer.
    """
    if not first_user or not first_assistant:
        return None
    prompt = _TITLE_PROMPT_TEMPLATE.format(
        u=first_user[:500], a=first_assistant[:500],
    )
    if provider_name == "claude_code":
        # Subprocess CLI — see _title_via_claude_code for why this
        # bypasses BaseProvider.chat() and the MCP config.
        title = _title_via_claude_code(prompt)
    else:
        title = _title_via_provider(provider_name, prompt)
    if not title:
        logger.warning(
            f"AI title generation via '{provider_name}' returned nothing"
        )
    return title


def _user_settings_value(key: str) -> Optional[str]:
    """One-shot lookup from user_settings (the DB-backed prefs that
    the Web UI's Settings screen writes to). Falls back to None if
    the row is missing or unreadable."""
    try:
        from db_pool import db_query_one
        row = db_query_one(
            "SELECT value FROM user_settings WHERE key = %(k)s", {"k": key}
        )
        if row is None:
            return None
        v = row.get("value")
        return v if isinstance(v, str) else None
    except Exception as e:
        logger.debug(f"user_settings[{key}] read failed: {e}")
        return None


def _resolve_provider(req_provider: Optional[str]) -> str:
    """Pick a provider name, validating availability. Raises
    HTTPException on no usable provider. Selection priority:
      1. explicit `req_provider` from the request,
      2. `ai.provider` row in user_settings (Settings UI writes this),
      3. `settings.default_provider` from env,
      4. first available provider in the registry."""
    from providers import available_providers, get_provider

    providers = available_providers()
    if not providers:
        raise HTTPException(
            status_code=503,
            detail="No LLM providers configured. Set CLAUDE_CODE_ENABLED, ANTHROPIC_API_KEY, GROQ_API_KEY, or OPENAI_API_KEY.",
        )

    name = (
        req_provider
        or _user_settings_value("ai.provider")
        or settings.default_provider
    )
    # Web UI used to surface the Anthropic API provider under the
    # short id 'claude'; backend registers it as 'anthropic'. Map the
    # legacy value here so users who picked it before the rename
    # don't silently fall through to claude_code.
    if name == "claude":
        name = "anthropic"
    if get_provider(name) is None:
        # Requested provider isn't available — pick whatever first
        # appears in the registry (claude_code wins when it's ready).
        name = providers[0]["id"]
    return name


def _resolve_model(provider_name: str, req_model: Optional[str]) -> str:
    """Pick the model identifier the request will actually run against.
    Mirrors the per-provider validation done deep inside `chat_stream()`
    so the streaming `meta` event can carry an authoritative model
    name before any provider tokens arrive — the chat bubble shows
    the model tag immediately, not just after `done`. Falls back to
    the user's Settings choice (`ai.model`) when the request omits one."""
    fallback = req_model or _user_settings_value("ai.model")

    if provider_name == "claude_code":
        from claude_code_runner import ALLOWED_MODELS, DEFAULT_MODEL
        return fallback if fallback in ALLOWED_MODELS else DEFAULT_MODEL

    from providers import get_provider

    provider = get_provider(provider_name)
    if provider is None:
        return fallback or ""
    models = provider.models()
    if fallback and fallback in models:
        return fallback
    return models[0] if models else ""


def _build_provider_stream(
    provider_name: str,
    session_id: int,
    message: str,
    history: list[dict],
    player_context: Optional[str],
    model: Optional[str],
) -> Iterator[StreamEvent]:
    """Pick the right streaming source for the provider name. For
    `claude_code` we go through the subprocess runner; for API
    providers we use `BaseProvider.chat_stream`."""
    from claude_dj_prompt import get_system_prompt

    if provider_name == "claude_code":
        from claude_code_runner import call_claude_code_stream

        claude_sid = _get_claude_session_id(session_id)
        prompt = get_system_prompt("claude_code", player_context)
        return call_claude_code_stream(
            message=message,
            system_prompt=prompt,
            session_id=claude_sid,
            resume=bool(claude_sid),
            model=model,
        )

    from providers import get_provider
    from providers.base import ProviderMessage

    provider = get_provider(provider_name)
    if provider is None:
        raise HTTPException(status_code=503, detail=f"Provider '{provider_name}' is not available")

    pm_history = [ProviderMessage(role=h["role"], content=h["content"]) for h in (history or [])]
    prompt = get_system_prompt(provider_name, player_context)
    return provider.chat_stream(
        message=message,
        history=pm_history,
        system_prompt=prompt,
        player_context=player_context,
        model=model,
    )


@router.post("/sessions/{session_id}/messages")
async def send_message(session_id: int, req: ChatMessageRequest):
    """Stream the assistant's reply as Server-Sent Events.

    Pre-stream (synchronous): pick provider, save user message,
    update session metadata, gather history + player context.

    Stream events:
      - `meta`   — once at the start; carries the persisted user_msg.
      - `tool`   — every time the model invokes a tool.
      - `delta`  — text fragments with the `[DJ_BLOCKS]` marker elided.
      - `blocks` — hydrated block list, emitted once when the model
                   closes the marker. May not arrive at all when the
                   reply has no structured payload.
      - `done`   — final event with the persisted assistant message
                   and run metadata (model, provider, tool count).
      - `error`  — sent in place of `done` on a fatal failure.
    """
    provider_name = _resolve_provider(req.provider)
    model_resolved = _resolve_model(provider_name, req.model)

    session = _db_query_one(
        "SELECT id, title FROM chat_sessions WHERE id = %(id)s", {"id": session_id}
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Atomically claim the session's generation slot — two concurrent
    # sends (second tab, double-tap) must not spawn two subprocesses
    # over the same Claude session.
    run = _LiveRun()
    with _active_runs_lock:
        if session_id in _active_runs:
            raise HTTPException(
                status_code=409,
                detail="A reply is already being generated for this session",
            )
        _active_runs[session_id] = run

    try:
        # Persist the user message and bump session metadata before we
        # start the model — that way reload-during-stream still shows the
        # user's prompt, and we have a stable id to put on the meta event.
        user_row = _db_execute("""
            INSERT INTO chat_messages (session_id, role, content)
            VALUES (%(sid)s, 'user', %(content)s)
            RETURNING id, role, content, created_at
        """, {"sid": session_id, "content": req.message})

        if not session["title"]:
            title = req.message[:80].strip()
            _db_execute(
                "UPDATE chat_sessions SET title = %(t)s WHERE id = %(id)s",
                {"t": title, "id": session_id},
            )
        _db_execute(
            "UPDATE chat_sessions SET updated_at = NOW() WHERE id = %(id)s",
            {"id": session_id},
        )

        history_rows = _db_query("""
            SELECT role, content FROM chat_messages
            WHERE session_id = %(sid)s AND id < %(uid)s
            ORDER BY id DESC LIMIT 10
        """, {"sid": session_id, "uid": user_row["id"]})
        history_rows.reverse()
        history = [{"role": r["role"], "content": r["content"]} for r in history_rows]

        player_context = _get_player_context()

        try:
            provider_stream = _build_provider_stream(
                provider_name=provider_name,
                session_id=session_id,
                message=req.message,
                history=history,
                player_context=player_context,
                model=model_resolved,
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to build provider stream: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to start AI request")
    except BaseException:
        with _active_runs_lock:
            _active_runs.pop(session_id, None)
        run.close()
        raise

    def producer(run: _LiveRun):
        """Run the full provider stream → filter → persist pipeline on
        a worker thread, publishing rendered SSE strings to the run.

        The pipeline is fundamentally synchronous (subprocess Popen,
        Anthropic context manager, etc.) and can sit on a blocking
        read for many seconds at a time. The run object decouples it
        from every SSE consumer: the original client, reattach clients
        after a reload, or nobody at all — generation and persistence
        finish regardless.
        """
        blocks_filter = BlocksFilter()
        blocks: list[dict] = []
        final: Optional[StreamDone] = None

        def feed_filter(items) -> Iterator[str]:
            nonlocal blocks
            for kind, val in items:
                if kind == "text" and val:
                    yield _format_sse("delta", {"text": val})
                elif kind == "blocks_json":
                    raw = parse_blocks_payload(val)
                    hydrated = _hydrate_blocks(raw)
                    if hydrated:
                        blocks = hydrated
                        yield _format_sse("blocks", {"blocks": hydrated})

        try:
            for ev in provider_stream:
                if isinstance(ev, TextDelta):
                    for sse in feed_filter(blocks_filter.feed(ev.text)):
                        run.publish(sse)
                elif isinstance(ev, ToolStart):
                    run.publish(_format_sse("tool", {"name": ev.name}))
                elif isinstance(ev, StreamDone):
                    final = ev

            for sse in feed_filter(blocks_filter.flush()):
                run.publish(sse)

            if not blocks:
                raw_blocks, _ = extract_blocks_with_fallback(blocks_filter.full_text)
                if raw_blocks:
                    hydrated = _hydrate_blocks(raw_blocks)
                    if hydrated:
                        blocks = hydrated
                        run.publish(_format_sse("blocks", {"blocks": hydrated}))

            clean_text = strip_tracks_marker(strip_blocks_marker(blocks_filter.full_text))
            # When the provider failed before producing any tokens
            # (quota / auth / rate limit) clean_text is empty. Persist
            # the user-facing error as the assistant content so the
            # message survives a reload — otherwise the chat bubble
            # disappears as soon as the user switches sessions or
            # refreshes. Embed the action link as inline markdown so
            # the link is preserved through the markdown renderer on
            # history reloads.
            if not clean_text and final and final.error:
                clean_text = f"⚠️ {final.error}"
                act = getattr(final, "error_action", None)
                if isinstance(act, dict) and act.get("url") and act.get("label"):
                    clean_text += f"\n\n[{act['label']}]({act['url']})"
            tracks = _tracks_from_blocks(blocks)
            tracks_data = [
                {
                    "id": t.get("id"),
                    "title": t.get("title"),
                    "artist": t.get("artist"),
                    "album": t.get("album"),
                    "cover_id": t.get("cover_id"),
                }
                for t in tracks
            ] if tracks else None

            model_name = final.model if final else ""
            provider_used = final.provider if final else provider_name
            tool_calls_count = final.tool_calls_count if final else 0
            retrieval_log = (
                [{"source": "tools", "description": f"{tool_calls_count} tool calls"}]
                if tool_calls_count else []
            )

            if final and final.claude_session_id:
                try:
                    _save_claude_session_id(session_id, final.claude_session_id)
                except Exception as e:
                    logger.warning(f"Failed to save Claude session id: {e}")

            assistant_row = _db_execute("""
                INSERT INTO chat_messages
                    (session_id, role, content, tracks_data, blocks_data, model,
                     filters_detected, retrieval_log, tracks_retrieved)
                VALUES
                    (%(sid)s, 'assistant', %(content)s, %(tracks_data)s, %(blocks_data)s,
                     %(model)s, %(filters)s, %(rlog)s, %(tr)s)
                RETURNING id, role, content, tracks_data, blocks_data, model, created_at
            """, {
                "sid": session_id,
                "content": clean_text,
                "tracks_data": json.dumps(tracks_data) if tracks_data else None,
                "blocks_data": json.dumps(blocks) if blocks else None,
                "model": f"{provider_used}:{model_name}" if provider_used else model_name,
                "filters": None,
                "rlog": json.dumps(retrieval_log) if retrieval_log else None,
                "tr": len(tracks),
            })

            # First-exchange title refinement: when there was no prior
            # history at request time, we just persisted the user-msg
            # truncation as a fallback title (above). Now that we also
            # have the assistant reply we can ask the SAME provider
            # for a short summary and overwrite. Skips silently on any
            # failure — the truncation is still good enough.
            ai_title: Optional[str] = None
            # Skip title generation on a provider error/timeout — it spawns
            # ANOTHER (slow) model call and would delay the error `done` past
            # the ~180s point where the client drops the stream.
            if not history_rows and not (final and final.error):
                ai_title = _generate_session_title(
                    req.message, clean_text, provider_used or provider_name,
                )
                if ai_title:
                    try:
                        _db_execute(
                            "UPDATE chat_sessions SET title = %(t)s "
                            "WHERE id = %(id)s",
                            {"t": ai_title, "id": session_id},
                        )
                    except Exception as e:
                        logger.warning(f"Failed to save AI title: {e}")
                        ai_title = None

            done_payload = {
                "assistant_msg": assistant_row,
                "blocks": blocks,
                "tracks": tracks,
                "model": model_name,
                "provider": provider_used,
                "tracks_retrieved": len(tracks),
                "retrieval_log": retrieval_log,
            }
            if ai_title:
                done_payload["title"] = ai_title
            if final and final.error:
                done_payload["provider_error"] = final.error
            if final and getattr(final, "error_action", None):
                done_payload["provider_error_action"] = final.error_action
            run.publish(_format_sse("done", done_payload))
        except Exception as e:
            logger.error(f"Provider stream error: {e}", exc_info=True)
            run.publish(_format_sse("error", {"message": str(e)}))
        finally:
            # Deregister before waking subscribers: a reattach arriving
            # after this point gets a 404 and refetches messages — the
            # assistant row is already persisted by now.
            with _active_runs_lock:
                _active_runs.pop(session_id, None)
            run.close()

    run.publish(_format_sse("meta", {
        "user_msg": user_row,
        "model": model_resolved,
        "provider": provider_name,
    }))
    threading.Thread(
        target=producer, args=(run,), daemon=True, name="chat-stream-worker",
    ).start()

    return StreamingResponse(
        _run_event_stream(run),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/sessions/{session_id}/stream")
async def reattach_stream(session_id: int):
    """Reattach to an in-flight generation after a page reload.

    Replays every SSE event emitted so far (meta, deltas, tool pips,
    blocks), then streams live until `done`. 404 when nothing is being
    generated — the caller should just load persisted messages.
    """
    with _active_runs_lock:
        run = _active_runs.get(session_id)
    if run is None:
        raise HTTPException(status_code=404, detail="No generation in progress")
    return StreamingResponse(
        _run_event_stream(run),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------

@router.post("/messages/{message_id}/feedback")
async def set_feedback(message_id: int, req: FeedbackRequest):
    """Mark an assistant message as not relevant."""
    msg = _db_query_one(
        "SELECT id, role FROM chat_messages WHERE id = %(id)s", {"id": message_id}
    )
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    if msg["role"] != "assistant":
        raise HTTPException(status_code=400, detail="Feedback only for assistant messages")

    _db_execute("""
        UPDATE chat_messages
        SET is_not_relevant = %(flag)s,
            feedback_comment = %(comment)s,
            feedback_at = NOW()
        WHERE id = %(id)s
    """, {
        "id": message_id,
        "flag": req.is_not_relevant,
        "comment": req.comment,
    })
    return {"ok": True}


@router.get("/feedback")
async def list_feedback(limit: int = 50):
    """
    List assistant messages marked as not relevant (for debugging).
    Includes the original user query (previous message in session).
    """
    rows = _db_query("""
        SELECT
            m.id,
            m.session_id,
            m.content,
            m.tracks_data,
            m.feedback_comment,
            m.feedback_at,
            m.filters_detected,
            m.retrieval_log,
            m.tracks_retrieved,
            m.model,
            m.created_at,
            (
                SELECT um.content FROM chat_messages um
                WHERE um.session_id = m.session_id
                  AND um.id < m.id
                  AND um.role = 'user'
                ORDER BY um.id DESC LIMIT 1
            ) as user_query
        FROM chat_messages m
        WHERE m.is_not_relevant = TRUE
        ORDER BY m.feedback_at DESC
        LIMIT %(limit)s
    """, {"limit": limit})
    return rows


# ---------------------------------------------------------------------------
# Legacy endpoint (backward compatibility with current frontend)
# ---------------------------------------------------------------------------

class LegacyChatRequest(BaseModel):
    message: str
    history: list[dict] = []


@router.post("")
async def legacy_chat(req: LegacyChatRequest):
    """Stateless chat endpoint for backward compatibility with existing frontend."""
    player_context = _get_player_context()

    from providers import get_provider as _get_provider
    if _get_provider("claude_code") is None:
        raise HTTPException(status_code=503, detail="Claude Code is not installed or not signed in")

    try:
        from claude_code_runner import call_claude_code
        from claude_dj_prompt import get_system_prompt

        prompt = get_system_prompt("claude_code", player_context)

        result = call_claude_code(
            message=req.message,
            system_prompt=prompt,
        )
        raw_blocks, clean_answer = extract_blocks_with_fallback(result.get("answer", ""))
        blocks = _hydrate_blocks(raw_blocks)
        tracks = _tracks_from_blocks(blocks)
        return {
            "answer": clean_answer,
            "blocks": blocks,
            "tracks": tracks,
            "filters_detected": {},
            "retrieval_log": [],
            "model": result.get("model", "claude-code"),
            "tracks_retrieved": len(tracks),
        }
    except Exception as e:
        logger.error(f"Claude Code chat failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
