import { lazy, Suspense, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import { Archive, Copy, Download, Eye, File, FileText, Image, Paperclip, Pin, RotateCw, Sparkles, Trash2 } from 'lucide-react';
import { httpUrl, postJson, request, sendJson } from './api.js';
import { bytes, time } from './format.js';
import { documentType, PREVIEW_LIMIT } from './previewFormat.js';
import { Button, IconButton, Modal } from './ui.jsx';
import AiConfirmation, { useAiConfirmations } from './AiConfirmation.jsx';

const SCRATCHPAD = '/api/scratchpad';
const DocumentPreview = lazy(() => import('./DocumentPreview.jsx'));
const PREVIEW = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/avif', 'image/bmp', 'image/x-icon']);
const CLIPBOARD_EXTENSIONS = { 'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp', 'image/gif': 'gif' };

function fileUrl(id, download = false) {
  return httpUrl(`${SCRATCHPAD}/files/${id}${download ? '?download=true' : ''}`).href;
}

function sortRecent(items) {
  return [...items].sort((a, b) => Number(b.pinned) - Number(a.pinned) || Date.parse(b.created_at) - Date.parse(a.created_at) || b.id - a.id);
}

function TextCard({ text, tools }) {
  const [expanded, setExpanded] = useState(false);
  const [selecting, setSelecting] = useState(false);
  const [copyStatus, setCopyStatus] = useState('');
  const field = useRef(null);
  const length = [...text].length;
  const lines = text.split('\n');
  const truncated = length > 175 || lines.length > 4;
  const preview = [...lines.slice(0, 4).join('\n')].slice(0, 175).join('') + (truncated ? '…' : '');

  useLayoutEffect(() => {
    if (!selecting || !field.current) return;
    field.current.focus();
    field.current.select();
    setSelecting(false);
  }, [expanded, selecting]);

  async function copy() {
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(text);
        setCopyStatus('已复制');
        return;
      } catch {
        // 普通 HTTP 或浏览器拒绝写剪贴板时，交给系统复制操作。
      }
    }
    setExpanded(true);
    setSelecting(true);
    setCopyStatus('已选中文字，请使用系统菜单或 Ctrl+C / ⌘C 复制。');
  }

  return (
    <>
      <div className="scratch-card-content">
        {expanded ? <textarea className="text-expand" rows={Math.min(10, Math.max(4, lines.length))} readOnly ref={field}
          aria-label="中转站文本内容" value={text} /> : <p className="text-excerpt">{preview}</p>}
        {truncated && <Button className="quiet-button" onClick={() => setExpanded(!expanded)} aria-expanded={expanded}>{expanded ? '收起' : '展开'}</Button>}
        {copyStatus && <p className="feedback" role="status">{copyStatus}</p>}
      </div>
      <div className="card-tools">
        <IconButton label="复制文本" onClick={copy}><Copy size={18} /></IconButton>
        {tools}
      </div>
    </>
  );
}

