import { useState } from 'react';
import { Pencil, Plus, Trash2 } from 'lucide-react';
import { request, sendJson } from './api.js';
import { Button, IconButton, Segmented } from './ui.jsx';
import useResource from './useOverviewResource.js';
import './expensesWidget.css';

const tabs = [{ value: 'receipts', label: '本月账单' }, { value: 'subscriptions', label: '订阅' }];
const cycles = { month: '每月', year: '每年' };
const amountFormats = Array.from({ length: 5 }, (_, digits) => new Intl.NumberFormat('zh-CN', {
  minimumFractionDigits: digits, maximumFractionDigits: digits,
}));

function displayAmount(amount) {
  if (amount == null) return '—';
  const digits = amount.split('.')[1]?.length ?? 0;
  return amountFormats[digits].format(amount);
}

function MonthlyTotal({ data }) {
  return <div className="bills-monthly">
    <p className="widget-muted">本月支出</p>
    <p className="widget-value numeric">{displayAmount(data.month_total)} <small>{data.base_currency}</small></p>
    <p className="widget-muted">{data.monthly_count} 笔账单
      {data.missing_rates.length > 0 && <span className="bills-missing"> · 部分汇率不可用</span>}
    </p>
  </div>;
}

function PreviewRows({ title, items, subscription = false }) {
  return <div className="bills-preview-group">
    <p className="widget-muted">{title}</p>
    {items.length > 0 ? <ul className="preview-rows" aria-label={title}>
      {items.map((item) => {
        const label = subscription ? item.name : item.merchant;
        return <li key={item.id}>
          <span className="widget-truncate" title={label}>{label}</span>
          <small className="numeric">{subscription ? item.next_billing_date.slice(5) : `${displayAmount(item.amount)} ${item.currency}`}</small>
        </li>;
      })}
    </ul> : <p className="widget-muted">暂无</p>}
  </div>;
}

export function ExpensesPreview({ resource, wide }) {
  const { data, error } = resource;
  if (!data) return <p className="widget-muted">{error ? '账单暂不可用' : '正在读取账单…'}</p>;
  const latest = data.recent_expenses[0];
  const next = data.upcoming_bills[0];
  if (wide) return <div className="preview-split bills-preview-split">
    <div className="preview-primary"><MonthlyTotal data={data} /></div>
    <div className="preview-secondary bills-preview-lists">
      <PreviewRows title="最近交易" items={data.recent_expenses.slice(0, 2)} />
      <PreviewRows title="即将续费" items={data.upcoming_bills.slice(0, 2)} subscription />
    </div>
  </div>;
  return <div className="bills-compact">
    <MonthlyTotal data={data} />
    {latest && <p className="bills-compact-line widget-truncate" title={latest.merchant}>
      <span className="widget-muted">最近交易 </span>{latest.merchant} · <span className="numeric">{displayAmount(latest.amount)} {latest.currency}</span>
    </p>}
    {!latest && next && <p className="bills-compact-line widget-truncate" title={next.name}>
      <span className="widget-muted">即将续费 </span>{next.name} · <span className="numeric">{next.next_billing_date.slice(5)}</span>
    </p>}
    {!latest && !next && <p className="widget-muted">暂无交易或待续费订阅</p>}
  </div>;
}

