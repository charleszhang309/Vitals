import { lazy, Suspense, useEffect, useMemo, useRef, useState } from 'react';
import { Maximize2, MessageSquareQuote, Music2, Volume2, VolumeX } from 'lucide-react';
import { postJson, request } from './api.js';
import { IconButton, Modal, ProgressBar } from './ui.jsx';
import { artworkUrl } from './useMedia.js';

export const playbackLabels = { playing: '播放中', paused: '已暂停', stopped: '已停止' };
const volumePath = '/api/media/volume';
const AmllLyrics = lazy(() => import('./AmllLyrics.jsx'));

export function MediaCover({ track, compact = false }) {
  const [failedVersion, setFailedVersion] = useState(null);
  const available = track?.artwork_version && failedVersion !== track.artwork_version;
  return <span className={`media-cover${compact ? ' media-cover-small' : ''}`} aria-hidden="true">
    {available ? <img key={track.artwork_version} src={artworkUrl(track)} alt=""
      onError={() => setFailedVersion(track.artwork_version)} /> : <Music2 size={compact ? 18 : 35} />}
  </span>;
}

function clockText(seconds) {
  if (!Number.isFinite(seconds)) return '—';
  const whole = Math.max(0, Math.floor(seconds));
  const minutes = Math.floor(whole / 60);
  return `${minutes}:${String(whole % 60).padStart(2, '0')}`;
}

function usePosition(track, received) {
  const [now, setNow] = useState(() => performance.now());
  const playing = track?.status === 'playing';
  useEffect(() => {
    if (!playing) return undefined;
    const timer = window.setInterval(() => setNow(performance.now()), 140);
    return () => window.clearInterval(timer);
  }, [playing]);
  if (!track) return 0;
  const elapsed = playing ? Math.max(0, now - received) / 1000 * (track.playback_rate || 1) : 0;
  return Math.max(0, Math.min(track.duration_seconds ?? Infinity, track.position_seconds + elapsed));
}

const emptyPlayback = {
  desktop_unavailable: '等待 Windows 桌面会话',
  media_unavailable: 'Windows 媒体会话暂不可用',
};

function PlayerCard({ media, onOpen }) {
  const { track } = media;
  return <section className="surface panel detail-tile player-card" aria-labelledby="player-heading">
    <div className="panel-header"><h2 id="player-heading">正在播放</h2></div>
    <div className="player-track"><MediaCover track={track} />
      <div className="player-details">
        {track ? <>
          <h3>{track.title || '未命名媒体'}</h3>
          {track.artist && <p>{track.artist}</p>}
          <span className="muted media-playback-status">{[track.player, playbackLabels[track.status]].filter(Boolean).join(' · ')}</span>
        </> : <p className="section-empty">{emptyPlayback[media.reason] ?? '当前未在播放媒体'}</p>}
      </div>
    </div>
    <button className="detail-link" type="button" aria-label="展开正在播放" aria-haspopup="dialog" onClick={onOpen}>
      <Maximize2 size={17} aria-hidden="true" />
    </button>
  </section>;
}

function useTrackLyrics(track) {
  const [reply, setReply] = useState(null);
  const sessionId = track?.session_id;
  const trackId = track?.track_id;
  const lookupKey = sessionId && trackId && track.title?.trim() && track.artist?.trim() ? `${sessionId}:${trackId}` : '';

  useEffect(() => {
    if (!lookupKey) return undefined;
    const controller = new AbortController();
    const params = new URLSearchParams({ session_id: sessionId, track_id: trackId });
    request(`/api/media/lyrics?${params}`, { signal: controller.signal }).then((data) => {
      if (!controller.signal.aborted) setReply({ key: lookupKey, data });
    }).catch(() => {
      if (!controller.signal.aborted) setReply({ key: lookupKey, data: null });
    });
    return () => controller.abort();
  }, [lookupKey, sessionId, trackId]);

  return reply?.key === lookupKey ? reply.data : null;
}

