import { t } from './i18n/messages.js';
import { useEffect, useRef, useState } from 'react';
import { Download, ExternalLink } from 'lucide-react';
import DOMPurify from 'dompurify';
import { request } from './api.js';
import { documentType, PREVIEW_LIMIT } from './previewFormat.js';
import { Modal } from './ui.jsx';

function ParsedDocument({ type, url }) {
  const [file, setFile] = useState({});
  const [selected, setSelected] = useState(0);
  const worker = useRef(null);
  const timer = useRef(null);

  useEffect(() => {
    const controller = new AbortController();
    const instance = new Worker(new URL('./documentWorker.js', import.meta.url), { type: 'module' });
    worker.current = instance;
    const fail = (error) => {
      clearTimeout(timer.current);
      instance.terminate();
      if (!controller.signal.aborted) setFile({ error });
    };
    instance.onerror = () => fail(t('无法处理此文件预览，请下载查看。'));
    instance.onmessage = ({ data }) => {
      clearTimeout(timer.current);
      if (data.error) { fail(data.error); return; }
      if (typeof data.html === 'string') {
        setFile({ html: DOMPurify.sanitize(data.html, {
          ALLOWED_TAGS: ['p', 'br', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'a', 'ul', 'ol', 'li', 'strong', 'em',
            's', 'u', 'sup', 'sub', 'table', 'th', 'td', 'tr', 'thead', 'tbody', 'blockquote', 'pre', 'code', 'img'],
          ALLOWED_ATTR: ['src', 'alt', 'colspan', 'rowspan'], ALLOW_DATA_ATTR: false,
        }) });
      } else {
        setFile((previous) => ({ ...previous, ...data }));
      }
    };
    request(url, { signal: controller.signal }, (response) => {
      if (Number(response.headers.get('Content-Length')) > PREVIEW_LIMIT) throw new Error(t('文件超过 15 MB，请下载查看。'));
      return response.arrayBuffer();
    }).then((buffer) => {
      if (controller.signal.aborted) return;
      if (buffer.byteLength > PREVIEW_LIMIT) throw new Error(t('文件超过 15 MB，请下载查看。'));
      timer.current = setTimeout(() => fail(t('预览处理超时，请下载查看。')), 12_000);
      instance.postMessage({ type, buffer }, [buffer]);
    }).catch((error) => { if (!controller.signal.aborted) fail(error.message); });
    return () => {
      controller.abort();
      clearTimeout(timer.current);
      instance.terminate();
    };
  }, [type, url]);

  function select(index) {
    if (index === selected) return;
    setSelected(index);
    worker.current?.postMessage({ index });
  }

  if (file.error) return <p className="feedback error" role="alert">{t(file.error)}</p>;
  if ('html' in file) return file.html
    ? <div className="document-content word-content" dangerouslySetInnerHTML={{ __html: file.html }} />
    : <p className="section-empty muted">{t('文档没有可预览的内容。')}</p>;
  if (!file.names) return <p className="section-empty muted" role="status">{t('正在打开预览…')}</p>;
  return <>
    <div className="sheet-tabs" role="group" aria-label={t('选择工作表')}>{file.names.map((name, index) =>
      <button key={name} type="button" aria-pressed={selected === index} onClick={() => select(index)}>{name}</button>)}</div>
    {selected === file.index && (file.rows.length ? <div className="document-content sheet-scroll" tabIndex={0} aria-label={file.names[selected]}>
      <table><tbody>{file.rows.map((row, index) => <tr key={index}>
        <th scope="row">{index + 1}</th>{row.map((value, column) => <td key={column}>{value}</td>)}
      </tr>)}</tbody></table>
    </div> : <p className="section-empty muted">{t('此工作表为空。')}</p>)}
    {selected === file.index && file.cropped && <p className="feedback muted">{t('预览最多显示前 500 行与 60 列，可下载查看完整内容。')}</p>}
  </>;
}

export default function DocumentPreview({ item, inline, download, onClose }) {
  const type = documentType(item.filename);
  const oversized = item.size_bytes > PREVIEW_LIMIT;
  const query = '(max-width: 700px), (pointer: coarse)';
  const [mobile, setMobile] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const media = window.matchMedia(query);
    const update = () => setMobile(media.matches);
    media.addEventListener('change', update);
    return () => media.removeEventListener('change', update);
  }, [query]);
  return <Modal id="document-preview" title={item.filename} className="detail-dialog preview-dialog document-dialog" onClose={onClose}>
    {oversized ? <p className="section-empty muted">{t('文件超过 15 MB，请下载查看。')}</p>
      : type === 'pdf' ? <>
        {mobile ? <p className="section-empty muted">{t('在新标签页打开 PDF。')}</p>
          : <iframe className="document-pdf" title={t('PDF 预览 {p1}', { p1: item.filename })} src={inline} referrerPolicy="no-referrer" />}
        <a className="button document-open" href={inline} target="_blank" rel="noopener noreferrer"><ExternalLink size={16} />{t('在新标签页打开')}</a>
      </> : <ParsedDocument url={download} type={type} />}
    <div className="document-actions"><a className="button" href={download} download><Download size={16} />{t('下载文件')}</a></div>
  </Modal>;
}
