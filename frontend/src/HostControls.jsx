import { useEffect, useId, useState } from 'react';
import { Activity, Lock, MonitorOff, Power, RotateCw, Timer, XCircle } from 'lucide-react';
import { postJson, request } from './api.js';
import { measure, rateText, time } from './format.js';
import { Button, DetailLink, IconButton, Modal, Segmented } from './ui.jsx';
import useResource from './useOverviewResource.js';

const powerModes = [{ value: 'silent', label: '静音' }, { value: 'balanced', label: '平衡' }, { value: 'performance', label: '性能' }];
const powerPath = '/api/control/power';

export function usePowerControls(enabled) {
  const resource = useResource(powerPath, 15_000, enabled);
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState('');

  async function select(mode) {
    if (busy) return;
    setBusy(true);
    setFeedback('');
    try {
      const result = await postJson(powerPath, { mode });
      resource.replace({ ...resource.data, mode: result.mode, guid: result.guid, name: null });
      if (result.fan_control?.status === 'failed') setFeedback(result.fan_control.message);
    } catch (error) {
      setFeedback(error.message);
    } finally {
      setBusy(false);
    }
  }

  return { ...resource, busy, feedback, select };
}

export function PowerCard({ controller }) {
  const id = useId();
  const { data, busy, feedback, error, retry, select } = controller;
  const options = powerModes.map((mode) => ({ ...mode, disabled: !data?.modes?.includes(mode.value) }));
  return <section className="surface panel power-card" aria-labelledby={id}>
    <div className="panel-header"><h2 id={id}>电源模式</h2>
      {!data && error && <IconButton label="重试电源计划查询" onClick={retry}><RotateCw size={16} /></IconButton>}
    </div>
    <Segmented id={id} label="电源模式" options={options} value={data?.mode} onChange={select} disabled={busy} />
    {(busy || (!data && !error)) && <p className="muted subtle-line" role="status">{busy ? '正在切换…' : '正在读取电源模式…'}</p>}
    {(feedback || error) && <p className="feedback error" role="alert">{feedback || error}</p>}
  </section>;
}
export function HardwareSummary({ sample, power, onOpen }) {
  const gpu = sample?.gpus[0];
  const network = sample?.network;
  const plan = power ?? sample?.power;
  const mode = powerModes.find(({ value }) => value === plan?.mode)?.label ?? plan?.name ?? (plan ? '自定义' : '—');
  const readings = [
    ['CPU', measure(sample?.cpu.usage_percent, '%', 0), measure(sample?.cpu.temperature_c, '°C', 0)],
    ['GPU', measure(gpu?.usage_percent, '%', 0), measure(gpu?.temperature_c, '°C', 0)],
    ['内存', measure(sample?.memory.percent, '%', 0)],
    ['网络', `↓ ${rateText(network?.download_bytes_per_sec)}`, `↑ ${rateText(network?.upload_bytes_per_sec)}`],
    ['电源模式', mode],
  ];
  return <section className="surface panel detail-tile control-hardware">
    <div className="panel-header"><span className="metric-icon tone-blue" aria-hidden="true"><Activity size={21} /></span><h2>硬件监控</h2></div>
    <dl>{readings.map(([label, value, secondary]) => <div key={label}>
      <dt>{label}</dt><dd className="numeric">{value}{secondary && <small>{secondary}</small>}</dd>
    </div>)}</dl>
    <DetailLink title="硬件监控" onClick={onOpen} />
  </section>;
}

function ShutdownCountdown({ deadline }) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 250);
    return () => window.clearInterval(timer);
  }, [deadline]);
  const seconds = Math.max(0, Math.ceil((Date.parse(deadline) - now) / 1000));
  const hours = Math.floor(seconds / 3600);
  const pad = (value) => String(value).padStart(2, '0');
  return <p className="shutdown-countdown" title={`预计关机 ${time(deadline, true)}`}>
    <span>距离关机</span><strong className="numeric" role="timer">{hours ? `${hours}:` : ''}{pad(Math.floor(seconds / 60) % 60)}:{pad(seconds % 60)}</strong>
  </p>;
}

