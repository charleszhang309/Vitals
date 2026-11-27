import { getLocale, t } from './i18n/messages.js';
import { useLayoutEffect, useRef } from 'react';
import { motion, usePresence, useReducedMotion } from 'framer-motion';
import { ChevronRight, X } from 'lucide-react';

const easeOut = [0.22, 1, 0.36, 1];
const modalDuration = 0.18;
const modalStack = [];

export function Button({ className = '', children, disabled, ...props }) {
  return <motion.button type="button" className={`button ${className}`} disabled={disabled}
    whileTap={disabled ? undefined : { scale: 0.97 }} {...props}>{children}</motion.button>;
}

export function IconButton({ label, children, href, className = '', disabled, ...props }) {
  const attributes = { className: `button icon-button ${className}`, 'aria-label': label, title: label,
    whileTap: disabled ? undefined : { scale: 0.97 }, ...props };
  return href && !disabled
    ? <motion.a href={href} {...attributes}>{children}</motion.a>
    : <motion.button type="button" disabled={disabled} {...attributes}>{children}</motion.button>;
}

export function Segmented({ id, label, options, value, onChange, tabs = false, disabled = false }) {
  const locale = getLocale();
  const groupRef = useRef(null);
  const highlightRef = useRef(null);
  const hasMeasuredRef = useRef(false);
  const reduced = useReducedMotion();
  const tabStop = tabs && (options.find((option) => !option.disabled && option.value === value)
    ?? options.find((option) => !option.disabled));

  useLayoutEffect(() => {
    const group = groupRef.current;
    const highlight = highlightRef.current;
    if (!group || !highlight) return;

    function sync(animate) {
      const active = group.querySelector('[aria-selected="true"], [aria-pressed="true"]');
      if (!active) {
        highlight.style.opacity = '0';
        return;
      }
      if (animate && !reduced) {
        highlight.dataset.animated = 'true';
      } else {
        delete highlight.dataset.animated;
      }
      highlight.style.opacity = '1';
      highlight.style.width = `${active.offsetWidth}px`;
      highlight.style.height = `${active.offsetHeight}px`;
      highlight.style.transform = `translate3d(${active.offsetLeft}px, ${active.offsetTop}px, 0)`;
    }

    sync(hasMeasuredRef.current);
    hasMeasuredRef.current = true;

    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(() => sync(false));
    observer.observe(group);
    return () => observer.disconnect();
  }, [value, options, reduced, locale]);

  function moveFocus(event) {
    const keys = ['ArrowLeft', 'ArrowRight', 'Home', 'End'];
    if (!tabs || disabled || !keys.includes(event.key)) return;
    const availableTabs = [...event.currentTarget.querySelectorAll('[role="tab"]:enabled')];
    if (!availableTabs.length) return;
    event.preventDefault();
    const index = availableTabs.indexOf(event.target);
    const nextIndex = event.key === 'Home' ? 0 : event.key === 'End' ? availableTabs.length - 1
      : (availableTabs.length + index + (event.key === 'ArrowLeft' ? -1 : 1)) % availableTabs.length;
    const nextTab = availableTabs[nextIndex];
    nextTab.focus();
    nextTab.click();
  }

  return (
    <div ref={groupRef} className="segments" role={tabs ? 'tablist' : 'group'} aria-label={label} onKeyDown={moveFocus}>
      <span ref={highlightRef} className="segment-highlight" aria-hidden="true" />
      {options.map((option) => {
        const selected = option.value === value;
        const state = tabs
          ? { role: 'tab', id: `tab-${option.value}`, 'aria-selected': selected, 'aria-controls': `panel-${option.value}`, tabIndex: option === tabStop ? 0 : -1 }
          : { 'aria-pressed': selected };
        return (
          <button key={option.value} type="button" className="segment" disabled={disabled || option.disabled}
            onClick={() => { if (!selected) onChange(option.value); }} {...state}>
            <span className="segment-label">{option.label}</span>
          </button>
        );
      })}
    </div>
  );
}

