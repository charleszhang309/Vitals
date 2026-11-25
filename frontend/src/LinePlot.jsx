import { useEffect, useRef } from 'react';
import { init, use } from 'echarts/core';
import { LineChart } from 'echarts/charts';
import { GridComponent, TooltipComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';

use([LineChart, GridComponent, TooltipComponent, CanvasRenderer]);

export function plotStyle(dark, formatValue) {
  return {
    animation: false,
    textStyle: { fontFamily: 'system-ui, sans-serif', fontSize: 11 },
    grid: { top: 12, right: 11, bottom: 26, left: 43 },
    tooltip: {
      trigger: 'axis', renderMode: 'richText', confine: true,
      backgroundColor: dark ? '#252528' : '#FFFFFF', borderColor: dark ? '#39393D' : '#E6E6EA',
      textStyle: { color: dark ? '#F5F5F7' : '#1C1C1E', fontSize: 12 }, valueFormatter: formatValue,
    },
  };
}

export function fill(color) {
  return { type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
    colorStops: [{ offset: 0, color: `${color}2F` }, { offset: 1, color: `${color}04` }] };
}

export default function LinePlot({ options, label }) {
  const surface = useRef(null);
  const chart = useRef(null);

  useEffect(() => {
    const instance = init(surface.current, null, { renderer: 'canvas' });
    chart.current = instance;
    const observer = new ResizeObserver(() => instance.resize());
    observer.observe(surface.current);
    return () => {
      observer.disconnect();
      instance.dispose();
      chart.current = null;
    };
  }, []);

  useEffect(() => { chart.current?.setOption(options, { notMerge: true }); }, [options]);
  return <div ref={surface} className="trend-plot" role="img" aria-label={label} />;
}
