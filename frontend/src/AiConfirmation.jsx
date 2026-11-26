import { useEffect, useRef, useState } from 'react';
import { postJson, request } from './api.js';
import { Button } from './ui.jsx';

export function useAiConfirmations() {
  const [states, setStates] = useState({});
  const inFlight = useRef(new Set());

  async function resolve(confirmation, confirm) {
    const { id } = confirmation;
    if (inFlight.current.has(id) || states[id]) return;
    inFlight.current.add(id);
    const path = '/api/ai/confirm/' + encodeURIComponent(id);
    setStates((previous) => ({ ...previous, [id]: { busy: true } }));
    let state;
    try {
      if (confirm) {
        const result = await postJson(path, {});
        state = { message: result.message };
      } else {
        await request(path, { method: 'DELETE' });
        state = { message: '已取消' };
      }
    } catch (failure) {
      state = { message: failure.message, error: true };
    } finally {
      inFlight.current.delete(id);
    }
    setStates((previous) => ({ ...previous, [id]: state }));
  }

  return { states, resolve };
}

export default function AiConfirmation({ confirmation, state, onResolve }) {
  const [expired, setExpired] = useState(() => Date.now() >= Date.parse(confirmation.expires_at));
  useEffect(() => {
    const delay = Math.max(0, Date.parse(confirmation.expires_at) - Date.now());
    const timer = window.setTimeout(() => setExpired(true), delay);
    return () => window.clearTimeout(timer);
  }, [confirmation.expires_at]);

  return <div className="ai-confirmation" aria-label="待确认的操作">
    <strong>{confirmation.label}</strong>
    {state?.message || expired && !state?.busy ? <span className={state?.error ? 'feedback error' : 'muted'} role="status">
      {state?.message ?? '已过期'}
    </span> : <div className="ai-confirmation-actions">
      <Button disabled={state?.busy} onClick={() => onResolve(confirmation, false)}>取消</Button>
      <Button disabled={state?.busy} className="primary-button" onClick={() => onResolve(confirmation, true)}>确认执行</Button>
    </div>}
  </div>;
}
