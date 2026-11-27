import { localizedDate, localizedNumber, t } from './i18n/messages.js';
const numberFormats = new Map();
const clock = localizedDate({ hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' });
const dateTime = localizedDate({ year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });

export function number(value, digits = 1) {
  if (!Number.isFinite(value)) return '—';
  if (!numberFormats.has(digits)) numberFormats.set(digits, localizedNumber({ maximumFractionDigits: digits }));
  return numberFormats.get(digits).format(value);
}

export function measure(value, unit, digits = 1) {
  return Number.isFinite(value) ? `${number(value, digits)} ${unit}` : '—';
}

export function bytes(value) {
  if (!Number.isFinite(value)) return '—';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  let scale = 0;
  while (value >= 1024 && scale < units.length - 1) {
    value /= 1024;
    scale += 1;
  }
  return measure(value, units[scale], scale ? 1 : 0);
}

export function rate(value) {
  if (!Number.isFinite(value)) return { value: '—', unit: '' };
  const bits = value * 8;
  if (bits >= 1e9) return { value: number(bits / 1e9, 2), unit: 'Gbps' };
  if (bits >= 1e6) return { value: number(bits / 1e6, 2), unit: 'Mbps' };
  if (bits >= 1e3) return { value: number(bits / 1e3, 1), unit: 'Kbps' };
  return { value: number(bits, 0), unit: 'bps' };
}

export function rateText(value) {
  const formatted = rate(value);
  return `${formatted.value} ${formatted.unit}`.trim();
}

export function time(value, date = false) {
  const epoch = typeof value === 'number' ? value : Date.parse(value);
  return Number.isFinite(epoch) ? (date ? dateTime : clock).format(epoch) : '—';
}

export function sleepTime(minutes) {
  if (!Number.isFinite(minutes)) return '—';
  const whole = Math.round(minutes);
  return whole < 60 ? t('{p1} 分', { p1: whole }) : t('{p1} 小时 {p2} 分', { p1: Math.floor(whole / 60), p2: whole % 60 });
}

export function duration(start, end) {
  const delta = Date.parse(end) - Date.parse(start);
  if (!Number.isFinite(delta) || delta < 0) return null;
  const seconds = Math.floor(delta / 1000);
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return [hours ? t('{p1} 小时', { p1: hours }) : '', hours || minutes ? t('{p1} 分', { p1: minutes }) : '',
    t('{p1} 秒', { p1: seconds % 60 })].filter(Boolean).join(' ');
}
