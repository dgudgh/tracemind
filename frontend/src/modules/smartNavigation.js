import { MENU_CONFIG, normalizeMenuKey } from "../menuConfig";

const DB_TYPE_ALIASES = {
  oracle: ["oracle", "orcl", "rac", "dg", "dataguard", "rman", "awr"],
  mysql: ["mysql", "innodb", "binlog", "xtrabackup", "mysqldump"],
  postgresql: ["postgresql", "postgres", "pg", "wal", "vacuum"],
  sqlserver: ["sqlserver", "sql server", "mssql"],
};

const SMART_ROUTE_ALIASES = [
  ["instances", ["实例", "数据库连接", "连接配置", "添加数据库", "编辑数据库", "测试连接", "资产连接"]],
  ["sshAssets", ["ssh", "主机", "远程主机", "服务器资产", "主机连接"]],
  ["inspectionCenter", ["巡检", "一键巡检", "检查数据库", "健康检查", "生成巡检"]],
  ["inspectionTasks", ["巡检任务", "定时巡检", "任务调度"]],
  ["inspectionReports", ["巡检报告", "健康报告", "检查报告"]],
  ["inspectionCompare", ["巡检对比", "历史对比", "前后对比"]],
  ["inspectionPolicies", ["巡检策略", "巡检规则", "检查策略"]],
  ["inspectionTemplates", ["巡检模板", "检查模板"]],
  ["oracleOverview", ["oracle状态", "oracle概览", "数据库状态", "启动数据库", "关闭数据库", "实例状态"]],
  ["oracleSpace", ["表空间", "空间", "temp", "undo", "redo", "空间扩容", "空间不足"]],
  ["oracleLogArchive", ["归档", "归档日志", "fra", "闪回区", "日志清理", "archive log"]],
  ["oracleSession", ["会话", "事务", "锁", "锁等待", "阻塞", "kill session", "长事务"]],
  ["oracleObject", ["对象", "表统计", "索引统计", "统计信息", "碎片", "对象统计"]],
  ["oracleHA", ["高可用", "主备", "dataguard", "data guard", "dg", "切换", "同步延迟"]],
  ["oracleGovernance", ["oracle数据治理", "历史数据", "归档表", "清理表", "分区治理"]],
  ["mysqlOverview", ["mysql状态", "mysql概览", "mysql运行", "mysql参数"]],
  ["mysqlStorage", ["mysql存储", "binlog", "relay log", "error log", "日志清理"]],
  ["mysqlInnoDBTx", ["innodb", "mysql事务", "死锁", "锁等待", "阻塞链"]],
  ["mysqlUserSecurity", ["mysql用户", "mysql权限", "账号安全", "授权"]],
  ["mysqlTableIndex", ["mysql表", "mysql索引", "表索引", "统计信息"]],
  ["mysqlReplHA", ["mysql复制", "主从", "gtid", "mgr", "复制延迟"]],
  ["mysqlGovernance", ["mysql数据治理", "mysql归档", "mysql清理"]],
  ["ops.postgresql.overview", ["postgresql状态", "pg状态", "postgres概览"]],
  ["ops.postgresql.space", ["pg空间", "postgresql空间", "pg表空间"]],
  ["ops.postgresql.wal", ["wal", "pg日志", "postgresql日志", "wal归档"]],
  ["ops.postgresql.sessionTransaction", ["pg会话", "pg事务", "postgresql锁", "pg锁等待"]],
  ["ops.postgresql.objectStats", ["pg对象", "pg表统计", "pg索引"]],
  ["ops.postgresql.ha", ["pg高可用", "postgresql主备", "pg复制"]],
  ["ops.postgresql.governance", ["pg数据治理", "postgresql治理"]],
  ["ops.sqlserver.overview", ["sqlserver状态", "mssql状态", "sql server概览"]],
  ["ops.sqlserver.space", ["sqlserver空间", "mssql空间"]],
  ["ops.sqlserver.logs", ["sqlserver日志", "mssql日志", "日志管理"]],
  ["ops.sqlserver.sessionTransaction", ["sqlserver会话", "mssql事务", "sqlserver锁"]],
  ["ops.sqlserver.objectStats", ["sqlserver对象", "mssql表统计"]],
  ["ops.sqlserver.ha", ["sqlserver高可用", "alwayson", "故障转移"]],
  ["ops.sqlserver.governance", ["sqlserver治理", "mssql治理"]],
  ["backupOverview", ["备份总览", "备份概览", "备份看板"]],
  ["backupPolicies", ["备份", "数据库备份", "备份策略", "rman备份", "全备", "增备", "归档备份", "导出备份", "expdp", "dump"]],
  ["backupTasks", ["执行备份", "立即备份", "手工备份", "备份任务", "开始备份", "启动备份"]],
  ["backupHistory", ["备份历史", "备份记录", "备份结果"]],
  ["backupFiles", ["备份文件", "备份集", "backupset", "backup piece", "备份目录"]],
  ["backupRecoveryCenter", ["恢复", "还原", "数据库恢复", "pitr", "restore", "recover", "时间点恢复"]],
  ["backupValidation", ["备份验证", "备份校验", "校验备份", "rman validate", "恢复验证", "validate"]],
  ["backupDrill", ["恢复演练", "灾备演练", "演练"]],
  ["backupAlerts", ["备份告警", "备份失败", "备份异常"]],
  ["backupReports", ["备份报告", "恢复报告"]],
  ["storageRescueOverview", ["恢复总览", "救援总览", "数据库恢复中心"]],
  ["storageRescueAgents", ["agent", "远程agent", "恢复agent"]],
  ["storageRescueTasks", ["恢复任务", "救援任务"]],
  ["storageRescueReports", ["恢复报告", "救援报告"]],
  ["storageRescueApprovals", ["风险审批", "恢复审批", "审批"]],
  ["performanceOverview", ["性能", "性能总览", "性能分析", "性能诊断"]],
  ["sqlProfile", ["sql画像", "sql分析", "执行计划画像", "sql profile"]],
  ["tableProfile", ["表画像", "表分析", "表容量画像"]],
  ["indexAnalysis", ["索引", "索引分析", "缺失索引", "索引建议"]],
  ["slowSqlAnalysis", ["慢sql", "慢查询", "sql慢", "耗时sql", "sql超时"]],
  ["lockAnalysis", ["锁分析", "锁等待分析", "阻塞分析", "死锁分析"]],
  ["perfBaseline", ["性能基线", "基线", "baseline"]],
  ["problemTrend", ["问题趋势", "故障趋势", "趋势分析"]],
  ["capacity", ["容量", "容量分析", "容量预测", "空间预测"]],
  ["optimizationCenter", ["优化", "优化建议", "调优", "优化中心"]],
  ["databaseProfile", ["数据库画像", "实例画像", "库画像"]],
  ["awrReport", ["awr", "awr报告", "awr导出", "awr上传", "oracle报告"]],
  ["awrCompare", ["awr对比", "报告对比", "awr比较"]],
  ["awrAutoCollect", ["awr采集", "自动采集awr", "定时awr"]],
  ["schedule", ["定时任务", "调度", "计划任务"]],
  ["unifiedAlerts", ["综合告警", "统一告警", "告警事件"]],
  ["alertCenter", ["数据库告警", "告警中心", "数据库报警"]],
  ["observabilityCenter", ["可观测", "采集中心", "指标采集", "观测中心"]],
  ["zabbixIntegration", ["zabbix", "zabbix联动", "zabbix告警"]],
  ["prometheusIntegration", ["prometheus", "alertmanager", "prometheus告警"]],
  ["notifyManagement", ["通知", "通知管理", "售后群", "消息通知"]],
  ["itsmManagement", ["工单", "itsm", "工单管理"]],
  ["ecosystemIntegration", ["生态", "集成", "外部系统", "第三方"]],
  ["ai", ["ai诊断", "智能诊断", "问ai"]],
  ["diagnosisCenter", ["根因", "根因分析", "端到端诊断", "客户报障", "现场问题", "诊断工作台"]],
  ["ruleDiagnosis", ["规则诊断", "规则分析", "诊断规则"]],
  ["aiConfig", ["ai配置", "模型配置", "大模型", "provider"]],
  ["unifiedReports", ["统一报告", "报告中心", "运维报告", "售后报告", "售后分析报告"]],
  ["awrReports", ["awr分析报告", "awr历史报告"]],
  ["alertReports", ["告警报告", "报警报告"]],
  ["userManagement", ["用户", "用户管理", "账号", "权限"]],
  ["systemParams", ["系统参数", "参数配置", "系统设置"]],
  ["dashboard", ["首页", "总览", "仪表盘"]],
].map(([targetKey, keywords]) => ({ targetKey, keywords }));

