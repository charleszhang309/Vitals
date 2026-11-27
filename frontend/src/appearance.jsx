import { getLocale, t } from './i18n/messages.js';
import { lazy, Suspense, useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { AnimatePresence } from 'framer-motion';
import { Activity, ArrowDown, ArrowUp, Download, ImagePlus, Inbox, Info, LayoutGrid, Palette, RotateCcw, ServerCog, Sparkles, Upload } from 'lucide-react';
import { httpUrl, request, sendJson } from './api.js';
import { Button, IconButton, Modal, Segmented } from './ui.jsx';
import { defaultCardOrder, orderedCards, overviewModules, overviewStrips, readCardOrder, readCardWidths } from './overviewCards.js';
import { clockStyles } from './Clock.jsx';
import { DeviceDot, HUB_DEVICE_ID } from './RemoteDevice.jsx';
import { AboutSettings, useReleaseUpdates } from './AboutSettings.jsx';
import SamplingModeSettings from './SamplingModeSettings.jsx';
import { useI18n } from './i18n/index.jsx';
const HostSetup = lazy(() => import('./HostSetup.jsx'));

const STORAGE_KEY = 'pcc-appearance';
const defaults = { theme: 'light', material: 'glass', opacity: 46, strength: 60, clockStyle: 'flip', widgets: {},
  cardOrder: defaultCardOrder, cardWidths: {}, defaultDevice: HUB_DEVICE_ID, autoUpdateCheck: true };
const level = (value, fallback) => Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : fallback;
const themes = [{ value: 'light', get label() { return t('浅色'); } }, { value: 'dark', get label() { return t('深色'); } }, { value: 'system', get label() { return t('跟随系统'); } }];
const systemColorScheme = '(prefers-color-scheme: dark)';
const materials = [{ value: 'glass', get label() { return t('液态玻璃'); } }, { value: 'acrylic', get label() { return t('亚克力'); } }];
const baseCategories = [
  { id: 'appearance', get label() { return t('外观与壁纸'); }, Icon: Palette },
  { id: 'overview', get label() { return t('今日概览'); }, Icon: LayoutGrid },
  { id: 'scratchpad', get label() { return t('中转站'); }, Icon: Inbox },
  { id: 'ai', get label() { return t('AI 助手'); }, Icon: Sparkles },
  { id: 'system', get label() { return t('系统状态'); }, Icon: Activity },
];

const desktopModes = [
  { value: 'hub', get label() { return t('本机主机'); } },
  { value: 'remote', get label() { return t('远程客户端'); } },
];

function DesktopModeSettings() {
  const invoke = window.__TAURI__?.core?.invoke;
  const [saved, setSaved] = useState(null);
  const [mode, setMode] = useState('hub');
  const [remoteUrl, setRemoteUrl] = useState('');
  const [token, setToken] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (typeof invoke !== 'function') return undefined;
    let active = true;
    invoke('get_desktop_mode').then((settings) => {
      if (!active) return;
      setSaved(settings);
      setMode(settings.mode);
      setRemoteUrl(settings.remote_url);
    }).catch(() => {
      // 旧版桌面应用与无权限的页面不显示桌面设置。
    });
    return () => { active = false; };
  }, [invoke]);

  if (!saved) return null;

  let sameServer = false;
  try {
    sameServer = new URL(remoteUrl.trim()).href === saved.remote_url;
  } catch {
    // 输入未完成时等待表单校验。
  }
  const keepToken = sameServer && saved.has_token;
  const unchanged = mode === saved.mode && (mode === 'hub' || (sameServer && !token.trim()));

  async function save(event) {
    event.preventDefault();
    setBusy(true);
    setError('');
    try {
      const settings = await invoke('set_desktop_mode', { mode, remote_url: remoteUrl.trim(), token });
      setSaved(settings);
      setRemoteUrl(settings.remote_url);
      setToken('');
    } catch (failure) {
      setError(typeof failure === 'string' ? failure : t('暂时无法保存桌面运行模式。'));
    } finally {
      setBusy(false);
    }
  }

  return <form className="settings-subsection settings-fields" onSubmit={save} aria-labelledby="desktop-mode-title">
    <div className="settings-row">
      <strong id="desktop-mode-title">{t('桌面运行模式')}</strong>
      <Segmented id="desktop-mode" label={t('桌面运行模式')} options={desktopModes} value={mode}
        onChange={setMode} disabled={busy} />
    </div>
    {mode === 'remote' && <div className="host-setup-grid">
      <label className="host-setup-field">
        <span>{t('远程主中心 URL')}</span>
        <input type="url" inputMode="url" value={remoteUrl} required disabled={busy}
          placeholder="https://your-device.your-tailnet.ts.net" onChange={(event) => setRemoteUrl(event.target.value)} />
      </label>
      <label className="host-setup-field">
        <span>{t('配对 Token')}</span>
        <input type="password" autoComplete="new-password" value={token} required={!keepToken} disabled={busy}
          placeholder={keepToken ? t('已配置，留空保留') : t('输入远程主中心的配对 Token')}
          onChange={(event) => setToken(event.target.value)} />
      </label>
    </div>}
    {error && <p className="feedback error" role="alert">{error}</p>}
    <div><Button type="submit" disabled={busy || unchanged}>{busy ? t('正在切换…') : t('保存并切换')}</Button></div>
  </form>;
}

