/* Sautium — Frontend Logic */

// -- State -------------------------------------------------------------------

let currentState = "disconnected";
let currentSessionId = null;
let sessions = [];
let sessionsLoaded = false;
let currentPlaylist = [];
// Last `playlist_version` we've seen on the SSE stream. Whenever the
// backend bumps this (after any queue mutation, ours or external), the
// SSE handler awaits a fresh fetchPlaylist() before letting subscribers
// see the new status payload — guarantees `currentPlaylist` is always
// in sync with the song reported by HQPlayer. See backend
// _refresh_playlist_cache for the bump rule.
let lastPlaylistVersion = null;
let providersData = [];  // [{id, name, models}, ...]
let selectedProvider = localStorage.getItem("djProvider") || "";
let selectedModel = localStorage.getItem("djModel") || "";

// Lyrics state
let lyricsVisible = false;
let lyricsData = null;         // {synced_lyrics, plain_lyrics, instrumental, ...}
let lyricsTrackKey = null;     // "artist|song" to detect track changes
let lyricsMediaFileId = null;  // media_file_id used for fetching
let _lyricsLines = [];         // cached .lyrics-line elements

// -- DOM refs ----------------------------------------------------------------

const connDot = document.getElementById("connDot");
const npSong = document.getElementById("npSong");
const npArtist = document.getElementById("npArtist");
const progressFill = document.getElementById("progressFill");
const progressTime = document.getElementById("progressTime");
const playPauseBtn = document.getElementById("playPauseBtn");
const volLabel = document.getElementById("volLabel");
const searchInput = document.getElementById("searchInput");
const searchResults = document.getElementById("searchResults");
const chatMessages = document.getElementById("chatMessages");
const chatInput = document.getElementById("chatInput");
const chatSendBtn = document.getElementById("chatSendBtn");

// -- SSE status stream -------------------------------------------------------

let _sseSource = null;
let _sseRetryDelay = 1000;

function connectStatusSSE() {
  if (_sseSource) {
    _sseSource.close();
  }

  _sseSource = new EventSource("/api/player/status/stream");

  _sseSource.onmessage = (event) => {
    _sseRetryDelay = 1000; // reset backoff on success
    let data;
    try {
      data = JSON.parse(event.data);
    } catch (e) {
      console.error("SSE parse error:", e);
      return;
    }
    handleStatusEvent(data);
  };

  _sseSource.onerror = () => {
    _sseSource.close();
    _sseSource = null;
    currentState = "disconnected";
    updateNowPlaying({ state: "disconnected" });
    // Reconnect with exponential backoff (max 10s)
    setTimeout(connectStatusSSE, _sseRetryDelay);
    _sseRetryDelay = Math.min(_sseRetryDelay * 1.5, 10000);
  };
}

// Process a single SSE status payload. If the backend bumped
// `playlist_version`, await a fresh `fetchPlaylist()` BEFORE notifying
// subscribers — that way `currentPlaylist` is always consistent with
// the song the status payload describes (no setTimeout/race patches).
async function processStatusEvent(data) {
  currentState = data.state;
  if (data.playlist_version !== undefined &&
      data.playlist_version !== lastPlaylistVersion) {
    // Update marker first so a duplicate event doesn't trigger a second
    // refetch while this one is in flight.
    lastPlaylistVersion = data.playlist_version;
    try {
      await fetchPlaylist();
    } catch (e) {
      console.warn("fetchPlaylist failed during SSE handling:", e);
    }
  }
  updateNowPlaying(data);
}

// SSE delivers events sequentially. If we make onmessage async directly,
// JS dispatches each callback on its own microtask without awaiting the
// previous one — two rapid playlist mutations would launch two
// fetchPlaylist() calls in parallel and race over `currentPlaylist`.
// We restore strict serial processing by chaining each event onto a
// promise. `.catch()` keeps the chain alive after a failed handler.
let _sseChain = Promise.resolve();

function handleStatusEvent(data) {
  _sseChain = _sseChain
    .then(() => processStatusEvent(data))
    .catch((e) => console.error("SSE handler error:", e));
}

// -- Chat SSE stream ----------------------------------------------------------

function connectChatSSE() {
  if (_chatSSE) return; // already connected

  _chatSSE = new EventSource("/api/p2p/chat/stream");

  _chatSSE.onmessage = (event) => {
    _chatSSERetryDelay = 1000;
    loadFriends();
    if (_selectedFriendId) {
      loadP2PMessages(_selectedFriendId);
    }
  };

  _chatSSE.onerror = () => {
    _chatSSE.close();
    _chatSSE = null;
    setTimeout(connectChatSSE, _chatSSERetryDelay);
    _chatSSERetryDelay = Math.min(_chatSSERetryDelay * 1.5, 10000);
  };
}

async function pollStatus() {
  try {
    const resp = await fetch("/api/player/status");
    const data = await resp.json();
    currentState = data.state;
    updateNowPlaying(data);
  } catch {
    currentState = "disconnected";
    updateNowPlaying({ state: "disconnected" });
  }
}

function updateNowPlaying(data) {
  const connected = data.state !== "disconnected";
  connDot.classList.toggle("connected", connected);

  if (!connected) {
    npSong.textContent = "Not connected";
    npSong.classList.add("np-idle");
    npArtist.textContent = "";
    progressFill.style.width = "0%";
    progressTime.textContent = "00:00 / 00:00";
    playPauseBtn.innerHTML = "&#9654;";
    volLabel.textContent = "-- dB";
    return;
  }

  if (data.state === "stopped" && !data.song) {
    npSong.textContent = "Stopped";
    npSong.classList.add("np-idle");
    npArtist.textContent = "";
    progressFill.style.width = "0%";
    progressTime.textContent = "00:00 / 00:00";
    playPauseBtn.innerHTML = "&#9654;";
  } else {
    npSong.textContent = data.song || "Unknown";
    npSong.classList.remove("np-idle");
    const parts = [];
    if (data.artist) parts.push(data.artist);
    if (data.album) parts.push(data.album);
    npArtist.textContent = parts.join(" \u2014 ");
    progressFill.style.width = (data.progress_percent || 0) + "%";
    progressTime.textContent =
      (data.position_formatted || "00:00") + " / " + (data.length_formatted || "00:00");

    playPauseBtn.innerHTML = data.state === "playing" ? "&#9208;" : "&#9654;";
  }

  if (data.volume !== undefined && data.volume !== null) {
    volLabel.textContent = Math.round(data.volume) + " dB";
  }

  // Lyrics: detect track change and update highlight
  _latest_status_cache = data;
  const newTrackKey = (data.artist || "") + "|" + (data.song || "");
  if (newTrackKey !== lyricsTrackKey && data.song) {
    lyricsTrackKey = newTrackKey;
    lyricsData = null;
    lyricsMediaFileId = null;
    _lyricsLines = [];
    if (lyricsVisible) {
      fetchLyricsForCurrentTrack();
    }
  }
  if (data.position !== undefined) {
    updateLyricsHighlight(data.position);
  }

  // Update discover "similar to now playing" + radio mode
  updateDiscoverSimilar(data);
  _radioCheck();

  // Update queue highlighting
  if (currentPlaylist.length > 0 && data.track_index !== undefined) {
    const queueItems = document.querySelectorAll(".queue-item");
    queueItems.forEach((item, idx) => {
      // HQPlayer uses 1-based indexing, JS arrays use 0-based
      item.classList.toggle("playing", idx === data.track_index - 1);
    });
  }

  // Bridge to the new shell's mini-player. Single source of truth (this
  // function), multiple consumers (legacy hidden stubs above + new
  // mini-player in app-shell.js listening for np-update).
  document.dispatchEvent(new CustomEvent("np-update", { detail: data }));
}

// -- Transport controls ------------------------------------------------------

async function playerCmd(cmd) {
  try {
    await fetch("/api/player/" + cmd, { method: "POST" });
  } catch (e) {
    console.error("Player command failed:", e);
  }
}

function togglePlayPause() {
  if (currentState === "playing") {
    playerCmd("pause");
  } else {
    playerCmd("play");
  }
}

