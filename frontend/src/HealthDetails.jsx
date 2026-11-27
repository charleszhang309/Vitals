import { localizedDate, localizedNumber, t } from './i18n/messages.js';
import { useEffect, useState } from 'react';
import { use } from 'echarts/core';
import { BarChart } from 'echarts/charts';
import { request } from './api.js';
import { measure, sleepTime } from './format.js';
import LinePlot, { plotStyle } from './LinePlot.jsx';
import OverviewChart from './OverviewChart.jsx';
import HealthTrend from './HealthTrend.jsx';
import { DetailValues, Modal } from './ui.jsx';

use([BarChart]);

const titles = { get heart() { return t('心率详情'); }, get activity() { return t('活动能量详情'); }, get steps() { return t('步数详情'); }, get sleep() { return t('睡眠详情'); },
  get oxygen() { return t('血氧详情'); }, get respiratory() { return t('呼吸频率详情'); } };
const stages = [['deep', '深睡'], ['rem', 'REM'], ['core', '核心'], ['awake', '清醒'], ['asleep', '未分期']];
const red = '#FF3B30';
const green = '#34C759';
const orange = '#FF9500';
const purple = '#5856D6';
const monthDay = localizedDate({ month: 'short', day: 'numeric', timeZone: 'UTC' });
const axisNumber = localizedNumber({ maximumFractionDigits: 2 });

function DetailChart({ title, dates, series, format, dark, line = false }) {
  const hasValues = series.some((item) => item.data.some(Number.isFinite));
  return <section className="detail-section">
    <h3>{title}</h3>
    {series.length > 1 && <ul className="detail-legend">{series.map(({ name, color }) =>
      <li key={name}><span style={{ backgroundColor: color }} />{name}</li>)}</ul>}
    {hasValues ? <LinePlot label={title} options={{
      ...plotStyle(dark, format),
      grid: { top: 12, right: 14, bottom: 25, left: 50 },
      xAxis: { type: 'category', data: dates, axisLabel: { color: '#8E8E93', hideOverlap: true }, axisTick: { show: false },
        axisLine: { lineStyle: { color: dark ? '#424247' : '#D6D8DF' } } },
      yAxis: { type: 'value', scale: line, splitNumber: 4, axisLabel: { color: '#8E8E93', formatter: (value) => axisNumber.format(value) },
        splitLine: { lineStyle: { type: 'dashed', color: dark ? '#3A3A3E' : '#E4E6EB' } } },
      series: series.map(({ name, color, data }) => line
        ? { name, type: 'line', data, color, smooth: 0.3, symbolSize: 6, showAllSymbol: true, connectNulls: false }
        : { name, type: 'bar', data, color, barMaxWidth: 24, itemStyle: { borderRadius: [5, 5, 0, 0] } }),
    }} /> : <p className="section-empty muted">—</p>}
  </section>;
}

