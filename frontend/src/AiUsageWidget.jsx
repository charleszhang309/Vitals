import { useId, useState } from 'react';
import { number, time } from './format.js';
import { DetailValues, Segmented } from './ui.jsx';

const weekdays = ['日', '一', '', '三', '', '五', ''];
const views = [
  { value: 'daily', label: '每天' },
  { value: 'weekly', label: '每周' },
  { value: 'cumulative', label: '累计总量' },
];
const compactNumber = new Intl.NumberFormat('zh-CN', { notation: 'compact', maximumFractionDigits: 1 });
const monthName = (date) => `${Number(date.slice(5, 7))}月`;
const tokenText = (value) => Number.isFinite(value) ? `${number(value, 0)} tokens` : '暂无数据';

export function availableUsageSources(data) {
  return data?.sources.filter((source) => source.available) ?? [];
}

function hasTokenHistory(source) {
  return source.weeks.some((week) => week.some((day) => Number.isFinite(day?.tokens)));
}

function heatLevel(tokens, peak) {
  if (tokens <= 0) return 0;
  return Math.min(5, Math.ceil(tokens / Math.max(tokens, peak) * 5));
}

function useUsagePoints(points) {
  const id = useId();
  const [active, setActive] = useState(null);
  const [hovered, setHovered] = useState(null);
  const [focused, setFocused] = useState(null);
  const [pinned, setPinned] = useState(null);
  const current = points.find((point) => point.id === (hovered ?? focused ?? pinned));
  const tabbable = points.some((point) => point.id === active) ? active : points.at(-1)?.id;

  function pointProps(point, navigate) {
    const { id: pointId, label, tokens, peak } = point;
    return {
      'data-point': pointId,
      'aria-label': `${label} · ${tokenText(tokens)}${peak ? ' · 单日峰值' : ''}`,
      'aria-pressed': pinned === pointId,
      tabIndex: tabbable === pointId ? 0 : -1,
      onPointerEnter: (event) => { if (event.pointerType === 'mouse') setHovered(pointId); },
      onPointerLeave: () => setHovered(null),
      onFocus: () => { setActive(pointId); setFocused(pointId); },
      onBlur: () => setFocused(null),
      onClick: () => {
        setActive(pointId);
        setPinned(pinned === pointId ? null : pointId);
        setFocused(pinned === pointId ? null : pointId);
      },
      onKeyDown: (event) => {
        if (event.key === 'Escape' && current) {
          event.preventDefault();
          event.stopPropagation();
          setHovered(null);
          setFocused(null);
          setPinned(null);
          return;
        }
        const next = navigate(event.key, point);
        if (!next) return;
        event.preventDefault();
        event.currentTarget.parentElement.querySelector(`[data-point="${next.id}"]`)?.focus();
      },
    };
  }

  return { current, id, pointProps };
}

function UsageTooltip({ point, id, columns }) {
  if (!point) return null;
  return <div id={id} className="usage-point-popup" role="tooltip"
    data-side={point.row >= 3 ? 'above' : 'below'}
    style={{ '--point-x': `${(point.column + 0.5) / columns * 100}%`, '--point-row': point.row }}>
    <span className="numeric">{point.label}</span>
    <strong className="numeric">{tokenText(point.tokens)}</strong>
    {point.peak && <span>单日峰值</span>}
  </div>;
}

