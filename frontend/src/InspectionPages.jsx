import React, { useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Col,
  Descriptions,
  Dropdown,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Row,
  Select,
  Space,
  Spin,
  Statistic,
  Table,
  Tag,
  Tooltip,
  Typography,
  Upload,
  message,
} from "antd";
import { BinaryStatusSwitch } from "./framework/BinaryStatusSwitch";
import {
  AlertOutlined,
  CheckCircleOutlined,
  ClockCircleOutlined,
  DatabaseOutlined,
  DeleteOutlined,
  DownloadOutlined,
  ExperimentOutlined,
  FileTextOutlined,
  InfoCircleOutlined,
  InboxOutlined,
  MoreOutlined,
  PlusOutlined,
  ReloadOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
  SearchOutlined,
  UploadOutlined,
  WarningOutlined,
} from "@ant-design/icons";
import { mysqlOpsApi } from "./apiClients";
import { inspectionOutcome, inspectionNotice, INSPECTION_STATUS_TEXT } from "./InspectionExecutionState.js";
import { CurrentInstanceSummary, OpsEmptyState } from "./OpsLayout";
import { MySQLSlowSqlTab } from "./MySQLOpsPages";
import { InspectionReportDetailPage } from "./InspectionReportDetailPage";
import { OracleSlowSqlPage } from "./OracleSlowSqlPage";
import { ReportPage } from "./ReportPages";
import { stableRecordKey } from "./modules/stableKeys";
import {
  downloadInspectionReportHtml,
  downloadInspectionReportMarkdown,
  downloadInspectionReportWord,
  getInspectionReportStructuredDetail,
} from "./modules/reportApi";
import {
  cloneInspectionPolicy,
  createInspectionPolicy,
  deleteInspectionPolicy,
  exportInspectionTemplate,
  exportInspectionTemplates,
  getInspectionTask,
  importInspectionTemplates,
  listInspectionPolicies,
  listInspectionTasks,
  previewInspectionTemplateImport,
  runInspectionTask,
  testInspectionCustomMetric,
  updateInspectionPolicy,
} from "./modules/inspectionApi";

const { Text } = Typography;

function useSyncedInstanceId(selectedInstanceId, availableInstances) {
  const [instanceId, setInstanceId] = useState(selectedInstanceId || availableInstances[0]?.id);

  useEffect(() => {
    if (selectedInstanceId && availableInstances.some((item) => item.id === selectedInstanceId)) {
      setInstanceId(selectedInstanceId);
      return;
    }
    setInstanceId(availableInstances[0]?.id);
  }, [selectedInstanceId, availableInstances]);

  return [instanceId, setInstanceId];
}

const DB_TYPE_OPTIONS = [
  { value: "oracle", label: "Oracle" },
  { value: "mysql", label: "MySQL" },
  { value: "postgresql", label: "PostgreSQL" },
  { value: "sqlserver", label: "SQL Server" },
  { value: "host", label: "Host" },
];

const DATABASE_DB_TYPE_OPTIONS = DB_TYPE_OPTIONS.filter((item) => item.value !== "host");
const AUTO_TEMPLATE_KEY = "__auto__";
const ORACLE_PROFILE_OPTIONS = [
  { value: "auto", label: "自动检测" },
  { value: "10g", label: "Oracle 10g" },
  { value: "noncdb", label: "Non-CDB" },
  { value: "cdb", label: "CDB" },
];
const TEMPLATE_ORDER = { oracle_10g: 0, oracle_noncdb: 1, oracle_cdb: 2, mysql_standard: 3, postgresql_standard: 4, sqlserver_standard: 5, host_resource: 6, host_windows: 7, daily_standard: 8 };

function oracleCollectionText(row) {
  const runtime = row.runtime_summary?.by_db_type?.oracle;
  if (runtime?.script_section_count == null || runtime?.health_item_count == null) return "";
  return `数据 SQL 章节 ${runtime.script_section_count} · 健康指标 ${runtime.health_item_count}`;
}

function isLegacyTemplate(row) {
  return row.legacy_template === true || row.policy_key === "oracle_standard";
}

function visibleTemplateCatalog(rows = []) {
  return rows.filter((row) => !isLegacyTemplate(row)).sort((a, b) => (
    (TEMPLATE_ORDER[a.policy_key] ?? 50) - (TEMPLATE_ORDER[b.policy_key] ?? 50)
    || String(a.name || "").localeCompare(String(b.name || ""), "zh-Hans-CN")
  ));
}

function hasPolicyConfiguration(row) {
  const config = row.report_config || {};
  return Boolean(row.instance_ids?.length || config.instance_ids?.length
    || (row.schedule_type && row.schedule_type !== "manual")
    || row.assistant_default === true || config.assistant_default === true
    || config.assistant_default_db_types?.length);
}

function oracleProfileText(row) {
  const profile = row.report_config?.oracle_profile || "auto";
  return ORACLE_PROFILE_OPTIONS.find((item) => item.value === profile)?.label || profile;
}

const CUSTOM_METRIC_VALUE_TYPE_OPTIONS = [
  { value: "number", label: "数值" },
  { value: "text", label: "文本" },
];

const CUSTOM_METRIC_DIRECTION_OPTIONS = [
  { value: "higher_is_worse", label: "升高越危险" },
  { value: "lower_is_worse", label: "降低越危险" },
];

const DB_TYPE_LABELS = {
  oracle: "Oracle",
  mysql: "MySQL",
  postgresql: "PostgreSQL",
  postgres: "PostgreSQL",
  sqlserver: "SQL Server",
  mssql: "SQL Server",
  host: "主机",
};

const TEXT_LABELS = {
  daily_standard: "日常标准巡检",
  host_resource: "主机巡检（Linux / AIX / HP-UX）",
  host_windows: "主机巡检（Windows）",
  manual_or_daily: "手动或每日定时",
  create_ticket: "创建工单",
  push_zabbix: "推送 Zabbix",
  generate_action_plan: "生成处置方案",
  export_report: "导出报告",
  readonly_fallback: "只读降级采集",
  "readonly fallback": "只读降级采集",
  full_inspection: "完整巡检能力",
  default: "默认阈值",
  html_report: "HTML 报告",
  markdown_report: "Markdown 报告",
  critical_alert: "Critical 生成告警",
  basic: "基础信息",
  space: "空间容量",
  performance: "性能",
  security: "安全",
  backup: "备份恢复",
  availability: "高可用",
  governance: "数据治理",
  host: "主机资源",
  connection: "连接可用性",
  logs: "日志检查",
  sessions: "会话状态",
  objects: "对象健康",
  ha: "高可用",
  include_summary: "汇总概览",
  include_findings: "风险明细",
  include_actions: "处置建议",
  include_raw_data: "原始数据",
};

Object.assign(TEXT_LABELS, {
  host_os: "主机基础信息",
  host_cpu: "CPU 与负载",
  host_memory: "内存与 Swap",
  host_storage: "磁盘与 inode",
  host_kernel: "内核参数",
  host_limits: "用户限制",
  host_scheduler: "计划任务",
  oracle_runtime: "Oracle 运行上下文",
  oracle_patch: "Oracle 补丁",
  oracle_listener: "监听与网络",
  rac_cluster: "RAC 集群",
  report_archive: "报告归档",
});

const CATEGORY_OPTIONS = [
  "basic",
  "connection",
  "space",
  "logs",
  "sessions",
  "objects",
  "performance",
  "backup",
  "security",
  "ha",
  "availability",
  "governance",
  "host",
  "host_os",
  "host_cpu",
  "host_memory",
  "host_storage",
  "host_kernel",
  "host_limits",
  "host_scheduler",
  "oracle_runtime",
  "oracle_patch",
  "oracle_listener",
  "rac_cluster",
  "report_archive",
].map((value) => ({ value, label: displayText(value) }));

const ACTION_OPTIONS = [
  "create_ticket",
  "push_zabbix",
  "generate_action_plan",
  "export_report",
  "readonly_fallback",
].map((value) => ({ value, label: displayText(value) }));

const REPORT_OPTION_KEYS = ["include_summary", "include_findings", "include_actions", "include_raw_data"];
const REPORT_OPTIONS = REPORT_OPTION_KEYS.map((value) => ({ value, label: displayText(value) }));

const TEMPLATE_RUNTIME_REPORT_KEYS = new Set([
  "instance_ids",
  "last_run_at",
  "last_status",
  "last_message",
  "assistant_default",
  "assistant_default_db_types",
]);
const INSPECTION_POLICIES_CHANGED_EVENT = "tracemind:inspection-policies-changed";
const INSPECTION_POLICIES_CHANGED_STORAGE_KEY = "tracemind:inspection-policies-version";

function notifyInspectionPoliciesChanged() {
  window.dispatchEvent(new window.CustomEvent(INSPECTION_POLICIES_CHANGED_EVENT));
  try {
    window.localStorage.setItem(INSPECTION_POLICIES_CHANGED_STORAGE_KEY, String(Date.now()));
  } catch (error) {
    // Same-page event still refreshes templates when storage is unavailable.
    void error;
  }
}

function subscribeInspectionPoliciesChanged(load) {
  const handleStorageChange = (event) => {
    if (event.key === INSPECTION_POLICIES_CHANGED_STORAGE_KEY) load();
  };
  window.addEventListener(INSPECTION_POLICIES_CHANGED_EVENT, load);
  window.addEventListener("storage", handleStorageChange);
  return () => {
    window.removeEventListener(INSPECTION_POLICIES_CHANGED_EVENT, load);
    window.removeEventListener("storage", handleStorageChange);
  };
}

const SEVERITY_OPTIONS = [
  { value: "critical", label: "严重" },
  { value: "warning", label: "警告" },
  { value: "info", label: "提示" },
  { value: "normal", label: "健康" },
];

const STANDARD_THRESHOLD_FIELDS = [
  { key: "space_usage_pct", label: "空间预警阈值(%)", min: 1, max: 100, help: "达到后标记为警告。" },
  { key: "critical_space_usage_pct", label: "空间严重阈值(%)", min: 1, max: 100, help: "达到后标记为严重。" },
  { key: "slow_sql_ms", label: "慢 SQL 阈值(ms)", min: 1, help: "SQL 执行耗时超过该值进入慢 SQL 检查。" },
  { key: "blocking_seconds", label: "阻塞时长阈值(秒)", min: 1, help: "会话阻塞超过该值需要处置。" },
  { key: "backup_age_hours", label: "备份过期阈值(小时)", min: 1, help: "最近备份超过该时长视为风险。" },
  { key: "cpu_pct", label: "CPU 使用率阈值(%)", min: 1, max: 100, help: "主机 CPU 使用率预警线。" },
  { key: "memory_pct", label: "内存使用率阈值(%)", min: 1, max: 100, help: "主机内存使用率预警线。" },
  { key: "swap_pct", label: "Swap 使用率阈值(%)", min: 1, max: 100, help: "Swap 使用率预警线。" },
  { key: "filesystem_pct", label: "文件系统阈值(%)", min: 1, max: 100, help: "文件系统使用率预警线。" },
  { key: "critical_filesystem_pct", label: "文件系统严重阈值(%)", min: 1, max: 100, help: "文件系统使用率严重线。" },
  { key: "inode_pct", label: "inode 阈值(%)", min: 1, max: 100, help: "inode 使用率预警线。" },
  { key: "load_per_cpu", label: "单 CPU 负载阈值", min: 0, step: 0.1, help: "系统负载除以 CPU 数后的预警线。" },
];

const STANDARD_THRESHOLD_KEYS = new Set(STANDARD_THRESHOLD_FIELDS.map((item) => item.key));

