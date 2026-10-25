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

    async def _tick(self):
        while True:
            await asyncio.sleep(0.05)
            length = self.stream_length()
            if self.state == "PLAYING" and length is not None and self.position() >= length:
                if self.next_uri and self.takes(self.next_meta):
                    self.uri, self.meta = self.next_uri, self.next_meta
                    self.next_uri = self.next_meta = ""
                    self._base, self._t0 = 0.0, time.monotonic()
                else:
                    self.state, self._base, self._t0 = "STOPPED", 0.0, None
                    self.next_uri = self.next_meta = ""
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
            if self.faults.get(action):
                raise Fault(self.faults[action].pop(0), "Current state of service prevents invoking that action")
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


def _opus_seek_node(tmp_path, proxy, renderers, nodes, quality, monkeypatch, seconds):
    """A phone renderer playing one owned track in opus_96: it reports, as
    its TrackDuration, what its decoder measured of the stream it was given."""
    monkeypatch.setattr(transcode, "opus_path_for_file", lambda path, q, ss=0.0: path)
    quality["q"] = "opus_96"
    r = renderers(sink=PHONE_SINK,
                  reports=lambda uri, didl: (didl or 40.0) - FakeRenderer.offset(uri))
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


def test_an_opus_seek_keeps_the_track_length(tmp_path, proxy, renderers, nodes, quality, monkeypatch):
    """N-13: an Opus seek reloads the stream from the target, and the phone
    reports that stream's own length (30 s of a 40-s track after ss=10). The
    track keeps the length the node knows, so the next seek lands where it
    is asked — the slider's scale used to shrink to the stream's."""
    r, n = _opus_seek_node(tmp_path, proxy, renderers, nodes, quality, monkeypatch, 40.0)
    assert n.last.length == 40
    _seek_to(r, n, 10)
    assert r.reported_length() == 30
    assert n.last.length == 40
    _seek_to(r, n, 25)
    assert n.last.length == 40 and 26 <= n.last.position <= 29


def test_a_length_only_the_renderer_knows_is_read_from_the_seek(tmp_path, proxy, renderers, nodes,
                                                                quality, monkeypatch):
    """N-13: a track whose length the node does not know takes the renderer's
    word — on an offset stream read 0-based, like RelTime: the offset plus
    the rest the decoder measured."""
    r, n = _opus_seek_node(tmp_path, proxy, renderers, nodes, quality, monkeypatch, None)
    wait_for(lambda: n.last.length == 40, what="the renderer's length")
    _seek_to(r, n, 10)
    assert n.last.length == 40


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
        for loc in found:
            await callback({"location": loc, "usn": "uuid:x::urn:schemas-upnp-org:device:MediaRenderer:1"})

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
