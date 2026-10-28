import {
  CheckCircleOutlined,
  DatabaseOutlined,
  FileSearchOutlined,
  FileTextOutlined,
  LineChartOutlined,
  RobotOutlined,
  SafetyOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";

export const QUICK_ACTION_STORAGE_KEY = "tracemind.quickActionCards.v1";
export const QUICK_ACCESS_UPDATED_EVENT = "tracemind:quick-access-updated";

export const QUICK_ICON_OPTIONS = [
  { value: "database", label: "数据库", icon: DatabaseOutlined },
  { value: "search", label: "分析", icon: FileSearchOutlined },
  { value: "thunderbolt", label: "故障", icon: ThunderboltOutlined },
  { value: "report", label: "报告", icon: FileTextOutlined },
  { value: "chart", label: "趋势", icon: LineChartOutlined },
  { value: "check", label: "巡检", icon: CheckCircleOutlined },
  { value: "safety", label: "安全", icon: SafetyOutlined },
  { value: "ai", label: "AI", icon: RobotOutlined },
];

export const QUICK_ICON_MAP = Object.fromEntries(QUICK_ICON_OPTIONS.map((item) => [item.value, item.icon]));

export const QUICK_TONE_OPTIONS = [
  { value: "emerald", label: "墨绿" },
  { value: "blue", label: "深蓝" },
  { value: "red", label: "风险红" },
  { value: "violet", label: "紫灰" },
  { value: "cyan", label: "青绿" },
  { value: "green", label: "森林绿" },
  { value: "amber", label: "琥珀" },
  { value: "slate", label: "石墨" },
  { value: "ink", label: "黑绿" },
];

export const DEFAULT_QUICK_ACTION_CARDS = [
  {
    key: "backup",
    title: "Oracle 备份",
    subtitle: "RMAN / EXPDP",
    prompt: "我需要对 Oracle 数据库进行备份",
    iconKey: "database",
    tone: "emerald",
    enabled: true,
    managed: true,
    targetKey: "backup.overview",
  },
  {
    key: "slow-sql",
    title: "慢 SQL",
    subtitle: "耗时 SQL 分析",
    prompt: "我需要分析数据库慢 SQL 和执行计划",
    iconKey: "search",
    tone: "blue",
    enabled: true,
    managed: true,
    targetKey: "perf.slowsql",
  },
  {
    key: "lock",
    title: "锁等待",
    subtitle: "阻塞链路",
    prompt: "数据库出现锁等待和阻塞，需要定位会话事务",
    iconKey: "thunderbolt",
    tone: "red",
    enabled: true,
    managed: true,
    targetKey: "perf.lock",
  },
  {
    key: "awr",
    title: "AWR",
    subtitle: "报告导出 / 对比",
    prompt: "我要查看 Oracle AWR 报告和性能对比",
    iconKey: "report",
    tone: "violet",
    enabled: true,
    managed: true,
    targetKey: "perf.awr.report",
  },
  {
    key: "space",
    title: "表空间",
    subtitle: "容量与扩容",
    prompt: "Oracle 表空间不足，需要查看空间管理和扩容方案",
    iconKey: "chart",
    tone: "cyan",
    enabled: true,
    managed: true,
    targetKey: "ops.space",
  },
  {
    key: "inspection",
    title: "一键巡检",
    subtitle: "健康检查",
    prompt: "我需要对当前数据库进行一键巡检",
    iconKey: "check",
    tone: "green",
    enabled: true,
    managed: true,
    targetKey: "inspect.run",
  },
  {
    key: "alerts",
    title: "告警",
    subtitle: "综合事件",
    prompt: "我要查看数据库综合告警和异常事件",
    iconKey: "safety",
    tone: "amber",
    enabled: true,
    managed: true,
    targetKey: "monitor.alert.unified",
  },
  {
    key: "report",
    title: "报告",
    subtitle: "售后输出",
    prompt: "我需要生成统一运维报告和售后分析报告",
    iconKey: "report",
    tone: "slate",
    enabled: true,
    managed: true,
    targetKey: "report.unified",
  },
  {
    key: "ai",
    title: "根因分析",
    subtitle: "AI Cockpit",
    prompt: "客户现场出现问题，需要进行端到端根因分析",
    iconKey: "ai",
    tone: "ink",
    enabled: true,
    managed: true,
    targetKey: "diagnose.investigation",
  },
];

const DEFAULT_CARD_BY_KEY = new Map(DEFAULT_QUICK_ACTION_CARDS.map((item) => [item.key, item]));
const ENGINE_VARIANTS = {
  backup: {
    oracle: { title: "Oracle 备份", subtitle: "RMAN / EXPDP", prompt: "打开 Oracle 备份与恢复" },
    mysql: { title: "MySQL 备份", subtitle: "备份策略 / 恢复", prompt: "打开 MySQL 备份与恢复" },
    postgresql: { title: "PostgreSQL 备份", subtitle: "备份策略 / 恢复", prompt: "打开 PostgreSQL 备份与恢复" },
    sqlserver: { title: "SQL Server 备份", subtitle: "完整 / 差异 / 日志", prompt: "打开 SQL Server 备份与恢复" },
    generic: { title: "备份与恢复", subtitle: "策略 / 方案 / 状态", prompt: "打开数据库备份与恢复" },
  },
  space: {
    oracle: { title: "表空间", subtitle: "容量与扩容", prompt: "打开 Oracle 表空间与容量管理" },
    mysql: { title: "库表容量", subtitle: "Schema / 表占用", prompt: "打开 MySQL 库表容量分析" },
    postgresql: { title: "数据库空间", subtitle: "库 / Schema / 表占用", prompt: "打开 PostgreSQL 空间与容量分析" },
    sqlserver: { title: "数据文件空间", subtitle: "数据库文件与容量", prompt: "打开 SQL Server 空间与容量分析" },
    generic: { title: "空间与容量", subtitle: "按目标引擎查看", prompt: "打开数据库空间与容量分析" },
  },
};
const KNOWN_ENGINES = new Set(["oracle", "mysql", "postgresql", "sqlserver"]);

const PAGE_ICON_BY_PREFIX = [
  ["backup.", "database"],
  ["ops.", "database"],
  ["asset.", "database"],
  ["inspect.", "check"],
  ["perf.awr", "report"],
  ["perf.", "search"],
  ["monitor.", "safety"],
  ["diagnose.", "ai"],
  ["report.", "report"],
  ["integration.", "chart"],
  ["system.", "safety"],
  ["rescue.", "safety"],
];

const PAGE_TONE_BY_PREFIX = [
  ["backup.", "emerald"],
  ["ops.", "cyan"],
  ["asset.", "slate"],
  ["inspect.", "green"],
  ["perf.awr", "violet"],
  ["perf.", "blue"],
  ["monitor.", "amber"],
  ["diagnose.", "ink"],
  ["report.", "slate"],
  ["integration.", "cyan"],
  ["system.", "slate"],
  ["rescue.", "red"],
];

const INSTANCE_SCOPED_PREFIXES = [
  "backup.",
  "diagnose.",
  "inspect.",
  "monitor.alert.db",
  "ops.",
  "perf.",
];

function compactText(value, fallback = "") {
  const text = String(value || "").trim();
  return text || fallback;
}

function inferByPrefix(targetKey, table, fallback) {
  const key = String(targetKey || "");
  return table.find(([prefix]) => key.startsWith(prefix))?.[1] || fallback;
}

function shouldAttachInstance(targetKey) {
  const key = String(targetKey || "");
  return INSTANCE_SCOPED_PREFIXES.some((prefix) => key.startsWith(prefix));
}

function normalizeOptionalString(value) {
  const text = compactText(value);
  return text || undefined;
}

export function normalizeQuickCards(cards) {
  const source = Array.isArray(cards) && cards.length ? cards : DEFAULT_QUICK_ACTION_CARDS;
  return source.map((item, index) => {
    const defaultCard = DEFAULT_CARD_BY_KEY.get(item.key);
    const matchesLegacyDefault = Boolean(defaultCard && ["title", "subtitle", "prompt"].every((field) =>
      item[field] === undefined || item[field] === defaultCard[field]));
    const managed = typeof item.managed === "boolean" ? item.managed
      : item.custom ? false : matchesLegacyDefault;
    const targetKey = normalizeOptionalString(item.targetKey || (managed ? defaultCard?.targetKey : undefined));
    const instanceId = item.instanceId === null || item.instanceId === undefined ? undefined : item.instanceId;
    return {
      key: String(item.key || `quick-${index + 1}`),
      title: compactText(item.title, "自定义卡片").slice(0, 12),
      subtitle: compactText(item.subtitle, "快速入口").slice(0, 20),
      prompt: compactText(item.prompt || item.title, "打开功能"),
      iconKey: QUICK_ICON_MAP[item.iconKey] ? item.iconKey : "ai",
      tone: QUICK_TONE_OPTIONS.some((tone) => tone.value === item.tone) ? item.tone : "ink",
      enabled: item.enabled !== false,
      custom: !!item.custom,
      managed,
      targetKey,
      instanceId,
      pinnedPage: !!item.pinnedPage,
    };
  });
}

function menuContains(items, targetKey) {
  if (!Array.isArray(items)) return false;
  return items.some((item) => item?.key === targetKey || menuContains(item?.children, targetKey));
}

// Project saved cards into the selected database context without rewriting localStorage.
// Legacy default cards are migrated to managed cards; edited/custom cards retain their copy.
export function quickActionCardsForContext(cards, { dbType, menuItems } = {}) {
  const engine = KNOWN_ENGINES.has(String(dbType || "").toLowerCase())
    ? String(dbType).toLowerCase() : "";
  return normalizeQuickCards(cards).filter((item) => item.enabled !== false).flatMap((item) => {
    if (item.managed && item.key === "awr" && engine !== "oracle") return [];
    const variants = item.managed ? ENGINE_VARIANTS[item.key] : null;
    const variant = variants?.[engine || "generic"] || variants?.generic;
    const projected = variant ? { ...item, ...variant } : item;
    if (projected.targetKey && Array.isArray(menuItems) && !menuContains(menuItems, projected.targetKey)) return [];
    return [projected];
  });
}

export function loadQuickActionCards() {
  if (typeof window === "undefined") return normalizeQuickCards(DEFAULT_QUICK_ACTION_CARDS);
  try {
    const raw = window.localStorage.getItem(QUICK_ACTION_STORAGE_KEY);
    if (!raw) return normalizeQuickCards(DEFAULT_QUICK_ACTION_CARDS);
    return normalizeQuickCards(JSON.parse(raw));
  } catch {
    return normalizeQuickCards(DEFAULT_QUICK_ACTION_CARDS);
  }
}

export function saveQuickActionCards(cards, options = {}) {
  const nextCards = normalizeQuickCards(cards);
  if (typeof window !== "undefined") {
    try {
      window.localStorage.setItem(QUICK_ACTION_STORAGE_KEY, JSON.stringify(nextCards));
      if (options.notify !== false) {
        window.dispatchEvent(new window.CustomEvent(QUICK_ACCESS_UPDATED_EVENT, { detail: { cards: nextCards } }));
      }
    } catch (error) {
      void error;
    }
  }
  return nextCards;
}

export function createQuickActionCard(cards) {
  const usedKeys = new Set(cards.map((item) => item.key));
  let index = cards.length + 1;
  let key = `custom-${index}`;
  while (usedKeys.has(key)) {
    index += 1;
    key = `custom-${index}`;
  }
  return {
    key,
    title: "新卡片",
    subtitle: "自定义入口",
    prompt: "我要打开自定义功能",
    iconKey: "ai",
    tone: "ink",
    enabled: true,
    custom: true,
  };
}

export function buildPinnedQuickAccessCard({ targetKey, title, breadcrumb = [], instance }) {
  const normalizedKey = compactText(targetKey, "home.dashboard");
  const label = compactText(title, "快速入口");
  const path = Array.isArray(breadcrumb) ? breadcrumb.filter(Boolean) : [];
  const parentLabel = path.length > 1 ? path.slice(0, -1).join(" / ") : "首页固定";
  const scopedInstance = shouldAttachInstance(normalizedKey) ? instance : null;
  const instanceLabel = scopedInstance?.instance_name
    ? `${String(scopedInstance.db_type || "DB").toUpperCase()} · ${scopedInstance.instance_name}`
    : "";

  return {
    key: `page-${normalizedKey.replace(/[^a-zA-Z0-9]+/g, "-")}`,
    title: label,
    subtitle: instanceLabel || parentLabel,
    prompt: `打开${label}`,
    iconKey: inferByPrefix(normalizedKey, PAGE_ICON_BY_PREFIX, "ai"),
    tone: inferByPrefix(normalizedKey, PAGE_TONE_BY_PREFIX, "ink"),
    enabled: true,
    custom: true,
    targetKey: normalizedKey,
    instanceId: scopedInstance?.id,
    pinnedPage: true,
  };
}

export function pinQuickAccessPage(payload) {
  const card = normalizeQuickCards([buildPinnedQuickAccessCard(payload)])[0];
  const currentCards = loadQuickActionCards();
  const existingIndex = currentCards.findIndex((item) => item.key === card.key);
  const nextCards = existingIndex >= 0
    ? currentCards.map((item, index) => (index === existingIndex ? { ...item, ...card } : item))
    : [...currentCards, card];
  const savedCards = saveQuickActionCards(nextCards);
  return { card, cards: savedCards, updated: existingIndex >= 0 };
}

export function isQuickAccessPagePinned(targetKey) {
  const key = `page-${compactText(targetKey, "home.dashboard").replace(/[^a-zA-Z0-9]+/g, "-")}`;
  return loadQuickActionCards().some((item) => item.key === key && item.enabled !== false);
}
