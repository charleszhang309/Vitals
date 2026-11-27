import { lazy, Suspense, useEffect, useRef, useState } from 'react';
import { AnimatePresence, MotionConfig, motion, useReducedMotion } from 'framer-motion';
import { Activity, RefreshCw, Settings, Sparkles } from 'lucide-react';
import { AppearanceDialog, AppearanceFrame, useAppearance } from './appearance.jsx';
import { ClockDialog, FocusNotice, HeaderClock } from './Clock.jsx';
import ControlPage from './ControlPage.jsx';
import { usePowerControls } from './HostControls.jsx';
import MonitorPage from './MonitorPage.jsx';
import ScratchpadPage from './ScratchpadPage.jsx';
import TodayPage from './TodayPage.jsx';
import { Button, IconButton, Segmented } from './ui.jsx';
import { useTelemetry } from './useTelemetry.js';
import { useAiSettings } from './useAiSettings.js';
import { useMedia } from './useMedia.js';
import { useFocusTimer } from './useFocusTimer.js';
import useOverviewResource from './useOverviewResource.js';
import { HUB_DEVICE_ID } from './RemoteDevice.jsx';
import { request, sendJson } from './api.js';

const AiChat = lazy(() => import('./AiChat.jsx'));
const views = [{ value: 'today', label: '今日概览' }, { value: 'control', label: '控制' },
  { value: 'scratchpad', label: '中转站' }];

function useHostUpdate() {
  const host = useOverviewResource('/api/system/version', 300_000).data;
  const initial = useRef(null);
  const [available, setAvailable] = useState(false);
  useEffect(() => {
    if (!host?.version) return;
    if (!initial.current) {
      initial.current = host;
    } else if (host.version !== initial.current.version ||
      (host.build_id && initial.current.build_id && host.build_id !== initial.current.build_id)) {
      setAvailable(true);
    } else if (!initial.current.build_id && host.build_id) {
      initial.current = host;
    }
  }, [host]);
  return available;
}