function UsageHeatmap({ source, full = false, interactive = false, legend = false }) {
  const { weeks, peak_tokens: peak } = source;
  const days = weeks.flatMap((week, column) => week.flatMap((day, row) => day
    ? [{ ...day, id: day.date, label: day.date, column, row, peak: day.tokens > 0 && day.tokens === peak }] : []));
  const today = days.at(-1).date;
  const interaction = useUsagePoints(days);
  const firstWeekday = new Date(`${days[0].date}T00:00:00Z`).getUTCDay();
  const months = new Map();
  days.forEach((day, index) => {
    const month = day.date.slice(0, 7);
    if (month !== days[index - 1]?.date.slice(0, 7)) months.set(day.column, monthName(day.date));
  });

  function navigate(key, { column, row }) {
    if (key === 'Home') return days[0];
    if (key === 'End') return days.at(-1);
    const offset = { ArrowUp: -1, ArrowDown: 1, ArrowLeft: -7, ArrowRight: 7 }[key];
    if (offset === undefined) return undefined;
    const index = column * 7 + row + offset;
    return days.find((day) => day.column * 7 + day.row === index);
  }

  return <figure className={`usage-heatmap usage-heatmap-v2${full ? ' usage-heatmap-full' : ''}`}
    aria-label={`${source.name}近 12 周每日 Token 用量`}>
    {full && <div className="usage-weekdays" aria-hidden="true">{Array.from({ length: 7 }, (_, index) =>
      <span key={index}>{weekdays[(firstWeekday + index) % 7]}</span>)}</div>}
    <div className="usage-heatmap-grid">
      {full && [...months].map(([column, month]) => <span key={column} className="usage-month" aria-hidden="true"
        style={{ gridColumn: column + 1 }}>{month}</span>)}
      {days.map((day) => {
        const attrs = {
          className: 'usage-day',
          'aria-current': day.date === today ? 'date' : undefined,
          'data-level': Number.isFinite(day.tokens) ? heatLevel(day.tokens, peak ?? 0) : undefined,
          'data-peak': day.peak || undefined,
          style: { gridColumn: day.column + 1, gridRow: day.row + 1 + Number(full) },
        };
        return interactive
          ? <button key={day.date} type="button" {...attrs} {...interaction.pointProps(day, navigate)}
            aria-describedby={interaction.current?.id === day.id ? interaction.id : undefined} />
          : <span key={day.date} {...attrs} aria-hidden="true" />;
      })}
      {interactive && <UsageTooltip point={interaction.current} id={interaction.id} columns={weeks.length} />}
    </div>
    {legend && <figcaption className="usage-heatmap-caption">
      {peak > 0 && <span className="usage-legend"><i className="usage-day" data-level="5" data-peak aria-hidden="true" />单日峰值</span>}
      <span className="usage-legend" aria-label="用量从零到最高"><span>0</span>
        {[0, 1, 2, 3, 4, 5].map((level) => <i key={level} className="usage-day" data-level={level} aria-hidden="true" />)}
        <span>多</span>
      </span>
    </figcaption>}
  </figure>;
}

function UsagePeriods({ source, cumulative }) {
  let running = 0;
  const points = source.weeks.flatMap((week, column) => {
    const days = week.filter(Boolean);
    const observed = days.filter((day) => Number.isFinite(day.tokens));
    if (!days.length) return [];
    const id = days[0].date;
    const label = `${id} 至 ${days.at(-1).date}`;
    const total = observed.reduce((sum, day) => sum + day.tokens, 0);
    running += total;
    return [{ id, label, column, row: 4, tokens: observed.length ? (cumulative ? running : total) : null }];
  });
  const max = Math.max(1, ...points.map((point) => point.tokens ?? 0));
  const interaction = useUsagePoints(points);
  const monthLabels = new Map(points
    .filter(({ id }, index) => id.slice(0, 7) !== points[index - 1]?.id.slice(0, 7))
    .map(({ id, column }) => [column, monthName(id)]));

  function navigate(key, point) {
    if (key === 'Home') return points[0];
    if (key === 'End') return points.at(-1);
    const offset = { ArrowLeft: -1, ArrowRight: 1 }[key];
    return offset && points[points.indexOf(point) + offset];
  }

  return <figure className="usage-periods" aria-label={`${source.name}近 12 周${cumulative ? '累计' : '每周'} Token 用量`}>
    <div className="usage-periods-axis numeric" aria-hidden="true"><span>{number(max, 0)}</span><span>0</span></div>
    <div className="usage-periods-grid">
      {points.map((point) => <button key={point.id} type="button" className="usage-period"
        style={{ gridColumn: point.column + 1 }}
        {...interaction.pointProps(point, navigate)}
        aria-describedby={interaction.current?.id === point.id ? interaction.id : undefined}>
        {Number.isFinite(point.tokens) && <span className="usage-period-bar" data-zero={!point.tokens || undefined}
          style={{ height: `${point.tokens / max * 100}%` }} aria-hidden="true" />}
      </button>)}
      {[...monthLabels].map(([column, month]) => <span key={column} className="usage-month" aria-hidden="true"
        style={{ gridColumn: column + 1 }}>{month}</span>)}
      <UsageTooltip point={interaction.current} id={interaction.id} columns={source.weeks.length} />
    </div>
  </figure>;
}

