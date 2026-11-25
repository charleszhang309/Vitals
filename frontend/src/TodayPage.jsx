import { lazy, Suspense, useEffect, useState } from 'react';
import { motion } from 'framer-motion';
import { Activity, ArrowDown, ArrowUp, ArrowUpRight, Cloud, CloudDrizzle, CloudFog, CloudLightning, CloudRain, CloudSun, Flame, Footprints, HeartPulse, MoonStar, RotateCw, Snowflake, Sparkles, Sun } from 'lucide-react';
import { MediaCover, playbackLabels } from './ControlPage.jsx';
import { request } from './api.js';
import { measure, number, sleepTime, time } from './format.js';
import { DetailLink, IconButton } from './ui.jsx';
import HealthTrend from './HealthTrend.jsx';

const OverviewChart = lazy(() => import('./OverviewChart.jsx'));
const HealthDetails = lazy(() => import('./HealthDetails.jsx'));
const modeLabels = { silent: '静音', balanced: '平衡', performance: '性能' };
const taskLabels = { idle: '空闲', running: '高负载运行中', completed: '已完工' };
const stages = [
  ['deep', '深睡'], ['rem', 'REM'], ['core', '核心'], ['asleep', '未分期'], ['awake', '清醒'],
];

function useResource(path, interval) {
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  const [version, refresh] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let pending = false;
    async function load() {
      if (pending || document.hidden) return;
      pending = true;
      try {
        const result = await request(path, { signal: controller.signal });
        if (!controller.signal.aborted) { setData(result); setError(''); }
      } catch (failure) {
        if (!controller.signal.aborted) setError(failure.message);
      } finally {
        pending = false;
      }
    }
    load();
    const timer = window.setInterval(load, interval);
    document.addEventListener('visibilitychange', load);
    return () => {
      controller.abort();
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', load);
    };
  }, [path, interval, version]);
  return { data, setData, error, retry: () => refresh((value) => value + 1) };
}

function DailyBriefing() {
  const { data, setData } = useResource('/api/ai/briefing', 300_000);
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState('');
  async function refresh() {
    setBusy(true);
    setFeedback('');
    try {
      const result = await request('/api/ai/briefing?refresh=1');
      if (result.available) setData(result);
      if (!result.available || result.refresh_failed) setFeedback('暂时无法更新简报');
    } catch {
      setFeedback('暂时无法更新简报');
    } finally {
      setBusy(false);
    }
  }
  if (!data?.available) return null;
  return <section className="surface daily-briefing" aria-label="明日香简报">
    <Sparkles className="briefing-icon" size={18} aria-hidden="true" />
    <div><p aria-live="polite">{data.content}</p>{(busy || feedback) && <p className="muted briefing-feedback" role="status">{busy ? '正在更新简报…' : feedback}</p>}</div>
    <IconButton label="刷新明日香简报" onClick={refresh} disabled={busy}><RotateCw size={15} /></IconButton>
  </section>;
}

function HostSummary({ telemetry, onOpen }) {
  const sample = telemetry.sample;
  const loads = sample?.gpus.map((gpu) => gpu.usage_percent).filter(Number.isFinite) ?? [];
  const alerts = sample?.last_alerts?.length ?? 0;
  const power = sample?.power;
  const mode = power ? modeLabels[power.mode] ?? (power.mode || '自定义') : '—';
  const task = taskLabels[sample?.task?.state] ?? '—';
  return (
    <motion.button className="surface host-strip" type="button" onClick={onOpen} whileTap={{ scale: 0.97 }}>
      <span className="host-title"><span className="metric-icon tone-blue"><Activity size={20} aria-hidden="true" /></span><strong>主机状态</strong></span>
      <span className="host-stats">
        <span><small>电源模式</small><strong>{mode}</strong></span>
        <span><small>CPU</small><strong className="numeric">{measure(sample?.cpu.usage_percent, '%', 0)}</strong></span>
        <span><small>GPU</small><strong className="numeric">{measure(loads.length ? Math.max(...loads) : null, '%', 0)}</strong></span>
        <span><small>任务与告警</small><strong className={alerts ? 'host-warning' : ''}>{task}{alerts > 0 && ` · ${alerts} 项告警`}</strong></span>
      </span>
      <span className="host-link">硬件监控<ArrowUpRight size={17} aria-hidden="true" /></span>
    </motion.button>
  );
}

