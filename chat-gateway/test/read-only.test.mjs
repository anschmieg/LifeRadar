import assert from 'node:assert/strict';
import test from 'node:test';

import { READ_ONLY_ERROR_MESSAGE, rejectOutboundMessage } from '../src/read-only.mjs';

test('direct connector send guard rejects every outbound attempt', () => {
  const error = rejectOutboundMessage();

  assert.equal(error.message, READ_ONLY_ERROR_MESSAGE);
  assert.equal(error.code, 'read_only');
  assert.equal(error.statusCode, 403);
});