function DesktopLaunchAtLogin() {
  const { t } = useI18n();
  const invoke = window.__TAURI__?.core?.invoke;
  const [enabled, setEnabled] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const changing = useRef(false);

  useEffect(() => {
    if (typeof invoke !== 'function') return;
    let active = true;
    function refresh() {
      if (changing.current) return;
      invoke('get_launch_at_login').then((value) => {
        if (active && !changing.current) setEnabled(value);
      }).catch(() => {});
    }
    refresh();
    window.addEventListener('focus', refresh);
    return () => {
      active = false;
      window.removeEventListener('focus', refresh);
    };
  }, [invoke]);

  async function change(next) {
    changing.current = true;
    setBusy(true);
    setError('');
    try {
      setEnabled(await invoke('set_launch_at_login', { enabled: next }));
    } catch {
      setError('无法更新开机自启动。');
    } finally {
      changing.current = false;
      setBusy(false);
    }
  }

  if (enabled === null) return null;
  return <div className="settings-subsection">
    <label className="settings-row"><span>{t('开机自启动')}</span>
      <input type="checkbox" role="switch" checked={enabled} disabled={busy} onChange={(event) => change(event.target.checked)} />
    </label>
    {error && <p className="feedback error" role="alert">{t(error)}</p>}
  </div>;
}

function LanguageSettings() {
  const { language, setLanguage, t } = useI18n();
  const options = [{ value: 'auto', label: t('language.auto') },
    { value: 'zh-CN', label: '简体中文' }, { value: 'en-US', label: 'English' }];
  return <div className="settings-row">
    <span>{t('language.title')}</span>
    <Segmented id="language-preference" label={t('language.title')} value={language} onChange={setLanguage} options={options} />
  </div>;
}

function initialPreferences() {
  try {
    const stored = JSON.parse(localStorage.getItem(STORAGE_KEY));
    return {
      theme: themes.some(({ value }) => value === stored?.theme) ? stored.theme : defaults.theme,
      material: stored?.material === 'acrylic' ? 'acrylic' : 'glass',
      opacity: level(stored?.opacity, defaults.opacity),
      strength: level(stored?.strength, defaults.strength),
      clockStyle: clockStyles.some(({ value }) => value === stored?.clockStyle) ? stored.clockStyle : defaults.clockStyle,
      widgets: Object.fromEntries(overviewModules.map(({ id }) => [id, stored?.widgets?.[id] !== false])),
      cardOrder: readCardOrder(stored?.cardOrder),
      cardWidths: readCardWidths(stored?.cardWidths),
      defaultDevice: typeof stored?.defaultDevice === 'string' && /^[a-z0-9][a-z0-9._-]{0,63}$/i.test(stored.defaultDevice)
        ? stored.defaultDevice : defaults.defaultDevice,
      autoUpdateCheck: stored?.autoUpdateCheck !== false,
    };
  } catch {
    return defaults;
  }
}

