import { getLocale, t } from './i18n/messages.js';

export function httpUrl(path) {
  return new URL(path, `${window.location.protocol}//${window.location.host}`);
}

export function metricsUrl() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${window.location.host}/ws/metrics`;
}

export async function request(path, options = {}, read = (response) => response.status === 204 ? null : response.json()) {
  const isUpload = options.body instanceof FormData || options.body instanceof Blob;
  const { signal, timeoutMs = isUpload ? 180_000 : 30_000, ...init } = options;
  const headers = new Headers(init.headers);
  if (!headers.has('Accept-Language')) headers.set('Accept-Language', getLocale());
  const controller = new AbortController();
  const cancel = () => controller.abort();
  if (signal?.aborted) cancel();
  else signal?.addEventListener('abort', cancel, { once: true });
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    let response;
    try {
      response = await fetch(httpUrl(path), { cache: 'no-store', ...init, headers, signal: controller.signal });
    } catch (error) {
      if (controller.signal.aborted) throw error;
      throw new Error(t('无法连接控制中心，请检查网络后重试。'));
    }
    if (!response.ok) {
      const body = await response.json().catch(() => null);
      const detail = typeof body?.detail === 'string' ? t(body.detail) : t('请求失败（{p1}）', { p1: response.status });
      throw new Error(detail);
    }
    return await read(response);
  } catch (error) {
    if (signal?.aborted) throw new DOMException(t('请求已取消。'), 'AbortError');
    if (controller.signal.aborted) throw new Error(t('控制中心响应超时，请稍后重试。'));
    throw error;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', cancel);
  }
}

export function postJson(path, payload) {
  return sendJson('POST', path, payload);
}

export function sendJson(method, path, payload) {
  return request(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}
