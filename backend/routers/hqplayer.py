"""
HQPlayer settings endpoint.

Single round-trip /state for the More → HQPlayer screen: connection
status, current DSP selections, all available lists (modes / rates /
filters / shapers / matrix profiles) and the user's favourite-filter list
from the user_settings key/value store; with `dsp=1` (the screen itself)
also how each picker entry runs on this HQPlayer (`headroom`,
playback.hqp_load), the benchmark's block (playback.hqp_benchmark), each
filter and shaper row's `facts` (hqp_atlas), and what the queue meets at
this setting (`rate_check`, hqp_rate_check); a name's card comes from
/facts. Writes go through /config (any subset of filter / mode / rate /
shaper / matrix_profile / convolution — the last the assistant's switch, no
control on the screen), /favorites (add/remove a filter name from the
favourites list), /benchmark and /cuda; /meter records a page's peak-meter
interest (its readings ride the page's /api/events stream). The assistant's
six filter and modulator tools read through /assistant/{tool}
(hqp_assistant), the one path of both its tool surfaces.

The screen reads state on mount only — no polling. If the user
changes settings via HQP Desktop, a manual refresh in the UI picks
up the new values.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from config import settings

logger = logging.getLogger(__name__)
import hqp_assistant
import hqp_atlas
from db_pool import db_execute, db_query_one
from hqp_rate_check import mode_kind
from hqplayer_client import VOLUME_FIXED
from playback import hqp_link
from playback.hqp_backend import change_dsp
from playback.hqp_link import Building, Dropped, NotAnswering
from playback.hqp_meter import meter as hqp_meter
from playback.manager import OutputHeld, manager

router = APIRouter(prefix="/api/hqplayer", tags=["hqplayer"])

_FAVORITES_KEY = "hqplayer.favorite_filters"


def _load_favorites() -> List[str]:
    row = db_query_one(
        "SELECT value FROM user_settings WHERE key = %(k)s",
        {"k": _FAVORITES_KEY},
    )
    if not row or not isinstance(row.get("value"), list):
        return []
    return [str(x) for x in row["value"] if isinstance(x, str)]


def _save_favorites(favs: List[str]) -> None:
    import json
    db_execute(
        """
        INSERT INTO user_settings (key, value) VALUES (%s, %s::jsonb)
        ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value,
                updated_at = CURRENT_TIMESTAMP
        """,
        (_FAVORITES_KEY, json.dumps(favs)),
    )


# -- Request models -----------------------------------------------------------

class ConfigRequest(BaseModel):
    filter: Optional[int] = None        # filter index from get_filters()
    filter1x: Optional[int] = None      # PCM 1x filter; paired with `filter`
    shaper: Optional[int] = None
    mode: Optional[int] = None
    rate: Optional[int] = None
    matrix_profile: Optional[str] = None
    convolution: Optional[bool] = None     # the assistant's switch: no control on the screen


class FavoriteRequest(BaseModel):
    name: str
    action: str                         # 'add' | 'remove'


class BenchmarkRequest(BaseModel):
    mode: Optional[str] = None          # 'pcm' | 'sdm'; None = the mode HQPlayer is in
    fixed_volume_ok: bool = False       # the owner turned the amplifier down: HQPlayer cannot


class LeaveRequest(BaseModel):
    run: int                            # the benchmark run whose mark the owner leaves as it is


class CudaRequest(BaseModel):
    value: Optional[str] = None         # 'off' | 'full' | 'convolution'; None = not known


class VolumeRequest(BaseModel):
    # +1 nudges up by 1 dB; -1 nudges down. Bigger steps are
    # accepted so the same endpoint can serve a larger button or a
    # slider drag. HQPlayer's own volume_up/down step is 0.5 dB,
    # which is too granular for tap-tap-tap; we apply set_volume
    # with the delta computed from the current state.
    delta: float


# -- State --------------------------------------------------------------------

@router.get("/state")
def get_state(dsp: bool = False) -> Dict[str, Any]:
    """Snapshot everything the HQPlayer settings screen needs. `dsp` adds
    what only that screen shows — the headroom marks, the Benchmark block
    and the volume range, a dozen queries and two more round-trips — and
    records what HQPlayer said of itself; the More drawer and the Output
    picker's dot ask the plain state, a connection check."""
    from playback.hqp_backend import _stream_mode, hqp_media_host
    response: Dict[str, Any] = {
        "host": settings.hqplayer_host,
        "port": settings.hqplayer_port,
        "connected": False,
        # How HQPlayer reaches the files — read off its address — and the
        # proxy address its media URLs name: the screen shows both so a
        # remote HQPlayer that cannot fetch anything is diagnosable from
        # the phone.
        "file_access": "stream" if _stream_mode() else "path",
        "media_url_host": hqp_media_host(),
        "media_url_port": settings.media_proxy_port,
    }
    # The row the Output picker registered for this HQPlayer — its name
    # is what the screen calls it, its id what Settings › Library keys on.
    import hqp_library
    ep = hqp_library.endpoint_by_address(settings.hqplayer_host, settings.hqplayer_port)
    response["endpoint_id"] = ep["id"] if ep else None
    response["name"] = ep["name"] if ep else None
    response["label"] = hqp_library.label(ep["name"], ep["product"]) if ep else None
    # Its library, when it has one of its own (another machine): what this
    # node holds of it and the running job — the screen's Library section.
    from routers.settings import _hqp_library_state
    lib = _hqp_library_state()
    response["library"] = {k: lib.get(k) for k in ("own_library", "synced", "files", "albums",
                                                   "measured", "running", "cancel_requested",
                                                   "progress", "last_synced_at")}
    if dsp:
        # The job and the last run whether or not HQPlayer answers now: a
        # heavy point can hold its control port past this read, and the
        # run's Cancel must still be there.
        from playback import hqp_benchmark
        response["benchmark"] = hqp_benchmark.summary(ep)
    link = hqp_link.selected()
    held = manager.held
    if link.lent and held is not None:
        # The benchmark drives HQPlayer on its own connection: nothing else
        # asks it meanwhile — the screen keeps what it read last.
        return {**response, "connected": True, "held": held["label"]}
    building = link.build.silent()
    if building is not None:
        # HQPlayer answers nothing while it builds: the screen keeps what it
        # read last and says why its controls wait.
        return {**response, "connected": True, "building": building, "busy": link.busy()}

    try:
        with link.asking() as hqp:
            info = hqp.get_info()
            state = hqp.get_state()
            lists, refused = {}, {}
            for name, read in (("modes", hqp.get_modes), ("rates", hqp.get_rates),
                               ("filters", hqp.get_filters), ("shapers", hqp.get_shapers),
                               ("matrix_profiles", hqp.matrix_list_profiles)):
                lists[name] = read()
                if lists[name] is None:
                    refused[name] = hqp.refusal()
            status = hqp.get_status() if dsp else None
            volume_range = hqp.volume_range() if dsp else None
    except Building as e:
        return {**response, "connected": True, "building": e.state, "busy": str(e)}
    except (BrokenPipeError, ConnectionError, OSError) as e:
        return {**response, "error": str(e)}

    # TCP connect() on its own isn't proof of being talking to the
    # HQPlayer control protocol — HQPlayer also listens on 4322 (its
    # metering stream, the control port + 1), on 8019 (the UPnP renderer)
    # and, Embedded, on 8088 (the web interface); and a stray service can
    # hold any port. Treat the connection as healthy only when GetInfo came
    # back with the actual `product` field — that's protocol-level
    # confirmation that the other side is HQPlayer.
    really_connected = bool(info and info.get("product"))
    response["connected"] = really_connected
    response["info"] = info or {}
    response["state"] = state or {}
    # A list HQPlayer did not give (a refusal, a lost answer) is null and
    # named in `missing`, its refusal in `refused` — never an empty one: an
    # empty picker would say HQPlayer has nothing to choose from.
    response.update(lists)
    response["missing"] = [name for name, items in lists.items() if items is None]
    response["refused"] = refused
    response["favorite_filters"] = _load_favorites()
    if not dsp:
        return response
    if ep is not None and info:
        hqp_library.note_info(ep["id"], info, here=not _stream_mode())
        ep = hqp_library.endpoint_by_address(settings.hqplayer_host, settings.hqplayer_port)
    response["headroom"] = _headroom(ep, state, status, lists)
    response["benchmark"] = _benchmark_block(ep, state, lists, volume_range)
    response["volume_range"] = volume_range
    # Said by the server, in the words the volume route and the assistant use.
    response["volume_fixed"] = (VOLUME_FIXED if volume_range is not None
                                and not volume_range["enabled"] else None)
    _join_facts(response, info, state, lists)
    rate_check = _rate_lines(info, state, lists)
    if rate_check:
        response["rate_check"] = rate_check
    return response


