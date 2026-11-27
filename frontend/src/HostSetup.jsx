import { useEffect, useRef, useState } from 'react';
import { Copy, Plus, Search, Trash2 } from 'lucide-react';
import { postJson, request, sendJson } from './api.js';
import { Button } from './ui.jsx';

const BASE = '/api/setup';
const calendarTypes = { icloud: 'iCloud', google: 'Google', outlook: 'Outlook', ics: 'ICS 订阅' };
const credentials = {
  icloud: [['apple_id', 'Apple ID'], ['app_password', 'App 专用密码']],
  google: [['client_id', 'Client ID'], ['client_secret', 'Client Secret'], ['refresh_token', 'Refresh Token']],
  outlook: [['client_id', 'Client ID'], ['client_secret', 'Client Secret'], ['refresh_token', 'Refresh Token']],
  ics: [['url', '订阅地址']],
};

function SetupField({ label, secret, configured, ...props }) {
  return <label className="host-setup-field">
    <span>{label}</span>
    <input {...props} type={secret ? 'password' : props.type}
      autoComplete={secret ? 'off' : props.autoComplete}
      placeholder={secret && configured ? '••••••••（已配置）' : props.placeholder} />
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
    }, '设置已保存');
  }}>
    <h3>{title}</h3>
    {children}
    <div className="host-setup-actions">
      <Button type="submit" disabled={action.busy}>保存</Button>
      {onTest && <Button onClick={() => action.run(onTest, '测试推送已发送')} disabled={action.busy}>发送测试推送</Button>}
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
  const label = !state ? '正在读取状态…'
    : !state.installed ? '本机未安装 Tailscale'
      : !state.logged_in ? '请在 Tailscale 客户端登录并连接'
        : state.serve_enabled ? 'HTTPS 已开启' : 'Tailscale 已连接，HTTPS 未开启';
  return <section className="settings-subsection host-setup-section">
    <h3>Tailscale 远程访问</h3>
    <p>{label}</p>
    {state?.dns_name && <p className="muted">{state.https_url || state.dns_name}</p>}
    {!!state?.ip_addresses?.length && <p className="muted">{state.ip_addresses.join(' · ')}</p>}
    {state?.blocked && <p className="feedback error">HTTPS 根路径已被其他 Tailscale 规则占用。</p>}
    <div className="host-setup-actions">
      <Button onClick={toggle} disabled={action.busy || !state?.logged_in || !state?.dns_name || state?.blocked || !!state?.error}>
        {state?.serve_enabled ? '关闭 Serve HTTPS' : '开启 Serve HTTPS'}
      </Button>
      <Button disabled={action.busy} onClick={() => action.run(async () => setState(await request(`${BASE}/tailscale`)))}>刷新状态</Button>
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
    }, '日历源已保存');
  };
  return <form className="settings-subsection host-setup-section" onSubmit={submit}>
    <h3>日历源</h3>
    {sources.map((source, index) => <fieldset className="host-calendar-source" key={source.id} disabled={action.busy}>
      <legend>{source.name || `日历源 ${index + 1}`}</legend>
      <div className="host-setup-grid">
        <SetupField label="名称" value={source.name} required maxLength={80} onChange={(event) => update(source.id, { name: event.target.value })} />
        <label className="host-setup-field"><span>类型</span>
          <select value={source.type} disabled={config.sources.some(({ id }) => id === source.id)}
            onChange={(event) => update(source.id, { type: event.target.value })}>
            {Object.entries(calendarTypes).map(([type, label]) => <option key={type} value={type}>{label}</option>)}
          </select>
        </label>
        {credentials[source.type].map(([key, label]) => <SetupField key={key} label={label} secret
          configured={source[`has_${key}`]} value={source[key] || ''} onChange={(event) => update(source.id, { [key]: event.target.value })} />)}
        {source.type !== 'ics' && <SetupField label="日历 ID（可选）" value={source.calendar_id || ''}
          onChange={(event) => update(source.id, { calendar_id: event.target.value })} />}
        {source.type === 'outlook' && <SetupField label="租户" value={source.tenant || ''} placeholder="common"
          onChange={(event) => update(source.id, { tenant: event.target.value })} />}
      </div>
      <div className="host-setup-actions">
        <label className="host-setup-color">颜色<input type="color" value={source.color}
          onChange={(event) => update(source.id, { color: event.target.value })} /></label>
        {source.type !== 'ics' && <label><input type="checkbox" checked={source.writable}
          onChange={(event) => update(source.id, { writable: event.target.checked })} /> 允许写入</label>}
        <Button aria-label={`移除${source.name || '日历源'}`} onClick={() =>
          setSources((current) => current.filter(({ id }) => id !== source.id))}><Trash2 size={16} />移除</Button>
      </div>
    </fieldset>)}
    <div className="host-setup-actions">
      <Button onClick={add} disabled={sources.length >= 16 || action.busy}><Plus size={16} />添加日历源</Button>
      <Button type="submit" disabled={action.busy}>保存日历源</Button>
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
  }, regenerate ? '配对令牌已生成' : '');
  const copy = () => action.run(async () => {
    try {
      await navigator.clipboard.writeText(token);
      return '配对令牌已复制';
    } catch {
      input.current?.focus();
      input.current?.select();
      return '已选中令牌，请手动复制';
    }
  });
  return <section className="settings-subsection host-setup-section">
    <h3>多设备配对</h3>
    <p className="muted">{config.has_token ? '已配置 Agent Token' : '生成 Agent Token 后可配对远程设备。'}</p>
    {token && <SetupField label="Agent Token" ref={input} readOnly value={token} />}
    {confirming ? <div className="host-setup-actions">
      <span>重启后，已配对设备须更新为新令牌。</span>
      <Button disabled={action.busy} onClick={() => tokenRequest(true)}>确认重新生成</Button>
      <Button disabled={action.busy} onClick={() => setConfirming(false)}>取消</Button>
    </div> : <div className="host-setup-actions">
      {config.has_token && <Button disabled={action.busy} onClick={() => token ? setToken('') : tokenRequest(false)}>{token ? '隐藏令牌' : '查看令牌'}</Button>}
      {token && <Button disabled={action.busy} onClick={copy}><Copy size={16} />复制</Button>}
      <Button disabled={action.busy} onClick={() => config.has_token ? setConfirming(true) : tokenRequest(true)}>
        {config.has_token ? '重新生成令牌' : '生成令牌'}
      </Button>
    </div>}
    <Feedback {...action} />
  </section>;
}