function LyricsPanel({ lyrics, track, received }) {
  const timeline = useMemo(() => {
    if (lyrics?.status !== 'synced') return [];
    if (lyrics.amll_lines?.length) return lyrics.amll_lines;
    // 部署时也能读取旧接口的逐行响应。
    return (lyrics.lines ?? []).flatMap((line, index, lines) => {
      if (!line.text || !Number.isFinite(line.time)) return [];
      const startTime = Math.round(line.time * 1000);
      const nextTime = Math.round((lines[index + 1]?.time ?? line.time + 4) * 1000);
      const endTime = Math.max(startTime + 1, Math.min(nextTime, startTime + 5000));
      return [{ startTime, endTime, words: [{ startTime, endTime, word: line.text }],
        translatedLyric: '', romanLyric: '', isBG: false, isDuet: false }];
    });
  }, [lyrics]);

  return <section className="now-lyrics" id="now-playing-lyrics" aria-label="歌词">
    {timeline.length ? <Suspense fallback={<p className="now-lyrics-empty" role="status">正在加载歌词…</p>}>
      <AmllLyrics lines={timeline} track={track} received={received} />
    </Suspense> : <p className="lyrics-scroll lyrics-plain" tabIndex={0}>{lyrics.plain}</p>}
  </section>;
}

function useSystemVolume() {
  const [volume, setVolume] = useState(null);
  const [draft, setDraft] = useState(null);
  const [feedback, setFeedback] = useState('');
  const pending = useRef(null);
  const writing = useRef(false);
  const revision = useRef(0);
  const timer = useRef(null);
  const mounted = useRef(false);
  useEffect(() => {
    mounted.current = true;
    const controller = new AbortController();
    let reading = false;
    async function poll() {
      if (reading || writing.current || pending.current || document.hidden) return;
      reading = true;
      const started = revision.current;
      try {
        const actual = await request(volumePath, { signal: controller.signal });
        if (!controller.signal.aborted && started === revision.current) setVolume(actual);
      } catch {
        if (!controller.signal.aborted && started === revision.current) setVolume(null);
      } finally {
        reading = false;
      }
    }
    poll();
    const interval = window.setInterval(poll, 1900);
    document.addEventListener('visibilitychange', poll);
    return () => {
      mounted.current = false;
      controller.abort();
      window.clearInterval(interval);
      window.clearTimeout(timer.current);
      timer.current = null;
      document.removeEventListener('visibilitychange', poll);
    };
  }, []);

  async function flush() {
    window.clearTimeout(timer.current);
    timer.current = null;
    if (!mounted.current || writing.current || !pending.current) return;
    const change = pending.current;
    pending.current = null;
    writing.current = true;
    try {
      const actual = await postJson(volumePath, change);
      if (mounted.current) setVolume(actual);
    } catch (error) {
      if (mounted.current) setFeedback(error.message);
    } finally {
      writing.current = false;
      if (mounted.current && pending.current) timer.current = window.setTimeout(flush, 140);
      else if (mounted.current) setDraft(null);
    }
  }

  function queue(change, immediate = false) {
    revision.current += 1;
    const changingDevice = change.device_id != null;
    setDraft((previous) => changingDevice ? change : { ...previous, ...change });
    setFeedback('');
    pending.current = changingDevice ? change : { ...pending.current, ...change };
    if (immediate) flush();
    else if (!timer.current && !writing.current) timer.current = window.setTimeout(flush, 140);
  }

  const percent = draft?.percent ?? volume?.percent ?? 0;
  const muted = draft?.muted ?? volume?.muted ?? false;
  const output = draft?.device_id ?? volume?.device_id ?? '';
  const switching = draft?.device_id != null;
  const devices = volume?.devices ?? [];
  return { available: volume?.available, percent, muted, output, switching, devices, feedback, queue, flush };
}

