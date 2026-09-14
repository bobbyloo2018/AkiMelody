'use strict';

const MP_AUDIO = document.getElementById('mp-audio');
const MP_PREBUFFER_AUDIO = document.createElement('audio');
MP_PREBUFFER_AUDIO.preload = 'auto';
MP_PREBUFFER_AUDIO.muted = true;
const MP_QUEUE_KEY = 'akimelody_mobile_player_queue_v1';
const MP_PREFS_KEY = 'akimelody_mobile_player_prefs_v1';
const MP_STREAM_TIMEOUT_MS = 15000;
const MP_AUDIO_START_TIMEOUT_MS = 8000;
const MP_STREAM_RETRY_DELAY_MS = 1200;
const MP_QUEUE_PREFETCH_COUNT = 4;
let MP_NATIVE_MEDIA_SIGNATURE = '';
let MP_NATIVE_MEDIA_SENT_AT = 0;

const MP = {
  view: 'player', previousView: 'player', libraryTab: 'favorites',
  queue: [], queueIdx: -1, queueContext: { type: 'manual', label: 'Queue', radioExtended: false }, liked: [], likedIds: new Set(), playlists: [],
  favoritesLoaded: false, playlistsLoaded: false, youtubeLikes: [], youtubeLikesLoaded: false, youtubeLikesSource: '', youtubeLikesError: '', youtubeLikesPromise: null,
  downloads: [], downloadsLoaded: false, downloadSummary: null, playlistTracks: new Map(), playlistPromises: new Map(), libraryGeneration: 0,
  artworkGeneration: 0, artworkTransitionTimer: null, currentArtworkUrl: '', currentTrackKey: '',
  playing: false, shuffle: false, repeat: 0, radioMode: true,
  online: navigator.onLine !== false, currentLocal: false, playGeneration: 0, playbackAbort: null,
  searchGeneration: 0, searchWarmGeneration: 0, searchAbort: null, searchTracks: [], lyrics: [], lyricsSynced: false,
  lyricOffset: 0, activeLyric: -1, lyricGeneration: 0, lyricsKey: '', detail: null, dynamicColor: true,
  history: [], sleepTimer: null, sleepEndsAt: 0, playStarting: 0,
  currentSource: null, currentSourceKey: '',
  unplayableLocalIds: new Set(), navHideTimer: null, navTouchStart: null,
  queueRecordingPrefetchKey: '', queueStreamWarmKey: '', queueWarmGeneration: 0, queueStreamWarmTimer: null,
  queueWarmAbort: null, queueWarmAttemptKey: '',
  prebufferKey: '', prebufferUrl: '', prebufferReady: false,
  warmedStreams: new Map(), streamBuildInFlight: new Map(), offlinePendingIds: new Set(),
  youtubeAuthFlow: '', youtubeAuthUrl: '', youtubePollTimer: null, isAndroid: /(?:^|\s)AkiMelodyAndroid\//.test(navigator.userAgent),
  spotifyMode: 'playlist', spotifyImporting: false, radioSuggesting: false, radioSuggestKey: '',
  offlineCollectionName: '', offlineCollectionStatus: null, offlineCollectionTimer: null,
  savedTrackKey: '', savedPosition: 0, persistPositionTimer: null
};

function mpApiFetch(endpoint, params = {}, method = 'GET', timeoutMs = 15000, requestOptions = {}) {
  const options = { method };
  let url = endpoint;
  if (method === 'POST' || method === 'DELETE') {
    options.headers = { 'Content-Type': 'application/json' };
    options.body = JSON.stringify(params);
  } else {
    const parsed = new URL(endpoint, window.location.origin);
    Object.entries(params).forEach(([key, value]) => {
      if (value !== undefined && value !== null && value !== '') parsed.searchParams.set(key, value);
    });
    url = parsed.toString();
  }
  const controller = new AbortController();
  options.signal = controller.signal;
  const externalSignal=requestOptions&&requestOptions.signal;
  let externalAbort=null;
  if(externalSignal){
    externalAbort=()=>controller.abort();
    if(externalSignal.aborted)controller.abort();
    else externalSignal.addEventListener('abort',externalAbort,{once:true});
  }
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const cleanup=()=>{clearTimeout(timer);if(externalSignal&&externalAbort)externalSignal.removeEventListener('abort',externalAbort)};
  return fetch(url, options).then(async response => {
    cleanup();
    let payload = null;
    try { payload = await response.json(); } catch (_error) {}
    if (!response.ok) {
      const error = new Error((payload && payload.error) || `Request failed (${response.status})`);
      error.status = response.status; error.payload = payload; throw error;
    }
    return payload;
  }).catch(error => { cleanup(); throw error; });
}

