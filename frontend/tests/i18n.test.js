import assert from 'node:assert/strict';
import test from 'node:test';
import { localizedDate, resolveLocale, setLocale, t, translate, validPreference } from '../src/i18n/messages.js';
import { request } from '../src/api.js';

test('auto uses the preferred supported browser language and manual selection overrides it', () => {
  assert.equal(resolveLocale('auto', ['fr-CA', 'zh-Hant-TW', 'en-CA']), 'zh-CN');
  assert.equal(resolveLocale('auto', ['en-GB', 'zh-CN']), 'en-US');
  assert.equal(resolveLocale('auto', ['de-DE']), 'en-US');
  assert.equal(resolveLocale('zh-CN', ['en-CA']), 'zh-CN');
  assert.equal(validPreference('invalid'), 'auto');
});

test('translations interpolate parameters and retain Chinese source text as a fallback', () => {
  assert.equal(translate('en-US', '未来 24 小时'), 'Next 24 Hours');
  assert.equal(translate('en-US', '查看{p1}详情', { p1: 'CPU' }), 'View CPU details');
  assert.equal(translate('zh-CN', '查看{p1}详情', { p1: 'CPU' }), '查看CPU详情');
  assert.equal(translate('en-US', 'A custom name'), 'A custom name');
});

test('an existing formatter and message reader pick up a language change', () => {
  const date = localizedDate({ month: 'short', weekday: 'short', timeZone: 'UTC' });
  const time = Date.UTC(2026, 8, 7);
  setLocale('zh-CN');
  assert.equal(t('未来 24 小时'), '未来 24 小时');
  assert.match(date.format(time), /周一|星期一/);
  setLocale('en-US');
  assert.equal(t('未来 24 小时'), 'Next 24 Hours');
  assert.match(date.format(time), /Mon/);
  assert.match(date.format(time), /Sep/);
});

test('requests send the selected language and translate API errors', async (context) => {
  setLocale('en-US');
  const originalWindow = globalThis.window;
  globalThis.window = { location: { protocol: 'https:', host: 'fixture.example' } };
  context.after(() => { globalThis.window = originalWindow; });
  context.mock.method(globalThis, 'fetch', async (url, { headers }) => {
    assert.equal(url.href, 'https://fixture.example/api/setup/config');
    assert.equal(headers.get('Accept-Language'), 'en-US');
    return Response.json({ detail: '配置请求过大' }, { status: 413 });
  });
  await assert.rejects(request('/api/setup/config'), /configuration request is too large/);
});
