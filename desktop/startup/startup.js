const button = document.getElementById('start');
const status = document.getElementById('status');
const messages = {
  starting: '正在等待本机服务。',
  stopped: '本机服务已停止。',
  failed: '本机服务启动失败，请重试。',
  occupied: '服务端口已被占用，释放端口后可重试。',
};

function show(message, starting = false) {
  status.textContent = message;
  button.disabled = starting;
  button.textContent = starting ? '正在启动…' : '启动 Vitals 服务';
}

const current = new URLSearchParams(location.search).get('status');
const initial = current && Object.hasOwn(messages, current) ? current : 'starting';
show(messages[initial], initial === 'starting');

button.addEventListener('click', async () => {
  show(messages.starting, true);
  try {
    await window.__TAURI__.core.invoke('start_service');
  } catch {
    show('无法启动服务，请重试或从系统托盘启动。');
  }
});
