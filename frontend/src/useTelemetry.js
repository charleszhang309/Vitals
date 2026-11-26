import { useEffect, useState } from 'react';
import { metricsUrl } from './api.js';

const WINDOW_MS = 120_000;
const STALE_MS = 9000;

export function useTelemetry(enabled = true) {
  const [connection, setConnection] = useState('connecting');
  const [history, setHistory] = useState([]);
  const [fanPeaks, setFanPeaks] = useState({});

  useEffect(() => {
    if (!enabled) return undefined;
    let socket;
    let retry;
    let startedAt;
    let lastFrameAt;
    let attempts = 0;
    let disposed = false;

    function connect() {
      if (disposed) return;
      retry = undefined;
      startedAt = Date.now();
      lastFrameAt = undefined;
      setConnection('connecting');
      const current = new WebSocket(metricsUrl());
      socket = current;

      current.onopen = () => {
        if (!disposed) setConnection('online');
      };
      current.onmessage = (event) => {
        if (disposed || socket !== current) return;
        let sample;
        try {
          sample = JSON.parse(event.data);
        } catch {
          return;
        }
        const epoch = Date.parse(sample?.timestamp);
        if (!Number.isFinite(epoch) || !sample.cpu || !sample.memory || !Array.isArray(sample.gpus)) return;
        attempts = 0;
        lastFrameAt = Date.now();
        setHistory((previous) => {
          const retained = previous.filter((point) => point.epoch > epoch - WINDOW_MS && point.epoch < epoch);
          return [...retained, { ...sample, epoch }];
        });
        setFanPeaks((previous) => {
          const peaks = { ...previous };
          let updated = false;
          for (const fan of sample.fan_summary ?? []) {
            if (Number.isFinite(fan.rpm) && fan.rpm > (peaks[fan.id] ?? 0)) {
              peaks[fan.id] = fan.rpm;
              updated = true;
            }
          }
          return updated ? peaks : previous;
        });
      };
      current.onerror = () => current.close();
      current.onclose = () => {
        if (disposed || socket !== current) return;
        socket = undefined;
        setConnection('offline');
        retry = window.setTimeout(connect, Math.min(750 * 2 ** attempts, 10_000));
        attempts = Math.min(attempts + 1, 4);
      };
    }

    function checkFreshness() {
      if (document.hidden) return;
      if (socket && socket.readyState < WebSocket.CLOSING && Date.now() - (lastFrameAt ?? startedAt) > STALE_MS) {
        // 锁屏或网络切换后，主动结束长时间没有快照的连接。
        socket.close();
      }
    }

    connect();
    const heartbeat = window.setInterval(checkFreshness, 2500);
    document.addEventListener('visibilitychange', checkFreshness);
    return () => {
      disposed = true;
      window.clearTimeout(retry);
      window.clearInterval(heartbeat);
      document.removeEventListener('visibilitychange', checkFreshness);
      socket?.close();
    };
  }, [enabled]);

  return { connection, sample: history.at(-1), history, fanPeaks };
}