function WeatherSetup({ weather, timezone, save }) {
  const [fields, setFields] = useState({ ...weather, timezone });
  const [matches, setMatches] = useState(null);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState('');
  const lookup = useRef(null);
  useEffect(() => () => lookup.current?.abort(), []);

  function edit(key, value) {
    setFields((current) => ({ ...current, [key]: value }));
    if (key === 'city_name') {
      lookup.current?.abort();
      setMatches(null);
      setSearchError('');
      setSearching(false);
    }
  }

  async function search() {
    const query = fields.city_name.trim();
    if (!query || searching) return;
    lookup.current?.abort();
    const controller = new AbortController();
    lookup.current = controller;
    setSearching(true);
    setMatches(null);
    setSearchError('');
    try {
      const results = await request(`${BASE}/geocode?${new URLSearchParams({ q: query })}`, { signal: controller.signal });
      if (!controller.signal.aborted) setMatches(results);
    } catch (error) {
      if (!controller.signal.aborted) setSearchError(error.message);
    } finally {
      if (!controller.signal.aborted) setSearching(false);
    }
  }

  return <SetupForm title="天气与时区" onSave={(data) => save({
    weather: { city_name: data.get('city_name').trim(), latitude: Number(data.get('latitude')), longitude: Number(data.get('longitude')) },
    health: { timezone: data.get('timezone').trim() },
  })}>
    <div className="host-city-search">
      <SetupField label="城市名" name="city_name" value={fields.city_name} required maxLength={100}
        onChange={(event) => edit('city_name', event.target.value)} onKeyDown={(event) => {
          if (event.key === 'Enter' && !event.nativeEvent.isComposing) {
            event.preventDefault();
            search();
          }
        }} />
      <Button disabled={searching || !fields.city_name.trim()} onClick={search}>
        <Search size={16} />{searching ? '正在搜索…' : '搜索匹配'}
      </Button>
    </div>
    <Feedback error={searchError} notice={matches?.length === 0 ? '未找到匹配城市，可以换个名称或手动填写。' : ''} />
    {!!matches?.length && <ul className="host-city-matches" aria-label="城市匹配结果">
      {matches.map(({ name, admin1, country, timezone: cityTimezone, latitude, longitude }) => <li key={
        JSON.stringify([name, admin1, country, latitude, longitude])
      }>
        <Button onClick={() => {
          setFields({ city_name: name, latitude, longitude, timezone: cityTimezone });
          setMatches(null);
        }}>
          {[name, admin1, country, cityTimezone].filter(Boolean).join(' · ')}
        </Button>
      </li>)}
    </ul>}
    <div className="host-setup-grid">
      <SetupField label="IANA 时区" name="timezone" value={fields.timezone} required onChange={(event) => edit('timezone', event.target.value)} />
      <SetupField label="纬度" name="latitude" type="number" min="-90" max="90" step="any" value={fields.latitude} required
        onChange={(event) => edit('latitude', event.target.value)} />
      <SetupField label="经度" name="longitude" type="number" min="-180" max="180" step="any" value={fields.longitude} required
        onChange={(event) => edit('longitude', event.target.value)} />
    </div>
  </SetupForm>;
}

