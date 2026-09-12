# HQPlayer Desktop manuals

The official user manuals are Signalyst's copyrighted documents and are not
redistributed in this repository. Get them from the vendor:

- **HQPlayer Desktop 5** — User Manual, version 5.16.0 (63 pages). Ships with
  the HQPlayer Desktop 5 installation; Signalyst: https://www.signalyst.com/
- **HQPlayer Desktop 6** — User Manual, version 6.0.0 (65 pages). Ships with
  the HQPlayer Desktop 6 installation; same source.

What Sautium keeps in-tree is its own, independently written material:

- `HQPLAYER_KNOWLEDGE_BASE.md` — the structured reference the AI assistant
  reads (filters, modulators, shapers, rates, per-scenario recommendations).
  Checked 2026-09-12 against both manuals: no manual prose appears verbatim;
  the only overlap is filter names and genre labels inside the tables.
- `HQPLAYER_INTEGRATION.md` — the control protocol as Sautium uses it.
- `../HQPLAYER_QUICKSTART.md`, `../DSP_CONTROLS_SUMMARY.md`.

Version notes that matter to Sautium:

- The control protocol is the same on HQPlayer 5 and 6, so one client
  implementation talks to either (`HQPLAYER_INTEGRATION.md`, "SDK Version").
- HQPlayer 6 exposes two extra status fields that Sautium uses when present
  and degrades without (`HQPLAYER_INTEGRATION.md`, the HQPlayer 6 notes).
