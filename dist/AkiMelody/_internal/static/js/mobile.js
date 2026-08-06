/* ──────────────────────────────────────────────
   GLOBAL STATE
────────────────────────────────────────────── */
const S = {
  queue: [],
  queueIdx: -1,
  activeView: 'player',
  shuffle: false,
  repeat: 0,   // 0=off, 1=repeat all, 2=repeat one
  radioMode: true,
  liked: [],
  recentHistory: [],
  isManualSkip: false,
  radioExhausted: false,
  playGen: 0,
  libGen: 0,
  searchGen: 0,
  theme: 'dark',
  layoutMode: 'card'
};

const AUDIO = document.getElementById('akiAudio');

/* ──────────────────────────────────────────────
   ARTWORK (mirrors templates/player.html)
────────────────────────────────────────────── */
function resolveArtUrl(track) {
  if (!track) return '';
  if (track.local_art && track.tid) return `/api/local_file?q=${track.tid}.jpg`;
  return track.album_art || track.art || track.thumbnail || '';
}

const PLACEHOLDER_ART = 'static/assets/placeholder.png';

function imgOnErrorFallback(img) {
  img.src = PLACEHOLDER_ART;
  img.onerror = null;
}

/* ──────────────────────────────────────────────
   VIEW CONTROLLER
────────────────────────────────────────────── */
function mSwitchView(viewId, pushState) {
    S.activeView = viewId;

    if (pushState !== false) {
        history.pushState({ view: viewId }, '', `#${viewId}`);
    }

    // Toggle active views
    document.querySelectorAll('.mobile-view').forEach(v => v.classList.remove('active'));
    const targetView = document.getElementById(`view-${viewId}`);
    if(targetView) targetView.classList.add('active');

    // Toggle active nav buttons
    document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
    const targetNav = document.getElementById(`nav-${viewId}`);
    if(targetNav) targetNav.classList.add('active');

    if (viewId === 'library') {
        mRenderLibrary();
    } else if (viewId === 'search') {
        const resultsContainer = document.getElementById('m-search-results');
        if (resultsContainer && resultsContainer.innerHTML === '') {
            resultsContainer.innerHTML = '<div style="text-align:center; padding:40px; opacity:0.3;"><i class="fa-solid fa-bolt" style="font-size:40px; margin-bottom:15px; display:block;"></i>Ready to Discover</div>';
        }
    }

    // Animate the activated view's primary content with a small stagger.
    // Respects prefers-reduced-motion and handles dynamic children added by renderers.
    try {
        if(!window.matchMedia || !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
            if (!targetView) return;

            // Remove any existing animate class so reflow can restart it
            targetView.querySelectorAll('.animate-pop-content').forEach(el => {
                el.classList.remove('animate-pop-content');
                el.style.animationDelay = '';
            });

            // Candidate selectors for cards, shelves, and grid items inside the view
            const selectors = [
                ':scope > *',
                '.list-card',
                '.card',
                '.card-art',
                '.artwork-container',
                '.results-list > *',
                '.player-controls',
                '.view-header',
                '.player-header',
                '.card-info',
                '.list-card'
            ];

            const nodeSet = new Set();
            selectors.forEach(sel => {
                try {
                    targetView.querySelectorAll(sel).forEach(n => nodeSet.add(n));
                } catch (e) { /* ignore invalid selectors in some contexts */ }
            });

            const items = Array.from(nodeSet).filter(Boolean);

            // Force a synchronous reflow to reset animations
            void targetView.offsetWidth;

            items.forEach((el, i) => {
                // Add class and micro-stagger via inline delay so dynamically added nodes behave predictably
                el.classList.add('animate-pop-content');
                el.style.animationDelay = (i * 40) + 'ms';

                const cleanup = () => {
                    // Keep DOM clean: remove class and reset inline styles after the animation completes
                    el.classList.remove('animate-pop-content');
                    el.style.animationDelay = '';
                    el.removeEventListener('animationend', cleanup);
                };
                el.addEventListener('animationend', cleanup);
            });
        }
    } catch (e) {
        console.error('Animation apply failed', e);
    }
}


