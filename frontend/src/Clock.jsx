import { useEffect, useRef, useState } from 'react';
import { useReducedMotion } from 'framer-motion';
import { Clock3, Hourglass, Timer } from 'lucide-react';
import { Button, IconButton, Modal } from './ui.jsx';
import { FocusDetails } from './FocusControls.jsx';
import { focusClock } from './useFocusTimer.js';
import useResource from './useOverviewResource.js';

const minuteFormat = new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
const secondFormat = new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' });
const dateFormat = new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: 'long', day: 'numeric', weekday: 'long' });
export const clockStyles = [
  { value: 'flip', label: '翻页' }, { value: 'digital', label: '极简数字' },
  { value: 'analog', label: '模拟表盘' },
];

function useClock(period) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    let timer;
    function sync() {
      window.clearTimeout(timer);
      if (document.hidden) return;
      const current = Date.now();
      setNow(current);
      timer = window.setTimeout(sync, period - current % period);
    }
    sync();
    document.addEventListener('visibilitychange', sync);
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener('visibilitychange', sync);
    };
  }, [period]);
  return now;
}

export function HeaderClock({ onOpen }) {
  const now = useClock(60_000);
  return <Button className="header-clock numeric" aria-haspopup="dialog" onClick={onOpen}>
    <span className="sr-only">打开全屏时钟，当前时间 </span><time>{minuteFormat.format(now)}</time>
  </Button>;
}

function FlipUnit({ value, reduced }) {
  const [pair, setPair] = useState({ current: value, previous: value });
  if (pair.current !== value) setPair({ current: value, previous: pair.current });
  const before = pair.previous;
  return <span className="flip-unit">
    <span className="flip-face flip-upper"><span>{value}</span></span>
    <span className="flip-face flip-lower"><span>{before !== value && !reduced ? before : value}</span></span>
    {before !== value && !reduced && <span className="flip-animation" key={value}>
      <span className="flip-face flip-upper flip-fold-out"><span>{before}</span></span>
      <span className="flip-face flip-lower flip-fold-in"><span>{value}</span></span>
    </span>}
  </span>;
}

function AnalogFace({ now, reduced }) {
  const secondHand = useRef(null);
  const clock = new Date(now);
  const secondAngle = clock.getSeconds() * 6;
  const minuteAngle = clock.getMinutes() * 6 + secondAngle / 60;
  const hourAngle = clock.getHours() * 30 + minuteAngle / 12;
  useEffect(() => {
    if (reduced) return undefined;
    let frame;
    function draw() {
      const current = new Date();
      const angle = (current.getSeconds() + current.getMilliseconds() / 1000) * 6;
      secondHand.current?.setAttribute('transform', `rotate(${angle} 60 60)`);
      frame = requestAnimationFrame(draw);
    }
    draw();
    return () => cancelAnimationFrame(frame);
  }, [reduced]);

  return <svg className="analog-clock" viewBox="0 0 120 120" aria-hidden="true">
    <circle className="analog-rim" cx="60" cy="60" r="58" />
    {Array.from({ length: 60 }, (_, index) => <line key={index} className={index % 5 === 0 ? 'analog-hour-mark' : 'analog-minute-mark'}
      x1="60" y1="8" x2="60" y2={index % 5 === 0 ? 14 : 10} transform={`rotate(${index * 6} 60 60)`} />)}
    <line className="analog-hour-hand" x1="60" y1="60" x2="60" y2="34" transform={`rotate(${hourAngle} 60 60)`} />
    <line className="analog-minute-hand" x1="60" y1="60" x2="60" y2="21" transform={`rotate(${minuteAngle} 60 60)`} />
    <line ref={secondHand} className="analog-second-hand" x1="60" y1="69" x2="60" y2="16"
      transform={reduced ? `rotate(${secondAngle} 60 60)` : undefined} />
    <circle className="analog-pin" cx="60" cy="60" r="2.5" />
  </svg>;
}

function ClockReading({ style }) {
  const now = useClock(1000);
  const reduced = useReducedMotion();
  const reading = secondFormat.format(now);
  return <div className={`clock-reading clock-style-${style}`}>
    <time className="clock-digits numeric" dateTime={new Date(now).toISOString()}>
      {style === 'digital' ? reading : <>
        <span className="sr-only">{reading}</span>
        {style === 'flip' ? <span className="flip-clock" aria-hidden="true">
          {reading.split(':').map((value, index) => <FlipUnit key={index} value={value} reduced={reduced} />)}
        </span> : <AnalogFace now={now} reduced={reduced} />}
      </>}
    </time>
    <p className="clock-date">{dateFormat.format(now)}</p>
  </div>;
}

export function FocusNotice({ notice, clockOpen }) {
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    if (!notice) return undefined;
    setVisible(true);
    const timeout = setTimeout(() => setVisible(false), 8000);
    return () => clearTimeout(timeout);
  }, [notice]);
  return notice && visible && !clockOpen ? <p className="surface focus-toast" role="status">{notice}</p> : null;
}

export function ClockDialog({ style, focus, onClose }) {
  const [panel, setPanel] = useState(false);
  const [showFocus, setShowFocus] = useState(true);
  const totals = useResource('/api/focus', 20_000, panel);
  const { replace } = totals;
  useEffect(() => { if (focus.record) replace(focus.record); }, [focus.record, replace]);
  const active = focus.running || focus.paused || focus.finishing;
  const showing = active && showFocus;
  const actions = <>
    {active && <IconButton label={showing ? '显示时钟' : '显示专注倒计时'} onClick={() => setShowFocus(!showing)}>
      {showing ? <Clock3 size={18} /> : <Hourglass size={18} />}
    </IconButton>}
    <IconButton label="专注计时" aria-expanded={panel} aria-controls={panel ? 'clock-focus-panel' : undefined}
      onClick={() => setPanel(!panel)}><Timer size={20} /></IconButton>
  </>;
  return <Modal id="standby-clock" title="全屏时钟" className="clock-dialog" onClose={onClose} headerActions={actions}>
    {showing ? <div className="clock-reading clock-focus-reading">
      <div className="clock-digits numeric" role="timer" aria-label="专注剩余时间">{focusClock(focus.remaining)}</div>
      <p className="clock-date">{focus.finishing ? '正在记录专注' : focus.paused ? '专注已暂停' : '专注中'}</p>
    </div> : <ClockReading style={style} />}
    {focus.notice && <p className="clock-notice" role="status">{focus.notice}</p>}
    {panel && <section className="clock-focus-panel" id="clock-focus-panel" aria-label="专注计时设置">
      <FocusDetails resource={totals} focus={{ ...focus, toggle: () => {
        if (!focus.running) { setShowFocus(true); setPanel(false); }
        focus.toggle();
      } }} />
    </section>}
  </Modal>;
}
