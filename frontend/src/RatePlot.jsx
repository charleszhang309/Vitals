import { getLocale, localizedDate, localizedNumber, t } from './i18n/messages.js';
import { useMemo } from 'react';
import LinePlot, { fill, plotStyle } from './LinePlot.jsx';

const formatter = localizedNumber({ maximumSignificantDigits: 6 });
const dateFormat = localizedDate({ month: 'short', day: 'numeric', timeZone: 'UTC' });
const hourFormat = localizedDate({ hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
const color = '#007aff';

export default function RatePlot({ data, dark }) {
  const locale = getLocale();
  const options = useMemo(() => {
    const hourly = data.granularity === 'hourly_snapshot';
    return {
      ...plotStyle(dark, (value) => formatter.format(value)),
      grid: { top: 12, right: 10, bottom: 24, left: 56 },
      xAxis: {
        type: 'time', splitNumber: 4,
        axisLabel: { color: '#8E8E93', formatter: (value) => (hourly ? hourFormat : dateFormat).format(value), hideOverlap: true },
        axisTick: { show: false },
      },
      yAxis: {
        type: 'value', scale: true, splitNumber: 3,
        axisLabel: { color: '#8E8E93', formatter: (value) => formatter.format(value) },
        splitLine: { lineStyle: { type: 'dashed', color: dark ? '#3A3A3E' : '#E4E6EB' } },
      },
      series: [{
        type: 'line', name: `${data.base} → ${data.target}`, color,
        data: data.points.map(({ time, rate }) => [Date.parse(time), rate]),
        showSymbol: data.points.length < 4, symbolSize: 7, lineStyle: { width: 2 }, areaStyle: { color: fill(color) },
      }],
    };
  }, [data, dark, locale]);
  return <LinePlot options={options} label={t('{p1} → {p2} 汇率走势图', { p1: data.base, p2: data.target })} />;
}
