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
describe the machine, not the moment:
- CUDA:  >=7.5 GB VRAM -> full, >=5.5 GB -> standard, else lite
- MPS:   >=23 GB unified -> full, >=15 GB -> standard, else lite
- CPU-only -> lite (bulk analysis at 30-100x GPU time is not a product)
A machine with <12 GB system RAM is demoted one tier: on CUDA the
whole-track audio buffers live in system RAM, and the model set +
Postgres + OS don't fit a full/standard working set below that.

`ml_available` is the separate axis: whether torch imports at all. It is
False on nodes where the ML wheels do not exist for the platform (Intel
macOS — PyTorch stopped at 2.2.2 there, see backend/requirements.txt), and
such a node is lite by tier anyway. Every ML-gated property folds it in, so
one check keeps a torch-less install off the model paths even when
SAUTIUM_PROFILE forces a tier the machine cannot honour.
"""

import functools
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

PROFILES = ("full", "standard", "lite")

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
    accel_memory_gb: float       # total VRAM / unified memory / RAM
    ram_gb: float
    cores: int
    ml_available: bool           # torch imports — the model stack can run

    @property
    def prewarm_keys(self) -> tuple:
        if not self.ml_available:
            return ()
        return _PREWARM[self.name]

    @property
    def local_analysis(self) -> bool:
        """Bulk library analysis (embeddings + features) is a product on
        this machine. False on lite — analysis arrives via P2P import."""
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
    def background_enrichment_default(self) -> bool:
        """Default for enrichment.background_enabled when the user never
        set the key. An explicit user_settings row always wins."""
        return self.name != "lite"

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
        the event loop on small machines."""
        if self.device != "cpu":
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
            "source": self.source,
            "detected": {
                "device": self.device,
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
                "stream_enrich_mode": self.stream_enrich_mode,
                "io_workers": self.io_workers,
            },
        }


def _total_ram_gb() -> float:
    """Total system RAM without hard-requiring psutil (the Docker image
    doesn't ship it; the desktop venv does)."""
    try:
        import psutil
        return psutil.virtual_memory().total / 1e9
    except ImportError:
        pass
    try:
        # POSIX (Linux container, macOS)
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1e9
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
        return status.ullTotalPhys / 1e9
    except Exception:
        logger.warning("RAM detection failed — assuming 16 GB")
        return 16.0


def _detect_hardware() -> tuple:
    """(device, total accel memory GB, total RAM GB, cores, torch present)."""
    ram_gb = _total_ram_gb()
    cores = os.cpu_count() or 4
    try:
        import torch
        from device import get_device
        device = get_device()
    except Exception:
        # torch-less install — every ML feature is unavailable anyway.
        return "cpu", ram_gb, ram_gb, cores, False
    if device == "cuda":
        _free, total = torch.cuda.mem_get_info()
        accel_gb = total / 1e9
    else:
        # MPS unified memory == system RAM; CPU works out of RAM anyway.
        accel_gb = ram_gb
    return device, accel_gb, ram_gb, cores, True


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
    device, accel_gb, ram_gb, cores, ml_available = _detect_hardware()
    configured, source = _configured_profile()
    if configured == "auto":
        name = _auto_tier(device, accel_gb, ram_gb)
        source = "auto" if source != "env" else "env"
    else:
        name = configured
    profile = HardwareProfile(
        name=name, source=source, device=device,
        accel_memory_gb=accel_gb, ram_gb=ram_gb, cores=cores,
        ml_available=ml_available,
    )
    logger.info(
        "Hardware profile: %s (%s; %s %.1fGB accel, %.1fGB RAM, %d cores, "
        "ml=%s) — prewarm=%s local_analysis=%s stream=%s",
        profile.name, profile.source, profile.device, profile.accel_memory_gb,
        profile.ram_gb, profile.cores, "yes" if ml_available else "no",
        ",".join(profile.prewarm_keys) or "none",
        profile.local_analysis, profile.stream_enrich_mode,
    )
    return profile
