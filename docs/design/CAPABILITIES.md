# Sautium — Capabilities

_2026-09-29 · derived from the code at commit `c5613ce` · regenerate by running
`CAPABILITIES-DOC-PROMPT.md` (private) against a newer commit._

What Sautium does, what it deliberately does not do, and what is planned.
The code is the source; the other docs are background. Where a doc and the
code disagree, the code is what this file records and § 14 names the doc.

## 1. Scope and vocabulary

- Paths are relative to `backend/` unless they start with `desktop/`, `mcp/`
  or `worker/`; routes are written in full.
- Every line carries where it is reached from: **[UI]** a control in the Web
  UI, **[AI]** an assistant tool, **[L]** the launcher, **[CLI]** a command,
  **[API]** a route with no control of its own, **[auto]** no control needed.
- **Node** — one installation, one account. **Owned** — a row in the
  `owned_files` view: a file on this node's disk or a copy in the library of
  the HQPlayer the node drives. **Phantom** — an artist, album or track the
  node knows and does not own; together they are the **Streaming library**.
- **Output** — where the sound goes (HQPlayer, local device, DLNA renderer,
  browser), chosen in the **Output picker** (More drawer › Audio output).
- Hardware tiers are Lite / Standard / Curator (`lite` / `standard` / `full`).

## 2. Library and metadata

