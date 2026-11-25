import { useEffect, useRef, useState } from 'react';
import { request, sendJson } from './api.js';

const path = '/api/ai/settings';

export function useAiSettings() {
  const [settings, setSettings] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const revision = useRef(0);
  const saving = useRef(false);

  useEffect(() => {
    const controller = new AbortController();
    async function refresh() {
      if (saving.current || document.hidden) return;
      const current = ++revision.current;
      try {
        const result = await request(path, { signal: controller.signal });
        if (!controller.signal.aborted && current === revision.current) { setSettings(result); setError(''); }
      } catch (failure) {
        if (!controller.signal.aborted && current === revision.current) setError(failure.message);
      }
    }
    refresh();
    const timer = setInterval(refresh, 30_000);
    document.addEventListener('visibilitychange', refresh);
    return () => {
      controller.abort();
      clearInterval(timer);
      document.removeEventListener('visibilitychange', refresh);
    };
  }, []);

  async function update(changes) {
    if (saving.current) return;
    saving.current = true;
    revision.current++;
    setBusy(true);
    setError('');
    try {
      const result = await sendJson('PATCH', path, changes);
      setSettings(result);
      return result;
    } catch (failure) {
      setError(failure.message);
    } finally {
      saving.current = false;
      setBusy(false);
    }
  }

  return { settings, busy, error, update };
}
