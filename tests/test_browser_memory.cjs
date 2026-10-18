const { test } = require('node:test');
const assert = require('node:assert/strict');
const { createFrameLedger, formatBytes } = require('../src/openmind/static/browser-memory.js');

test('replacement tracks current frame without accumulating frame storage', () => {
  const ledger = createFrameLedger();
  ledger.frame('blob:first', 100, 200);
  ledger.encodedSize('blob:first', ledger.generation, 5000);
  ledger.frame('blob:first', 100, 200);
  assert.equal(ledger.updates, 1);
  assert.equal(ledger.encoded, 5000);
  ledger.frame('blob:second', 50, 100);
  assert.equal(ledger.updates, 2);
  assert.equal(ledger.encoded, null);
  ledger.sample(null);
  assert.equal(ledger.history.at(-1), 50 * 100 * 4);
  assert.equal(ledger.heap, null);
});

test('late blob results cannot restore a released or replaced frame', () => {
  const ledger = createFrameLedger();
  ledger.frame('blob:first', 100, 200);
  const generation = ledger.generation;
  ledger.clear();
  ledger.encodedSize('blob:first', generation, 5000);
  assert.equal(ledger.encoded, null);
  assert.equal(ledger.width * ledger.height, 0);
  ledger.frame('blob:second', 50, 100);
  ledger.encodedSize('blob:first', generation, 5000);
  assert.equal(ledger.encoded, null);
  assert.equal(ledger.updates, 2);
});

test('history stays bounded and unavailable memory is distinct from zero', () => {
  const ledger = createFrameLedger();
  for (let i = 0; i < 200; i++) ledger.sample(i);
  assert.equal(ledger.history.length, 60);
  assert.equal(ledger.history[0], 140);
  ledger.sample(undefined);
  assert.equal(ledger.heap, null);
  assert.equal(formatBytes(null), 'Unavailable');
  assert.equal(formatBytes(0), '0 B');
  assert.equal(formatBytes(32768000), '31.25 MiB');
});
