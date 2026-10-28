import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  DEFAULT_QUICK_ACTION_CARDS,
  normalizeQuickCards,
  quickActionCardsForContext,
} from "../src/modules/quickAccess.js";

const byKey = (rows, key) => rows.find((item) => item.key === key);

test("legacy default cards migrate to managed visible-menu navigation without overwriting storage", () => {
  const legacy = DEFAULT_QUICK_ACTION_CARDS.map(({ managed, targetKey, ...item }) => ({ ...item }));
  const normalized = normalizeQuickCards(legacy);
  assert.equal(byKey(normalized, "backup").managed, true);
  assert.equal(byKey(normalized, "backup").targetKey, "backup.overview");
  assert.equal(legacy[0].targetKey, undefined, "migration is a projection, not an in-place storage rewrite");

  const edited = normalizeQuickCards([{ ...legacy[0], title: "我的备份入口" }])[0];
  assert.equal(edited.managed, false);
  assert.equal(quickActionCardsForContext([edited], { dbType: "mysql" })[0].title, "我的备份入口");
});

test("managed quick cards use the selected engine and never default an absent target to Oracle", () => {
  const expectations = {
    oracle: ["Oracle 备份", "表空间", true],
    mysql: ["MySQL 备份", "库表容量", false],
    postgresql: ["PostgreSQL 备份", "数据库空间", false],
    sqlserver: ["SQL Server 备份", "数据文件空间", false],
  };
  for (const [dbType, [backup, space, awr]] of Object.entries(expectations)) {
    const cards = quickActionCardsForContext(DEFAULT_QUICK_ACTION_CARDS, { dbType });
    assert.equal(byKey(cards, "backup").title, backup);
    assert.equal(byKey(cards, "space").title, space);
    assert.equal(Boolean(byKey(cards, "awr")), awr);
    if (dbType !== "oracle") assert.doesNotMatch(cards.map((item) => `${item.title} ${item.subtitle}`).join(" "), /RMAN|EXPDP|AWR|Oracle/);
  }
  const unscoped = quickActionCardsForContext(DEFAULT_QUICK_ACTION_CARDS);
  assert.equal(byKey(unscoped, "backup").title, "备份与恢复");
  assert.equal(byKey(unscoped, "space").title, "空间与容量");
  assert.equal(byKey(unscoped, "awr"), undefined);
  assert.doesNotMatch(unscoped.map((item) => `${item.title} ${item.subtitle}`).join(" "), /RMAN|EXPDP|AWR|Oracle/);
});

test("quick cards only navigate to leaves present in the caller-visible menu", () => {
  const menuItems = [{ key: "perf", children: [{ key: "perf.lock" }] }, { key: "ops", children: [{ key: "ops.space" }] }];
  const cards = quickActionCardsForContext(DEFAULT_QUICK_ACTION_CARDS, { dbType: "mysql", menuItems });
  assert.deepEqual(cards.map((item) => item.key), ["lock", "space"]);
  assert.equal(byKey(cards, "space").targetKey, "ops.space");
});

test("landing quick-card clicks remain manual navigation, not execution", () => {
  const source = readFileSync(new URL("../src/DiagnosisCenterPage.jsx", import.meta.url), "utf8");
  const select = source.slice(source.indexOf("const selectQuickAction"), source.indexOf("useEffect(() =>", source.indexOf("const selectQuickAction")));
  assert.match(select, /onNavigate\(item\.targetKey, item\.instanceId \|\| instanceId\)/);
  assert.doesNotMatch(select, /dispatchAssistantIntent|resolveCustomerRequirement|createAssistantPlan|executeAssistantPlan/);
});
