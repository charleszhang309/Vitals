import { lazy, Suspense, useState } from 'react';
import { AnimatePresence, MotionConfig, motion, useReducedMotion } from 'framer-motion';
import { Activity, Settings, Sparkles } from 'lucide-react';
import { AppearanceDialog, AppearanceFrame, useAppearance } from './appearance.jsx';
import ControlPage from './ControlPage.jsx';
import MonitorPage from './MonitorPage.jsx';
import ScratchpadPage from './ScratchpadPage.jsx';
import TodayPage from './TodayPage.jsx';
import { IconButton, Segmented } from './ui.jsx';
import { useTelemetry } from './useTelemetry.js';
import { useAiSettings } from './useAiSettings.js';
import { useMedia } from './useMedia.js';

const AiChat = lazy(() => import('./AiChat.jsx'));
const views = [{ value: 'today', label: '今日概览' }, { value: 'control', label: '控制' },
  { value: 'monitor', label: '硬件监控' }, { value: 'scratchpad', label: '中转站' }];
const connectionLabels = { online: 'WebSocket 在线', offline: 'WebSocket 已断开', connecting: 'WebSocket 连接中' };

export default function App() {
  const telemetry = useTelemetry();
  const [view, setView] = useState('today');
  const [playerOpen, setPlayerOpen] = useState(false);
  const appearance = useAppearance(view === 'control' && playerOpen);
  const ai = useAiSettings();
  const media = useMedia(view === 'today' || view === 'control');
  const [settings, setSettings] = useState(false);
  const [chat, setChat] = useState(false);
  const reduced = useReducedMotion();
  const { connection, sample } = telemetry;
  const lhm = sample?.lhm_connected === false ? 'off' : connection === 'online' && sample?.lhm_connected ? 'on' : 'waiting';
  const dark = appearance.preferences.theme === 'dark';
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
            <div className="header-right">
              <div role="status" className="connection-status">
                <span className={`state-dot ${connection === 'online' ? 'live-dot pulse' : connection === 'offline' ? 'offline-dot' : ''}`} aria-hidden="true" />
                {connectionLabels[connection]}
              </div>
              <div role="status" className={`lhm-status lhm-${lhm}`}><span className="state-dot" aria-hidden="true" />
                {lhm === 'on' ? 'LHM 已连接' : lhm === 'off' ? 'LHM 未连接' : 'LHM 等待数据'}
              </div>
              <div className="header-actions">
                {ai.settings?.enabled && <IconButton label="打开 Vitals AI" className="ai-trigger" aria-haspopup="dialog"
                  onClick={() => setChat(true)}><Sparkles size={21} /></IconButton>}
                <IconButton label="个性化设置" onClick={() => setSettings(true)} aria-haspopup="dialog"><Settings size={21} /></IconButton>
              </div>
            </div>
          </div>
        </header>
        <main className="main-content" id="main-content">
          <AnimatePresence initial={false} mode="wait">
            <motion.section key={view} role="tabpanel" aria-labelledby={`tab-${view}`} id={`panel-${view}`}
              initial={{ opacity: 0, y: reduced ? 0 : 7 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 0 }} transition={{ duration: reduced ? 0 : 0.16 }}>
              {view === 'today' && <TodayPage telemetry={telemetry} media={media} dark={dark} openMonitor={() => navigate('monitor')}
                openControl={() => { media.select(media.current?.session_id ?? null); navigate('control'); }}
                briefingEnabled={Boolean(ai.settings?.enabled && ai.settings.configured && ai.settings.include_vitals_context)} />}
              {view === 'control' && <ControlPage media={media} open={playerOpen} onOpenChange={setPlayerOpen} />}
              {view === 'monitor' && <MonitorPage telemetry={telemetry} dark={dark} />}
              {view === 'scratchpad' && <ScratchpadPage aiEnabled={Boolean(ai.settings?.enabled && ai.settings.configured)} />}
            </motion.section>
          </AnimatePresence>
        </main>
        {chat && ai.settings?.enabled && <Suspense fallback={null}>
          <AiChat settings={ai.settings} onClose={() => setChat(false)} />
        </Suspense>}
        {settings && <AppearanceDialog appearance={appearance} ai={ai} onClose={() => {
          if (!ai.settings?.enabled) setChat(false);
          setSettings(false);
        }} />}
      </AppearanceFrame>
    </MotionConfig>
  );
}
