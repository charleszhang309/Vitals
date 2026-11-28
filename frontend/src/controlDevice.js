export const HUB_DEVICE_ID = 'hub';

export function reportedDevices(report, aliasData, queryError) {
  return (report?.devices ?? []).map((item) => ({
    ...item,
    name: aliasData?.aliases?.[item.device_id] ?? (aliasData ? item.snapshot.name : item.name),
    online: item.online && !queryError,
    statusUnknown: Boolean(queryError) || typeof item.online !== 'boolean',
  }));
}

export function controlDevice(id, devices, aliases = {}) {
  if (id === HUB_DEVICE_ID) return null;
  return devices.find((item) => item.device_id === id)
    ?? { device_id: id, name: aliases[id] ?? id, online: false, statusUnknown: true };
}

export function deckTarget(id, hub, devices) {
  const selected = controlDevice(id, devices);
  return selected && (selected.online || selected.statusUnknown) ? selected : hub;
}
