import { readFileSync } from 'node:fs';
import { decryptQrcHex } from '@applemusic-like-lyrics/lyric';

const payload = JSON.parse(readFileSync(0, 'utf8'));
const decoded = {};
for (const key of ['original', 'translation']) {
  const value = payload[key]?.trim();
  if (!value || value.length > 192_000) continue;
  try {
    const hex = /^(?:[a-f\d]{2})+$/i.test(value) ? value : Buffer.from(value, 'base64').toString('hex');
    decoded[key] = decryptQrcHex(hex);
  } catch {
    // The server can still use QQ's LRC when the optional QRC track is malformed.
  }
}
process.stdout.write(JSON.stringify(decoded));