// -- Play track/album ---------------------------------------------------------

async function playTrack(trackId) {
  try {
    await fetch("/api/player/play-track", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ track_id: trackId }),
    });
    // No client-side refetch — backend bumps playlist_version, SSE
    // handler awaits fetchPlaylist() before notifying the UI.
  } catch (e) {
    console.error("Play track failed:", e);
  }
}

async function playAlbum(albumName, artistName) {
  console.log("Playing album:", albumName, "by", artistName);
  try {
    const resp = await fetch("/api/player/play-album", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ album_name: albumName, artist_name: artistName }),
    });
    if (!resp.ok) {
      const result = await resp.json();
      console.error("Play album error:", result);
    }
    // No client-side refetch — see playTrack().
  } catch (e) {
    console.error("Play album failed:", e);
  }
}

function toggleAlbumTracks(albumId) {
  const el = document.getElementById("tracks-" + albumId);
  if (el) {
    el.style.display = el.style.display === "none" ? "block" : "none";
  }
}

async function playRecommendations(tracks) {
  try {
    const trackIds = tracks.map(t => t.id);
    await fetch("/api/player/play-tracks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ track_ids: trackIds }),
    });
    // No client-side refetch — see playTrack().
  } catch (e) {
    console.error("Play recommendations failed:", e);
  }
}

// -- Tabs --------------------------------------------------------------------

function switchTab(name) {
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.tab === name);
  });
  document.querySelectorAll(".tab-content").forEach((div) => {
    div.classList.toggle("active", div.id === "tab-" + name);
  });
  if (name === "chat" && !sessionsLoaded) {
    loadSessions();
  }
}

// -- Search ------------------------------------------------------------------

async function doSearch(e) {
  e.preventDefault();
  const q = searchInput.value.trim();
  if (!q) return;

  searchResults.innerHTML = '<div class="loading"><span class="spinner"></span>Searching...</div>';

  try {
    const params = new URLSearchParams({ q: q, limit: "20" });
    const resp = await fetch("/api/player/search?" + params.toString());
    const data = await resp.json();
    const albums = data.albums || [];

    if (albums.length === 0) {
      searchResults.innerHTML = '<div class="empty-state">No results found</div>';
      return;
    }

    renderAlbumList(searchResults, albums);
  } catch (err) {
    searchResults.innerHTML = '<div class="empty-state">Search error: ' + esc(err.message) + "</div>";
  }
}

function groupTracksByAlbum(tracks) {
  const albumsMap = new Map();

  for (const t of tracks) {
    const key = `${t.artist}|||${t.album}`;
    if (!albumsMap.has(key)) {
      albumsMap.set(key, {
        artist: t.artist,
        album: t.album,
        album_id: `chat-${Math.random().toString(36).substr(2, 9)}`,
        genre: t.genre,
        is_lossless: t.is_lossless,
        tracks: [],
      });
    }
    albumsMap.get(key).tracks.push({
      id: t.id,
      title: t.title,
      track_number: t.track_number,
      disc_number: t.disc_number,
      duration_seconds: t.duration_seconds,
    });
  }

  const albums = Array.from(albumsMap.values());
  for (const album of albums) {
    album.track_count = album.tracks.length;
    album.total_duration = album.tracks.reduce((sum, t) => sum + (t.duration_seconds || 0), 0);
  }

  return albums;
}

function renderAlbumList(container, albums) {
  let html = '<div class="results-count">' + albums.length + " albums</div>";
  html += '<div class="album-list">';

  for (const album of albums) {
    const duration = album.total_duration ? formatTime(album.total_duration) : "";
    const qualityLabel = album.is_lossless === true ? "Lossless" : album.is_lossless === false ? "Lossy" : "";
    const meta = [album.genre, qualityLabel, album.track_count + " tracks", duration]
      .filter(Boolean).join(" \u00B7 ");

    const albumId = album.album_id;

    html +=
      '<div class="album-card">' +
        '<div class="album-header" onclick="toggleAlbumTracks(\'' + albumId + '\')">' +
          '<div class="album-info">' +
            '<div class="album-title">' + esc(album.artist) + " \u2014 " + esc(album.album) + "</div>" +
            '<div class="album-meta">' + esc(meta) + "</div>" +
          "</div>" +
          '<button class="album-play-btn" data-album="' + escAttr(album.album) +
            '" data-artist="' + escAttr(album.artist) + '">&#9654;</button>' +
        "</div>" +
        '<div class="album-tracks" id="tracks-' + albumId + '" style="display:none">';

    const hasMultipleDiscs = album.tracks.some(t => t.disc_number && t.disc_number > 1);
    let lastDisc = 0;

    for (const t of album.tracks) {
      // Disc separator
      if (hasMultipleDiscs && t.disc_number && t.disc_number !== lastDisc) {
        lastDisc = t.disc_number;
        html += '<div class="disc-header">Disc ' + t.disc_number + '</div>';
      }
      const trackDur = t.duration_seconds ? formatTime(t.duration_seconds) : "";
      html +=
        '<div class="track-row" onclick="playTrack(' + t.id + ')">' +
          '<span class="track-num">' + (t.track_number || "") + "</span>" +
          '<span class="track-title">' + esc(t.title) + "</span>" +
          '<span class="track-dur">' + trackDur + "</span>" +
        "</div>";
    }

    html += "</div></div>";
  }

  html += "</div>";
  container.innerHTML = html;

  // Attach event listeners to album play buttons
  container.querySelectorAll(".album-play-btn").forEach(btn => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const album = btn.dataset.album;
      const artist = btn.dataset.artist;
      playAlbum(album, artist);
    });
  });
}

function renderTrackList(container, tracks) {
  let html = '<div class="results-count">' + tracks.length + " tracks</div>";
  html += '<div class="track-list">';

  for (const t of tracks) {
    const duration = t.duration_seconds ? formatTime(t.duration_seconds) : "";
    const meta = [t.album, t.genre, duration].filter(Boolean).join(" \u00B7 ");
    html +=
      '<div class="track-card" onclick="playTrack(' + t.id + ')">' +
        '<div class="track-info">' +
          '<div class="track-title">' + esc(t.artist || "") + " \u2014 " + esc(t.title || "") + "</div>" +
          '<div class="track-meta">' + esc(meta) + "</div>" +
        "</div>" +
        '<button class="track-play-btn" onclick="event.stopPropagation(); playTrack(' + t.id + ')">&#9654;</button>' +
      "</div>";
  }

  html += "</div>";
  container.innerHTML = html;
}

// -- Sessions ----------------------------------------------------------------

async function loadSessions() {
  try {
    const resp = await fetch("/api/chat/sessions");
    sessions = await resp.json();
    sessionsLoaded = true;
    renderSessionList();
    // Auto-select the most recent session
    if (sessions.length > 0 && !currentSessionId) {
      selectSession(sessions[0].id);
    }
  } catch (e) {
    console.error("Failed to load sessions:", e);
  }
}

function renderSessionList() {
  const container = document.getElementById("sessionList");
  if (!container) return;
  let html = "";
  for (const s of sessions) {
    const title = s.title || "New chat";
    const isActive = s.id === currentSessionId;
    html +=
      '<div class="session-pill' + (isActive ? " active" : "") + '" data-sid="' + s.id + '" onclick="selectSession(' + s.id + ')">' +
        '<span class="session-pill-title">' + esc(title) + '</span>' +
        '<span class="session-pill-delete" onclick="event.stopPropagation(); deleteSession(' + s.id + ')">&times;</span>' +
      '</div>';
  }
  container.innerHTML = html;
}

