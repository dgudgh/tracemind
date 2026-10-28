import React from "react";
import { Tag } from "antd";

const CAT_LABEL = {
  overview: "首页",
  assets: "资产",
  inspection: "巡检",
  operations: "运维",
  backup: "备份",
  performance: "性能",
  monitoring: "监控",
  ai: "AI",
  reports: "报告",
  settings: "设置",
};
export const catTag = (c) => <Tag color={c === "backup" ? "cyan" : c === "performance" ? "purple" : c === "monitoring" ? "orange" : "green"} style={{ fontSize: 10, lineHeight: "16px", padding: "0 4px" }}>{CAT_LABEL[c] || c || "通用"}</Tag>;
const moduleDescMap = {
  dashboard: "数据库实例运行状态总览，关键指标一图掌握",
  instances: "管理数据库连接实例，新增、编辑、删除实例配置",
  inspectionOverview: "巡检总览：健康分、异常实例、风险排行和最近任务",
  inspectionCenter: "一键执行数据库健康巡检，生成巡检报告",
  inspectionTasks: "查看一键巡检和定时巡检任务执行状态",
  inspectionReports: "阅读、导出和查看巡检报告",
  inspectionCompare: "对比最近两次巡检的评分、风险和新增异常",
  inspectionPolicies: "管理巡检阈值、评分规则、告警规则和报告配置",
  inspectionTemplates: "管理 Oracle、MySQL、PostgreSQL、SQL Server 巡检模板",
  inspectionHistoryReports: "集中查看历史巡检报告",
  slowSqlAnalysis: "捕获和分析慢查询 SQL，定位性能瓶颈",
  capacity: "监控表空间和磁盘组容量，预测增长趋势",
  schedule: "配置定时巡检策略，自动执行容量采集",
  alertCenter: "集中查看和管理所有容量告警信息",
  userManagement: "管理系统用户账号、密码策略和模块权限",
  systemParams: "管理系统级参数和平台配置",
  backupOverview: "查看备份覆盖率、成功率、容量和恢复点",
  backupOracle: "生成 Oracle 逻辑/物理备份方案",
  backupMysql: "生成 MySQL 逻辑/物理备份方案",
  backupPostgresql: "生成 PostgreSQL 备份和 PITR 方案",
  backupSqlserver: "生成 SQL Server Full/Diff/Log 备份方案",
  backupStorage: "管理本地、NFS、对象存储等备份介质",
  backupRecoveryCenter: "生成多数据库恢复方案，所有恢复保持 plan_only",
  backupValidation: "执行备份文件校验、恢复校验和抽样恢复计划",
  backupDrill: "管理恢复演练和演练报告",
  backupAlerts: "管理备份失败、超时和存储不可达告警",
  backupReports: "查看备份日报、周报和月报",
  ai: "AI 智能诊断数据库问题，提供优化建议",
  ruleDiagnosis: "基于结构化巡检数据生成规则诊断、证据链和处置动作",
  diagnosisCenter: "端到端智能诊断中心：事件、证据、根因、拓扑和复盘",
  aiConfig: "管理 AI 服务商、模型和密钥配置",
  dataGovernance: "历史归档、跨库迁移、CSV/EXPDP 文件归档、校验与清理",
  notifyManagement: "告警通知管理：通道配置、策略管理、模板管理、发送记录",
  itsmManagement: "ITSM 工单管理：工单系统配置、告警/巡检转工单、工单记录、状态同步",
  unifiedReports: "统一查看巡检、AWR、告警、工单、治理和 AI 报告快照",
  awrReports: "查看 AWR 分析报告历史",
  alertReports: "查看告警报告",
};

export const getModuleDesc = (key) => moduleDescMap[key] || "";

export const statusTag = (status, lockedUntil) => {
  if (lockedUntil && new Date(lockedUntil) > new Date()) return <Tag color="red">已锁定</Tag>;
  if (status === "locked") return <Tag color="red">已锁定</Tag>;
  if (status === "disabled") return <Tag color="default">已停用</Tag>;
  return <Tag color="green">正常</Tag>;
};

export const roleTag = (role) => <Tag color={role === "admin" ? "purple" : "blue"}>{role === "admin" ? "管理员" : "普通用户"}</Tag>;
