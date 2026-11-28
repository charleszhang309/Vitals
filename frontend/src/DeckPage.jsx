import { useEffect, useId, useRef, useState } from 'react';
import {
  ArrowLeft, Check, ChevronLeft, ChevronRight, Download, FolderOpen,
  Grip, Maximize, Minimize, Pencil, Plug, Plus, Upload, X,
} from 'lucide-react';
import { AnimatePresence } from 'framer-motion';
import { CUSTOM_ICON_RE, DECK_ICONS } from './deckIcons.js';
import { Button, IconButton, Modal } from './ui.jsx';
import { httpUrl, request, sendJson } from './api.js';
import useResource from './useOverviewResource.js';
import { useMedia } from './useMedia.js';
import { focusClock } from './useFocusTimer.js';
import { getLocale, t } from './i18n/messages.js';
import { freshTile, liveLabels, SceneEditor, TileEditor } from './DeckEditor.jsx';
import { deckActionTile, deckSlots, fitsDeckGrid, grids } from './deckLayout.js';
import { deckTarget, HUB_DEVICE_ID } from './controlDevice.js';
import DeckPlugins from './DeckPlugins.jsx';
import DeckTouchStrip, { useDeckFaders } from './DeckTouchStrip.jsx';
import ScreenshotControl from './ScreenshotControl.jsx';
import { desktopPlatform } from './DesktopChrome.jsx';
import './deck.css';

const root = '/api/deck';
const canceledAction = '目标或布局已更改，操作已取消。请重新选择按键。';

function selectedScene(layout, id) {
  return layout?.scenes.find((item) => item.id === id) ?? layout?.scenes.find((item) => !item.folder);
}

function pluginLabel(names, fallback) {
  return names?.[getLocale()] ?? names?.[getLocale().slice(0, 2)] ?? names?.['en-US'] ?? names?.en ?? t(fallback);
}

function labelFor(tile, layout, catalog) {
  if (tile.title) return tile.title;
  if (tile.page) return t(layout.scenes.find((scene) => scene.id === tile.page)?.title ?? '文件夹');
  if (tile.live) return t(liveLabels[tile.live]);
  if (tile.feedback && !tile.steps.length) {
    const { plugin_id, feedback_id, variable_id } = tile.feedback;
    const plugin = catalog?.plugins.find((item) => item.id === plugin_id);
    const spec = variable_id ? plugin?.variables.find((item) => item.id === variable_id)
      : plugin?.feedbacks.find((item) => item.id === feedback_id);
    return pluginLabel(spec?.name, '插件状态');
  }
  if (tile.steps.length > 1) return t('多动作');
  const action = tile.steps[0]?.action;
  if (action?.id === 'plugin') {
    const plugin = catalog?.plugins.find((item) => item.id === action.params.plugin_id);
    return pluginLabel(plugin?.actions.find((item) => item.id === action.params.action_id)?.name, '插件动作');
  }
  if (action?.id === 'preset') {
    const labels = catalog?.actions.find((item) => item.id === 'preset')?.parameters[0]?.options;
    return t(labels?.find((item) => item.value === action.params.preset)?.label ?? '桌面预设');
  }
  if (action?.id === 'media') return t(({ toggle: '播放 / 暂停', next: '下一首', previous: '上一首' })[action.params.action]);
  if (action?.id === 'volume') return t(action.params.step < 0 ? '音量 −' : '音量 +');
  if (action?.id === 'desktop') return t(action.params.action === 'lock' ? '锁屏' : '关闭显示器');
  return t(catalog?.actions.find((item) => item.id === action?.id)?.name ?? '动作');
}

const defaultIconMap = {
  '🔈': 'mute', '🔇': 'mute', '🔊': 'volume', '⏱': 'focus', '⏱️': 'focus',
  '⏹': 'screenshot', '📷': 'screenshot', '🌡': 'cpu', '🌡️': 'cpu',
  '✦': 'ai', '✨': 'ai', '▦': 'folder', '📁': 'folder',
};

