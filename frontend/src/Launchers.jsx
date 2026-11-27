import { useState } from 'react';
import { AppWindow, ExternalLink } from 'lucide-react';
import { httpUrl, request } from './api.js';
import { Button } from './ui.jsx';
import useResource from './useOverviewResource.js';
import './launchers.css';

const base = '/api/control/launchers';

export default function Launchers() {
  const { data } = useResource(base, 15_000);
  const [busy, setBusy] = useState('');
  const [feedback, setFeedback] = useState(null);
  const entries = data?.items ?? [];
  if (!entries.length) return null;

  async function launch(entry) {
    setBusy(entry.id);
    setFeedback(null);
    try {
      await request(`${base}/${encodeURIComponent(entry.id)}`, { method: 'POST' });
      setFeedback({ message: `已请求启动 ${entry.name}` });
    } catch (error) {
      setFeedback({ message: error.message, failed: true });
    } finally {
      setBusy('');
    }
  }

  return <section className="surface panel launcher-card" aria-labelledby="launcher-heading">
    <div className="panel-header"><h2 id="launcher-heading">快捷启动</h2></div>
    <div className="launcher-actions">
      {entries.map((entry) => entry.kind === 'url'
        ? <a key={entry.id} className="button"
          href={window.__TAURI__?.core?.invoke ? httpUrl(`${base}/${encodeURIComponent(entry.id)}/open`).href : entry.url}
          target="_blank" rel="noopener noreferrer">
          <ExternalLink size={17} aria-hidden="true" /><span>{entry.name}</span>
        </a>
        : <Button key={entry.id} disabled={Boolean(busy)} onClick={() => launch(entry)}>
          <AppWindow size={17} aria-hidden="true" /><span>{busy === entry.id ? '正在启动…' : entry.name}</span>
        </Button>)}
    </div>
    {feedback && <p className={`feedback${feedback.failed ? ' error' : ''}`} role={feedback.failed ? 'alert' : 'status'}>
      {feedback.message}
    </p>}
  </section>;
}
