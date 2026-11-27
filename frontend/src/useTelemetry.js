import { useEffect, useRef, useState } from 'react';
import { metricsUrl } from './api.js';

const WINDOW_MS = 120_000;
const STALE_MS = 9000;
const FRAME_GRACE_MS = 5000;
const IDLE_INTERVAL_SECONDS = 15;
const ACTIVITY_IDLE_MS = 60_000;
const INTERACTION_EVENTS = ['pointerdown', 'pointermove', 'keydown', 'wheel', 'scroll'];

export function useTelemetry(enabled = true, { activeTelemetry = false } = {}) {
  const [connection, setConnection] = useState('connecting');
  const [history, setHistory] = useState([]);
  const [fanPeaks, setFanPeaks] = useState({});
  const clientState = useRef({ activeTelemetry, publish: null });

  useEffect(() => {
    clientState.current.activeTelemetry = activeTelemetry;
    clientState.current.publish?.();
  }, [activeTelemetry]);

  useEffect(() => {
    if (!enabled) return undefined;
    let socket;
    let retry;
    let idleTimer;
    let startedAt;
    let lastFrameAt;
    let lastClientState;
    let lastInteractionAt = Date.now();
    let staleAfter = STALE_MS;
    let attempts = 0;
    let disposed = false;

    function publishClientState() {
      if (socket?.readyState !== WebSocket.OPEN) return;
      const visible = !document.hidden;
      const active = visible && clientState.current.activeTelemetry && Date.now() - lastInteractionAt < ACTIVITY_IDLE_MS;
      if (lastClientState?.visible === visible && lastClientState.active_telemetry === active) return;
      lastClientState = { type: 'client_state', visible, active_telemetry: active };
      socket.send(JSON.stringify(lastClientState));
    }

    function checkActivity() {
      idleTimer = undefined;
      publishClientState();
      const remaining = lastInteractionAt + ACTIVITY_IDLE_MS - Date.now();
      if (!document.hidden && remaining > 0) idleTimer = window.setTimeout(checkActivity, remaining);
    }

    function recordInteraction() {
      if (document.hidden) return;
      lastInteractionAt = Date.now();
      if (idleTimer === undefined) {
        checkActivity();
      } else if (lastClientState?.active_telemetry === false && clientState.current.activeTelemetry) {
        publishClientState();
      }
    }

    function retrySocket(current) {
      if (disposed || socket !== current) return;
      socket = undefined;
      if (current.readyState < WebSocket.CLOSING) current.close();
      setConnection('offline');
      if (!document.hidden) {
        retry = window.setTimeout(connect, Math.min(750 * 2 ** attempts, 10_000));
        attempts = Math.min(attempts + 1, 4);
      }
    }

    function connect() {
      if (disposed || document.hidden || socket) return;
      retry = undefined;
      startedAt = Date.now();
      lastFrameAt = undefined;
      lastClientState = undefined;
      staleAfter = STALE_MS;
      setConnection('connecting');
      const current = new WebSocket(metricsUrl());
      socket = current;

      current.onopen = () => {
        if (disposed || socket !== current) return;
        startedAt = Date.now();
        staleAfter = IDLE_INTERVAL_SECONDS * 1000 + FRAME_GRACE_MS;
        setConnection('online');
        publishClientState();
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
        const interval = sample.sampling_interval_seconds;
        const samplingSeconds = Number.isFinite(interval) && interval > 0 ? interval : IDLE_INTERVAL_SECONDS;
        staleAfter = Math.max(STALE_MS, samplingSeconds * 1000 + FRAME_GRACE_MS);
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
      current.onerror = current.onclose = () => retrySocket(current);
    }

    function checkFreshness() {
      if (document.hidden) return;
      if (socket && Date.now() - (lastFrameAt ?? startedAt) > staleAfter) {
        // 网络切换后，旧连接可能没有及时发出 close 事件。
        retrySocket(socket);
      }
    }

    function disconnect() {
      window.clearTimeout(retry);
      retry = undefined;
      const current = socket;
      socket = undefined;
      current?.close();
    }

    function visibilityChanged() {
      if (document.hidden) {
        // 后台标签页释放订阅，让中心端在没有查看者时降频。
        window.clearTimeout(idleTimer);
        idleTimer = undefined;
        publishClientState();
        disconnect();
      } else {
        recordInteraction();
        window.clearTimeout(retry);
        attempts = 0;
        connect();
      }
    }

    clientState.current.publish = publishClientState;
    recordInteraction();
    connect();
    const heartbeat = window.setInterval(checkFreshness, 2500);
    document.addEventListener('visibilitychange', visibilityChanged);
    for (const event of INTERACTION_EVENTS) {
      window.addEventListener(event, recordInteraction, { capture: true, passive: true });
    }
    return () => {
      disposed = true;
      clientState.current.publish = null;
      window.clearTimeout(idleTimer);
      window.clearInterval(heartbeat);
      document.removeEventListener('visibilitychange', visibilityChanged);
      for (const event of INTERACTION_EVENTS) window.removeEventListener(event, recordInteraction, true);
      disconnect();
    };
  }, [enabled]);

  return { connection, sample: history.at(-1), history, fanPeaks };
}