function UsageEmpty({ resource, children }) {
  return <p className="widget-muted" role="status">{resource.error ? 'AI 用量暂不可用'
    : !resource.data ? '正在读取 AI 用量…' : children}</p>;
}

function PreviewTokenCount({ value }) {
  const exact = number(value, 0);
  return <span>
    <span className="sr-only">{exact}</span>
    <span className="usage-count-full" aria-hidden="true">{exact}</span>
    <span className="usage-count-compact" aria-hidden="true">{Number.isFinite(value) ? compactNumber.format(value) : exact}</span>
  </span>;
}

export function AiUsagePreview({ resource, wide, preferredSource }) {
  const sources = availableUsageSources(resource.data);
  const source = sources.find((item) => item.id === preferredSource && hasTokenHistory(item))
    ?? sources.find(hasTokenHistory) ?? sources[0];
  if (!source) return <UsageEmpty resource={resource}>暂无可用的用量来源</UsageEmpty>;

  const showHistory = hasTokenHistory(source);
  const balances = source.balances ?? [];
  return <div className={`usage-preview${wide && showHistory ? ' preview-split' : ''}`}>
    <div className="usage-preview-summary">
      <p className="widget-muted">{source.name}{showHistory ? ' · 今日' : balances.length ? ' · 余额' : ''}</p>
      {showHistory && <p className="widget-value numeric" title={tokenText(source.today_tokens)}>
        {wide ? <PreviewTokenCount value={source.today_tokens} /> : number(source.today_tokens, 0)} <small>tokens</small>
      </p>}
      {wide && showHistory && <dl className="usage-preview-totals">
        <div><dt>12 周累计</dt><dd className="numeric" title={tokenText(source.total_tokens)}><PreviewTokenCount value={source.total_tokens} /></dd></div>
        <div><dt>单日峰值</dt><dd className="numeric" title={tokenText(source.peak_tokens)}><PreviewTokenCount value={source.peak_tokens} /></dd></div>
      </dl>}
      {!showHistory && balances.length > 0 && <p className="widget-value numeric" title={balances.map(({ amount, currency }) => `${amount} ${currency}`).join(' · ')}>
        {balances[0].amount} <small>{balances[0].currency}</small>
      </p>}
      {!showHistory && !balances.length && <p className="widget-muted">暂无用量数据</p>}
      {(source.stale || resource.error) && <p className="widget-muted">缓存数据</p>}
    </div>
    {showHistory && <UsageHeatmap key={`${source.id}-${wide}`} source={source} full={wide} interactive={wide} />}
  </div>;
}

export function AiUsageDetails({ resource, preferredSource, onSource, readAt }) {
  const [view, setView] = useState('daily');
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
      <div className="usage-view-heading">
        <Segmented id="ai-usage-view" label="用量视图" options={views} value={view} onChange={setView} />
        <p className="widget-muted numeric">{days[0].date} 至 {days.at(-1).date}{source.timezone && ` · ${source.timezone}`}</p>
      </div>
      {view === 'daily'
        ? <UsageHeatmap key={source.id} source={source} full interactive legend />
        : <UsagePeriods key={`${source.id}-${view}`} source={source} cumulative={view === 'cumulative'} />}
      <DetailValues items={[
        ['今日用量', tokenText(source.today_tokens)],
        ['累计总量', tokenText(source.total_tokens)],
        [source.updated_at ? '更新时间' : '最近读取', time(source.updated_at ?? readAt, true)],
      ]} />
    </> : <p className="section-empty">{balances.length ? '此来源仅提供余额' : '暂无 Token 用量数据'}</p>}
    {balances.length > 0 && <DetailValues items={balances.map(({ currency, amount }) => [`余额（${currency}）`, amount])} />}
    {(source.stale || resource.error) && <p className="widget-muted" role="status">正在显示缓存数据</p>}
  </div>;
}
