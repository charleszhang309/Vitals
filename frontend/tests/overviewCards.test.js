import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readOverviewWidgets } from '../src/overviewCards.js';

test('Expenses starts disabled only for new overview preferences', () => {
  const defaults = readOverviewWidgets(null);
  assert.equal(defaults.expenses, false);
  assert.equal(defaults.weather, true);
  assert.equal(readOverviewWidgets({ theme: 'dark' }).expenses, true);
  assert.equal(readOverviewWidgets({ widgets: { weather: false, expenses: true } }).expenses, true);
  assert.equal(readOverviewWidgets({ widgets: { expenses: false } }).expenses, false);
});
