import { localizedDate, localizedNumber, t } from './i18n/messages.js';
import { lazy, Suspense, useEffect, useState } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import { Activity, ArrowDown, ArrowUp, ArrowUpRight, Cloud, CloudDrizzle, CloudFog, CloudLightning, CloudRain, CloudSun, Flame, Footprints, HeartPulse, MoonStar, RotateCw, Snowflake, Sparkles, Sun } from 'lucide-react';
import { MediaCover, playbackLabels } from './ControlPage.jsx';
import { request } from './api.js';
import { measure, number, sleepTime, time } from './format.js';
import { DetailLink, IconButton, Modal, Segmented } from './ui.jsx';
import HealthTrend from './HealthTrend.jsx';
import useResource from './useOverviewResource.js';
import { orderedCards } from './overviewCards.js';
import { AiUsageDetails, AiUsagePreview } from './AiUsageWidget.jsx';
import { CalendarDetails, CalendarPreview } from './CalendarWidget.jsx';
import { ExpensesDetails, ExpensesPreview } from './ExpensesWidget.jsx';
import { CountdownDetails, CountdownPreview, RateDetails, RatePreview, TasksDetails, TasksPreview } from './LifeWidgets.jsx';
import './overviewWidgets.css';

const OverviewChart = lazy(() => import('./OverviewChart.jsx'));
const HealthDetails = lazy(() => import('./HealthDetails.jsx'));
const modeLabels = { get silent() { return t('静音'); }, get balanced() { return t('平衡'); }, get performance() { return t('性能'); } };
const taskLabels = { get idle() { return t('空闲'); }, get running() { return t('高负载运行中'); }, get completed() { return t('已完工'); } };
const stages = [
  ['deep', '深睡'], ['rem', 'REM'], ['core', '核心'], ['asleep', '未分期'], ['awake', '清醒'],
];
const weeklyMeasures = [
  ['steps', '日均步数', '步', (value) => measure(value, t('步'), 0), '步数'],
  ['sleep_minutes', '平均睡眠', '分钟', sleepTime, '睡眠'],
  ['resting_heart_rate', '平均静息心率', '次/分', (value) => measure(value, t('次/分'), 1), '静息心率'],
];
const weatherMetrics = [{ value: 'temperature', get label() { return t('温度'); } }, { value: 'rain', get label() { return t('降水概率'); } }];
const forecastWeekday = localizedDate({ timeZone: 'UTC', weekday: 'short' });
const monthDay = localizedDate({ month: 'short', day: 'numeric', timeZone: 'UTC' });
const wholePercent = localizedNumber({ style: 'percent', maximumFractionDigits: 0 });

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
      if (!result.available || result.refresh_failed) setFeedback(t('暂时无法更新简报'));
    } catch {
      setFeedback(t('暂时无法更新简报'));
    } finally {
      setBusy(false);
    }
  }
  if (!data?.available) return null;
  return <section className="surface daily-briefing" aria-label={t('明日香简报')}>
    <Sparkles className="briefing-icon" size={18} aria-hidden="true" />
    <div><p aria-live="polite">{data.content}</p>{(busy || feedback) && <p className="muted briefing-feedback" role="status">{busy ? t('正在更新简报…') : feedback}</p>}</div>
    <IconButton label={t('刷新明日香简报')} onClick={refresh} disabled={busy}><RotateCw size={15} /></IconButton>
  </section>;
}

