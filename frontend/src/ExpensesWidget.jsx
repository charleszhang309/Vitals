import { getLocale, localizedDate, localizedNumber, t } from './i18n/messages.js';
import { useState } from 'react';
import { Pencil, Plus, Trash2 } from 'lucide-react';
import { request, sendJson } from './api.js';
import { useI18n } from './i18n/index.jsx';
import { Button, IconButton, Segmented } from './ui.jsx';
import useResource from './useOverviewResource.js';
import './expensesWidget.css';

const tabs = [{ value: 'receipts', get label() { return t('本月账单'); } }, { value: 'subscriptions', get label() { return t('订阅'); } }];
const cycles = { get month() { return t('每月'); }, get year() { return t('每年'); } };
const dueMonthDay = localizedDate({ month: 'short', day: 'numeric', timeZone: 'UTC' });
const dueDate = localizedDate({ year: 'numeric', month: 'short', day: 'numeric', timeZone: 'UTC' });
const percentFormat = localizedNumber({ style: 'percent', maximumFractionDigits: 1 });
const countFormat = localizedNumber({ maximumFractionDigits: 0 });

function displayAmount(amount) {
  if (amount == null) return '—';
  const digits = amount.split('.')[1]?.length ?? 0;
  return localizedNumber({ minimumFractionDigits: digits, maximumFractionDigits: digits }).format(amount);
}

function BudgetProgress({ budget, currency, partial }) {
  const { t } = useI18n();
  const percent = budget.used_percent;
  const tone = percent >= 100 ? 'limit' : percent >= 80 ? 'warning' : 'normal';
  const percentage = percent === null ? null : percentFormat.format(percent / 100);
  const over = budget.remaining?.startsWith('-');
  return <div className="bills-budget" data-tone={tone}>
    <p className="bills-budget-line"><span>{t('月度预算')}</span>
      <strong className="numeric">{displayAmount(budget.amount)} {currency}</strong></p>
    {percent !== null && <>
      <progress className="bills-budget-progress" max="100" value={Math.min(percent, 100)}
        aria-label={t('月度预算使用率')} aria-valuetext={percentage} />
      <p className="bills-budget-line bills-budget-status"><span>
        {t(over ? '超支 {amount} {currency}' : '剩余 {amount} {currency}', {
          amount: displayAmount(over ? budget.remaining.slice(1) : budget.remaining), currency,
        })}
      </span><span className="numeric">{percentage}</span></p>
    </>}
    {partial && percent !== null && <p className="widget-muted">{t('按已换算金额计算')}</p>}
  </div>;
}

function BudgetEditor({ amount, currency, refresh }) {
  const { t } = useI18n();
  const [draft, setDraft] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function save(value) {
    setBusy(true);
    setError('');
    try {
      await sendJson('PUT', '/api/expenses/budget', { amount: value });
      setDraft(null);
      refresh();
    } catch (failure) {
      setError(failure.message);
    } finally {
      setBusy(false);
    }
  }

  if (draft === null) return <div><Button onClick={() => setDraft(amount ?? '')}>
    {amount ? t('编辑预算') : t('设置预算')}
  </Button></div>;

  return <form className="widget-form bills-budget-editor" onSubmit={(event) => {
    event.preventDefault();
    if (draft.trim()) save(draft.trim());
  }}>
    <label>{t('月度预算金额（{currency}）', { currency })}
      <input autoFocus required inputMode="decimal" maxLength={32} value={draft} disabled={busy}
        onChange={(event) => setDraft(event.target.value)} />
    </label>
    <div className="widget-actions">
      <Button type="submit" disabled={busy || !draft.trim()}>{t('保存预算')}</Button>
      <Button disabled={busy} onClick={() => { setError(''); setDraft(null); }}>{t('取消')}</Button>
      {amount && <Button disabled={busy} onClick={() => save(null)}>{t('移除预算')}</Button>}
    </div>
    {error && <p className="feedback error" role="alert">{t(error)}</p>}
  </form>;
}

function MonthlyTotal({ data }) {
  return <div className="bills-monthly">
    <p className="widget-muted">{t('本月支出')}</p>
    <p className="widget-value numeric">{displayAmount(data.month_total)} <small>{data.base_currency}</small></p>
    <p className="widget-muted">{t('{p1} 笔账单', { p1: countFormat.format(data.monthly_count) })}{data.missing_rates.length > 0 && <span className="bills-missing">{t('· 部分汇率不可用')}</span>}
    </p>
    {data.monthly_budget && <BudgetProgress budget={data.monthly_budget} currency={data.base_currency}
      partial={data.missing_rates.length > 0} />}
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
          <small className="numeric">{subscription ? dueMonthDay.format(new Date(item.next_billing_date)) : `${displayAmount(item.amount)} ${item.currency}`}</small>
        </li>;
      })}
    </ul> : <p className="widget-muted">{t('暂无')}</p>}
  </div>;
}

