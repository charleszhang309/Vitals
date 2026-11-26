import { useCallback, useEffect, useState } from 'react';
import { request } from './api.js';

export default function useOverviewResource(path, interval, enabled = true) {
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  const [version, refresh] = useState(0);
  useEffect(() => {
    if (!enabled) return undefined;
    const controller = new AbortController();
    let pending = false;
    async function load() {
      if (pending || document.hidden) return;
      pending = true;
      try {
        const result = await request(path, { signal: controller.signal });
        if (!controller.signal.aborted) { setData(result); setError(''); }
      } catch (failure) {
        if (!controller.signal.aborted) setError(failure.message);
      } finally {
        pending = false;
      }
    }
    load();
    const timer = window.setInterval(load, interval);
    document.addEventListener('visibilitychange', load);
    return () => {
      controller.abort();
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', load);
    };
  }, [path, interval, version, enabled]);

  const replace = useCallback((value) => {
    setData(value);
    setError('');
    refresh((current) => current + 1);
  }, []);
  return { data, setData, error, replace, retry: () => refresh((value) => value + 1) };
}