function HostSummary({ telemetry, hub, device, waiting, onOpen }) {
  const sample = device ? device.snapshot : telemetry.sample;
  const mac = (device?.os ?? hub.platform) === 'macos';
  const loads = sample?.gpus?.map((gpu) => gpu.usage_percent).filter(Number.isFinite) ?? [];
  const alerts = sample?.last_alerts?.length ?? 0;
  const power = sample?.power;
  const mode = power ? modeLabels[power.mode] ?? (power.mode || t('自定义')) : '—';
  const task = taskLabels[sample?.task?.state] ?? '—';
  return (
    <motion.button className="surface host-strip" type="button" disabled={waiting} onClick={onOpen} whileTap={{ scale: 0.97 }}>
      <span className="host-title"><span className="metric-icon tone-blue"><Activity size={20} aria-hidden="true" /></span>
        <strong title={device?.name ?? hub.name}>{waiting ? t('正在读取设备…') : device?.name ?? hub.name}</strong>
      </span>
      <span className="host-stats">
        {!device && hub.platform === 'windows' && <span><small>{t('电源模式')}</small><strong>{mode}</strong></span>}
        <span><small>CPU</small><strong className="numeric">{measure(sample?.cpu?.usage_percent, '%', 0)}</strong></span>
        {(mac || device) ? <>
          {sample?.battery && <span><small>{t('电池')}</small><strong className="numeric">{measure(sample.battery.percent, '%', 0)}</strong></span>}
          <span><small>{t('内存')}</small><strong className="numeric">{measure(sample?.memory?.percent, '%', 0)}</strong></span>
        </> : <span><small>GPU</small><strong className="numeric">{measure(loads.length ? Math.max(...loads) : null, '%', 0)}</strong></span>}
        {device ? <span><small>{t('状态')}</small><strong className={device.online ? '' : 'muted'}>{device.online ? t('在线') : t('已休眠或离线')}</strong></span>
          : <span><small>{t('任务与告警')}</small><strong className={alerts ? 'host-warning' : ''}>{task}{alerts > 0 && t(' · {p1} 项告警', { p1: alerts })}</strong></span>}
      </span>
      <span className="host-link">{t('硬件监控')}<ArrowUpRight size={17} aria-hidden="true" /></span>
    </motion.button>
  );
}

function MediaSummary({ track, deviceName, onOpen }) {
  return <motion.button className="surface host-strip music-strip" type="button"
    onClick={onOpen} whileTap={{ scale: 0.97 }}>
    <MediaCover track={track} compact />
    <span className="music-strip-text">
      <strong>{track.title || t('未命名媒体')}</strong>
      <small className="muted">{[track.artist, track.player, playbackLabels[track.status], deviceName].filter(Boolean).join(' · ')}</small>
    </span>
    <span className="host-link">{t('控制')}<ArrowUpRight size={17} aria-hidden="true" /></span>
  </motion.button>;
}

function condition(code) {
  if (code === 0) return [Sun, t('晴')];
  if (code === 1 || code === 2) return [CloudSun, code === 1 ? t('大致晴朗') : t('局部多云')];
  if (code === 3) return [Cloud, t('阴')];
  if (code === 45 || code === 48) return [CloudFog, t('雾')];
  if (code >= 51 && code <= 57) return [CloudDrizzle, t('毛毛雨')];
  if (code >= 61 && code <= 67 || code >= 80 && code <= 82) return [CloudRain, t('雨')];
  if (code >= 71 && code <= 77 || code === 85 || code === 86) return [Snowflake, t('雪')];
  if (code >= 95) return [CloudLightning, t('雷雨')];
  return [Cloud, t('天气状况未知')];
}

function aqiWord(aqi) {
  if (!Number.isFinite(aqi)) return '';
  if (aqi <= 50) return t('优');
  if (aqi <= 100) return t('良');
  if (aqi <= 150) return t('敏感人群不佳');
  if (aqi <= 200) return t('不健康');
  if (aqi <= 300) return t('很不健康');
  return t('严重');
}

function AirBadge({ air, compact = false }) {
  const aqi = air?.data?.us_aqi;
  const valid = Number.isFinite(aqi);
  const tone = aqi <= 50 ? 'green' : aqi <= 150 ? 'orange' : 'red';
  return <span className={`aqi-badge ${valid ? `tone-${tone}` : ''}`} title={valid ? aqiWord(aqi) : t('暂无空气质量数据')}>
    US AQI <strong className="numeric">{number(aqi, 0)}</strong>{!compact && air?.status === 'stale' && t(' · 缓存')}
  </span>;
}

function temperatureScale(days) {
  const values = days.flatMap((day) => [day.low_c, day.high_c]).filter(Number.isFinite);
  const minimum = values.length ? Math.min(...values) : 0;
  return { minimum, span: values.length ? Math.max(Math.max(...values) - minimum, 1) : 1 };
}

