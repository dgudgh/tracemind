import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

const pageSource = await readFile(new URL("../src/ConfigGovernancePage.jsx", import.meta.url), "utf8");
const apiSource = await readFile(new URL("../src/modules/configGovernanceApi.js", import.meta.url), "utf8");
const routerSource = await readFile(new URL("../src/AppPageRouter.jsx", import.meta.url), "utf8");
const menuSource = await readFile(new URL("../src/menuConfig.jsx", import.meta.url), "utf8");
const styleSource = await readFile(new URL("../src/styles.css", import.meta.url), "utf8");

test("configuration governance exposes the complete baseline drift remediation lifecycle", () => {
  assert.match(pageSource, /配置基线与漂移治理/);
  assert.match(pageSource, /基线库/);
  assert.match(pageSource, /采集与漂移/);
  assert.match(pageSource, /整改闭环/);
  assert.match(pageSource, /审计记录/);
  assert.match(pageSource, /实时比对/);
  assert.match(pageSource, /生成整改方案/);
  assert.match(pageSource, /提交审批/);
  assert.match(pageSource, /维护窗口/);
  assert.match(pageSource, /回滚脚本/);
  assert.match(pageSource, /复核 SQL/);
});

test("configuration governance never presents save as an implicit collection or change", () => {
  assert.match(pageSource, /不会因保存基线自动刷新或修改数据库/);
  assert.match(pageSource, /只有点击“实时比对”才连接实例采集/);
  assert.match(pageSource, /审批通过前不会执行任何变更/);
  assert.match(pageSource, /连接失败不会计为配置漂移/);
});

test("configuration governance is wired to menu router and every backend workflow endpoint", () => {
  assert.match(menuSource, /ops\.configGovernance/);
  assert.match(routerSource, /ops\.configGovernance/);
  assert.match(apiSource, /operations\/config-governance/);
  assert.match(apiSource, /baselines\/\$\{id\}\/evaluate/);
  assert.match(apiSource, /drifts\/\$\{id\}\/accept/);
  assert.match(apiSource, /remediations\/\$\{id\}\/submit/);
  assert.match(apiSource, /remediations\/\$\{id\}\/apply/);
  assert.match(apiSource, /remediations\/\$\{id\}\/verify/);
});

test("configuration governance dialogs and dense content remain viewport safe", () => {
  assert.match(pageSource, /centered/);
  assert.match(styleSource, /\.config-governance-modal \.ant-modal-body/);
  assert.match(styleSource, /@media \(max-width: 1180px\)[\s\S]*?\.config-governance-summary/);
  assert.match(styleSource, /@media \(max-width: 720px\)[\s\S]*?\.config-governance-summary/);
});
