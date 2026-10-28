import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  attachDefaultInspectionPolicies,
  resolveAssistantInspectionCommand,
  runAssistantInspectionSequence,
  selectAssistantInspectionTargets,
} from "../src/modules/assistantInspection.js";

const diagnosisPageSource = readFileSync(
  new URL("../src/DiagnosisCenterPage.jsx", import.meta.url),
  "utf8",
);

test("assistant executes explicit all-database inspection commands", () => {
  assert.deepEqual(
    resolveAssistantInspectionCommand("帮我巡检一下所有数据库"),
    { action: "run_inspection", scope: "all" },
  );
  assert.deepEqual(
    resolveAssistantInspectionCommand("现在开始巡检全部实例"),
    { action: "run_inspection", scope: "all" },
  );
});

test("assistant runs an explicit inspection on the selected instance", () => {
  assert.deepEqual(
    resolveAssistantInspectionCommand("对数据库进行巡检，看看有什么问题"),
    { action: "run_inspection", scope: "selected" },
  );
});

test("assistant keeps navigation and report questions as navigation intents", () => {
  assert.equal(resolveAssistantInspectionCommand("打开一键巡检"), null);
  assert.equal(resolveAssistantInspectionCommand("查看所有数据库的巡检报告"), null);
  assert.equal(resolveAssistantInspectionCommand("所有数据库巡检进度怎么样"), null);
});

test("assistant selects only enabled supported database instances", () => {
  const targets = selectAssistantInspectionTargets(
    [
      { id: 1, db_type: "oracle", enabled: 1 },
      { id: 2, db_type: "mysql", enabled: 0 },
      { id: 3, db_type: "postgresql", enabled: true },
      { id: 4, db_type: "host", enabled: 1 },
    ],
    { scope: "all" },
  );
  assert.deepEqual(targets.map((item) => item.id), [1, 3]);
});

test("assistant leaves automatic template resolution to the backend for every database", () => {
  const targets = attachDefaultInspectionPolicies(
    [
      { id: 1, db_type: "oracle" },
      { id: 2, db_type: "mysql" },
      { id: 3, db_type: "postgresql" },
      { id: 4, db_type: "sqlserver" },
    ],
    [
      { policy_key: "daily_standard", name: "日常标准巡检", db_types: ["oracle", "mysql", "postgresql"], enabled: true },
      { policy_key: "oracle_standard", name: "Oracle 默认巡检模板", db_types: ["oracle"], enabled: true },
      { policy_key: "mysql_standard", name: "停用模板", db_types: ["mysql"], enabled: false },
    ],
  );

  assert.ok(targets.every((target) => target.inspectionPolicyKey === null));
  assert.ok(targets.every((target) => target.inspectionPolicyName === "自动匹配模板"));
});

test("assistant does not pin a snapshot of the user-configured default template", () => {
  const [target] = attachDefaultInspectionPolicies(
    [{ id: 1, db_type: "oracle" }],
    [
      { policy_key: "oracle_standard", name: "Oracle 默认巡检模板", db_types: ["oracle"], enabled: true },
      {
        policy_key: "custom_oracle_mes",
        name: "MES Oracle 巡检模板",
        db_types: ["oracle"],
        enabled: true,
        read_only: false,
        report_config: {
          assistant_default: true,
          assistant_default_db_types: ["oracle"],
        },
      },
    ],
  );

  assert.equal(target.inspectionPolicyKey, null);
  assert.equal(target.inspectionPolicyName, "自动匹配模板");
});

test("assistant does not replace a broken default with a client-side builtin fallback", () => {
  const [target] = attachDefaultInspectionPolicies(
    [{ id: 1, db_type: "mysql" }],
    [
      {
        policy_key: "custom_broken",
        name: "损坏的助手默认模板",
        db_types: ["mysql"],
        enabled: true,
        runtime_valid: false,
        report_config: {
          assistant_default: true,
          assistant_default_db_types: ["mysql"],
        },
      },
      {
        policy_key: "mysql_standard",
        name: "MySQL 默认巡检模板",
        db_types: ["mysql"],
        enabled: true,
        runtime_valid: true,
      },
    ],
  );
  assert.equal(target.inspectionPolicyKey, null);
});

test("coexisting Oracle scoped defaults are resolved by a fresh backend probe, independent of catalog order", async () => {
  const policies = ["cdb", "noncdb"].map((oracle_profile) => ({
    policy_key: `custom_${oracle_profile}`, db_types: ["oracle"], enabled: true,
    report_config: { oracle_profile, assistant_default: true, assistant_default_db_types: ["oracle"] },
  }));
  for (const catalog of [policies, [...policies].reverse()]) {
    const targets = attachDefaultInspectionPolicies([{ id: 8, db_type: "oracle" }], catalog);
    assert.equal(targets[0].inspectionPolicyKey, null);
    const result = await runAssistantInspectionSequence({
      targets,
      runInspection: async (_dbType, _instanceId, policyKey) => {
        assert.equal(policyKey, null);
        return { task: { task_id: "scoped", template_key: "custom_noncdb", template_name: "Non-CDB default" } };
      },
    });
    assert.equal(result.items[0].templateKey, "custom_noncdb");
  }
});

