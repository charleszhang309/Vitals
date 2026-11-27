import { t } from './i18n/messages.js';
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { ArrowUp, Sparkles, Square, Trash2 } from 'lucide-react';
import { request } from './api.js';
import { Button, IconButton, Modal } from './ui.jsx';
import AiConfirmation, { useAiConfirmations } from './AiConfirmation.jsx';

const HISTORY = '/api/ai/messages';
const shortcuts = ['总结今日状态', '检查电脑硬件', '整理中转站最新内容'];

function streamChat(message, signal, append, toolEvent) {
  return request('/api/ai/chat', {
    method: 'POST', signal, timeoutMs: 105_000, body: JSON.stringify({ message }),
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
  }, (response) => readChatStream(response, append, toolEvent));
}

async function readChatStream(response, append, toolEvent) {
  if (!response.body || !response.headers.get('Content-Type')?.includes('text/event-stream')) {
    throw new Error(t('服务器没有返回实时聊天流。'));
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  try {
    for (;;) {
      const part = await reader.read();
      buffer += decoder.decode(part.value, { stream: !part.done });
      const events = buffer.split(/\r?\n\r?\n/);
      buffer = events.pop();
      for (const event of events) {
        const lines = event.split(/\r?\n/);
        const type = lines.find((line) => line.startsWith('event:'))?.slice(6).trim();
        const data = lines.filter((line) => line.startsWith('data:')).map((line) => line.slice(5).trimStart()).join('\n');
        if (!data) continue;
        const payload = JSON.parse(data);
        if (type === 'delta') append(payload.text);
        else if (type === 'tool') toolEvent(payload);
        else if (type === 'error') throw new Error(t(payload.message));
        else if (type === 'done') return payload.message_id;
      }
      if (part.done) throw new Error(t('连接中断，请重试。'));
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

function MessageContent({ text }) {
  const blocks = text.split(/(\x60{3}[\s\S]*?(?:\x60{3}|$))/g);
  return blocks.map((block, index) => {
    if (block.startsWith('\x60\x60\x60')) {
      const body = block.replace(/^\x60{3}[^\n]*\n?/, '').replace(/\x60{3}$/, '');
      return <pre key={index}><code>{body}</code></pre>;
    }
    return block.split(/\n{2,}/).filter(Boolean).map((part, i) => {
      const lines = part.trim().split('\n');
      const unordered = lines.every((line) => /^[-*+]\s/.test(line));
      const ordered = lines.every((line) => /^\d+[.)]\s/.test(line));
      if (unordered || ordered) {
        const List = ordered ? 'ol' : 'ul';
        return <List key={index + '-' + i}>{lines.map((line, j) =>
          <li key={j}>{line.replace(/^(?:[-*+]|\d+[.)])\s+/, '')}</li>)}</List>;
      }
      return <p key={index + '-' + i}>{part}</p>;
    });
  });
}

function AssistantMessage({ text, tools = [], confirmations, children }) {
  const badges = tools.filter((event) => !event.confirmation);
  return <div className="ai-answer">
    {badges.length > 0 && <div className="ai-tool-badges" aria-label={t('工具操作状态')}>
      {badges.map((event, index) => <span key={index} data-ok={event.ok}><span aria-hidden="true">{event.ok ? '✓' : '!'}</span> {t(event.label)}</span>)}
    </div>}
    <div className="ai-bubble ai-assistant" aria-label="Vitals AI"><MessageContent text={text} />{children}</div>
    {tools.filter((event) => event.confirmation).map(({ confirmation }) => <AiConfirmation key={confirmation.id}
      confirmation={confirmation} state={confirmations.states[confirmation.id]} onResolve={confirmations.resolve} />)}
  </div>;
}

export default function AiChat({ settings, onClose }) {
  const [messages, setMessages] = useState(null);
  const [input, setInput] = useState('');
  const [turn, setTurn] = useState(null);
  const [toolReplies, setToolReplies] = useState({});
  const confirmations = useAiConfirmations();
  const [running, setRunning] = useState(false);
  const [confirmClear, setConfirmClear] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  const [viewportStyle, setViewportStyle] = useState(undefined);
  const stream = useRef(null);
  const body = useRef(null);
  const stickToBottom = useRef(true);
  const mounted = useRef(false);

  useEffect(() => {
    mounted.current = true;
    const viewport = window.visualViewport;
    function resize() {
      if (!viewport) return;
      setViewportStyle({ '--ai-viewport-height': viewport.height + 'px',
        '--ai-keyboard-inset': Math.max(0, window.innerHeight - viewport.height - viewport.offsetTop) + 'px' });
    }
    resize();
    viewport?.addEventListener('resize', resize);
    viewport?.addEventListener('scroll', resize);
    return () => {
      mounted.current = false;
      stream.current?.abort();
      viewport?.removeEventListener('resize', resize);
      viewport?.removeEventListener('scroll', resize);
    };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    request(HISTORY, { signal: controller.signal }).then((items) => {
      if (!controller.signal.aborted) setMessages(items);
    }).catch((failure) => {
      if (!controller.signal.aborted) setError(failure.message);
    });
    return () => controller.abort();
  }, [revision]);

  useLayoutEffect(() => {
    if (stickToBottom.current && body.current) body.current.scrollTop = body.current.scrollHeight;
  }, [messages, turn, running, viewportStyle]);

  async function send(raw) {
    const prompt = raw.trim();
    if (!prompt || !settings.configured || messages === null || stream.current || clearing) return;
    const controller = new AbortController();
    stream.current = controller;
    setInput('');
    setError('');
    setConfirmClear(false);
    const tools = [];
    setTurn({ prompt, answer: '', tools });
    setRunning(true);
    stickToBottom.current = true;
    try {
      const messageId = await streamChat(prompt, controller.signal, (text) => {
        if (!controller.signal.aborted) setTurn((previous) => ({ ...previous, answer: previous.answer + text }));
      }, (event) => {
        if (!controller.signal.aborted) {
          tools.push(event);
          setTurn((previous) => ({ ...previous, tools: [...tools] }));
        }
      });
      const history = await request(HISTORY, { signal: controller.signal });
      if (tools.length && messageId && history.some(({ id }) => id === messageId)) {
        setToolReplies((previous) => Object.fromEntries(history.flatMap(({ id }) =>
          id === messageId ? [[id, tools]] : previous[id] ? [[id, previous[id]]] : [])));
      }
      setMessages(history);
      setTurn(null);
    } catch (failure) {
      if (!mounted.current) return;
      if (!controller.signal.aborted) setError(failure.message);
      setTurn((previous) => previous ? { ...previous, interrupted: true } : null);
      if (!controller.signal.aborted && tools.length === 0) setInput(prompt);
    } finally {
      stream.current = null;
      if (mounted.current) setRunning(false);
    }
  }

  async function clear() {
    setClearing(true);
    setError('');
    try {
      await request(HISTORY, { method: 'DELETE' });
      setMessages([]);
      setTurn(null);
      setToolReplies({});
      setConfirmClear(false);
    } catch (failure) {
      setError(failure.message);
    } finally {
      setClearing(false);
    }
  }

  return <Modal id="vitals-ai" title="Vitals AI" className="detail-dialog ai-chat" style={viewportStyle} onClose={onClose}
    headerActions={<IconButton label={t('清空对话')} disabled={running || clearing || (!messages?.length && !turn)}
      onClick={() => setConfirmClear((previous) => !previous)}><Trash2 size={17} /></IconButton>}>
    {confirmClear && <div className="ai-clear">
      <span>{t('清空所有设备上的聊天记录？')}</span>
      <Button onClick={() => setConfirmClear(false)} disabled={clearing}>{t('取消')}</Button>
      <Button onClick={clear} disabled={clearing}>{t('清空')}</Button>
    </div>}
    <div className="ai-chat-scroll" ref={body} onScroll={(event) => {
      const el = event.currentTarget;
      stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
    }}>
      {!settings.configured && <p className="ai-empty muted">{t('请在服务器 config.json 中配置 ai.api_key')}</p>}
      {settings.configured && messages?.length === 0 && !turn && <div className="ai-quick">
        <Sparkles size={26} aria-hidden="true" />
        {shortcuts.map((prompt) => <Button key={prompt} onClick={() => send(t(prompt))}>{t(prompt)}</Button>)}
      </div>}
      {messages === null && !error && <p className="ai-empty muted" role="status">{t('正在读取对话…')}</p>}
      {messages?.map((message) => message.role === 'assistant'
        ? <AssistantMessage key={message.id} text={message.content} tools={toolReplies[message.id]} confirmations={confirmations} />
        : <div key={message.id} className="ai-bubble ai-user" aria-label={t('你')}><MessageContent text={message.content} /></div>)}
      {turn && <>
        <div className="ai-bubble ai-user"><MessageContent text={turn.prompt} /></div>
        <AssistantMessage text={turn.answer || (running ? t('正在生成…') : t('回复未完成'))} tools={turn.tools} confirmations={confirmations}>
          {running && <span className="ai-cursor" aria-hidden="true" />}
          {turn.interrupted && turn.answer && <small className="muted">{t('回复未完整保存')}</small>}
        </AssistantMessage>
      </>}
    </div>
    {error && <div className="feedback error ai-error" role="alert">{error}
      {messages === null && <Button onClick={() => { setError(''); setRevision((value) => value + 1); }}>{t('重试')}</Button>}
    </div>}
    <form className="ai-composer" onSubmit={(event) => { event.preventDefault(); send(input); }}>
      <textarea aria-label={t('聊天消息')} placeholder={t('发消息给 Vitals AI')} value={input} maxLength={6000}
        disabled={!settings.configured} rows={1} onChange={(event) => setInput(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing && !running) {
            event.preventDefault();
            send(input);
          }
        }} />
      {running ? <IconButton label={t('停止')} onClick={() => stream.current?.abort()}><Square size={16} fill="currentColor" /></IconButton>
        : <IconButton label={t('发送')} type="submit" disabled={!input.trim() || messages === null || !settings.configured || clearing}>
          <ArrowUp size={20} />
        </IconButton>}
    </form>
  </Modal>;
}
