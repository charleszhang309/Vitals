import { useState } from 'react';
import { number } from './format.js';
import { DetailValues, Segmented } from './ui.jsx';

const dayNames = ['日', '一', '二', '三', '四', '五', '六'];
const monthName = (date) => `${Number(date.slice(5, 7))}月`;
const tokenText = (value) => Number.isFinite(value) ? `${number(value, 0)} tokens` : '暂无数据';
const dayText = (day) => `${day.date} · ${tokenText(day.tokens)}`;

export function availableUsageSources(data) {
  return data?.sources.filter((source) => source.available) ?? [];
}

function hasTokenHistory(source) {
  return source.weeks.some((week) => week.some((day) => Number.isFinite(day?.tokens)));
}

function heatLevel(tokens, peak) {
  if (tokens <= 0) return 0;
  return Math.min(4, Math.ceil(tokens / Math.max(tokens, peak) * 4));
}

function UsageHeatmap({ source, full = false, interactive = false }) {
  const [selectedDate, setSelectedDate] = useState(null);
  const { weeks, peak_tokens: peak } = source;
  const days = weeks.flat().filter(Boolean);
  const today = days.at(-1).date;
  const selected = days.find((day) => day.date === selectedDate)
    ?? days.find((day) => day.date === today) ?? days.at(-1);
  const weekStart = new Date(`${days[0].date}T00:00:00Z`).getUTCDay();

  function moveDay(event, column, row) {
    const step = { ArrowUp: -1, ArrowDown: 1, ArrowLeft: -7, ArrowRight: 7 }[event.key];
    if (step === undefined) return;
    event.preventDefault();
    const index = column * 7 + row + step;
    const next = weeks[Math.floor(index / 7)]?.[index % 7];
    if (next) event.currentTarget.parentElement.querySelector(`[data-date="${next.date}"]`)?.focus();
  }

  return <figure className={`usage-heatmap${full ? ' usage-heatmap-full' : ''}`} aria-label={`${source.name}近 12 周每日 Token 用量`}>
    {full && <div className="usage-weekdays" aria-hidden="true">{Array.from({ length: 7 }, (_, index) =>
      <span key={index}>{dayNames[(weekStart + index) % 7]}</span>)}</div>}
    <div className="usage-heatmap-grid">
      {weeks.flatMap((week, column) => {
        const first = week.find(Boolean);
        const previous = column > 0 && weeks[column - 1].find(Boolean);
        const showMonth = full && first && first.date.slice(0, 7) !== previous?.date?.slice(0, 7);
        return [
          ...(showMonth ? [<span key={`month-${column}`} className="usage-month" aria-hidden="true"
            style={{ gridColumn: column + 1 }}>{monthName(first.date)}</span>] : []),
          ...week.map((day, row) => {
            const style = { gridColumn: column + 1, gridRow: row + 1 + Number(full) };
            if (!day) return <span key={`empty-${column}-${row}`} className="usage-day" style={style} aria-hidden="true" />;
            const attrs = {
              className: 'usage-day',
              title: dayText(day),
              'aria-label': dayText(day),
              'aria-current': day.date === today ? 'date' : undefined,
              'data-level': Number.isFinite(day.tokens) ? heatLevel(day.tokens, peak ?? 0) : undefined,
              style,
            };
            return interactive
              ? <button key={day.date} type="button" {...attrs} data-date={day.date} tabIndex={day === selected ? 0 : -1}
                aria-pressed={day === selected} onFocus={() => setSelectedDate(day.date)}
                onClick={() => setSelectedDate(day.date)} onKeyDown={(event) => moveDay(event, column, row)} />
              : <span key={day.date} role="img" {...attrs} />;
          }),
        ];
      })}
    </div>
    {interactive && <figcaption className="usage-heatmap-caption">
      <span className="numeric" aria-live="polite">{dayText(selected)}</span>
      <span className="usage-legend" aria-label="用量由少到多"><span>少</span>
        {[0, 1, 2, 3, 4].map((level) => <i key={level} className="usage-day" data-level={level} aria-hidden="true" />)}
        <span>多</span>
      </span>
    </figcaption>}
  </figure>;
}

function UsageEmpty({ resource, children }) {
  return <p className="widget-muted" role="status">{resource.error ? 'AI 用量暂不可用'
    : !resource.data ? '正在读取 AI 用量…' : children}</p>;
}

export function AiUsagePreview({ resource, wide, preferredSource }) {
  const sources = availableUsageSources(resource.data);
  const source = sources.find((item) => item.id === preferredSource && hasTokenHistory(item))
    ?? sources.find(hasTokenHistory) ?? sources[0];
  if (!source) return <UsageEmpty resource={resource}>暂无可用的用量来源</UsageEmpty>;

  const showHistory = hasTokenHistory(source);
  const balances = source.balances ?? [];
  return <div className="usage-preview">
    <div className="usage-preview-summary">
      <p className="widget-muted">{source.name}{showHistory ? ' · 今日' : balances.length ? ' · 余额' : ''}</p>
      {showHistory && <p className="widget-value numeric">{number(source.today_tokens, 0)} <small>tokens</small></p>}
      {!showHistory && balances.length > 0 && <p className="widget-value numeric" title={balances.map(({ amount, currency }) => `${amount} ${currency}`).join(' · ')}>
        {balances[0].amount} <small>{balances[0].currency}</small>
      </p>}
      {!showHistory && !balances.length && <p className="widget-muted">暂无用量数据</p>}
      {(source.stale || resource.error) && <p className="widget-muted">缓存数据</p>}
    </div>
    {showHistory && <UsageHeatmap source={source} full={wide} />}
  </div>;
}

export function AiUsageDetails({ resource, preferredSource, onSource }) {
  const sources = availableUsageSources(resource.data);
  const source = sources.find((item) => item.id === preferredSource) ?? sources.find(hasTokenHistory) ?? sources[0];
  if (!source) return <div className="widget-detail-stack">
    <UsageEmpty resource={resource}>暂无可用的用量来源</UsageEmpty>
    {resource.error && <div><button className="text-link" type="button" onClick={resource.retry}>重试</button></div>}
  </div>;
  const balances = source.balances ?? [];
  const days = source.weeks.flat().filter(Boolean);
  return <div className="widget-detail-stack usage-details">
    <Segmented id="ai-usage-source" label="用量来源" value={source.id} onChange={onSource}
      options={sources.map(({ id, name }) => ({ value: id, label: name }))} />
    {hasTokenHistory(source) ? <>
      <DetailValues items={[
        ['今日', tokenText(source.today_tokens)],
        ['累计（近 12 周）', tokenText(source.total_tokens)],
        ['单日峰值', tokenText(source.peak_tokens)],
      ]} />
      <p className="widget-muted numeric">{days[0].date} 至 {days.at(-1).date}{source.timezone && ` · ${source.timezone}`}</p>
      <UsageHeatmap key={source.id} source={source} full interactive />
    </> : <p className="section-empty">{balances.length ? '此来源仅提供余额' : '暂无 Token 用量数据'}</p>}
    {balances.length > 0 && <DetailValues items={balances.map(({ currency, amount }) => [`余额（${currency}）`, amount])} />}
    {(source.stale || resource.error) && <p className="widget-muted" role="status">正在显示缓存数据</p>}
  </div>;
}
