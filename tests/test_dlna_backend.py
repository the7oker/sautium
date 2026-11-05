"""The DLNA output against renderers of its own.

FakeRenderer is a UPnP MediaRenderer on loopback — device description,
SCPDs, SOAP control, GENA eventing — that the real DlnaBackend drives
through async_upnp_client, as it drives a DAP or a phone. It plays by
the clock: a load is a URI, Play starts the clock, the end of the stream
moves to the armed NextURI or stops. Its sink list, the length its decoder
reports and whether it answers at all are the knobs the tests turn.
"""

import asyncio
import re
import shutil
import socket
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import pytest
from aiohttp import ClientSession, ClientTimeout, web

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from config import settings  # noqa: E402
from hqplayer_client import format_time  # noqa: E402
from playback import dlna_backend  # noqa: E402
from playback import tracker  # noqa: E402
from playback.dlna_backend import DlnaBackend  # noqa: E402
from playback.queue import CanonicalQueue, QueueItem  # noqa: E402
from streaming import service as streaming_service  # noqa: E402
from streaming import transcode  # noqa: E402
from streaming.proxy import MediaProxy  # noqa: E402
# The HQPlayer side of a session that switches: its fake and transport
from test_hqp_backend import _moving_transport, fake  # noqa: E402,F401

# The tracker as the module defines it: the manager fixture stands it down
_TRACK_PLAY_EVENT = tracker.track_play_event

AVT = "urn:schemas-upnp-org:service:AVTransport:1"
RC = "urn:schemas-upnp-org:service:RenderingControl:1"
CM = "urn:schemas-upnp-org:service:ConnectionManager:1"
SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"

# An AK Connect renderer's sink list (a DAP's), as it answered GetProtocolInfo
# (2026-10-24): DSD and WAV under spellings the node does not use, no WavPack.
_AK_FLAGS = "DLNA.ORG_OP=01;DLNA.ORG_FLAGS=40000000000000000000000000000000"
AK_CONNECT_SINK = [f"http-get:*:{mime}:DLNA.ORG_PN={pn};{_AK_FLAGS}" for mime, pn in (
    ("audio/mpeg", "MP3"), ("audio/mp4", "M4A"), ("audio/x-wav", "WAV"),
    ("audio/x-ms-wma", "WMA"), ("audio/ogg", "OGG"), ("audio/aac", "AAC"),
    ("audio/x-ape", "APE"), ("audio/dsf", "DSF"), ("audio/dff", "DSD"),
    ("audio/aiff", "AIFF"), ("audio/flac", "FLAC"))]
# A phone renderer that lists only what it decodes: never DSD, WavPack or APE.
PHONE_SINK = [f"http-get:*:{mime}:*" for mime in (
    "audio/flac", "audio/x-flac", "audio/mpeg", "audio/mp4", "audio/x-m4a",
    "audio/aac", "audio/wav", "audio/x-wav", "audio/aiff", "audio/x-aiff",
    "audio/ogg", "application/ogg")]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _hms(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"


def _seconds(hms: str) -> Optional[float]:
    try:
        h, m, s = hms.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except (AttributeError, ValueError):
        return None


# -- the service descriptions ---------------------------------------------------

_AVT_VARS = {
    "TransportState": "string", "TransportStatus": "string", "TransportPlaySpeed": "string",
    "PlaybackStorageMedium": "string", "RecordStorageMedium": "string",
    "PossiblePlaybackStorageMedia": "string", "PossibleRecordStorageMedia": "string",
    "PossibleRecordQualityModes": "string", "CurrentRecordQualityMode": "string",
    "RecordMediumWriteStatus": "string", "CurrentPlayMode": "string",
    "NumberOfTracks": "ui4", "CurrentTrack": "ui4", "CurrentTrackDuration": "string",
    "CurrentMediaDuration": "string", "CurrentTrackMetaData": "string",
    "CurrentTrackURI": "string", "AVTransportURI": "string",
    "AVTransportURIMetaData": "string", "NextAVTransportURI": "string",
    "NextAVTransportURIMetaData": "string", "RelativeTimePosition": "string",
    "AbsoluteTimePosition": "string", "RelativeCounterPosition": "i4",
    "AbsoluteCounterPosition": "i4", "CurrentTransportActions": "string",
    "LastChange": "string", "A_ARG_TYPE_SeekMode": "string",
    "A_ARG_TYPE_SeekTarget": "string", "A_ARG_TYPE_InstanceID": "ui4",
}
_I = ("InstanceID", "A_ARG_TYPE_InstanceID")
_AVT_ACTIONS = {
    "SetAVTransportURI": ([_I, ("CurrentURI", "AVTransportURI"),
                           ("CurrentURIMetaData", "AVTransportURIMetaData")], []),
    "SetNextAVTransportURI": ([_I, ("NextURI", "NextAVTransportURI"),
                               ("NextURIMetaData", "NextAVTransportURIMetaData")], []),
    "GetMediaInfo": ([_I], [("NrTracks", "NumberOfTracks"), ("MediaDuration", "CurrentMediaDuration"),
                            ("CurrentURI", "AVTransportURI"), ("CurrentURIMetaData", "AVTransportURIMetaData"),
                            ("NextURI", "NextAVTransportURI"), ("NextURIMetaData", "NextAVTransportURIMetaData"),
                            ("PlayMedium", "PlaybackStorageMedium"), ("RecordMedium", "RecordStorageMedium"),
                            ("WriteStatus", "RecordMediumWriteStatus")]),
    "GetTransportInfo": ([_I], [("CurrentTransportState", "TransportState"),
                                ("CurrentTransportStatus", "TransportStatus"),
                                ("CurrentSpeed", "TransportPlaySpeed")]),
    "GetPositionInfo": ([_I], [("Track", "CurrentTrack"), ("TrackDuration", "CurrentTrackDuration"),
                               ("TrackMetaData", "CurrentTrackMetaData"), ("TrackURI", "CurrentTrackURI"),
                               ("RelTime", "RelativeTimePosition"), ("AbsTime", "AbsoluteTimePosition"),
                               ("RelCount", "RelativeCounterPosition"),
                               ("AbsCount", "AbsoluteCounterPosition")]),
    "GetDeviceCapabilities": ([_I], [("PlayMedia", "PossiblePlaybackStorageMedia"),
                                     ("RecMedia", "PossibleRecordStorageMedia"),
                                     ("RecQualityModes", "PossibleRecordQualityModes")]),
    "GetTransportSettings": ([_I], [("PlayMode", "CurrentPlayMode"),
                                    ("RecQualityMode", "CurrentRecordQualityMode")]),
    "GetCurrentTransportActions": ([_I], [("Actions", "CurrentTransportActions")]),
    "Stop": ([_I], []),
    "Play": ([_I, ("Speed", "TransportPlaySpeed")], []),
    "Pause": ([_I], []),
    "Seek": ([_I, ("Unit", "A_ARG_TYPE_SeekMode"), ("Target", "A_ARG_TYPE_SeekTarget")], []),
    "Next": ([_I], []),
    "Previous": ([_I], []),
}
_RC_VARS = {"LastChange": "string", "Mute": "boolean", "Volume": "ui2",
            "A_ARG_TYPE_Channel": "string", "A_ARG_TYPE_InstanceID": "ui4"}
_C = ("Channel", "A_ARG_TYPE_Channel")
_RC_ACTIONS = {
    "GetMute": ([_I, _C], [("CurrentMute", "Mute")]),
    "SetMute": ([_I, _C, ("DesiredMute", "Mute")], []),
    "GetVolume": ([_I, _C], [("CurrentVolume", "Volume")]),
    "SetVolume": ([_I, _C, ("DesiredVolume", "Volume")], []),
}
_CM_VARS = {"SourceProtocolInfo": "string", "SinkProtocolInfo": "string",
            "CurrentConnectionIDs": "string"}
_CM_ACTIONS = {
    "GetProtocolInfo": ([], [("Source", "SourceProtocolInfo"), ("Sink", "SinkProtocolInfo")]),
    "GetCurrentConnectionIDs": ([], [("ConnectionIDs", "CurrentConnectionIDs")]),
}
_EVENTED = {"LastChange", "SourceProtocolInfo", "SinkProtocolInfo", "CurrentConnectionIDs"}


def _scpd(actions: dict, variables: dict) -> str:
    acts = "".join(
        f"<action><name>{name}</name><argumentList>"
        + "".join(f"<argument><name>{a}</name><direction>in</direction>"
                  f"<relatedStateVariable>{v}</relatedStateVariable></argument>" for a, v in ins)
        + "".join(f"<argument><name>{a}</name><direction>out</direction>"
                  f"<relatedStateVariable>{v}</relatedStateVariable></argument>" for a, v in outs)
        + "</argumentList></action>"
        for name, (ins, outs) in actions.items())
    def var(name, kind):
        extra = ("<allowedValueRange><minimum>0</minimum><maximum>100</maximum>"
                 "<step>1</step></allowedValueRange>") if name == "Volume" else ""
        evented = "yes" if name in _EVENTED else "no"
        return (f'<stateVariable sendEvents="{evented}"><name>{name}</name>'
                f"<dataType>{kind}</dataType>{extra}</stateVariable>")
    return ('<?xml version="1.0"?><scpd xmlns="urn:schemas-upnp-org:service-1-0">'
            "<specVersion><major>1</major><minor>0</minor></specVersion>"
            f"<actionList>{acts}</actionList><serviceStateTable>"
            + "".join(var(n, k) for n, k in variables.items())
            + "</serviceStateTable></scpd>")


_SERVICES = {"avt": (AVT, "urn:upnp-org:serviceId:AVTransport", _AVT_ACTIONS, _AVT_VARS),
             "rc": (RC, "urn:upnp-org:serviceId:RenderingControl", _RC_ACTIONS, _RC_VARS),
             "cm": (CM, "urn:upnp-org:serviceId:ConnectionManager", _CM_ACTIONS, _CM_VARS)}


class Fault(Exception):
    def __init__(self, code: int, text: str):
        super().__init__(f"{code} {text}")
        self.code, self.text = code, text


class FakeRenderer:
    """A MediaRenderer on 127.0.0.1. `sink`: its GetProtocolInfo sink list —
    a SetAVTransportURI whose DIDL names a MIME the list lacks is refused
    with 714, as the phone does, and such a NextURI is not played at the
    boundary: the renderer stops there instead. `reports`: the length its decoder reports
    for a URI, given the DIDL's res@duration (the default echoes the DIDL,
    as BubbleUPnP does). The stream itself plays for the DIDL's length less
    the URI's `ss` offset."""

    def __init__(self, *, name: str = "Fake Renderer", udn: Optional[str] = None,
                 sink: list = (), reports: Optional[Callable] = None):
        self.udn = udn or f"uuid:{uuid.uuid4()}"
        self.name = name
        self.sink = list(sink)
        self.reports = reports or (lambda uri, didl_length: didl_length)
        self.calls: list[tuple[str, dict]] = []
        self.state = "NO_MEDIA_PRESENT"
        self.status = "OK"
        self.uri = self.meta = self.next_uri = self.next_meta = ""
        self.volume = 50
        self.notifying = True             # False: its events stop too
        self.faults: dict[str, list] = {}  # action → UPnP error codes to answer it with, in turn
        # Seconds after a Seek is answered until it takes effect: the renderer
        # plays on from where it was meanwhile, and says so
        self.seek_delay = 0.0
        # Its transport state says STOPPED while it plays on — GetTransportInfo
        # and its events alike (a wedged stack, live 2026-09-01)
        self.says_stopped = False
        # Its description is held until this is set: an attach stopped half
        # way, as a phone waking on a tunnel answers late
        self.hold_description: Optional[threading.Event] = None
        # How its services' documents are answered: "large" (past the node's
        # bound on an answer), "packed" (compressed), "moved" (a redirect)
        self.scpd_shape: Optional[str] = None
        self.describing = threading.Event()    # its description was asked
        # In an Opus stream (?q=opus…): False — its RelTime reads 0:00:00
        # throughout; "ignores" — a Seek is answered and moves nothing. A DAP
        # with no byte↔time map for VBR Opus does both (live, 2026-07-24)
        self.opus_position = True
        self.opus_seek = "lands"
        # An action held this many seconds before it takes effect and is
        # answered: a request still on its way — or, named in `acts_first`,
        # taken at once and answered only then (an answer held or lost)
        self.slow: dict[str, float] = {}
        self.acts_first: set = set()
        self.answered: list[str] = []          # actions in the order their answers went
        self._base = 0.0                  # position at _t0
        self._t0: Optional[float] = None  # monotonic start while PLAYING
        self._subs: dict[str, tuple[str, str]] = {}   # sid → (service key, callback)
        self._seq: dict[str, int] = {}
        self.port = _free_port()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True,
                                        name=f"fake-renderer-{self.port}")
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._ticker: Optional[asyncio.Task] = None
        self._session: Optional[ClientSession] = None

    # -- life ----------------------------------------------------------------

    @property
    def location(self) -> str:
        return f"http://127.0.0.1:{self.port}/desc.xml"

    @property
    def record(self) -> dict:
        """The renderer as the node stores one ({udn, location, name})."""
        return {"udn": self.udn, "location": self.location, "name": self.name}

    def _run(self, coro, timeout: float = 10.0):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def start(self) -> "FakeRenderer":
        self._thread.start()
        self._run(self._start())
        return self

    async def _start(self):
        app = web.Application()
        app.router.add_get("/desc.xml", self._description)
        app.router.add_get("/{svc}/scpd.xml", self._scpd)
        app.router.add_post("/{svc}/control", self._control)
        app.router.add_route("SUBSCRIBE", "/{svc}/event", self._subscribe)
        app.router.add_route("UNSUBSCRIBE", "/{svc}/event", self._unsubscribe)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._session = ClientSession(timeout=ClientTimeout(total=5))
        await self._listen()
        self._ticker = asyncio.ensure_future(self._tick())

    async def _listen(self):
        self._site = web.TCPSite(self._runner, "127.0.0.1", self.port)
        await self._site.start()

    def stop_answering(self) -> None:
        """Its listener closes — every request to it is refused — while it
        plays on and keeps sending events to its subscribers, as the phone
        did when its listener stopped with Wi-Fi coming back."""
        async def _close():
            await self._site.stop()
            self._site = None
        self._run(_close())

    def answer_again(self) -> None:
        self._run(self._listen())

    def close(self) -> None:
        async def _close():
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await self._session.close()
            await self._runner.cleanup()
        self._run(_close())
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)

    # -- the player ------------------------------------------------------------

    @staticmethod
    def didl_length(meta: str) -> Optional[float]:
        m = re.search(r'duration="([^"]+)"', meta or "")
        return _seconds(m.group(1)) if m else None

    @staticmethod
    def didl_mime(meta: str) -> Optional[str]:
        m = re.search(r'protocolInfo="[^:"]*:[^:"]*:([^:"]+):', meta or "")
        return m.group(1) if m else None

    def takes(self, meta: str) -> bool:
        mime = self.didl_mime(meta)
        return not (self.sink and mime and not any(e.split(":")[2] == mime for e in self.sink))

    @staticmethod
    def offset(uri: str) -> float:
        return float(parse_qs(urlparse(uri).query).get("ss", ["0"])[0])

    def position(self) -> float:
        if self._t0 is None:
            return self._base
        return self._base + (time.monotonic() - self._t0)

    def stream_length(self) -> Optional[float]:
        whole = self.didl_length(self.meta)
        return None if whole is None else whole - self.offset(self.uri)

    def reported_length(self) -> Optional[float]:
        return self.reports(self.uri, self.didl_length(self.meta))

    def _ended(self) -> bool:
        """The stream played to its end, or was sought there: on to the next
        URI armed, else stopped. True when it ended."""
        length = self.stream_length()
        if self.state != "PLAYING" or length is None or self.position() < length:
            return False
        if self.next_uri and self.takes(self.next_meta):
            self.uri, self.meta = self.next_uri, self.next_meta
            self.next_uri = self.next_meta = ""
            self._base, self._t0 = 0.0, time.monotonic()
        else:
            self.state, self._base, self._t0 = "STOPPED", 0.0, None
            self.next_uri = self.next_meta = ""
        return True

    async def _tick(self):
        while True:
            await asyncio.sleep(0.05)
            if self._ended():
                await self._changed()

    def _transport_state(self) -> str:
        return "STOPPED" if self.says_stopped and self.state == "PLAYING" else self.state

    def _avt_state(self) -> dict:
        return {"TransportState": self._transport_state(), "TransportStatus": self.status,
                "CurrentTrackURI": self.uri, "AVTransportURI": self.uri,
                "CurrentTrackDuration": _hms(self.reported_length() or 0),
                "NextAVTransportURI": self.next_uri, "NumberOfTracks": "1" if self.uri else "0"}

    async def _changed(self):
        await self._notify_all("avt", self._last_change("AVT", self._avt_state()))

    @staticmethod
    def _last_change(kind: str, values: dict) -> dict:
        ns = {"AVT": "urn:schemas-upnp-org:metadata-1-0/AVT/",
              "RCS": "urn:schemas-upnp-org:metadata-1-0/RCS/"}[kind]
        inner = "".join(
            f'<{k} channel="Master" val="{escape(str(v), {chr(34): "&quot;"})}"/>' if kind == "RCS"
            else f'<{k} val="{escape(str(v), {chr(34): "&quot;"})}"/>'
            for k, v in values.items())
        return {"LastChange": f'<Event xmlns="{ns}"><InstanceID val="0">{inner}</InstanceID></Event>'}

    def _act(self, svc: str, action: str, args: dict) -> dict:
        if svc == "cm":
            if action == "GetProtocolInfo":
                return {"Source": "", "Sink": ",".join(self.sink)}
            return {"ConnectionIDs": "0"}
        if svc == "rc":
            if action == "GetVolume":
                return {"CurrentVolume": str(self.volume)}
            if action == "SetVolume":
                self.volume = int(args["DesiredVolume"])
                return {}
            return {"CurrentMute": "0"} if action == "GetMute" else {}
        if action == "SetAVTransportURI":
            if not self.takes(args.get("CurrentURIMetaData", "")):
                raise Fault(714, "Illegal MIME-type")
            self.uri, self.meta = args["CurrentURI"], args.get("CurrentURIMetaData", "")
            self.state, self.status, self._base, self._t0 = "STOPPED", "OK", 0.0, None
        elif action == "SetNextAVTransportURI":
            self.next_uri, self.next_meta = args.get("NextURI", ""), args.get("NextURIMetaData", "")
        elif action == "Play":
            if not self.uri:
                raise Fault(701, "Transition not available")
            if self.state != "PLAYING":
                self._base, self._t0 = self.position(), time.monotonic()
                self.state = "PLAYING"
        elif action == "Pause":
            if self.state == "PLAYING":
                self._base, self._t0, self.state = self.position(), None, "PAUSED_PLAYBACK"
        elif action == "Stop":
            if self.uri:
                self.state, self._base, self._t0 = "STOPPED", 0.0, None
        elif action == "Seek":
            if self.state not in ("PLAYING", "PAUSED_PLAYBACK"):
                raise Fault(701, "Transition not available")
            if self.opus_seek == "ignores" and "q=opus" in self.uri:
                return {}
            target, uri = _seconds(args["Target"]) or 0.0, self.uri

            def lands():
                if self.uri == uri:
                    self._base = target
                    self._t0 = time.monotonic() if self.state == "PLAYING" else None
                    # Sought to its end, the stream ends there at once: no
                    # reading at the target comes between
                    if self._ended():
                        asyncio.ensure_future(self._changed())
            if self.seek_delay:
                self._loop.call_later(self.seek_delay, lands)
            else:
                lands()
        elif action == "GetTransportInfo":
            return {"CurrentTransportState": self._transport_state(), "CurrentTransportStatus": self.status,
                    "CurrentSpeed": "1"}
        elif action == "GetPositionInfo":
            silent = not self.opus_position and "q=opus" in self.uri
            rel = _hms(self.position()) if self.uri and not silent else "0:00:00"
            return {"Track": "1" if self.uri else "0",
                    "TrackDuration": _hms(self.reported_length() or 0),
                    "TrackMetaData": self.meta, "TrackURI": self.uri, "RelTime": rel,
                    "AbsTime": rel, "RelCount": "2147483647", "AbsCount": "2147483647"}
        elif action == "GetMediaInfo":
            return {"NrTracks": "1" if self.uri else "0", "MediaDuration": _hms(self.reported_length() or 0),
                    "CurrentURI": self.uri, "CurrentURIMetaData": self.meta,
                    "NextURI": self.next_uri, "NextURIMetaData": self.next_meta,
                    "PlayMedium": "NETWORK", "RecordMedium": "NOT_IMPLEMENTED",
                    "WriteStatus": "NOT_IMPLEMENTED"}
        elif action == "GetDeviceCapabilities":
            return {"PlayMedia": "NETWORK", "RecMedia": "NOT_IMPLEMENTED",
                    "RecQualityModes": "NOT_IMPLEMENTED"}
        elif action == "GetTransportSettings":
            return {"PlayMode": "NORMAL", "RecQualityMode": "NOT_IMPLEMENTED"}
        elif action == "GetCurrentTransportActions":
            return {"Actions": "Play,Stop,Pause,Seek"}
        return {}

    # -- HTTP --------------------------------------------------------------------

    async def _description(self, request):
        self.describing.set()
        if self.hold_description is not None:
            await asyncio.to_thread(self.hold_description.wait, 20)
        services = "".join(
            f"<service><serviceType>{st}</serviceType><serviceId>{sid}</serviceId>"
            f"<SCPDURL>/{key}/scpd.xml</SCPDURL><controlURL>/{key}/control</controlURL>"
            f"<eventSubURL>/{key}/event</eventSubURL></service>"
            for key, (st, sid, _a, _v) in _SERVICES.items())
        xml = ('<?xml version="1.0"?><root xmlns="urn:schemas-upnp-org:device-1-0">'
               "<specVersion><major>1</major><minor>0</minor></specVersion><device>"
               "<deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>"
               f"<friendlyName>{escape(self.name)}</friendlyName><manufacturer>Test</manufacturer>"
               f"<modelName>FakeRenderer</modelName><UDN>{self.udn}</UDN>"
               f"<serviceList>{services}</serviceList></device></root>")
        return web.Response(text=xml, content_type="text/xml")

    async def _scpd(self, request):
        _st, _sid, actions, variables = _SERVICES[request.match_info["svc"]]
        text = _scpd(actions, variables)
        if self.scpd_shape == "moved" and "moved" not in request.query:
            raise web.HTTPFound(f"{request.path}?moved=1")
        if self.scpd_shape == "large":
            text = text.replace("</scpd>", f'<!--{"p" * dlna_backend.ANSWER_BYTES}--></scpd>')
        if self.scpd_shape == "packed":
            import gzip
            return web.Response(body=gzip.compress(text.encode()), content_type="text/xml",
                                headers={"Content-Encoding": "gzip"})
        return web.Response(text=text, content_type="text/xml")

    async def _control(self, request):
        svc = request.match_info["svc"]
        st = _SERVICES[svc][0]
        action = request.headers.get("SOAPACTION", "").strip('"').split("#")[-1]
        body = ET.fromstring(await request.read())
        element = body.find(f".//{{{st}}}{action}")
        args = {child.tag: child.text or "" for child in (element if element is not None else [])}
        self.calls.append((action, args))
        if self.slow.get(action) and action not in self.acts_first:
            await asyncio.sleep(self.slow[action])
        before = self._avt_state()
        try:
            if self.faults.get(action):
                raise Fault(self.faults[action].pop(0), "Current state of service prevents invoking that action")
            out = self._act(svc, action, args)
            if self.slow.get(action) and action in self.acts_first:
                await asyncio.sleep(self.slow[action])
            self.answered.append(action)
        except Fault as f:
            fault = (f'<?xml version="1.0"?><s:Envelope xmlns:s="{SOAP_NS}" '
                     's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
                     "<s:Fault><faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring>"
                     '<detail><UPnPError xmlns="urn:schemas-upnp-org:control-1-0">'
                     f"<errorCode>{f.code}</errorCode><errorDescription>{f.text}</errorDescription>"
                     "</UPnPError></detail></s:Fault></s:Body></s:Envelope>")
            return web.Response(status=500, text=fault, content_type="text/xml")
        if svc == "avt" and self._avt_state() != before:
            asyncio.ensure_future(self._changed())
        payload = "".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in out.items())
        xml = (f'<?xml version="1.0"?><s:Envelope xmlns:s="{SOAP_NS}" '
               's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
               f'<u:{action}Response xmlns:u="{st}">{payload}</u:{action}Response>'
               "</s:Body></s:Envelope>")
        return web.Response(text=xml, content_type="text/xml", charset="utf-8")

    async def _subscribe(self, request):
        svc = request.match_info["svc"]
        sid = request.headers.get("SID")
        if sid:
            if sid not in self._subs:
                return web.Response(status=412)
        else:
            callback = request.headers.get("CALLBACK", "").strip("<>")
            sid = f"uuid:{uuid.uuid4()}"
            self._subs[sid] = (svc, callback)
            self._seq[sid] = 0
            self.calls.append(("SUBSCRIBE", {"SID": sid, "service": svc}))
            asyncio.ensure_future(self._initial(sid, svc))
        return web.Response(headers={"SID": sid, "TIMEOUT": "Second-1800"})

    async def _unsubscribe(self, request):
        self.calls.append(("UNSUBSCRIBE", {"SID": request.headers.get("SID")}))
        self._subs.pop(request.headers.get("SID"), None)
        return web.Response()

    async def _initial(self, sid: str, svc: str):
        await asyncio.sleep(0.05)       # after the SUBSCRIBE answer has landed
        if svc == "avt":
            values = self._last_change("AVT", self._avt_state())
        elif svc == "rc":
            values = self._last_change("RCS", {"Volume": self.volume, "Mute": 0})
        else:
            values = {"SinkProtocolInfo": ",".join(self.sink), "SourceProtocolInfo": "",
                      "CurrentConnectionIDs": "0"}
        await self._notify(sid, values)

    async def _notify_all(self, svc: str, values: dict):
        for sid, (key, _cb) in list(self._subs.items()):
            if key == svc:
                await self._notify(sid, values)

    async def _notify(self, sid: str, values: dict):
        _svc, callback = self._subs.get(sid, (None, None))
        if not callback or not self.notifying:
            return
        props = "".join(f"<e:property><{k}>{escape(str(v))}</{k}></e:property>"
                        for k, v in values.items())
        body = ('<?xml version="1.0"?><e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">'
                f"{props}</e:propertyset>")
        seq = self._seq[sid]
        self._seq[sid] = seq + 1
        try:
            async with self._session.request(
                    "NOTIFY", callback, data=body.encode(),
                    headers={"CONTENT-TYPE": 'text/xml; charset="utf-8"', "NT": "upnp:event",
                             "NTS": "upnp:propchange", "SID": sid, "SEQ": str(seq)}) as resp:
                await resp.read()
        except Exception:
            pass            # a subscriber gone is the subscriber's business, as on a device

    # -- for the tests ---------------------------------------------------------------

    def taken_by_another(self, how: str) -> None:
        """Another controller takes the device past UPnP — Roon over RAAT on
        a player that is a Roon endpoint too — and its AVTransport says so in
        one of two ways, guessed until a live run records it: "stopped", the
        transport stopped on our URI, its position frozen there; "playing",
        playing a URI of the other controller's, its position running from
        0. Our NextURI is gone either way. Evented as any change."""
        if how == "stopped":
            self._base, self._t0, self.state = self.position(), None, "STOPPED"
        else:
            self.uri, self.meta = "raat://another-controller/stream", ""
            self._base, self._t0, self.state = 0.0, time.monotonic(), "PLAYING"
        self.next_uri = self.next_meta = ""
        self._run(self._changed())

    def called(self, action: str) -> list[dict]:
        return [args for name, args in self.calls if name == action]