function MediaSummary({ track, onOpen }) {
  return <motion.button className={`surface host-strip music-strip${track ? '' : ' music-strip-empty'}`} type="button"
    onClick={onOpen} whileTap={{ scale: 0.97 }}>
    <MediaCover track={track} compact />
    <span className="music-strip-text">
      <strong>{track ? track.title || '未命名媒体' : '当前未在播放媒体'}</strong>
      {track && <small className="muted">{[track.artist, track.player, playbackLabels[track.status]].filter(Boolean).join(' · ')}</small>}
    </span>
    <span className="host-link">控制<ArrowUpRight size={17} aria-hidden="true" /></span>
  </motion.button>;
}

function condition(code) {
  if (code === 0) return [Sun, '晴'];
  if (code === 1 || code === 2) return [CloudSun, code === 1 ? '大致晴朗' : '局部多云'];
  if (code === 3) return [Cloud, '阴'];
  if (code === 45 || code === 48) return [CloudFog, '雾'];
  if (code >= 51 && code <= 57) return [CloudDrizzle, '毛毛雨'];
  if (code >= 61 && code <= 67 || code >= 80 && code <= 82) return [CloudRain, '雨'];
  if (code >= 71 && code <= 77 || code === 85 || code === 86) return [Snowflake, '雪'];
  if (code >= 95) return [CloudLightning, '雷雨'];
  return [Cloud, '天气状况未知'];
}

function aqiWord(aqi) {
  if (!Number.isFinite(aqi)) return '';
  if (aqi <= 50) return '优';
  if (aqi <= 100) return '良';
  if (aqi <= 150) return '敏感人群不佳';
  if (aqi <= 200) return '不健康';
  if (aqi <= 300) return '很不健康';
  return '严重';
}

function ForecastDays({ days }) {
  const values = days.flatMap((day) => [day.low_c, day.high_c]).filter(Number.isFinite);
  const minimum = values.length ? Math.min(...values) : 0;
  const span = values.length ? Math.max(Math.max(...values) - minimum, 1) : 1;
  const formatter = new Intl.DateTimeFormat('zh-CN', { timeZone: 'UTC', weekday: 'short' });
  return (
    <section className="surface panel forecast-card">
      <div className="panel-header"><h3>未来 7 天</h3></div>
      <ul className="forecast-list">{days.map((day, index) => {
        const [Icon, label] = condition(day.code);
        const valid = Number.isFinite(day.low_c) && Number.isFinite(day.high_c);
        const start = valid ? (day.low_c - minimum) / span * 100 : 0;
        const width = valid ? (day.high_c - day.low_c) / span * 100 : 0;
        return <li className="forecast-row" key={day.date}>
          <span className="forecast-day">{index === 0 ? '今天' : formatter.format(new Date(`${day.date}T00:00:00Z`))}</span>
          <span className="forecast-condition" aria-label={label} title={label}><Icon size={19} aria-hidden="true" /><span>{label}</span></span>
          <span className="numeric muted">{measure(day.low_c, '°', 0)}</span>
          <span className="temperature-track" aria-hidden="true">{valid && <span style={{ left: `${start}%`, width: `max(4px, ${width}%)` }} />}</span>
          <strong className="numeric">{measure(day.high_c, '°', 0)}</strong>
        </li>;
      })}</ul>
    </section>
  );
}

