import { useEffect, useId, useRef, useState } from 'react';
import { Clipboard, Download, Settings2, Trash2 } from 'lucide-react';
import { request } from './api.js';
import { getLocale, t } from './i18n/messages.js';
import { Button, IconButton, Modal } from './ui.jsx';
import './deckPlugins.css';

const pluginsPath = '/api/deck/plugins';

function localizedName(names, fallback) {
  const locale = getLocale();
  return names?.[locale] ?? names?.[locale.split('-')[0]] ?? names?.['en-US'] ?? names?.en
    ?? Object.values(names ?? {})[0] ?? fallback;
}

function pluginRequest(key, path = pluginsPath, { method = 'GET', body, signal, timeoutMs } = {}) {
  const headers = {};
  if (key) headers['X-Vitals-Deck-Key'] = key;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  return request(path, {
    method, headers, signal, timeoutMs,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

function PluginName({ plugin }) {
  return <div className="deck-plugin-name">
    <strong>{localizedName(plugin.name, plugin.id)}</strong>
    <span className="numeric">{t('版本 {p1}', { p1: plugin.version })}</span>
  </div>;
}

function ConfigField({ field, value, configured, update }) {
  const name = localizedName(field.name, field.id);
  if (field.secret) return <label className="deck-field">
    <span>{name}</span>
    <input type="password" autoComplete="new-password" value={value} required={field.required && !configured}
      placeholder={configured ? t('留空以保留原值') : undefined} onChange={(event) => update(event.target.value)} />
  </label>;

  if (field.choices || field.type === 'boolean') {
    const choices = field.choices ?? [true, false];
    const selected = choices.findIndex((choice) => choice === value);
    return <label className="deck-field">
      <span>{name}</span>
      <select value={selected < 0 ? '' : selected} required={field.required} onChange={(event) =>
        update(choices[Number(event.target.value)])}>
        <option value="" disabled>{t('请选择配置值')}</option>
        {choices.map((choice, index) => <option key={index} value={index}>
          {typeof choice === 'boolean' ? t(choice ? '开' : '关闭') : String(choice)}
        </option>)}
      </select>
    </label>;
  }

  const numeric = field.type === 'integer' || field.type === 'number';
  return <label className="deck-field">
    <span>{name}</span>
    <input type={numeric ? 'number' : 'text'} value={value} autoCapitalize="none"
      required={field.required} min={field.minimum} max={field.maximum}
      step={numeric ? (field.type === 'integer' ? 1 : 'any') : undefined}
      onChange={(event) => update(numeric && event.target.value !== '' ? event.target.valueAsNumber : event.target.value)} />
  </label>;
}

function PluginConfig({ plugin, id, disabled, close, save }) {
  const [draft, setDraft] = useState({});
  function submit(event) {
    event.preventDefault();
    const config = Object.fromEntries(plugin.config_schema.flatMap((field) => {
      if (!Object.hasOwn(draft, field.id)) return [];
      const value = draft[field.id];
      if (value === '' && (field.secret || field.type !== 'string')) return [];
      return [[field.id, value]];
    }));
    if (Object.keys(config).length) save(config);
    else close();
  }
  return <form id={id} className="deck-plugin-panel" onSubmit={submit}>
    <fieldset disabled={disabled} className="deck-plugin-form">
      <div className="deck-plugin-fields">
        {plugin.config_schema.map((field) => {
          const saved = !field.secret && Object.hasOwn(plugin.config, field.id) ? plugin.config[field.id] : field.default;
          const value = Object.hasOwn(draft, field.id) ? draft[field.id] : saved;
          return <ConfigField key={field.id} field={field} value={value ?? ''}
            configured={plugin.configured_secrets.includes(field.id)}
            update={(next) => setDraft((current) => ({ ...current, [field.id]: next }))} />;
        })}
      </div>
      <div className="deck-plugin-actions">
        <Button onClick={close}>{t('取消')}</Button>
        <Button className="primary-button" type="submit">{t('保存')}</Button>
      </div>
    </fieldset>
  </form>;
}

function GitHubInstall({ disabled, install }) {
  const [url, setUrl] = useState('');
  const [reviewUrl, setReviewUrl] = useState('');
  return reviewUrl ? <div className="deck-plugin-form">
    <div className="deck-plugin-warning">
      <p>{t('Deck 插件代码在 Vitals 主机上运行。请仅安装您信任的来源。')}</p>
      <code>{reviewUrl}</code>
    </div>
    <div className="deck-plugin-actions">
      <Button disabled={disabled} onClick={() => setReviewUrl('')}>{t('取消')}</Button>
      <Button className="primary-button" disabled={disabled} onClick={async () => {
        if (await install({ url: reviewUrl, trust: true })) {
          setReviewUrl('');
          setUrl('');
        }
      }}>{t('信任并安装')}</Button>
    </div>
  </div> : <form className="deck-plugin-inline-form" onSubmit={(event) => {
    event.preventDefault();
    setReviewUrl(url.trim());
  }}>
    <label className="deck-field">
      <span>{t('GitHub ZIP 下载链接')}</span>
      <input type="url" required autoCapitalize="none" spellCheck={false} value={url} disabled={disabled}
        onChange={(event) => setUrl(event.target.value)} />
    </label>
    <Button type="submit" disabled={disabled}>{t('检查安装')}</Button>
  </form>;
}

export default function DeckPlugins({ close, initialKey, rememberKey, onChange }) {
  const id = useId();
  const key = useRef(initialKey);
  const [data, setData] = useState(null);
  const [loadError, setLoadError] = useState('');
  const [reload, setReload] = useState(0);
  const [pending, setPending] = useState('');
  const [message, setMessage] = useState(null);
  const [keyInput, setKeyInput] = useState('');
  const [copyFallback, setCopyFallback] = useState('');
  const [expanded, setExpanded] = useState(null);
  const busy = Boolean(pending);

  useEffect(() => {
    const controller = new AbortController();
    pluginRequest(key.current, pluginsPath, { signal: controller.signal }).then((result) => {
      if (!controller.signal.aborted) setData(result);
    }).catch((error) => {
      if (!controller.signal.aborted) setLoadError(error.message);
    });
    return () => controller.abort();
  }, [reload]);

  async function perform(label, operation) {
    if (busy) return false;
    setPending(label);
    setMessage(null);
    try {
      await operation();
      return true;
    } catch (error) {
      setMessage({ text: error.message, error: true });
      return false;
    } finally {
      setPending('');
    }
  }

  async function change(method, suffix, body) {
    const installing = suffix === '/install';
    return perform(installing ? t('正在安装插件…') : t('正在更新插件…'), async () => {
      const result = await pluginRequest(key.current, pluginsPath + suffix, {
        method, body, timeoutMs: installing ? 90_000 : undefined,
      });
      setData({ ...result, can_manage: result.can_manage ?? data.can_manage });
      setMessage({ text: t(installing ? '插件已安装' : method === 'DELETE' ? '插件已卸载' : '插件已更新') });
      onChange();
    });
  }

  function authorize(event) {
    event.preventDefault();
    const candidate = keyInput.trim();
    perform(t('正在验证管理密钥…'), async () => {
      if (!/^[A-Za-z0-9_-]{1,128}$/.test(candidate)) throw new Error(t('无法使用此 Deck 插件管理密钥。'));
      const result = await pluginRequest(candidate);
      if (!result.can_manage) throw new Error(t('无法使用此 Deck 插件管理密钥。'));
      key.current = candidate;
      rememberKey(candidate);
      setKeyInput('');
      setData(result);
    });
  }

  function copyManagementKey() {
    perform(t('正在获取管理密钥…'), async () => {
      const { key: localKey } = await request('/api/setup/deck/plugins/key');
      try {
        await navigator.clipboard.writeText(localKey);
        setCopyFallback('');
        setMessage({ text: t('Deck 插件管理密钥已复制') });
      } catch {
        setCopyFallback(localKey);
      }
    });
  }

  return <Modal id={id} title={t('Deck 插件')} className="deck-plugins-dialog" onClose={() => {
    if (!busy) close();
  }}>
    <div className="deck-plugin-content">
      {(pending || message) && <p className="deck-plugin-message" data-error={message?.error || undefined}
        role={message?.error ? 'alert' : 'status'}>{pending || message.text}</p>}
      {!data && (loadError ? <div className="deck-plugin-form">
        <p className="deck-plugin-description" role="alert">{loadError}</p>
        <div className="deck-plugin-actions"><Button onClick={() => {
          setLoadError('');
          setReload((value) => value + 1);
        }}>{t('重试')}</Button></div>
      </div> : <p className="deck-plugin-description" role="status">{t('正在加载 Deck 插件…')}</p>)}
      {data && <>
        {!data.can_manage && <section className="deck-plugin-section">
          <h3>{t('远程管理')}</h3>
          <p className="deck-plugin-description">{t('请在 Vitals 主机上打开 Deck 插件并复制管理密钥。')}</p>
          <form className="deck-plugin-inline-form" onSubmit={authorize}>
            <label className="deck-field">
              <span>{t('Deck 插件管理密钥')}</span>
              <input type="password" autoComplete="new-password" required value={keyInput} disabled={busy}
                onChange={(event) => setKeyInput(event.target.value)} />
            </label>
            <Button type="submit" disabled={busy}>{t('解锁管理')}</Button>
          </form>
        </section>}
        <section className="deck-plugin-section">
          <h3>{t('已安装插件')}</h3>
          {data.installed.length ? <ul className="deck-plugin-list">
            {data.installed.map((plugin) => {
              const name = localizedName(plugin.name, plugin.id);
              const pluginPath = '/' + encodeURIComponent(plugin.id);
              const detailId = id + '-' + plugin.id;
              const showingConfig = expanded?.id === plugin.id && expanded.kind === 'config';
              const showingRemoval = expanded?.id === plugin.id && expanded.kind === 'remove';
              return <li key={plugin.id} className="deck-plugin-card">
                <div className="deck-plugin-row">
                  <PluginName plugin={plugin} />
                  <span className="deck-plugin-state" data-enabled={plugin.enabled}>{t(plugin.enabled ? '已启用' : '已停用')}</span>
                  {data.can_manage && <div className="deck-plugin-actions">
                    <Button disabled={busy} aria-label={t(plugin.enabled ? '停用 {p1}' : '启用 {p1}', { p1: name })} onClick={() =>
                      change('POST', pluginPath + '/toggle', { enabled: !plugin.enabled })}>
                      {t(plugin.enabled ? '停用插件' : '启用插件')}
                    </Button>
                    {plugin.config_schema.length > 0 && <IconButton label={t('配置 {p1}', { p1: name })} disabled={busy}
                      aria-expanded={showingConfig} aria-controls={showingConfig ? detailId : undefined}
                      onClick={() => setExpanded(showingConfig ? null : { id: plugin.id, kind: 'config' })}>
                      <Settings2 size={16} aria-hidden="true" />
                    </IconButton>}
                    {plugin.managed && <IconButton label={t('卸载 {p1}', { p1: name })} disabled={busy}
                      aria-expanded={showingRemoval} aria-controls={showingRemoval ? detailId : undefined}
                      onClick={() => setExpanded(showingRemoval ? null : { id: plugin.id, kind: 'remove' })}>
                      <Trash2 size={16} aria-hidden="true" />
                    </IconButton>}
                  </div>}
                </div>
                {showingConfig && <PluginConfig id={detailId} plugin={plugin} disabled={busy}
                  close={() => setExpanded(null)} save={async (config) => {
                    if (await change('PUT', pluginPath + '/config', { config })) setExpanded(null);
                  }} />}
                {showingRemoval && <div className="deck-plugin-panel deck-plugin-form" id={detailId}>
                  <p>{t('卸载 {p1}？', { p1: name })}</p>
                  <div className="deck-plugin-actions">
                    <Button disabled={busy} onClick={() => setExpanded(null)}>{t('取消')}</Button>
                    <Button disabled={busy} className="deck-plugin-danger" onClick={async () => {
                      if (await change('DELETE', pluginPath)) setExpanded(null);
                    }}>
                      {t('卸载插件')}
                    </Button>
                  </div>
                </div>}
              </li>;
            })}
          </ul> : <p className="deck-plugin-description">{t('暂无已安装插件。')}</p>}
        </section>
        {data.catalog.length > 0 && <section className="deck-plugin-section">
          <h3>{t('官方插件')}</h3>
          <ul className="deck-plugin-list">
            {data.catalog.map((plugin) => <li key={plugin.id} className="deck-plugin-card deck-plugin-row">
              <PluginName plugin={plugin} />
              {plugin.installed ? <span className="deck-plugin-state">{t('已安装')}</span>
                : data.can_manage && <Button disabled={busy} onClick={() => change('POST', '/install', { plugin_id: plugin.id })}>
                  <Download size={15} aria-hidden="true" />{t('安装插件')}
                </Button>}
            </li>)}
          </ul>
        </section>}
        {data.can_manage && <section className="deck-plugin-section">
          <h3>{t('从 GitHub 安装')}</h3>
          <GitHubInstall disabled={busy} install={(body) => change('POST', '/install', body)} />
        </section>}
        {data.can_manage && !key.current && <section className="deck-plugin-section">
          <h3>{t('远程管理')}</h3>
          <p className="deck-plugin-description">{t('使用管理密钥在另一台设备上管理 Deck 插件。')}</p>
          <div>
            <Button disabled={busy} onClick={copyManagementKey}>
              <Clipboard size={15} aria-hidden="true" />{t('复制管理密钥')}
            </Button>
          </div>
          {copyFallback && <label className="deck-field">
            <span>{t('手动复制管理密钥')}</span>
            <input type="text" readOnly value={copyFallback} onFocus={(event) => event.target.select()} />
          </label>}
        </section>}
      </>}
    </div>
  </Modal>;
}
