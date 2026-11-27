import { useEffect, useRef, useState } from 'react';
import { ArrowDown, ArrowUp, ImagePlus, Plus, Sparkles, Trash2, X } from 'lucide-react';
import { Button, IconButton, Segmented } from './ui.jsx';
import { httpUrl, request } from './api.js';
import { getLocale, t } from './i18n/messages.js';
import { CUSTOM_ICON_RE, DECK_ICON_KEYS, DECK_ICONS } from './deckIcons.js';
import { fitsDeckGrid, grids } from './deckLayout.js';

export const palettes = ['slate', 'blue', 'teal', 'green', 'amber', 'pink', 'purple', 'red'];
export const liveLabels = { cpu: 'CPU 温度', gpu: 'GPU 温度', focus: '专注计时', ai: 'AI 余量' };
const typeLabels = { action: '触发键', toggle: '开关键', live: '实时信息键', folder: '文件夹 / 切页' };
const plainAction = () => ({ id: 'mute', params: { direction: 'output' } });
export const freshTile = () => ({ kind: 'action', title: '', icon: '', color: 'slate', span: 1, steps: [{ action: plainAction(), delay_ms: 0 }] });

function name(label) {
  return typeof label === 'object' && label
    ? label[getLocale()] ?? label[getLocale().slice(0, 2)] ?? label['en-US'] ?? label.en ?? Object.values(label)[0] : t(label);
}

function options(catalog, key, values, audio) {
  if (key === 'audio_device') return (audio?.[values.direction === 'input' ? 'input_devices' : 'devices'] ?? [])
    .map((item) => ({ value: item.id, label: item.name }));
  if (key === 'power') return (catalog?.power_modes ?? []).map((mode) => ({
    value: mode, label: t(({ silent: '静音', balanced: '平衡', performance: '性能' })[mode] ?? mode),
  }));
  if (key === 'launcher' || key === 'webhook') return (catalog?.[key === 'launcher' ? 'launchers' : 'webhooks'] ?? [])
    .map((item) => ({ value: item.id, label: item.name }));
  return null;
}

function Params({ fields, value, set, catalog, audio }) {
  return fields?.map((field) => {
    const actual = value[field.id] ?? field.default ?? '';
    const update = (next) => {
      const values = { ...value };
      if (next === undefined) delete values[field.id];
      else values[field.id] = next;
      if (field.id === 'direction' && 'device_id' in values) delete values.device_id;
      set(values);
    };
    const choices = field.options?.map((item) => ({ ...item, label: name(item.label) }))
      ?? field.choices?.map((item) => ({ value: item, label: String(item) }))
      ?? options(catalog, field.type, value, audio);
    const current = choices?.findIndex((item) => Object.is(item.value, actual));
    return <label key={field.id} className="deck-field"><span>{name(field.name)}</span>
      {choices ? <select value={current < 0 ? '' : current} onChange={(event) =>
        update(event.target.value === '' ? undefined : choices[Number(event.target.value)].value)}>
        <option value="">{actual !== '' && current < 0 ? String(actual) : t('请选择')}</option>
        {choices.map((item, index) => <option key={index} value={index}>{item.label}</option>)}
      </select> : field.type === 'boolean' ? <select value={typeof actual === 'boolean' ? String(actual) : ''}
        onChange={(event) => update(event.target.value === '' ? undefined : event.target.value === 'true')}>
        <option value="">{t('请选择')}</option><option value="true">{t('开启')}</option><option value="false">{t('关闭')}</option>
      </select> : field.type === 'textarea' ? <textarea rows={2} maxLength={field.id === 'body' ? 2048 : 500}
        value={actual} onChange={(event) => update(event.target.value)} />
        : ['integer', 'number'].includes(field.type) ? <input type="number" step={field.type === 'integer' ? 1 : 'any'}
          min={field.minimum} max={field.maximum} value={actual} onChange={(event) =>
            update(event.target.value === '' ? undefined : event.target.valueAsNumber)} />
          : <input type="text" maxLength={500} autoCapitalize="none" value={actual} onChange={(event) => update(event.target.value)} />}
    </label>;
  });
}

function defaults(fields = []) {
  return Object.fromEntries(fields.filter((field) => field.default != null).map((field) => [field.id, field.default]));
}