# -- the node's side ------------------------------------------------------------------

def wait_for(cond: Callable[[], bool], timeout: float = 10.0, what: str = "") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what or cond}")


@pytest.fixture(scope="module", autouse=True)
def _gena_port():
    """The node's GENA callback server is a process singleton on a fixed port
    (dlna_backend._ensure_notify): one free port for the module."""
    before = settings.dlna_gena_port
    settings.dlna_gena_port = _free_port()
    yield
    settings.dlna_gena_port = before


@pytest.fixture
def proxy(monkeypatch, tmp_path):
    """The node's media proxy on loopback, its transcode cache in tmp_path."""
    p = MediaProxy(port=_free_port(), advertised_host="127.0.0.1",
                   file_token_key=b"test-key", bind_host="127.0.0.1")
    p.start()
    monkeypatch.setattr(streaming_service, "_proxy", p)
    monkeypatch.setattr(transcode, "_CACHE_DIR", tmp_path / "transcode_cache")
    yield p
    p._httpd.shutdown()
    p._httpd.server_close()


@pytest.fixture
def renderers():
    started = []

    def make(**kw) -> FakeRenderer:
        r = FakeRenderer(**kw).start()
        started.append(r)
        return r
    yield make
    for r in started:
        r.close()


@pytest.fixture
def quality(monkeypatch):
    """output.stream_quality as the backend reads it (no database here)."""
    value = {"q": "lossless"}
    monkeypatch.setattr(DlnaBackend, "_quality", staticmethod(lambda: value["q"]))
    return value


@pytest.fixture
def stored(monkeypatch):
    """The node's settings in memory, for a backend with no manager around
    it: what it remembers of a renderer."""
    import routers.settings as settings_router
    store = {}
    monkeypatch.setattr(settings_router, "_read", lambda key: store.get(key))
    monkeypatch.setattr(settings_router, "_write", lambda key, value: store.__setitem__(key, value))
    return store


class Node:
    """A DlnaBackend on a queue, its statuses kept."""

    def __init__(self, queue: CanonicalQueue, renderer: FakeRenderer):
        self.queue = queue
        self.statuses = []
        self.backend = DlnaBackend(emit=lambda b, s: self.statuses.append(s),
                                   queue=queue, renderer=renderer.record)

    @property
    def last(self):
        return self.statuses[-1]


@pytest.fixture
def nodes():
    made = []

    def make(queue: CanonicalQueue, renderer: FakeRenderer) -> Node:
        n = Node(queue, renderer)
        n.backend.start()
        made.append(n)
        return n
    yield make
    for n in made:
        n.backend.shutdown()


def track(path: Path, n: int, *, fmt: str = "FLAC", seconds: float = 4.0,
          data: bytes = b"\x00" * 4096) -> QueueItem:
    """An owned file of `seconds` — its bytes are whatever the test needs the
    proxy to serve; this renderer decodes nothing."""
    f = path / f"{n:02d}.{fmt.lower()}"
    if not f.exists():
        f.write_bytes(data)
    return QueueItem(track_id=f"00000000-0000-0000-0000-{n:012d}", media_file_id=n,
                     source={"kind": "file", "path": str(f), "format": fmt},
                     title=f"Track {n}", artist="Artist", album="Album",
                     duration_seconds=seconds)


def streamed(proxy: MediaProxy, n: int, *, cover_url: str, seconds: float = 4.0) -> QueueItem:
    """A phantom track whose stream the proxy holds already (fetched)."""
    from types import SimpleNamespace
    from streaming.base import FetchedAudio, TrackQuery
    from streaming.proxy import _Entry
    track_id = f"10000000-0000-0000-0000-{n:012d}"
    tok = f"stream{n}"
    e = _Entry(tok, TrackQuery(artist="Artist", title=f"Stream {n}", album="Album",
                               duration=seconds, track_id=track_id, cover_url=cover_url), n)
    e.audio = FetchedAudio(data=b"fLaC" + b"\x00" * 4092, mime="audio/flac", lossless=True)
    e.provider = SimpleNamespace(manifest=SimpleNamespace(id="test", demo_limited=False))
    e._claimed = True
    e.ready.set()
    proxy._entries[tok] = e
    return QueueItem(track_id=track_id, media_file_id=None, source={"kind": "proxy", "token": tok},
                     title=f"Stream {n}", artist="Artist", album="Album", duration_seconds=seconds,
                     cover_url=cover_url, preview=True, provider="test")


def _hosts(xml: str) -> set:
    """The hosts of the URLs a DIDL hands the renderer — its namespace names
    (purl.org, the Samsung one the library adds) are no fetches."""
    return set(re.findall(r"https?://([^/:\"<&]+)", re.sub(r'xmlns(:\w+)?="[^"]*"', "", xml)))


def test_a_cover_the_node_could_not_fetch_is_no_cover(tmp_path, proxy, renderers, nodes,
                                                      quality, monkeypatch):
    """N-3: a phantom's cover reaches the renderer only through the node's own
    /art/ route. One the node could not fetch is left out of the DIDL — its
    provider's URL would send the renderer to the internet."""
    fetchable = "https://covers.example.invalid/a.jpg"
    unfetchable = "https://covers.example.invalid/b.jpg"
    jpeg = DlnaBackend._to_jpeg(_tiny_png(), "test")

    async def fetch(url):
        return jpeg if url == fetchable else None

    monkeypatch.setattr(DlnaBackend, "_fetch_cover_jpeg", staticmethod(fetch))
    r = renderers(sink=AK_CONNECT_SINK)
    q = CanonicalQueue()
    q.replace([streamed(proxy, 1, cover_url=unfetchable), streamed(proxy, 2, cover_url=fetchable)])
    n = nodes(q, r)
    assert n.backend.select(1)
    wait_for(lambda: r.called("SetNextAVTransportURI"), what="the next track armed")
    first = r.called("SetAVTransportURI")[0]["CurrentURIMetaData"]
    second = r.called("SetNextAVTransportURI")[-1]["NextURIMetaData"]
    assert "albumArtURI" not in first
    assert _hosts(first) == {"127.0.0.1"}
    assert re.search(r"<upnp:albumArtURI>http://127\.0\.0\.1:\d+/art/", second)
    assert _hosts(second) == {"127.0.0.1"}


def test_a_renderers_cover_is_fetched_by_the_rule_of_every_outside_cover(monkeypatch):
    """The renderer's /art/ asks only what the Web UI's covers ask
    (covers.ART_SOURCES): a URL of a source's shape is fetched and made JPEG;
    any other — a cover_url a share file carried, an address the node was
    never handed — is no cover, and nothing is fetched for it."""
    import httpx
    deezer = ("https://cdn-images.dzcdn.net/images/cover/5718f7c81c27e0b2417e2a4c45224f8a/"
              "1000x1000-000000-80-0-0.jpg")
    asked = []

    def handle(request):
        asked.append(str(request.url))
        return httpx.Response(200, headers={"content-type": "image/png"}, content=_tiny_png())

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handle), **kw))
    assert asyncio.run(DlnaBackend._fetch_cover_jpeg(deezer))[:2] == b"\xff\xd8"
    for url in ("http://192.0.2.1/cover.jpg", "https://covers.example.invalid/a.jpg"):
        assert asyncio.run(DlnaBackend._fetch_cover_jpeg(url)) is None
    assert asked == [deezer]


def _tiny_png() -> bytes:
    import io
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (2, 2), (200, 100, 50)).save(out, format="PNG")
    return out.getvalue()


def dsf_bytes(seconds: float = 0.5) -> bytes:
    """A DSD64 stereo DSF file of digital silence (the 0x69 idle pattern)."""
    import struct
    rate, block = 2822400, 4096
    samples = int(rate * seconds)
    blocks = -(-samples // 8 // block)
    data = b"\x69" * (block * 2 * blocks)
    fmt = struct.pack("<4sQIIIIIIQII", b"fmt ", 52, 1, 0, 2, 2, rate, 1, samples, block, 0)
    chunk = struct.pack("<4sQ", b"data", 12 + len(data)) + data
    return struct.pack("<4sQQQ", b"DSD ", 28, 28 + len(fmt) + len(chunk), 0) + fmt + chunk


def wavpack_bytes(tmp_path: Path, seconds: float = 0.5) -> bytes:
    import subprocess
    out = tmp_path / "source.wv"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency=1000:sample_rate=44100:duration={seconds}",
                    "-ac", "2", "-c:a", "wavpack", str(out)], check=True)
    return out.read_bytes()


def probe(url: str) -> dict:
    """ffprobe's word on what the proxy serves at `url`."""
    import json
    import subprocess
    out = subprocess.run(["ffprobe", "-hide_banner", "-v", "error", "-show_entries",
                          "stream=codec_name,sample_rate,bits_per_raw_sample", "-of", "json", url],
                         check=True, capture_output=True, text=True).stdout
    return json.loads(out)["streams"][0]


needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="no ffmpeg here")


def _tier_of(sink: list, fmt: str, quality: str) -> str:
    """The query a renderer with `sink` is sent an owned `fmt` file with."""
    from types import SimpleNamespace
    from streaming.proxy import MIME_BY_FORMAT
    backend = DlnaBackend(emit=lambda b, s: None, queue=CanonicalQueue(),
                          renderer={"udn": "uuid:x", "name": "R", "location": "http://127.0.0.1:9/d"})
    backend._dmr = SimpleNamespace(sink_protocol_info=list(sink))
    return backend._tier(MIME_BY_FORMAT[fmt], quality, recode=True)


def test_the_sink_list_decides_the_stream_format():
    """N-1: a format the renderer lists goes as it is, under whichever of its
    spellings the list uses; one it lacks goes as a whole-file FLAC, failing
    that as Opus; Opus only to a renderer that lists Ogg; a list that says
    nothing keeps today's choice."""
    assert _tier_of(AK_CONNECT_SINK, "DSF", "lossless") == ""     # listed as audio/dsf
    assert _tier_of(AK_CONNECT_SINK, "DFF", "lossless") == ""     # listed as audio/dff
    assert _tier_of(AK_CONNECT_SINK, "WAV", "lossless") == ""     # listed as audio/x-wav
    assert _tier_of(AK_CONNECT_SINK, "WV", "lossless") == "?q=flac"
    assert _tier_of(AK_CONNECT_SINK, "FLAC", "opus_96") == "?q=opus_96"
    for fmt in ("DSF", "DFF", "WV", "APE"):
        assert _tier_of(PHONE_SINK, fmt, "lossless") == "?q=flac", fmt
    assert _tier_of(PHONE_SINK, "MP3", "lossless") == ""
    no_ogg = ["http-get:*:audio/flac:*", "http-get:*:audio/mpeg:*"]
    assert _tier_of(no_ogg, "FLAC", "opus_96") == ""
    assert _tier_of(no_ogg, "DSF", "opus_192") == "?q=flac"
    no_flac = ["http-get:*:audio/mpeg:*", "http-get:*:audio/ogg:*"]
    assert _tier_of(no_flac, "DSF", "lossless") == "?q=opus_192"
    assert _tier_of(["http-get:*:audio/mpeg:*"], "DSF", "lossless") == ""
    for silent in ([], ["http-get:*:*:*"], ["http-get:*:audio/*:*"]):
        assert _tier_of(silent, "DSF", "lossless") == ""
        assert _tier_of(silent, "FLAC", "opus_96") == "?q=opus_96"


