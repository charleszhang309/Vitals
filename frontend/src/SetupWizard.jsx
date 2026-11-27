import { t } from './i18n/messages.js';
import { useI18n } from './i18n/index.jsx';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ArrowLeft, ArrowRight, Bell, CalendarDays, Check, Copy, Laptop, Network, Server, Sparkles } from 'lucide-react';
import qrcode from 'qrcode-generator';
import { postJson, request, sendJson } from './api.js';
import SetupWeatherFields, { setupWeatherPatch } from './SetupWeatherFields.jsx';
import { Button, Modal } from './ui.jsx';
import './setupWizard.css';

const setup = '/api/setup';
const stepNames = ['运行模式', '主机设置', '可选服务'];
const modes = [
  ['hub', Server, '本机主机', '在这台电脑上运行 Vitals，存储数据并接收其他设备的指标。'],
  ['remote', Laptop, '远程客户端', '连接已有的 Vitals 主机，将本机的硬件指标发送给主机。'],
];

function ErrorNote({ children }) {
  return children ? <p className="feedback error" role="alert">{t(children)}</p> : null;
}

function Field({ children, ...props }) {
  return <label className="host-setup-field"><span>{children}</span><input {...props} /></label>;
}

function ModeStep({ desktop, busy, onRemote, onHub, onSkip }) {
  const [mode, setMode] = useState('hub');
  const [url, setUrl] = useState(desktop?.remote_url ?? '');
  const [token, setToken] = useState('');
  let keepToken = false;
  try {
    keepToken = desktop?.has_token && new URL(url.trim()).href === desktop.remote_url;
  } catch {
    // 输入未完成时等待表单校验。
  }
  return <form className="wizard-step" onSubmit={(event) => {
    event.preventDefault();
    if (mode === 'remote') onRemote(url.trim(), token.trim());
    else onHub();
  }}>
    <div className="wizard-step-heading"><h3>{t('选择这台电脑的用途')}</h3></div>
    <div className="wizard-modes" role="group" aria-label={t('桌面运行模式')}>
      {(desktop ? modes : modes.slice(0, 1)).map(([id, Icon, title, description]) => <Button key={id}
        className="wizard-mode" aria-pressed={mode === id} disabled={busy} onClick={() => setMode(id)}>
        <span className="wizard-mode-icon"><Icon size={23} aria-hidden="true" /></span>
        <span><strong>{t(title)}</strong><small>{t(description)}</small></span>
        {mode === id && <Check size={17} className="wizard-selected" aria-hidden="true" />}
      </Button>)}
    </div>
    {mode === 'remote' && <div className="wizard-panel wizard-remote-fields host-setup-grid">
      <Field type="url" value={url} required disabled={busy} placeholder="https://your-device.your-tailnet.ts.net"
        onChange={(event) => setUrl(event.target.value)}>{t('远程主机 HTTPS 地址')}</Field>
      <Field type="password" autoComplete="new-password" value={token} required={!keepToken} disabled={busy}
        placeholder={keepToken ? t('已配置，留空保留') : t('输入主机的配对 Token')}
        onChange={(event) => setToken(event.target.value)}>{t('配对 Token')}</Field>
      <p className="muted">{t('在主机的「主机配置 → 设备配对」中读取 Token。')}</p>
    </div>}
    <div className="wizard-footer">
      <Button disabled={busy} onClick={onSkip}>{t('稍后设置')}</Button>
      <Button className="primary-button" type="submit" disabled={busy}>
        {busy ? t('正在切换…') : mode === 'remote' ? t('保存并切换') : t('下一步')}<ArrowRight size={16} aria-hidden="true" />
      </Button>
    </div>
  </form>;
}

