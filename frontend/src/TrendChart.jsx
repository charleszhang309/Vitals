import { useMemo } from 'react';
import LinePlot, { fill, plotStyle } from './LinePlot.jsx';
import { measure, time } from './format.js';

const names = ['CPU', 'GPU', '内存'];
const colors = ['#FF3B30', '#FF9500', '#5856D6'];

export default function TrendChart({ history, gpuId, dark, mode }) {
  const temperature = mode === 'temperature';
  const options = useMemo(() => {
    const end = history.at(-1)?.epoch ?? Date.now();
    const lines = temperature ? [[], []] : [[], [], []];
    const key = temperature ? 'temperature_c' : 'usage_percent';
    const unit = temperature ? '°C' : '%';
    let previous;
    for (const point of history) {
      if (previous) {
        const gap = point.epoch - previous.epoch;
        const interval = Math.max(previous.sampling_interval_seconds ?? 0, point.sampling_interval_seconds ?? 0);
        if (gap > Math.max(4500, interval * 1500)) {
          for (const line of lines) line.push([previous.epoch + gap / 2, null]);
        }
      }
      lines[0].push([point.epoch, point.cpu[key] ?? null]);
      lines[1].push([point.epoch, point.gpus.find((item) => item.id === gpuId)?.[key] ?? null]);
      if (!temperature) lines[2].push([point.epoch, point.memory.percent]);
      previous = point;
    }
    return {
      ...plotStyle(dark, (value) => measure(value, unit)),
      color: colors,
      xAxis: {
        type: 'time', min: end - 120_000, max: end, splitNumber: 5,
        axisLine: { lineStyle: { color: dark ? '#424247' : '#E0E2E6' } },
        axisLabel: { formatter: (value) => time(value), color: '#8E8E93', hideOverlap: true, margin: 10 },
        axisTick: { show: false },
      },
      yAxis: {
        type: 'value', ...(temperature ? { scale: true, splitNumber: 4 } : { min: 0, max: 100, interval: 25 }),
        axisLabel: { color: '#8E8E93', formatter: `{value}${unit}` },
        splitLine: { lineStyle: { type: 'dashed', color: dark ? '#3A3A3E' : '#E4E6EB' } },
      },
      series: lines.map((data, index) => ({
        name: names[index], data, type: 'line', smooth: 0.3,
        connectNulls: false, showSymbol: history.length < 2, symbolSize: 5,
        lineStyle: { width: 2, color: colors[index] }, areaStyle: { color: fill(colors[index]) },
      })),
    };
  }, [history, gpuId, dark, temperature]);
  return <LinePlot options={options} label={temperature ? '最近两分钟 CPU 和 GPU 温度折线图' : '最近两分钟 CPU、GPU 和内存占用率折线图'} />;
}
