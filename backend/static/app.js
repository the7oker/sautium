/* Music AI DJ — Frontend Logic */

// -- State -------------------------------------------------------------------

let currentState = "disconnected";
let currentSessionId = null;
let sessions = [];
let sessionsLoaded = false;
let currentPlaylist = [];
let providersData = [];  // [{id, name, models}, ...]
let selectedProvider = localStorage.getItem("djProvider") || "";
let selectedModel = localStorage.getItem("djModel") || "";

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

// -- Status polling ----------------------------------------------------------

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

  // Update queue highlighting
  if (currentPlaylist.length > 0 && data.track_index !== undefined) {
    const queueItems = document.querySelectorAll(".queue-item");
    queueItems.forEach((item, idx) => {
      // HQPlayer uses 1-based indexing, JS arrays use 0-based
      item.classList.toggle("playing", idx === data.track_index - 1);
    });
  }
}

// -- Transport controls ------------------------------------------------------

async function playerCmd(cmd) {
  try {
    await fetch("/api/player/" + cmd, { method: "POST" });
    // Poll immediately after command
    setTimeout(pollStatus, 300);
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
    const resp = await fetch("/api/player/play-track", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ track_id: trackId }),
    });
    const data = await resp.json();
    if (data.ok) {
      setTimeout(fetchPlaylist, 500);
    }
    setTimeout(pollStatus, 500);
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
    const result = await resp.json();
    console.log("Play album result:", result);
    if (!resp.ok) {
      console.error("Play album error:", result);
    } else if (result.ok) {
      setTimeout(fetchPlaylist, 500);
    }
    setTimeout(pollStatus, 500);
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
    const resp = await fetch("/api/player/play-tracks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ track_ids: trackIds }),
    });
    const data = await resp.json();
    if (data.ok) {
      setTimeout(fetchPlaylist, 500);
    }
    setTimeout(pollStatus, 500);
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
    searchResults.innerHTML = '<div class="empty-state">Search error: ' + err.message + "</div>";
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
          '<button class="album-play-btn" data-album="' + esc(album.album) +
            '" data-artist="' + esc(album.artist) + '">&#9654;</button>' +
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
      '" data-provider="' + p.id + '" onclick="selectProvider(\'' + p.id + '\')">' +
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
        '" data-model="' + m + '" onclick="selectModelBtn(\'' + esc(m) + '\')">' +
        esc(shortName) + '</button>';
    }
    html += '</div>';
  }

  container.innerHTML = html;
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
      return;
    }
  }

  // Clear empty state
  const emptyState = chatMessages.querySelector(".empty-state");
  if (emptyState) emptyState.remove();

  // Add user message bubble
  appendChatBubble("user", msg);
  chatInput.value = "";
  chatSendBtn.disabled = true;

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

  // Simple markdown-like formatting: bold, newlines
  let html = esc(text)
    .replace(/\n/g, "<br>")
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\*(.+?)\*/g, "<em>$1</em>");
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

// -- Init --------------------------------------------------------------------

// Load providers on startup
loadProviders();

// Fetch playlist from HQPlayer on load
fetchPlaylist();

pollStatus();
setInterval(pollStatus, 3000);
