const form = document.querySelector('form');
const remote = document.getElementById('remote');
const description = document.getElementById('description');
const feedback = document.getElementById('feedback');
const url = document.getElementById('url');
const token = document.getElementById('token');
const save = form.querySelector('button');
let storedUrl = '';

function update() {
  const useRemote = form.elements.role.value === 'remote';
  remote.hidden = remote.disabled = !useRemote;
  description.textContent = useRemote
    ? '打开主机上的完整 Vitals 网页。macOS 同时运行本机设备代理。'
    : '在本机运行 Vitals 服务并保存数据。';
  let sameServer = false;
  try {
    sameServer = Boolean(storedUrl) && new URL(url.value).href === storedUrl;
  } catch {
    // An incomplete address cannot use the saved token.
  }
  token.required = useRemote && !sameServer;
  token.placeholder = sameServer ? '已保存，留空可继续使用' : '';
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
    feedback.textContent = typeof error === 'string' ? error : '无法保存桌面设置。';
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
    if (!settings.role) feedback.textContent = '请重新选择并保存桌面模式。';
    update();
    save.disabled = false;
  } catch {
    feedback.textContent = '无法读取桌面设置，请重新打开此窗口。';
  }
}

load();