function TemperatureTrack({ day, scale }) {
  const valid = Number.isFinite(day.low_c) && Number.isFinite(day.high_c);
  const start = valid ? (day.low_c - scale.minimum) / scale.span * 100 : 0;
  const width = valid ? (day.high_c - day.low_c) / scale.span * 100 : 0;
  return <span className="temperature-track" aria-hidden="true">
    {valid && <span style={{ left: `min(calc(100% - 4px), ${start}%)`, width: `max(4px, ${width}%)` }} />}
  </span>;
}

function WeatherPreview({ resource, wide }) {
  const reply = resource.data;
  const current = reply?.data?.current;
  const [CurrentIcon, description] = condition(current?.code);
  const location = current ? [description, reply?.city_name].filter(Boolean).join(' · ')
    : resource.error || reply?.status === 'unavailable' ? t('天气暂不可用') : t('正在获取天气…');
  const temperature = (value) => Number.isFinite(value) ? `${number(value, 0)}°` : '—';
  const percent = (value) => Number.isFinite(value) ? wholePercent.format(value / 100) : '—';
  const forecast = wide ? reply?.data?.daily?.slice(1, 5) ?? [] : [];
  const scale = temperatureScale(forecast);
  return <div className={forecast.length ? 'preview-split weather-preview-split' : 'weather-preview'}>
    <div className="weather-preview">
      <div className="widget-weather">
        <div className="widget-weather-current">
          <p className="widget-weather-temperature numeric">{temperature(current?.temperature_c)}</p>
          <p className="widget-muted widget-weather-location" title={location}>
            {wide && current && <CurrentIcon size={13} aria-hidden="true" />}<span className="widget-truncate">{location}</span>
          </p>
        </div>
        <dl className="widget-weather-range">{[[t('最高'), current?.high_c], [t('最低'), current?.low_c]].map(([label, value]) => (
          <div key={label}><dt>{label}</dt><dd className="numeric">{temperature(value)}</dd></div>
        ))}</dl>
      </div>
      <dl className="widget-weather-facts">{[
        [t('体感'), temperature(current?.feels_like_c)], [t('湿度'), percent(current?.humidity_percent)],
        [t('降水'), percent(current?.precipitation_percent)],
      ].map(([label, value]) => <div key={label}><dt>{label}</dt><dd className="numeric">{value}</dd></div>)}</dl>
    </div>
    {forecast.length > 0 && <ul className="preview-secondary weather-preview-days" aria-label={t('未来四天')}>
      {forecast.map((day) => {
        const [Icon, label] = condition(day.code);
        return <li key={day.date} title={label}>
          <time dateTime={day.date}>{forecastWeekday.format(new Date(`${day.date}T00:00:00Z`))}</time>
          <Icon size={17} role="img" aria-label={label} />
          <span className="numeric muted">{temperature(day.low_c)}</span>
          <TemperatureTrack day={day} scale={scale} />
          <strong className="numeric">{temperature(day.high_c)}</strong>
        </li>;
      })}
    </ul>}
  </div>;
}

function SleepStageTrack({ sleep }) {
  const duration = stages.reduce((sum, [key]) => sum + (sleep?.stages?.[key] ?? 0), 0);
  const scale = Math.max(duration, sleep?.in_bed_minutes ?? 0);
  if (scale <= 0) return null;
  return <div className="sleep-track" aria-hidden="true">{stages.map(([key]) => sleep?.stages?.[key] > 0 &&
    <span key={key} className={`stage-${key}`} style={{ width: `${sleep.stages[key] / scale * 100}%` }} />)}</div>;
}

