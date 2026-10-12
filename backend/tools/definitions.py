"""Register all tools in the global REGISTRY.

The tools the API providers (Anthropic / OpenAI) call. They are the SAME tools
the MCP server exposes to Claude Code / Codex — same names, same arguments, same
canonical UUIDs — and they now go through the same code: catalog reads via
`assistant_queries`, search via the discovery engine (`routers.discovery.
run_search`), playback via the player router, which owns the canonical queue and
the user's chosen output. The private copies this module used to carry had
drifted badly: search called /search/* endpoints deleted in the engine refactor,
and playback pushed file:// URIs straight at HQPlayer, bypassing both the queue
and the output choice.
"""

import json
import logging
from contextlib import contextmanager

import assistant_queries as aq
from db_pool import db_query as _db_query, db_query_one as _db_query_one
from tools.registry import REGISTRY, ToolDef, ToolParam

logger = logging.getLogger(__name__)

def _hqp_gate() -> None:
    """The rules every HQPlayer tool here keeps, the MCP server's too:
    refuse when HQPlayer is not the chosen output (a filter change or a
    skip would reconfigure a device in another room while the sound goes
    elsewhere, with the canonical queue none the wiser), while it is lent
    to the benchmark, and while it builds a setting."""
    from routers.settings import _read
    from playback.manager import manager
    active = _read("output.type")
    if active and active != "hqplayer":
        raise ConnectionError(
            f"HQPlayer is not the active audio output (currently: {active}). "
            "Its transport and DSP controls are unavailable. Use play_track / "
            "play_album / play_similar / add_to_queue, which play through "
            "whatever output the user has chosen."
        )
    hold = manager.held
    if hold is not None:
        raise ConnectionError(
            f"HQPlayer is busy: {hold['label']}. Its transport and DSP controls "
            "come back when it finishes; the user can cancel it on the HQPlayer "
            "screen."
        )
    from playback import hqp_link
    hqp_link.selected().build.refuse()


@contextmanager
def _hqp_asking():
    """The selected HQPlayer's link for the reading tools here (_hqp_gate
    first): their questions go where every request of this node to it goes,
    one at a time. A command goes the backend's way instead (_hqp_transport,
    change_dsp) — after the block: a change holds the link itself."""
    _hqp_gate()
    from playback import hqp_link
    with hqp_link.selected().asking() as hqp:
        yield hqp


def _hqp_transport(command: str, route, req=None, **values) -> str:
    """A transport command the way the Web UI sends it (routers.player):
    the backend's own command sequence on the link, the play press's resume
    spot, the playback trace; refused in HQPlayer's words when it did not
    take it."""
    try:
        _hqp_gate()
    except Exception as e:
        return f"Error: {e}"
    r, err = _player_call(lambda req: route(req) if req is not None else route(), req)
    if err:
        return f"Error: {err}"
    return aq.hqp_transport_text(command, r, **values)


# ===========================================================================
# Handler functions
# ===========================================================================

def _h_execute_query(sql: str) -> str:
    from tools.execute_query import execute_query
    return execute_query(sql)


def _h_search_tracks(query: str = "", artist: str = "", album: str = "",
                     genre: str = "", limit: int = 20, corpus: str = "owned") -> str:
    try:
        if not any([query, artist, album, genre]):
            return "Provide at least one of query / artist / album / genre."
        rows = aq.search_tracks(_db_query, query=query, artist=artist, album=album,
                                genre=genre, limit=limit, corpus=corpus)
        scope = "" if corpus == "all" else " in the library"
        return aq.format_track_list(rows, f"Search results{scope} ({len(rows)} tracks):")
    except Exception as e:
        return f"Error searching tracks: {e}"


def _search(target: str, **params) -> list[dict]:
    from routers.discovery import run_search
    return run_search(target, **params).get("results", [])


def _h_search_similar(track_id: str, limit: int = 15, vocalist: str = "",
                      gender: str = "", genres: list = None, instruments: list = None,
                      corpus: str = "owned") -> str:
    try:
        seed = aq.valid_uuids([track_id])
        if not seed:
            return f"'{track_id}' is not a track UUID. Search first and pass the ID it returned."
        src = _db_query_one("""
            SELECT t.title, a.name AS artist,
                   EXISTS (SELECT 1 FROM embeddings e WHERE e.track_id = t.id) AS analysed
            FROM tracks t
            JOIN track_artists ta ON ta.track_id = t.id AND ta.role = 'primary'
            JOIN artists a ON a.id = ta.artist_id
            WHERE t.id = %(t)s::uuid
        """, {"t": seed[0]})
        if not src:
            return f"Track {track_id} not found."
        if not src["analysed"]:
            return (f"{src['artist']} - {src['title']} has no audio analysis yet, "
                    "so nothing can be matched against it. Use search_semantic instead.")
        rows = _search("track", seed_track_id=seed[0], limit=limit, vocalist=vocalist,
                       gender=gender, genres=genres, instruments=instruments, corpus=corpus)
        return aq.format_track_list(
            rows, f"Tracks similar to: {src['artist']} - {src['title']} ({len(rows)} results):")
    except Exception as e:
        return f"Error finding similar tracks: {e}"