test("assistant keeps explicit custom keys unchanged even when the catalog is unavailable", async () => {
  for (const policies of [undefined, [{ policy_key: "custom_imported", name: "Imported Oracle" }]]) {
    const targets = attachDefaultInspectionPolicies([
      { id: 1, db_type: "oracle", inspectionPolicyKey: "custom_imported" },
    ], policies);
    assert.equal(targets[0].inspectionPolicyKey, "custom_imported");
    const calls = [];
    await runAssistantInspectionSequence({
      targets,
      runInspection: async (...args) => {
        calls.push(args);
        return { task: { task_id: "explicit-task" } };
      },
    });
    assert.deepEqual(calls, [["oracle", 1, "custom_imported"]]);
  }
});

test("automatic assistant progress shows automatic matching until the backend resolves the template", async () => {
  for (const inspectionPolicyKey of [undefined, null, "__auto__"]) {
    const progress = [];
    const result = await runAssistantInspectionSequence({
      targets: [{ id: 7, db_type: "oracle", inspectionPolicyKey, inspectionPolicyName: "Stale default" }],
      runInspection: async (dbType, instanceId, policyKey) => {
        assert.deepEqual([dbType, instanceId, policyKey], ["oracle", 7, null]);
        return { task: {
          task_id: "auto-task", template_key: "oracle_cdb", template_name: "Oracle CDB", template_revision: "latest",
        } };
      },
      onProgress: (state) => progress.push(state),
    });
    assert.equal(progress[0].items[0].templateName, "自动匹配模板");
    assert.equal(progress[1].items[0].templateName, "自动匹配模板");
    assert.equal(result.items[0].templateKey, "oracle_cdb");
    assert.equal(result.items[0].templateName, "Oracle CDB");
    assert.equal(result.items[0].templateRevision, "latest");
  }
});

test("natural-language inspection requests use model dispatch instead of the legacy browser loop", () => {
  const runBlock = diagnosisPageSource.slice(diagnosisPageSource.indexOf("const runDiagnosis = async"), diagnosisPageSource.indexOf("const createTicket ="));
  assert.match(runBlock, /resolveCustomerRequirement/);
  assert.doesNotMatch(runBlock, /runAssistantInspection|resolveAssistantInspectionCommand|runInspectionTask/);
});

test("assistant runs inspections sequentially and continues after an instance failure", async () => {
  const targets = [
    { id: 1, instance_name: "orcl", db_type: "oracle", host: "10.0.0.1", port: 1521, enabled: 1 },
    { id: 2, instance_name: "mysql", db_type: "mysql", host: "10.0.0.2", port: 3306, enabled: 1 },
    { id: 3, instance_name: "pg", db_type: "postgresql", host: "10.0.0.3", port: 5432, enabled: 1 },
  ];
  let active = 0;
  let maxActive = 0;
  const calls = [];
  const progress = [];

  const result = await runAssistantInspectionSequence({
    targets,
    runInspection: async (dbType, instanceId) => {
      calls.push([dbType, instanceId]);
      active += 1;
      maxActive = Math.max(maxActive, active);
      await new Promise((resolve) => setTimeout(resolve, 5));
      active -= 1;
      if (instanceId === 2) throw new Error("connection failed");
      return {
        task: {
          task_id: `task-${instanceId}`,
          task_status: "success",
          overall_score: 96,
          risk_level: "normal",
          summary: "未发现明显风险",
        },
      };
    },
    onProgress: (state) => progress.push(state),
  });

  assert.deepEqual(calls, [["oracle", 1], ["mysql", 2], ["postgresql", 3]]);
  assert.equal(maxActive, 1);
  assert.equal(result.status, "partial");
  assert.equal(result.completed, 3);
  assert.equal(result.succeeded, 2);
  assert.equal(result.failed, 1);
  assert.equal(result.items[1].error, "connection failed");
  assert.equal(progress.at(-1).status, "partial");
});

test("assistant treats collector failure task metadata as a failed inspection", async () => {
  const result = await runAssistantInspectionSequence({
    targets: [
      {
        id: 1,
        instance_name: "orcl",
        db_type: "oracle",
        host: "10.0.0.1",
        port: 1521,
        inspectionPolicyKey: "custom_oracle_mes",
        inspectionPolicyName: "MES Oracle 巡检模板",
      },
    ],
    runInspection: async () => ({
      task: {
        task_id: "failed-task",
        task_status: "failed",
        failure_reason: "cannot connect to database",
        overall_score: null,
        template_key: "custom_oracle_mes",
        template_name: "MES Oracle 巡检模板",
        template_revision: "abc123",
      },
    }),
  });

  assert.equal(result.status, "failed");
  assert.equal(result.succeeded, 0);
  assert.equal(result.failed, 1);
  assert.equal(result.items[0].status, "failed");
  assert.equal(result.items[0].error, "cannot connect to database");
  assert.equal(result.items[0].templateRevision, "abc123");
});