@needs_ffmpeg
def test_a_queue_with_formats_a_phone_lacks_plays_through(tmp_path, proxy, renderers, nodes, quality):
    """N-1: the phone renderer decodes no DSD or WavPack and refuses a DIDL
    naming one (714). Each goes to it as a FLAC of the whole file, 24-bit at
    88.2 kHz for DSD, and the queue plays to its end — it used to stop at
    the first DSD file."""
    r = renderers(sink=PHONE_SINK)
    q = CanonicalQueue()
    q.replace([track(tmp_path, 1, seconds=1.5),
               track(tmp_path, 2, fmt="DSF", seconds=1.5, data=dsf_bytes()),
               track(tmp_path, 3, fmt="WV", seconds=1.5, data=wavpack_bytes(tmp_path))])
    n = nodes(q, r)
    assert n.backend.select(1)
    wait_for(lambda: n.last.queue_index == 3 and n.last.state == "playing", timeout=20,
             what="slot 3 playing")
    wait_for(lambda: r.state == "STOPPED", what="the end of the queue")
    handed = [(a["NextURI"], a["NextURIMetaData"]) for a in r.called("SetNextAVTransportURI")
              if a["NextURI"]]
    assert [u.endswith("?q=flac") for u, _m in handed] == [True, True]
    assert all('protocolInfo="http-get:*:audio/flac:' in m for _u, m in handed)
    dsd, wv = (probe(u) for u, _m in handed)
    assert (dsd["codec_name"], dsd["sample_rate"], dsd["bits_per_raw_sample"]) == ("flac", "88200", "24")
    assert (wv["codec_name"], wv["sample_rate"]) == ("flac", "44100")


@needs_ffmpeg
def test_a_format_listed_under_another_spelling_goes_as_it_is(tmp_path, proxy, renderers, nodes,
                                                              quality):
    """N-1: a DAP that lists DSF as audio/dsf plays the file itself, named in
    the DIDL as its list names it; only what it lists under no spelling
    (WavPack) is turned into FLAC."""
    r = renderers(sink=AK_CONNECT_SINK)
    q = CanonicalQueue()
    q.replace([track(tmp_path, 1, fmt="DSF", seconds=1.5, data=dsf_bytes()),
               track(tmp_path, 2, fmt="WV", seconds=1.5, data=wavpack_bytes(tmp_path))])
    n = nodes(q, r)
    assert n.backend.select(1)
    wait_for(lambda: r.called("SetNextAVTransportURI"), what="the next track armed")
    load = r.called("SetAVTransportURI")[0]
    assert "?q=" not in load["CurrentURI"]
    assert 'protocolInfo="http-get:*:audio/dsf:' in load["CurrentURIMetaData"]
    nxt = r.called("SetNextAVTransportURI")[-1]
    assert nxt["NextURI"].endswith("?q=flac")
    assert 'protocolInfo="http-get:*:audio/flac:' in nxt["NextURIMetaData"]


def _opus_seek_node(tmp_path, proxy, renderers, nodes, quality, monkeypatch, seconds, stored):
    """A phone renderer playing one owned track in opus_96: it reports, as
    its TrackDuration, what its decoder measured of the stream it was given.
    Remembered as one that does not seek Opus itself (N-7), so a seek loads
    the stream again from the second (?ss=)."""
    monkeypatch.setattr(transcode, "opus_path_for_file", lambda path, q, ss=0.0: path)
    quality["q"] = "opus_96"
    r = renderers(sink=PHONE_SINK,
                  reports=lambda uri, didl: (didl or 40.0) - FakeRenderer.offset(uri))
    stored["output.dlna_opus_seek"] = {r.udn.lower(): False}
    q = CanonicalQueue()
    q.replace([track(tmp_path, 1, seconds=seconds)])
    n = nodes(q, r)
    assert n.backend.select(1)
    wait_for(lambda: n.last.state == "playing" and n.last.position >= 1, what="playing")
    return r, n


def _seek_to(r: FakeRenderer, n: Node, target: int) -> None:
    assert n.backend.seek(target)
    wait_for(lambda: f"ss={target}" in r.uri and r.state == "PLAYING", what=f"reloaded at {target}")
    wait_for(lambda: n.last.state == "playing" and n.last.position >= target + 1.5,
             what=f"playing on past {target}")


def test_an_opus_seek_keeps_the_track_length(tmp_path, proxy, renderers, nodes, quality, monkeypatch, stored):
    """N-13: an Opus seek reloads the stream from the target, and the phone
    reports that stream's own length (30 s of a 40-s track after ss=10). The
    track keeps the length the node knows, so the next seek lands where it
    is asked — the slider's scale used to shrink to the stream's."""
    r, n = _opus_seek_node(tmp_path, proxy, renderers, nodes, quality, monkeypatch, 40.0, stored)
    assert n.last.length == 40
    _seek_to(r, n, 10)
    assert r.reported_length() == 30
    assert n.last.length == 40
    _seek_to(r, n, 25)
    assert n.last.length == 40 and 26 <= n.last.position <= 29


def test_a_length_only_the_renderer_knows_is_read_from_the_seek(tmp_path, proxy, renderers, nodes,
                                                                quality, monkeypatch, stored):
    """N-13: a track whose length the node does not know takes the renderer's
    word — on an offset stream read 0-based, like RelTime: the offset plus
    the rest the decoder measured."""
    r, n = _opus_seek_node(tmp_path, proxy, renderers, nodes, quality, monkeypatch, None, stored)
    wait_for(lambda: n.last.length == 40, what="the renderer's length")
    _seek_to(r, n, 10)
    assert n.last.length == 40


def test_a_track_the_renderer_starts_over_by_itself_is_said(tmp_path, proxy, renderers, nodes,
                                                           quality, caplog):
    """A renderer that starts the playing track over on its own — no command
    of the node's — is followed, and the log says it moved the slot, from
    where to where, with what it reported."""
    r = renderers()
    q = CanonicalQueue()
    q.replace([track(tmp_path, 1, seconds=40.0)])
    n = nodes(q, r)
    assert n.backend.select(1)
    wait_for(lambda: n.statuses and n.last.state == "playing" and n.last.position >= 6,
             what="playing past 6 s")
    with caplog.at_level("INFO", logger="playback.dlna_backend"):
        r._base, r._t0 = 0.0, time.monotonic()           # the renderer, from 0:00
        wait_for(lambda: n.last.position < 4, what="the node following it")
    assert "moved slot 1 from" in caplog.text and "by itself (playing" in caplog.text


def test_a_start_seek_the_renderer_takes_late_is_no_move_of_its_own(tmp_path, proxy, renderers, nodes,
                                                                   quality, caplog):
    """A start at a second — an output switch's spot — that the renderer
    seeks only seconds after it began playing from the top: what it reads
    meanwhile is where it was before the Seek, not where the slot is. Never
    shown, never said as a move of its own — taken for its word, the listen
    the switch carried restarted there and was scrobbled twice. Its landing,
    seconds late, is seen at once: the node follows the renderer from there,
    never runs ahead of it until a deadline and then blames it."""
    r = renderers()
    r.seek_delay = 4.0
    q = CanonicalQueue()
    q.replace([track(tmp_path, 1, seconds=60.0)])
    n = nodes(q, r)
    n.backend.resume_at(1, 35.0)
    with caplog.at_level("INFO", logger="playback.dlna_backend"):
        assert n.backend.play()
        wait_for(lambda: r.called("Seek"), what="the start sought")
        wait_for(lambda: r.position() >= 36, what="the Seek landed")
        wait_for(lambda: abs(n.last.position - r.position()) <= 1.5, timeout=4,
                 what="the node at the renderer's second once the Seek landed")
    playing = [s.position for s in n.statuses if s.state == "playing"]
    assert playing and min(playing) >= 34, playing
    assert "by itself" not in caplog.text


def test_a_seek_while_paused_the_renderer_takes_late_holds_still(tmp_path, proxy, renderers, nodes,
                                                               quality, caplog):
    """Paused, the person seeks; the renderer acts on it seconds later. What
    it reads meanwhile is where it paused: the slot stays at the Seek's
    second — a paused position never runs — and the landing is no move of
    the renderer's own."""
    r = renderers()
    q = CanonicalQueue()
    q.replace([track(tmp_path, 1, seconds=90.0)])
    n = nodes(q, r)
    assert n.backend.select(1)
    wait_for(lambda: n.statuses and n.last.state == "playing" and n.last.position >= 2, timeout=20,
             what="playing past 2 s")
    assert n.backend.pause()
    wait_for(lambda: r.state == "PAUSED_PLAYBACK", what="paused")
    r.seek_delay = 4.0
    with caplog.at_level("INFO", logger="playback.dlna_backend"):
        mark = len(n.statuses)
        assert n.backend.seek(30)
        wait_for(lambda: r.position() >= 30, what="the Seek landed")
        wait_for(lambda: n.backend._seek_due is None, timeout=4, what="the landing seen")
    paused = [s.position for s in n.statuses[mark:] if s.state == "paused"]
    assert paused and max(paused) <= 31, paused
    assert "by itself" not in caplog.text


def test_a_renderer_that_says_stopped_while_it_plays_runs_on_from_a_seek_of_the_nodes(
        tmp_path, proxy, renderers, nodes, quality):
    """A renderer whose transport says STOPPED through a whole playing track
    is read by its position probe. Started at a spot, its readings before
    the Seek lands are the clock's, and the landing is its word again: the
    slot runs on, never frozen at the spot's second for the rest of the
    track — the listen credited up to there, a whole play written as a
    skip."""
    r = renderers()
    r.notifying = False
    r.says_stopped = True
    q = CanonicalQueue()
    q.replace([track(tmp_path, 1, seconds=60.0)])
    n = nodes(q, r)
    n.backend.resume_at(1, 35.0)
    assert n.backend.play()
    wait_for(lambda: r.called("Seek"), timeout=15, what="the start sought")
    wait_for(lambda: r.position() >= 41.5, timeout=15, what="six seconds past the Seek")
    wait_for(lambda: n.last.state == "playing" and abs(n.last.position - r.position()) <= 1.5,
             timeout=4, what="the node at the renderer's second")


@pytest.fixture
def manager(monkeypatch):
    """The real PlaybackManager with the node's settings in memory and no
    database: no listen is recorded, no queue persisted, every slot opened
    as it was queued (substitute.native_plays)."""
    import routers.settings as settings_router
    from playback import substitute, tracker
    from playback.manager import PlaybackManager
    store = {}
    monkeypatch.setattr(settings_router, "_read", lambda key: store.get(key))
    monkeypatch.setattr(settings_router, "_write", lambda key, value: store.__setitem__(key, value))
    monkeypatch.setattr(tracker, "track_play_event", lambda *a, **k: None)
    monkeypatch.setattr(substitute, "native_plays", lambda items, output, endpoint: [None] * len(items))
    m = PlaybackManager()
    monkeypatch.setattr(m, "_schedule_persist", lambda: None)
    m.store = store
    yield m
    if m.active is not None:
        m.activate(None, stop_old=False)


def _on_renderer(manager, r: FakeRenderer, items: list) -> None:
    manager.store.update({"output.type": "dlna", "output.dlna_renderer": r.record})
    manager.activate("dlna", renderer=r.record)
    added, _gen = manager.replace_queue(items, play=True)
    assert added == len(items)


def _lost_mid_track(r: FakeRenderer, manager) -> float:
    """The renderer stops answering two seconds into slot 1 (20 s long, room
    for three missed polls on a loaded machine) and plays on — across into
    slot 2 by its armed NextURI, its events still sent. Returns the second
    the node was left at."""
    wait_for(lambda: r.called("SetAVTransportURI"), what="slot 1 loaded")
    first = r.called("SetAVTransportURI")[0]["CurrentURI"]
    wait_for(lambda: manager.latest_status.get("state") == "playing" and r.next_uri
             and r.uri == first and r.position() >= 2, what="slot 1 playing, slot 2 armed")
    r.stop_answering()
    wait_for(lambda: manager.latest_status.get("resume"), timeout=15, what="the renderer lost")
    assert manager.latest_status["resume"]["index"] == 1 and r.uri == first, "lost on slot 1"
    lost = manager.latest_status["resume"]["position"]
    wait_for(lambda: r.position() < 3 and r.uri == r.called("SetNextAVTransportURI")[0]["NextURI"],
             timeout=25, what="the renderer playing slot 2 on its own")
    time.sleep(1.5)     # its events for slot 2 reach the node
    return lost


def test_a_renderer_that_stops_answering_leaves_the_queue_and_the_spot(tmp_path, proxy, renderers,
                                                                       manager, quality):
    """N-17: a phone that lost its listener played on through its armed
    NextURI and kept sending events. The node stays where it lost it — the
    slot, the second, the queue as it was — and the next play intent
    attaches the renderer again and resumes there: the track is sought to
    the second it was left at once it plays. The play press used to start
    the slot from 0:00, after the phone had already played the next."""
    r = renderers(sink=PHONE_SINK)
    items = [track(tmp_path, 1, seconds=20.0), track(tmp_path, 2, seconds=30.0),
             track(tmp_path, 3, seconds=30.0)]
    _on_renderer(manager, r, items)
    lost = _lost_mid_track(r, manager)
    st = manager.latest_status
    assert (st["state"], st["track_index"], st["position"]) == ("stopped", 1, lost)
    assert 2 <= lost <= 19 and "stopped responding" in st["error"]
    assert manager.queue.snapshot() == items
    r.answer_again()
    seeks = len(r.called("Seek"))
    assert manager.ensure_active().play()
    wait_for(lambda: len(r.called("Seek")) > seeks, what="the start sought")
    assert r.called("Seek")[-1]["Target"] == f"0:00:{int(lost):02d}"
    assert r.uri == r.called("SetAVTransportURI")[-1]["CurrentURI"]
    assert r.uri.split("?")[0] == r.called("SetAVTransportURI")[0]["CurrentURI"].split("?")[0]
    wait_for(lambda: manager.latest_status.get("state") == "playing", what="playing again")
    st = manager.latest_status
    assert st["track_index"] == 1 and st["position"] >= int(lost)
    assert manager.queue.snapshot() == items


def test_an_opus_stream_resumes_where_the_renderer_was_lost(tmp_path, proxy, renderers, manager,
                                                            quality, monkeypatch):
    """N-17: an Opus stream starts at the second it was left at by its own
    reload from there (?ss=)."""
    monkeypatch.setattr(transcode, "opus_path_for_file", lambda path, q, ss=0.0: path)
    quality["q"] = "opus_96"
    r = renderers(sink=PHONE_SINK)
    items = [track(tmp_path, 1, seconds=20.0), track(tmp_path, 2, seconds=30.0)]
    _on_renderer(manager, r, items)
    lost = _lost_mid_track(r, manager)
    r.answer_again()
    assert manager.ensure_active().play()
    wait_for(lambda: r.state == "PLAYING" and f"ss={int(lost)}" in r.uri, what="reloaded at the spot")
    assert "q=opus_96" in r.uri and not r.called("Seek")
    wait_for(lambda: manager.latest_status.get("state") == "playing", what="playing again")
    assert manager.latest_status["position"] >= int(lost)


@pytest.fixture
def picker(manager, monkeypatch):
    """The Output picker's routes on the in-memory settings: no local
    devices, no HQPlayer rows, and a network scan that finds what the test
    says it finds (`found`: description URLs)."""
    import async_upnp_client.search
    import routers.player as player_router
    import tls_gen
    from playback.local import devices
    found: list[str] = []

    async def search(callback, **kw):
        # as async_upnp_client hands an answer over: `_host` is where it came from
        for loc in found:
            await callback({"location": loc, "usn": "uuid:x::urn:schemas-upnp-org:device:MediaRenderer:1",
                            "_host": urlparse(loc).hostname})

    async def sweep(found_dlna, found_hqp):
        return None
    monkeypatch.setattr(async_upnp_client.search, "async_search", search)
    monkeypatch.setattr(player_router, "_unicast_sweep", sweep)
    monkeypatch.setattr(tls_gen, "detect_private_host_ips", lambda: [])
    monkeypatch.setattr(player_router, "_hqplayer_entries", lambda: [])
    monkeypatch.setattr(devices, "list_devices", lambda: [])
    monkeypatch.setattr(player_router, "_dlna_discovered", {})
    monkeypatch.setattr(player_router, "manager", manager)
    player_router.found = found
    return player_router


def _rows(picker) -> list:
    return next(o for o in picker.get_outputs()["outputs"] if o["type"] == "dlna")["renderers"]


def test_one_renderer_at_two_addresses_is_one_row(renderers, manager, picker):
    """N-16: a phone pinned at its LAN address and found by a scan through a
    tunnel is one row — where the scan reached it, the pinned address kept
    behind it — not two, whichever case its UDN was written in."""
    lan, tunnel = renderers(name="Phone"), renderers(name="Phone")
    tunnel.udn = lan.udn
    pinned = {**lan.record, "udn": lan.udn.upper()}
    manager.store.update({"output.dlna_pinned": [pinned], "output.type": "dlna",
                          "output.dlna_renderer": pinned})
    picker.found[:] = [tunnel.location]
    asyncio.run(picker._scan_run())
    rows = _rows(picker)
    assert len(rows) == 1
    row = rows[0]
    assert (row["udn"], row["location"], row["pinned"]) == (lan.udn, tunnel.location, True)
    assert row["locations"][:2] == [tunnel.location, lan.location]
    assert picker.get_outputs()["active"]["renderer_udn"] == row["udn"]


def test_a_scan_teaches_the_remembered_renderer_where_it_answers(renderers, manager, picker):
    """N-16: every address a scan reaches a remembered renderer at joins its
    pin and the chosen output's record, newest first; the old one stays a
    fallback."""
    lan, tunnel = renderers(), renderers()
    tunnel.udn = lan.udn
    manager.store.update({"output.dlna_pinned": [lan.record], "output.type": "dlna",
                          "output.dlna_renderer": tunnel.record})
    picker.found[:] = [lan.location]
    asyncio.run(picker._scan_run())
    pin = manager.store["output.dlna_pinned"][0]
    chosen = manager.store["output.dlna_renderer"]
    assert pin["location"] == chosen["location"] == lan.location
    assert chosen["locations"] == [lan.location, tunnel.location]