function PlaybackGlyph({ kind }) {
  const shape = {
    play: 'M6 3.7a1 1 0 0 1 1.5-.87l14 8.3a1 1 0 0 1 0 1.74l-14 8.3A1 1 0 0 1 6 20.3z',
    pause: 'M5.5 4h4A1.5 1.5 0 0 1 11 5.5v13A1.5 1.5 0 0 1 9.5 20h-4A1.5 1.5 0 0 1 4 18.5v-13A1.5 1.5 0 0 1 5.5 4ZM14.5 4h4A1.5 1.5 0 0 1 20 5.5v13a1.5 1.5 0 0 1-1.5 1.5h-4a1.5 1.5 0 0 1-1.5-1.5v-13A1.5 1.5 0 0 1 14.5 4Z',
    next: 'M1 5a1 1 0 0 1 1.6-.8L12 11V5a1 1 0 0 1 1.6-.8l9 7a1 1 0 0 1 0 1.6l-9 7A1 1 0 0 1 12 19v-6l-9.4 6.8A1 1 0 0 1 1 19z',
    previous: 'M23 5a1 1 0 0 0-1.6-.8L12 11V5a1 1 0 0 0-1.6-.8l-9 7a1 1 0 0 0 0 1.6l9 7A1 1 0 0 0 12 19v-6l9.4 6.8A1 1 0 0 0 23 19z',
    speaker: 'M3 9h3l4-3.6a1 1 0 0 1 1.7.74v11.72a1 1 0 0 1-1.7.74L6 15H3a1 1 0 0 1-1-1v-4a1 1 0 0 1 1-1z',
  };
  const volume = kind === 'volume' || kind === 'muted';
  return <svg className={volume ? 'now-volume-glyph' : 'now-playback-glyph'} viewBox="0 0 24 24"
    fill="currentColor" aria-hidden="true" focusable="false">
    <path d={shape[volume ? 'speaker' : kind]} />
    {volume && <path d={kind === 'muted' ? 'm15.5 9 6 6m0-6-6 6' : 'M15 9a4 4 0 0 1 0 6m3-8a7 7 0 0 1 0 10'}
      fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />}
  </svg>;
}

function VolumeControls({ sound, immersive = false }) {
  const { available, percent, muted, switching, queue, flush } = sound;
  const disabled = !available || switching;
  return <div className="volume-controls">
    <IconButton label={muted ? '取消静音' : '静音'} disabled={disabled} onClick={() => queue({ muted: !muted }, true)}>
      {immersive ? <PlaybackGlyph kind={muted ? 'muted' : 'volume'} /> : muted ? <VolumeX size={21} /> : <Volume2 size={21} />}
    </IconButton>
    <input className="media-range" type="range" min="0" max="100" aria-label="系统主音量" aria-valuetext={`${percent}%`}
      disabled={disabled} value={percent} style={{ '--range-fill': `${percent}%` }}
      onChange={(event) => queue({ percent: Number(event.target.value) })} onPointerUp={flush} onBlur={flush} />
  </div>;
}

function VolumeCard({ sound }) {
  const { available, percent, output, switching, devices, feedback, queue } = sound;
  return <section className="surface panel volume-card" aria-labelledby="volume-heading">
    <div className="panel-header"><h2 id="volume-heading">系统音量</h2>
      <span className="numeric volume-readout">{available ? `${percent}%` : '—'}</span></div>
    <VolumeControls sound={sound} />
    {devices.length > 0 && <label className="volume-device"><span>输出设备</span>
      <select value={output} disabled={switching} onChange={(event) => queue({ device_id: event.target.value }, true)}>
        {!devices.some((device) => device.id === output) && <option value="" disabled>选择输出设备</option>}
        {devices.map((device) => <option key={device.id} value={device.id}>{device.name}</option>)}
      </select>
    </label>}
    {!available && <p className="muted subtle-line">等待 Windows 音频设备</p>}
    {feedback && <p className="feedback error" role="alert">{feedback}</p>}
  </section>;
}

