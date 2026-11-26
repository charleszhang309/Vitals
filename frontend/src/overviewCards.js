import { CalendarDays, CircleDollarSign, CloudSun, HeartPulse, ListTodo, CalendarClock } from 'lucide-react';

export const overviewStrips = [
  { id: 'host', title: '主机状态条' },
  { id: 'media', title: '正在播放条' },
  { id: 'briefing', title: '明日香每日简报' },
];

export const overviewCards = [
  { id: 'weather', title: '天气', Icon: CloudSun, tone: 'blue' },
  { id: 'health', title: '健康', Icon: HeartPulse, tone: 'red' },
  { id: 'calendar', title: '日历', Icon: CalendarDays, tone: 'red' },
  { id: 'tasks', title: '待办', Icon: ListTodo, tone: 'green' },
  { id: 'countdowns', title: '倒数日', Icon: CalendarClock, tone: 'purple' },
  { id: 'rates', title: '汇率', Icon: CircleDollarSign, tone: 'blue' },
];

export const overviewModules = [...overviewStrips, ...overviewCards];