export function useAppearance(immersiveView) {
  const [preferences, setPreferences] = useState(initialPreferences);
  const [systemDark, setSystemDark] = useState(() => window.matchMedia(systemColorScheme).matches);
  const [wallpaper, setWallpaper] = useState(null);
  const [busy, setBusy] = useState(false);
  const [appearanceError, setAppearanceError] = useState('');
  const changing = useRef(false);
  const revision = useRef(0);
  const theme = preferences.theme === 'system' ? (systemDark ? 'dark' : 'light') : preferences.theme;
  const updates = useReleaseUpdates(preferences.autoUpdateCheck);

  useLayoutEffect(() => {
    if (preferences.theme !== 'system') return undefined;
    const media = window.matchMedia(systemColorScheme);
    const sync = ({ matches }) => setSystemDark(matches);
    sync(media);
    if (media.addEventListener) {
      media.addEventListener('change', sync);
      return () => media.removeEventListener('change', sync);
    }
    media.addListener(sync);
    return () => media.removeListener(sync);
  }, [preferences.theme]);

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(preferences));
    } catch {
      // 在浏览器限制存储时，设置仍可在当前页面使用。
    }
  }, [preferences]);

  useLayoutEffect(() => {
    const root = document.documentElement;
    const themeColor = document.querySelector('meta[name="theme-color"]');
    const pageColor = theme === 'dark' ? '#000000' : '#f5f5f7';
    root.dataset.theme = theme;
    if (immersiveView) root.dataset.immersiveOpen = immersiveView;
    else root.removeAttribute('data-immersive-open');
    if (themeColor) themeColor.content = immersiveView === 'player' ? '#111318' : pageColor;
    return () => {
      root.removeAttribute('data-immersive-open');
      if (themeColor) themeColor.content = pageColor;
    };
  }, [immersiveView, theme]);

  useEffect(() => {
    const controller = new AbortController();
    async function refresh() {
      if (changing.current || document.hidden) return;
      const sequence = ++revision.current;
      try {
        const result = await request('/api/wallpaper', { signal: controller.signal });
        if (revision.current === sequence && !controller.signal.aborted) setWallpaper(result.version);
      } catch {
        // 网络恢复后通过下一次刷新同步，暂时保留已加载的壁纸。
      }
    }
    refresh();
    const timer = window.setInterval(refresh, 12_000);
    window.addEventListener('focus', refresh);
    document.addEventListener('visibilitychange', refresh);
    return () => {
      controller.abort();
      window.clearInterval(timer);
      window.removeEventListener('focus', refresh);
      document.removeEventListener('visibilitychange', refresh);
    };
  }, []);

  const update = useCallback((key, value) => {
    setPreferences((previous) => ({ ...previous, [key]: value }));
  }, []);

  async function changeWallpaper(file) {
    if (changing.current) return;
    revision.current += 1;
    changing.current = true;
    setBusy(true);
    setAppearanceError('');
    try {
      if (file) {
        if (file.size > 10 * 1024 * 1024) throw new Error(t('壁纸图片上限为 10 MiB。'));
        const result = await request('/api/wallpaper', { method: 'PUT', body: file });
        setWallpaper(result.version);
      } else {
        await request('/api/wallpaper', { method: 'DELETE' });
        setWallpaper(null);
        setPreferences((current) => ({
          ...defaults, widgets: current.widgets, clockStyle: current.clockStyle, defaultDevice: current.defaultDevice,
          cardOrder: current.cardOrder, cardWidths: current.cardWidths,
          autoUpdateCheck: current.autoUpdateCheck,
        }));
      }
    } catch (error) {
      setAppearanceError(error.message);
    } finally {
      changing.current = false;
      setBusy(false);
    }
  }

  function moveCard(id, offset) {
    setPreferences((current) => {
      const index = current.cardOrder.indexOf(id);
      const target = index + offset;
      if (index < 0 || target < 0 || target >= current.cardOrder.length) return current;
      const cardOrder = [...current.cardOrder];
      [cardOrder[index], cardOrder[target]] = [cardOrder[target], cardOrder[index]];
      return { ...current, cardOrder };
    });
  }

  function resetCardLayout() {
    setPreferences((current) => ({ ...current, cardOrder: defaults.cardOrder, cardWidths: defaults.cardWidths }));
  }

  return { preferences, theme, wallpaper, busy, appearanceError, update, changeWallpaper, updates, moveCard, resetCardLayout };
}