def _atlas() -> Optional[hqp_atlas.Atlas]:
    """The atlas this node holds: the shipped file, else its own copy."""
    overlay = settings.hqp_atlas_overlay
    return hqp_atlas.read(hqp_atlas.SHIPPED, Path(overlay) if overlay else None)


def _list_mode(state: Optional[dict], modes: Optional[List[dict]],
               filters: Optional[List[dict]]) -> Optional[str]:
    """The mode HQPlayer's lists belong to: the filters' own arrow (⥮ PCM,
    ⥣ SDM), or for lists without descriptions (HQPlayer 5) the mode it runs;
    None when neither says."""
    arrows = {f["parsed"]["mode"] for f in filters or () if f["parsed"]}
    if arrows:
        return arrows.pop() if len(arrows) == 1 else None
    return mode_kind(_mode_of(state, modes)) if state and modes else None


def _join_facts(response: Dict[str, Any], info: Optional[dict], state: Optional[dict],
                lists: dict) -> None:
    """Each row of the filter and shaper lists gains `facts`, in HQPlayer's
    order and by name: HQPlayer's own and, while the node holds an atlas
    file, the measured ones; the sheets' head lines ride in `atlas`, absent
    without a file. A PCM shapers list is dithers, which the atlas holds
    none of."""
    atlas = _atlas()
    engine = (info or {}).get("engine")
    mode = _list_mode(state, lists["modes"], lists["filters"])
    for key, kind in (("filters", "filter"), ("shapers", "modulator" if mode == "sdm" else "dither")):
        rows = lists[key]
        if rows is not None:
            response[key] = [{**row, "facts": facts}
                             for row, facts in zip(rows, hqp_atlas.facts(atlas, kind, rows, engine, mode))]
    if atlas is not None:
        response["atlas"] = hqp_atlas.head(atlas, engine, mode)


