/* ──────────────────────────────────────────────
   AkiMelody — Phase 1 / Round 1 entry shim
   Bridges ES modules to the legacy inline <script> in player.html.
────────────────────────────────────────────── */
import { initDomRefs, setHydrators, setNavHelpers, setRefs } from './state.js';
import { setHydrators as routerSetHydrators, setNavHelpers as routerSetNavHelpers, setRefs as routerSetRefs } from './router.js';

// Resolve abort controllers through window.* since state.js owns their bindings.
// The inline monolith defines `let trackAbort = null; ...` at module scope;
// after Round 1 we read those lets off `window` (set by a one-line setter shim
// below) so the modules can use the same upstream refs.
const _abortRefs = () => ({
  trackAbort: window.trackAbort || null,
  artistAbort: window.artistAbort || null,
  albumAbort: window.albumAbort || null,
  lyricsAbort: window.lyricsAbort || null,
});

// Populate module-scoped DOM refs (AUDIO, LYRICS_PANEL, LYRICS_WRAPPER).
export function bootPhase1() {
  if (typeof window === 'undefined') return;
  initDomRefs();
  window.AUDIO = document.getElementById('akiAudio');
  window.LYRICS_PANEL = document.getElementById('lyrics-display-panel');
  window.LYRICS_WRAPPER = document.getElementById('lyrics-wrapper');
  routerSetRefs(_abortRefs());
}

// Wire legacy render/nav refs into the router module. Caller passes
// hydrators/navHelpers from the inline monolith after DOMContentLoaded.
export function wirePhase1({ hydrators, navHelpers } = {}) {
  if (hydrators) routerSetHydrators(hydrators);
  if (navHelpers) routerSetNavHelpers(navHelpers);
}

// ── Vendor-compat (Phase 1 R1 boot helpers) ─────────────────────────────────
// Expose boot/wire functions onto window for the legacy inline <script> to call.
// Removed at the end of Phase 1 once the monolith itself becomes a module.
if (typeof window !== 'undefined') {
  window.bootPhase1 = bootPhase1;
  window.wirePhase1 = wirePhase1;
}