async function selectSession(id) {
  currentSessionId = id;
  renderSessionList();
  chatMessages.innerHTML = '<div class="loading"><span class="spinner"></span>Loading...</div>';

  try {
    const resp = await fetch("/api/chat/sessions/" + id + "/messages");
    const messages = await resp.json();

    chatMessages.innerHTML = "";
    if (messages.length === 0) {
      chatMessages.innerHTML = '<div class="empty-state">Ask the AI DJ for recommendations</div>';
      return;
    }

    for (const m of messages) {
      const tracks = m.tracks_data ? (typeof m.tracks_data === "string" ? JSON.parse(m.tracks_data) : m.tracks_data) : null;
      appendChatBubble(m.role, m.content, tracks, null, null, null, m.id, m.is_not_relevant);
    }
  } catch (e) {
    chatMessages.innerHTML = '<div class="empty-state">Error loading messages</div>';
    console.error("Failed to load messages:", e);
  }
}

function startNewChat() {
  currentSessionId = null;
  renderSessionList();
  chatMessages.innerHTML = '<div class="empty-state">Ask the AI DJ for recommendations</div>';
  chatInput.focus();
}

async function deleteSession(id) {
  try {
    await fetch("/api/chat/sessions/" + id, { method: "DELETE" });
    sessions = sessions.filter(s => s.id !== id);
    if (currentSessionId === id) {
      currentSessionId = null;
      chatMessages.innerHTML = '<div class="empty-state">Ask the AI DJ for recommendations</div>';
      if (sessions.length > 0) {
        selectSession(sessions[0].id);
      }
    }
    renderSessionList();
  } catch (e) {
    console.error("Failed to delete session:", e);
  }
}

function openFeedbackForm(messageId, btn) {
  // Don't open twice
  if (btn.parentElement.querySelector(".feedback-form")) return;

  btn.style.display = "none";

  const form = document.createElement("div");
  form.className = "feedback-form";

  const input = document.createElement("input");
  input.type = "text";
  input.className = "feedback-input";
  input.placeholder = "What was wrong? (optional)";

  const sendBtn = document.createElement("button");
  sendBtn.className = "feedback-send";
  sendBtn.textContent = "Send";
  sendBtn.onclick = () => submitFeedback(messageId, input.value.trim(), form, btn);

  const cancelBtn = document.createElement("button");
  cancelBtn.className = "feedback-cancel";
  cancelBtn.textContent = "Cancel";
  cancelBtn.onclick = () => { form.remove(); btn.style.display = ""; };

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      submitFeedback(messageId, input.value.trim(), form, btn);
    }
  });

  form.appendChild(input);
  form.appendChild(sendBtn);
  form.appendChild(cancelBtn);
  btn.parentElement.appendChild(form);
  input.focus();
}

async function submitFeedback(messageId, comment, form, btn) {
  try {
    await fetch("/api/chat/messages/" + messageId + "/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ is_not_relevant: true, comment: comment || null }),
    });
    form.remove();
    btn.classList.add("sent");
    btn.disabled = true;
    btn.textContent = "Not relevant";
    btn.style.display = "";
  } catch (e) {
    console.error("Failed to send feedback:", e);
  }
}

// -- Provider selector -------------------------------------------------------

async function loadProviders() {
  try {
    const resp = await fetch("/api/chat/providers");
    providersData = await resp.json();
    renderProviderSelector();
  } catch (e) {
    console.error("Failed to load providers:", e);
    // Fallback: show nothing
    providersData = [];
    renderProviderSelector();
  }
}

function renderProviderSelector() {
  const container = document.getElementById("providerSelector");
  if (!container) return;

  if (providersData.length === 0) {
    container.innerHTML = '<div class="provider-empty">No AI providers configured</div>';
    return;
  }

  // Auto-select provider if current selection is invalid
  const validIds = providersData.map(p => p.id);
  if (!selectedProvider || !validIds.includes(selectedProvider)) {
    selectedProvider = validIds[0];
    localStorage.setItem("djProvider", selectedProvider);
  }

  // Auto-select model if current selection is invalid for this provider
  const currentProviderData = providersData.find(p => p.id === selectedProvider);
  if (currentProviderData && (!selectedModel || !currentProviderData.models.includes(selectedModel))) {
    selectedModel = currentProviderData.models[0] || "";
    localStorage.setItem("djModel", selectedModel);
  }

  let html = '<div class="provider-row">';
  for (const p of providersData) {
    const isActive = p.id === selectedProvider;
    html += '<button class="provider-btn' + (isActive ? " active" : "") +
      '" data-provider="' + escAttr(p.id) + '">' +
      esc(p.name) + '</button>';
  }
  html += '</div>';

  // Model buttons for selected provider
  if (currentProviderData && currentProviderData.models.length > 0) {
    html += '<div class="model-row">';
    for (const m of currentProviderData.models) {
      const isActive = m === selectedModel;
      // Show short model name
      const shortName = formatModelName(m);
      html += '<button class="model-btn' + (isActive ? " active" : "") +
        '" data-model="' + escAttr(m) + '">' +
        esc(shortName) + '</button>';
    }
    html += '</div>';
  }

  container.innerHTML = html;

  // Event delegation (avoids inline onclick + escaping issues)
  container.querySelectorAll(".provider-btn").forEach(btn => {
    btn.addEventListener("click", () => selectProvider(btn.dataset.provider));
  });
  container.querySelectorAll(".model-btn").forEach(btn => {
    btn.addEventListener("click", () => selectModelBtn(btn.dataset.model));
  });
}

function formatModelName(model) {
  // Shorten common model names for UI display
  const shorts = {
    "claude-sonnet-4-20250514": "Sonnet 4",
    "claude-haiku-4-5-20251001": "Haiku 4.5",
    "gpt-4o": "GPT-4o",
    "gpt-4o-mini": "GPT-4o mini",
    "llama-3.3-70b-versatile": "Llama 70B",
    "llama-3.1-8b-instant": "Llama 8B",
    "sonnet": "Sonnet",
    "haiku": "Haiku",
  };
  return shorts[model] || model;
}

function selectProvider(id) {
  selectedProvider = id;
  localStorage.setItem("djProvider", id);

  // Auto-select first model of this provider
  const prov = providersData.find(p => p.id === id);
  if (prov && prov.models.length > 0) {
    selectedModel = prov.models[0];
    localStorage.setItem("djModel", selectedModel);
  }

  renderProviderSelector();
}

function selectModelBtn(model) {
  selectedModel = model;
  localStorage.setItem("djModel", model);
  renderProviderSelector();
}

// -- Chat --------------------------------------------------------------------

