import { t } from './i18n/messages.js';
import { useEffect, useRef, useState } from 'react';
import { Copy, Plus, Sparkles, Trash2 } from 'lucide-react';
import { postJson, request, sendJson } from './api.js';
import SetupWeatherFields, { setupWeatherPatch } from './SetupWeatherFields.jsx';
import { Button } from './ui.jsx';

const BASE = '/api/setup';
const calendarTypes = { icloud: 'iCloud', google: 'Google', outlook: 'Outlook', get ics() { return t('ICS 订阅'); } };
const credentials = {
  icloud: [['apple_id', 'Apple ID'], ['app_password', 'App 专用密码']],
  google: [['client_id', 'Client ID'], ['client_secret', 'Client Secret'], ['refresh_token', 'Refresh Token']],
  outlook: [['client_id', 'Client ID'], ['client_secret', 'Client Secret'], ['refresh_token', 'Refresh Token']],
  ics: [['url', '订阅地址']],
};

function SetupField({ label, secret, configured, ...props }) {
  return <label className="host-setup-field">
    <span>{t(label)}</span>
    <input {...props} type={secret ? 'password' : props.type}
      autoComplete={secret ? 'off' : props.autoComplete}
      placeholder={secret && configured ? t('••••••••（已配置）') : props.placeholder} />
  </label>;
}

function Feedback({ error, notice }) {
  return error
    ? <p className="feedback error" role="alert">{error}</p>
    : notice && <p className="feedback" role="status">{notice}</p>;
}

function useSetupAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  async function run(action, success = '') {
    setBusy(true);
    setError('');
    setNotice('');
    try {
      setNotice((await action()) || success);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }
  return { busy, error, notice, run };
}

function SetupForm({ title, onSave, children, onTest }) {
  const action = useSetupAction();
  return <form className="settings-subsection host-setup-section" onSubmit={(event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const fields = new FormData(form);
    action.run(async () => {
      await onSave(fields);
      form.querySelectorAll('input[type="password"]').forEach((input) => { input.value = ''; });
    }, t('设置已保存'));
  }}>
    <h3>{title}</h3>
    {children}
    <div className="host-setup-actions">
      <Button type="submit" disabled={action.busy}>{t('保存')}</Button>
      {onTest && <Button onClick={() => action.run(onTest, t('测试推送已发送'))} disabled={action.busy}>{t('发送测试推送')}</Button>}
    </div>
    <Feedback {...action} />
  </form>;
}

function TailscaleSetup() {
  const [state, setState] = useState(null);
  const action = useSetupAction();
  useEffect(() => {
    const controller = new AbortController();
    action.run(async () => {
      const value = await request(`${BASE}/tailscale`, { signal: controller.signal });
      setState(value);
    });
    return () => controller.abort();
  }, []);
  const toggle = () => action.run(async () => {
    setState(await postJson(`${BASE}/tailscale/serve`, { enabled: !state.serve_enabled }));
  });
  const label = !state ? t('正在读取状态…')
    : !state.installed ? t('本机未安装 Tailscale')
      : !state.logged_in ? t('请在 Tailscale 客户端登录并连接')
        : state.serve_enabled ? t('HTTPS 已开启') : t('Tailscale 已连接，HTTPS 未开启');
  return <section className="settings-subsection host-setup-section">
    <h3>{t('Tailscale 远程访问')}</h3>
    <p>{label}</p>
    {state?.dns_name && <p className="muted">{state.https_url || state.dns_name}</p>}
    {!!state?.ip_addresses?.length && <p className="muted">{state.ip_addresses.join(' · ')}</p>}
    {state?.blocked && <p className="feedback error">{t('HTTPS 根路径已被其他 Tailscale 规则占用。')}</p>}
    <div className="host-setup-actions">
      <Button onClick={toggle} disabled={action.busy || !state?.logged_in || !state?.dns_name || state?.blocked || !!state?.error}>
        {state?.serve_enabled ? t('关闭 Serve HTTPS') : t('开启 Serve HTTPS')}
      </Button>
      <Button disabled={action.busy} onClick={() => action.run(async () => setState(await request(`${BASE}/tailscale`)))}>{t('刷新状态')}</Button>
    </div>
    <Feedback error={action.error || state?.error} />
  </section>;
}