def _rate_queue(state: Optional[dict], lists: dict) -> Optional[Tuple[Optional[str], Any]]:
    """What the rate check (hqp_rate_check) reads besides HQPlayer's State
    and lists: the adaptive output rate as HQPlayer saved it, and the
    sources of the queue from the slot HQPlayer reads — the first while
    nothing has played — to the end. None without State and the four lists,
    while HQPlayer is not the output, while another controller's playlist
    plays (an entry not ours, a mirror that drifted), or for an empty
    queue."""
    import hqp_library
    from playback import hqp_load
    from playback.hqp_backend import _stream_mode
    active, status = manager.active, manager.latest_status
    if (not state or any(lists.get(k) is None for k in ("modes", "rates", "filters", "shapers"))
            or active is None or active.id != "hqplayer" or active.drift
            or status.get("source") == "external"):
        return None
    items = manager.queue.snapshot()[max(status.get("track_index") or 1, 1) - 1:]
    if not items:
        return None
    return (None if _stream_mode() else hqp_library.local_engine().adaptive_rate,
            hqp_load.sources([it.opener() for it in items]))


def _rate_lines(info: Optional[dict], state: Optional[dict], lists: dict) -> Dict[str, list]:
    """What the screen says when the queue meets a rate HQPlayer cannot reach
    (hqp_rate_check): each phrase under the row it names, for the queue
    _rate_queue reads. The node's own atlas copy speaks here, on the node's
    own screen."""
    import hqp_rate_check
    queue = _rate_queue(state, lists)
    if queue is None:
        return {}
    adaptive, sources = queue
    inputs = hqp_rate_check.inputs(state, lists["modes"], lists["rates"], lists["filters"], lists["shapers"],
                                   adaptive=adaptive, atlas=_atlas(), engine=(info or {}).get("engine"))
    return hqp_rate_check.lines(hqp_rate_check.check(inputs, sources))


