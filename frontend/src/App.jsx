import { lazy, Suspense, useState } from 'react';
import { AnimatePresence, MotionConfig, motion, useReducedMotion } from 'framer-motion';
import { Activity, Settings, Sparkles } from 'lucide-react';
import { AppearanceDialog, AppearanceFrame, useAppearance } from './appearance.jsx';
import { ClockDialog, FocusNotice, HeaderClock } from './Clock.jsx';
import ControlPage from './ControlPage.jsx';
import { usePowerControls } from './HostControls.jsx';
import MonitorPage from './MonitorPage.jsx';
import ScratchpadPage from './ScratchpadPage.jsx';
import TodayPage from './TodayPage.jsx';
import { IconButton, Segmented } from './ui.jsx';
import { useTelemetry } from './useTelemetry.js';
import { useAiSettings } from './useAiSettings.js';
import { useMedia } from './useMedia.js';
import { useFocusTimer } from './useFocusTimer.js';
import useOverviewResource from './useOverviewResource.js';

const AiChat = lazy(() => import('./AiChat.jsx'));
const views = [{ value: 'today', label: '今日概览' }, { value: 'control', label: '控制' },
  { value: 'scratchpad', label: '中转站' }];

export default function App() {
  const [view, setView] = useState('today');
  const [playerOpen, setPlayerOpen] = useState(false);
  const [clockOpen, setClockOpen] = useState(false);
  const [monitorOpen, setMonitorOpen] = useState(false);
  const [settings, setSettings] = useState(false);
  const [selectedDevice, setSelectedDevice] = useState(null);
  const [agentActive, setAgentActive] = useState(false);
  const agents = useOverviewResource('/api/agents', agentActive ? 250 : 2500, view === 'control' || monitorOpen);
  const devices = agents.data?.devices ?? [];
  const selected = devices.find((device) => device.device_id === selectedDevice);
  const device = selected && agents.error ? { ...selected, online: false } : selected;
  const immersiveView = clockOpen ? 'clock' : view === 'control' && playerOpen ? 'player' : null;
  const appearance = useAppearance(immersiveView);
  const widgets = appearance.preferences.widgets;
  const telemetry = useTelemetry(settings || monitorOpen || view === 'control' || (view === 'today' && widgets.host !== false));
  const power = usePowerControls((view === 'control' || monitorOpen) && !device);
  const ai = useAiSettings();
  const media = useMedia((view === 'control' && !device) || (view === 'today' && widgets.media !== false));
  const focusTimer = useFocusTimer();
  const [chat, setChat] = useState(false);
  const reduced = useReducedMotion();
  const dark = appearance.theme === 'dark';
  function chooseDevice(next) {
    setSelectedDevice(next);
    setPlayerOpen(false);
  }
  function navigate(next) {
    if (next !== 'control') setPlayerOpen(false);
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
          <AnimatePresence initial={false} mode="wait">
            <motion.section key={view} role="tabpanel" aria-labelledby={`tab-${view}`} id={`panel-${view}`}
              initial={{ opacity: 0, y: reduced ? 0 : 7 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 0 }} transition={{ duration: reduced ? 0 : 0.16 }}>
              {view === 'today' && <TodayPage telemetry={telemetry} media={media} dark={dark} widgets={widgets}
                openMonitor={() => { chooseDevice(null); navigate('control'); setMonitorOpen(true); }}
                openControl={() => { chooseDevice(null); media.select(media.current?.session_id ?? null); navigate('control'); }}
                briefingEnabled={Boolean(ai.settings?.enabled && ai.settings.configured && ai.settings.include_vitals_context)} />}
              {view === 'control' && <ControlPage media={media} open={playerOpen} onOpenChange={setPlayerOpen}
                sample={telemetry.sample} power={power} onMonitor={() => setMonitorOpen(true)}
                devices={devices} device={device} onDevice={chooseDevice} onAgentCommand={agents.retry} onAgentActivity={setAgentActive} />}
              {view === 'scratchpad' && <ScratchpadPage aiEnabled={Boolean(ai.settings?.enabled && ai.settings.configured)} />}
            </motion.section>
          </AnimatePresence>
        </main>
        {chat && ai.settings?.enabled && <Suspense fallback={null}>
          <AiChat settings={ai.settings} onClose={() => setChat(false)} />
        </Suspense>}
        {settings && <AppearanceDialog appearance={appearance} ai={ai} telemetry={telemetry} onClose={() => {
          if (!ai.settings?.enabled) setChat(false);
          setSettings(false);
        }} />}
        {monitorOpen && <MonitorPage telemetry={telemetry} dark={dark} power={power} onClose={() => setMonitorOpen(false)}
          devices={devices} device={device} onDevice={chooseDevice} />}
        {clockOpen && <ClockDialog style={appearance.preferences.clockStyle} focus={focusTimer} onClose={() => setClockOpen(false)} />}
        <FocusNotice notice={focusTimer.notice} clockOpen={clockOpen} />
      </AppearanceFrame>
    </MotionConfig>
  );
}
