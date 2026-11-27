import { t } from './i18n/messages.js';
import { CalendarDays, CircleDollarSign, CloudSun, HeartPulse, ListTodo, CalendarClock, ChartNoAxesColumnIncreasing, Receipt } from 'lucide-react';

export const overviewStrips = [
  { id: 'host', get title() { return t('主机状态条'); } },
  { id: 'media', get title() { return t('正在播放条'); } },
  { id: 'briefing', get title() { return t('明日香每日简报'); } },
];

export const overviewCards = [
  { id: 'weather', get title() { return t('天气'); }, Icon: CloudSun, tone: 'blue' },
  { id: 'health', get title() { return t('健康'); }, Icon: HeartPulse, tone: 'red' },
  { id: 'calendar', get title() { return t('日历'); }, Icon: CalendarDays, tone: 'red' },
  { id: 'tasks', get title() { return t('待办'); }, Icon: ListTodo, tone: 'green' },
  { id: 'countdowns', get title() { return t('倒数日'); }, Icon: CalendarClock, tone: 'purple' },
  { id: 'rates', get title() { return t('汇率'); }, Icon: CircleDollarSign, tone: 'blue' },
  { id: 'ai-usage', get title() { return t('AI 用量'); }, Icon: ChartNoAxesColumnIncreasing, tone: 'green' },
  { id: 'expenses', get title() { return t('账单'); }, Icon: Receipt, tone: 'purple' },
];

export const overviewModules = [...overviewStrips, ...overviewCards];
export const defaultCardOrder = overviewCards.map(({ id }) => id);
const cardsById = new Map(overviewCards.map((card) => [card.id, card]));

export function readOverviewWidgets(preferences) {
  const existing = preferences !== null && typeof preferences === 'object' && !Array.isArray(preferences);
  return Object.fromEntries(overviewModules.map(({ id }) => [id, existing
    ? preferences.widgets?.[id] !== false : id !== 'expenses']));
}

export function readCardOrder(storedOrder) {
  const known = Array.isArray(storedOrder) ? storedOrder.filter((id) => cardsById.has(id)) : [];
  return [...new Set([...known, ...defaultCardOrder])];
}

export function readCardWidths(storedWidths) {
  return Object.fromEntries(defaultCardOrder.filter((id) => storedWidths?.[id] === 'wide').map((id) => [id, 'wide']));
}

export function orderedCards(order) {
  return order.map((id) => cardsById.get(id));
}