const TEMPLATE_FIELD_HELP = [
  { label: "技术标识", description: "模板在系统和接口里的唯一编码，建议使用英文、数字和下划线。" },
  { label: "采集源 ID", description: "绑定内置采集器的稳定标识。复制模板后会自动保留，修改检查项名称或技术别名不会改变实际采集来源。" },
  { label: "模板名称", description: "给运维人员看的名称，说明这套巡检模板的用途。" },
  { label: "适用数据库", description: "决定模板能分配给哪些实例，例如 Oracle、MySQL、PostgreSQL、SQL Server 或主机。" },
  { label: "检查分类", description: "用于把检查项分组，例如基础信息、空间容量、性能、备份恢复。" },
  { label: "检查项", description: "每一行代表一个检查项，可添加、删除、启用、停用，并设置风险级别。" },
  { label: "正常范围", description: "指标处于健康状态时应满足的范围，例如 < 85% 或 SYNCHRONIZED。" },
  { label: "警告范围", description: "指标需要关注但尚未严重影响业务的范围。" },
  { label: "严重范围", description: "指标需要立即处理的范围，会优先出现在风险报告里。" },
  { label: "单位", description: "指标值的计量方式，例如 %、秒、小时、次/分钟或状态。" },
  { label: "采集方式", description: "说明指标来自内置 SQL、系统视图、状态变量、主机采集或自定义 SQL。" },
  { label: "采集 SQL", description: "内置指标显示代表 SQL 或脚本章节，自定义指标填写实际查询语句。" },
  { label: "数据来源", description: "自定义 SQL 指标通过已配置的数据库实例连接采集，不使用浏览器直接连接数据库。" },
  { label: "返回字段", description: "SQL 结果中作为指标值的列名，默认使用 metric_value，大小写不敏感。" },
  { label: "查询对象", description: "SQL 的 FROM 和 JOIN 所引用的系统视图、状态表或业务视图。" },
  { label: "测试查询", description: "选择同类型测试实例执行只读查询，确认字段、指标值、阈值和账号权限。" },
  { label: "查询样例", description: "测试查询最多展示二十行结果，用于核对返回结构，不代表完整结果集。" },
  { label: "级别", description: "检查项异常时的默认风险等级，影响报告排序和处置优先级。" },
  { label: "状态", description: "启用后会参与巡检，停用后保留配置但不执行该项。" },
  { label: "阈值", description: "定义判定警告或严重的数字边界，例如空间百分比、慢 SQL 毫秒数。" },
  { label: "动作", description: "巡检发现问题后可以生成处置方案、导出报告、创建工单或推送告警。" },
  { label: "报告", description: "控制报告里包含汇总、风险明细、处置建议或原始数据。" },
  { label: "处理建议", description: "指标异常后给运维人员看的处置方向。" },
];

const TASK_STATUS_TEXT = {
  running: "运行中",
  success: "成功",
  failed: "失败",
  partial: "部分成功",
  canceled: "已取消",
  plan_only: "Plan Only",
};

const TRIGGER_TEXT = {
  manual: "手动",
  schedule: "定时",
  scheduled: "定时",
  batch: "批量",
  api: "API",
};

const WEEKDAY_LABELS = ["", "一", "二", "三", "四", "五", "六", "日"];
const WEEKDAY_OPTIONS = [1, 2, 3, 4, 5, 6, 7].map((value) => ({ value, label: `周${WEEKDAY_LABELS[value]}` }));
const MONTH_DAY_OPTIONS = Array.from({ length: 31 }, (_, index) => {
  const value = index + 1;
  return { value, label: `${value} 日` };
});
const QUARTER_MONTH_OPTIONS = [
  { value: 1, label: "季度第 1 个月" },
  { value: 2, label: "季度第 2 个月" },
  { value: 3, label: "季度第 3 个月" },
];
const POLICY_SCHEDULE_OPTIONS = [
  { value: "manual", label: "手动" },
  { value: "daily", label: "每天" },
  { value: "weekdays", label: "工作日" },
  { value: "weekly", label: "每周" },
  { value: "monthly", label: "每月" },
  { value: "quarterly", label: "每季度" },
];

const RISK_ORDER = { normal: 0, info: 1, warning: 2, critical: 3 };

