import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import { parse } from "@babel/parser";
import { inspectionOutcome, inspectionNotice } from "../src/InspectionExecutionState.js";

const source = readFileSync(new URL("../src/InspectionPages.jsx", import.meta.url), "utf8");
const ast = parse(source, { sourceType: "module", plugins: ["jsx"] });
const topLevel = ast.program.body.map((node) => node.declaration || node);
const snippet = (node) => source.slice(node.start, node.end);
const helpers = [
  "AUTO_TEMPLATE_KEY", "ORACLE_PROFILE_OPTIONS", "TEMPLATE_ORDER", "REPORT_OPTION_KEYS",
  "TEMPLATE_RUNTIME_REPORT_KEYS", "INSPECTION_POLICIES_CHANGED_EVENT", "INSPECTION_POLICIES_CHANGED_STORAGE_KEY",
  "isLegacyTemplate", "visibleTemplateCatalog", "hasPolicyConfiguration", "oracleProfileText", "oracleCollectionText",
  "toArray", "omitKeys", "jsonText", "parseJsonField", "reportExtraText", "reportOptionsForForm", "buildReportConfig",
  "subscribeInspectionPoliciesChanged",
  "normalizeTemplateItem", "templateItemsForForm", "cleanTemplateItems", "normalizeRiskLevel", "isEnabledValue",
  "isCustomSqlMetric", "hasValue", "defaultTemplateItem", "nextTemplateItemKey", "templateItemSequence",
];
const helperSource = topLevel.filter((node) => helpers.includes(node.id?.name)
  || node.declarations?.some((item) => helpers.includes(item.id.name))).map(snippet).join("\n");
const api = vm.runInNewContext(`${helperSource}\n({ ${helpers.join(",")} });`);
const plain = (value) => JSON.parse(JSON.stringify(value));
const component = (name) => topLevel.find((node) => node.id?.name === name);
const handler = (componentName, name) => component(componentName).body.body
  .flatMap((node) => node.declarations || []).find((node) => node.id.name === name).init;

test("native Oracle bindings survive cloned form load, alias editing and repeated cleaning", () => {
  const items = [
    { key: "oracle.tablespace.used_pct", source_key: "oracle.query:oracle.tablespace.used_pct", name: "表空间使用率", collect_method: "内置 SQL 采集" },
    { key: "oracle.script:tablespace_usage", source_key: "oracle.script:tablespace_usage", name: "表空间使用情况", collect_method: "内置脚本 SQL 章节",
      script_name: "dbcheck_cdb.sql", script_section: "tablespace_usage", script_line: 123,
      sql: "select tablespace_name\nfrom dba_tablespaces" },
  ];
  const original = structuredClone(items);
  const loaded = api.templateItemsForForm(structuredClone(items));
  const edited = loaded.map((item, index) => ({ ...item, key: `custom.alias_${index}`, name: `${item.name} 副本` }));
  const saved = api.cleanTemplateItems(api.templateItemsForForm(api.cleanTemplateItems(edited)));
  for (const [index, item] of saved.entries()) {
    assert.equal(item.source_key, items[index].source_key);
    assert.equal(item.key, `custom.alias_${index}`);
    for (const field of ["script_name", "script_section", "script_line", "sql"]) {
      if (Object.hasOwn(items[index], field)) assert.equal(item[field], items[index][field]);
    }
  }
  assert.deepEqual(items, original);
  assert.equal(api.cleanTemplateItems([{ key: "legacy", name: "Legacy" }])[0].source_key, "legacy");
  for (const field of ["source_key", "script_name", "script_section", "script_line"]) {
    assert.ok(source.includes(`name={[name, "${field}"]} hidden`));
  }
  assert.match(snippet(handler("InspectionPolicyEditor", "save")), /cleanTemplateItems\(values.item_config\)/);
});