function ItemCard({ item, openImage, openDocument, remove, pin, busy, aiEnabled, ref }) {
  const text = item.kind === 'text';
  const document = !text && Boolean(documentType(item.filename));
  const image = !text && !document && PREVIEW.has(item.mime_type);
  const oversized = item.size_bytes > PREVIEW_LIMIT;
  const picture = image && !oversized;
  const Icon = text ? FileText : picture ? Image : File;
  const title = text ? '文本' : item.filename;
  const [aiResult, setAiResult] = useState('');
  const [aiError, setAiError] = useState('');
  const [aiBusy, setAiBusy] = useState(false);
  const [aiOpen, setAiOpen] = useState(false);

  async function analyze() {
    if (aiResult) { setAiOpen((open) => !open); return; }
    setAiBusy(true);
    setAiError('');
    setAiOpen(true);
    try {
      const reply = await postJson('/api/ai/analyze', { id: item.id });
      setAiResult(reply.result);
    } catch (error) {
      setAiError(error.message);
    } finally {
      setAiBusy(false);
    }
  }

  const tools = <>
    {text && aiEnabled && <Button className="quiet-button ai-button" title="将此卡片文本发送给已配置的 AI"
      disabled={aiBusy} onClick={analyze} aria-expanded={aiOpen}><Sparkles size={15} />{aiBusy ? '正在整理…' : 'AI 整理'}</Button>}
    <IconButton label={item.pinned ? '取消固定' : '固定卡片'} aria-pressed={item.pinned} className={item.pinned ? 'pinned-button' : ''}
      disabled={busy} onClick={() => pin(item)}><Pin size={18} /></IconButton>
    {document && item.size_bytes <= PREVIEW_LIMIT && <IconButton label={`预览 ${item.filename}`} aria-haspopup="dialog"
      onClick={() => openDocument(item)}><Eye size={18} /></IconButton>}
    {!text && <IconButton label={`下载 ${item.filename}`} href={fileUrl(item.id, true)} download><Download size={18} /></IconButton>}
    <IconButton label={text ? '删除文本' : `删除 ${item.filename}`} className="delete-button" disabled={busy} onClick={() => remove(item.id)}><Trash2 size={18} /></IconButton>
  </>;

  return (
    <motion.li ref={ref} layout="position" className="surface scratch-card"
      initial={{ opacity: 0, scale: 0.92 }}
      animate={{ opacity: 1, scale: 1 }}
      exit={{ opacity: 0, scale: 0.88, transition: { duration: 0.16, ease: [0.32, 0, 0.67, 0] } }}
      transition={{ type: 'spring', stiffness: 420, damping: 32, mass: 0.8 }}
      transition={{ duration: 0.2 }}>
      <div className="scratch-card-header">
        <span className={`scratch-type ${picture ? 'picture-type' : ''}`}><Icon size={20} aria-hidden="true" /></span>
        <div className="scratch-card-title"><h2 title={title}>{title}</h2><time dateTime={item.created_at} className="muted numeric">{time(item.created_at, true)}</time></div>
        {item.pinned && <span className="pin-mark"><Pin size={14} aria-hidden="true" />已固定</span>}
      </div>
      {text ? <TextCard text={item.text ?? ''} tools={tools} /> : <>
        <div className="scratch-card-content">{picture ? (
          <>
            <motion.button type="button" className="thumbnail" aria-label={`预览 ${item.filename}`} onClick={() => openImage(item)} whileTap={{ scale: 0.97 }}>
              <img src={fileUrl(item.id)} alt={item.filename} loading="lazy" />
            </motion.button>
            <p className="file-meta muted">{bytes(item.size_bytes)} · {item.mime_type}</p>
          </>
        ) : <>
          <p className="file-meta muted">{bytes(item.size_bytes)} · {item.mime_type}</p>
          {(document || image) && oversized && <p className="file-meta muted">超过 15 MB，请下载查看。</p>}
        </>}</div>
        <div className="card-tools">{tools}</div>
      </>}
      <AnimatePresence initial={false}>
        {aiOpen && aiEnabled && <motion.div className="ai-expand" initial={{ height: 0, opacity: 0 }} animate={{ height: 'auto', opacity: 1 }}
          exit={{ height: 0, opacity: 0 }} transition={{ duration: 0.2 }}>
          <div className="ai-output">{aiError ? <p role="alert" className="error">{aiError}</p> : <p>{aiResult || '正在整理这条文本…'}</p>}</div>
        </motion.div>}
      </AnimatePresence>
    </motion.li>
  );
}