function QrAddress({ url }) {
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState('');
  const image = useMemo(() => {
    const qr = qrcode(0, 'M');
    qr.addData(url);
    qr.make();
    return qr.createDataURL(4);
  }, [url]);
  async function copy() {
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
      setError('');
    } catch {
      setError(t('无法访问剪贴板，请选中下方地址复制。'));
    }
  }
  return <div className="wizard-qr">
    <img src={image} width="116" height="116" alt={t('主机 HTTPS 地址二维码')} />
    <div>
      <p>{t('用已连接同一 Tailscale 网络的设备扫码打开 Vitals。')}</p>
      <code>{url}</code>
      <Button onClick={copy}><Copy size={15} aria-hidden="true" />{copied ? t('已复制') : t('复制地址')}</Button>
      <ErrorNote>{error}</ErrorNote>
    </div>
  </div>;
}

function serveLabel(state, failed) {
  if (!state) return failed ? t('暂时无法读取 Tailscale 状态') : t('正在读取 Tailscale 状态…');
  if (!state.installed) return t('本机未安装 Tailscale');
  if (state.error) return t(state.error);
  if (!state.logged_in) return t('请在 Tailscale 客户端登录并连接');
  if (!state.dns_name) return t('请在 Tailscale 管理后台开启 MagicDNS 和 HTTPS');
  if (state.blocked) return t('HTTPS 根路径已被其他 Tailscale 规则占用');
  return state.serve_enabled ? t('Serve HTTPS 已开启') : t('Tailscale 已连接，可以开启 Serve HTTPS');
}

function canEnableServe(state) {
  return state.logged_in && state.dns_name && !state.serve_enabled && !state.blocked && !state.error;
}

function TailscalePanel({ state, checking, error, onRefresh, onEnable }) {
  return <section className="wizard-panel" aria-labelledby="wizard-network">
    <h4 id="wizard-network"><Network size={17} aria-hidden="true" />{t('Tailscale 远程访问')}</h4>
    <p className="muted" role="status">{checking ? t('正在检测 Tailscale…')
      : state || error ? serveLabel(state, error) : t('等待检测 Tailscale')}</p>
    {state?.https_url && <QrAddress key={state.https_url} url={state.https_url} />}
    {state && <div className="host-setup-actions">
      {canEnableServe(state) && <Button disabled={checking} onClick={onEnable}>{t('开启 Serve HTTPS')}</Button>}
      <Button disabled={checking} onClick={onRefresh}>{t('刷新状态')}</Button>
    </div>}
    <ErrorNote>{error}</ErrorNote>
    {!state && error && <Button disabled={checking} onClick={onRefresh}>{t('重试检测')}</Button>}
  </section>;
}

const lhmLabels = {
  get connected() { return t('LibreHardwareMonitor 已连接。'); },
  get running() { return t('LibreHardwareMonitor 已运行。请在 Options → Remote Web Server 中开启 Run。'); },
  get not_running() { return t('未检测到正在运行的 LibreHardwareMonitor。启动后可读取温度和风扇转速。'); },
  get unknown() { return t('暂时无法检测 LibreHardwareMonitor。稍后可以重新检测。'); },
};

function systemLocation() {
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const parts = timezone?.split('/') ?? [];
  const city = parts.length > 1 && parts[0] !== 'Etc' ? parts.at(-1).replaceAll('_', ' ') : '';
  return { timezone, city };
}

function canonicalTimezone(timezone) {
  try {
    return new Intl.DateTimeFormat('en', { timeZone: timezone }).resolvedOptions().timeZone;
  } catch {
    return timezone;
  }
}

function matchedCity(cities, system) {
  const zone = canonicalTimezone(system.timezone);
  const cityName = system.city.toLowerCase();
  const nearby = cities.filter((city) => canonicalTimezone(city.timezone) === zone);
  return nearby.find((city) => city.name.toLowerCase() === cityName) ?? (nearby.length === 1 ? nearby[0] : null);
}