async function sendChat(e) {
  e.preventDefault();
  const msg = chatInput.value.trim();
  if (!msg) return;

  // Disable immediately to prevent double-submit race condition
  chatInput.value = "";
  chatSendBtn.disabled = true;

  // Create session if needed
  if (!currentSessionId) {
    try {
      const resp = await fetch("/api/chat/sessions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      const session = await resp.json();
      currentSessionId = session.id;
      chatMessages.innerHTML = "";
    } catch (err) {
      appendChatBubble("assistant", "Error creating session: " + err.message);
      chatSendBtn.disabled = false;
      return;
    }
  }

  // Clear empty state
  const emptyState = chatMessages.querySelector(".empty-state");
  if (emptyState) emptyState.remove();

  // Add user message bubble
  appendChatBubble("user", msg);

  // Show loading
  const loadingId = "chat-loading-" + Date.now();
  const loadingEl = document.createElement("div");
  loadingEl.id = loadingId;
  loadingEl.className = "loading";
  loadingEl.innerHTML = '<span class="spinner"></span>Thinking...';
  chatMessages.appendChild(loadingEl);
  chatMessages.scrollTop = chatMessages.scrollHeight;

  try {
    const body = { message: msg };
    if (selectedProvider) body.provider = selectedProvider;
    if (selectedModel) body.model = selectedModel;

    const resp = await fetch("/api/chat/sessions/" + currentSessionId + "/messages", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await resp.json();

    // Remove loading
    const el = document.getElementById(loadingId);
    if (el) el.remove();

    if (data.detail) {
      appendChatBubble("assistant", "Error: " + data.detail);
    } else {
      const assistantId = data.assistant_msg ? data.assistant_msg.id : null;
      const providerLabel = data.provider ? (data.provider + ":" + (data.model || "")) : data.model;
      appendChatBubble(
        "assistant",
        data.assistant_msg ? data.assistant_msg.content : "",
        data.tracks,
        data.retrieval_log,
        providerLabel,
        data.tracks_retrieved,
        assistantId
      );
    }

    // Refresh session list
    loadSessions();
  } catch (err) {
    const el = document.getElementById(loadingId);
    if (el) el.remove();
    appendChatBubble("assistant", "Error: " + err.message);
  }

  chatSendBtn.disabled = false;
}

function appendChatBubble(role, text, tracks, retrievalLog, model, tracksRetrieved, messageId, alreadyFlagged) {
  const div = document.createElement("div");
  div.className = "chat-msg " + role;

  // Simple markdown-like formatting: bold, italic, links, newlines
  let html = esc(text)
    .replace(/\n/g, "<br>")
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\*(.+?)\*/g, "<em>$1</em>")
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_, t, u) =>
      /^https?:\/\//i.test(u)
        ? '<a href="' + u.replace(/"/g, '&quot;') + '" target="_blank" rel="noopener noreferrer">' + t + '</a>'
        : t
    );
  div.innerHTML = html;

  chatMessages.appendChild(div);

  // Add retrieval log (collapsible debug info)
  if (retrievalLog && retrievalLog.length > 0) {
    const logDiv = document.createElement("details");
    logDiv.className = "retrieval-log";
    let logHtml = "<summary>Pipeline (" + (tracksRetrieved || 0) + " tracks, " + esc(model || "?") + ")</summary>";
    logHtml += '<div class="retrieval-steps">';
    for (const step of retrievalLog) {
      logHtml += '<div class="retrieval-step">'
        + '<span class="retrieval-source">' + esc(step.source) + '</span> '
        + '<span class="retrieval-desc">' + esc(step.description) + '</span>'
        + '</div>';
    }
    logHtml += '</div>';
    logDiv.innerHTML = logHtml;
    div.appendChild(logDiv);
  }

  // Add playable tracks if provided (after div is in DOM)
  if (tracks && tracks.length > 0) {
    // Add "Play All" button
    const playAllBtn = document.createElement("button");
    playAllBtn.className = "play-all-btn";
    playAllBtn.textContent = "\u25b6 Play Recommendations (" + tracks.length + " tracks)";
    playAllBtn.onclick = () => playRecommendations(tracks);
    div.appendChild(playAllBtn);

    const tracksDiv = document.createElement("div");
    tracksDiv.className = "chat-tracks";
    // Group tracks by album
    const albums = groupTracksByAlbum(tracks);
    renderAlbumList(tracksDiv, albums);
    div.appendChild(tracksDiv);
  }

  // Add feedback button for assistant messages
  if (role === "assistant" && messageId) {
    const feedbackBtn = document.createElement("button");
    feedbackBtn.className = "feedback-btn" + (alreadyFlagged ? " sent" : "");
    feedbackBtn.textContent = alreadyFlagged ? "Not relevant" : "\ud83d\udc4e Not relevant";
    feedbackBtn.disabled = !!alreadyFlagged;
    if (!alreadyFlagged) {
      feedbackBtn.onclick = () => openFeedbackForm(messageId, feedbackBtn);
    }
    div.appendChild(feedbackBtn);
  }

  chatMessages.scrollTop = chatMessages.scrollHeight;
}

// -- Queue display -----------------------------------------------------------

async function fetchPlaylist() {
  try {
    const resp = await fetch("/api/player/playlist");
    const data = await resp.json();
    if (data.tracks) {
      currentPlaylist = data.tracks;
      updateQueueDisplay();
      // Notify the new shell that mediaFileId resolution may now succeed.
      // Sheet's tryFetchDetail is gated on _getCurrentMediaFileId() which
      // depends on currentPlaylist being populated; firing this event
      // closes the race between SSE arriving before playlist load.
      document.dispatchEvent(new CustomEvent("playlist-loaded", { detail: data }));
    }
  } catch (e) {
    console.warn("Failed to fetch playlist:", e);
  }
}

function updateQueueDisplay() {
  const container = document.getElementById("queueContent");
  if (!container) return;

  if (currentPlaylist.length === 0) {
    container.innerHTML = '<div class="empty-state">No tracks in queue</div>';
    return;
  }

  let html = '<div class="results-count">' + currentPlaylist.length + " tracks in queue</div>";
  html += '<div class="queue-list">';

  for (let i = 0; i < currentPlaylist.length; i++) {
    const t = currentPlaylist[i];
    const num = i + 1;
    const isCurrent = false; // Will be updated from status polling
    html +=
      '<div class="queue-item' + (isCurrent ? ' playing' : '') + '" data-index="' + i + '">' +
        '<span class="queue-num">' + num + "</span>" +
        '<div class="queue-info">' +
          '<div class="queue-title">' + esc(t.title) + "</div>" +
          '<div class="queue-artist">' + esc(t.artist) + "</div>" +
        "</div>" +
      "</div>";
  }

  html += "</div>";
  container.innerHTML = html;
}

// -- Helpers -----------------------------------------------------------------

function formatTime(sec) {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return m + ":" + String(s).padStart(2, "0");
}

function esc(str) {
  const el = document.createElement("span");
  el.textContent = str;
  return el.innerHTML;
}

function escAttr(str) {
  return esc(str).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// -- Lyrics ------------------------------------------------------------------

function toggleLyrics() {
  const panel = document.getElementById("lyricsPanel");
  const btn = document.getElementById("lyricsToggleBtn");
  lyricsVisible = !lyricsVisible;
  panel.style.display = lyricsVisible ? "block" : "none";
  btn.classList.toggle("active", lyricsVisible);

  if (lyricsVisible && !lyricsData) {
    fetchLyricsForCurrentTrack();
  }
}

function fetchLyricsForCurrentTrack() {
  // Find media_file_id from current playlist + track_index
  const status = _latest_status_cache;
  if (!status || !status.song) return;

  // Use playlist to find media_file_id
  const trackIndex = status.track_index;
  if (currentPlaylist.length > 0 && trackIndex !== undefined && trackIndex >= 1) {
    const playlistTrack = currentPlaylist[trackIndex - 1];
    if (playlistTrack && playlistTrack.id) {
      fetchLyrics(playlistTrack.id);
      return;
    }
  }

  // Fallback: no playlist info, show message
  const content = document.getElementById("lyricsContent");
  content.innerHTML = '<div class="lyrics-placeholder">Play a track to see lyrics</div>';
}

async function fetchLyrics(mediaFileId) {
  if (lyricsMediaFileId === mediaFileId && lyricsData) return;
  lyricsMediaFileId = mediaFileId;

  const content = document.getElementById("lyricsContent");
  content.innerHTML = '<div class="lyrics-placeholder">Loading lyrics...</div>';

  try {
    const resp = await fetch("/api/player/lyrics/" + mediaFileId);
    const data = await resp.json();
    lyricsData = data;
    renderLyrics(data);
  } catch (e) {
    console.error("Lyrics fetch failed:", e);
    content.innerHTML = '<div class="lyrics-placeholder">Failed to load lyrics</div>';
  }
}

function renderLyrics(data) {
  const content = document.getElementById("lyricsContent");

  if (data.instrumental) {
    content.innerHTML = '<div class="lyrics-instrumental">Instrumental</div>';
    _lyricsLines = [];
    return;
  }

  if (data.synced_lyrics && data.synced_lyrics.length > 0) {
    let html = "";
    for (let i = 0; i < data.synced_lyrics.length; i++) {
      const line = data.synced_lyrics[i];
      html += '<div class="lyrics-line" data-time="' + line.time_ms + '" data-idx="' + i + '">' +
        esc(line.text || "") + '</div>';
    }
    content.innerHTML = html;
    _lyricsLines = content.querySelectorAll(".lyrics-line");
    return;
  }

  if (data.plain_lyrics) {
    content.innerHTML = '<div class="lyrics-plain">' + esc(data.plain_lyrics) + '</div>';
    _lyricsLines = [];
    return;
  }

  content.innerHTML = '<div class="lyrics-placeholder">No lyrics available</div>';
  _lyricsLines = [];
}

function updateLyricsHighlight(positionSeconds) {
  if (!lyricsVisible || !lyricsData || !lyricsData.synced_lyrics) return;

  const positionMs = positionSeconds * 1000;
  if (_lyricsLines.length === 0) return;

  let activeIdx = -1;
  for (let i = lyricsData.synced_lyrics.length - 1; i >= 0; i--) {
    if (positionMs >= lyricsData.synced_lyrics[i].time_ms) {
      activeIdx = i;
      break;
    }
  }

  _lyricsLines.forEach((line, idx) => {
    line.classList.toggle("active", idx === activeIdx);
    line.classList.toggle("past", idx < activeIdx);
  });

  // Auto-scroll to active line
  if (activeIdx >= 0 && _lyricsLines[activeIdx]) {
    const panel = document.getElementById("lyricsPanel");
    const activeLine = _lyricsLines[activeIdx];
    const panelRect = panel.getBoundingClientRect();
    const lineRect = activeLine.getBoundingClientRect();

    // Scroll if active line is outside visible area
    if (lineRect.top < panelRect.top || lineRect.bottom > panelRect.bottom) {
      activeLine.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }
}

// Cache for track change detection
let _latest_status_cache = {};

// -- Hamburger menu ----------------------------------------------------------

let currentSection = "music";

function toggleMenu() {
  document.getElementById("slideMenu").classList.toggle("open");
  document.getElementById("menuOverlay").classList.toggle("open");
}

function closeMenu() {
  document.getElementById("slideMenu").classList.remove("open");
  document.getElementById("menuOverlay").classList.remove("open");
}

function switchSection(name) {
  if (name === currentSection) {
    closeMenu();
    return;
  }

  // Hide disabled items
  const menuItem = document.querySelector(`.menu-item[data-section="${name}"]`);
  if (!menuItem || menuItem.classList.contains("disabled")) {
    closeMenu();
    return;
  }

  currentSection = name;

  // Close chat SSE when leaving friends section (prevent memory leak)
  if (name !== "friends" && _chatSSE) {
    _chatSSE.close();
    _chatSSE = null;
  }

  // Update menu active state
  document.querySelectorAll(".menu-item[data-section]").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.section === name);
  });

  // Switch section
  document.querySelectorAll(".app-section").forEach(sec => {
    sec.classList.toggle("active", sec.id === "section-" + name);
  });

  // Update title
  const titles = { music: "Music", friends: "Friends", settings: "Settings" };
  document.getElementById("sectionTitle").textContent = titles[name] || name;

  // Load friends data on first visit + connect SSE for live updates
  if (name === "friends") {
    if (!_friendsLoaded) {
      loadAccountInfo();
      loadFriends();
      _friendsLoaded = true;
    }
    connectChatSSE();
  }

  if (name === "settings") {
    if (!_settingsLoaded) {
      loadSettings();
      _settingsLoaded = true;
    }
  }

  closeMenu();
}