/* ──────────────────────────────────────────────
   CORE AUDIO ENGINE
────────────────────────────────────────────── */
async function mPlayTrack(i, newQueue = null) {
  if (newQueue) { 
    S.queue = [...newQueue]; 
    S.queueIdx = 0; 
    S.radioExhausted = false;
  } else { 
    S.queueIdx = i; 
  }
  
  if (S.queueIdx < 0 || S.queueIdx >= S.queue.length) return;
  
  const t = S.queue[S.queueIdx];
  const gen = ++S.playGen;

  AUDIO.pause();
  AUDIO.src = '';

  mLoadTrackUI(S.queueIdx);

  // Radio Bootstrap
  if ( S.radioMode && S.queue.length <= 1 && t.videoId) {
    mTriggerSmartFetch(t.videoId, false);
  }

  try {
    const params = { 
        q: (t.artist_name || t.artist || '') + ' ' + (t.title || t.name || '') + ' audio', 
        tid: t.tid || '', 
        vid: t.videoId || '' 
    };
    const res = await mApiFetch('/api/stream', params);
    if(gen !== S.playGen) return;
    if(res && res.url) {
      AUDIO.src = res.url;
      AUDIO.play().then(() => {
        if(gen !== S.playGen) return;
        mSetPlayState(true);
      }).catch(e => {
        console.warn('Autoplay prevented', e);
        mSetPlayState(false);
      });
    } else {
      console.error("No stream URL returned");
      mShowNotification("Playback failed");
    }
  } catch (e) {
    if(gen !== S.playGen) return;
    console.error("Stream fetch error:", e);
    mShowNotification("Network error");
  }
}

function mLoadTrackUI(i) {
  const t = S.queue[i];
  if(!t) return;

  document.getElementById('m-title').textContent = t.title || t.name || "Unknown Track";
  document.getElementById('m-artist').textContent = t.artist_name || t.artist || "Unknown Artist";
  document.getElementById('m-time-total').textContent = t.duration || mFmt(t.dur) || '3:30';
  
  const artUrl = resolveArtUrl(t);
  const artImg = document.getElementById('m-art');
  if(artImg && !artImg._akiFallbackBound) {
    artImg.onerror = imgOnErrorFallback;
    artImg._akiFallbackBound = true;
  }
  const ph = document.getElementById('m-ph');
  const bg = document.getElementById('mobile-bg-artwork');

  if(artUrl) {
    artImg.src = artUrl;
    artImg.style.display = 'block';
    if(ph) ph.style.display = 'none';
    if(bg) bg.style.backgroundImage = `url(${artUrl})`;
  } else {
    artImg.style.display = 'none';
    if(ph) ph.style.display = 'flex';
    if(bg) bg.style.backgroundImage = '';
  }
  mUpdateLikeButton();
}

function mTogglePlay() {
  if(!AUDIO.src && S.queue.length > 0) { mPlayTrack(0); return; }
  if(AUDIO.paused) {
    AUDIO.play().catch(e => console.warn('Play error', e));
  } else {
    AUDIO.pause();
  }
}

function mSetPlayState(playing) {
  S.playing = playing;
  const icon = document.getElementById('m-play-icon');
  if(!icon) return;
  icon.className = playing ? 'fa-solid fa-pause' : 'fa-solid fa-play';
}

let _mLastTimeUpdate = 0;
AUDIO.ontimeupdate = () => {
  if(!AUDIO.duration) return;
  const now = performance.now();
  if(now - _mLastTimeUpdate < 250) return;
  _mLastTimeUpdate = now;
  const pct = (AUDIO.currentTime / AUDIO.duration) * 100;
  document.getElementById('m-progress').value = pct;
  document.getElementById('m-time-cur').textContent = mFmt(AUDIO.currentTime);
};

