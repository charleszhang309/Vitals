const form = document.querySelector('form');
const remote = document.getElementById('remote');
const description = document.getElementById('description');
const feedback = document.getElementById('feedback');
const url = document.getElementById('url');
const token = document.getElementById('token');
const save = form.querySelector('button');
let storedUrl = '';
const chinese = new URLSearchParams(location.search).get('language') === 'zh-CN';
document.documentElement.lang = chinese ? 'zh-CN' : 'en';
if (chinese) {
  document.title = 'Vitals 桌面设置';
  const labels = {
    heading: '桌面模式',
    'role-legend': '选择运行方式',
    'hub-label': '主机（Hub）',
    'remote-label': '远程（Remote）',
    'url-label': '主机 HTTPS 地址',
    'token-label': '配对令牌',
    save: '保存并应用',
  };
  for (const [id, label] of Object.entries(labels)) {
    document.getElementById(id).textContent = label;
  }
  remote.setAttribute('aria-label', '远程主机连接');
}

const messages = chinese ? {
  hub: '在本机运行 Vitals 服务并保存数据。',
  remote: '打开主机上的完整 Vitals 网页。macOS 同时运行本机设备代理。',
  saved: '已保存，留空可继续使用',
  needsConfiguration: '请重新选择并保存桌面模式。',
  readError: '无法读取桌面设置，请重新打开此窗口。',
  saveError: '无法保存桌面设置。',
} : {
  hub: 'Run the Vitals service and keep data on this computer.',
  remote: 'Open the full Vitals dashboard from the hub. On macOS, the Mac agent also runs locally.',
  saved: 'Saved. Leave blank to keep it.',
  needsConfiguration: 'Choose your desktop mode again and save it.',
  readError: 'Could not read desktop settings. Reopen this window and try again.',
  saveError: 'Could not save desktop settings.',
};

function update() {
  const useRemote = form.elements.role.value === 'remote';
  remote.hidden = remote.disabled = !useRemote;
  description.textContent = useRemote ? messages.remote : messages.hub;
  let sameServer = false;
  try {
    sameServer = Boolean(storedUrl) && new URL(url.value).href === storedUrl;
  } catch {
    // An incomplete address cannot use the saved token.
  }
  token.required = useRemote && !sameServer;
  token.placeholder = sameServer ? messages.saved : '';
}

form.addEventListener('input', update);
form.addEventListener('submit', async (event) => {
  event.preventDefault();
  feedback.textContent = '';
  save.disabled = true;
  try {
    await window.__TAURI__.core.invoke('save_desktop_settings', {
      role: form.elements.role.value,
      url: url.value,
      token: token.value,
    });
    token.value = '';
  } catch (error) {
    feedback.textContent = typeof error === 'string' ? error : messages.saveError;
  } finally {
    save.disabled = false;
  }
});

async function load() {
  try {
    const settings = await window.__TAURI__.core.invoke('read_desktop_settings');
    form.elements.role.value = settings.role ?? 'hub';
    url.value = settings.url;
    storedUrl = settings.has_token ? settings.url : '';
    if (!settings.role) feedback.textContent = messages.needsConfiguration;
    update();
    save.disabled = false;
  } catch {
    feedback.textContent = messages.readError;
  }
}

load();