function mpEl(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function mpTrack(raw) {
  const track = raw && typeof raw === 'object' ? raw : {};
  return {
    ...track,
    name: track.name || track.title || 'Unknown track',
    artist: track.artist || track.artist_name || 'Unknown artist',
    art: track.art || track.album_art || track.thumbnail || '',
    dur: track.dur || track.duration || 0,
    tid: track.tid || '', videoId: track.videoId || '', albumId: track.albumId || '',
    album: track.album || track.albumName || '',
    local_audio: !!(track.local_audio || track.localAudio),
    local_art: !!track.local_art
  };
}

function mpFmt(value) {
  if (typeof value === 'string' && value.includes(':')) return value;
  const seconds = Math.max(0, Math.floor(Number(value) || 0));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
}

function mpArtUrl(track) {
  if (!track) return '';
  if (track.local_art && track.tid) return `/api/local_file?q=${encodeURIComponent(track.tid)}.jpg`;
  const raw = track.art || track.album_art || track.thumbnail || '';
  if (!raw || /^(data:|blob:|\/api\/|\/static\/)/.test(raw)) return raw;
  return /^https?:\/\//.test(raw) ? `/api/img-proxy?u=${encodeURIComponent(raw)}` : raw;
}

function mpEmpty(icon, title, message) {
  const wrap = mpEl('div', 'empty-state');
  const symbol = mpEl('i', icon); symbol.setAttribute('aria-hidden', 'true');
  wrap.append(symbol, mpEl('strong', '', title), mpEl('span', '', message));
  return wrap;
}

function mpLoading(message) {
  const wrap = mpEl('div', 'loading-state');
  wrap.innerHTML = '<i class="fa-solid fa-circle-notch fa-spin" aria-hidden="true"></i>';
  wrap.appendChild(mpEl('span', '', message));
  return wrap;
}

function mpToast(message) {
  const toast = document.getElementById('mp-toast');
  toast.textContent = message; toast.style.display = 'block';
  document.getElementById('mp-a11y').textContent = message;
  clearTimeout(mpToast.timer);
  mpToast.timer = setTimeout(() => { toast.style.display = 'none'; }, 2600);
}

function mpPersist() {
  try {
    localStorage.setItem(MP_QUEUE_KEY, JSON.stringify({
      queue: MP.queue.slice(0, 250), queueIdx: MP.queueIdx, queueContext: MP.queueContext,
      playback: { key: MP.savedTrackKey, position: Math.max(0, Number(MP.savedPosition) || 0) }
    }));
    localStorage.setItem(MP_PREFS_KEY, JSON.stringify({ shuffle: MP.shuffle, repeat: MP.repeat, radioMode: MP.radioMode, lyricOffset: MP.lyricOffset, dynamicColor: MP.dynamicColor, volume: MP_AUDIO.volume }));
  } catch (error) { console.warn('Mobile player persistence failed', error); }
}

function mpRememberPlaybackPosition(immediate=false){
  const track=MP.queue[MP.queueIdx],key=mpPlaybackKey(track);
  if(!key||MP.currentSourceKey!==key)return;
  const position=Number(MP_AUDIO.currentTime);
  if(Number.isFinite(position)){MP.savedTrackKey=key;MP.savedPosition=Math.max(0,position)}
  clearTimeout(MP.persistPositionTimer);
  if(immediate)mpPersist();else MP.persistPositionTimer=setTimeout(()=>{MP.persistPositionTimer=null;mpPersist()},1200);
}

function mpRestore() {
  try {
    const saved = JSON.parse(localStorage.getItem(MP_QUEUE_KEY) || '{}');
    MP.queue = Array.isArray(saved.queue) ? saved.queue.map(mpTrack).slice(0, 250) : [];
    MP.queueIdx = Number.isInteger(saved.queueIdx) ? Math.min(Math.max(saved.queueIdx, -1), MP.queue.length - 1) : -1;
    const context=saved.queueContext&&typeof saved.queueContext==='object'?saved.queueContext:{};
    MP.queueContext={type:String(context.type||'manual'),label:String(context.label||'Queue').slice(0,100),radioExtended:!!context.radioExtended};
    const playback=saved.playback&&typeof saved.playback==='object'?saved.playback:{};
    const restoredTrack=MP.queue[MP.queueIdx],restoredKey=mpPlaybackKey(restoredTrack);
    MP.savedTrackKey=String(playback.key||'');
    MP.savedPosition=MP.savedTrackKey&&MP.savedTrackKey===restoredKey?Math.max(0,Number(playback.position)||0):0;
    if(!MP.savedTrackKey&&restoredKey)MP.savedTrackKey=restoredKey;
    const prefs = JSON.parse(localStorage.getItem(MP_PREFS_KEY) || '{}');
    MP.shuffle = !!prefs.shuffle; MP.repeat = Number(prefs.repeat) || 0; MP.radioMode = prefs.radioMode !== false;
    MP.lyricOffset = Number(prefs.lyricOffset) || 0; MP.dynamicColor = prefs.dynamicColor !== false;
    MP_AUDIO.volume = Math.min(1, Math.max(0, Number(prefs.volume ?? .55)));
  } catch (error) { console.warn('Mobile player restore failed', error); }
}

const MobilePalette = (() => {
  const canvas = document.createElement('canvas'); canvas.width = 32; canvas.height = 32;
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  const defaults = [{r:136,g:78,b:238},{r:238,g:72,b:139},{r:255,g:118,b:96},{r:18,g:10,b:30},{r:244,g:225,b:255}];
  function rgbToHsl(r,g,b){r/=255;g/=255;b/=255;const max=Math.max(r,g,b),min=Math.min(r,g,b),d=max-min;let h=0;const l=(max+min)/2;const s=d===0?0:d/(1-Math.abs(2*l-1));if(d){if(max===r)h=60*(((g-b)/d)%6);else if(max===g)h=60*((b-r)/d+2);else h=60*((r-g)/d+4)}return{h:(h+360)%360,s,l}}
  function hslToRgb(h,s,l){const c=(1-Math.abs(2*l-1))*s,x=c*(1-Math.abs((h/60)%2-1)),m=l-c/2;let r=0,g=0,b=0;if(h<60){r=c;g=x}else if(h<120){r=x;g=c}else if(h<180){g=c;b=x}else if(h<240){g=x;b=c}else if(h<300){r=x;b=c}else{r=c;b=x}return{r:Math.round((r+m)*255),g:Math.round((g+m)*255),b:Math.round((b+m)*255)}}
  function apply(colors){const names=['primary','secondary','accent','shadow','highlight'];names.forEach((name,index)=>document.documentElement.style.setProperty(`--album-${name}`,`${colors[index].r}, ${colors[index].g}, ${colors[index].b}`))}
  function extract(image){
    if(!MP.dynamicColor||!image||!image.complete||!image.naturalWidth){apply(defaults);return}
    try{
      ctx.clearRect(0,0,32,32);ctx.drawImage(image,0,0,32,32);const data=ctx.getImageData(0,0,32,32).data;
      const buckets=Array.from({length:36},()=>({r:0,g:0,b:0,h:0,s:0,l:0,n:0}));let ar=0,ag=0,ab=0,count=0;
      for(let i=0;i<data.length;i+=4){const r=data[i],g=data[i+1],b=data[i+2],lum=(r*.299+g*.587+b*.114)/255;ar+=r;ag+=g;ab+=b;count++;const hsl=rgbToHsl(r,g,b);if(lum<.08||lum>.95||hsl.s<.12)continue;const bucket=buckets[Math.floor(hsl.h/10)%36];bucket.r+=r;bucket.g+=g;bucket.b+=b;bucket.h+=hsl.h;bucket.s+=hsl.s;bucket.l+=hsl.l;bucket.n++}
      const active=buckets.filter(b=>b.n).map(b=>({r:Math.round(b.r/b.n),g:Math.round(b.g/b.n),b:Math.round(b.b/b.n),h:b.h/b.n,s:b.s/b.n,l:b.l/b.n,n:b.n})).sort((a,b)=>b.n-a.n);
      let colors;
      if(active.length<2){const base=rgbToHsl(ar/count,ag/count,ab/count);const saturation=Math.max(.42,base.s);colors=[hslToRgb(base.h,saturation,Math.max(.42,base.l)),hslToRgb((base.h+145)%360,Math.max(.38,saturation-.08),.58),hslToRgb((base.h+260)%360,Math.max(.35,saturation-.12),.64)];}
      else{const primary=active[0];const distance=c=>{let d=Math.abs(c.h-primary.h);return d>180?360-d:d};const secondary=active.slice(1).sort((a,b)=>distance(b)-distance(a))[0]||primary;const accent=active.filter(c=>c!==primary&&c!==secondary).sort((a,b)=>(distance(b)+Math.abs(b.h-secondary.h))-(distance(a)+Math.abs(a.h-secondary.h)))[0]||hslToRgb((primary.h+150)%360,Math.max(.45,primary.s),.58);colors=[primary,secondary,accent]}
      colors=colors.map(color=>({r:Math.max(0,Math.min(255,color.r)),g:Math.max(0,Math.min(255,color.g)),b:Math.max(0,Math.min(255,color.b))}));
      const avgBright=colors.reduce((sum,color)=>sum+color.r+color.g+color.b,0)/(255*colors.length*3);if(avgBright<.2)colors=colors.map(color=>({r:Math.min(255,Math.round(color.r*1.6+40)),g:Math.min(255,Math.round(color.g*1.6+40)),b:Math.min(255,Math.round(color.b*1.6+40))}));
      const darkest=[...colors].sort((a,b)=>(a.r+a.g+a.b)-(b.r+b.g+b.b))[0],lightest=[...colors].sort((a,b)=>(b.r+b.g+b.b)-(a.r+a.g+a.b))[0];
      colors.push({r:Math.round(darkest.r*.2),g:Math.round(darkest.g*.2),b:Math.round(darkest.b*.2)},{r:Math.min(255,Math.round(lightest.r*.5+128)),g:Math.min(255,Math.round(lightest.g*.5+128)),b:Math.min(255,Math.round(lightest.b*.5+128))});apply(colors);
    }catch(error){console.warn('Palette extraction failed',error);apply(defaults)}
  }
  return { extract, reset:()=>apply(defaults) };
})();

function mpSwitchView(view, push = true) {
  const target = document.querySelector(`.aki-view[data-view="${view}"]`);
  if (!target) return;
  if (MP.view !== view) MP.previousView = MP.view;
  MP.view = view;
  document.body.dataset.view = view;
  document.querySelectorAll('.aki-view').forEach(node => node.classList.toggle('active', node === target));
  document.querySelectorAll('.aki-nav [data-view-target]').forEach(button => button.classList.toggle('active', button.dataset.viewTarget === view));
  if (push) history.pushState({ view }, '', `#${view}`);
  if (view === 'home') mpRenderHome();
  if (view === 'library') mpRenderLibrary();
  if (view === 'settings') mpRefreshYouTubeAuthStatus();
  mpSetNavVisible(true, view === 'player');
}

function mpSetNavVisible(visible, autoHide = true) {
  clearTimeout(MP.navHideTimer);
  const player = MP.view === 'player';
  document.body.dataset.navHidden = String(player && !visible);
  if (player && visible && autoHide) {
    MP.navHideTimer = setTimeout(() => {
      if (MP.view === 'player' && !document.querySelector('.aki-sheet:not([hidden])')) {
        document.body.dataset.navHidden = 'true';
      }
    }, 2600);
  }
}

function mpOpenSheet(name) {
  const sheet = document.querySelector(`[data-sheet="${name}"]`); if (!sheet) return;
  sheet.hidden = false;
  if (name === 'queue') mpRenderQueue();
  if (name === 'playlist') mpRenderPlaylistPicker();
}
function mpCloseSheet(sheet) { const target = typeof sheet === 'string' ? document.querySelector(`[data-sheet="${sheet}"]`) : sheet.closest('.aki-sheet'); if (target) { target.hidden = true; if (target.dataset.sheet === 'youtube') clearTimeout(MP.youtubePollTimer); } }

function mpSetLyricsOpen(open) {
  const overlay=document.getElementById('mp-album-lyrics'),button=document.getElementById('mp-lyrics-toggle');
  overlay.hidden=!open;overlay.closest('.player-stage').classList.toggle('lyrics-open',open);button.classList.toggle('active',open);button.setAttribute('aria-pressed',String(open));button.setAttribute('aria-label',open?'Hide lyrics':'Show lyrics');
  if(open)mpLoadLyrics(false);
}

function mpSyncConnection() {
  const pill=document.getElementById('mp-connection'), label=pill.querySelector('span');
  pill.dataset.state=!MP.online?'offline':MP.currentLocal?'local':'online';label.textContent=!MP.online?(MP.currentLocal?'Offline · local':'Offline'):MP.currentLocal?'Playing local':'Online';
}

function mpSyncControlState() {
  const play=document.getElementById('mp-play');play.querySelector('i').className=MP.playing?'fa-solid fa-pause':'fa-solid fa-play';play.setAttribute('aria-label',MP.playing?'Pause':'Play');play.setAttribute('aria-pressed',String(MP.playing));
  const shuffle=document.getElementById('mp-shuffle');shuffle.classList.toggle('active',MP.shuffle);shuffle.setAttribute('aria-pressed',String(MP.shuffle));
  const repeat=document.getElementById('mp-repeat');repeat.classList.toggle('active',MP.repeat>0);repeat.dataset.mode=MP.repeat===2?'one':MP.repeat===1?'all':'off';repeat.querySelector('i').className='fa-solid fa-repeat';repeat.setAttribute('aria-label',['Repeat off','Repeat all','Repeat one'][MP.repeat]);repeat.setAttribute('aria-pressed',String(MP.repeat>0));
  document.getElementById('mp-radio').classList.toggle('active',MP.radioMode);
  mpNativeMediaUpdate(true);
}

function mpNativeMediaUpdate(force=false){
  if(!MP.isAndroid||!window.AkiAndroidMedia)return;
  const track=MP.queue[MP.queueIdx];
  if(!track){MP_NATIVE_MEDIA_SIGNATURE='';try{window.AkiAndroidMedia.clearPlayback()}catch(_error){}return}
  const now=Date.now(),position=Number.isFinite(MP_AUDIO.currentTime)?MP_AUDIO.currentTime:(MP.savedPosition||0),duration=Number.isFinite(MP_AUDIO.duration)&&MP_AUDIO.duration>0?MP_AUDIO.duration:mpDurationSeconds(track.dur),rawArt=mpArtUrl(track);let art='';
  try{art=rawArt?new URL(rawArt,location.origin).href:''}catch(_error){art=rawArt||''}
  const payload={title:track.name||'AkiMelody',artist:track.artist||'',album:track.album||track.albumName||'Single',art,playing:!!MP.playing,position,duration};
  const signature=[mpPlaybackKey(track),payload.playing,art,Math.floor(position/2),Math.round(duration)].join('|');
  if(!force&&(signature===MP_NATIVE_MEDIA_SIGNATURE||now-MP_NATIVE_MEDIA_SENT_AT<1200))return;
  MP_NATIVE_MEDIA_SIGNATURE=signature;MP_NATIVE_MEDIA_SENT_AT=now;
  try{window.AkiAndroidMedia.updatePlayback(JSON.stringify(payload))}catch(error){console.warn('Android media update failed',error)}
}

window.AkiAndroidMediaAction=function(action){
  if(action==='play'){if(MP_AUDIO.paused)mpTogglePlay();return}
  if(action==='pause'){if(!MP_AUDIO.paused)MP_AUDIO.pause();return}
  if(action==='next'){mpNext(true);return}
  if(action==='previous')mpPrev();
};

function mpClearArtworkTransition(generation=MP.artworkGeneration,promote=false){if(generation!==MP.artworkGeneration)return;clearTimeout(MP.artworkTransitionTimer);MP.artworkTransitionTimer=null;const stage=document.querySelector('.player-stage'),art=document.getElementById('mp-art'),incoming=document.getElementById('mp-art-incoming');if(promote&&incoming.naturalWidth&&(incoming.currentSrc||incoming.src)){art.onload=null;art.onerror=null;art.src=incoming.currentSrc||incoming.src;art.style.display='block'}stage.classList.remove('cover-transitioning','cover-transition-active','cover-forward','cover-backward');incoming.onload=null;incoming.onerror=null;incoming.style.display='none';incoming.removeAttribute('src')}
function mpPrepareArtworkTransition(direction){const stage=document.querySelector('.player-stage'),art=document.getElementById('mp-art'),reduced=matchMedia('(prefers-reduced-motion: reduce)').matches,source=art.currentSrc||art.src;mpClearArtworkTransition(MP.artworkGeneration,stage.classList.contains('cover-transition-active'));if(reduced||!source||!art.naturalWidth||art.style.display==='none')return false;stage.classList.add('cover-transitioning',direction==='backward'?'cover-backward':'cover-forward');return true}
function mpCommitArtworkTransition(generation){if(generation!==MP.artworkGeneration)return;const stage=document.querySelector('.player-stage');requestAnimationFrame(()=>{if(generation!==MP.artworkGeneration)return;stage.classList.add('cover-transition-active');MP.artworkTransitionTimer=setTimeout(()=>mpClearArtworkTransition(generation,true),760)})}
function mpSyncTrackUI(options={}) {
  const track=MP.queue[MP.queueIdx];
  if(!track)return;
  const trackKey=mpPlaybackKey(track),animateArt=!!options.animateArt&&trackKey!==MP.currentTrackKey;MP.currentTrackKey=trackKey;
  const restoredPosition=MP.savedTrackKey===trackKey?Math.max(0,Number(MP.savedPosition)||0):0,knownDuration=mpDurationSeconds(track.dur);
  document.getElementById('mp-current-time').textContent=mpFmt(restoredPosition);
  if(!MP_AUDIO.src)document.getElementById('mp-seek').value=String(knownDuration?Math.min(1000,Math.round(restoredPosition/knownDuration*1000)):0);
  const offlinePending=MP.offlinePendingIds.has(track.tid),localChip=document.getElementById('mp-local-chip');document.getElementById('mp-title').textContent=track.name;document.getElementById('mp-artist').textContent=track.artist;document.getElementById('mp-album').textContent=track.album||'Single';document.getElementById('mp-source').textContent=track.local_audio?'Saved locally':offlinePending?'Saving for offline…':track.videoId?'YouTube Music':'AkiMelody';document.getElementById('mp-total-time').textContent=mpFmt(track.dur);localChip.hidden=!track.local_audio&&!offlinePending;localChip.innerHTML=track.local_audio?'<i class="fa-solid fa-download"></i> On device':'<i class="fa-solid fa-circle-notch fa-spin"></i> Saving';
  document.getElementById('mp-home-track').textContent=track.name;document.getElementById('mp-home-artist').textContent=track.artist;
  const art=document.getElementById('mp-art'),homeArt=document.getElementById('mp-home-art'),placeholder=document.getElementById('mp-art-placeholder'),url=mpArtUrl(track);
  if(url&&(animateArt||url!==MP.currentArtworkUrl)){const generation=++MP.artworkGeneration,transitioning=animateArt&&mpPrepareArtworkTransition(options.direction),incoming=document.getElementById('mp-art-incoming'),target=transitioning?incoming:art,safeUrl=`url("${url.replace(/"/g,'%22')}")`;let settled=false;const ready=()=>{if(settled||generation!==MP.artworkGeneration)return;settled=true;MP.currentArtworkUrl=url;MobilePalette.extract(target);document.getElementById('mp-cover-bg').style.backgroundImage=safeUrl;document.getElementById('mp-cover-hero').style.backgroundImage=safeUrl;if(transitioning)mpCommitArtworkTransition(generation);else mpClearArtworkTransition(generation)};target.onload=ready;target.onerror=()=>{if(generation!==MP.artworkGeneration)return;settled=true;MP.currentArtworkUrl='';mpClearArtworkTransition(generation);art.style.display='none';placeholder.style.display='flex';MobilePalette.reset();document.getElementById('mp-cover-bg').style.backgroundImage='';document.getElementById('mp-cover-hero').style.backgroundImage=''};target.src=url;target.style.display='block';placeholder.style.display='none';if(target.complete&&target.naturalWidth)requestAnimationFrame(ready)}else if(url){art.style.display='block';placeholder.style.display='none'}else{MP.artworkGeneration++;MP.currentArtworkUrl='';mpClearArtworkTransition();art.style.display='none';homeArt.style.display='none';placeholder.style.display='flex';MobilePalette.reset();document.getElementById('mp-cover-bg').style.backgroundImage='';document.getElementById('mp-cover-hero').style.backgroundImage=''}
  if(url){homeArt.onerror=()=>{homeArt.style.display='none'};if(homeArt.src!==new URL(url,location.origin).href)homeArt.src=url;homeArt.style.display='block'}
  const liked=MP.likedIds.has(track.tid),like=document.getElementById('mp-like');like.classList.toggle('active',liked);like.querySelector('i').className=liked?'fa-solid fa-heart':'fa-regular fa-heart';like.setAttribute('aria-pressed',String(liked));
  document.getElementById('mp-lyrics-subtitle').textContent=`${track.name} · ${track.artist}`;mpRenderQueue();mpPersist();mpNativeMediaUpdate(true);
}

function mpPlaybackKey(track){return track&&(track.tid||track.videoId||`${track.name}|${track.artist}`)||''}
function mpQueueSource(source,startIndex=0,context={}){
  const queue=(Array.isArray(source)?source:[]).map(mpTrack).filter(track=>track.name&&track.artist).slice(0,250);
  if(!queue.length)return false;
  MP.queue=queue;MP.queueIdx=Math.min(Math.max(Number(startIndex)||0,0),queue.length-1);
  const selectedKey=mpPlaybackKey(MP.queue[MP.queueIdx]);
  if(selectedKey!==MP.savedTrackKey){MP.savedTrackKey=selectedKey;MP.savedPosition=0}
  MP.queueContext={type:String(context.type||'collection'),label:String(context.label||'Collection').slice(0,100),radioExtended:false};
  mpResetQueuePrefetch();
  mpPersist();mpRenderQueue();return true;
}
function mpPlayFromSource(source,index,context={}){if(!mpQueueSource(source,index,context))return;mpPlay(MP.queueIdx);mpSwitchView('player')}
function mpPlaySearchResult(track,query=''){
  const seed=mpTrack(track);mpWarmSearchTracks([seed],1);MP.radioMode=true;mpSyncControlState();
  if(!mpQueueSource([seed],0,{type:'search-radio',label:`Radio · ${seed.name||query||'Search'}`}))return;
  mpPlay(0);mpSwitchView('player');
}
function mpAppendQueue(track){MP.queue.push(mpTrack(track));MP.queue=MP.queue.slice(0,250);MP.queueContext={type:'mixed',label:'Mixed queue',radioExtended:false};mpResetQueuePrefetch();mpPersist();mpRenderQueue();if(MP.playing)mpPrefetchNext();mpToast('Added to queue')}
function mpRemoveQueue(index){
  if(index<0||index>=MP.queue.length)return;
  const wasCurrent=index===MP.queueIdx;MP.queue.splice(index,1);
  if(!MP.queue.length){MP.queueIdx=-1;MP.queueContext={type:'manual',label:'Queue',radioExtended:false};MP.savedTrackKey='';MP.savedPosition=0;MP.currentSourceKey='';MP_AUDIO.pause();MP_AUDIO.removeAttribute('src');MP_AUDIO.load();MP.playing=false;mpSyncControlState()}
  else if(index<MP.queueIdx)MP.queueIdx--;
  else if(wasCurrent){MP.queueIdx=Math.min(index,MP.queue.length-1);mpPersist();mpRenderQueue();mpPlay(MP.queueIdx);return}
  mpResetQueuePrefetch();mpPersist();mpRenderQueue();if(MP.playing)mpPrefetchNext();if(MP.view==='library')mpRenderLibrary();
}
function mpFinishStarting(generation){if(MP.playStarting===generation)MP.playStarting=0}
function mpStreamParams(track,{force=false,skipLocal=false}={}){return{q:`${track.artist} ${track.name} audio`.trim(),tid:track.tid,vid:track.videoId,title:track.name,artist:track.artist,duration:track.dur,album:track.album,albumId:track.albumId,force:force?1:0,local_only:MP.online?0:1,skip_local:(skipLocal||MP.unplayableLocalIds.has(track.tid))?1:0,_c:'mobile-player-v2'}}
function mpDurationSeconds(value){if(typeof value==='number')return value>10000?value/1000:value;const parts=String(value||'').split(':').map(Number);if(parts.some(Number.isNaN))return 0;if(parts.length===2)return parts[0]*60+parts[1];if(parts.length===3)return parts[0]*3600+parts[1]*60+parts[2];return Number(value)||0}
function mpReleasePrebuffer(){MP_PREBUFFER_AUDIO.pause();MP_PREBUFFER_AUDIO.removeAttribute('src');MP_PREBUFFER_AUDIO.load();MP.prebufferKey='';MP.prebufferUrl='';MP.prebufferReady=false}
MP_PREBUFFER_AUDIO.addEventListener('loadeddata',()=>{if(MP.prebufferUrl&&MP_PREBUFFER_AUDIO.src===new URL(MP.prebufferUrl,location.href).href)MP.prebufferReady=true});
MP_PREBUFFER_AUDIO.addEventListener('error',()=>{MP.prebufferReady=false});
function mpSourceUrls(result){
  // One authoritative URL per tap. Android receives the same Flask-protected
  // transport as desktop; raw Google URLs are never retried in the renderer.
  return result&&result.url?[result.url]:[];
}
function mpTakeWarmedStream(track){
  const key=mpPlaybackKey(track),entry=MP.warmedStreams.get(key);if(!entry)return null;
  MP.warmedStreams.delete(key);
  if(Date.now()-entry.createdAt>8*60*1000){if(MP.prebufferKey===key)mpReleasePrebuffer();return null}
  let preparedUrl='';
  if(MP.prebufferKey===key&&MP.prebufferUrl){preparedUrl=MP.prebufferUrl;entry.preparedReady=MP.prebufferReady;mpReleasePrebuffer()}
  return{...entry,preparedUrl};
}
async function mpResolveLocalSource(track,signal=null){
  if(!track||!track.tid||(MP.online&&MP.unplayableLocalIds.has(track.tid)))return null;
  const data=await mpApiFetch('/api/media/status',{tids:[track.tid]},'POST',2500,{signal});
  const state=data&&data.tracks&&data.tracks[track.tid];
  if(!state)return null;
  track.local_audio=!!state.local_audio;track.local_art=!!state.local_art;
  if(!state.local_audio||!state.url)return null;
  const key=mpPlaybackKey(track);MP.warmedStreams.delete(key);MP.streamBuildInFlight.delete(key);
  if(MP.prebufferKey===key)mpReleasePrebuffer();
  return{url:state.url,local:true,offlineReady:true,source:state.audio_source||'device',format:state.format||''};
}
async function mpBuildStreamCandidatesReal(track,generation,{force=false,skipLocal=false,signal=null}={}){
  const result=await mpApiFetch('/api/stream',mpStreamParams(track,{force,skipLocal}),'GET',MP_STREAM_TIMEOUT_MS,{signal});
  if(generation!==MP.playGeneration&&generation!==0)return[];
  if(!result||!result.url)return[];
  if(result.matchedVideoId&&(!track.videoId||result.recordingResolved))track.videoId=result.matchedVideoId;
  if(result.local)track.local_audio=true;
  return[result];
}
async function mpBuildStreamCandidates(track,generation,{force=false,skipLocal=false,signal=null}={}){
  const key=mpPlaybackKey(track);if(!signal&&!force&&!skipLocal&&key&&MP.streamBuildInFlight.has(key)){try{return await MP.streamBuildInFlight.get(key)}catch(_error){}}
  if(!key||force||skipLocal||signal)return mpBuildStreamCandidatesReal(track,generation,{force,skipLocal,signal});
  const build=mpBuildStreamCandidatesReal(track,generation).finally(()=>MP.streamBuildInFlight.delete(key));MP.streamBuildInFlight.set(key,build);return build;
}
function mpIsGestureBlock(error){return !!error&&(error.name==='NotAllowedError'||/user gesture|not allowed/i.test(error.message||''))}
function mpWaitForStreamRetry(signal){return new Promise((resolve,reject)=>{if(signal&&signal.aborted){const error=new Error('Playback superseded');error.name='AbortError';reject(error);return}let timer=null;const onAbort=()=>{clearTimeout(timer);const error=new Error('Playback superseded');error.name='AbortError';reject(error)};timer=setTimeout(()=>{if(signal)signal.removeEventListener('abort',onAbort);resolve()},MP_STREAM_RETRY_DELAY_MS);if(signal)signal.addEventListener('abort',onAbort,{once:true})})}
function mpPlaybackMessage(error){
  if(!MP.online||(error&&error.payload&&error.payload.offline))return'This track is not available offline';
  if(error&&error.name==='AbortError')return'Stream lookup was canceled';
  if(error&&error.name==='TimeoutError')return'The audio host did not respond in time';
  if(error&&error.status===401)return'YouTube Music sign-in needs attention';
  if(error&&error.mediaCode===3)return'This audio format could not be decoded';
  if(error&&error.mediaCode===4)return'No compatible stream was found for this track';
  if(error&&/stream not available|no playable/i.test(error.message||''))return'No playable YouTube source was found';
  return'Unable to open a verified stream for this track';
}
function mpMediaFailure(error){const failure=error instanceof Error?error:new Error('Audio playback failed');const media=MP_AUDIO.error;if(media)failure.mediaCode=media.code;return failure}
async function mpAwaitAudioStart(){
  return new Promise((resolve,reject)=>{
    let settled=false,timer=null;
    const finish=(callback,value)=>{if(settled)return;settled=true;clearTimeout(timer);MP_AUDIO.removeEventListener('playing',onPlaying);MP_AUDIO.removeEventListener('error',onError);callback(value)};
    const onPlaying=()=>finish(resolve);
    const onError=()=>finish(reject,mpMediaFailure(new Error('Audio source failed before playback')));
    MP_AUDIO.addEventListener('playing',onPlaying);MP_AUDIO.addEventListener('error',onError);
    timer=setTimeout(()=>{const error=new Error('Audio startup timed out');error.name='TimeoutError';finish(reject,error)},MP_AUDIO_START_TIMEOUT_MS);
    try{Promise.resolve(MP_AUDIO.play()).catch(error=>finish(reject,error))}catch(error){finish(reject,error)}
  });
}
async function mpApplySavedPosition(generation,key,position){
  const target=Math.max(0,Number(position)||0);if(!target||key!==MP.savedTrackKey)return;
  if(MP_AUDIO.readyState<1){
    await new Promise(resolve=>{let settled=false;const finish=()=>{if(settled)return;settled=true;clearTimeout(timer);MP_AUDIO.removeEventListener('loadedmetadata',finish);MP_AUDIO.removeEventListener('error',finish);resolve()};const timer=setTimeout(finish,2500);MP_AUDIO.addEventListener('loadedmetadata',finish,{once:true});MP_AUDIO.addEventListener('error',finish,{once:true})});
  }
  if(generation!==MP.playGeneration||key!==MP.savedTrackKey)return;
  const duration=Number(MP_AUDIO.duration)||0,resumeAt=duration?Math.min(target,Math.max(0,duration-2)):target;
  if(resumeAt>0)try{MP_AUDIO.currentTime=resumeAt}catch(error){console.warn('Could not restore playback position',error)}
}
async function mpTryResolvedSource(result,generation,preparedUrl='',key='',resumeAt=0){
  if(generation!==MP.playGeneration){const stale=new Error('Playback superseded');stale.stale=true;throw stale}
  const url=preparedUrl&&preparedUrl===result.url?preparedUrl:result.url;if(!url)throw new Error('No playable stream returned');
  document.getElementById('mp-source').textContent=result.local?'Opening saved audio…':preparedUrl===url?'Opening prepared stream…':'Opening verified stream…';const started=performance.now();MP_AUDIO.src=url;MP_AUDIO.load();
  try{await mpApplySavedPosition(generation,key,resumeAt);await mpAwaitAudioStart();if(generation!==MP.playGeneration){const stale=new Error('Playback superseded');stale.stale=true;throw stale}console.debug('[MOBILE PLAYBACK]',{phase:'audio-playing',transport:result.local?'local':'proxy',prepared:preparedUrl===url,elapsedMs:Math.round(performance.now()-started)});return url}catch(error){const failure=mpMediaFailure(error);console.warn('[MOBILE PLAYBACK]',{phase:'source-failed',transport:result.local?'local':'proxy',elapsedMs:Math.round(performance.now()-started),error:failure.message});throw failure}
}

async function mpPlay(index, replacement = null, options = {}) {
  const previousIndex=MP.queueIdx,replacing=!!replacement;if(replacement){if(!mpQueueSource(replacement,index,options.context||{}))return}else MP.queueIdx=index;
  if(MP.queueIdx<0||MP.queueIdx>=MP.queue.length)return;
  const track=MP.queue[MP.queueIdx],key=mpPlaybackKey(track);if(MP.playStarting&&key===MP.currentTrackKey)return;
  const generation=++MP.playGeneration,direction=options.direction||(!replacing&&previousIndex>=0&&MP.queueIdx<previousIndex?'backward':'forward'),playStarted=performance.now();
  if(key!==MP.savedTrackKey){MP.savedTrackKey=key;MP.savedPosition=0}
  if(MP.playbackAbort)MP.playbackAbort.abort();
  MP.playbackAbort=new AbortController();
  const playbackSignal=MP.playbackAbort.signal;
  if(MP.queueWarmAbort){MP.queueWarmAbort.abort();MP.queueWarmAbort=null}
  MP.playStarting=generation;MP.lyrics=[];MP.lyricsSynced=false;MP.activeLyric=-1;MP.lyricsKey='';MP.lyricGeneration++;const lyricContent=document.getElementById('mp-lyrics-content');lyricContent.replaceChildren();if(!document.getElementById('mp-album-lyrics').hidden)lyricContent.appendChild(mpLoading('Lyrics will follow playback…'));MP_AUDIO.pause();MP_AUDIO.removeAttribute('src');MP_AUDIO.load();MP.currentSource=null;MP.playing=false;mpSyncControlState();mpSyncTrackUI({animateArt:true,direction});mpPrefetchRecordingWindow();
  const skipLocal=!!options.skipLocal;let lastError=null;
  let localCandidate=null;
  if(!skipLocal){try{localCandidate=await mpResolveLocalSource(track,playbackSignal)}catch(error){if(error&&error.name==='AbortError')return;console.warn('On-device media check failed',error)}}
  if(generation!==MP.playGeneration)return;
  const warmed=!localCandidate&&!skipLocal?mpTakeWarmedStream(track):null;
  if(MP.prebufferKey)mpReleasePrebuffer();
  let preparedUrl=warmed&&warmed.preparedUrl||'',preparedCandidate=warmed&&warmed.candidates&&warmed.candidates[0]||null;
  const finishPlayback=async result=>{
    if(!result||!result.url)throw new Error('No playable stream returned');
    if(result.matchedVideoId&&(!track.videoId||result.recordingResolved))track.videoId=result.matchedVideoId;
    MP.currentLocal=!!result.local;if(result.local)track.local_audio=true;mpSyncConnection();mpSyncTrackUI();
    const handoffUrl=result===preparedCandidate?preparedUrl:'';preparedUrl='';preparedCandidate=null;
    const resumeAt=MP.savedTrackKey===key?MP.savedPosition:0,playedUrl=await mpTryResolvedSource(result,generation,handoffUrl,key,resumeAt);if(generation!==MP.playGeneration)return false;
    MP.currentSource={...result,playedUrl};MP.currentSourceKey=key;MP.playing=true;mpFinishStarting(generation);document.getElementById('mp-source').textContent=result.local?'Playing on-device audio':'Streaming';console.debug('[MOBILE PLAYBACK]',{phase:'ready',track:key,totalMs:Math.round(performance.now()-playStarted),usedWarm:!!warmed});mpSyncControlState();mpPersist();mpPrefetchNext();mpPushHistory(track);if(MP.queueContext.type==='search-radio'&&!MP.queueContext.radioExtended)mpRadioSuggest(false);return true;
  };
  let candidates=localCandidate?[localCandidate]:(warmed&&warmed.candidates||null);
  if(!candidates){document.getElementById('mp-source').textContent='Resolving verified recording…';try{candidates=await mpBuildStreamCandidates(track,generation,{skipLocal,signal:playbackSignal})}catch(error){if(error&&error.name==='AbortError')return;lastError=error;candidates=[]}}
  if(generation!==MP.playGeneration)return;
  let primary=(candidates||[])[0],remoteOnly=skipLocal;
  if(primary){
    try{if(await finishPlayback(primary))return}
    catch(error){
      if(generation!==MP.playGeneration||error.stale)return;
      lastError=error;
      if(mpIsGestureBlock(error)&&MP_AUDIO.src){MP.playing=false;mpFinishStarting(generation);document.getElementById('mp-source').textContent='Ready — tap Play';mpSyncControlState();return}
      if(primary.local&&MP.online){
        remoteOnly=true;if(track.tid)MP.unplayableLocalIds.add(track.tid);track.local_audio=false;MP.currentLocal=false;mpSyncConnection();document.getElementById('mp-source').textContent='Local copy failed · opening online stream…';
        try{const remote=await mpBuildStreamCandidates(track,generation,{skipLocal:true,signal:playbackSignal});primary=remote[0]||null;if(primary&&await finishPlayback(primary))return}
        catch(remoteError){if(remoteError&&remoteError.name==='AbortError')return;lastError=remoteError}
      }
    }
  }
  // One bounded retry for a transient resolver or startup failure. It keeps the
  // same strict backend path, but bypasses a stale signed-URL cache after 2s.
  if(MP.online&&generation===MP.playGeneration&&!(lastError&&lastError.status===401)){
    document.getElementById('mp-source').textContent='Retrying verified stream in 2 seconds…';
    try{
      await mpWaitForStreamRetry(playbackSignal);
      if(generation!==MP.playGeneration)return;
      MP_AUDIO.pause();MP_AUDIO.removeAttribute('src');MP_AUDIO.load();preparedUrl='';preparedCandidate=null;
      document.getElementById('mp-source').textContent='Refreshing verified stream…';
      const retry=await mpBuildStreamCandidates(track,generation,{force:true,skipLocal:remoteOnly,signal:playbackSignal});
      if(retry[0]&&await finishPlayback(retry[0]))return;
    }catch(retryError){if(retryError&&retryError.name==='AbortError')return;lastError=retryError}
  }
  if(generation!==MP.playGeneration)return;MP.playing=false;MP.currentSource=null;MP.currentSourceKey='';MP_AUDIO.pause();MP_AUDIO.removeAttribute('src');MP_AUDIO.load();mpFinishStarting(generation);document.getElementById('mp-source').textContent='Playback unavailable';mpSyncControlState();mpToast(mpPlaybackMessage(lastError));console.warn('Mobile playback failed',{track:key,error:lastError});
}

function mpTogglePlay(){if(MP.playStarting){document.getElementById('mp-source').textContent='Still connecting…';return}if((!MP_AUDIO.src||MP_AUDIO.error)&&MP.queue.length){mpPlay(Math.max(0,MP.queueIdx));return}if(MP_AUDIO.paused)MP_AUDIO.play().catch(error=>{console.warn('Play failed',error);mpToast(mpIsGestureBlock(error)?'Tap Play again to allow audio':mpPlaybackMessage(mpMediaFailure(error)))});else MP_AUDIO.pause()}
function mpNext(allowRadio=true){if(!MP.queue.length)return;if(MP.shuffle&&MP.queue.length>1){let next;do{next=Math.floor(Math.random()*MP.queue.length)}while(next===MP.queueIdx);mpPlay(next,null,{direction:'forward'});return}if(MP.queueIdx<MP.queue.length-1)mpPlay(MP.queueIdx+1,null,{direction:'forward'});else if(MP.repeat===1)mpPlay(0,null,{direction:'forward'});else if(MP.online&&MP.radioMode&&allowRadio)mpRadioSuggest(true)}
function mpPrev(){if(MP_AUDIO.currentTime>4){MP_AUDIO.currentTime=0;return}if(MP.queueIdx>0)mpPlay(MP.queueIdx-1,null,{direction:'backward'})}
function mpResetQueuePrefetch(){clearTimeout(MP.queueStreamWarmTimer);MP.queueStreamWarmTimer=null;if(MP.queueWarmAbort)MP.queueWarmAbort.abort();MP.queueWarmAbort=null;MP.queueRecordingPrefetchKey='';MP.queueStreamWarmKey='';MP.queueWarmAttemptKey='';MP.warmedStreams.clear();mpReleasePrebuffer()}
function mpNextQueueIndices(limit=MP_QUEUE_PREFETCH_COUNT){
  if(MP.queueIdx<0||MP.queue.length<2)return[];
  const indices=[];
  for(let offset=1;offset<MP.queue.length&&indices.length<limit;offset++){
    const index=MP.queueIdx+offset;
    if(index<MP.queue.length)indices.push(index);
    else if(MP.repeat===1)indices.push(index%MP.queue.length);
    else break;
  }
  return indices;
}
function mpPostPrefetch(mode,scope,generation,tracks){return fetch('/api/stream/prefetch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode,scope,generation,tracks})}).catch(error=>console.warn(`Mobile ${mode} prefetch failed`,error))}
function mpPrefetchRecordingWindow(){
  if(!MP.online||MP.queueIdx<0)return;
  const recordingTracks=MP.queue.slice(MP.queueIdx+1,MP.queueIdx+4).filter(track=>!track.videoId&&!track.local_audio),recordingKey=recordingTracks.map(mpPlaybackKey).join('|');
  if(recordingTracks.length&&recordingKey!==MP.queueRecordingPrefetchKey){MP.queueRecordingPrefetchKey=recordingKey;mpPostPrefetch('recording','mobile-player-queue-recordings',String(++MP.queueWarmGeneration),recordingTracks)}
}
function mpPrefetchNext(){
  if(!MP.online||!MP.playing)return;
  mpPrefetchRecordingWindow();
  // Desktop-parity: queue look-ahead stays recording-only (no full stream warming)
  // Backend's 1-worker prefetch handles it without blocking the UI thread
}
async function mpRadioSuggest(advance){const current=MP.queue[MP.queueIdx],seedKey=mpPlaybackKey(current);if(!current||!current.videoId||MP.radioSuggesting)return;MP.radioSuggesting=true;MP.radioSuggestKey=seedKey;try{const results=await mpApiFetch('/api/radio/suggest',{vid:current.videoId,active_queue:MP.queue.map(track=>track.videoId).filter(Boolean).join(','),history:MP.history.map(track=>track.videoId).filter(Boolean).join(',')});if(seedKey!==mpPlaybackKey(MP.queue[MP.queueIdx]))return;const ids=new Set(MP.queue.map(track=>track.videoId));const fresh=(results||[]).map(mpTrack).filter(track=>track.videoId&&!ids.has(track.videoId));if(fresh.length){MP.queue.push(...fresh);MP.queue=MP.queue.slice(0,250);MP.queueContext.radioExtended=true;mpPersist();mpRenderQueue();if(advance)mpNext(false);else if(MP.playing)mpPrefetchNext()}else mpToast('Radio queue is caught up')}catch(error){console.warn('Radio suggest failed',error);mpToast('Radio is unavailable right now')}finally{MP.radioSuggesting=false;MP.radioSuggestKey='';const latest=MP.queue[MP.queueIdx];if(latest&&mpPlaybackKey(latest)!==seedKey&&MP.queueContext.type==='search-radio'&&!MP.queueContext.radioExtended)mpRadioSuggest(false)}}
function mpPushHistory(track){MP.history=[track,...MP.history.filter(item=>item.tid!==track.tid)].slice(0,30);try{localStorage.setItem('akimelody_mobile_history',JSON.stringify(MP.history))}catch(_error){}}

function mpTrackRow(track,index,context='list',source=null,sourceContext={}) {
  const row=mpEl('article','track-row');if(MP.queue[MP.queueIdx]&&track.tid&&MP.queue[MP.queueIdx].tid===track.tid)row.classList.add('active');
  const art=mpEl('span','row-art'),url=mpArtUrl(track);if(url){const image=document.createElement('img');image.src=url;image.alt='';image.loading='lazy';image.onerror=()=>{image.remove();art.appendChild(mpEl('i','fa-solid fa-music'))};art.appendChild(image)}else art.appendChild(mpEl('i','fa-solid fa-music'));
  const copy=mpEl('span','row-copy');copy.append(mpEl('strong','',track.name),mpEl('span','',track.artist));const meta=mpEl('span','row-meta',track.local_audio?'Offline':MP.offlinePendingIds.has(track.tid)?'Saving…':mpFmt(track.dur));const action=mpEl('button','row-action');action.type='button';action.setAttribute('aria-label',context==='queue'?'Remove from queue':'Add to queue');action.innerHTML=context==='queue'?'<i class="fa-solid fa-xmark"></i>':'<i class="fa-solid fa-plus"></i>';
  action.addEventListener('click',event=>{event.stopPropagation();if(context==='queue')mpRemoveQueue(index);else mpAppendQueue(track)});
  row.append(art,copy,meta,action);row.addEventListener('click',()=>{if(context==='queue'){mpPlay(index);mpSwitchView('player')}else if(sourceContext&&sourceContext.type==='search')mpPlaySearchResult(track,String(sourceContext.label||'').replace(/^Search\s*·\s*/i,''));else mpPlayFromSource(source||[track],index,sourceContext)});return row;
}

function mpDiscoveryRow(item, type) {
  const title=item.title||item.name||`Unknown ${type}`, artist=item.artist||item.subtitle||(type==='artist'?'Artist':''), artUrl=mpArtUrl({art:item.art||item.thumbnail||''});
  const row=mpEl('button','track-row');row.type='button';
  const art=mpEl('span','row-art');if(artUrl){const image=document.createElement('img');image.src=artUrl;image.alt='';image.loading='lazy';image.onerror=()=>{image.remove();art.appendChild(mpEl('i',type==='artist'?'fa-solid fa-user':'fa-solid fa-compact-disc'))};art.appendChild(image)}else art.appendChild(mpEl('i',type==='artist'?'fa-solid fa-user':'fa-solid fa-compact-disc'));
  const copy=mpEl('span','row-copy');copy.append(mpEl('strong','',title),mpEl('span','',artist));
  const meta=mpEl('span','row-meta',type==='album'?(item.trackCount?`${item.trackCount} tracks`:'Album'):'Artist');
  row.append(art,copy,meta,mpEl('i','fa-solid fa-chevron-right row-meta'));
  row.addEventListener('click',()=>type==='album'?mpOpenAlbum(item):mpOpenArtist(title,item.browseId||''));
  return row;
}

function mpRenderQueue(){const list=document.getElementById('mp-queue-list'),count=MP.queue.length,source=MP.queueContext&&MP.queueContext.label?MP.queueContext.label:'Queue',radio=MP.queueContext&&MP.queueContext.radioExtended?' + Radio':'';document.getElementById('mp-queue-count').textContent=count>99?'99+':String(count);document.getElementById('mp-queue-summary').textContent=count?`${count} ${count===1?'track':'tracks'} · ${source}${radio}`:'0 tracks';document.getElementById('mp-home-queue').textContent=`${count} ${count===1?'track':'tracks'}`;list.replaceChildren();if(!count){list.appendChild(mpEmpty('fa-solid fa-list-ul','Your queue is clear','Add music from Search, Favorites, or a playlist.'));return}MP.queue.forEach((track,index)=>list.appendChild(mpTrackRow(track,index,'queue')))}

function mpRenderHome(){const shelf=document.getElementById('mp-home-shelf');document.getElementById('mp-home-favorites').textContent=`${MP.liked.length} saved`;document.getElementById('mp-home-playlists').textContent=`${MP.playlists.length} collections`;shelf.replaceChildren();const isHistory=MP.history.length>0,items=(isHistory?MP.history:MP.liked).slice(0,8),context={type:isHistory?'history':'favorites',label:isHistory?'Recently played':'Favorites'};if(!items.length){shelf.appendChild(mpEmpty('fa-solid fa-wand-magic-sparkles','Your space is ready','Favorite or play music to build this shelf.'));return}items.forEach((track,index)=>{const card=mpEl('button','shelf-card'),url=mpArtUrl(track);card.type='button';if(url){const image=document.createElement('img');image.src=url;image.alt='';image.loading='lazy';image.onerror=()=>{image.replaceWith(mpEl('span','shelf-placeholder','♪'))};card.appendChild(image)}else card.appendChild(mpEl('span','shelf-placeholder','♪'));card.append(mpEl('strong','',track.name),mpEl('span','',track.artist));card.addEventListener('click',()=>mpPlayFromSource(items,index,context));shelf.appendChild(card)})}

async function mpLoadFavorites(){try{const favorites=await mpApiFetch('/api/favorites');MP.liked=Array.isArray(favorites)?favorites.map(mpTrack):[];MP.likedIds=new Set(MP.liked.map(track=>track.tid).filter(Boolean));await mpReconcileMedia()}catch(error){console.warn('Favorites load failed',error)}finally{MP.favoritesLoaded=true}}
async function mpLoadPlaylists(){try{const playlists=await mpApiFetch('/api/playlists');MP.playlists=Array.isArray(playlists)?playlists:[]}catch(error){console.warn('Playlists load failed',error)}finally{MP.playlistsLoaded=true}}
async function mpGetPlaylistTracks(playlist,force=false){const name=playlist&&playlist.name;if(!name)return[];if(!force&&MP.playlistTracks.has(name))return MP.playlistTracks.get(name);if(!force&&MP.playlistPromises.has(name))return MP.playlistPromises.get(name);const promise=mpApiFetch('/api/playlists/tracks',{name}).then(data=>{const tracks=(Array.isArray(data)?data:[]).map(mpTrack);MP.playlistTracks.set(name,tracks);return tracks}).catch(error=>{console.warn(`Playlist load failed: ${name}`,error);throw error}).finally(()=>MP.playlistPromises.delete(name));MP.playlistPromises.set(name,promise);return promise}
const MP_YOUTUBE_LIKES_PLAYLIST='YouTube Likes';
function mpYouTubeOfflinePlaylist(){return MP.playlists.find(playlist=>playlist&&playlist.name===MP_YOUTUBE_LIKES_PLAYLIST)||null}
async function mpLoadOfflineYouTubeLikes(){const playlist=mpYouTubeOfflinePlaylist();if(!playlist)return[];const tracks=await mpGetPlaylistTracks(playlist,true);MP.youtubeLikes=tracks.map(mpTrack);MP.youtubeLikesLoaded=true;MP.youtubeLikesSource='local';MP.youtubeLikesError='';return MP.youtubeLikes}
async function mpLoadYouTubeLikes(force=false){if(!force&&MP.youtubeLikesLoaded&&MP.youtubeLikesSource!=='local')return MP.youtubeLikes;if(MP.youtubeLikesPromise)return MP.youtubeLikesPromise;if(!MP.online){return mpLoadOfflineYouTubeLikes()}MP.youtubeLikesPromise=mpApiFetch('/api/youtube/liked_songs',{limit:100},'GET',22000).then(async data=>{MP.youtubeLikes=(data&&Array.isArray(data.tracks)?data.tracks:[]).map(mpTrack);MP.youtubeLikesLoaded=true;MP.youtubeLikesSource='remote';MP.youtubeLikesError='';await mpReconcileTracks(MP.youtubeLikes);return MP.youtubeLikes}).catch(async error=>{try{const local=await mpLoadOfflineYouTubeLikes();if(local.length)return local}catch(localError){console.warn('Offline YouTube Likes fallback failed',localError)}MP.youtubeLikes=[];MP.youtubeLikesLoaded=true;MP.youtubeLikesSource='';MP.youtubeLikesError=error.status===401?'Connect YouTube Music in Settings':error.status===504||error.name==='AbortError'?'YouTube Music took too long to answer · tap the card to retry':String(error.message||'YouTube Likes are unavailable').slice(0,140);return[]}).finally(()=>{MP.youtubeLikesPromise=null});return MP.youtubeLikesPromise}

function mpSetYouTubeAuthState(state) {
  const connected=!!(state&&state.authenticated), configured=!!(state&&state.oauth_configured),cookies=!!(state&&state.cookies);
  MP.isAndroid=MP.isAndroid||!!(state&&state.android);
  document.getElementById('mp-auth-state').textContent=connected&&cookies?'Connected with local browser session':connected&&MP.isAndroid?'Account linked · add playback session':configured?'Ready to authorize on this phone':'Connect locally on this phone';
  return connected;
}

async function mpRefreshYouTubeAuthStatus() {
  try { const state=await mpApiFetch('/api/youtube/auth_status');mpSetYouTubeAuthState(state);return state; }
  catch(error){document.getElementById('mp-auth-state').textContent='Could not check connection';return null}
}

function mpShowYouTubeConfig(show=true) {
  document.getElementById('mp-youtube-cookie').hidden=true;
  document.getElementById('mp-youtube-config').hidden=!show;
  document.getElementById('mp-youtube-device').hidden=show;
}

function mpShowYouTubeCookie() {
  document.getElementById('mp-youtube-cookie').hidden=false;
  document.getElementById('mp-youtube-config').hidden=true;
  document.getElementById('mp-youtube-device').hidden=true;
}

function mpStartYouTubeCookieLogin() {
  if(!MP.isAndroid||!window.AkiAndroidMedia||typeof window.AkiAndroidMedia.startYouTubeLogin!=='function'){
    mpToast('Google cookie sign-in is available in the Android app');return;
  }
  const button=document.getElementById('mp-youtube-cookie-start');button.disabled=true;button.innerHTML='<i class="fa-solid fa-circle-notch fa-spin"></i> Opening YouTube Music…';
  try{window.AkiAndroidMedia.startYouTubeLogin()}catch(error){button.disabled=false;button.innerHTML='<i class="fa-brands fa-google"></i> Sign in with Google';mpToast(error.message||'Could not open Google sign-in')}
}

window.AkiAndroidYouTubeLoginResult=async function(success,message=''){
  const button=document.getElementById('mp-youtube-cookie-start');button.disabled=false;button.innerHTML='<i class="fa-brands fa-google"></i> Sign in with Google';
  if(!success){if(message)mpToast(message);mpShowYouTubeCookie();return}
  MP.youtubeLikesLoaded=false;MP.youtubeLikesError='';
  const state=await mpRefreshYouTubeAuthStatus();
  if(state&&state.authenticated){mpToast('YouTube Music connected');setTimeout(()=>mpCloseSheet('youtube'),500)}
  else mpToast('Google sign-in finished, but YouTube Music could not be verified');
};

async function mpStartYouTubeOAuth(credentials={}) {
  const submit=document.querySelector('#mp-youtube-config button[type="submit"]');
  if(submit)submit.disabled=true;
  try{
    const flow=await mpApiFetch('/api/youtube/oauth/device',credentials,'POST',20000);
    MP.youtubeAuthFlow=flow.flow_id;MP.youtubeAuthUrl=flow.verification_url;
    document.getElementById('mp-youtube-code').textContent=flow.user_code||'--- --- ---';
    document.getElementById('mp-youtube-wait').innerHTML='<i class="fa-solid fa-circle-notch fa-spin"></i> Waiting for approval…';
    mpShowYouTubeConfig(false);mpScheduleYouTubePoll(Math.max(2,Number(flow.interval)||5));
  }catch(error){
    mpShowYouTubeConfig(true);mpToast(error.message);
  }finally{if(submit)submit.disabled=false}
}

function mpScheduleYouTubePoll(seconds) {
  clearTimeout(MP.youtubePollTimer);
  MP.youtubePollTimer=setTimeout(mpPollYouTubeOAuth,Math.max(2,Number(seconds)||5)*1000);
}

async function mpPollYouTubeOAuth() {
  if(!MP.youtubeAuthFlow||document.getElementById('mp-sheet-youtube').hidden)return;
  try{
    const state=await mpApiFetch('/api/youtube/oauth/poll',{flow_id:MP.youtubeAuthFlow},'POST',20000);
    if(state&&state.authenticated){
      MP.youtubeAuthFlow='';MP.youtubeLikesLoaded=false;MP.youtubeLikesError='';
      document.getElementById('mp-auth-state').textContent='Connected on this device';
      document.getElementById('mp-youtube-wait').innerHTML='<i class="fa-solid fa-circle-check"></i> Connected';
      mpToast('YouTube Music connected');setTimeout(()=>mpCloseSheet('youtube'),700);return;
    }
    mpScheduleYouTubePoll(state&&state.retry_after||5);
  }catch(error){
    MP.youtubeAuthFlow='';document.getElementById('mp-youtube-wait').textContent=error.message;mpToast(error.message);
  }
}

async function mpOpenYouTubeAuth() {
  if(!MP.online){mpToast('Connect to the internet first');return}
  const state=await mpRefreshYouTubeAuthStatus();if(!state)return;
  const connected=mpSetYouTubeAuthState(state);
  if(connected&&(!MP.isAndroid||state.cookies)){mpToast('YouTube Music is already connected');return}
  mpOpenSheet('youtube');
  if(MP.isAndroid)mpShowYouTubeCookie();
  else if(state.oauth_configured){mpShowYouTubeConfig(false);await mpStartYouTubeOAuth();}
  else mpShowYouTubeConfig(true);
}

async function mpCopyYouTubeCode() {
  const code=document.getElementById('mp-youtube-code').textContent.trim();
  try{await navigator.clipboard.writeText(code)}catch(_error){const area=document.createElement('textarea');area.value=code;area.style.position='fixed';area.style.opacity='0';document.body.appendChild(area);area.select();document.execCommand('copy');area.remove()}
  mpToast('Sign-in code copied');
}
function mpKnownTrackMap(){const map=new Map();[...MP.liked,...MP.youtubeLikes,...MP.playlistTracks.values()].flat().forEach(track=>{if(track&&track.tid&&!map.has(track.tid))map.set(track.tid,track)});return map}
async function mpLoadDownloads(force=false){if(MP.downloadsLoaded&&!force)return MP.downloads;try{const data=await mpApiFetch('/api/downloads/status',{},'GET',20000),known=mpKnownTrackMap(),seen=new Set();MP.downloads=(data&&Array.isArray(data.completed)?data.completed:[]).map(item=>{const base=known.get(item.tid)||item;return mpTrack({...base,...item,local_audio:true,local_art:!!(item.artSize||(base&&base.local_art))})}).filter(track=>track.tid&&!seen.has(track.tid)&&seen.add(track.tid));MP.downloadSummary=data||{};MP.downloadsLoaded=true;return MP.downloads}catch(error){console.warn('Downloads load failed',error);MP.downloadSummary={error:error.message};MP.downloadsLoaded=true;return[]}}
async function mpReconcileTracks(tracks){const items=(Array.isArray(tracks)?tracks:[]).filter(track=>track&&track.tid),tids=[...new Set(items.map(track=>track.tid))];if(!tids.length)return{};const result=await mpApiFetch('/api/media/status',{tids},'POST',10000),states=result&&result.tracks||{};items.forEach(track=>{const state=states[track.tid];if(state){track.local_audio=!!state.local_audio;track.local_art=!!state.local_art;if(state.local_audio){const key=mpPlaybackKey(track);MP.warmedStreams.delete(key);if(MP.prebufferKey===key)mpReleasePrebuffer()}}});return states}
async function mpReconcileMedia(){const groups=[MP.queue,MP.liked,MP.youtubeLikes,MP.downloads,MP.detail&&MP.detail.tracks],tracks=groups.filter(Array.isArray).flat();MP.playlistTracks.forEach(items=>tracks.push(...items));try{await mpReconcileTracks(tracks);mpPersist()}catch(error){console.warn('Media reconciliation failed',error)}}

function mpTracksWithTid(tid){const groups=[MP.queue,MP.liked,MP.downloads,MP.searchTracks,MP.youtubeLikes,MP.detail&&MP.detail.tracks];MP.playlistTracks.forEach(tracks=>groups.push(tracks));return groups.filter(Array.isArray).flat().filter(track=>track&&track.tid===tid)}
function mpApplyOfflineState(tid,state){mpTracksWithTid(tid).forEach(track=>{track.local_audio=!!state.local_audio;track.local_art=!!state.local_art});if(state.local_audio){MP.unplayableLocalIds.delete(tid);for(const [key,entry] of MP.warmedStreams){const candidate=entry&&entry.candidates&&entry.candidates[0];if(key===tid||candidate&&!candidate.local&&key.includes(tid))MP.warmedStreams.delete(key)}const current=MP.queue[MP.queueIdx];if(current&&current.tid===tid&&MP.prebufferKey===mpPlaybackKey(current))mpReleasePrebuffer()}mpPersist();const current=MP.queue[MP.queueIdx];if(current&&current.tid===tid)mpSyncTrackUI();mpRenderHome();mpRenderQueue()}
async function mpWatchOfflineDownload(track){const tid=track&&track.tid;if(!tid||mpWatchOfflineDownload.running&&mpWatchOfflineDownload.running.has(tid))return;if(!mpWatchOfflineDownload.running)mpWatchOfflineDownload.running=new Set();mpWatchOfflineDownload.running.add(tid);try{for(let attempt=0;attempt<90&&MP.offlinePendingIds.has(tid);attempt++){try{const media=await mpApiFetch('/api/media/status',{tids:[tid]},'POST',10000),state=media&&media.tracks&&media.tracks[tid];if(state&&state.local_audio){MP.offlinePendingIds.delete(tid);mpApplyOfflineState(tid,state);await mpLoadDownloads(true);document.getElementById('mp-home-downloads').textContent=`${MP.downloads.length} offline`;if(MP.view==='library')mpRenderLibrary();mpToast(`${track.name} is ready offline`);return}const statuses=await mpApiFetch('/api/download/status',{},'GET',10000),status=statuses&&statuses[tid];if(status&&status.ok===false){MP.offlinePendingIds.delete(tid);mpSyncTrackUI();mpRenderQueue();mpToast(`Offline save failed for ${track.name}`);console.warn('Offline favorite download failed',status.error);return}}catch(error){console.warn('Offline download check failed',error)}await new Promise(resolve=>setTimeout(resolve,2000))}if(MP.offlinePendingIds.delete(tid)){mpSyncTrackUI();mpRenderQueue();mpToast('Offline save is taking longer than expected')}}finally{mpWatchOfflineDownload.running.delete(tid)}}

async function mpToggleLike(){
  const current=MP.queue[MP.queueIdx];if(!current||!current.tid)return;
  const adding=!MP.likedIds.has(current.tid),snapshot=MP.liked.slice();
  if(adding){MP.likedIds.add(current.tid);MP.liked.push(current);if(!current.local_audio)MP.offlinePendingIds.add(current.tid);mpToast(current.local_audio?'Added to favorites':'Added · saving for offline…')}else{MP.likedIds.delete(current.tid);MP.liked=MP.liked.filter(track=>track.tid!==current.tid);MP.offlinePendingIds.delete(current.tid);mpToast('Removed from favorites')}
  mpSyncTrackUI();mpRenderHome();mpRenderQueue();
  try{const response=await mpApiFetch('/api/save_favorites',MP.liked,'POST');if(adding&&!current.local_audio){MP.offlinePendingIds.add(current.tid);mpSyncTrackUI();mpWatchOfflineDownload(current)}else if(response&&Array.isArray(response.queued)&&response.queued.includes(current.tid))mpWatchOfflineDownload(current)}catch(error){console.warn('Favorite save failed',error);MP.liked=snapshot;MP.likedIds=new Set(snapshot.map(track=>track.tid).filter(Boolean));MP.offlinePendingIds.delete(current.tid);mpSyncTrackUI();mpRenderHome();mpRenderQueue();mpToast('Could not save favorites')}
}

function mpLibraryArtwork(tracks,icon='fa-solid fa-music',tone=''){const art=mpEl('span',`library-card-art${tone?` ${tone}`:''}`),urls=[];(Array.isArray(tracks)?tracks:[]).forEach(track=>{const url=mpArtUrl(track);if(url&&!urls.includes(url)&&urls.length<4)urls.push(url)});art.dataset.count=String(urls.length);if(!urls.length){art.appendChild(mpEl('i',icon));return art}urls.forEach(url=>{const image=document.createElement('img');image.src=url;image.alt='';image.loading='lazy';image.onerror=()=>{image.remove();if(!art.querySelector('img')&&!art.querySelector('i'))art.appendChild(mpEl('i',icon))};art.appendChild(image)});return art}
function mpLibraryCard({kind,title,meta,tracks=[],icon='fa-solid fa-music',tone='',eyebrow='COLLECTION',onClick}){const card=mpEl('button',`library-card${tone?` ${tone}`:''}`);card.type='button';card.dataset.libraryCard=kind;card.appendChild(mpLibraryArtwork(tracks,icon,tone));const copy=mpEl('span','library-card-copy');copy.append(mpEl('small','',eyebrow),mpEl('strong','',title),mpEl('span','library-card-meta',meta));card.append(copy,mpEl('i','fa-solid fa-arrow-right library-card-arrow'));card.addEventListener('click',onClick);return card}
function mpLibraryHeading(kicker,title,action){const row=mpEl('div','library-section-heading'),copy=mpEl('div');copy.append(mpEl('span','kicker',kicker),mpEl('h2','',title));row.appendChild(copy);if(action)row.appendChild(action);return row}
function mpPlaylistCard(playlist,index){const cached=MP.playlistTracks.get(playlist.name)||[],cover=playlist.coverArt?[{art:playlist.coverArt},...cached]:cached,tone=playlist.source==='spotify'?'spotify':'';const card=mpLibraryCard({kind:'playlist',title:playlist.name,meta:`${playlist.count||cached.length||0} ${(playlist.count||cached.length)===1?'track':'tracks'}${playlist.downloaded?` · ${playlist.downloaded} offline`:''}`,tracks:cover,icon:playlist.isAlbum?'fa-solid fa-compact-disc':'fa-solid fa-folder-open',tone,eyebrow:playlist.isAlbum?'ALBUM':playlist.source==='spotify'?'SPOTIFY PLAYLIST':'PLAYLIST',onClick:()=>mpOpenPlaylist(playlist)});card.dataset.playlistIndex=String(index);return card}
async function mpHydratePlaylistCards(generation){let cursor=0;const workers=Array.from({length:Math.min(3,MP.playlists.length)},async()=>{while(cursor<MP.playlists.length){const index=cursor++,playlist=MP.playlists[index];let tracks=[];try{tracks=await mpGetPlaylistTracks(playlist)}catch(_error){continue}if(generation!==MP.libraryGeneration)return;const card=document.querySelector(`[data-playlist-index="${index}"]`);if(!card)continue;const old=card.querySelector('.library-card-art'),cover=playlist.coverArt?[{art:playlist.coverArt},...tracks]:tracks;if(old)old.replaceWith(mpLibraryArtwork(cover,playlist.isAlbum?'fa-solid fa-compact-disc':'fa-solid fa-folder-open',playlist.source==='spotify'?'spotify':''))}});await Promise.all(workers)}
async function mpOpenLibraryCollection(kind){
  if(kind==='queue'){mpOpenSheet('queue');return}
  if(kind==='favorites'){mpShowDetail({type:'FAVORITES',title:'Aki Favorites',subtitle:`${MP.liked.length} saved ${MP.liked.length===1?'track':'tracks'}`,art:MP.liked[0]&&MP.liked[0].art||'',tracks:MP.liked});return}
  if(kind==='downloads'){if(!MP.downloadsLoaded)await mpLoadDownloads();mpShowDetail({type:'OFFLINE',title:'Downloads',subtitle:`${MP.downloads.length} tracks on this device`,art:MP.downloads[0]&&MP.downloads[0].art||'',tracks:MP.downloads});return}
  if(kind!=='youtube')return;
  const localPlaylist=mpYouTubeOfflinePlaylist();
  mpShowDetail({type:'YOUTUBE MUSIC',title:'YouTube Likes',subtitle:MP.online?'Loading liked songs…':'Opening offline likes…',art:'',tracks:[],playlistName:MP_YOUTUBE_LIKES_PLAYLIST,existing:!!localPlaylist});
  const pendingDetail=MP.detail,content=document.getElementById('mp-detail-content');content.replaceChildren(mpLoading(MP.online?'Loading YouTube Likes…':'Opening downloaded YouTube Likes…'));
  if(MP.online)await mpLoadYouTubeLikes(!!MP.youtubeLikesError||MP.youtubeLikesSource==='local');
  else{try{await mpLoadOfflineYouTubeLikes()}catch(error){MP.youtubeLikesError=error.message||'Downloaded YouTube Likes could not be opened'}}
  if(MP.detail!==pendingDetail||MP.view!=='detail')return;
  if(MP.youtubeLikesError&&!MP.youtubeLikes.length){pendingDetail.subtitle=MP.youtubeLikesError;document.getElementById('mp-detail-subtitle').textContent=MP.youtubeLikesError;content.replaceChildren(mpEmpty('fa-brands fa-youtube','YouTube Likes unavailable',MP.youtubeLikesError));return}
  const offline=MP.youtubeLikesSource==='local'||!MP.online,subtitle=offline?`${MP.youtubeLikes.length} downloaded ${MP.youtubeLikes.length===1?'track':'tracks'}`:`${MP.youtubeLikes.length} liked ${MP.youtubeLikes.length===1?'track':'tracks'}`;
  mpShowDetail({type:'YOUTUBE MUSIC',title:'YouTube Likes',subtitle,art:MP.youtubeLikes[0]&&MP.youtubeLikes[0].art||'',tracks:MP.youtubeLikes,playlistName:MP_YOUTUBE_LIKES_PLAYLIST,existing:!!mpYouTubeOfflinePlaylist()})
}
async function mpRenderLibrary(){const content=document.getElementById('mp-library-content'),generation=++MP.libraryGeneration;content.replaceChildren(mpLoading('Gathering your music…'));if(!MP.favoritesLoaded)await mpLoadFavorites();if(!MP.playlistsLoaded)await mpLoadPlaylists();if(generation!==MP.libraryGeneration)return;content.replaceChildren();const localYouTube=mpYouTubeOfflinePlaylist(),collectionGrid=mpEl('div','library-feature-grid'),favoriteCount=MP.liked.length,ytMeta=!MP.online?(localYouTube?`${localYouTube.downloaded||localYouTube.count||0} available offline`:'Connect to view your likes'):MP.youtubeLikesPromise?'Loading…':MP.youtubeLikesError?MP.youtubeLikesError:MP.youtubeLikesLoaded?`${MP.youtubeLikes.length} liked ${MP.youtubeLikes.length===1?'track':'tracks'}`:'Checking YouTube Music…',downloadMeta=MP.downloadsLoaded?`${MP.downloads.length} offline ${MP.downloads.length===1?'track':'tracks'}`:'Checking this device…';collectionGrid.append(mpLibraryCard({kind:'favorites',title:'Aki Favorites',meta:`${favoriteCount} saved ${favoriteCount===1?'track':'tracks'}`,tracks:MP.liked,icon:'fa-solid fa-heart',tone:'favorites',eyebrow:'YOUR LIKES',onClick:()=>mpOpenLibraryCollection('favorites')}),mpLibraryCard({kind:'youtube',title:'YouTube Likes',meta:ytMeta,tracks:MP.youtubeLikes,icon:'fa-brands fa-youtube',tone:'youtube',eyebrow:'YOUTUBE MUSIC',onClick:()=>mpOpenLibraryCollection('youtube')}),mpLibraryCard({kind:'downloads',title:'Downloads',meta:downloadMeta,tracks:MP.downloads,icon:'fa-solid fa-circle-down',tone:'downloads',eyebrow:'ON THIS DEVICE',onClick:()=>mpOpenLibraryCollection('downloads')}));content.append(mpLibraryHeading('COLLECTIONS','Made for your library'),collectionGrid);const queue=mpEl('button','library-queue-card');queue.type='button';queue.dataset.libraryCard='queue';const queueIcon=mpEl('span','library-queue-icon');queueIcon.appendChild(mpEl('i','fa-solid fa-list-ul'));const queueCopy=mpEl('span','library-queue-copy');queueCopy.append(mpEl('strong','',MP.queue.length?'Continue your queue':'Build your queue'),mpEl('span','',MP.queue.length?`${MP.queue.length} tracks · ${MP.queueContext.label}`:'Add tracks while you browse'));queue.append(queueIcon,queueCopy,mpEl('i','fa-solid fa-chevron-right'));queue.addEventListener('click',()=>mpOpenLibraryCollection('queue'));content.appendChild(queue);const create=mpEl('button','library-heading-action');create.type='button';create.innerHTML='<i class="fa-solid fa-plus"></i> New';create.addEventListener('click',()=>mpOpenSheet('playlist'));content.appendChild(mpLibraryHeading('PLAYLISTS','Your collections',create));const playlistGrid=mpEl('div','library-playlist-grid');if(MP.playlists.length)MP.playlists.forEach((playlist,index)=>playlistGrid.appendChild(mpPlaylistCard(playlist,index)));else{const empty=mpEl('button','library-create-card');empty.type='button';empty.append(mpEl('i','fa-solid fa-folder-plus'),mpEl('strong','','Create your first playlist'),mpEl('span','','Save albums or build a mix from any track.'));empty.addEventListener('click',()=>mpOpenSheet('playlist'));playlistGrid.appendChild(empty)}content.appendChild(playlistGrid);mpHydratePlaylistCards(generation);if(!MP.youtubeLikesLoaded&&!MP.youtubeLikesPromise&&MP.online)mpLoadYouTubeLikes().then(()=>{if(MP.view==='library'&&generation===MP.libraryGeneration)mpRenderLibrary()});if(!MP.downloadsLoaded)mpLoadDownloads().then(()=>{if(MP.view==='library'&&generation===MP.libraryGeneration)mpRenderLibrary()})}

async function mpOpenPlaylist(playlist){mpShowDetail({type:playlist.isAlbum?'ALBUM':'PLAYLIST',title:playlist.name,subtitle:`${playlist.count||0} tracks`,art:playlist.coverArt||'',bio:'',tracks:[],playlistName:playlist.name,source:playlist.source||'',albumId:playlist.albumId||'',existing:true});const pendingDetail=MP.detail,content=document.getElementById('mp-detail-content');content.replaceChildren(mpLoading('Loading playlist…'));try{const tracks=await mpGetPlaylistTracks(playlist);if(MP.detail!==pendingDetail||MP.view!=='detail')return;MP.detail.tracks=tracks;mpRenderDetailTracks();if(playlist.source==='spotify'&&tracks.some(track=>!mpArtUrl(track)))mpRepairSpotifyArtwork(playlist,pendingDetail)}catch(error){if(MP.detail===pendingDetail&&MP.view==='detail')content.replaceChildren(mpEmpty('fa-solid fa-triangle-exclamation','Playlist unavailable',error.message))}}
function mpDetailContext(){return{type:String(MP.detail&&MP.detail.type||'collection').toLowerCase(),label:String(MP.detail&&MP.detail.title||'Collection')}}
function mpDetailCanGoOffline(detail=MP.detail){const type=String(detail&&detail.type||'').toUpperCase();return type==='PLAYLIST'||type==='ALBUM'||type==='YOUTUBE MUSIC'}
function mpRenderOfflineButton(){const button=document.getElementById('mp-detail-offline'),detail=MP.detail,tracks=detail&&detail.tracks||[],eligible=mpDetailCanGoOffline(detail);button.hidden=!eligible;button.disabled=!eligible||!tracks.length;if(!eligible)return;const type=String(detail&&detail.type||'').toUpperCase(),name=detail.playlistName||detail.title,status=MP.offlineCollectionName===name?MP.offlineCollectionStatus:null,label=button.querySelector('span');button.classList.remove('busy','ready');if(status&&['queued','downloading','assets'].includes(status.status)){button.disabled=true;button.classList.add('busy');if(status.status==='assets')label.textContent=`Covers & lyrics ${status.assetsProcessed||0}/${status.downloaded||0}`;else label.textContent=`Downloading ${status.processed||0}/${status.total||tracks.length}`;return}const ready=tracks.length&&tracks.every(track=>track.local_audio);button.classList.toggle('ready',!!ready);label.textContent=ready?'Offline ready':type==='YOUTUBE MUSIC'?'Download all':'Offline play'}
function mpRenderDetailTracks(){const content=document.getElementById('mp-detail-content'),tracks=(MP.detail&&MP.detail.tracks||[]).map(mpTrack),type=String(MP.detail&&MP.detail.type||'COLLECTION').toUpperCase();if(MP.detail)MP.detail.tracks=tracks;document.getElementById('mp-detail-play').disabled=!tracks.length;document.getElementById('mp-detail-save').disabled=!tracks.length;mpRenderOfflineButton();content.replaceChildren();if(!tracks.length){const states=type==='FAVORITES'?['fa-solid fa-heart','No favorites yet','Tap the heart on any track to save it here.']:type==='OFFLINE'?['fa-solid fa-circle-down','Nothing downloaded yet','Saved audio will appear here for offline listening.']:['fa-solid fa-folder-open','This collection is empty','Add a track to start this collection.'];content.appendChild(mpEmpty(...states));return}tracks.forEach((track,index)=>content.appendChild(mpTrackRow(track,index,'list',tracks,mpDetailContext())))}
function mpShowDetail(detail){MP.detail={...detail,tracks:(detail.tracks||[]).map(mpTrack)};document.getElementById('mp-detail-type').textContent=detail.type||'COLLECTION';document.getElementById('mp-detail-title').textContent=detail.title||'Details';document.getElementById('mp-detail-subtitle').textContent=detail.subtitle||'';document.getElementById('mp-detail-bio').textContent=detail.bio||'';const art=document.getElementById('mp-detail-art'),url=mpArtUrl({art:detail.art});if(url){art.onerror=()=>{art.style.display='none'};art.src=url;art.style.display='block'}else art.style.display='none';mpRenderDetailTracks();mpSwitchView('detail')}
async function mpOpenAlbum(album){const albumId=album.albumId||album.browseId;if(!albumId)return;mpShowDetail({type:'ALBUM',title:album.title||album.name||'Album',subtitle:album.artist||'',art:album.art||'',tracks:[],albumId,existing:false});const content=document.getElementById('mp-detail-content');content.replaceChildren(mpLoading('Loading album…'));try{const data=await mpApiFetch('/api/album',{albumId});MP.detail={type:'ALBUM',title:data.title,subtitle:data.artist,art:data.art,albumId,tracks:(data.tracks||[]).map(mpTrack),existing:false};mpShowDetail(MP.detail)}catch(error){content.replaceChildren(mpEmpty('fa-solid fa-triangle-exclamation','Album unavailable',error.message))}}
async function mpOpenArtist(artist,browseId=''){if(!artist)return;mpShowDetail({type:'ARTIST',title:artist,subtitle:'Artist',art:'',tracks:[]});const content=document.getElementById('mp-detail-content');content.replaceChildren(mpLoading('Loading artist…'));try{const data=await mpApiFetch('/api/artist',{name:artist,browseId});let art='';try{const image=await mpApiFetch('/api/artist/image',{name:artist});art=image&&image.art||''}catch(_error){}MP.detail={type:'ARTIST',title:artist,subtitle:`${(data.tracks||[]).length} top tracks`,art,bio:data.bio||'',tracks:(data.tracks||[]).map(mpTrack),albums:data.albums||[]};mpShowDetail(MP.detail)}catch(error){content.replaceChildren(mpEmpty('fa-solid fa-triangle-exclamation','Artist unavailable',error.message))}}

function mpSearchFilterLabel(filter){return{all:'All results',track:'Tracks',album:'Albums',artist:'Artists'}[filter]||'All results'}
function mpWarmSearchTracks(tracks,limit=2){const generation=String(++MP.searchWarmGeneration),warm=(Array.isArray(tracks)?tracks:[]).filter(Boolean).slice(0,Math.max(0,Math.min(limit,2)));mpPostPrefetch('stream','mobile-player-search',generation,warm);return generation}
function mpCancelSearchWork(){if(MP.searchAbort)MP.searchAbort.abort();MP.searchAbort=null;mpWarmSearchTracks([],0)}
async function mpSearch(event){
  event.preventDefault();const input=document.getElementById('mp-search-input'),query=input.value.trim(),filter=document.getElementById('mp-search-filter').value,filterLabel=mpSearchFilterLabel(filter),results=document.getElementById('mp-search-results');if(!query)return;
  mpCancelSearchWork();
  if(!MP.online){results.replaceChildren(mpEmpty('fa-solid fa-wifi','Search needs a connection','Your downloaded library is still available.'));return}
  const generation=++MP.searchGeneration;MP.searchAbort=new AbortController();const signal=MP.searchAbort.signal;results.replaceChildren(mpLoading(`Searching for “${query}”…`));document.getElementById('mp-search-caption').textContent=`Searching · ${filterLabel}`;
  try{
    const data=await mpApiFetch('/api/search',{q:query,filter},'GET',15000,{signal});if(generation!==MP.searchGeneration)return;
    const items=Array.isArray(data)?data:[];results.replaceChildren();document.getElementById('mp-search-caption').textContent=`${items.length} ${items.length===1?'result':'results'} · ${filterLabel}`;
    if(!items.length){MP.searchTracks=[];results.appendChild(mpEmpty('fa-solid fa-magnifying-glass','No results found','Try a different spelling or filter.'));return}
    MP.searchTracks=items.filter(item=>filter!=='album'&&filter!=='artist'&&!(item.browseId&&!item.videoId)).map(mpTrack);if(filter==='all'||filter==='track')mpWarmSearchTracks(MP.searchTracks,2);
    let trackIndex=0;items.forEach(item=>{if(filter==='album')results.appendChild(mpDiscoveryRow(item,'album'));else if(filter==='artist'||(item.browseId&&!item.videoId))results.appendChild(mpDiscoveryRow(item,'artist'));else{results.appendChild(mpTrackRow(MP.searchTracks[trackIndex],trackIndex,'list',MP.searchTracks,{type:'search',label:`Search · ${query}`}));trackIndex++}})
  }catch(error){if(generation!==MP.searchGeneration||error&&error.name==='AbortError')return;MP.searchTracks=[];results.replaceChildren(mpEmpty('fa-solid fa-triangle-exclamation','Search failed',error.message));document.getElementById('mp-search-caption').textContent='Try again'}
  finally{if(MP.searchAbort&&MP.searchAbort.signal===signal)MP.searchAbort=null}
}

function mpLyricText(line){return typeof line==='string'?line:String((line&&(line.text??line.words))??'')}
function mpLyricTime(line){return Number(line&&typeof line==='object'?(line.time??line.startTime??line.start??0):0)||0}
async function mpLoadLyrics(force){
  const content=document.getElementById('mp-lyrics-content'),track=MP.queue[MP.queueIdx];
  if(!track){content.replaceChildren(mpEmpty('fa-solid fa-microphone-lines','Nothing playing','Choose a track before opening lyrics.'));return}
  const key=track.tid||`${track.name}|${track.artist}`;
  if(!force&&MP.lyricsKey===key&&MP.lyrics.length){mpSyncLyrics(true);return}
  const generation=++MP.lyricGeneration;content.replaceChildren(mpLoading('Finding the best lyric match…'));
  try{
    const data=await mpApiFetch('/api/lyrics',{title:track.name,artist:track.artist,videoId:track.videoId,album:track.album,duration:track.dur,force:force?1:0},'GET',25000);
    if(generation!==MP.lyricGeneration)return;
    MP.lyrics=Array.isArray(data&&data.lines)?data.lines:[];MP.lyricsSynced=!!(data&&data.synced);MP.activeLyric=-1;MP.lyricsKey=key;content.replaceChildren();
    if(!MP.lyrics.length){content.appendChild(mpEmpty('fa-solid fa-microphone-slash','Lyrics unavailable','Retry to bypass the temporary lyrics cache.'));return}
    const rendered=MP.lyricsSynced?MP.lyrics:MP.lyrics.flatMap(line=>mpLyricText(line).split(/\r?\n/).map(text=>text.trim()).filter(Boolean));
    rendered.forEach((line,index)=>{const node=mpEl('div',`lyrics-line${MP.lyricsSynced?'':' plain'}`,mpLyricText(line));node.dataset.lyricIndex=String(index);if(MP.lyricsSynced){node.style.setProperty('--distance','5');node.addEventListener('click',()=>{MP_AUDIO.currentTime=Math.max(0,mpLyricTime(MP.lyrics[index])-MP.lyricOffset);mpSyncLyrics(true)})}content.appendChild(node)});
    if(MP.lyricsSynced)mpSyncLyrics(true);else content.scrollTop=0;
  }catch(error){if(generation!==MP.lyricGeneration)return;content.replaceChildren(mpEmpty('fa-solid fa-triangle-exclamation','Lyrics could not load',error.message))}
}
function mpSyncLyrics(force=false){
  if(!MP.lyricsSynced||!MP.lyrics.length)return;
  const time=MP_AUDIO.currentTime+MP.lyricOffset;let low=0,high=MP.lyrics.length-1,active=-1;
  while(low<=high){const middle=(low+high)>>1;if(mpLyricTime(MP.lyrics[middle])<=time){active=middle;low=middle+1}else high=middle-1}
  if(active===MP.activeLyric&&!force)return;MP.activeLyric=active;
  const content=document.getElementById('mp-lyrics-content'),rows=content.querySelectorAll('.lyrics-line');
  rows.forEach((row,index)=>{const distance=Math.min(Math.abs(index-active),5);if(row._lyricDistance!==distance){row._lyricDistance=distance;row.style.setProperty('--distance',String(distance))}row.classList.toggle('active',index===active)});
  if(active<0||!rows[active]||document.getElementById('mp-album-lyrics').hidden)return;
  const node=rows[active],top=node.offsetTop-(content.clientHeight/2)+(node.offsetHeight/2),reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;
  content.scrollTo({top:Math.max(0,top),behavior:force||reduced?'auto':'smooth'});
}

async function mpRenderPlaylistPicker(){const list=document.getElementById('mp-playlist-picker');if(!MP.playlists.length)await mpLoadPlaylists();list.replaceChildren();if(!MP.playlists.length){list.appendChild(mpEmpty('fa-solid fa-folder-plus','No playlists yet','Create your first playlist above.'));return}MP.playlists.forEach(playlist=>{const row=mpEl('button','playlist-row');row.type='button';row.append(mpEl('span','row-art','♪'));const copy=mpEl('span','row-copy');copy.append(mpEl('strong','',playlist.name),mpEl('span','',`${playlist.count||0} tracks`));row.append(copy,mpEl('i','fa-solid fa-plus row-meta'));row.addEventListener('click',()=>mpAddCurrentToPlaylist(playlist.name));list.appendChild(row)})}
async function mpCreatePlaylist(name){const clean=String(name||'').trim();if(!clean)return false;try{await mpApiFetch('/api/playlists/create',{name:clean},'POST');await mpLoadPlaylists();mpRenderPlaylistPicker();if(MP.view==='library')mpRenderLibrary();mpToast(`Created ${clean}`);return true}catch(error){mpToast(error.message);return false}}
async function mpSaveTracksAsPlaylist(name,tracks){const clean=String(name||'').trim();if(!clean||!Array.isArray(tracks)||!tracks.length)return false;const created=await mpCreatePlaylist(clean);if(!created)return false;let added=0;for(const track of tracks){try{await mpApiFetch('/api/playlists/add',{playlist:clean,track,download:false},'POST');added++}catch(error){console.warn(`Could not add ${track.name} to ${clean}`,error)}}await mpLoadPlaylists();mpToast(`Saved ${added} tracks to ${clean}`);return added>0}
async function mpAddCurrentToPlaylist(name){const track=MP.queue[MP.queueIdx];if(!track){mpToast('Nothing is playing');return}try{await mpApiFetch('/api/playlists/add',{playlist:name,track,download:false},'POST');await mpLoadPlaylists();mpCloseSheet('playlist');mpToast(`Added to ${name}`)}catch(error){mpToast(error.message)}}

function mpOfflineStatusLabel(status){if(!status)return'Preparing offline play…';if(status.status==='assets')return`Saving covers and lyrics ${status.assetsProcessed||0}/${status.downloaded||0}`;if(status.status==='queued')return'Preparing offline play…';return`Downloading ${status.processed||0}/${status.total||0}${status.current?` · ${status.current}`:''}`}
async function mpFinishOfflineCollection(name,status){clearTimeout(MP.offlineCollectionTimer);MP.offlineCollectionTimer=null;MP.playlistTracks.delete(name);MP.playlistsLoaded=false;MP.downloadsLoaded=false;await Promise.all([mpLoadPlaylists(),mpLoadDownloads(true)]);if(MP.detail&&MP.detail.playlistName===name){try{const tracks=await mpGetPlaylistTracks({name},true);MP.detail.existing=true;MP.detail.tracks=tracks;if(name===MP_YOUTUBE_LIKES_PLAYLIST){MP.youtubeLikes=tracks;MP.youtubeLikesLoaded=true;MP.youtubeLikesSource='local';MP.youtubeLikesError=''}mpRenderDetailTracks()}catch(error){console.warn('Offline collection refresh failed',error)}}await mpReconcileMedia();if(MP.view==='library')mpRenderLibrary();const failed=Number(status.failed)||0;mpToast(failed?`${name} is offline · ${failed} ${failed===1?'track':'tracks'} need retry`:`${name} is ready for offline play`)}
async function mpPollOfflineCollection(name){clearTimeout(MP.offlineCollectionTimer);try{const status=await mpApiFetch('/api/playlists/offline/status',{playlist:name},'GET',15000);if(MP.offlineCollectionName!==name)return;MP.offlineCollectionStatus=status;mpRenderOfflineButton();if(status.status==='complete'){await mpFinishOfflineCollection(name,status);return}if(status.status==='idle'){mpToast('Offline download could not be found');return}MP.offlineCollectionTimer=setTimeout(()=>mpPollOfflineCollection(name),1300)}catch(error){console.warn('Offline collection status failed',error);MP.offlineCollectionTimer=setTimeout(()=>mpPollOfflineCollection(name),2500)}}
async function mpStartDetailOffline(){const detail=MP.detail,tracks=detail&&detail.tracks||[];if(!mpDetailCanGoOffline(detail)||!tracks.length){mpToast('This collection has no tracks to download');return}if(!MP.online&&!tracks.every(track=>track.local_audio)){mpToast('Connect to the internet to save this collection');return}const payload={playlist:detail.playlistName||detail.title,tracks,coverUrl:detail.art||'',existing:!!detail.playlistName&&detail.existing!==false};if(String(detail.type).toUpperCase()==='ALBUM')payload.albumData={albumId:detail.albumId||'',title:detail.title,artist:detail.subtitle||'',art:detail.art||''};try{const status=await mpApiFetch('/api/playlists/offline',payload,'POST',30000);const name=status.playlist||payload.playlist;detail.playlistName=name;detail.existing=true;MP.offlineCollectionName=name;MP.offlineCollectionStatus=status;mpRenderOfflineButton();mpToast(mpOfflineStatusLabel(status));mpPollOfflineCollection(name)}catch(error){mpToast(error.message||'Could not start offline play')}}

function mpSpotifyPlaylistId(url){const importer=window.SpotifyImporter;return importer&&typeof importer.extractPlaylistId==='function'?importer.extractPlaylistId(String(url||'').trim()):null}
function mpSetSpotifyStatus(message,state=''){
  const status=document.getElementById('mp-spotify-status');status.hidden=!message;status.dataset.state=state;
  if(!message){status.replaceChildren();return}
  if(state==='working'){const spinner=mpEl('i','fa-solid fa-circle-notch fa-spin');status.replaceChildren(spinner,document.createTextNode(message));return}
  status.textContent=message;
}
function mpUpdateSpotifyForm(){
  const input=document.getElementById('mp-spotify-url'),submit=document.getElementById('mp-spotify-submit');
  submit.disabled=MP.spotifyImporting||!mpSpotifyPlaylistId(input.value);
  if(!MP.spotifyImporting&&input.value.trim()&&!mpSpotifyPlaylistId(input.value))mpSetSpotifyStatus('Paste a valid open.spotify.com playlist link.','error');
  else if(!MP.spotifyImporting)mpSetSpotifyStatus('');
}
function mpSetSpotifyMode(mode){
  MP.spotifyMode=mode==='likes'?'likes':'playlist';const likes=MP.spotifyMode==='likes';
  document.querySelectorAll('[data-spotify-mode]').forEach(button=>{const active=button.dataset.spotifyMode===MP.spotifyMode;button.classList.toggle('active',active);button.setAttribute('aria-selected',String(active))});
  document.getElementById('mp-spotify-playlist-copy').hidden=likes;document.getElementById('mp-spotify-likes-copy').hidden=!likes;
  document.getElementById('mp-spotify-title').textContent=likes?'Link Spotify Likes':'Import a playlist';
  document.getElementById('mp-spotify-subtitle').textContent=likes?'Turn your saved songs into a local AkiMelody playlist.':'Bring a public Spotify playlist into AkiMelody.';
  document.querySelector('#mp-spotify-submit span').textContent=likes?'Link Likes':'Import Playlist';
  mpUpdateSpotifyForm();
}
function mpOpenSpotify(mode='playlist'){
  if(!MP.online){mpToast('Connect to the internet to import from Spotify');return}
  if(!window.SpotifyImporter){mpToast('Spotify import tools are unavailable in this build');return}
  if(!MP.spotifyImporting){document.getElementById('mp-spotify-url').value='';mpSetSpotifyStatus('')}
  mpSetSpotifyMode(mode);mpOpenSheet('spotify');setTimeout(()=>document.getElementById('mp-spotify-url').focus(),160);
}
async function mpPasteSpotifyLink(){
  const input=document.getElementById('mp-spotify-url'),button=document.getElementById('mp-spotify-paste');
  try{input.value=(await navigator.clipboard.readText()).trim();mpUpdateSpotifyForm();const label=button.querySelector('span'),previous=label.textContent;label.textContent='Pasted';button.classList.add('active');setTimeout(()=>{label.textContent=previous;button.classList.remove('active')},1300)}
  catch(error){console.warn('Spotify clipboard read failed',error);mpSetSpotifyStatus('Clipboard access was unavailable. Press and hold the field to paste.','error')}
}
async function mpResolveSpotifyArtwork(tracks){
  if(!Array.isArray(tracks)||!tracks.length)return tracks||[];
  for(let offset=0;offset<tracks.length;offset+=10){
    const batch=tracks.slice(offset,offset+10),result=await mpApiFetch('/api/artwork/resolve',{tracks:batch},'POST',45000),resolved=result&&Array.isArray(result.tracks)?result.tracks:[];
    resolved.forEach((art,index)=>{if(art&&tracks[offset+index])Object.assign(tracks[offset+index],art)});
    mpSetSpotifyStatus(`Finding album artwork… ${Math.min(offset+batch.length,tracks.length)} of ${tracks.length}`,'working');
  }
  return tracks;
}
async function mpFinishPlaylistArtwork(playlist){
  await mpApiFetch('/api/playlists/enrich_artwork',{playlist},'POST',20000);
  let status=null;
  for(let attempt=0;attempt<120;attempt++){
    await new Promise(resolve=>setTimeout(resolve,750));
    status=await mpApiFetch('/api/playlists/enrich_artwork/status',{playlist},'GET',15000);
    if(!status||status.status==='complete'||status.status==='failed')break;
  }
  MP.playlistTracks.delete(playlist);await mpLoadPlaylists();if(MP.view==='library')mpRenderLibrary();
  return status;
}
async function mpRepairSpotifyArtwork(playlist,detail){
  try{
    mpToast(`Repairing artwork for ${playlist.name}…`);const status=await mpFinishPlaylistArtwork(playlist.name);
    const refreshed=await mpGetPlaylistTracks(playlist);
    if(MP.detail===detail&&MP.view==='detail'){MP.detail.tracks=refreshed;mpRenderDetailTracks()}
    if(status&&status.status==='complete')mpToast(`Artwork refreshed for ${playlist.name}`);
  }catch(error){console.warn('Existing Spotify artwork repair failed',error)}
}
async function mpSubmitSpotify(event){
  event.preventDefault();if(MP.spotifyImporting)return;
  const input=document.getElementById('mp-spotify-url'),submit=document.getElementById('mp-spotify-submit'),url=input.value.trim(),playlistId=mpSpotifyPlaylistId(url),likes=MP.spotifyMode==='likes';
  if(!playlistId){mpSetSpotifyStatus('Paste a valid open.spotify.com playlist link.','error');return}
  if(!MP.online){mpSetSpotifyStatus('Connect to the internet to import this playlist.','error');return}
  const importer=window.SpotifyImporter;if(!importer){mpSetSpotifyStatus('Spotify import tools are unavailable in this build.','error');return}
  MP.spotifyImporting=true;submit.disabled=true;document.querySelector('#mp-spotify-submit span').textContent=likes?'Linking…':'Importing…';mpSetSpotifyStatus('Fetching playlist details…','working');
  try{
    const playlist=await importer.importFromUrl(url),tracks=Array.isArray(playlist.tracks)?playlist.tracks:[];
    mpSetSpotifyStatus(`Finding album artwork… 0 of ${tracks.length}`,'working');
    try{await mpResolveSpotifyArtwork(tracks)}catch(error){console.warn('Spotify artwork pre-resolution failed; playlist enrichment will retry',error)}
    if(likes&&String(playlist.name||'').trim().toLowerCase()!=='likes')throw new Error(`This playlist is named "${playlist.name}". Please rename it to "Likes" in Spotify and try again.`);
    const requestedName=likes?'Spotify Likes':playlist.name;mpSetSpotifyStatus(`Creating ${requestedName}…`,'working');
    const created=await mpApiFetch('/api/playlists/create',{name:requestedName},'POST');const targetName=created.name||requestedName;
    const sideTasks=[];
    if(playlist.coverArt)sideTasks.push(mpApiFetch('/api/playlists/cover',{playlist:targetName,coverUrl:playlist.coverArt},'POST',25000));
    sideTasks.push(mpApiFetch('/api/playlists/metadata',{playlist:targetName,source:'spotify',spotifyPlaylistId:playlistId,description:playlist.description||''},'POST'));
    const decorationResults=await Promise.allSettled(sideTasks);decorationResults.filter(result=>result.status==='rejected').forEach(result=>console.warn('Spotify playlist decoration failed',result.reason));
    mpCloseSheet('spotify');MP.playlistTracks.delete(targetName);await mpLoadPlaylists();if(MP.view==='library')mpRenderLibrary();
    let added=0;mpToast(`Importing ${targetName}: 0 of ${tracks.length}`);
    for(let index=0;index<tracks.length;index++){
      try{await mpApiFetch('/api/playlists/add',{playlist:targetName,track:tracks[index],trackNumber:index+1,download:false},'POST',25000);added++}catch(error){console.warn(`Could not add ${tracks[index].name} to ${targetName}`,error)}
      if((index+1)%5===0||index===tracks.length-1)mpToast(`Importing ${targetName}: ${index+1} of ${tracks.length}`);
    }
    MP.playlistTracks.delete(targetName);await mpLoadPlaylists();if(MP.view==='library')mpRenderLibrary();mpToast(`Finishing artwork for ${targetName}…`);
    let artworkStatus=null;try{artworkStatus=await mpFinishPlaylistArtwork(targetName)}catch(error){console.warn('Spotify artwork enrichment did not finish',error)}
    const artworkNote=artworkStatus&&artworkStatus.status==='complete'?` · ${artworkStatus.resolved||0} covers ready`:'';
    mpToast(`Imported ${targetName} (${added} tracks)${artworkNote} · use Offline play to download`);
  }catch(error){console.error('Spotify import failed',error);mpSetSpotifyStatus(error.message||'Import failed. Please try again.','error')}
  finally{MP.spotifyImporting=false;document.querySelector('#mp-spotify-submit span').textContent=MP.spotifyMode==='likes'?'Link Likes':'Import Playlist';mpUpdateSpotifyForm()}
}

function mpStartSleepTimer(minutes){clearTimeout(MP.sleepTimer);if(!minutes){MP.sleepEndsAt=0;document.getElementById('mp-sleep-label').textContent='Off';return}MP.sleepEndsAt=Date.now()+minutes*60000;document.getElementById('mp-sleep-label').textContent=`${minutes} min`;MP.sleepTimer=setTimeout(()=>{MP_AUDIO.pause();MP.sleepEndsAt=0;document.getElementById('mp-sleep-label').textContent='Off';mpToast('Sleep timer finished')},minutes*60000);mpToast(`Sleep timer set for ${minutes} minutes`)}

function mpAnimateGestureHeart(){
  const heart=document.getElementById('mp-gesture-heart');
  heart.classList.remove('burst');
  requestAnimationFrame(()=>requestAnimationFrame(()=>heart.classList.add('burst')));
  clearTimeout(mpAnimateGestureHeart.timer);mpAnimateGestureHeart.timer=setTimeout(()=>heart.classList.remove('burst'),850);
}

function mpLikeCurrentFromGesture(){
  const track=MP.queue[MP.queueIdx];if(!track||!track.tid)return;
  mpAnimateGestureHeart();
  if(MP.likedIds.has(track.tid)){mpToast('Already in your favorites');return}
  mpToggleLike();
}

function mpPreviousFromGesture(){
  if(MP.queueIdx>0){mpPlay(MP.queueIdx-1,null,{direction:'backward'});return}
  if(MP.repeat===1&&MP.queue.length>1){mpPlay(MP.queue.length-1,null,{direction:'backward'});return}
  mpToast('This is the first song in the queue');
}

function mpOpenCurrentAlbumFromGesture(){
  const track=MP.queue[MP.queueIdx];if(!track)return;
  if(!track.albumId){mpToast('Album details are unavailable for this track');return}
  mpOpenAlbum(track);
}

function mpWireAlbumGestures(){
  const frame=document.getElementById('mp-album-frame'),stage=frame.closest('.player-stage');
  const state={active:false,pointerId:null,startX:0,startY:0,x:0,y:0,holdFired:false,pressTimer:null,holdTimer:null,tapTimer:null,lastTapAt:0,lastTapX:0,lastTapY:0};
  const clearPress=()=>{clearTimeout(state.pressTimer);clearTimeout(state.holdTimer);stage.classList.remove('gesture-holding')};
  const resetVisual=()=>{stage.classList.remove('gesture-dragging','gesture-holding');stage.style.setProperty('--gesture-x','0px');stage.style.setProperty('--gesture-y','0px');stage.style.setProperty('--gesture-tilt','0deg')};
  const currentTrack=()=>MP.view==='player'&&MP.queueIdx>=0&&MP.queue[MP.queueIdx];

  frame.addEventListener('pointerdown',event=>{
    if(!event.isPrimary||event.button!==0||!currentTrack())return;
    state.active=true;state.pointerId=event.pointerId;state.startX=state.x=event.clientX;state.startY=state.y=event.clientY;state.holdFired=false;
    try{frame.setPointerCapture(event.pointerId)}catch(_error){}
    state.pressTimer=setTimeout(()=>{if(state.active)stage.classList.add('gesture-holding')},420);
    state.holdTimer=setTimeout(()=>{if(!state.active)return;state.holdFired=true;clearTimeout(state.tapTimer);state.lastTapAt=0;if(navigator.vibrate)navigator.vibrate(24);mpOpenCurrentAlbumFromGesture()},650);
  });

  frame.addEventListener('pointermove',event=>{
    if(!state.active||event.pointerId!==state.pointerId)return;
    state.x=event.clientX;state.y=event.clientY;const dx=state.x-state.startX,dy=state.y-state.startY;
    if(Math.hypot(dx,dy)>10)clearPress();
    if(Math.hypot(dx,dy)>4){event.preventDefault();stage.classList.add('gesture-dragging');stage.style.setProperty('--gesture-x',`${Math.max(-32,Math.min(32,dx*.28))}px`);stage.style.setProperty('--gesture-y',`${Math.max(-22,Math.min(25,dy*.18))}px`);stage.style.setProperty('--gesture-tilt',`${Math.max(-4,Math.min(4,dx/38))}deg`)}
  },{passive:false});

  const finish=event=>{
    if(!state.active||event.pointerId!==state.pointerId)return;
    state.active=false;clearPress();const dx=(event.clientX??state.x)-state.startX,dy=(event.clientY??state.y)-state.startY,distance=Math.hypot(dx,dy),held=state.holdFired;resetVisual();
    try{frame.releasePointerCapture(event.pointerId)}catch(_error){}
    if(held)return;
    if(Math.abs(dx)>52&&Math.abs(dx)>Math.abs(dy)*1.12){clearTimeout(state.tapTimer);state.lastTapAt=0;if(dx<0)mpNext(true);else mpPreviousFromGesture();return}
    if(dy>52&&dy>Math.abs(dx)*1.12){clearTimeout(state.tapTimer);state.lastTapAt=0;mpOpenSheet('queue');return}
    if(distance>14)return;
    const now=performance.now(),isDouble=now-state.lastTapAt<310&&Math.hypot(event.clientX-state.lastTapX,event.clientY-state.lastTapY)<42;
    if(isDouble){clearTimeout(state.tapTimer);state.lastTapAt=0;mpLikeCurrentFromGesture();return}
    state.lastTapAt=now;state.lastTapX=event.clientX;state.lastTapY=event.clientY;clearTimeout(state.tapTimer);
    state.tapTimer=setTimeout(()=>{state.lastTapAt=0;if(currentTrack())mpSetLyricsOpen(true)},280);
  };
  frame.addEventListener('pointerup',finish);frame.addEventListener('pointercancel',event=>{if(state.active&&event.pointerId===state.pointerId){state.active=false;clearPress();resetVisual()}});
}

function mpWireEvents(){
  mpWireAlbumGestures();
  document.querySelectorAll('[data-view-target]').forEach(button=>button.addEventListener('click',()=>mpSwitchView(button.dataset.viewTarget)));
  document.querySelectorAll('[data-sheet-open]').forEach(button=>button.addEventListener('click',()=>mpOpenSheet(button.dataset.sheetOpen)));
  document.querySelectorAll('[data-sheet-close]').forEach(button=>button.addEventListener('click',()=>mpCloseSheet(button)));
  document.getElementById('mp-nav-reveal').addEventListener('click',()=>mpSetNavVisible(true,true));
  document.querySelector('.aki-nav').addEventListener('touchstart',()=>mpSetNavVisible(true,true),{passive:true});
  document.addEventListener('touchstart',event=>{const touch=event.touches[0];MP.navTouchStart=MP.view==='player'&&touch&&touch.clientY>window.innerHeight-130?touch.clientY:null},{passive:true});
  document.addEventListener('touchend',event=>{const touch=event.changedTouches[0];if(MP.navTouchStart!==null&&touch&&MP.navTouchStart-touch.clientY>34)mpSetNavVisible(true,true);MP.navTouchStart=null},{passive:true});
  document.getElementById('mp-lyrics-toggle').addEventListener('click',()=>mpSetLyricsOpen(document.getElementById('mp-album-lyrics').hidden));document.getElementById('mp-lyrics-close').addEventListener('click',()=>mpSetLyricsOpen(false));
  document.querySelectorAll('[data-library-mode]').forEach(button=>button.addEventListener('click',()=>{const mode=button.dataset.libraryMode;MP.libraryTab=mode;mpSwitchView('library');if(mode==='favorites'||mode==='downloads')mpOpenLibraryCollection(mode)}));
  document.getElementById('mp-search-form').addEventListener('submit',mpSearch);document.querySelectorAll('[data-search-filter]').forEach(button=>button.addEventListener('click',()=>{const filter=button.dataset.searchFilter;document.getElementById('mp-search-filter').value=filter;document.querySelectorAll('[data-search-filter]').forEach(item=>{const active=item===button;item.classList.toggle('active',active);item.setAttribute('aria-checked',String(active))});document.getElementById('mp-search-caption').textContent=mpSearchFilterLabel(filter);if(document.getElementById('mp-search-input').value.trim())document.getElementById('mp-search-form').requestSubmit()}));document.getElementById('mp-search-clear').addEventListener('click',()=>{MP.searchGeneration++;mpCancelSearchWork();MP.searchTracks=[];document.getElementById('mp-search-input').value='';document.getElementById('mp-search-caption').textContent=mpSearchFilterLabel(document.getElementById('mp-search-filter').value);document.getElementById('mp-search-results').replaceChildren(mpEmpty('fa-solid fa-magnifying-glass','Ready to discover','Search tracks, albums, and artists.'))});
  document.getElementById('mp-play').addEventListener('click',mpTogglePlay);document.getElementById('mp-next').addEventListener('click',()=>mpNext(true));document.getElementById('mp-prev').addEventListener('click',mpPrev);document.getElementById('mp-like').addEventListener('click',mpToggleLike);
  document.getElementById('mp-shuffle').addEventListener('click',()=>{MP.shuffle=!MP.shuffle;mpSyncControlState();mpPersist();mpToast(MP.shuffle?'Shuffle on':'Shuffle off')});document.getElementById('mp-repeat').addEventListener('click',()=>{MP.repeat=(MP.repeat+1)%3;mpSyncControlState();mpPersist();mpToast(['Repeat off','Repeat all','Repeat one'][MP.repeat])});document.getElementById('mp-radio').addEventListener('click',()=>{MP.radioMode=!MP.radioMode;mpSyncControlState();mpPersist();mpToast(MP.radioMode?'Radio on':'Radio off')});
  document.getElementById('mp-seek').addEventListener('input',event=>{if(MP_AUDIO.duration)MP_AUDIO.currentTime=(Number(event.target.value)/1000)*MP_AUDIO.duration});document.getElementById('mp-artist').addEventListener('click',()=>{const track=MP.queue[MP.queueIdx];if(track)mpOpenArtist(track.artist)});document.getElementById('mp-album').addEventListener('click',()=>{const track=MP.queue[MP.queueIdx];if(track&&track.albumId)mpOpenAlbum(track)});
  document.getElementById('mp-detail-back').addEventListener('click',()=>mpSwitchView(MP.previousView==='detail'?'library':MP.previousView));document.getElementById('mp-detail-play').addEventListener('click',()=>{if(MP.detail&&MP.detail.tracks&&MP.detail.tracks.length)mpPlayFromSource(MP.detail.tracks,0,mpDetailContext())});document.getElementById('mp-detail-save').addEventListener('click',()=>{if(MP.detail&&MP.detail.tracks&&MP.detail.tracks.length)mpSaveTracksAsPlaylist(MP.detail.title,MP.detail.tracks);else mpToast('This collection has no tracks to save')});document.getElementById('mp-detail-offline').addEventListener('click',mpStartDetailOffline);
  document.getElementById('mp-clear-queue').addEventListener('click',()=>{MP.queue=[];MP.queueIdx=-1;MP.queueContext={type:'manual',label:'Queue',radioExtended:false};MP.savedTrackKey='';MP.savedPosition=0;MP.currentSourceKey='';mpResetQueuePrefetch();MP_AUDIO.pause();MP_AUDIO.removeAttribute('src');mpPersist();mpRenderQueue();mpToast('Queue cleared')});document.getElementById('mp-save-queue').addEventListener('click',()=>{if(!MP.queue.length){mpToast('Queue is empty');return}const name=prompt('Playlist name');if(name)mpSaveTracksAsPlaylist(name,MP.queue)});
  document.getElementById('mp-create-playlist').addEventListener('click',()=>mpOpenSheet('playlist'));document.getElementById('mp-playlist-create-form').addEventListener('submit',event=>{event.preventDefault();const input=document.getElementById('mp-playlist-name');mpCreatePlaylist(input.value);input.value=''});
  document.querySelectorAll('[data-spotify-open]').forEach(button=>button.addEventListener('click',()=>mpOpenSpotify(button.dataset.spotifyOpen)));
  document.querySelectorAll('[data-spotify-mode]').forEach(button=>button.addEventListener('click',()=>{if(!MP.spotifyImporting)mpSetSpotifyMode(button.dataset.spotifyMode)}));
  document.getElementById('mp-spotify-form').addEventListener('submit',mpSubmitSpotify);document.getElementById('mp-spotify-url').addEventListener('input',mpUpdateSpotifyForm);document.getElementById('mp-spotify-paste').addEventListener('click',mpPasteSpotifyLink);
  document.getElementById('mp-lyrics-retry').addEventListener('click',()=>mpLoadLyrics(true));document.getElementById('mp-lyrics-offset').addEventListener('input',event=>{MP.lyricOffset=Number(event.target.value);document.getElementById('mp-lyrics-offset-value').textContent=`${MP.lyricOffset>=0?'+':''}${MP.lyricOffset.toFixed(2)}s`;mpPersist()});
  document.getElementById('mp-volume').addEventListener('input',event=>{MP_AUDIO.volume=Number(event.target.value)/100;mpPersist()});document.getElementById('mp-dynamic-color').addEventListener('change',event=>{MP.dynamicColor=event.target.checked;mpPersist();if(MP.dynamicColor)MobilePalette.extract(document.getElementById('mp-art'));else MobilePalette.reset()});document.getElementById('mp-theme').addEventListener('change',event=>{document.body.dataset.theme=event.target.checked?'light':'dark';localStorage.setItem('akimelody_theme',event.target.checked?'light':'dark')});document.getElementById('mp-community').addEventListener('change',async()=>{try{await mpApiFetch('/api/settings/toggle_community_showcase',{},'POST')}catch(error){mpToast(error.message)}});
  document.querySelectorAll('[data-space-action]').forEach(button=>button.addEventListener('click',()=>{const action=button.dataset.spaceAction;if(action==='sleep'){const value=prompt('Sleep timer minutes (0 to disable)','30');if(value!==null)mpStartSleepTimer(Math.max(0,Number(value)||0))}else mpToast(`${button.querySelector('strong').textContent} is not available in this mobile build yet`)}));
  document.getElementById('mp-auth').addEventListener('click',mpOpenYouTubeAuth);
  document.getElementById('mp-youtube-cookie-start').addEventListener('click',mpStartYouTubeCookieLogin);
  document.getElementById('mp-youtube-advanced').addEventListener('click',()=>mpShowYouTubeConfig(true));
  document.getElementById('mp-youtube-config').addEventListener('submit',event=>{event.preventDefault();mpStartYouTubeOAuth({client_id:document.getElementById('mp-youtube-client-id').value.trim(),client_secret:document.getElementById('mp-youtube-client-secret').value.trim()})});
  document.getElementById('mp-youtube-code').addEventListener('click',mpCopyYouTubeCode);
  document.getElementById('mp-youtube-open').addEventListener('click',()=>{if(!MP.youtubeAuthUrl)return;if(MP.isAndroid)window.location.href=MP.youtubeAuthUrl;else window.open(MP.youtubeAuthUrl,'_blank','noopener,noreferrer')});
  document.getElementById('mp-clear-data').addEventListener('click',()=>mpToast('Data clearing requires confirmation and will be added with the maintenance screen'));
  window.addEventListener('online',()=>{MP.online=true;mpSyncConnection()});window.addEventListener('offline',()=>{MP.online=false;mpSyncConnection()});window.addEventListener('popstate',event=>mpSwitchView(event.state&&event.state.view||'player',false));window.addEventListener('keydown',event=>{if(event.key==='Escape'&&!document.getElementById('mp-album-lyrics').hidden)mpSetLyricsOpen(false)});
  window.addEventListener('pagehide',()=>mpRememberPlaybackPosition(true));
  document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='hidden')mpRememberPlaybackPosition(true)});
}