def test_a_held_renderer_takes_no_new_address_while_it_answers_where_it_is_known(
        renderers, manager, picker):
    """A device that claims the chosen renderer's UDN at another address
    takes nothing of its records while the renderer answers where it is
    known; once the renderer answers there no more, the scan finds it where
    it answers now."""
    # the claimant's address sorts first: the order a scan took addresses in
    claimant, real = sorted((renderers(name="Shelf"), renderers(name="Shelf")), key=lambda r: r.location)
    claimant.udn = real.udn
    manager.store.update({"output.dlna_pinned": [real.record], "output.type": "dlna",
                          "output.dlna_renderer": real.record})
    picker.found[:] = [claimant.location, real.location]
    asyncio.run(picker._scan_run())
    records = (manager.store["output.dlna_pinned"][0], manager.store["output.dlna_renderer"],
               _rows(picker)[0])
    for record in records:
        assert record["location"] == real.location
        assert claimant.location not in (record.get("locations") or [])
    real.stop_answering()
    asyncio.run(picker._scan_run())
    assert manager.store["output.dlna_renderer"]["location"] == claimant.location


def test_play_on_the_remembered_renderer_goes_where_it_answers_now(tmp_path, proxy, renderers,
                                                                   manager, picker, quality):
    """N-16: the output was chosen at one address and the renderer moved (a
    phone back on the LAN from a tunnel). A scan found it at the other; the
    play press attaches it there and plays — it used to answer "output
    unavailable", or go to the old address and fail."""
    old, new = renderers(), renderers()
    new.udn = old.udn
    items = [track(tmp_path, 1, seconds=20.0)]
    _on_renderer(manager, old, items)
    wait_for(lambda: manager.latest_status.get("state") == "playing", what="playing at the old address")
    manager.backend().pause()
    wait_for(lambda: old.state == "PAUSED_PLAYBACK", what="paused")
    old.stop_answering()
    picker.found[:] = [new.location]
    asyncio.run(picker._scan_run())
    assert manager.ensure_active().play()
    wait_for(lambda: new.state == "PLAYING", what="playing at the new address")
    assert new.called("SetAVTransportURI") and manager.backend()._renderer["location"] == new.location
    wait_for(lambda: manager.latest_status.get("state") == "playing", what="the node says playing")


def test_play_with_no_renderer_attached_goes_where_it_answers_now(tmp_path, proxy, renderers,
                                                                  manager, picker, quality):
    """N-16: nothing attached (a node started while the phone was away), the
    chosen record naming the old address only: once a scan has found the
    renderer elsewhere, the play press attaches it there."""
    old, new = renderers(), renderers()
    new.udn = old.udn
    old.stop_answering()
    manager.store.update({"output.type": "dlna", "output.dlna_renderer": old.record})
    manager.queue.replace([track(tmp_path, 1, seconds=20.0)])
    picker.found[:] = [new.location]
    asyncio.run(picker._scan_run())
    assert manager.ensure_active().play()
    wait_for(lambda: new.state == "PLAYING", what="playing at the new address")


def test_play_where_no_known_address_answers_scans_and_plays_where_the_renderer_is(
        tmp_path, proxy, renderers, manager, picker, quality, monkeypatch):
    """The chosen renderer answers at none of the addresses it is known at
    (a phone gone from the home Wi-Fi to a tunnel) and no scan has run: the
    play press scans, takes the renderer at the address the scan finds it
    at, and plays the track there, loaded once. The probe, the scan and the
    attach are one "connecting"."""
    old, new = renderers(name="Phone"), renderers(name="Phone")
    new.udn = old.udn
    _on_renderer(manager, old, [track(tmp_path, 1, seconds=20.0)])
    wait_for(lambda: manager.latest_status.get("state") == "playing", what="playing at the old address")
    manager.backend().pause()
    wait_for(lambda: old.state == "PAUSED_PLAYBACK", what="paused")
    old.stop_answering()
    picker.found[:] = [new.location]
    sweeps = _sweeps(picker, monkeypatch)
    seen = _statuses(manager)
    assert picker.play() == {"ok": True}
    assert len(sweeps) == 1
    wait_for(lambda: new.state == "PLAYING", what="playing at the address the scan found")
    assert len(new.called("SetAVTransportURI")) == 1
    assert manager.store["output.dlna_renderer"]["location"] == new.location
    words = [data.get("connecting") for data in seen]
    first, last = words.index("Phone"), len(words) - 1 - words[::-1].index("Phone")
    assert all(word == "Phone" for word in words[first:last + 1]), words


def test_play_with_nothing_attached_scans_when_no_known_address_answers(
        tmp_path, proxy, renderers, manager, picker, quality):
    """Nothing attached (a node started while the phone was away) and the
    chosen record naming an address that does not answer: the play press
    scans and plays where the renderer is found."""
    old, new = renderers(), renderers()
    new.udn = old.udn
    old.stop_answering()
    manager.store.update({"output.type": "dlna", "output.dlna_renderer": old.record})
    manager.queue.replace([track(tmp_path, 1, seconds=20.0)])
    picker.found[:] = [new.location]
    assert manager.ensure_active().play()
    wait_for(lambda: new.state == "PLAYING", what="playing at the address the scan found")
    assert len(new.called("SetAVTransportURI")) == 1


def test_a_press_that_replaces_the_queue_scans_with_no_queue_lock_held(
        tmp_path, proxy, renderers, manager, picker, quality, monkeypatch):
    """A play route that replaces the queue, its renderer held at an address
    that went dark: the scan runs before the queue lock is taken — a queue
    edit goes on meanwhile — and the track plays where the scan found the
    renderer, loaded once."""
    old, new = renderers(name="Phone"), renderers(name="Phone")
    new.udn = old.udn
    _on_renderer(manager, old, [track(tmp_path, 1, seconds=20.0)])
    wait_for(lambda: manager.latest_status.get("state") == "playing", what="playing at the old address")
    manager.backend().pause()
    wait_for(lambda: old.state == "PAUSED_PLAYBACK", what="paused")
    old.stop_answering()
    picker.found[:] = [new.location]
    sweeping, free = threading.Event(), []

    def lock_is_free() -> bool:
        if not manager._mutate_lock.acquire(timeout=2):
            return False
        manager._mutate_lock.release()
        return True

    async def sweep(found_dlna, found_hqp):
        sweeping.set()
        free.append(await asyncio.to_thread(lock_is_free))
        if not free[-1]:
            # the scan ends here, so a press that holds the lock answers and lets it go
            raise RuntimeError("the queue lock is held while the press scans")
    monkeypatch.setattr(picker, "_unicast_sweep", sweep)
    answer = []

    def pressed():
        try:
            answer.append(manager.replace_queue([track(tmp_path, 2, seconds=20.0)], play=True))
        except ConnectionError as e:
            answer.append(e)
    press = threading.Thread(target=pressed, daemon=True)
    press.start()
    assert sweeping.wait(15), "the press scans"
    press.join(30)
    assert not press.is_alive() and free == [True]
    assert answer and answer[0][0] == 1
    wait_for(lambda: new.state == "PLAYING", what="playing at the address the scan found")
    assert len(new.called("SetAVTransportURI")) == 1


def test_one_scan_at_a_time_whoever_asks(picker, monkeypatch):
    """A press waiting on a scan the picker started waits for that one: the
    route and a press share the scan under way."""
    sweeping, go, sweeps = threading.Event(), threading.Event(), []

    async def sweep(found_dlna, found_hqp):
        sweeps.append(1)
        sweeping.set()
        await asyncio.to_thread(go.wait, 20)
    monkeypatch.setattr(picker, "_unicast_sweep", sweep)
    monkeypatch.setattr(picker, "_scan_running", None)
    answers = []

    async def route():
        return await picker.scan_outputs()
    first = threading.Thread(target=lambda: answers.append(asyncio.run(route())))
    first.start()
    assert sweeping.wait(15)
    second = threading.Thread(target=lambda: answers.append(picker.scan_once()))
    second.start()
    go.set()
    first.join(20)
    second.join(20)
    assert len(answers) == 2 and answers[0] is answers[1] and sweeps == [1]


@pytest.mark.parametrize("shape", ["large", "packed", "moved"])
def test_an_attach_takes_no_service_document_past_the_bound(renderers, shape):
    """The attach reads its renderer's every answer within the node's bound:
    a service document past it, compressed or redirected fails the attach,
    which says the renderer answered and what was not taken."""
    r = renderers(name="Shelf")
    r.scpd_shape = shape
    backend = DlnaBackend(emit=lambda b, s: None, queue=CanonicalQueue(), renderer=r.record)
    with pytest.raises(dlna_backend.DlnaAttachError, match="answered at 127.0.0.1, but") as refused:
        backend.start()
    # the failure described in its own words, not a raw exception's
    words = {"large": "an answer past its size", "packed": "a compressed answer", "moved": "Did not receive HTTP 200 but 302"}
    if shape in words:
        assert f"was not taken ({words[shape]})" in str(refused.value), str(refused.value)
    backend.shutdown()


def _sweeps(picker, monkeypatch) -> list:
    """The scans run from now on, one entry each (the sweep every scan makes)."""
    sweeps = []

    async def sweep(found_dlna, found_hqp):
        sweeps.append(1)
    monkeypatch.setattr(picker, "_unicast_sweep", sweep)
    return sweeps


def test_play_where_the_scan_finds_the_renderer_nowhere_says_it_is_offline(
        tmp_path, renderers, manager, picker, monkeypatch):
    """A scan that does not find the renderer either leaves the press with
    the word it had: offline."""
    gone = renderers(name="Phone")
    gone.stop_answering()
    manager.store.update({"output.type": "dlna", "output.dlna_renderer": gone.record})
    manager.queue.replace([track(tmp_path, 1, seconds=20.0)])
    picker.found[:] = []
    sweeps = _sweeps(picker, monkeypatch)
    with pytest.raises(ConnectionError, match="'Phone' is offline"):
        manager.ensure_active()
    assert len(sweeps) == 1 and manager.active is None


def test_the_spot_is_read_from_the_outputs_latest_word(manager):
    """N-14: where an output stands — the slot, the second, whether it plays
    — is its latest status: a track it plays, starts or holds paused, or a
    stopped one's resume spot."""
    from playback.base import Spot
    for status, spot in (
            ({"state": "playing", "track_index": 3, "position": 42.5}, Spot(3, 42.5, True)),
            ({"state": "loading", "track_index": 2, "position": 0.0}, Spot(2, 0.0, True)),
            ({"state": "paused", "track_index": 2, "position": 61.0}, Spot(2, 61.0, False)),
            ({"state": "stopped", "track_index": 2, "position": 0.0,
              "resume": {"index": 2, "position": 150}}, Spot(2, 150.0, False)),
            ({"state": "stopped", "track_index": 2, "position": 0.0}, None),
            ({"state": "disconnected"}, None)):
        manager._latest_status = status
        assert manager.spot() == spot, status


@pytest.fixture
def switch(manager, picker, monkeypatch):
    """The Output picker's switch (PUT /api/settings/output) on the
    in-memory settings."""
    import routers.settings as settings_router
    from playback import manager as manager_mod
    monkeypatch.setattr(manager_mod, "manager", manager)
    monkeypatch.setattr(settings_router, "notify_library_subscribers", lambda: None)
    monkeypatch.setattr(settings_router, "get_output_prefs", lambda: {})

    def to(**prefs) -> dict:
        return settings_router.put_output_prefs(settings_router.OutputPrefs(**prefs))
    return to


def _playing_at(manager, r: FakeRenderer, second: float) -> None:
    wait_for(lambda: manager.latest_status.get("state") == "playing"
             and manager.latest_status.get("position", 0) >= second, what=f"playing past {second}")


def test_a_switch_between_renderers_plays_on_at_the_second(tmp_path, proxy, renderers, manager,
                                                           switch, quality):
    """N-14: the music moves to another room: the new renderer starts the
    track at the second the old one was at, playing — the switch used to
    restart it from 0:00, stopped."""
    a, b = renderers(name="Kitchen"), renderers(name="Study")
    items = [track(tmp_path, 1, seconds=40.0), track(tmp_path, 2, seconds=40.0)]
    _on_renderer(manager, a, items)
    _playing_at(manager, a, 3)
    out = switch(type="dlna", renderer=b.record)
    assert "resume_refused" not in out
    wait_for(lambda: b.called("Seek"), what="the new renderer sought")
    assert a.state == "STOPPED"
    second = _seconds(b.called("Seek")[-1]["Target"])
    assert 3 <= second <= 6
    assert b.called("SetAVTransportURI")[0]["CurrentURI"].split("/file/")[1] == \
        a.called("SetAVTransportURI")[0]["CurrentURI"].split("/file/")[1]
    wait_for(lambda: manager.latest_status.get("state") == "playing", what="playing on")
    assert manager.latest_status["track_index"] == 1 and manager.latest_status["position"] >= second


def test_a_switch_from_a_paused_renderer_stays_paused_there(tmp_path, proxy, renderers, manager,
                                                            switch, quality):
    """N-14: paused on one renderer, paused on the next at the same second —
    nothing loaded there until Play, which starts the track at it."""
    a, b = renderers(name="Kitchen"), renderers(name="Study")
    _on_renderer(manager, a, [track(tmp_path, 1, seconds=40.0)])
    _playing_at(manager, a, 3)
    manager.backend().pause()
    wait_for(lambda: manager.latest_status.get("state") == "paused", what="paused")
    paused_at = manager.latest_status["position"]
    switch(type="dlna", renderer=b.record)
    st = manager.latest_status
    assert (st["state"], st["track_index"], st["position"]) == ("paused", 1, paused_at)
    assert not b.called("SetAVTransportURI") and not b.called("Play")
    import routers.player as player_router
    assert player_router.play()["ok"]
    wait_for(lambda: b.called("Seek"), what="sought on Play")
    assert b.called("Seek")[-1]["Target"] == f"0:00:{int(paused_at):02d}"


@pytest.fixture
def heard(monkeypatch):
    """The play tracker as the manager drives it, its writes and Last.fm
    calls recorded instead of made: [(track, percent) per listen written],
    [(Last.fm call, track)]."""
    listened, lastfm = [], []
    monkeypatch.setattr(tracker, "track_play_event", _TRACK_PLAY_EVENT)
    monkeypatch.setattr(tracker, "_play_session", None)
    monkeypatch.setattr(tracker, "_carried", False)
    monkeypatch.setattr(tracker, "_db_execute", lambda sql, p=None: listened.append(
        (p["tid"], round(p["pct"]))) if "INSERT INTO listening_history" in sql else None)
    # The report goes: the Scrobbling switch, read inside _scrobble_async, is on.
    monkeypatch.setattr(tracker, "_scrobble_async",
                        lambda method, **kw: lastfm.append((method, kw.get("title"))) or True)
    return listened, lastfm


def test_a_switch_carries_the_listen_to_the_next_renderer(tmp_path, proxy, renderers, manager,
                                                          switch, quality, heard):
    """N-14: the track plays on on the new output at its second — the same
    listen. Nothing is written at the switch and the track is not announced
    again (live, 2026-10-31: every switch wrote a skip of the track playing and
    opened its listen afresh)."""
    listened, lastfm = heard
    a, b = renderers(name="Kitchen"), renderers(name="Study")
    _on_renderer(manager, a, [track(tmp_path, 1, seconds=40.0), track(tmp_path, 2, seconds=40.0)])
    _playing_at(manager, a, 3)
    switch(type="dlna", renderer=b.record)
    wait_for(lambda: b.called("Seek"), what="the new renderer sought")
    second = _seconds(b.called("Seek")[-1]["Target"])
    _playing_at(manager, b, second + 2)
    assert listened == []
    assert lastfm == [("update_now_playing", "Track 1")]


def test_a_stop_on_the_renderer_a_switch_went_to_ends_the_listen(tmp_path, proxy, renderers, manager,
                                                                  switch, quality, heard):
    """Paused on one renderer and switched to another, which holds the track
    paused at its second — then stopped there before it played: the listen
    ends at the Stop, as after any pause, not at whatever plays next."""
    listened, lastfm = heard
    a, b = renderers(name="Kitchen"), renderers(name="Study")
    _on_renderer(manager, a, [track(tmp_path, 1, seconds=40.0)])
    _playing_at(manager, a, 3)
    manager.backend().pause()
    wait_for(lambda: manager.latest_status.get("state") == "paused", what="paused")
    switch(type="dlna", renderer=b.record)
    assert listened == []                                # held through the switch
    assert manager.backend().stop()
    wait_for(lambda: listened, what="the listen written at the Stop")
    assert [t for t, _ in listened] == ["00000000-0000-0000-0000-000000000001"]


_STREAM_5 = "10000000-0000-0000-0000-000000000005"


def _at(manager, index: int, second: float) -> None:
    wait_for(lambda: manager.latest_status.get("state") == "playing"
             and manager.latest_status.get("track_index") == index
             and manager.latest_status.get("position", 0) >= second,
             timeout=20, what=f"slot {index} playing past {second}")


def test_a_session_across_hqplayer_a_renderer_and_back_keeps_one_listen_a_track(
        fake, proxy, renderers, manager, switch, quality, heard, tmp_path, monkeypatch):
    """HQPlayer, then a renderer, then HQPlayer again, a streamed track among
    owned ones (live, 2026-10-31): HQPlayer's Next credits the track moved to
    with nothing of the one left; each switch plays the track on at its
    second, the same listen; the renderer walks into the stream, and
    HQPlayer, picked again, resumes the stream at its second; a tap far
    ahead into a stream whose bytes the RAM budget dropped waits for them in
    the node, loading, and plays once they land. One listen a track, each
    announced once, none written at a switch."""
    import types
    from streaming.base import FetchedAudio, ProviderManifest, TrackQuery
    listened, lastfm = heard
    renderer = renderers(name="Kitchen")
    fake.length, fake.takes_seek = 8, True
    _moving_transport(fake, monkeypatch, transit=2)
    gate = threading.Event()
    gate.set()
    provider = types.SimpleNamespace(
        manifest=ProviderManifest(id="test", name="Test", kind="direct_url", lossless=True),
        fetch=lambda query: gate.wait(20) and FetchedAudio(data=b"fLaC" + b"\x00" * 4092,
                                                           mime="audio/flac", lossless=True))
    late = proxy.start_session([(TrackQuery(artist="Artist", title="Stream 5", album="Album",
                                            duration=8.0, track_id=_STREAM_5), [(provider, None)])])[0]
    assert proxy.wait_ready(late, timeout=5).audio is not None
    proxy.track_ready_hooks.append(lambda e: manager.stream_landed(e.token, e.reshaped))
    items = [track(tmp_path, 1, seconds=8.0), track(tmp_path, 2, seconds=8.0),
             streamed(proxy, 3, cover_url="", seconds=8.0), track(tmp_path, 4, seconds=8.0),
             QueueItem(track_id=_STREAM_5, media_file_id=None, source={"kind": "proxy", "token": late},
                       title="Stream 5", artist="Artist", album="Album", duration_seconds=8.0,
                       preview=True, provider="test")]
    hqplayer = {"host": "127.0.0.1", "port": fake.port}
    switch(type="hqplayer", hqplayer=hqplayer)
    assert manager.replace_queue(items, play=True)[0] == len(items)
    _at(manager, 1, 2)
    seen = []
    push = manager._push_status

    def recorded(data):
        seen.append((data.get("track_index"), data.get("position")))
        push(data)
    manager._push_status = recorded
    assert manager.backend().next()
    _at(manager, 2, 2)
    assert [p for i, p in seen if i == 2][0] < 1, seen    # at its start from the first word on
    switch(type="dlna", renderer=renderer.record)
    wait_for(lambda: renderer.called("Seek"), what="the renderer sought")
    _at(manager, 3, 1)                           # the renderer walked into the stream
    assert renderer.uri.split("?")[0].endswith("/preview/stream3")
    n = len(fake.commands)
    switch(type="hqplayer", hqplayer=hqplayer)
    _at(manager, 3, 3)
    assert any(c == "Seek" and float(a["position"]) >= 1 for c, a in fake.commands[n:])
    assert [t for t, _ in listened] == ["00000000-0000-0000-0000-000000000001",
                                        "00000000-0000-0000-0000-000000000002"], listened
    assert [title for call, title in lastfm if call == "update_now_playing"] == \
        ["Track 1", "Track 2", "Stream 3"], lastfm
    # A tap far ahead, into a stream whose bytes the budget dropped
    proxy.enforce_budget(set(), [late], 0)
    gate.clear()
    n = len(fake.commands)
    assert manager.backend().select(5, _STREAM_5)
    wait_for(lambda: manager.latest_status.get("state") == "loading"
             and manager.latest_status.get("track_index") == 5, what="slot 5 loading")
    assert not [c for c, _ in fake.commands[n:] if c in ("SelectTrack", "Play", "Next")]
    gate.set()
    _at(manager, 5, 0)
    assert [t for t, _ in listened][-1] == "10000000-0000-0000-0000-000000000003"
    assert [title for call, title in lastfm if call == "update_now_playing"][-1] == "Stream 5"


