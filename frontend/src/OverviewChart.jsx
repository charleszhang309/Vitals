import { useMemo } from 'react';
import LinePlot, { fill, plotStyle } from './LinePlot.jsx';
import { measure } from './format.js';

export default function OverviewChart({ type, readings, timezone, start, end, dark, period }) {
  const weather = type === 'weather';
  const options = useMemo(() => {
    const clock = new Intl.DateTimeFormat('zh-CN', { timeZone: timezone, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
    const pointTime = (point) => weather ? point.time * 1000 : Date.parse(point.time);
    const daily = period === '7d' || period === '30d';
    const color = { weather: '#007AFF', heart: '#FF3B30', oxygen: '#007AFF', respiratory: '#208A87' }[type];
    const unit = { weather: '°C', heart: '次/分', oxygen: '%', respiratory: '次/分' }[type];
    const keys = weather ? [['temperature_c', '气温', color]] : type === 'heart' ? [
      ['max', '最高', '#D67E81'], ['min', '最低', '#E6A5A4'], ['avg', '平均', color],
    ] : [['avg', '每日平均', color]];
    return {
      ...plotStyle(dark, (value) => measure(value, unit, type === 'heart' || weather ? 0 : 1)),
      xAxis: {
        ...(daily ? { type: 'category', data: readings.map((point) => point.date), boundaryGap: false }
          : { type: 'time', splitNumber: 5, min: start, max: end }),
        axisLine: { lineStyle: { color: dark ? '#424247' : '#E0E2E6' } },
        axisLabel: { formatter: (value) => daily ? value.slice(5).replace('-', '/') : clock.format(value),
          color: '#8E8E93', hideOverlap: true, margin: 10 },
        axisTick: { show: false },
      },
      yAxis: {
        type: 'value', scale: true, splitNumber: 4,
        axisLabel: { color: '#8E8E93', formatter: weather ? '{value}°' : type === 'oxygen' ? '{value}%' : '{value}' },
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
  }, [type, weather, timezone, start, end, dark, readings, period]);
  const label = weather ? '未来 24 小时气温曲线' : type === 'heart' ? '心率最低、平均和最高趋势'
    : type === 'oxygen' ? '血氧趋势曲线' : '呼吸频率趋势曲线';
  return <LinePlot options={options} label={label} />;
}
