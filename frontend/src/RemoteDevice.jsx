import { useEffect, useMemo, useRef, useState } from 'react';
import { Clipboard, Coffee, LockKeyhole, MonitorOff, Moon, Pause, Play, SkipBack, SkipForward, Volume2, VolumeX } from 'lucide-react';
import { postJson } from './api.js';
import { bytes, measure, rateText, sleepTime, time } from './format.js';
import { Button, DetailLink, DetailValues, IconButton, Modal, ProgressBar, Segmented } from './ui.jsx';

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

export function RemoteHardware({ device, onOpen }) {
  const { snapshot, online, last_seen: lastSeen, name } = device;
  const { battery, cpu, memory, disks, network, top_processes: processes } = snapshot;
  const batteryStatus = battery && [
    batteryStates[battery.state] ?? (battery.charging ? '充电中' : '使用电池'),
    battery.minutes_left != null && `${battery.charging ? '预计充满' : '剩余'} ${sleepTime(battery.minutes_left)}`,
  ].filter(Boolean).join(' · ');
  const stats = [
    ['电池', measure(battery?.percent, '%'), batteryStatus],
    ['CPU', measure(cpu?.usage_percent, '%')],
    ['内存', measure(memory?.percent, '%'), memory && `${bytes(memory.used_bytes)} / ${bytes(memory.total_bytes)}`],
    ['网络', rateText(network?.download_bytes_per_sec), network && `上传 ${rateText(network.upload_bytes_per_sec)}`],
  ];
  return <section className={`surface panel remote-hardware${onOpen ? ' detail-tile' : ''}`} data-offline={!online}>
    <div className="panel-header">
      <h2>{name}</h2>
      <span className="remote-status"><span className={`state-dot ${online ? 'live-dot' : ''}`} aria-hidden="true" />
        {online ? '在线' : '已休眠或离线'}</span>
    </div>
    {!online && <p className="muted subtle-line">上次在线 <time dateTime={lastSeen}>{time(lastSeen, true)}</time></p>}
    <div className="remote-readings">
      {stats.map(([label, value, note]) => <div key={label}>
        <span className="muted">{label}</span><strong className="numeric">{value}</strong>
        {note && <span className="muted numeric">{note}</span>}
      </div>)}
    </div>
    {onOpen && <DetailLink title="远程硬件" onClick={onOpen} />}
    <div className="remote-detail-grid">
      <section><h3>磁盘空间</h3>
        {disks.length ? <ul className="remote-disks">
          {disks.map((disk, index) => <li key={`${disk.mountpoint}-${index}`}>
            <div className="hardware-label"><strong>{disk.name ?? disk.mountpoint}</strong>
              <span className="muted numeric">剩余 {bytes(disk.free_bytes)} / {bytes(disk.total_bytes)}</span></div>
            <ProgressBar value={disk.total_bytes ? (1 - disk.free_bytes / disk.total_bytes) * 100 : null} label={`${disk.mountpoint} 使用率`} />
          </li>)}
        </ul> : <p className="muted">暂无磁盘数据</p>}
      </section>
      {battery && [battery.cycle_count, battery.health_percent, battery.temperature_c].some(value => value != null) &&
        <section><h3>电池健康</h3><DetailValues items={[
          ['循环次数', battery.cycle_count ?? '—'],
          ['最大容量', measure(battery.health_percent, '%')],
          ['电池温度', measure(battery.temperature_c, '°C')],
        ]} /></section>}
      {network?.interfaces?.length > 0 && <section><h3>活跃网卡</h3>
        <DetailValues items={network.interfaces.map(item => [
          item.tailscale ? item.name + ' · Tailscale' : item.name, item.addresses.join(', '),
        ])} />
      </section>}
      <section><h3>内存占用前 5 进程</h3>
        {processes.length ? <ol className="remote-processes">
          {processes.map((process, index) => <li key={process.pid ?? index}>
            <span>{process.name}</span><strong className="numeric">{bytes(process.rss_bytes)}</strong>
          </li>)}
        </ol> : <p className="muted">暂无进程数据</p>}
      </section>
    </div>
  </section>;
}

