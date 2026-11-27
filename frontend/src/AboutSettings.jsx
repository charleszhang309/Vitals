import { t } from './i18n/messages.js';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Download, ExternalLink, RefreshCw } from 'lucide-react';
import { version as frontendVersion } from '../package.json';
import { httpUrl, request } from './api.js';
import { Button } from './ui.jsx';

const REPOSITORY = 'https://github.com/charleszhang309/Vitals';
const CHECK_STORAGE = `vitals-release-check-${frontendVersion}`;
const DAY = 24 * 60 * 60 * 1000;

function readCheck() {
  try {
    const saved = JSON.parse(localStorage.getItem(CHECK_STORAGE));
    if (Number.isFinite(saved?.at)) return saved;
  } catch {
    // Browsers can restrict preference storage.
  }
  return { at: 0, result: null };
}

function writeCheck(value) {
  try {
    localStorage.setItem(CHECK_STORAGE, JSON.stringify(value));
  } catch {
    // The in-memory check record still throttles this page.
  }
}

export function useReleaseUpdates(enabled) {
  const [initial] = useState(readCheck);
  const [result, setResult] = useState(initial.result);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const record = useRef(initial);
  const pending = useRef(false);
  const check = useCallback(async (automatic = false) => {
    if (pending.current) return;
    const stored = readCheck();
    if (stored.at > record.current.at) {
      record.current = stored;
      setResult(stored.result);
    }
    const now = Date.now();
    if (automatic && now - record.current.at < DAY && now >= record.current.at) return;
    const started = { ...record.current, at: now };
    record.current = started;
    writeCheck(started);
    pending.current = true;
    setBusy(true);
    setError('');
    try {
      const release = await request('/api/system/update/check');
      const completed = { at: now, result: release };
      record.current = completed;
      if (readCheck().at <= now) writeCheck(completed);
      setResult(release);
    } catch (failure) {
      if (!automatic) setError(failure.message);
    } finally {
      pending.current = false;
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    if (!enabled) return undefined;
    const automatic = () => { if (!document.hidden) check(true); };
    automatic();
    const timer = window.setInterval(automatic, 60 * 60 * 1000);
    document.addEventListener('visibilitychange', automatic);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', automatic);
    };
  }, [check, enabled]);

  return { result, busy, error, check };
}

export function AboutSettings({ updates, enabled, onToggle }) {
  const [identity, setIdentity] = useState(null);
  useEffect(() => {
    const controller = new AbortController();
    request('/api/system/version', { signal: controller.signal }).then(setIdentity).catch(() => {});
    return () => controller.abort();
  }, []);
  const { result, busy, error, check } = updates;
  const currentVersion = identity?.version || frontendVersion;
  const release = result?.current_version === currentVersion ? result : null;
  const platform = { windows: 'Windows', macos: 'macOS', linux: 'Linux' }[identity?.platform];

  return <div className="settings-fields about-settings">
    <div className="about-app">
      <img src={httpUrl('/icon-192.png').href} alt="" width="54" height="54" />
      <div><h3>Vitals</h3>
        <p className="muted">v{currentVersion}{platform ? ` · ${platform}` : ''}{identity?.desktop ? t(' · 桌面版') : ''}</p>
      </div>
    </div>
    <a className="button about-repository" href={REPOSITORY} target="_blank" rel="noopener noreferrer">
      <ExternalLink size={16} />{t('GitHub 仓库')}</a>
    <section className="settings-subsection about-updates">
      <div className="settings-row">
        <h3>{t('更新')}</h3>
        <Button onClick={() => check()} disabled={busy}><RefreshCw size={16} />{busy ? t('正在查找…') : t('查找更新')}</Button>
      </div>
      <label className="settings-row"><span>{t('自动检查更新')}</span>
        <input type="checkbox" role="switch" className="toggle-switch" checked={enabled}
          onChange={(event) => onToggle(event.target.checked)} />
      </label>
      {error && <p className="feedback error" role="alert">{error}</p>}
      {release?.has_update && <div className="about-release" role="status">
        <strong>{t('发现新版本 v{p1}', { p1: release.latest_version })}</strong>
        {release.notes && <p className="about-notes">{release.notes}</p>}
        <a className="button" href={release.download_url} target="_blank" rel="noopener noreferrer">
          <Download size={16} />{release.download_url === release.release_url ? t('查看发布页') : t('下载安装包')}
        </a>
      </div>}
      {!error && release && !release.has_update && <p className="muted" role="status">
        {release.latest_version ? t('已是最新版本') : t('GitHub 暂无公开发布版本')}
      </p>}
    </section>
  </div>;
}
