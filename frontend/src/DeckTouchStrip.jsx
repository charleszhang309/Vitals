import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { Mic, MicOff, Pause, Play, SkipForward, Volume2, VolumeX } from 'lucide-react';
import { MediaCover } from './ControlPage.jsx';
import { postJson, request } from './api.js';
import { t } from './i18n/messages.js';
import { IconButton } from './ui.jsx';

const faderInterval = 120;

export function useDeckFaders(target, snapshot, setHostAudio, refreshDevices, reportError) {
  const [optimistic, setOptimistic] = useState(null);
  const queueRef = useRef(null);

  useLayoutEffect(() => {
    setOptimistic(null);
    if (!target) return undefined;
    const controller = new AbortController();
    const queue = { target, controller, pending: {}, writing: false };
    queueRef.current = queue;
    return () => {
      controller.abort();
      clearTimeout(queue.timer);
      clearTimeout(queue.reconcile);
      queueRef.current = null;
    };
  }, [target]);

  useEffect(() => {
    const queue = queueRef.current;
    if (!queue?.reconcile || queue.writing || Object.keys(queue.pending).length) return;
    setOptimistic((previous) => {
      if (previous?.target !== target) return previous;
      return Object.entries(previous.values).every(([key, value]) => snapshot?.[key] === value) ? null : previous;
    });
  }, [snapshot, target]);

  async function flush() {
    const queue = queueRef.current;
    if (!queue || queue.target !== target) return;
    clearTimeout(queue.timer);
    queue.timer = null;
    if (queue.writing || !Object.keys(queue.pending).length) return;
    const change = queue.pending;
    queue.pending = {};
    queue.writing = true;
    const { signal } = queue.controller;
    try {
      const actual = await request(`/api/deck/audio?target=${encodeURIComponent(target)}`, {
        method: 'POST', signal, timeoutMs: 27_000,
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(change),
      });
      if (signal.aborted) return;
      if (target === 'hub') setHostAudio(actual);
      else refreshDevices();
    } catch (error) {
      if (!signal.aborted) {
        reportError(error.message);
        setOptimistic({ target, values: { ...queue.pending } });
        if (target !== 'hub') refreshDevices();
      }
    } finally {
      queue.writing = false;
      if (!signal.aborted) {
        if (Object.keys(queue.pending).length) queue.timer = setTimeout(flush, faderInterval);
        else if (target === 'hub') setOptimistic(null);
        else queue.reconcile = setTimeout(() => setOptimistic(null), 3500);
      }
    }
  }

  function change(field, value, immediate = false) {
    const queue = queueRef.current;
    if (!queue || queue.target !== target) return;
    clearTimeout(queue.reconcile);
    queue.pending[field] = value;
    setOptimistic((previous) => ({ target, values: {
      ...(previous?.target === target ? previous.values : {}), [field]: value,
    } }));
    if (immediate) flush();
    else if (!queue.timer && !queue.writing) queue.timer = setTimeout(flush, faderInterval);
  }

  const values = optimistic?.target === target ? { ...snapshot, ...optimistic.values } : snapshot;
  return { values, change, flush };
}

function Fader({ audio, input = false, disabled }) {
  const volumeField = input ? 'input_percent' : 'percent';
  const muteField = input ? 'input_muted' : 'muted';
  const percent = audio.values?.[volumeField];
  const muted = audio.values?.[muteField];
  const canAdjust = Number.isFinite(percent) && (input || audio.values?.available !== false);
  const canMute = typeof muted === 'boolean' && (input || audio.values?.available !== false);
  const level = canAdjust ? Math.round(percent) : 0;
  const label = t(input ? '麦克风' : '扬声器');
  const muteLabel = input ? t(muted ? '开启麦克风' : '关闭麦克风') : t(muted ? '取消静音' : '静音');
  const Icon = input ? (muted ? MicOff : Mic) : (muted ? VolumeX : Volume2);
  return <div className="deck-strip-module deck-fader" role="group" aria-label={label}>
    <span className="deck-strip-label">{label}</span>
    <div className="deck-fader-controls">
      <IconButton label={muteLabel} aria-pressed={canMute ? muted : undefined} disabled={disabled || !canMute}
        onClick={() => audio.change(muteField, !muted, true)}><Icon size={16} aria-hidden="true" /></IconButton>
      <input type="range" min="0" max="100" aria-label={label} disabled={disabled || !canAdjust} value={level}
        style={{ '--deck-level': `${level}%` }} onChange={(event) => audio.change(volumeField, Number(event.target.value))}
        onPointerUp={audio.flush} onPointerCancel={audio.flush} onBlur={audio.flush} />
      <output className="numeric">{canAdjust ? `${level}%` : '—'}</output>
    </div>
  </div>;
}

export default function DeckTouchStrip({ deviceId, track, faders, onMediaChange, reportError, disabled }) {
  const [pending, setPending] = useState(false);
  const playing = track?.status === 'playing';
  const action = track?.actions?.toggle ? 'toggle' : playing ? 'pause' : 'play';
  const PlayIcon = playing ? Pause : Play;
  const activeTrack = track && track.status !== 'stopped';
  const canToggle = activeTrack && (deviceId !== 'hub' || track.actions?.[action]);
  const canSkip = activeTrack && (deviceId !== 'hub' || track.actions?.next);

  async function control(operation) {
    if (disabled || pending) return;
    setPending(true);
    try {
      if (deviceId === 'hub') {
        await postJson('/api/media/action', { action: operation, session_id: track.session_id });
      } else {
        await postJson(`/api/agents/${encodeURIComponent(deviceId)}/command`, {
          action: operation === 'next' ? 'media_next' : 'media_play_pause', payload: {},
        });
      }
    } catch (error) {
      reportError(error.message);
    } finally {
      onMediaChange();
      setPending(false);
    }
  }

  return <div className="surface deck-touch-strip" role="group" aria-label={t('触控条')}>
    <Fader audio={faders} disabled={disabled} />
    <Fader audio={faders} input disabled={disabled} />
    <div className="deck-strip-module deck-strip-media">
      <MediaCover compact track={track} />
      <div className="deck-strip-track">
        <strong title={track?.title}>{activeTrack ? track.title || t('未命名媒体') : t('当前未在播放媒体')}</strong>
        {activeTrack && track.artist && <small title={track.artist}>{track.artist}</small>}
      </div>
      <div className="deck-strip-transport">
        <IconButton label={t(playing ? '暂停' : '播放')} disabled={disabled || !canToggle || pending}
          onClick={() => control(action)}><PlayIcon size={16} aria-hidden="true" /></IconButton>
        <IconButton label={t('下一首')} disabled={disabled || !canSkip || pending}
          onClick={() => control('next')}><SkipForward size={16} aria-hidden="true" /></IconButton>
      </div>
    </div>
  </div>;
}
