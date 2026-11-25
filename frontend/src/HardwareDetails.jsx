import { bytes, measure, rateText } from './format.js';
import { DetailValues, Modal, ProgressBar } from './ui.jsx';

const rpm = (value) => measure(value, 'RPM', 0);
const temperature = (value) => measure(value, '°C');
const clock = (value) => measure(value, 'MHz', 0);
const percentage = (value) => measure(value, '%', 0);
const titles = { cpu: 'CPU 详情', gpu: 'GPU 详情', memory: '内存详情', network: '网络详情' };

function CpuDetail({ cpu }) {
  return <>
    <DetailValues items={[
      ['型号', cpu?.name], ['核心数 / 线程数', `${cpu?.physical_cores ?? '—'} / ${cpu?.logical_cores ?? '—'}`],
      ['实时平均主频', clock(cpu?.average_clock_mhz)], ['实时最高主频', clock(cpu?.max_clock_mhz)],
      ['核心电压', measure(cpu?.voltage_v, 'V', 3)], ['封装温度', temperature(cpu?.package_temperature_c)],
      ['最高核心温度', temperature(cpu?.max_core_temperature_c)], ['封装功耗', measure(cpu?.power_w, 'W')],
      ['CPU 风扇', rpm(cpu?.fan_rpm)],
      ...(Number.isFinite(cpu?.pump_rpm) ? [['水泵', rpm(cpu.pump_rpm)]] : []),
    ]} />
    <section className="detail-section"><h3>各核心占用</h3>
      {cpu?.cores?.length ? <ul className="core-grid">
        {cpu.cores.map((core) => <li key={core.id}>
          <span>{core.name}</span><strong className="numeric">{percentage(core.usage_percent)}</strong>
          <ProgressBar value={core.usage_percent} tone="red" label={`${core.name} 占用率`} />
        </li>)}
      </ul> : <p className="section-empty muted">—</p>}
    </section>
  </>;
}

function GpuDetail({ gpu }) {
  const used = gpu?.vram_used_bytes;
  const total = gpu?.vram_total_bytes;
  const fill = Number.isFinite(used) && total > 0 ? used / total * 100 : null;
  return <>
    <DetailValues items={[
      ['型号', gpu?.name], ['核心占用率', percentage(gpu?.usage_percent)],
      ['核心频率', clock(gpu?.core_clock_mhz)], ['显存频率', clock(gpu?.memory_clock_mhz)],
      ['核心温度', temperature(gpu?.temperature_c)], ['热点温度', temperature(gpu?.hotspot_temperature_c)],
      ['功耗', measure(gpu?.power_w, 'W')],
    ]} />
    <section className="detail-section">
      <h3>显存</h3>
      <DetailValues items={[[ '已用 / 总量', `${bytes(used)} / ${bytes(total)}`], ['占用率', percentage(fill)]]} />
      <ProgressBar value={fill} tone="orange" label="显存占用率" />
    </section>
    <section className="detail-section"><h3>GPU 风扇</h3>
      {gpu?.fans?.length ? <ul className="detail-list">{gpu.fans.map((fan) => <li key={fan.id}>
        <span>{fan.name}</span><strong className="numeric">{rpm(fan.rpm)} · {percentage(fan.percent)}</strong>
      </li>)}</ul> : <p className="section-empty muted">—</p>}
    </section>
  </>;
}

function MemoryDetail({ memory }) {
  const virtual = memory?.virtual;
  return <>
    <DetailValues items={[
      ['已用', bytes(memory?.used_bytes)], ['可用', bytes(memory?.available_bytes)],
      ['总量', bytes(memory?.total_bytes)], ['占用率', percentage(memory?.percent)],
    ]} />
    <section className="detail-section"><h3>{virtual?.source === 'lhm' ? '虚拟内存' : '虚拟内存 · 分页文件'}</h3>
      <DetailValues items={[[ '已用', bytes(virtual?.used_bytes)], ['可用', bytes(virtual?.available_bytes)],
        ['总量', bytes(virtual?.total_bytes)], ['占用率', percentage(virtual?.percent)]]} />
    </section>
    <section className="detail-section"><h3>内存占用 Top 5 进程</h3>
      {memory?.top_processes?.length ? <ol className="detail-list">{memory.top_processes.map((process) =>
        <li key={process.pid}><span>{process.name}</span>
          <strong className="numeric">{measure(process.rss_bytes / (1024 ** 2), 'MiB')}</strong></li>)}</ol>
        : <p className="section-empty muted">—</p>}
    </section>
  </>;
}

function NetworkDetail({ network }) {
  return <>
    <DetailValues items={[
      ['实时上传', rateText(network?.upload_bytes_per_sec)], ['实时下载', rateText(network?.download_bytes_per_sec)],
      ['本次开机累计上传', bytes(network?.total_upload_bytes)], ['本次开机累计下载', bytes(network?.total_download_bytes)],
    ]} />
    <section className="detail-section"><h3>活跃网卡</h3>
      {network?.interfaces?.length ? <ul className="detail-list interfaces">{network.interfaces.map((adapter) => <li key={adapter.name}>
        <span><strong>{adapter.name}</strong><small>{adapter.tailscale ? 'Tailscale IP' : '本机 IP'}</small></span>
        <span className="numeric">{adapter.addresses.map((address) => <span key={address}>{address}</span>)}</span>
      </li>)}</ul> : <p className="section-empty muted">—</p>}
    </section>
  </>;
}

const views = { cpu: CpuDetail, gpu: GpuDetail, memory: MemoryDetail, network: NetworkDetail };

export default function HardwareDetails({ category, sample, gpu, onClose }) {
  const View = views[category];
  return <Modal id={`hardware-${category}`} title={titles[category]} className="detail-dialog" onClose={onClose}>
    <View cpu={sample?.cpu} memory={sample?.memory} network={sample?.network} gpu={gpu} />
  </Modal>;
}
