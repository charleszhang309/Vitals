import { t } from './i18n/messages.js';
import { lazy, Suspense, useEffect, useState } from 'react';
import { request } from './api.js';
import { measure } from './format.js';
import { DetailValues, Segmented } from './ui.jsx';

const TrendChart = lazy(() => import('./OverviewChart.jsx'));
const recentDay = { value: '24h', get label() { return t('24小时'); } };
const recentWeek = { value: '7d', get label() { return t('7天'); } };
const recentMonth = { value: '30d', get label() { return t('30天'); } };
const heartPeriods = [recentDay, recentWeek];
const vitalPeriods = [recentWeek, recentMonth];

export default function HealthTrend({ metric, title, dark }) {
  const heart = metric === 'heart';
  const periods = heart ? heartPeriods : vitalPeriods;
  const [period, setPeriod] = useState(periods[0].value);
  const [result, setResult] = useState(null);
  const [retry, setRetry] = useState(0);
  const ready = result?.period === period ? result : null;
  const data = ready?.data;
  const unit = metric === 'oxygen' ? '%' : t('次/分');
  const digits = heart ? 0 : 1;

  useEffect(() => {
    const controller = new AbortController();
    let pending = false;
    async function load() {
      if (pending || document.hidden) return;
      pending = true;
      try {
        const data = await request(`/api/health/trend/${metric}?period=${period}`, { signal: controller.signal });
        if (!controller.signal.aborted) setResult({ period, data });
      } catch {
        if (!controller.signal.aborted) setResult((previous) => previous?.period === period && previous.data
          ? { ...previous, error: true } : { period, error: true });
      } finally {
        pending = false;
      }
    }
    load();
    const timer = window.setInterval(load, 30_000);
    document.addEventListener('visibilitychange', load);
    return () => {
      controller.abort();
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', load);
    };
  }, [metric, period, retry]);

  const stats = heart ? [[t('最低'), 'min'], [t('平均'), 'avg'], [t('最高'), 'max']] : [[t('区间最新'), 'latest'], [t('区间平均'), 'avg']];
  return <>
    <div className="panel-header health-trend-header"><h3>{title}</h3>
      <Segmented id={`health-trend-${metric}`} label={t('{p1}时间范围', { p1: title })} options={periods} value={period} onChange={setPeriod} />
    </div>
    {heart ? <dl className="heart-stats">{stats.map(([label, key]) => <div key={key}>
      <dt className="muted">{label}</dt><dd className="numeric">{measure(data?.stats[key], unit, digits)}</dd>
    </div>)}</dl> : <DetailValues items={stats.map(([label, key]) => [label, measure(data?.stats[key], unit, digits)])} />}
    {ready?.error && <p className="feedback error" role="status">{t('趋势暂不可用')}<button className="text-link" type="button"
      onClick={() => setRetry((value) => value + 1)}>{t('重试')}</button></p>}
    {data?.points.some((point) => Number.isFinite(point.avg))
      ? <Suspense fallback={<div className="chart-loading trend-plot muted">{t('正在加载图表…')}</div>}>
        <TrendChart type={metric} period={period} readings={data.points} timezone={data.timezone}
          start={Date.parse(data.start)} end={Date.parse(data.end)} dark={dark} />
      </Suspense>
      : !ready?.error && <p className="overview-chart-empty muted" role="status">{ready ? t('这个时间范围暂无数据') : t('正在读取趋势…')}</p>}
  </>;
}
