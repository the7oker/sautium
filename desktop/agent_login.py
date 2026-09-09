"""One sign-in driver for both CLI agents — Claude Code and OpenAI Codex.

Neither CLI needs a terminal to sign in; the console window Sautium used to
open for `/login` and `codex login` was the only interactive thing about
either flow. Measured on claude-code 2.1.266 and codex-cli 0.149 (2026-09-09):

- `claude auth login` writes two lines and waits: "Opening browser to sign
  in…" and "If the browser didn't open, visit: <URL>", then the prompt
  "Paste code here if prompted >" (no newline). The URL it PRINTS carries
  `redirect_uri=https://platform.claude.com/oauth/code/callback` — a page
  that shows the user a code to paste. The URL it hands to the BROWSER is
  the same request with `redirect_uri=http://localhost:<random>/callback`,
  so on the machine running the CLI the click on Authorize completes the
  login by itself; the printed URL and the pasted code are for a browser
  on another machine (a phone, Docker's host). The code is read from a
  stdin pipe; a wrong one exits 1 with "Login failed: …". Success prints
  "Login successful", exits 0 and stores the credentials where the chat
  turns read them (~/.claude/.credentials.json, or the macOS Keychain).
- `codex login` starts its callback server on the FIXED port 1455, prints
  the auth URL, opens the browser itself and exits 0 with auth.json
  written once the callback lands. `codex login --device-auth` prints
  `https://auth.openai.com/codex/device` plus a one-time code (15 min) and
  polls OpenAI itself — the flow for a browser that cannot reach the
  CLI's localhost (Docker, a phone). Neither reads stdin.

The driver is one process per agent, read on a thread, with a snapshot the
launcher wizard renders directly and the backend serves over
/api/settings/ai/<agent>/state; `on_change` fires from the reader thread on
every parsed change and on exit, which is what wakes the SSE clients — the
2-second credential polls both surfaces used to run are gone.
"""

from __future__ import annotations

import codecs
import logging
import re
import subprocess
import sys
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Codex device codes expire after 15 minutes; `claude auth login` would
# wait forever, and a browser tab the user closed must not keep a process
# and a callback port alive behind the settings screen.
DEADLINE_SECONDS = 15 * 60

_URL_RE = re.compile(r"https://\S+")
_DEVICE_CODE_RE = re.compile(r"^\s*([A-Z0-9]{3,8}-[A-Z0-9]{3,8})\s*$", re.MULTILINE)
_CLAUDE_CODE_PROMPT = "Paste code here"
# codex 0.153 colours its output even into a pipe (`\x1b[94m<url>\x1b[0m`);
# claude renders plain text without a TTY. Parsing sees neither.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")


def claude_login_command(exe) -> list:
    return [str(exe), "auth", "login"]


def codex_login_command(exe, device: bool) -> list:
    cmd = [str(exe), "login"]
    if device:
        cmd.append("--device-auth")
    return cmd


def native_popen_kwargs(env: dict) -> dict:
    """Popen setup for a sign-in spawned by a native process (the launcher):
    the given env, no console flash on Windows. The backend adds the Docker
    user demotion on top through the assistant runners' own spawn setup."""
    kwargs = {"env": env}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs


def parse_output(agent: str, text: str) -> dict:
    """What the CLI has told the user so far: the link to open, the device
    code to type (codex device flow), whether a pasted code is accepted
    (claude). Parsed over the whole accumulated output, not per line — the
    claude prompt ends without a newline."""
    text = _ANSI_RE.sub("", text).replace("\r", "")
    url = None
    m = _URL_RE.search(text)
    if m:
        url = m.group(0).rstrip(".,)")
    code = None
    if agent == "codex":
        cm = _DEVICE_CODE_RE.search(text)
        if cm:
            code = cm.group(1)
    return {
        "url": url,
        "code": code,
        "accepts_code": agent == "claude" and _CLAUDE_CODE_PROMPT in text,
    }


def failure_message(text: str) -> str:
    """The CLI's last word on a non-zero exit, without the paste prompt it
    shares a line with ("Paste code here if prompted > Login failed: …")."""
    text = _ANSI_RE.sub("", text).replace("\r", "")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return "The sign-in process exited without a message."
    last = lines[-1]
    if _CLAUDE_CODE_PROMPT in last and ">" in last:
        last = last.split(">", 1)[1].strip()
    return last or "The sign-in process exited without a message."