class Tab:
    """A browser tab holding the output's command channel."""

    def __init__(self, backend, name: str = "tab-1"):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True, name=name).start()
        self.channel = asyncio.run_coroutine_threadsafe(self._queue(), self.loop).result(5)
        backend.attach_tab(name, self.channel, self.loop)

    @staticmethod
    async def _queue():
        return asyncio.Queue()

    def directives(self) -> list:
        async def drain():
            await asyncio.sleep(0.05)
            out = []
            while not self.channel.empty():
                out.append(self.channel.get_nowait())
            return out
        return asyncio.run_coroutine_threadsafe(drain(), self.loop).result(5)

    def close(self):
        self.loop.call_soon_threadsafe(self.loop.stop)


def test_a_switch_to_the_browser_starts_its_tab_at_the_second(tmp_path, proxy, renderers, manager,
                                                              switch, quality, monkeypatch):
    """N-14: playing on a renderer, then "This device": the play press waits
    for the tab that claims the output and starts it at the second."""
    import media_urls
    monkeypatch.setattr(media_urls, "signed_media_url", lambda kind, ident, q=None: f"/m/{kind}/{ident}")
    a = renderers()
    _on_renderer(manager, a, [track(tmp_path, 1, seconds=40.0)])
    _playing_at(manager, a, 3)
    switch(type="browser")
    # No tab holds the output yet: the start waits for the one that chose
    # it, and no "no playback device" is said meanwhile
    assert "error" not in manager.latest_status, manager.latest_status
    browser = manager.backend()
    tab = Tab(browser)
    try:
        load = [d for d in tab.directives() if d["cmd"] == "load"][-1]
        assert load["play"] is True and load["queue_index"] == 1 and 3 <= load["position"] <= 6
    finally:
        tab.close()


def test_a_switch_to_the_browser_carries_the_listen(tmp_path, proxy, renderers, manager, switch,
                                                    quality, heard, monkeypatch):
    """A track scrobbled past its half on a renderer, then "This device": the
    browser holds the spot (`resume`) until the tab that chose it claims the
    output and plays on — one listen on both outputs: no row at the switch,
    one now-playing, one scrobble. Its hold said as a plain stop, the listen
    ended at the switch, and the tab's first tick past the half scrobbled
    the track again."""
    import media_urls
    monkeypatch.setattr(media_urls, "signed_media_url", lambda kind, ident, q=None: f"/m/{kind}/{ident}")
    listened, lastfm = heard
    a = renderers()
    _on_renderer(manager, a, [track(tmp_path, 1, seconds=60.0)])
    _playing_at(manager, a, 1)
    assert manager.backend().seek(31)
    _playing_at(manager, a, 32)
    wait_for(lambda: ("scrobble", "Track 1") in lastfm, what="scrobbled on the renderer")
    switch(type="browser")
    assert (manager.latest_status.get("resume") or {}).get("index") == 1, manager.latest_status
    assert listened == []
    browser = manager.backend()
    tab = Tab(browser)
    try:
        wait_for(lambda: manager.latest_status.get("state") == "playing", what="the tab plays on")
        load = [d for d in tab.directives() if d["cmd"] == "load"][-1]
        browser.on_client_event("tab-1", "timeupdate", load["position"] + 1, 60.0, queue_index=1,
                                epoch=browser._epoch)
    finally:
        tab.close()
    assert listened == []
    assert lastfm == [("update_now_playing", "Track 1"), ("scrobble", "Track 1")], lastfm


def test_the_browser_keeps_a_carried_second_on_a_slot_whose_stream_is_coming(tmp_path, manager,
                                                                            monkeypatch):
    """A switch to "This device" at 0:21 of a track only a stream serves
    there: the tab that claims the output starts it at 0:21 once its stream
    lands."""
    import media_urls
    monkeypatch.setattr(media_urls, "signed_media_url", lambda kind, ident, q=None: f"/m/{kind}/{ident}")
    manager.activate("browser")
    # The stream lands when the test says so: no provider resolves it here
    monkeypatch.setattr(manager._subs, "maintain", lambda idx: None)
    waiting = track(tmp_path, 1, seconds=40.0)
    waiting.play = {"kind": "pending"}
    manager.queue.replace([waiting])
    browser = manager.backend()
    browser.resume_at(1, 21.0)
    assert browser.play_on()
    tab = Tab(browser)
    try:
        assert manager.latest_status["state"] == "loading"
        tab.directives()
        waiting.play = {"kind": "proxy", "token": "tok-1"}
        browser.slot_ready(1)
        load = [d for d in tab.directives() if d["cmd"] == "load"][-1]
        assert load["play"] is True and load["position"] == 21.0
    finally:
        tab.close()


def test_a_switch_from_the_browser_keeps_its_second_on_a_renderer(tmp_path, proxy, renderers, manager,
                                                                  switch, quality, monkeypatch):
    """N-14: a tab paused at 0:21 hands the next output that second, paused."""
    import media_urls
    monkeypatch.setattr(media_urls, "signed_media_url", lambda kind, ident, q=None: f"/m/{kind}/{ident}")
    b = renderers()
    manager.store.update({"output.type": "browser"})
    manager.activate("browser")
    manager.queue.replace([track(tmp_path, 1, seconds=40.0), track(tmp_path, 2, seconds=40.0)])
    browser = manager.backend()
    tab = Tab(browser)
    try:
        browser.select(2)
        epoch = browser._epoch
        browser.on_client_event("tab-1", "playing", 0.0, 40.0, queue_index=2, epoch=epoch)
        browser.on_client_event("tab-1", "paused", 21.0, 40.0, queue_index=2, epoch=epoch)
        switch(type="dlna", renderer=b.record)
    finally:
        tab.close()
    st = manager.latest_status
    assert (st["state"], st["track_index"], st["position"]) == ("paused", 2, 21.0)


def test_the_browser_starts_its_last_track_over_after_the_end(tmp_path, manager, monkeypatch):
    """The second a tab holds carries into the play press — but the end of
    the queue is no spot: Play starts the last track over."""
    import media_urls
    monkeypatch.setattr(media_urls, "signed_media_url", lambda kind, ident, q=None: f"/m/{kind}/{ident}")
    manager.activate("browser")
    manager.queue.replace([track(tmp_path, 1, seconds=40.0)])
    browser = manager.backend()
    tab = Tab(browser)
    try:
        browser.select(1)
        epoch = browser._epoch
        browser.on_client_event("tab-1", "playing", 0.0, 40.0, queue_index=1, epoch=epoch)
        browser.on_client_event("tab-1", "ended", 40.0, 40.0, queue_index=1, epoch=epoch)
        assert manager.latest_status["state"] == "stopped" and manager.spot() is None
        tab.directives()
        browser.play()
        load = [d for d in tab.directives() if d["cmd"] == "load"][-1]
        assert load["play"] is True and load["position"] == 0.0
    finally:
        tab.close()


def test_a_seeded_slot_starts_the_local_engine_at_its_second(tmp_path):
    """N-14: the built-in engine shows a carried-over spot paused, opens
    nothing, and its play command starts the slot at that second."""
    from playback.local.engine import Command, Engine
    q = CanonicalQueue()
    q.replace([track(tmp_path, 1, seconds=40.0), track(tmp_path, 2, seconds=40.0)])
    statuses = []
    engine = Engine(q, statuses.append, None, False)
    started = []
    engine._start_at = lambda index, seek_to=0.0: started.append((index, seek_to))
    engine._handle(Command("seed", index=2, seconds=33.0))
    st = statuses[-1]
    assert (st.state, st.queue_index, st.position, st.length) == ("paused", 2, 33.0, 40.0)
    engine._handle(Command("play"))
    assert started == [(2, 33.0)]


def test_a_stop_the_lost_renderer_never_answers_leaves_no_spot(tmp_path, proxy, renderers, manager,
                                                               quality):
    """A person's Stop lets the spot go, also on a renderer that does not
    answer it: the clock it was left by does not bring the spot back."""
    r = renderers(sink=PHONE_SINK)
    _on_renderer(manager, r, [track(tmp_path, 1, seconds=40.0)])
    _playing_at(manager, r, 2)
    r.stop_answering()
    wait_for(lambda: manager.latest_status.get("resume"), timeout=15, what="the renderer lost")
    manager.backend().stop()
    st = manager.latest_status
    assert st["state"] == "stopped" and "resume" not in st and manager.spot() is None, st


def test_a_track_change_the_renderer_never_takes_is_left_at_its_own_start(tmp_path, proxy, renderers,
                                                                          manager, quality):
    """A tap on slot 3 the renderer does not answer (its listener just
    closed): the spot is slot 3 from its start, never slot 3 at the second
    slot 1 was at."""
    r = renderers(sink=PHONE_SINK)
    items = [track(tmp_path, i, seconds=40.0) for i in (1, 2, 3)]
    _on_renderer(manager, r, items)
    _playing_at(manager, r, 4)
    r.stop_answering()
    manager.backend().select(3)
    wait_for(lambda: manager.latest_status.get("resume"), timeout=15, what="the renderer lost")
    assert manager.latest_status["resume"] == {"index": 3, "position": 0.0}


def test_a_play_waits_on_a_slot_whose_stream_is_on_its_way(tmp_path, proxy, renderers, nodes, quality):
    """A slot only a stream can serve here (a copy in an HQPlayer's own
    library) is `pending` while its stream is fetched: the play waits on it
    and goes when it lands — at the second it was asked at — and a slot
    found unplayable sends it on to the next. It used to be walked past."""
    r = renderers(sink=PHONE_SINK)
    q = CanonicalQueue()
    waiting, after = track(tmp_path, 1, seconds=40.0), track(tmp_path, 2, seconds=40.0)
    waiting.play = {"kind": "pending"}
    q.replace([waiting, after])
    n = nodes(q, r)
    n.backend.resume_at(1, 12.0)
    assert n.backend.play()
    wait_for(lambda: n.last.state == "loading" and n.last.queue_index == 1, what="waiting on slot 1")
    assert not r.called("SetAVTransportURI")
    # Waiting: a second Play keeps the second, a volume change keeps
    # "loading", a seek moves the second without asking the renderer
    assert n.backend.play()
    assert n.backend.set_volume(40)
    wait_for(lambda: r.volume == 40, what="the volume set")
    assert n.last.state == "loading"
    assert n.backend.seek(14) and not r.called("Seek")
    assert n.last.state == "loading" and n.last.position == 14
    n.backend._error = "an earlier word"
    n.backend.slot_ready(2)                        # a slot nobody waits on
    time.sleep(0.3)
    assert n.backend._error == "an earlier word"
    n.backend._error = None
    landed = streamed(proxy, 1, cover_url="")
    waiting.play = landed.source
    n.backend.slot_ready(1)
    wait_for(lambda: r.called("Seek"), what="slot 1 started at its second")
    assert r.called("SetAVTransportURI")[0]["CurrentURI"].endswith("/preview/stream1")
    assert r.called("Seek")[-1]["Target"] == "0:00:14"
    # Unplayable: the play walks on
    nothing = track(tmp_path, 3, seconds=40.0)
    nothing.play = {"kind": "pending"}
    q.replace([nothing, after])
    assert n.backend.select(1)
    wait_for(lambda: n.last.state == "loading" and n.last.queue_index == 1, what="waiting again")
    nothing.play = {"kind": "unplayable", "reason": "no provider has it"}
    n.backend.slot_ready(1)
    wait_for(lambda: n.last.queue_index == 2 and r.state == "PLAYING", what="slot 2 instead")


def test_a_wait_follows_its_track_and_ends_at_a_pause(tmp_path, proxy, renderers, nodes, quality):
    """The slot a play waits on follows its track through a removal before
    it; a Pause ends the wait (paused at its second), and the stream's
    landing then starts nothing."""
    r = renderers(sink=PHONE_SINK)
    q = CanonicalQueue()
    first, waiting = track(tmp_path, 1, seconds=40.0), track(tmp_path, 2, seconds=40.0)
    waiting.play = {"kind": "pending"}
    q.replace([first, waiting])
    n = nodes(q, r)
    assert n.backend.select(2)
    wait_for(lambda: n.last.state == "loading" and n.last.queue_index == 2, what="waiting on slot 2")
    q.remove(1)
    n.backend.queue_changed("remove")
    assert n.last.queue_index == 1 and n.last.state == "loading"
    waiting.play = streamed(proxy, 2, cover_url="").source
    n.backend.slot_ready(1)
    wait_for(lambda: r.state == "PLAYING", what="the track that waited, at its new slot")
    assert r.uri.endswith("/preview/stream2")
    # A Pause ends a wait
    other = track(tmp_path, 3, seconds=40.0)
    other.play = {"kind": "pending"}
    q.replace([other])
    assert n.backend.select(1)
    wait_for(lambda: n.last.state == "loading" and n.last.queue_index == 1, what="waiting again")
    assert n.backend.pause()
    assert n.last.state == "paused"
    loads = len(r.called("SetAVTransportURI"))
    other.play = streamed(proxy, 3, cover_url="").source
    n.backend.slot_ready(1)
    time.sleep(0.5)
    assert len(r.called("SetAVTransportURI")) == loads


def test_a_stop_the_renderer_refuses_is_no_stop_of_the_listen(tmp_path, proxy, renderers, nodes,
                                                              quality):
    """A Stop the renderer refuses (a transient 501 of a freshly selected AK
    Connect stack) says its words, and the status stays what the renderer
    plays: "stopped" is said only of a Stop it took, so the listen is not
    closed and opened again; the renderer is not taken for lost."""
    r = renderers(sink=AK_CONNECT_SINK)
    q = CanonicalQueue()
    q.replace([track(tmp_path, 1, seconds=40.0)])
    n = nodes(q, r)
    assert n.backend.select(1)
    wait_for(lambda: n.last.state == "playing", what="playing")
    said = len(n.statuses)
    r.faults["Stop"] = [501]
    assert not n.backend.stop()
    assert "stopped" not in [st.state for st in n.statuses[said:]]
    assert n.last.state == "playing" and "stop failed" in n.backend._error
    assert n.backend.healthy()
    assert n.backend.stop() and r.state == "STOPPED" and n.last.state == "stopped"


def test_a_spot_carried_at_its_first_second_is_held_as_any_other(tmp_path, proxy, renderers, nodes,
                                                                  quality):
    """An output switch right at a track's start carries its spot at 0 s:
    the renderer and the built-in engine hold it paused, as at any second —
    said stopped, it ended the listen the switch carried (a 0 % row, the
    track announced again as it played on)."""
    from playback.local.engine import Command, Engine
    q = CanonicalQueue()
    q.replace([track(tmp_path, 1, seconds=40.0)])
    n = nodes(q, renderers())
    n.backend.resume_at(1, 0.0)
    assert (n.last.state, n.last.position, n.last.queue_index) == ("paused", 0.0, 1)
    statuses = []
    engine = Engine(q, statuses.append, None, False)
    engine._handle(Command("seed", index=1, seconds=0.0))
    assert (statuses[-1].state, statuses[-1].position) == ("paused", 0.0)


def test_the_local_engine_waits_on_a_slot_whose_stream_is_on_its_way(tmp_path):
    """The built-in engine: a start on a pending slot waits there, a seek
    moves the second it will start at, and the landed stream starts it."""
    from playback.local.engine import Command, Engine
    q = CanonicalQueue()
    waiting = track(tmp_path, 1, seconds=40.0)
    waiting.play = {"kind": "pending"}
    q.replace([waiting])
    statuses = []
    engine = Engine(q, statuses.append, None, False)
    engine._handle(Command("seed", index=1, seconds=20.0))
    engine._handle(Command("play"))
    assert (statuses[-1].state, statuses[-1].position) == ("loading", 20.0)
    engine._handle(Command("seek", seconds=25.0))
    assert statuses[-1].position == 25.0
    started = []
    real_start = engine._start_at
    engine._start_at = lambda index, seek_to=0.0: started.append((index, seek_to))
    engine._handle(Command("slot_ready", index=1))
    assert started == [(1, 25.0)]
    # A Pause ends a wait: the landing starts nothing
    engine._start_at = real_start
    waiting.play = {"kind": "pending"}
    engine._handle(Command("play"))
    assert statuses[-1].state == "loading"
    engine._handle(Command("pause"))
    assert (statuses[-1].state, engine._parked) == ("paused", None)
    engine._start_at = lambda index, seek_to=0.0: started.append((index, seek_to))
    engine._handle(Command("slot_ready", index=1))
    assert started == [(1, 25.0)]


def test_a_transcode_larger_than_the_cache_is_kept_for_its_consumer(tmp_path, monkeypatch):
    """The file just made is never what the cache's own eviction takes."""
    monkeypatch.setattr(transcode, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(transcode, "_BUDGET_BYTES", 1000)
    old, new = tmp_path / "old.flac", tmp_path / "new.flac"
    old.write_bytes(b"x" * 600)
    new.write_bytes(b"y" * 1500)
    transcode._evict_if_needed(new)
    assert new.exists() and not old.exists()


def test_the_fake_renderer_plays_a_queue_through(tmp_path, proxy, renderers, nodes, quality):
    """The harness itself: attach, load, play, the gapless boundary by the
    armed NextURI, and the stop at the end of the queue."""
    r = renderers(sink=AK_CONNECT_SINK)
    q = CanonicalQueue()
    q.replace([track(tmp_path, 1, seconds=2.0), track(tmp_path, 2, seconds=2.0)])
    n = nodes(q, r)
    assert n.backend.select(1)
    wait_for(lambda: r.state == "PLAYING", what="playing")
    wait_for(lambda: r.next_uri, what="the next track armed")
    wait_for(lambda: n.last.queue_index == 2 and n.last.state == "playing", what="slot 2")
    wait_for(lambda: r.state == "STOPPED", what="the end")
    # One load: the second track came by the NextURI the node armed
    loads = [a["CurrentURI"] for a in r.called("SetAVTransportURI")]
    assert len(loads) == 1 and loads[0] != r.uri
    assert r.called("SetNextAVTransportURI")[0]["NextURI"] == r.uri


# -- N-20: an instance let go sends nothing; a renderer picked again stays ----------


class _Awaited:
    """A lock that tells when another thread waits for it."""

    def __init__(self, lock):
        self._lock = lock
        self.waiting = threading.Event()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if self._lock.acquire(False):
            return True
        if not blocking:
            return False
        self.waiting.set()
        return self._lock.acquire(True, timeout)

    def release(self) -> None:
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc) -> bool:
        self.release()
        return False


