const button = document.getElementById('start');
const settings = document.getElementById('settings');
const status = document.getElementById('status');
const params = new URLSearchParams(location.search);
const remote = params.get('mode') === 'remote';
const messages = {
  starting: remote ? '正在打开远程 Vitals。' : '正在等待本机服务。',
  stopped: remote ? '远程模式已停止。' : '本机服务已停止。',
  failed: '本机服务启动失败，请重试。',
  occupied: '服务端口已被占用，释放端口后可重试。',
  unconfigured: '请检查桌面模式设置。',
};

function show(message, starting = false) {
  status.textContent = message;
  button.disabled = starting;
  button.textContent = starting ? '正在启动…' : remote ? '启动远程模式' : '启动 Vitals 服务';
}

const current = params.get('status');
const initial = current && Object.hasOwn(messages, current) ? current : 'starting';
show(messages[initial], initial === 'starting');
button.hidden = initial === 'unconfigured';

button.addEventListener('click', async () => {
  show(messages.starting, true);
  try {
    await window.__TAURI__.core.invoke('start_service');
  } catch {
    show('无法启动服务，请重试或从系统托盘启动。');
  }
});

settings.addEventListener('click', async () => {
  try {
    await window.__TAURI__.core.invoke('open_desktop_settings');
  } catch {
    status.textContent = '无法打开桌面设置，请从系统托盘重试。';
  }
});