@router.get("/facts")
def get_facts(kind: str, name: str) -> Dict[str, Any]:
    """The card of one of HQPlayer's names (hqp_atlas.card): HQPlayer's own
    facts and, while the node holds an atlas file, the measured ones. Read
    from HQPlayer's list as the screen reads it, so it answers only for a
    name HQPlayer lists now — a modulator only while its list is SDM's: 404
    otherwise; 409 while the output is lent, HQPlayer builds or answers
    nothing; 503 when it cannot be reached or did not give the list."""
    if kind not in ("filter", "modulator"):
        raise HTTPException(status_code=400, detail="kind must be 'filter' or 'modulator'")
    link = hqp_link.selected()
    try:
        link.refuse()
        with link.asking() as hqp:
            info, state, modes, filters = hqp.get_info(), hqp.get_state(), hqp.get_modes(), hqp.get_filters()
            shapers = hqp.get_shapers() if kind == "modulator" else None
    except (Building, Dropped, NotAnswering) as e:
        raise _busy(e)
    except (BrokenPipeError, ConnectionError, OSError) as e:
        raise HTTPException(status_code=503, detail=f"HQPlayer not reachable: {e}")
    rows = filters if kind == "filter" else shapers
    if rows is None:
        raise HTTPException(status_code=503, detail=f"HQPlayer did not list its {kind}s")
    mode = _list_mode(state, modes, filters)
    card = (hqp_atlas.card(_atlas(), kind, name, rows, (info or {}).get("engine"), mode)
            if kind == "filter" or mode == "sdm" else None)
    if card is None:
        raise HTTPException(status_code=404, detail="HQPlayer does not list it now — Refresh asks again.")
    return card


def _mode_of(state: dict, modes: List[dict]) -> Optional[str]:
    """The mode a setting is measured in, by name: the one chosen, or for
    [source] the one HQPlayer runs (State active_mode is a ModesItem value)."""
    name = next((m["name"] for m in modes if m["index"] == state.get("mode")), None)
    if mode_kind(name) is None:
        name = next((m["name"] for m in modes if m["value"] == state.get("active_mode")), None)
    return name


def _headroom(ep: Optional[dict], state: Optional[dict], status, lists: dict) -> Dict[str, Any]:
    """How each picker entry runs on this HQPlayer, for the source the owner
    hears: the playing one (what HQPlayer decodes), else the file the queue
    plays next — and the setting the marks are read at (`setting`: the
    output rate, the filter and the shaper). Empty without a measured build
    or a known source — or, for the queued file, a list HQPlayer did not
    give."""
    from hqplayer_client import PlaybackState
    from playback import hqp_load
    from playback.hqp_benchmark import running_filter
    if not ep or not ep.get("hqp_engine") or not state:
        return {}
    if (status is not None and status.src_rate and status.src_channels
            and status.state in (PlaybackState.PLAYING, PlaybackState.PAUSED)):
        mode, rate_out = status.active_mode, status.active_rate
        filt, shaper = status.active_filter, status.active_shaper
        src = (status.src_rate, status.src_channels)
    else:
        if any(lists[k] is None for k in ("modes", "rates", "filters", "shapers")):
            return {}
        item = manager.queue.item_at(manager.latest_status.get("track_index") or 1)
        src = hqp_load.file_source(item.opener()) if item is not None else None
        mode = _mode_of(state, lists["modes"])
        if src is None or mode is None:
            return {}
        hz = {r["index"]: r["rate"] for r in lists["rates"]}
        rate_out = hz.get(state["rate"]) or state.get("active_rate") or 0
        filt = running_filter({f["index"]: f["name"] for f in lists["filters"]},
                               state["filterNx"], state["filter1x"], src[0])
        shaper = next((sh["name"] for sh in lists["shapers"] if sh["index"] == state["shaper"]), "")
    ctx = hqp_load.context(ep, mode=mode, state=state)
    return {**hqp_load.headroom(ctx, rate_out=rate_out, filter=filt, shaper=shaper,
                                src_rate=src[0], src_channels=src[1]),
            "setting": {"rate_out": rate_out, "filter": filt, "shaper": shaper}}


