import { lazy, Suspense, useEffect, useState } from 'react';
import { motion } from 'framer-motion';
import { Activity, Cpu, Gpu, MemoryStick, Wifi, HardDrive, Fan, ArrowDown, ArrowUp, TriangleAlert, RotateCw, LockKeyhole, MonitorOff } from 'lucide-react';
import { postJson, request } from './api.js';
import { bytes, duration, measure, number, rate, rateText, time } from './format.js';
import { Button, DetailLink, IconButton, Modal, ProgressBar, Segmented } from './ui.jsx';
import HardwareDetails from './HardwareDetails.jsx';

const TrendChart = lazy(() => import('./TrendChart.jsx'));
const POWER_ENDPOINT = '/api/control/power';
const powerModes = [{ value: 'silent', label: '静音' }, { value: 'balanced', label: '平衡' }, { value: 'performance', label: '性能' }];
const trendModes = [{ value: 'usage', label: '占用' }, { value: 'temperature', label: '温度' }];
const fanLabels = { cpu: 'CPU 风扇', gpu: 'GPU 风扇', chassis: '机箱风扇' };

function Metric({ title, icon: Icon, tone, display, unit, lines, choice, onOpen }) {
  return (
    <motion.article className={`surface metric detail-tile tone-${tone}`} aria-label={title} whileTap={{ scale: 0.97 }}>
      <div className="metric-header flex items-center gap-3">
        <span className="metric-icon"><Icon size={23} strokeWidth={1.9} aria-hidden="true" /></span>
        <h2>{title}</h2>
      </div>
      <div className="metric-reading numeric" title={unit || undefined}>{display ?? '—'}{unit && <span>{unit}</span>}</div>
      <dl className="metric-details">
        {lines.map(([label, value, Direction]) => (
          <div key={label} className="flex items-center gap-2">
            <dt className="flex items-center gap-1">{Direction && <Direction size={13} aria-hidden="true" />}{label}</dt>
            <dd className="numeric">{value}</dd>
          </div>
        ))}
      </dl>
      <DetailLink title={title} onClick={onOpen} />
      {choice}
    </motion.article>
  );
}

function PowerCard() {
  const [power, setPower] = useState(null);
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState(null);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    request(POWER_ENDPOINT, { signal: controller.signal }).then(setPower).catch((error) => {
      if (!controller.signal.aborted) setFeedback({ kind: 'error', message: error.message });
    });
    return () => controller.abort();
  }, [reload]);

  async function select(mode) {
    if (busy) return;
    setBusy(true);
    setFeedback(null);
    try {
      const result = await postJson(POWER_ENDPOINT, { mode });
      setPower((previous) => ({ ...previous, mode: result.mode, guid: result.guid, name: null }));
      if (result.fan_control?.status === 'failed') setFeedback({ kind: 'error', message: result.fan_control.message });
    } catch (error) {
      setFeedback({ kind: 'error', message: error.message });
    } finally {
      setBusy(false);
    }
  }

  const options = powerModes.map((mode) => ({ ...mode, disabled: !power?.modes.includes(mode.value) }));
  return (
    <section className="surface panel power-card" aria-labelledby="power-heading">
      <div className="panel-header"><h2 id="power-heading">电源模式</h2>
        {!power && feedback && <IconButton label="重试电源计划查询" onClick={() => { setFeedback(null); setReload((value) => value + 1); }}><RotateCw size={16} /></IconButton>}
      </div>
      <Segmented id="power" label="电源模式" options={options} value={power?.mode} onChange={select} disabled={busy} />
      {(busy || !power && !feedback) && <p className="muted subtle-line" role="status">{busy ? '正在切换…' : '正在读取电源模式…'}</p>}
      {feedback && <p className="feedback error" role="alert">{feedback.message}</p>}
    </section>
  );
}

