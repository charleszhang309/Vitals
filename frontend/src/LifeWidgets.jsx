import { lazy, Suspense, useState } from 'react';
import { ArrowDown, ArrowLeftRight, ArrowUp, Check, Circle, Pin, Plus, X } from 'lucide-react';
import { postJson, request, sendJson } from './api.js';
import { Button, IconButton, ProgressBar, Segmented } from './ui.jsx';
import useResource from './useOverviewResource.js';

function ReminderSource({ item }) {
  return item.external_source === 'apple_reminders' && <small className="reminder-source">提醒事项</small>;
}

export function TasksPreview({ todos, habits, wide }) {
  if (!todos.data || !habits.data) return <p className="widget-muted">{todos.error || habits.error ? '事项暂不可用' : '正在读取事项…'}</p>;
  const todoItems = todos.data.items.map((item) => ({ ...item, kind: '待办' }));
  const habitItems = habits.data.items.map((item) => ({ ...item, kind: '习惯' }));
  const items = [...todoItems, ...habitItems];
  const done = items.filter((item) => item.done).length;
  const progress = <>
    <p className="widget-value numeric">{done} <small>/ {items.length} 已完成</small></p>
    <ProgressBar label="今日完成进度" value={items.length ? done / items.length * 100 : 0} />
  </>;
  if (!wide) return <>
    {progress}
    <ul className="widget-lines">{items.filter((item) => !item.done).slice(0, 2).map((item) =>
      <li className="reminder-preview" key={`${item.kind}-${item.id}`}><span className="widget-truncate">{item.title}</span>
        <ReminderSource item={item} /></li>)}</ul>
    {items.length === done && <p className="widget-muted">{items.length ? '今日事项已完成' : '今日暂无事项'}</p>}
  </>;

  const recent = Array.from({ length: Math.max(todoItems.length, habitItems.length) }, (_, index) =>
    [todoItems[index], habitItems[index]]).flat().filter(Boolean)
    .sort((left, right) => Number(left.done) - Number(right.done)).slice(0, 4);
  return <div className="preview-split tasks-preview-split">
    <div className="preview-primary">{progress}</div>
    {recent.length ? <ul className="preview-secondary preview-rows tasks-preview-rows" aria-label="今日待办和习惯">
      {recent.map((item) => {
        const Mark = item.done ? Check : Circle;
        return <li key={`${item.kind}-${item.id}`}>
          <Mark size={12} data-done={item.done || undefined} aria-hidden="true" />
          <span className="sr-only">{item.done ? '已完成' : '未完成'}</span>
          <span className="widget-truncate" title={item.title}>{item.title}</span>
          <small>{item.external_source === 'apple_reminders' ? '提醒事项' : item.kind}</small>
        </li>;
      })}
    </ul> : <p className="preview-secondary widget-muted">今日暂无事项</p>}
  </div>;
}

function TaskList({ title, resource, kind }) {
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [confirm, setConfirm] = useState(null);
  const { data, retry } = resource;
  const path = `/api/${kind}`;
  async function change(action, wasAdd = false) {
    setBusy(true); setError('');
    try {
      await action();
      if (wasAdd) setDraft('');
      setConfirm(null);
      retry();
    } catch (failure) { setError(failure.message); }
    finally { setBusy(false); }
  }
  return <section className="widget-detail-stack">
    <h3>{title}</h3>
    <form className="widget-inline-form" onSubmit={(event) => {
      event.preventDefault();
      if (draft.trim()) change(() => postJson(path, { title: draft }), true);
    }}>
      <input value={draft} maxLength={200} onChange={(event) => setDraft(event.target.value)} aria-label={`新增${title}`} placeholder={`新增${title}`} />
      <Button type="submit" disabled={busy || !draft.trim()}><Plus size={16} />添加</Button>
    </form>
    {(error || resource.error) && <p className="feedback error" role="alert">{error || resource.error}</p>}
    {data?.items.length ? <ul className="widget-detail-stack">
      {data.items.map((item) => <li key={item.id} className="widget-list-row task-line">
        <label className="widget-checkbox"><input type="checkbox" checked={item.done} disabled={busy}
          onChange={(event) => { const done = event.target.checked; change(() => sendJson('PUT', `${path}/${item.id}/check`, { done })); }} />
          <span>{item.title}<ReminderSource item={item} /></span></label>
        {confirm === item.id ? <div className="widget-actions">
          <Button disabled={busy} onClick={() => change(() => request(`${path}/${item.id}`, { method: 'DELETE' }))}>确认删除</Button>
          <Button disabled={busy} onClick={() => setConfirm(null)}>取消</Button>
        </div> : <IconButton label={`删除${title} ${item.title}`} disabled={busy} onClick={() => setConfirm(item.id)}><X size={17} /></IconButton>}
      </li>)}
    </ul> : <p className="section-empty">{data ? '暂无事项' : '正在读取…'}</p>}
  </section>;
}