test("visible catalog lists the three Oracle variants first and hides legacy without mutating configuration", () => {
  const keys = ["custom_reusable", "daily_standard", "host_windows", "host_resource", "sqlserver_standard", "oracle_cdb", "mysql_standard", "oracle_standard", "oracle_noncdb", "postgresql_standard", "oracle_10g"];
  const rows = keys.map((policy_key) => ({ policy_key, db_types: ["oracle"] }));
  rows.push({ policy_key: "retired", legacy_template: true });
  const visible = api.visibleTemplateCatalog(rows);
  assert.deepEqual(plain(visible.map((row) => row.policy_key)), [
    "oracle_10g", "oracle_noncdb", "oracle_cdb", "mysql_standard", "postgresql_standard", "sqlserver_standard", "host_resource", "host_windows", "daily_standard", "custom_reusable",
  ]);
  assert.equal(rows.length, 12);
  assert.equal(visible.at(-1), rows[0]);
  assert.equal(api.oracleProfileText({}), "自动检测");
  assert.equal(api.oracleProfileText({ report_config: { oracle_profile: "noncdb" } }), "Non-CDB");
});

test("Oracle collection counts distinguish SQL sections and health queries from categories and execution units", () => {
  for (const sections of [53, 56]) {
    const row = { runtime_summary: { by_db_type: { oracle: {
      script_section_count: sections, health_item_count: 19, health_query_count: 22, execution_unit_count: 999,
    } } } };
    assert.equal(api.oracleCollectionText(row), `数据 SQL 章节 ${sections} · 健康指标 19`);
  }
  assert.equal(api.oracleCollectionText({}), "");
  assert.equal(api.oracleCollectionText({ runtime_summary: { by_db_type: { oracle: { execution_unit_count: 75 } } } }), "");
  assert.equal(api.oracleCollectionText({ runtime_summary: { by_db_type: { oracle: { script_section_count: 0, health_item_count: 0, health_query_count: 22 } } } }), "数据 SQL 章节 0 · 健康指标 0");
  assert.equal(api.oracleCollectionText({ runtime_summary: { by_db_type: { oracle: { script_section_count: 56, health_query_count: 22 } } } }), "");
  for (const page of ["InspectionPolicyPage", "InspectionTemplatePage"]) {
    assert.match(snippet(component(page)), /配置检查项/);
    assert.match(snippet(component(page)), /oracleCollectionText\(row\)/);
  }
  assert.match(source, /item_count: toArray\(row.item_config\)\.length,/);
  assert.match(source, /tableLayout="fixed"/);
  assert.match(source, /<LongText value=\{templateItemHelp\(item\)\}/);
});

test("host edits preserve platform, source script and extra settings without an Oracle profile control", () => {
  for (const [host_platform, script_name] of [["windows", "oscheck.ps1"], ["unix", "oscheck.sh"]]) {
    const initial = { host_platform, script_name, custom_host_option: { timeout: 300 } };
    const config = api.buildReportConfig({
      db_types: ["host"], report_extra_text: api.reportExtraText(initial),
    }, initial, ["host"]);
    for (const key of Object.keys(initial)) assert.deepEqual(plain(config[key]), initial[key]);
    assert.equal(Object.hasOwn(config, "oracle_profile"), false);
  }
  assert.doesNotMatch(source, /name="host_platform"/);
});

test("legacy policies remain manageable only when assigned, scheduled or configured as a default", () => {
  assert.equal(api.hasPolicyConfiguration({ schedule_type: "manual" }), false);
  assert.equal(api.hasPolicyConfiguration({ report_config: { assistant_default_db_types: [] } }), false);
  for (const row of [
    { instance_ids: [12] },
    { schedule_type: "weekly" },
    { report_config: { instance_ids: [12] } },
    { report_config: { assistant_default: true } },
    { report_config: { assistant_default_db_types: ["oracle"] } },
    { assistant_default: true },
  ]) assert.equal(api.hasPolicyConfiguration(row), true);
  assert.match(snippet(component("InspectionPolicyPage")), /!isLegacyTemplate\(row\) \|\| hasPolicyConfiguration\(row\)/);
});

test("editing cloned/imported templates preserves the exact script filename, profile and arbitrary report options", () => {
  for (const profile of ["10g", "noncdb", "cdb", "auto"]) {
    const initial = {
      oracle_profile: profile,
      script_name: "oracle/check noncdb.sql",
      format: "html_report",
      custom_output: { heading: "Operations", sections: ["summary"] },
      include_summary: true, include_findings: true, include_actions: false, include_raw_data: true,
      assistant_default: true, assistant_default_db_types: ["oracle", "mysql"],
      last_run_at: "2026-09-08", last_status: "success", last_message: "ok",
    };
    const extra = api.reportExtraText(initial);
    assert.equal(JSON.parse(extra).oracle_profile, undefined);
    const config = api.buildReportConfig({
      oracle_profile: profile, db_types: ["oracle", "mysql"], assistant_default: true,
      report_options: api.reportOptionsForForm(initial), report_extra_text: extra,
    }, initial);
    assert.deepEqual(plain(config), initial);
  }
});