def _first_stream_held(proxy, monkeypatch) -> tuple:
    """The first stream a load makes ready waits until `go` is set
    (`waiting` says it does); the others go at once."""
    waiting, go, made = threading.Event(), threading.Event(), proxy.materialize_file

    def held(*args):
        if not waiting.is_set():
            waiting.set()
            go.wait(10)
        return made(*args)
    monkeypatch.setattr(proxy, "materialize_file", held)
    return waiting, go


def test_an_instance_let_go_while_its_load_is_on_its_way_sends_nothing_more(tmp_path, proxy, renderers, nodes,
                                                                            quality, monkeypatch):
    """N-20: an instance let go — a switch away, the play gate attaching the
    renderer elsewhere — while one of its loads waits for its stream sends
    the renderer nothing more: not that load's URI, its Play or its next. A
    phone started 6:45 a second time from an instance that had already
    unsubscribed (2026-11-01)."""
    r = renderers()
    q = CanonicalQueue()
    q.replace([track(tmp_path, 1, seconds=40.0), track(tmp_path, 2, seconds=40.0)])
    n = nodes(q, r)
    waiting, go = _first_stream_held(proxy, monkeypatch)
    assert n.backend.select(1)
    assert waiting.wait(10), "the load waits for its stream"
    n.backend.shutdown()
    let_go = len(r.calls)
    go.set()
    wait_for(lambda: not n.backend._loading, what="the load over")
    assert r.called("UNSUBSCRIBE"), "the instance was let go"
    assert r.calls[let_go:] == []
    assert r.state == "NO_MEDIA_PRESENT"


def test_a_switch_away_while_a_load_is_on_its_way_leaves_the_renderer_stopped(tmp_path, proxy, renderers, manager,
                                                                              switch, quality, monkeypatch):
    """N-20: a track tapped on one renderer is on its way (its stream being
    made ready) when the person moves the music to another. The renderer left
    gets the switch's Stop, its instance's last word, and the tap's load goes
    no further — it used to go on past that Stop and start the renderer
    switched away from, beside the new one."""
    a, b = renderers(name="Kitchen"), renderers(name="Study")
    _on_renderer(manager, a, [track(tmp_path, 1, seconds=40.0), track(tmp_path, 2, seconds=40.0)])
    _playing_at(manager, a, 2)
    waiting, go = _first_stream_held(proxy, monkeypatch)
    left = manager.backend()
    assert left.select(2)
    assert waiting.wait(10), "the tap's load waits for its stream"
    switch(type="dlna", renderer=b.record)
    assert a.called("Stop"), "the switch stopped the renderer left"
    stopped = len(a.calls)
    go.set()
    wait_for(lambda: not left._loading, what="the tap's load over")
    wait_for(lambda: b.state == "PLAYING", what="the new renderer playing")
    assert a.calls[stopped:] == []
    assert a.state == "STOPPED"
    assert manager.latest_status["track_index"] == 2


def test_a_renderer_picked_again_while_the_play_gate_attaches_it_loads_the_track_once(
        tmp_path, proxy, renderers, manager, picker, switch, quality):
    """N-20: the phone, paused, moved from the LAN to a tunnel; Play attaches
    it again at the tunnel (the play gate), and the person, hearing nothing
    yet, picks it again in the Output picker. The renderer picked again stays
    the output as it is: the track loads once, by the instance the press was
    made on, at the second it was left at. On 2026-11-01 the re-pick let the
    gate's instance go — whose load still went, after its UNSUBSCRIBE — and
    attached another, which loaded the track a second time."""
    lan, tunnel = renderers(name="Phone"), renderers(name="Phone")
    tunnel.udn = lan.udn
    _on_renderer(manager, lan, [track(tmp_path, 1, seconds=40.0)])
    _playing_at(manager, lan, 3)
    manager.backend().pause()
    wait_for(lambda: manager.latest_status.get("state") == "paused", what="paused")
    lan.stop_answering()                                  # Wi-Fi off
    wait_for(lambda: manager.latest_status.get("resume"), timeout=15, what="the renderer lost")
    left_at = manager.latest_status["resume"]["position"]
    picker.note_renderer_location(lan.udn, tunnel.location)     # a scan reached it through the tunnel
    hold = tunnel.hold_description = threading.Event()
    manager._mutate_lock = gate = _Awaited(manager._mutate_lock)
    answers = []
    press = threading.Thread(target=lambda: answers.append(picker.play()))
    press.start()
    assert tunnel.describing.wait(15), "the play gate attaches the renderer at the tunnel"
    row = _rows(picker)[0]
    repick = threading.Thread(target=lambda: answers.append(switch(type="dlna", renderer=row)))
    repick.start()
    assert gate.waiting.wait(10), "the re-pick waits for the gate"
    hold.set()
    press.join(20)
    repick.join(20)
    assert len(answers) == 2 and answers[0] == {"ok": True}
    wait_for(lambda: tunnel.state == "PLAYING", timeout=20, what="the phone playing")
    assert not tunnel.called("UNSUBSCRIBE"), "the instance the press was made on was never let go"
    wait_for(lambda: manager.latest_status.get("state") == "playing", timeout=20, what="the node saying so")
    assert len(tunnel.called("SetAVTransportURI")) == 1
    wait_for(lambda: tunnel.called("Seek"), what="the start sought")
    assert [s["Target"] for s in tunnel.called("Seek")] == [dlna_backend._rel_time(left_at)]


# -- N-20: the play gate — where the renderer answers now, said while it attaches ----


class _Swallowing:
    """An address a device has left: it neither answers nor refuses — a
    connect there waits out its whole timeout. A listener whose backlog is
    full drops every further SYN; the address is the renderer's own, so its
    fake must have stopped answering first."""

    def __init__(self, port: int):
        self._hole = socket.socket()
        self._hole.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._hole.bind(("127.0.0.1", port))
        self._hole.listen(0)
        self._held = [socket.create_connection(("127.0.0.1", port), timeout=1)]
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()

    def close(self) -> None:
        for s in [*self._held, self._hole]:
            s.close()


@pytest.fixture
def swallowing():
    """Addresses made to swallow connects (_Swallowing), let go at the end."""
    made = []

    def make(port: int) -> _Swallowing:
        made.append(_Swallowing(port))
        return made[-1]
    yield make
    for hole in made:
        hole.close()


def _lost_paused(manager, r: FakeRenderer) -> float:
    """Paused, the renderer leaves the network: the node loses it there.
    Returns the second it was left at."""
    manager.backend().pause()
    wait_for(lambda: manager.latest_status.get("state") == "paused" and r.state == "PAUSED_PLAYBACK",
             what="paused")
    r.stop_answering()
    wait_for(lambda: manager.latest_status.get("resume"), timeout=15, what="the renderer lost")
    return manager.latest_status["resume"]["position"]


def _pressed(picker) -> float:
    """Play, as the person presses it; the seconds the press took."""
    started = time.monotonic()
    assert picker.play() == {"ok": True}
    return time.monotonic() - started


def _statuses(manager) -> list:
    """Every status the manager publishes from now on, as published."""
    seen, push = [], manager._push_status
    manager._push_status = lambda data: (push(data), seen.append(dict(data)))[0]
    return seen


def test_a_scan_that_reaches_the_renderer_through_a_tunnel_moves_the_output_there(
        tmp_path, proxy, renderers, manager, picker, quality, swallowing):
    """N-20, the owner's Tailscale requirement, there: the phone, paused on
    the LAN, goes off Wi-Fi onto a tunnel. A scan that reaches it through
    the tunnel moves the output there at once — at the spot, nothing
    started, saying meanwhile that it connects — and the first Play goes
    there with no wait on the LAN address it left (9 s, 2026-11-01): the
    track starts once, at the second it was left at."""
    lan, tunnel = renderers(name="Phone"), renderers(name="Phone")
    tunnel.udn = lan.udn
    _on_renderer(manager, lan, [track(tmp_path, 1, seconds=90.0)])
    _playing_at(manager, lan, 3)
    left_at = _lost_paused(manager, lan)                  # Wi-Fi off
    swallowing(lan.port)
    seen = _statuses(manager)
    picker.found[:] = [tunnel.location]                   # the tunnel up; the person scans
    asyncio.run(picker._scan_run())
    moved = manager.active
    assert moved._renderer["location"] == tunnel.location and moved.healthy(), "the output moved at the scan"
    st = manager.latest_status
    assert (st["state"], st["track_index"], st["position"]) == ("paused", 1, left_at)
    assert "connecting" not in st and not tunnel.called("SetAVTransportURI")
    moving = [s for s in seen if s.get("state") != "paused"]
    assert moving and all(s.get("connecting") == "Phone" for s in moving)
    assert "disconnected" not in [s.get("state") for s in seen]
    changes = manager.output_changes()
    assert _pressed(picker) < dlna_backend._PROBE_TIMEOUT - 1, "the press waited on the address left"
    assert manager.output_changes() == changes and manager.active is moved
    wait_for(lambda: tunnel.state == "PLAYING" and tunnel.called("Seek"), what="the phone playing at the tunnel")
    assert [s["Target"] for s in tunnel.called("Seek")] == [dlna_backend._rel_time(left_at)]
    assert len(tunnel.called("SetAVTransportURI")) == 1


def test_back_on_the_lan_the_first_play_attaches_the_renderer_there_with_no_wait_on_the_tunnel(
        tmp_path, proxy, renderers, manager, picker, quality, swallowing):
    """N-20, the owner's Tailscale requirement, back: the phone played
    through the tunnel, paused, and came back on Wi-Fi, the tunnel down; no
    scan. The first Play attaches it at its LAN address with no wait on the
    tunnel address (11 s, 2026-11-01: a probe, an UNSUBSCRIBE and an attach
    each waited out the address it had left), saying meanwhile that it
    connects — never "disconnected" — and starts the track once, at the
    second it was left at."""
    # Attached through the tunnel, the LAN address known behind it, as a
    # scan through the tunnel leaves the chosen record
    tunnel, lan = _attached_at_one_of_two(tmp_path, manager, renderers, other_answers=False)
    left_at = _lost_paused(manager, tunnel)               # the tunnel down
    swallowing(tunnel.port)
    lan.answer_again()                                    # back on Wi-Fi
    seen = _statuses(manager)
    assert _pressed(picker) < dlna_backend._PROBE_TIMEOUT - 1, "the press waited on the address left"
    assert manager.active._renderer["location"] == lan.location
    wait_for(lambda: lan.state == "PLAYING" and lan.called("Seek"), what="the phone playing on the LAN")
    assert [s["Target"] for s in lan.called("Seek")] == [dlna_backend._rel_time(left_at)]
    assert len(lan.called("SetAVTransportURI")) == 1
    connecting = [s for s in seen if s.get("connecting")]
    assert connecting and all(s["connecting"] == "Phone" for s in connecting)
    assert "disconnected" not in [s.get("state") for s in seen]
    assert not manager.latest_status.get("connecting")


# -- N-7: a renderer's own Seek in an Opus stream before the reload ---------------


def _opus_on(tmp_path, monkeypatch, quality, renderers, nodes, **knobs) -> tuple:
    """One owned 90-s track in opus_96 on a phone renderer of its own (its
    knobs set first), playing past 4 s: a renderer that reports its position
    has been heard more than once by then."""
    monkeypatch.setattr(transcode, "opus_path_for_file", lambda path, q, ss=0.0: path)
    quality["q"] = "opus_96"
    r = renderers(sink=PHONE_SINK)
    for knob, value in knobs.items():
        setattr(r, knob, value)
    q = CanonicalQueue()
    q.replace([track(tmp_path, 1, seconds=90.0)])
    n = nodes(q, r)
    assert n.backend.select(1)
    wait_for(lambda: n.last.state == "playing" and n.last.position >= 4, what="playing past 4 s")
    if r.opus_position:
        wait_for(lambda: getattr(n.backend, "_heard_on_load", 2) >= 2, what="its position heard twice")
    assert "q=opus_96" in r.uri and "ss=" not in r.uri
    return r, n


def test_an_opus_stream_is_sought_by_a_renderer_that_reports_its_position_there(
        tmp_path, proxy, renderers, nodes, quality, monkeypatch, stored):
    """N-7: the phone reports its position in an Opus stream and seeks one
    itself in 57–74 ms: it is sent a Seek — no reload, so its own player keeps
    the track's scale (after ss=309 in a 7:22 track it read 2:13, from 0:00:
    2026-11-01), its transport plays on and nothing is re-encoded — and it is
    remembered as a renderer that seeks one. A DAP that reports 0:00:00
    throughout an Opus stream and plays on where it was at a Seek keeps the
    reload: a Seek's landing could never be seen there."""
    phone, n = _opus_on(tmp_path, monkeypatch, quality, renderers, nodes)
    assert n.backend.seek(40)
    wait_for(lambda: phone.called("Seek"), what="the Seek")
    assert [s["Target"] for s in phone.called("Seek")] == ["0:00:40"]
    wait_for(lambda: n.last.state == "playing" and n.last.position >= 41.5, what="playing on past 40")
    assert len(phone.called("SetAVTransportURI")) == 1 and "ss=" not in phone.uri
    assert stored["output.dlna_opus_seek"] == {phone.udn.lower(): True}
    dap, m = _opus_on(tmp_path, monkeypatch, quality, renderers, nodes,
                      opus_position=False, opus_seek="ignores")
    assert m.backend.seek(40)
    wait_for(lambda: "ss=40" in dap.uri and dap.state == "PLAYING", what="the DAP's stream again from 40")
    assert not dap.called("Seek")
    assert dap.udn.lower() not in stored["output.dlna_opus_seek"]


def test_a_renderer_that_refuses_a_seek_in_an_opus_stream_gets_the_reload_and_is_remembered(
        tmp_path, proxy, renderers, nodes, quality, monkeypatch, stored):
    """N-7: a renderer that answers a Seek in an Opus stream with UPnP 710
    (no such seek mode) gets the stream again from the second (?ss=), and is
    remembered as one that does not seek one: its next seek is the reload,
    with no Seek first."""
    r, n = _opus_on(tmp_path, monkeypatch, quality, renderers, nodes)
    r.faults["Seek"] = [710]
    assert n.backend.seek(40)
    wait_for(lambda: "ss=40" in r.uri and r.state == "PLAYING", what="the stream again from 40")
    assert len(r.called("Seek")) == 1
    assert stored["output.dlna_opus_seek"] == {r.udn.lower(): False}
    assert n.backend.seek(60)
    wait_for(lambda: "ss=60" in r.uri and r.state == "PLAYING", what="the stream again from 60")
    assert len(r.called("Seek")) == 1


def test_a_seek_in_an_opus_stream_that_does_not_land_loads_the_stream_again_from_the_clock(
        tmp_path, proxy, renderers, nodes, quality, monkeypatch, stored):
    """N-7: a renderer that reports its position in an Opus stream but plays
    on where it was at a Seek. Not seen landing by the pending-play deadline,
    the stream loads again from where the clock stands — the Seek's second,
    run on since, where the slider is — and the renderer is remembered as one
    that does not seek one."""
    monkeypatch.setattr(dlna_backend, "_PENDING_PLAY_DEADLINE", 3.0)
    r, n = _opus_on(tmp_path, monkeypatch, quality, renderers, nodes, opus_seek="ignores")
    asked = time.monotonic()
    assert n.backend.seek(40)
    wait_for(lambda: "ss=" in r.uri and r.state == "PLAYING", timeout=15, what="the stream again")
    second = FakeRenderer.offset(r.uri)
    assert 42 <= second <= 40 + (time.monotonic() - asked) + 1
    assert [s["Target"] for s in r.called("Seek")] == ["0:00:40"]
    assert stored["output.dlna_opus_seek"] == {r.udn.lower(): False}
    wait_for(lambda: n.last.state == "playing" and n.last.position >= second + 1, what="playing on there")


def test_a_renderer_known_to_seek_opus_starts_a_spot_with_its_own_seek(tmp_path, proxy, renderers, nodes,
                                                                       quality, monkeypatch, stored):
    """N-7 at a start mid-track — an output switch's spot, a lost renderer's:
    a renderer remembered as one that seeks an Opus stream itself starts it
    from the top and is sought there once it plays, as in any other format;
    the reload's offset stream would have restarted its own player at 0:00."""
    monkeypatch.setattr(transcode, "opus_path_for_file", lambda path, q, ss=0.0: path)
    quality["q"] = "opus_96"
    r = renderers(sink=PHONE_SINK)
    stored["output.dlna_opus_seek"] = {r.udn.lower(): True}
    q = CanonicalQueue()
    q.replace([track(tmp_path, 1, seconds=90.0)])
    n = nodes(q, r)
    n.backend.resume_at(1, 35.0)
    assert n.backend.play()
    wait_for(lambda: r.called("Seek"), what="the start sought")
    loaded = r.called("SetAVTransportURI")
    assert len(loaded) == 1 and "q=opus_96" in loaded[0]["CurrentURI"] and "ss=" not in loaded[0]["CurrentURI"]
    assert [s["Target"] for s in r.called("Seek")] == ["0:00:35"]
    wait_for(lambda: n.last.state == "playing" and n.last.position >= 36, what="playing on from 35")


# -- the review's first round ----------------------------------------------------


def _attached_at_one_of_two(tmp_path, manager, renderers, *, other_answers: bool = True) -> tuple:
    """One renderer at two addresses — `here`, where it is attached, and
    `there`, known behind it in the chosen record (as a scan through a tunnel
    leaves it) — playing past 3 s. `other_answers` False: `there` refuses
    meanwhile (the phone away from its LAN)."""
    here, there = renderers(name="Phone"), renderers(name="Phone")
    there.udn = here.udn
    there.stop_answering()            # the attach takes the first address that answers
    manager.store.update({"output.type": "dlna", "output.dlna_renderer": {
        **here.record, "locations": [here.location, there.location]}})
    manager.activate("dlna", renderer=manager.store["output.dlna_renderer"])
    assert manager.active._renderer["location"] == here.location
    if other_answers:
        there.answer_again()
    assert manager.replace_queue([track(tmp_path, 1, seconds=90.0)], play=True)[0] == 1
    _playing_at(manager, here, 3)
    assert manager.active._renderer["location"] == here.location
    return here, there


