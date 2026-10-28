const test = require("node:test");
const assert = require("node:assert/strict");
const { backendRequestTimeout } = require("../backendProxyTimeout.cjs");

test("only exact model analysis POST routes receive a longer bounded wait", () => {
  for (const url of ["/api/v1/slow-sql/oracle/1/analyze", "/api/v1/slow-sql/12/analyze?sample=1"]) {
    assert.equal(backendRequestTimeout({ method: "POST", url }, 30000), 190000);
    assert.equal(backendRequestTimeout({ method: "GET", url }, 30000), 30000);
  }
  for (const url of ["/health/ready", "/api/v1/slow-sql/collect/1", "/api/v1/slow-sql/oracle/1/analyze/nope", "/api/v1/reports/catalog"]) {
    assert.equal(backendRequestTimeout({ method: "POST", url }, 30000), 30000);
  }
  assert.equal(backendRequestTimeout({ method: "POST", url: "/api/v1/slow-sql/1/analyze" }, 240000), 240000);
});