export function TasksDetails({ todos, habits }) {
  return <div className="widget-detail-stack task-groups">
    <TaskList title="今日待办" kind="todos" resource={todos} />
    <TaskList title="微习惯" kind="habits" resource={habits} />
  </div>;
}

export function CountdownPreview({ resource, wide }) {
  const [next, ...upcoming] = resource.data?.items.filter((item) => item.days >= 0).slice(0, wide ? 4 : 1) ?? [];
  if (!next) return <p className="widget-muted">{resource.error ? '倒数日暂不可用' : resource.data ? '暂无未到期倒数日' : '正在读取倒数日…'}</p>;
  const summary = <><p className="widget-value numeric">{next.days === 0 ? '就在今天' : <>{next.days}<small> 天后</small></>}</p>
    <p className="widget-truncate" title={next.title}>{next.title}</p>
    <time className="widget-muted numeric" dateTime={next.target_date}>{next.target_date}</time></>;
  if (!upcoming.length) return summary;
  return <div className="preview-split countdown-preview-split">
    <div className="preview-primary">{summary}</div>
    <ul className="preview-secondary preview-rows" aria-label="其他未到期倒数日">{upcoming.map((item) => <li key={item.id}>
      <span className="widget-truncate" title={item.title}>{item.title}</span>
      <small className="numeric" title={item.target_date}>{item.days ? `${item.days} 天后` : '今天'}</small>
    </li>)}</ul>
  </div>;
}

export function CountdownDetails({ resource }) {
  const [draft, setDraft] = useState(null);
  const [confirm, setConfirm] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const { data, retry } = resource;
  const path = '/api/countdowns';
  const set = (key, value) => setDraft((current) => ({ ...current, [key]: value }));
  async function change(action) {
    setBusy(true); setError('');
    try { await action(); setDraft(null); setConfirm(null); retry(); }
    catch (failure) { setError(failure.message); }
    finally { setBusy(false); }
  }
  function edit(item) {
    setError(''); setConfirm(null);
    setDraft(item ?? { title: '', target_date: data?.date ?? '', note: '' });
  }
  return <div className="widget-detail-stack">
    {!draft && <div><Button onClick={() => edit(null)}><Plus size={16} />新建倒数日</Button></div>}
    {(error || resource.error) && <p className="feedback error" role="alert">{error || resource.error}</p>}
    {draft && <form className="widget-form" onSubmit={(event) => {
      event.preventDefault();
      const { id, title, target_date, note } = draft;
      change(() => sendJson(id ? 'PATCH' : 'POST', id ? `${path}/${id}` : path, { title, target_date, note }));
    }}>
      <h3>{draft.id ? '编辑倒数日' : '新建倒数日'}</h3>
      <label>标题<input required value={draft.title} maxLength={200} onChange={(event) => set('title', event.target.value)} /></label>
      <label>目标日期<input required type="date" value={draft.target_date} onChange={(event) => set('target_date', event.target.value)} /></label>
      <label>备注<textarea rows={3} maxLength={2000} value={draft.note} onChange={(event) => set('note', event.target.value)} /></label>
      <div className="widget-actions"><Button disabled={busy} type="submit">保存倒数日</Button><Button disabled={busy} onClick={() => setDraft(null)}>取消</Button></div>
    </form>}
    {[[false, '未到期'], [true, '已过去']].map(([past, label]) => {
      const items = data?.items.filter((item) => (item.days < 0) === past) ?? [];
      return <section className="widget-detail-stack" key={label}><h3>{label}</h3>
        {items.length ? items.map((item) => <article key={item.id} className="widget-list-row widget-detail-stack">
          <div className="countdown-row"><strong>{item.title}</strong><span className="muted numeric">{item.days < 0 ? `已过 ${-item.days} 天` : item.days === 0 ? '今天' : `还有 ${item.days} 天`}</span></div>
          <p className="muted numeric">{item.target_date}{item.note && ` · ${item.note}`}</p>
          <div className="widget-actions">{confirm === item.id ? <>
            <Button disabled={busy} onClick={() => change(() => request(`${path}/${item.id}`, { method: 'DELETE' }))}>确认删除</Button>
            <Button disabled={busy} onClick={() => setConfirm(null)}>取消</Button>
          </> : <><Button disabled={busy || Boolean(draft)} onClick={() => edit(item)}>编辑</Button>
            <Button disabled={busy || Boolean(draft)} onClick={() => setConfirm(item.id)}>删除</Button></>}</div>
        </article>) : <p className="section-empty">{data ? '暂无倒数日' : '正在读取…'}</p>}
      </section>;
    })}
  </div>;
}

