import { useEffect, useMemo, useRef, useState } from 'react';
import { Activity, Clipboard, Coffee, LockKeyhole, MonitorOff, Moon } from 'lucide-react';
import { postJson } from './api.js';
import { bytes, measure, rateText, sleepTime, time } from './format.js';
import { Button, DetailLink, DetailValues, Modal, ProgressBar, Segmented } from './ui.jsx';

const deviceKey = (device) => `agent:${device.device_id}`;

export function DevicePicker({ id, devices, device, onChange }) {
  if (!devices.length) return null;
  const options = [{ value: 'windows', label: 'Windows' }, ...devices.map((item) => ({
    value: deviceKey(item), label: item.name,
  }))];
  return <div className="device-picker"><Segmented id={id} label="设备" options={options}
    value={device ? deviceKey(device) : 'windows'}
    onChange={(value) => onChange(value === 'windows' ? null : value.slice('agent:'.length))} /></div>;
}

const batteryStates = {
  charging: '充电中', charged: '已充满', discharging: '使用电池', not_charging: '未充电', unknown: '状态未知',
};

function batteryNote(battery) {
  return battery && [
    batteryStates[battery.state] ?? (battery.charging ? '充电中' : '使用电池'),
    battery.minutes_left != null && `${battery.charging ? '距充满' : '剩余'} ${sleepTime(battery.minutes_left)}`,
  ].filter(Boolean).join(' · ');
}

function RemoteStatus({ device }) {
  return <span className="remote-status"><span className={`state-dot ${device.online ? 'live-dot' : ''}`} aria-hidden="true" />
    {device.online ? '在线' : '已休眠或离线'}</span>;
}

function RemoteLastSeen({ device }) {
  return !device.online && <p className="muted subtle-line">上次在线 <time dateTime={device.last_seen}>{time(device.last_seen, true)}</time></p>;
}

function RemoteHardwareSummary({ device, onOpen }) {
  const { battery, cpu, memory, disks, network } = device.snapshot;
  const mainDisk = disks.find((disk) => disk.mountpoint === '/');
  const readings = [
    ['电池', measure(battery?.percent, '%', 0), batteryNote(battery)],
    ['CPU', measure(cpu?.usage_percent, '%', 0)],
    ['内存', measure(memory?.percent, '%', 0)],
    ['网络', `↓ ${rateText(network?.download_bytes_per_sec)}`, `↑ ${rateText(network?.upload_bytes_per_sec)}`],
    ['主盘剩余', bytes(mainDisk?.free_bytes)],
  ];
  return <section className="surface panel detail-tile control-hardware remote-hardware" data-offline={!device.online}>
    <div className="panel-header">
      <span className="metric-icon tone-blue" aria-hidden="true"><Activity size={21} /></span>
      <h2>硬件监控</h2><RemoteStatus device={device} />
    </div>
    <dl>{readings.map(([label, value, note]) => <div key={label}>
      <dt>{label}</dt><dd className="numeric">{value}{note && <small>{note}</small>}</dd>
    </div>)}</dl>
    <RemoteLastSeen device={device} />
    <DetailLink title="硬件监控" onClick={onOpen} />
  </section>;
}

