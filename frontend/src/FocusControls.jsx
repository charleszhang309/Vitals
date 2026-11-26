import { useState } from 'react';
import { AlertCircle } from 'lucide-react';
import { Button, Segmented } from './ui.jsx';
import { focusClock, presets, validFocusMinutes } from './useFocusTimer.js';

const focusChoices = [...presets.map((minutes) => ({ value: minutes, label: `${minutes} 分钟` })), { value: 'custom', label: '自定义' }];

export function FocusDetails({ focus, resource }) {
  const [custom, setCustom] = useState(() => !presets.includes(focus.minutes));
  const [draft, setDraft] = useState(() => String(focus.minutes));
  const locked = focus.running || focus.paused || focus.finishing;
  const valid = validFocusMinutes(Number(draft)) && /^\d+$/.test(draft);
  return <div className="widget-detail-stack focus-detail">
    <Segmented id="focus-presets" label="专注时长" options={focusChoices} value={custom ? 'custom' : focus.minutes}
      onChange={(choice) => {
        setCustom(choice === 'custom');
        if (choice === 'custom') setDraft(String(focus.minutes));
        else focus.reset(choice);
      }} disabled={locked} />
    {custom && <div className="widget-form"><label>自定义时长（分钟）
      <input type="number" className="numeric" min="1" max="180" step="1" inputMode="numeric" value={draft} disabled={locked}
        aria-invalid={!valid} aria-describedby={!valid ? 'focus-duration-error' : undefined}
        onChange={(event) => {
          const value = event.target.value;
          setDraft(value);
          if (/^\d+$/.test(value) && validFocusMinutes(Number(value))) focus.reset(Number(value));
        }} /></label>
      {!valid && <p className="feedback error" id="focus-duration-error">请输入 1～180 分钟的整数。</p>}
    </div>}
    <div className="focus-clock numeric" aria-label="剩余时间" role="timer">{focusClock(focus.remaining)}</div>
    <div className="widget-actions"><Button disabled={focus.finishing || (custom && !valid)} onClick={focus.toggle}>{focus.running ? '暂停' : focus.paused ? '继续' : '开始专注'}</Button>
      <Button disabled={focus.finishing} onClick={() => { focus.reset(); setDraft(String(focus.minutes)); }}>重置</Button></div>
    <p className="muted numeric">今日累计 {resource.data?.total_minutes ?? '—'} 分钟</p>
    {resource.error && <p className="feedback error focus-feedback" role="alert"><AlertCircle size={16} />{resource.error}</p>}
  </div>;
}
