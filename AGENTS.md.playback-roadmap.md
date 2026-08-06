# AkiMelody — Playback & Lyrics Roadmap (Phase 2 & 3)

Phase 1 (Repair & Resilience) is DONE. Phases 2 & 3 below remain pending.

---

## Phase 2 — Coverage & Accuracy (lyrics wins)

### L1 — Expand syncedlyrics providers  (HIGH, trivial; `app.py:2763`)
Currently `_fetch_syncedlyrics` restricts to `["NetEase", "Megalobiz"]` — Asian-market focus only.
Western-track coverage depends on LRCLIB winning the race, which often doesn't.
- Expand to include `Musixmatch`, `Deezer`, `Genius`, `Lyricsify`, `LRCLIB` (syncedlyrics wraps it).
- Add per-provider timeout to avoid a slow NetEase call blocking the race.
- NetEase/Megalobiz keep their primacy for Asian tracks; Western providers cover the long tail.

### L2 — Negative-caching of empty results  (HIGH, small; `app.py:~2837`)
The Tier-4 fall-through `return jsonify({"synced": false, "lines": []})` is NOT written to cache today.
Every replay of an unlyricked track re-hits all providers and burns latency.
- Cache the empty result keyed by `tid` with a short TTL (~7 days via `_write_lyrics_cache`).
- Add a `neg_until` (or `exp`) timestamp field to the lyrics cache entry schema.
- `_read_lyrics_cache` returns cached empties as long as `now() < exp`, then proceeds to retry providers.
- Include the cache-bypass flag (`force=1`) on the lyrics API too — manual search/retry can re-fire.

### L3 — Rank by quality, not race-winner  (HIGH, medium; `app.py:2774-2787`)
Tier-1 race today hands the result to whichever of LRCLIB-strict / syncedlyrics returns first non-None.
A mislabelled/synced-but-out-of-phase NetEase lyric can beat a duration-matched LRCLIB synced lyric.
- Await both Tier-1 futures, then rank candidates:
  1. synced + duration-match (closest line-time spans requested `dur`)
  2. synced, unmatched duration
  3. plain + duration-match
  4. plain, unmatched
- Once a synced result is in hand from any source, don't fall through to plain — short-circuit.
- Optionally pick the synced candidate whose `[ti:]` / `[ar:]` LRC metadata matches the cleaned target.

### L5 — Stronger `clean_query` / normalisation  (MED, medium; `app.py:2707-2714`)
Existing `clean_query` strips parenthesised feat./remaster/(Live)/etc., plus the YT `- Topic` suffix.
- Fold `&` <-> `and`, drop `feat.`/`ft.`/`featuring`/`vs.`/`with` even WITHOUT parens (`"Song feat. Artist"` form).
- Apply `unicodedata.normalize("NFKC", q)` to fold smart quotes, fullwidth punctuation, ellipses variants.
- Collapse internal whitespace (`re.sub(r"\s+", " ", q).strip()`).
- Romanise the title side (currently only artist gets a YTMusic reverse-resolve pass at `app.py:2720`).

### L9 — In-browser lyrics cache (IndexedDB)  (HIGH, medium; `player.html` `fetchLyrics`)
Zero client cache today — every page load + every track change re-fetches over HTTP for the same track.
- Add a `tid -> {synced, lines, ts}` IndexedDB cache (lyrics are 10-50 KB/track; localStorage would explode).
- Use a no-dep IDB pattern (~30 LOC, `idb-keyval`-style).
- LRU cap ~200 tracks, persisted across sessions.
- Eviction: oldest by insertion; persistent across reloads.
- On `fetchLyrics(t)`, return cached immediately for instant render AND issue a backend refresh in the background if `ts > 30 days` (passive upgrade). Tier-1 race still gives fresh data on a cache miss.
- Keyed by `tid` + `dur` (edition mismatch protection).

### L10 — Prefetch next-track lyrics  (HIGH, small; `player.html` ~`_prebufferNextTrack`)
No lyrics equivalent of the audio prebuffer machinerch exists.
- Add `_prefetchLyricsForIdx(idx)` called from the same trigger sites as `_prebufferNextTrack`:
  - the 80% progress look-ahead in `AUDIO.ontimeupdate` (line ~10446)
  - the 15s-before-end pre-buffer check
- Fires a background `fetchLyrics(S.queue[idx])` that does NOT mutate `S.lyrics` / `S.lyricsTid`,
  just warms the IDB cache from L9 so the actual on-track-change render is instant.
- Debounce: don't fire if `_prefetchedLyricsIdx === idx`.