export function AppearanceFrame({ appearance, children }) {
  const { preferences, wallpaper } = appearance;
  const { strength, material, opacity } = preferences;
  const acrylic = material === 'acrylic';
  const properties = {
    '--backdrop-blur': `${(acrylic ? 34 : 40) * strength / 100}px`,
    '--backdrop-saturation': `${100 + (acrylic ? 80 : 400 / 3) * strength / 100}%`,
    '--overlay-opacity': opacity / 100,
    '--glass-alpha': (acrylic ? 0.76 : 0.65) + strength / 500,
  };
  const image = wallpaper ? httpUrl(`/api/wallpaper/image?v=${encodeURIComponent(wallpaper)}`).href : null;
  return (
    <div className={`app-frame${image ? ' has-wallpaper' : ''}`} style={properties}>
      {image && <div className="wallpaper" aria-hidden="true"><img key={image} src={image} alt="" /></div>}
      <div className="app-layer">{children}</div>
    </div>
  );
}

function Slider({ label, value, onChange }) {
  return (
    <label className="range-setting">
      <span className="flex justify-between gap-2"><span>{label}</span><span className="muted numeric">{value}%</span></span>
      <input type="range" min="0" max="100" value={value} onChange={(event) => onChange(Number(event.target.value))} />
    </label>
  );
}

function OverviewSettings({ appearance }) {
  const { preferences: { widgets, cardOrder, cardWidths }, update, moveCard, resetCardLayout } = appearance;
  const setVisible = (id, checked) => update('widgets', { ...widgets, [id]: checked });
  const hasCustomLayout = Object.values(cardWidths).includes('wide')
    || cardOrder.some((id, index) => id !== defaultCardOrder[index]);

  return <div className="settings-fields">
    <div className="settings-switches">
      {overviewStrips.map(({ id, title }) => <label className="settings-row" key={id}><span>{title}</span>
        <input className="toggle-switch" type="checkbox" role="switch" checked={widgets[id] !== false}
          onChange={(event) => setVisible(id, event.target.checked)} />
      </label>)}
    </div>
    <section className="settings-subsection">
      <div className="settings-row"><h3>{t('概览卡片')}</h3>
        <Button className="reset-overview-layout" disabled={!hasCustomLayout} onClick={resetCardLayout}>
          <RotateCcw size={14} aria-hidden="true" />{t('恢复默认布局')}</Button>
      </div>
      <ol className="overview-settings-cards">
        {orderedCards(cardOrder).map(({ id, title, Icon }, index) => <li className="overview-settings-card" key={id}>
          <span><Icon size={16} aria-hidden="true" />{title}</span>
          <select aria-label={t('{p1}卡片尺寸', { p1: title })} value={cardWidths[id] ?? 'standard'}
            onChange={(event) => update('cardWidths', { ...cardWidths, [id]: event.target.value })}>
            <option value="standard">{t('标准 1×1')}</option><option value="wide">{t('加宽 2×1')}</option>
          </select>
          <div className="overview-settings-move">
            <IconButton label={t('上移{p1}', { p1: title })} disabled={index === 0} onClick={() => moveCard(id, -1)}><ArrowUp size={15} aria-hidden="true" /></IconButton>
            <IconButton label={t('下移{p1}', { p1: title })} disabled={index === cardOrder.length - 1} onClick={() => moveCard(id, 1)}><ArrowDown size={15} aria-hidden="true" /></IconButton>
          </div>
          <input className="toggle-switch" type="checkbox" role="switch" aria-label={t('显示{p1}', { p1: title })}
            checked={widgets[id] !== false} onChange={(event) => setVisible(id, event.target.checked)} />
        </li>)}
      </ol>
    </section>
  </div>;
}