// -- Friends section ---------------------------------------------------------

let _friendsLoaded = false;
let _friends = [];
let _selectedFriendId = null;
let _chatSSE = null;
let _chatSSERetryDelay = 1000;

function switchFriendsTab(name) {
  document.querySelectorAll("[data-ftab]").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.ftab === name);
  });
  document.querySelectorAll(".ftab-content").forEach(div => {
    div.classList.toggle("active", div.id === "ftab-" + name);
  });
}

async function loadAccountInfo() {
  try {
    const resp = await fetch("/api/p2p/account");
    const data = await resp.json();
    const codeEl = document.getElementById("myInviteCode");
    if (data.invite_code) {
      codeEl.textContent = data.invite_code;
    } else {
      codeEl.textContent = "No account configured";
      codeEl.style.color = "var(--text-dim)";
    }
  } catch {
    document.getElementById("myInviteCode").textContent = "Error loading account";
  }
}

function copyInviteCode() {
  const code = document.getElementById("myInviteCode").textContent;
  if (!code || code.startsWith("No") || code.startsWith("Error")) return;

  const btn = document.querySelector(".copy-btn");
  navigator.clipboard.writeText(code).then(() => {
    btn.textContent = "Copied!";
    btn.classList.add("copied");
    setTimeout(() => {
      btn.textContent = "Copy";
      btn.classList.remove("copied");
    }, 2000);
  }).catch(() => {
    // Clipboard API unavailable (non-HTTPS LAN access)
    btn.textContent = "Failed";
    setTimeout(() => { btn.textContent = "Copy"; }, 2000);
  });
}

async function loadFriends() {
  try {
    const resp = await fetch("/api/p2p/friends");
    _friends = await resp.json();
    renderFriendList();
  } catch {
    document.getElementById("friendList").innerHTML =
      '<div class="empty-state">Failed to load friends</div>';
  }
}

function renderFriendList() {
  const container = document.getElementById("friendList");

  if (_friends.length === 0) {
    container.innerHTML = '<div class="empty-state">No friends yet. Share your invite code!</div>';
    return;
  }

  let html = "";
  for (const f of _friends) {
    const isOnline = f.last_seen &&
      (Date.now() - new Date(f.last_seen).getTime()) < 35 * 60 * 1000;
    const displayName = f.display_name || f.username || f.invite_code;
    const statusText = isOnline ? "Online" : (f.last_seen ? "Last seen " + timeAgo(f.last_seen) : "Never seen");
    const selected = f.id === _selectedFriendId;

    html += `<div class="friend-card${selected ? " selected" : ""}" onclick="selectFriend(${f.id})">
      <div class="friend-avatar${isOnline ? " online" : ""}">&#9787;</div>
      <div class="friend-info">
        <div class="friend-name">${esc(displayName)}</div>
        <div class="friend-status">${esc(statusText)}</div>
      </div>
      ${f.unread_count ? `<div class="friend-unread">${f.unread_count}</div>` : ""}
    </div>`;
  }

  container.innerHTML = html;
}

async function addFriend(e) {
  e.preventDefault();
  const input = document.getElementById("addFriendInput");
  const code = input.value.trim();
  if (!code) return;

  input.disabled = true;

  try {
    const resp = await fetch("/api/p2p/friends/add", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ invite_code: code }),
    });
    const data = await resp.json();

    if (data.error) {
      alert("Failed: " + data.error);
    } else {
      input.value = "";
      loadFriends();
    }
  } catch (err) {
    alert("Error: " + err.message);
  }

  input.disabled = false;
}

async function sendInviteByEmail(e) {
  e.preventDefault();
  const input = document.getElementById("inviteEmailInput");
  const btn = document.getElementById("inviteEmailBtn");
  const status = document.getElementById("inviteEmailStatus");
  const email = input.value.trim();
  if (!email) return;

  input.disabled = true;
  btn.disabled = true;
  status.style.display = "block";
  status.style.color = "var(--text-dim)";
  status.textContent = "Sending...";

  try {
    const resp = await fetch("/api/p2p/invite-by-email", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ to_email: email }),
    });
    const data = await resp.json();

    if (resp.ok) {
      status.style.color = "#4caf50";
      status.textContent = "Invite sent!" + (data.verified_sender ? " (verified sender)" : "");
      input.value = "";
    } else {
      status.style.color = "#f44336";
      status.textContent = data.detail || data.error || "Failed to send";
    }
  } catch (err) {
    status.style.color = "#f44336";
    status.textContent = "Error: " + err.message;
  }

  input.disabled = false;
  btn.disabled = false;
  setTimeout(() => { status.style.display = "none"; }, 5000);
}

async function checkPendingAccepts() {
  try {
    const resp = await fetch("/api/p2p/pending-accepts");
    const data = await resp.json();
    if (data.added && data.added.length > 0) {
      loadFriends();
    }
  } catch { /* silent */ }
}

async function selectFriend(friendId) {
  _selectedFriendId = friendId;
  renderFriendList();
  switchFriendsTab("friend-chat");

  const friend = _friends.find(f => f.id === friendId);
  const nameEl = document.getElementById("chatFriendName");
  nameEl.textContent = friend
    ? (friend.display_name || friend.username || friend.invite_code)
    : "Chat";

  document.getElementById("p2pChatForm").style.display = "flex";
  await loadP2PMessages(friendId);
  connectChatSSE();
}

