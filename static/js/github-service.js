/* ════════════════════════════════════════════════════════════════
   AkiMelody — GitHub Service (Device Flow)
   Public-client-id only: GitHub's Device Flow needs NO client secret,
   NO redirect URI, and NO PKCE. The user enters a short code on
   github.com/login/device to approve the app.

   Auth payload is stored under:
     localStorage['aki_github_token'] = access token
     localStorage['aki_github_user']  = JSON { username, avatarUrl }

   Flow:
     signIn() → POST /api/github/device/code → { user_code, verification_uri }
       → open github.com/login/device in the webview
       → poll POST /api/github/device/token until approved
       → store token + user
   ════════════════════════════════════════════════════════════════ */
(function (global) {
  'use strict';

  var TOKEN_KEY = 'aki_github_token';
  var USER_KEY = 'aki_github_user';
  var CONFIG_URL = '/api/github/config';
  var DEVICE_CODE_URL = '/api/github/device/code';
  var DEVICE_TOKEN_URL = '/api/github/device/token';
  var THEMES_URL = '/api/github/themes';
  var PLAYLISTS_URL = '/api/community-playlists';

  var _pendingResolve = null;
  var _pendingReject = null;

  function safeGet(key) {
    try { return global.localStorage.getItem(key); } catch (e) { return null; }
  }
  function safeSet(key, val) {
    try { global.localStorage.setItem(key, val); } catch (e) {}
  }
  function safeRemove(key) {
    try { global.localStorage.removeItem(key); } catch (e) {}
  }

  function parseUser(raw) {
    if (!raw) return null;
    try {
      var u = JSON.parse(raw);
      return (u && typeof u === 'object') ? u : null;
    } catch (e) { return null; }
  }

  function _rejectAuth(err) {
    if (_pendingReject) {
      var r = _pendingReject;
      _pendingResolve = null; _pendingReject = null;
      r(err || new Error('GitHub sign-in failed.'));
    }
  }

  var GitHubService = {
    TOKEN_KEY: TOKEN_KEY,
    USER_KEY: USER_KEY,

    /* ── Token / user persistence ── */
    getToken: function () { return safeGet(TOKEN_KEY); },
    getUser: function () { return parseUser(safeGet(USER_KEY)); },
    storeAuth: function (token, user) {
      safeSet(TOKEN_KEY, token);
      safeSet(USER_KEY, JSON.stringify(user || {}));
    },
    clearAuth: function () {
      safeRemove(TOKEN_KEY);
      safeRemove(USER_KEY);
      _rejectAuth(new Error('Signed out.'));
    },
    getAuthState: function () {
      var u = parseUser(safeGet(USER_KEY));
      var token = safeGet(TOKEN_KEY);
      return {
        authenticated: !!(token && u),
        username: (u && u.username) || null,
        avatarUrl: (u && u.avatarUrl) || null,
      };
    },

    /* ── Profile verification ──
       Validates the stored token against GitHub's API and refreshes the
       cached user. Returns Promise<{authenticated, user}>. */
    verifySession: function () {
      var token = safeGet(TOKEN_KEY);
      if (!token) return Promise.resolve({ authenticated: false, user: null });
      return global.fetch('https://api.github.com/user', {
        headers: { 'Authorization': 'Bearer ' + token, 'Accept': 'application/json' },
      }).then(function (r) {
        if (!r.ok) return { authenticated: false, user: null };
        return r.json().then(function (ud) {
          var user = { username: ud.login || '', avatarUrl: ud.avatar_url || '' };
          GitHubService.storeAuth(token, user);
          return { authenticated: true, user: user };
        });
      }).catch(function () {
        return { authenticated: false, user: null };
      });
    },

    /* ── Device Flow sign-in ──
       1. Request a device code from the backend.
       2. Open github.com/login/device in the webview so the user can enter
          the code and approve the app.
       3. Poll the backend for the access token until approved or timeout.
       No redirect URI needed — the Device Flow works entirely via polling.

       opts (optional):
         skipNavigation — don't navigate the main window (caller handles UI)
         onDeviceCode   — callback({ userCode, verificationUri }) when code arrives */
    signIn: function (opts) {
      opts = opts || {};
      return new Promise(function (resolve, reject) {
        var existing = GitHubService.getAuthState();
        if (existing.authenticated) { resolve(existing); return; }
        _pendingResolve = resolve;
        _pendingReject = reject;

        /* Step 1: Get device code */
        global.fetch(DEVICE_CODE_URL, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: '{}',
        }).then(function (r) {
          if (!r.ok) throw new Error('GitHub device code request failed (' + r.status + ')');
          return r.json();
        }).then(function (d) {
          if (!d || !d.ok) throw new Error((d && d.error) || 'Could not start GitHub sign-in.');

          var userCode = d.user_code || '';
          var verificationUri = d.verification_uri || 'https://github.com/login/device';
          var deviceCode = d.device_code || '';
          var interval = d.interval || 5;
          var expiresAt = Date.now() + (d.expires_in || 900) * 1000;

          /* Notify caller of device code (for modal display) */
          if (typeof opts.onDeviceCode === 'function') {
            opts.onDeviceCode({ userCode: userCode, verificationUri: verificationUri });
          }

          /* Step 2: Navigate (unless caller handles it) */
          if (!opts.skipNavigation) {
            if (window.__aki__ && window.__aki__.openInWebView) {
              window.__aki__.openInWebView(verificationUri);
            } else {
              global.location.href = verificationUri;
            }
            if (typeof showNotification === 'function') {
              showNotification('Enter code ' + userCode + ' on github.com/login/device', 12000);
            }
          }

          /* Step 3: Poll for token */
          function pollToken() {
            if (Date.now() > expiresAt) {
              _rejectAuth(new Error('GitHub sign-in timed out.'));
              return;
            }
            global.fetch(DEVICE_TOKEN_URL, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ device_code: deviceCode }),
            }).then(function (r) {
              if (!r.ok) throw new Error('GitHub token poll failed (' + r.status + ')');
              return r.json();
            }).then(function (d) {
              if (d.ok && d.accessToken) {
                /* Success — store and resolve */
                GitHubService.storeAuth(d.accessToken, d.user || {});
                var state = GitHubService.getAuthState();
                _pendingResolve = null; _pendingReject = null;
                resolve(state);
                return;
              }
              if (d.error === 'authorization_pending' || d.error === 'slow_down') {
                var wait = (d.interval || interval) * 1000;
                setTimeout(pollToken, wait);
                return;
              }
              /* Token denied or expired */
              _rejectAuth(new Error('GitHub sign-in was denied or expired.'));
            }).catch(function () {
              /* Network error — retry after interval */
              setTimeout(pollToken, interval * 1000);
            });
          }
          setTimeout(pollToken, interval * 1000);
        }).catch(function (e) {
          if (_pendingReject) _pendingReject(e);
          _pendingResolve = null; _pendingReject = null;
        });
      });
    },

    /* ── Legacy: finalizeAuth (kept for backward compat) ── */
    finalizeAuth: function () {
      return global.fetch(CONFIG_URL).then(function (r) {
          if (!r.ok) throw new Error('GitHub config endpoint returned ' + r.status);
          return r.json();
        })
        .then(function (d) {
          var token = d && d.token;
          var user = d && d.user;
          if (token) {
            GitHubService.storeAuth(token, user || {});
            var state = GitHubService.getAuthState();
            if (_pendingResolve) _pendingResolve(state);
            return state;
          }
          throw new Error('GitHub sign-in did not complete.');
        })
        .catch(function (e) {
          if (_pendingReject) _pendingReject(e);
          throw e;
        })
        .finally(function () { _pendingResolve = null; _pendingReject = null; });
    },

    /* ── Community themes ── */
    fetchCommunityThemes: function () {
      var token = safeGet(TOKEN_KEY);
      var url = THEMES_URL + (token ? '?token=' + encodeURIComponent(token) : '');
      return global.fetch(url).then(function (r) {
        if (!r.ok) throw new Error('Could not load community themes');
        return r.json().then(function (d) { return (d && d.themes) || []; });
      });
    },
    publishTheme: function (theme) {
      var token = safeGet(TOKEN_KEY);
      if (!token) return Promise.reject(new Error('Not signed in to GitHub'));
      return global.fetch(THEMES_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: token, theme: theme }),
      }).then(function (r) { return r.json(); });
    },
    deleteTheme: function (id) {
      var token = safeGet(TOKEN_KEY);
      if (!token) return Promise.reject(new Error('Not signed in to GitHub'));
      return global.fetch(THEMES_URL, {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: token, id: id }),
      }).then(function (r) { return r.json(); });
    },
    likeTheme: function (id) {
      var token = safeGet(TOKEN_KEY);
      if (!token) return Promise.reject(new Error('Not signed in to GitHub'));
      return global.fetch(THEMES_URL + '/like', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: token, id: id }),
      }).then(function (r) { return r.json(); });
    },
    downloadCountTheme: function (id) {
      return global.fetch(THEMES_URL + '/download', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: id }),
      }).then(function (r) { return r.json(); });
    },

    // Community Playlists
    fetchCommunityPlaylists: function () {
      var token = safeGet(TOKEN_KEY);
      var url = PLAYLISTS_URL + (token ? '?token=' + encodeURIComponent(token) : '');
      return global.fetch(url).then(function (r) {
        if (!r.ok) throw new Error('Could not load community playlists');
        return r.json().then(function (d) { return (d && d.playlists) || []; });
      });
    },
    publishPlaylist: function (playlist) {
      var token = safeGet(TOKEN_KEY);
      if (!token) return Promise.reject(new Error('Not signed in to GitHub'));
      return global.fetch(PLAYLISTS_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: token, playlist: playlist }),
      }).then(function (r) { return r.json(); });
    },
    deletePlaylist: function (id) {
      var token = safeGet(TOKEN_KEY);
      if (!token) return Promise.reject(new Error('Not signed in to GitHub'));
      return global.fetch(PLAYLISTS_URL, {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: token, id: id }),
      }).then(function (r) { return r.json(); });
    },
  };

  global.GitHubService = GitHubService;
})(window);
