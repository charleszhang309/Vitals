import { useEffect, useMemo, useRef, useState } from 'react';
import { Activity, Clipboard, Coffee, LockKeyhole, MonitorOff, Moon } from 'lucide-react';
import { postJson } from './api.js';
import { bytes, measure, rateText, sleepTime, time } from './format.js';
import { Button, DetailLink, DetailValues, Modal, ProgressBar, Segmented } from './ui.jsx';

export const HUB_DEVICE_ID = 'hub';
const compactPicker = '(max-width: 640px)';

export function DeviceDot({ online }) {
  return <span className="device-dot" data-online={Boolean(online)} aria-hidden="true" />;
}

export function DevicePicker({ id, hub, devices, device, onChange }) {
  const [compact, setCompact] = useState(() => window.matchMedia(compactPicker).matches);
  useEffect(() => {
    const media = window.matchMedia(compactPicker);
    const sync = () => setCompact(media.matches);
    media.addEventListener('change', sync);
    return () => media.removeEventListener('change', sync);
  }, []);
  const choices = [hub, ...devices];
  if (!devices.length) return null;
  const selected = device ?? hub;
  if (choices.length >= (compact ? 3 : 4)) {
    return <div className="device-picker device-picker-select" data-offline={!selected.online}>
      <DeviceDot online={selected.online} />
      <select aria-label="设备" value={selected.device_id} onChange={(event) => onChange(event.target.value)}>
        {choices.map(({ device_id: key, name, online }) => <option key={key} value={key} data-online={Boolean(online)}>
          {online ? '●' : '○'} {name}{!online ? ' · 离线' : ''}
        </option>)}
      </select>
    </div>;
  }
  const options = choices.map(({ device_id: key, name, online }) => ({
    value: key, label: <span className="device-segment-label" data-offline={!online} title={name}>
      <DeviceDot online={online} /><span>{name}</span>
      {!online && <span className="sr-only">（离线）</span>}
    </span>,
  }));
  return <div className="device-picker"><Segmented id={id} label="设备" options={options}
    value={selected.device_id} onChange={onChange} /></div>;
}

const batteryStates = {
  charging: '充电中', charged: '已充满', discharging: '使用电池', not_charging: '未充电', unknown: '状态未知',
};

export function batteryNote(battery) {
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
    ...(battery ? [['电池', measure(battery.percent, '%', 0), batteryNote(battery)]] : []),
    ['CPU', measure(cpu?.usage_percent, '%', 0)],
    ['内存', measure(memory?.percent, '%', 0)],
    ['网络', `↓ ${rateText(network?.download_bytes_per_sec)}`, `↑ ${rateText(network?.upload_bytes_per_sec)}`],
    ['主盘剩余', bytes(mainDisk?.free_bytes)],
  ];
  return <section className="surface panel detail-tile control-hardware remote-hardware" data-offline={!device.online}
    style={{ '--metrics-count': readings.length }}>
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
      {battery && <section><h4>电池</h4><DetailValues items={[
        ['电量', measure(battery.percent, '%')], ['供电状态', batteryNote(battery)],
        ['电池健康度', measure(battery.health_percent, '%')], ['循环次数', battery.cycle_count],
        ['温度', measure(battery.temperature_c, '°C')],
      ]} /></section>}
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
            <div className="hardware-label"><strong>{disk.name || (disk.mountpoint === '/' ? 'Macintosh HD' : disk.mountpoint)}</strong>
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
  lock: '锁屏', display_sleep: '息屏', display_lock: '息屏', sleep: '休眠', volume_set: '调整音量',
  volume_mute: '切换静音', audio_device_set: '切换输出设备', caffeinate_toggle: '保持唤醒', clipboard_push: '推送剪贴板',
  input_volume_set: '调整麦克风音量', input_volume_mute: '切换麦克风静音', input_audio_device_set: '切换输入设备',
  media_previous: '上一首', media_play_pause: '播放或暂停', media_next: '下一首',
};
const results = { queued: '等待设备接收', delivered: '等待设备回报', succeeded: '已执行', failed: '执行失败', expired: '等待超时' };
const transportActions = { toggle: 'media_play_pause', next: 'media_next', previous: 'media_previous' };
const pending = (command) => command?.status === 'queued' || command?.status === 'delivered';