function QuickControlCard() {
  const [schedule, setSchedule] = useState(null);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState(null);
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
    setBusy(true);
    setConfirm(null);
    setFeedback(null);
    try {
      if (action === 'cancel') {
        setSchedule(await request('/api/control/shutdown', { method: 'DELETE' }));
        setFeedback({ text: '已取消定时关机' });
      } else if (typeof action === 'number') {
        setSchedule(await postJson('/api/control/shutdown', { minutes: action, confirmed: true }));
        setFeedback({ text: `已安排 ${action} 分钟后关机` });
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

  const locked = confirm === 'lock';
  return <section className="surface panel quick-card" aria-labelledby="quick-control-heading">
    <div className="panel-header"><h2 id="quick-control-heading">主机快捷控制</h2></div>
    <div className="quick-control-row">
      <Button onClick={() => setConfirm('lock')} disabled={busy}><LockKeyhole size={16} aria-hidden="true" />锁屏</Button>
      <Button onClick={() => run('display-off')} disabled={busy}><MonitorOff size={16} aria-hidden="true" />关闭显示器</Button>
    </div>
    <div className="quick-control-row">
      <span>定时关机</span>
      {[30, 60].map((minutes) => <Button key={minutes} onClick={() => setConfirm(minutes)} disabled={busy || Boolean(schedule?.shutdown_at)}>{minutes}分钟</Button>)}
      <Button onClick={() => run('cancel')} disabled={busy || !schedule?.shutdown_at}>取消</Button>
    </div>
    {schedule?.shutdown_at && <p className="muted subtle-line">预计关机 <time className="numeric" dateTime={schedule.shutdown_at}>{time(schedule.shutdown_at, true)}</time></p>}
    {(busy || feedback) && <p className={`feedback ${feedback?.error ? 'error' : ''}`} role={feedback?.error ? 'alert' : 'status'}>{busy ? '正在处理…' : feedback.text}</p>}
    {confirm !== null && <Modal id="confirm-quick-control" className="detail-dialog" title={locked ? '确认锁屏' : '确认定时关机'} onClose={() => setConfirm(null)}>
      <p>{locked ? 'Windows 将锁定当前桌面，解锁需要重新验证身份。' : `Windows 将在 ${confirm} 分钟后关机。到时前可以取消。`}</p>
      <div className="quick-confirm-actions">
        <Button onClick={() => setConfirm(null)}>取消</Button>
        <Button onClick={() => run(confirm)}>{locked ? '确认锁屏' : '确认定时关机'}</Button>
      </div>
    </Modal>}
  </section>;
}

function TaskCard({ sample }) {
  const task = sample?.task;
  const status = { idle: '空闲', running: '高负载运行中', completed: '已完工' };
  const elapsed = task && duration(task.started_at, task.completed_at ?? sample.timestamp);
  const device = task?.source === 'cpu' ? 'CPU' : (sample?.gpus.find((gpu) => gpu.id === task?.source)?.name ?? task?.source);
  const alerts = [...(sample?.last_alerts ?? [])].sort((a, b) => Date.parse(b.timestamp) - Date.parse(a.timestamp));
  return (
    <section className="surface panel task-card" aria-labelledby="task-heading">
      <div className="panel-header"><h2 id="task-heading">任务与告警</h2></div>
      <div className="task-summary flex flex-wrap items-center gap-2">
        <span className={`state-dot ${task?.state === 'running' ? 'live-dot pulse' : task?.state === 'completed' ? 'live-dot' : ''}`} aria-hidden="true" />
        <span className={task?.state === 'completed' ? 'completed' : ''}>{status[task?.state] ?? '等待任务状态'}</span>
        {elapsed && <span className="muted numeric">{elapsed}</span>}
      </div>
      {device && <p className="muted task-device">{device}</p>}
      <div className="alert-area">
        {alerts.length ? <ul className="alerts">
          {alerts.map((alert) => <li key={`${alert.type}-${alert.timestamp}`} className={alert.type === 'disk_space' ? 'alert warning' : 'alert critical'}>
            <TriangleAlert size={17} aria-hidden="true" />
            <div><div className="alert-heading"><strong>{alert.title}</strong><time className="numeric" dateTime={alert.timestamp}>{time(alert.timestamp)}</time></div><p>{alert.message}</p></div>
          </li>)}
        </ul> : <p className="muted">{sample ? '暂无告警' : '等待监控数据'}</p>}
      </div>
    </section>
  );
}

function Disks({ items, ready }) {
  return (
    <section className="surface panel" aria-labelledby="disk-heading">
      <div className="panel-header"><h2 id="disk-heading">磁盘分区</h2></div>
      {items.length ? <ul className="hardware-list">
        {items.map((disk) => <li className="hardware-row disk-row" key={disk.mountpoint}>
          <span className="hardware-icon"><HardDrive size={19} aria-hidden="true" /></span>
          <div className="hardware-label"><strong>{disk.mountpoint}</strong><span className="muted numeric">{measure(disk.percent, '%')}</span></div>
          <ProgressBar value={disk.percent} label={`${disk.mountpoint} 使用率`} />
          <div className="hardware-caption muted numeric">剩余 {bytes(disk.free_bytes)} <span>/ {bytes(disk.total_bytes)}</span></div>
        </li>)}
      </ul> : <p className="muted section-empty">{ready ? '暂无可读取的磁盘分区' : '等待磁盘数据'}</p>}
    </section>
  );
}

function Fans({ items, peaks, ready }) {
  return (
    <section className="surface panel" aria-labelledby="fan-heading">
      <div className="panel-header"><h2 id="fan-heading">风扇转速</h2>{items.length > 0 && <span className="muted panel-note">相对本页峰值</span>}</div>
      {items.length ? <ul className="hardware-list">
        {items.map((fan) => (
          <li className="hardware-row fan-row" key={fan.id}>
            <span className="hardware-icon"><Fan size={19} aria-hidden="true" /></span>
            <div className="flex-1 min-w-0"><div className="hardware-label"><strong>{fanLabels[fan.group]}</strong><span className="numeric">{measure(fan.rpm, 'RPM', 0)}</span></div>
              <ProgressBar value={peaks[fan.id] ? fan.rpm * 100 / peaks[fan.id] : 100} tone="blue" label={`${fanLabels[fan.group]} 相对本页峰值`} />
            </div>
          </li>
        ))}
      </ul> : <div className="fan-empty"><Fan size={26} aria-hidden="true"/><p>{ready ? '设备未提供风扇转速数据' : '等待风扇数据'}</p></div>}
    </section>
  );
}

export default function MonitorPage({ telemetry, dark }) {
  const { sample, history, fanPeaks, connection } = telemetry;
  const [selectedGpu, setSelectedGpu] = useState('');
  const [detail, setDetail] = useState(null);
  const [trendMode, setTrendMode] = useState('usage');
  const gpus = sample?.gpus ?? [];
  const gpu = gpus.find((item) => item.id === selectedGpu) ?? gpus[0];
  const remembered = history.findLast((item) => item.gpus.length > 0)?.gpus ?? [];
  const chartGpu = gpu?.id ?? (remembered.find((item) => item.id === selectedGpu) ?? remembered[0])?.id;
  const network = rate(sample?.network?.download_bytes_per_sec);
  return (
    <>
      <div className="page-heading">
        <h1>硬件监控</h1>
        <div className="page-meta muted"><Activity size={16} aria-hidden="true" />
          {sample ? <span>{connection === 'online' ? '最近更新' : '最后收到'} <time dateTime={sample.timestamp} className="numeric">{time(sample.epoch)}</time></span> : <span>等待实时数据</span>}
        </div>
      </div>
      <div className="metrics-grid">
        <Metric title="CPU" tone="red" icon={Cpu} onOpen={() => setDetail('cpu')} display={number(sample?.cpu.usage_percent)} unit={Number.isFinite(sample?.cpu.usage_percent) ? '%' : ''}
          lines={[["温度", measure(sample?.cpu.temperature_c, '°C')], ['功耗', measure(sample?.cpu.power_w, 'W')]]} />
        <Metric title="GPU" tone="orange" icon={Gpu} onOpen={() => setDetail('gpu')} display={number(gpu?.usage_percent)} unit={Number.isFinite(gpu?.usage_percent) ? '%' : ''}
          lines={[["温度", measure(gpu?.temperature_c, '°C')], ['功耗', measure(gpu?.power_w, 'W')], ['显存', gpu ? `${bytes(gpu.vram_used_bytes)} / ${bytes(gpu.vram_total_bytes)}` : '—']]}
          choice={gpus.length > 1 ? <select className="gpu-choice" aria-label="选择 GPU" value={gpu.id} onChange={(event) => setSelectedGpu(event.target.value)}>
            {gpus.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
          </select> : gpu?.name && <p className="metric-device muted" title={gpu.name}>{gpu.name}</p>} />
        <Metric title="内存" tone="purple" icon={MemoryStick} onOpen={() => setDetail('memory')} display={number(sample?.memory.percent)} unit={Number.isFinite(sample?.memory.percent) ? '%' : ''}
          lines={[["已用", bytes(sample?.memory.used_bytes)], ['总量', bytes(sample?.memory.total_bytes)]]} />
        <Metric title="网络" tone="blue" icon={Wifi} onOpen={() => setDetail('network')} display={network.value} unit={network.unit}
          lines={[["上传", rateText(sample?.network?.upload_bytes_per_sec), ArrowUp], ['下载', rateText(sample?.network?.download_bytes_per_sec), ArrowDown]]} />
      </div>
      <div className="monitor-grid">
        <section className="surface panel trend-card" aria-labelledby="trend-heading">
          <div className="panel-header trend-header">
            <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1"><h2 id="trend-heading">实时曲线</h2><span className="muted panel-note">最近 2 分钟</span></div>
            <Segmented id="trend" label="实时曲线显示指标" options={trendModes} value={trendMode} onChange={setTrendMode} />
          </div>
          <ul className="legend trend-legend"><li className="tone-red">CPU</li><li className="tone-orange">GPU</li>
            {trendMode === 'usage' && <li className="tone-purple">内存</li>}</ul>
          <Suspense fallback={<div className="trend-plot chart-loading muted">正在加载图表…</div>}>
            <TrendChart history={history} gpuId={chartGpu} dark={dark} mode={trendMode} />
          </Suspense>
        </section>
        <div className="side-stack"><PowerCard /><TaskCard sample={sample} /></div>
        <Disks items={sample?.disks ?? []} ready={Boolean(sample)} />
        <Fans items={sample?.fan_summary ?? []} peaks={fanPeaks} ready={Boolean(sample)} />
      </div>
      <QuickControlCard />
      {detail && <HardwareDetails category={detail} sample={sample} gpu={gpu} onClose={() => setDetail(null)} />}
    </>
  );
}