export function ExpensesPreview({ resource, wide }) {
  const { data, error } = resource;
  if (!data) return <p className="widget-muted">{error ? t('账单暂不可用') : t('正在读取账单…')}</p>;
  const latest = data.recent_expenses[0];
  const next = data.upcoming_bills[0];
  if (wide) return <div className="preview-split bills-preview-split">
    <div className="preview-primary"><MonthlyTotal data={data} /></div>
    <div className="preview-secondary bills-preview-lists">
      <PreviewRows title={t('最近交易')} items={data.recent_expenses.slice(0, 2)} />
      <PreviewRows title={t('即将续费')} items={data.upcoming_bills.slice(0, 2)} subscription />
    </div>
  </div>;
  return <div className="bills-compact">
    <MonthlyTotal data={data} />
    {latest && <p className="bills-compact-line widget-truncate" title={latest.merchant}>
      <span className="widget-muted">{t('最近交易')} </span>{latest.merchant} · <span className="numeric">{displayAmount(latest.amount)} {latest.currency}</span>
    </p>}
    {!latest && next && <p className="bills-compact-line widget-truncate" title={next.name}>
      <span className="widget-muted">{t('即将续费')} </span>{next.name} · <span className="numeric">{dueMonthDay.format(new Date(next.next_billing_date))}</span>
    </p>}
    {!latest && !next && <p className="widget-muted">{t('暂无交易或待续费订阅')}</p>}
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
  const heading = subscription ? (draft.id ? t('编辑订阅') : t('新增订阅')) : (draft.id ? t('编辑账单') : t('新增账单'));
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
    <h3>{heading}</h3>
    <label>{subscription ? t('订阅名称') : t('商户')}<input autoFocus required maxLength={200} value={draft[name]} onChange={set(name)} /></label>
    <div className="widget-form-pair">
      <label>{t('金额')}<input required inputMode="decimal" maxLength={32} value={draft.amount} onChange={set('amount')} /></label>
      <label>{t('币种')}<input required list="bills-currencies" pattern="[A-Za-z]{3}" maxLength={3} autoCapitalize="characters"
        value={draft.currency} onChange={(event) => setDraft((current) => ({ ...current, currency: event.target.value.toUpperCase() }))} />
        <datalist id="bills-currencies">{currencies.map((code) => <option value={code} key={code} />)}</datalist>
      </label>
    </div>
    {subscription ? <>
      <div className="widget-form-pair">
        <label>{t('续费周期')}<select value={draft.cycle} onChange={set('cycle')}>
          {Object.entries(cycles).map(([key, text]) => <option value={key} key={key}>{text}</option>)}
        </select></label>
        <label>{t('下次续费日期')}<input required type="date" value={draft.next_billing_date ?? ''} onChange={set('next_billing_date')} /></label>
      </div>
      <label className="widget-checkbox"><input type="checkbox" checked={draft.active}
        onChange={(event) => setDraft((current) => ({ ...current, active: event.target.checked }))} />{t('启用订阅')}</label>
    </> : <>
      <label>{t('交易时间')}<input required type="datetime-local" value={draft.timeInput} onChange={set('timeInput')} /></label>
      <details className="bills-extra-fields" open={expanded} onToggle={(event) => setExpanded(event.currentTarget.open)}>
        <summary>{t('类别、付款卡和备注')}</summary>
        <div className="widget-form-pair">
          <label>{t('类别')}<input maxLength={200} value={draft.category} onChange={set('category')} /></label>
          <label>{t('付款卡')}<input maxLength={200} value={draft.card} onChange={set('card')} /></label>
        </div>
        <label>{t('备注')}<textarea maxLength={2000} rows={2} value={draft.note} onChange={set('note')} /></label>
      </details>
    </>}
    <div className="widget-actions"><Button type="submit" disabled={busy}>{subscription ? t('保存订阅') : t('保存账单')}</Button>
      <Button disabled={busy} onClick={() => setDraft(null)}>{t('取消')}</Button></div>
  </form>;
}

