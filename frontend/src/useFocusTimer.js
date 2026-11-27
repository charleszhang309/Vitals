import { t } from './i18n/messages.js';
import { useEffect, useRef, useState } from 'react';
import { postJson } from './api.js';

const storageKey = 'vitals-focus-timer';
export const presets = [15, 30, 45];
export const validFocusMinutes = (minutes) => Number.isInteger(minutes) && minutes >= 1 && minutes <= 180;
const initial = (minutes = 30) => ({ minutes, remaining: minutes * 60_000, deadline: null, id: null });

function restore() {
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey));
    if (validFocusMinutes(saved?.minutes) && Number.isFinite(saved.remaining) && saved.remaining >= 0
      && saved.remaining <= saved.minutes * 60_000 && (saved.deadline === null || Number.isFinite(saved.deadline))
      && (saved.deadline === null || /^[a-f0-9-]{36}$/i.test(saved.id))) return saved;
  } catch { /* 当前会话内仍可使用。 */ }
  return initial();
}

function sessionId() {
  if (crypto.randomUUID) return crypto.randomUUID();
  const parts = Array.from(crypto.getRandomValues(new Uint8Array(16)));
  parts[6] = (parts[6] & 15) | 64;
  parts[8] = (parts[8] & 63) | 128;
  const id = parts.map((part) => part.toString(16).padStart(2, '0')).join('');
  return [id.slice(0, 8), id.slice(8, 12), id.slice(12, 16), id.slice(16, 20), id.slice(20)].join('-');
}

export function focusClock(milliseconds) {
  const seconds = Math.max(0, Math.ceil(milliseconds / 1000));
  return `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
}

export function useFocusTimer(enabled = true) {
  const [timer, setTimer] = useState(restore);
  const [now, setNow] = useState(Date.now);
  const [notice, setNotice] = useState('');
  const [record, setRecord] = useState(null);
  const writing = useRef(false);
  const { deadline } = timer;
  const remaining = deadline ? Math.max(0, deadline - now) : timer.remaining;
  const finishing = Boolean(deadline && remaining === 0);

  useEffect(() => {
    try { localStorage.setItem(storageKey, JSON.stringify(timer)); } catch { /* 可继续计时。 */ }
  }, [timer]);

  useEffect(() => {
    if (!enabled || !deadline) return undefined;
    const tick = () => setNow(Date.now());
    tick();
    const interval = window.setInterval(tick, 250);
    document.addEventListener('visibilitychange', tick);
    return () => { window.clearInterval(interval); document.removeEventListener('visibilitychange', tick); };
  }, [enabled, deadline]);

  useEffect(() => {
    if (!enabled || !finishing) return undefined;
    const { id, minutes, deadline: completed } = timer;
    let active = true;
    async function submit() {
      if (writing.current) return;
      writing.current = true;
      try {
        const stats = await postJson('/api/focus', { session_id: id, duration_minutes: minutes,
          completed_at: new Date(completed).toISOString() });
        if (active) {
          setRecord(stats);
          setNotice(t('专注完成，已计入今日统计。'));
          setTimer(initial(minutes));
        }
      } catch {
        if (active) setNotice(t('专注完成，等待同步记录。'));
      } finally {
        writing.current = false;
      }
    }
    submit();
    const interval = window.setInterval(submit, 10_000);
    window.addEventListener('online', submit);
    return () => { active = false; window.clearInterval(interval); window.removeEventListener('online', submit); };
  }, [enabled, finishing, timer]);

  function reset(minutes = timer.minutes) {
    if (!validFocusMinutes(minutes)) return;
    setNotice('');
    setTimer(initial(minutes));
  }
  function toggle() {
    const current = Date.now();
    setNow(current);
    setNotice('');
    if (deadline) {
      if (deadline > current) setTimer({ ...timer, remaining: deadline - current, deadline: null });
    } else {
      setTimer({ ...timer, deadline: current + timer.remaining, id: timer.id ?? sessionId() });
    }
  }
  return { minutes: timer.minutes, running: Boolean(deadline) && !finishing, finishing, remaining,
    paused: !deadline && timer.id !== null, notice, record, reset, toggle };
}