function CleanupSettings() {
  const [days, setDays] = useState(undefined);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    request('/api/scratchpad/settings', { signal: controller.signal }).then((data) => {
      if (!controller.signal.aborted) setDays(data.retention_days);
    }).catch((failure) => {
      if (!controller.signal.aborted) setError(failure.message);
    });
    return () => controller.abort();
  }, []);

  async function save(value) {
    setBusy(true);
    setError('');
    try {
      const result = await sendJson('PATCH', '/api/scratchpad/settings', { retention_days: value === 'off' ? null : Number(value) });
      setDays(result.retention_days);
    } catch (failure) {
      setError(failure.message);
    } finally {
      setBusy(false);
    }
  }

  return <div className="retention-setting">
    <label className="settings-row">
      <span>{t('中转站自动清理')}</span>
      <select disabled={busy || days === undefined} value={days === undefined ? 'loading' : days ?? 'off'} onChange={(event) => save(event.target.value)}>
        {days === undefined && <option value="loading">{error ? t('暂不可用') : t('正在读取…')}</option>}
        <option value="3">{t('3 天')}</option><option value="7">{t('7 天')}</option><option value="30">{t('30 天')}</option><option value="off">{t('关闭')}</option>
      </select>
    </label>
    <p className="muted">{t('只清理超过保留期的未固定卡片。此设置对所有设备生效。')}</p>
    {error && <p className="feedback error" role="alert">{error}</p>}
  </div>;
}

function SystemStatus({ telemetry, platform }) {
  const { connection, sample } = telemetry;
  const socket = { online: [t('在线'), 'on'], offline: [t('已断开'), 'off'], connecting: [t('连接中'), 'waiting'] }[connection] ?? [t('等待连接'), 'waiting'];
  const lhm = connection !== 'online' || !sample ? [t('等待数据'), 'waiting']
    : sample.lhm_connected ? [t('已连接'), 'on'] : [t('未连接'), 'off'];
  const statuses = [['WebSocket', socket]];
  if (platform === 'windows') statuses.push(['LHM', lhm]);
  return <dl className="settings-switches" role="status">
    {statuses.map(([name, [status, tone]]) => <div className="settings-row" key={name}>
      <dt>{name}</dt><dd className={`system-badge system-${tone}`}><span className="state-dot" aria-hidden="true" />{status}</dd>
    </div>)}
  </dl>;
}

function DeviceNameRow({ device, alias, busy, onRename, onRemove }) {
  const [draft, setDraft] = useState(alias ?? '');
  useEffect(() => setDraft(alias ?? ''), [alias]);
  const isHub = device.device_id === HUB_DEVICE_ID;
  const reportedName = isHub ? device.reportedName ?? device.platform ?? t('本机') : device.snapshot.name;
  const status = device.online ? t('在线') : isHub ? t('连接中') : t('已休眠或离线');
  const lastSeen = !isHub && !device.online && device.last_seen
    ? new Date(device.last_seen).toLocaleString(getLocale()) : null;
  return <form className="settings-device-row" onSubmit={(event) => {
    event.preventDefault();
    if (!busy) onRename(device.device_id, draft.trim() || null);
  }}>
    <div className="settings-device-identity">
      <strong title={device.name}>{device.name}</strong>
      <small className="muted"><DeviceDot online={device.online} />{isHub ? t('本机 · {p1}', { p1: status }) : status}
        {lastSeen && ` · ${lastSeen}`}
      </small>
    </div>
    <div className="settings-device-actions">
      <input aria-label={t('{p1} 的昵称', { p1: device.name })} maxLength={64} value={draft} placeholder={reportedName}
        onChange={(event) => setDraft(event.target.value)} />
      <Button type="submit" disabled={busy || draft.trim() === (alias ?? '')}>{t('保存')}</Button>
      {!isHub && !device.online && <Button disabled={busy} onClick={() => onRemove(device)} aria-haspopup="dialog">{t('移除')}</Button>}
    </div>
  </form>;
}