function BillingList({ resource, summary, subscription }) {
  const [draft, setDraft] = useState(null);
  const [confirm, setConfirm] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const { data, retry } = resource;
  const path = subscription ? '/api/subscriptions' : '/api/expenses';
  const displayTime = localizedDate({
    timeZone: data?.time_zone, month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  });
  const emptyMessage = subscription ? (data ? t('暂无订阅') : t('正在读取订阅…'))
    : (data ? t('本月暂无账单') : t('正在读取账单…'));

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
      <Plus size={16} aria-hidden="true" />{subscription ? t('新增订阅') : t('新增账单')}
    </Button></div>}
    {(error || resource.error) && <p className="feedback error" role="alert">{error || resource.error}
      {!error && <button className="text-link" type="button" onClick={retry}>{t('重试')}</button>}
    </p>}
    {draft && <BillingEditor draft={draft} setDraft={setDraft} subscription={subscription} currencies={data.currencies} busy={busy}
      onSave={(payload) => change(() => sendJson(draft.id ? 'PATCH' : 'POST', draft.id ? `${path}/${draft.id}` : path, payload))} />}
    {data?.items.length > 0 ? <ul className="widget-detail-stack" aria-label={subscription ? t('订阅列表') : t('本月账单列表')}>
      {data.items.map((item) => {
        const name = subscription ? item.name : item.merchant;
        const metadata = subscription
          ? [cycles[item.cycle], item.active ? item.next_billing_date && t('{p1} 续费', { p1: dueDate.format(new Date(item.next_billing_date)) }) : t('已停用')]
          : [displayTime.format(new Date(item.occurred_at)), item.category, item.card, item.source === 'apple_pay' && 'Apple Pay'];
        return <li className="widget-list-row bills-record" key={item.id}>
          <div className="bills-record-body">
            <strong className="bills-record-name">{name}</strong>
            <p className="muted">{metadata.filter(Boolean).join(' · ')}</p>
            {item.note && <p className="bills-record-note muted">{item.note}</p>}
          </div>
          <strong className="bills-record-amount numeric">{displayAmount(item.amount)} <small>{item.currency}</small></strong>
          <div className="bills-record-actions">
            <IconButton disabled={busy || Boolean(draft)} label={t('编辑{p1}', { p1: name })} onClick={() => edit(item)}><Pencil size={15} aria-hidden="true" /></IconButton>
            <IconButton disabled={busy || Boolean(draft)} label={t('删除{p1}', { p1: name })} onClick={() => setConfirm(item.id)}><Trash2 size={15} aria-hidden="true" /></IconButton>
          </div>
          {confirm === item.id && <div className="widget-actions bills-confirm">
            <span>{subscription ? t('删除这项订阅？') : t('删除这项账单记录？')}</span>
            <Button disabled={busy} onClick={() => change(() => request(`${path}/${item.id}`, { method: 'DELETE' }))}>{t('确认删除')}</Button>
            <Button disabled={busy} onClick={() => setConfirm(null)}>{t('取消')}</Button>
          </div>}
        </li>;
      })}
    </ul> : !resource.error && <p className="section-empty">{emptyMessage}</p>}
  </div>;
}

export function ExpensesDetails({ resource }) {
  const [tab, setTab] = useState('receipts');
  const isSubscriptions = tab === 'subscriptions';
  const receipts = useResource('/api/expenses', 30_000, !isSubscriptions);
  const subscriptions = useResource('/api/subscriptions', 30_000, isSubscriptions);
  const { data, error, retry } = resource;
  return <div className="widget-detail-stack bills-details">
    {data && <section className="surface bills-total" aria-label={t('本月支出汇总')}>
      <MonthlyTotal data={data} />
      {data.missing_rates.length > 0 && <p className="bills-missing">{t('未计入 {p1}', { p1: new Intl.ListFormat(getLocale(), { type: 'unit', style: 'short' })
        .format(data.missing_rates.map((code) => `${displayAmount(data.totals_by_currency[code])} ${code}`)) })}
      </p>}
      <BudgetEditor amount={data.monthly_budget?.amount} currency={data.base_currency} refresh={retry} />
    </section>}
    {error && <p className="feedback error" role="alert">{t('本月汇总暂不可用')}<button className="text-link" type="button" onClick={retry}>{t('重试')}</button></p>}
    <Segmented id="bills-tab" label={t('账单与订阅')} options={tabs} value={tab} onChange={setTab} tabs />
    <div role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>
      <BillingList key={tab} resource={isSubscriptions ? subscriptions : receipts} summary={resource} subscription={isSubscriptions} />
    </div>
  </div>;
}
