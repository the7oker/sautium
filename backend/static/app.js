/* Music AI DJ — Frontend Logic */

// -- State -------------------------------------------------------------------

let currentState = "disconnected";
let chatHistory = [];
let currentPlaylist = [];

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
        quality_source: t.quality_source,
        tracks: [],
      });
    }
    albumsMap.get(key).tracks.push({
      id: t.id,
      title: t.title,
      track_number: t.track_number,
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
    const meta = [album.genre, album.quality_source, album.track_count + " tracks", duration]
      .filter(Boolean).join(" \u00B7 ");

    const albumId = album.album_id;

    html +=
      '<div class="album-card">' +
        '<div class="album-header" onclick="toggleAlbumTracks(' + albumId + ')">' +
          '<div class="album-info">' +
            '<div class="album-title">' + esc(album.artist) + " \u2014 " + esc(album.album) + "</div>" +
            '<div class="album-meta">' + esc(meta) + "</div>" +
          "</div>" +
          '<button class="album-play-btn" data-album="' + esc(album.album) +
            '" data-artist="' + esc(album.artist) + '">&#9654;</button>' +
        "</div>" +
        '<div class="album-tracks" id="tracks-' + albumId + '" style="display:none">';

    for (const t of album.tracks) {
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

// -- Chat --------------------------------------------------------------------

async function sendChat(e) {
  e.preventDefault();
  const msg = chatInput.value.trim();
  if (!msg) return;

  // Clear empty state on first message
  if (chatHistory.length === 0) {
    chatMessages.innerHTML = "";
  }

  // Add user message
  chatHistory.push({ role: "user", content: msg });
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
    const resp = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: msg, history: chatHistory.slice(-10) }),
    });
    const data = await resp.json();

    // Remove loading
    const el = document.getElementById(loadingId);
    if (el) el.remove();

    if (data.error || data.detail) {
      appendChatBubble("assistant", "Error: " + (data.detail || data.error));
    } else {
      chatHistory.push({ role: "assistant", content: data.answer });
      appendChatBubble("assistant", data.answer, data.tracks, data.retrieval_log, data.model, data.tracks_retrieved);
    }
  } catch (err) {
    const el = document.getElementById(loadingId);
    if (el) el.remove();
    appendChatBubble("assistant", "Error: " + err.message);
  }

  chatSendBtn.disabled = false;
}

function appendChatBubble(role, text, tracks, retrievalLog, model, tracksRetrieved) {
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
    let logHtml = "<summary>Retrieval pipeline (" + (tracksRetrieved || 0) + " tracks, " + esc(model || "?") + ")</summary>";
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
    playAllBtn.textContent = "▶ Play Recommendations (" + tracks.length + " tracks)";
    playAllBtn.onclick = () => playRecommendations(tracks);
    div.appendChild(playAllBtn);

    const tracksDiv = document.createElement("div");
    tracksDiv.className = "chat-tracks";
    // Group tracks by album
    const albums = groupTracksByAlbum(tracks);
    renderAlbumList(tracksDiv, albums);
    div.appendChild(tracksDiv);
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
    const num = t.track_number || (i + 1);
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

// Fetch playlist from HQPlayer on load
fetchPlaylist();

pollStatus();
setInterval(pollStatus, 3000);
