import { test } from 'node:test';
import assert from 'node:assert/strict';
import { catalogEpoch } from '../src/modules/catalogEpoch.js';

test('new permission, instance or refresh epoch discards a late response', () => {
  const gate = catalogEpoch();
  const old = gate.next();
  const current = gate.next();
  assert.equal(old(), false);
  assert.equal(current(), true);
  gate.close();
  assert.equal(current(), false);
});