MP_AUDIO.addEventListener('play',()=>{MP.playing=true;mpSyncControlState();const track=MP.queue[MP.queueIdx],key=track&&(track.tid||`${track.name}|${track.artist}`);if(!document.getElementById('mp-album-lyrics').hidden&&key&&MP.lyricsKey!==key)mpLoadLyrics(false)});
MP_AUDIO.addEventListener('playing',()=>setTimeout(mpPrefetchNext,250));
MP_AUDIO.addEventListener('pause',()=>{MP.playing=false;mpRememberPlaybackPosition(true);mpSyncControlState()});
MP_AUDIO.addEventListener('timeupdate',()=>{if(MP_AUDIO.duration){document.getElementById('mp-seek').value=String(Math.round(MP_AUDIO.currentTime/MP_AUDIO.duration*1000));document.getElementById('mp-current-time').textContent=mpFmt(MP_AUDIO.currentTime);document.getElementById('mp-total-time').textContent=mpFmt(MP_AUDIO.duration);mpRememberPlaybackPosition(false);mpSyncLyrics();mpNativeMediaUpdate();if(MP_AUDIO.currentTime>3&&MP_AUDIO.duration-MP_AUDIO.currentTime<15)mpPrefetchNext()}});
MP_AUDIO.addEventListener('ended',()=>{MP.savedPosition=0;mpPersist();if(MP.repeat===2)mpPlay(MP.queueIdx);else mpNext(true)});
MP_AUDIO.addEventListener('error',()=>{if(MP.playStarting)return;const failure=mpMediaFailure(new Error('Active audio stream was interrupted'));MP.playing=false;MP.currentSource=null;MP.currentSourceKey='';MP_AUDIO.removeAttribute('src');MP_AUDIO.load();mpSyncControlState();document.getElementById('mp-source').textContent='Playback interrupted';mpToast(MP.online?mpPlaybackMessage(failure):'Track unavailable offline')});