function Weather({ resource, dark }) {
  const { data: reply, error, retry } = resource;
  const data = reply?.data;
  const air = reply?.air_quality;
  const aqi = air?.data?.us_aqi;
  const aqiTone = aqi <= 50 ? 'green' : aqi <= 150 ? 'orange' : 'red';
  const current = data?.current;
  const [Icon, label] = condition(current?.code);
  return (
    <section className="today-section" aria-labelledby="weather-heading">
      <div className="today-section-title"><h2 id="weather-heading">天气</h2><span className="muted">{reply?.city_name}</span></div>
      {data ? <>
        {reply.status === 'stale' && <p className="overview-notice">暂时无法更新，显示上次天气 · {time(reply.updated_at, true)}</p>}
        <div className="weather-grid">
          <section className="surface panel current-weather" aria-label="当前天气">
            <div className="panel-header"><h3>当前天气</h3>
              <span className={`aqi-badge ${Number.isFinite(aqi) ? `tone-${aqiTone}` : ''}`} title={Number.isFinite(aqi) ? aqiWord(aqi) : '暂无空气质量数据'}>
                US AQI <strong className="numeric">{number(aqi, 0)}</strong>{air?.status === 'stale' && ' · 缓存'}
              </span>
            </div>
            <div className="weather-temperature numeric">{number(current.temperature_c, 0)}{Number.isFinite(current.temperature_c) && <span>°C</span>}</div>
            <div className="weather-caption"><Icon size={19} className="weather-symbol" aria-hidden="true" /><strong>{label}</strong><span className="numeric muted">最高 {measure(current.high_c, '°', 0)} · 最低 {measure(current.low_c, '°', 0)}</span></div>
            <dl className="weather-stats">
              {[['体感', measure(current.feels_like_c, '°C', 0)], ['降水概率', measure(current.precipitation_percent, '%', 0)],
                ['湿度', measure(current.humidity_percent, '%', 0)], ['风速', measure(current.wind_kmh, 'km/h')]].map(([key, value]) => (
                <div key={key}><dt className="muted">{key}</dt><dd className="numeric">{value}</dd></div>
              ))}
            </dl>
          </section>
          <section className="surface panel"><div className="panel-header"><h3>未来 24 小时</h3><span className="muted panel-note">气温</span></div>
            {data.hourly.some((hour) => Number.isFinite(hour.temperature_c)) ? <Suspense fallback={<div className="chart-loading trend-plot muted">正在加载图表…</div>}>
              <OverviewChart type="weather" readings={data.hourly} timezone={data.timezone} dark={dark}
                start={data.hourly_start == null ? undefined : data.hourly_start * 1000} end={data.hourly_end == null ? undefined : data.hourly_end * 1000} />
            </Suspense> : <p className="section-empty">暂无逐小时数据</p>}
          </section>
          {data.daily.length > 0 && <ForecastDays days={data.daily} />}
        </div>
      </> : <div className="surface panel overview-empty">
        <Cloud size={24} aria-hidden="true" />
        <p>{reply?.status === 'unavailable' || error ? '天气暂不可用' : '正在获取天气…'}</p>
        {(error || reply?.status === 'unavailable') && <button className="text-link" type="button" onClick={retry}>重试</button>}
        {!data && Number.isFinite(aqi) && <span className="numeric">US AQI {number(aqi, 0)} · {aqiWord(aqi)}</span>}
      </div>}
      <p className="source-note">天气 <a href="https://open-meteo.com/" target="_blank" rel="noopener noreferrer">Open-Meteo</a> · 空气质量 <a href="https://atmosphere.copernicus.eu/" target="_blank" rel="noopener noreferrer">CAMS</a></p>
    </section>
  );
}

function HealthTile({ title, Icon, tone, value, unit, details, onOpen }) {
  return <motion.section className={`surface metric health-metric detail-tile tone-${tone}`} whileTap={{ scale: 0.97 }}>
    <div className="metric-header flex items-center gap-3"><span className="metric-icon"><Icon size={22} aria-hidden="true" /></span><h3>{title}</h3></div>
    <div className="metric-reading numeric">{value}{unit && value !== '—' && <span>{unit}</span>}</div>
    <dl className="metric-details">
      {details.map(([label, item]) => <div key={label} className="flex items-center gap-2"><dt>{label}</dt><dd className="numeric">{item}</dd></div>)}
    </dl>
    <DetailLink title={title} onClick={onOpen} />
  </motion.section>;
}