export function Modal({ id, title, onClose, className = '', headerActions, style, children }) {
  const ref = useRef(null);
  const release = useRef(null);
  const [present, safeToRemove] = usePresence();
  const reduced = useReducedMotion();
  useLayoutEffect(() => () => release.current?.(), []);
  useLayoutEffect(() => {
    if (!present || release.current) return;
    const dialog = ref.current;
    const entry = { dialog, opener: document.activeElement };
    if (!dialog.open) dialog.showModal();
    modalStack.push(entry);
    release.current = () => {
      release.current = null;
      const index = modalStack.indexOf(entry);
      if (index === -1) return;
      const above = modalStack[index + 1];
      if (above && dialog.contains(above.opener)) above.opener = entry.opener;
      modalStack.splice(index, 1);
      dialog.close();
      if (above) return;
      const next = modalStack.at(-1)?.dialog;
      const opener = entry.opener;
      if (opener instanceof HTMLElement && opener.isConnected && (!next || next.contains(opener))) {
        opener.focus({ preventScroll: true });
      }
      if (next && !next.contains(document.activeElement)) next.querySelector('.modal-close')?.focus({ preventScroll: true });
    };
  }, [present]);

  useLayoutEffect(() => {
    if (present) return undefined;
    // 退出动画被中断时也要解除浏览器的原生模态锁。
    const timer = window.setTimeout(() => {
      release.current?.();
      safeToRemove?.();
    }, reduced ? 0 : modalDuration * 1000 + 40);
    return () => window.clearTimeout(timer);
  }, [present, reduced, safeToRemove]);

  useLayoutEffect(() => {
    const dialog = ref.current;
    function handleEscape(event) {
      if (event.key !== 'Escape' || event.defaultPrevented || event.isComposing || modalStack.at(-1)?.dialog !== dialog) return;
      event.preventDefault();
      if (present && !event.repeat) onClose();
    }
    // Route Escape to one dialog even when the browser groups multiple native close requests.
    document.addEventListener('keydown', handleEscape);
    return () => document.removeEventListener('keydown', handleEscape);
  }, [onClose, present]);

  function dismiss() {
    if (present && modalStack.at(-1)?.dialog === ref.current) onClose();
  }

  return (
    <motion.dialog ref={ref} className={`modal ${className}`} style={style} aria-labelledby={`${id}-title`}
      data-closing={!present || undefined}
      initial={reduced ? false : { opacity: 0, scale: 0.95 }}
      animate={present ? { opacity: 1, scale: 1 } : { opacity: 0, scale: reduced ? 1 : 0.95 }}
      transition={{ duration: reduced ? 0 : modalDuration, ease: easeOut }}
      onCancel={(event) => {
        if (event.target !== event.currentTarget) return;
        event.preventDefault();
        dismiss();
      }}
      onClick={(event) => { if (event.target === event.currentTarget) dismiss(); }}>
      <div className="modal-body surface" inert={!present || undefined}>
        <div className="modal-header flex items-center justify-between gap-4">
          <h2 id={`${id}-title`}>{title}</h2>
          <div className="modal-controls">{headerActions}<IconButton className="modal-close" label={t('关闭')} onClick={dismiss}><X size={19} aria-hidden="true" /></IconButton></div>
        </div>
        {children}
      </div>
    </motion.dialog>
  );
}

export function DetailLink({ title, onClick }) {
  return <button className="detail-link" type="button" aria-label={t('查看{p1}详情', { p1: title })} aria-haspopup="dialog" onClick={onClick}>
    <ChevronRight size={17} aria-hidden="true" />
  </button>;
}

export function DetailValues({ items }) {
  return <dl className="detail-values">{items.map(([label, value]) => <div key={label}>
    <dt>{label}</dt><dd className="numeric">{value ?? '—'}</dd>
  </div>)}</dl>;
}

export function ProgressBar({ value, tone = 'green', label }) {
  const available = Number.isFinite(value);
  const percent = available ? Math.min(100, Math.max(0, value)) : 0;
  return (
    <div className={`progress tone-${tone}`} role={available ? 'progressbar' : undefined} aria-hidden={!available || undefined}
      aria-label={available ? label : undefined} aria-valuenow={available ? Math.round(percent) : undefined}>
      <span style={{ '--progress-scale': percent / 100 }} />
    </div>
  );
}