async function loadP2PMessages(friendId) {
  try {
    const resp = await fetch(`/api/p2p/friends/${friendId}/messages`);
    const messages = await resp.json();
    renderP2PMessages(messages);
    // Mark messages as read (fire-and-forget)
    fetch(`/api/p2p/friends/${friendId}/messages/read`, { method: "POST" });
  } catch {
    document.getElementById("p2pMessages").innerHTML =
      '<div class="empty-state">Failed to load messages</div>';
  }
}

function renderP2PMessages(messages) {
  const container = document.getElementById("p2pMessages");

  if (messages.length === 0) {
    container.innerHTML = '<div class="empty-state">No messages yet</div>';
    return;
  }

  let html = "";
  for (const m of messages) {
    const timeStr = new Date(m.timestamp).toLocaleTimeString([], {
      hour: "2-digit", minute: "2-digit"
    });
    html += `<div class="p2p-msg ${m.direction}">
      ${esc(m.content)}
      <div class="p2p-msg-time">${timeStr}${m.direction === "out" && !m.delivered ? " &#9201;" : ""}</div>
    </div>`;
  }

  const wasAtBottom = container.scrollTop + container.clientHeight >= container.scrollHeight - 20;
  container.innerHTML = html;
  if (wasAtBottom) {
    container.scrollTop = container.scrollHeight;
  }
}

async function sendP2PMessage(e) {
  e.preventDefault();
  const input = document.getElementById("p2pChatInput");
  const content = input.value.trim();
  if (!content || !_selectedFriendId) return;

  input.value = "";

  try {
    await fetch(`/api/p2p/friends/${_selectedFriendId}/send`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    });
    await loadP2PMessages(_selectedFriendId);
  } catch (err) {
    console.error("Send failed:", err);
  }
}

function timeAgo(dateStr) {
  const diff = Date.now() - new Date(dateStr).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return mins + "m ago";
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return hrs + "h ago";
  const days = Math.floor(hrs / 24);
  return days + "d ago";
}

// -- Settings section --------------------------------------------------------

let _settingsLoaded = false;

function switchSettingsTab(name) {
  document.querySelectorAll("[data-stab]").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.stab === name);
  });
  document.querySelectorAll(".stab-content").forEach(div => {
    div.classList.toggle("active", div.id === "stab-" + name);
  });
}

async function loadSettings() {
  // Load account info + email status + config in parallel
  const [accountResp, emailResp, configResp] = await Promise.allSettled([
    fetch("/api/p2p/account"),
    fetch("/api/p2p/email/status"),
    fetch("/config"),
  ]);

  // Account
  if (accountResp.status === "fulfilled" && accountResp.value.ok) {
    const acc = await accountResp.value.json();
    document.getElementById("settingsUsername").textContent = acc.username || "—";
    document.getElementById("settingsInviteCode").textContent = acc.invite_code || "—";

    if (acc.email) {
      document.getElementById("settingsEmailGroup").style.display = "";
      document.getElementById("settingsEmail").textContent = acc.email;
    }
  }

  // Email verification status
  if (emailResp.status === "fulfilled" && emailResp.value.ok) {
    const em = await emailResp.value.json();
    if (em.email) {
      document.getElementById("settingsEmailGroup").style.display = "";
      const badge = document.getElementById("settingsEmailBadge");
      if (em.verified) {
        badge.textContent = "Verified";
        badge.className = "settings-badge verified";
        document.getElementById("emailVerifySection").style.display = "none";
      } else {
        badge.textContent = "Not verified";
        badge.className = "settings-badge unverified";
        document.getElementById("emailVerifySection").style.display = "";
      }
    }
  }

  // Config (HQPlayer, Last.fm)
  if (configResp.status === "fulfilled" && configResp.value.ok) {
    const cfg = await configResp.value.json();

    // HQPlayer
    const hqpEnabled = cfg.hqplayer_enabled;
    document.getElementById("settingsHqpStatus").textContent = hqpEnabled ? "Enabled" : "Disabled";
    document.getElementById("settingsHqpStatus").style.color = hqpEnabled ? "var(--accent)" : "var(--text-dim)";
    document.getElementById("settingsHqpHost").textContent = cfg.hqplayer_host || "—";
    document.getElementById("settingsHqpPort").textContent = cfg.hqplayer_port || "—";

    // Last.fm
    document.getElementById("settingsLastfmUser").textContent = cfg.lastfm_username || "—";
    const lfBadge = document.getElementById("settingsLastfmBadge");
    const lfAuthBtn = document.getElementById("lastfmAuthBtn");
    if (cfg.lastfm_authorized) {
      lfBadge.textContent = "Authorized";
      lfBadge.className = "settings-badge verified";
      lfAuthBtn.style.display = "none";
    } else {
      lfBadge.textContent = "Not authorized";
      lfBadge.className = "settings-badge unverified";
      lfAuthBtn.style.display = "";
    }
  }
}

function copySettingsInviteCode() {
  const code = document.getElementById("settingsInviteCode").textContent;
  if (!code || code === "—") return;

  const btn = document.querySelector("#stab-settings-account .copy-btn");
  navigator.clipboard.writeText(code).then(() => {
    btn.textContent = "Copied!";
    btn.classList.add("copied");
    setTimeout(() => { btn.textContent = "Copy"; btn.classList.remove("copied"); }, 2000);
  }).catch(() => {
    btn.textContent = "Failed";
    setTimeout(() => { btn.textContent = "Copy"; }, 2000);
  });
}

async function sendEmailVerifyCode() {
  const btn = document.getElementById("sendVerifyBtn");
  btn.disabled = true;
  btn.textContent = "Sending...";

  try {
    const resp = await fetch("/api/p2p/email/send-code", { method: "POST" });
    const data = await resp.json();

    if (resp.ok) {
      document.getElementById("emailVerifySection").style.display = "none";
      document.getElementById("emailCodeSection").style.display = "";
      document.getElementById("emailVerifyCodeInput").focus();
    } else {
      btn.textContent = data.detail || "Error";
      setTimeout(() => { btn.textContent = "Send verification code"; btn.disabled = false; }, 3000);
    }
  } catch (err) {
    btn.textContent = "Network error";
    setTimeout(() => { btn.textContent = "Send verification code"; btn.disabled = false; }, 3000);
  }
}

async function submitEmailVerifyCode(e) {
  e.preventDefault();
  const input = document.getElementById("emailVerifyCodeInput");
  const btn = document.getElementById("verifyCodeBtn");
  const status = document.getElementById("emailVerifyStatus");
  const code = input.value.trim();
  if (!code) return;

  input.disabled = true;
  btn.disabled = true;

  try {
    const resp = await fetch("/api/p2p/email/verify-code", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    });
    const data = await resp.json();

    if (data.verified) {
      document.getElementById("emailCodeSection").style.display = "none";
      const badge = document.getElementById("settingsEmailBadge");
      badge.textContent = "Verified";
      badge.className = "settings-badge verified";
      status.style.display = "none";
    } else {
      status.style.display = "block";
      status.style.color = "#f44336";
      status.textContent = data.error || data.detail || "Invalid code";
      input.disabled = false;
      btn.disabled = false;
      input.focus();
      input.select();
    }
  } catch (err) {
    status.style.display = "block";
    status.style.color = "#f44336";
    status.textContent = "Network error";
    input.disabled = false;
    btn.disabled = false;
  }
}

async function startLastfmAuth() {
  const btn = document.getElementById("lastfmAuthBtn");
  btn.disabled = true;
  btn.textContent = "Starting...";

  try {
    const resp = await fetch("/lastfm/auth/start", { method: "POST" });
    const data = await resp.json();

    if (data.auth_url) {
      btn.style.display = "none";
      const urlDiv = document.getElementById("lastfmAuthUrl");
      const link = document.getElementById("lastfmAuthLink");
      link.href = data.auth_url;
      link.textContent = data.auth_url;
      urlDiv.style.display = "";
    } else {
      btn.textContent = "Error — try again";
      btn.disabled = false;
    }
  } catch {
    btn.textContent = "Network error";
    setTimeout(() => { btn.textContent = "Authorize scrobbling"; btn.disabled = false; }, 3000);
  }
}

