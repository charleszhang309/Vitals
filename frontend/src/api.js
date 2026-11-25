export function httpUrl(path) {
  return new URL(path, `${window.location.protocol}//${window.location.host}`);
}

export function metricsUrl() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${window.location.host}/ws/metrics`;
}

export async function fetchResponse(path, options) {
  let response;
  try {
    response = await fetch(httpUrl(path), { cache: 'no-store', ...options });
  } catch (error) {
    if (error.name === 'AbortError') throw error;
    throw new Error('无法连接控制中心，请检查网络后重试。');
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const detail = typeof body?.detail === 'string' ? body.detail : `请求失败（${response.status}）`;
    throw new Error(detail);
  }
  return response;
}

export async function request(path, options) {
  const response = await fetchResponse(path, options);
  return response.status === 204 ? null : response.json();
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