def _paused(manager, r: FakeRenderer) -> float:
    manager.backend().pause()
    wait_for(lambda: manager.latest_status.get("state") == "paused" and r.state == "PAUSED_PLAYBACK",
             what="paused")
    return manager.latest_status["position"]


def test_back_on_the_lan_play_takes_the_lan_address_at_once(
        tmp_path, proxy, renderers, manager, picker, quality, swallowing, monkeypatch):
    """N-20 as it happened: the phone played through the tunnel, paused, and
    came back on Wi-Fi, the tunnel down — its address swallowing what is sent
    to it, the node not yet aware (each poll waits out its timeout) — and
    Play pressed at once. A LAN address that answers is better than the
    tunnel's: taken at once (the press waited out the tunnel address's probe
    window first, 11 s live), "connecting" said, the track started once at
    the second it was left at."""
    tunnel, lan = _attached_at_one_of_two(tmp_path, manager, renderers, other_answers=False)
    monkeypatch.setattr(dlna_backend, "_tunnel_address", lambda location: location == tunnel.location,
                        raising=False)
    left_at = _paused(manager, tunnel)
    lan.answer_again()                                      # back on Wi-Fi
    tunnel.stop_answering()
    swallowing(tunnel.port)                                 # the tunnel down
    assert manager.active.healthy(), "the node has no word of it yet"
    seen = _statuses(manager)
    assert _pressed(picker) < dlna_backend._PROBE_TIMEOUT - 1, "the press waited on the address left"
    assert manager.active._renderer["location"] == lan.location
    wait_for(lambda: lan.state == "PLAYING" and lan.called("Seek"), what="the phone playing on the LAN")
    assert [s["Target"] for s in lan.called("Seek")] == [dlna_backend._rel_time(left_at)]
    assert len(lan.called("SetAVTransportURI")) == 1
    assert any(s.get("connecting") == "Phone" for s in seen)


def test_play_takes_another_address_at_once_once_a_poll_failed_where_it_is_attached(
        tmp_path, proxy, renderers, manager, picker, quality, swallowing):
    """The address the renderer is attached at went dark while it was paused
    (its poll waits out each request): once one of the poll's requests has
    failed there, the node doubts it, and Play takes the other address that
    answers at once, at the spot."""
    here, there = _attached_at_one_of_two(tmp_path, manager, renderers)
    left_at = _paused(manager, here)
    here.stop_answering()
    swallowing(here.port)
    wait_for(lambda: getattr(manager.active, "_misses", 1) >= 1, timeout=20, what="a poll failed there")
    assert manager.active.healthy(), "not lost yet"
    assert _pressed(picker) < dlna_backend._PROBE_TIMEOUT - 1, "the press waited on the address left"
    assert manager.active._renderer["location"] == there.location
    wait_for(lambda: there.state == "PLAYING" and there.called("Seek"), what="playing at the other address")
    assert [s["Target"] for s in there.called("Seek")] == [dlna_backend._rel_time(left_at)]


def test_play_waiting_on_the_address_it_is_attached_at_says_it_connects(
        tmp_path, proxy, renderers, manager, picker, quality, swallowing):
    """The renderer stopped — no poll runs, so nothing has failed at its
    address when it goes dark — and Play pressed at once. The address it is
    attached at decides first, and its wait is that address's probe window;
    but once another address has answered, the wait is said ("connecting",
    within a second of the press, not after it), never "disconnected", and
    the track then plays at the other address."""
    here, there = _attached_at_one_of_two(tmp_path, manager, renderers)
    manager.backend().stop()
    wait_for(lambda: manager.latest_status.get("state") == "stopped" and here.state == "STOPPED", what="stopped")
    here.stop_answering()
    swallowing(here.port)
    seen = []
    push = manager._push_status
    manager._push_status = lambda data: (push(data), seen.append((time.monotonic(), dict(data))))[0]
    pressed = time.monotonic()
    assert picker.play() == {"ok": True}
    said = next((at for at, data in seen if data.get("connecting") == "Phone"), None)
    assert said is not None and said - pressed < 1.5, (said and said - pressed)
    # One word from the wait through the attach the move needs, never
    # taken back in between (it was: the wait's own, then the loss and the
    # attach's)
    connecting = [i for i, (_at, data) in enumerate(seen) if data.get("connecting")]
    assert all(seen[i][1].get("connecting") == "Phone" for i in range(connecting[0], connecting[-1] + 1)), \
        [data.get("connecting") for _at, data in seen]
    assert "disconnected" not in [data.get("state") for _at, data in seen]
    assert manager.active._renderer["location"] == there.location
    wait_for(lambda: there.state == "PLAYING", what="playing at the other address")


def test_a_renderer_answering_at_two_addresses_of_a_kind_stays_where_it_is_attached(
        tmp_path, proxy, renderers, manager, picker, quality):
    """A renderer that answers at two addresses of a kind about as fast stays
    where it is attached press after press: moved to whichever answered
    first, it would be attached afresh at random, every press a reload."""
    here, there = _attached_at_one_of_two(tmp_path, manager, renderers)
    attached, changes = manager.active, manager.output_changes()
    for _press in range(3):
        _paused(manager, here)
        assert picker.play() == {"ok": True}
        wait_for(lambda: manager.latest_status.get("state") == "playing", what="playing again")
    assert manager.active is attached and manager.output_changes() == changes
    assert not there.called("SetAVTransportURI")


def test_a_scan_moves_a_playing_renderer_whose_address_went_dark_at_the_clocks_second(
        tmp_path, proxy, renderers, manager, picker, quality, swallowing):
    """N-20: the phone plays when its LAN address goes dark, the node not yet
    aware (its poll waits out each request), and a scan reaches it through
    the tunnel. The move holds the track at the second by the clock — where
    the music is — not at the last reading the stalled poll left seconds
    behind, and starts nothing."""
    lan, tunnel = renderers(name="Phone"), renderers(name="Phone")
    tunnel.udn = lan.udn
    _on_renderer(manager, lan, [track(tmp_path, 1, seconds=90.0)])
    _playing_at(manager, lan, 3)
    lan.stop_answering()
    swallowing(lan.port)
    dark, at_dark = time.monotonic(), manager.latest_status["position"]
    assert manager.active.healthy(), "the node has no word of it yet"
    picker.found[:] = [tunnel.location]
    asyncio.run(picker._scan_run())
    moved = time.monotonic() - dark
    st = manager.latest_status
    assert manager.active._renderer["location"] == tunnel.location
    assert (st["state"], st["track_index"]) == ("paused", 1)
    assert st["position"] >= at_dark + moved - 2 * dlna_backend._RELTIME_TOLERANCE, (st["position"], moved)
    assert not tunnel.called("SetAVTransportURI")


def test_a_move_whose_attach_fails_keeps_the_spot_for_the_next_play(
        tmp_path, proxy, renderers, manager, picker, quality, monkeypatch, swallowing):
    """N-20: a scan reaches the lost phone through the tunnel, but the attach
    there fails (a description it cannot read, an update timing out): the
    output is said disconnected with its spot kept, and the next Play
    attaches it there and starts the track at the second it was left at —
    it used to start at 0:00, the spot gone with the failed attach."""
    lan, tunnel = renderers(name="Phone"), renderers(name="Phone")
    tunnel.udn = lan.udn
    _on_renderer(manager, lan, [track(tmp_path, 1, seconds=90.0)])
    _playing_at(manager, lan, 5)
    left_at = _lost_paused(manager, lan)
    swallowing(lan.port)
    create, failing = DlnaBackend._create_device, {"on": True}

    async def once_failing(self, factory):
        if failing["on"]:
            raise dlna_backend.DlnaAttachError("described badly")
        return await create(self, factory)
    monkeypatch.setattr(DlnaBackend, "_create_device", once_failing)
    picker.found[:] = [tunnel.location]
    asyncio.run(picker._scan_run())
    assert manager.active is None and manager.latest_status["state"] == "disconnected"
    failing["on"] = False
    assert picker.play() == {"ok": True}
    wait_for(lambda: tunnel.state == "PLAYING" and tunnel.called("Seek"), what="the phone playing at its second")
    assert [s["Target"] for s in tunnel.called("Seek")] == [dlna_backend._rel_time(left_at)]


def test_a_seek_in_an_opus_stream_a_renderer_ignores_is_never_taken_for_a_landing(
        tmp_path, proxy, renderers, nodes, quality, monkeypatch, stored):
    """N-7: a renderer that reports its position in an Opus stream but plays
    on where it was at a Seek. Its readings come into the Seek's window once
    it has played the distance, and were taken for a landing: the scrub lost,
    the renderer remembered as one that seeks. A reading its staying put
    explains is no landing: at the deadline the stream loads again from the
    clock's second, remembered false. A seek within both readings' tolerance
    cannot be told from staying put: it is left to the renderer, and teaches
    nothing."""
    monkeypatch.setattr(dlna_backend, "_PENDING_PLAY_DEADLINE", 10.0)
    r, n = _opus_on(tmp_path, monkeypatch, quality, renderers, nodes, opus_seek="ignores")
    target = int(n.last.position) + 9
    assert n.backend.seek(target)
    wait_for(lambda: "ss=" in r.uri and r.state == "PLAYING", timeout=20, what="the stream again")
    assert FakeRenderer.offset(r.uri) >= target + 8, r.uri          # the clock's second, not the renderer's
    assert stored["output.dlna_opus_seek"] == {r.udn.lower(): False}
    near, m = _opus_on(tmp_path, monkeypatch, quality, renderers, nodes, opus_seek="ignores")
    assert m.backend.seek(int(m.last.position) + 5)
    wait_for(lambda: near.called("Seek"), what="the Seek")
    wait_for(lambda: m.backend._seek_due is None, timeout=15, what="the Seek let go")
    assert near.udn.lower() not in stored["output.dlna_opus_seek"] and "ss=" not in near.uri


def test_a_paused_seek_in_an_opus_stream_never_starts_playback_by_itself(
        tmp_path, proxy, renderers, nodes, quality, monkeypatch, stored):
    """N-7, paused: a renderer that ignores a Seek in an Opus stream stays
    paused past the deadline — the reload would start it, and a scrub while
    paused is no Play — and the Seek's wait counts once it plays again: its
    readings then show it played on from where it was, and the stream loads
    again from the clock's second."""
    monkeypatch.setattr(dlna_backend, "_PENDING_PLAY_DEADLINE", 3.0)
    r, n = _opus_on(tmp_path, monkeypatch, quality, renderers, nodes, opus_seek="ignores")
    assert n.backend.pause()
    wait_for(lambda: r.state == "PAUSED_PLAYBACK" and n.last.state == "paused", what="paused")
    assert n.backend.seek(40)
    time.sleep(5.0)                                       # past the deadline, paused
    assert r.state == "PAUSED_PLAYBACK" and "ss=" not in r.uri and n.last.state == "paused"
    assert r.udn.lower() not in stored.get("output.dlna_opus_seek", {})
    assert n.backend.play()
    wait_for(lambda: "ss=" in r.uri and r.state == "PLAYING", timeout=15, what="the stream again")
    assert FakeRenderer.offset(r.uri) >= 42, r.uri
    assert stored["output.dlna_opus_seek"] == {r.udn.lower(): False}


def test_a_seek_refused_as_no_such_target_reloads_and_teaches_nothing(
        tmp_path, proxy, renderers, nodes, quality, monkeypatch, stored):
    """N-7: UPnP 711 (no such target) is about the target, not the renderer:
    the stream loads again from the second, and nothing is remembered."""
    r, n = _opus_on(tmp_path, monkeypatch, quality, renderers, nodes)
    r.faults["Seek"] = [711]
    assert n.backend.seek(40)
    wait_for(lambda: "ss=40" in r.uri and r.state == "PLAYING", what="the stream again from 40")
    assert len(r.called("Seek")) == 1
    assert r.udn.lower() not in stored.get("output.dlna_opus_seek", {})


def test_a_start_seek_refused_in_an_opus_stream_loads_it_from_the_spot(
        tmp_path, proxy, renderers, nodes, quality, monkeypatch, stored):
    """N-7 at a spot: a renderer remembered to seek Opus refuses the start's
    Seek with 710 — it is remembered false, and the stream loads again from
    the spot's second."""
    monkeypatch.setattr(transcode, "opus_path_for_file", lambda path, q, ss=0.0: path)
    quality["q"] = "opus_96"
    r = renderers(sink=PHONE_SINK)
    r.faults["Seek"] = [710]
    stored["output.dlna_opus_seek"] = {r.udn.lower(): True}
    q = CanonicalQueue()
    q.replace([track(tmp_path, 1, seconds=90.0)])
    n = nodes(q, r)
    n.backend.resume_at(1, 35.0)
    assert n.backend.play()
    wait_for(lambda: "ss=35" in r.uri and r.state == "PLAYING", what="the stream again from 35")
    assert len(r.called("Seek")) == 1
    assert stored["output.dlna_opus_seek"] == {r.udn.lower(): False}


def test_a_play_on_its_way_at_a_switch_away_is_answered_before_the_last_word(
        tmp_path, proxy, renderers, manager, switch, quality):
    """N-20: the renderer left was still to answer a Play of the node's when
    the person switched away. The switch's Stop goes once that Play has its
    answer: sent while it was on its way, the Stop came first, and the Play
    then started the renderer switched away from."""
    a, b = renderers(name="Kitchen"), renderers(name="Study")
    _on_renderer(manager, a, [track(tmp_path, 1, seconds=40.0), track(tmp_path, 2, seconds=40.0)])
    _playing_at(manager, a, 2)
    a.slow["Play"] = 1.5
    plays, answered = len(a.called("Play")), a.answered.count("Play")
    assert manager.backend().select(2)
    wait_for(lambda: len(a.called("Play")) > plays, what="the tap's Play on its way")
    switch(type="dlna", renderer=b.record)
    wait_for(lambda: a.answered.count("Play") > answered, what="the tap's Play answered")
    wait_for(lambda: b.state == "PLAYING", what="the new renderer playing")
    assert a.state == "STOPPED", a.state
    assert [act for act in a.answered if act in ("Play", "Stop")][-2:] == ["Play", "Stop"]


# -- the review's second round ---------------------------------------------------


def test_a_scan_that_ends_while_the_play_gate_attaches_the_renderer_moves_nothing(
        tmp_path, proxy, renderers, manager, picker, quality, monkeypatch, swallowing):
    """N-20: the phone lost on the LAN, the person presses Play — the gate
    attaches it through the tunnel — and opens the Output picker, whose scan
    ends while that attach is under way. The scan finds the output being
    attached and leaves it: one attach, the press played once. It used to
    take the attach under way for one to move, attach the renderer a second
    time, and drop the press (or play it while the node said "paused")."""
    lan, tunnel = renderers(name="Phone"), renderers(name="Phone")
    tunnel.udn = lan.udn
    _on_renderer(manager, lan, [track(tmp_path, 1, seconds=90.0)])
    _playing_at(manager, lan, 3)
    _lost_paused(manager, lan)                              # Wi-Fi off
    swallowing(lan.port)
    # Known through the tunnel too, behind its LAN address — as before the
    # scan that follows teaches the record otherwise
    manager.store["output.dlna_renderer"] = {**lan.record, "locations": [lan.location, tunnel.location]}
    entered, go, create = threading.Event(), threading.Event(), DlnaBackend._create_device

    async def held(self, factory):
        if not entered.is_set():                            # the gate's attach, half way
            entered.set()
            await asyncio.to_thread(go.wait, 20)
        return await create(self, factory)
    monkeypatch.setattr(DlnaBackend, "_create_device", held)
    manager._mutate_lock = lock = _Awaited(manager._mutate_lock)
    changes, answers = manager.output_changes(), []
    press = threading.Thread(target=lambda: answers.append(picker.play()))
    try:
        press.start()
        assert entered.wait(15), "the gate attaches the phone"
        attaching = manager.active
        picker.found[:] = [tunnel.location]                 # the picker's scan
        scan = threading.Thread(target=lambda: asyncio.run(picker._scan_run()))
        scan.start()
        assert lock.waiting.wait(15), "the scan waits for the gate"
    finally:
        go.set()
    press.join(30)
    scan.join(30)
    assert answers == [{"ok": True}]
    wait_for(lambda: tunnel.state == "PLAYING" and manager.latest_status.get("state") == "playing",
             timeout=15, what="the press played")
    assert manager.active is attaching and manager.output_changes() == changes + 1
    assert len(tunnel.called("SetAVTransportURI")) == 1


def test_another_device_at_a_renderers_old_address_is_forgotten_for_it(
        tmp_path, proxy, renderers, manager, picker, quality, monkeypatch):
    """The phone attached through the tunnel; its old LAN address, still
    remembered, now answers as another device, faster than the tunnel. The
    first press finds the other device there, attaches the phone at the
    tunnel again, and forgets that address for it: the presses after it are
    plain resumes — it was drawn off its tunnel and attached afresh at every
    press, each a reload."""
    tunnel, other = renderers(name="Phone"), renderers(name="Another phone")     # its own UDN
    other.stop_answering()
    manager.store.update({"output.type": "dlna", "output.dlna_renderer": {
        **tunnel.record, "locations": [tunnel.location, other.location]}})
    manager.activate("dlna", renderer=manager.store["output.dlna_renderer"])
    assert manager.active._renderer["location"] == tunnel.location
    other.answer_again()                                    # another device holds the old LAN address
    monkeypatch.setattr(dlna_backend, "_tunnel_address", lambda location: location == tunnel.location,
                        raising=False)
    reachable = dlna_backend.renderer_reachable

    def the_tunnel_a_little_later(location, timeout=dlna_backend._PROBE_TIMEOUT):
        if location == tunnel.location:
            time.sleep(0.2)
        return reachable(location, timeout)
    monkeypatch.setattr(dlna_backend, "renderer_reachable", the_tunnel_a_little_later)
    assert manager.replace_queue([track(tmp_path, 1, seconds=90.0)], play=True)[0] == 1
    _playing_at(manager, tunnel, 3)
    changes, loads = manager.output_changes(), len(tunnel.called("SetAVTransportURI"))
    for _press in range(3):
        _paused(manager, tunnel)
        assert picker.play() == {"ok": True}
        wait_for(lambda: manager.latest_status.get("state") == "playing" and tunnel.state == "PLAYING",
                 timeout=15, what="playing again")
    assert manager.output_changes() - changes <= 1, manager.output_changes() - changes
    assert len(tunnel.called("SetAVTransportURI")) - loads <= 1
    assert other.location not in manager.store["output.dlna_renderer"]["locations"]
    assert not other.called("SetAVTransportURI")


def test_an_attach_that_finds_another_device_where_the_renderer_was_says_so(renderers, manager):
    """Every address the renderer is known at answers as another device (a
    phone set up again, a new UDN): the attach says so, and the address is
    no longer the renderer's — it used to say the renderer did not answer at
    all."""
    phone, other = renderers(name="Phone"), renderers(name="Another phone")
    phone.stop_answering()
    record = {**phone.record, "location": other.location, "locations": [other.location]}
    manager.store.update({"output.type": "dlna", "output.dlna_renderer": record})
    with pytest.raises(dlna_backend.DlnaAttachError, match="another device answers"):
        manager.activate("dlna", renderer=record)
    assert other.location not in (manager.store["output.dlna_renderer"].get("locations") or [])