class AgentLogin:
    """One sign-in at a time for one agent. `start()` on a finished
    instance starts over; on a running one it returns the current
    snapshot, so a second click is harmless."""

    def __init__(self, agent: str, cmd: list, popen_kwargs: dict,
                 on_change: Optional[Callable[[dict], None]] = None,
                 flow: str = "browser", deadline: float = DEADLINE_SECONDS):
        self.agent = agent
        self.flow = flow
        self._cmd = list(cmd)
        self._popen_kwargs = dict(popen_kwargs)
        self._on_change = on_change
        self._deadline = deadline
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._timer: Optional[threading.Timer] = None
        self._text = ""
        self._cancelled = False
        self._timed_out = False
        self._parsed = parse_output(agent, "")
        self._error: Optional[str] = None
        self._exit_code: Optional[int] = None
        self._started_at: Optional[float] = None

    # -- state ---------------------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            return self._snapshot_locked()

    @property
    def running(self) -> bool:
        return self.snapshot()["running"]

    # -- control -------------------------------------------------------

    def start(self) -> dict:
        with self._lock:
            if self._proc is not None and self._exit_code is None:
                return self._snapshot_locked()
            kwargs = dict(self._popen_kwargs)
            # The pipes are ours, in bytes: a caller's text-mode setup
            # (the assistant runners spawn their chat turns with
            # text=True) would wrap stdout in a TextIOWrapper, which has
            # no read1 and would decode a half-received prompt.
            for key in ("text", "encoding", "errors", "universal_newlines", "bufsize"):
                kwargs.pop(key, None)
            kwargs.update(stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT)
            self._text = ""
            self._cancelled = False
            self._timed_out = False
            self._parsed = parse_output(self.agent, "")
            self._error = None
            self._exit_code = None
            self._started_at = time.time()
            self._proc = subprocess.Popen(self._cmd, **kwargs)
            self._timer = threading.Timer(self._deadline, self._expire)
            self._timer.daemon = True
            self._timer.start()
            proc = self._proc
            logger.info("%s sign-in started (%s flow, pid %s)", self.agent, self.flow, proc.pid)
        threading.Thread(target=self._read, args=(proc,), daemon=True,
                         name=f"{self.agent}-signin").start()
        return self.snapshot()

    def submit_code(self, code: str) -> None:
        """The code the platform page showed, into the CLI's stdin."""
        code = code.strip()
        if not code:
            raise ValueError("Paste the code first")
        with self._lock:
            proc = self._proc
            if proc is None or self._exit_code is not None:
                raise RuntimeError("No sign-in is in progress")
            if not self._parsed["accepts_code"]:
                raise RuntimeError("This sign-in does not take a pasted code")
            try:
                proc.stdin.write((code + "\n").encode("utf-8"))
                proc.stdin.flush()
            except OSError as e:
                raise RuntimeError("The sign-in process has already exited") from e

    def cancel(self) -> None:
        with self._lock:
            proc = self._proc
            if proc is None or self._exit_code is not None:
                return
            self._cancelled = True
        self._kill(proc)

    # -- internals -----------------------------------------------------

    def _snapshot_locked(self) -> dict:
        running = self._proc is not None and self._exit_code is None
        return {
            "agent": self.agent,
            "flow": self.flow,
            "running": running,
            "url": self._parsed["url"],
            "code": self._parsed["code"],
            "accepts_code": running and self._parsed["accepts_code"],
            "error": self._error,
            "timed_out": self._timed_out,
            "cancelled": self._cancelled,
            "exit_code": self._exit_code,
            "started_at": self._started_at,
        }

    def _expire(self) -> None:
        with self._lock:
            proc = self._proc
            if proc is None or self._exit_code is not None:
                return
            self._timed_out = True
        logger.warning("%s sign-in timed out after %ds", self.agent, self._deadline)
        self._kill(proc)

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        try:
            proc.kill()
        except OSError as e:
            logger.debug("sign-in process kill: %s", e)

    def _read(self, proc: subprocess.Popen) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        stream = proc.stdout
        reader_failed = False
        try:
            while True:
                chunk = stream.read1(4096)
                if not chunk:
                    break
                piece = decoder.decode(chunk)
                if not piece:
                    continue
                with self._lock:
                    self._text += piece
                    parsed = parse_output(self.agent, self._text)
                    changed = parsed != self._parsed
                    self._parsed = parsed
                if changed:
                    self._notify()
        except Exception:
            # Without a reader the CLI would block on a full pipe and the
            # sign-in would look alive forever: end it and say so.
            logger.exception("%s sign-in reader failed", self.agent)
            reader_failed = True
            self._kill(proc)
        rc = proc.wait()
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._exit_code = rc
            if reader_failed:
                self._error = "Sautium could not read the sign-in process — see the log."
            elif self._timed_out:
                self._error = "Sign-in timed out — start again when you are ready."
            elif self._cancelled:
                self._error = None
            elif rc != 0:
                self._error = failure_message(self._text)
            try:
                proc.stdin.close()
            except OSError:
                pass
        if self._error:
            logger.warning("%s sign-in ended: rc=%s — %s", self.agent, rc, self._error)
        else:
            logger.info("%s sign-in ended: rc=%s%s", self.agent, rc,
                        " (cancelled)" if self._cancelled else "")
        self._notify()

    def _notify(self) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change(self.snapshot())
        except Exception:
            logger.exception("%s sign-in on_change failed", self.agent)
