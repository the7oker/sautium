# HQPlayer Desktop manuals

The official user manuals are Signalyst's copyrighted documents and are not
redistributed in this repository. Get them from the vendor:

- **HQPlayer Desktop 5** — User Manual, version 5.16.0 (63 pages). Ships with
  the HQPlayer Desktop 5 installation; Signalyst: https://www.signalyst.com/
- **HQPlayer Desktop 6** — User Manual, version 6.0.0 (65 pages). Ships with
  the HQPlayer Desktop 6 installation; same source.

What Sautium keeps in-tree is its own, independently written material:

- `HQPLAYER_KNOWLEDGE_BASE.md` — a structured reference for people (filters,
  modulators, shapers, rates, per-scenario recommendations), compiled from
  the HQPlayer 5 manual; the assistant reads the live lists from HQPlayer,
  not this file.
  Checked 2026-09-12 against both manuals: no manual prose appears verbatim;
  the only overlap is filter names and genre labels inside the tables.
- `HQPLAYER_INTEGRATION.md` — the control protocol as Sautium uses it.
- `../HQPLAYER_QUICKSTART.md`, `../DSP_CONTROLS_SUMMARY.md`.

Version notes that matter to Sautium:

- The control protocol is the same on HQPlayer 5 and 6, so one client
  implementation talks to either (`HQPLAYER_INTEGRATION.md`, "SDK Version").
- Two fields of recent builds — a per-filter description (HQPlayer 6) and
  `process_speed` in Status (since 5.17.0) — that Sautium uses when present
  and degrades without (`HQPLAYER_INTEGRATION.md`, "Additions of recent
  builds"); `process_speed` is what the DSP load measurements are made of.
- HQPlayer Embedded 6 speaks the same protocol (verified 2026-09-27, engine
  6.2.3); its library is configured and scanned in its own web interface.
