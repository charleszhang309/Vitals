import { lazy, Suspense, useState } from 'react';
import { AnimatePresence } from 'framer-motion';
import { Activity, Battery, Cpu, Gpu, MemoryStick, Wifi, HardDrive, Fan, ArrowDown, ArrowUp, TriangleAlert } from 'lucide-react';
import { bytes, duration, measure, number, rate, rateText, time } from './format.js';
import { DetailLink, Modal, ProgressBar, Segmented } from './ui.jsx';
import HardwareDetails from './HardwareDetails.jsx';
import { PowerCard } from './HostControls.jsx';
import { batteryNote, DevicePicker, RemoteHardware } from './RemoteDevice.jsx';

const TrendChart = lazy(() => import('./TrendChart.jsx'));
const trendModes = [{ value: 'usage', label: '占用' }, { value: 'temperature', label: '温度' }];
const fanLabels = { cpu: 'CPU 风扇', gpu: 'GPU 风扇', chassis: '机箱风扇' };

function Metric({ title, icon: Icon, tone, display, unit, lines, choice, onOpen }) {
  return (
    <article className={`surface metric detail-tile tone-${tone}`} aria-label={title}>
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
    </article>
  );
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
          <div className="hardware-label"><strong>{disk.name ?? disk.mountpoint}</strong><span className="muted numeric">{measure(disk.percent, '%')}</span></div>
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

function HostMonitor({ telemetry, platform, dark, power }) {
  const { sample, history, fanPeaks, connection } = telemetry;
  const [selectedGpu, setSelectedGpu] = useState('');
  const [detail, setDetail] = useState(null);
  const [trendMode, setTrendMode] = useState('usage');
  const mac = platform === 'macos';
  const battery = sample?.battery;
  const gpus = sample?.gpus ?? [];
  const gpu = gpus.find((item) => item.id === selectedGpu) ?? gpus[0];
  const remembered = history.findLast((item) => item.gpus.length > 0)?.gpus ?? [];
  const chartGpu = gpu?.id ?? (remembered.find((item) => item.id === selectedGpu) ?? remembered[0])?.id;
  const network = rate(sample?.network?.download_bytes_per_sec);
  return (
    <>
        <div className="page-meta muted"><Activity size={16} aria-hidden="true" />
          {sample ? <span>{connection === 'online' ? '最近更新' : '最后收到'} <time dateTime={sample.timestamp} className="numeric">{time(sample.epoch)}</time></span> : <span>等待实时数据</span>}
        </div>
        <div className="metrics-grid">
          <Metric title="CPU" tone="red" icon={Cpu} onOpen={() => setDetail('cpu')} display={number(sample?.cpu.usage_percent)} unit={Number.isFinite(sample?.cpu.usage_percent) ? '%' : ''}
            lines={[["温度", measure(sample?.cpu.temperature_c, '°C')], ['功耗', measure(sample?.cpu.power_w, 'W')]]} />
          {mac && battery && <section className="surface metric" aria-label="电池">
            <div className="metric-header flex items-center gap-3"><Battery size={23} aria-hidden="true" /><h2>电池</h2></div>
            <div className="metric-reading numeric">{measure(battery?.percent, '%')}</div>
            <p className="muted">{batteryNote(battery)}</p>
            <dl className="metric-details">
              <div><dt>健康度</dt><dd>{measure(battery?.health_percent, '%')}</dd></div>
              <div><dt>循环次数</dt><dd>{number(battery?.cycle_count, 0)}</dd></div>
            </dl>
          </section>}
          {platform === 'windows' && <Metric title="GPU" tone="orange" icon={Gpu} onOpen={() => setDetail('gpu')} display={number(gpu?.usage_percent)} unit={Number.isFinite(gpu?.usage_percent) ? '%' : ''}
            lines={[["温度", measure(gpu?.temperature_c, '°C')], ['功耗', measure(gpu?.power_w, 'W')], ['显存', gpu ? `${bytes(gpu.vram_used_bytes)} / ${bytes(gpu.vram_total_bytes)}` : '—']]}
            choice={gpus.length > 1 ? <select className="gpu-choice" aria-label="选择 GPU" value={gpu.id} onChange={(event) => setSelectedGpu(event.target.value)}>
              {gpus.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
            </select> : gpu?.name && <p className="metric-device muted" title={gpu.name}>{gpu.name}</p>} />}
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
            <ul className="legend trend-legend"><li className="tone-red">CPU</li>{!mac && <li className="tone-orange">GPU</li>}
              {trendMode === 'usage' && <li className="tone-purple">内存</li>}</ul>
            <Suspense fallback={<div className="trend-plot chart-loading muted">正在加载图表…</div>}>
              <TrendChart history={history} gpuId={chartGpu} dark={dark} mode={trendMode} />
            </Suspense>
          </section>
          <div className="side-stack">{platform === 'windows' && <PowerCard controller={power} />}<TaskCard sample={sample} /></div>
          <Disks items={sample?.disks ?? []} ready={Boolean(sample)} />
          {!mac && <Fans items={sample?.fan_summary ?? []} peaks={fanPeaks} ready={Boolean(sample)} />}
        </div>
        <AnimatePresence>
          {detail && <HardwareDetails category={detail} sample={sample} gpu={gpu} onClose={() => setDetail(null)} />}
        </AnimatePresence>
    </>
  );
}

export default function MonitorPage({ telemetry, dark, power, onClose, hub, devices, device, onDevice }) {
  return <Modal id="full-monitor" title="硬件监控" className={device ? 'detail-dialog remote-monitor-dialog' : 'monitor-dialog'} onClose={onClose}>
    <div className="monitor-content">
      <DevicePicker id="monitor-device" hub={hub} devices={devices} device={device} onChange={onDevice} />
      {device ? <RemoteHardware device={device} /> : <HostMonitor telemetry={telemetry} platform={hub.platform} dark={dark} power={power} />}
    </div>
  </Modal>;
}