function KeyIcon({ tile, state }) {
  if (!tile) return <span className="deck-key-empty-plus"><Plus size={26} strokeWidth={1.8} /></span>;
  if (CUSTOM_ICON_RE.test(tile.icon)) {
    return <img className="deck-key-cover" src={httpUrl(tile.icon).href} alt="" decoding="async" draggable={false} />;
  }
  if (tile.icon?.startsWith('lucide:')) {
    const name = tile.icon.slice(7);
    const Icon = Object.hasOwn(DECK_ICONS, name) ? DECK_ICONS[name] : DECK_ICONS.zap;
    return <span className="deck-key-glyph" aria-hidden="true"><Icon size={36} strokeWidth={1.9} /></span>;
  }
  const action = tile.steps?.[0]?.action;
  const actionId = action?.id;
  const mapped = tile.icon ? defaultIconMap[tile.icon.trim()] : null;

  if (tile.icon && !mapped) {
    return <span className="deck-key-glyph deck-key-emoji" aria-hidden="true">{tile.icon}</span>;
  }

  let key = 'zap';
  if (tile.kind === 'folder' || mapped === 'folder') key = 'folder-open';
  else if (tile.live === 'cpu' || mapped === 'cpu') key = 'thermometer';
  else if (tile.live === 'gpu') key = 'cpu';
  else if (tile.live === 'focus' || mapped === 'focus') key = 'timer';
  else if (tile.live === 'ai' || mapped === 'ai') key = 'sparkles';
  else if (actionId === 'mute' || mapped === 'mute') key = state.active ? 'volume-x' : 'volume-2';
  else if (actionId === 'volume' || mapped === 'volume') key = action?.params?.step < 0 ? 'volume-1' : 'volume-2';
  else if (actionId === 'media') {
    key = action?.params?.action === 'next' ? 'skip-forward'
      : action?.params?.action === 'previous' ? 'skip-back'
      : state.active ? 'pause' : 'play';
  }
  else if (actionId === 'screenshot' || mapped === 'screenshot') key = 'camera';
  else if (actionId === 'focus') key = 'timer';
  else if (actionId === 'desktop') key = action?.params?.action === 'lock' ? 'lock' : 'monitor-off';
  else if (actionId === 'hotkey' || actionId === 'preset' || actionId === 'text') key = 'keyboard';
  else if (actionId === 'webhook') key = 'globe';
  else if (actionId === 'power' || actionId === 'audio_device') key = 'sliders';
  else if (actionId === 'plugin') key = 'plug';
  else if (tile.steps?.length > 1) key = 'layers';

  const Icon = DECK_ICONS[key] ?? DECK_ICONS.zap;
  return <span className="deck-key-glyph" aria-hidden="true"><Icon size={36} strokeWidth={1.9} /></span>;
}

function tileState(tile, { sample, audio, media, focus, ai, feedback }) {
  if (!tile) return {};
  const params = tile.steps[0]?.action.params;
  const action = tile.steps[0]?.action.id;
  if (tile.feedback) {
    const scalar = typeof feedback === 'object' && feedback !== null ? feedback.value ?? feedback.active : feedback;
    const active = typeof feedback === 'object' && feedback !== null ? feedback.active ?? scalar === true : scalar === true;
    return { value: typeof scalar === 'boolean' ? t(scalar ? '开启' : '关闭') :
      typeof scalar === 'string' || typeof scalar === 'number' ? String(scalar) : '—', active };
  }
  if (tile.kind === 'live') {
    if (tile.live === 'focus') return { value: focusClock(focus.remaining), active: focus.running };
    if (tile.live === 'ai') return { value: ai == null ? '—' : `${ai}%` };
    const temperature = tile.live === 'cpu' ? sample?.cpu?.temperature_c : sample?.gpus?.[0]?.temperature_c;
    return { value: temperature == null ? '—' : `${Math.round(temperature)}°` };
  }
  if (tile.kind !== 'toggle') return {};
  if (action === 'mute') return { active: audio?.[params.direction === 'input' ? 'input_muted' : 'muted'] };
  if (action === 'media') return { active: media?.status === 'playing' };
  if (action === 'focus') return { active: focus.running, value: focus.running ? focusClock(focus.remaining) : null };
  return {};
}

