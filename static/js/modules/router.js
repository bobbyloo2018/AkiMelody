/* ──────────────────────────────────────────────
   AkiMelody — Router / Navigation (ES Module)
   Extracted from templates/player.html
────────────────────────────────────────────── */

import { S, AkiNav, VIEW_IDS, _hideAllViews, _purgeDynamicContainers, getPlayGen, incPlayGen } from './state.js';
import { Spatial } from './spatial.js'; // Will be created later or imported from global

// Forward references (set via setters from main)
let _hydrateListView = null;
let _hydrateArtistView = null;
let _hydrateAlbumView = null;
let _hydrateHomeView = null;
let _updateNavStates = null;
let triggerPopInAnimation = null;
let Motion = null;
let AUDIO = null;
let LYRICS_PANEL = null;
let trackAbort = null;
let artistAbort = null;
let albumAbort = null;
let lyricsAbort = null;

export function setHydrators(fns) {
  _hydrateListView = fns._hydrateListView;
  _hydrateArtistView = fns._hydrateArtistView;
  _hydrateAlbumView = fns._hydrateAlbumView;
  _hydrateHomeView = fns._hydrateHomeView;
}

export function setNavHelpers(fns) {
  _updateNavStates = fns._updateNavStates;
  triggerPopInAnimation = fns.triggerPopInAnimation;
  Motion = fns.Motion;
}

export function setRefs(refs) {
  AUDIO = refs.AUDIO;
  LYRICS_PANEL = refs.LYRICS_PANEL;
  trackAbort = refs.trackAbort;
  artistAbort = refs.artistAbort;
  albumAbort = refs.albumAbort;
  lyricsAbort = refs.lyricsAbort;
}

// ── Core view switching ────────────────────────
export async function switchAkiView(viewId, data = {}, isBack = false) {
  if (!VIEW_IDS.includes(viewId)) return;

  Spatial.cancel();

  if (viewId !== 'artistView' && artistAbort) artistAbort.abort();
  if (viewId !== 'albumView' && albumAbort) albumAbort.abort();

  const sourceEl = data && data._sourceEl;
  delete (data || {})._sourceEl;

  // Spatial forward transition
  if (sourceEl && !isBack) {
    const srcRect = Spatial.capture(sourceEl);

    // Push current view onto stack
    if (AkiNav.current) {
      if (AkiNav.current.viewId === 'listView') {
        AkiNav.current.data.activePlaylist = S.activePlaylist;
      }
      AkiNav.stack.push(AkiNav.current);
    }
    AkiNav.current = { viewId, data: data || {}, _reverseData: srcRect ? { srcRect } : null };

    // Show target temporarily (off-screen) to hydrate and capture rect
    const targetEl = document.getElementById(viewId);
    targetEl.style.cssText = 'position:absolute;left:-9999px;top:0;display:flex !important;visibility:visible;';

    switch (viewId) {
      case 'listView': _hydrateListView(data); break;
      case 'artistView': _hydrateArtistView(data); break;
      case 'albumView': _hydrateAlbumView(data); break;
      case 'homeView': _hydrateHomeView(); break;
    }

    // Find target element for spatial animation
    let targetSrc = null;
    if (viewId === 'albumView') targetSrc = document.getElementById('albumArtImg');
    else if (viewId === 'artistView') targetSrc = document.querySelector('#artistView .artist-header');
    else if (viewId === 'listView') targetSrc = document.querySelector('#listZone');

    const targetRect = Spatial.capture(targetSrc);

    // Hide target again, restore original state
    targetEl.style.cssText = '';
    targetEl.classList.add('hidden');

    if (srcRect && targetRect) {
      document.body.classList.add('view-transitioning');

      // Await artwork images in target view before completing transition
      // Mirrors inline patch from Phase 0.4 (player.html:4603-4607): race against
      // a 1500ms timeout so off-screen lazy-loaded <img> tags can never silently
      // freeze the spatial transition by failing to fire onload/onerror.
      const awaitArtwork = () => {
        const imgs = targetEl.querySelectorAll('img[src]');
        const loads = Array.from(imgs).map(img => {
          if (img.complete) return Promise.resolve();
          return new Promise(res => { img.onload = img.onerror = res; });
        });
        const allLoads = Promise.all(loads);
        return Promise.race([
          allLoads,
          new Promise(res => setTimeout(res, 1500))
        ]);
      };

      Spatial.forward(srcRect, targetRect, null, async () => {
        await awaitArtwork();
        // Midpoint: just show the already-hydrated target
        _hideAllViews();
        targetEl.classList.remove('hidden');
        document.body.classList.remove('view-transitioning');
        document.body.classList.toggle('view-home-active', viewId === 'homeView');
        _updateNavStates(viewId, data);
        setTimeout(() => triggerPopInAnimation(viewId), window.Motion ? Motion.dur('quick') : 50);
      });
      return;
    }
  }

  // Non-spatial fallback
  _hideAllViews();
  _purgeDynamicContainers();

  if (!isBack && AkiNav.current) {
    if (AkiNav.current.viewId === 'listView') {
      AkiNav.current.data.activePlaylist = S.activePlaylist;
    }
    AkiNav.stack.push(AkiNav.current);
  }

  AkiNav.current = { viewId, data: data || {} };
  document.getElementById(viewId).classList.remove('hidden');
  document.body.classList.toggle('view-home-active', viewId === 'homeView');
  _updateNavStates(viewId, data);

  switch (viewId) {
    case 'listView': _hydrateListView(data); break;
    case 'artistView': _hydrateArtistView(data); break;
    case 'albumView': _hydrateAlbumView(data); break;
    case 'homeView': _hydrateHomeView(); break;
  }

  setTimeout(() => {
    triggerPopInAnimation(viewId);
  }, window.Motion ? Motion.dur('quick') : 50);
}