export default function ScratchpadPage({ aiEnabled = false }) {
  const [items, setItems] = useState(null);
  const [text, setText] = useState('');
  const [activeImage, setActiveImage] = useState(null);
  const [activeDocument, setActiveDocument] = useState(null);
  const [notice, setNotice] = useState(null);
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [reload, setReload] = useState(0);
  const [commands, setCommands] = useState([]);
  const confirmations = useAiConfirmations();
  const input = useRef(null);
  const dragDepth = useRef(0);
  const changing = useRef(false);
  const revision = useRef(0);

  useEffect(() => {
    const controller = new AbortController();
    async function refresh(reportError = false) {
      if (changing.current || document.hidden) return;
      const sequence = ++revision.current;
      try {
        const all = await request(SCRATCHPAD, { signal: controller.signal });
        if (!controller.signal.aborted && sequence === revision.current) setItems(all);
      } catch (error) {
        if (reportError && !controller.signal.aborted && sequence === revision.current) setNotice({ error: true, text: error.message });
      }
    }
    refresh(true);
    const refreshOnFocus = () => { refresh(); };
    const timer = window.setInterval(refreshOnFocus, 10_000);
    window.addEventListener('focus', refreshOnFocus);
    document.addEventListener('visibilitychange', refreshOnFocus);
    return () => {
      controller.abort();
      window.clearInterval(timer);
      window.removeEventListener('focus', refreshOnFocus);
      document.removeEventListener('visibilitychange', refreshOnFocus);
    };
  }, [reload]);

  async function change(action, pendingText) {
    if (changing.current) return;
    changing.current = true;
    revision.current += 1;
    setBusy(true);
    setNotice({ text: pendingText });
    try {
      await action();
      setNotice(null);
    } catch (error) {
      setNotice({ error: true, text: error.message });
    } finally {
      changing.current = false;
      setBusy(false);
      setReload((value) => value + 1);
    }
  }

  function saveText(event) {
    event.preventDefault();
    if (!text.trim()) return;
    const command = aiEnabled ? text.trim().match(/^\/(?:ai|cmd)\s+([\s\S]+)$/i) : null;
    if (command) {
      change(async () => {
        const result = await postJson('/api/ai/command', { command: command[1].trim() });
        setCommands((previous) => [{ ...result, command: command[1].trim(), id: Date.now() }, ...previous]);
        setText((previous) => previous === text ? '' : previous);
      }, '正在执行快捷控制…');
      return;
    }
    change(async () => {
      const item = await postJson(`${SCRATCHPAD}/text`, { text });
      setItems((previous) => sortRecent([item, ...(previous ?? [])]));
      setText((previous) => previous === text ? '' : previous);
    }, '正在保存文字…');
  }

  function uploadFiles(files, clipboard = false) {
    if (!files.length) return;
    change(async () => {
      const failures = [];
      for (const [index, file] of files.entries()) {
        const form = new FormData();
        const ext = CLIPBOARD_EXTENSIONS[file.type] ?? (file.type.slice(6).replace(/[^a-z0-9]/gi, '') || 'img');
        const filename = clipboard ? `截图-${new Date().toISOString().replace(/[:.]/g, '-')}-${index + 1}.${ext}` : file.name;
        form.append('file', file, filename);
        try {
          const item = await request(`${SCRATCHPAD}/file`, { method: 'POST', body: form });
          setItems((previous) => sortRecent([item, ...(previous ?? [])]));
        } catch (error) {
          failures.push(`${filename}：${error.message}`);
        }
      }
      if (failures.length) throw new Error(failures.join('\n'));
    }, `正在上传 ${files.length} 个文件…`);
  }

  function paste(event) {
    const clip = event.clipboardData;
    if (!clip) return;
    const files = Array.from(clip.files).filter((file) => file.type.startsWith('image/'));
    const images = files.length ? files : Array.from(clip.items ?? []).filter((item) => item.kind === 'file' && item.type.startsWith('image/'))
      .map((item) => item.getAsFile()).filter(Boolean);
    if (images.length) {
      event.preventDefault();
      uploadFiles(images, true);
    }
  }

  function drop(event) {
    if (!Array.from(event.dataTransfer.types).includes('Files')) return;
    event.preventDefault();
    dragDepth.current = 0;
    setDragging(false);
    uploadFiles(Array.from(event.dataTransfer.files));
  }

  function remove(id) {
    change(async () => {
      await request(`${SCRATCHPAD}/${id}`, { method: 'DELETE' });
      setItems((previous) => previous?.filter((item) => item.id !== id));
      setActiveImage((previous) => previous?.id === id ? null : previous);
    }, '正在删除…');
  }

  function pin(item) {
    change(async () => {
      const updated = await sendJson('PATCH', `${SCRATCHPAD}/${item.id}/pin`, { pinned: !item.pinned });
      setItems((previous) => sortRecent((previous ?? []).map((record) => record.id === item.id ? updated : record)));
    }, item.pinned ? '正在取消固定…' : '正在固定…');
  }

  return (
    <>
      <div className="page-heading"><h1>中转站</h1></div>
      <form onSubmit={saveText} className={`surface composer${dragging ? ' drop-active' : ''}`}
        onDragEnter={(event) => {
          if (!Array.from(event.dataTransfer.types).includes('Files')) return;
          event.preventDefault();
          dragDepth.current += 1;
          setDragging(true);
        }}
        onDragOver={(event) => {
          if (!Array.from(event.dataTransfer.types).includes('Files')) return;
          event.preventDefault();
          event.dataTransfer.dropEffect = busy ? 'none' : 'copy';
        }}
        onDragLeave={() => {
          dragDepth.current = Math.max(0, dragDepth.current - 1);
          if (!dragDepth.current) setDragging(false);
        }} onDrop={drop}>
        <textarea aria-label="中转文字" placeholder={aiEnabled ? '输入文字，或用 /ai 或 /cmd 执行快捷控制…' : '在此输入要中转的文字…'} rows={5} maxLength={20000} value={text}
          onChange={(event) => setText(event.target.value)} onPaste={paste} />
        <p className="composer-hint muted"><Paperclip size={16} aria-hidden="true" />{dragging ? '松开以上传文件' : '拖入文件或粘贴截图（Ctrl+V）'}</p>
        <div className="composer-actions flex items-center justify-between gap-3">
          <input ref={input} type="file" multiple hidden onChange={(event) => {
            const files = Array.from(event.target.files);
            event.target.value = '';
            uploadFiles(files);
          }} />
          <Button disabled={busy} onClick={() => input.current.click()}><Paperclip size={17} /> 上传文件</Button>
          <Button className="primary-button" disabled={busy || !text.trim()} type="submit">{aiEnabled && /^\/(?:ai|cmd)\s+\S/i.test(text.trim()) ? '执行指令' : '保存文字'}</Button>
        </div>
        {notice && <p className={`feedback${notice.error ? ' error' : ''}`} role={notice.error ? 'alert' : 'status'}>{notice.text}</p>}
      </form>
      {commands.length > 0 && <ul className="command-results" aria-label="快捷控制结果">{commands.map((result) => <li key={result.id} className="surface panel">
        <strong>{result.message}</strong><p className="muted">{result.command}{result.scheduled_at && ` · ${time(result.scheduled_at, true)}`}</p>
        {result.confirmation && <AiConfirmation confirmation={result.confirmation}
          state={confirmations.states[result.confirmation.id]} onResolve={confirmations.resolve} />}
      </li>)}</ul>}
      {items === null ? <div className="surface empty-scratch">
        {notice?.error ? <Button onClick={() => { setNotice(null); setReload((value) => value + 1); }}><RotateCw size={16} />重试读取</Button> : <p>正在读取中转站…</p>}
      </div> : (
        <>
          {items.length === 0 && <div className="surface empty-scratch"><Archive size={28} aria-hidden="true" /><p>中转站暂无内容</p></div>}
          <ul className="scratch-grid" aria-label="中转记录"><AnimatePresence initial={false} mode="popLayout">
            {items.map((item) => <ItemCard key={item.id} item={item} openImage={setActiveImage} openDocument={setActiveDocument}
              remove={remove} pin={pin} busy={busy} aiEnabled={aiEnabled} />)}
          </AnimatePresence></ul>
        </>
      )}
      <AnimatePresence>
        {activeImage && <Modal key="image" id="image-preview" title={activeImage.filename} className="preview-dialog" onClose={() => setActiveImage(null)}>
          <img className="preview-image" src={fileUrl(activeImage.id)} alt={activeImage.filename} />
          <div className="card-tools"><IconButton label="下载图片" href={fileUrl(activeImage.id, true)} download><Download size={18} /></IconButton></div>
        </Modal>}
        {activeDocument && <Suspense key="document" fallback={null}>
          <DocumentPreview item={activeDocument} inline={httpUrl(`${SCRATCHPAD}/files/${activeDocument.id}?preview=true`).href}
            download={fileUrl(activeDocument.id, true)} onClose={() => setActiveDocument(null)} />
        </Suspense>}
      </AnimatePresence>
    </>
  );
}