def _h_search_semantic(query: str, limit: int = 15, vocalist: str = "", gender: str = "",
                       genres: list = None, instruments: list = None,
                       bpm_min: float = None, bpm_max: float = None,
                       corpus: str = "owned") -> str:
    try:
        rows = _search("track", sound=query, limit=limit, vocalist=vocalist, gender=gender,
                       genres=genres, instruments=instruments, bpm_min=bpm_min,
                       bpm_max=bpm_max, corpus=corpus)
        return aq.format_track_list(
            rows, f"Semantic search for '{query}' ({len(rows)} results):")
    except Exception as e:
        return f"Error in semantic search: {e}"


def _h_search_lyrics(query: str, limit: int = 15) -> str:
    try:
        rows = _search("track", lyrics=query, limit=limit)
        return aq.format_track_list(
            rows, f"Lyrics search for '{query}' ({len(rows)} results):")
    except Exception as e:
        return f"Error in lyrics search: {e}"


def _h_search_artists(query: str, limit: int = 10, by_bio: bool = False) -> str:
    try:
        rows = _search("artist", q=query, limit=limit,
                       **({"scope": "bio"} if by_bio else {}))
        return aq.format_artist_list(
            rows, f"Artist search for '{query}' ({len(rows)} results):")
    except Exception as e:
        return f"Error in artist search: {e}"


def _h_search_albums(query: str, limit: int = 10) -> str:
    try:
        rows = _search("album", q=query, limit=limit)
        return aq.format_album_list(
            rows, f"Album search for '{query}' ({len(rows)} results):")
    except Exception as e:
        return f"Error in album search: {e}"


def _h_search_genres(query: str, limit: int = 10) -> str:
    try:
        rows = _search("genre", q=query, limit=limit)
        return aq.format_genre_list(
            rows, f"Genre search for '{query}' ({len(rows)} results):")
    except Exception as e:
        return f"Error in genre search: {e}"


def _h_get_lyrics(track_id: str) -> str:
    try:
        tid = aq.valid_uuids([track_id])
        if not tid:
            return f"'{track_id}' is not a track UUID."
        row = aq.track_lyrics(_db_query, tid[0])
        if not row:
            return f"Track {track_id} not found."
        if row.get("instrumental"):
            return f"{row['artist']} - {row['title']}: instrumental track (no lyrics)."
        if not row.get("plain_lyrics"):
            return f"{row['artist']} - {row['title']}: lyrics not available."
        return (f"{row['artist']} - {row['title']} [source: {row['source']}]\n\n"
                f"{row['plain_lyrics']}")
    except Exception as e:
        return f"Error getting lyrics: {e}"


def _h_get_track_info(track_id: str) -> str:
    try:
        tid = aq.valid_uuids([track_id])
        if not tid:
            return f"'{track_id}' is not a track UUID."
        row = aq.track_info(_db_query, tid[0])
        if not row:
            return f"Track {track_id} not found."
        return aq.format_track_info(row, aq.track_features(_db_query, tid[0]))
    except Exception as e:
        return f"Error getting track info: {e}"


# -- Playback handlers -------------------------------------------------------
#
# Thin wrappers over the player router: it owns the canonical queue, the output
# the user picked, and the owned/streamed routing. Anything that talks to a
# device directly from here plays to the wrong room and leaves the queue lying.

def _player_call(fn, req):
    from fastapi import HTTPException
    try:
        return fn(req), None
    except HTTPException as e:
        detail = e.detail
        if isinstance(detail, dict):
            detail = detail.get("message") or str(detail)
        return None, str(detail)
    except Exception as e:
        return None, str(e)


