import assert from "node:assert/strict";
import test from "node:test";

import {
  connectionTestIsCurrent,
  discoveryError,
  effectiveHealthStatus,
  failedConnectionAfterDiscovery,
  successfulConnectionAfterDiscovery,
} from "../src/AssetConnectionState.js";

const item = (overrides = {}) => ({
  status: "healthy",
  last_seen_at: "2026-07-24T10:00:00",
  attributes: {
    discovery_status: "success",
    ...overrides,
  },
});

test("a newer failed manual test overrides an earlier successful discovery", () => {
  const value = item({
    connection_test_status: "failed",
    connection_test_error: "ORA-01033",
    last_connection_test_at: "2026-07-24T10:01:00",
  });

  assert.equal(connectionTestIsCurrent(value), true);
  assert.equal(failedConnectionAfterDiscovery(value), true);
  assert.equal(successfulConnectionAfterDiscovery(value), false);
  assert.equal(discoveryError(value), "ORA-01033");
  assert.equal(effectiveHealthStatus(value), "warning");
});

test("a newer successful manual test marks failed discovery as recovered but pending refresh", () => {
  const value = item({
    discovery_status: "failed",
    discovery_error: "cached connection failure",
    connection_test_status: "success",
    last_connection_test_at: "2026-07-24T10:01:00",
  });

  assert.equal(connectionTestIsCurrent(value), true);
  assert.equal(successfulConnectionAfterDiscovery(value), true);
  assert.equal(failedConnectionAfterDiscovery(value), false);
  assert.equal(discoveryError(value), "");
  assert.equal(effectiveHealthStatus(value), "pending_confirmation");
});

test("a newer automatic discovery supersedes an older failed manual test", () => {
  const value = item({
    connection_test_status: "failed",
    connection_test_error: "old failure",
    last_connection_test_at: "2026-07-24T09:59:00",
  });

  assert.equal(connectionTestIsCurrent(value), false);
  assert.equal(failedConnectionAfterDiscovery(value), false);
  assert.equal(discoveryError(value), "");
  assert.equal(effectiveHealthStatus(value), "healthy");
});
