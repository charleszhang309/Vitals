import { useEffect, useState } from 'react';
import { Copy, Minus, Square, X } from 'lucide-react';
import { t } from './i18n/messages.js';

export const desktopPlatform = window.__VITALS_DESKTOP_PLATFORM__;
const invoke = window.__TAURI__?.core?.invoke;

export function dragDesktop(event) {
  if (!desktopPlatform || event.button !== 0 || event.target.closest('button, a, input, select, textarea, [role="tablist"]')) return;
  event.preventDefault();
  event.stopPropagation();
  invoke?.('desktop_window_action', { action: event.detail === 2 ? 'toggle_maximize' : 'drag' }).catch(() => {});
}

export default function DesktopChrome() {
  const [maximized, setMaximized] = useState(false);
  useEffect(() => {
    if (desktopPlatform !== 'windows' || !invoke) return undefined;
    let active = true;
    function update() {
      invoke('desktop_window_action', { action: 'is_maximized' }).then((value) => {
        if (active) setMaximized(Boolean(value));
      }).catch(() => {});
    }
    window.addEventListener('resize', update);
    update();
    return () => { active = false; window.removeEventListener('resize', update); };
  }, []);
  if (desktopPlatform !== 'windows') return null;

  const commands = [
    ['minimize', t('最小化窗口'), Minus],
    ['toggle_maximize', t(maximized ? '还原窗口' : '最大化窗口'), maximized ? Copy : Square],
    ['close', t('关闭窗口'), X],
  ];
  return <div className="desktop-caption-controls">
    {commands.map(([action, label, Icon]) => <button type="button" key={action} data-action={action} title={label} aria-label={label}
      onClick={() => invoke?.('desktop_window_action', { action }).then((value) => {
        if (action === 'toggle_maximize') setMaximized(Boolean(value));
      }).catch(() => {})}><Icon size={15} aria-hidden="true" /></button>)}
  </div>;
}

export function DockSetting() {
  const [value, setValue] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    if (desktopPlatform !== 'macos' || !invoke) return undefined;
    let active = true;
    invoke('get_hide_dock_icon').then((enabled) => { if (active) setValue(enabled); }).catch(() => {});
    return () => { active = false; };
  }, []);
  if (value === null) return null;
  return <div className="settings-switches">
    <label className="settings-row"><span>{t('隐藏 Dock 图标（仅菜单栏显示）')}</span>
      <input type="checkbox" role="switch" checked={value} disabled={busy} onChange={async (event) => {
        const enabled = event.target.checked;
        setBusy(true);
        setError('');
        try { setValue(await invoke('set_hide_dock_icon', { enabled })); }
        catch { setError(t('无法更新 Dock 图标设置。')); }
        finally { setBusy(false); }
      }} />
    </label>
    {error && <p className="feedback error" role="alert">{error}</p>}
  </div>;
}
