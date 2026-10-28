import assert from "node:assert/strict";
import test from "node:test";

import {
  OBSERVABILITY_NAV_GROUPS,
  findObservabilityGroup,
} from "../src/modules/observabilityNavigation.js";

const expectedSections = [
  "overview",
  "dataQuality",
  "linkageHealth",
  "sources",
  "bindings",
  "zabbix",
  "prometheus",
  "alertmanager",
  "grafana",
  "exporters",
  "skywalking",
  "nginx",
  "tomcat",
  "elasticsearch",
  "policies",
  "tasks",
  "logs",
  "mappings",
  "currentMetrics",
  "metricHistory",
  "alerts",
  "logAnalysis",
  "faultTimeline",
  "apmTrace",
  "sqlTrace",
];

test("observability navigation groups every function into five domains", () => {
  const sectionKeys = OBSERVABILITY_NAV_GROUPS.flatMap(
    (group) => group.items.map((item) => item.key),
  );
  assert.deepEqual(
    OBSERVABILITY_NAV_GROUPS.map((group) => group.key),
    ["overview", "sources", "integrations", "collection", "metrics"],
  );
  assert.deepEqual(sectionKeys.sort(), expectedSections.sort());
  assert.equal(new Set(sectionKeys).size, expectedSections.length);
});

test("observability sections resolve to their owning group", () => {
  assert.equal(findObservabilityGroup("overview")?.key, "overview");
  assert.equal(findObservabilityGroup("dataQuality")?.key, "overview");
  assert.equal(findObservabilityGroup("prometheus")?.key, "integrations");
  assert.equal(findObservabilityGroup("tasks")?.key, "collection");
  assert.equal(findObservabilityGroup("metricHistory")?.key, "metrics");
  assert.equal(findObservabilityGroup("missing"), null);
});
