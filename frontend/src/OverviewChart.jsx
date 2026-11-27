import { getLocale, localizedDate, localizedNumber, t } from './i18n/messages.js';
import { useMemo } from 'react';
import LinePlot, { fill, plotStyle } from './LinePlot.jsx';
import { measure } from './format.js';

const monthDay = localizedDate({ month: 'short', day: 'numeric', timeZone: 'UTC' });
const axisNumber = localizedNumber({ maximumFractionDigits: 2 });

export default function OverviewChart({ type, readings, timezone, start, end, dark, period, weatherMetric }) {
  const locale = getLocale();
  const weather = type === 'weather';
  const rain = weather && weatherMetric === 'rain';
  const options = useMemo(() => {
    const clock = localizedDate({ timeZone: timezone, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
    const pointTime = (point) => weather ? point.time * 1000 : Date.parse(point.time);
    const daily = period === '7d' || period === '30d';
    const color = { weather: '#007AFF', heart: '#FF3B30', oxygen: '#007AFF', respiratory: '#208A87' }[type];
    const unit = { weather: rain ? '%' : '°C', heart: t('次/分'), oxygen: '%', respiratory: t('呼吸次数/分') }[type];
    const keys = weather ? [rain ? ['precipitation_percent', t('降水概率'), color] : ['temperature_c', t('气温'), color]] : type === 'heart' ? [
      ['max', t('最高'), '#D67E81'], ['min', t('最低'), '#E6A5A4'], ['avg', t('平均'), color],
    ] : [['avg', t('每日平均'), color]];
    return {
      ...plotStyle(dark, (value) => measure(value, unit, type === 'heart' || weather ? 0 : 1)),
      xAxis: {
        ...(daily ? { type: 'category', data: readings.map((point) => monthDay.format(new Date(point.date))), boundaryGap: false }
          : { type: 'time', splitNumber: 5, min: start, max: end }),
        axisLine: { lineStyle: { color: dark ? '#424247' : '#E0E2E6' } },
        axisLabel: { formatter: daily ? undefined : (value) => clock.format(value),
          color: '#8E8E93', hideOverlap: true, margin: 10 },
        axisTick: { show: false },
      },
      yAxis: {
        type: 'value', scale: !rain, splitNumber: 4, ...(rain ? { min: 0, max: 100 } : {}),
        axisLabel: { color: '#8E8E93', formatter: (value) => `${axisNumber.format(value)}${rain || type === 'oxygen' ? '%' : weather ? '°' : ''}` },
        splitLine: { lineStyle: { type: 'dashed', color: dark ? '#3A3A3E' : '#E4E6EB' } },
      },
      series: keys.map(([key, name, lineColor]) => ({
        name, type: 'line', smooth: 0.3, color: lineColor,
        lineStyle: { width: lineColor === color ? 2 : 1, color: lineColor, type: lineColor === color ? 'solid' : 'dotted' },
        data: daily ? readings.map((point) => point[key] ?? null) : readings.flatMap((point, index) => {
          const stamp = pointTime(point);
          const previous = index ? pointTime(readings[index - 1]) : null;
          const pair = [stamp, point[key] ?? null];
          return previous !== null && stamp - previous > 5400000 ? [[previous + 3600000, null], pair] : [pair];
        }),
        showSymbol: !weather || readings.length < 2, symbolSize: weather ? 6 : 4, connectNulls: false,
        areaStyle: lineColor === color ? { color: fill(color) } : undefined,
      })),
    };
  }, [type, weather, rain, timezone, start, end, dark, readings, period, locale]);
  const label = weather ? t('未来 24 小时{p1}曲线', { p1: rain ? t('降水概率') : t('气温') }) : type === 'heart' ? t('心率最低、平均和最高趋势')
    : type === 'oxygen' ? t('血氧趋势曲线') : t('呼吸频率趋势曲线');
  return <LinePlot options={options} label={label} />;
}