async function completeLastfmAuth() {
  const status = document.getElementById("lastfmAuthStatus");
  status.style.display = "block";
  status.style.color = "var(--text-dim)";
  status.textContent = "Completing...";

  try {
    const resp = await fetch("/lastfm/auth/complete", { method: "POST" });
    const data = await resp.json();

    if (data.success) {
      status.style.color = "var(--accent)";
      status.textContent = "Authorized successfully!";
      document.getElementById("lastfmAuthUrl").style.display = "none";
      const badge = document.getElementById("settingsLastfmBadge");
      badge.textContent = "Authorized";
      badge.className = "settings-badge verified";
    } else {
      status.style.color = "#f44336";
      status.textContent = data.detail || "Authorization failed";
    }
  } catch {
    status.style.color = "#f44336";
    status.textContent = "Network error";
  }
}

// -- Discover ----------------------------------------------------------------

let discoverMode = "sound";
let radioMode = false;
let _radioQueuing = false; // prevent concurrent queue-next calls

const discoverPlaceholders = {
  sound: "energetic rock with heavy drums",
  lyrics: "songs about loneliness and rain",
  artists: "British progressive rock band from 70s",
  albums: "live recording, jazz concert",
  genres: "electronic music with African rhythms",
};

function setDiscoverMode(mode) {
  discoverMode = mode;
  document.querySelectorAll(".discover-mode").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.mode === mode);
  });
  const input = document.getElementById("discoverInput");
  input.placeholder = discoverPlaceholders[mode] || "";
  input.focus();
}

function updateDiscoverSimilar(data) {
  const section = document.getElementById("discoverSimilar");
  if (!section) return;

  const hasTrack = data.state !== "disconnected" && data.state !== "stopped" && data.song;
  section.style.display = hasTrack ? "" : "none";

  if (hasTrack) {
    const info = document.getElementById("discoverNowPlaying");
    info.innerHTML =
      '<div class="discover-similar-song">' + esc(data.song) + '</div>' +
      '<div class="discover-similar-artist">' + esc(data.artist || "") +
      (data.album ? " \u2014 " + esc(data.album) : "") + '</div>';
  }
}

function _getCurrentMediaFileId() {
  const status = _latest_status_cache;
  if (!status || !status.song) return null;
  const idx = status.track_index;
  if (currentPlaylist.length > 0 && idx !== undefined && idx >= 1) {
    const track = currentPlaylist[idx - 1];
    if (track && track.id) return track.id;
  }
  return null;
}

async function findSimilar() {
  const trackId = _getCurrentMediaFileId();
  if (!trackId) return;

  const resultsDiv = document.getElementById("discoverResults");
  resultsDiv.innerHTML = '<div class="loading"><span class="spinner"></span>Searching...</div>';

  try {
    const params = new URLSearchParams({ track_id: trackId, limit: "20" });
    const resp = await fetch("/search/similar?" + params, { method: "POST" });
    const data = await resp.json();

    if (data.error) {
      resultsDiv.innerHTML = '<div class="empty-state">' + esc(data.error) + '</div>';
      return;
    }

    renderDiscoverTracks(resultsDiv, data.results);
  } catch (err) {
    resultsDiv.innerHTML = '<div class="empty-state">Error: ' + esc(err.message) + '</div>';
  }
}

async function discoverSearch(e) {
  e.preventDefault();
  const query = document.getElementById("discoverInput").value.trim();
  if (!query) return;

  const resultsDiv = document.getElementById("discoverResults");
  const btn = document.getElementById("discoverSearchBtn");
  resultsDiv.innerHTML = '<div class="loading"><span class="spinner"></span>Searching...</div>';
  btn.disabled = true;

  try {
    let resp;
    const params = new URLSearchParams({ query: query, limit: "20" });

    switch (discoverMode) {
      case "sound":
        resp = await fetch("/search/text?" + params, { method: "POST" });
        break;
      case "lyrics":
        resp = await fetch("/search/lyrics?" + params);
        break;
      case "artists":
        resp = await fetch("/search/artists?" + params);
        break;
      case "albums":
        resp = await fetch("/search/albums?" + params);
        break;
      case "genres":
        resp = await fetch("/search/genres?" + params);
        break;
    }

    const data = await resp.json();

    if (data.error) {
      resultsDiv.innerHTML = '<div class="empty-state">' + esc(data.error) + '</div>';
    } else if (!data.results || data.results.length === 0) {
      resultsDiv.innerHTML = '<div class="empty-state">No results found</div>';
    } else if (discoverMode === "artists") {
      renderDiscoverArtists(resultsDiv, data.results);
    } else if (discoverMode === "albums") {
      renderDiscoverAlbums(resultsDiv, data.results);
    } else if (discoverMode === "genres") {
      renderDiscoverGenres(resultsDiv, data.results);
    } else {
      renderDiscoverTracks(resultsDiv, data.results);
    }
  } catch (err) {
    resultsDiv.innerHTML = '<div class="empty-state">Error: ' + esc(err.message) + '</div>';
  }

  btn.disabled = false;
}

// Feature chip toggle (radio within group)
document.addEventListener("click", (e) => {
  const chip = e.target.closest(".feature-chip");
  if (!chip) return;
  const group = chip.dataset.group;
  chip.closest(".feature-chips").querySelectorAll(".feature-chip").forEach(c => {
    c.classList.toggle("active", c === chip);
  });
});

function _getFeatureChipValue(group) {
  const active = document.querySelector('.feature-chip.active[data-group="' + group + '"]');
  return active ? active.dataset.val : "";
}

async function searchByFeatures() {
  const params = new URLSearchParams();

  const bpmMin = document.getElementById("bpmMin").value;
  const bpmMax = document.getElementById("bpmMax").value;
  if (bpmMin) params.set("bpm_min", bpmMin);
  if (bpmMax) params.set("bpm_max", bpmMax);

  const key = document.getElementById("featureKey").value;
  if (key) params.set("key", key);

  const mode = _getFeatureChipValue("mode");
  if (mode) params.set("mode", mode);

  const vocalist = _getFeatureChipValue("vocalist");
  if (vocalist) params.set("vocalist", vocalist);

  const gender = _getFeatureChipValue("gender");
  if (gender) params.set("gender", gender);

  const danceable = _getFeatureChipValue("danceable");
  if (danceable) params.set("danceable", danceable);

  const energy = _getFeatureChipValue("energy");
  if (energy === "low") { params.set("energy_max", "-22"); }
  else if (energy === "mid") { params.set("energy_min", "-22"); params.set("energy_max", "-12"); }
  else if (energy === "high") { params.set("energy_min", "-12"); }

  params.set("limit", "30");

  // At least one filter required
  if (params.toString() === "limit=30") {
    return;
  }

  const resultsDiv = document.getElementById("discoverResults");
  resultsDiv.innerHTML = '<div class="loading"><span class="spinner"></span>Searching...</div>';

  try {
    const resp = await fetch("/search/features?" + params);
    const data = await resp.json();

    if (!data.results || data.results.length === 0) {
      resultsDiv.innerHTML = '<div class="empty-state">No results found</div>';
    } else {
      renderDiscoverTracks(resultsDiv, data.results);
    }
  } catch (err) {
    resultsDiv.innerHTML = '<div class="empty-state">Error: ' + esc(err.message) + '</div>';
  }
}

// -- Radio Mode ---------------------------------------------------------------

function toggleRadio() {
  radioMode = !radioMode;
  const btn = document.getElementById("radioBtn");
  if (btn) btn.classList.toggle("active", radioMode);

  if (radioMode) {
    // If something is playing, start radio immediately
    const trackId = _getCurrentMediaFileId();
    if (trackId) _radioCheck();
  }
}

