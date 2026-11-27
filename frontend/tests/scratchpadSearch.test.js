import assert from 'node:assert/strict';
import test from 'node:test';
import { filterScratchpadItems } from '../src/scratchpadSearch.js';

const records = [
  { id: 1, kind: 'text', text: `${'x'.repeat(200)}\nOld CAFÉ invoice`, pinned: true },
  { id: 3, kind: 'file', filename: 'Cafe\u0301 Receipt.pdf', pinned: false },
  { id: 2, kind: 'text', text: 'A different note', pinned: false },
];

test('search matches full text, filenames and the displayed title while preserving pinned-first order', () => {
  const ids = (query) => filterScratchpadItems(records, query, '文本').map(({ id }) => id);
  assert.deepEqual(ids('  café  '), [1, 3]);
  assert.deepEqual(ids('receipt.PDF'), [3]);
  assert.deepEqual(ids('文本'), [1, 2]);
  assert.deepEqual(ids('different'), [2]);
  assert.deepEqual(ids('unknown'), []);
  assert.deepEqual(ids('   '), [1, 3, 2]);
});