function CalendarSetup({ config, save }) {
  const [sources, setSources] = useState(config.sources);
  const action = useSetupAction();
  const update = (id, values) => setSources((current) => current.map((source) => source.id === id ? { ...source, ...values } : source));
  function add() {
    const id = `calendar-${crypto.randomUUID().slice(0, 8)}`;
    setSources((current) => [...current, { id, type: 'icloud', name: '', color: '#007AFF', writable: true }]);
  }
  const submit = (event) => {
    event.preventDefault();
    action.run(async () => {
      const result = await save({ calendar: { sources: sources.map((source) => {
        const { id, name, type, color, writable, calendar_id, tenant } = source;
        return {
          id, name: name.trim(), type, color, writable: type !== 'ics' && writable,
          calendar_id: calendar_id?.trim() || null,
          ...(type === 'outlook' ? { tenant: tenant || 'common' } : {}),
          ...Object.fromEntries(credentials[type].map(([key]) => [key, source[key] || ''])),
        };
      }) } });
      setSources(result.calendar.sources);
    }, t('日历源已保存'));
  };
  return <form className="settings-subsection host-setup-section" onSubmit={submit}>
    <h3>{t('日历源')}</h3>
    {sources.map((source, index) => <fieldset className="host-calendar-source" key={source.id} disabled={action.busy}>
      <legend>{source.name || t('日历源 {p1}', { p1: index + 1 })}</legend>
      <div className="host-setup-grid">
        <SetupField label={t('名称')} value={source.name} required maxLength={80} onChange={(event) => update(source.id, { name: event.target.value })} />
        <label className="host-setup-field"><span>{t('类型')}</span>
          <select value={source.type} disabled={config.sources.some(({ id }) => id === source.id)}
            onChange={(event) => update(source.id, { type: event.target.value })}>
            {Object.entries(calendarTypes).map(([type, label]) => <option key={type} value={type}>{label}</option>)}
          </select>
        </label>
        {credentials[source.type].map(([key, label]) => <SetupField key={key} label={label} secret
          configured={source[`has_${key}`]} value={source[key] || ''} onChange={(event) => update(source.id, { [key]: event.target.value })} />)}
        {source.type !== 'ics' && <SetupField label={t('日历 ID（可选）')} value={source.calendar_id || ''}
          onChange={(event) => update(source.id, { calendar_id: event.target.value })} />}
        {source.type === 'outlook' && <SetupField label={t('租户')} value={source.tenant || ''} placeholder="common"
          onChange={(event) => update(source.id, { tenant: event.target.value })} />}
      </div>
      <div className="host-setup-actions">
        <label className="host-setup-color">{t('颜色')}<input type="color" value={source.color}
          onChange={(event) => update(source.id, { color: event.target.value })} /></label>
        {source.type !== 'ics' && <label><input type="checkbox" checked={source.writable}
          onChange={(event) => update(source.id, { writable: event.target.checked })} />{t('允许写入')}</label>}
        <Button aria-label={t('移除{p1}', { p1: source.name || t('日历源') })} onClick={() =>
          setSources((current) => current.filter(({ id }) => id !== source.id))}><Trash2 size={16} />{t('移除')}</Button>
      </div>
    </fieldset>)}
    <div className="host-setup-actions">
      <Button onClick={add} disabled={sources.length >= 16 || action.busy}><Plus size={16} />{t('添加日历源')}</Button>
      <Button type="submit" disabled={action.busy}>{t('保存日历源')}</Button>
    </div>
    <Feedback {...action} />
  </form>;
}