### L13 — "Lyrics unavailable" empty state needs a retry button + manual search
(HIGH, small; `player.html:5674-5682`)
Currently it's a static `.lyric-line.plain` text — no affordance.
- Append a "Retry" button below the "Lyrics unavailable for this track." text.
- Retry calls `fetchLyrics(t)` with a `force=1` (depends on L2's backend bypass).
- Add a manual search input: user types alternate spelling, fetch `lrclib.net/api/search?q=…` preview,
  pick candidate → commit. (Cross-cuts L5 — better queries reduce manual search needs.)
- Optional badges: "Source: LRCLIB" / "NetEase" / "YTMusic".

---

## Phase 3 — Quality Polish (UX / perf)

### L14 — `syncLyrics` should diff-update rows  (LOW, trivial; `player.html:5707-5711`)
Current `syncLyrics` iterates ALL rows every `ontimeupdate` tick and re-sets `--distance` + `active-line` class.
- Cache `previousActiveIdx`; on a new active, only toggle the OLD and NEW row classes (the `--distance`
  stays correct because it's a function of `|i - activeIdx|` and unchanged when the active doesn't move).
- Scales much better on long synced lyrics (300+ lines, e.g. extended DJ sets).
- Drop the `forEach` except when `force=true`.

### L15 — Make `+0.25s` lyric offset user-adjustable  (MED, small; `player.html:5689`)
Some LRC data has a baked-in global offset; user controls fix sync mismatches.
- Add `S.lyricOffset` (numeric, defaults 0.25, persisted via `akimelody_lyric_offset`).
- Slider UI in the lyrics panel header (next to `toggleLyrics` button) — small, only visible when lyrics are synced.
- `syncLyrics` substitutes `AUDIO.currentTime + S.lyricOffset` (replacing the hard-coded + 0.25).
- Persisted across sessions; reset-on-double-click returns to 0.25 default.

### L11 — Debounce `fetchLyrics` by ~150ms  (MED, small; `player.html:5586`)
Rapid skips cause `lyricsAbort.abort()` churn + repeated `_lyricsGen++` despite the gen-guard
handling correctness. A small debounce coalesces bursts into a single real network call.
- Wrap the start of `fetchLyrics` body in a 150ms `setTimeout` that bumps `_lyricsGen` before firing.
- Existing `_lyricsGen` guard + `signal.aborted` check still handle correctness at the tail end.

### L12 — Hydrate `track.dur` fallback  (LOW, trivial; `player.html:5602`)
`track.dur` may be `undefined` for cache-restored queues → apiFetch drops falsy → backend loses
`duration` and can't apply Tier-2 ±5s tolerance (no duration == ALL candidates pass the duration filter).
- Replace `duration: track.dur` with `duration: track.dur || track.duration || ''`.

### F12 — Equal-power crossfade  (MED, trivial; `player.html` ~`_cfFrame`)
Current linear-amplitude fade (targetVol * (1-e) / targetVol * e) dips perceived loudness in the middle.
- Swap the linear curves for equal-power:
  `AUDIO.volume  = targetVol * Math.cos(p * Math.PI / 2);`
  `AUDIO2.volume = targetVol * Math.sin(p * Math.PI / 2);`
- Or simpler `Math.sqrt(1-e) / Math.sqrt(e)` (same perceptual result).
- Optional: pair with the existing ease-out cubic curve (`e = 1 - Math.pow(1-p, 3)`) for a
  cross-fade-crossfade curve that starts slow → fast → smooth — leaves the audible fade shape but is
  loudness-consistent. (Cosine-squared over the linear progress `p`, not over `e`.)

### F13 — Crossfade pause edge case  (MED, small; `player.html` ~`_cfFrame`)
If `AUDIO.paused` goes true mid-crossfade (numeric drift / OS suspend), current code calls
`_finishCrossfade` mid-stream → silent finish.

- In `_cfFrame`: if `AUDIO.paused && p < 1 && _crossfading`, do not enter the finish branch.
- Better: detect the crossfade pause via `manifest` (listen to `pause` event on AUDIO once and
  trigger `_finishCrossfade` only when AUDIO actively resumes).
- Simplest pruning: restore `AUDIO.volume = targetVol`, `AUDIO2.pause()`, `_crossfading = false`,
  bump `_crossfadeGen`. Don't fire finish — the user's pause is a hard interrupt like a manual skip.

### F15 — Throttle `_syncMediaSessionPosition` to ~1 Hz  (LOW, trivial;
`player.html` `ontimeupdate` ~10457)
Currently fires every `ontimeupdate` tick (~4 Hz, browser default). Browsers coalesce internally.
- Cache `lastMediaSessionSyncTs`; in the `ontimeupdate` site, skip if `now - lastMediaSessionSyncTs < 1000`.

### L25 (bonus) — Provider merge / dedup  (MED, medium; `app.py` post-race)
After Tier-1 race + Tier-2 search, might have both LRCLIB synced + syncedlyrics synced.
- Tiny reconciliation step: prefer longest non-empty snippet OR best-duration span (line-time array coverage).
- Even a no-op — keep both, expose via an `/api/lyrics/providers` diagnostic — would help debugging sync drift.

---

## Notes

- Phase 2 doesn't touch the export-cache (Phase 1's TTL on stream URLs) — these are largely orthogonal.
- L9 (IndexedDB) is the biggest single win for repeated-playback latency.
- L1 + L2 + L3 collectively unlock Western + non-lyricked catalogue + correct synced-lyric ranking.
- F12/F13 are local-only UX polish, completely orthogonal to the lyrics pipeline.
- No phase here includes the backend auto-rebuild on `_AUTH_FAIL_RE` (deprioritised — the manual
  refresh-auth button exists; auto-rebuild is a quality-of-life improvement for Phase 4 if needed).
