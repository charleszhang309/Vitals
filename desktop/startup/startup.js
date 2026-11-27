const button = document.getElementById('start');
const settings = document.getElementById('settings');
const status = document.getElementById('status');
const params = new URLSearchParams(location.search);
const remote = params.get('mode') === 'remote';
const chinese = params.get('language') === 'zh-CN';
document.documentElement.lang = chinese ? 'zh-CN' : 'en';
const messages = chinese ? {
  starting: remote ? '正在打开远程 Vitals。' : '正在等待本机服务。',
  stopped: remote ? '远程模式已停止。' : '本机服务已停止。',
  failed: '本机服务启动失败，请重试。',
  occupied: '服务端口已被占用，释放端口后可重试。',
  unconfigured: '请检查桌面模式设置。',
  startingButton: '正在启动…',
  startButton: remote ? '启动远程模式' : '启动 Vitals 服务',
  settings: '配置桌面模式…',
  startError: '无法启动服务，请重试或从系统托盘启动。',
  settingsError: '无法打开桌面设置，请从系统托盘重试。',
} : {
  starting: remote ? 'Opening remote Vitals.' : 'Waiting for the local service.',
  stopped: remote ? 'Remote mode is stopped.' : 'The local service is stopped.',
  failed: 'The local service could not start. Try again.',
  occupied: 'The service port is in use. Free the port and try again.',
  unconfigured: 'Check the desktop mode settings.',
  startingButton: 'Starting…',
  startButton: remote ? 'Start remote mode' : 'Start Vitals service',
  settings: 'Configure desktop mode…',
  startError: 'Could not start the service. Try again or start it from the tray menu.',
  settingsError: 'Could not open desktop settings. Try again from the tray menu.',
};

function show(message, starting = false) {
  status.textContent = message;
  button.disabled = starting;
  button.textContent = starting ? messages.startingButton : messages.startButton;
}

const current = params.get('status');
const initial = ['starting', 'stopped', 'failed', 'occupied', 'unconfigured'].includes(current)
  ? current : 'starting';
show(messages[initial], initial === 'starting');
button.hidden = initial === 'unconfigured';
settings.textContent = messages.settings;

button.addEventListener('click', async () => {
  show(messages.starting, true);
  try {
    await window.__TAURI__.core.invoke('start_service');
  } catch {
    show(messages.startError);
  }
});

settings.addEventListener('click', async () => {
  try {
    await window.__TAURI__.core.invoke('open_desktop_settings');
  } catch {
    status.textContent = messages.settingsError;
  }
});