AUDIO.onplay = () => mSetPlayState(true);
AUDIO.onpause = () => mSetPlayState(false);
AUDIO.onerror = () => mSetPlayState(false);
AUDIO.onended = () => {
  if(S.repeat === 2) {
    mPlayTrack(S.queueIdx);
  } else if(S.radioMode && !S.isManualSkip && !S.radioExhausted) {
    const cur = S.queue[S.queueIdx];
    if(cur && cur.videoId) mTriggerSmartFetch(cur.videoId, true);
    else mNext();
  } else {
    mNext();
  }
  S.isManualSkip = false;
};

function mNext(manual) {
  if(manual) S.isManualSkip = true;
  if (S.shuffle && S.queue.length > 1) {
    let nextIdx;
    do { nextIdx = Math.floor(Math.random() * S.queue.length); } while (nextIdx === S.queueIdx);
    mPlayTrack(nextIdx);
    return;
  }
  if(S.queueIdx < S.queue.length - 1) {
    mPlayTrack(S.queueIdx + 1);
  } else if(S.repeat === 1 && S.queue.length > 0) {
    mPlayTrack(0);
  } else if(S.radioMode && S.queueIdx >= 0 && !S.radioExhausted) {
    const cur = S.queue[S.queueIdx];
    if(cur && cur.videoId) mTriggerSmartFetch(cur.videoId, true);
  }
}

function mPrev(manual) {
  if(manual) S.isManualSkip = true;
  if(S.queueIdx > 0) mPlayTrack(S.queueIdx - 1);
}

function mSeek(val) {
  if(!AUDIO.duration) return;
  AUDIO.currentTime = (val / 100) * AUDIO.duration;
}

function mToggleRadio() {
  S.radioMode = !S.radioMode;
  const btn = document.getElementById('m-radio-btn');
  if(btn) btn.classList.toggle('active', S.radioMode);
  mShowNotification(S.radioMode ? "Radio Mode On" : "Radio Mode Off");
}

function mToggleShuffle() {
  S.shuffle = !S.shuffle;
  const btn = document.getElementById('m-shuffle-btn');
  if(btn) btn.classList.toggle('active', S.shuffle);
  mShowNotification(S.shuffle ? "Shuffle On" : "Shuffle Off");
}

function mToggleRepeat() {
  S.repeat = (S.repeat + 1) % 3;
  const btn = document.getElementById('m-repeat-btn');
  if(btn) {
    btn.classList.toggle('active', S.repeat > 0);
    const icon = btn.querySelector('i');
    if(icon) icon.className = S.repeat === 2 ? 'fa-solid fa-repeat-1' : 'fa-solid fa-repeat';
  }
  const labels = ["Repeat Off", "Repeat All", "Repeat One"];
  mShowNotification(labels[S.repeat]);
}

let _likedTids = new Set();
async function mLoadLiked() {
  try {
    const res = await mApiFetch('/api/favorites');
    if(res && Array.isArray(res)) {
      S.liked = res;
      _likedTids = new Set(res.map(t => t.tid).filter(Boolean));
    }
  } catch(e) {}
}

function mUpdateLikeButton() {
  const t = S.queue[S.queueIdx];
  if(!t) return;
  const btn = document.getElementById('m-like-btn');
  if(!btn) return;
  const icon = btn.querySelector('i');
  const isLiked = _likedTids.has(t.tid);
  if(icon) icon.className = isLiked ? 'fa-solid fa-heart' : 'fa-regular fa-heart';
  btn.classList.toggle('active', isLiked);
}

async function mHandleLike() {
  const t = S.queue[S.queueIdx];
  if(!t || !t.tid) return;
  const btn = document.getElementById('m-like-btn');
  const icon = btn ? btn.querySelector('i') : null;
  if(_likedTids.has(t.tid)) {
    _likedTids.delete(t.tid);
    S.liked = S.liked.filter(x => x.tid !== t.tid);
    if(icon) icon.className = 'fa-regular fa-heart';
    if(btn) btn.classList.remove('active');
    mShowNotification("Removed from favorites");
  } else {
    _likedTids.add(t.tid);
    S.liked.push({ name: t.name || t.title, artist: t.artist || t.artist_name, art: t.art || t.album_art, dur: t.dur, tid: t.tid, videoId: t.videoId, albumId: t.albumId });
    if(icon) icon.className = 'fa-solid fa-heart';
    if(btn) btn.classList.add('active');
    mShowNotification("Added to favorites");
  }
  try { await fetch('/api/save_favorites', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(S.liked) }); } catch(e) {}
}