def _benchmark_block(ep: Optional[dict], state: Optional[dict], lists: dict,
                     volume_range: Optional[dict]) -> Dict[str, Any]:
    """The Benchmark block: the job and the last run, CUDA offload and where
    it is known from, whether HQPlayer can lower its volume here, the
    machine it runs on (this one only), and what a run of the current mode
    would measure now — or, while no run is running, why that is not known
    (`no_estimate`): the HQPlayer is not registered yet, it did not report
    its state, a list the run is planned from is missing, or its mode
    follows the source."""
    import hqp_library
    from playback import hqp_benchmark
    from playback.hqp_backend import _stream_mode
    out = hqp_benchmark.summary(ep)
    # What a run left to the owner still holds of its test settings, named
    # from the lists this screen read
    out["left"] = hqp_benchmark.left_on((ep or {}).get("id"), state, lists)
    here = not _stream_mode()
    out.update(cuda=(ep or {}).get("cuda"),
               cuda_read=here and hqp_library.local_cuda()[0],
               volume_control=bool(state) and hqp_benchmark.mute_level(
                   volume_range, state["volume"]) is not None,
               host=({k: ep.get(f"host_{k}") for k in ("cpu", "cores", "gpu", "ram_gb")}
                     if here and ep else None),
               estimate=None, no_estimate=None, other_modes=[])
    if out["job"]["running"]:
        return out
    out["no_estimate"] = ("unregistered" if not ep or not ep.get("hqp_engine")
                          else "state" if not state
                          else "lists" if any(lists[k] is None
                                              for k in ("modes", "rates", "filters", "shapers"))
                          else None)
    if out["no_estimate"] is not None:
        return out
    # A run measures the mode chosen in HQPlayer ([source] chooses none):
    # the others are measured on request.
    mode = next((m["name"] for m in lists["modes"] if m["index"] == state.get("mode")), None)
    kind = mode_kind(mode)
    kinds = {mode_kind(m["name"]) for m in lists["modes"]} - {None}
    out["other_modes"] = sorted(kinds - {kind})
    if kind is None:
        out["no_estimate"] = "source"
        return out
    grid = hqp_benchmark.plan(ep, kind=kind, mode_name=mode, state=state,
                              filters=lists["filters"], shapers=lists["shapers"],
                              rates=lists["rates"])
    asks = grid.asks()
    out["estimate"] = {"mode": mode, "points": asks,
                       "seconds": round(asks * hqp_benchmark.pace(ep["id"]))}
    return out


def _busy(e: Exception) -> HTTPException:
    """409 for a request HQPlayer's link does not send now: lent to the
    benchmark (named by its job — the link only knows it is lent), building
    a setting, answering nothing, or superseded by a newer intent. Busy, not
    broken."""
    hold = manager.held
    return HTTPException(status_code=409,
                         detail=str(OutputHeld(hold)) if hold is not None else str(e))


def _refuse_while_held() -> None:
    """A lent output (the benchmark drives HQPlayer) takes no volume change
    from here (a DSP change checks it in change_dsp): it would land in the
    middle of a measurement. Called under the link's command lock — the
    hold detaches the backend under it, so a change that passed the check
    is through before the run sends anything."""
    hold = manager.held
    if hold is not None:
        raise HTTPException(status_code=409, detail=str(OutputHeld(hold)))


# -- The assistant's filter and modulator tools ------------------------------

_LIST_READS = {"modes": "get_modes", "rates": "get_rates", "filters": "get_filters", "shapers": "get_shapers"}


@router.get("/assistant/{tool}")
def assistant(tool: Literal[tuple(hqp_assistant.BY_SHORT)], name: str = "", name_a: str = "",
              name_b: str = "", axis: str = "", near: str = "", rate: str = "", mode: str = "") -> Dict[str, str]:
    """One of the assistant's six HQPlayer filter and modulator tools
    (hqp_assistant), the one path both its tool surfaces take — in process,
    and the MCP server over HTTP — so the two say one thing: the refusals,
    what HQPlayer answers on its link, the atlas as the site published it
    (the shipped file, never the node's own copy), this host's speeds.
    Always 200 with the tool's text: a refusal is its answer."""
    args = {k: v.strip() for k, v in (("name", name), ("name_a", name_a), ("name_b", name_b), ("axis", axis),
                                      ("near", near), ("rate", rate), ("mode", mode))}
    return {"text": hqp_assistant.answer(tool, args, _assistant_read(tool, args))}