const normalizeRiskLevel = (level) => {
  if (level === "unknown") return "unknown";
  const value = String(level || "normal").toLowerCase();
  if (["critical", "high", "disaster"].includes(value)) return "critical";
  if (["warning", "average"].includes(value)) return "warning";
  if (["info", "low", "notice"].includes(value)) return "info";
  return "normal";
};
const riskColor = (level) => ({ critical: "red", warning: "orange", info: "blue", normal: "green" }[normalizeRiskLevel(level)] || "default");
const riskText = (level) => ({ critical: "严重", warning: "警告", info: "提示", normal: "健康", unknown: "未确认" }[normalizeRiskLevel(level)] || "-");
const todayText = () => {
  const now = new Date();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${now.getFullYear()}-${month}-${day}`;
};

function scoreTag(score) {
  return <Tag color={score == null ? "default" : score >= 90 ? "green" : score >= 75 ? "blue" : "red"}>{score ?? "-"}</Tag>;
}

function displayText(value) {
  if (value === null || value === undefined || value === "") return "-";
  const key = String(value).toLowerCase();
  return TEXT_LABELS[key] || value;
}

function dbTypeText(value) {
  return DB_TYPE_LABELS[String(value || "").toLowerCase()] || String(value || "-").toUpperCase();
}

function isEnabledValue(value) {
  if (value === undefined || value === null) return true;
  if (typeof value === "string") {
    return !["", "0", "false", "off", "no", "disabled", "停用"].includes(value.trim().toLowerCase());
  }
  return Boolean(value);
}

function isEnabledInstance(item) {
  return isEnabledValue(item?.enabled);
}

function dbTypeTags(values = []) {
  const list = Array.isArray(values) ? values : [values].filter(Boolean);
  if (!list.length) return "-";
  return <Space size={4} wrap>{list.map((item) => <Tag key={item}>{dbTypeText(item)}</Tag>)}</Space>;
}

function toArray(value) {
  if (!value) return [];
  return Array.isArray(value) ? value : [value];
}

function formatDuration(row = {}) {
  if (row.duration_ms !== undefined && row.duration_ms !== null) {
    const seconds = Math.round(Number(row.duration_ms) / 1000);
    if (!Number.isNaN(seconds)) return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
  }
  const start = Date.parse(row.started_at || row.start_time || "");
  const end = Date.parse(row.finished_at || row.end_time || row.create_time || "");
  if (!Number.isNaN(start) && !Number.isNaN(end) && end >= start) {
    const seconds = Math.round((end - start) / 1000);
    return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
  }
  return "-";
}

function riskPenalty(item = {}) {
  const score = Number(item.risk_score ?? item.score);
  if (Number.isFinite(score) && score > 0) return score;
  const level = normalizeRiskLevel(item.severity || item.risk_level);
  return { critical: 15, warning: 5, info: 2, normal: 0 }[level] || 0;
}

function buildTaskView(row = {}) {
  const outcome = inspectionOutcome(row);
  return {
    ...row,
    score: outcome.score,
    overall_score: outcome.score,
    risk_level: outcome.complete ? normalizeRiskLevel(row.risk_level) : "unknown",
    trigger_type: row.trigger_type || "manual",
    task_status: outcome.status,
    started_at: row.started_at || row.start_time || row.create_time,
    finished_at: row.finished_at || row.end_time || row.create_time,
    duration_text: formatDuration(row),
    total_count: row.total_count ?? ((row.normal_count || 0) + (row.info_count || 0) + (row.warning_count || 0) + (row.critical_count || 0)),
    failure_reason: outcome.reason,
  };
}

function isAssistantDefaultTemplate(item, dbType) {
  const reportConfig = item?.report_config || {};
  if (Array.isArray(reportConfig.assistant_default_db_types)) {
    return reportConfig.assistant_default_db_types.includes(dbType);
  }
  return reportConfig.assistant_default === true;
}

function TaskStatusTag({ status }) {
  const value = status || "unknown";
  const color = { running: "processing", success: "green", failed: "red", partial: "orange", canceled: "default", plan_only: "blue" }[value] || "default";
  return <Tag color={color}>{INSPECTION_STATUS_TEXT[value] || TASK_STATUS_TEXT[value] || value}</Tag>;
}

function TriggerTag({ value }) {
  return <Tag>{TRIGGER_TEXT[value] || value || "手动"}</Tag>;
}

function showPlanOnly(action) {
  Modal.info({
    title: `${action}（Plan Only）`,
    content: "当前阶段仅生成方案或展示预览，不会直接执行生产变更。",
  });
}

function RiskInsightCard({ item, index }) {
  const level = normalizeRiskLevel(item?.severity || item?.risk_level);
  const finding = item?.finding || item?.summary || "暂无发现描述";
  const suggestions = toArray(item?.suggestions).join("；");
  const moreMenu = {
    items: [
      { key: "plan", label: "生成处置方案" },
      { key: "alert", label: "转告警" },
      { key: "ticket", label: "转工单" },
    ],
    onClick: ({ key }) => {
      if (key === "plan") showPlanOnly("生成处置方案");
      if (key === "alert") showPlanOnly("转告警");
      if (key === "ticket") showPlanOnly("转工单");
    },
  };
  return (
    <div className={`inspection-risk-card risk-${level}`}>
      <div className="inspection-risk-card-head">
        <Space wrap className="inspection-risk-card-title">
          <Tag color={riskColor(level)}>{riskText(level)}</Tag>
          <LongText value={`${index ? `${index}. ` : ""}${item?.check_item || item?.title || "巡检项"}`} maxWidth={260} />
        </Space>
        <Text type="secondary">扣分 {riskPenalty(item)}</Text>
      </div>
      <div className="inspection-risk-card-body">
        <LongText value={finding} maxWidth="100%" />
        {suggestions && (
          <LongText value={`建议：${suggestions}`} maxWidth="100%" />
        )}
      </div>
      <Space size={6} wrap>
        <Button size="small" disabled>查看证据</Button>
        <Dropdown menu={moreMenu} trigger={["click"]}>
          <Button size="small" icon={<MoreOutlined />}>更多</Button>
        </Dropdown>
      </Space>
    </div>
  );
}

function ExplainEmpty({ description }) {
  return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={description} />;
}

function LongText({ value, maxWidth = 260 }) {
  const text = value || "-";
  return (
    <Tooltip title={text}>
      <Text ellipsis style={{ maxWidth, display: "inline-block" }}>{text}</Text>
    </Tooltip>
  );
}

function summarizeList(values, unit = "项", limit = 3) {
  const rows = toArray(values).map(displayText).filter(Boolean);
  if (!rows.length) return "-";
  if (rows.length <= limit) return rows.join(" / ");
  return `${rows.slice(0, limit).join(" / ")} 等 ${rows.length} ${unit}`;
}

function groupTemplateItems(items = []) {
  const groups = new Map();
  toArray(items).forEach((item) => {
    const category = item.category || "default";
    if (!groups.has(category)) groups.set(category, []);
    groups.get(category).push(item);
  });
  return Array.from(groups.entries()).map(([category, rows]) => ({
    category,
    title: displayText(category),
    rows,
  }));
}

function scoreRiskLevel(score) {
  const value = Number(score);
  if (!Number.isFinite(value)) return "info";
  if (value < 60) return "critical";
  if (value < 80) return "warning";
  return "normal";
}

function shortDateTime(value) {
  const text = String(value || "-");
  const matched = text.match(/\d{4}-\d{2}-\d{2}\s+(\d{2}:\d{2})/);
  return matched ? matched[1] : text;
}

function scheduleText(row = {}) {
  const type = row.schedule_type || "manual";
  const time = row.schedule_time || "02:00";
  const days = toArray(row.schedule_days).length
    ? toArray(row.schedule_days).map((item) => Number(item)).filter(Boolean)
    : (row.schedule_day ? [Number(row.schedule_day)] : []);
  if (type === "daily") return `每天 ${time}`;
  if (type === "weekdays") return `工作日 ${time}`;
  if (type === "weekly") return `每周${(days.length ? days : [1]).map((value) => WEEKDAY_LABELS[value] || value).join("、")} ${time}`;
  if (type === "monthly") return `每月 ${(days.length ? days : [1]).join("、")} 日 ${time}`;
  if (type === "quarterly") return `每季度第 ${row.schedule_month || 1} 个月 ${(days.length ? days : [1]).join("、")} 日 ${time}`;
  return "手动";
}

function hasValue(value) {
  return value !== undefined && value !== null && value !== "";
}

function omitKeys(source = {}, keys = new Set()) {
  return Object.fromEntries(Object.entries(source || {}).filter(([key]) => !keys.has(key)));
}

let templateItemSequence = 0;

function nextTemplateItemKey() {
  templateItemSequence += 1;
  return `custom.check_${Date.now()}_${templateItemSequence}`;
}

function isCustomSqlMetric(item = {}) {
  const sourceType = String(item.source_type || "").trim().toLowerCase();
  const collectMethod = String(item.collect_method || "").trim().toLowerCase();
  return sourceType === "database_sql" || collectMethod.includes("自定义 sql") || collectMethod.includes("custom sql");
}

function queryObjectText(sql) {
  const objects = [];
  const pattern = /\b(?:from|join)\s+([a-z0-9_$#.\[\]`"]+)/gi;
  for (const match of String(sql || "").matchAll(pattern)) {
    const value = String(match[1] || "").replace(/^[\[\]`"]+|[\[\]`"]+$/g, "");
    if (value && !objects.includes(value)) objects.push(value);
  }
  return objects.join("、") || "-";
}

function metricValueText(value) {
  if (value === null || value === undefined || value === "") return "-";
  if (typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(displayText(value));
}

function defaultTemplateItem(overrides = {}) {
  return {
    key: nextTemplateItemKey(),
    name: "自定义检查项",
    category: "basic",
    severity: "warning",
    enabled: true,
    normal_range: "< 80",
    warning_range: ">= 80",
    critical_range: ">= 90",
    unit: "数值/状态",
    collect_method: "自定义 SQL 指标",
    source_type: "database_sql",
    db_type: "oracle",
    result_field: "metric_value",
    value_type: "number",
    threshold_direction: "higher_is_worse",
    warning_value: 80,
    critical_value: 90,
    expected_value: "OK",
    description: "说明这个指标检查什么。",
    advice: "说明异常时如何处理。",
    sql: "",
    ...overrides,
  };
}

function normalizeTemplateItem(item = {}) {
  const normalized = {
    ...item,
    key: String(item.key || "").trim(),
    name: String(item.name || "").trim(),
    category: String(item.category || "basic").trim(),
    severity: normalizeRiskLevel(item.severity || "warning"),
    enabled: isEnabledValue(item.enabled),
    normal_range: String(item.normal_range || "").trim(),
    warning_range: String(item.warning_range || "").trim(),
    critical_range: String(item.critical_range || "").trim(),
    unit: String(item.unit || "").trim(),
    collect_method: String(item.collect_method || "").trim(),
    description: String(item.description || "").trim(),
    advice: String(item.advice || "").trim(),
    sql: String(item.sql || item.query || "").trim(),
  };
  if (isCustomSqlMetric(normalized)) {
    delete normalized.source_key;
    normalized.source_type = "database_sql";
    normalized.db_type = String(item.db_type || "").trim().toLowerCase();
    normalized.result_field = String(item.result_field || "metric_value").trim();
    normalized.value_type = String(item.value_type || "number").trim().toLowerCase();
    normalized.threshold_direction = String(item.threshold_direction || "higher_is_worse").trim().toLowerCase();
    normalized.warning_value = hasValue(item.warning_value) ? Number(item.warning_value) : 80;
    normalized.critical_value = hasValue(item.critical_value) ? Number(item.critical_value) : 90;
    normalized.expected_value = String(item.expected_value || "OK").trim();
  } else {
    normalized.source_key = String(item.source_key || item.key || "").trim();
  }
  delete normalized.test_instance_id;
  return normalized;
}

function templateItemsForForm(items = []) {
  const rows = toArray(items)
    .map(normalizeTemplateItem)
    .filter((item) => item.key || item.name);
  return rows.length ? rows : [defaultTemplateItem()];
}

function cleanTemplateItems(items = []) {
  return toArray(items)
    .map(normalizeTemplateItem)
    .filter((item) => item.key && item.name);
}

function thresholdValuesForForm(config = {}) {
  const known = {};
  for (const field of STANDARD_THRESHOLD_FIELDS) {
    if (hasValue(config[field.key])) known[field.key] = config[field.key];
  }
  return known;
}

function thresholdExtraText(config = {}) {
  const extra = omitKeys(config, STANDARD_THRESHOLD_KEYS);
  return Object.keys(extra).length ? jsonText(extra) : "";
}

function buildThresholdConfig(values = {}) {
  const known = {};
  for (const [key, value] of Object.entries(values.threshold_config || {})) {
    if (hasValue(value)) known[key] = value;
  }
  return {
    ...parseJsonField(values.threshold_extra_text, {}),
    ...known,
  };
}

function reportOptionsForForm(config = {}) {
  const hasAny = REPORT_OPTION_KEYS.some((key) => Object.prototype.hasOwnProperty.call(config, key));
  if (!hasAny) return ["include_summary", "include_findings", "include_actions"];
  return REPORT_OPTION_KEYS.filter((key) => config[key] === true);
}

function reportExtraText(config = {}) {
  const hiddenKeys = new Set([...REPORT_OPTION_KEYS, ...TEMPLATE_RUNTIME_REPORT_KEYS, "oracle_profile"]);
  const extra = omitKeys(config, hiddenKeys);
  return Object.keys(extra).length ? jsonText(extra) : "";
}

function buildReportConfig(values = {}, initialReportConfig = {}, initialDbTypes = []) {
  const reportConfig = {
    ...parseJsonField(values.report_extra_text, {}),
  };
  if (toArray(values.db_types).includes("oracle")) {
    reportConfig.oracle_profile = values.oracle_profile || "auto";
    if (reportConfig.oracle_profile !== (initialReportConfig.oracle_profile || "auto")) {
      // Let the backend bind the exact script for the newly selected profile.
      delete reportConfig.script_name;
    }
  } else {
    delete reportConfig.oracle_profile;
    const hadOracleScript = (initialReportConfig.oracle_profile && initialReportConfig.oracle_profile !== "auto")
      || (initialDbTypes.length === 1 && initialDbTypes.includes("oracle"))
      || ["dbcheck10g.sql", "dbcheck_noncdb.sql", "dbcheck_cdb.sql"].includes(initialReportConfig.script_name);
    if (hadOracleScript && reportConfig.script_name === initialReportConfig.script_name) {
      delete reportConfig.script_name;
    }
  }
  for (const key of REPORT_OPTION_KEYS) {
    reportConfig[key] = toArray(values.report_options).includes(key);
  }
  for (const key of ["last_run_at", "last_status", "last_message"]) {
    if (initialReportConfig?.[key] !== undefined) reportConfig[key] = initialReportConfig[key];
  }
  reportConfig.assistant_default = values.assistant_default === true;
  reportConfig.assistant_default_db_types = values.assistant_default === true
    ? toArray(values.db_types)
    : [];
  return reportConfig;
}

function templateItemHelp(item = {}) {
  if (item.description) return item.description;
  const category = displayText(item.category || "basic");
  return `归属 ${category}，巡检时采集并判断 ${item.name || item.key || "该项目"} 的状态。`;
}

function rangeText(value) {
  return value || "-";
}

function templateTypeText(row = {}) {
  const dbTypes = toArray(row.db_types);
  if (String(row.policy_key || "").includes("host")) return "主机资源模板";
  if (dbTypes.length === 1) return `${dbTypeText(dbTypes[0])} 专属巡检模板`;
  return "标准巡检模板";
}

function buildMetricLibrary(policies = [], selectedDbTypes = []) {
  const selected = new Set(toArray(selectedDbTypes));
  const seen = new Set();
  const metrics = [];
  for (const policy of toArray(policies)) {
    const dbTypes = toArray(policy.db_types);
    if (selected.size && !dbTypes.some((dbType) => selected.has(dbType))) continue;
    for (const item of toArray(policy.item_config)) {
      const normalized = normalizeTemplateItem(item);
      if (!normalized.key || !normalized.name) continue;
      const uniqueKey = `${policy.policy_key || policy.name || "policy"}::${normalized.key}`;
      if (seen.has(uniqueKey)) continue;
      seen.add(uniqueKey);
      metrics.push({
        ...normalized,
        source_template: policy.name || policy.policy_key || "内置模板",
        source_db_text: dbTypes.map(dbTypeText).join("/") || "通用",
        library_key: uniqueKey,
      });
    }
  }
  const options = metrics.map((item) => ({
    value: item.library_key,
    label: `${item.source_db_text}｜${item.name}｜正常 ${item.normal_range || "-"}`,
  }));
  return { metrics, options, byKey: new Map(metrics.map((item) => [item.library_key, item])) };
}

function TemplateFieldHelpTrigger({ compact = false }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <div className={compact ? "inspection-field-help-note compact" : "inspection-field-help-note"}>
        <Text type="secondary">需要了解每个字段的含义时查看说明。</Text>
        <Button
          className="inspection-field-help-trigger"
          size="small"
          icon={<InfoCircleOutlined />}
          onClick={() => setOpen(true)}
        >
          字段说明
        </Button>
      </div>
      <Modal
        title="字段说明"
        open={open}
        onCancel={() => setOpen(false)}
        footer={<Button onClick={() => setOpen(false)}>关闭</Button>}
        width={760}
        destroyOnClose
      >
        <Table
          size="small"
          rowKey="label"
          pagination={false}
          dataSource={TEMPLATE_FIELD_HELP}
          scroll={{ x: 620 }}
          columns={[
            { title: "字段", dataIndex: "label", width: 150, render: (value) => <Text strong>{value}</Text> },
            { title: "说明", dataIndex: "description", render: (value) => <Text type="secondary">{value}</Text> },
          ]}
        />
      </Modal>
    </>
  );
}

function jsonText(value) {
  try {
    return JSON.stringify(value || {}, null, 2);
  } catch {
    return "{}";
  }
}

function parseJsonField(text, fallback) {
  if (!String(text || "").trim()) return fallback;
  try {
    return JSON.parse(text);
  } catch {
    throw new Error("JSON 配置格式不正确");
  }
}

function InspectionPolicyEditor({ open, mode, initial, instances = [], metricLibraryPolicies = [], variant = "policy", onCancel, onSaved }) {
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);
  const [selectedMetricKey, setSelectedMetricKey] = useState();
  const [metricTestContext, setMetricTestContext] = useState(null);
  const [metricTestInstanceId, setMetricTestInstanceId] = useState();
  const [metricTestLoading, setMetricTestLoading] = useState(false);
  const [metricTestResult, setMetricTestResult] = useState(null);
  const scheduleType = Form.useWatch("schedule_type", form);
  const dbTypes = Form.useWatch("db_types", form) || [];
  const isTemplateEditor = variant === "template";
  const entityName = isTemplateEditor ? "巡检模板" : "巡检策略";
  const metricLibrary = useMemo(
    () => buildMetricLibrary(metricLibraryPolicies, dbTypes),
    [metricLibraryPolicies, dbTypes],
  );
  const metricDbTypeOptions = DATABASE_DB_TYPE_OPTIONS.filter(
    (item) => !dbTypes.length || dbTypes.includes(item.value),
  );

  useEffect(() => {
    if (!open) return;
    const row = initial || {};
    const isCreate = mode === "create";
    const thresholdConfig = row.threshold_config || {};
    const reportConfig = row.report_config || {};
    form.setFieldsValue({
      policy_key: isCreate ? `custom_${Date.now()}` : row.policy_key,
      name: isCreate ? "自定义巡检模板" : row.name,
      db_types: row.db_types?.length ? row.db_types : ["oracle", "mysql"],
	      enabled: row.enabled !== false,
	      schedule_type: row.schedule_type || "manual",
	      schedule_day: row.schedule_day || 1,
	      schedule_days: toArray(row.schedule_days).length
	        ? toArray(row.schedule_days).map((value) => Number(value)).filter(Boolean)
	        : (row.schedule_day ? [Number(row.schedule_day)] : [1]),
	      schedule_month: row.schedule_month || 1,
	      schedule_time: row.schedule_time || "02:00",
      instance_ids: row.instance_ids || [],
      categories: toArray(row.categories).length ? toArray(row.categories) : ["basic", "space", "performance"],
      action_config: toArray(row.action_config).length ? toArray(row.action_config) : ["generate_action_plan", "export_report"],
      item_config: templateItemsForForm(row.item_config),
      threshold_config: thresholdValuesForForm(thresholdConfig),
      threshold_extra_text: thresholdExtraText(thresholdConfig),
      report_options: reportOptionsForForm(reportConfig),
      oracle_profile: reportConfig.oracle_profile || "auto",
      report_extra_text: reportExtraText(reportConfig),
      assistant_default: Array.isArray(reportConfig.assistant_default_db_types)
        ? reportConfig.assistant_default_db_types.some((value) => (row.db_types || []).includes(value))
        : reportConfig.assistant_default === true,
      remark: row.remark || "",
    });
    setSelectedMetricKey(undefined);
    setMetricTestContext(null);
    setMetricTestInstanceId(undefined);
    setMetricTestResult(null);
  }, [form, initial, mode, open]);

  const instanceOptions = (instances || [])
    .filter((item) => !dbTypes.length || dbTypes.includes(item.db_type))
    .map((item) => ({ value: item.id, label: `${item.instance_name} (${dbTypeText(item.db_type)} ${item.host}:${item.port})` }));

  const metricTestInstances = (instances || []).filter((item) => (
    item.db_type === metricTestContext?.metric?.db_type && isEnabledInstance(item)
  ));
  const metricTestInstanceOptions = metricTestInstances.map((item) => ({
    value: item.id,
    label: `${item.instance_name} (${dbTypeText(item.db_type)} ${item.host}:${item.port})`,
  }));
  const metricTestColumns = toArray(metricTestResult?.columns).map((column) => ({
    title: column,
    dataIndex: column,
    key: column,
    width: 180,
    ellipsis: true,
    render: (value) => <LongText value={metricValueText(value)} maxWidth={160} />,
  }));

  const openMetricTest = (itemIndex) => {
    const rawMetric = form.getFieldValue(["item_config", itemIndex]) || {};
    const dbType = rawMetric.db_type || dbTypes.find((value) => value !== "host") || "";
    if (!dbType) {
      message.warning("请先选择指标适用数据库");
      return;
    }
    const metric = normalizeTemplateItem({ ...rawMetric, source_type: "database_sql", db_type: dbType });
    const candidates = (instances || []).filter((item) => item.db_type === dbType && isEnabledInstance(item));
    setMetricTestContext({ itemIndex, metric });
    setMetricTestInstanceId(candidates[0]?.id);
    setMetricTestResult(null);
    if (!candidates.length) message.warning(`暂无已启用的 ${dbTypeText(dbType)} 实例可供测试`);
  };

  const closeMetricTest = () => {
    if (metricTestLoading) return;
    setMetricTestContext(null);
    setMetricTestInstanceId(undefined);
    setMetricTestResult(null);
  };

  const runCustomMetricTest = async () => {
    if (!metricTestContext || !metricTestInstanceId) {
      message.warning("请选择测试实例");
      return;
    }
    const rawMetric = form.getFieldValue(["item_config", metricTestContext.itemIndex]) || metricTestContext.metric;
    const metric = normalizeTemplateItem({
      ...rawMetric,
      source_type: "database_sql",
      db_type: rawMetric.db_type || metricTestContext.metric.db_type,
    });
    setMetricTestLoading(true);
    setMetricTestResult(null);
    try {
      const result = await testInspectionCustomMetric({ instance_id: metricTestInstanceId, metric });
      setMetricTestResult(result);
      message.success("测试查询完成");
    } catch (e) {
      message.error(e.message || "测试查询失败");
    } finally {
      setMetricTestLoading(false);
    }
  };

  const save = async () => {
    try {
      const values = await form.validateFields();
      setSaving(true);
      const itemConfig = cleanTemplateItems(values.item_config);
      if (!itemConfig.length) throw new Error("至少需要保留一个有效检查项");
      const payload = {
        policy_key: values.policy_key,
        name: values.name,
        db_types: values.db_types || [],
	        enabled: values.enabled,
	        schedule_type: values.schedule_type || "manual",
	        schedule_day: values.schedule_day || values.schedule_days?.[0] || null,
	        schedule_days: values.schedule_days || [],
	        schedule_month: values.schedule_month || null,
	        schedule_time: values.schedule_time || "02:00",
        instance_ids: values.instance_ids || [],
        categories: values.categories || [],
        item_config: itemConfig,
        threshold_config: buildThresholdConfig(values),
        action_config: values.action_config || [],
        report_config: buildReportConfig(values, initial?.report_config || {}, initial?.db_types || []),
        remark: values.remark,
      };
      if (mode === "edit") {
        await updateInspectionPolicy(initial.id, payload);
        message.success(`${entityName}已更新`);
      } else {
        await createInspectionPolicy(payload);
	        message.success(`${entityName}已创建`);
      }
      notifyInspectionPoliciesChanged();
      onSaved?.();
    } catch (e) {
      if (!e?.errorFields) message.error(e.message || "保存失败");
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <Modal
	        title={mode === "edit" ? `编辑${entityName}` : `新增${entityName}`}
        open={open}
        onCancel={() => {
          setMetricTestContext(null);
          onCancel?.();
        }}
        onOk={save}
        confirmLoading={saving}
        width={980}
        className="inspection-policy-editor-modal"
        destroyOnClose
      >
        <div className="inspection-policy-editor-body">
          <Form form={form} layout="vertical">
          <Alert
            type="info"
            showIcon
	            message={`怎么填写${entityName}`}
	            description={isTemplateEditor
	              ? "模板由基础信息、适用数据库、检查项、阈值、动作和报告内容组成。每一行代表一个检查项，可以按数据库类型添加或删除。"
	              : "策略由基础信息、执行计划、检查分类、检查项、阈值、动作和报告内容组成。每一行代表一个检查项，可以按巡检范围添加或删除。"}
          />
          <TemplateFieldHelpTrigger />
          <Row gutter={16}>
            <Col xs={24} md={10}><Form.Item name="policy_key" label="模板 ID" rules={[{ required: true }]}><Input disabled={mode === "edit"} /></Form.Item></Col>
	            <Col xs={24} md={14}><Form.Item name="name" label={isTemplateEditor ? "模板名称" : "策略名称"} rules={[{ required: true }]}><Input /></Form.Item></Col>
	            <Col xs={24} md={12}><Form.Item name="db_types" label="适用数据库" rules={[{ required: true }]}><Select mode="multiple" options={DB_TYPE_OPTIONS} /></Form.Item></Col>
            {dbTypes.includes("oracle") && (
              <Col xs={24} md={12}>
                <Form.Item name="oracle_profile" label="Oracle 适用架构">
                  <Select options={ORACLE_PROFILE_OPTIONS} />
                </Form.Item>
              </Col>
            )}
	            <Col xs={24} md={12}><Form.Item name="instance_ids" label="分配实例"><Select mode="multiple" allowClear options={instanceOptions} placeholder="选择每周/每月自动巡检实例" /></Form.Item></Col>
	            <Col xs={24} md={8}><Form.Item name="schedule_type" label="执行周期"><Select options={POLICY_SCHEDULE_OPTIONS} /></Form.Item></Col>
	            {scheduleType === "weekly" && (
	              <Col xs={24} md={8}>
	                <Form.Item name="schedule_days" label="每周几" rules={[{ required: true, message: "请选择执行星期" }]}>
	                  <Select mode="multiple" options={WEEKDAY_OPTIONS} placeholder="选择周几" />
	                </Form.Item>
	              </Col>
	            )}
	            {scheduleType === "monthly" && (
	              <Col xs={24} md={8}>
	                <Form.Item name="schedule_days" label="每月几日" rules={[{ required: true, message: "请选择执行日期" }]}>
	                  <Select mode="multiple" options={MONTH_DAY_OPTIONS} placeholder="选择日期，可多选" />
	                </Form.Item>
	              </Col>
	            )}
	            {scheduleType === "quarterly" && (
	              <>
	                <Col xs={24} md={8}><Form.Item name="schedule_month" label="季度月份"><Select options={QUARTER_MONTH_OPTIONS} /></Form.Item></Col>
	                <Col xs={24} md={8}>
	                  <Form.Item name="schedule_days" label="季度日期" rules={[{ required: true, message: "请选择执行日期" }]}>
	                    <Select mode="multiple" options={MONTH_DAY_OPTIONS} placeholder="选择日期，可多选" />
	                  </Form.Item>
	                </Col>
	              </>
	            )}
	            {scheduleType !== "manual" && (
	              <Col xs={24} md={8}>
	                <Form.Item
	                  name="schedule_time"
	                  label="执行时间"
	                  rules={[{ pattern: /^([01]\d|2[0-3]):[0-5]\d$/, message: "请输入 HH:mm，例如 02:00" }]}
	                >
	                  <Input placeholder="02:00" />
	                </Form.Item>
	              </Col>
            )}
            <Col xs={24} md={8}><Form.Item name="enabled" label="状态"><BinaryStatusSwitch trueValue={true} falseValue={false} /></Form.Item></Col>
            {!initial?.read_only && (
              <Col xs={24} md={8}>
                <Form.Item
                  name="assistant_default"
                  label="智能助手默认"
                  tooltip="小助手执行全部数据库巡检时，优先使用该模板。每种数据库只保留一个助手默认模板。"
                >
                  <BinaryStatusSwitch
                    trueValue={true}
                    falseValue={false}
                    checkedChildren="默认"
                    unCheckedChildren="普通"
                  />
                </Form.Item>
              </Col>
            )}
          </Row>
          <Row gutter={16}>
            <Col xs={24} md={12}>
              <Form.Item name="categories" label="检查分类" rules={[{ required: true, message: "请选择检查分类" }]}>
                <Select mode="tags" options={CATEGORY_OPTIONS} optionFilterProp="label" placeholder="选择或输入分类" />
              </Form.Item>
            </Col>
            <Col xs={24} md={12}>
              <Form.Item name="action_config" label="动作规则">
                <Select mode="multiple" options={ACTION_OPTIONS} optionFilterProp="label" placeholder="选择巡检后的动作" />
              </Form.Item>
            </Col>
          </Row>

          <div className="inspection-editor-section">
            <div className="inspection-editor-section-title">检查项设置</div>
            <Text type="secondary">每一行代表一个检查项；删除后该项不会再参与该模板的巡检。可以从指标库添加，也可以新增自定义 SQL 指标。</Text>
            <Form.List name="item_config">
              {(fields, { add, remove }) => (
                <Space direction="vertical" size={10} className="full-width inspection-item-editor-list">
                  <Space wrap className="inspection-metric-library-bar">
                    <Select
                      allowClear
                      showSearch
                      value={selectedMetricKey}
                      onChange={setSelectedMetricKey}
                      options={metricLibrary.options}
                      optionFilterProp="label"
                      placeholder="从指标库添加"
                      style={{ width: 420 }}
                    />
                    <Button
                      icon={<PlusOutlined />}
                      onClick={() => {
                        const item = metricLibrary.byKey.get(selectedMetricKey);
                        if (!item) {
                          message.warning("请先选择一个指标库指标");
                          return;
                        }
                        const currentKeys = new Set(toArray(form.getFieldValue("item_config")).map((row) => row?.key));
                        if (currentKeys.has(item.key)) {
                          message.warning("该指标已经在模板中");
                          return;
                        }
                        add(normalizeTemplateItem({ ...item, enabled: true }));
                        setSelectedMetricKey(undefined);
                      }}
                    >
                      添加到模板
                    </Button>
                    <Button
                      icon={<PlusOutlined />}
                      onClick={() => add(defaultTemplateItem({
                        collect_method: "自定义 SQL 指标",
                        db_type: dbTypes.find((value) => value !== "host") || "oracle",
                        sql: "select 1 as metric_value",
                      }))}
                    >
                      自定义 SQL 指标
                    </Button>
                  </Space>
                  {fields.map(({ key, name, ...restField }, index) => (
                    <div className="inspection-item-editor-row" key={key}>
                      <Form.Item {...restField} name={[name, "source_key"]} hidden><Input /></Form.Item>
                      <Form.Item {...restField} name={[name, "script_name"]} hidden><Input /></Form.Item>
                      <Form.Item {...restField} name={[name, "script_section"]} hidden><Input /></Form.Item>
                      <Form.Item {...restField} name={[name, "script_line"]} hidden><InputNumber /></Form.Item>
                      <Row gutter={[10, 8]} align="middle">
                        <Col xs={24} md={6}>
                          <Form.Item {...restField} name={[name, "name"]} label={index === 0 ? "检查项名称" : undefined} rules={[{ required: true, message: "请输入检查项名称" }]}>
                            <Input placeholder="例如：表空间使用率" />
                          </Form.Item>
                        </Col>
                        <Col xs={24} md={7}>
                          <Form.Item {...restField} name={[name, "key"]} label={index === 0 ? "技术标识" : undefined} rules={[{ required: true, message: "请输入技术标识" }]}>
                            <Input placeholder="例如：space.capacity" />
                          </Form.Item>
                        </Col>
                        <Col xs={24} md={4}>
                          <Form.Item {...restField} name={[name, "category"]} label={index === 0 ? "分类" : undefined} rules={[{ required: true, message: "请选择分类" }]}>
                            <Select options={CATEGORY_OPTIONS} optionFilterProp="label" showSearch />
                          </Form.Item>
                        </Col>
                        <Col xs={12} md={3}>
                          <Form.Item {...restField} name={[name, "severity"]} label={index === 0 ? "级别" : undefined}>
                            <Select options={SEVERITY_OPTIONS} />
                          </Form.Item>
                        </Col>
                        <Col xs={12} md={2}>
                          <Form.Item {...restField} name={[name, "enabled"]} label={index === 0 ? "状态" : undefined}>
                            <Select options={[{ value: true, label: "启用" }, { value: false, label: "停用" }]} />
                          </Form.Item>
                        </Col>
                        <Col xs={24} md={2} className="inspection-item-editor-actions">
                          <Button danger icon={<DeleteOutlined />} onClick={() => remove(name)}>删除</Button>
                        </Col>
                      </Row>
                      <Row gutter={[10, 8]} className="inspection-item-standard-row">
                        <Col xs={24} md={6}>
                          <Form.Item {...restField} name={[name, "normal_range"]} label="正常范围" rules={[{ required: true, message: "请输入正常范围" }]}>
                            <Input placeholder="< 85%" />
                          </Form.Item>
                        </Col>
                        <Col xs={24} md={6}>
                          <Form.Item {...restField} name={[name, "warning_range"]} label="警告范围" rules={[{ required: true, message: "请输入警告范围" }]}>
                            <Input placeholder="85% - 95%" />
                          </Form.Item>
                        </Col>
                        <Col xs={24} md={6}>
                          <Form.Item {...restField} name={[name, "critical_range"]} label="严重范围" rules={[{ required: true, message: "请输入严重范围" }]}>
                            <Input placeholder=">= 95%" />
                          </Form.Item>
                        </Col>
                        <Col xs={24} md={3}>
                          <Form.Item {...restField} name={[name, "unit"]} label="单位" rules={[{ required: true, message: "请输入单位" }]}>
                            <Input placeholder="% / 秒" />
                          </Form.Item>
                        </Col>
                        <Col xs={24} md={3}>
                          <Form.Item {...restField} name={[name, "collect_method"]} label="采集方式" rules={[{ required: true, message: "请输入采集方式" }]}>
                            <Input placeholder="内置 SQL 采集" />
                          </Form.Item>
                        </Col>
                      </Row>
                      <Row gutter={[10, 8]} className="inspection-item-standard-row">
                        <Col xs={24} md={8}>
                          <Form.Item {...restField} name={[name, "description"]} label="指标说明">
                            <Input.TextArea rows={2} placeholder="说明这个指标检查什么" />
                          </Form.Item>
                        </Col>
                        <Col xs={24} md={8}>
                          <Form.Item {...restField} name={[name, "advice"]} label="处理建议" rules={[{ required: true, message: "请输入处理建议" }]}>
                            <Input.TextArea rows={2} placeholder="异常时怎么处理" />
                          </Form.Item>
                        </Col>
                        <Col xs={24} md={8}>
                          <Form.Item
                            {...restField}
                            name={[name, "sql"]}
                            label="采集 SQL / 自定义 SQL"
                            rules={[{
                              validator: (_, value) => {
                                const item = form.getFieldValue(["item_config", name]) || {};
                                if (isCustomSqlMetric(item) && !String(value || "").trim()) {
                                  return Promise.reject(new Error("请填写自定义指标查询 SQL"));
                                }
                                return Promise.resolve();
                              },
                            }]}
                          >
                            <Input.TextArea rows={2} placeholder="内置指标展示代表 SQL；自定义 SQL 指标填写查询语句，返回 metric_value" />
                          </Form.Item>
                        </Col>
                      </Row>
                      <Form.Item
                        noStyle
                        shouldUpdate={(previous, current) => {
                          const before = previous.item_config?.[name] || {};
                          const after = current.item_config?.[name] || {};
                          return before.collect_method !== after.collect_method
                            || before.source_type !== after.source_type
                            || before.value_type !== after.value_type;
                        }}
                      >
                        {({ getFieldValue }) => {
                          const metric = getFieldValue(["item_config", name]) || {};
                          if (!isCustomSqlMetric(metric)) return null;
                          return (
                            <div className="inspection-custom-metric-source">
                              <div className="inspection-custom-metric-source-head">
                                <Space wrap>
                                  <DatabaseOutlined />
                                  <Text strong>数据来源</Text>
                                  <Tag color="blue">数据库 SQL</Tag>
                                </Space>
                                <Button
                                  icon={<ExperimentOutlined />}
                                  onClick={() => openMetricTest(name)}
                                >
                                  测试查询
                                </Button>
                              </div>
                              <Row gutter={[10, 8]}>
                                <Col xs={24} md={6}>
                                  <Form.Item
                                    {...restField}
                                    name={[name, "db_type"]}
                                    label="适用数据库"
                                    rules={[{ required: true, message: "请选择适用数据库" }]}
                                  >
                                    <Select options={metricDbTypeOptions} />
                                  </Form.Item>
                                </Col>
                                <Col xs={24} md={6}>
                                  <Form.Item
                                    {...restField}
                                    name={[name, "result_field"]}
                                    label="返回字段"
                                    rules={[
                                      { required: true, message: "请输入返回字段" },
                                      { pattern: /^[A-Za-z_][A-Za-z0-9_$#]*$/, message: "字段名格式不正确" },
                                    ]}
                                  >
                                    <Input placeholder="metric_value" />
                                  </Form.Item>
                                </Col>
                                <Col xs={24} md={6}>
                                  <Form.Item {...restField} name={[name, "value_type"]} label="指标值类型">
                                    <Select options={CUSTOM_METRIC_VALUE_TYPE_OPTIONS} />
                                  </Form.Item>
                                </Col>
                                {metric.value_type === "text" ? (
                                  <Col xs={24} md={6}>
                                    <Form.Item
                                      {...restField}
                                      name={[name, "expected_value"]}
                                      label="正常期望值"
                                      rules={[{ required: true, message: "请输入正常期望值" }]}
                                    >
                                      <Input placeholder="例如 OK" />
                                    </Form.Item>
                                  </Col>
                                ) : (
                                  <>
                                    <Col xs={24} md={6}>
                                      <Form.Item {...restField} name={[name, "threshold_direction"]} label="阈值方向">
                                        <Select options={CUSTOM_METRIC_DIRECTION_OPTIONS} />
                                      </Form.Item>
                                    </Col>
                                    <Col xs={24} md={6}>
                                      <Form.Item
                                        {...restField}
                                        name={[name, "warning_value"]}
                                        label="预警阈值"
                                        rules={[{ required: true, message: "请输入预警阈值" }]}
                                      >
                                        <InputNumber className="full-width" />
                                      </Form.Item>
                                    </Col>
                                    <Col xs={24} md={6}>
                                      <Form.Item
                                        {...restField}
                                        name={[name, "critical_value"]}
                                        label="严重阈值"
                                        rules={[{ required: true, message: "请输入严重阈值" }]}
                                      >
                                        <InputNumber className="full-width" />
                                      </Form.Item>
                                    </Col>
                                  </>
                                )}
                              </Row>
                            </div>
                          );
                        }}
                      </Form.Item>
                    </div>
                  ))}
                  <Button type="dashed" icon={<PlusOutlined />} onClick={() => add(defaultTemplateItem())}>新增检查项</Button>
                </Space>
              )}
            </Form.List>
          </div>

          <div className="inspection-editor-section">
            <div className="inspection-editor-section-title">阈值设置</div>
            <Row gutter={16}>
              {STANDARD_THRESHOLD_FIELDS.map((field) => (
                <Col xs={24} md={8} key={field.key}>
                  <Form.Item name={["threshold_config", field.key]} label={field.label} tooltip={field.help}>
                    <InputNumber min={field.min} max={field.max} step={field.step || 1} className="full-width" />
                  </Form.Item>
                </Col>
              ))}
            </Row>
            <Form.Item name="threshold_extra_text" label="高级阈值 JSON" extra="只有不在上方数字输入中的阈值需要写在这里。">
              <Input.TextArea rows={3} placeholder='{"custom_threshold": 10}' />
            </Form.Item>
          </div>

          <div className="inspection-editor-section">
            <div className="inspection-editor-section-title">报告内容</div>
            <Form.Item name="report_options" label="报告内容">
              <Checkbox.Group options={REPORT_OPTIONS} />
            </Form.Item>
            <Form.Item name="report_extra_text" label="高级报告配置 JSON" extra="脚本名、输出文件等扩展配置会保留在这里。">
              <Input.TextArea rows={3} placeholder='{"outputs": ["html report"]}' />
            </Form.Item>
          </div>

          <Form.Item name="remark" label="说明"><Input.TextArea rows={2} /></Form.Item>
          </Form>
        </div>
      </Modal>
      <Modal
        title={`测试查询${metricTestContext?.metric?.name ? ` - ${metricTestContext.metric.name}` : ""}`}
        open={Boolean(metricTestContext)}
        width={920}
        className="inspection-custom-metric-test-modal"
        onCancel={closeMetricTest}
        maskClosable={!metricTestLoading}
        closable={!metricTestLoading}
        destroyOnClose
        footer={[
          <Button key="cancel" disabled={metricTestLoading} onClick={closeMetricTest}>关闭</Button>,
          <Button
            key="test"
            type="primary"
            icon={<ExperimentOutlined />}
            loading={metricTestLoading}
            disabled={!metricTestInstanceId}
            onClick={runCustomMetricTest}
          >
            执行只读测试
          </Button>,
        ]}
      >
        <div className="inspection-custom-metric-test-controls">
          <Text strong>测试实例</Text>
          <Select
            showSearch
            value={metricTestInstanceId}
            options={metricTestInstanceOptions}
            optionFilterProp="label"
            placeholder={`选择 ${dbTypeText(metricTestContext?.metric?.db_type)} 实例`}
            onChange={(value) => {
              setMetricTestInstanceId(value);
              setMetricTestResult(null);
            }}
          />
          <Text type="secondary">查询仅返回最多 20 行样例，并在只读事务中执行。</Text>
        </div>
        {metricTestResult && (
          <div className="inspection-custom-metric-test-result">
            <Descriptions bordered size="small" column={{ xs: 1, sm: 2 }}>
              <Descriptions.Item label="数据来源">数据库 SQL</Descriptions.Item>
              <Descriptions.Item label="测试实例">{metricTestResult.source?.instance_name || "-"}</Descriptions.Item>
              <Descriptions.Item label="数据库类型">{dbTypeText(metricTestResult.source?.db_type)}</Descriptions.Item>
              <Descriptions.Item label="连接地址">
                {metricTestResult.source?.host ? `${metricTestResult.source.host}:${metricTestResult.source.port}` : "-"}
              </Descriptions.Item>
              <Descriptions.Item label="查询对象">
                {toArray(metricTestResult.source?.query_objects).join("、") || "无固定表或视图"}
              </Descriptions.Item>
              <Descriptions.Item label="返回字段">{metricTestResult.source?.result_field || "-"}</Descriptions.Item>
              <Descriptions.Item label="指标值">{metricValueText(metricTestResult.metric_value)}</Descriptions.Item>
              <Descriptions.Item label="判定">
                <Tag color={riskColor(metricTestResult.status)}>{riskText(metricTestResult.status)}</Tag>
              </Descriptions.Item>
              <Descriptions.Item label="判定阈值">{metricTestResult.threshold || "-"}</Descriptions.Item>
              <Descriptions.Item label="查询耗时">{metricTestResult.duration_ms ?? "-"} ms</Descriptions.Item>
            </Descriptions>
            <div className="inspection-custom-metric-sample-head">
              <Text strong>查询样例</Text>
              {metricTestResult.truncated && <Tag color="gold">仅展示前 20 行</Tag>}
            </div>
            <Table
              size="small"
              rowKey={(row) => stableRecordKey("inspection-custom-metric-sample", row, metricTestResult.columns || [])}
              pagination={false}
              dataSource={metricTestResult.rows || []}
              columns={metricTestColumns}
              scroll={{ x: Math.max(640, metricTestColumns.length * 180), y: 280 }}
            />
          </div>
        )}
      </Modal>
    </>
  );
}

function KpiDashboardCard({ title, value, suffix, trend, icon, tone = "info" }) {
  return (
    <Card className={`inspection-kpi-card tone-${tone}`}>
      <div className="inspection-kpi-content">
        <div className="inspection-kpi-icon">{icon}</div>
        <div className="inspection-kpi-text">
          <Text type="secondary" className="inspection-kpi-title">{title}</Text>
          <div className="inspection-kpi-value">
            {value}
            {suffix && <span>{suffix}</span>}
          </div>
          <Text type="secondary" className="inspection-kpi-trend">{trend}</Text>
        </div>
      </div>
    </Card>
  );
}

function OverviewStatusPill({ level }) {
  const normalized = normalizeRiskLevel(level);
  return <span className={`inspection-status-pill status-${normalized}`}>{riskText(normalized)}</span>;
}

function InspectionReportDrawer({ detail, open, onClose }) {
  const task = detail?.task || {};
  const exportWord = async () => {
    if (!task.instance_id || !task.task_id) return;
    await downloadInspectionReportWord(task.instance_id, task.task_id);
  };
  const exportMarkdown = async () => {
    if (!task.task_id) return;
    await downloadInspectionReportMarkdown(task.task_id, { instance_id: task.instance_id });
  };
  const exportHtml = async () => {
    if (!task.task_id) return;
    await downloadInspectionReportHtml(task.task_id, { instance_id: task.instance_id });
  };
  return (
    <Modal
      className="inspection-report-modal"
      open={open}
      onCancel={onClose}
      footer={null}
      centered
      width={1360}
      destroyOnHidden
      title={detail ? `巡检报告 · ${detail.instance?.name || task.instance_id || "-"} · ${(task.db_type || "").toUpperCase()}` : "巡检报告"}
    >
      {detail && (
        <InspectionReportDetailPage
          detail={detail}
          onExportHtml={exportHtml}
          onExportMarkdown={exportMarkdown}
          onExportWord={exportWord}
        />
      )}
    </Modal>
  );
}

export function InspectionOverviewPage({ instances, onNavigate }) {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(false);
  const [overviewDrawer, setOverviewDrawer] = useState({ open: false, title: "", rows: [] });
  const [reportDetail, setReportDetail] = useState(null);
  const [reportOpen, setReportOpen] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const res = await listInspectionTasks({ limit: 100 });
      setItems(res.items || []);
    } catch (e) {
      message.error(e.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  const today = todayText();
  const taskRows = items.map(buildTaskView);
  const todayItems = taskRows.filter((item) => String(item.create_time || "").startsWith(today));
  const latestByInstance = new Map();
  for (const item of taskRows) {
    if (!latestByInstance.has(item.instance_id)) latestByInstance.set(item.instance_id, item);
  }
  const latest = Array.from(latestByInstance.values());
  const instanceList = Array.isArray(instances) ? instances : [];
  const taskInstanceIds = new Set(taskRows.map((item) => item.instance_id).filter(Boolean));
  const instanceTotal = Math.max(instanceList.length, taskInstanceIds.size, latest.length);
  const criticalTotal = todayItems.reduce((sum, item) => sum + (item.critical_count || 0), 0);
  const abnormalInstances = latest.filter((item) => ["critical", "warning"].includes(normalizeRiskLevel(item.risk_level))).length;
  const scoredLatest = latest.filter((item) => (
    item.task_status !== "failed"
    && item.overall_score !== null
    && item.overall_score !== undefined
    && Number.isFinite(Number(item.overall_score))
  ));
  const avgScore = scoredLatest.length
    ? Math.round(scoredLatest.reduce((sum, item) => sum + Number(item.overall_score), 0) / scoredLatest.length)
    : null;
  const inspectedTodayIds = new Set(todayItems.map((item) => item.instance_id));
  const inspectedTodayTotal = inspectedTodayIds.size;
  const uninspectedTotal = Math.max(instanceTotal - inspectedTodayTotal, 0);
  const allRiskRanking = [...latest].sort((a, b) =>
    (RISK_ORDER[normalizeRiskLevel(b.risk_level)] || 0) - (RISK_ORDER[normalizeRiskLevel(a.risk_level)] || 0)
    || (a.overall_score ?? 999) - (b.overall_score ?? 999)
    || (b.critical_count || 0) - (a.critical_count || 0)
  );
  const riskRanking = allRiskRanking.slice(0, 4);
  const allTodayRiskItems = todayItems.filter((item) => (item.critical_count || 0) + (item.warning_count || 0) > 0);
  const recentTasks = taskRows.slice(0, 4);
  const todayRiskTotal = allTodayRiskItems.reduce((sum, item) => sum + (item.critical_count || 0) + (item.warning_count || 0), 0);
  const todayCriticalTotal = allTodayRiskItems.reduce((sum, item) => sum + (item.critical_count || 0), 0);
  const todayWarningTotal = allTodayRiskItems.reduce((sum, item) => sum + (item.warning_count || 0), 0);
  const todayRiskInstanceTotal = new Set(allTodayRiskItems.map((item) => item.instance_id || item.instance_name).filter(Boolean)).size;
  const criticalTarget = allTodayRiskItems.find((item) => (item.critical_count || 0) > 0);
  const warningTarget = allTodayRiskItems.find((item) => (item.warning_count || 0) > 0);
  const heroAdvice = todayRiskTotal
    ? `今日建议：优先处理${criticalTarget ? ` ${dbTypeText(criticalTarget.db_type)} 严重项` : "严重项"}${warningTarget ? `，再排查 ${dbTypeText(warningTarget.db_type)} 警告项` : "，并持续观察警告项"}。`
    : "今日建议：暂无严重或警告风险，保持巡检覆盖并观察提示项即可。";
  const actionSteps = todayRiskTotal ? [
    {
      key: "critical",
      level: criticalTarget?.risk_level || "critical",
      title: criticalTarget ? `先处理 ${dbTypeText(criticalTarget.db_type)} 严重项` : "先处理严重项",
      desc: criticalTarget
        ? `${criticalTarget.instance_name || criticalTarget.instance_id || "-"} · 查看报告并安排处置`
        : "检查严重风险报告，确认影响范围",
    },
    {
      key: "warning",
      level: warningTarget?.risk_level || "warning",
      title: warningTarget ? `再排查 ${dbTypeText(warningTarget.db_type)} 警告项` : "再排查警告项",
      desc: warningTarget
        ? `${warningTarget.instance_name || warningTarget.instance_id || "-"} · 纳入今日排查队列`
        : "关注容量、性能和配置类警告",
    },
    {
      key: "verify",
      level: "normal",
      title: "处置后重新巡检",
      desc: "对比健康分、风险数量和报告结论",
    },
  ] : [
    {
      key: "coverage",
      level: "normal",
      title: "保持今日巡检覆盖",
      desc: "确认关键实例已完成巡检",
    },
    {
      key: "observe",
      level: "info",
      title: "持续观察提示项",
      desc: "提示项无需立即处置，可纳入趋势观察",
    },
    {
      key: "report",
      level: "normal",
      title: "按需查看巡检报告",
      desc: "复核健康分和模块结论",
    },
  ];
  const riskDistribution = [
    { level: "critical", count: todayCriticalTotal },
    { level: "warning", count: todayWarningTotal },
    { level: "info", count: 0 },
    { level: "normal", count: 0 },
  ];
  const recentHealthyTotal = recentTasks.filter((item) => normalizeRiskLevel(item.risk_level) === "normal").length;
  const recentAbnormalTotal = Math.max(recentTasks.length - recentHealthyTotal, 0);
  const kpiItems = [
    {
      title: "实例总数",
      value: instanceTotal,
      trend: "纳管数据库实例",
      icon: <DatabaseOutlined />,
      tone: "info",
    },
    {
      title: "今日已巡检",
      value: inspectedTodayTotal,
      trend: inspectedTodayTotal ? "今日已有体检结果" : "等待今日巡检",
      icon: <CheckCircleOutlined />,
      tone: "normal",
    },
    {
      title: "未巡检实例",
      value: uninspectedTotal,
      trend: uninspectedTotal ? `还需补齐 ${uninspectedTotal} 个` : "今日已覆盖",
      icon: <ClockCircleOutlined />,
      tone: uninspectedTotal ? "warning" : "normal",
    },
    {
      title: "异常实例",
      value: abnormalInstances,
      trend: abnormalInstances ? "优先关注异常库" : "暂无异常实例",
      icon: <WarningOutlined />,
      tone: abnormalInstances ? "warning" : "normal",
    },
    {
      title: "Critical",
      value: criticalTotal,
      trend: criticalTotal ? "需要立即处理" : "无严重风险",
      icon: <AlertOutlined />,
      tone: criticalTotal ? "critical" : "normal",
    },
    {
      title: "平均健康分",
      value: avgScore ?? "-",
      suffix: avgScore === null ? undefined : "/100",
      trend: avgScore === null ? "暂无有效巡检结果" : avgScore >= 80 ? "整体健康稳定" : avgScore >= 60 ? "整体需关注" : "整体风险偏高",
      icon: <SafetyCertificateOutlined />,
      tone: avgScore === null ? "normal" : scoreRiskLevel(avgScore),
    },
  ];
  const closeOverviewDrawer = () => setOverviewDrawer({ open: false, title: "", rows: [] });
  const openOverviewDrawer = (title, rows) => setOverviewDrawer({ open: true, title, rows });
  const viewAllButton = (title, rows, navigateKey, visibleCount = 5) => rows.length > visibleCount || navigateKey ? (
    <Button
      size="small"
      type="link"
      onClick={() => (navigateKey && onNavigate ? onNavigate(navigateKey) : openOverviewDrawer(title, rows))}
    >
      查看全部
    </Button>
  ) : null;
  const openOverviewReport = async (row) => {
    if (!row?.task_id) {
      message.warning("当前记录暂无报告编号");
      return;
    }
    try {
      setReportDetail(await getInspectionReportStructuredDetail(row.task_id, { instance_id: row.instance_id }));
      setReportOpen(true);
    } catch (e) {
      message.error(e.message);
    }
  };
  const renderTaskDrawerRows = (rows) => (
    <Space direction="vertical" size={10} className="full-width">
      {rows.map((item, index) => (
        <div className="inspection-compact-task" key={stableRecordKey("inspection-overview-drawer", item, ["task_id", "instance_id", "create_time"])}>
          <div className="inspection-list-main">
            <LongText value={`${index + 1}. ${item.instance_name || item.instance_id || "-"}`} maxWidth={360} />
            <div><LongText value={`${dbTypeText(item.db_type)} · ${item.create_time || "-"}`} maxWidth={360} /></div>
          </div>
          <Space wrap>
            {scoreTag(item.overall_score)}
            <Tag color={riskColor(item.risk_level)}>{riskText(item.risk_level)}</Tag>
            {(item.critical_count || 0) > 0 && <Tag color="red">严重 {item.critical_count}</Tag>}
            {(item.warning_count || 0) > 0 && <Tag color="orange">警告 {item.warning_count}</Tag>}
          </Space>
        </div>
      ))}
    </Space>
  );

  return (
    <Space direction="vertical" size={22} className="full-width inspection-page-shell inspection-overview-page">
      <Row gutter={[18, 18]}>
        {kpiItems.map((item) => (
          <Col xs={12} md={4} key={item.title}>
            <KpiDashboardCard {...item} />
          </Col>
        ))}
      </Row>

      <Row gutter={[22, 22]}>
        <Col xs={24}>
          <Card
            title="今日待处理风险"
            size="small"
            className="inspection-dashboard-card inspection-primary-card inspection-action-risk-section"
            extra={todayRiskTotal ? (
              <Button size="small" type="link" onClick={() => openOverviewDrawer(`待处理风险明细（${todayRiskTotal}项，按实例汇总）`, allTodayRiskItems)}>
                查看明细 {todayRiskTotal} 项
              </Button>
            ) : null}
          >
            <div className="inspection-hero-wide">
              <div className="inspection-hero-left">
                <div className={`inspection-hero-number tone-${todayCriticalTotal ? "critical" : todayWarningTotal ? "warning" : "normal"}`}>{todayRiskTotal}</div>
                <Text type="secondary">个风险项待处理</Text>
                <Text className="inspection-hero-title">涉及 {todayRiskInstanceTotal} 个实例</Text>
                <Text type="secondary" className="inspection-hero-subtitle">
                  严重 {todayCriticalTotal} 项 · 警告 {todayWarningTotal} 项
                </Text>
                <Text type="secondary" className="inspection-hero-advice">{heroAdvice}</Text>
              </div>

              <div className="inspection-hero-center">
                <div className="inspection-hero-list-title">
                  <Text type="secondary">今日建议处置顺序</Text>
                </div>
                <div className="inspection-action-path">
                  {actionSteps.map((step, index) => (
                    <div className={`inspection-action-step tone-${normalizeRiskLevel(step.level)}`} key={step.key}>
                      <span className="inspection-step-index">{String(index + 1).padStart(2, "0")}</span>
                      <span className="inspection-row-line" />
                      <div className="inspection-list-main">
                        <LongText value={step.title} maxWidth="100%" />
                        <LongText value={step.desc} maxWidth="100%" />
                      </div>
                    </div>
                  ))}
                </div>
              </div>

              <div className="inspection-hero-tools">
                <div className="inspection-hero-tool-section">
                  <Text type="secondary" className="inspection-hero-tool-title">待处理风险项分布</Text>
                  <div className="inspection-distribution-list">
                    {riskDistribution.map((item) => (
                      <div className={`inspection-distribution-card risk-${item.level}`} key={item.level}>
                        <div className="inspection-distribution-label">
                          <span className={`inspection-risk-dot status-${item.level}`} />
                          <Text>{riskText(item.level)}</Text>
                        </div>
                        <div className="inspection-distribution-value">{item.count}</div>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            </div>
          </Card>
        </Col>
      </Row>

      <Row gutter={[22, 22]}>
        <Col xs={24} lg={16}>
          <Card title="风险实例排行" size="small" className="inspection-dashboard-card inspection-ranking-card" extra={<Space size={6}>{viewAllButton("风险实例排行", allRiskRanking, undefined, 4)}<Button size="small" icon={<ReloadOutlined />} loading={loading} onClick={load}>刷新</Button></Space>}>
            <Space direction="vertical" size={8} className="full-width inspection-card-scroll">
              {riskRanking.map((item, index) => (
                <div className={`inspection-rank-row tone-${normalizeRiskLevel(item.risk_level)}`} key={item.task_id || `${item.instance_id}-${index}`}>
                  <span className="inspection-row-line" />
                  <div className="inspection-list-main inspection-rank-main">
                    <div className="inspection-row-title">
                      <span className="inspection-row-rank">{String(index + 1).padStart(2, "0")}</span>
                      <LongText value={item.instance_name || item.instance_id || "-"} maxWidth="100%" />
                    </div>
                    <Text type="secondary">
                      {dbTypeText(item.db_type)} · {shortDateTime(item.create_time)} · 严重 {item.critical_count || 0} / 警告 {item.warning_count || 0}
                    </Text>
                  </div>
                  <div className={`inspection-rank-score tone-${normalizeRiskLevel(item.risk_level)}`}>
                    {item.overall_score ?? "-"}
                  </div>
                  <OverviewStatusPill level={item.risk_level} />
                </div>
              ))}
              {!riskRanking.length && <ExplainEmpty description="暂无巡检记录。完成一次巡检后，这里会按风险等级和健康分排序展示高风险实例。" />}
            </Space>
          </Card>
        </Col>
        <Col xs={24} lg={8}>
          <Card
            title="最近巡检动态"
            size="small"
            className="inspection-dashboard-card inspection-recent-task-card"
            extra={viewAllButton("最近巡检动态", taskRows, "inspectionTasks", 4)}
          >
            <Text type="secondary" className="inspection-recent-summary">
              最近 {recentTasks.length} 次巡检 · {recentHealthyTotal} 次健康 · {recentAbnormalTotal} 次异常
            </Text>
            <Space direction="vertical" size={8} className="full-width inspection-card-scroll">
              {recentTasks.map((item) => (
                <div className={`inspection-event-row tone-${normalizeRiskLevel(item.risk_level)}`} key={item.task_id}>
                  <span className="inspection-row-line" />
                  <div className="inspection-list-main inspection-event-main">
                    <div className="inspection-event-meta">
                      <Text type="secondary">{shortDateTime(item.create_time)}</Text>
                      <Text type="secondary">{dbTypeText(item.db_type)}</Text>
                    </div>
                    <LongText value={`${item.instance_name || "-"} · 巡检完成`} maxWidth="100%" />
                  </div>
                  <div className={`inspection-event-score tone-${normalizeRiskLevel(item.risk_level)}`}>{item.overall_score ?? "-"}</div>
                  <OverviewStatusPill level={item.risk_level} />
                </div>
              ))}
              {!recentTasks.length && <ExplainEmpty description="暂无巡检任务。执行一键巡检后，任务会进入「巡检任务」页面集中管理。" />}
            </Space>
          </Card>
        </Col>
      </Row>
      <Modal
        title={overviewDrawer.title}
        open={overviewDrawer.open}
        onCancel={closeOverviewDrawer}
        footer={null}
        width={820}
        centered
        className="inspection-overview-modal"
      >
        <div className="inspection-overview-modal-body">
          {renderTaskDrawerRows(overviewDrawer.rows)}
        </div>
      </Modal>
      <InspectionReportDrawer detail={reportDetail} open={reportOpen} onClose={() => setReportOpen(false)} />
    </Space>
  );
}

export function InspectionCenterPage({ instances, selectedInstanceId }) {
  return <InspectionRunPage instances={instances} selectedInstanceId={selectedInstanceId} />;
}

export function InspectionRunPage({ dbType, instances, selectedInstanceId }) {
  return <InspectionPage dbType={dbType} instances={instances} selectedInstanceId={selectedInstanceId} />;
}

export function InspectionReportPage() {
  return <ReportPage />;
}

export function InspectionPage({ dbType, instances, selectedInstanceId }) {
  // 统一巡检入口：任意数据库类型，按所选实例自动适配，结果与历史统一展示
  const inspectionInstances = useMemo(
    () => (dbType && dbType !== "host" ? instances.filter((item) => item.db_type === dbType) : instances),
    [dbType, instances]
  );
  const [instanceId] = useSyncedInstanceId(selectedInstanceId, inspectionInstances);
  const [templateKey, setTemplateKey] = useState(AUTO_TEMPLATE_KEY);
  const [templateRows, setTemplateRows] = useState([]);
  const [templateLoading, setTemplateLoading] = useState(false);
  const [templateLoadError, setTemplateLoadError] = useState("");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState(null);
  const [history, setHistory] = useState([]);
  const [reportDetail, setReportDetail] = useState(null);
  const [reportOpen, setReportOpen] = useState(false);
  const selectedInstance = inspectionInstances.find((item) => item.id === instanceId);
  const inspectionDbType = dbType === "host" ? "host" : selectedInstance?.db_type || dbType;
  const applicableTemplates = useMemo(
    () => templateRows.filter(
      (item) => item.enabled
        && item.runtime_valid !== false
        && (item.db_types || []).includes(inspectionDbType)
    ),
    [inspectionDbType, templateRows]
  );
  const selectedTemplate = applicableTemplates.find((item) => item.policy_key === templateKey);
  const templateOptions = useMemo(() => {
    const applicable = templateRows.filter(
      (item) => item.enabled
        && item.runtime_valid !== false
        && (item.db_types || []).includes(inspectionDbType)
    );
    const invalidApplicable = templateRows.filter(
      (item) => item.enabled
        && item.runtime_valid === false
        && (item.db_types || []).includes(inspectionDbType)
    );
    const disabledApplicable = templateRows.filter(
      (item) => !item.enabled && (item.db_types || []).includes(inspectionDbType)
    );
    const incompatible = templateRows.filter(
      (item) => !(item.db_types || []).includes(inspectionDbType)
    );
    const option = (item, disabled = false) => ({
      value: item.policy_key,
      label: disabled
        ? `${item.name}（仅适用 ${(item.db_types || []).map(dbTypeText).join(" / ") || "未配置"}）`
        : `${item.name}${isAssistantDefaultTemplate(item, inspectionDbType) ? "（助手默认）" : ""}`,
      disabled,
    });
    const groups = [{ value: AUTO_TEMPLATE_KEY, label: "自动匹配模板" }];
    if (templateKey !== AUTO_TEMPLATE_KEY && !templateRows.some((item) => item.policy_key === templateKey)) {
      groups.push({ value: templateKey, label: "所选模板已不可用，请重新选择", disabled: true });
    }
    if (applicable.length) {
      groups.push({
        label: `适用于当前 ${dbTypeText(inspectionDbType)}（${applicable.length}）`,
        options: applicable.map((item) => option(item)),
      });
    }
    if (disabledApplicable.length) {
      groups.push({
        label: `适用但已停用（${disabledApplicable.length}）`,
        options: disabledApplicable.map((item) => ({
          ...option(item, true),
          label: `${item.name}（已停用）`,
        })),
      });
    }
    if (invalidApplicable.length) {
      groups.push({
        label: `运行校验未通过（${invalidApplicable.length}）`,
        options: invalidApplicable.map((item) => ({
          ...option(item, true),
          label: `${item.name}（无可执行检查项）`,
        })),
      });
    }
    if (incompatible.length) {
      groups.push({
        label: `其他数据库模板（${incompatible.length}）`,
        options: incompatible.map((item) => option(item, true)),
      });
    }
    return groups;
  }, [inspectionDbType, templateRows, templateKey]);

  useEffect(() => {
    let cancelled = false;
    let requestVersion = 0;
    const loadTemplates = () => {
      const version = ++requestVersion;
      if (!inspectionDbType) {
        setTemplateRows([]);
        setTemplateLoading(false);
        return;
      }
      setTemplateLoading(true);
      setTemplateLoadError("");
      listInspectionPolicies()
        .then((response) => {
          if (cancelled || version !== requestVersion) return;
          const rows = visibleTemplateCatalog(response.items || []);
          setTemplateRows(rows);
        })
        .catch((error) => {
          if (cancelled || version !== requestVersion) return;
          setTemplateRows([]);
          setTemplateLoadError(error.message || "巡检模板加载失败");
        })
        .finally(() => {
          if (!cancelled && version === requestVersion) setTemplateLoading(false);
        });
    };
    const handleStorageChange = (event) => {
      if (event.key === INSPECTION_POLICIES_CHANGED_STORAGE_KEY) loadTemplates();
    };
    loadTemplates();
    window.addEventListener(INSPECTION_POLICIES_CHANGED_EVENT, loadTemplates);
    window.addEventListener("storage", handleStorageChange);
    return () => {
      cancelled = true;
      window.removeEventListener(INSPECTION_POLICIES_CHANGED_EVENT, loadTemplates);
      window.removeEventListener("storage", handleStorageChange);
    };
  }, [inspectionDbType]);

  const loadHistory = async (iid) => {
    if (!iid) { setHistory([]); return; }
    try { const r = await listInspectionTasks({ instance_id: iid }); setHistory((r.items || []).map(buildTaskView)); }
    catch { setHistory([]); }
  };
  const loadLatest = async (iid) => {
    if (!iid) { setResult(null); return; }
    try {
      const r = await listInspectionTasks({ instance_id: iid, limit: 1 });
      const top = (r.items || [])[0];
      if (top) { const d = await getInspectionTask(top.task_id, { instance_id: iid }); setResult(d); }
      else setResult(null);
    } catch { setResult(null); }
  };
  useEffect(() => { setResult(null); loadHistory(instanceId); loadLatest(instanceId); }, [instanceId]);

  const runInspect = async () => {
    if (!instanceId) { message.warning("请选择实例"); return; }
    if (!templateKey) { message.warning("请选择适用于当前数据库的巡检模板"); return; }
    if (templateKey !== AUTO_TEMPLATE_KEY && !selectedTemplate) {
      message.warning("所选模板当前不可用，请重新选择巡检模板");
      return;
    }
    if (selectedTemplate?.runtime_valid === false) {
      message.error(`巡检模板运行校验未通过：${(selectedTemplate.runtime_errors || []).join("；")}`);
      return;
    }
    setLoading(true);
    try {
      const engine = dbType === "host" ? "host" : selectedInstance?.db_type;
      const data = await runInspectionTask(engine, instanceId, templateKey === AUTO_TEMPLATE_KEY ? null : templateKey);
      setResult(data);
      const notice = inspectionNotice(data, data?.task?.template_name || data?.task?.template_key || selectedTemplate?.name);
      message[notice.type](notice.message);
      await loadHistory(instanceId);
      return inspectionOutcome(data).complete;
    } catch (error) { message.error(error.message); }
    finally { setLoading(false); }
  };
  const runInspectWithAi = async () => {
    if (await runInspect()) message.info("巡检完成后可在结果区查看「AI 诊断」和「处置方案」。");
  };

  const openReport = async (task_id = result?.task?.task_id) => {
    if (!task_id) {
      message.warning("请先完成一次巡检或选择历史任务");
      return;
    }
    try {
      setReportDetail(await getInspectionReportStructuredDetail(task_id, { instance_id: instanceId }));
      setReportOpen(true);
    } catch (e) { message.error(e.message); }
  };
  const task = result?.task || history[0] || {};
  const report = result?.report || {};
  const findings = result?.findings || [];
  const severeWarnings = findings
    .filter((item) => ["critical", "warning"].includes(normalizeRiskLevel(item.severity || item.risk_level)))
    .sort((a, b) => (RISK_ORDER[normalizeRiskLevel(b.severity || b.risk_level)] || 0) - (RISK_ORDER[normalizeRiskLevel(a.severity || a.risk_level)] || 0))
    .slice(0, 5);
  const hintItems = findings
    .filter((item) => normalizeRiskLevel(item.severity || item.risk_level) === "info")
    .slice(0, 5);
  const outcome = inspectionOutcome(result || task);
  const lastTaskView = buildTaskView({ ...task, task_status: outcome.status, overall_score: outcome.score, failure_reason: outcome.reason });
  const hasOnlyHints = outcome.complete && severeWarnings.length === 0 && (task.info_count || hintItems.length) > 0;

  return (
    <Space direction="vertical" size={16} className="full-width inspection-page-shell">
      <Card title="巡检对象">
        <Row gutter={[16, 16]} align="middle">
          <Col xs={24} lg={10}>
            <CurrentInstanceSummary instance={selectedInstance} />
          </Col>
          <Col xs={24} lg={14}>
            {selectedInstance ? (
              <Descriptions size="small" column={2}>
                <Descriptions.Item label="实例名">{selectedInstance.name || selectedInstance.instance_name || "-"}</Descriptions.Item>
                <Descriptions.Item label="主机">{selectedInstance.host || "-"}</Descriptions.Item>
                <Descriptions.Item label="端口">{selectedInstance.port || "-"}</Descriptions.Item>
                <Descriptions.Item label="数据库">{dbTypeText(selectedInstance.db_type)}</Descriptions.Item>
                <Descriptions.Item label="巡检模板">
                  <Space direction="vertical" size={2}>
                    <Select
                      size="small"
                      value={templateKey || undefined}
                      onChange={setTemplateKey}
                      loading={templateLoading}
                      placeholder="选择巡检模板"
                      style={{ minWidth: 260 }}
                      options={templateOptions}
                      showSearch
                      optionFilterProp="label"
                    />
                    <Text type={templateLoadError ? "danger" : "secondary"}>
                      {templateLoadError || `当前可用 ${applicableTemplates.length} 个，模板库共 ${templateRows.length} 个；不适配当前数据库的模板仅展示不可选。`}
                    </Text>
                  </Space>
                </Descriptions.Item>
                <Descriptions.Item label="最近巡检">{history[0]?.create_time || "-"}</Descriptions.Item>
                <Descriptions.Item label="上次健康分">{history[0]?.overall_score ?? "-"}</Descriptions.Item>
                <Descriptions.Item label="账号能力">
                  {task?.account_capability_label || (task?.read_only ? "只读降级采集" : "账号能力未评估")}
                </Descriptions.Item>
              </Descriptions>
            ) : (
              <Alert type="info" showIcon message="请选择实例" description="在右上角统一实例选择器中选择数据库实例后执行一键巡检。" />
            )}
          </Col>
        </Row>
        <Space wrap className="inspection-run-actions">
          <Button type="primary" icon={<ReloadOutlined />} loading={loading} disabled={!instanceId || !templateKey} onClick={runInspect}>开始巡检</Button>
          <Button icon={<RobotOutlined />} loading={loading} disabled={!instanceId || !templateKey} onClick={runInspectWithAi}>一键巡检 + AI分析</Button>
          <Button icon={<FileTextOutlined />} disabled={!result?.task?.task_id} onClick={() => openReport()}>查看完整报告</Button>
          <Button icon={<SearchOutlined />} onClick={() => loadHistory(instanceId)} disabled={!instanceId}>刷新最近状态</Button>
          <Text type="secondary">支持 Oracle / MySQL / PostgreSQL / SQL Server，所有结果统一进入巡检报告与报告中心。</Text>
        </Space>
      </Card>

      {result ? (
        <>
          <Row gutter={[16, 16]}>
            <Col xs={12} md={4}>
              <Card>
                <Statistic
                  title="健康分"
                  value={lastTaskView.overall_score ?? "未生成"}
                  suffix={lastTaskView.overall_score == null ? undefined : "/ 100"}
                />
              </Card>
            </Col>
            <Col xs={12} md={4}><Card><Statistic title="执行状态" value={outcome.label} valueStyle={{ color: lastTaskView.task_status === "failed" ? "#cf1322" : "#1677ff" }} /></Card></Col>
            <Col xs={12} md={4}><Card><Statistic title="严重" value={lastTaskView.critical_count || 0} valueStyle={{ color: "#cf1322" }} /></Card></Col>
            <Col xs={12} md={4}><Card><Statistic title="警告" value={lastTaskView.warning_count || 0} valueStyle={{ color: "#d48806" }} /></Card></Col>
            <Col xs={12} md={4}><Card><Statistic title="提示" value={lastTaskView.info_count || hintItems.length || 0} valueStyle={{ color: "#1677ff" }} /></Card></Col>
            <Col xs={12} md={4}><Card><Statistic title="检查项" value={lastTaskView.total_count || findings.length || 0} /></Card></Col>
          </Row>
          <Card title="本次结果摘要" size="small">
            <Space direction="vertical" size={12} className="full-width">
              <Alert
                type={lastTaskView.task_status === "failed" || normalizeRiskLevel(lastTaskView.risk_level) === "critical" ? "error" : normalizeRiskLevel(lastTaskView.risk_level) === "warning" ? "warning" : "info"}
                showIcon
                message={outcome.complete ? report.overall_conclusion || task.summary || "巡检已完成，建议查看关键风险与完整报告。" : lastTaskView.failure_reason || `巡检${outcome.label}，不能据此判断数据库健康。`}
              />
              <Descriptions bordered size="small" column={3}>
                <Descriptions.Item label="任务号">{task.task_id || "-"}</Descriptions.Item>
                <Descriptions.Item label="巡检时间">{report.inspection_time || task.finished_at || task.create_time || "-"}</Descriptions.Item>
                <Descriptions.Item label="耗时">{lastTaskView.duration_text}</Descriptions.Item>
              </Descriptions>
              {hasOnlyHints && (
                <Alert
                  type="info"
                  showIcon
                  message="暂无严重或警告风险。本次存在提示项，建议纳入观察。"
                  description={`提示项数量：${task.info_count || hintItems.length || 0}`}
                />
              )}
            </Space>
          </Card>
          <Row gutter={[16, 16]}>
            <Col xs={24} lg={12}>
              <Card title="关键风险 Top 5" size="small">
                <Space direction="vertical" size={10} className="full-width">
                  {severeWarnings.map((item, index) => <RiskInsightCard key={stableRecordKey("one-click-risk", item, ["id", "check_item", "finding"])} item={item} index={index + 1} />)}
                  {!severeWarnings.length && <ExplainEmpty description={!outcome.complete ? "尚无完整采集结果，不能判断是否存在风险。" : hasOnlyHints ? "暂无严重或警告风险。提示项已单独展示，建议持续观察。" : "暂无严重或警告风险。当前巡检未发现需要立即处置的问题。"} />}
                </Space>
              </Card>
            </Col>
            <Col xs={24} lg={12}>
              <Card title="提示项 Top 5" size="small">
                <Space direction="vertical" size={10} className="full-width">
                  {hintItems.map((item, index) => <RiskInsightCard key={stableRecordKey("one-click-hint", item, ["id", "check_item", "finding"])} item={item} index={index + 1} />)}
                  {!hintItems.length && <ExplainEmpty description={outcome.complete ? "暂无提示项。本次巡检没有发现需要纳入观察的轻量信息。" : "尚无完整采集结果，提示项未确认。"} />}
                </Space>
              </Card>
            </Col>
          </Row>
          <Card size="small">
            <Space wrap>
              <Button type="primary" icon={<FileTextOutlined />} onClick={() => openReport()}>查看完整报告</Button>
              <Text type="secondary">完整明细、模块得分、证据链和处置建议已移入报告详情页。</Text>
            </Space>
          </Card>
        </>
      ) : (
        <Card>
          <ExplainEmpty description="请选择实例并点击「开始巡检」。一键巡检页只展示本次摘要，完整明细会进入巡检报告。" />
        </Card>
      )}

      <InspectionReportDrawer detail={reportDetail} open={reportOpen} onClose={() => setReportOpen(false)} />
    </Space>
  );
}

export function InspectionTaskPage() {
  const [items, setItems] = useState([]);
  const [dbType, setDbType] = useState();
  const [loading, setLoading] = useState(false);
  const [detail, setDetail] = useState(null);
  const [detailOpen, setDetailOpen] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const res = await listInspectionTasks({ db_type: dbType, limit: 200 });
      setItems(res.items || []);
    } catch (e) {
      message.error(e.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, [dbType]);

  const openReport = async (row) => {
    try {
      setDetail(await getInspectionReportStructuredDetail(row.task_id, { instance_id: row.instance_id }));
      setDetailOpen(true);
    } catch (e) { message.error(e.message); }
  };
  const taskRows = items.map(buildTaskView);
  const showError = (row) => {
    Modal.info({
      title: `任务错误 - ${row.task_id}`,
      content: row.failure_reason && row.failure_reason !== "-" ? row.failure_reason : "该任务没有失败原因记录。",
    });
  };
  const taskMoreMenu = (row) => ({
    items: [
      { key: "report", label: "生成报告" },
      { key: "rerun", label: "重新执行" },
      { key: "error", label: "查看错误", disabled: row.failure_reason === "-" },
    ],
    onClick: ({ key }) => {
      if (key === "report") openReport(row);
      if (key === "rerun") showPlanOnly("重新执行");
      if (key === "error") showError(row);
    },
  });

  return (
    <Space direction="vertical" size={16} className="full-width inspection-page-shell">
      <Card>
        <Space wrap>
          <Select
            allowClear
            placeholder="数据库类型"
            style={{ width: 180 }}
            value={dbType}
            onChange={setDbType}
            options={DB_TYPE_OPTIONS}
          />
          <Button icon={<ReloadOutlined />} loading={loading} onClick={load}>刷新</Button>
          <Text type="secondary">巡检任务只展示执行记录。正式报告请进入「巡检报告」。</Text>
        </Space>
      </Card>
      <Card size="small" title="巡检任务">
        <Table
          size="small"
          rowKey="task_id"
          loading={loading}
          dataSource={taskRows}
          pagination={{ defaultPageSize: 10, showSizeChanger: true, pageSizeOptions: [10, 20, 50, 100] }}
          scroll={{ x: 1280 }}
          locale={{ emptyText: "暂无巡检任务。完成一键巡检、定时巡检或批量巡检后，这里会显示执行记录。" }}
          columns={[
            { title: "任务号", dataIndex: "task_id", width: 190, ellipsis: true },
            { title: "实例", dataIndex: "instance_name", width: 170, ellipsis: true },
            { title: "数据库类型", dataIndex: "db_type", width: 110, render: (v) => <Tag>{dbTypeText(v)}</Tag> },
            {
              title: "实际巡检模板",
              dataIndex: "template_name",
              width: 220,
              ellipsis: true,
              render: (value, row) => (
                <LongText
                  value={`${value || row.template_key || "未记录"}${row.template_revision ? ` · ${row.template_revision}` : ""}`}
                  maxWidth={200}
                />
              ),
            },
            { title: "触发方式", dataIndex: "trigger_type", width: 100, render: (v) => <TriggerTag value={v} /> },
            { title: "任务状态", dataIndex: "task_status", width: 105, render: (v) => <TaskStatusTag status={v} /> },
            { title: "开始时间", dataIndex: "started_at", width: 170 },
            { title: "结束时间", dataIndex: "finished_at", width: 170 },
            { title: "耗时", dataIndex: "duration_text", width: 90 },
            { title: "检查项数", dataIndex: "total_count", width: 95 },
            { title: "失败原因", dataIndex: "failure_reason", ellipsis: true, render: (v) => v === "-" ? <Text type="secondary">无失败记录</Text> : <LongText value={v} maxWidth={220} /> },
            { title: "操作", width: 150, fixed: "right", render: (_, row) => (
              <Space size={4}>
                <Button size="small" type="link" onClick={() => openReport(row)}>查看结果</Button>
                <Dropdown menu={taskMoreMenu(row)} trigger={["click"]}>
                  <Button size="small" type="link" icon={<MoreOutlined />}>更多</Button>
                </Dropdown>
              </Space>
            ) },
          ]}
        />
      </Card>
      <InspectionReportDrawer detail={detail} open={detailOpen} onClose={() => setDetailOpen(false)} />
    </Space>
  );
}



export function SlowSqlPage({ instances, engine = "oracle", selectedInstanceId, executionResult }) {
  if (engine === "mysql") {
    return <MySQLSlowSqlPerformancePage instances={instances} selectedInstanceId={selectedInstanceId} executionResult={executionResult} />;
  }
  if (engine !== "oracle") {
    return <Alert type="warning" showIcon message="该数据库尚未接入慢 SQL 分析" />;
  }
  return <OracleSlowSqlPage instances={instances} selectedInstanceId={selectedInstanceId} executionResult={executionResult} />;
}

export function MySQLSlowSqlPerformancePage({ instances, selectedInstanceId, executionResult }) {
  const mysqlInstances = useMemo(() => instances.filter((item) => item.db_type === "mysql"), [instances]);
  const [instanceId] = useSyncedInstanceId(selectedInstanceId, mysqlInstances);
  const selectedInstance = mysqlInstances.find((item) => item.id === instanceId);
  const [data, setData] = useState({});
  const [loading, setLoading] = useState(false);

  const load = async () => {
    if (!instanceId) return;
    setLoading(true);
    try {
      const result = await mysqlOpsApi.slowSqlTop(instanceId, { time_range: "24h", limit: 200 });
      setData(result);
    } catch (e) {
      message.error(e.message || "慢 SQL 数据加载失败");
      setData({ error: e.message || "慢 SQL 数据加载失败" });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (executionResult?.engine === "mysql" && String(executionResult.instanceId) === String(instanceId) && executionResult.data) {
      setData(executionResult.data);
      return;
    }
    load();
  }, [instanceId, executionResult]);

  return (
    <Space direction="vertical" size={16} className="full-width">
      <Card>
        <Space wrap>
          <CurrentInstanceSummary instance={selectedInstance} emptyText="请在右上角选择 MySQL 实例" />
          <Button type="primary" icon={<SearchOutlined />} loading={loading} disabled={!instanceId} onClick={load}>查询</Button>
          <Text type="secondary">MySQL 慢 SQL 已合并到性能诊断</Text>
        </Space>
      </Card>
      <Spin spinning={loading}>
        {instanceId ? <MySQLSlowSqlTab data={data} instanceId={instanceId} /> : (
          <OpsEmptyState title="请选择 MySQL 实例" description="在右上角统一实例选择器中选择 MySQL 实例后再查看慢 SQL。" />
        )}
      </Spin>
    </Space>
  );
}
