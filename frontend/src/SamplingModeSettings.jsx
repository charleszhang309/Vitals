import { t } from './i18n/messages.js';
import { useEffect, useState } from 'react';
import { request, sendJson } from './api.js';
import { Segmented } from './ui.jsx';

const path = '/api/system/sampling';
const modes = [
  { value: 'smart', get label() { return t('智能'); } },
  { value: 'realtime', get label() { return t('实时'); } },
  { value: 'eco', get label() { return t('节能'); } },
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
      <h3 id="sampling-mode-heading">{t('采样模式')}</h3>
      <Segmented id="sampling-mode" label={t('采样模式')} options={modes} value={mode} onChange={save} disabled={!mode || busy} />
    </div>
    {error && <p className="feedback error" role="alert">{error}</p>}
  </section>;
}