def _assistant_read(tool: str, args: Dict[str, str]) -> hqp_assistant.Read:
    """What one of the six reads: nothing while another output is chosen or
    the output is lent; else HQPlayer's answers on its link in one asking
    block — a reading, never held at the command gate — and what the tool
    reads besides."""
    from routers.settings import _read
    output = _read("output.type")
    if output and output != "hqplayer":
        return hqp_assistant.Read(output=output)
    hold = manager.held
    if hold is not None:
        return hqp_assistant.Read(held=hold["label"])
    needs = hqp_assistant.READS[tool]
    try:
        with hqp_link.selected().asking() as hqp:
            info, state = hqp.get_info(), hqp.get_state()
            lists, refused = {}, {}
            for name in needs:
                lists[name] = getattr(hqp, _LIST_READS[name])()
                if lists[name] is None:
                    refused[name] = hqp.refusal()
            status = hqp.get_status() if tool == "compare" else None
    except (Building, Dropped, NotAnswering) as e:
        # Lent while it read: the hold's words, as before it
        hold = manager.held
        return hqp_assistant.Read(held=hold["label"]) if hold is not None else hqp_assistant.Read(busy=str(e))
    except (BrokenPipeError, ConnectionError, OSError) as e:
        return hqp_assistant.Read(error=str(e))
    if not (info and info.get("product")):
        return hqp_assistant.Read(error="no answer to GetInfo")
    read = hqp_assistant.Read(info=info, state=state or {}, lists=lists, refused=refused,
                              mode=_list_mode(state, lists.get("modes"), lists.get("filters")))
    if tool == "host_fit":
        return read._replace(host=_host_records(args, read))
    read = read._replace(atlas=hqp_atlas.read(hqp_atlas.SHIPPED, None))
    if tool in ("filter_facts", "compare"):
        queue = _rate_queue(state, lists)
        if queue is not None:
            read = read._replace(adaptive=queue[0], sources=queue[1])
    if tool == "compare":
        import hqp_library
        ep = hqp_library.endpoint_by_address(settings.hqplayer_host, settings.hqplayer_port)
        # This host's speeds under the engine HQPlayer reports now — the
        # endpoint's last one while it reports none — as host_fit reads them
        live = ({**ep, "hqp_engine": info.get("engine") or ep["hqp_engine"]}
                if ep and ep.get("hqp_engine") else ep)
        read = read._replace(endpoint=ep, headroom=_headroom(live, state, status, lists))
    return read


def _host_records(args: Dict[str, str], read: hqp_assistant.Read) -> Dict[str, Any]:
    """host_fit's: the context of the mode asked (the one HQPlayer is in, by
    default) — the engine HQPlayer reports (the endpoint's last one while it
    reports none), this endpoint's CUDA offload, the matrix profile and
    convolution — and one name's rows there (hqp_load.fit), at a rate this
    host holds records at in it; or the words why there are none."""
    import hqp_library
    from playback import hqp_load
    ep = hqp_library.endpoint_by_address(settings.hqplayer_host, settings.hqplayer_port)
    if ep is None or not ep.get("hqp_engine"):
        return {"words": hqp_assistant.NOTHING_HERE}
    mode, why = hqp_assistant.host_ask(args, read)
    if why is not None:
        return {"words": why}
    ctx = hqp_load.context({**ep, "hqp_engine": read.info.get("engine") or ep["hqp_engine"]}, mode=mode,
                           state=read.state)
    held = hqp_load.names(ctx)
    picked = hqp_assistant.host_name(args["name"], held)
    if picked is None:
        return {"words": hqp_assistant.no_record(args["name"], mode)}
    rate, why = hqp_assistant.host_rate(args["rate"], held["rates"], mode)
    if why is not None:
        return {"words": why}
    knob, name = picked
    return {"context": ctx, "knob": knob, "name": name, "rate": rate,
            **hqp_load.fit(ctx, **{knob: name}, rate_out=rate)}


# -- Config -------------------------------------------------------------------