function ActionFields({ action, catalog, audio, set }) {
  const builtins = catalog?.actions ?? [];
  const plugins = (catalog?.plugins ?? []).filter((plugin) => plugin.enabled);
  const plugin = plugins.find((item) => item.id === action.params.plugin_id);
  const spec = action.id === 'plugin' ? plugin?.actions.find((item) => item.id === action.params.action_id)
    : builtins.find((item) => item.id === action.id);
  const selected = action.id === 'plugin' ? `plugin:${action.params.plugin_id}:${action.params.action_id}` : action.id;
  return <>
    <label className="deck-field"><span>{t('动作')}</span><select value={selected} onChange={(event) => {
      const [id, pluginId, actionId] = event.target.value.split(':');
      const parameters = id === 'plugin'
        ? plugins.find((item) => item.id === pluginId)?.actions.find((item) => item.id === actionId)?.parameters
        : builtins.find((item) => item.id === id)?.parameters;
      set({ id, params: id === 'plugin' ? { plugin_id: pluginId, action_id: actionId, args: defaults(parameters) } : defaults(parameters) });
    }}>
      {!spec && <option value={selected}>{t('动作暂不可用')}</option>}
      {builtins.map((item) => <option key={item.id} value={item.id}>{name(item.name)}</option>)}
      {plugins.filter((item) => item.actions.length).map((item) => <optgroup key={item.id} label={name(item.name)}>
        {item.actions.map((definition) => <option key={definition.id} value={`plugin:${item.id}:${definition.id}`}>{name(definition.name)}</option>)}
      </optgroup>)}
    </select></label>
    <Params fields={spec?.parameters} value={action.id === 'plugin' ? action.params.args ?? {} : action.params}
      set={(params) => set({ ...action, params: action.id === 'plugin' ? { ...action.params, args: params } : params })}
      catalog={catalog} audio={audio} />
  </>;
}

function FeedbackFields({ tile, catalog, set, audio }) {
  const plugins = (catalog?.plugins ?? []).filter((item) => item.enabled);
  const spec = tile.feedback;
  const active = plugins.find((item) => item.id === spec?.plugin_id);
  const field = active?.feedbacks.find((item) => item.id === spec?.feedback_id);
  const selected = spec ? `${spec.plugin_id}:${spec.variable_id ? 'variable_id' : 'feedback_id'}:${spec.variable_id ?? spec.feedback_id}`
    : tile.live ?? '';
  const hasSpec = !spec || (spec.variable_id ? active?.variables.some((item) => item.id === spec.variable_id) : field);
  return <>
    <label className="deck-field"><span>{t('状态来源')}</span><select value={selected} onChange={(event) => {
      const [source, kind, id] = event.target.value.split(':');
      const match = plugins.find((item) => item.id === source)?.feedbacks.find((item) => item.id === id);
      set({ ...tile, live: kind ? null : source || null,
        feedback: kind ? { plugin_id: source, [kind]: id, args: kind === 'feedback_id' ? defaults(match?.parameters) : {} } : null });
    }}>
      {tile.kind === 'toggle' && <option value="">{t('使用动作状态')}</option>}
      {tile.kind === 'live' && Object.entries(liveLabels).map(([key, label]) => <option key={key} value={key}>{t(label)}</option>)}
      {!hasSpec && <option value={selected}>{t('插件暂不可用')}</option>}
      {plugins.map((plugin) => <optgroup key={plugin.id} label={name(plugin.name)}>
        {plugin.feedbacks.map((entry) => <option key={entry.id} value={`${plugin.id}:feedback_id:${entry.id}`}>{name(entry.name)}</option>)}
        {plugin.variables.map((entry) => <option key={`var:${entry.id}`} value={`${plugin.id}:variable_id:${entry.id}`}>{name(entry.name)}</option>)}
      </optgroup>)}
    </select></label>
    <Params fields={field?.parameters} value={spec?.args ?? {}} set={(args) => set({ ...tile, feedback: { ...spec, args } })}
      catalog={catalog} audio={audio} />
  </>;
}