function HourlyBars({ detail, title, metric, label, color, unit, dark }) {
  const times = [];
  const clock = localizedDate({ timeZone: detail.timezone, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
  const points = new Map((detail.hourly ?? []).map((point) => [Date.parse(point.time), point[metric]]));
  for (let at = Date.parse(detail.day_start); at < Date.parse(detail.day_end); at += 3600000) times.push(at);
  return <DetailChart title={title} dates={times.map((at) => clock.format(at))} dark={dark}
    series={[{ name: label, color, data: times.map((at) => points.get(at) ?? null) }]} format={(value) => measure(value, unit, 0)} />;
}

function WeeklyChart({ detail, title, series, unit, dark, line, toHours = false }) {
  const dates = detail.weekly.map(({ date }) => monthDay.format(new Date(date)));
  return <DetailChart title={title} dates={dates} dark={dark} line={line} format={(value) => measure(value, unit, toHours ? 1 : 0)}
    series={series.map(([key, name, color]) => ({ name, color, data: detail.weekly.map((day) => {
      const value = day[key];
      return Number.isFinite(value) ? value / (toHours ? 60 : 1) : null;
    }) }))} />;
}

function HeartDetail({ detail, dark }) {
  const heartRate = (value) => measure(value, t('次/分'), 0);
  const values = detail.values;
  const clock = localizedDate({ timeZone: detail.timezone, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
  return <>
    <DetailValues items={[
      [t('静息心率'), heartRate(values.resting_heart_rate)],
      [t('步行平均心率'), heartRate(values.walking_heart_rate_average)],
      [t('最新 HRV'), measure(values.heart_rate_variability, 'ms', 0)],
    ]} />
    <section className="detail-section">
      <h3>{t('今日每小时心率')}</h3>
      {detail.hourly.length ? <>
        <ul className="detail-legend">{[[t('最低'), '#E6A5A4'], [t('平均'), red], [t('最高'), '#D67E81']].map(([label, color]) =>
          <li key={label}><span style={{ backgroundColor: color }} />{label}</li>)}</ul>
        <OverviewChart type="heart" readings={detail.hourly} timezone={detail.timezone}
          start={Date.parse(detail.day_start)} end={Date.parse(detail.day_end)} dark={dark} />
      </> : <p className="section-empty muted">—</p>}
    </section>
    <WeeklyChart detail={detail} title={t('最近 7 天静息心率')} series={[['resting_heart_rate', t('静息心率'), red]]} unit={t('次/分')} dark={dark} line />
    <section className="detail-section">
      <h3>{t('今日 HRV 记录')}</h3>
      {detail.hrv.length ? <ul className="detail-list detail-samples">{[...detail.hrv].reverse().map((point) =>
        <li key={point.time}><time dateTime={point.time}>{clock.format(Date.parse(point.time))}</time>
          <strong className="numeric">{measure(point.value, 'ms')}</strong></li>)}</ul> : <p className="section-empty muted">—</p>}
    </section>
  </>;
}

function ActivityDetail({ detail, dark }) {
  const v = detail.values;
  return <>
    <DetailValues items={[
      [t('今日活动能量'), measure(v.active_energy, 'kcal', 0)], [t('今日基础代谢'), measure(v.basal_energy_burned, 'kcal', 0)],
      [t('锻炼时长'), measure(v.apple_exercise_time, t('分钟'), 0)], [t('站立小时'), measure(v.apple_stand_hour, t('小时'), 0)],
      [t('站立时长'), measure(v.apple_stand_time, t('分钟'), 0)],
    ]} />
    <HourlyBars detail={detail} title={t('今日每小时活动能量')} metric="active_energy" label={t('活动能量')} color={orange} unit="kcal" dark={dark} />
    <WeeklyChart detail={detail} title={t('最近 7 天能量消耗')} series={[
      ['active_energy', t('活动能量'), orange], ['basal_energy_burned', t('基础代谢'), purple],
    ]} unit="kcal" dark={dark} />
  </>;
}

function StepsDetail({ detail, dark }) {
  const v = detail.values;
  return <>
    <DetailValues items={[
      [t('今日步数'), measure(v.step_count, t('步'), 0)], [t('步行与跑步距离'), measure(v.walking_running_distance, 'km', 2)],
      [t('爬楼层数'), measure(v.flights_climbed, t('层'), 0)], [t('平均步速'), measure(v.walking_speed, 'km/h', 2)],
      [t('平均步长'), measure(v.walking_step_length, 'cm', 1)],
    ]} />
    <HourlyBars detail={detail} title={t('今日每小时步数')} metric="step_count" label={t('步数')} color={green} unit={t('步')} dark={dark} />
    <WeeklyChart detail={detail} title={t('最近 7 天步数')} series={[[ 'step_count', t('步数'), green]]} unit={t('步')} dark={dark} />
  </>;
}

function SleepDetail({ detail, dark }) {
  const { sleep, values, timezone } = detail;
  const clock = localizedDate({ timeZone: timezone, month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
  const at = (value) => value ? clock.format(Date.parse(value)) : '—';
  return <>
    <DetailValues items={[
      [t('入睡'), at(sleep?.start)], [t('醒来'), at(sleep?.end)], [t('实际睡眠'), sleepTime(sleep?.total_minutes)],
      [t('在床时长'), sleepTime(sleep?.in_bed_minutes)], [t('睡眠效率'), measure(sleep?.efficiency_percent, '%', 0)],
      [t('夜间平均血氧'), measure(values.blood_oxygen_saturation, '%', 1)], [t('夜间平均呼吸频率'), measure(values.respiratory_rate, t('呼吸次数/分'))],
    ]} />
    <section className="detail-section">
      <h3>{t('睡眠阶段')}{sleep?.adjusted && <span className="muted panel-note">{t('时长已校正')}</span>}</h3>
      <ul className="detail-list sleep-detail-stages">{stages.map(([key, label]) =>
        <li key={key}><span><i className={`stage-${key}`} />{t(label)}</span>
          <strong className="numeric">{sleepTime(sleep?.stages[key])}</strong>
          <span className="muted numeric">{measure(sleep?.stage_percent[key], '%', 0)}</span></li>)}</ul>
    </section>
    <HourlyBars detail={detail} title={t('今日每小时睡眠时长')} metric="sleep_minutes" label={t('睡眠时长')} color={purple} unit={t('分钟')} dark={dark} />
    <WeeklyChart detail={detail} title={t('最近 7 天睡眠时长')} series={[[ 'total_minutes', t('睡眠时长'), purple]]}
      unit={t('小时')} toHours dark={dark} />
  </>;
}

const views = { heart: HeartDetail, activity: ActivityDetail, steps: StepsDetail, sleep: SleepDetail };

function DetailContent({ category, dark }) {
  const [result, setResult] = useState(null);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    request(`/api/health/detail/${category}`, { signal: controller.signal })
      .then((data) => { if (!controller.signal.aborted) setResult({ data }); })
      .catch(() => { if (!controller.signal.aborted) setResult({ error: true }); });
    return () => controller.abort();
  }, [category, retry]);
  const View = views[category];
  return result?.data ? <View detail={result.data} dark={dark} />
    : result?.error ? <p className="feedback error" role="status">{t('详情暂不可用')}<button className="text-link" type="button" onClick={() => {
      setResult(null);
      setRetry((value) => value + 1);
    }}>{t('重试')}</button></p> : <p className="section-empty muted" role="status">{t('正在读取健康详情…')}</p>;
}

export default function HealthDetails({ category, dark, onClose }) {
  return <Modal id={`health-${category}`} title={titles[category]} className="detail-dialog" onClose={onClose}>
    {views[category] ? <DetailContent category={category} dark={dark} /> :
      <HealthTrend metric={category} title={category === 'oxygen' ? t('血氧趋势') : t('呼吸频率趋势')} dark={dark} />}
  </Modal>;
}
