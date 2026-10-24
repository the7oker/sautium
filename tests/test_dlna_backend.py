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
from playback import dlna_backend  # noqa: E402
from playback.dlna_backend import DlnaBackend  # noqa: E402
from playback.queue import CanonicalQueue, QueueItem  # noqa: E402
from streaming import service as streaming_service  # noqa: E402
from streaming import transcode  # noqa: E402
from streaming.proxy import MediaProxy  # noqa: E402

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
    with 714, as the phone does. `reports`: the length its decoder reports
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

    async def _tick(self):
        while True:
            await asyncio.sleep(0.05)
            length = self.stream_length()
            if self.state == "PLAYING" and length is not None and self.position() >= length:
                if self.next_uri:
                    self.uri, self.meta = self.next_uri, self.next_meta
                    self.next_uri = self.next_meta = ""
                    self._base, self._t0 = 0.0, time.monotonic()
                else:
                    self.state, self._base, self._t0 = "STOPPED", 0.0, None
                await self._changed()

    def _avt_state(self) -> dict:
        return {"TransportState": self.state, "TransportStatus": self.status,
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
            mime = self.didl_mime(args.get("CurrentURIMetaData", ""))
            if self.sink and mime and not any(e.split(":")[2] == mime for e in self.sink):
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
            self._base = _seconds(args["Target"]) or 0.0
            self._t0 = time.monotonic() if self.state == "PLAYING" else None
        elif action == "GetTransportInfo":
            return {"CurrentTransportState": self.state, "CurrentTransportStatus": self.status,
                    "CurrentSpeed": "1"}
        elif action == "GetPositionInfo":
            rel = _hms(self.position()) if self.uri else "0:00:00"
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
        return web.Response(text=_scpd(actions, variables), content_type="text/xml")

    async def _control(self, request):
        svc = request.match_info["svc"]
        st = _SERVICES[svc][0]
        action = request.headers.get("SOAPACTION", "").strip('"').split("#")[-1]
        body = ET.fromstring(await request.read())
        element = body.find(f".//{{{st}}}{action}")
        args = {child.tag: child.text or "" for child in (element if element is not None else [])}
        self.calls.append((action, args))
        before = self._avt_state()
        try:
            out = self._act(svc, action, args)
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
            asyncio.ensure_future(self._initial(sid, svc))
        return web.Response(headers={"SID": sid, "TIMEOUT": "Second-1800"})

    async def _unsubscribe(self, request):
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
        if not callback:
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
    monkeypatch.setattr(DlnaBackend, "_quality_suffix", staticmethod(
        lambda: "" if value["q"] == "lossless" else f"?q={value['q']}"))
    return value


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
    monkeypatch.setattr(DlnaBackend, "_fetch_cover_jpeg",
                        staticmethod(lambda url: jpeg if url == fetchable else None))
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


def _tiny_png() -> bytes:
    import io
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (2, 2), (200, 100, 50)).save(out, format="PNG")
    return out.getvalue()


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