function DeviceSettings({ hub, devices, aliases, defaultDevice, error, onDefault, onRename, onRemove }) {
  const [pendingRemoval, setPendingRemoval] = useState(null);
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState('');
  const entries = [hub, ...devices];
  async function mutate(action) {
    setBusy(true);
    setFeedback('');
    try {
      await action();
      setPendingRemoval(null);
    } catch (failure) {
      setFeedback(failure.message);
    } finally {
      setBusy(false);
    }
  }
  return <section className="settings-subsection">
    <h3>{t('设备管理')}</h3>
    <label className="settings-row">
      <span>{t('默认主控设备')}</span>
      <select value={entries.some(({ device_id }) => device_id === defaultDevice) ? defaultDevice : HUB_DEVICE_ID}
        onChange={(event) => onDefault(event.target.value)}>
        {entries.map(({ device_id, name, online }) => <option key={device_id} value={device_id}>
          {name}{device_id !== HUB_DEVICE_ID && !online ? t(' · 离线') : ''}
        </option>)}
      </select>
    </label>
    <div className="settings-device-list">
      {entries.map((device) => <DeviceNameRow key={device.device_id} device={device} busy={busy}
        alias={aliases[device.device_id]} onRename={(id, alias) => mutate(() => onRename(id, alias))}
        onRemove={(item) => { setFeedback(''); setPendingRemoval(item); }} />)}
    </div>
    {(error || (feedback && !pendingRemoval)) && <p className="feedback error" role="alert">{feedback || error}</p>}
    <AnimatePresence>
      {pendingRemoval && <Modal id="remove-device" title={t('移除离线设备')} className="detail-dialog shutdown-dialog"
        onClose={() => !busy && setPendingRemoval(null)}>
        <p>{t('移除「{p1}」并清除它的昵称及最后一次设备快照？', { p1: pendingRemoval.name })}</p>
        {feedback && <p className="feedback error" role="alert">{feedback}</p>}
        <div className="quick-confirm-actions">
          <Button disabled={busy} onClick={() => setPendingRemoval(null)}>{t('取消')}</Button>
          <Button className="primary-button" disabled={busy} onClick={() => mutate(() => onRemove(pendingRemoval.device_id))}>{t('确认移除')}</Button>
        </div>
      </Modal>}
    </AnimatePresence>
  </section>;
}