export default function DeckPage({ close, hub, devices, defaultDevice, sample, focus, refreshDevices }) {
  const [selection, setSelection] = useState(defaultDevice);
  const target = deckTarget(selection, hub, devices);
  const canRun = !target.statusUnknown;
  const [saved, setSaved] = useState(null);
  const [announcedRevision, setAnnouncedRevision] = useState(-1);
  const [draft, setDraft] = useState(null);
  const [entry, setEntry] = useState(null);
  const [sceneId, setScene] = useState(null);
  const [trail, setTrail] = useState([]);
  const [moveFrom, setMoveFrom] = useState(null);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState(null);
  const [pluginsOpen, setPluginsOpen] = useState(false);
  const [managementKey, setManagementKey] = useState('');
  const [message, setMessage] = useState(null);
  const [syncConflict, setSyncConflict] = useState(false);
  const [fullscreen, setFullscreen] = useState(false);
  const [toolbarHidden, setToolbarHidden] = useState(false);
  const [feedback, setFeedback] = useState({});
  const fileInput = useRef(null);
  const region = useRef(null);
  const screenshot = useRef(null);
  const drag = useRef(null);
  const swipe = useRef(null);
  const skipSwipeClick = useRef(0);
  const skipDragClick = useRef(false);
  const sending = useRef(null);
  const alive = useRef(true);
  const revision = useRef(-1);
  const draftRef = useRef(null);
  const confirmationId = useId();
  const layout = draft ?? saved?.layout;
  const scene = selectedScene(layout, sceneId);
  const list = layout?.scenes.filter((item) => draft || !item.folder) ?? [];
  const [columns, rows] = grids[scene?.grid] ?? [3, 2];
  const { data: catalog, retry: refreshCatalog } = useResource(`${root}/catalog`, 60_000);
  const { data: hostAudio, setData: setHostAudio, retry: refreshAudio } = useResource(`${root}/audio?target=hub`, 2500, target.device_id === HUB_DEVICE_ID);
  const audioTarget = target.device_id;
  const currentTarget = useRef(audioTarget);
  currentTarget.current = canRun ? audioTarget : null;
  const localTarget = audioTarget === HUB_DEVICE_ID;
  const media = useMedia(localTarget);
  const targetTrack = localTarget ? media.current : target.snapshot?.media && { ...target.snapshot.media, agent_id: audioTarget };
  const reportError = (text) => setMessage({ text, error: true });
  const faders = useDeckFaders(canRun ? audioTarget : null, localTarget ? hostAudio : target.snapshot?.volume, setHostAudio, refreshDevices, reportError);
  const audio = faders.values;
  const { data: aiData } = useResource('/api/ai/usage', 60_000, Boolean(scene?.tiles.some((tile) => tile?.live === 'ai')));
  const quota = aiData?.sources?.find((item) => item.id === 'codex' || item.source === 'codex')?.quota;
  const ai = quota?.five_hour?.remaining_percent ?? quota?.seven_day?.remaining_percent;
  const activeSceneId = scene?.id;
  const feedbackKey = scene?.tiles.flatMap((tile, index) => tile?.feedback ? [index] : []).join(',');
  const savedRevision = saved?.revision;
  const editing = Boolean(draft);
  const confirmedTile = canRun && !editing && deckActionTile(confirm?.action, audioTarget, Math.max(savedRevision ?? -1, announcedRevision), scene);

  useEffect(() => {
    if (confirm && !confirmedTile) {
      setConfirm(null);
      setMessage({ text: t(canceledAction), error: true });
    }
  }, [confirm, confirmedTile]);

  function install(document) {
    if (document.revision < revision.current) return;
    revision.current = document.revision;
    setSaved(document);
  }

  useEffect(() => {
    alive.current = true;
    region.current?.focus({ preventScroll: true });
    const connection = new AbortController();
    async function load(next) {
      if (next <= revision.current) return;
      if (draftRef.current) {
        setSyncConflict(true);
        return;
      }
      try {
        const value = await request(root, { signal: connection.signal });
        if (connection.signal.aborted || value.revision < revision.current) return;
        if (draftRef.current) setSyncConflict(value.revision > revision.current);
        else { install(value); setMessage((current) => current?.loadError ? null : current); }
      } catch (error) {
        if (!connection.signal.aborted) setMessage({ text: error.message, error: true, loadError: true });
      }
    }
    load(Infinity);
    const stream = new EventSource(httpUrl(`${root}/events`));
    stream.onmessage = (event) => {
      const next = Number(event.data);
      if (!Number.isSafeInteger(next)) return;
      setAnnouncedRevision((previous) => Math.max(previous, next));
      load(next);
    };
    const refresh = () => { if (!document.hidden && !draftRef.current) load(Infinity); };
    document.addEventListener('visibilitychange', refresh);
    return () => {
      alive.current = false;
      sending.current?.abort();
      connection.abort();
      stream.close();
      document.removeEventListener('visibilitychange', refresh);
    };
  }, []);

  useEffect(() => {
    setFeedback({});
    if (!activeSceneId || !feedbackKey || savedRevision == null || editing) return undefined;
    const controller = new AbortController();
    let pending = false;
    async function poll() {
      if (controller.signal.aborted || pending || document.hidden) return;
      pending = true;
      try {
        const result = await request(`${root}/feedback`, { method: 'POST', signal: controller.signal, timeoutMs: 4500,
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ revision: savedRevision, scene: activeSceneId, slots: feedbackKey.split(',').map(Number) }) });
        if (!controller.signal.aborted) setFeedback(result.values);
      } catch {
        if (!controller.signal.aborted) setFeedback({});
      } finally {
        pending = false;
      }
    }
    poll();
    const timer = setInterval(poll, 2500);
    return () => { controller.abort(); clearInterval(timer); };
  }, [activeSceneId, feedbackKey, savedRevision, editing]);

  useEffect(() => {
    const update = () => setFullscreen(document.fullscreenElement === region.current);
    document.addEventListener('fullscreenchange', update);
    return () => document.removeEventListener('fullscreenchange', update);
  }, []);

  useEffect(() => {
    setToolbarHidden(false);
    if (!fullscreen || editing || pluginsOpen || confirm !== null) return undefined;
    let timer;
    const events = ['pointermove', 'pointerdown', 'keydown', 'focusin'];
    function reveal() {
      setToolbarHidden(false);
      clearTimeout(timer);
      timer = setTimeout(() => setToolbarHidden(true), 3000);
    }
    reveal();
    for (const event of events) document.addEventListener(event, reveal);
    return () => {
      clearTimeout(timer);
      for (const event of events) document.removeEventListener(event, reveal);
    };
  }, [fullscreen, editing, pluginsOpen, confirm]);

  useEffect(() => {
    if (!message || message.error || message.link) return undefined;
    const timer = setTimeout(() => setMessage(null), 2200);
    return () => clearTimeout(timer);
  }, [message]);

  function leave() {
    if (document.fullscreenElement === region.current) document.exitFullscreen?.().catch(() => {});
    close();
  }
  async function toggleFullscreen() {
    if (fullscreen && document.fullscreenElement === region.current) {
      await document.exitFullscreen().catch(() => {});
    } else if (!fullscreen && region.current?.requestFullscreen) {
      try { await region.current.requestFullscreen(); }
      catch { setFullscreen(true); }
    } else {
      setFullscreen(!fullscreen);
    }
  }
  function back() {
    if (!trail.length) return;
    setScene(trail.at(-1));
    setTrail(trail.slice(0, -1));
    setEntry(null);
  }
  function changePage(id) {
    setScene(id);
    setTrail([]);
    setEntry(null);
    setMoveFrom(null);
  }
  function handleKeys(event) {
    if (pluginsOpen || event.defaultPrevented || event.target.closest('input, select, textarea, dialog, [role="dialog"]')) return;
    if (event.key === 'Escape' && confirm === null) {
      if (editing && entry !== null) setEntry(null);
      else if (editing) return;
      else if (fullscreen) toggleFullscreen();
      else if (trail.length) back();
      else leave();
    }
    if (!draft && !trail.length && ['ArrowLeft', 'ArrowRight'].includes(event.key) && list.length > 1) {
      event.preventDefault();
      const index = Math.max(0, list.findIndex((item) => item.id === scene?.id));
      changePage(list[(index + list.length + (event.key === 'ArrowLeft' ? -1 : 1)) % list.length].id);
    }
  }

  function setEditor(value) {
    draftRef.current = value;
    setDraft(value);
  }
  function updateScene(value) {
    setEditor({ ...draft, scenes: draft.scenes.map((item) => item.id === value.id ? value : item) });
  }
  function changeTile(index, tile, current = scene) {
    const tiles = Array.from({ length: Math.max(current.tiles.length, index + 1) }, (_, n) => current.tiles[n] ?? null);
    tiles[index] = tile;
    return { ...current, tiles };
  }
  function editTile(sceneId, index, next) {
    const current = draftRef.current;
    const edited = current?.scenes.find((item) => item.id === sceneId);
    if (!edited?.tiles[index]) return;
    const tile = typeof next === 'function' ? next(edited.tiles[index]) : next;
    const updated = changeTile(index, tile, edited);
    if (!fitsDeckGrid(updated)) return;
    setEditor({ ...current, scenes: current.scenes.map((item) => item.id === sceneId ? updated : item) });
  }
  function moveTile(from, to) {
    if (from == null || to == null || from === to) return;
    const moved = changeTile(to, scene.tiles[from] ?? null, changeTile(from, scene.tiles[to] ?? null));
    if (!fitsDeckGrid(moved)) {
      reportError(t('宽键需要同一行右侧有空位'));
      return;
    }
    updateScene(moved);
    setEntry(to);
  }
  function newScene(folder = false) {
    const id = Array.from(crypto.getRandomValues(new Uint32Array(3)), (item) => item.toString(36)).join('-');
    return { id, title: folder ? t('文件夹') : t('新场景'), folder, grid: '3x2', tiles: [] };
  }
  function addScene() {
    if (draft.scenes.length >= 16) return;
    const page = newScene();
    setEditor({ ...draft, scenes: [...draft.scenes, page] });
    changePage(page.id);
  }
  function addFolder(makeTile) {
    if (draft.scenes.length >= 16) return;
    const page = newScene(true);
    setEditor({ ...draft, scenes: [...draft.scenes.map((item) =>
      item.id === scene.id ? changeTile(entry, makeTile(page.id)) : item), page] });
  }
  function removeScene() {
    const scenes = draft.scenes.filter((item) => item.id !== scene.id).map((item) => ({
      ...item, tiles: item.tiles.map((tile) => tile?.page === scene.id ? null : tile),
    }));
    setEditor({ ...draft, scenes });
    changePage(scenes.find((item) => !item.folder).id);
  }

  async function loadLatest() {
    const value = await request(root);
    install(value);
    setSyncConflict(false);
  }
  async function cancelEdit() {
    setEditor(null);
    setEntry(null);
    setMoveFrom(null);
    try { await loadLatest(); } catch (error) { setMessage({ text: error.message, error: true }); }
  }
  async function save() {
    if (busy || syncConflict) return;
    setBusy(true);
    setMessage(null);
    try {
      const value = await sendJson('PUT', root, { revision: saved.revision, layout: draft });
      install(value);
      setSyncConflict(false);
      setEditor(null);
      setEntry(null);
      setMoveFrom(null);
    } catch (error) {
      setMessage({ text: error.message, error: true });
    } finally { setBusy(false); }
  }
  function exportLayout() {
    const url = URL.createObjectURL(new Blob([JSON.stringify(layout, null, 2)], { type: 'application/json' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = 'vitals-deck.json';
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  async function importLayout(file) {
    if (!file) return;
    try {
      if (file.size > 256 * 1024) throw new Error(t('Deck 布局超过大小限制'));
      const layout = JSON.parse(await file.text());
      setEditor(await sendJson('POST', `${root}/validate`, layout));
      changePage(null);
      setMessage(null);
    } catch {
      setMessage({ text: t('无法导入此 Deck 布局。'), error: true });
    } finally { fileInput.current.value = ''; }
  }

  function pressTile(index, tile) {
    if (draft) {
      if (moveFrom !== null) {
        moveTile(moveFrom, index);
        setMoveFrom(null);
      } else {
        if (!tile) updateScene(changeTile(index, freshTile()));
        setEntry(index);
      }
      return;
    }
    if (!tile || busy || !canRun) return;
    if (tile.kind === 'folder') {
      setTrail([...trail.slice(-15), scene.id]);
      setScene(tile.page);
      return;
    }
    const action = { target: audioTarget, revision: savedRevision, scene: scene.id, slot: index };
    if (tile.steps.some((step) => step.action.id === 'desktop' && step.action.params.action === 'lock')) {
      setConfirm({ action, name: target.name });
      return;
    }
    if (tile.steps.length) run(action);
  }

  async function run(action) {
    const tile = deckActionTile(action, currentTarget.current, Math.max(revision.current, announcedRevision), scene);
    if (!tile || draftRef.current) {
      setMessage({ text: t(canceledAction), error: true });
      return;
    }
    if (sending.current || busy) return;
    const controller = new AbortController();
    sending.current = controller;
    setBusy(true);
    setMessage(null);
    try {
      const isLink = (step) => step.action.id === 'launcher' && catalog?.launchers.some((item) => item.id === step.action.params.id && item.kind === 'url');
      const containsEffects = tile.steps.some((step) => ['focus', 'screenshot'].includes(step.action.id) || isLink(step));
      const directLink = tile.steps.length === 1 && isLink(tile.steps[0]) && !tile.steps[0].delay_ms;
      if (directLink) {
        const url = httpUrl(`/api/control/launchers/${encodeURIComponent(tile.steps[0].action.params.id)}/open`).href;
        window.open(url, '_blank', 'noopener,noreferrer');
        return;
      }
      async function call(step) {
        if (currentTarget.current !== action.target) throw new Error(t(canceledAction));
        const result = await request(`${root}/run`, { method: 'POST', timeoutMs: 75_000, signal: controller.signal,
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ...action, step }) });
        for (const effect of result.results) {
          if (effect.effect === 'focus') focus.control(effect.action, effect.minutes);
          if (effect.effect === 'screenshot' && (currentTarget.current !== audioTarget || !(await screenshot.current?.open()))) {
            if (alive.current) setMessage({ text: t('未收到有效截图，请重试。'), error: true });
            return false;
          }
          if (effect.effect === 'link') setMessage({ text: t('打开启动链接'), link: effect.id });
        }
        return true;
      }
      if (containsEffects && tile.steps.length > 1) {
        for (const step of tile.steps.keys()) {
          if (!(await call(step))) break;
        }
      } else {
        await call(undefined);
      }
      if (alive.current) {
        if (localTarget) {
          refreshAudio();
          media.reload();
        }
        refreshDevices();
        setMessage((current) => current ?? { text: t('动作已执行') });
      }
    } catch (error) {
      if (!controller.signal.aborted) setMessage({ text: error.message, error: true });
    } finally {
      sending.current = null;
      if (alive.current) setBusy(false);
    }
  }

  const currentIndex = list.findIndex((item) => item.id === scene?.id);
  const adjacent = (step) => {
    if (list.length > 1) changePage(list[(Math.max(0, currentIndex) + list.length + step) % list.length].id);
  };

  function startSwipe(event) {
    if (!editing && event.pointerType === 'touch' && event.isPrimary) {
      skipSwipeClick.current = 0;
      swipe.current = { id: event.pointerId, x: event.clientX, y: event.clientY };
    }
  }

  function endSwipe(event) {
    const start = swipe.current;
    swipe.current = null;
    if (!start || start.id !== event.pointerId || list.length < 2) return;
    const dx = event.clientX - start.x;
    if (Math.abs(dx) <= 60 || Math.abs(dx) <= Math.abs(event.clientY - start.y)) return;
    skipSwipeClick.current = performance.now() + 420;
    adjacent(dx < 0 ? 1 : -1);
  }

  return <section className="deck-view" ref={region} tabIndex={-1} autoFocus
    aria-label="Vitals Deck" onKeyDown={handleKeys} data-desktop={desktopPlatform} data-immersive={fullscreen}>
    <div className="deck-body" style={{ '--deck-cols': columns, '--deck-rows': rows }}>
      <header className="deck-toolbar surface" data-hidden={fullscreen && toolbarHidden}>
        <div className="deck-brand">
          <span className="deck-brand-mark" aria-hidden="true"><Grip size={18} /></span>
          <h1>Vitals Deck</h1>
        </div>
        {scene && <div className="deck-scenes">
          {trail.length > 0 && <IconButton label={t('返回上一级')} onClick={back}><ArrowLeft size={16} /></IconButton>}
          {!trail.length && list.length > 1 && <IconButton label={t('上一场景')} onClick={() => adjacent(-1)}><ChevronLeft size={16} /></IconButton>}
          <div className="deck-scene-list" aria-label={t('场景')}>
            {trail.length ? <strong className="deck-folder-crumb">{t(scene.title)}</strong> : list.map((item) =>
              <button type="button" key={item.id} className="deck-scene-pill" aria-pressed={item.id === scene.id} onClick={() => changePage(item.id)}>
                {item.folder && <FolderOpen size={13} />}{t(item.title)}
              </button>)}
          </div>
          {!trail.length && list.length > 1 && <IconButton label={t('下一场景')} onClick={() => adjacent(1)}><ChevronRight size={16} /></IconButton>}
        </div>}
        <div className="deck-toolbar-right">
          <label className="deck-target">
            <span className="deck-target-dot" aria-hidden="true" />
            <span className="deck-target-label">{t('目标主机')}</span>
            <select aria-label={t('目标主机')} value={audioTarget}
              disabled={busy} onChange={(event) => setSelection(event.target.value)}>
              {[hub, ...devices.filter((device) => device.online), ...(target.statusUnknown ? [target] : [])].map((device) =>
                <option key={device.device_id} value={device.device_id}>{device.name}</option>)}
            </select>
          </label>
          <div className="deck-toolbar-actions">
            <IconButton label={t('Deck 插件')} aria-haspopup="dialog" onClick={() => setPluginsOpen(true)}>
              <Plug size={17} aria-hidden="true" />
            </IconButton>
            {draft && <>
              <IconButton label={t('导入布局')} onClick={() => fileInput.current?.click()}><Upload size={16} /></IconButton>
              <IconButton label={t('导出布局')} onClick={exportLayout}><Download size={16} /></IconButton>
              <input ref={fileInput} type="file" accept=".json,application/json" hidden onChange={(event) => importLayout(event.target.files[0])} />
            </>}
            {saved && (draft ? <>
              <Button className="primary-button" disabled={busy || syncConflict} onClick={save}><Check size={16} />{t('保存')}</Button>
              <Button disabled={busy} onClick={cancelEdit}>{t('取消')}</Button>
            </> : <IconButton label={t('编辑 Deck')} disabled={busy} onClick={() => {
              setMessage(null); setEditor(structuredClone(saved.layout));
            }}><Pencil size={17} /></IconButton>)}
            <IconButton label={t(fullscreen ? '退出全屏' : '全屏')} aria-pressed={fullscreen} onClick={toggleFullscreen}>
              {fullscreen ? <Minimize size={17} /> : <Maximize size={17} />}
            </IconButton>
            <IconButton label={t('退出 Deck')} onClick={leave}><X size={19} /></IconButton>
          </div>
        </div>
      </header>
      {target.statusUnknown && <p className="feedback" role="status">{t('暂时无法确认 {p1} 的状态，设备操作已暂停。', { p1: target.name })}</p>}
      {scene ? <>
        {draft && <SceneEditor scene={scene} set={updateScene} add={addScene} remove={removeScene}
          canAdd={draft.scenes.length < 16} canRemove={scene.folder || draft.scenes.filter((item) => !item.folder).length > 1}
          hasFolder={draft.scenes.some((item) => item.tiles.some((tile) => tile?.page === scene.id))} />}
        {syncConflict && draft && <p className="feedback" role="status">{t('Deck 已在另一台设备修改，请重新载入后再保存')}
          <Button onClick={cancelEdit}>{t('重新载入')}</Button></p>}
        <div className="deck-layout" data-editing={editing} data-has-editor={editing && entry !== null}>
          <div className="deck-stage">
            <div className="surface deck-board">
              <div className="deck-grid" onPointerDown={startSwipe} onPointerUp={endSwipe}
                onPointerCancel={() => { swipe.current = null; }} onClickCapture={(event) => {
                  if (event.detail && performance.now() < skipSwipeClick.current) {
                    event.preventDefault();
                    event.stopPropagation();
                  }
                }}>
                {deckSlots(scene).map((index) => {
                  const tile = scene.tiles[index];
                  const state = tileState(tile, { sample: localTarget ? sample : target.snapshot, audio,
                    media: targetTrack, focus, ai, feedback: feedback[index] });
                  const label = tile ? labelFor(tile, layout, catalog) : t('添加按键');
                  const isButton = editing || tile?.kind === 'folder' || Boolean(tile?.steps.length);
                  const classes = 'deck-key';
                  const hasLed = tile && (tile.kind === 'toggle' || Boolean(tile.feedback));
                  const content = <>
                    {hasLed && <span className="deck-key-led" data-active={Boolean(state.active)} aria-hidden="true" />}
                    {state.value ? <>
                      <div className="deck-key-metric-top"><KeyIcon tile={tile} state={state} /></div>
                      <div className="deck-key-reading"><strong className="deck-key-value numeric">{state.value}</strong>
                        <span className="deck-key-name">{label}</span></div>
                    </> : <>
                      <KeyIcon tile={tile} state={state} />
                      {(tile || editing) && <span className="deck-key-name">{label}</span>}
                    </>}
                  </>;
                  const hasCover = CUSTOM_ICON_RE.test(tile?.icon || '') || undefined;
                  return <div key={index} data-deck-slot={index} className="deck-slot" data-span={tile?.span ?? 1}
                    data-selected={editing && entry === index}>
                    {isButton ? <button type="button" className={classes} title={label}
                      data-deck-color={tile?.color || 'slate'} data-kind={tile?.kind} data-active={Boolean(state.active)} data-selected={editing && entry === index}
                      data-empty={!tile} data-has-cover={hasCover} disabled={!editing && (busy || !canRun)} aria-pressed={tile?.kind === 'toggle' ? Boolean(state.active) : undefined}
                      onClick={() => pressTile(index, tile)}>{content}</button>
                      : <div className={classes} data-empty={!tile} data-has-cover={hasCover} data-kind={tile?.kind} data-deck-color={tile?.color || 'slate'} data-active={Boolean(state.active)}>{content}</div>}
                    {editing && tile && <button className="deck-grip" type="button" aria-label={t('移动按键')} aria-pressed={moveFrom === index}
                      onClick={() => {
                        if (!skipDragClick.current) setMoveFrom(moveFrom === index ? null : index);
                        skipDragClick.current = false;
                      }}
                      onPointerDown={(event) => {
                        skipDragClick.current = false;
                        drag.current = { index, x: event.clientX, y: event.clientY };
                        event.currentTarget.setPointerCapture(event.pointerId);
                      }}
                      onPointerCancel={() => { drag.current = null; }}
                      onPointerUp={(event) => {
                        const start = drag.current;
                        drag.current = null;
                        if (!start || Math.hypot(start.x - event.clientX, start.y - event.clientY) < 8) return;
                        skipDragClick.current = true;
                        const end = document.elementFromPoint(event.clientX, event.clientY)?.closest('[data-deck-slot]');
                        if (end) moveTile(start.index, Number(end.dataset.deckSlot));
                      }}><Grip size={15} /></button>}
                  </div>;
                })}
              </div>
            </div>
          </div>
          {editing && entry !== null && scene.tiles[entry] && <>
            <div className="deck-editor-scrim" aria-hidden="true" />
            <TileEditor key={`${scene.id}:${entry}`} tile={scene.tiles[entry]} scene={scene} scenes={draft.scenes}
              set={(tile) => editTile(scene.id, entry, tile)} ensureFolder={addFolder} catalog={catalog} audio={audio}
              canExpand={entry % columns < columns - 1 && !scene.tiles[entry + 1]}
              onClose={() => setEntry(null)} onRemove={() => { updateScene(changeTile(entry, null)); setEntry(null); }} />
          </>}
        </div>
      </> : !message && <p className="deck-loading" role="status">{t('正在加载 Deck…')}</p>}
      <DeckTouchStrip key={audioTarget} deviceId={audioTarget} track={targetTrack} faders={faders} reportError={reportError} disabled={!canRun}
        onMediaChange={localTarget ? media.reload : refreshDevices} />
      {message && <div className={`deck-toast ${message.error ? 'is-error' : ''}`} role={message.error ? 'alert' : 'status'}>
        {!message.error && <Check size={14} aria-hidden="true" />}
        {message.link ? <a href={httpUrl(`/api/control/launchers/${encodeURIComponent(message.link)}/open`).href}
          target="_blank" rel="noreferrer">{message.text}</a> : <span>{message.text}</span>}
      </div>}
      <div className="deck-screenshot">
        <ScreenshotControl key={audioTarget} deviceId={localTarget ? null : audioTarget} deviceName={target.name} controlRef={screenshot} disabled={!canRun} />
      </div>
      <AnimatePresence>
        {pluginsOpen && <DeckPlugins key="plugins" close={() => setPluginsOpen(false)} initialKey={managementKey}
          rememberKey={setManagementKey} onChange={refreshCatalog} />}
        {confirmedTile && <Modal key="lock" id={confirmationId} title={t('确认锁屏')} onClose={() => setConfirm(null)}>
          <p>{t('要锁定 {p1} 吗？', { p1: confirm.name })}</p>
          <div className="deck-editor-actions">
            <Button onClick={() => setConfirm(null)}>{t('取消')}</Button>
            <Button onClick={() => { setConfirm(null); run(confirm.action); }}>{t('确认锁屏')}</Button>
          </div>
        </Modal>}
      </AnimatePresence>
    </div>
  </section>;
}