function NowPlaying({ media, sound, lyricsPreferred, onLyrics, onClose }) {
  const { track, sessions } = media;
  const lyrics = useTrackLyrics(track);
  const hasLyrics = Boolean(lyrics?.amll_lines?.length || lyrics?.lines?.some((line) => line.text) || lyrics?.plain?.trim());
  const showLyrics = lyricsPreferred && hasLyrics;
  const position = usePosition(track, media.received);
  const [pending, setPending] = useState(false);
  const [feedback, setFeedback] = useState('');
  const playing = track?.status === 'playing';
  const action = track?.actions.toggle ? 'toggle' : playing ? 'pause' : 'play';
  const duration = track?.duration_seconds;
  const progress = duration > 0 ? position / duration * 100 : null;

  async function send(operation) {
    if (pending || !track?.actions[operation]) return;
    setPending(true);
    setFeedback('');
    try {
      await postJson('/api/media/action', { action: operation, session_id: track.session_id });
    } catch (error) {
      setFeedback(error.message);
    } finally {
      media.reload();
      setPending(false);
    }
  }

  const sessionSelector = sessions.length > 1 && <select className="now-session" aria-label="切换播放器"
    value={track?.session_id ?? ''} disabled={pending} onChange={(event) => {
      setFeedback('');
      media.select(event.target.value);
    }}>
    {sessions.map((item, index) => <option key={item.session_id} value={item.session_id}>
      {sessions.some((other) => other.session_id !== item.session_id && other.player === item.player)
        ? `${item.player} ${index + 1}` : item.player}
    </option>)}
  </select>;

  return <Modal id="now-playing" title="正在播放" className="now-playing-dialog" onClose={onClose} headerActions={sessionSelector}>
    {track?.artwork_version && <div className="now-ambient" aria-hidden="true"
      style={{ backgroundImage: `url("${artworkUrl(track)}")` }} />}
    <div className="now-stage" data-lyrics={showLyrics}>
      <div className="now-player">
        <div className="now-identity">
          <MediaCover track={track} />
          <div className="now-track">
            <h3>{track ? track.title || '未命名媒体' : emptyPlayback[media.reason] ?? '当前未在播放媒体'}</h3>
            {track?.artist && <p>{track.artist}</p>}
          </div>
        </div>
        {track && <div className="now-progress">
          <ProgressBar value={progress} tone="blue" label="播放进度" />
          <div className="player-times numeric">
            <span>{clockText(position)}</span><span>{clockText(duration)}</span>
          </div>
        </div>}
        <div className="now-transport">
          {track && <div className="player-actions">
            <IconButton label="上一首" disabled={pending || !track.actions.previous} onClick={() => send('previous')}><PlaybackGlyph kind="previous" /></IconButton>
            <IconButton className="player-primary" label={playing ? '暂停' : '播放'} disabled={pending || !track.actions[action]}
              onClick={() => send(action)}><PlaybackGlyph kind={playing ? 'pause' : 'play'} /></IconButton>
            <IconButton label="下一首" disabled={pending || !track.actions.next} onClick={() => send('next')}><PlaybackGlyph kind="next" /></IconButton>
          </div>}
          <VolumeControls sound={sound} immersive />
        </div>
        {(feedback || sound.feedback) && <p className="now-feedback" role="alert">{feedback || sound.feedback}</p>}
      </div>
      <div className="now-lyrics-slot">
        {showLyrics && <LyricsPanel key={`${track.session_id}:${track.track_id}`} lyrics={lyrics} track={track} received={media.received} />}
      </div>
    </div>
    <div className="now-footer">
      {hasLyrics && <IconButton className="now-lyrics-toggle" label="歌词" aria-pressed={showLyrics}
        aria-controls={showLyrics ? 'now-playing-lyrics' : undefined} onClick={() => onLyrics(!showLyrics)}>
        <MessageSquareQuote size={23} />
      </IconButton>}
    </div>
  </Modal>;
}

export default function ControlPage({ media, open, onOpenChange }) {
  const sound = useSystemVolume();
  const [lyricsPreferred, setLyricsPreferred] = useState(true);
  return <>
    <div className="page-heading"><h1>控制</h1></div>
    <div className="media-control-grid">
      <PlayerCard media={media} onOpen={() => onOpenChange(true)} />
      <VolumeCard sound={sound} />
    </div>
    {open && <NowPlaying media={media} sound={sound} lyricsPreferred={lyricsPreferred} onLyrics={setLyricsPreferred} onClose={() => onOpenChange(false)} />}
  </>;
}