export function TileEditor({ tile, set, onClose, onRemove, scenes, scene, ensureFolder, catalog, audio, canExpand }) {
  const [pickerOpen, setPickerOpen] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState('');
  const iconFileRef = useRef(null);
  const upload = useRef(null);
  const cover = CUSTOM_ICON_RE.test(tile.icon);
  const setStep = (index, next) => set({ ...tile, steps: tile.steps.map((item, i) => i === index ? next : item) });

  useEffect(() => () => upload.current?.abort(), []);

  async function uploadIcon(file) {
    if (!file) return;
    upload.current?.abort();
    const controller = new AbortController();
    upload.current = controller;
    setUploading(true);
    setUploadError('');
    try {
      const form = new FormData();
      form.append('file', file);
      const result = await request('/api/deck/icons', { method: 'POST', body: form, signal: controller.signal });
      if (controller.signal.aborted) return;
      set((current) => ({ ...current, icon: result.url }));
      setPickerOpen(false);
    } catch (error) {
      if (!controller.signal.aborted) setUploadError(error.message);
    } finally {
      if (!controller.signal.aborted) {
        setUploading(false);
        if (iconFileRef.current) iconFileRef.current.value = '';
      }
    }
  }
  function changeKind(kind) {
    const target = scenes.find((item) => item.id !== scene.id && item.folder)
      ?? scenes.find((item) => item.id !== scene.id);
    if (kind === 'folder' && !target) {
      ensureFolder((page) => ({ ...tile, kind, live: null, feedback: null, steps: [], page }));
      return;
    }
    const executable = kind === 'action' || kind === 'toggle';
    set({ ...tile, kind, steps: executable ? tile.steps.length ? tile.steps : freshTile().steps : [],
      live: kind === 'live' ? 'cpu' : null, page: kind === 'folder' ? target.id : null, feedback: null });
  }
  function shift(index, offset) {
    const steps = [...tile.steps];
    [steps[index], steps[index + offset]] = [steps[index + offset], steps[index]];
    set({ ...tile, steps });
  }
  return <aside className="deck-editor surface" aria-label={t('配置按键')} onKeyDown={(event) => {
    if (event.key === 'Escape' && !event.nativeEvent.isComposing) { event.stopPropagation(); onClose(); }
  }}>
    <div className="deck-editor-heading"><h2>{t('配置按键')}</h2><IconButton label={t('关闭')} onClick={onClose}><X size={18} /></IconButton></div>
    <label className="deck-field"><span>{t('按键类型')}</span><select value={tile.kind} onChange={(event) => changeKind(event.target.value)}>
      {Object.entries(typeLabels).map(([key, label]) => <option key={key} value={key}>{t(label)}</option>)}
    </select></label>
    <label className="deck-field"><span>{t('标题')}</span><input value={tile.title} maxLength={40} placeholder={t('自动标题')}
      onChange={(event) => set({ ...tile, title: event.target.value })} /></label>
    <div className="deck-field">
      <span>{t('宽度')}</span>
      <Segmented label={t('宽度')} value={tile.span ?? 1} onChange={(span) => set({ ...tile, span })}
        options={[{ value: 1, label: t('标准 1×1') }, { value: 2, label: t('宽键 2×1'), disabled: !canExpand }]} />
      {!canExpand && <small>{t('宽键需要同一行右侧有空位')}</small>}
    </div>
    <div className="deck-field">
      <span>{t('图标')}</span>
      {cover && <img className="deck-cover-preview" src={httpUrl(tile.icon).href} alt={t('按键封面预览')} data-span={tile.span ?? 1} />}
      <div className="deck-icon-bar">
        <Button onClick={() => setPickerOpen((open) => !open)}><Sparkles size={15} />{pickerOpen ? t('收起图标库') : t('选择图标')}</Button>
        <Button disabled={uploading} onClick={() => iconFileRef.current?.click()}><ImagePlus size={15} />{uploading ? t('上传中…') : t('上传图片')}</Button>
        {tile.icon && <IconButton label={t('清除图标')} onClick={() => set({ ...tile, icon: '' })}><X size={15} /></IconButton>}
        <input ref={iconFileRef} type="file" accept="image/png,image/jpeg,image/webp,image/svg+xml" hidden
          onChange={(event) => uploadIcon(event.target.files[0])} />
      </div>
      {uploadError && <p className="feedback error" role="alert">{uploadError}</p>}
      {pickerOpen && <div className="deck-icon-picker">
        <div className="deck-icon-grid">
          {DECK_ICON_KEYS.map((key) => {
            const Icon = DECK_ICONS[key];
            const value = `lucide:${key}`;
            return <button type="button" key={key} className="deck-icon-choice" aria-pressed={tile.icon === value}
              title={key} onClick={() => { set({ ...tile, icon: value }); setPickerOpen(false); }}>
              <Icon size={18} />
            </button>;
          })}
        </div>
        <input value={cover || tile.icon?.startsWith('lucide:') ? '' : tile.icon} maxLength={16}
          placeholder={t('或输入 Emoji')} onChange={(event) => set({ ...tile, icon: event.target.value })} />
      </div>}
    </div>
    <div className="deck-palette" role="group" aria-label={t('配色')}>
      {palettes.map((color, index) => <button type="button" key={color} data-deck-color={color}
        aria-label={t(['灰色', '蓝色', '青色', '绿色', '琥珀色', '粉色', '紫色', '红色'][index])}
        aria-pressed={tile.color === color} onClick={() => set({ ...tile, color })} />)}
    </div>
    {tile.kind === 'folder' && <label className="deck-field"><span>{t('目标场景')}</span><select value={tile.page ?? ''}
      onChange={(event) => set({ ...tile, page: event.target.value })}>
      {scenes.filter((item) => item.id !== scene.id).map((item) => <option key={item.id} value={item.id}>{t(item.title)}</option>)}
    </select></label>}
    {['live', 'toggle'].includes(tile.kind) && <FeedbackFields tile={tile} catalog={catalog} set={set} audio={audio} />}
    {tile.steps.map((step, index) => <fieldset key={index} className="deck-step">
      <legend>{t('步骤 {p1}', { p1: index + 1 })}</legend>
      {tile.steps.length > 1 && <div className="deck-step-tools">
        <IconButton label={t('上移')} disabled={!index} onClick={() => shift(index, -1)}><ArrowUp size={15} /></IconButton>
        <IconButton label={t('下移')} disabled={index === tile.steps.length - 1} onClick={() => shift(index, 1)}><ArrowDown size={15} /></IconButton>
        <IconButton label={t('删除步骤')} onClick={() => set({ ...tile, steps: tile.steps.filter((_, i) => i !== index) })}><Trash2 size={15} /></IconButton>
      </div>}
      <ActionFields action={step.action} catalog={catalog} audio={audio} set={(action) => setStep(index, { ...step, action })} />
      {(tile.steps.length > 1 || step.delay_ms > 0) && <label className="deck-field"><span>{t('步骤前延迟（毫秒）')}</span>
        <input type="number" min="0" max="3000" step="1" value={step.delay_ms}
          onChange={(event) => setStep(index, { ...step, delay_ms: Math.max(0, Math.min(3000, Number.parseInt(event.target.value, 10) || 0)) })} />
      </label>}
    </fieldset>)}
    <div className="deck-editor-actions">
      {tile.steps.length > 0 && <Button disabled={tile.steps.length >= 8}
        onClick={() => set({ ...tile, steps: [...tile.steps, { action: plainAction(), delay_ms: 0 }] })}><Plus size={15} />{t('添加步骤')}</Button>}
      <Button onClick={onRemove}><Trash2 size={15} />{t('删除按键')}</Button>
    </div>
  </aside>;
}

