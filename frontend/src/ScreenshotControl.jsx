import { t } from './i18n/messages.js';
import { useId, useLayoutEffect, useRef, useState } from 'react';
import { AnimatePresence } from 'framer-motion';
import { RefreshCw, ScanLine } from 'lucide-react';
import { request } from './api.js';
import { Button, IconButton, Modal } from './ui.jsx';
import './ScreenshotControl.css';

export default function ScreenshotControl({ deviceId, deviceName, disabled = false }) {
  const id = useId();
  const captureRequest = useRef(null);
  const objectUrl = useRef(null);
  const [open, setOpen] = useState(false);
  const [view, setView] = useState({ loading: true });

  useLayoutEffect(() => () => {
    captureRequest.current?.abort();
    if (objectUrl.current) URL.revokeObjectURL(objectUrl.current);
  }, []);

  function clear() {
    captureRequest.current?.abort();
    captureRequest.current = null;
    if (objectUrl.current) URL.revokeObjectURL(objectUrl.current);
    objectUrl.current = null;
  }

  async function capture() {
    clear();
    const controller = new AbortController();
    captureRequest.current = controller;
    setView({ loading: true });
    const path = deviceId ? `/api/control/screenshot/${encodeURIComponent(deviceId)}` : '/api/control/screenshot';
    try {
      const image = await request(path, { method: 'POST', signal: controller.signal }, (response) => response.blob());
      if (controller.signal.aborted || captureRequest.current !== controller) return;
      if (!['image/webp', 'image/jpeg'].includes(image.type)) throw new Error(t('未收到有效截图，请重试。'));
      objectUrl.current = URL.createObjectURL(image);
      setView({ url: objectUrl.current });
    } catch (error) {
      if (!controller.signal.aborted && captureRequest.current === controller) setView({ error: error.message });
    }
  }

  function close() {
    clear();
    setOpen(false);
    setView({ loading: true });
  }

  function openPreview() {
    setOpen(true);
    capture();
  }

  return <>
    <Button onClick={openPreview} disabled={disabled} aria-haspopup="dialog">
      <ScanLine size={15} aria-hidden="true" /><span>{t('截图')}</span>
    </Button>
    <AnimatePresence>
      {open && <Modal id={id} className="detail-dialog screenshot-dialog" title={deviceName ? t('{p1} 截图', { p1: deviceName }) : t('主屏截图')}
        onClose={close} headerActions={<IconButton label={t('重新截图')} disabled={disabled || view.loading} onClick={capture}>
          <RefreshCw size={17} aria-hidden="true" />
        </IconButton>}>
        <div className="screenshot-preview" aria-busy={Boolean(view.loading)}>
          {view.loading && <p className="muted" role="status">{t('正在截取主屏…')}</p>}
          {view.error && <p className="feedback error" role="alert">{view.error}</p>}
          {view.url && <img src={view.url} alt={deviceName ? t('{p1} 的主屏截图', { p1: deviceName }) : t('本机主屏截图')} />}
        </div>
      </Modal>}
    </AnimatePresence>
  </>;
}