export default function HostSetup({ config, onConfig }) {
  const [barkEnabled, setBarkEnabled] = useState(config.webhook.kind === 'bark');
  async function save(payload) {
    const result = await sendJson('PATCH', `${BASE}/config`, payload);
    onConfig(result);
    return result;
  }
  return <div className="settings-fields host-setup">
    {config.restart_required && <p className="feedback" role="status">设置已保存。请在托盘中重启服务，使新配置生效。</p>}
    <TailscaleSetup />
    <SetupForm title="AI 助手" onSave={(data) => save({ ai: Object.fromEntries(data) })}>
      <div className="host-setup-grid">
        <SetupField label="模型" name="model" defaultValue={config.ai.model} required maxLength={100} />
        <SetupField label="OpenAI 服务地址" name="base_url" type="url" defaultValue={config.ai.base_url} required />
        <SetupField label="OpenAI API Key" name="api_key" secret configured={config.ai.has_api_key} />
      </div>
    </SetupForm>
    <WeatherSetup weather={config.weather} timezone={config.health.timezone} save={save} />
    <SetupForm title="Bark 推送" onTest={() => postJson(`${BASE}/webhook/test`, {})}
      onSave={(data) => save({ webhook: {
        kind: barkEnabled ? 'bark' : config.webhook.kind === 'json' ? 'json' : 'disabled',
        device_key: data.get('device_key'), daily_briefing_time: data.get('daily_briefing_time') || null,
      } })}>
      <label className="settings-row"><span>启用 Bark</span>
        <input type="checkbox" role="switch" className="toggle-switch" checked={barkEnabled}
          onChange={(event) => setBarkEnabled(event.target.checked)} />
      </label>
      <div className="host-setup-grid">
        <SetupField label="Device Key" name="device_key" secret configured={config.webhook.has_key} />
        <SetupField label="每日简报推送时间" name="daily_briefing_time" type="time" defaultValue={config.webhook.daily_briefing_time || ''} />
      </div>
    </SetupForm>
    <CalendarSetup config={config.calendar} save={save} />
    <AgentSetup config={config.agents} onChange={(restart_required) =>
      onConfig((previous) => ({ ...previous, restart_required: previous.restart_required || restart_required, agents: { has_token: true } }))} />
  </div>;
}