function AgentSetup({ config, onChange }) {
  const [token, setToken] = useState('');
  const [confirming, setConfirming] = useState(false);
  const input = useRef(null);
  const action = useSetupAction();
  const tokenRequest = (regenerate) => action.run(async () => {
    const result = await postJson(`${BASE}/agents/token`, { regenerate });
    setToken(result.token || '');
    setConfirming(false);
    if (regenerate) onChange(result.restart_required);
  }, regenerate ? t('配对令牌已生成') : '');
  const copy = () => action.run(async () => {
    try {
      await navigator.clipboard.writeText(token);
      return t('配对令牌已复制');
    } catch {
      input.current?.focus();
      input.current?.select();
      return t('已选中令牌，请手动复制');
    }
  });
  return <section className="settings-subsection host-setup-section">
    <h3>{t('多设备配对')}</h3>
    <p className="muted">{config.has_token ? t('已配置 Agent Token') : t('生成 Agent Token 后可配对远程设备。')}</p>
    {token && <SetupField label="Agent Token" ref={input} readOnly value={token} />}
    {confirming ? <div className="host-setup-actions">
      <span>{t('重启后，已配对设备须更新为新令牌。')}</span>
      <Button disabled={action.busy} onClick={() => tokenRequest(true)}>{t('确认重新生成')}</Button>
      <Button disabled={action.busy} onClick={() => setConfirming(false)}>{t('取消')}</Button>
    </div> : <div className="host-setup-actions">
      {config.has_token && <Button disabled={action.busy} onClick={() => token ? setToken('') : tokenRequest(false)}>{token ? t('隐藏令牌') : t('查看令牌')}</Button>}
      {token && <Button disabled={action.busy} onClick={copy}><Copy size={16} />{t('复制')}</Button>}
      <Button disabled={action.busy} onClick={() => config.has_token ? setConfirming(true) : tokenRequest(true)}>
        {config.has_token ? t('重新生成令牌') : t('生成令牌')}
      </Button>
    </div>}
    <Feedback {...action} />
  </section>;
}

function WeatherSetup({ weather, timezone, save }) {
  return <SetupForm title={t('天气与时区')} onSave={(data) => save(setupWeatherPatch(data))}>
    <SetupWeatherFields weather={weather} timezone={timezone} />
  </SetupForm>;
}

function LauncherSetup({ entries, save }) {
  const [items, setItems] = useState(entries);
  const action = useSetupAction();
  function edit(id, key, value) {
    setItems((previous) => previous.map((item) => item.id === id ? { ...item, [key]: value } : item));
  }
  return <form className="settings-subsection host-setup-section" onSubmit={(event) => {
    event.preventDefault();
    action.run(async () => setItems((await save({ launchers: items })).launchers), t('启动项已保存'));
  }}>
    <h3>{t('快捷启动')}</h3>
    {items.map((item, index) => <fieldset key={item.id} className="launcher-settings-item" disabled={action.busy}
      aria-label={item.name || t('启动项 {p1}', { p1: index + 1 })}>
      <div className="host-setup-grid">
        <SetupField label={t('名称')} value={item.name} maxLength={64} required
          onChange={(event) => edit(item.id, 'name', event.target.value)} />
        <label className="host-setup-field"><span>{t('类型')}</span>
          <select value={item.kind} onChange={(event) => edit(item.id, 'kind', event.target.value)}>
            <option value="app">{t('应用')}</option><option value="url">{t('网页链接')}</option>
          </select>
        </label>
        <div className="launcher-config-target">
          <SetupField label={item.kind === 'app' ? t('本机应用绝对路径') : t('HTTP 或 HTTPS 地址')}
            value={item.target} type={item.kind === 'url' ? 'url' : 'text'} maxLength={2048} required
            onChange={(event) => edit(item.id, 'target', event.target.value)} />
        </div>
      </div>
      <div className="host-setup-actions">
        <Button onClick={() => setItems((previous) => previous.filter((entry) => entry.id !== item.id))}>
          <Trash2 size={16} aria-hidden="true" />{t('移除')}</Button>
      </div>
    </fieldset>)}
    <div className="host-setup-actions">
      <Button disabled={action.busy || items.length >= 32} onClick={() => {
        const id = `app-${crypto.randomUUID()}`;
        setItems((previous) => [...previous, { id, name: '', kind: 'app', target: '' }]);
      }}><Plus size={16} aria-hidden="true" />{t('添加启动项')}</Button>
      <Button type="submit" disabled={action.busy}>{t('保存')}</Button>
    </div>
    <Feedback {...action} />
  </form>;
}

