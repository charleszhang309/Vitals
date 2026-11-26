import { useEffect, useState } from 'react';
import { Plus, RefreshCw } from 'lucide-react';
import { request, sendJson } from './api.js';
import { Button, IconButton, Modal } from './ui.jsx';

const endpoint = '/api/calendar/events';
const timeZoneOptions = { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' };
const weekday = new Intl.DateTimeFormat('zh-CN', { weekday: 'short', timeZone: 'UTC' });
const namedDate = new Intl.DateTimeFormat('zh-CN', { month: 'long', day: 'numeric', weekday: 'long', timeZone: 'UTC' });

function localParts(instant, timeZone) {
  const parts = new Intl.DateTimeFormat('en-CA', { ...timeZoneOptions, timeZone }).formatToParts(new Date(instant));
  return Object.fromEntries(parts.filter(({ type }) => type !== 'literal').map(({ type, value }) => [type, value]));
}

function localDateTime(instant, zone) {
  const { year, month, day, hour, minute } = localParts(instant, zone);
  return `${year}-${month}-${day}T${hour}:${minute}`;
}

function calendarDate(value, zone) {
  return value.length === 10 ? value : localDateTime(value, zone).slice(0, 10);
}

function dayAfter(day, offset = 1) {
  return new Date(Date.parse(`${day}T00:00:00Z`) + offset * 86_400_000).toISOString().slice(0, 10);
}

function eventsOn(events, dates, zone) {
  const groups = new Map(dates.map((day) => [day, []]));
  for (const event of events) {
    const first = calendarDate(event.start, zone);
    // 全天的结束日期和跨午夜日程的结束时刻都不计入下一天。
    const last = event.all_day ? dayAfter(event.end, -1)
      : localDateTime(Math.max(Date.parse(event.start), Date.parse(event.end) - 1), zone).slice(0, 10);
    for (const [day, items] of groups) {
      if (first <= day && day <= last) items.push(event);
    }
  }
  return groups;
}

function relativeDay(day, today) {
  if (day === today) return '今天';
  if (day === dayAfter(today)) return '明天';
  return `${Number(day.slice(5, 7))}月${Number(day.slice(8))}日`;
}

function instantFor(value, zone) {
  const wall = Date.parse(`${value}Z`);
  if (!Number.isFinite(wall)) throw new Error('请选择有效的起止时间');
  let estimate = wall;
  for (let iteration = 0; iteration < 3; iteration += 1) {
    const shifted = Date.parse(`${localDateTime(estimate, zone)}Z`);
    estimate += wall - shifted;
  }
  if (localDateTime(estimate, zone) !== value) throw new Error('所选时间在夏令时切换时不存在');
  return new Date(estimate).toISOString();
}

function eventTime(event, zone) {
  if (event.all_day) return event.end === dayAfter(event.start) ? '全天' : `全天 · 至 ${dayAfter(event.end, -1).slice(5)}`;
  const start = localDateTime(event.start, zone);
  const end = localDateTime(event.end, zone);
  return `${start.slice(11)}–${start.slice(0, 10) === end.slice(0, 10) ? end.slice(11) : end.slice(5).replace('T', ' ')}`;
}

function eventPath(event) {
  return `${endpoint}/${encodeURIComponent(event.source_id)}/${encodeURIComponent(event.id)}`;
}

function Dot({ color }) {
  return <span className="calendar-source-dot" style={{ backgroundColor: color }} aria-hidden="true" />;
}

export function CalendarPreview({ resource }) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 20_000);
    return () => window.clearInterval(timer);
  }, []);
  const { data, error } = resource;
  const zone = data?.timezone;
  const today = localDateTime(now, zone).slice(0, 10);
  const limit = dayAfter(today, 7);
  const remaining = data?.events.filter((item) => calendarDate(item.start, zone) < limit && (
    item.all_day ? item.end > today : Math.max(Date.parse(item.end), Date.parse(item.start) + 1) > now
  )) ?? [];
  const next = remaining.find((item) => item.all_day ? item.start >= today : Date.parse(item.start) > now) ?? remaining[0];
  const unavailable = error || (data?.sources.length > 0 && !data.sources.some((source) => ['live', 'stale'].includes(source.status)));
  const eventDay = next && calendarDate(next.start, zone);
  const eventStart = next && (next.all_day ? '全天' : localDateTime(next.start, zone).slice(11));
  const starts = next && (eventDay < today || (!next.all_day && Date.parse(next.start) <= now)
    ? '进行中' : `${relativeDay(eventDay, today)} ${eventStart}`);
  return <div className="calendar-preview">
    <time className="calendar-preview-date" dateTime={today} aria-label={namedDate.format(new Date(today))}>
      <span>{weekday.format(new Date(today))}</span>
      <strong className="numeric">{Number(today.slice(8))}</strong>
      <small className="numeric">{Number(today.slice(5, 7))}月</small>
    </time>
    <div className="calendar-preview-event">
      {next ? <><p className="widget-muted numeric">{starts}</p>
        <p className="calendar-preview-title"><Dot color={next.color} /><strong className="widget-truncate" title={next.title}>{next.title}</strong></p></>
        : <p className="widget-muted">{unavailable ? '日历暂不可用' : data ? '近期暂无日程' : '正在读取日历…'}</p>}
    </div>
  </div>;
}