function HealthPreviewDetails({ sleep, weekly }) {
  const sleepStages = stages.filter(([key]) => sleep?.stages?.[key] > 0);
  const trends = weeklyMeasures.flatMap(([key, label, unit, format, short]) => {
    const metric = weekly?.data?.metrics?.[key];
    if (!Number.isFinite(metric?.recent)) return [];
    const change = key === 'steps' && Number.isFinite(metric.percent_change)
      ? `${number(Math.abs(metric.percent_change), 1)}%`
      : measure(Math.abs(metric.delta), t(key === 'sleep_minutes' ? '分' : unit), key === 'resting_heart_rate' ? 1 : 0);
    const direction = Number.isFinite(metric.delta) ? Math.sign(metric.delta) : null;
    const text = direction === null ? format(metric.recent) : `${direction > 0 ? '+' : direction < 0 ? '−' : ''}${change}`;
    return [{ key, short: t(short), text, label: `${t(label)} ${format(metric.recent)}${direction !== null ? t('，较前 7 天 {p1}', { p1: text }) : ''}` }];
  });
  return <div className="preview-secondary health-preview-details">
    <div>
      <p className="preview-caption">{t('睡眠阶段')}</p>
      {sleepStages.length ? <>
        <SleepStageTrack sleep={sleep} />
        <ul className="health-preview-stages">{sleepStages.map(([key, label]) => <li key={key}>
          <i className={`stage-${key}`} aria-hidden="true" />{t(label)}<strong className="numeric">{t('{p1}分', { p1: number(sleep.stages[key], 0) })}</strong>
        </li>)}</ul>
      </> : <p className="widget-muted">{t('暂无分期数据')}</p>}
    </div>
    <div>
      <p className="preview-caption">{t('近 7 天')}</p>
      {trends.length ? <ul className="health-preview-trends">{trends.map(({ key, short, text, label }) => <li key={key} title={label}>
        <span className="sr-only">{label}</span><span aria-hidden="true">{short} <strong className="numeric">{text}</strong></span>
      </li>)}</ul> : <p className="widget-muted">{weekly?.error ? t('趋势暂不可用') : weekly?.data ? t('暂无趋势数据') : t('正在读取趋势…')}</p>}
    </div>
  </div>;
}

function HealthPreview({ resource, wide, weekly }) {
  const { totals, heart_rate: heart, sleep } = resource.data ?? {};
  const rings = [
    [t('活动能量'), totals?.active_energy, 500, '#FF3B30', 39],
    [t('步数'), totals?.step_count, 8000, '#34C759', 28],
    [t('睡眠'), sleep?.total_minutes, 480, '#5856D6', 17],
  ];
  const metrics = <div className="widget-health">
    <svg viewBox="0 0 88 88" className="health-rings" fill="none" strokeWidth="8" role="img" aria-label={rings.map(
      ([label, value, goal]) => t('{p1}目标 {p2}', { p1: label, p2: Number.isFinite(value) ? wholePercent.format(value / goal) : t('暂无数据') })
    ).join(' · ')}>
      {rings.map(([label, value, goal, color, radius]) => {
        const progress = Number.isFinite(value) ? Math.min(1, Math.max(0, value / goal)) : 0;
        return <g key={label} stroke={color}>
          <circle cx="44" cy="44" r={radius} opacity=".18" />
          {progress > 0 && <circle className="health-ring-fill" cx="44" cy="44" r={radius} pathLength="1" strokeLinecap="round"
            strokeDasharray={`${progress} 1`} transform="rotate(-90 44 44)" />}
        </g>;
      })}
    </svg>
    <dl>{[
    [t('步数'), measure(totals?.step_count, t('步'), 0)], [t('睡眠'), sleepTime(sleep?.total_minutes)],
    [t('心率'), measure(heart?.current, t('次/分'), 0)], [t('活动能量'), measure(totals?.active_energy, 'kcal', 0)],
    ].map(([label, value]) => <div key={label}><dt>{label}</dt><dd className="numeric">{value}</dd></div>)}</dl>
  </div>;
  return wide ? <div className="preview-split health-preview-split">
    {metrics}<HealthPreviewDetails sleep={sleep} weekly={weekly} />
  </div> : metrics;
}

function ForecastDays({ days }) {
  const scale = temperatureScale(days);
  return (
    <section className="surface panel forecast-card">
      <div className="panel-header"><h3>{t('未来 7 天')}</h3></div>
      <ul className="forecast-list">{days.map((day, index) => {
        const [Icon, label] = condition(day.code);
        return <li className="forecast-row" key={day.date}>
          <span className="forecast-day">{index === 0 ? t('今天') : forecastWeekday.format(new Date(`${day.date}T00:00:00Z`))}</span>
          <span className="forecast-condition" aria-label={label} title={label}><Icon size={19} aria-hidden="true" /><span>{label}</span></span>
          <span className="numeric muted">{measure(day.low_c, '°', 0)}</span>
          <TemperatureTrack day={day} scale={scale} />
          <strong className="numeric">{measure(day.high_c, '°', 0)}</strong>
        </li>;
      })}</ul>
    </section>
  );
}