function VitalTile({ title, value, onOpen }) {
  return <motion.section className="surface panel detail-tile" whileTap={{ scale: 0.97 }}>
    <h3>{title}</h3><div className="health-extra-value numeric">{value}</div>
    <DetailLink title={title} onClick={onOpen} />
  </motion.section>;
}

function SleepCard({ sleep }) {
  const duration = stages.reduce((sum, [key]) => sum + (sleep?.stages[key] ?? 0), 0);
  const scale = Math.max(duration, sleep?.in_bed_minutes ?? 0);
  return <section className="surface panel sleep-card">
    <div className="panel-header"><h3>睡眠阶段</h3>{sleep?.adjusted && <span className="muted panel-note">时长已校正</span>}</div>
    {sleep ? <>
      <p className="muted sleep-summary numeric">{sleep.start && sleep.end ? `${time(sleep.start, true)} – ${time(sleep.end)}` : '最近一次睡眠'} · 在床 {sleepTime(sleep.in_bed_minutes)}</p>
      {scale > 0 && <div className="sleep-track" aria-hidden="true">{stages.map(([key]) => (sleep.stages[key] > 0 &&
        <span key={key} className={`stage-${key}`} style={{ width: `${sleep.stages[key] / scale * 100}%` }} />))}</div>}
      <dl className="sleep-legend">{stages.map(([key, label]) => <div key={key}>
        <dt><span className={`stage-${key}`} aria-hidden="true" />{label}</dt><dd className="numeric">{sleepTime(sleep.stages[key])}</dd>
      </div>)}</dl>
    </> : <p className="section-empty">暂无睡眠数据</p>}
  </section>;
}

function WeeklyComparison() {
  const { data, error } = useResource('/api/health/weekly-comparison', 60_000);
  const measures = [
    ['steps', '日均步数', '步', (value) => measure(value, '步', 0)],
    ['sleep_minutes', '平均睡眠', '分钟', sleepTime],
    ['resting_heart_rate', '平均静息心率', '次/分', (value) => measure(value, '次/分', 1)],
  ];
  const items = measures.filter(([key]) => data?.metrics[key].recent_days || data?.metrics[key].previous_days);
  function displayRange(range) {
    return `${range.start.slice(5).replace('-', '/')}–${range.end.slice(5).replace('-', '/')}`;
  }
  return <section className="surface panel weekly-card" aria-labelledby="week-comparison-heading">
    <div className="panel-header"><h3 id="week-comparison-heading">7 天对比</h3>
      {data && items.length > 0 && <span className="muted panel-note numeric">{displayRange(data.recent)} 对比 {displayRange(data.previous)}</span>}
    </div>
    {items.length ? <dl className="weekly-metrics">{items.map(([key, label, unit, format]) => {
      const { recent, previous, recent_days: recentDays, previous_days: previousDays, delta, percent_change: percent } = data.metrics[key];
      const precision = key === 'resting_heart_rate' ? 1 : 0;
      const change = Number.isFinite(delta) ? Number(delta.toFixed(precision)) : null;
      const ChangeIcon = change > 0 ? ArrowUp : ArrowDown;
      return <div className="weekly-metric" key={key}>
        <dt>{label}</dt>
        <dd className="weekly-current numeric">{format(recent)}{change !== null && <span className={`weekly-change ${change > 0 ? 'weekly-up' : change < 0 ? 'weekly-down' : 'muted'}`}>
          {change !== 0 && <ChangeIcon size={13} aria-hidden="true" />}{change > 0 ? '+' : change < 0 ? '−' : ''}{measure(Math.abs(change), unit, precision)}
          {Number.isFinite(percent) && `（${percent > 0 ? '+' : ''}${number(percent, 1)}%）`}
        </span>}</dd>
        <dd className="muted numeric">前 7 天 {format(previous)}
          {(recentDays < 7 || previousDays < 7) && ` · 两段有记录天数 ${recentDays} / ${previousDays}`}</dd>
      </div>;
    })}</dl> : <p className="section-empty muted">{error ? '对比数据暂不可用' : data ? '暂无对比数据' : '正在读取对比…'}</p>}
  </section>;
}