function mRemoveFromQueue(idx) {
  if(idx < 0 || idx >= S.queue.length) return;
  const wasPlaying = (idx === S.queueIdx);
  S.queue.splice(idx, 1);
  if(wasPlaying) {
    if(S.queue.length === 0) {
      S.queueIdx = -1;
      AUDIO.pause();
      AUDIO.src = '';
      document.getElementById('m-title').textContent = 'No Track';
      document.getElementById('m-artist').textContent = '';
      document.getElementById('m-time-total').textContent = '0:00';
      document.getElementById('m-time-cur').textContent = '0:00';
      document.getElementById('m-progress').value = 0;
      const artImg = document.getElementById('m-art');
      const ph = document.getElementById('m-ph');
      if(artImg) artImg.style.display = 'none';
      if(ph) ph.style.display = 'flex';
    } else {
      S.queueIdx = Math.min(idx, S.queue.length - 1);
      mPlayTrack(S.queueIdx);
    }
  } else if(idx < S.queueIdx) {
    S.queueIdx--;
  }
  mRenderLibrary();
}

/* ──────────────────────────────────────────────
   DATA FETCHING & RENDERING
────────────────────────────────────────────── */
async function mRenderLibrary() {
    const container = document.getElementById('m-library-list');
    if(!container) return;
    const gen = ++S.libGen;
    container.innerHTML = '<div style="text-align:center; padding:20px; opacity:0.5;">Loading Library...</div>';
    
    if (!S.liked || S.liked.length === 0) {
      try {
        const favs = await mApiFetch('/api/favorites');
        if(gen !== S.libGen) return;
        if (favs) S.liked = favs;
      } catch(e) { console.error("Load favs failed", e); if(gen !== S.libGen) return; }
    }
    
    container.innerHTML = '';
    
    // Add Queue Section
    if(S.queue.length > 0) {
      const qHeader = document.createElement('div');
      qHeader.innerHTML = '<h4 style="margin:10px 0; opacity:0.6; font-size:14px;">Playing Next</h4>';
      container.appendChild(qHeader);
      
      S.queue.forEach((t, i) => {
        const card = mBuildTrackCard(t, () => mPlayTrack(i));
        const removeBtn = document.createElement('button');
        removeBtn.className = 'card-remove-btn';
        removeBtn.innerHTML = '<i class="fa-solid fa-xmark"></i>';
        removeBtn.style.cssText = 'position:absolute; right:8px; top:50%; transform:translateY(-50%); background:none; border:none; color:var(--text-dim); font-size:16px; padding:8px; cursor:pointer; opacity:0.5; z-index:2;';
        removeBtn.onclick = (e) => { e.stopPropagation(); mRemoveFromQueue(i); };
        card.style.position = 'relative';
        card.appendChild(removeBtn);
        container.appendChild(card);
      });
    }

    const favHeader = document.createElement('div');
    favHeader.innerHTML = '<h4 style="margin:20px 0 10px 0; opacity:0.6; font-size:14px;">Favorites</h4>';
    container.appendChild(favHeader);

    const list = S.liked || [];
    if (list.length === 0) {
        const empty = document.createElement('div');
        empty.style.textAlign = 'center'; empty.style.padding = '20px'; empty.style.opacity = '0.5';
        empty.textContent = 'Your library is empty';
        container.appendChild(empty);
    } else {
      list.forEach((t, i) => {
          container.appendChild(mBuildTrackCard(t, () => {
              mHandleSearchSelect(t);
          }));
      });
    }
}

