import { useEffect, useState } from 'react';
import { request, sendJson } from './api.js';
import { Segmented } from './ui.jsx';

const path = '/api/system/sampling';
const modes = [
  { value: 'realtime', label: '实时' },
  { value: 'smart', label: '智能' },
  { value: 'eco', label: '节能' },
];

export default function SamplingModeSettings() {
  const [mode, setMode] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    const controller = new AbortController();
    request(path, { signal: controller.signal }).then((result) => {
      if (!controller.signal.aborted) setMode(result.mode);
    }).catch((failure) => {
      if (!controller.signal.aborted) setError(failure.message);
    });
    return () => controller.abort();
  }, []);

  async function save(value) {
    setBusy(true);
    setError('');
    try {
      const result = await sendJson('PUT', path, { mode: value });
      setMode(result.mode);
    } catch (failure) {
      setError(failure.message);
    } finally {
      setBusy(false);
    }
  }

  return <section className="settings-subsection" aria-labelledby="sampling-mode-heading">
    <div className="settings-row">
      <h3 id="sampling-mode-heading">采样模式</h3>
      <Segmented id="sampling-mode" label="采样模式" options={modes} value={mode} onChange={save} disabled={!mode || busy} />
    </div>
    <p className="muted">实时每 1 秒采样，节能每 5 秒采样。智能模式在有人查看时每 1 秒采样，其他时间每 5 秒采样。</p>
    {!mode && !error && <p className="muted" role="status">正在读取采样设置…</p>}
    {error && <p className="feedback error" role="alert">{error}</p>}
  </section>;
}