function Health({ resource, dark }) {
  const [detail, setDetail] = useState(null);
  const { data, error, retry } = resource;
  const totals = data?.totals;
  const latest = data?.latest;
  const heart = data?.heart_rate;
  const sleep = data?.sleep;
  return (
    <section className="today-section" aria-labelledby="health-heading">
      <div className="today-section-title"><h2 id="health-heading">健康状态</h2><span className="muted">{data?.available ? `最近导入 ${time(data.updated_at, true)}` : error ? '暂不可用' : data ? '暂无健康数据' : '正在读取健康数据…'}</span></div>
      {error && <p className="overview-notice">健康数据暂时无法更新 <button className="text-link" type="button" onClick={retry}>重试</button></p>}
      <div className="metrics-grid health-grid">
        <HealthTile title="心率" Icon={HeartPulse} tone="red" value={number(heart?.current, 0)} unit="次/分" onOpen={() => setDetail('heart')} details={[
          ['静息', measure(latest?.resting_heart_rate, '次/分', 0)], ['HRV', measure(latest?.heart_rate_variability, 'ms', 0)],
        ]} />
        <HealthTile title="活动能量" Icon={Flame} tone="orange" value={number(totals?.active_energy, 0)} unit="kcal" onOpen={() => setDetail('activity')} details={[
          ['锻炼', measure(totals?.apple_exercise_time, '分钟', 0)],
        ]} />
        <HealthTile title="步数" Icon={Footprints} tone="green" value={number(totals?.step_count, 0)} unit="步" onOpen={() => setDetail('steps')} details={[
          ['步行与跑步', measure(totals?.walking_running_distance, 'km', 2)],
        ]} />
        <HealthTile title="睡眠" Icon={MoonStar} tone="purple" value={sleepTime(sleep?.total_minutes)} onOpen={() => setDetail('sleep')} details={[
          ['深睡', sleepTime(sleep?.stages.deep)], ['REM', sleepTime(sleep?.stages.rem)],
        ]} />
      </div>
      <WeeklyComparison />
      <div className="health-detail-grid">
        <section className="surface panel heart-trend">
          <HealthTrend metric="heart" title="心率趋势" dark={dark} />
        </section>
        <div className="health-side"><SleepCard sleep={sleep} />
          <div className="health-extra-grid">
            <VitalTile title="血氧" value={measure(latest?.blood_oxygen_saturation, '%', 1)} onOpen={() => setDetail('oxygen')} />
            <VitalTile title="呼吸频率" value={measure(latest?.respiratory_rate, '次/分', 1)} onOpen={() => setDetail('respiratory')} />
          </div>
        </div>
      </div>
      {detail && <Suspense fallback={null}><HealthDetails category={detail} dark={dark} onClose={() => setDetail(null)} /></Suspense>}
    </section>
  );
}

export default function TodayPage({ telemetry, media, dark, openMonitor, openControl, briefingEnabled }) {
  const weather = useResource('/api/weather', 60_000);
  const health = useResource('/api/health/summary', 20_000);
  return <>
    <div className="page-heading"><h1>今日概览</h1></div>
    <HostSummary telemetry={telemetry} onOpen={openMonitor} />
    {briefingEnabled && <DailyBriefing />}
    <MediaSummary track={media.current} onOpen={openControl} />
    <Weather resource={weather} dark={dark} />
    <Health resource={health} dark={dark} />
  </>;
}