function mBuildTrackCard(t, onClick) {
    const card = document.createElement('div');
    card.className = 'list-card';
    const art = t.local_art ? `/api/local_file?q=${t.tid}.jpg` : (t.album_art || t.art || t.thumbnail);
    const isActive = (S.queueIdx >= 0 && S.queue[S.queueIdx] && S.queue[S.queueIdx].tid === t.tid);
    if(isActive) card.classList.add('active');
    
    const artDiv = document.createElement('div');
    artDiv.className = 'card-art';
    if(art) {
        const img = document.createElement('img');
        img.src = art;
        img.decoding = 'async';
        img.onerror = imgOnErrorFallback;
        artDiv.appendChild(img);
    } else {
        const fb = document.createElement('div');
        fb.style.cssText = 'width:100%;height:100%;background:rgba(255,255,255,0.05);display:flex;align-items:center;justify-content:center;';
        const icon = document.createElement('i');
        icon.className = 'fa-solid fa-music';
        icon.style.color = 'rgba(255,255,255,0.15)';
        fb.appendChild(icon);
        artDiv.appendChild(fb);
    }

    const infoDiv = document.createElement('div');
    infoDiv.className = 'card-info';
    const titleDiv = document.createElement('div');
    titleDiv.className = 'card-title';
    if(isActive) { const dot = document.createElement('span'); dot.className = 'status-dot'; titleDiv.appendChild(dot); }
    titleDiv.appendChild(document.createTextNode(t.title || t.name || 'Unknown'));
    const artistDiv = document.createElement('div');
    artistDiv.className = 'card-artist';
    artistDiv.textContent = t.artist_name || t.artist || 'Unknown Artist';
    infoDiv.appendChild(titleDiv);
    infoDiv.appendChild(artistDiv);

    const metaDiv = document.createElement('div');
    metaDiv.className = 'card-meta';
    metaDiv.textContent = t.duration || mFmt(t.dur) || '3:30';

    card.appendChild(artDiv);
    card.appendChild(infoDiv);
    card.appendChild(metaDiv);
    card.onclick = onClick;
    return card;
}

/* ──────────────────────────────────────────────
   SEARCH HANDLER
────────────────────────────────────────────── */
async function mHandleSearch() {
    const input = document.getElementById('m-search-input');
    if(!input || !input.value.trim()) return;
    
    const q = input.value.trim();
    const gen = ++S.searchGen;
    input.blur(); 
    
    const filter = document.getElementById('m-search-filter').value;
    const resultsContainer = document.getElementById('m-search-results');
    
    resultsContainer.innerHTML = '<div style="text-align:center; padding:20px; opacity:0.5;">Searching...</div>';
    
    try {
        const res = await mApiFetch('/api/search', { q, filter });
        if(gen !== S.searchGen) return;
        resultsContainer.innerHTML = '';
        
        if(res && res.length) {
            res.forEach(item => {
                if(filter === 'album') {
                    resultsContainer.appendChild(mBuildTrackCard({
                        title: item.title || item.name,
                        artist: item.artist,
                        art: item.art,
                        duration: item.trackCount ? item.trackCount + ' tracks' : ''
                    }, () => {
                        mHandleAlbumSelect(item);
                    }));
                } else if(filter === 'artist' || (item.browseId && !item.videoId)) {
                    resultsContainer.appendChild(mBuildTrackCard({
                        title: item.title || item.name || 'Unknown Artist',
                        artist: item.subscribers || '',
                        art: item.art,
                        duration: ''
                    }, () => {
                        mShowNotification("Artist playback not supported on mobile");
                    }));
                } else {
                    resultsContainer.appendChild(mBuildTrackCard(item, () => {
                        mHandleSearchSelect(item);
                    }));
                }
            });
        } else {
            resultsContainer.innerHTML = '<div style="text-align:center; padding:40px; opacity:0.3;"><i class="fa-solid fa-magnifying-glass" style="font-size:30px; margin-bottom:10px; display:block;"></i>No results found</div>';
        }
    } catch (e) {
        if(gen !== S.searchGen) return;
        console.error("Search failed:", e);
        resultsContainer.innerHTML = '<div style="text-align:center; padding:20px; opacity:0.5;">Search failed</div>';
    }
}

