import { t } from './i18n/messages.js';
import { bytes, measure, rateText } from './format.js';
import { DetailValues, Modal, ProgressBar } from './ui.jsx';

const rpm = (value) => measure(value, 'RPM', 0);
const temperature = (value) => measure(value, '°C');
const clock = (value) => measure(value, 'MHz', 0);
const percentage = (value) => measure(value, '%', 0);
const titles = { get cpu() { return t('CPU 详情'); }, get gpu() { return t('GPU 详情'); }, get memory() { return t('内存详情'); }, get network() { return t('网络详情'); } };

function CpuDetail({ cpu }) {
  return <>
    <DetailValues items={[
      [t('型号'), cpu?.name], [t('核心数 / 线程数'), `${cpu?.physical_cores ?? '—'} / ${cpu?.logical_cores ?? '—'}`],
      [t('实时平均主频'), clock(cpu?.average_clock_mhz)], [t('实时最高主频'), clock(cpu?.max_clock_mhz)],
      [t('核心电压'), measure(cpu?.voltage_v, 'V', 3)], [t('封装温度'), temperature(cpu?.package_temperature_c)],
      [t('最高核心温度'), temperature(cpu?.max_core_temperature_c)], [t('封装功耗'), measure(cpu?.power_w, 'W')],
      [t('CPU 风扇'), rpm(cpu?.fan_rpm)],
      ...(Number.isFinite(cpu?.pump_rpm) ? [[t('水泵'), rpm(cpu.pump_rpm)]] : []),
    ]} />
    <section className="detail-section"><h3>{t('各核心占用')}</h3>
      {cpu?.cores?.length ? <ul className="core-grid">
        {cpu.cores.map((core) => <li key={core.id}>
          <span>{core.name}</span><strong className="numeric">{percentage(core.usage_percent)}</strong>
          <ProgressBar value={core.usage_percent} tone="red" label={t('{p1} 占用率', { p1: core.name })} />
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
      [t('型号'), gpu?.name], [t('核心占用率'), percentage(gpu?.usage_percent)],
      [t('核心频率'), clock(gpu?.core_clock_mhz)], [t('显存频率'), clock(gpu?.memory_clock_mhz)],
      [t('核心温度'), temperature(gpu?.temperature_c)], [t('热点温度'), temperature(gpu?.hotspot_temperature_c)],
      [t('功耗'), measure(gpu?.power_w, 'W')],
    ]} />
    <section className="detail-section">
      <h3>{t('显存')}</h3>
      <DetailValues items={[[ t('已用 / 总量'), `${bytes(used)} / ${bytes(total)}`], [t('占用率'), percentage(fill)]]} />
      <ProgressBar value={fill} tone="orange" label={t('显存占用率')} />
    </section>
    <section className="detail-section"><h3>{t('GPU 风扇')}</h3>
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
      [t('已用'), bytes(memory?.used_bytes)], [t('可用'), bytes(memory?.available_bytes)],
      [t('总量'), bytes(memory?.total_bytes)], [t('占用率'), percentage(memory?.percent)],
    ]} />
    <section className="detail-section"><h3>{virtual?.source === 'lhm' ? t('虚拟内存') : t('虚拟内存 · 分页文件')}</h3>
      <DetailValues items={[[ t('已用'), bytes(virtual?.used_bytes)], [t('可用'), bytes(virtual?.available_bytes)],
        [t('总量'), bytes(virtual?.total_bytes)], [t('占用率'), percentage(virtual?.percent)]]} />
    </section>
    <section className="detail-section"><h3>{t('内存占用 Top 5 进程')}</h3>
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
      [t('实时上传'), rateText(network?.upload_bytes_per_sec)], [t('实时下载'), rateText(network?.download_bytes_per_sec)],
      [t('本次开机累计上传'), bytes(network?.total_upload_bytes)], [t('本次开机累计下载'), bytes(network?.total_download_bytes)],
    ]} />
    <section className="detail-section"><h3>{t('活跃网卡')}</h3>
      {network?.interfaces?.length ? <ul className="detail-list interfaces">{network.interfaces.map((adapter) => <li key={adapter.name}>
        <span><strong>{adapter.name}</strong><small>{adapter.tailscale ? 'Tailscale IP' : t('本机 IP')}</small></span>
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