export function RemoteControl({ device, onMonitor, onCommand, onActivity, children }) {
  const [confirm, setConfirm] = useState(null);
  const [sending, setSending] = useState(0);
  const [lastCommand, setLastCommand] = useState(null);
  const [error, setError] = useState('');
  const [optimistic, setOptimistic] = useState({});
  const volumeTimer = useRef(null);
  const pendingVolume = useRef(new Map());
  const volumeWriting = useRef(false);

  useEffect(() => () => {
    window.clearTimeout(volumeTimer.current);
    pendingVolume.current.clear();
  }, []);
  useEffect(() => {
    if (!device.online) {
      window.clearTimeout(volumeTimer.current);
      pendingVolume.current.clear();
    }
  }, [device.online]);

  const { media, volume, caffeinate, timestamp } = device.snapshot;
  const sampledAt = useMemo(() => {
    const elapsed = Math.max(0, Date.now() - Date.parse(device.last_seen));
    const sampleDelay = Math.max(0, Date.parse(timestamp) - Date.parse(media?.updated_at));
    return performance.now() - Math.min(15000, (elapsed || 0) + (sampleDelay || 0));
  }, [device.last_seen, timestamp, media?.updated_at]);
  const command = device.commands.find(({ id }) => id === lastCommand?.id) ?? lastCommand;
  function awaiting(action) {
    return Boolean(optimistic[action]) || (command?.action === action && pending(command))
      || device.commands.some((item) => item.action === action && pending(item));
  }

  const active = device.online && (sending > 0 || pending(command) || Object.keys(optimistic).length > 0);
  useEffect(() => {
    onActivity(active);
    return () => onActivity(false);
  }, [active, onActivity]);

  useEffect(() => {
    setOptimistic((previous) => {
      const entries = Object.entries(previous);
      const next = entries.filter(([, entry]) => {
        if (!device.online || entry.expires <= Date.now()) return false;
        const receipt = device.commands.find(({ id }) => id === entry.id);
        if (receipt?.status === 'failed' || receipt?.status === 'expired') return false;
        // Native acknowledgements can arrive before the next telemetry sample.
        const freshSnapshot = Date.parse(device.last_seen) > Date.parse(receipt?.completed_at ?? entry.lastSeen);
        return !(receipt?.status === 'succeeded' && freshSnapshot);
      });
      return next.length === entries.length ? previous : Object.fromEntries(next);
    });
  }, [device.commands, device.last_seen, device.online]);

  const disabled = !device.online || sending > 0;
  const upgraded = typeof caffeinate === 'boolean';
  const mediaChange = optimistic.media_play_pause?.value;
  const playingOverride = device.online && media && mediaChange?.trackId === media.track_id ? mediaChange : null;
  const track = media?.title || media?.artist ? {
    ...media, agent_id: device.device_id, session_id: device.device_id,
    status: device.online ? playingOverride?.status ?? media.status : 'offline',
    position_seconds: playingOverride?.position ?? media.position_seconds,
    actions: { toggle: device.online && !awaiting('media_play_pause'), previous: device.online, next: device.online },
  } : null;
  const received = playingOverride?.received ?? sampledAt;
  const offlineLabel = device.online ? undefined : `${device.name} 已休眠或离线`;
  const playback = { track, sessions: [], received, reload: onCommand, offlineLabel };

  function predict(action, value) {
    const token = Symbol(action);
    setOptimistic((previous) => ({ ...previous, [action]: {
      token, value, lastSeen: device.last_seen, expires: Date.now() + 65_000,
    } }));
    return token;
  }

  function settle(action, token, id) {
    if (!token) return;
    setOptimistic((previous) => {
      if (previous[action]?.token !== token) return previous;
      if (id) return { ...previous, [action]: { ...previous[action], id } };
      const next = { ...previous };
      delete next[action];
      return next;
    });
  }

  async function send(action, payload = {}, token) {
    if (!device.online) return;
    setSending((count) => count + 1);
    setError('');
    try {
      const queued = await postJson(`/api/agents/${encodeURIComponent(device.device_id)}/command`, { action, payload });
      setLastCommand({ ...queued, action });
      settle(action, token, queued.id);
      onCommand();
    } catch (failure) {
      setError(failure.message);
      settle(action, token);
    } finally {
      setSending((count) => count - 1);
    }
  }

  async function flushVolume() {
    window.clearTimeout(volumeTimer.current);
    if (volumeWriting.current || !pendingVolume.current.size) return;
    const [action, change] = pendingVolume.current.entries().next().value;
    pendingVolume.current.delete(action);
    volumeWriting.current = true;
    try {
      await send(action, change.payload, change.token);
    } finally {
      volumeWriting.current = false;
      if (pendingVolume.current.size) flushVolume();
    }
  }

  function queueVolume(change, prefix, immediate) {
    const field = Object.keys(change)[0];
    const action = prefix + { device_id: 'audio_device_set', muted: 'volume_mute', percent: 'volume_set' }[field];
    if (field === 'device_id') {
      for (const verb of ['volume_set', 'volume_mute']) {
        const key = prefix + verb;
        const queued = pendingVolume.current.get(key);
        if (queued) settle(key, queued.token);
        pendingVolume.current.delete(key);
      }
    }
    pendingVolume.current.set(action, { payload: change, token: predict(action, change[field]) });
    window.clearTimeout(volumeTimer.current);
    if (immediate) flushVolume();
    else volumeTimer.current = window.setTimeout(flushVolume, 80);
  }

  function audioChannel(prefix) {
    const value = (key) => volume?.[prefix + key];
    const writing = (verb) => awaiting(prefix + verb);
    const predicted = (verb) => optimistic[prefix + verb]?.value;
    return {
      available: device.online && Boolean(volume),
      canAdjust: device.online && Number.isFinite(value('percent')),
      canMute: device.online && typeof value('muted') === 'boolean' && !writing('volume_mute'),
      switching: !device.online || writing('audio_device_set'),
      percent: predicted('volume_set') ?? (Number.isFinite(value('percent')) ? Math.round(value('percent')) : null),
      muted: predicted('volume_mute') ?? value('muted'),
      devices: value('devices') ?? [], deviceId: predicted('audio_device_set') ?? value('device_id') ?? '',
      queue: (change, immediate) => queueVolume(change, prefix, immediate),
      flush: flushVolume,
    };
  }
  const hasInput = Boolean(volume?.input_devices?.length || volume?.input_device_id
    || Number.isFinite(volume?.input_percent) || typeof volume?.input_muted === 'boolean');

  function controlPlayback(operation) {
    const action = transportActions[operation];
    if (operation !== 'toggle' || !track) return send(action);
    const now = performance.now();
    const playing = track.status === 'playing';
    const elapsed = playing ? Math.max(0, (now - received) / 1000) : 0;
    const position = Number.isFinite(track.position_seconds)
      ? Math.min(track.duration_seconds ?? Infinity, track.position_seconds + elapsed) : null;
    return send(action, {}, predict(action, {
      trackId: track.track_id, status: playing ? 'paused' : 'playing', position, received: now,
    }));
  }

  const commandText = command && [actions[command.action], results[command.status],
    command.status === 'failed' && command.message].filter(Boolean).join(' · ');
  const commandFailed = command?.status === 'failed' || command?.status === 'expired';

  return <>
    <RemoteHardwareSummary device={device} onOpen={onMonitor} />
    <section className="surface panel quick-card remote-quick" aria-labelledby="remote-quick-heading">
      <div className="panel-header"><h2 id="remote-quick-heading">主机快捷控制</h2></div>
      <div className="quick-control-row remote-quick-row" role="group" aria-label={`${device.name} 操作`}>
        {upgraded ? <Button disabled={disabled} onClick={() => send('display_lock')}><LockKeyhole size={15} /><span>息屏</span></Button> : <>
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
      media: playback, onAction: controlPlayback,
      commandStatus: '', commandError: error || (commandFailed ? commandText : ''),
      sound: {
        ...audioChannel(''), input: hasInput ? audioChannel('input_') : null,
        unavailableLabel: offlineLabel ?? '当前输出设备未提供音量',
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
