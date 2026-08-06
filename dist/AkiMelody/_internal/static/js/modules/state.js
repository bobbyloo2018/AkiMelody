/* ──────────────────────────────────────────────
   AkiMelody — Global State (ES Module)
   Extracted from templates/player.html
────────────────────────────────────────────── */

// Core state object — single source of truth
export const S = {
  queue: [],
  queueIdx: 0,
  playing: false,
  liked: [],
  view: 'queue',
  shuffle: false,
  repeat: 0,
  tick: null,
  playlists: [],
  activePlaylist: null,
  plTrack: null,
  layoutMode: 'card',
  theme: 'dark',
  communityShowcase: true,
  showLyrics: false,
  lyrics: { synced: false, lines: [], activeIdx: -1 },
  lyricsTid: null,
  isUserScrolling: false,
  scrollTimeout: null,
  radioMode: false,
  radioPreview: [],
  recentHistory: [],
  isManualSkip: false,
};

// DOM refs populated after DOMContentLoaded
export let AUDIO = null;
export let LYRICS_PANEL = null;
export let LYRICS_WRAPPER = null;

// Module-scoped vars (not part of reactive state)
export let trackAbort = null;
export let artistAbort = null;
export let albumAbort = null;
export let lyricsAbort = null;
export let _lyricsGen = 0;
export let manualOffset = 0;
export let _cachedLyricRows = null;
export let _playGen = 0;

// View constants
export const VIEW_IDS = ['listView', 'artistView', 'albumView', 'homeView', 'themeShopView'];

// Navigation stack
export const AkiNav = {
  stack: [],
  current: null
};

// ── Helpers ─────────────────────────────────────
export function fmt(s) {
  if (!s) return '0:00';
  const m = Math.floor(s / 60), rs = Math.floor(s % 60);
  return m + ':' + String(rs).padStart(2, '0');
}

export function dlFmtSize(b) {
  if (b < 1024) return b + ' B';
  if (b < 1048576) return (b / 1024).toFixed(1) + ' KB';
  if (b < 1073741824) return (b / 1048576).toFixed(1) + ' MB';
  return (b / 1073741824).toFixed(2) + ' GB';
}

// ── Queue persistence ───────────────────────────
export function _persistQueue() {
  try {
    const data = {
      queue: S.queue.map(t => ({
        name: t.name, title: t.title, artist: t.artist, artist_name: t.artist_name,
        art: t.art, album_art: t.album_art, dur: t.dur, duration: t.duration,
        tid: t.tid, videoId: t.videoId, albumId: t.albumId,
        local_audio: t.local_audio, local_art: t.local_art,
        _radio: t._radio || false
      })),
      queueIdx: S.queueIdx,
      radioMode: S.radioMode,
      shuffle: S.shuffle,
      repeat: S.repeat,
    };
    localStorage.setItem('akimelody_queue', JSON.stringify(data));
  } catch (e) { /* localStorage full or unavailable */ }
}

export function _restoreQueue() {
  try {
    const raw = localStorage.getItem('akimelody_queue');
    if (!raw) return false;
    const data = JSON.parse(raw);
    if (!data || !Array.isArray(data.queue) || !data.queue.length) return false;
    S.queue = data.queue;
    S.queueIdx = data.queueIdx || 0;
    S.radioMode = data.radioMode || false;
    S.shuffle = data.shuffle || false;
    S.repeat = data.repeat || 0;
    return true;
  } catch (e) { return false; }
}

// ── View management ─────────────────────────────
export function _hideAllViews() {
  VIEW_IDS.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.classList.add('hidden');
  });
}

export function _purgeDynamicContainers() {
  const listZone = document.getElementById('listZone');
  const artistTracks = document.getElementById('artistTracks');
  const albumTracks = document.getElementById('albumTracks');
  const homeZone = document.getElementById('homeZone');

  if (listZone) listZone.innerHTML = '';
  if (artistTracks) artistTracks.innerHTML = '';
  if (albumTracks) albumTracks.innerHTML = '';
  if (homeZone) homeZone.innerHTML = '';
}

// Initialize DOM refs (called from main after DOMContentLoaded)
export function initDomRefs() {
  AUDIO = document.getElementById('akiAudio');
  LYRICS_PANEL = document.getElementById('lyrics-display-panel');
  LYRICS_WRAPPER = document.getElementById('lyrics-wrapper');
}

// Mutators for module-scoped vars (used by other modules)
export function setTrackAbort(v) { trackAbort = v; }
export function setArtistAbort(v) { artistAbort = v; }
export function setAlbumAbort(v) { albumAbort = v; }
export function setLyricsAbort(v) { lyricsAbort = v; }
export function incLyricsGen() { return ++_lyricsGen; }
export function getLyricsGen() { return _lyricsGen; }
export function setManualOffset(v) { manualOffset = v; }
export function getManualOffset() { return manualOffset; }
export function setCachedLyricRows(v) { _cachedLyricRows = v; }
export function getCachedLyricRows() { return _cachedLyricRows; }
export function incPlayGen() { return ++_playGen; }
export function getPlayGen() { return _playGen; }

// ── Vendor-compat shim (Phase 1 R1) ─────────────────────────────────────────
// Legacy inline monolith reads `AkiNav.stack` / `AkiNav.current`. Re-export the
// module-scoped binding onto window.* so un-migrated callers continue to resolve.
// Removed at the end of Phase 1 once all consumers migrate to ES imports.
if (typeof window !== 'undefined') {
  window.AkiNav = AkiNav;
}