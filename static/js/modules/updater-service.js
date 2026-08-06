/**
 * UpdaterService — Automatic Background Update System for AkiMelody.
 *
 * Responsibilities:
 *   • Poll /api/update/check on boot + every 6h, compare SemVer.
 *   • When an update is available, surface a non-intrusive glass toast with
 *     [Update Now] / [Release Notes] / [Dismiss].
 *   • Drive a chunked installer download via /api/update/download, polling
 *     /api/update/status for 0–100% progress to update toast + Settings.
 *   • After download completes, transition the action button to
 *     [Restart to Update] — never auto-restarts or interrupts playback.
 *   • On [Restart to Update]: persist playback position + queue to
 *     localStorage, launch the installer via the native bridge (with a
 *     Flask-route fallback), then quit the app.
 *
 * The module owns the update state machine but does NOT own any DOM
 * directly — it dispatches via `window.UpdaterService.fire(event, payload)`
 * so the renderer (templates/player.html) can subscribe and render toast /
 * Settings UI in its own context. This keeps the module testable in
 * isolation and free of any CSS/markup coupling.
 *
 * Requires: window.apiFetch (AkiMelody's fetch wrapper). Falls back to
 * raw `fetch` with the same {?method, body} shape if apiFetch is absent —
 * so the module still loads during partial init / in tests.
 */