function _radioCheck() {
  if (!radioMode || _radioQueuing) return;

  const status = _latest_status_cache;
  if (!status || status.state === "disconnected" || status.state === "stopped") return;

  const idx = status.track_index; // 1-based
  if (!idx || currentPlaylist.length === 0) return;

  // Queue more when playing the last or second-to-last track
  if (idx < currentPlaylist.length - 1) return;

  const currentTrack = currentPlaylist[idx - 1];
  if (!currentTrack || !currentTrack.id) return;

  _radioQueuing = true;

  // Exclude all tracks already in the playlist
  const excludeIds = currentPlaylist.map(t => t.id);

  fetch("/api/player/queue-next", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ track_id: currentTrack.id, limit: 5, exclude_ids: excludeIds }),
  })
    .catch(e => console.error("Radio queue-next failed:", e))
    .finally(() => { _radioQueuing = false; });
  // No client-side refetch — backend bumps playlist_version on queue-next.
}

// -- Mood presets -------------------------------------------------------------

const MOOD_PRESETS = {
  chill: { bpm_max: 100, energy: "low", vocalist: "instrumental", label: "Chill Evening" },
  workout: { bpm_min: 135, energy: "high", danceable: "yes", label: "Workout" },
  focus: { energy: "mid", vocalist: "instrumental", label: "Focus" },
  party: { bpm_min: 115, energy: "high", danceable: "yes", vocalist: "vocal", label: "Party" },
  melancholy: { bpm_max: 100, mode: "minor", vocalist: "vocal", energy: "low", label: "Melancholy" },
};

function applyPreset(name) {
  const preset = MOOD_PRESETS[name];
  if (!preset) return;

  // Build params directly from preset
  const params = new URLSearchParams();
  if (preset.bpm_min) params.set("bpm_min", preset.bpm_min);
  if (preset.bpm_max) params.set("bpm_max", preset.bpm_max);
  if (preset.mode) params.set("mode", preset.mode);
  if (preset.vocalist) params.set("vocalist", preset.vocalist);
  if (preset.danceable) params.set("danceable", preset.danceable);
  if (preset.energy === "low") { params.set("energy_max", "-22"); }
  else if (preset.energy === "mid") { params.set("energy_min", "-22"); params.set("energy_max", "-12"); }
  else if (preset.energy === "high") { params.set("energy_min", "-12"); }
  params.set("limit", "30");

  const resultsDiv = document.getElementById("discoverResults");
  resultsDiv.innerHTML = '<div class="loading"><span class="spinner"></span>Searching ' + esc(preset.label) + '...</div>';

  fetch("/search/features?" + params)
    .then(r => r.json())
    .then(data => {
      if (!data.results || data.results.length === 0) {
        resultsDiv.innerHTML = '<div class="empty-state">No results for ' + esc(preset.label) + '</div>';
      } else {
        renderDiscoverTracks(resultsDiv, data.results);
      }
    })
    .catch(err => {
      resultsDiv.innerHTML = '<div class="empty-state">Error: ' + esc(err.message) + '</div>';
    });
}

// -- Discover results rendering -----------------------------------------------

function renderDiscoverTracks(container, tracks) {
  if (tracks.length === 0) {
    container.innerHTML = '<div class="empty-state">No results found</div>';
    return;
  }

  // Play All button
  let html = '<button class="discover-play-all" id="discoverPlayAll">' +
    '\u25b6 Play All (' + tracks.length + ' tracks)</button>';

  // Group by album and reuse existing renderer
  const albums = groupTracksByAlbum(tracks);
  const tmpDiv = document.createElement("div");
  renderAlbumList(tmpDiv, albums);

  container.innerHTML = html;
  container.appendChild(tmpDiv.firstChild); // results-count
  container.appendChild(tmpDiv.lastChild);  // album-list

  document.getElementById("discoverPlayAll").onclick = () => playRecommendations(tracks);
}

function renderDiscoverArtists(container, results) {
  let html = '<div class="results-count">' + results.length + ' artists</div>';

  for (const r of results) {
    const meta = [
      r.track_count + " tracks",
      r.gender,
      r.is_vocalist === true ? "vocal" : r.is_vocalist === false ? "instrumental" : null,
    ].filter(Boolean).join(" \u00B7 ");

    html +=
      '<div class="discover-card" data-artist="' + escAttr(r.artist) + '">' +
        '<div class="discover-card-body">' +
          '<div class="discover-card-title">' + esc(r.artist) + '</div>' +
          '<div class="discover-card-meta">' + esc(meta) + '</div>' +
          (r.sample_tracks ? '<div class="discover-card-meta">' + esc(r.sample_tracks) + '</div>' : '') +
        '</div>' +
        '<div class="discover-card-similarity">' + Math.round(r.similarity * 100) + '%</div>' +
      '</div>';
  }

  container.innerHTML = html;

  // Click artist card → search for their tracks in Search tab
  container.querySelectorAll(".discover-card[data-artist]").forEach(card => {
    card.addEventListener("click", () => {
      const artist = card.dataset.artist;
      searchInput.value = artist;
      switchTab("search");
      doSearch(new Event("submit"));
    });
  });
}

function renderDiscoverAlbums(container, results) {
  let html = '<div class="results-count">' + results.length + ' albums</div>';

  for (const r of results) {
    const meta = [
      r.year,
      r.track_count + " tracks",
    ].filter(Boolean).join(" \u00B7 ");

    html +=
      '<div class="discover-card" data-album="' + escAttr(r.album) + '" data-artist="' + escAttr(r.artist) + '">' +
        '<div class="discover-card-body">' +
          '<div class="discover-card-title">' + esc(r.artist) + ' \u2014 ' + esc(r.album) + '</div>' +
          '<div class="discover-card-meta">' + esc(meta) + '</div>' +
        '</div>' +
        '<div class="discover-card-similarity">' + Math.round(r.similarity * 100) + '%</div>' +
        '<button class="album-play-btn discover-album-play" title="Play album">&#9654;</button>' +
      '</div>';
  }

  container.innerHTML = html;

  // Click play button → play album
  container.querySelectorAll(".discover-album-play").forEach(btn => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const card = btn.closest(".discover-card");
      playAlbum(card.dataset.album, card.dataset.artist);
    });
  });

  // Click card body → search in Search tab
  container.querySelectorAll(".discover-card[data-album]").forEach(card => {
    card.addEventListener("click", (e) => {
      if (e.target.closest(".discover-album-play")) return;
      searchInput.value = card.dataset.artist + " " + card.dataset.album;
      switchTab("search");
      doSearch(new Event("submit"));
    });
  });
}

function renderDiscoverGenres(container, results) {
  let html = '<div class="results-count">' + results.length + ' genres</div>';

  for (const r of results) {
    const meta = [
      r.track_count + " tracks",
      r.sample_artists,
    ].filter(Boolean).join(" \u00B7 ");

    html +=
      '<div class="discover-card" data-genre="' + escAttr(r.genre) + '">' +
        '<div class="discover-card-body">' +
          '<div class="discover-card-title">' + esc(r.genre) + '</div>' +
          '<div class="discover-card-meta">' + esc(meta) + '</div>' +
        '</div>' +
        '<div class="discover-card-similarity">' + Math.round(r.similarity * 100) + '%</div>' +
      '</div>';
  }

  container.innerHTML = html;

  // Click genre card → search for tracks in that genre
  container.querySelectorAll(".discover-card[data-genre]").forEach(card => {
    card.addEventListener("click", () => {
      const genre = card.dataset.genre;
      searchInput.value = genre;
      switchTab("search");
      doSearch(new Event("submit"));
    });
  });
}

// -- Init --------------------------------------------------------------------

// Load providers on startup
loadProviders();

// Fetch playlist from HQPlayer on load
fetchPlaylist();

connectStatusSSE();

// Check for pending invite accepts once on page load
// (ongoing accepts are handled by P2P nudge mechanism)
checkPendingAccepts();

// Cleanup SSE connections on page unload
window.addEventListener("beforeunload", () => {
  if (_sseSource) _sseSource.close();
  if (_chatSSE) _chatSSE.close();
});