export default function App() {
  const [view, setView] = useState('today');
  const [playerOpen, setPlayerOpen] = useState(false);
  const [clockOpen, setClockOpen] = useState(false);
  const [monitorOpen, setMonitorOpen] = useState(false);
  const [settings, setSettings] = useState(false);
  const [selectedDevice, setSelectedDevice] = useState(null);
  const [offlineNotice, setOfflineNotice] = useState('');
  const [agentActive, setAgentActive] = useState(false);
  const hostUpdated = useHostUpdate();
  const immersiveView = clockOpen ? 'clock' : view === 'control' && playerOpen ? 'player' : null;
  const appearance = useAppearance(immersiveView);
  const { widgets, cardOrder, cardWidths, defaultDevice } = appearance.preferences;
  const watchAgents = view === 'control' || monitorOpen || settings ||
    (view === 'today' && defaultDevice !== HUB_DEVICE_ID && (widgets.host !== false || widgets.media !== false));
  const info = useOverviewResource('/api/system/info', 60_000);
  const agents = useOverviewResource('/api/agents', agentActive ? 250 : 2500, watchAgents);
  const aliasResource = useOverviewResource('/api/agents/aliases', 10_000, view !== 'scratchpad' || settings);
  const aliases = aliasResource.data?.aliases ?? {};
  const devices = (agents.data?.devices ?? []).map((item) => ({
    ...item,
    name: aliases[item.device_id] ?? (aliasResource.data ? item.snapshot.name : item.name),
    online: item.online && !agents.error,
  }));
  const device = devices.find((item) => item.online && item.device_id === (selectedDevice ?? defaultDevice));
  const onlineDevices = devices.filter((item) => item.online);
  const primaryDevice = devices.find((item) => item.device_id === defaultDevice);
  const waitingForPrimary = defaultDevice !== HUB_DEVICE_ID && !agents.data;
  const waitingForSelection = (selectedDevice ?? defaultDevice) !== HUB_DEVICE_ID && !agents.data && !agents.error;
  const telemetry = useTelemetry(settings || ((monitorOpen || view === 'control') && !device && !waitingForSelection) ||
    (view === 'today' && !primaryDevice && !waitingForPrimary && widgets.host !== false));
  const platform = info.data?.platform ?? telemetry.sample?.platform;
  const hub = {
    device_id: HUB_DEVICE_ID, platform,
    reportedName: info.data?.name,
    name: aliases[HUB_DEVICE_ID] ?? (platform === 'macos' ? '本机 Mac' : platform === 'windows' ? 'Windows' : '本机'),
    online: Boolean((info.data && !info.error) || telemetry.connection === 'online'),
  };
  const power = usePowerControls((view === 'control' || monitorOpen) && !device && !waitingForSelection && platform === 'windows');
  const ai = useAiSettings();
  const media = useMedia((view === 'control' && !device && !waitingForSelection) ||
    (view === 'today' && !primaryDevice && !waitingForPrimary && widgets.media !== false));
  const focusTimer = useFocusTimer();
  const [chat, setChat] = useState(false);
  const reduced = useReducedMotion();
  const dark = appearance.theme === 'dark';
  const { data: agentData, error: agentsError } = agents;
  const updatePreference = appearance.update;
  useEffect(() => {
    if (!agentData || agentsError) return;
    const exists = (id) => id === HUB_DEVICE_ID || agentData.devices.some((item) => item.device_id === id);
    if (!exists(defaultDevice)) updatePreference('defaultDevice', HUB_DEVICE_ID);
    if (selectedDevice && !exists(selectedDevice)) setSelectedDevice(HUB_DEVICE_ID);
  }, [agentData, agentsError, defaultDevice, selectedDevice, updatePreference]);

  useEffect(() => {
    if (view !== 'control' || !agentData || agentsError) return;
    const id = selectedDevice ?? defaultDevice;
    const offline = agentData.devices.find((item) => item.device_id === id && !item.online);
    if (offline) {
      setOfflineNotice(`${aliases[id] ?? offline.name} 已离线，已切回本机。`);
      setSelectedDevice(HUB_DEVICE_ID);
      setPlayerOpen(false);
    }
  }, [view, agentData, agentsError, selectedDevice, defaultDevice, aliases]);

  async function renameDevice(id, alias) {
    aliasResource.replace(await sendJson('PATCH', '/api/agents/aliases', { device_id: id, alias }));
  }

  async function removeDevice(id) {
    await request(`/api/agents/${encodeURIComponent(id)}`, { method: 'DELETE' });
    agents.replace({ devices: (agents.data?.devices ?? []).filter((item) => item.device_id !== id) });
    aliasResource.replace({ aliases: Object.fromEntries(Object.entries(aliases).filter(([key]) => key !== id)) });
    if (defaultDevice === id) appearance.update('defaultDevice', HUB_DEVICE_ID);
    if (selectedDevice === id) chooseDevice(HUB_DEVICE_ID);
  }

  function chooseDevice(next) {
    setSelectedDevice(next);
    setPlayerOpen(false);
    setOfflineNotice('');
  }
  function navigate(next, selection = null) {
    if (next !== view) chooseDevice(selection);
    setView(next);
    window.scrollTo({ top: 0, behavior: 'instant' });
  }
  return (
    <MotionConfig reducedMotion="user">
      <AppearanceFrame appearance={appearance}>
        <header className="app-header">
          <div className="header-inner">
            <div className="brand">
              <span className="brand-icon" aria-hidden="true"><Activity size={26} strokeWidth={2} /></span>
              <div className="brand-name"><strong lang="en">Vitals</strong></div>
            </div>
            <nav className="header-tabs" aria-label="功能页面">
              <Segmented id="navigation" label="功能页面" tabs options={views} value={view} onChange={navigate} />
            </nav>
            <div className="header-actions">
              <HeaderClock onOpen={() => setClockOpen(true)} />
              {ai.settings?.enabled && <IconButton label="打开 Vitals AI" className="ai-trigger" aria-haspopup="dialog"
                onClick={() => setChat(true)}><Sparkles size={21} /></IconButton>}
              <IconButton label="个性化设置" onClick={() => setSettings(true)} aria-haspopup="dialog"><Settings size={21} /></IconButton>
            </div>
          </div>
        </header>
        <main className="main-content" id="main-content">
          {hostUpdated && <div className="host-update-notice" role="status">
            <Button onClick={() => window.location.reload()}><RefreshCw size={16} aria-hidden="true" />Vitals 已更新，点击刷新</Button>
          </div>}
          <AnimatePresence initial={false} mode="wait">
            <motion.section key={view} role="tabpanel" aria-labelledby={`tab-${view}`} id={`panel-${view}`}
              initial={{ opacity: 0, y: reduced ? 0 : 7 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 0 }} transition={{ duration: reduced ? 0 : 0.16 }}>
              {view === 'today' && <TodayPage telemetry={telemetry} media={media} dark={dark} widgets={widgets}
                cardOrder={cardOrder} cardWidths={cardWidths}
                hub={hub} primaryDevice={primaryDevice} waitingForPrimary={waitingForPrimary}
                openMonitor={() => { navigate('control', primaryDevice?.device_id ?? HUB_DEVICE_ID); setMonitorOpen(true); }}
                openControl={() => {
                  if (!primaryDevice) media.select(media.current?.session_id ?? null);
                  navigate('control', primaryDevice?.device_id ?? HUB_DEVICE_ID);
                }}
                briefingEnabled={Boolean(ai.settings?.enabled && ai.settings.configured && ai.settings.include_vitals_context)} />}
              {view === 'control' && (waitingForSelection
                ? <p className="feedback" role="status">{agents.error || '正在读取设备…'}</p>
                : <ControlPage media={media} open={playerOpen} onOpenChange={setPlayerOpen} hub={hub}
                sample={telemetry.sample} power={power} onMonitor={() => setMonitorOpen(true)}
                devices={onlineDevices} device={device} offlineNotice={offlineNotice} onDevice={chooseDevice}
                onAgentCommand={agents.retry} onAgentActivity={setAgentActive} />)}
              {view === 'scratchpad' && <ScratchpadPage aiEnabled={Boolean(ai.settings?.enabled && ai.settings.configured)} />}
            </motion.section>
          </AnimatePresence>
        </main>
        {chat && ai.settings?.enabled && <Suspense fallback={null}>
          <AiChat settings={ai.settings} onClose={() => setChat(false)} />
        </Suspense>}
        {settings && <AppearanceDialog appearance={appearance} ai={ai} telemetry={telemetry}
          deviceSettings={{ hub, devices, aliases, defaultDevice, error: agents.error || aliasResource.error,
            onRename: renameDevice, onRemove: removeDevice,
            onDefault: (id) => { appearance.update('defaultDevice', id); chooseDevice(null); } }}
          onClose={() => {
          if (!ai.settings?.enabled) setChat(false);
          setSettings(false);
        }} />}
        {monitorOpen && <MonitorPage telemetry={telemetry} dark={dark} power={power} hub={hub} onClose={() => setMonitorOpen(false)}
          devices={onlineDevices} device={device} onDevice={chooseDevice} />}
        {clockOpen && <ClockDialog style={appearance.preferences.clockStyle} focus={focusTimer} onClose={() => setClockOpen(false)} />}
        <FocusNotice notice={focusTimer.notice} clockOpen={clockOpen} />
      </AppearanceFrame>
    </MotionConfig>
  );
}