(function () {
  'use strict';

  // ── Constants ──────────────────────────────────────────────────────────
  var POLL_INTERVAL_MS = 6 * 60 * 60 * 1000;   // 6 hours
  var STATUS_POLL_MS = 1000;                    // while downloading
  var STATUS_POLL_MAX_MS = 90 * 1000;           // hard timeout — backend may be wedged
  var LS_KEY = 'akimelody_update_dismissed_tag'; // remember dismissed release tag

  // ── Internal state ──────────────────────────────────────────────────────
  var _state = {
    phase: 'idle', // idle | checking | available | downloading | ready | error
    localVersion: '',
    remoteVersion: '',
    releaseTag: '',
    releaseName: '',
    releaseNotes: '',
    releaseHtmlUrl: '',
    publishedAt: '',
    asset: null,                  // {name, url, size}
    progress: 0,                  // 0..100 (download percent)
    received: 0,
    total: 0,
    localPath: '',
    error: '',
    lastCheckTs: 0,
  };
  var _listeners = {};
  var _pollTimer = null;
  var _statusTimer = null;
  var _statusDeadline = 0;
  var _inited = false;

  // ── Minimal event emitter (no dep on EventEmitter) ────────────────────
  function on(event, cb) {
    (_listeners[event] = _listeners[event] || []).push(cb);
    return function () {
      _listeners[event] = (_listeners[event] || []).filter(function (c) { return c !== cb; });
    };
  }
  function fire(event, payload) {
    (_listeners[event] || []).forEach(function (cb) {
      try { cb(payload); } catch (e) { console.error('[Updater] listener', e); }
    });
  }

  // ── API helper: prefer AkiMelody's apiFetch, fall back to raw fetch ────
  function _post(endpoint, body) {
    if (typeof window.apiFetch === 'function') {
      return window.apiFetch(endpoint, { method: 'POST', body: JSON.stringify(body || {}) });
    }
    return fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    }).then(function (r) { return r.ok ? r.json() : null; }).catch(function () { return null; });
  }
  function _get(endpoint, params) {
    if (typeof window.apiFetch === 'function') {
      return window.apiFetch(endpoint, params || {});
    }
    var qs = '';
    if (params) qs = '?' + Object.keys(params).map(function (k) {
      return encodeURIComponent(k) + '=' + encodeURIComponent(params[k]);
    }).join('&');
    return fetch(endpoint + qs).then(function (r) { return r.ok ? r.json() : null; }).catch(function () { return null; });
  }

  // ── State mutator + dispatcher ─────────────────────────────────────────
  function _setState(patch) {
    var changed = false;
    for (var k in patch) {
      if (patch[k] !== _state[k]) { _state[k] = patch[k]; changed = true; }
    }
    if (changed) fire('change', _state);
    return changed;
  }

  // ── Core: check for updates ─────────────────────────────────────────────
  async function checkNow() {
    if (_state.phase === 'downloading') return _state;     // never interrupt a download
    _setState({ phase: 'checking', error: '' });
    fire('checking');
    var res = await _get('/api/update/check');
    _state.lastCheckTs = Date.now();
    if (!res || !res.ok) {
      _setState({ phase: 'error', error: (res && res.error) || 'check_failed' });
      fire('error', _state.error);
      return _state;
    }

    _setState({
      localVersion: res.local_version || _state.localVersion,
      remoteVersion: res.remote_version || '',
      releaseTag: res.release_tag || '',
      releaseName: res.release_name || '',
      releaseNotes: res.release_notes || '',
      releaseHtmlUrl: res.release_html_url || '',
      publishedAt: res.published_at || '',
      asset: res.asset || null,
    });

    if (!res.update_available) {
      _setState({ phase: 'idle' });
      return _state;
    }

    // Don't resurface a release the user already dismissed unless a NEWER
    // one appears (different release tag).
    var dismissed = '';
    try { dismissed = localStorage.getItem(LS_KEY) || ''; } catch (e) {}
    if (dismissed && dismissed === _state.releaseTag) {
      _setState({ phase: 'idle' });
      return _state;
    }

    // If an installer for this release is already on disk, skip straight to
    // the "ready" phase (e.g. user restarted without installing).
    var st = await _get('/api/update/status');
    if (st && st.status === 'ready' && st.local_path && st.release_tag === _state.releaseTag) {
      _setState({
        phase: 'ready',
        progress: 100,
        localPath: st.local_path || '',
      });
      fire('available');   // still notify so UI can show toast
      return _state;
    }

    _setState({ phase: 'available' });
    fire('available', _state);
    return _state;
  }

  // ── Core: start the background download ─────────────────────────────────
  async function startDownload() {
    if (_state.phase === 'downloading') return;
    if (!_state.asset || !_state.asset.url) {
      _setState({ phase: 'error', error: 'no_asset' });
      return;
    }
    var res = await _post('/api/update/download', {
      url: _state.asset.url,
      name: _state.asset.name,
      release_tag: _state.releaseTag,
      release_notes: _state.releaseNotes,
      release_html_url: _state.releaseHtmlUrl,
    });
    if (!res || !res.ok) {
      _setState({ phase: 'error', error: (res && res.error) || 'download_failed' });
      fire('error', _state.error);
      return;
    }
    _setState({ phase: 'downloading', progress: 0, error: '' });
    _startStatusPolling();
    fire('downloading');
  }

  // ── Core: poll /api/update/status while downloading ─────────────────────
  function _startStatusPolling() {
    if (_statusTimer) clearInterval(_statusTimer);
    _statusDeadline = Date.now() + STATUS_POLL_MAX_MS;
    _statusTimer = setInterval(async function () {
      if (Date.now() > _statusDeadline) {
        clearInterval(_statusTimer); _statusTimer = null;
        _setState({ phase: 'error', error: 'status_timeout' });
        fire('error', _state.error);
        return;
      }
      var st = await _get('/api/update/status');
      if (!st) return;
      var patch = {
        progress: st.progress || 0,
        received: st.received || 0,
        total: st.total || 0,
        localPath: st.local_path || '',
      };
      if (st.status === 'ready') {
        clearInterval(_statusTimer); _statusTimer = null;
        patch.phase = 'ready';
        _setState(patch);
        fire('ready');
      } else if (st.status === 'error') {
        clearInterval(_statusTimer); _statusTimer = null;
        patch.phase = 'error';
        patch.error = st.error || 'download_failed';
        _setState(patch);
        fire('error', patch.error);
      } else {
        _setState(patch);
        fire('progress', patch.progress);
      }
    }, STATUS_POLL_MS);
  }

  // ── Core: dismiss the update toast for this release ─────────────────────
  function dismiss() {
    if (_state.releaseTag) {
      try { localStorage.setItem(LS_KEY, _state.releaseTag); } catch (e) {}
    }
    _post('/api/update/dismiss', {});     // backend clears the available signal
    _setState({ phase: 'idle' });
    fire('dismissed');
  }

  // ── Core: Restart-to-Update ─────────────────────────────────────────────
  //  1. Persist playback position + queue (delegates to renderer via
  //     `beforeRestart` event so the renderer can save its own state).
  //  2. Launch installer (native bridge preferred; backend route fallback).
  //  3. Quit the app gracefully (native bridge preferred).
  async function restartToUpdate() {
    fire('beforeRestart');                 // give renderer a chance to save state
    // Flush AkiMelody's persistence primitives — never throw on missing refs.
    // `_persistQueue` already captures queue + queueIdx + position + playing
    // flag into localStorage, so the next launch will resume playback exactly.
    try { if (typeof window._persistQueue === 'function') window._persistQueue(); } catch (e) {}
    // Defensive: a small amount of explicit extra persistence in case the
    // renderer hasn't persisted recently (audio may not have fired the
    // periodic save yet).
    try {
      var snap = {
        ts: Date.now(),
        queueIdx: (window.S && S.queueIdx) || 0,
        position: (typeof window.AUDIO !== 'undefined' && window.AUDIO) ? (window.AUDIO.currentTime || 0) : 0,
        view: (window.S && S.view) || 'home',
      };
      localStorage.setItem('akimelody_pre_update_state', JSON.stringify(snap));
    } catch (e) {}

    // Launch — prefer the native bridge (knows elevation, working dir, quit).
    var launched = null;
    if (window.__aki__ && typeof window.__aki__.launchInstaller === 'function') {
      try {
        launched = await window.__aki__.launchInstaller(_state.localPath);
      } catch (e) { launched = { ok: false, reason: 'bridge_exception', error: String(e) }; }
    }
    if (!launched || !launched.ok) {
      // Fallback: Flask route (works in pure-Flask / browser dev mode).
      launched = await _post('/api/update/launch', {});
    }

    if (!launched || !launched.ok) {
      _setState({ phase: 'error', error: (launched && (launched.error || launched.reason)) || 'launch_failed' });
      fire('error', _state.error);
      return;
    }

    fire('launched', launched);

    // Quit — only when the installer actually started; otherwise we'd trap
    // the user with no app + a failed installer.
    if (window.__aki__ && typeof window.__aki__.quitApp === 'function') {
      try { await window.__aki__.quitApp(); } catch (e) {}
    } else {
      // Browser-only fallback: tell the user to close the app manually.
      fire('quitRequested');
    }
  }

  // ── Polling lifecycle ───────────────────────────────────────────────────
  function _schedulePoll() {
    if (_pollTimer) clearInterval(_pollTimer);
    _pollTimer = setInterval(function () {
      // Skip if document hidden — the WV background-timerdisable flag still
      // gives us visibility events, so we can defer work until foreground.
      if (document.hidden) return;
      checkNow();
    }, POLL_INTERVAL_MS);
  }

  function init() {
    if (_inited) return _state;
    _inited = true;
    // Initial check after a short delay so we don't compete with boot-critical
    // API calls (queue restore, playlists load, YT auth check).
    setTimeout(checkNow, 8000);
    _schedulePoll();
    return _state;
  }

  function getStatus() { return _state; }

  // ── Public surface ──────────────────────────────────────────────────────
  window.UpdaterService = {
    init: init,
    checkNow: checkNow,
    startDownload: startDownload,
    dismiss: dismiss,
    restartToUpdate: restartToUpdate,
    getStatus: getStatus,
    on: on,
    fire: fire,
    POLL_INTERVAL_MS: POLL_INTERVAL_MS,
  };
})();