test("changing Oracle architecture clears the previous script binding and defaults new Oracle templates to auto", () => {
  const initial = { oracle_profile: "10g", script_name: "legacy/oracle10g.sql", custom_output: { format: "html" } };
  for (const profile of ["noncdb", "cdb", "auto"]) {
    const config = api.buildReportConfig({
      db_types: ["oracle"], oracle_profile: profile, report_extra_text: api.reportExtraText(initial),
    }, initial);
    assert.equal(config.oracle_profile, profile);
    assert.equal(Object.hasOwn(config, "script_name"), false);
    assert.deepEqual(plain(config.custom_output), initial.custom_output);
  }
  assert.equal(api.buildReportConfig({ db_types: ["oracle"] }).oracle_profile, "auto");
  assert.equal(Object.hasOwn(api.buildReportConfig({ db_types: ["mysql"] }), "oracle_profile"), false);
  const withoutOracle = api.buildReportConfig({ db_types: ["mysql"], report_extra_text: api.reportExtraText(initial) }, initial);
  assert.equal(Object.hasOwn(withoutOracle, "oracle_profile"), false);
  assert.equal(Object.hasOwn(withoutOracle, "script_name"), false);
  assert.match(source, /dbTypes\.includes\("oracle"\) && \([\s\S]*?name="oracle_profile" label="Oracle 适用架构"/);
  assert.match(source, /templateDetail\.report_config\?\.script_name/);
  assert.match(source, /label="来源脚本"/);
  assert.doesNotMatch(source, /实际执行脚本/);
});

test("removing Oracle drops legacy, automatic and scoped Oracle bindings but preserves non-Oracle scripts", () => {
  for (const oracle_profile of [undefined, "auto", "10g", "noncdb", "cdb"]) {
    const initial = { script_name: "oracle/source.sql", ...(oracle_profile ? { oracle_profile } : {}) };
    const config = api.buildReportConfig({
      db_types: ["mysql"], oracle_profile, report_extra_text: api.reportExtraText(initial),
    }, initial, ["oracle"]);
    assert.equal(Object.hasOwn(config, "oracle_profile"), false);
    assert.equal(Object.hasOwn(config, "script_name"), false);
  }
  for (const [dbType, script] of [["mysql", "mysql_check.sh"], ["postgresql", "pg_check.sh"], ["sqlserver", "mssql_check.sh"]]) {
    const initial = { oracle_profile: "auto", script_name: script };
    const config = api.buildReportConfig({
      db_types: [dbType], report_extra_text: JSON.stringify(initial),
    }, initial, [dbType]);
    assert.equal(Object.hasOwn(config, "oracle_profile"), false);
    assert.equal(config.script_name, script);
  }
  const replacement = api.buildReportConfig({
    db_types: ["mysql"], report_extra_text: JSON.stringify({ script_name: "mysql_check.sh" }),
  }, { oracle_profile: "cdb", script_name: "oracle/source.sql" }, ["oracle"]);
  assert.equal(replacement.script_name, "mysql_check.sh");
  for (const script of ["dbcheck10g.sql", "dbcheck_noncdb.sql", "dbcheck_cdb.sql", "mysql_check.sh"]) {
    const initial = { script_name: script };
    const config = api.buildReportConfig({
      db_types: ["mysql"], report_extra_text: api.reportExtraText(initial),
    }, initial, ["oracle", "mysql"]);
    assert.equal(config.script_name, script === "mysql_check.sh" ? script : undefined);
  }
  assert.match(source, /buildReportConfig\(values, initial\?\.report_config \|\| \{\}, initial\?\.db_types \|\| \[\]\)/);
});

test("policy and template pages refresh on in-app and cross-tab events and unsubscribe on unmount", () => {
  for (const page of ["InspectionPolicyPage", "InspectionTemplatePage"]) {
    const effect = component(page).body.body.find((node) => node.type === "ExpressionStatement"
      && node.expression.callee?.name === "useEffect" && snippet(node).includes("subscribeInspectionPoliciesChanged"));
    const listeners = new Map();
    let loads = 0;
    const mount = vm.runInNewContext(`${helperSource}\n(${snippet(effect.expression.arguments[0])})`, {
      load: () => { loads += 1; },
      window: {
        addEventListener: (key, fn) => listeners.set(key, fn),
        removeEventListener: (key, fn) => {
          assert.equal(listeners.get(key), fn);
          listeners.delete(key);
        },
      },
    });
    const unmount = mount();
    assert.equal(loads, 1);
    listeners.get(api.INSPECTION_POLICIES_CHANGED_EVENT)();
    assert.equal(loads, 2);
    listeners.get("storage")({ key: "unrelated" });
    assert.equal(loads, 2);
    listeners.get("storage")({ key: api.INSPECTION_POLICIES_CHANGED_STORAGE_KEY });
    assert.equal(loads, 3);
    unmount();
    assert.equal(listeners.size, 0);
    const unmountAgain = mount();
    assert.equal(listeners.size, 2);
    unmountAgain();
  }
});

test("run page sends null for automatic and preserves an explicit key verbatim", async () => {
  for (const templateKey of [api.AUTO_TEMPLATE_KEY, "custom_imported_cdb"]) {
    const calls = [];
    const messages = [];
    const run = vm.runInNewContext(`(${snippet(handler("InspectionPage", "runInspect"))})`, {
      inspectionOutcome, inspectionNotice,
      ...api, instanceId: 17, dbType: "oracle", selectedInstance: { db_type: "oracle" }, templateKey,
      selectedTemplate: templateKey === api.AUTO_TEMPLATE_KEY ? undefined : { name: "Custom", runtime_valid: true },
      setLoading() {}, setResult() {}, loadHistory: async () => {},
      message: { success: (text) => messages.push(text), error: assert.fail, warning: assert.fail },
      runInspectionTask: async (...args) => {
        calls.push(args);
        return { task: { task_status: "success", template_key: "oracle_cdb", template_name: "Resolved CDB" } };
      },
    });
    await run();
    assert.deepEqual(calls, [["oracle", 17, templateKey === api.AUTO_TEMPLATE_KEY ? null : templateKey]]);
    assert.deepEqual(messages, ["巡检完成：Resolved CDB"]);
  }
});

const loadEffect = component("InspectionPage").body.body.find((node) => node.type === "ExpressionStatement"
  && node.expression.callee?.name === "useEffect" && snippet(node).includes("const loadTemplates"));

function mountTemplateLoader(initialKey = api.AUTO_TEMPLATE_KEY) {
  const listeners = new Map();
  const requests = [];
  const state = { key: initialKey, rows: [], error: "", loading: false };
  const mount = vm.runInNewContext(`(${snippet(loadEffect.expression.arguments[0])})`, {
    ...api, inspectionDbType: "oracle",
    listInspectionPolicies: () => new Promise((resolve, reject) => requests.push({ resolve, reject })),
    setTemplateKey: (value) => { state.key = typeof value === "function" ? value(state.key) : value; },
    setTemplateRows: (value) => { state.rows = value; },
    setTemplateLoadError: (value) => { state.error = value; },
    setTemplateLoading: (value) => { state.loading = value; },
    window: {
      addEventListener: (key, fn) => listeners.set(key, fn),
      removeEventListener: (key) => listeners.delete(key),
    },
  });
  return { state, requests, listeners, unmount: mount() };
}
const flush = () => new Promise((resolve) => setImmediate(resolve));
const custom = { policy_key: "custom_new", name: "New", db_types: ["oracle"], enabled: true, runtime_valid: true };

test("catalog refresh keeps auto stable and explicit custom selections intact across same-page/storage events", async () => {
  for (const initialKey of [api.AUTO_TEMPLATE_KEY, custom.policy_key]) {
    const { state, requests, listeners, unmount } = mountTemplateLoader(initialKey);
    requests[0].resolve({ items: [custom, { ...custom, policy_key: "oracle_standard" }] });
    await flush();
    assert.equal(state.key, initialKey);
    assert.deepEqual(plain(state.rows.map((row) => row.policy_key)), [custom.policy_key]);
    listeners.get(api.INSPECTION_POLICIES_CHANGED_EVENT)();
    requests[1].resolve({ items: [custom, { ...custom, policy_key: "new_import" }] });
    await flush();
    assert.equal(state.rows.length, 2);
    assert.equal(state.key, initialKey);
    listeners.get("storage")({ key: api.INSPECTION_POLICIES_CHANGED_STORAGE_KEY });
    requests[2].resolve({ items: [custom] });
    await flush();
    assert.equal(state.key, initialKey);
    unmount();
    assert.equal(listeners.size, 0);
  }
});

test("disabled, deleted, incompatible and invalid templates never replace an explicit selection with auto", async () => {
  for (const rows of [[], [{ ...custom, enabled: false }], [{ ...custom, db_types: ["mysql"] }], [{ ...custom, runtime_valid: false }]]) {
    const { state, requests, listeners, unmount } = mountTemplateLoader(custom.policy_key);
    requests[0].resolve({ items: [custom] });
    await flush();
    listeners.get(api.INSPECTION_POLICIES_CHANGED_EVENT)();
    requests[1].resolve({ items: rows });
    await flush();
    assert.equal(state.key, custom.policy_key);
    const optionFactory = handler("InspectionPage", "templateOptions").arguments[0];
    const options = vm.runInNewContext(`(${snippet(optionFactory)})()`, {
      ...api, templateRows: state.rows, templateKey: state.key, inspectionDbType: "oracle",
      dbTypeText: (value) => value, isAssistantDefaultTemplate: () => false,
    });
    const selectedOption = options.flatMap((option) => option.options || [option]).find((option) => option.value === state.key);
    assert.ok(selectedOption);
    assert.equal(selectedOption.disabled, true);
    assert.notEqual(selectedOption.label, custom.policy_key);
    if (!rows.length) assert.equal(selectedOption.label, "所选模板已不可用，请重新选择");

    const warnings = [];
    const run = vm.runInNewContext(`(${snippet(handler("InspectionPage", "runInspect"))})`, {
      ...api, instanceId: 17, templateKey: state.key, selectedTemplate: undefined,
      message: { warning: (text) => warnings.push(text) },
      runInspectionTask: assert.fail, setLoading: assert.fail,
    });
    await run();
    assert.deepEqual(warnings, ["所选模板当前不可用，请重新选择巡检模板"]);
    unmount();
  }
  assert.doesNotMatch(snippet(loadEffect), /setTemplateKey/);
});

test("failed refresh never silently replaces an explicit choice, and stale responses cannot overwrite a newer catalog", async () => {
  const { state, requests, listeners, unmount } = mountTemplateLoader(custom.policy_key);
  requests[0].reject(new Error("offline"));
  await flush();
  assert.equal(state.key, custom.policy_key);
  assert.equal(state.error, "offline");
  listeners.get(api.INSPECTION_POLICIES_CHANGED_EVENT)();
  listeners.get(api.INSPECTION_POLICIES_CHANGED_EVENT)();
  requests[2].resolve({ items: [custom] });
  await flush();
  requests[1].resolve({ items: [] });
  await flush();
  assert.equal(state.key, custom.policy_key);
  assert.equal(state.rows.length, 1);
  unmount();
});

test("template mutation entrypoints notify existing listeners and exchange remains server-side", () => {
  for (const [page, names] of [
    ["InspectionPolicyPage", ["clonePolicyRow", "togglePolicy", "removePolicy"]],
    ["InspectionTemplatePage", ["cloneTemplate", "deleteTemplate", "confirmImport"]],
    ["InspectionPolicyEditor", ["save"]],
  ]) {
    for (const name of names) assert.match(snippet(handler(page, name)), /notifyInspectionPoliciesChanged\(\)/);
  }
  assert.match(snippet(handler("InspectionTemplatePage", "exportTemplate")), /exportInspectionTemplate\(row.id\)/);
  assert.match(snippet(handler("InspectionTemplatePage", "exportSelectedTemplates")), /exportInspectionTemplates\(selectedTemplateIds\)/);
});