export function QuickControlCard() {
  const [schedule, setSchedule] = useState(null);
  const [busy, setBusy] = useState(false);
  const [dialog, setDialog] = useState(null);
  const [draft, setDraft] = useState('');
  const [feedback, setFeedback] = useState(null);
  useEffect(() => {
    if (busy) return undefined;
    const controller = new AbortController();
    async function load() {
      try {
        const result = await request('/api/control/quick', { signal: controller.signal });
        if (!controller.signal.aborted) setSchedule(result);
      } catch {
        if (!controller.signal.aborted) setSchedule(null);
      }
    }
    load();
    const timer = window.setInterval(load, 15_000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [busy]);

  async function run(action) {
    if (busy) return;
    setBusy(true);
    setDialog(null);
    setFeedback(null);
    try {
      if (action === 'cancel') {
        setSchedule(await request('/api/control/shutdown', { method: 'DELETE' }));
        setFeedback({ text: '已取消定时关机' });
      } else if (typeof action === 'number') {
        setSchedule(await postJson('/api/control/shutdown', { minutes: action, confirmed: true }));
      } else {
        await postJson('/api/control/desktop', { action, confirmed: action === 'lock' });
        setFeedback({ text: action === 'lock' ? 'Windows 已接受锁屏请求' : 'Windows 已接受关闭显示器请求' });
      }
    } catch (error) {
      setFeedback({ text: error.message, error: true });
    } finally {
      setBusy(false);
    }
  }

  const minutes = Number(draft);
  const valid = /^\d+$/.test(draft) && minutes >= 1 && minutes <= 1440;
  const deadline = schedule?.shutdown_at;
  const unavailable = busy || !schedule || Boolean(deadline);
  const locked = dialog === 'lock';
  const close = () => setDialog(null);
  return <section className="surface panel quick-card" aria-labelledby="quick-control-heading">
    <div className="panel-header"><h2 id="quick-control-heading">主机快捷控制</h2></div>
    <div className="quick-control-row" role="group" aria-label="主机操作">
      <Button onClick={() => setDialog('lock')} disabled={busy} aria-haspopup="dialog"><Lock size={15} /><span>锁屏</span></Button>
      <Button onClick={() => run('display-off')} disabled={busy}><MonitorOff size={15} /><span>关闭显示器</span></Button>
      <Button className="shutdown-button" onClick={() => setDialog('immediate')} disabled={unavailable} aria-haspopup="dialog"><Power size={15} /><span>立即关机</span></Button>
      <Button onClick={() => { setDraft(''); setDialog('schedule'); }} disabled={unavailable} aria-haspopup="dialog"><Timer size={15} /><span>定时关机</span></Button>
      <Button className="cancel-shutdown" onClick={() => run('cancel')} disabled={busy || !deadline}><XCircle size={15} /><span>取消关机</span></Button>
    </div>
    {deadline && <ShutdownCountdown deadline={deadline} />}
    {(busy || feedback) && <p className={`feedback ${feedback?.error ? 'error' : ''}`} role={feedback?.error ? 'alert' : 'status'}>{busy ? '正在处理…' : feedback.text}</p>}
    {dialog === 'schedule' && <Modal id="schedule-shutdown" className="detail-dialog shutdown-dialog" title="定时关机" onClose={close}>
      <form className="shutdown-form" onSubmit={(event) => {
        event.preventDefault();
        if (!unavailable && valid) run(minutes);
      }}>
        <div className="shutdown-presets" role="group" aria-label="关机时间预设">
          {[30, 60].map((preset) => <Button key={preset} aria-pressed={draft === String(preset)}
            onClick={() => setDraft(String(preset))}>{preset}分钟</Button>)}
        </div>
        <label>自定义关机分钟数
          <input type="number" className="numeric" min="1" max="1440" step="1" inputMode="numeric" value={draft}
            placeholder="1–1440 分钟" onChange={(event) => setDraft(event.target.value)} />
        </label>
        {valid && <p className="muted">Windows 将在 {minutes} 分钟后关机，倒计时结束前可取消。</p>}
        <div className="quick-confirm-actions">
          <Button onClick={close}>取消</Button>
          <Button className="primary-button" type="submit" disabled={unavailable || !valid}>确认</Button>
        </div>
      </form>
    </Modal>}
    {(locked || dialog === 'immediate') && <Modal id="confirm-quick-control" className="detail-dialog shutdown-dialog"
      title={locked ? '确认锁屏' : '确认 1 分钟后关机'} onClose={close}>
      <p>{locked ? 'Windows 将锁定当前桌面，解锁需要重新验证身份。' : 'Windows 将在 1 分钟后关机，倒计时结束前可取消。'}</p>
      <div className="quick-confirm-actions">
        <Button onClick={close}>取消</Button>
        <Button onClick={() => run(locked ? 'lock' : 1)} disabled={locked ? busy : unavailable}>{locked ? '确认锁屏' : '确认关机'}</Button>
      </div>
    </Modal>}
  </section>;
}
