import { useEffect, useId, useRef, useState } from 'react';
import { ArrowLeft, Check, ChevronLeft, ChevronRight, Download, FolderOpen, Grip, Maximize, Pencil, Plus, Upload, X } from 'lucide-react';
import { AnimatePresence } from 'framer-motion';
import { Button, IconButton, Modal } from './ui.jsx';
import { httpUrl, request, sendJson } from './api.js';
import useResource from './useOverviewResource.js';
import { focusClock } from './useFocusTimer.js';
import { getLocale, t } from './i18n/messages.js';
import { freshTile, grids, liveLabels, SceneEditor, TileEditor } from './DeckEditor.jsx';
import ScreenshotControl from './ScreenshotControl.jsx';
import { desktopPlatform } from './DesktopChrome.jsx';
import './deck.css';

const root = '/api/deck';
const HUB = 'hub';

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

export default function DeckPage({ close, hub, devices, defaultDevice, sample, media, focus, refreshMedia, refreshDevices }) {
  const [selection, setSelection] = useState(defaultDevice);
  const target = devices.find((item) => item.online && item.device_id === selection) ?? hub;
  const [saved, setSaved] = useState(null);
  const [draft, setDraft] = useState(null);
  const [entry, setEntry] = useState(null);
  const [sceneId, setScene] = useState(null);
  const [trail, setTrail] = useState([]);
  const [moveFrom, setMoveFrom] = useState(null);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState(null);
  const [message, setMessage] = useState(null);
  const [syncConflict, setSyncConflict] = useState(false);
  const [fullscreen, setFullscreen] = useState(false);
  const [feedback, setFeedback] = useState({});
  const fileInput = useRef(null);
  const region = useRef(null);
  const screenshot = useRef(null);
  const drag = useRef(null);
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
  const { data: catalog } = useResource(`${root}/catalog`, 60_000);
  const { data: hostAudio, retry: refreshAudio } = useResource(`${root}/audio?target=hub`, 2500, target.device_id === HUB);
  const audio = target.device_id === HUB ? hostAudio : target.snapshot?.volume;
  const audioTarget = target.device_id;
  const currentTarget = useRef(audioTarget);
  currentTarget.current = audioTarget;
  const localTarget = audioTarget === HUB;
  const { data: aiData } = useResource('/api/ai/usage', 60_000, Boolean(scene?.tiles.some((tile) => tile?.live === 'ai')));
  const quota = aiData?.sources?.find((item) => item.id === 'codex' || item.source === 'codex')?.quota;
  const ai = quota?.five_hour?.remaining_percent ?? quota?.seven_day?.remaining_percent;
  const activeSceneId = scene?.id;
  const feedbackKey = scene?.tiles.flatMap((tile, index) => tile?.feedback ? [index] : []).join(',');
  const savedRevision = saved?.revision;
  const editing = Boolean(draft);

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
        else { install(value); setMessage(null); }
      } catch (error) {
        if (!connection.signal.aborted) setMessage({ text: error.message, error: true });
      }
    }
    load(Infinity);
    const stream = new EventSource(httpUrl(`${root}/events`));
    stream.onmessage = (event) => load(Number(event.data));
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
    const update = () => setFullscreen(Boolean(document.fullscreenElement));
    document.addEventListener('fullscreenchange', update);
    return () => document.removeEventListener('fullscreenchange', update);
  }, []);

  function leave() {
    if (document.fullscreenElement === region.current) document.exitFullscreen?.().catch(() => {});
    close();
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
    if (event.defaultPrevented || event.target.closest('input, select, textarea, [role="dialog"]')) return;
    if (event.key === 'Escape' && !draft && !confirm) {
      if (trail.length) back();
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
  function moveTile(from, to) {
    if (from == null || to == null || from === to) return;
    updateScene(changeTile(to, scene.tiles[from] ?? null, changeTile(from, scene.tiles[to] ?? null)));
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
    if (!tile || busy) return;
    if (tile.kind === 'folder') {
      setTrail([...trail.slice(-15), scene.id]);
      setScene(tile.page);
      return;
    }
    if (tile.steps.some((step) => step.action.id === 'desktop' && step.action.params.action === 'lock')) {
      setConfirm(index);
      return;
    }
    if (tile.steps.length) run(index);
  }

  async function run(index) {
    const tile = scene.tiles[index];
    const controller = new AbortController();
    if (sending.current) return;
    sending.current = controller;
    setBusy(true);
    setMessage(null);
    const isLink = (step) => step.action.id === 'launcher' && catalog?.launchers.some((item) => item.id === step.action.params.id && item.kind === 'url');
    const containsEffects = tile.steps.some((step) => ['focus', 'screenshot'].includes(step.action.id) || isLink(step));
    const directLink = tile.steps.length === 1 && isLink(tile.steps[0]) && !tile.steps[0].delay_ms;
    try {
      if (directLink) {
        const url = httpUrl(`/api/control/launchers/${encodeURIComponent(tile.steps[0].action.params.id)}/open`).href;
        window.open(url, '_blank', 'noopener,noreferrer');
        return;
      }
      async function call(step) {
        const result = await request(`${root}/run`, { method: 'POST', timeoutMs: 75_000, signal: controller.signal,
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ revision: saved.revision, scene: scene.id, slot: index, target: audioTarget, step }) });
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
        refreshAudio();
        refreshMedia();
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
    if (list.length > 1) changePage(list[(currentIndex + list.length + step) % list.length].id);
  };

  return <section className="deck-view" ref={region} tabIndex={-1} autoFocus
    aria-label="Vitals Deck" onKeyDown={handleKeys} data-desktop={desktopPlatform}>
    <div className="deck-body">
      <div className="deck-toolbar">
        <div className="deck-brand"><Grip size={21} aria-hidden="true" /><h1>Vitals Deck</h1></div>
        <label className="deck-target"><span>{t('目标主机')}</span><select aria-label={t('目标主机')} value={audioTarget}
          disabled={busy} onChange={(event) => setSelection(event.target.value)}>
          {[hub, ...devices.filter((device) => device.online)].map((device) =>
            <option key={device.device_id} value={device.device_id}>{device.name}</option>)}
        </select></label>
        <div className="deck-toolbar-actions">
          {saved && (draft ? <>
            <Button disabled={busy || syncConflict} onClick={save}><Check size={16} />{t('保存')}</Button>
            <Button disabled={busy} onClick={cancelEdit}>{t('取消')}</Button>
          </> : <IconButton label={t('编辑 Deck')} disabled={busy} onClick={() => {
            setMessage(null); setEditor(structuredClone(saved.layout));
          }}><Pencil size={18} /></IconButton>)}
          {region.current?.requestFullscreen && !fullscreen && <IconButton label={t('全屏')} onClick={() =>
            region.current?.requestFullscreen().catch(() => {})}><Maximize size={18} /></IconButton>}
          <IconButton label={t('退出 Deck')} onClick={leave}><X size={20} /></IconButton>
        </div>
      </div>
      {scene ? <>
        <div className="deck-scenes">
          {trail.length > 0 && <IconButton label={t('返回上一级')} onClick={back}><ArrowLeft size={18} /></IconButton>}
          {!trail.length && <IconButton label={t('上一场景')} disabled={list.length < 2} onClick={() => adjacent(-1)}><ChevronLeft size={18} /></IconButton>}
          <div className="deck-scene-list" aria-label={t('场景')}>
            {trail.length ? <strong>{t(scene.title)}</strong> : list.map((item) =>
              <Button key={item.id} aria-pressed={item.id === scene.id} onClick={() => changePage(item.id)}>
                {item.folder && <FolderOpen size={14} />}{t(item.title)}
              </Button>)}
          </div>
          {!trail.length && <IconButton label={t('下一场景')} disabled={list.length < 2} onClick={() => adjacent(1)}><ChevronRight size={18} /></IconButton>}
          {draft && <>
            <IconButton label={t('导入布局')} onClick={() => fileInput.current?.click()}><Upload size={16} /></IconButton>
            <IconButton label={t('导出布局')} onClick={exportLayout}><Download size={16} /></IconButton>
            <input ref={fileInput} type="file" accept=".json,application/json" hidden onChange={(event) => importLayout(event.target.files[0])} />
          </>}
        </div>
        {draft && <SceneEditor scene={scene} set={updateScene} add={addScene} remove={removeScene}
          canAdd={draft.scenes.length < 16} canRemove={scene.folder || draft.scenes.filter((item) => !item.folder).length > 1}
          hasFolder={draft.scenes.some((item) => item.tiles.some((tile) => tile?.page === scene.id))} />}
        {syncConflict && draft && <p className="feedback" role="status">{t('Deck 已在另一台设备修改，请重新载入后再保存')}
          <Button onClick={cancelEdit}>{t('重新载入')}</Button></p>}
        <div className="deck-layout" data-editing={editing} data-has-editor={editing && entry !== null}>
          <div className="deck-grid" style={{ '--deck-cols': columns }}>
            {Array.from({ length: columns * rows }, (_, index) => {
              const tile = scene.tiles[index];
              const state = tileState(tile, { sample: localTarget ? sample : target.snapshot, audio,
                media: localTarget ? media : target.snapshot?.media, focus, ai, feedback: feedback[index] });
              const label = tile ? labelFor(tile, layout, catalog) : t('添加按键');
              const isButton = editing || tile?.kind === 'folder' || Boolean(tile?.steps.length);
              const classes = 'deck-key surface';
              const content = <>
                {tile?.icon ? <span className="deck-key-icon" aria-hidden="true">{tile.icon}</span>
                  : tile?.kind === 'folder' ? <FolderOpen size={24} /> : !tile && editing ? <Plus size={22} /> : null}
                {state.value && <strong className="deck-key-value numeric">{state.value}</strong>}
                {(tile || editing) && <span className="deck-key-name">{label}</span>}
              </>;
              return <div key={index} data-deck-slot={index} className="deck-slot">
                {isButton ? <button type="button" className={classes} title={label}
                  data-deck-color={tile?.color} data-active={Boolean(state.active)} data-selected={editing && entry === index}
                  data-empty={!tile} disabled={busy && !editing} aria-pressed={tile?.kind === 'toggle' ? Boolean(state.active) : undefined}
                  onClick={() => pressTile(index, tile)}>{content}</button>
                  : <div className={classes} data-empty={!tile} data-deck-color={tile?.color} data-active={Boolean(state.active)}>{content}</div>}
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
                  }}><Grip size={17} /></button>}
              </div>;
            })}
          </div>
          {editing && entry !== null && scene.tiles[entry] && <TileEditor tile={scene.tiles[entry]} scene={scene} scenes={draft.scenes}
            set={(tile) => updateScene(changeTile(entry, tile))} ensureFolder={addFolder} catalog={catalog} audio={audio}
            onClose={() => setEntry(null)} onRemove={() => { updateScene(changeTile(entry, null)); setEntry(null); }} />}
        </div>
      </> : !message && <p role="status">{t('正在加载 Deck…')}</p>}
      {message && <p className={`feedback ${message.error ? 'error' : ''}`} role={message.error ? 'alert' : 'status'}>
        {message.link ? <a href={httpUrl(`/api/control/launchers/${encodeURIComponent(message.link)}/open`).href}
          target="_blank" rel="noreferrer">{message.text}</a> : message.text}
      </p>}
      <div className="deck-screenshot">
        <ScreenshotControl key={audioTarget} deviceId={localTarget ? null : audioTarget} deviceName={target.name} controlRef={screenshot} />
      </div>
      <AnimatePresence>
        {confirm !== null && <Modal id={confirmationId} title={t('确认锁屏')} onClose={() => setConfirm(null)}>
          <p>{t('要锁定 {p1} 吗？', { p1: target.name })}</p>
          <div className="deck-editor-actions">
            <Button onClick={() => setConfirm(null)}>{t('取消')}</Button>
            <Button onClick={() => { const index = confirm; setConfirm(null); run(index); }}>{t('确认锁屏')}</Button>
          </div>
        </Modal>}
      </AnimatePresence>
    </div>
  </section>;
}