function eventFields(event, data, day) {
  const zone = data.timezone;
  if (event) return {
    source_id: event.source_id, title: event.title, all_day: event.all_day, location: event.location, notes: event.notes,
    start: event.all_day ? event.start : localDateTime(event.start, zone),
    end: event.all_day ? dayAfter(event.end, -1) : localDateTime(event.end, zone),
  };
  const now = Date.now();
  const currentDay = localDateTime(now, zone).slice(0, 10);
  return { source_id: data.sources.find((source) => source.writable)?.id ?? '', title: '', all_day: false, location: '', notes: '',
    start: day === currentDay ? localDateTime(now, zone) : `${day}T09:00`,
    end: day === currentDay ? localDateTime(now + 3_600_000, zone) : `${day}T10:00` };
}

function CalendarForm({ event, data, day, onSave, onCancel, busy, savingError }) {
  const [fields, setFields] = useState(() => eventFields(event, data, day));
  const [error, setError] = useState('');
  const zone = data.timezone;
  const set = (key, value) => setFields((current) => ({ ...current, [key]: value }));
  function toggleDay(allDay) {
    setFields((current) => ({ ...current, all_day: allDay,
      start: allDay ? current.start.slice(0, 10) : `${current.start}T09:00`,
      end: allDay ? current.end.slice(0, 10) : `${current.end}T10:00` }));
  }
  async function submit(submission) {
    submission.preventDefault();
    setError('');
    try {
      const start = fields.all_day ? fields.start : instantFor(fields.start, zone);
      const end = fields.all_day ? dayAfter(fields.end) : instantFor(fields.end, zone);
      if (end <= start) throw new Error('结束时间须晚于开始时间');
      const payload = { ...fields, start, end };
      if (event) {
        delete payload.source_id;
        for (const key of ['title', 'location', 'notes']) if (payload[key] === event[key]) delete payload[key];
        if (fields.all_day === event.all_day && fields.start === eventFields(event, data).start && fields.end === eventFields(event, data).end) {
          delete payload.start; delete payload.end; delete payload.all_day;
        }
      }
      await onSave(payload);
    } catch (failure) { setError(failure.message); }
  }
  const type = fields.all_day ? 'date' : 'datetime-local';
  return <form className="widget-form" onSubmit={submit}>
    <h3>{event ? '编辑日程' : '新建日程'}</h3>
    {!event && <label>日历<select required value={fields.source_id} onChange={(change) => set('source_id', change.target.value)}>
      {data.sources.filter((source) => source.writable).map((source) => <option key={source.id} value={source.id}>{source.name}</option>)}
    </select></label>}
    <label>标题<input required maxLength={300} value={fields.title} onChange={(change) => set('title', change.target.value)} /></label>
    <label className="widget-checkbox"><input type="checkbox" checked={fields.all_day} onChange={(change) => toggleDay(change.target.checked)} />全天</label>
    <div className="widget-form-pair"><label>{fields.all_day ? '开始日期' : '开始时间'}<input required type={type} value={fields.start} onChange={(change) => set('start', change.target.value)} /></label>
      <label>{fields.all_day ? '最后一天' : '结束时间'}<input required type={type} value={fields.end} onChange={(change) => set('end', change.target.value)} /></label></div>
    {!fields.all_day && <p className="muted">时区 · {zone}</p>}
    <label>地点<input maxLength={500} value={fields.location} onChange={(change) => set('location', change.target.value)} /></label>
    <label>备注<textarea rows={3} maxLength={8000} value={fields.notes} onChange={(change) => set('notes', change.target.value)} /></label>
    {(error || savingError) && <p className="feedback error" role="alert">{error || savingError}</p>}
    <div className="widget-actions"><Button type="submit" disabled={busy}>{busy ? '正在保存…' : '保存日程'}</Button><Button onClick={onCancel} disabled={busy}>取消</Button></div>
  </form>;
}

