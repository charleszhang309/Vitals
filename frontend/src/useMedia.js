import { useEffect, useState } from 'react';
import { httpUrl, request } from './api.js';

export function useMedia(enabled) {
  const [reading, setReading] = useState(null);
  const [selectedId, select] = useState(null);
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    if (!enabled) return undefined;
    const controller = new AbortController();
    let pending = false;
    async function load() {
      if (pending || document.hidden) return;
      pending = true;
      try {
        const state = await request('/api/media/state', { signal: controller.signal });
        if (!controller.signal.aborted) setReading({ state, received: performance.now() });
      } catch {
        if (!controller.signal.aborted) setReading(null);
      } finally {
        pending = false;
      }
    }
    load();
    const timer = window.setInterval(load, 1800);
    document.addEventListener('visibilitychange', load);
    return () => {
      controller.abort();
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', load);
    };
  }, [enabled, revision]);

  const state = reading?.state;
  const sessions = state?.sessions ?? [];
  const current = sessions.find((item) => item.session_id === state?.current_session_id) ?? null;
  const track = sessions.find((item) => item.session_id === selectedId) ?? current;
  return { sessions, track, current, reason: state?.reason, received: reading?.received ?? 0, select,
    reload: () => setRevision((value) => value + 1) };
}

export function artworkUrl(track) {
  const query = new URLSearchParams({ session_id: track.session_id, version: track.artwork_version });
  return httpUrl(`/api/media/artwork?${query}`).href;
}