function localInputTime(value) {
  const moment = value ? new Date(value) : new Date();
  return new Date(moment.getTime() - moment.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
}

function BillingEditor({ draft, setDraft, subscription, currencies, busy, onSave }) {
  const [expanded, setExpanded] = useState(Boolean(draft.category || draft.card || draft.note));
  const set = (key) => (event) => setDraft((current) => ({ ...current, [key]: event.target.value }));
  const name = subscription ? 'name' : 'merchant';
  const label = subscription ? '订阅' : '账单';
  return <form className="surface widget-form bills-editor" onSubmit={(event) => {
    event.preventDefault();
    const { amount, currency } = draft;
    if (subscription) {
      onSave({ name: draft.name, amount, currency, cycle: draft.cycle, next_billing_date: draft.next_billing_date, active: draft.active });
    } else {
      const { merchant, card, category, note, timeInput, occurred_at: originalTime } = draft;
      const occurred_at = originalTime && timeInput === localInputTime(originalTime) ? originalTime : new Date(timeInput).toISOString();
      onSave({ merchant, amount, currency, occurred_at, card, category, note });
    }
  }}>
    <h3>{draft.id ? '编辑' : '新增'}{label}</h3>
    <label>{subscription ? '订阅名称' : '商户'}<input autoFocus required maxLength={200} value={draft[name]} onChange={set(name)} /></label>
    <div className="widget-form-pair">
      <label>金额<input required inputMode="decimal" maxLength={32} value={draft.amount} onChange={set('amount')} /></label>
      <label>币种<input required list="bills-currencies" pattern="[A-Za-z]{3}" maxLength={3} autoCapitalize="characters"
        value={draft.currency} onChange={(event) => setDraft((current) => ({ ...current, currency: event.target.value.toUpperCase() }))} />
        <datalist id="bills-currencies">{currencies.map((code) => <option value={code} key={code} />)}</datalist>
      </label>
    </div>
    {subscription ? <>
      <div className="widget-form-pair">
        <label>续费周期<select value={draft.cycle} onChange={set('cycle')}>
          {Object.entries(cycles).map(([key, text]) => <option value={key} key={key}>{text}</option>)}
        </select></label>
        <label>下次续费日期<input required type="date" value={draft.next_billing_date ?? ''} onChange={set('next_billing_date')} /></label>
      </div>
      <label className="widget-checkbox"><input type="checkbox" checked={draft.active}
        onChange={(event) => setDraft((current) => ({ ...current, active: event.target.checked }))} />启用订阅</label>
    </> : <>
      <label>交易时间<input required type="datetime-local" value={draft.timeInput} onChange={set('timeInput')} /></label>
      <details className="bills-extra-fields" open={expanded} onToggle={(event) => setExpanded(event.currentTarget.open)}>
        <summary>类别、付款卡和备注</summary>
        <div className="widget-form-pair">
          <label>类别<input maxLength={200} value={draft.category} onChange={set('category')} /></label>
          <label>付款卡<input maxLength={200} value={draft.card} onChange={set('card')} /></label>
        </div>
        <label>备注<textarea maxLength={2000} rows={2} value={draft.note} onChange={set('note')} /></label>
      </details>
    </>}
    <div className="widget-actions"><Button type="submit" disabled={busy}>保存{label}</Button>
      <Button disabled={busy} onClick={() => setDraft(null)}>取消</Button></div>
  </form>;
}

function BillingList({ resource, summary, subscription }) {
  const [draft, setDraft] = useState(null);
  const [confirm, setConfirm] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const { data, retry } = resource;
  const path = subscription ? '/api/subscriptions' : '/api/expenses';
  const label = subscription ? '订阅' : '账单';
  const displayTime = new Intl.DateTimeFormat('zh-CN', {
    timeZone: data?.time_zone, month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  });

  function edit(item) {
    setError('');
    setConfirm(null);
    const payment = { amount: '', currency: data.base_currency };
    if (subscription) {
      setDraft(item ?? { ...payment, name: '', cycle: 'month', next_billing_date: data.date, active: true });
    } else {
      const receipt = item ?? { ...payment, merchant: '', card: '', category: '', note: '' };
      setDraft({ ...receipt, timeInput: localInputTime(receipt.occurred_at) });
    }
  }

  async function change(action) {
    setBusy(true);
    setError('');
    try {
      await action();
      setConfirm(null);
      setDraft(null);
      retry();
      summary.retry();
    } catch (failure) {
      setError(failure.message);
    } finally {
      setBusy(false);
    }
  }

  return <div className="widget-detail-stack">
    {data && !draft && <div><Button disabled={busy} onClick={() => edit(null)}>
      <Plus size={16} aria-hidden="true" />新增{label}
    </Button></div>}
    {(error || resource.error) && <p className="feedback error" role="alert">{error || resource.error}
      {!error && <button className="text-link" type="button" onClick={retry}>重试</button>}
    </p>}
    {draft && <BillingEditor draft={draft} setDraft={setDraft} subscription={subscription} currencies={data.currencies} busy={busy}
      onSave={(payload) => change(() => sendJson(draft.id ? 'PATCH' : 'POST', draft.id ? `${path}/${draft.id}` : path, payload))} />}
    {data?.items.length > 0 ? <ul className="widget-detail-stack" aria-label={subscription ? '订阅列表' : '本月账单列表'}>
      {data.items.map((item) => {
        const name = subscription ? item.name : item.merchant;
        const metadata = subscription
          ? [cycles[item.cycle], item.active ? item.next_billing_date && `${item.next_billing_date} 续费` : '已停用']
          : [displayTime.format(new Date(item.occurred_at)), item.category, item.card, item.source === 'apple_pay' && 'Apple Pay'];
        return <li className="widget-list-row bills-record" key={item.id}>
          <div className="bills-record-body">
            <strong className="bills-record-name">{name}</strong>
            <p className="muted">{metadata.filter(Boolean).join(' · ')}</p>
            {item.note && <p className="bills-record-note muted">{item.note}</p>}
          </div>
          <strong className="bills-record-amount numeric">{displayAmount(item.amount)} <small>{item.currency}</small></strong>
          <div className="bills-record-actions">
            <IconButton disabled={busy || Boolean(draft)} label={`编辑${name}`} onClick={() => edit(item)}><Pencil size={15} aria-hidden="true" /></IconButton>
            <IconButton disabled={busy || Boolean(draft)} label={`删除${name}`} onClick={() => setConfirm(item.id)}><Trash2 size={15} aria-hidden="true" /></IconButton>
          </div>
          {confirm === item.id && <div className="widget-actions bills-confirm">
            <span>删除这项{label === '账单' ? '账单记录' : label}？</span>
            <Button disabled={busy} onClick={() => change(() => request(`${path}/${item.id}`, { method: 'DELETE' }))}>确认删除</Button>
            <Button disabled={busy} onClick={() => setConfirm(null)}>取消</Button>
          </div>}
        </li>;
      })}
    </ul> : !resource.error && <p className="section-empty">{data ? `暂无${subscription ? label : `本月${label}`}` : `正在读取${label}…`}</p>}
  </div>;
}

export function ExpensesDetails({ resource }) {
  const [tab, setTab] = useState('receipts');
  const isSubscriptions = tab === 'subscriptions';
  const receipts = useResource('/api/expenses', 30_000, !isSubscriptions);
  const subscriptions = useResource('/api/subscriptions', 30_000, isSubscriptions);
  const { data, error, retry } = resource;
  return <div className="widget-detail-stack bills-details">
    {data && <section className="surface bills-total" aria-label="本月支出汇总">
      <MonthlyTotal data={data} />
      {data.missing_rates.length > 0 && <p className="bills-missing">
        未计入 {data.missing_rates.map((code) => `${displayAmount(data.totals_by_currency[code])} ${code}`).join('、')}
      </p>}
    </section>}
    {error && <p className="feedback error" role="alert">本月汇总暂不可用 <button className="text-link" type="button" onClick={retry}>重试</button></p>}
    <Segmented id="bills-tab" label="账单与订阅" options={tabs} value={tab} onChange={setTab} tabs />
    <div role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>
      <BillingList key={tab} resource={isSubscriptions ? subscriptions : receipts} summary={resource} subscription={isSubscriptions} />
    </div>
  </div>;
}