export function RemoteHardware({ device }) {
  const { battery, cpu, memory, disks, network, top_processes: processes } = device.snapshot;
  return <div className="remote-hardware" data-offline={!device.online}>
    <div className="remote-detail-header"><h3>{device.name}</h3><RemoteStatus device={device} /></div>
    <RemoteLastSeen device={device} />
    <div className="remote-detail-grid">
      <section><h4>电池</h4><DetailValues items={[
        ['电量', measure(battery?.percent, '%')], ['供电状态', batteryNote(battery)],
        ['电池健康度', measure(battery?.health_percent, '%')], ['循环次数', battery?.cycle_count],
        ['温度', measure(battery?.temperature_c, '°C')],
      ]} /></section>
      <section><h4>CPU 与内存</h4><DetailValues items={[
        ['CPU 使用率', measure(cpu?.usage_percent, '%')], ['处理器', cpu?.name],
        ['核心 / 线程', cpu?.physical_cores && cpu?.logical_cores ? `${cpu.physical_cores} / ${cpu.logical_cores}` : null],
        ['内存使用率', measure(memory?.percent, '%')],
        ['已用 / 总量', memory && `${bytes(memory.used_bytes)} / ${bytes(memory.total_bytes)}`],
        ['可用内存', bytes(memory?.available_bytes)],
      ]} /></section>
      <section><h4>磁盘空间</h4>
        {disks.length ? <ul className="remote-disks">
          {disks.map((disk, index) => <li key={`${disk.mountpoint}-${index}`}>
            <div className="hardware-label"><strong>{disk.name ?? disk.mountpoint}</strong>
              <span className="muted numeric">剩余 {bytes(disk.free_bytes)} / {bytes(disk.total_bytes)}</span></div>
            <ProgressBar value={disk.total_bytes ? (1 - disk.free_bytes / disk.total_bytes) * 100 : null} label={`${disk.mountpoint} 使用率`} />
          </li>)}
        </ul> : <p className="muted">暂无磁盘数据</p>}
      </section>
      <section><h4>内存占用前 5 进程</h4>
        {processes.length ? <ol className="remote-processes">
          {processes.map((process, index) => <li key={process.pid ?? index}>
            <span>{process.name}</span><strong className="numeric">{bytes(process.rss_bytes)}</strong>
          </li>)}
        </ol> : <p className="muted">暂无进程数据</p>}
      </section>
      <section className="remote-network"><h4>网络与 IP</h4>
        <DetailValues items={[
          ['下载', rateText(network?.download_bytes_per_sec)], ['上传', rateText(network?.upload_bytes_per_sec)],
          ...(network?.interfaces ?? []).map(({ name, addresses, tailscale }) => [
            tailscale ? name + ' · Tailscale' : name, addresses.join(' · '),
          ]),
        ]} />
      </section>
    </div>
  </div>;
}

const actions = {
  lock: '锁屏', display_sleep: '熄屏', display_lock: '息屏锁屏', sleep: '休眠', volume_set: '调整音量',
  volume_mute: '切换静音', audio_device_set: '切换输出设备', caffeinate_toggle: '保持唤醒', clipboard_push: '推送剪贴板',
  media_previous: '上一首', media_play_pause: '播放或暂停', media_next: '下一首',
};
const results = { queued: '等待设备接收', delivered: '等待设备回报', succeeded: '已执行', failed: '执行失败', expired: '等待超时' };
const transportActions = { toggle: 'media_play_pause', next: 'media_next', previous: 'media_previous' };