def test_a_seek_near_the_end_on_a_renderer_that_ignores_it_teaches_nothing(
        tmp_path, proxy, renderers, nodes, quality, monkeypatch, stored):
    """N-7 near a track's end: a judged Seek in an Opus stream that the
    renderer ignores, playing on. Where it would read had it stayed is the
    clock less the distance, uncapped: capped at the track's length, it
    stopped there while the renderer played on into the Seek's window, and
    was remembered as one that seeks."""
    monkeypatch.setattr(transcode, "opus_path_for_file", lambda path, q, ss=0.0: path)
    quality["q"] = "opus_96"
    r = renderers(sink=PHONE_SINK)
    r.opus_seek = "ignores"
    q = CanonicalQueue()
    q.replace([track(tmp_path, 1, seconds=20.0)])
    n = nodes(q, r)
    assert n.backend.select(1)
    wait_for(lambda: n.last.state == "playing" and n.last.position >= 4
             and getattr(n.backend, "_heard_on_load", 2) >= 2, what="playing past 4 s, heard twice")
    assert n.backend.seek(int(n.last.position) + 8)
    wait_for(lambda: r.called("Seek"), what="the Seek")
    wait_for(lambda: r.state == "STOPPED", timeout=25, what="the track played out")
    assert (stored.get("output.dlna_opus_seek") or {}).get(r.udn.lower()) is not True


# -- the review's third round ----------------------------------------------------


def test_a_seek_never_seen_landing_at_the_queues_end_is_over_when_the_track_starts_again(
        tmp_path, proxy, renderers, nodes, quality, monkeypatch, stored):
    """N-7 at the end of the queue: a judged Seek in an Opus stream to the
    last track's very end, which the renderer takes and so stops before any
    reading shows it there. Play starts the track over, and the Seek went
    with the play-through it was made in. It used to wait on into the new
    one, whose readings near 0 never landed it: at its deadline the stream
    loaded again from the clock's second, and the renderer — one that
    seeks — was remembered as one that does not."""
    monkeypatch.setattr(dlna_backend, "_PENDING_PLAY_DEADLINE", 3.0)
    r, n = _opus_on(tmp_path, monkeypatch, quality, renderers, nodes)
    assert n.backend.seek(90)
    wait_for(lambda: r.state == "STOPPED" and n.last.state == "stopped", what="the queue's end")
    loads = len(r.called("SetAVTransportURI"))
    assert n.backend.play()
    wait_for(lambda: r.state == "PLAYING" and n.last.state == "playing" and n.last.position >= 5,
             timeout=15, what="playing again past the Seek's deadline")
    assert len(r.called("SetAVTransportURI")) == loads and "ss=" not in r.uri, r.uri
    assert (stored.get("output.dlna_opus_seek") or {}).get(r.udn.lower()) is not False


def test_a_renderer_picked_again_where_its_address_went_dark_says_it_connects_once(
        tmp_path, proxy, renderers, manager, switch, quality, swallowing):
    """A renderer picked again in the Output picker while its address is
    dark — it stopped, so nothing has failed there — and it answers at
    another: the wait on its address and the attach where it answers are
    one "connecting". It was said, taken back ("Stopped at … — Play resumes
    here" for a moment) and said again."""
    here, there = _attached_at_one_of_two(tmp_path, manager, renderers)
    manager.backend().stop()
    wait_for(lambda: manager.latest_status.get("state") == "stopped" and here.state == "STOPPED", what="stopped")
    here.stop_answering()
    swallowing(here.port)
    seen = _statuses(manager)
    switch(type="dlna", renderer=manager.store["output.dlna_renderer"])
    words = [data.get("connecting") for data in seen]
    first, last = words.index("Phone"), len(words) - 1 - words[::-1].index("Phone")
    assert all(word == "Phone" for word in words[first:last + 1]), words
    assert words[-1] is None and manager.active._renderer["location"] == there.location


def test_a_connecting_word_whose_first_status_fails_is_taken_back(manager):
    """A status that fails as "connecting" is first said still lets the word
    go: kept, every later status said the output was connecting, and both
    transport buttons refused taps until a restart."""
    push, calls = manager._push_status, []

    def failing_first(data):
        calls.append(data)
        if len(calls) == 1:
            raise RuntimeError("the status bus failed")
        push(data)
    manager._push_status = failing_first
    with pytest.raises(RuntimeError, match="the status bus failed"):
        with manager._connecting_to("Phone"):
            pass
    manager._push_status = push
    push({**manager.latest_status, "state": "stopped"})
    assert "connecting" not in manager.latest_status, manager.latest_status


# -- DLNA-N3: a Seek that did not land -------------------------------------------


def _said(n: Node):
    """The first status that carried the node's words, and its index."""
    return next((i, s) for i, s in enumerate(n.statuses) if s.extra.get("seek_missed"))


def _words(n: Node) -> bool:
    return any(s.extra.get("seek_missed") for s in n.statuses)


def _told(status) -> str:
    m = status.extra["seek_missed"]
    return f"{m['title']}: {m['text']}"


def test_a_start_that_never_lands_is_said_and_the_renderer_followed_at_once(
        tmp_path, proxy, renderers, nodes, quality, monkeypatch, caplog):
    """N-19: a start at a second — an output switch's spot — whose Seek the
    renderer answers and never takes, playing from the top. Past the Seek's
    deadline the node says so, naming where the renderer plays, and the
    clock takes that reading at once — it ran on from the spot until then.
    Nothing of it is said as the renderer's own move (2026-11-01: "moved
    slot 1 from 105 s to 19 s by itself"), and the status that says it
    marks the fall as a correction."""
    monkeypatch.setattr(dlna_backend, "_PENDING_PLAY_DEADLINE", 3.0)
    r = renderers(name="Study DAC")
    r.seek_delay = 3600.0                         # answered, never taken
    q = CanonicalQueue()
    q.replace([track(tmp_path, 1, seconds=90.0)])
    n = nodes(q, r)
    n.backend.resume_at(1, 35.0)
    with caplog.at_level("INFO", logger="playback.dlna_backend"):
        assert n.backend.play()
        wait_for(lambda: r.called("Seek"), what="the start sought")
        wait_for(lambda: _words(n), timeout=10, what="the words")
    i, said = _said(n)
    assert said.extra.get("corrected") and said.position < 10, said
    assert _told(said) == ("'Study DAC' started the track from the top: not at 00:35 — it is at "
                           f"{format_time(said.position)}")
    assert not any(s.extra.get("error") for s in n.statuses)
    before = [s.position for s in n.statuses[:i] if s.state == "playing"]
    assert before and min(before) >= 35, before
    wait_for(lambda: n.last.state == "playing" and abs(n.last.position - r.position()) <= 1.5,
             timeout=3, what="the node at the renderer's second")
    assert "did not land the Seek to 35 s" in caplog.text and "by itself" not in caplog.text


def test_a_seek_of_the_persons_that_never_lands_says_where_the_renderer_plays(
        tmp_path, proxy, renderers, nodes, quality, monkeypatch, caplog):
    """The person's seek, answered and never taken: the same words at its
    deadline, the renderer's reading taken at once — never the renderer's
    move."""
    monkeypatch.setattr(dlna_backend, "_PENDING_PLAY_DEADLINE", 3.0)
    r = renderers(name="Study")
    q = CanonicalQueue()
    q.replace([track(tmp_path, 1, seconds=90.0)])
    n = nodes(q, r)
    assert n.backend.select(1)
    wait_for(lambda: n.statuses and n.last.state == "playing" and n.last.position >= 2,
             what="playing past 2 s")
    r.seek_delay = 3600.0
    with caplog.at_level("INFO", logger="playback.dlna_backend"):
        assert n.backend.seek(60)
        wait_for(lambda: _words(n), timeout=10, what="the words")
    _i, said = _said(n)
    assert said.extra.get("corrected") and said.position < 15, said
    assert _told(said) == f"'Study' did not go to 01:00: it is at {format_time(said.position)}"
    assert "by itself" not in caplog.text


def test_a_start_the_renderer_refuses_is_said_with_the_top_it_plays_from(
        tmp_path, proxy, renderers, nodes, quality):
    """A start at a second whose Seek the renderer refuses: it plays from the
    top, and the person is told so at once — the clock's fall marked as a
    correction, as for a Seek that did not land."""
    r = renderers(name="Study DAC")
    r.faults["Seek"] = [701]
    q = CanonicalQueue()
    q.replace([track(tmp_path, 1, seconds=90.0)])
    n = nodes(q, r)
    n.backend.resume_at(1, 35.0)
    assert n.backend.play()
    wait_for(lambda: _words(n), timeout=10, what="the words")
    _i, said = _said(n)
    assert said.extra.get("corrected") and said.position < 3, said
    assert _told(said) == "'Study DAC' refused the start at 00:35: it plays from the top"


def test_a_correction_of_a_switchs_start_never_splits_the_listen_it_carried(
        tmp_path, proxy, renderers, manager, switch, quality, heard, monkeypatch):
    """N-19's listen: the phone at 41 s, switched to a renderer that plays the
    track from the top and never takes the start's Seek; paused near its top,
    the node corrects its clock there. Play goes on with the listen the
    switch carried — a correction under 5 s made the next play a play from
    the top, and wrote the listen before it as a skip."""
    monkeypatch.setattr(dlna_backend, "_PENDING_PLAY_DEADLINE", 3.0)
    listened, lastfm = heard
    a, b = renderers(name="Phone"), renderers(name="Study DAC")
    _on_renderer(manager, a, [track(tmp_path, 1, seconds=90.0)])
    _playing_at(manager, a, 1)
    assert manager.backend().seek(40)
    _playing_at(manager, a, 41)
    b.seek_delay = 3600.0
    switch(type="dlna", renderer=b.record)
    wait_for(lambda: b.called("Seek"), what="the start sought")
    wait_for(lambda: b.position() >= 1.0, what="playing from its top")
    assert manager.backend().pause()
    wait_for(lambda: manager.latest_status.get("state") == "paused"
             and manager.latest_status.get("position", 99) < 5, timeout=10, what="the correction")
    corrected = manager.latest_status["position"]
    import routers.player as player_router
    assert player_router.play()["ok"]
    wait_for(lambda: manager.latest_status.get("state") == "playing"
             and manager.latest_status.get("position", 0) >= corrected + 1.5, what="playing on")
    assert listened == []
    assert lastfm == [("update_now_playing", "Track 1")]


def _anything_said(n: Node, statuses=None) -> list:
    """Every status that told or marked anything of a Seek: its slot, state,
    second and what it carried."""
    return [(s.queue_index, s.state, round(s.position, 1), s.extra.get("seek_missed") or s.extra.get("error"),
             s.extra.get("corrected"))
            for s in (n.statuses if statuses is None else statuses)
            if s.extra.get("seek_missed") or s.extra.get("error") or s.extra.get("corrected")]


def test_a_start_confirmed_late_waits_for_its_seek_from_when_it_goes(
        tmp_path, proxy, renderers, nodes, quality, monkeypatch, caplog):
    """A Play the renderer is seen acting on only seconds late (its position
    probe, its transport saying STOPPED), its start's Seek answered and
    landed 3 s after it went, the deadline at 4 s: the Seek's wait counts
    from when it went, so nothing is said — counted from the Play, it was
    over before the Seek was answered, and called a start that did not land."""
    monkeypatch.setattr(dlna_backend, "_PENDING_PLAY_DEADLINE", 4.0)
    r = renderers(name="Study DAC")
    r.notifying = False
    r.says_stopped = True
    r.slow["Seek"] = 3.0
    q = CanonicalQueue()
    q.replace([track(tmp_path, 1, seconds=90.0)])
    n = nodes(q, r)
    n.backend.resume_at(1, 35.0)
    with caplog.at_level("INFO", logger="playback.dlna_backend"):
        assert n.backend.play()
        wait_for(lambda: r.called("Seek"), timeout=15, what="the start sought")
        wait_for(lambda: r.position() >= 37, timeout=15, what="the Seek landed")
        wait_for(lambda: abs(n.last.position - r.position()) <= 1.5, timeout=5, what="the node there")
    assert not _anything_said(n)
    assert "did not land" not in caplog.text


def test_a_start_refused_after_its_load_was_replaced_says_nothing_of_the_new_one(
        tmp_path, proxy, renderers, nodes, quality):
    """Slot 1 started at 35 s; its Seek is refused 1.5 s after it went, and
    meanwhile the person tapped slot 2, which plays. The answer belongs to
    the load it was made on: nothing of it is said on slot 2, nor its clock
    set back."""
    r = renderers(name="Study DAC")
    r.slow["Seek"] = 1.5
    r.faults["Seek"] = [701]
    q = CanonicalQueue()
    q.replace([track(tmp_path, 1, seconds=90.0), track(tmp_path, 2, seconds=90.0)])
    n = nodes(q, r)
    n.backend.resume_at(1, 35.0)
    assert n.backend.play()
    wait_for(lambda: r.called("Seek"), what="the start sought")
    assert n.backend.select(2)
    wait_for(lambda: r.faults["Seek"] == [], what="the Seek answered")
    wait_for(lambda: n.statuses and n.last.queue_index == 2 and n.last.state == "playing"
             and n.last.position >= 2.5, what="slot 2 playing past the answer")
    assert not _anything_said(n)


def test_the_words_of_a_seek_that_did_not_land_leave_with_their_track(
        tmp_path, proxy, renderers, nodes, quality, monkeypatch):
    """The person's seek on slot 1 never lands: said, on slot 1. The renderer
    plays slot 1 out and moves on to slot 2 by its armed next: slot 2 carries
    none of it — standing, the words greeted every tab opened later with a
    moment long past."""
    monkeypatch.setattr(dlna_backend, "_PENDING_PLAY_DEADLINE", 3.0)
    r = renderers(name="Study")
    q = CanonicalQueue()
    q.replace([track(tmp_path, 1, seconds=12.0), track(tmp_path, 2, seconds=30.0)])
    n = nodes(q, r)
    assert n.backend.select(1)
    wait_for(lambda: n.statuses and n.last.state == "playing" and n.last.position >= 1, what="playing")
    r.seek_delay = 3600.0
    assert n.backend.seek(10)
    wait_for(lambda: _anything_said(n), timeout=10, what="the words")
    mark = len(n.statuses)
    wait_for(lambda: n.last.queue_index == 2 and n.last.state == "playing" and n.last.position >= 2,
             timeout=25, what="slot 2 playing")
    assert not [w for w in _anything_said(n, n.statuses[mark:]) if w[0] == 2]


def test_a_start_seek_that_gets_no_answer_is_waited_for_as_sent(
        tmp_path, proxy, renderers, nodes, quality, caplog):
    """The start's Seek gets no answer within the command's window, and the
    renderer acts on it after: nothing refused it. It is waited for as sent,
    and its landing is seen — never "it plays from the top", then the
    landing named the renderer's own move."""
    r = renderers(name="Study DAC")
    r.slow["Seek"] = 12.0
    q = CanonicalQueue()
    q.replace([track(tmp_path, 1, seconds=90.0)])
    n = nodes(q, r)
    n.backend.resume_at(1, 35.0)
    with caplog.at_level("INFO", logger="playback.dlna_backend"):
        assert n.backend.play()
        wait_for(lambda: r.called("Seek"), what="the start sought")
        wait_for(lambda: r.position() >= 37, timeout=25, what="the renderer at the second")
        wait_for(lambda: abs(n.last.position - r.position()) <= 1.5, timeout=5, what="the node there")
    assert not _anything_said(n)
    assert "by itself" not in caplog.text and "got no answer" in caplog.text


_SEEK_MISSED_DRIVER = r"""
const fs = require('node:fs'), vm = require('node:vm');
const shell = fs.readFileSync(process.argv[process.argv.length - 1], 'utf8');
const start = shell.indexOf("  // A Seek of the node's that left a renderer elsewhere");
const end = shell.indexOf('  // Surface a queue/play failure', start);
if (start < 0 || end < 0) throw new Error('the seek_missed block was not found');
const handlers = [], toasts = [];
const document = { addEventListener: (t, f) => { if (t === 'np-update') handlers.push(f); } };
const notices = { toast: t => toasts.push(t) };
const escapeProfileHtml = s => String(s).replace(/[&<>"']/g, c => `&#${c.charCodeAt(0)};`);
vm.runInNewContext(shell.slice(start, end), { document, notices, escapeProfileHtml });
for (const st of JSON.parse(fs.readFileSync(0, 'utf8'))) handlers.forEach(h => h({ detail: st }));
process.stdout.write(JSON.stringify(toasts.map(t => [t.title, t.text])));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_a_tab_toasts_each_seek_it_sees_missed_once_and_none_it_connects_with():
    """The page's side: the status a tab connects with paints nothing (a
    moment long past); each id after it toasts once, its renderer's name
    escaped; a status without one, and the same id again, say nothing."""
    import json
    import subprocess
    old = {"id": "a", "title": "'Study' did not go to 01:00", "text": "it is at 00:45"}
    new = {"id": "b", "title": "'<Study>' started the track from the top", "text": "not at 01:24 — it is at 00:19"}
    ticks = [{"seek_missed": old}, {"seek_missed": old}, {}, {"seek_missed": new}, {"seek_missed": new}, {},
             {"seek_missed": new}]
    out = subprocess.run(["node", "-e", _SEEK_MISSED_DRIVER, "--",
                          str(BACKEND / "static" / "app-shell.js")],
                         input=json.dumps(ticks).encode(), capture_output=True, check=True)
    assert json.loads(out.stdout) == [["&#39;&#60;Study&#62;&#39; started the track from the top",
                                       "not at 01:24 — it is at 00:19"]]


@pytest.mark.parametrize("answer", [5.0, 12.0], ids=["answered-late", "never-answered"])
def test_a_start_the_renderer_takes_at_once_is_no_missed_start_however_late_its_answer(
        tmp_path, proxy, renderers, nodes, quality, monkeypatch, caplog, answer):
    """A renderer that acts on the start's Seek as it receives it, and
    answers 5 s later — or past the command's window, never. Its landing is
    seen; a wait stamped again at the answer, or at the timeout, put the
    second it was already past outside the landing window, and at the
    deadline said "started the track from the top" of a start that went
    where it was asked."""
    monkeypatch.setattr(dlna_backend, "_PENDING_PLAY_DEADLINE", 3.0)
    r = renderers(name="Study DAC")
    r.slow["Seek"], r.acts_first = answer, {"Seek"}
    q = CanonicalQueue()
    q.replace([track(tmp_path, 1, seconds=90.0)])
    n = nodes(q, r)
    n.backend.resume_at(1, 35.0)
    with caplog.at_level("INFO", logger="playback.dlna_backend"):
        assert n.backend.play()
        wait_for(lambda: r.called("Seek"), what="the start sought")
        wait_for(lambda: "Seek" in r.answered or "got no answer" in caplog.text, timeout=15,
                 what="the Seek's answer, or its end")
        wait_for(lambda: n.last.state == "playing" and n.last.position >= 35 + answer + 4.5, timeout=10,
                 what="past the deadline after the answer")
    assert not _anything_said(n)
    assert abs(n.last.position - r.position()) <= 1.5