function BackupSettings() {
  const input = useRef(null);
  const [file, setFile] = useState(null);
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState(null);
  async function restore() {
    setBusy(true);
    setFeedback(null);
    try {
      const body = new FormData();
      body.append('file', file);
      const result = await request('/api/system/backup/restore', { method: 'POST', body });
      setFile(null);
      setFeedback({ text: result.message });
    } catch (failure) {
      setFeedback({ text: failure.message, error: true });
    } finally {
      setBusy(false);
    }
  }
  return <section className="settings-subsection">
    <h3>{t('数据备份与迁移')}</h3>
    <div className="settings-backup-actions">
      <a className="button" href={httpUrl('/api/system/backup').href} download><Download size={16} />{t('导出数据备份 (.zip)')}</a>
      <Button disabled={busy} onClick={() => input.current?.click()}><Upload size={16} />{t('导入备份恢复')}</Button>
    </div>
    <input type="file" ref={input} accept=".zip,application/zip" hidden onChange={(event) => {
      const selected = event.target.files[0];
      event.target.value = '';
      if (!selected) return;
      setFeedback(null);
      if (selected.name.toLowerCase().endsWith('.zip')) setFile(selected);
      else setFeedback({ text: t('请选择 ZIP 备份文件。'), error: true });
    }} />
    {!file && feedback && <p className={feedback.error ? 'feedback error' : 'feedback'}
      role={feedback.error ? 'alert' : 'status'}>{t(feedback.text)}</p>}
    <AnimatePresence>
      {file && <Modal id="restore-backup" title={t('确认恢复备份')} className="detail-dialog shutdown-dialog"
        onClose={() => !busy && setFile(null)}>
        <p className="backup-file-name">{file.name}</p>
        <p>{t('重启后将用备份替换本机数据库、中转站附件和壁纸。')}</p>
        {feedback && <p className="feedback error" role="alert">{feedback.text}</p>}
        <div className="quick-confirm-actions">
          <Button disabled={busy} onClick={() => { setFile(null); setFeedback(null); }}>{t('取消')}</Button>
          <Button className="primary-button" disabled={busy} onClick={restore}>{busy ? t('正在校验…') : t('确认恢复')}</Button>
        </div>
      </Modal>}
    </AnimatePresence>
  </section>;
}