const rateNumber = new Intl.NumberFormat('zh-CN', { maximumSignificantDigits: 6 });
const defaultPair = { base: 'CAD', target: 'CNY' };
const samePair = (left, right) => left?.base === right.base && left?.target === right.target;
const RatePlot = lazy(() => import('./RatePlot.jsx'));
const rateRanges = [{ value: '24h', label: '24小时' }, { value: '7d', label: '7天' }, { value: '30d', label: '30天' }, { value: '1y', label: '1年' }];

function RateHistory({ base, target, dark }) {
  const [range, setRange] = useState('7d');
  const query = new URLSearchParams({ base, target, range });
  const { data, error } = useResource(`/api/rates/history?${query}`, 60_000);
  const current = data?.base === base && data.target === target && data.range === range ? data : null;
  const fallback = data?.base === base && data.target === target && data.points?.length > 0 ? data : null;
  const shown = current?.points?.length > 0 ? current : (!error && !current ? fallback : null);
  const loading = !current && !error;
  const points = shown?.points.length ?? 0;
  let note = '';
  if (current?.points?.length) {
    if (range === '24h' && current.granularity === 'hourly_snapshot') note = current.points.length > 1 ? '本地小时快照 · 采样 Frankfurter 参考汇率'
      : '小时样本尚不足 · 显示最近快照';
    else note = range === '24h' ? '小时样本尚不足 · 显示最近日参考汇率' : 'Frankfurter 日参考汇率';
    if (current.status === 'stale') note += ' · 缓存';
  }
  return <section className="rate-history" aria-labelledby="rate-history-heading">
    <div className="rate-history-header"><h3 id="rate-history-heading">历史走势</h3>
      <Segmented id="rate-history-range" label="汇率时间范围" options={rateRanges} value={range} onChange={setRange} />
    </div>
    <div className={`rate-history-stage${loading && points > 0 ? ' is-loading' : ''}`}>
      {points > 0 ? <Suspense fallback={<p className="section-empty">正在加载图表…</p>}>
        <RatePlot data={shown} dark={dark} />
      </Suspense> : <p className="section-empty">{error || (current ? '该区间暂无可用汇率' : '正在读取历史汇率…')}</p>}
    </div>
    <p className="muted rate-history-note">{note || '\u00a0'}</p>
  </section>;
}

export function RatePreview({ resource, wide }) {
  const { data, error } = resource;
  const [first, ...others] = data?.pins?.slice(0, wide ? 4 : 1) ?? [];
  if (!first) return <p className="widget-muted">{data ? '暂无固定汇率' : error ? '汇率暂不可用' : '正在读取汇率…'}</p>;
  const summary = <><p className="widget-muted numeric">1 {first.base} =</p>
    <p className="widget-value numeric">{Number.isFinite(first.rate) ? rateNumber.format(first.rate) : '—'}<small> {first.target}</small></p></>;
  if (!others.length) return summary;
  return <div className="preview-split rate-preview-split">
    <div className="preview-primary">{summary}</div>
    <ul className="preview-secondary preview-rows" aria-label="其他固定汇率">{others.map((item) => <li key={`${item.base}-${item.target}`}>
      <span className="widget-truncate">{item.base} → {item.target}</span>
      <strong className="numeric">{Number.isFinite(item.rate) ? rateNumber.format(item.rate) : '—'}</strong>
    </li>)}</ul>
  </div>;
}

