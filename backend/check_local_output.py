#!/usr/bin/env python3
"""
Local-output bring-up check (built-in player, Phase 1).

Run NATIVELY on the machine whose devices you want to validate (Windows /
macOS) BEFORE trusting the engine with real listening:

    SD_ENABLE_ASIO=1 python check_local_output.py           # list devices
    python check_local_output.py --play                      # default device
    python check_local_output.py --play --device "Windows WASAPI::Speakers"
    python check_local_output.py --play --exclusive          # WASAPI exclusive

--play renders 2 s of a −20 dBFS sine at 44100 and then 96000 Hz through
the selected device (both shared-mode fallback and the exclusive path are
exercised), which is exactly the open→feed→rate-switch sequence the engine
performs at track boundaries.
"""

import argparse
import os
import sys
import time

if "SD_ENABLE_ASIO" not in os.environ:
    os.environ["SD_ENABLE_ASIO"] = "1"

import numpy as np
import sounddevice as sd


def list_devices() -> None:
    hostapis = sd.query_hostapis()
    print(f"PortAudio {sd.get_portaudio_version()[1]}")
    for i, api in enumerate(hostapis):
        print(f"\n[{api['name']}]")
        for dev_index, dev in enumerate(sd.query_devices()):
            if dev["hostapi"] != i or dev["max_output_channels"] < 1:
                continue
            default = " (default)" if dev_index == api["default_output_device"] else ""
            print(f"  {dev['name']}  ch={dev['max_output_channels']} "
                  f"rate={dev['default_samplerate']:.0f}{default}")


def resolve(device_id: str | None) -> int | None:
    if device_id is None:
        return None
    hostapi, _, name = device_id.partition("::")
    hostapis = sd.query_hostapis()
    for dev_index, dev in enumerate(sd.query_devices()):
        if (dev["max_output_channels"] >= 1
                and hostapis[dev["hostapi"]]["name"] == hostapi
                and dev["name"] == name):
            return dev_index
    sys.exit(f"device not found: {device_id}")


def play_tone(device, rate: int, exclusive: bool) -> None:
    t = np.arange(int(2.0 * rate)) / rate
    tone = (0.1 * np.sin(2 * np.pi * 440.0 * t) * (2 ** 31 - 1)).astype(np.int32)
    frames = np.column_stack([tone, tone])
    extra = sd.WasapiSettings(exclusive=True) if exclusive else None
    label = f"rate={rate} exclusive={exclusive}"
    try:
        sd.play(frames, samplerate=rate, device=device, extra_settings=extra)
        sd.wait()
        print(f"  OK   {label}")
    except Exception as e:
        print(f"  FAIL {label}: {e}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--play", action="store_true")
    ap.add_argument("--device", help='"{hostapi}::{name}" from the listing')
    ap.add_argument("--exclusive", action="store_true")
    args = ap.parse_args()

    list_devices()
    if not args.play:
        return
    device = resolve(args.device)
    print(f"\nplaying test tones on {args.device or 'default output'}:")
    for rate in (44100, 96000):
        play_tone(device, rate, args.exclusive)
        time.sleep(0.3)


if __name__ == "__main__":
    main()
