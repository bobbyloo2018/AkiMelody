/**
 * Spotify Playlist Importer Module
 * Extracts playlist data from Spotify embed pages
 */

(function() {
    'use strict';

    window.SpotifyImporter = {
        /**
         * Extract Spotify playlist ID from various URL formats
         * @param {string} url - Spotify playlist URL
         * @returns {string|null} - Playlist ID or null if invalid
         */
        extractPlaylistId(url) {
            if (!url || typeof url !== 'string') return null;

            const patterns = [
                /open\.spotify\.com\/playlist\/([a-zA-Z0-9]+)/,
                /spotify\.com\/playlist\/([a-zA-Z0-9]+)/,
                /spotify:playlist:([a-zA-Z0-9]+)/
            ];

            for (const pattern of patterns) {
                const match = url.match(pattern);
                if (match && match[1]) {
                    return match[1];
                }
            }
            return null;
        },

        /**
         * Fetch Spotify embed page via backend proxy (bypasses CORS)
         * @param {string} playlistId - Spotify playlist ID
         * @returns {Promise<Object>} - Parsed playlist data
         */
        async fetchPlaylistData(playlistId) {
            const proxyUrl = `/api/spotify/embed_proxy?playlist_id=${encodeURIComponent(playlistId)}`;
            
            const response = await fetch(proxyUrl);
            if (!response.ok) {
                let errMsg = `Failed to fetch playlist (HTTP ${response.status})`;
                try {
                    const errData = await response.json();
                    if (errData.error) errMsg = errData.error;
                } catch(e) {}
                throw new Error(errMsg);
            }

            const html = await response.text();
            return this.parsePlaylistHtml(html, playlistId);
        },

        /**
         * Parse HTML response to extract playlist JSON data
         * @param {string} html - HTML content from embed page
         * @param {string} playlistId - Playlist ID for reference
         * @returns {Object} - Parsed playlist data
         */
        parsePlaylistHtml(html, playlistId) {
            const scriptTagPattern = /<script[^>]*id="__NEXT_DATA__"[^>]*>([\s\S]*?)<\/script>/i;
            const match = html.match(scriptTagPattern);

            if (!match || !match[1]) {
                throw new Error('Could not find playlist data in embed page');
            }

            try {
                const jsonData = JSON.parse(match[1]);
                return this.extractPlaylistFromJson(jsonData, playlistId);
            } catch (e) {
                throw new Error(`Failed to parse playlist JSON: ${e.message}`);
            }
        },

        /**
         * Extract playlist data from Next.js JSON
         * @param {Object} jsonData - Parsed JSON from __NEXT_DATA__
         * @param {string} playlistId - Playlist ID
         * @returns {Object} - Normalized playlist data
         */
        extractPlaylistFromJson(jsonData, playlistId) {
            let entity = null;

            if (jsonData.props && jsonData.props.pageProps) {
                const pp = jsonData.props.pageProps;

                if (pp.state && pp.state.data && pp.state.data.entity) {
                    entity = pp.state.data.entity;
                } else if (pp.playlist) {
                    return this._legacyParse(pp.playlist, playlistId);
                } else if (pp.dehydratedState && pp.dehydratedState.queries) {
                    for (const query of pp.dehydratedState.queries) {
                        if (query.state && query.state.data && query.state.data.playlist) {
                            return this._legacyParse(query.state.data.playlist, playlistId);
                        }
                    }
                }
            }

            if (!entity || !entity.trackList || !entity.trackList.length) {
                throw new Error('Playlist data not found in JSON structure');
            }

            const artUrl = (entity.coverArt && entity.coverArt.sources && entity.coverArt.sources.length > 0)
                ? entity.coverArt.sources[0].url : '';

            const tracks = entity.trackList.map(t => ({
                name: t.title || 'Unknown Track',
                artist: t.subtitle || 'Unknown Artist',
                duration_ms: t.duration || 0,
                album: '',
                album_art: artUrl,
                uri: t.uri || ''
            }));

            return {
                id: playlistId,
                name: entity.title || entity.name || 'Unknown Playlist',
                description: '',
                coverArt: artUrl,
                tracks: tracks,
                owner: entity.subtitle || '',
                trackCount: tracks.length
            };
        },

        _legacyParse(playlistData, playlistId) {
            const tracks = [];
            if (playlistData.tracks && playlistData.tracks.items) {
                for (const item of playlistData.tracks.items) {
                    if (item.track) {
                        const track = item.track;
                        tracks.push({
                            name: track.name || 'Unknown Track',
                            artist: track.artists ? track.artists.map(a => a.name).join(', ') : 'Unknown Artist',
                            duration_ms: track.duration_ms || 0,
                            album: track.album ? track.album.name : '',
                            album_art: track.album && track.album.images && track.album.images.length > 0
                                ? track.album.images[0].url : '',
                            uri: track.uri || ''
                        });
                    }
                }
            }
            const coverArt = playlistData.images && playlistData.images.length > 0
                ? playlistData.images[0].url : '';
            return {
                id: playlistId,
                name: playlistData.name || 'Unknown Playlist',
                description: playlistData.description || '',
                coverArt: coverArt,
                tracks: tracks,
                owner: playlistData.owner ? playlistData.owner.display_name : '',
                trackCount: tracks.length
            };
        },

        /**
         * Main function: Convert Spotify URL to AkiMelody-compatible playlist
         * @param {string} spotifyUrl - Spotify playlist URL
         * @returns {Promise<Object>} - AkiMelody playlist object
         */
        async importFromUrl(spotifyUrl) {
            const playlistId = this.extractPlaylistId(spotifyUrl);
            if (!playlistId) {
                throw new Error('Invalid Spotify playlist URL. Expected format: open.spotify.com/playlist/{PLAYLIST_ID}');
            }

            const playlistData = await this.fetchPlaylistData(playlistId);

            const akiTracks = playlistData.tracks.map((track, index) => ({
                name: track.name,
                artist: track.artist,
                duration_ms: track.duration_ms,
                dur: Math.round(track.duration_ms / 1000),
                album: track.album,
                art: track.album_art,
                tid: this.generateTid(track.name, track.artist),
                videoId: '',
                albumId: '',
                local_audio: false,
                local_art: false,
                trackNumber: index + 1,
                _spotifyUri: track.uri
            }));

            return {
                name: playlistData.name,
                coverArt: playlistData.coverArt,
                description: playlistData.description,
                tracks: akiTracks,
                trackCount: akiTracks.length,
                source: 'spotify_import',
                spotifyPlaylistId: playlistId
            };
        },

        /**
         * Generate track ID (same algorithm as backend)
         * @param {string} name - Track name
         * @param {string} artist - Artist name
         * @returns {string} - MD5 hash
         */
        generateTid(name, artist) {
            const str = `${name}_${artist}`.toLowerCase().trim();
            let hash = 0;
            for (let i = 0; i < str.length; i++) {
                const char = str.charCodeAt(i);
                hash = ((hash << 5) - hash) + char;
                hash = hash & hash;
            }
            return Math.abs(hash).toString(16).padStart(32, '0');
        }
    };
})();