def _h_play_track(track_id: str) -> str:
    from routers.player import EntityRef, QueueEntitiesRequest, play_entities
    tid = aq.valid_uuids([track_id])
    if not tid:
        return f"'{track_id}' is not a track UUID. Search first and pass the ID it returned."
    info = aq.track_info(_db_query, tid[0]) or {}
    r, err = _player_call(play_entities,
                          QueueEntitiesRequest(items=[EntityRef(kind="track", id=tid[0])]))
    if err:
        return f"Could not play track: {err}"
    if r.get("not_found"):
        return "No streaming provider has that track, so it cannot be played."
    how = "streaming" if r.get("streaming") else "playing"
    return f"Now {how}: {info.get('artist')} - {info.get('title')}\nAlbum: {info.get('album')}"


def _h_play_album(album: str, artist_name: str = "") -> str:
    from routers.player import PlayAlbumRequest, play_album
    aid = aq.valid_uuids([album])
    req = (PlayAlbumRequest(album_id=aid[0]) if aid
           else PlayAlbumRequest(album_name=album, artist_name=artist_name))
    r, err = _player_call(play_album, req)
    if err:
        return f"Could not play album: {err}"
    if r.get("provider"):
        missing = len(r.get("missing") or [])
        tail = f" ({missing} track(s) no provider has)" if missing else ""
        return (f"Now streaming the album via {r['provider']}: "
                f"{r.get('track_count', '?')} tracks{tail}")
    return (f"Now playing album: {r.get('artist')} - {r.get('album')}"
            f" ({r.get('track_count', '?')} tracks)")


def _h_play_similar(track_id: str, limit: int = 10) -> str:
    from routers.player import PlaySimilarRequest, play_similar
    tid = aq.valid_uuids([track_id])
    if not tid:
        return f"'{track_id}' is not a track UUID."
    r, err = _player_call(play_similar, PlaySimilarRequest(track_id=tid[0], limit=limit))
    if err:
        return f"Could not start similar playback: {err}"
    tracks = r.get("tracks") or [{}]
    streamed = sum(1 for t in tracks if t.get("is_owned") is False)
    tail = f" ({streamed} of them streamed)" if streamed else ""
    return (f"Now playing: {tracks[0].get('artist')} - {tracks[0].get('title')}\n"
            f"Queued {r.get('count', '?')} tracks by acoustic similarity{tail}.")


def _entity_items(ids: list):
    """Canonical UUIDs → EntityRefs, or an explanatory string when nothing
    usable is left."""
    from routers.player import EntityRef
    valid = aq.valid_uuids(ids or [])
    if not valid:
        return "No valid UUIDs given. Pass the IDs the search tools returned."
    kinds = aq.entity_kinds(_db_query, valid)
    items = [EntityRef(kind=kinds[i], id=i) for i in valid if i in kinds]
    return items or "None of those IDs exist in the catalog."


def _queue_outcome(r: dict, verb: str) -> str:
    parts = []
    if r.get("queued"):
        parts.append(f"{r['queued']} track(s) from the library")
    if r.get("streaming"):
        parts.append(f"{r['streaming']} streaming in behind them")
    missing = len(r.get("not_found") or [])
    tail = f" ({missing} had nothing playable)" if missing else ""
    return f"{verb}: " + (", ".join(parts) or "nothing") + tail


def _h_build_playlist(ids: list) -> str:
    from routers.player import QueueEntitiesRequest, play_entities
    items = _entity_items(ids)
    if isinstance(items, str):
        return items
    r, err = _player_call(play_entities,
                          QueueEntitiesRequest(items=items, autoplay=False))
    if err:
        return f"Could not build the playlist: {err}"
    return _queue_outcome(r, "New queue ready (not started)")


def _h_play_all(ids: list) -> str:
    from routers.player import QueueEntitiesRequest, play_entities
    items = _entity_items(ids)
    if isinstance(items, str):
        return items
    r, err = _player_call(play_entities, QueueEntitiesRequest(items=items))
    if err:
        return f"Could not start playback: {err}"
    return _queue_outcome(r, "Playing a new queue")


def _h_add_to_queue(ids: list) -> str:
    from routers.player import QueueEntitiesRequest, queue_entities
    items = _entity_items(ids)
    if isinstance(items, str):
        return items
    r, err = _player_call(queue_entities, QueueEntitiesRequest(items=items))
    if err:
        return f"Could not queue: {err}"
    return _queue_outcome(r, "Queued")


# -- HQPlayer control handlers -----------------------------------------------

def _h_hqplayer_play() -> str:
    from routers.player import play
    return _hqp_transport("play", play)


def _h_hqplayer_pause() -> str:
    from routers.player import pause
    return _hqp_transport("pause", pause)


def _h_hqplayer_stop() -> str:
    from routers.player import stop
    return _hqp_transport("stop", stop)


