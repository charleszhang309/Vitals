import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { ImagePlus, RotateCcw } from 'lucide-react';
import { httpUrl, request, sendJson } from './api.js';
import { Button, Modal, Segmented } from './ui.jsx';

const STORAGE_KEY = 'pcc-appearance';
const defaults = { theme: 'light', material: 'glass', opacity: 46, strength: 60 };
const level = (value, fallback) => Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : fallback;
const themes = [{ value: 'light', label: '浅色' }, { value: 'dark', label: '深色' }];
const materials = [{ value: 'glass', label: '液态玻璃' }, { value: 'acrylic', label: '亚克力' }];

function initialPreferences() {
  try {
    const stored = JSON.parse(localStorage.getItem(STORAGE_KEY));
    return {
      theme: stored?.theme === 'dark' ? 'dark' : 'light',
      material: stored?.material === 'acrylic' ? 'acrylic' : 'glass',
      opacity: level(stored?.opacity, defaults.opacity),
      strength: level(stored?.strength, defaults.strength),
    };
  } catch {
    return defaults;
  }
}

export function useAppearance(playerOpen) {
  const [preferences, setPreferences] = useState(initialPreferences);
  const [wallpaper, setWallpaper] = useState(null);
  const [busy, setBusy] = useState(false);
  const [appearanceError, setAppearanceError] = useState('');
  const changing = useRef(false);
  const revision = useRef(0);

  useLayoutEffect(() => {
    const root = document.documentElement;
    root.dataset.theme = preferences.theme;
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(preferences));
    } catch {
      // 在浏览器限制存储时，设置仍可在当前页面使用。
    }
  }, [preferences]);

  useLayoutEffect(() => {
    const root = document.documentElement;
    const themeColor = document.querySelector('meta[name="theme-color"]');
    const pageColor = preferences.theme === 'dark' ? '#000000' : '#f5f5f7';
    root.toggleAttribute('data-player-open', playerOpen);
    if (themeColor) themeColor.content = playerOpen ? '#111318' : pageColor;
    return () => {
      root.removeAttribute('data-player-open');
      if (themeColor) themeColor.content = pageColor;
    };
  }, [playerOpen, preferences.theme]);

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

  function update(key, value) {
    setPreferences((previous) => ({ ...previous, [key]: value }));
  }

  async function changeWallpaper(file) {
    if (changing.current) return;
    revision.current += 1;
    changing.current = true;
    setBusy(true);
    setAppearanceError('');
    try {
      if (file) {
        if (file.size > 10 * 1024 * 1024) throw new Error('壁纸图片上限为 10 MiB。');
        const result = await request('/api/wallpaper', { method: 'PUT', body: file });
        setWallpaper(result.version);
      } else {
        await request('/api/wallpaper', { method: 'DELETE' });
        setWallpaper(null);
        setPreferences(defaults);
      }
    } catch (error) {
      setAppearanceError(error.message);
    } finally {
      changing.current = false;
      setBusy(false);
    }
  }

  return { preferences, wallpaper, busy, appearanceError, update, changeWallpaper };
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
      <span>中转站自动清理</span>
      <select disabled={busy || days === undefined} value={days === undefined ? 'loading' : days ?? 'off'} onChange={(event) => save(event.target.value)}>
        {days === undefined && <option value="loading">{error ? '暂不可用' : '正在读取…'}</option>}
        <option value="3">3 天</option><option value="7">7 天</option><option value="30">30 天</option><option value="off">关闭</option>
      </select>
    </label>
    <p className="muted">只清理超过保留期的未固定卡片。此设置对所有设备生效。</p>
    {error && <p className="feedback error" role="alert">{error}</p>}
  </div>;
}

export function AppearanceDialog({ appearance, onClose, ai }) {
  const input = useRef(null);
  const { preferences, busy, appearanceError, update, changeWallpaper } = appearance;
  return (
    <Modal id="appearance" title="个性化设置" className="settings-dialog" onClose={onClose}>
      <div className="settings-fields">
        <div className="settings-row">
          <span>外观</span><Segmented id="theme" label="外观" options={themes} value={preferences.theme} onChange={(value) => update('theme', value)} />
        </div>
        <div className="settings-wallpaper">
          <div className="settings-row">
            <span>壁纸</span>
            <Button disabled={busy} onClick={() => input.current.click()}><ImagePlus size={18} />{busy ? '正在处理…' : '上传壁纸'}</Button>
            <input type="file" ref={input} accept="image/png,image/jpeg,image/webp" hidden onChange={(event) => {
              const file = event.target.files[0];
              event.target.value = '';
              if (file) changeWallpaper(file);
            }} />
          </div>
          <p className="muted">PNG、JPEG 或 WebP，最大 10 MiB。壁纸会同步到其他设备。</p>
          <Button disabled={busy || (!appearance.wallpaper && preferences.theme === 'light')} onClick={() => changeWallpaper()}>
            <RotateCcw size={16} /> 恢复默认背景
          </Button>
        </div>
        <Slider label="遮罩透明度" value={preferences.opacity} onChange={(value) => update('opacity', value)} />
        <div className="settings-row">
          <span>卡片质感</span><Segmented id="material" label="卡片质感" options={materials} value={preferences.material} onChange={(value) => update('material', value)} />
        </div>
        <Slider label="玻璃强度" value={preferences.strength} onChange={(value) => update('strength', value)} />
        <CleanupSettings />
        <div className="settings-ai" role="group" aria-label="AI 助手">
          <h3>AI 助手</h3>
          <label className="settings-row"><span>启用 AI 助手</span>
            <input className="toggle-switch" type="checkbox" role="switch" checked={ai.settings?.enabled ?? false}
              disabled={!ai.settings || ai.busy} onChange={(event) => ai.update({ enabled: event.target.checked })} />
          </label>
          <label className="settings-row"><span>附带 Vitals 实时状态</span>
            <input className="toggle-switch" type="checkbox" role="switch" checked={ai.settings?.include_vitals_context ?? false}
              disabled={!ai.settings || ai.busy} onChange={(event) => ai.update({ include_vitals_context: event.target.checked })} />
          </label>
          {ai.settings?.configured === false && <p className="muted">请在服务器 config.json 中配置 ai.api_key</p>}
          {ai.error && <p className="feedback error" role="status">{ai.error}</p>}
        </div>
        {appearanceError && <p className="feedback error" role="alert">{appearanceError}</p>}
      </div>
    </Modal>
  );
}