export function CalendarDetails({ resource, onClose }) {
  const { data, error, replace, retry } = resource;
  const [selectedDay, setSelectedDay] = useState(null);
  const [editing, setEditing] = useState(null);
  const [confirm, setConfirm] = useState(null);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState('');
  async function change(action) {
    setBusy(true);
    setFailure('');
    try { await action(); }
    catch (problem) { setFailure(problem.message); }
    finally { setBusy(false); }
  }
  function open(event) {
    setFailure(''); setConfirm(null); setEditing({ event });
  }
  const today = localDateTime(Date.now(), data?.timezone).slice(0, 10);
  const days = Array.from({ length: 7 }, (_, index) => dayAfter(today, index));
  const groups = eventsOn(data?.events ?? [], days, data?.timezone);
  const day = groups.has(selectedDay) ? selectedDay : today;
  const items = groups.get(day);
  const headerActions = <>
    <IconButton label="刷新同步" disabled={busy || Boolean(editing)}
      onClick={() => change(async () => replace(await request(`${endpoint}?refresh=1`)))}><RefreshCw size={17} /></IconButton>
    <IconButton label="新建日程" disabled={busy || Boolean(editing) || !data?.sources.some((source) => source.writable)}
      onClick={() => open(null)}><Plus size={19} /></IconButton>
  </>;
  return <Modal id="overview-calendar" title="日历" className="detail-dialog overview-dialog calendar-dialog" onClose={onClose} headerActions={headerActions}>
    <div className="widget-detail-stack">
      <div className="calendar-week" role="group" aria-label="未来 7 天">
        {days.map((date) => {
          const instant = new Date(date);
          const count = groups.get(date).length;
          return <Button className="calendar-day" key={date} disabled={busy || Boolean(editing)}
            aria-pressed={date === day} aria-current={date === today ? 'date' : undefined}
            aria-label={`${namedDate.format(instant)}，${count ? `${count} 项日程` : '暂无日程'}`}
            onClick={() => { setSelectedDay(date); setConfirm(null); setFailure(''); }}>
            <span>{weekday.format(instant)}</span><strong className="numeric">{instant.getUTCDate()}</strong>
            <i className={count ? 'has-events' : ''} aria-hidden="true" />
          </Button>;
        })}
      </div>
      {data?.sources.length > 0 && <ul className="calendar-sources" aria-label="日历来源">{data.sources.map((source) => <li key={source.id}><Dot color={source.color} />{source.name}{!source.writable && ' · 只读'}{source.status === 'stale' && ' · 缓存'}{source.status === 'unavailable' && ' · 暂不可用'}</li>)}</ul>}
      {editing?.event === null && <CalendarForm data={data} day={day} busy={busy} savingError={failure} onCancel={() => setEditing(null)}
        onSave={(payload) => change(async () => {
          await sendJson('POST', endpoint, payload);
          setSelectedDay(calendarDate(payload.start, data.timezone));
          setEditing(null);
          retry();
        })} />}
      {!editing && (failure || error) && <p className="feedback error" role="alert">{failure || error}</p>}
      <section className="widget-detail-stack calendar-group" aria-labelledby="calendar-day-heading">
        <h3 id="calendar-day-heading">{relativeDay(day, today)}</h3>
        {!items.length && <p className="section-empty">{!data ? (error ? '日历暂不可用' : '正在读取日程…')
          : data.sources.length ? '当天暂无日程' : '请在服务端配置日历来源。'}</p>}
        {items.map((event) => {
          const key = `${event.source_id}:${event.id}`;
          const active = editing?.event && eventPath(editing.event) === eventPath(event);
          return <article className="widget-list-row calendar-event" key={key}>
            {active ? <CalendarForm key={key} event={editing.event} data={data} busy={busy} savingError={failure} onCancel={() => setEditing(null)}
              onSave={(payload) => change(async () => { await sendJson('PATCH', eventPath(event), payload); setEditing(null); retry(); })} /> : <>
              <div className="calendar-event-title"><Dot color={event.color} /><strong>{event.title}</strong></div>
              <p className="muted numeric">{eventTime(event, data.timezone)} · {event.source_name}{event.location && ` · ${event.location}`}</p>
              {event.notes && <p className="calendar-notes">{event.notes}</p>}
              {event.writable && <div className="widget-actions">{confirm === key ? <>
                <Button disabled={busy} onClick={() => change(async () => { await request(eventPath(event), { method: 'DELETE' }); setConfirm(null); retry(); })}>确认删除</Button>
                <Button disabled={busy} onClick={() => setConfirm(null)}>取消</Button>
              </> : <><Button disabled={busy || Boolean(editing)} aria-label={`编辑日程 ${event.title}`} onClick={() => open(event)}>编辑</Button>
                <Button disabled={busy || Boolean(editing)} aria-label={`删除日程 ${event.title}`} onClick={() => setConfirm(key)}>删除</Button></>}</div>}
            </>}
          </article>;
        })}
      </section>
    </div>
  </Modal>;
}