function mHandleSearchSelect(track) {
    const isNothingPlaying = AUDIO.paused && (AUDIO.currentTime === 0 || S.queueIdx === -1);
    
    if (isNothingPlaying) {
        mPlayTrack(0, [track]);
        mSwitchView('player');
    } else {
        S.queue.push(track);
        mShowNotification("Added to queue");
    }
}

async function mHandleAlbumSelect(album) {
    const browseId = album.browseId || album.albumId;
    if(!browseId) return;
    try {
        const data = await mApiFetch('/api/album', { albumId: browseId });
        if(data && data.tracks && data.tracks.length) {
            mPlayTrack(0, data.tracks);
            mSwitchView('player');
        } else {
            mShowNotification("No tracks found in album");
        }
    } catch(e) {
        console.error("Album fetch failed:", e);
        mShowNotification("Failed to load album");
    }
}

/* ──────────────────────────────────────────────
   UTILITIES
────────────────────────────────────────────── */
async function mApiFetch(endpoint, params = {}, method = 'GET', timeoutMs = 15000) {
  const opts = { method };
  let url;
  if (method === 'POST') {
    opts.headers = { 'Content-Type': 'application/json' };
    opts.body = JSON.stringify(params);
    url = endpoint;
  } else {
    url = new URL(endpoint, window.location.origin);
    Object.entries(params).forEach(([k,v]) => { 
      if(v !== undefined && v !== null && v !== '') url.searchParams.set(k,v); 
    });
  }
  const controller = new AbortController();
  opts.signal = controller.signal;
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const r = await fetch(url, opts);
    if(!r.ok) throw new Error("API error: " + r.status);
    return r.json();
  } finally {
    clearTimeout(timer);
  }
}

function mFmt(s) {
  if(!s || isNaN(s)) return '0:00';
  const m = Math.floor(s/60);
  const sec = Math.floor(s%60);
  return m + ':' + (sec<10?'0':'') + sec;
}

async function mTriggerSmartFetch(vid, shouldSkip = true) {
  const activeIds = S.queue.map(t => t.videoId).filter(Boolean).join(',');
  const historyIds = S.recentHistory.join(',');
  
  try {
    const res = await mApiFetch('/api/radio/suggest', { 
        vid, 
        active_queue: activeIds,
        history: historyIds
    });
    if(S.radioMode && res && res.length) {
        const existingIds = new Set(S.queue.map(t => t.videoId).filter(Boolean));
        const newTracks = res.filter(t => t.videoId && !existingIds.has(t.videoId));
        if(newTracks.length) S.queue = [...S.queue, ...newTracks];
        S.radioExhausted = false;
        if(S.activeView === 'library') mRenderLibrary();
    } else {
        S.radioExhausted = true;
        mShowNotification("Radio queue exhausted");
    }
    if(shouldSkip) mNext();
  } catch(e) {
    console.error("Smart fetch failed", e);
    S.radioExhausted = true;
    mShowNotification("Radio queue exhausted");
    if(shouldSkip) mNext();
  }
}

function mShowNotification(msg) {
  const toast = document.getElementById('notification-toast');
  if(!toast) return;
  toast.textContent = msg;
  toast.style.display = 'block';
  if(window._mToastTimeout) clearTimeout(window._mToastTimeout);
  window._mToastTimeout = setTimeout(() => { toast.style.display = 'none'; }, 2500);
}

// Settings Handlers
function mToggleSettings() {
    const modal = document.getElementById('modal-settings');
    if(!modal) return;
    const isShowing = modal.style.display === 'flex';
    modal.style.display = isShowing ? 'none' : 'flex';
}

function mToggleFullscreen() {
    const toggle = document.getElementById('toggle-fullscreen');
    if(!toggle) return;
    const isActive = toggle.classList.toggle('active');
    localStorage.setItem('m-fullscreen', isActive);
    
    if (isActive) {
        if (!document.fullscreenElement) {
            document.documentElement.requestFullscreen().catch(e => {
                console.warn('Fullscreen request failed', e);
            });
        }
    } else {
        if (document.fullscreenElement) {
            document.exitFullscreen();
        }
    }
}

