import assert from 'node:assert/strict';
import test from 'node:test';
import { deckSlots, fitsDeckGrid } from '../src/deckLayout.js';

test('wide keys reserve one cell while action slots keep their stored indices', () => {
  const scene = { grid: '3x2', tiles: [{ span: 2 }, null, { title: 'Run' }] };
  assert.deepEqual(deckSlots(scene), [0, 2, 3, 4, 5]);
  assert.equal(fitsDeckGrid(scene), true);
});

test('moves and grid changes cannot overlap tiles, split a wide key or discard occupied slots', () => {
  assert.equal(fitsDeckGrid({ grid: '3x2', tiles: [{ span: 2 }, {}] }), false);
  assert.equal(fitsDeckGrid({ grid: '4x3', tiles: [null, null, { span: 2 }] }, '3x2'), false);
  assert.equal(fitsDeckGrid({ grid: '4x3', tiles: Array.from({ length: 8 }, () => null).concat({}) }, '3x2'), false);
  assert.equal(fitsDeckGrid({ grid: '4x3', tiles: Array(9).fill(null) }, '3x2'), true);
});
