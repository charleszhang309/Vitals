import assert from 'node:assert/strict';
import test from 'node:test';
import { controlDevice, deckTarget, reportedDevices } from '../src/controlDevice.js';

const hub = { device_id: 'hub', name: 'Hub' };
const remote = { device_id: 'mac', name: 'Mac', online: true,
  snapshot: { name: 'Mac', media: { title: 'Remote track' }, volume: { percent: 23 } } };

test('a failed agent poll preserves the remote target and snapshot with commands disabled until recovery', () => {
  const report = { devices: [remote] };
  const aliasData = { aliases: { mac: 'Studio' } };
  for (const error of ['', 'timeout', '']) {
    const devices = reportedDevices(report, aliasData, error);
    const selected = controlDevice('mac', devices);
    assert.equal(selected.name, 'Studio');
    assert.equal(selected.snapshot, remote.snapshot);
    assert.equal(selected.online, !error);
    assert.equal(selected.statusUnknown, Boolean(error));
    assert.equal(deckTarget('mac', hub, devices), selected);
  }
});

test('unresolved agents stay selected and only a confirmed offline Deck target falls back to the hub', () => {
  for (const devices of [reportedDevices(null, null, 'timeout'), reportedDevices({ devices: [] }, null, '')]) {
    for (const device of [controlDevice('mac', devices), deckTarget('mac', hub, devices)]) {
      assert.equal(device.device_id, 'mac');
      assert.equal(device.online, false);
      assert.equal(device.statusUnknown, true);
    }
  }
  const devices = reportedDevices({ devices: [{ ...remote, online: false }] }, null, '');
  assert.equal(controlDevice('mac', devices).statusUnknown, false);
  assert.equal(deckTarget('mac', hub, devices), hub);
  assert.equal(deckTarget('hub', hub, devices), hub);
  assert.equal(controlDevice('hub', devices), null);
});