@router.post("/config")
def set_config(req: ConfigRequest) -> Dict[str, Any]:
    """Apply any subset of the DSP knobs (hqp_backend.change_dsp): each
    knob's outcome — taken, or refused in HQPlayer's words — and, when
    HQPlayer builds past a moment, `building` at once with the knobs still
    to go; the rest follows behind the answer. 409 while the output is lent,
    HQPlayer builds or answers nothing past a short silence, or a newer
    intent took over; 503 when it cannot be reached."""
    try:
        return change_dsp(req.model_dump())
    except (OutputHeld, Building, Dropped, NotAnswering) as e:
        raise _busy(e)
    except (BrokenPipeError, ConnectionError, OSError) as e:
        raise HTTPException(status_code=503, detail=f"HQPlayer not reachable: {e}")


# -- Volume -------------------------------------------------------------------

@router.post("/volume")
def nudge_volume(req: VolumeRequest) -> Dict[str, Any]:
    """Step the master volume by ±N dB within the range HQPlayer allows.

    HQPlayer's own VolumeUp/Down jumps in 0.5 dB increments, which is
    too granular for a tap-tap-tap UI. Read the current volume from
    State and the range from VolumeRange, write back via Volume. A
    volume HQPlayer holds fixed is refused here with the reason — its
    own answer to the Volume is a bare Error. With no range given the
    step never goes above 0 dB: a tap must not be what turns a pair of
    headphones past full scale."""
    link = hqp_link.selected()
    try:
        with link.command():
            _refuse_while_held()
            hqp = link.client
            state = hqp.get_state()
            if state is None:
                raise HTTPException(status_code=503, detail="HQPlayer state unavailable")
            span = hqp.volume_range()
            if span is not None and not span["enabled"]:
                raise HTTPException(status_code=409, detail=VOLUME_FIXED)
            new_vol = min(span["max"] if span is not None else 0.0,
                          state["volume"] + req.delta)
            if span is not None:
                new_vol = max(span["min"], new_vol)
            ok = hqp.set_volume(new_vol)
            refusal = None if ok else hqp.refusal()
    except (Building, Dropped, NotAnswering) as e:
        raise _busy(e)
    except (BrokenPipeError, ConnectionError, OSError) as e:
        raise HTTPException(status_code=503, detail=f"HQPlayer not reachable: {e}")
    if not ok:
        raise HTTPException(status_code=503, detail=refusal)
    return {"volume": new_vol}


# -- Peak meter ---------------------------------------------------------------