export function RemoteControl({ device, onMonitor, onCommand, children }) {
  const [confirm, setConfirm] = useState(null);
  const [sending, setSending] = useState(false);
  const [lastCommand, setLastCommand] = useState(null);
  const [volumeCommand, setVolumeCommand] = useState(null);
  const [error, setError] = useState('');
  const [draft, setDraft] = useState(null);
  const volumeTimer = useRef(null);
  const pendingPercent = useRef(null);
  const volumeWriting = useRef(false);

  useEffect(() => () => {
    window.clearTimeout(volumeTimer.current);
    pendingPercent.current = null;
  }, []);
  useEffect(() => {
    if (!device.online) {
      window.clearTimeout(volumeTimer.current);
      pendingPercent.current = null;
      setDraft(null);
    }
  }, [device.online]);

  const { media, volume, caffeinate, timestamp } = device.snapshot;
  const received = useMemo(() => {
    const elapsed = Math.max(0, Date.now() - Date.parse(device.last_seen));
    const sampleDelay = Math.max(0, Date.parse(timestamp) - Date.parse(media?.updated_at));
    return performance.now() - Math.min(15000, (elapsed || 0) + (sampleDelay || 0));
  }, [device.last_seen, timestamp, media?.updated_at]);
  const command = device.commands.find(({ id }) => id === lastCommand?.id) ?? lastCommand;
  const volumeStatus = device.commands.find(({ id }) => id === volumeCommand?.id)?.status;
  const pending = ({ status }) => status === 'queued' || status === 'delivered';
  function awaiting(action) {
    return (command?.action === action && pending(command))
      || device.commands.some((item) => item.action === action && pending(item));
  }

  const disabled = !device.online || sending;
  const upgraded = typeof caffeinate === 'boolean';
  const track = media?.title || media?.artist ? {
    ...media, agent_id: device.device_id, session_id: device.device_id,
    status: device.online ? media.status : 'offline',
    actions: { toggle: device.online && !awaiting('media_play_pause'), previous: device.online, next: device.online },
  } : null;
  const playback = { track, sessions: [], received, reload: onCommand, reason: device.online ? null : 'agent_offline' };
  const switching = disabled || awaiting('audio_device_set');
  const canAdjust = device.online && Number.isFinite(volume?.percent);
  const canMute = device.online && typeof volume?.muted === 'boolean' && !awaiting('volume_mute');

  async function send(action, payload = {}) {
    if (!device.online) return;
    setSending(true);
    setError('');
    try {
      const queued = await postJson(`/api/agents/${encodeURIComponent(device.device_id)}/command`, { action, payload });
      setLastCommand({ ...queued, action });
      if (action === 'volume_set') setVolumeCommand({ id: queued.id, percent: payload.percent });
      onCommand();
    } catch (failure) {
      setError(failure.message);
      if (action === 'volume_set' && pendingPercent.current == null) setDraft(null);
    } finally {
      setSending(false);
    }
  }

  async function flushVolume() {
    window.clearTimeout(volumeTimer.current);
    const percent = pendingPercent.current;
    if (volumeWriting.current || percent == null) return;
    pendingPercent.current = null;
    volumeWriting.current = true;
    try {
      await send('volume_set', { percent });
    } finally {
      volumeWriting.current = false;
      if (pendingPercent.current != null) flushVolume();
    }
  }

  function queueVolume(change) {
    if (Object.hasOwn(change, 'device_id')) return send('audio_device_set', change);
    if (Object.hasOwn(change, 'muted')) return send('volume_mute', change);
    setDraft(change.percent);
    pendingPercent.current = change.percent;
    window.clearTimeout(volumeTimer.current);
    volumeTimer.current = window.setTimeout(flushVolume, 350);
  }

  useEffect(() => {
    if (['succeeded', 'failed', 'expired'].includes(volumeStatus) && pendingPercent.current == null) {
      setDraft((value) => value === volumeCommand.percent ? null : value);
    }
  }, [volumeStatus, volumeCommand]);

  const commandText = command && [actions[command.action], results[command.status],
    command.status === 'failed' && command.message].filter(Boolean).join(' · ');
  const commandFailed = command?.status === 'failed' || command?.status === 'expired';

  return <>
    <RemoteHardwareSummary device={device} onOpen={onMonitor} />
    <section className="surface panel quick-card remote-quick" aria-labelledby="remote-quick-heading">
      <div className="panel-header"><h2 id="remote-quick-heading">主机快捷控制</h2></div>
      <div className="quick-control-row remote-quick-row" role="group" aria-label="MacBook 操作">
        {upgraded ? <Button disabled={disabled} onClick={() => send('display_lock')}><LockKeyhole size={15} /><span>息屏锁屏</span></Button> : <>
          <Button disabled={disabled} onClick={() => setConfirm('lock')} aria-haspopup="dialog"><LockKeyhole size={15} /><span>锁屏</span></Button>
          <Button disabled={disabled} onClick={() => send('display_sleep')}><MonitorOff size={15} /><span>熄屏</span></Button>
        </>}
        <Button disabled={disabled} onClick={() => setConfirm('sleep')} aria-haspopup="dialog"><Moon size={15} /><span>休眠</span></Button>
        {upgraded && <>
          <Button disabled={disabled || awaiting('caffeinate_toggle')} aria-pressed={caffeinate} onClick={() => send('caffeinate_toggle')}>
            <Coffee size={15} /><span>保持唤醒</span>
          </Button>
          <Button disabled={disabled || awaiting('clipboard_push')} title="发送 Mac 剪贴板内容到中转站"
            onClick={() => send('clipboard_push')}><Clipboard size={15} /><span>推剪贴板</span></Button>
        </>}
      </div>
    </section>
    {children({
      media: playback, onAction: (action) => send(transportActions[action]),
      commandStatus: commandText, commandError: error || (commandFailed ? commandText : ''),
      sound: {
        available: device.online && Boolean(volume),
        canAdjust, canMute, switching, percent: draft ?? (Number.isFinite(volume?.percent) ? Math.round(volume.percent) : null),
        muted: volume?.muted, devices: volume?.devices ?? [], output: volume?.device_id ?? '',
        unavailableLabel: device.online ? '当前输出设备未提供音量' : 'MacBook 已休眠或离线',
        queue: queueVolume, flush: flushVolume,
      },
    })}
    {confirm && <Modal id="remote-confirm" className="detail-dialog shutdown-dialog"
      title={`确认${actions[confirm]}`} onClose={() => setConfirm(null)}>
      <p className="muted">确定让 {device.name} {actions[confirm]}吗？</p>
      <div className="quick-confirm-actions">
        <Button onClick={() => setConfirm(null)}>取消</Button>
        <Button disabled={disabled} onClick={() => { send(confirm); setConfirm(null); }}>确认</Button>
      </div>
    </Modal>}
  </>;
}