function normalizeText(value) {
  return String(value || "")
    .toLowerCase()
    .replace(/[\s_\-:：/\\()[\]{}.,，。;；'"“”‘’]+/g, "");
}

function flattenMenuItems(items = MENU_CONFIG, parents = [], inheritedDbTypes) {
  return items.flatMap((item) => {
    const dbTypes = item.dbTypes || inheritedDbTypes;
    const path = [...parents, item.label].filter(Boolean);
    if (item.children?.length) return flattenMenuItems(item.children, path, dbTypes);
    return [{
      targetKey: item.key,
      title: item.label,
      path,
      dbTypes,
      keywords: [item.key, item.label, ...path],
    }];
  });
}

export function detectSmartNavigationDbType(text) {
  const normalized = normalizeText(text);
  return Object.entries(DB_TYPE_ALIASES).find(([, aliases]) =>
    aliases.some((alias) => normalized.includes(normalizeText(alias)))
  )?.[0] || "";
}

const DIRECT_COMMAND_HINTS = [
  "打开",
  "查看",
  "进入",
  "跳转",
  "生成",
  "执行",
  "运行",
  "开始",
  "我要",
  "需要",
  "帮我",
  "进行",
  "配置",
  "创建",
  "新增",
  "编辑",
  "测试",
  "导出",
];

const PROBLEM_DESCRIPTION_HINTS = [
  "客户反馈",
  "报障",
  "故障",
  "异常",
  "超时",
  "无响应",
  "报错",
  "失败",
  "出现",
  "疑似",
  "变慢",
  "很慢",
  "卡顿",
  "等待",
];

export function shouldUseDirectSmartNavigation(result, problemText) {
  if (!result) return false;
  if (result.score >= 80) return true;

  const rawText = String(problemText || "").trim();
  const normalizedText = normalizeText(rawText);
  const matchedKeywords = result.matchedKeywords || [];
  const exactKeywordMatch = matchedKeywords.some((keyword) => normalizeText(keyword) === normalizedText);
  if (exactKeywordMatch) return true;

  const commandLike = DIRECT_COMMAND_HINTS.some((word) => normalizedText.includes(normalizeText(word)));
  if (commandLike) return true;

  const problemLike = PROBLEM_DESCRIPTION_HINTS.some((word) => normalizedText.includes(normalizeText(word)));
  return rawText.length <= 18 && !problemLike;
}

function mergeCandidates(menuItems = MENU_CONFIG, restrictToMenu = false) {
  const candidates = new Map();
  flattenMenuItems(menuItems).forEach((item) => candidates.set(item.targetKey, { ...item, aliasKeywords: [] }));
  SMART_ROUTE_ALIASES.forEach((alias) => {
    const targetKey = normalizeMenuKey(alias.targetKey);
    if (restrictToMenu && !candidates.has(targetKey)) return;
    const base = candidates.get(targetKey) || {
      targetKey,
      title: targetKey,
      path: [targetKey],
      keywords: [targetKey],
      aliasKeywords: [],
    };
    candidates.set(targetKey, {
      ...base,
      aliasKeywords: [...(base.aliasKeywords || []), ...alias.keywords],
    });
  });
  return Array.from(candidates.values());
}

function scoreCandidate(candidate, normalizedText, queryDbType, selectedDbType) {
  const words = [...new Set([...(candidate.keywords || []), ...(candidate.aliasKeywords || [])])]
    .map((word) => ({ raw: word, normalized: normalizeText(word) }))
    .filter((word) => word.normalized.length >= 2);

  let score = 0;
  const matched = [];

  words.forEach((word) => {
    if (!normalizedText.includes(word.normalized)) return;
    const isAlias = (candidate.aliasKeywords || []).includes(word.raw);
    const weight = isAlias ? 36 : 18;
    score += weight + Math.min(24, word.normalized.length * 2);
    matched.push(word.raw);
  });

  const label = normalizeText(candidate.title);
  if (label && normalizedText.includes(label)) score += 50;
  if (normalizeText(candidate.targetKey) && normalizedText.includes(normalizeText(candidate.targetKey))) score += 20;

  if (candidate.dbTypes?.length) {
    if (queryDbType && !candidate.dbTypes.includes(queryDbType)) score -= 80;
    if (selectedDbType && candidate.dbTypes.includes(selectedDbType)) score += 8;
  }

  return { score, matched: matched.slice(0, 5) };
}

export function resolveSmartNavigation(problemText, options = {}) {
  const rawText = String(problemText || "").trim();
  const normalizedText = normalizeText(rawText);
  if (!normalizedText) return null;

  const queryDbType = detectSmartNavigationDbType(rawText);
  const selectedDbType = String(options.selectedInstance?.db_type || "").toLowerCase();
  const candidates = mergeCandidates(options.menuItems || MENU_CONFIG, Boolean(options.menuItems))
    .map((candidate) => ({ ...candidate, ...scoreCandidate(candidate, normalizedText, queryDbType, selectedDbType) }))
    .filter((candidate) => candidate.score >= 38)
    .sort((a, b) => b.score - a.score);

  const best = candidates[0];
  if (!best) return null;

  return {
    targetKey: best.targetKey,
    title: best.title,
    path: best.path,
    score: best.score,
    matchedKeywords: best.matched,
    targetDbType: queryDbType || selectedDbType || "",
    message: `已传送到${best.path?.join(" / ") || best.title}`,
  };
}