def _h_hqplayer_next() -> str:
    from routers.player import next_track
    return _hqp_transport("next", next_track)


def _h_hqplayer_previous() -> str:
    from routers.player import previous_track
    return _hqp_transport("previous", previous_track)


def _h_hqplayer_get_status() -> str:
    try:
        from hqplayer_client import PlaybackState, format_time
        with _hqp_asking() as hqp:
            status = hqp.get_status()
        if status is None:
            return "Could not get HQPlayer status."
        state_names = {
            PlaybackState.STOPPED: "Stopped",
            PlaybackState.PAUSED: "Paused",
            PlaybackState.PLAYING: "Playing",
            PlaybackState.STOPREQ: "Stopping",
        }
        lines = [f"State: {state_names.get(status.state, 'Unknown')}"]
        if status.artist or status.song:
            lines.append(f"Track: {status.artist} - {status.song}")
        if status.album:
            lines.append(f"Album: {status.album}")
        if status.genre:
            lines.append(f"Genre: {status.genre}")
        lines.append(f"Position: {format_time(status.position)} / {format_time(status.length)}")
        lines.append(f"Volume: {status.volume}")
        lines.append(f"Track index: {status.track_index}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def _h_hqplayer_volume_up() -> str:
    from routers.player import volume_up
    return _hqp_transport("volume/up", volume_up)


def _h_hqplayer_volume_down() -> str:
    from routers.player import volume_down
    return _hqp_transport("volume/down", volume_down)


def _h_hqplayer_set_volume(level: float) -> str:
    from routers.player import VolumeRequest, set_volume
    return _hqp_transport("volume", set_volume, VolumeRequest(level=level), level=level)


def _h_hqplayer_get_settings() -> str:
    try:
        lines = []
        with _hqp_asking() as hqp:
            info = hqp.get_info()
            if info:
                lines.append(f"HQPlayer: {info.get('product', '')} v{info.get('version', '')}")
                lines.append(f"Engine: {info.get('engine', '')}")
                lines.append("")
            aq.hqp_list(lines, "Available filters", hqp.get_filters(), hqp,
                        lambda f: f"  [{f['index']}] {f['name']}"
                                  + (f" — {f['description']}" if f.get("description") else ""))
            aq.hqp_list(lines, "\nAvailable dither/shapers", hqp.get_shapers(), hqp,
                        lambda s: f"  [{s['index']}] {s['name']}")
            aq.hqp_list(lines, "\nOutput modes", hqp.get_modes(), hqp,
                        lambda m: f"  [{m['index']}] {m['name']}")
            aq.hqp_list(lines, "\nSample rates", hqp.get_rates(), hqp,
                        lambda r: f"  [{r['index']}] {r['rate'] / 1000:.1f} kHz")
        return "\n".join(lines)
    except Exception as e:
        return f"Error getting settings: {e}"


def _h_hqplayer_set_filter(filter_name: str) -> str:
    try:
        with _hqp_asking() as hqp:
            match, said = aq.hqp_pick("filter", hqp.get_filters(), filter_name, hqp,
                                      key=lambda f: f["name"])
        if match is None:
            return said
        # Both slots, as the HQPlayer screen sends it: a 44.1/48 kHz source
        # plays through the 1x slot
        from playback.hqp_backend import change_dsp
        outcome = change_dsp({"filter": match["index"], "filter1x": match["index"]})
        desc = match.get("description", "")
        return aq.hqp_change_text(outcome, f"the filter {match['name']}",
                                  f"Filter set to: {match['name']}" + (f" — {desc}" if desc else ""))
    except Exception as e:
        return f"Error setting filter: {e}"


def _h_hqplayer_set_shaper(shaper_name: str) -> str:
    try:
        with _hqp_asking() as hqp:
            match, said = aq.hqp_pick("shaper", hqp.get_shapers(), shaper_name, hqp,
                                      key=lambda s: s["name"])
        if match is None:
            return said
        from playback.hqp_backend import change_dsp
        outcome = change_dsp({"shaper": match["index"]})
        return aq.hqp_change_text(outcome, f"the dither/shaper {match['name']}",
                                  f"Dither/shaper set to: {match['name']}")
    except Exception as e:
        return f"Error setting shaper: {e}"


def _h_hqplayer_set_convolution(enabled: bool) -> str:
    try:
        _hqp_gate()
        from playback.hqp_backend import change_dsp
        outcome = change_dsp({"convolution": enabled})
        state = "enabled" if enabled else "disabled"
        return aq.hqp_change_text(outcome, f"convolution {'on' if enabled else 'off'}",
                                  f"Convolution {state}.")
    except Exception as e:
        return f"Error: {e}"


def _h_hqplayer_list_matrix_profiles() -> str:
    try:
        with _hqp_asking() as hqp:
            profiles = hqp.matrix_list_profiles()
            refusal = hqp.refusal()
        if profiles is None:
            return f"HQPlayer did not list its matrix profiles ({refusal})."
        if not profiles:
            return "No matrix profiles found."
        lines = [f"Available matrix profiles ({len(profiles)}):"]
        for p in profiles:
            lines.append(f"  - {p}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def _h_hqplayer_get_matrix_profile() -> str:
    try:
        with _hqp_asking() as hqp:
            profile = hqp.matrix_get_profile()
        if profile is None:
            return "Could not get current matrix profile."
        return f"Current matrix profile: '{profile}'" if profile else "No matrix profile active."
    except Exception as e:
        return f"Error: {e}"


def _h_hqplayer_set_matrix_profile(profile_name: str) -> str:
    try:
        with _hqp_asking() as hqp:
            match, said = aq.hqp_pick("profile", hqp.matrix_list_profiles(), profile_name, hqp)
        if match is None:
            return said
        profile_name = match
        from playback.hqp_backend import change_dsp
        outcome = change_dsp({"matrix_profile": profile_name})
        return aq.hqp_change_text(outcome, f"the matrix profile '{profile_name}'",
                                  f"Matrix profile set to: '{profile_name}'")
    except Exception as e:
        return f"Error: {e}"


def _h_hqplayer_get_dsp_state() -> str:
    try:
        with _hqp_asking() as hqp:
            state = hqp.get_state()
            filters = hqp.get_filters() if state else None
            refusal = hqp.refusal()
        if not state:
            return "Could not get DSP state."
        active_filter = None if filters is None else next(
            (f for f in filters if f["index"] == state["filter"]), None
        )
        filter_line = f"  Active filter index: {state['filter']}"
        if filters is None:
            filter_line += f" (its name unknown: HQPlayer did not list its filters — {refusal})"
        elif active_filter:
            filter_line += f" ({active_filter['name']})"
            if active_filter.get("description"):
                filter_line += f" — {active_filter['description']}"
        lines = ["Current DSP state:"]
        lines.append(f"  Convolution: {'ON' if state['convolution'] else 'OFF'}")
        lines.append(f"  Matrix profile: '{state['matrix_profile']}'" if state['matrix_profile'] else "  Matrix profile: (none)")
        lines.append(filter_line)
        lines.append(f"  Active shaper index: {state['shaper']}")
        lines.append(f"  Active mode index: {state['mode']}")
        lines.append(f"  Phase invert: {'ON' if state['invert'] else 'OFF'}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def _h_generate_eq_preset(name: str, filters_json: str, description: str = "") -> str:
    try:
        filters = json.loads(filters_json)
        if not isinstance(filters, list) or not filters:
            return "Error: filters_json must be a non-empty JSON array."
        for i, f in enumerate(filters):
            if "type" not in f or "freq" not in f:
                return f"Error: filter {i+1} must have 'type' and 'freq'."
        from eq_generator import save_eq_preset
        result = save_eq_preset(filters=filters, name=name, description=description)
        return result["instructions"]
    except Exception as e:
        return f"Error: {e}"


# ===========================================================================
# Register all tools
# ===========================================================================

def register_all():
    """Register all tools in the global REGISTRY."""

    REGISTRY.register(ToolDef(
        name="execute_query",
        description="Execute a read-only SQL SELECT query against the music database. "
                    "Use this to answer questions that require custom queries not covered by other tools. "
                    "Only SELECT and WITH queries are allowed. Results limited to 100 rows.",
        parameters=[
            ToolParam("sql", "string", "SQL SELECT query to execute", required=True),
        ],
        handler=_h_execute_query,
    ))

    REGISTRY.register(ToolDef(
        name="search_tracks",
        description="Search the catalog by metadata (artist, album, genre, or free text query). "
                    "All parameters are optional. Tolerant to typos (fuzzy trigram matching) "
                    "and to script (a Latin query finds Cyrillic/CJK names). "
                    "Returns each track's canonical UUID — the id every other tool takes.",
        parameters=[
            ToolParam("query", "string", "Free text search across artist, album, and track title", required=False, default=""),
            ToolParam("artist", "string", "Filter by artist name (fuzzy match)", required=False, default=""),
            ToolParam("album", "string", "Filter by album name (fuzzy match)", required=False, default=""),
            ToolParam("genre", "string", "Filter by genre (partial match)", required=False, default=""),
            ToolParam("limit", "integer", "Maximum number of results (default 20)", required=False, default=20),
            ToolParam("corpus", "string", "'owned' (default, files on disk) or 'all' (adds not-owned tracks, which stream)", required=False, default="owned", enum=["owned", "all"]),
        ],
        handler=_h_search_tracks,
    ))

    REGISTRY.register(ToolDef(
        name="search_similar",
        description="Find tracks with similar sound to a given track (CLAP audio embeddings), "
                    "optionally narrowed by hard filters in the same query.",
        parameters=[
            ToolParam("track_id", "string", "Track UUID of the source track", required=True),
            ToolParam("limit", "integer", "Maximum number of similar tracks to return (default 15)", required=False, default=15),
            ToolParam("vocalist", "string", "'vocal' or 'instrumental'", required=False, default="", enum=["vocal", "instrumental"]),
            ToolParam("gender", "string", "'male', 'female' or 'mixed'", required=False, default="", enum=["male", "female", "mixed"]),
            ToolParam("genres", "array", "Genre names to require (OR within the list)", required=False, items_type="string"),
            ToolParam("instruments", "array", "Broad instrument names to require", required=False, items_type="string"),
            ToolParam("corpus", "string", "'owned' (default) or 'all' (adds not-owned tracks, which stream)", required=False, default="owned", enum=["owned", "all"]),
        ],
        handler=_h_search_similar,
    ))

    REGISTRY.register(ToolDef(
        name="search_semantic",
        description="Search tracks by a SOUND description (CLAP text-to-audio), optionally with "
                    "hard filters. E.g. 'energetic rock', 'calm piano music'. NOT a name search.",
        parameters=[
            ToolParam("query", "string", "Natural language description of the music you want", required=True),
            ToolParam("limit", "integer", "Maximum number of results (default 15)", required=False, default=15),
            ToolParam("vocalist", "string", "'vocal' or 'instrumental'", required=False, default="", enum=["vocal", "instrumental"]),
            ToolParam("gender", "string", "'male', 'female' or 'mixed'", required=False, default="", enum=["male", "female", "mixed"]),
            ToolParam("genres", "array", "Genre names to require (OR within the list)", required=False, items_type="string"),
            ToolParam("instruments", "array", "Broad instrument names to require", required=False, items_type="string"),
            ToolParam("bpm_min", "number", "Lower BPM bound", required=False),
            ToolParam("bpm_max", "number", "Upper BPM bound", required=False),
            ToolParam("corpus", "string", "'owned' (default) or 'all' (adds not-owned tracks, which stream)", required=False, default="owned", enum=["owned", "all"]),
        ],
        handler=_h_search_semantic,
    ))

    REGISTRY.register(ToolDef(
        name="search_lyrics",
        description="Search tracks by lyrics content using AI semantic understanding. "
                    "Finds songs whose lyrics match a description. "
                    "E.g. 'songs about love', 'rain and sadness', 'protest and freedom', 'dancing in the moonlight'.",
        parameters=[
            ToolParam("query", "string", "Description of lyrical content to search for", required=True),
            ToolParam("limit", "integer", "Maximum number of results (default 15)", required=False, default=15),
        ],
        handler=_h_search_lyrics,
    ))

    REGISTRY.register(ToolDef(
        name="search_artists",
        description="Search artists by NAME (default) or by biography description (by_bio=true). "
                    "Returns artists, not tracks, each with its canonical UUID. "
                    "E.g. 'Madonna', or by_bio: 'British rock band from the 70s'.",
        parameters=[
            ToolParam("query", "string", "Artist name, or (with by_bio) a description of the artist", required=True),
            ToolParam("limit", "integer", "Maximum number of artists (default 10)", required=False, default=10),
            ToolParam("by_bio", "boolean", "Search biographies instead of names", required=False, default=False),
        ],
        handler=_h_search_artists,
    ))

    REGISTRY.register(ToolDef(
        name="search_albums",
        description="Search albums by title or description. Returns albums (not tracks) with their "
                    "canonical UUIDs — the ids play_album and add_to_queue take.",
        parameters=[
            ToolParam("query", "string", "Album title or description to search for", required=True),
            ToolParam("limit", "integer", "Maximum number of albums (default 10)", required=False, default=10),
        ],
        handler=_h_search_albums,
    ))

    REGISTRY.register(ToolDef(
        name="search_genres",
        description="Search genres by name or description. Returns genres, not tracks. "
                    "E.g. 'heavy distorted guitars', 'African rhythms'.",
        parameters=[
            ToolParam("query", "string", "Genre name or description to search for", required=True),
            ToolParam("limit", "integer", "Maximum number of genres (default 10)", required=False, default=10),
        ],
        handler=_h_search_genres,
    ))

    REGISTRY.register(ToolDef(
        name="get_lyrics",
        description="Get the full lyrics text for a specific track. "
                    "Use this when the user asks what a song is about, to quote lyrics, "
                    "or to analyze lyrical content of a specific track.",
        parameters=[
            ToolParam("track_id", "string", "Track UUID", required=True),
        ],
        handler=_h_get_lyrics,
    ))

    REGISTRY.register(ToolDef(
        name="get_track_info",
        description="Get full details about a specific track including audio features. "
                    "Works for a not-owned track too (no file facts, same analysis).",
        parameters=[
            ToolParam("track_id", "string", "Track UUID", required=True),
        ],
        handler=_h_get_track_info,
    ))

    REGISTRY.register(ToolDef(
        name="play_track",
        description="Play one track on the output the user chose (replaces the queue). "
                    "A track with no file in the library plays too — it streams.",
        parameters=[
            ToolParam("track_id", "string", "Track UUID", required=True),
        ],
        handler=_h_play_track,
    ))

    REGISTRY.register(ToolDef(
        name="play_album",
        description="Play a whole album on the output the user chose (replaces the queue). "
                    "Pass the album UUID (works owned or not), or a title to fuzzy-match "
                    "among owned albums.",
        parameters=[
            ToolParam("album", "string", "Album UUID, or an album title to fuzzy-match", required=True),
            ToolParam("artist_name", "string", "Optional artist name to narrow a title match", required=False, default=""),
        ],
        handler=_h_play_album,
    ))

    REGISTRY.register(ToolDef(
        name="play_similar",
        description="Play a station of tracks acoustically similar to the seed track "
                    "(replaces the queue). Results mix owned files with streamed tracks.",
        parameters=[
            ToolParam("track_id", "string", "Track UUID of the seed", required=True),
            ToolParam("limit", "integer", "Number of similar tracks to queue (default 10)", required=False, default=10),
        ],
        handler=_h_play_similar,
    ))

    REGISTRY.register(ToolDef(
        name="build_playlist",
        description="Assemble a NEW queue from these tracks and/or albums, in the given "
                    "order, and leave it ready — nothing starts playing. This is "
                    "'put her albums together into one playlist': curation, not a play "
                    "command. The previous queue is archived to the listening history.",
        parameters=[
            ToolParam("ids", "array", "Track and/or album UUIDs, in play order", required=True, items_type="string"),
        ],
        handler=_h_build_playlist,
    ))

    REGISTRY.register(ToolDef(
        name="play_all",
        description="Start a NEW queue from these tracks and/or albums, in the given order, "
                    "and PLAY it — for 'put these on'. If the user only asked to assemble "
                    "or collect a playlist, use build_playlist instead. The previous queue "
                    "is archived to the listening history, not extended.",
        parameters=[
            ToolParam("ids", "array", "Track and/or album UUIDs, in play order", required=True, items_type="string"),
        ],
        handler=_h_play_all,
    ))

    REGISTRY.register(ToolDef(
        name="add_to_queue",
        description="Append tracks AND/OR albums to the CURRENT queue, in the given order, "
                    "without clearing it — for 'add these too'. For a playlist use "
                    "build_playlist (or play_all to start it): this tool leaves whatever "
                    "is already queued in front, burying a freshly assembled set.",
        parameters=[
            ToolParam("ids", "array", "Track and/or album UUIDs, in the order to append", required=True, items_type="string"),
        ],
        handler=_h_add_to_queue,
    ))

    REGISTRY.register(ToolDef(
        name="hqplayer_play",
        description="Start or resume HQPlayer playback.",
        parameters=[],
        handler=_h_hqplayer_play,
    ))

    REGISTRY.register(ToolDef(
        name="hqplayer_pause",
        description="Pause HQPlayer playback.",
        parameters=[],
        handler=_h_hqplayer_pause,
    ))

    REGISTRY.register(ToolDef(
        name="hqplayer_stop",
        description="Stop HQPlayer playback.",
        parameters=[],
        handler=_h_hqplayer_stop,
    ))

    REGISTRY.register(ToolDef(
        name="hqplayer_next",
        description="Skip to the next track in HQPlayer.",
        parameters=[],
        handler=_h_hqplayer_next,
    ))

    REGISTRY.register(ToolDef(
        name="hqplayer_previous",
        description="Go back to the previous track in HQPlayer.",
        parameters=[],
        handler=_h_hqplayer_previous,
    ))

    REGISTRY.register(ToolDef(
        name="hqplayer_get_status",
        description="Get current HQPlayer status: track info, position, state, volume.",
        parameters=[],
        handler=_h_hqplayer_get_status,
    ))

    REGISTRY.register(ToolDef(
        name="hqplayer_volume_up",
        description="Increase HQPlayer volume by one step.",
        parameters=[],
        handler=_h_hqplayer_volume_up,
    ))

    REGISTRY.register(ToolDef(
        name="hqplayer_volume_down",
        description="Decrease HQPlayer volume by one step.",
        parameters=[],
        handler=_h_hqplayer_volume_down,
    ))

    REGISTRY.register(ToolDef(
        name="hqplayer_set_volume",
        description="Set HQPlayer volume to an exact level (dB, typically -100 to 0).",
        parameters=[
            ToolParam("level", "number", "Volume level in dB (e.g. -10.0)", required=True),
        ],
        handler=_h_hqplayer_set_volume,
    ))

    REGISTRY.register(ToolDef(
        name="hqplayer_get_settings",
        description="Get current HQPlayer DSP settings: filters, output mode, sample rate.",
        parameters=[],
        handler=_h_hqplayer_get_settings,
    ))

    REGISTRY.register(ToolDef(
        name="hqplayer_set_filter",
        description="Set HQPlayer upsampling filter by name. Use hqplayer_get_settings first to see available filter names.",
        parameters=[
            ToolParam("filter_name", "string", "Name of the filter to set (e.g. 'poly-sinc-gauss-xla')", required=True),
        ],
        handler=_h_hqplayer_set_filter,
    ))

    REGISTRY.register(ToolDef(
        name="hqplayer_set_shaper",
        description="Set HQPlayer dither/noise shaper by name. Use hqplayer_get_settings first to see available shaper names.",
        parameters=[
            ToolParam("shaper_name", "string", "Name of the dither/shaper to set (e.g. 'NS9')", required=True),
        ],
        handler=_h_hqplayer_set_shaper,
    ))

    REGISTRY.register(ToolDef(
        name="hqplayer_set_convolution",
        description="Enable or disable HQPlayer convolution engine on the fly.",
        parameters=[
            ToolParam("enabled", "boolean", "True to enable, False to disable", required=True),
        ],
        handler=_h_hqplayer_set_convolution,
    ))

    REGISTRY.register(ToolDef(
        name="hqplayer_list_matrix_profiles",
        description="List all saved HQPlayer Matrix Processor profiles (EQ/convolution presets).",
        parameters=[],
        handler=_h_hqplayer_list_matrix_profiles,
    ))

    REGISTRY.register(ToolDef(
        name="hqplayer_get_matrix_profile",
        description="Get the currently active HQPlayer Matrix Processor profile name.",
        parameters=[],
        handler=_h_hqplayer_get_matrix_profile,
    ))

    REGISTRY.register(ToolDef(
        name="hqplayer_set_matrix_profile",
        description="Set HQPlayer Matrix Processor profile by name. Use hqplayer_list_matrix_profiles to see available profiles.",
        parameters=[
            ToolParam("profile_name", "string", "Name of the matrix profile to activate", required=True),
        ],
        handler=_h_hqplayer_set_matrix_profile,
    ))

    REGISTRY.register(ToolDef(
        name="hqplayer_get_dsp_state",
        description="Get current HQPlayer DSP processing state: active filter, shaper, convolution on/off, matrix profile.",
        parameters=[],
        handler=_h_hqplayer_get_dsp_state,
    ))

    REGISTRY.register(ToolDef(
        name="generate_eq_preset",
        description="Generate a parametric EQ preset file (REW format) for HQPlayer Matrix Processor. "
                    "Use when user asks for EQ adjustments (de-essing, warmth, bass boost, etc.). "
                    "Returns download link and instructions for loading into HQPlayer.",
        parameters=[
            ToolParam("name", "string", "Preset name (e.g. 'de-ess', 'warm-vocal')", required=True),
            ToolParam("filters_json", "string",
                      'JSON array of filters. Each: {"type":"peak|lshelf|hshelf|hp|lp|notch","freq":Hz,"gain":dB,"q":Q}. '
                      'Example: [{"type":"peak","freq":6500,"gain":-3,"q":4}]',
                      required=True),
            ToolParam("description", "string", "Human-readable description", required=False, default=""),
        ],
        handler=_h_generate_eq_preset,
    ))


# Auto-register on import
register_all()
