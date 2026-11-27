import { useCallback, useEffect, useRef, useState } from 'react';
import { Search } from 'lucide-react';
import { request } from './api.js';
import { Button } from './ui.jsx';

function Field({ label, ...props }) {
  return <label className="host-setup-field"><span>{label}</span><input {...props} required /></label>;
}

export function setupWeatherPatch(data, timezoneOnly = false) {
  const health = { timezone: data.get('timezone').trim() };
  if (timezoneOnly) return { health };
  return {
    health,
    weather: { city_name: data.get('city_name').trim(), latitude: Number(data.get('latitude')), longitude: Number(data.get('longitude')) },
  };
}

export default function SetupWeatherFields({ weather, timezone, systemTimezone }) {
  const [fields, setFields] = useState(() => ({ ...weather, timezone }));
  const [matches, setMatches] = useState(null);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState('');
  const lookup = useRef(null);

  const search = useCallback(async (query) => {
    if (!query) return;
    lookup.current?.abort();
    const controller = new AbortController();
    lookup.current = controller;
    setSearching(true);
    setMatches(null);
    setSearchError('');
    try {
      const results = await request(`/api/setup/geocode?${new URLSearchParams({ q: query })}`, { signal: controller.signal });
      if (!controller.signal.aborted) setMatches(results);
    } catch (error) {
      if (!controller.signal.aborted) setSearchError(error.message);
    } finally {
      if (!controller.signal.aborted) setSearching(false);
    }
  }, []);

  useEffect(() => () => lookup.current?.abort(), []);

  function edit(key, value) {
    setFields((current) => ({ ...current, [key]: value }));
    if (key === 'city_name') {
      lookup.current?.abort();
      setMatches(null);
      setSearchError('');
      setSearching(false);
    }
  }

  return <>
    <div className="host-city-search">
      <Field label="城市名" name="city_name" value={fields.city_name} maxLength={100}
        onChange={(event) => edit('city_name', event.target.value)} onKeyDown={(event) => {
          if (event.key === 'Enter' && !event.nativeEvent.isComposing) {
            event.preventDefault();
            search(fields.city_name.trim());
          }
        }} />
      <Button disabled={searching || !fields.city_name.trim()} onClick={() => search(fields.city_name.trim())}>
        <Search size={16} aria-hidden="true" />{searching ? '正在搜索…' : '搜索匹配'}
      </Button>
    </div>
    {searchError && <p className="feedback error" role="alert">{searchError}</p>}
    {matches?.length === 0 && <p className="feedback" role="status">未找到匹配城市，可以换个名称或手动填写。</p>}
    {!!matches?.length && <ul className="host-city-matches" aria-label="城市匹配结果">
      {matches.map(({ name, admin1, country, timezone: cityTimezone, latitude, longitude }) => <li key={
        JSON.stringify([name, admin1, country, latitude, longitude])
      }>
        <Button onClick={() => {
          setFields({ city_name: name, latitude, longitude, timezone: cityTimezone });
          setMatches(null);
        }}>
          {[name, admin1, country, cityTimezone].filter(Boolean).join(' · ')}
        </Button>
      </li>)}
    </ul>}
    <div className="host-setup-grid">
      <Field label="IANA 时区" name="timezone" value={fields.timezone} onChange={(event) => edit('timezone', event.target.value)} />
      {systemTimezone && fields.timezone !== systemTimezone && <Button className="wizard-use-timezone"
        onClick={() => edit('timezone', systemTimezone)}>使用系统时区 · {systemTimezone}</Button>}
      <Field label="纬度" name="latitude" type="number" min="-90" max="90" step="any" value={fields.latitude}
        onChange={(event) => edit('latitude', event.target.value)} />
      <Field label="经度" name="longitude" type="number" min="-180" max="180" step="any" value={fields.longitude}
        onChange={(event) => edit('longitude', event.target.value)} />
    </div>
  </>;
}
