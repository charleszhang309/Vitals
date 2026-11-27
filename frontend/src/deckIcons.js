import { createElement } from 'react';
import {
  Bell, Bookmark, Camera, Circle, Clapperboard, Code2, Cpu, Film, Flame, FolderOpen,
  Globe, Headphones, Home, Keyboard, Layers, Lock, Mic, MicOff, Monitor,
  MonitorOff, Moon, Music, Pause, Play, Plug, Power, Radio, Rocket, Scissors,
  SkipBack, SkipForward, Sliders, Sparkles, Sun, Terminal, Thermometer, Timer,
  Tv, Video, Volume1, Volume2, VolumeX, Wifi, Zap,
} from 'lucide-react';

function customIcon(nodes) {
  return function CustomDeckIcon({ size = 24, strokeWidth = 1.8, ...props }) {
    return createElement(
      'svg',
      {
        xmlns: 'http://www.w3.org/2000/svg',
        width: size,
        height: size,
        viewBox: '0 0 24 24',
        fill: 'none',
        stroke: 'currentColor',
        strokeWidth,
        strokeLinecap: 'round',
        strokeLinejoin: 'round',
        'aria-hidden': 'true',
        ...props,
      },
      ...nodes.map(([tag, attrs], index) => createElement(tag, { key: index, ...attrs })),
    );
  };
}

const ObsIcon = customIcon([
  ['circle', { cx: 12, cy: 12, r: 9 }],
  ['circle', { cx: 12, cy: 12, r: 5, fill: 'currentColor', opacity: 0.24 }],
  ['path', { d: 'M12 7v10M7 12h10' }],
]);

const DavinciIcon = customIcon([
  ['circle', { cx: 12, cy: 12, r: 8, opacity: 0.22, fill: 'currentColor' }],
  ['path', { d: 'M12 4v16M4 12h16' }],
  ['circle', { cx: 12, cy: 12, r: 3 }],
]);

const RecIcon = customIcon([
  ['circle', { cx: 12, cy: 12, r: 8, fill: 'currentColor', opacity: 0.85 }],
  ['circle', { cx: 12, cy: 12, r: 8 }],
]);

const ScreenPeekIcon = customIcon([
  ['rect', { x: 3, y: 4, width: 18, height: 12, rx: 2 }],
  ['path', { d: 'M7 20h10' }],
  ['circle', { cx: 12, cy: 10, r: 2, fill: 'currentColor', opacity: 0.3 }],
]);

const StreamIcon = customIcon([
  ['path', { d: 'M5 12.55a11 11 0 0 1 14.08 0' }],
  ['path', { d: 'M1.42 9a16 16 0 0 1 21.16 0' }],
  ['path', { d: 'M8.53 16.11a6 6 0 0 1 6.95 0' }],
  ['circle', { cx: 12, cy: 20, r: 1, fill: 'currentColor' }],
]);

const SceneIcon = customIcon([
  ['rect', { x: 2, y: 3, width: 20, height: 14, rx: 2 }],
  ['path', { d: 'M8 21h8M12 17v4' }],
  ['rect', { x: 6, y: 7, width: 5, height: 6, rx: 1, fill: 'currentColor', opacity: 0.2 }],
  ['rect', { x: 13, y: 7, width: 5, height: 6, rx: 1, fill: 'currentColor', opacity: 0.2 }],
]);

export const DECK_ICONS = {
  'volume-2': Volume2, 'volume-1': Volume1, 'volume-x': VolumeX,
  mic: Mic, 'mic-off': MicOff, headphones: Headphones,
  play: Play, pause: Pause, 'skip-back': SkipBack, 'skip-forward': SkipForward, music: Music,
  video: Video, radio: Radio, clapperboard: Clapperboard, film: Film, scissors: Scissors, camera: Camera, tv: Tv,
  monitor: Monitor, 'monitor-off': MonitorOff, lock: Lock, power: Power, moon: Moon, sun: Sun,
  keyboard: Keyboard, terminal: Terminal, 'code-2': Code2, rocket: Rocket, globe: Globe,
  layers: Layers, sliders: Sliders, timer: Timer, thermometer: Thermometer, cpu: Cpu,
  sparkles: Sparkles, 'folder-open': FolderOpen, plug: Plug, zap: Zap, flame: Flame,
  bell: Bell, bookmark: Bookmark, wifi: Wifi, home: Home, circle: Circle,
  obs: ObsIcon, davinci: DavinciIcon, rec: RecIcon,
  'screen-peek': ScreenPeekIcon, stream: StreamIcon, scene: SceneIcon,
};

export const DECK_ICON_KEYS = Object.keys(DECK_ICONS);
export const CUSTOM_ICON_RE = /^\/api\/deck\/icons\/[a-f0-9]{64}\.(?:png|jpg|webp|svg)$/;