function mSyncThemeToggle() {
    const toggle = document.getElementById('toggle-theme');
    const darkLabel = document.getElementById('themeLabelDark');
    const lightLabel = document.getElementById('themeLabelLight');
    if(toggle) toggle.classList.toggle('active', S.theme === 'light');
    if(darkLabel) darkLabel.style.color = S.theme === 'dark' ? 'var(--accent)' : 'var(--text-dim)';
    if(lightLabel) lightLabel.style.color = S.theme === 'light' ? 'var(--accent)' : 'var(--text-dim)';
}

function mSetTheme(themeName) {
    S.theme = themeName;
    document.body.setAttribute('data-theme', themeName);
    localStorage.setItem('akimelody_theme', themeName);
    mSyncThemeToggle();
}

function mToggleTheme() {
    const newTheme = S.theme === 'dark' ? 'light' : 'dark';
    mSetTheme(newTheme);
}

function mSyncLayoutToggle() {
    const toggle = document.getElementById('toggle-layout');
    const listLabel = document.getElementById('layoutLabelList');
    const cardLabel = document.getElementById('layoutLabelCard');
    if(toggle) toggle.classList.toggle('active', S.layoutMode === 'card');
    if(listLabel) listLabel.style.color = S.layoutMode === 'list' ? 'var(--accent)' : 'var(--text-dim)';
    if(cardLabel) cardLabel.style.color = S.layoutMode === 'card' ? 'var(--accent)' : 'var(--text-dim)';
}

async function mToggleLayout() {
    try {
        const res = await mApiFetch('/api/settings/toggle_layout', {}, 'POST');
        if(res && res.ui_layout_mode) {
            S.layoutMode = res.ui_layout_mode;
            mSyncLayoutToggle();
            if(S.activeView === 'library') mRenderLibrary();
        }
    } catch(e) { console.error("Layout toggle failed", e); }
}

// Initial View & Fullscreen Check
document.addEventListener('DOMContentLoaded', () => {
    const initialView = location.hash.replace('#', '') || 'player';
    mSwitchView(initialView, false);
    history.replaceState({ view: initialView }, '', `#${initialView}`);
    
    // Initialize Theme
    const savedTheme = localStorage.getItem('akimelody_theme') || 'dark';
    mSetTheme(savedTheme);
    
    // Initialize Layout Mode
    mApiFetch('/api/settings').then(res => {
        if(res && res.ui_layout_mode) S.layoutMode = res.ui_layout_mode;
        mSyncLayoutToggle();
    }).catch(() => {});
    
    // Initialize Fullscreen
    const fs = localStorage.getItem('m-fullscreen') === 'true';
    if(fs) {
        const toggle = document.getElementById('toggle-fullscreen');
        if(toggle) toggle.classList.add('active');
    }

    // Initialize favorites
    mLoadLiked();

    // Swipe gestures on artwork
    const artContainer = document.querySelector('.artwork-container');
    if(artContainer) {
        let touchStartX = 0, touchStartY = 0, swiping = false;
        artContainer.addEventListener('touchstart', (e) => {
            touchStartX = e.changedTouches[0].clientX;
            touchStartY = e.changedTouches[0].clientY;
            swiping = true;
        }, { passive: true });
        artContainer.addEventListener('touchend', (e) => {
            if(!swiping) return;
            swiping = false;
            const dx = e.changedTouches[0].clientX - touchStartX;
            const dy = e.changedTouches[0].clientY - touchStartY;
            if(Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 1.5) {
                if(dx > 0) mPrev(true);
                else mNext(true);
            }
        }, { passive: true });
    }
    
    // Global Error Catcher
    window.onerror = (msg, url, line) => {
        console.error("Mobile error:", msg, "at", line);
    };
    window.addEventListener('unhandledrejection', (e) => {
        console.error("Mobile unhandled promise:", e.reason);
    });
});

window.addEventListener('popstate', (e) => {
    const view = (e.state && e.state.view) || 'player';
    mSwitchView(view, false);
});