- [auto] Indexes 11 extensions: flac, ape, wav, aiff, wv, tta, dsf, dff, mp3, ogg, m4a — `scanner.py: AUDIO_EXTENSIONS`
- [auto] Lossless is decided by extension, so an `.m4a` scans as lossy; its real codec is probed at play — `uuid_utils.py: LOSSLESS_FORMATS`, `streaming/local.py: _source_is_lossless`
- [auto] Tags read with mutagen (Vorbis, ID3, MP4): title, artist, album artist, album, genre, date, track, disc, label, catalogue number, ISRC; vinyl side notation (A1, B2) — `scanner.py: LibraryScanner.extract_metadata`
- [auto] A file without a title, or without artist and album artist, is skipped and counted — `scanner.py: import_metadata`
- [auto] Folder-aware albums: a loose mix folds into one album named after its folder, a box set splits per album tag — `album_identity.py: assign_dir_albums`
- [auto] CUE image + sheet → one virtual track per INDEX on one file; utf-8, cp1251, cp1252; multi-FILE sheets ignored — `cue_sheet.py: resolve_cue`, `parse_cue`, `read_cue_text`
- [UI][L][CLI] Scan on demand only: scan, rescan with prune, cancel; incremental; a prune refuses an empty tree (an unmounted library) — `POST /api/settings/library/scan[?prune=true]`, `/scan/cancel`; `desktop/launcher.py: _scan_library`; `cli.py: scan`; `scanner.py: prune_missing_files`
- [UI] Several copies of one album are variants of one album; the default tracklist takes each track's best copy, the variant picker pins one — `GET /api/albums/{album_id}?variant_id=`; `routers/albums.py: _PICKED_FILES`
- [auto] Best copy = lossless, then sample rate, then bit depth; ahead of it, the copy the active output opens itself — `sql_queries.py: best_rip_order`, `owned_rank`
- [UI] Editions of one release group are separate albums under one release-group page — `canon/content.py: _split_album_editions`; `GET /api/release-groups/{group_id}`
- [auto] MusicBrainz canonicalization: recordings matched by title and duration, artists renamed to the canonical name and merged, collaborations split, release groups bound — `canon/content.py: resolve_artist`, `rename_to_canonical`, `merge_collisions`, `split_collaborations`
- [auto] It needs MusicBrainz rows on the node: the local dump, or per-artist slices fetched from peers — `canon/content.py: canonicalize_pending`; `desktop/p2p/mb_slice_cycle.py: MbSliceCycle`
- [UI] Namesakes: one name mapped to several MusicBrainz artists splits the artist page — `GET /api/artists/{artist_id}?mbid=`
- [UI] Optional AI tier that picks among real MusicBrainz candidates for what the rules left — `canon/ai_canon.py: ai_canonize_stream`; `PUT /api/settings/ai/canonization`, `POST …/canonization/run`
- [auto] Covers: embedded picture → image file in the folder → Last.fm album image; stored as WebP ≤ 1024 px — `covers.py: resolve_cover_for_folder`, `TARGET_MAX_DIM`
- [auto] Phantom and HQPlayer-held albums take the Cover Art Archive front — `caa.py: fill_held_album_covers`; `GET /api/covers/caa/{rg_mbid}`
- [auto] Artist photos from the Deezer public API, namesakes settled by album titles — `deezer_photos.py: fetch_deezer_photo_url`
- [auto] Lyrics: LRCLIB (plain and synced), Genius (plain, with the owner's token) as fallback — `lrclib.py: LrclibService`; `genius.py: GeniusService`
- [API][AI] Lyrics are served parsed (`synced_lyrics` as time + text) for owned files and read by the assistant; the Web UI shows none — `GET /api/player/lyrics/{media_file_id}`; `mcp/assistant_server.py: get_lyrics`
- [auto] Audio analysis per track: CLAP 512-d on 10 s windows (12 / 16 / 24 by duration), the track vector their normalized mean — `embeddings.py: balanced_k`, `EMBEDDING_ANALYSIS_VERSION`
- [auto] Stored features: bpm, key, mode, key_confidence, energy, energy_db, brightness, dynamic_range_db, zero_crossing_rate — `models.py: AudioFeature`
- [auto] Zero-shot labels: 8 moods, vocal / instrumental, danceability — `audio_analysis.py: MOOD_LABELS`, `VOCAL_LABELS`, `DANCE_LABELS`
- [auto] Instruments: AST + PaSST ensemble over the whole track — `ensemble_instruments.py: InstrumentEnsembleTagger`
- [auto] Text embeddings (BGE-M3, 1024-d) of track metadata, lyrics, artist bios, genre descriptions — `text_embeddings.py`, `lyrics_embeddings.py`, `enrichment_embeddings.py`
- [UI] "Analyse library": audio analysis, then the text encoders; Lite skips the audio phase — `POST /api/settings/library/enrich`, `/enrich/cancel`
- [auto] Last.fm per artist: bio, top 30 tags, listeners, up to 20 similar artists (engaged artists only); genre descriptions — `lastfm.py: enrich_artist`, `backfill_similar`, `enrich_genre`
- [auto] Gender and vocalist classified from the bio — `lastfm.py: _update_artist_gender`, `_update_artist_is_vocalist`
- [UI] An album page shows a description where one exists; today they come with the cold-start picks and travel in a share file — nothing fetches or writes a new one — `routers/albums.py: _album_description`; `seed_import.py: apply_seed`
- [UI] Last.fm listening history import: walk, resume, sync, remove imported — `lastfm_history.py: start`, `remove_imported`; `POST /api/profile/lastfm/history/sync`, `/remove`
- [auto] Imported scrobbles are placed on canonical tracks and become listens (`source = 'lastfm'`) — `canon/scrobbles.py: run_pass`
- [auto] ListenBrainz listen counts rank popular tracks and the popularity sort — `routers/artists.py: get_artist`
- [UI] Background enrichment loop: lyrics → bios → genre descriptions → similars → text, lyrics, bio embeddings → canon → discography; one switch — `background_enrichment.py: _NETWORK_STEPS`, `_LOCAL_MODEL_STEPS`; `PUT /api/settings/sync`
- [UI][AI] Offline databases: MusicBrainz dump (7.5 GB archive, 21 GB of tables) and ListenBrainz statistics (22 GB archive, 2 GB of tables); download, update, delete, automatic update — `POST /api/settings/{musicbrainz|listenbrainz}/update`, `/delete`; `mb_dump_load.py: TABLES_GB`; `lb_dump_load.py`
- [UI] Artist page: albums with five sorts (release year, time listened, popularity, recently added, A–Z), Missing albums, top 5 popular tracks, similar artists — `discography.py: ALBUM_SORT_EXPR`; `PUT /api/settings/albums-sort`
- [UI] Missing albums: MusicBrainz albums and EPs the owner lacks, without compilations, live, remix, DJ-mix, demo — `discography.py: _ALLOWED_PRIMARY`, `_DISQUALIFYING_SECONDARY`
- [UI] Genre page: description, popular tracks, artists — `GET /api/genres/{genre_id}`

## 3. Search and discovery

- [UI][AI] One search engine, four result blocks (artists, albums, tracks, genres) — `GET /api/discovery/search?target=`; `discovery_engine.py: TOOLS`
- [UI] Scopes: names and titles (trigram, cross-script), artist bios, sound (text → audio), lyrics — `discovery_engine.py: TOOLS["text"]`, `["bio"]`, `["sound"]`, `["lyrics"]`
- [UI] Filters: BPM range, key, mode, vocalist, gender, danceable, energy, instruments, genre, artist — `routers/discovery.py: discovery_search`, `_engine_filters`
- [UI] "Similar to now playing" as a filter that composes with the others — `discovery_engine.py: TOOLS["seed"]`, `_SEED_SCORE`
- [API][AI] Corpus `owned | phantom | all`; the Web UI never sends it, so its searches run on `all`, owned first at equal relevance — `discovery_engine.py: _corpus_clause`, `_TIE_BREAK`
- [UI] Paging: artists and albums scroll along their row, tracks by "Show more", genres unpaged — `routers/discovery.py: discovery_search` (`limit`, `offset`)
- [UI] MusicBrainz scope: search the whole catalogue, a tap mints the artist's discography as phantoms — `GET /api/discovery/mb-search`, `POST /api/discovery/mb-mint`
- [auto] A non-ASCII sound query is translated to English on the node (MADLAD-400, CTranslate2 int8, CPU); never on Lite — `translation.py: QueryTranslator`; `hardware_profile.py: translation_available`
- [UI] "Shuffle your library": an endless mosaic of random owned albums — a way to browse, not a queue mode — `GET /api/discovery/shuffle`
- [UI] Similar tracks on Now Playing (7 rows, owned and phantom mixed); tap plays, "+" queues — `GET /api/player/similar/{track_uuid}`; `track_similarity.py: similar_tracks`
- [AI] Play similar: a fixed list from the same scorer replaces the queue — `POST /api/player/play-similar`
- [UI] Radio: a station from the track that is playing; batches of 10 seeded from the track playing at refill time, at most 4 streamed, no track twice in a station — `POST /api/player/radio/start`, `/radio/stop`; `routers/player.py: _radio_build_batch`, `_radio_played`; `playback/manager.py: set_radio_mode`
- [UI] Similar albums shelf on owned and phantom album pages — `GET /api/albums/{album_id}/similar`; `album_similarity.py: compute_similar`
- [UI] Home: Favourite artists (listening time, 90-day decay), Recommendations (nearest in sound to the last 60 days of listening, rebuilt when a listen lands), New in my collection, Listening history — `routers/home.py: get_favourite_artists`, `_rank_recommendations`, `get_new_in_library`, `get_listening_history`
- [UI] A past listening session can be played or queued again: the only restorable list — `POST /api/player/play-session`, `/queue-session`
- [UI][AI] Lyrics are searchable by meaning (BGE-M3 over lyric chunks) — `discovery_engine.py: TOOLS["lyrics"]`; `mcp/assistant_server.py: search_lyrics`

## 4. Streaming library

- [auto] Phantoms are minted from MusicBrainz discographies, Last.fm similar artists, the MusicBrainz search and the cold-start picks — `discography.py: sync_artist_discography`; `lastfm.py: _store_similar_artists`; `mb_discovery.py: mint`; `seed_import.py: apply_seed`
- [UI] The layer is the owner's switch ("Discover new"); removal is one explicit, confirmed action — `PUT /api/settings/phantoms`; `POST /api/settings/phantoms/prune`
- [UI] A phantom album or track plays and queues like an owned one — `POST /api/player/play-phantom-album`, `/play-phantom-track`, `/queue-phantom-album`, `/queue-phantom-track`
- [auto] Core ships two providers: the demo channel — a provider whose manifest carries `demo_limited`, governed by the demo policy in the next line — and the catalogue's 30 s excerpt — `streaming/service.py: init`; `streaming/base.py: ProviderManifest` (`demo_limited`); `streaming/deezer_preview.py: DeezerPreviewProvider`
- [auto] Demo policy: a track streams in full from the demo channel once (spent past 90 %), then as the excerpt; no setting turns it off — `streaming/demo.py: CONSUMED_FRACTION`, `consumed`, `link_admissible`
- [auto] An excerpt is never analysed and never a listen or a scrobble — `streaming/enrichment.py: PreviewEnricher`; `playback/tracker.py` (`item.excerpt`)
- [auto] A bring-your-own provider is a module the owner installs; core discovers it and knows nothing else about it; lossless providers are asked before lossy ones — `streaming/loader.py: load_external_providers`; `streaming/service.py: providers_preferred`
- [auto] Each track resolves to a fallback chain of providers, cached for an hour — `routers/player.py: _resolve_waterfall`, `_CHAIN_TTL_S`
- [auto] Streamed audio is analysed like a file and signed against the stream's own fingerprint, when the catalogue length confirms the recording — `streaming/enrichment.py`
- [UI] Badges: Demo on the album, 30s on an excerpt row, the streamed quality — `GET /api/player/phantom-availability/{album_id}`
- [UI] Buy: the album's or the artist's Bandcamp page, from MusicBrainz relations — `routers/albums.py: _buy_link`
- [auto] A streamed listen is tracked and scrobbled like an owned one — `playback/tracker.py`

## 5. Playback

- [auto] One canonical queue on the node; an output renders it or mirrors it — `playback/queue.py: CanonicalQueue`; `GET /api/player/playlist`
- [UI] Play replaces the queue (a track, an album, a session); "+" adds Next or End, placed by the server — `POST /api/player/play-track`, `/play-tracks`, `/play-album`, `/queue-tracks` (`position`), `/play-entities`, `/queue-entities`
- [auto] Every replace archives the previous queue as a listening session (origin album, track, radio or mix) — `playback/sessions.py: rotate_session`, `_SESSION_ORIGINS`
- [UI] Queue sheet: jump, play/pause on the playing row, long press to restart a row, drag to reorder, remove — `POST /api/player/jump`, `/reorder`, `/remove`
- [UI] Transport: play/pause, previous, next, seek on the progress bar; no stop button (the route exists) — `POST /api/player/play`, `/pause`, `/previous`, `/next`, `/seek`, `/stop`
- [API] Volume routes exist for every output; the only volume control in the Web UI is ±1 dB on the HQPlayer screen — `POST /api/player/volume`, `/volume/up`, `/volume/down`
- [auto] Switching the output keeps the queue; each slot is re-read for the new output, and a copy it cannot open is streamed — `playback/substitute.py`; `QueueItem.play`
- [auto] The queue of an engine-rendered output survives a restart, stopped — `playback/manager.py: _persist_queue`
- [auto] A queued file that leaves the library (a rescan's prune, a superseded CUE image) moves its slot to a live copy of the track, else to the track itself, streamed; the persisted queue is re-bound on restore — `playback/manager.py: rebind_files`, woken by the `media_files` delete trigger (`032_files_removed_notify.sql`); `playback/queue.py: rebind_origins`
- [auto] Status reaches every tab over one event stream — `GET /api/events`
- [auto] Listens are recorded per track identity; Last.fm scrobble after half the track or 4 minutes — `playback/tracker.py: _SCROBBLE_MIN_SECONDS`
- [auto] Lock-screen metadata and play, pause, previous, next — browser output only — `static/player.js: _mediaSession`

| Output | Gapless | Seek | Volume | Exclusive mode | Formats and transcoding | How it is found |
|---|---|---|---|---|---|---|
| **HQPlayer** `playback/hqp_backend.py` | yes (declared) | yes | dB | — | HQPlayer opens what it is handed: a `file://` path on this machine, a stream from the media proxy anywhere else; `.m4a` is transcoded to FLAC, a CUE slice is a cached FLAC cut; never lossy (`streaming/local.py: TRANSCODE_FORMATS`) | network scan — `<discover/>` on UDP 4321 (`hqp_library.py: discover`) — or Add device by address |
| **Local device** `playback/local/backend.py` | yes (declared); seamless at equal rate and channels, a short gap when they change | yes | percent, 100 = untouched samples | yes — WASAPI exclusive | ffmpeg decode to PCM; no DSD, no resampling; never lossy (`playback/local/engine.py`) | PortAudio devices over WASAPI, ASIO, Core Audio (`playback/local/devices.py: _HOSTAPIS`); a natively run node only |
| **DLNA** `playback/dlna_backend.py` | no (declared); the next track is armed with `SetNextAVTransportURI` and a renderer's own gapless advance is followed | yes | percent | — | the original bytes, the renderer decodes (`streaming/proxy.py: MIME_BY_FORMAT`); Opus 192k / 96k by choice; a CUE slice is a FLAC cut | unicast SSDP sweep of the LAN (`routers/player.py: _unicast_sweep`), or Add device by address, which keeps the renderer listed |
| **Browser** `playback/browser_backend.py` | no (declared) | yes | percent | — | the original bytes over a signed URL, the browser decodes what it can (`routers/media.py`); Opus 192k / 96k by choice | "This device": the tab that selects it becomes the renderer, a newer tab displaces it |

- [UI] Output picker: rescan, Add device by address (HQPlayer probed first), forget a device, exclusive-mode toggle, output quality — `POST /api/player/outputs/scan`, `/outputs/add`, `/outputs/dlna/remove`; `PUT /api/settings/output`
- [UI] Output quality for DLNA and the browser: Lossless, Opus 192k, Opus 96k; applies from the next track — `streaming/transcode.py: OPUS_BITRATES`

## 6. HQPlayer

Controlled:

- [UI][AI] Transport, through the mirrored queue — `playback/hqp_backend.py: HqpBackend`; `hqplayer_client.py: play`, `pause`, `stop`, `next`, `previous`, `seek`, `select_track`
- [UI] Mode (PCM / SDM), rate, filter with its 1x filter, dither / modulator, matrix profile — `POST /api/hqplayer/config` (`ConfigRequest`)
- [AI] Filter, shaper, matrix profile by name — `mcp/assistant_server.py: hqplayer_set_filter`, `hqplayer_set_shaper`, `hqplayer_set_matrix_profile`
- [UI][AI] Volume: ±1 dB steps in the Web UI within HQPlayer's `VolumeRange`, steps and an exact level for the assistant; a volume HQPlayer holds fixed (Direct SDM) rests the buttons with the reason, and a step it refuses is told under the row in its words — `POST /api/hqplayer/volume`; `hqplayer_set_volume`
- [UI] Favourite filters: starred in the filter picker, one tap to switch — `POST /api/hqplayer/favorites/filter`
- [AI] Convolution on and off — the assistant only, no control in the Web UI — `hqplayer_set_convolution`
- [AI] Parametric-EQ preset written as a Room EQ Wizard file the owner loads into the Matrix Processor — `generate_eq_preset`; `eq_generator.py: save_eq_preset`; `GET /api/eq/presets`, `/download/{filename}`
- [auto] A DSP change made mid-play resumes playback where it stood — `routers/hqplayer.py: _resume_after_dsp_change`
- [UI] Found on the network by its own discovery datagram; every HQPlayer the owner picked is kept, aliases of this machine are one — `hqp_library.py: discover`, `register`, `address_key`
- [auto] On this machine it is handed paths, anywhere else streams; nothing to configure — `playback/hqp_backend.py: _stream_mode`
- [UI][CLI] Library import: an HQPlayer on another machine lists its library (`<LibraryGet/>`), which joins the catalogue as album variants; first import previewed, then sync, rescan, forget, cancel; re-synced when its hash moved — `hqp_library.py: sync`, `forget_missing`, `request_sync`; `POST /api/settings/library/hqp-sync`, `/hqp-rescan`, `/hqp-forget`, `/hqp-sync/cancel`
- [auto] An entry HQPlayer plays that is not in the queue (its own window, another controller) is external playback from its first tick, and nothing is tracked against it; the playlist canary only logs the drift — `playback/hqp_backend.py: _playing`, `_check_drift`
- [UI][API] Benchmark: muted test noise through the settings of the current mode that no listen or earlier run measured (the other mode on request), the owner's own setting first and last, each modulator (PCM: filter) climbing from DSD256 (PCM: 8×) while it keeps up and stepping down at once where it does not (a rate a neighbouring rate answers for is not measured), the filters held back while the owner's own setting does not keep up, each source at its own rate family, a combination HQPlayer stops at Play recorded as refused, a setting whose build keeps HQPlayer silent is waited out and measured with its start time, one still building after 10 minutes recorded as never started — while HQPlayer answers, only once a setting that started earlier in the run starts again (neither asked again); the output lent for the run and everything put back — `playback/hqp_benchmark.py`; `GET`/`POST /api/hqplayer/benchmark`, `/benchmark/cancel`
- [UI] CUDA offload of an HQPlayer on another machine, said once (one here is read from its settings) — `PUT /api/hqplayer/cuda`

Read-only:

- [UI][AI] Product, version, platform, engine, the lists of modes, filters, shapers and rates, the DSP speed factor — `GET /api/hqplayer/state`; `hqplayer_client.py: get_info`, `get_state`
- [UI][API] Why a play did not play: every play intent on the HQPlayer output traced (what the slot was handed as, every command with HQPlayer's own answer, ten seconds of status, the media proxy's requests for that file) and judged — unreachable, played, too slow, another controller, a media-server error, never fetched, refused, not played; a toast, a "Why didn't it play?" tag on Now Playing, the HQPlayer screen's Diagnostics, the `hqplayer.failing` notice after three alike; HQPlayer's own log read on demand (a Desktop's file on this machine, an Embedded box's `/log` page) — `playback/hqp_diagnostics.py`; `GET /api/player/diagnostics/hqplayer`, `/{attempt_id}`, `/log`
- [UI][AI] A refused DSP change or command quoted in HQPlayer's own words — `hqplayer_client.py: HQPlayerClient.refusal`, `last_errors`; `POST /api/hqplayer/config` (`failed`)
- [auto] DSP samples: every listen leaves HQPlayer's processing speed for what it runs, keyed by build, setting and source, with a rollup per key — `playback/hqp_load.py: Sampler`, `write`
- [UI] How each filter, modulator and rate runs on this HQPlayer, as a mark in its picker (ok / tight / too slow, the speed, never a guess) — `GET /api/hqplayer/state?dsp=1` (`headroom`); `playback/hqp_load.py: headroom`

Not implemented — searched over `backend desktop mcp`, only the definitions match:

- `hqplayer_client.py: get_inputs`, `set_repeat`, `set_random`, `forward`, `backward`, `volume_mute` have no caller: no route, tool or control (`grep -rn 'set_repeat\|set_random\|get_inputs\|volume_mute'`)
- No `set_input`, no output-device or NAA switching, no controller hand-off, no metering (`grep -n '4322\|set_input\|set_output' hqplayer_client.py` → 0)

## 7. Assistant and MCP tools

- [UI] Two selectable agents, Claude Code and OpenAI Codex, each driving the same MCP servers; API providers (Anthropic, OpenAI, an OpenAI-compatible endpoint) as the alternative — `claude_code_runner.py`; `codex_runner.py`; `providers/__init__.py: _init_providers`
- **41 MCP tools** — `mcp/assistant_server.py` (`grep -c '^@mcp.tool'` → 41):

| Area | Tools |
|---|---|
| Search (7) | `search_tracks`, `search_similar`, `search_semantic`, `search_lyrics`, `search_artists`, `search_albums`, `search_genres` |
| Catalogue (2) | `get_track_info`, `get_lyrics` |
| MusicBrainz and dumps (5) | `mb_resolve`, `mb_dump_status`, `mb_dump_download`, `lb_dump_status`, `lb_dump_download` |
| Playback on the chosen output (6) | `play_track`, `play_album`, `play_similar`, `play_all`, `build_playlist`, `add_to_queue` |
| HQPlayer transport (9) | `hqplayer_play`, `_pause`, `_stop`, `_next`, `_previous`, `_get_status`, `_volume_up`, `_volume_down`, `_set_volume` |
| HQPlayer DSP (8) | `hqplayer_get_settings`, `_get_dsp_state`, `_set_filter`, `_set_shaper`, `_set_convolution`, `_list_matrix_profiles`, `_get_matrix_profile`, `_set_matrix_profile` |
| EQ (1) | `generate_eq_preset` |
| Gear (3) | `gear_advisor_report`, `gear_system_report`, `gear_add_candidate` |

- [auto] A second, read-only PostgreSQL MCP server gives the agent the database — `desktop/config_manager.py: generate_mcp_config`
- [auto] The API providers get 34 tools of their own, `execute_query` among them — `tools/definitions.py`
- [auto] Play tools take track UUIDs, so a phantom plays through them; `build_playlist` assembles a queue without starting it — `mcp/assistant_server.py: play_all`, `build_playlist`
- [auto] The `hqplayer_*` tools refuse while another output is selected — `mcp/assistant_server.py: _get_hqp`
- [UI] Chat: sessions, a streamed reply that survives a dropped connection, tiles and track rows in replies that open and play — `POST /api/chat/sessions/{id}/messages`, `GET …/stream`; `static/app-shell.js` (blocks `artist`, `album`, `tracks`)
- [auto] The assistant is told what is playing; it is not told which screen is open — `routers/chat.py: _get_player_context`
- [auto] A turn is capped at 150 s; the agent's file, shell and web tools are off — `claude_code_runner.py: DISALLOWED_TOOLS_MCP`; `codex_runner.py: _DISABLED_FEATURES`
- [UI][L] Agent sign-in without a console, from the Web UI or the wizard — `desktop/agent_login.py: AgentLogin`; `POST /api/settings/ai/claude/signin`, `/ai/codex/signin`

## 8. Gear advisor

- [UI] The audio chain: add by brand and model with autocomplete, 15 categories, statuses own / want / sell / previously owned, notes — `POST /api/profile/gear`; `routers/profile.py: VALID_STATUSES`; `GET /api/gear-models/search`
- [auto] A new model is researched by the agent with web search: specs, technologies, measured caveats, community praise and criticism — `gear_research_worker.py: _persist_specs`, `_persist_caveats`, `_persist_sentiment`
- [UI] A failed research never retries by itself; Retry and Refresh are the owner's — `POST /api/gear-models/{id}/retry-research`
- [L] Measurement registries: AutoEq headphone responses reduced to five band deviations, spinorama loudspeaker scores; refreshed by the launcher's update check — `gear_registry.py: import_autoeq`, `import_spinorama`; `POST /api/gear-models/registry/refresh`
- [UI][AI] System analysis, a pair matrix: headphone out → transducer (SPL headroom, damping, driver ceiling), line out → line in (bridging, level, gain staging), amp → speakers (headroom, load, damping), cartridge → phono stage, cartridge → tonearm — `gear_pairs.py: _pair_hp_transducer`, `_pair_line`, `_pair_speaker`, `_pair_phono`, `_pair_tonearm`; `GET /api/profile/gear/system`
- [auto] Community synergy researched per pair and shown beside the checks, never deciding the verdict — `gear_research_worker.py: research_pair`
- [UI][AI] Upgrade advisor: where owned electronics have measurably plateaued, candidates by price with what each improves; no merged score — `gear_advisor.py: _plateau_diagnosis`, `_candidates`; `GET /api/profile/gear/advisor`
- [UI] A registry entry becomes a "want" in one tap — `POST /api/profile/gear/registry/{entry_id}/want`

## 9. Network, identity, chat, sync, backup

- [auto] Identity is derived, not stored: username + password → Argon2id → Ed25519; the same pair gives the same node anywhere — `desktop/node_identity.py: derive_seed`
- [UI] Changing the name or password is a new key; the retired one signs the notice friends re-key on — `POST /api/auth/change-identity`; `desktop/node_identity.py: rotate_identity`
- [auto] Identity certificate from the Worker, by proof of work or by email; the proof is mined in the background — `desktop/p2p/birth_cert.py: request_certificate`; `desktop/p2p/identity_pow.py: pow_mine`
- [UI] Email verification, optional — `POST /api/p2p/email/send-code`, `/verify-code`
- [auto] Peers are found on the BitTorrent DHT, on the LAN (launcher only) and through the Worker's directory — `desktop/p2p/dht_service.py: DHTService`; `desktop/p2p/lan_discovery.py`; `desktop/p2p/node_hints.py`
- [auto][CLI] The router maps the peer port by UPnP; PCP only from the command line — `desktop/p2p/upnp_service.py`; `desktop/portmap.py` (`map --pcp`)
- [auto] A node nobody can reach holds wake streams to relays; any reachable node can be a relay — `desktop/p2p/p2p_manager.py: _peer_relay_manager`; `GET /api/relay/wake-stream`
- [auto] Sync pulls sealed analysis: CLAP segment bundles and audio features; the track vector is derived locally — `desktop/sync_client.py: CATEGORIES`, `_import_segments`
- [auto] Carry pushes analysis nobody asked for yet, track ↔ recording bindings with it — `desktop/p2p/sync_walk.py: _push_to_carrier`; `desktop/p2p/sync_queries.py: CARRY_CATEGORIES`
- [auto] A peer's holdings arrive as a Bloom filter — `desktop/p2p/bloom.py`; `GET /api/sync/holdings`
- [auto] Sync runs on events and a timer; there is no sync button — `desktop/p2p/sync_walk.py: dispatch_loop`, `interval_loop`
- [auto] Every record is signed by its author, batched into a Merkle tree and timestamped; an import drops what does not verify — `sign_audio.py: sign`, `stamp`; `desktop/sync_client.py: _verify_enrichment`
- [auto] MusicBrainz and ListenBrainz data travel as signed slices, per artist name and per artist MBID — `desktop/p2p/mb_slice_queries.py`; `desktop/p2p/lb_slice_queries.py`
- [UI] Sync & P2P settings: sharing, background enrichment, re-analysis of synced audio, relay role, diagnostics, sync interval, rare-artist keys, carry budget — `PUT /api/settings/sync`
- [UI] Friends by invite code, invite link (token with rights, uses, expiry) or email; rename, pin, block, delete — `POST /api/p2p/friends/add`; `POST /api/p2p/tokens`; `POST /api/p2p/invite-by-email`; `PATCH /api/p2p/friends/{id}`
- [UI] Chat is end-to-end encrypted text, delivered directly or through a relay — `desktop/p2p/chat_service.py: encrypt_message`; `POST /api/relay/forward`
- [auto] The admission gate prices strangers' requests in work; it ships in `shadow` — computed and logged, nothing charged — `desktop/p2p/pricing.py: Pricer`; `routers/settings.py: _DEFAULTS` (`p2p.gate_mode`)
- [UI] Support diagnostics, on by default, one switch: content-free event reports, and a bundle only on a signed, single-use warrant; the launcher sends, a Docker node only records — `desktop/p2p/p2p_manager.py: _diag_report`, `_diag_handle_warrant`; `desktop/p2p/diag_protocol.py: SCOPES`
- [L][CLI] Backup: one encrypted `.sbk` per node, keyed by the account password; MusicBrainz and ListenBrainz tables left out — `python -m backup create|inspect|restore|selftest`; `desktop/node_backup.py: BackupWriter`, `EXCLUDED_TABLE_PATTERNS`
- [L][CLI] Restore replaces a database that holds own data only on `--replace`, keeping the old one — `desktop/node_backup.py: restore_database`
- [L][CLI] Share export and import of sealed analysis for another collector; scopes analysed, engaged, owned, named artists or albums — `python -m backup export|import`; `share.py: SCOPES`
- [L][CLI] Merge of one's own life data from another machine's backup — `python -m backup merge`; `life_merge.py: LIFE_TABLES`
- [auto] A new node starts with curated picks, imported once through the sync gate — `seed_import.py: ensure_bundle`, `apply_seed`

## 10. Platforms, hardware tiers, remote access, auth

- [auto] Docker: three compose files — NVIDIA GPU, a WSL-side agent login, Apple Silicon (CPU) — `docker-compose.yml`, `docker-compose.wsl.yml`, `docker-compose.mac.yml`
- [L] Windows installer (per user, Windows 10+) and macOS bundle (macOS 12+, needs Homebrew); both clone `main` and keep it current — `desktop/installer/sautium.iss`; `desktop/build_macos.py: MIN_MACOS`; `desktop/windows/bootstrap.py`; `desktop/macos/bootstrap.py`
- [L] The launcher runs PostgreSQL, migrations, the backend and the P2P layer, restarts a crashed backend, holds off idle sleep — `desktop/service_manager.py: start_all`, `_watch_backend`; `desktop/utils.py: keep_awake`
- [L] Setup wizard: welcome, identity (or restore from a backup), AI provider, Last.fm, Music catalogue, summary — `desktop/wizard.py: SetupWizard.steps`
- [L] Self-update mirrors `origin/main`; hand edits in an installed tree are set aside as a patch; a Docker node does not update itself — `desktop/updater.py: reset_to_origin`, `is_managed_install`
- [auto] Hardware tier is detected, never picked: CUDA ≥ 7.5 GiB or MPS ≥ 23 GiB is Curator, ≥ 5.5 / ≥ 15 GiB Standard, the rest and every CPU-only machine Lite; under 12 GB of RAM drops one tier — `hardware_profile.py: _auto_tier`
- [auto] A tier decides what the node computes (local analysis, pre-warm, thread caps), never what it stores — `hardware_profile.py: _PREWARM`, `local_analysis`
- [UI] Sign-in by account password, by a pairing code, or by creating the account on a fresh node; sign out everywhere — `POST /api/auth/login`, `/pair`, `/create-account`, `/logout-all`
- [L] The pairing code reaches a phone as a QR code or link from the launcher; valid 5 minutes, burned after 5 wrong tries — `desktop/launcher.py: _draw_pairing_qr`; `device_auth.py: PIN_TTL_SECONDS`, `MAX_PIN_ATTEMPTS`
- [auto] Every request is signed (HMAC-SHA256, 60 s window) with a device token that is derived, never stored — `auth_hmac.py: HMACAuthMiddleware`, `REPLAY_WINDOW_SECONDS`; `device_auth.py: current_token`
- [auto] Credentials cross the network in a box to a key the node signs; the browser pins the node on first sign-in and asks before accepting another — `device_auth.py: open_channel`; `static/auth.js: askNodeChanged`
- [auto] Media for the browser rides signed URLs valid 4 hours — `media_urls.py: _TTL_SECONDS`
- [auto] A request whose Host is not the node's own is refused; a named front is added by setting — `auth_hmac.py: host_allowed`; `SAUTIUM_ALLOWED_HOSTS`, `SAUTIUM_HOST_IPS`
- [L] Tailscale, when it is up, gets a second QR code and its peers are included in the output scan; Sautium does not install or configure it — `desktop/utils.py: get_tailscale_ip`; `routers/player.py: _tailnet_peers`
- [UI] Notices and the guidance trail name what needs the owner: a missing music mount, missing media tools, a silent provider, a cooling-down source; an output to choose, a library to analyse — `routers/settings.py: _notices_state`, `_guidance_state`
- [auto] The Web UI installs to a phone's home screen; it works online only — `static/site.webmanifest`
- [auto] Layouts: phone below 768 px, tablet from 768 px — `static/tokens.css` (`--layout-mode`)
- [auto] The interface is English; the assistant is told the OS language and opens in it — `desktop/os_locale.py: resolve`; `assistant_prompt.py: _describe_user_language`

## 11. Absent by design

| Not built | Reason | Evidence |
|---|---|---|
| Transfer of audio or files between nodes | "Not a capability of this project and not a planned one" — `PHANTOM-DISCOVERY.md` § Out of scope | `grep -rnE 'add_torrent\|create_torrent\|add_magnet' desktop backend` → 0; libtorrent serves the DHT only |
| Logins to streaming services, decryption, bulk retrieval of a catalogue | a capability that must stay out of the tree — `CLAUDE.md` § Public repository rules | core registers two providers — `streaming/service.py: init` |
| Unlimited streaming of music not owned | the demo ledger is "the product's legal posture, not a preference" — `CLAUDE.md` § Enrichment Pipeline Conventions | no setting reads or bypasses `demo_plays` |
| Several users or profiles on one node | "one node, one account" — `SECURITY.md` | `user_profile` is one row (`CHECK (id = 1)`); `grep -E '\buser_id\b\|\baccount_id\b'` over the schema and Python → 0 |
| Last.fm data and lyrics over the network | Last.fm's API terms; lyrics are copyrighted text — `P2P_NETWORK.md` § Last.fm data is node-local, `BACKUP.md` § Data classes | no pull handler for either — `desktop/p2p/sync_queries.py: PULL_HANDLERS` |
| TLS on the Web UI | a certificate a phone trusts cannot exist for a LAN address — `SECURITY.md` | `entrypoint.py` serves plain HTTP |
| Backup from the Web UI | "a browser cannot receive a 3 GB file" — `backup.py` docstring | `grep -i backup backend/routers` → 0 routes |
| A sync button | the sync runs itself — `P2P_NETWORK.md` § Layered sync flow | `grep -rniE 'sync now\|trigger_sync'` → 0 |
| A hardware-tier picker | detection must be good enough on its own — `HARDWARE-TIERS.md` § 4 | `GET /api/settings/hardware` is read-only |
| HQPlayer connection form, mount mode, path mapping | the address says how files reach it — `docs/HQPLAYER_INTEGRATION.md` | `desktop/migrations/031_hqp_no_mount_mode.sql` |
| Auto-resume after an HQPlayer restart | "deliberately not done" — `docs/HQPLAYER_INTEGRATION.md` | the play-intent gate re-mirrors on the next play |
| Social feed, gamification | anti-patterns — `POSITIONING.md` | — |
| Voice interface, selective sharing, browsing or searching a friend's library, shared queues, friend recommendations, provider search, creator profiles | "dropped as no longer intended", commit `a2ef382` (2026-09-21), confirmed 2026-09-29; reason: not documented | the invite-token right `can_search` stays in the protocol and is offered nowhere |
| Repeat | the Radio toggle took its place on Now Playing (2026-05-26); reason: not documented | `grep -n -i -w 'repeat' static/*.js static/index.html` → no control |
| DSD output and resampling in the local engine | reason: not documented | "No DSD. No resampling" — `playback/local/engine.py` |

## 12. Absent, planned

| Not built | Planned in |
|---|---|
| HQPlayer input selection, output and NAA switching, controller state and hand-off | `HQPLAYER-EMBEDDED-DECISION.md` (private) |
| A native mobile app, as a DLNA client | `MOBILE-APP-CONCEPT.md` (private) |
| Lyrics on Now Playing — the button is in the page, hidden and unwired | `static/index.html` ("Parked until the lyrics sheet is wired up") |
| A level meter on Now Playing | `docs/HQPLAYER_INTEGRATION.md` § Future Enhancements |
| A desktop layout mode | `INFORMATION-ARCHITECTURE.md` § Layout modes (reserved, not designed) |
| Moods and year as search filters — the engine has the tools, nothing reaches them | `DISCOVERY-SEARCH-ENGINE.md` § Phasing (open) |
| A peer's audio chain and taste match on their profile | `INFORMATION-ARCHITECTURE.md` § Profile (Phase 2) |
| Gear research shared between nodes | `INFORMATION-ARCHITECTURE.md` § Backend work required, item 12 |
| The trust fabric: recompute ladder, flag reports, local standing, karma | `P2P-SYNC-INTEGRITY.md` (status header) |
| The admission gate charging (`enforce`) | `P2P-SYNC-INTEGRITY.md` § Pricing formula v1 (a release decision) |
| A lyrics dump among the Offline databases | `INFORMATION-ARCHITECTURE.md` § More drawer |
| A recovery key for backups; a "backup is old" reminder | `BACKUP.md` § Open questions |
| A volume control for the local, DLNA and browser outputs — the routes exist, the Web UI has none | no design doc yet; the owner's decision of 2026-09-29 (`POSITIONING.md` § 9) |
| Album descriptions written by the agent from the facts it finds, on request, and shared between nodes — the way gear research works | no design doc yet; the owner's decision of 2026-09-29 (`gear_research_worker.py` is the model) |

## 13. Absent, undecided

Searched with `git grep -niE <pattern> -- backend desktop mcp worker scripts`
(docs, vendored files and lock files excluded); no decision is recorded in
any doc.

| Not built | Pattern | Result |
|---|---|---|
| Crossfade | `crossfade\|cross-fade` | 0 |
| ReplayGain, volume levelling | `replaygain\|r128\|loudnorm\|ebur128` | 0 |
| M3U / PLS / XSPF import or export | `\.m3u\|m3u8\|\.pls\b\|xspf` | 0 |
| Saved and smart playlists | `smart.?playlist\|saved.?playlist`; `playlist` in `desktop/migrations` | 0; 0 — `/api/player/playlist` is the live queue |
| Queue shuffle, Clear queue | `shuffle` | 71 hits, none in the queue or the player: the Discovery mosaic and unrelated code. Both were drawn in the 2026-04 blueprint |
| Sleep timer | `sleep.?timer` | 0 |
| Folder watching, scheduled or start-up scan | `watchdog\|inotify\|ReadDirectoryChanges\|watchfiles` | 25 hits, all process and stream timers; no caller of the scan but the routes, the launcher button and the CLI |
| Tag editing | `\.save\(\)\|write_tags\|tag.?edit` | 0 — mutagen only reads |
| CD ripping | `cdparanoia\|cd.?rip\|libcdio` | 0 (the matches are the words "CD rip") |
| AirPlay, Chromecast, Bluetooth outputs | `airplay\|chromecast\|googlecast\|raop\|bluetooth` | 0 |
| Multi-room grouping | `multi.?room\|party.?mode\|group.?play` | 0 |
| ListenBrainz listen submission | `submit-listens\|submit_listens` | 0 — scrobbling goes to Last.fm only |
| Offline downloads to the phone; a service worker | `offline.?download\|serviceWorker` | 0 |
| CarPlay, Android Auto | `carplay\|android.?auto` | 0 |
| Podcasts, internet radio | `podcast\|internet.?radio\|icecast\|shoutcast` | 0 (one comment) |
| Ratings, likes, Last.fm "love" | `\brating(s)?\b\|track\.love\|\.love\(` | 5 hits, all electrical ratings in the gear specs |
| A graphic or live equalizer | `equalizer\|graphic.?eq` | 0 — EQ is a generated file for HQPlayer |
| Keyboard transport shortcuts | `Arrow(Left\|Right)\|MediaPlayPause` in `static/` | 0 — Escape and Enter only |
| Lock-screen controls for outputs other than the browser | `mediaSession` | `static/player.js` only, inside the browser renderer |
| Interface languages other than English | `i18n\|gettext` | 0; `ui.language` exists with nothing that writes it |
| Stop generation in the chat; message feedback | `feedback` in `static/app-shell.js` | 3 hits, all comments: no control for either, though `POST /api/chat/messages/{id}/feedback` exists |
| Last.fm disconnect; sign out of this device only | routes of `lastfm_auth.py`; `Sautium.auth.forget` | no route; defined, never called |
| A Linux installer | `ls desktop/` | `windows/`, `macos/`, `installer/` only; the launcher has Linux code paths |

## 14. Discrepancies between the docs and the code

Open rows only. What was settled on 2026-09-29 is applied: the docs were
corrected, and the code defects this pass met were fixed (the missing
import behind the HQPlayer library sync, the unassigned cover URL, an
album's "+ Queue → Next", the announce port of the macOS compose file).

| Doc | Says | Code | Open question |
|---|---|---|---|
| `docs/design/INFORMATION-ARCHITECTURE.md:291` | a peer's profile opens from the chat header or the friends list | the screen exists, nothing navigates to `#profile/<prefix>`, and its "Add as friend" button has no handler (`static/app-shell.js: renderProfileOther`) | wire it, describe it as not reachable yet, or remove the screen |
| `docs/design/INFORMATION-ARCHITECTURE.md` § Discovery structure | Danceable as a filter | the "No" chip is accepted and ignored (`routers/discovery.py: _engine_filters`) | postponed 2026-09-29: whether the chip is needed at all is undecided |
| `docker-compose.mac.yml` against `README.md` § Quick Start | a Docker node on macOS is a full node | the file publishes no peer or media port and mounts neither the identity documents nor the peer certificate | align it with `docker-compose.yml`, on a Mac that can test it |
| `backend/test_playback_queue.py: test_payload_shapes` | the queue payload has ten fields | the payload carries `play` since 2026-09-28; the test sits outside `tests/` and the usual run does not reach it | update the expectation, or keep `play` out of the payload |