// ── Back navigation ────────────────────────────
export function handleAkiBack() {
  Spatial.cancel();

  if (AkiNav.stack.length === 0) {
    switchAkiView('listView', { navView: 'queue' }, true);
    return;
  }
  const prev = AkiNav.stack.pop();
  const reverseData = AkiNav.current._reverseData;

  // Reverse spatial transition
  if (reverseData && reverseData.srcRect) {
    // Capture the back button position BEFORE we touch anything
    const backBtn = document.querySelector('#' + AkiNav.current.viewId + ' .artist-back');
    const fromRect = Spatial.capture(backBtn);

    AkiNav.current = prev;

    // Hydrate previous view while off-screen (current view stays visible)
    const prevEl = document.getElementById(prev.viewId);
    prevEl.style.cssText = 'position:absolute;left:-9999px;top:0;display:flex !important;visibility:visible;';
    _purgeDynamicContainers();

    switch (prev.viewId) {
      case 'listView': _hydrateListView(prev.data); break;
      case 'artistView': _hydrateArtistView(prev.data); break;
      case 'albumView': _hydrateAlbumView(prev.data); break;
      case 'homeView': _hydrateHomeView(); break;
    }

    // Find target element in previous view
    let reverseTarget = null;
    if (prev.viewId === 'listView') reverseTarget = document.querySelector('#listZone');
    else if (prev.viewId === 'homeView') reverseTarget = document.querySelector('#homeZone');
    else if (prev.viewId === 'artistView') reverseTarget = document.querySelector('#artistView .artist-header');
    else if (prev.viewId === 'albumView') reverseTarget = document.getElementById('albumArtImg');

    const targetRect = Spatial.capture(reverseTarget);
    prevEl.style.cssText = '';
    prevEl.classList.add('hidden');

    if (fromRect && targetRect) {
      document.body.classList.add('view-transitioning');
      Spatial.reverse(fromRect, targetRect, null, () => {
        // Current view was kept visible during animation; now swap
        _hideAllViews();
        prevEl.classList.remove('hidden');
        document.body.classList.remove('view-transitioning');
        document.body.classList.toggle('view-home-active', prev.viewId === 'homeView');
        _updateNavStates(prev.viewId, prev.data);
        setTimeout(() => triggerPopInAnimation(prev.viewId), window.Motion ? Motion.dur('quick') : 50);
      });
      return;
    }
  }

  // Non-spatial fallback
  AkiNav.current = prev;
  _hideAllViews();
  _purgeDynamicContainers();
  document.getElementById(prev.viewId).classList.remove('hidden');

  document.body.classList.toggle('view-home-active', prev.viewId === 'homeView');
  _updateNavStates(prev.viewId, prev.data);

  switch (prev.viewId) {
    case 'listView': _hydrateListView(prev.data); break;
    case 'artistView': _hydrateArtistView(prev.data); break;
    case 'albumView': _hydrateAlbumView(prev.data); break;
    case 'homeView': _hydrateHomeView(); break;
  }

  setTimeout(() => {
    triggerPopInAnimation(prev.viewId);
  }, window.Motion ? Motion.dur('quick') : 50);
}