export default function HostSetup({ config, onConfig, onOpenWizard }) {
  const [barkEnabled, setBarkEnabled] = useState(config.webhook.kind === 'bark');
  async function save(payload) {
    const result = await sendJson('PATCH', `${BASE}/config`, payload);
    onConfig(result);
    return result;
  }
  return <div className="settings-fields host-setup">
    {onOpenWizard && <div className="host-setup-actions"><Button onClick={onOpenWizard} aria-haspopup="dialog">
      <Sparkles size={16} aria-hidden="true" />{t('打开设置向导')}</Button></div>}
    {config.restart_required && <p className="feedback" role="status">{t('设置已保存。请在托盘中重启服务，使新配置生效。')}</p>}
    <TailscaleSetup />
    <SetupForm title={t('AI 助手')} onSave={(data) => save({ ai: Object.fromEntries(data) })}>
      <div className="host-setup-grid">
        <SetupField label={t('模型')} name="model" defaultValue={config.ai.model} required maxLength={100} />
        <SetupField label={t('OpenAI 服务地址')} name="base_url" type="url" defaultValue={config.ai.base_url} required />
        <SetupField label="OpenAI API Key" name="api_key" secret configured={config.ai.has_api_key} />
      </div>
    </SetupForm>
    <SetupForm title={t('AI 用量统计')} onSave={(data) => save({ ai_usage: Object.fromEntries(data) })}>
      <p className="muted">{t('密钥用于查询云端用量或余额，留空会保留已保存的值。')}</p>
      <div className="host-setup-grid">
        <SetupField label="DeepSeek API Key" name="deepseek_api_key" secret configured={config.ai_usage?.has_deepseek_api_key} />
        <SetupField label="OpenAI Admin Key" name="openai_admin_key" secret configured={config.ai_usage?.has_openai_admin_key} />
        <SetupField label="Anthropic Admin Key" name="anthropic_admin_key" secret configured={config.ai_usage?.has_anthropic_admin_key} />
      </div>
    </SetupForm>
    <WeatherSetup weather={config.weather} timezone={config.health.timezone} save={save} />
    <SetupForm title={t('Bark 推送')} onTest={() => postJson(`${BASE}/webhook/test`, {})}
      onSave={(data) => save({ webhook: {
        kind: barkEnabled ? 'bark' : config.webhook.kind === 'json' ? 'json' : 'disabled',
        device_key: data.get('device_key'), daily_briefing_time: data.get('daily_briefing_time') || null,
      } })}>
      <label className="settings-row"><span>{t('启用 Bark')}</span>
        <input type="checkbox" role="switch" className="toggle-switch" checked={barkEnabled}
          onChange={(event) => setBarkEnabled(event.target.checked)} />
      </label>
      <div className="host-setup-grid">
        <SetupField label="Device Key" name="device_key" secret configured={config.webhook.has_key} />
        <SetupField label={t('每日简报推送时间')} name="daily_briefing_time" type="time" defaultValue={config.webhook.daily_briefing_time || ''} />
      </div>
    </SetupForm>
    <CalendarSetup config={config.calendar} save={save} />
    <LauncherSetup entries={config.launchers ?? []} save={save} />
    <AgentSetup config={config.agents} onChange={(restart_required) =>
      onConfig((previous) => ({ ...previous, restart_required: previous.restart_required || restart_required, agents: { has_token: true } }))} />
  </div>;
}