const actions = {
  lock: '锁屏', display_sleep: '熄屏', display_lock: '息屏锁屏', sleep: '休眠', volume_set: '调整音量',
  volume_mute: '切换静音', audio_device_set: '切换输出设备', caffeinate_toggle: '保持唤醒', clipboard_push: '推送剪贴板',
  media_previous: '上一首', media_play_pause: '播放或暂停', media_next: '下一首',
};
const results = { queued: '等待设备接收', delivered: '等待设备回报', succeeded: '已执行', failed: '执行失败', expired: '等待超时' };
const transportActions = { toggle: 'media_play_pause', next: 'media_next', previous: 'media_previous' };

export function RemoteControl({ device, onMonitor, onCommand, PlayerCard, NowPlaying }) {
  const [playerOpen, setPlayerOpen] = useState(false);
  const [lyricsPreferred, setLyricsPreferred] = useState(true);
  const [confirm, setConfirm] = useState(null);
  const [sending, setSending] = useState(false);
  const [lastCommand, setLastCommand] = useState(null);
  const [volumeCommand, setVolumeCommand] = useState(null);
  const [error, setError] = useState('');
  const [draft, setDraft] = useState(null);
  const volumeTimer = useRef(null);
  useEffect(() => () => window.clearTimeout(volumeTimer.current), []);
  useEffect(() => {
    if (!device.online) window.clearTimeout(volumeTimer.current);
  }, [device.online]);

  const { media, volume, caffeinate } = device.snapshot;
  const sampleTime = media?.updated_at ?? device.last_seen;
  const received = useMemo(() => {
    const age = Math.max(0, Date.now() - Date.parse(sampleTime));
    return performance.now() - (Number.isFinite(age) ? age : 0);
  }, [sampleTime]);
  const percent = draft ?? Math.round(volume?.percent ?? 0);
  const playing = media?.status === 'playing';
  const command = device.commands.find(({ id }) => id === lastCommand?.id) ?? lastCommand;
  const disabled = !device.online || sending;
  const upgraded = typeof caffeinate === 'boolean';
  const track = media ? { ...media, agent_id: device.device_id, session_id: device.device_id,
    status: !device.online && playing ? 'paused' : media.status,
    actions: { toggle: device.online, previous: device.online, next: device.online } } : null;
  const playback = { track, sessions: [], received, reload: onCommand };
  const volumeAvailable = Number.isFinite(volume?.percent) && typeof volume.muted === 'boolean';

  async function send(action, payload = {}) {
    setSending(true);
    setError('');
    try {
      const queued = await postJson(`/api/agents/${encodeURIComponent(device.device_id)}/command`, { action, payload });
      setLastCommand({ ...queued, action });
      if (action === 'volume_set') setVolumeCommand({ id: queued.id, percent: payload.percent });
      onCommand();
    } catch (failure) {
      setError(failure.message);
      if (action === 'volume_set') setDraft(null);
    } finally {
      setSending(false);
    }
  }

  function changeVolume(value) {
    setDraft(value);
    window.clearTimeout(volumeTimer.current);
    volumeTimer.current = window.setTimeout(() => send('volume_set', { percent: value }), 350);
  }

  const volumeStatus = device.commands.find(({ id }) => id === volumeCommand?.id)?.status;
  useEffect(() => {
    if (!device.online) setDraft(null);
    else if (['succeeded', 'failed', 'expired'].includes(volumeStatus)) {
      setDraft((value) => value === volumeCommand.percent ? null : value);
    }
  }, [device.online, volumeStatus, volumeCommand]);

  return <>
    <RemoteHardware device={device} onOpen={onMonitor} />
    <section className="surface panel remote-quick">
      <div className="panel-header"><h2>快捷控制</h2></div>
      <div className="remote-actions">
        {upgraded ? <Button disabled={disabled} onClick={() => send('display_lock')}><LockKeyhole size={17} />息屏锁屏</Button> : <>
          <Button disabled={disabled} onClick={() => setConfirm('lock')}><LockKeyhole size={17} />锁屏</Button>
          <Button disabled={disabled} onClick={() => send('display_sleep')}><MonitorOff size={17} />熄屏</Button>
        </>}
        <Button disabled={disabled} onClick={() => setConfirm('sleep')}><Moon size={17} />休眠</Button>
        {upgraded && <>
          <Button disabled={disabled} aria-pressed={caffeinate} onClick={() => send('caffeinate_toggle')}>
            <Coffee size={17} />{caffeinate ? '关闭保持唤醒' : '保持唤醒'}
          </Button>
          <Button disabled={disabled} onClick={() => send('clipboard_push')}><Clipboard size={17} />推送剪贴板</Button>
        </>}
      </div>
    </section>
    <div className="media-control-grid">
      <PlayerCard media={playback} onOpen={track ? () => setPlayerOpen(true) : undefined}>
        <div className="remote-actions remote-transport">
          <IconButton label="上一首" disabled={disabled || !media} onClick={() => send('media_previous')}><SkipBack size={20} /></IconButton>
          <IconButton label={playing ? '暂停' : '播放'} disabled={disabled || !media} onClick={() => send('media_play_pause')}>
            {playing ? <Pause size={20} /> : <Play size={20} />}
          </IconButton>
          <IconButton label="下一首" disabled={disabled || !media} onClick={() => send('media_next')}><SkipForward size={20} /></IconButton>
        </div>
      </PlayerCard>
      <section className="surface panel">
        <div className="panel-header"><h2>系统音量</h2><span className="numeric">{volumeAvailable ? `${percent}%` : '—'}</span></div>
        <div className="volume-controls">
          <IconButton label={volume?.muted ? '取消静音' : '静音'} disabled={disabled || !volumeAvailable}
            onClick={() => send('volume_mute', { muted: !volume.muted })}>
            {volume?.muted ? <VolumeX size={21} /> : <Volume2 size={21} />}
          </IconButton>
          <input className="media-range" aria-label="远程设备音量" aria-valuetext={`${percent}%`} type="range"
            min="0" max="100" value={percent} disabled={disabled || !volumeAvailable} style={{ '--range-fill': `${percent}%` }}
            onChange={(event) => changeVolume(Number(event.target.value))} />
        </div>
        {volume?.devices?.length > 0 && <label className="volume-device"><span>输出设备</span>
          <select value={volume.device_id ?? ''} disabled={disabled}
            onChange={event => send('audio_device_set', { device_id: event.target.value })}>
            {!volume.devices.some(device => device.id === volume.device_id) &&
              <option value="" disabled>选择输出设备</option>}
            {volume.devices.map(device => <option key={device.id} value={device.id}>{device.name}</option>)}
          </select>
        </label>}
      </section>
    </div>
    {error ? <p className="feedback error" role="alert">{error}</p> : command &&
      <p className="feedback muted" role="status">{actions[command.action]} · {results[command.status]}{command.message && ` · ${command.message}`}</p>}
    {playerOpen && <NowPlaying media={playback} lyricsPreferred={lyricsPreferred}
      onLyrics={setLyricsPreferred} onClose={() => setPlayerOpen(false)}
      onAction={action => send(transportActions[action])}
      sound={{ available: volumeAvailable && !disabled, percent, muted: volume?.muted, feedback: error,
        queue: change => Object.hasOwn(change, 'muted')
          ? send('volume_mute', change) : changeVolume(change.percent) }} />}
    {confirm && <Modal id="remote-confirm" title={`确认${actions[confirm]}`} onClose={() => setConfirm(null)}>
      <p className="muted">确定让 {device.name} {actions[confirm]}吗？</p>
      <div className="remote-actions remote-confirm-actions">
        <Button onClick={() => setConfirm(null)}>取消</Button>
        <Button disabled={disabled} onClick={() => { send(confirm); setConfirm(null); }}>确认</Button>
      </div>
    </Modal>}
  </>;
}
