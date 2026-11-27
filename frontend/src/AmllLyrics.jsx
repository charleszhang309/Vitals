import { t } from './i18n/messages.js';
import { useEffect, useState } from 'react';
import { useReducedMotion } from 'framer-motion';
import { LyricPlayer } from '@applemusic-like-lyrics/react';
import '@applemusic-like-lyrics/core/style.css';

export default function AmllLyrics({ lines, track, received }) {
  const playing = track.status === 'playing';
  const base = Math.round(track.position_seconds * 1000);
  const duration = track.duration_seconds == null ? Infinity : track.duration_seconds * 1000;
  const speed = track.playback_rate || 1;
  const reduced = useReducedMotion();
  const [time, setTime] = useState(base);

  useEffect(() => {
    let frame;
    function update(now) {
      const elapsed = playing ? Math.max(0, now - received) * speed : 0;
      setTime(Math.max(0, Math.round(Math.min(duration, base + elapsed))));
      if (playing) frame = requestAnimationFrame(update);
    }
    update(performance.now());
    return () => cancelAnimationFrame(frame);
  }, [base, duration, playing, received, speed]);

  return <div className="now-amll" role="group" aria-label={t('同步歌词')}>
    <LyricPlayer
      lyricLines={lines}
      currentTime={time}
      playing={playing && !reduced}
      alignPosition={0.47}
      enableSpring={!reduced}
      enableBlur={!reduced}
      enableScale={!reduced}
    />
  </div>;
}