function HubStep({ onBack, onContinue, onRestart }) {
  const [details, setDetails] = useState(null);
  const [checkError, setCheckError] = useState('');
  const [checking, setChecking] = useState(true);
  const [serve, setServe] = useState(null);
  const [serveError, setServeError] = useState('');
  const [checkingServe, setCheckingServe] = useState(false);
  const [pairing, setPairing] = useState('idle');
  const [pairingError, setPairingError] = useState('');
  const [locationNote, setLocationNote] = useState('');
  const [locationVersion, setLocationVersion] = useState(0);
  const [configuring, setConfiguring] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState('');
  const [system] = useState(systemLocation);
  const busy = configuring || saving || checkingServe || pairing === 'pending';

  const checks = useCallback(async (signal) => {
    setChecking(true);
    setCheckError('');
    try {
      const result = await request(`${setup}/wizard`, { signal });
      if (signal?.aborted) return null;
      setDetails(result);
      return result;
    } catch (failure) {
      if (!signal?.aborted) setCheckError(failure.message);
      return null;
    } finally {
      if (!signal?.aborted) setChecking(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    checks(controller.signal);
    return () => controller.abort();
  }, [checks]);

  async function checkServe(enable = false) {
    setCheckingServe(true);
    setServeError('');
    try {
      const result = await request(`${setup}/tailscale`);
      setServe(result);
      if (enable && canEnableServe(result)) {
        setServe(await postJson(`${setup}/tailscale/serve`, { enabled: true }));
      }
    } catch (failure) {
      setServeError(failure.message);
    } finally {
      setCheckingServe(false);
    }
  }

  async function provision() {
    setPairing('pending');
    setPairingError('');
    try {
      const result = await postJson(`${setup}/wizard/provision`, {});
      setPairing(result.has_token ? 'ready' : 'failed');
      if (result.restart_required) onRestart();
    } catch (failure) {
      setPairing('failed');
      setPairingError(failure.message);
    }
  }

  async function applyLocation(patch) {
    const result = await sendJson('PATCH', `${setup}/config`, patch);
    if (result.restart_required) onRestart();
    setDetails((current) => ({ ...current, weather: result.weather, timezone: result.health.timezone,
      weather_is_default: current.weather_is_default && !patch.weather,
      timezone_is_default: current.timezone_is_default && !patch.health }));
    setLocationVersion((version) => version + 1);
  }

  async function configureLocation() {
    setLocationNote('');
    setSaveError('');
    const current = await checks();
    if (!current) return;
    const patch = {};
    const usingSystem = current.timezone_is_default && system.timezone;
    if (usingSystem) patch.health = { timezone: system.timezone };

    let cityWarning = '';
    if (current.weather_is_default) {
      try {
        const query = new URLSearchParams({ q: system.city, language: 'en' });
        const cities = system.city ? await request(`${setup}/geocode?${query}`) : [];
        const match = matchedCity(cities, system);
        if (match) patch.weather = { city_name: match.name, latitude: match.latitude, longitude: match.longitude };
        else cityWarning = t('未找到与系统时区匹配的城市，请搜索或手动填写。');
      } catch (failure) {
        cityWarning = failure.message;
      }
    }
    try {
      if (Object.keys(patch).length) await applyLocation(patch);
      setLocationNote(cityWarning || (patch.weather || patch.health ? t('天气与时区设置已保存。') : t('已保留当前天气与时区设置。')));
    } catch (failure) {
      setSaveError(failure.message);
      setLocationNote(cityWarning);
    }
  }

  async function configure() {
    setConfiguring(true);
    await Promise.allSettled([checkServe(true), provision(), configureLocation()]);
    setConfiguring(false);
  }

  async function saveLocation(form, timezoneOnly) {
    const data = new FormData(form);
    setSaving(true);
    setSaveError('');
    setLocationNote('');
    try {
      await applyLocation(setupWeatherPatch(data, timezoneOnly));
      setLocationNote(timezoneOnly ? t('时区已保存。') : t('天气与时区设置已保存。'));
    } catch (failure) {
      setSaveError(failure.message);
    } finally {
      setSaving(false);
    }
  }

  const autoCity = details?.weather_is_default ? system.city : '';
  const timezone = details?.timezone_is_default ? system.timezone || details.timezone : details?.timezone;
  const location = autoCity ? { city_name: autoCity, latitude: '', longitude: '' } : details?.weather;
  const pairingReady = pairing === 'ready' || (pairing === 'idle' && details?.has_token);
  return <div className="wizard-step">
    <div className="wizard-step-heading wizard-auto">
      <div><h3>{t('设置本机主机')}</h3><p className="muted">{t('检测远程访问和硬件，准备配对 Token，并按系统时区查找天气城市。')}</p></div>
      <Button className="primary-button" disabled={busy || checking} onClick={configure}>
        <Sparkles size={16} aria-hidden="true" />{configuring ? t('正在自动配置…') : t('一键自动配置')}
      </Button>
    </div>
    <TailscalePanel state={serve} checking={checkingServe} error={serveError}
      onRefresh={() => checkServe()} onEnable={() => checkServe(true)} />
    <div className="wizard-panel" aria-label={t('设备配对与硬件检测')}>
      <h4>{t('设备配对与硬件检测')}</h4>
      <p role="status">{pairingReady ? t('配对 Token 已就绪，可在主机配置中读取。')
        : pairing === 'pending' ? t('正在准备配对 Token…') : pairing === 'failed' ? t('暂时无法准备配对 Token。') : t('等待准备配对 Token')}</p>
      <ErrorNote>{pairingError}</ErrorNote>
      {pairing === 'failed' && <Button disabled={busy} onClick={provision}>{t('重试准备配对 Token')}</Button>}
      {lhmLabels[details?.lhm] && <><p className="muted" role="status">{lhmLabels[details.lhm]}</p>
        {details.lhm !== 'connected' && <Button disabled={busy || checking} onClick={() => checks()}>{t('重新检测')}</Button>}</>}
    </div>
    <form className="wizard-panel" onSubmit={(event) => {
      event.preventDefault();
      saveLocation(event.currentTarget, false);
    }}>
      <h4>{t('天气与时区')}</h4>
      {locationNote && <p className="feedback" role="status">{locationNote}</p>}
      {details ? <>
        <fieldset className="wizard-location-fields" disabled={configuring || saving}>
          <SetupWeatherFields key={locationVersion} weather={location} timezone={timezone} systemTimezone={system.timezone} />
          <div className="host-setup-actions">
            <Button type="submit">{t('保存位置')}</Button>
            <Button onClick={(event) => saveLocation(event.currentTarget.form, true)}>{t('仅保存时区')}</Button>
          </div>
        </fieldset>
        <ErrorNote>{saveError}</ErrorNote>
      </> : <p className="muted" role="status">{checking ? t('正在读取本地设置…') : t('暂时无法读取本地设置。')}</p>}
      <ErrorNote>{checkError}</ErrorNote>
      {checkError && <Button disabled={busy || checking} onClick={() => checks()}>{t('重试读取')}</Button>}
    </form>
    <div className="wizard-footer">
      <Button disabled={busy} onClick={onBack}><ArrowLeft size={16} aria-hidden="true" />{t('上一步')}</Button>
      <Button disabled={busy} onClick={onContinue}>{t('下一步')}<ArrowRight size={16} aria-hidden="true" /></Button>
    </div>
  </div>;
}

const optionalServices = [
  [Sparkles, 'OpenAI', '在「主机配置 → AI 助手」填入 OpenAI API Key，再到「AI 助手」启用。'],
  [CalendarDays, 'iCloud 日历', '在「主机配置 → 日历源」添加 iCloud，使用 Apple ID 和 App 专用密码。'],
  [Bell, 'Bark', '在「主机配置 → Bark 推送」填入 Bark App 的 Device Key，并发送测试通知。'],
];

function OptionalStep({ desktop, restartRequired, busy, onBack, onFinish, onOpenHost }) {
  return <div className="wizard-step">
    <ul className="wizard-services">
      {optionalServices.map(([Icon, title, instructions]) => <li className="wizard-panel" key={title}>
        <span className="wizard-mode-icon"><Icon size={19} aria-hidden="true" /></span>
        <div><h4>{t(title)}</h4><p className="muted">{t(instructions)}</p></div>
      </li>)}
    </ul>
    {restartRequired && <p className="feedback" role="status">{desktop
      ? t('完成设置时将重启 Vitals 服务，应用已保存的更改。')
      : t('设置已保存。请重启 Vitals 服务，使新配置生效。')}</p>}
    <div className="wizard-footer">
      <Button disabled={busy} onClick={onBack}><ArrowLeft size={16} aria-hidden="true" />{t('上一步')}</Button>
      <div className="host-setup-actions">
        <Button disabled={busy} onClick={onOpenHost}>{t('打开主机配置')}</Button>
        <Button className="primary-button" disabled={busy} onClick={onFinish}>
          {busy ? t('正在重启…') : t('跳过并完成')}<ArrowRight size={16} aria-hidden="true" />
        </Button>
      </div>
    </div>
  </div>;
}

export default function SetupWizard({ desktop, onClose, onRemember, onOpenHost }) {
  const { locale, setLanguage, t } = useI18n();
  const [step, setStep] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [restartRequired, setRestartRequired] = useState(false);
  const setRestart = useCallback(() => setRestartRequired(true), []);
  const progress = useRef(null);
  const changeStep = (next) => {
    setStep(next);
    setError('');
  };
  useEffect(() => {
    if (step > 0) progress.current?.focus({ preventScroll: true });
  }, [step]);
  async function switchToRemote(url, token) {
    setBusy(true);
    setError('');
    const restoreSeen = onRemember();
    try {
      await window.__TAURI__.core.invoke('set_desktop_mode', { mode: 'remote', remote_url: url, token });
      onClose();
    } catch (failure) {
      restoreSeen();
      setError(typeof failure === 'string' ? failure : t('暂时无法切换到远程客户端。'));
    } finally {
      setBusy(false);
    }
  }
  async function finish() {
    if (!desktop || !restartRequired) {
      onClose();
      return;
    }
    setBusy(true);
    setError('');
    const restoreSeen = onRemember();
    try {
      await window.__TAURI__.core.invoke('restart_desktop_hub');
      onClose();
    } catch (failure) {
      restoreSeen();
      setError(typeof failure === 'string' ? failure : t('暂时无法重启服务，可通过托盘重启。'));
    } finally {
      setBusy(false);
    }
  }
  return <Modal id="vitals-setup-wizard" title={t('Vitals 设置向导')} className="detail-dialog wizard-dialog" onClose={() => !busy && onClose()}
    headerActions={<Button title={t('language.switch')} aria-label={t('language.switch')}
      onClick={() => setLanguage(locale === 'zh-CN' ? 'en-US' : 'zh-CN')}>中 / EN</Button>}>
    <ol ref={progress} className="wizard-progress" aria-label={t('设置步骤')} tabIndex={-1}>
      {stepNames.map((label, index) => <li key={label} aria-current={step === index ? 'step' : undefined}>
        <span className="wizard-step-number">{index < step ? <Check size={14} aria-hidden="true" /> : index + 1}</span>{t(label)}
      </li>)}
    </ol>
    {step === 0 && <ModeStep desktop={desktop} busy={busy} onRemote={switchToRemote} onHub={() => changeStep(1)} onSkip={onClose} />}
    {step === 1 && <HubStep onBack={() => changeStep(0)} onContinue={() => changeStep(2)} onRestart={setRestart} />}
    {step === 2 && <OptionalStep desktop={desktop} restartRequired={restartRequired} busy={busy}
      onBack={() => changeStep(1)} onFinish={finish} onOpenHost={onOpenHost} />}
    <ErrorNote>{error}</ErrorNote>
  </Modal>;
}
