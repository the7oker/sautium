"""Hardware profile resolution — full / standard / lite.

One place that answers "how much machine is this node" and expands the
answer into the effective feature set consumed at the existing seams
(model pre-warm list, analysis availability, stream-enrichment mode,
pool sizes). See docs/design/HARDWARE-TIERS.md for the tier matrix.

Selection is AUTOMATIC (Valerii, 2026-07-10: no manual picker — the
detection must be good enough on its own). The only override is the
SAUTIUM_PROFILE env var, kept for diagnostics and for simulating a
weaker machine (mirrors SAUTIUM_ACCEL_MEMORY_GB). Resolution happens
once per process.

Auto-detection keys off TOTAL accelerator memory, not free — tiers
describe the machine, not the moment. Every figure is in GiB, the unit
the box and the spec sheet use: a "16 GB" card is 16 GiB, and printing
it as 17.2 decimal GB made the owner ask where the number came from.
- CUDA:  >=7.5 GiB VRAM -> full, >=5.5 GiB -> standard, else lite
- MPS:   >=23 GiB unified -> full, >=15 GiB -> standard, else lite
- CPU-only -> lite (a CPU runs the sonic pass at a second or two per minute
  of audio, and the instrument tagger at three to nine times that — fewer
  torch threads, the larger the factor: lite keeps the tagger behind the
  owner's switch, off by default)
A machine with <12 GB system RAM is demoted one tier: on CUDA the
whole-track audio buffers live in system RAM, and the model set +
Postgres + OS don't fit a full/standard working set below that.

`models` is the separate axis — whether the model stack runs here — and
`ml_available` is its one reading (`models == 'on'`):
- 'absent': torch is not installed. The ML wheels do not exist for the
  platform (Intel macOS — PyTorch stopped at 2.2.2 there, see
  backend/requirements-ml.txt), or the image was built without them
  (Dockerfile.mac's MODELS=0);
- 'off': installed, and the owner switched "AI models on this machine" off
  (user_settings `compute.models`, PI-NODE-2B) — the choice a small board is
  born with (db_migrate's birth preset). Read once per process, here, before
  anything imports torch: an off node never loads it (181 MiB at rest on a
  Pi 5), and a change of the switch takes a restart;
- 'on': everything else.
A node whose models are off or absent is lite by tier (no accelerator is
looked for). Every ML-gated property folds `ml_available` in, so one check
keeps such a node off the model paths even when SAUTIUM_PROFILE forces a tier
the machine cannot honour. Lite with no models is what a person meets as
"Nano" (`nano`): it analyses nothing, and sound features arrive from other
nodes.
"""

import functools
import importlib.util
import logging
import os
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

PROFILES = ("full", "standard", "lite")
MODELS_SETTING = "compute.models"
_GIB = 1024 ** 3

_IO_WORKER_CAP = {"full": 16, "standard": 8, "lite": 4}
_PREFETCH_CAP = {"full": 4, "standard": 4, "lite": 2}
_PREWARM = {
    # Discovery degrades per-channel when a model is cold (is_loaded ->
    # "loading" + kick_load), so a shorter pre-warm list costs first-query
    # latency on the dropped models, never correctness. NLLB is only used
    # for Cyrillic CLAP queries — on standard it warms on first use.
    "full": ("clap", "enrichment", "lyrics", "translate"),
    "standard": ("clap", "enrichment", "lyrics"),
    "lite": (),
}