async function mpBoot(){mpRestore();try{MP.history=JSON.parse(localStorage.getItem('akimelody_mobile_history')||'[]').map(mpTrack)}catch(_error){MP.history=[]}mpWireEvents();mpSyncConnection();mpSyncControlState();document.getElementById('mp-volume').value=String(Math.round(MP_AUDIO.volume*100));document.getElementById('mp-lyrics-offset').value=String(MP.lyricOffset);document.getElementById('mp-lyrics-offset-value').textContent=`${MP.lyricOffset>=0?'+':''}${MP.lyricOffset.toFixed(2)}s`;document.getElementById('mp-dynamic-color').checked=MP.dynamicColor;const theme=localStorage.getItem('akimelody_theme')||'dark';document.body.dataset.theme=theme;document.getElementById('mp-theme').checked=theme==='light';if(MP.queueIdx>=0)mpSyncTrackUI();else{MobilePalette.reset();mpRenderQueue()}await Promise.all([mpLoadFavorites(),mpLoadPlaylists()]);mpRenderHome();const requested=location.hash.replace('#',''),initial=document.querySelector(`[data-view="${requested}"]`)?requested:MP.view;mpSwitchView(initial,false);history.replaceState({view:MP.view},'',`#${MP.view}`);document.getElementById('mp-search-results').appendChild(mpEmpty('fa-solid fa-magnifying-glass','Ready to discover','Search tracks, albums, and artists.'))}
document.addEventListener('DOMContentLoaded',mpBoot);