class MeterInterest(BaseModel):
    tab: str = Field(..., max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    on: bool
    seq: int = Field(..., ge=0)


@router.put("/meter", status_code=204)
def set_meter(req: MeterInterest) -> None:
    """A page opens or closes the peak meter (playback.hqp_meter). The wish is
    recorded whether HQPlayer is there or not: what the meter can do is said
    on the page's event stream, as `meter` messages, never as an error here.
    404 only for a page that holds no event stream — it asks again on the
    stream's `hello`."""
    if not hqp_meter.want(req.tab, req.on, req.seq):
        raise HTTPException(status_code=404, detail="This page has no event stream open")


# -- Benchmark ----------------------------------------------------------------

@router.get("/benchmark")
def get_benchmark() -> Dict[str, Any]:
    """The job, the last run, and every key measured in the current context
    — the results sheet. When HQPlayer is another build than the last run
    measured, that run's results, marked stale."""
    import hqp_library
    from playback import hqp_benchmark, hqp_load
    ep = hqp_library.endpoint_by_address(settings.hqplayer_host, settings.hqplayer_port)
    out = hqp_benchmark.summary(ep)
    out.update(rows=[], context=None, current=None, thresholds=None)
    if ep is None:
        return out
    link = hqp_link.selected()
    try:
        link.refuse()
        with link.asking() as hqp:
            state, modes = hqp.get_state(), hqp.get_modes()
            filters, shapers, rates = hqp.get_filters(), hqp.get_shapers(), hqp.get_rates()
    except (Building, Dropped, NotAnswering) as e:
        raise _busy(e)
    except (BrokenPipeError, ConnectionError, OSError) as e:
        raise HTTPException(status_code=503, detail=f"HQPlayer not reachable: {e}")
    if state is None:
        raise HTTPException(status_code=503, detail="HQPlayer state unavailable")
    missing = [name for name, items in (("modes", modes), ("filters", filters),
                                        ("shapers", shapers), ("rates", rates)) if items is None]
    if missing:
        # The sheet's tables are cut at the current filter, shaper and rate.
        raise HTTPException(status_code=503,
                            detail=f"HQPlayer did not list its {', '.join(missing)}")
    mode = _mode_of(state, modes)
    run = out["last_run"]
    engine = run["hqp_engine"] if run and run["stale"] else ep.get("hqp_engine")
    ctx = hqp_load.context({**ep, "hqp_engine": engine}, mode=mode, state=state)
    from playback.hqp_benchmark import running_filter
    hz = {r["index"]: r["rate"] for r in rates}
    out.update(
        context={k: ctx[k] for k in ("engine", "cuda", "mode", "matrix_profile", "convolution")},
        current={"filter": running_filter({f["index"]: f["name"] for f in filters},
                                           state["filterNx"], state["filter1x"], 44100),
                 "shaper": next((s["name"] for s in shapers if s["index"] == state["shaper"]), ""),
                 "rate_out": hz.get(state["rate"]) or state.get("active_rate") or 0},
        thresholds=hqp_load.thresholds(ctx),
        rows=hqp_load.results(ctx))
    return out


@router.post("/benchmark")
def start_benchmark(req: BenchmarkRequest) -> Dict[str, Any]:
    """Start a run; 409 with the reason when it cannot start now."""
    from playback import hqp_benchmark
    if req.mode not in (None, "pcm", "sdm"):
        raise HTTPException(status_code=400, detail="mode must be 'pcm' or 'sdm'")
    try:
        return hqp_benchmark.start(req.mode, fixed_volume_ok=req.fixed_volume_ok)
    except hqp_benchmark.BenchmarkRefused as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/benchmark/cancel")
def cancel_benchmark() -> Dict[str, Any]:
    from playback import hqp_benchmark
    return {"cancelled": hqp_benchmark.cancel()}


@router.post("/benchmark/leave")
def leave_benchmark_mark(req: LeaveRequest) -> Dict[str, Any]:
    """The owner's word on a run's mark HQPlayer at rest did not let be
    judged: the mode it measured is left as it is
    (hqp_benchmark.leave_as_it_is) — said again, it is still left. 409 while
    a run measures, and for a mark still to be checked; 404 for a run there
    is none of."""
    from playback import hqp_benchmark
    try:
        left = hqp_benchmark.leave_as_it_is(req.run)
    except hqp_benchmark.BenchmarkRefused as e:
        raise HTTPException(status_code=409, detail=str(e))
    except hqp_benchmark.RunNotFound:
        raise HTTPException(status_code=404, detail="No such benchmark run")
    if not left:
        raise HTTPException(status_code=409, detail="That run's settings are checked, or about to be")
    return {"left": True}


@router.put("/cuda")
def put_cuda(req: CudaRequest) -> Dict[str, Any]:
    """The owner's answer about CUDA offload on an HQPlayer whose settings
    this node cannot read. One here is read from HQPlayer's own settings."""
    import hqp_library
    from playback.hqp_backend import _stream_mode
    if req.value not in (None, "off", "full", "convolution"):
        raise HTTPException(status_code=400, detail="value must be off, full or convolution")
    ep = hqp_library.endpoint_by_address(settings.hqplayer_host, settings.hqplayer_port)
    if ep is None:
        raise HTTPException(status_code=409, detail="This HQPlayer is not registered yet")
    if not _stream_mode() and hqp_library.local_cuda()[0]:
        raise HTTPException(status_code=409, detail="Read from HQPlayer's own settings on this computer")
    hqp_library.set_cuda(ep["id"], req.value)
    return {"cuda": req.value}


# -- Favorites ----------------------------------------------------------------

@router.post("/favorites/filter")
def update_favorite_filter(req: FavoriteRequest) -> Dict[str, Any]:
    """Add or remove a filter name from the user's favourites list."""
    if req.action not in ("add", "remove"):
        raise HTTPException(status_code=400, detail="action must be 'add' or 'remove'")
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is empty")
    favs = _load_favorites()
    if req.action == "add" and name not in favs:
        favs.append(name)
    elif req.action == "remove" and name in favs:
        favs.remove(name)
    _save_favorites(favs)
    return {"ok": True, "favorite_filters": favs}