function Weather({ resource, dark }) {
  const [weatherMetric, setWeatherMetric] = useState('temperature');
  const { data: reply, error, retry } = resource;
  const data = reply?.data;
  const air = reply?.air_quality;
  const aqi = air?.data?.us_aqi;
  const current = data?.current;
  const [Icon, label] = condition(current?.code);
  const clock = localizedDate({ timeZone: data?.timezone, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
  const solarTime = (stamp) => Number.isFinite(stamp) ? clock.format(stamp * 1000) : '—';
  const hourlyKey = weatherMetric === 'rain' ? 'precipitation_percent' : 'temperature_c';
  return (
    <section className="today-detail-content" aria-label={t('天气预报')}>
      {data ? <>
        {reply.status === 'stale' && <p className="overview-notice">{t('暂时无法更新，显示上次天气 · {p1}', { p1: time(reply.updated_at, true) })}</p>}
        <div className="weather-grid">
          <section className="surface panel current-weather" aria-label={t('当前天气')}>
            <div className="panel-header"><h3>{t('当前天气')}</h3>
              <AirBadge air={air} />
            </div>
            <div className="weather-temperature numeric">{number(current.temperature_c, 0)}{Number.isFinite(current.temperature_c) && <span>°C</span>}</div>
            <div className="weather-caption"><Icon size={19} className="weather-symbol" aria-hidden="true" /><strong>{label}</strong><span className="numeric muted">{t('最高 {p1} · 最低 {p2}', { p1: measure(current.high_c, '°', 0), p2: measure(current.low_c, '°', 0) })}</span></div>
            <dl className="weather-stats">
              {[[t('体感'), measure(current.feels_like_c, '°C', 0)], [t('降水概率'), measure(current.precipitation_percent, '%', 0)],
                [t('湿度'), measure(current.humidity_percent, '%', 0)], [t('风速'), measure(current.wind_kmh, 'km/h')],
                [t('紫外线（UV）'), number(current.uv_index, 1)], [t('日出'), solarTime(current.sunrise)], [t('日落'), solarTime(current.sunset)]].map(([key, value]) => (
                <div key={key}><dt className="muted">{key}</dt><dd className="numeric">{value}</dd></div>
              ))}
            </dl>
          </section>
          <section className="surface panel"><div className="panel-header weather-chart-header"><h3>{t('未来 24 小时')}</h3>
            <Segmented id="weather-chart" label={t('天气图表')} options={weatherMetrics} value={weatherMetric} onChange={setWeatherMetric} />
          </div>
            {data.hourly.some((hour) => Number.isFinite(hour[hourlyKey])) ? <Suspense fallback={<div className="chart-loading trend-plot muted">{t('正在加载图表…')}</div>}>
              <OverviewChart type="weather" weatherMetric={weatherMetric} readings={data.hourly} timezone={data.timezone} dark={dark}
                start={data.hourly_start == null ? undefined : data.hourly_start * 1000} end={data.hourly_end == null ? undefined : data.hourly_end * 1000} />
            </Suspense> : <p className="section-empty">{t('暂无逐小时数据')}</p>}
          </section>
          {data.daily.length > 0 && <ForecastDays days={data.daily} />}
        </div>
      </> : <div className="surface panel overview-empty">
        <Cloud size={24} aria-hidden="true" />
        <p>{reply?.status === 'unavailable' || error ? t('天气暂不可用') : t('正在获取天气…')}</p>
        {(error || reply?.status === 'unavailable') && <button className="text-link" type="button" onClick={retry}>{t('重试')}</button>}
        {!data && Number.isFinite(aqi) && <span className="numeric">US AQI {number(aqi, 0)} · {aqiWord(aqi)}</span>}
      </div>}
      <p className="source-note">{t('天气')}<a href="https://open-meteo.com/" target="_blank" rel="noopener noreferrer">Open-Meteo</a>{t('· 空气质量')}<a href="https://atmosphere.copernicus.eu/" target="_blank" rel="noopener noreferrer">CAMS</a></p>
    </section>
  );
}

function HealthTile({ title, Icon, tone, value, unit, details, onOpen }) {
  return <section className={`surface metric health-metric detail-tile tone-${tone}`}>
    <div className="metric-header flex items-center gap-3"><span className="metric-icon"><Icon size={22} aria-hidden="true" /></span><h3>{title}</h3></div>
    <div className="metric-reading numeric">{value}{unit && value !== '—' && <span>{unit}</span>}</div>
    <dl className="metric-details">
      {details.map(([label, item]) => <div key={label} className="flex items-center gap-2"><dt>{label}</dt><dd className="numeric">{item}</dd></div>)}
    </dl>
    <DetailLink title={title} onClick={onOpen} />
  </section>;
}

function VitalTile({ title, value, onOpen }) {
  return <section className="surface panel detail-tile">
    <h3>{title}</h3><div className="health-extra-value numeric">{value}</div>
    <DetailLink title={title} onClick={onOpen} />
  </section>;
}

function SleepCard({ sleep }) {
  return <section className="surface panel sleep-card">
    <div className="panel-header"><h3>{t('睡眠阶段')}</h3>{sleep?.adjusted && <span className="muted panel-note">{t('时长已校正')}</span>}</div>
    {sleep ? <>
      <p className="muted sleep-summary numeric">{sleep.start && sleep.end ? `${time(sleep.start, true)} – ${time(sleep.end)}` : t('最近一次睡眠')}{t('· 在床 {p1}', { p1: sleepTime(sleep.in_bed_minutes) })}</p>
      <SleepStageTrack sleep={sleep} />
      <dl className="sleep-legend">{stages.map(([key, label]) => <div key={key}>
        <dt><span className={`stage-${key}`} aria-hidden="true" />{t(label)}</dt><dd className="numeric">{sleepTime(sleep.stages[key])}</dd>
      </div>)}</dl>
    </> : <p className="section-empty">{t('暂无睡眠数据')}</p>}
  </section>;
}

function WeeklyComparison({ resource }) {
  const { data, error } = resource;
  const items = weeklyMeasures.filter(([key]) => data?.metrics[key].recent_days || data?.metrics[key].previous_days);
  function displayRange(range) {
    return `${monthDay.format(new Date(range.start))}–${monthDay.format(new Date(range.end))}`;
  }
  return <section className="surface panel weekly-card" aria-labelledby="week-comparison-heading">
    <div className="panel-header"><h3 id="week-comparison-heading">{t('7 天对比')}</h3>
      {data && items.length > 0 && <span className="muted panel-note numeric">{t('{p1} 对比 {p2}', { p1: displayRange(data.recent), p2: displayRange(data.previous) })}</span>}
    </div>
    {items.length ? <dl className="weekly-metrics">{items.map(([key, label, unit, format]) => {
      const { recent, previous, recent_days: recentDays, previous_days: previousDays, delta, percent_change: percent } = data.metrics[key];
      const precision = key === 'resting_heart_rate' ? 1 : 0;
      const change = Number.isFinite(delta) ? Number(delta.toFixed(precision)) : null;
      const ChangeIcon = change > 0 ? ArrowUp : ArrowDown;
      return <div className="weekly-metric" key={key}>
        <dt>{t(label)}</dt>
        <dd className="weekly-current numeric">{format(recent)}{change !== null && <span className={`weekly-change ${change > 0 ? 'weekly-up' : change < 0 ? 'weekly-down' : 'muted'}`}>
          {change !== 0 && <ChangeIcon size={13} aria-hidden="true" />}{change > 0 ? '+' : change < 0 ? '−' : ''}{measure(Math.abs(change), t(unit), precision)}
          {Number.isFinite(percent) && ` (${percent > 0 ? '+' : ''}${number(percent, 1)}%)`}
        </span>}</dd>
        <dd className="muted numeric">{t('前 7 天 {p1}', { p1: format(previous) })}
          {(recentDays < 7 || previousDays < 7) && t(' · 两段有记录天数 {p1} / {p2}', { p1: recentDays, p2: previousDays })}</dd>
      </div>;
    })}</dl> : <p className="section-empty muted">{error ? t('对比数据暂不可用') : data ? t('暂无对比数据') : t('正在读取对比…')}</p>}
  </section>;
}

function Health({ resource, weekly, dark }) {
  const [detail, setDetail] = useState(null);
  const { data, error, retry } = resource;
  const totals = data?.totals;
  const latest = data?.latest;
  const heart = data?.heart_rate;
  const sleep = data?.sleep;
  return (
    <section className="today-detail-content" aria-label={t('健康概览')}>
      <p className="muted health-import-status">{data?.available ? t('最近导入 {p1}', { p1: time(data.updated_at, true) }) : error ? t('暂不可用') : data ? t('暂无健康数据') : t('正在读取健康数据…')}</p>
      {error && <p className="overview-notice">{t('健康数据暂时无法更新')}<button className="text-link" type="button" onClick={retry}>{t('重试')}</button></p>}
      <div className="metrics-grid health-grid">
        <HealthTile title={t('心率')} Icon={HeartPulse} tone="red" value={number(heart?.current, 0)} unit={t('次/分')} onOpen={() => setDetail('heart')} details={[
          [t('静息'), measure(latest?.resting_heart_rate, t('次/分'), 0)], ['HRV', measure(latest?.heart_rate_variability, 'ms', 0)],
        ]} />
        <HealthTile title={t('活动能量')} Icon={Flame} tone="orange" value={number(totals?.active_energy, 0)} unit="kcal" onOpen={() => setDetail('activity')} details={[
          [t('锻炼'), measure(totals?.apple_exercise_time, t('分钟'), 0)],
        ]} />
        <HealthTile title={t('步数')} Icon={Footprints} tone="green" value={number(totals?.step_count, 0)} unit={t('步')} onOpen={() => setDetail('steps')} details={[
          [t('步行与跑步'), measure(totals?.walking_running_distance, 'km', 2)],
        ]} />
        <HealthTile title={t('睡眠')} Icon={MoonStar} tone="purple" value={sleepTime(sleep?.total_minutes)} onOpen={() => setDetail('sleep')} details={[
          [t('深睡'), sleepTime(sleep?.stages.deep)], ['REM', sleepTime(sleep?.stages.rem)],
        ]} />
      </div>
      <WeeklyComparison resource={weekly} />
      <div className="health-detail-grid">
        <section className="surface panel heart-trend">
          <HealthTrend metric="heart" title={t('心率趋势')} dark={dark} />
        </section>
        <div className="health-side"><SleepCard sleep={sleep} />
          <div className="health-extra-grid">
            <VitalTile title={t('血氧')} value={measure(latest?.blood_oxygen_saturation, '%', 1)} onOpen={() => setDetail('oxygen')} />
            <VitalTile title={t('呼吸频率')} value={measure(latest?.respiratory_rate, t('呼吸次数/分'), 1)} onOpen={() => setDetail('respiratory')} />
          </div>
        </div>
      </div>
      <AnimatePresence>
        {detail && <Suspense fallback={null}><HealthDetails category={detail} dark={dark} onClose={() => setDetail(null)} /></Suspense>}
      </AnimatePresence>
    </section>
  );
}

export default function TodayPage({ telemetry, media, hub, primaryDevice, waitingForPrimary, dark, openMonitor, openControl,
  briefingEnabled, widgets, cardOrder, cardWidths }) {
  const [open, setOpen] = useState(null);
  const [usageSource, setUsageSource] = useState(null);
  const [usageReadAt, setUsageReadAt] = useState(null);
  const visible = (id) => widgets[id] !== false;
  const isWide = (id) => cardWidths[id] === 'wide';
  const weather = useResource('/api/weather', 60_000, visible('weather'));
  const health = useResource('/api/health/summary', 20_000, visible('health'));
  const healthWeek = useResource('/api/health/weekly-comparison', 60_000, visible('health') && (isWide('health') || open === 'health'));
  const calendar = useResource('/api/calendar/events', 45_000, visible('calendar'));
  const todos = useResource('/api/todos', 15_000, visible('tasks'));
  const habits = useResource('/api/habits', 15_000, visible('tasks'));
  const countdowns = useResource('/api/countdowns', 30_000, visible('countdowns'));
  const rates = useResource('/api/rates', 60_000, visible('rates'));
  const aiUsage = useResource('/api/ai/usage', 300_000, visible('ai-usage'));
  const expenses = useResource('/api/expenses/summary', 30_000, visible('expenses'));
  useEffect(() => {
    if (aiUsage.data) setUsageReadAt(Date.now());
  }, [aiUsage.data]);

  const previews = {
    weather: <WeatherPreview resource={weather} wide={isWide('weather')} />,
    health: <HealthPreview resource={health} wide={isWide('health')} weekly={healthWeek} />,
    calendar: <CalendarPreview resource={calendar} wide={isWide('calendar')} />,
    tasks: <TasksPreview todos={todos} habits={habits} wide={isWide('tasks')} />,
    countdowns: <CountdownPreview resource={countdowns} wide={isWide('countdowns')} />,
    rates: <RatePreview resource={rates} wide={isWide('rates')} />,
    'ai-usage': <AiUsagePreview resource={aiUsage} wide={isWide('ai-usage')} preferredSource={usageSource} />,
    expenses: <ExpensesPreview resource={expenses} wide={isWide('expenses')} />,
  };
  const details = {
    weather: <Weather resource={weather} dark={dark} />, health: <Health resource={health} weekly={healthWeek} dark={dark} />,
    tasks: <TasksDetails todos={todos} habits={habits} />,
    countdowns: <CountdownDetails resource={countdowns} />, rates: <RateDetails resource={rates} dark={dark} />,
    'ai-usage': <AiUsageDetails resource={aiUsage} preferredSource={usageSource} onSource={setUsageSource} readAt={usageReadAt} />,
    expenses: <ExpensesDetails resource={expenses} />,
  };
  const shown = orderedCards(cardOrder).filter(({ id }) => visible(id));
  const selected = shown.find(({ id }) => id === open);
  const remoteTrack = primaryDevice?.online && primaryDevice.snapshot.media;
  const track = primaryDevice || waitingForPrimary
    ? remoteTrack && { ...remoteTrack, agent_id: primaryDevice.device_id }
    : media.current;
  return <>
    <div className="page-heading"><h1>{t('今日概览')}</h1></div>
    {visible('host') && <HostSummary telemetry={telemetry} hub={hub} device={primaryDevice} waiting={waitingForPrimary} onOpen={openMonitor} />}
    {visible('media') && (track?.status === 'playing' || track?.status === 'paused') &&
      <MediaSummary track={track} deviceName={primaryDevice?.name ?? hub.name} onOpen={openControl} />}
    {visible('briefing') && briefingEnabled && <DailyBriefing />}
    {shown.length > 0 && <div className="overview-grid">
      {shown.map(({ id, title, Icon, tone }) => (
        <section key={id} className={`surface detail-tile overview-widget tone-${tone}`} data-widget={id}
          data-width={isWide(id) ? 'wide' : 'standard'}>
          <header><span className="metric-icon"><Icon size={18} aria-hidden="true" /></span><h2>{title}</h2>
            {id === 'weather' && <div className="widget-aqi"><AirBadge air={weather.data?.air_quality} compact /></div>}
          </header>
          <div className="widget-content">{previews[id]}</div>
          <DetailLink title={title} onClick={() => setOpen(id)} />
        </section>
      ))}
    </div>}
    <AnimatePresence>
      {selected?.id === 'calendar' && <CalendarDetails key="calendar" resource={calendar} onClose={() => setOpen(null)} />}
      {selected && selected.id !== 'calendar' && <Modal key={selected.id} id={`overview-${selected.id}`}
        title={selected.id === 'weather' && weather.data?.city_name ? t('天气 · {p1}', { p1: weather.data.city_name }) : selected.title}
        className={`detail-dialog overview-dialog ${selected.id}-dialog`} onClose={() => setOpen(null)}>{details[selected.id]}</Modal>}
    </AnimatePresence>
  </>;
}