export function SceneEditor({ scene, set, add, remove, hasFolder, canAdd, canRemove }) {
  return <div className="deck-scene-editor surface">
    <label className="deck-field"><span>{t('场景名称')}</span><input maxLength={32} value={scene.title}
      onChange={(event) => set({ ...scene, title: event.target.value })} /></label>
    <label className="deck-field"><span>{t('网格')}</span><select value={scene.grid} onChange={(event) => {
      const grid = event.target.value;
      const [cols, rows] = grids[grid];
      set({ ...scene, grid, tiles: scene.tiles.slice(0, cols * rows) });
    }}>
      {Object.entries(grids).map(([key, [cols, rows]]) => <option key={key} value={key} disabled={!fitsDeckGrid(scene, key)}>{cols} × {rows}</option>)}
    </select></label>
    <label className="deck-field deck-folder-field"><input type="checkbox" checked={scene.folder}
      disabled={!scene.folder && !canRemove} onChange={(event) => set({ ...scene, folder: event.target.checked })} /><span>{t('仅在文件夹中显示')}</span></label>
    <Button disabled={!canAdd} onClick={add}><Plus size={15} />{t('新建场景')}</Button>
    {canRemove && <Button onClick={remove}><Trash2 size={15} />{t(hasFolder ? '删除场景及其入口' : '删除场景')}</Button>}
  </div>;
}