export function RateDetails({ resource, dark }) {
  const { data, error, retry, replace } = resource;
  const [selection, setSelection] = useState(null);
  const [entry, setEntry] = useState({ side: 'base', text: '1' });
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const pins = data?.pins ?? [];
  const pair = selection ?? pins[0] ?? defaultPair;
  const { base, target } = pair;
  const query = new URLSearchParams({ base, target });
  const quote = useResource(`/api/rates/quote?${query}`, 60_000, Boolean(data));
  const pinned = pins.find((item) => samePair(item, pair));
  const activeQuote = samePair(quote.data, pair) ? quote.data : pinned;
  const rate = activeQuote?.rate;
  const amount = Number(entry.text);
  const result = entry.side === 'base' ? amount * rate : amount / rate;
  const converted = entry.text.trim() !== '' && amount >= 0 && Number.isFinite(rate) && Number.isFinite(result)
    ? String(Number(result.toPrecision(10))) : '';

  async function change(action) {
    setBusy(true);
    setNotice('');
    setSelection({ base, target });
    try {
      const updated = await action();
      replace({ ...data, pins: updated.pins.map((item) => pins.find((old) => samePair(old, item))
        ?? (samePair(activeQuote, item) ? { ...item, ...activeQuote } : item)) });
    } catch (failure) { setNotice(failure.message); }
    finally { setBusy(false); }
  }

  function swap() {
    setSelection({ base: target, target: base });
    setEntry({ ...entry, side: entry.side === 'base' ? 'target' : 'base' });
  }

  if (!data) return <div className="widget-detail-stack"><p>{error || '正在读取汇率…'}</p>
    {error && <div><Button onClick={retry}>重试</Button></div>}</div>;

  function currencyField(side, label) {
    return <div className="rate-field">
      <label>{label}币种<select value={pair[side]} onChange={(event) => setSelection({ base, target, [side]: event.target.value })}>
        {data.currencies.map((code) => <option key={code}>{code}</option>)}
      </select></label>
      <label>{label}金额<input className="numeric" type="number" min="0" step="any" inputMode="decimal"
        value={entry.side === side ? entry.text : converted}
        onChange={(event) => setEntry({ side, text: event.target.value })} /></label>
    </div>;
  }

  return <div className="widget-detail-stack">
    <div className="widget-form rate-converter">
      {currencyField('base', '左侧')}
      <IconButton label="互换币种" onClick={swap}><ArrowLeftRight size={18} /></IconButton>
      {currencyField('target', '右侧')}
    </div>
    <div className="rate-toolbar">
      <p className="muted numeric">{Number.isFinite(rate)
        ? `1 ${base} = ${rateNumber.format(rate)} ${target}${activeQuote.status === 'stale' ? ' · 缓存' : ''}`
        : activeQuote?.status === 'unavailable' || quote.error ? '该汇率暂不可用' : '正在读取汇率…'}</p>
      <Button disabled={busy || Boolean(pinned) || base === target || pins.length >= data.max_pins}
        onClick={() => change(() => postJson('/api/rates/pins', { base, target }))}><Pin size={16} />
        {pinned ? '已固定' : pins.length >= data.max_pins ? '已达固定上限' : '固定此汇率对'}</Button>
    </div>
    {(notice || error) && <p className="feedback error" role="alert">{notice || error}</p>}
    <RateHistory base={base} target={target} dark={dark} />
    <h3>已固定 <span className="muted numeric">{pins.length} / {data.max_pins}</span></h3>
    {pins.length ? <ul className="widget-detail-stack">{pins.map((item, index) => {
      const name = `${item.base} → ${item.target}`;
      const path = `/api/rates/pins/${item.base}/${item.target}`;
      return <li className="widget-list-row rate-row" key={name}>
        <button type="button" className="rate-pair" aria-pressed={samePair(item, pair)}
          onClick={() => setSelection({ base: item.base, target: item.target })}>
          <strong>{name}</strong>
          <small className="muted numeric">{Number.isFinite(item.rate) ? rateNumber.format(item.rate) : '—'}{item.date ? ` · ${item.date}` : ''}{index === 0 && ' · 首页'}</small>
        </button>
        <div className="widget-actions">
          <IconButton label={`上移 ${name}`} disabled={busy || index === 0}
            onClick={() => change(() => sendJson('PATCH', path, { direction: 'up' }))}><ArrowUp size={15} /></IconButton>
          <IconButton label={`下移 ${name}`} disabled={busy || index === pins.length - 1}
            onClick={() => change(() => sendJson('PATCH', path, { direction: 'down' }))}><ArrowDown size={15} /></IconButton>
          <IconButton label={`取消固定 ${name}`} disabled={busy}
            onClick={() => change(() => request(path, { method: 'DELETE' }))}><X size={15} /></IconButton>
        </div>
      </li>;
    })}</ul> : <p className="section-empty">暂无固定汇率</p>}
    <p className="source-note">参考汇率 · <a href="https://frankfurter.dev/" target="_blank" rel="noopener noreferrer">Frankfurter</a></p>
  </div>;
}