export function AppearanceDialog({ appearance, onClose, ai, telemetry, deviceSettings, initialCategory, onOpenWizard, wizardOpen }) {
  const input = useRef(null);
  const [category, setCategory] = useState(initialCategory);
  const [hostConfig, setHostConfig] = useState(null);
  const categories = [
    ...baseCategories,
    ...(hostConfig ? [{ id: 'host', label: t('主机配置'), Icon: ServerCog }] : []),
    { id: 'about', label: t('关于'), Icon: Info },
  ];
  const { preferences, busy, appearanceError, update, changeWallpaper } = appearance;
  useEffect(() => {
    if (wizardOpen || !['127.0.0.1', '[::1]'].includes(window.location.hostname)) return undefined;
    const controller = new AbortController();
    request('/api/setup/config', { signal: controller.signal })
      .then((config) => { if (!controller.signal.aborted) setHostConfig(config); }).catch(() => {});
    return () => controller.abort();
  }, [wizardOpen]);
  function changeTab(event) {
    const offset = { ArrowLeft: -1, ArrowUp: -1, ArrowRight: 1, ArrowDown: 1 }[event.key];
    if (!offset && event.key !== 'Home' && event.key !== 'End') return;
    event.preventDefault();
    const index = categories.findIndex(({ id }) => id === category);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? categories.length - 1
      : (index + offset + categories.length) % categories.length;
    setCategory(categories[next].id);
    event.currentTarget.querySelectorAll('[role="tab"]')[next]?.focus();
  }

  return (
    <Modal id="appearance" title={t('个性化设置')} className="detail-dialog settings-dialog" onClose={onClose}>
      <div className="settings-layout">
        <div className="settings-nav" role="tablist" aria-label={t('设置分类')} onKeyDown={changeTab}>
          {categories.map(({ id, label, Icon }) => <Button key={id} id={`settings-tab-${id}`} className="settings-category"
            role="tab" aria-selected={id === category} aria-controls={`settings-panel-${id}`}
            tabIndex={id === category ? 0 : -1} onClick={() => setCategory(id)}>
            <Icon size={17} aria-hidden="true" />{label}
            {id === 'about' && appearance.updates.result?.has_update &&
              <span className="about-update-dot" role="img" aria-label={t('有新版本')} />}
          </Button>)}
        </div>
        <div key={category} className="settings-panel" id={`settings-panel-${category}`} role="tabpanel"
          aria-labelledby={`settings-tab-${category}`} tabIndex={0}>
          {category === 'appearance' && <div className="settings-fields">
            <LanguageSettings />
            <div className="settings-row theme-setting">
              <span>{t('外观')}</span><Segmented id="theme" label={t('外观')} options={themes} value={preferences.theme} onChange={(value) => update('theme', value)} />
            </div>
            <div className="settings-wallpaper">
              <div className="settings-row">
                <span>{t('壁纸')}</span>
                <Button disabled={busy} onClick={() => input.current.click()}><ImagePlus size={18} />{busy ? t('正在处理…') : t('上传壁纸')}</Button>
                <input type="file" ref={input} accept="image/png,image/jpeg,image/webp" hidden onChange={(event) => {
                  const file = event.target.files[0];
                  event.target.value = '';
                  if (file) changeWallpaper(file);
                }} />
              </div>
              <p className="muted">{t('PNG、JPEG 或 WebP，最大 10 MiB。壁纸会同步到其他设备。')}</p>
              <Button disabled={busy || (!appearance.wallpaper && preferences.theme === 'light')} onClick={() => changeWallpaper()}>
                <RotateCcw size={16} />{t('恢复默认背景')}</Button>
            </div>
            <Slider label={t('遮罩透明度')} value={preferences.opacity} onChange={(value) => update('opacity', value)} />
            <div className="settings-row">
              <span>{t('卡片质感')}</span><Segmented id="material" label={t('卡片质感')} options={materials} value={preferences.material} onChange={(value) => update('material', value)} />
            </div>
            <Slider label={t('玻璃强度')} value={preferences.strength} onChange={(value) => update('strength', value)} />
            <label className="settings-row"><span>{t('表盘样式')}</span>
              <select value={preferences.clockStyle} onChange={(event) => update('clockStyle', event.target.value)}>
                {clockStyles.map(({ value, label }) => <option key={value} value={value}>{label}</option>)}
              </select>
            </label>
            {appearanceError && <p className="feedback error" role="alert">{appearanceError}</p>}
          </div>}
          {category === 'overview' && <OverviewSettings appearance={appearance} />}
          {category === 'scratchpad' && <CleanupSettings />}
          {category === 'system' && <div className="settings-fields">
            <DesktopModeSettings />
            <DesktopLaunchAtLogin />
            <SystemStatus telemetry={telemetry} platform={deviceSettings.hub.platform} />
            <SamplingModeSettings />
            <DeviceSettings {...deviceSettings} />
            <BackupSettings />
          </div>}
          {category === 'host' && (hostConfig
            ? <Suspense fallback={<p className="muted" role="status">{t('正在加载主机配置…')}</p>}>
              <HostSetup config={hostConfig} onConfig={setHostConfig} onOpenWizard={onOpenWizard} />
            </Suspense>
            : <p className="muted" role="status">{t('正在加载主机配置…')}</p>)}
          {category === 'about' && <AboutSettings updates={appearance.updates} enabled={preferences.autoUpdateCheck}
            onToggle={(value) => update('autoUpdateCheck', value)} />}
          {category === 'ai' && <div className="settings-switches">
              <label className="settings-row"><span>{t('启用 AI 助手')}</span>
                <input className="toggle-switch" type="checkbox" role="switch" checked={ai.settings?.enabled ?? false}
                  disabled={!ai.settings || ai.busy} onChange={(event) => ai.update({ enabled: event.target.checked })} />
              </label>
              <label className="settings-row"><span>{t('附带 Vitals 实时状态')}</span>
                <input className="toggle-switch" type="checkbox" role="switch" checked={ai.settings?.include_vitals_context ?? false}
                  disabled={!ai.settings || ai.busy} onChange={(event) => ai.update({ include_vitals_context: event.target.checked })} />
              </label>
              {ai.settings?.configured === false && <p className="muted">{t('请在服务器 config.json 中配置 ai.api_key')}</p>}
              {ai.error && <p className="feedback error" role="status">{ai.error}</p>}
          </div>}
        </div>
      </div>
    </Modal>
  );
}
