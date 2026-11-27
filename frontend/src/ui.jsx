import { useLayoutEffect, useRef } from 'react';
import { motion, useIsPresent, useReducedMotion } from 'framer-motion';
import { ChevronRight, X } from 'lucide-react';

const spring = { type: 'spring', stiffness: 440, damping: 35, mass: 0.8 };
const easeOut = [0.22, 1, 0.36, 1];
const modalStack = [];

export function Button({ className = '', children, ...props }) {
  return <motion.button type="button" className={`button ${className}`} whileTap={{ scale: 0.97 }} {...props}>{children}</motion.button>;
}

export function IconButton({ label, children, href, className = '', ...props }) {
  const attributes = { className: `button icon-button ${className}`, 'aria-label': label, title: label, whileTap: { scale: 0.97 }, ...props };
  return href
    ? <motion.a href={href} {...attributes}>{children}</motion.a>
    : <motion.button type="button" {...attributes}>{children}</motion.button>;
}

export function Segmented({ id, label, options, value, onChange, tabs = false, disabled = false }) {
  function moveFocus(event) {
    const keys = ['ArrowLeft', 'ArrowRight', 'Home', 'End'];
    if (!tabs || !keys.includes(event.key)) return;
    event.preventDefault();
    const index = options.findIndex((option) => option.value === value);
    const nextIndex = event.key === 'Home' ? 0 : event.key === 'End' ? options.length - 1
      : (options.length + index + (event.key === 'ArrowLeft' ? -1 : 1)) % options.length;
    event.currentTarget.querySelectorAll('[role="tab"]')[nextIndex]?.focus();
    onChange(options[nextIndex].value);
  }

  return (
    <div className="segments" role={tabs ? 'tablist' : 'group'} aria-label={label} onKeyDown={moveFocus}>
      {options.map((option) => {
        const selected = option.value === value;
        const state = tabs
          ? { role: 'tab', id: `tab-${option.value}`, 'aria-selected': selected, 'aria-controls': `panel-${option.value}`, tabIndex: selected ? 0 : -1 }
          : { 'aria-pressed': selected };
        return (
          <motion.button key={option.value} type="button" className="segment" disabled={disabled || option.disabled}
            onClick={() => { if (!selected) onChange(option.value); }} whileTap={{ scale: 0.97 }} {...state}>
            {selected && <motion.span className="segment-highlight" layoutId={`${id}-selection`} transition={spring} aria-hidden="true" />}
            <span className="segment-label">{option.label}</span>
          </motion.button>
        );
      })}
    </div>
  );
}

export function Modal({ id, title, onClose, className = '', headerActions, style, children }) {
  const ref = useRef(null);
  const present = useIsPresent();
  const reduced = useReducedMotion();
  useLayoutEffect(() => {
    const dialog = ref.current;
    const entry = { dialog, opener: document.activeElement };
    dialog.showModal();
    modalStack.push(entry);
    return () => {
      const index = modalStack.indexOf(entry);
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
  }, []);

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
      animate={{ opacity: 1, scale: 1 }} exit={{ opacity: 0, scale: reduced ? 1 : 0.95 }}
      transition={{ duration: reduced ? 0 : 0.18, ease: easeOut }}
      onCancel={(event) => {
        if (event.target !== event.currentTarget) return;
        event.preventDefault();
        dismiss();
      }}
      onClick={(event) => { if (event.target === event.currentTarget) dismiss(); }}>
      <div className="modal-body surface" inert={!present || undefined}>
        <div className="modal-header flex items-center justify-between gap-4">
          <h2 id={`${id}-title`}>{title}</h2>
          <div className="modal-controls">{headerActions}<IconButton className="modal-close" label="关闭" onClick={dismiss}><X size={19} aria-hidden="true" /></IconButton></div>
        </div>
        {children}
      </div>
    </motion.dialog>
  );
}

export function DetailLink({ title, onClick }) {
  return <button className="detail-link" type="button" aria-label={`查看${title}详情`} aria-haspopup="dialog" onClick={onClick}>
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