@dataclass(frozen=True)
class HardwareProfile:
    name: str                    # full | standard | lite
    source: str                  # 'auto' | 'user' | 'env'
    device: str                  # cuda | mps | cpu
    accel_name: Optional[str]    # the GPU / chip as the driver names it
    accel_memory_gb: float       # total VRAM / unified memory / RAM, GiB
    ram_gb: float                # GiB
    cores: int
    models: str = "on"           # on | off (the owner's switch) | absent (not installed)

    @property
    def ml_available(self) -> bool:
        """The model stack runs here: installed, and not switched off."""
        return self.models == "on"

    @property
    def nano(self) -> bool:
        """The smallest node: lite, and no model runs."""
        return self.name == "lite" and not self.ml_available

    @property
    def prewarm_keys(self) -> tuple:
        if not self.ml_available:
            return ()
        return _PREWARM[self.name]

    @property
    def local_analysis(self) -> bool:
        """The sonic pass runs here — the segment grid, CLAP and librosa,
        the cheap part of the analysis — on every profile with the ML
        stack, lite included (a Pi analyses a 5 000-track library in about
        a day). The instrument pass follows the owner's switch
        (routers.settings.instruments_on)."""
        return self.ml_available

    @property
    def instruments_by_default(self) -> bool:
        """The switch's value while the owner never set it
        (enrichment.instruments): on for full and standard, off for lite,
        where the tagger costs ~19 s per minute of audio on a Pi — five days
        for 5 000 tracks. A profile decides what the node computes, never
        what it keeps: off, nothing stored is removed."""
        return self.ml_available and self.name != "lite"

    @property
    def stream_enrich_mode(self) -> str:
        """'full' = as today; 'trickle' = bounded queue + drop-on-backlog +
        idle model unload, paced by listening (§2.7 HARDWARE-TIERS)."""
        if self.name != "lite" and self.device != "cpu":
            return "full"
        return "trickle"

    @property
    def translation_available(self) -> bool:
        """MADLAD ct2-int8 (~3GB RAM, CPU-only runtime): full pre-warms it,
        standard lazy-loads on the first non-ASCII sound query, lite never
        loads it — the sound scope degrades per query to English-only
        (HARDWARE-TIERS sound-scope policy)."""
        return self.ml_available and self.name != "lite"

    @property
    def unload_instruments_after_run(self) -> bool:
        """Release AST+PaSST after a bulk run / trickle idle. On CUDA-full
        the resident pair is cheap next to dedicated VRAM headroom and keeps
        re-enrichment instant. On MPS it is ALWAYS released — unified memory
        is shared with the OS, and holding tagger weights between rare runs
        is what a 24GB Mac notices as "memory never came back"."""
        if self.device == "mps":
            return True
        return self.name != "full"

    @property
    def torch_cpu_threads(self) -> int | None:
        """Intra-op thread cap for CPU inference; None = leave torch alone.
        Without it torch grabs every core and starves the decode pool +
        the event loop on small machines. An MPS node infers on the CPU
        too: the text encoder's copy there takes what MPS should not hold
        (text_embedder.encode)."""
        if self.device == "cuda":
            return None
        if self.name == "lite":
            return max(2, self.cores // 2)
        return max(2, self.cores - 2)

    @property
    def io_workers(self) -> int:
        return max(2, min(_IO_WORKER_CAP[self.name], self.cores))

    @property
    def prefetch_workers(self) -> int:
        return max(1, min(_PREFETCH_CAP[self.name], self.cores))

    def describe(self) -> dict:
        return {
            "profile": self.name,
            "nano": self.nano,
            "models": self.models,
            "source": self.source,
            "detected": {
                "device": self.device,
                "accel_name": self.accel_name,
                "accel_memory_gb": round(self.accel_memory_gb, 1),
                "ram_gb": round(self.ram_gb, 1),
                "cores": self.cores,
                "ml_available": self.ml_available,
                "auto_tier": _auto_tier(self.device, self.accel_memory_gb,
                                        self.ram_gb),
            },
            "effective": {
                "prewarm": list(self.prewarm_keys),
                "local_analysis": self.local_analysis,
                "instruments_by_default": self.instruments_by_default,
                "stream_enrich_mode": self.stream_enrich_mode,
                "io_workers": self.io_workers,
            },
        }


def cap_torch_threads(profile: Optional["HardwareProfile"] = None) -> Optional[int]:
    """Cap torch's intra-op threads at the profile's (torch_cpu_threads), the
    one place every process that runs a model does it — the backend at its
    start, the CLI's analysis commands — so a pass's pace is recorded under
    the threads the estimate reads. Returns the cap, or None where none
    applies."""
    profile = profile or resolve()
    if not (profile.ml_available and profile.torch_cpu_threads):
        return None
    import torch
    torch.set_num_threads(profile.torch_cpu_threads)
    return profile.torch_cpu_threads


def _total_ram_gb() -> float:
    """Total system RAM without hard-requiring psutil (the Docker image
    doesn't ship it; the desktop venv does)."""
    try:
        import psutil
        return psutil.virtual_memory().total / _GIB
    except ImportError:
        pass
    try:
        # POSIX (Linux container, macOS)
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / _GIB
    except (ValueError, OSError, AttributeError):
        pass
    try:
        # Windows without psutil
        import ctypes

        class _MemStatus(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        status = _MemStatus()
        status.dwLength = ctypes.sizeof(_MemStatus)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
        return status.ullTotalPhys / _GIB
    except Exception:
        logger.warning("RAM detection failed — assuming 16 GB")
        return 16.0


def models_switch() -> bool:
    """The owner's switch "AI models on this machine": on while never set."""
    from db_pool import db_query_one
    row = db_query_one("SELECT value FROM user_settings WHERE key = %(k)s", {"k": MODELS_SETTING})
    return row is None or row["value"] is not False


def _detect_hardware() -> tuple:
    """(device, accelerator name, total accel memory GiB, total RAM GiB,
    cores, models: on | off | absent)."""
    ram_gb = _total_ram_gb()
    cores = os.cpu_count() or 4
    # The switch before the import: an off node never loads torch
    if importlib.util.find_spec("torch") is None:
        return "cpu", None, ram_gb, ram_gb, cores, "absent"
    if not models_switch():
        return "cpu", None, ram_gb, ram_gb, cores, "off"
    try:
        import torch
        from device import get_device
        device = get_device()
    except Exception:
        # An install whose torch does not import — every ML feature is
        # unavailable anyway.
        return "cpu", None, ram_gb, ram_gb, cores, "absent"
    if device == "cuda":
        _free, total = torch.cuda.mem_get_info()
        return device, torch.cuda.get_device_name(0), total / _GIB, ram_gb, cores, "on"
    # MPS unified memory == system RAM; CPU works out of RAM anyway.
    name = "Apple Silicon (Metal)" if device == "mps" else None
    return device, name, ram_gb, ram_gb, cores, "on"


def _auto_tier(device: str, accel_gb: float, ram_gb: float) -> str:
    if device == "cuda":
        tier = "full" if accel_gb >= 7.5 else "standard" if accel_gb >= 5.5 else "lite"
    elif device == "mps":
        tier = "full" if accel_gb >= 23 else "standard" if accel_gb >= 15 else "lite"
    else:
        tier = "lite"
    if ram_gb < 12 and tier != "lite":
        tier = PROFILES[PROFILES.index(tier) + 1]
    return tier


def _configured_profile() -> tuple:
    """(profile-or-'auto', source): SAUTIUM_PROFILE env or auto-detect."""
    env = os.environ.get("SAUTIUM_PROFILE", "").strip().lower()
    if env in PROFILES or env == "auto":
        return env, "env"
    return "auto", "auto"


@functools.lru_cache(maxsize=1)
def resolve() -> HardwareProfile:
    """Process-lifetime profile. First call logs the decision."""
    device, accel_name, accel_gb, ram_gb, cores, models = _detect_hardware()
    configured, source = _configured_profile()
    if configured == "auto":
        name = _auto_tier(device, accel_gb, ram_gb)
        source = "auto" if source != "env" else "env"
    else:
        name = configured
    profile = HardwareProfile(
        name=name, source=source, device=device, accel_name=accel_name,
        accel_memory_gb=accel_gb, ram_gb=ram_gb, cores=cores,
        models=models,
    )
    logger.info(
        "Hardware profile: %s (%s; %s %.1fGiB accel, %.1fGiB RAM, %d cores, "
        "models=%s) — prewarm=%s local_analysis=%s stream=%s",
        profile.name, profile.source, profile.device, profile.accel_memory_gb,
        profile.ram_gb, profile.cores, models,
        ",".join(profile.prewarm_keys) or "none",
        profile.local_analysis, profile.stream_enrich_mode,
    )
    return profile
