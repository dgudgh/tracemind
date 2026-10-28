import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  Checkbox,
  Empty,
  Input,
  Modal,
  Progress,
  Select,
  Space,
  Tag,
  Tooltip,
  Typography,
  message,
} from "antd";
import {
  CheckCircleOutlined,
  ClockCircleOutlined,
  CloseOutlined,
  DatabaseOutlined,
  ExclamationCircleOutlined,
  FileSearchOutlined,
  FileTextOutlined,
  LineChartOutlined,
  MinusOutlined,
  PushpinOutlined,
  ReloadOutlined,
  RobotOutlined,
  SafetyOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import { useSyncedInstanceId } from "./DiagnosisPageHelpers";
import { ItsmTicketDrawer } from "./framework/integration";
import { AssistantPlanWorkspace } from "./AssistantPlanWorkspace";
import { RequirementClarificationModal } from "./RequirementClarificationModal";
import { getAuthSession, isAuthSessionCurrent } from "./modules/authStorage";
import { clearAssistantDispatchJournal, currentAssistantDispatchJournal, readAssistantDispatchJournal,
  writeAssistantDispatchJournal } from "./modules/assistantDispatchJournal";
import { normalizeMenuKey } from "./menuConfig";
import { resolveSmartNavigation } from "./modules/smartNavigation";
import { resolveCustomerRequirement } from "./modules/assistantRequirement";
import {
  DEFAULT_QUICK_ACTION_CARDS,
  QUICK_ACTION_STORAGE_KEY,
  QUICK_ACCESS_UPDATED_EVENT,
  QUICK_ICON_MAP,
  QUICK_ICON_OPTIONS,
  QUICK_TONE_OPTIONS,
  createQuickActionCard,
  loadQuickActionCards,
  normalizeQuickCards,
  quickActionCardsForContext,
  saveQuickActionCards,
} from "./modules/quickAccess";
import {
  getDiagnosisCenterIncident,
  loadDiagnosisCenterContext,
  loadDiagnosisCenterIncidents,
  loadDiagnosisCenterInstanceContext,
  notifyDiagnosisCenterIncident,
} from "./modules/diagnosisCenterApi";
import { dispatchAssistantIntent, getDispatchReceipt } from "./modules/dispatchApi";
import { downloadReportArtifact } from "./modules/reportApi";
import { isInstanceProvisioningCommand } from "./modules/assistantActions";

const { Text } = Typography;

const CUSTOMER_PLACEHOLDER = "你有什么问题？\n\n例如：订单提交从 10:05 开始超时，客户反馈支付后无响应，应用日志出现 submitOrder timeout。";
const ENGINEER_PLACEHOLDER = "粘贴工程师上下文：\n\n时间窗：2026-06-26 10:00-10:30\n现象：订单提交 timeout / Nginx 499\n日志：submitOrder timeout ...\n数据库：SQL_ID / AWR Top Event / 锁等待 / 会话信息\n变更：发布单、参数调整、批处理、容量变化";

const INCIDENT_PAGE_SIZE = 50;
const CUSTOMER_HINTS = ["描述现象", "说明时间", "影响范围"];
const ENGINEER_HINTS = ["日志片段", "SQL_ID / AWR", "等待事件", "变更单"];

// 说明：这里原本有一套 ENTRY_INTENT_RULES + inferDiagnosisIntent 的"入口意图预判"，
// 其 confidence 由关键词命中数拼出来（42 / 68+8n），既不展示给用户也不提交后端；
// 为避免继续维护一段无人消费的假数据，整体移除。真实分诊走 route-intent 接口。

const CARD_DEFINITIONS = [
  { key: "summary", dock: "摘要", title: "案例摘要", icon: FileSearchOutlined, tone: "green" },
  { key: "timeline", dock: "时间线", title: "时间线", icon: ClockCircleOutlined, tone: "blue" },
  { key: "rootCause", dock: "根因", title: "根因候选", icon: ThunderboltOutlined, tone: "red" },
  { key: "evidence", dock: "证据", title: "证据链", icon: DatabaseOutlined, tone: "cyan" },
  { key: "change", dock: "变更", title: "变更影响", icon: LineChartOutlined, tone: "amber" },
  { key: "lab", dock: "实验", title: "根因实验室", icon: RobotOutlined, tone: "violet" },
  { key: "retest", dock: "复测", title: "复测计划", icon: CheckCircleOutlined, tone: "green" },
  { key: "report", dock: "报告", title: "售后报告", icon: FileTextOutlined, tone: "blue" },
  { key: "knowledge", dock: "知识", title: "知识库建议", icon: SafetyOutlined, tone: "gray" },
];

const DOCK_FLOW = [
  "summary",
  "timeline",
  "rootCause",
  "evidence",
  "change",
  "retest",
  "report",
  "knowledge",
];

function compactText(value, fallback = "-") {
  if (value === null || value === undefined) return fallback;
  const text = String(value).trim();
  return text || fallback;
}

function shortTime(value) {
  if (!value) return "待确认";
  const text = String(value);
  if (text.length <= 16) return text;
  return text.slice(5, 16).replace("T", " ");
}

function severityColor(value) {
  return {
    critical: "red",
    high: "volcano",
    warning: "orange",
    warn: "orange",
    info: "blue",
    normal: "green",
    success: "green",
    error: "red",
  }[String(value || "").toLowerCase()] || "default";
}

function severityLabel(value) {
  return {
    critical: "CRITICAL",
    high: "HIGH",
    warning: "WARNING",
    warn: "WARNING",
    info: "INFO",
    normal: "NORMAL",
    success: "NORMAL",
    error: "ERROR",
  }[String(value || "").toLowerCase()] || compactText(value, "UNKNOWN").toUpperCase();
}

function statusLabel(status) {
  return {
    open: "分析中",
    running: "分析中",
    completed: "已完成",
    done: "已完成",
    failed: "失败",
    error: "异常",
  }[String(status || "").toLowerCase()] || compactText(status, "待确认");
}

function diagnosisExecutionPresentation(detail) {
  const aiResult = detail?.ai_result && typeof detail.ai_result === "object" && !Array.isArray(detail.ai_result)
    ? detail.ai_result : {};
  const status = String(aiResult.execution_status || aiResult.status || detail?.status || "").toLowerCase();
  if (["success", "succeeded", "completed", "done", "root_caused"].includes(status)) {
    return { label: "诊断完成", color: "success" };
  }
  if (status === "partial") return { label: "部分完成", color: "warning" };
  if (["failed", "error", "connection_failed"].includes(status)) return { label: "失败", color: "error" };
  return { label: "证据待补充", color: "warning" };
}

function countEvidenceForLayer(evidences, layer, source) {
  const normalizedLayer = String(layer || "").toLowerCase();
  const normalizedSource = String(source || "").toLowerCase();
  return evidences.filter((item) => {
    const itemLayer = String(item.layer || "").toLowerCase();
    const itemSource = String(item.source || "").toLowerCase();
    return (
      (normalizedLayer && itemLayer.includes(normalizedLayer)) ||
      (normalizedSource && itemSource.includes(normalizedSource))
    );
  }).length;
}

function stableContentKey(prefix, item, fields) {
  if (!item || typeof item !== "object") return `${prefix}-${compactText(item, "empty")}`;
  const parts = fields.map((field) => compactText(item[field], "")).filter(Boolean);
  if (parts.length) return `${prefix}-${parts.join("|")}`;
  return `${prefix}-${JSON.stringify(item).slice(0, 120)}`;
}

function buildFaultChain({ detail, selectedInstance, evidenceRows, timelineRows }) {
  const firstTime = timelineRows[0]?.event_time || detail?.create_time;
  const nodes = (detail?.topology?.nodes || []).slice(0, 4).map((node, index) => {
    const status = String(node.status || "").toLowerCase();
    const isCritical = ["critical", "error", "failed"].includes(status);
    const isSuspected = node.suspected === true || node.is_root_cause === true;
    return {
      key: node.node_key || `${node.node_type || "node"}-${index}`,
      layer: compactText(node.node_type || node.layer, `层级 ${index + 1}`),
      asset: compactText(node.node_name || node.asset || selectedInstance?.instance_name, "未知资产"),
      exception: node.alert_count > 0 ? `${node.alert_count} 条告警` : compactText(node.status, "异常待确认"),
      time: shortTime(node.first_seen || firstTime),
      status: isCritical ? "CRITICAL" : statusLabel(node.status),
      evidenceCount: node.alert_count ?? countEvidenceForLayer(evidenceRows, node.node_type, node.node_name),
      critical: isCritical,
      suspected: isSuspected,
    };
  });
  return nodes;
}

function buildRecommendations({ evidenceRows, rootCauseRows, timelineRows, dataSources, activeCards }) {
  const topCause = rootCauseRows[0]?.cause || rootCauseRows[0]?.rule_code || "根因候选";
  const recommended = [
    {
      key: "evidence",
      level: "必须",
      title: "证据链",
      reason: `${topCause} 已进入根因候选，需要确认支撑证据、反证和排除项。`,
      outcome: "把规则命中、日志、指标和数据库现象串成可审计证据。",
      action: "打开证据",
    },
    {
      key: "change",
      level: evidenceRows.length ? "建议" : "必须",
      title: "变更影响",
      reason: "故障时间窗需要和发布、参数、批处理或容量变更对齐。",
      outcome: "确认是否由近期变更触发，避免只处理表面告警。",
      action: "查看变更",
    },
    {
      key: "lab",
      level: "建议",
      title: "根因实验室",
      reason: "根因候选需要进一步比较置信度、影响面和反证。",
      outcome: "沉淀候选根因排序，并给出下一步验证动作。",
      action: "打开实验室",
    },
    {
      key: "retest",
      level: timelineRows.length ? "建议" : "可选",
      title: "复测计划",
      reason: "处置前后需要用同一组指标复测，确保告警真正回落。",
      outcome: "生成 plan_only 复测清单，避免直接执行生产变更。",
      action: "生成复测",
    },
    {
      key: "report",
      level: "可选",
      title: "售后报告",
      reason: "当前案例已经具备摘要、时间线和根因候选，可生成客户报告草稿。",
      outcome: "输出面向客户的现象、证据、根因、动作和复盘结论。",
      action: "打开报告",
    },
    {
      key: "knowledge",
      level: dataSources.length ? "可选" : "建议",
      title: "知识库建议",
      reason: "相似故障的方法论可以补充排查路径和注意事项。",
      outcome: "复用历史经验，补齐遗漏的数据源和检查项。",
      action: "查看知识",
    },
  ];

  return recommended.map((item) => ({
    ...item,
    opened: activeCards.includes(item.key),
  }));
}

function QuickActionDock({ cards, activeKey, onSelect, onCustomize }) {
  return (
    <nav className="diagnosis-quick-dock" aria-label="快速能力卡片">
      <div className="diagnosis-quick-dock-head">
        <span>快速卡片</span>
        <button type="button" onClick={onCustomize}>自定义</button>
      </div>
      <div className="diagnosis-quick-dock-track">
        {cards.map((item) => {
          const Icon = QUICK_ICON_MAP[item.iconKey] || RobotOutlined;
          const active = item.key === activeKey;
          return (
            <button
              key={item.key}
              type="button"
              className={`diagnosis-quick-card tone-${item.tone}${active ? " is-active" : ""}`}
              onClick={() => onSelect(item)}
              aria-pressed={active}
            >
              <span className="diagnosis-quick-card-icon"><Icon /></span>
              <span className="diagnosis-quick-card-copy">
                <strong>{item.title}</strong>
                <em>{item.subtitle}</em>
              </span>
            </button>
          );
        })}
      </div>
    </nav>
  );
}

function QuickActionCustomizer({ open, cards, onCardsChange, onClose }) {
  const updateCard = (key, patch) => {
    const editsContent = Object.keys(patch).some((field) => field !== "enabled");
    onCardsChange(cards.map((item) => (item.key === key
      ? { ...item, ...patch, ...(editsContent ? { custom: true, managed: false } : {}) } : item)));
  };

  const addCard = () => {
    onCardsChange([...cards, createQuickActionCard(cards)]);
  };

  const removeCard = (key) => {
    const nextCards = cards.filter((item) => item.key !== key);
    onCardsChange(nextCards.length ? nextCards : normalizeQuickCards(DEFAULT_QUICK_ACTION_CARDS));
  };

  const resetCards = () => {
    onCardsChange(normalizeQuickCards(DEFAULT_QUICK_ACTION_CARDS));
  };

  return (
    <Modal
      title="自定义快速卡片"
      open={open}
      onCancel={onClose}
      width={920}
      className="diagnosis-quick-custom-modal"
      footer={[
        <Button key="reset" onClick={resetCards}>恢复默认</Button>,
        <Button key="add" onClick={addCard}>新增卡片</Button>,
        <Button key="done" type="primary" onClick={onClose}>完成</Button>,
      ]}
    >
      <div className="diagnosis-quick-customizer">
        {cards.map((item) => {
          const Icon = QUICK_ICON_MAP[item.iconKey] || RobotOutlined;
          return (
            <article key={item.key} className={`diagnosis-quick-editor tone-${item.tone}`}>
              <div className="diagnosis-quick-editor-preview">
                <span><Icon /></span>
                <div>
                  <strong>{item.title || "自定义卡片"}</strong>
                  <em>{item.subtitle || "快速入口"}</em>
                </div>
              </div>
              <div className="diagnosis-quick-editor-fields">
                <Input
                  value={item.title}
                  maxLength={12}
                  placeholder="标题"
                  onChange={(event) => updateCard(item.key, { title: event.target.value })}
                />
                <Input
                  value={item.subtitle}
                  maxLength={20}
                  placeholder="副标题"
                  onChange={(event) => updateCard(item.key, { subtitle: event.target.value })}
                />
                <Input.TextArea
                  value={item.prompt}
                  autoSize={{ minRows: 2, maxRows: 3 }}
                  placeholder="点击卡片后写入输入框的内容"
                  onChange={(event) => updateCard(item.key, { prompt: event.target.value })}
                />
                <div className="diagnosis-quick-editor-row">
                  <Select
                    value={item.iconKey}
                    options={QUICK_ICON_OPTIONS.map((option) => ({ value: option.value, label: option.label }))}
                    onChange={(value) => updateCard(item.key, { iconKey: value })}
                  />
                  <Select
                    value={item.tone}
                    options={QUICK_TONE_OPTIONS}
                    onChange={(value) => updateCard(item.key, { tone: value })}
                  />
                </div>
                <div className="diagnosis-quick-editor-actions">
                  <Checkbox checked={item.enabled !== false} onChange={(event) => updateCard(item.key, { enabled: event.target.checked })}>
                    显示
                  </Checkbox>
                  <Button type="text" danger onClick={() => removeCard(item.key)}>删除</Button>
                </div>
              </div>
            </article>
          );
        })}
      </div>
    </Modal>
  );
}

function AssistantActionProgressModal({ task, open, onClose, onOpenTarget }) {
  if (!task) return null;

  const running = task.status === "running";
  const statusView = {
    running: { label: "执行中", color: "processing", progress: "active" },
    success: { label: "执行完成", color: "success", progress: "success" },
    partial: { label: "部分完成", color: "warning", progress: "normal" },
    failed: { label: "执行失败", color: "error", progress: "exception" },
    needs_input: { label: "需要补充信息", color: "warning", progress: "normal" },
    unsupported: { label: "当前不可执行", color: "warning", progress: "normal" },
    blocked: { label: "已阻断", color: "warning", progress: "normal" },
    unknown: { label: "结果待核对", color: "warning", progress: "normal" },
    cancelled: { label: "已取消", color: "default", progress: "normal" },
    expired: { label: "已过期", color: "warning", progress: "normal" },
    plan_only: { label: "仅生成方案", color: "warning", progress: "normal" },
  }[task.status] || { label: "准备中", color: "default", progress: "normal" };

  return (
    <Modal
      title={task.title || "功能执行进度"}
      open={open}
      centered
      width={880}
      className="diagnosis-assistant-action-modal"
      onCancel={onClose}
      footer={[
        <Button key="close" onClick={onClose}>关闭</Button>,
        !running && task.targetKey ? (
          <Button key="target" type="primary" onClick={onOpenTarget}>
            {task.executionResult ? "查看查询报告" : "打开功能"}
          </Button>
        ) : null,
      ].filter(Boolean)}
    >
      <section className="diagnosis-assistant-action" aria-live="polite">
        <header className="diagnosis-assistant-action-head">
          <div>
            <strong>{task.targetName || task.title}</strong>
            <span>{task.command || "由 TraceMind AI 发起"}</span>
          </div>
          <Tag color={statusView.color}>{statusView.label}</Tag>
        </header>

        <Progress
          percent={Math.max(0, Math.min(100, Number(task.percent) || 0))}
          status={statusView.progress}
          showInfo={false}
          strokeColor={task.status === "needs_input" ? "#d97706" : "#1677ff"}
        />

        {task.error && (
          <Alert
            type={["needs_input", "unsupported", "blocked", "unknown", "plan_only"].includes(task.status) ? "warning" : "error"}
            showIcon
            message={["needs_input", "unsupported", "blocked", "plan_only"].includes(task.status) ? "暂未执行" : task.status === "unknown" ? "结果待核对" : "执行失败"}
            description={task.error}
          />
        )}
        {task.summary && <Alert type={task.status === "success" ? "success" : "warning"} showIcon message={task.summary} />}
        {task.notice && <Alert type="info" showIcon message={task.notice} />}

        {task.metrics?.length > 0 && (
          <div className="diagnosis-assistant-action-metrics">
            {task.metrics.map((item) => (
              <div key={`${item.label}-${item.value}`}>
                <span>{item.label}</span>
                <strong>{compactText(item.value, "0")}</strong>
              </div>
            ))}
          </div>
        )}

        <div className="diagnosis-assistant-action-steps">
          {task.steps?.map((step) => (
            <div key={step.key} className={`diagnosis-assistant-action-step status-${step.status}`}>
              <span className="diagnosis-assistant-action-step-icon" aria-label={step.status}>
                {step.status === "success" && <CheckCircleOutlined />}
                {step.status === "failed" && <CloseOutlined />}
                {step.status === "running" && <ReloadOutlined spin />}
                {step.status === "pending" && <ClockCircleOutlined />}
              </span>
              <div>
                <strong>{step.title}</strong>
                <span>{step.detail}</span>
              </div>
            </div>
          ))}
        </div>

        {task.preview && (
          <details className="diagnosis-assistant-action-preview">
            <summary>查看原始结果</summary>
            <pre>{task.preview}</pre>
          </details>
        )}
      </section>
    </Modal>
  );
}

function LandingPage({
  dbInstances,
  instanceId,
  setInstanceId,
  onNavigate,
  problemText,
  setProblemText,
  onOpenRuleDiagnosis,
  inspectionBatch,
  inspectionModalOpen,
  onInspectionModalOpenChange,
  onOpenInspectionTasks,
  assistantActionTask,
  assistantActionModalOpen,
  onAssistantActionModalOpenChange,
  onOpenAssistantActionTarget,
  onOpenPlans,
  running,
  onRun,
  entryMode,
  setEntryMode,
  menuItems,
  items,
  total,
  loading,
  loadingMore,
  hasMoreIncidents,
  onOpenIncident,
  onReload,
  onLoadMore,
}) {
  const [activeQuickKey, setActiveQuickKey] = useState("");
  const [quickCustomizerOpen, setQuickCustomizerOpen] = useState(false);
  const [quickCards, setQuickCards] = useState(loadQuickActionCards);
  const isEngineer = entryMode === "engineer";
  const hintItems = isEngineer ? ENGINEER_HINTS : CUSTOMER_HINTS;
  const selectedInstance = dbInstances.find((item) => item.id === instanceId);
  const visibleQuickCards = useMemo(
    () => quickActionCardsForContext(quickCards, { dbType: selectedInstance?.db_type, menuItems }),
    [quickCards, selectedInstance?.db_type, menuItems],
  );
  const inspectionPercent = inspectionBatch?.total
    ? Math.round((inspectionBatch.completed / inspectionBatch.total) * 100)
    : 0;
  const inspectionRunning = inspectionBatch?.status === "running";
  const inspectionTitle = inspectionRunning
    ? `正在巡检 ${inspectionBatch.current?.instanceName || "数据库"}`
    : inspectionBatch?.status === "completed"
      ? "全部数据库巡检完成"
      : inspectionBatch?.status === "partial"
        ? "数据库巡检完成，部分实例失败"
        : "数据库巡检未完成";

  const updateQuickCards = (cards) => {
    const nextCards = normalizeQuickCards(cards);
    setQuickCards(nextCards);
    saveQuickActionCards(nextCards);
  };

  const selectQuickAction = (item) => {
    setActiveQuickKey(item.key);
    if (item.targetKey && onNavigate) {
      message.success(`已打开${item.title}`);
      onNavigate(item.targetKey, item.instanceId || instanceId);
      return;
    }
    setProblemText(item.prompt);
  };

  useEffect(() => {
    const syncQuickCards = () => setQuickCards(loadQuickActionCards());
    const syncFromStorage = (event) => {
      if (event.key && event.key !== QUICK_ACTION_STORAGE_KEY) return;
      syncQuickCards();
    };
    window.addEventListener(QUICK_ACCESS_UPDATED_EVENT, syncQuickCards);
    window.addEventListener("storage", syncFromStorage);
    return () => {
      window.removeEventListener(QUICK_ACCESS_UPDATED_EVENT, syncQuickCards);
      window.removeEventListener("storage", syncFromStorage);
    };
  }, []);

  return (
    <div className="diagnosis-cockpit-shell diagnosis-entry-screen diagnosis-entry-single">
      <section className={`diagnosis-single-entry-card mode-${entryMode}`} aria-label="TraceMind AI 诊断入口">
        <header className="diagnosis-single-entry-head">
          <div className="diagnosis-single-brand">
            <span>AI</span>
            <div>
              <strong>TraceMind AI</strong>
              <Text>{isEngineer ? "工程师诊断入口" : "客户诊断入口"}</Text>
            </div>
          </div>
          <div className="diagnosis-single-entry-actions">
            <Button icon={<FileSearchOutlined />} disabled={running} onClick={onOpenPlans}>任务清单</Button>
            <div className="diagnosis-entry-mode-switch" aria-label="入口模式">
              {[
                ["customer", "客户"],
                ["engineer", "工程师"],
              ].map(([key, label]) => (
                <button
                  key={key}
                  type="button"
                  className={entryMode === key ? "active" : ""}
                  onClick={() => setEntryMode(key)}
                >
                  {label}
                </button>
              ))}
            </div>
            {onOpenRuleDiagnosis && (
              <Tooltip title="打开规则分析">
                <Button
                  className="diagnosis-rule-analysis-button"
                  aria-label="打开规则分析"
                  icon={<FileSearchOutlined />}
                  onClick={onOpenRuleDiagnosis}
                />
              </Tooltip>
            )}
          </div>
        </header>

        {isEngineer && (
          <Select
            className="diagnosis-single-instance-picker"
            value={instanceId}
            onChange={setInstanceId}
            placeholder="选择诊断实例"
            options={dbInstances.map((item) => ({
              value: item.id,
              label: `[${(item.db_type || "").toUpperCase()}] ${item.instance_name} (${item.host}:${item.port})`,
            }))}
          />
        )}

        <Input.TextArea
          className="diagnosis-single-textarea"
          value={problemText}
          onChange={(event) => {
            setActiveQuickKey("");
            setProblemText(event.target.value);
          }}
          autoSize={false}
          aria-label={isEngineer ? "工程师上下文" : "你要办理的需求"}
          maxLength={2000}
          disabled={running}
          placeholder={isEngineer ? ENGINEER_PLACEHOLDER : CUSTOMER_PLACEHOLDER}
        />

        <footer className="diagnosis-single-entry-footer">
          <div className="diagnosis-single-hints" aria-label="输入提示">
            {hintItems.map((item) => <span key={item}>{item}</span>)}
          </div>
          <div className="diagnosis-single-entry-commands">
            {assistantActionTask && (
              <Button
                size="large"
                icon={assistantActionTask.status === "running" ? <ReloadOutlined spin /> : <ThunderboltOutlined />}
                onClick={() => onAssistantActionModalOpenChange(true)}
              >
                查看执行进度
              </Button>
            )}
            {inspectionBatch && (
              <Button
                size="large"
                icon={<FileSearchOutlined />}
                onClick={() => onInspectionModalOpenChange(true)}
              >
                查看巡检进度
              </Button>
            )}
            <Button
              type="primary"
              size="large"
              icon={<ThunderboltOutlined />}
              loading={running}
              disabled={!problemText.trim()}
              onClick={onRun}
            >
              让 TraceMind 执行
            </Button>
          </div>
        </footer>

        {isEngineer && (
          <div className="diagnosis-engineer-options">
            <span>{selectedInstance ? `${selectedInstance.instance_name} · ${(selectedInstance.db_type || "").toUpperCase()}` : "未选择实例"}</span>
            <Tag className="diagnosis-plan-tag" icon={<SafetyOutlined />}>plan_only</Tag>
          </div>
        )}
      </section>

      <section className="diagnosis-recent-incidents" aria-label="历史诊断">
        <header className="diagnosis-recent-incidents-bar">
          <div>
            <span>历史诊断</span>
            <strong>{total > 0 ? `共 ${total} 条` : "暂无记录"}</strong>
          </div>
          <Button size="small" icon={<ReloadOutlined />} loading={loading} onClick={onReload}>刷新</Button>
        </header>
        {loading && items.length === 0 ? (
          <p className="diagnosis-recent-incidents-empty">正在加载历史诊断…</p>
        ) : items.length > 0 ? (
          <>
            <ul className="diagnosis-recent-incidents-list">
              {items.map((row) => (
                <li key={row.id}>
                  <button type="button" onClick={() => onOpenIncident(row)}>
                    <Tag color={severityColor(row.severity)}>{severityLabel(row.severity)}</Tag>
                    <strong>{compactText(row.title || row.incident_no, "未命名诊断")}</strong>
                    <span>
                      {compactText(row.incident_no, "-")} · {compactText(row.instance_name, "未知实例")} · {shortTime(row.create_time)}
                      {row.evidence_count ? ` · ${row.evidence_count} 条证据` : ""}
                    </span>
                    {row.root_cause_summary ? <em>{compactText(row.root_cause_summary, "")}</em> : null}
                  </button>
                </li>
              ))}
            </ul>
            <div className="diagnosis-recent-incidents-footer">
              {hasMoreIncidents ? (
                <Button size="small" loading={loadingMore} onClick={onLoadMore}>
                  加载更多（已显示 {items.length} / {total}）
                </Button>
              ) : (
                <span>已显示当前实例全部 {items.length} 条诊断记录</span>
              )}
            </div>
          </>
        ) : (
          <p className="diagnosis-recent-incidents-empty">
            {instanceId
              ? "当前实例还没有诊断记录，描述现象后即可发起第一次诊断。"
              : "请先在上方选择数据库实例，再查看历史诊断。"}
          </p>
        )}
      </section>

      <QuickActionDock
        cards={visibleQuickCards}
        activeKey={activeQuickKey}
        onSelect={selectQuickAction}
        onCustomize={() => setQuickCustomizerOpen(true)}
      />
      <QuickActionCustomizer
        open={quickCustomizerOpen}
        cards={quickCards}
        onCardsChange={updateQuickCards}
        onClose={() => setQuickCustomizerOpen(false)}
      />
      <Modal
        title={inspectionTitle}
        open={Boolean(inspectionBatch) && inspectionModalOpen}
        centered
        width={980}
        className="diagnosis-assistant-inspection-modal"
        onCancel={() => onInspectionModalOpenChange(false)}
        footer={[
          <Button key="close" onClick={() => onInspectionModalOpenChange(false)}>
            关闭
          </Button>,
          !inspectionRunning ? (
            <Button key="tasks" type="primary" icon={<FileSearchOutlined />} onClick={onOpenInspectionTasks}>
              查看巡检任务
            </Button>
          ) : null,
        ].filter(Boolean)}
      >
        {inspectionBatch && (
          <section className="diagnosis-assistant-inspection" aria-live="polite">
            <header className="diagnosis-assistant-inspection-head">
              <div>
                <strong>巡检进度</strong>
                <span>
                  已完成 {inspectionBatch.completed}/{inspectionBatch.total}
                  {" · "}成功 {inspectionBatch.succeeded}
                  {" · "}失败 {inspectionBatch.failed}
                </span>
              </div>
              <Tag color={inspectionRunning ? "processing" : inspectionBatch.failed ? "warning" : "success"}>
                {inspectionRunning ? "执行中" : inspectionBatch.failed ? "部分失败" : "已完成"}
              </Tag>
            </header>
            <Progress
              percent={inspectionPercent}
              status={
                inspectionBatch.status === "failed"
                  ? "exception"
                  : inspectionBatch.status === "completed"
                    ? "success"
                    : inspectionBatch.status === "partial"
                      ? "normal"
                      : "active"
              }
              showInfo={false}
              strokeColor={inspectionBatch.status === "partial" ? "#d97706" : "#1c7a60"}
            />
            <div className="diagnosis-assistant-inspection-list">
              {inspectionBatch.items.map((item) => (
                <div key={item.key} className={`diagnosis-assistant-inspection-row status-${item.status}`}>
                  <span className="diagnosis-assistant-inspection-state" aria-label={item.status}>
                    {item.status === "success" && <CheckCircleOutlined />}
                    {item.status === "failed" && <CloseOutlined />}
                    {item.status === "partial" && <ExclamationCircleOutlined />}
                    {item.status === "running" && <ReloadOutlined spin />}
                    {item.status === "pending" && <ClockCircleOutlined />}
                  </span>
                  <div className="diagnosis-assistant-inspection-instance">
                    <strong>{item.instanceName}</strong>
                    <span>{item.endpoint}</span>
                  </div>
                  <Tag>{item.dbType.toUpperCase()}</Tag>
                  <div className="diagnosis-assistant-inspection-result">
                    {item.status === "success" && (
                      <>
                        <strong>健康分 {item.score ?? "-"}</strong>
                        <span>
                          {item.riskLevel || "normal"} · {item.templateName || "默认规则"}
                          {item.templateRevision ? ` · 修订 ${item.templateRevision}` : ""}
                          {" · "}任务 {item.taskId}
                        </span>
                      </>
                    )}
                    {["failed", "partial"].includes(item.status) && (
                      <>
                        <strong>{item.status === "partial" ? "巡检部分完成" : "巡检采集失败"}</strong>
                        <span>
                          {item.templateName || "默认规则"}
                          {item.templateRevision ? ` · 修订 ${item.templateRevision}` : ""}
                          {item.taskId ? ` · 任务 ${item.taskId}` : ""}
                        </span>
                        <span title={item.error}>{item.error}</span>
                      </>
                    )}
                    {item.status === "running" && <span>正在采集并分析</span>}
                    {item.status === "pending" && <span>等待执行</span>}
                  </div>
                </div>
              ))}
            </div>
          </section>
        )}
      </Modal>
      <AssistantActionProgressModal
        task={assistantActionTask}
        open={Boolean(assistantActionTask) && assistantActionModalOpen}
        onClose={() => onAssistantActionModalOpenChange(false)}
        onOpenTarget={onOpenAssistantActionTarget}
      />
    </div>
  );
}

function FaultChain({ nodes }) {
  return (
    <section className="diagnosis-fault-chain">
      <div className="diagnosis-section-head">
        <div>
          <Text strong>故障传播链</Text>
          <Text type="secondary">入口、应用、数据库与证据的传播关系。</Text>
        </div>
        <Tag color="green">Adaptive Workspace</Tag>
      </div>
      <div className="diagnosis-chain-stage">
        {nodes.length ? nodes.map((node, index) => (
          <React.Fragment key={node.key}>
            <article className={`diagnosis-chain-node${node.critical ? " is-critical" : ""}`}>
              <div className="diagnosis-chain-node-top">
                <span>{node.layer}</span>
                <Tag color={node.critical ? "red" : "green"}>{node.status}</Tag>
              </div>
              <strong>{node.asset}</strong>
              <p>{node.exception}</p>
              <div className="diagnosis-chain-meta">
                <span>{node.time}</span>
                <span>{node.evidenceCount || 0} 条证据</span>
              </div>
              {node.suspected && <div className="diagnosis-root-badge">Suspected Root Node</div>}
            </article>
            {index < nodes.length - 1 && <div className="diagnosis-chain-link"><span /></div>}
          </React.Fragment>
        )) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="暂无持久化传播链证据；需要采集并保存真实拓扑后才能展示传播关系。" />}
      </div>
    </section>
  );
}

function RecommendationRail({ recommendations, onOpenCard }) {
  const levelClass = {
    必须: "must",
    建议: "suggest",
    可选: "optional",
  };

  return (
    <aside className="diagnosis-recommendation-rail">
      <div className="diagnosis-section-head">
        <div>
          <Text strong>下一步建议</Text>
          <Text type="secondary">TraceMind 根据当前上下文推荐卡片。</Text>
        </div>
      </div>
      <div className="diagnosis-recommendation-list">
        {recommendations.map((item) => (
          <article key={item.key} className={`diagnosis-recommendation-card level-${levelClass[item.level]}`}>
            <div className="diagnosis-recommendation-top">
              <span>{item.level}</span>
              {item.opened && <Tag color="green">已打开</Tag>}
            </div>
            <strong>{item.title}</strong>
            <p>{item.reason}</p>
            <em>{item.outcome}</em>
            <Button disabled={item.opened} onClick={() => onOpenCard(item.key)}>
              {item.opened ? "正在查看" : item.action}
            </Button>
          </article>
        ))}
      </div>
    </aside>
  );
}

function FloatingCard({ definition, pinned, minimized, onPin, onMinimize, onClose, children }) {
  const Icon = definition.icon;
  return (
    <article className={`diagnosis-floating-card tone-${definition.tone}${pinned ? " is-pinned" : ""}${minimized ? " is-minimized" : ""}`}>
      <header>
        <div className="diagnosis-card-title">
          <span><Icon /></span>
          <strong>{definition.title}</strong>
        </div>
        <Space size={4}>
          <Tooltip title={pinned ? "取消固定" : "固定卡片"}>
            <Button type="text" icon={<PushpinOutlined />} onClick={onPin} />
          </Tooltip>
          <Tooltip title={minimized ? "展开" : "最小化"}>
            <Button type="text" icon={<MinusOutlined />} onClick={onMinimize} />
          </Tooltip>
          <Tooltip title="关闭">
            <Button type="text" icon={<CloseOutlined />} onClick={onClose} />
          </Tooltip>
        </Space>
      </header>
      {!minimized && <div className="diagnosis-card-body">{children}</div>}
    </article>
  );
}

function CardWorkspace({
  activeCards,
  minimizedCards,
  pinnedCards,
  detail,
  evidenceRows,
  rootCauseRows,
  timelineRows,
  methodologies,
  dataSources,
  autoPolicy,
  onOpenCard,
  onCloseCard,
  onToggleMinimize,
  onTogglePin,
}) {
  const openedDefinitions = CARD_DEFINITIONS.filter((item) => activeCards.includes(item.key));

  return (
    <section className="diagnosis-card-workspace">
      <div className="diagnosis-section-head">
        <div>
          <Text strong>诊断卡片</Text>
          <Text type="secondary">卡片可打开、关闭、固定和最小化。</Text>
        </div>
        <Button onClick={() => onOpenCard("evidence")}>打开证据链</Button>
      </div>
      {openedDefinitions.length ? (
        <div className="diagnosis-card-grid">
          {openedDefinitions.map((definition) => (
            <FloatingCard
              key={definition.key}
              definition={definition}
              pinned={pinnedCards.includes(definition.key)}
              minimized={minimizedCards.includes(definition.key)}
              onPin={() => onTogglePin(definition.key)}
              onMinimize={() => onToggleMinimize(definition.key)}
              onClose={() => onCloseCard(definition.key)}
            >
              <CardBody
                cardKey={definition.key}
                detail={detail}
                evidenceRows={evidenceRows}
                rootCauseRows={rootCauseRows}
                timelineRows={timelineRows}
                methodologies={methodologies}
                dataSources={dataSources}
                autoPolicy={autoPolicy}
                onOpenCard={onOpenCard}
              />
            </FloatingCard>
          ))}
        </div>
      ) : (
        <div className="diagnosis-card-empty">
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前没有打开的诊断卡片。" />
          <Button type="primary" onClick={() => onOpenCard("summary")}>打开案例摘要</Button>
        </div>
      )}
    </section>
  );
}

function CardBody({
  cardKey,
  detail,
  evidenceRows,
  rootCauseRows,
  timelineRows,
  methodologies,
  dataSources,
  autoPolicy,
  onOpenCard,
}) {
  const topCause = rootCauseRows[0];

  if (cardKey === "summary") {
    return (
      <div className="diagnosis-summary-card">
        <div className="diagnosis-summary-statement">
          <span>AI 结论</span>
          <p>{compactText(detail?.ai_summary || detail?.root_cause_summary, "当前案例正在聚合证据，优先查看时间线与根因候选。")}</p>
        </div>
        <div className="diagnosis-metric-grid">
          <div><span>风险</span><strong>{severityLabel(detail?.severity)}</strong></div>
          <div><span>评分</span><strong>{detail?.score ?? "-"}</strong></div>
          <div><span>证据</span><strong>{evidenceRows.length}</strong></div>
          <div><span>候选</span><strong>{rootCauseRows.length}</strong></div>
        </div>
        <button type="button" className="diagnosis-inline-action" onClick={() => onOpenCard("report")}>
          生成面向客户的售后报告草稿
        </button>
      </div>
    );
  }

  if (cardKey === "timeline") {
    return timelineRows.length ? (
      <div className="diagnosis-timeline-list">
        {timelineRows.slice(0, 7).map((item) => (
          <article key={stableContentKey("timeline", item, ["event_time", "layer", "source", "title", "severity"])}>
            <span>{shortTime(item.event_time)}</span>
            <div>
              <strong>{compactText(item.title || item.source, "诊断事件")}</strong>
              <p>{compactText(item.layer, "未知层级")} · {compactText(item.source, "系统采集")}</p>
            </div>
            <Tag color={severityColor(item.severity)}>{severityLabel(item.severity)}</Tag>
          </article>
        ))}
      </div>
    ) : (
      <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无时间线，执行诊断后会自动对齐异常时间窗。" />
    );
  }

  if (cardKey === "rootCause") {
    return rootCauseRows.length ? (
      <div className="diagnosis-rootcause-list">
        {rootCauseRows.slice(0, 5).map((item) => (
          <article key={stableContentKey("root-cause", item, ["rank", "layer", "cause", "total_score", "confidence"])}>
            <div className="diagnosis-rootcause-rank">#{item.rank || "-"}</div>
            <div>
              <strong>{compactText(item.cause, "根因候选")}</strong>
              <p>{compactText(item.suggestion, "需要继续收集证据确认该候选。")}</p>
            </div>
            <div className="diagnosis-confidence">
              <span>置信度</span>
              <strong>{item.confidence ?? item.total_score ?? "-"}</strong>
            </div>
          </article>
        ))}
      </div>
    ) : (
      <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无根因候选，建议先补充客户现象或打开证据链。" />
    );
  }

  if (cardKey === "evidence") {
    return evidenceRows.length ? (
      <div className="diagnosis-evidence-list">
        {evidenceRows.slice(0, 8).map((item) => (
          <article key={stableContentKey("evidence", item, ["layer", "source", "severity", "phenomenon", "evidence"])}>
            <div>
              <Tag color={severityColor(item.severity)}>{compactText(item.layer, "证据")}</Tag>
              <strong>{compactText(item.phenomenon || item.evidence, "证据项")}</strong>
            </div>
            <p>{compactText(item.evidence || item.judgement, "等待采集更多证据。")}</p>
            <em>{compactText(item.suggestion || item.judgement, "用于支撑或排除根因候选。")}</em>
          </article>
        ))}
      </div>
    ) : (
      <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无证据明细，建议确认数据源接入和诊断实例。" />
    );
  }

  if (cardKey === "change") {
    // 四类变更的接入状态只能来自变更类数据源的采集结果。
    // 平台当前没有变更类数据源 —— 这是一条**恒真的能力事实**，必须常驻展示；
    // 不能写成「命中数为 0 才提示」，否则只要有一条证据文本恰好含关键词，
    // 「未接入变更类数据源」的结论就会消失，等于把能力缺口说成已验证。
    // 下面的计数同样不是变更记录：它只是证据文本与类别关键词的模糊包含匹配，
    // 例如证据标签「表空间使用率过高（storage/TEMP）」里的 storage 会命中「容量水位」。
    const changeCategories = [
      { label: "应用发布 / 配置变更", keywords: ["发布", "配置变更", "版本", "deploy", "release", "nginx", "tomcat"] },
      // 不放 "os"：两字母子串会命中 cost / host / process 等无关文本。
      { label: "数据库参数 / 统计信息变更", keywords: ["参数变更", "参数", "统计信息", "analyze", "statistics"] },
      { label: "批处理 / 数据导入任务", keywords: ["批处理", "批量", "导入", "batch", "job", "kafka"] },
      { label: "容量水位 / 连接池上限", keywords: ["容量", "连接池", "水位", "pool", "storage", "redis"] },
    ];
    const changeCounts = changeCategories.map(({ keywords }) => (evidenceRows || []).filter((row) => {
      const text = `${row.phenomenon || ""} ${row.evidence || ""} ${row.source || ""} ${row.layer || ""}`.toLowerCase();
      return keywords.some((word) => text.includes(word.toLowerCase()));
    }).length);
    const totalChangeHits = changeCounts.reduce((sum, count) => sum + count, 0);
    return (
      <div className="diagnosis-change-card">
        <div className="diagnosis-summary-statement">
          <span>变更窗口</span>
          <p>将客户报障时间与发布、参数调整、批处理、容量变化和外部告警对齐，确认是否存在触发点。</p>
        </div>
        <div className="diagnosis-check-list">
          {changeCategories.map(({ label }, index) => (
            <div key={label}>
              <span>{changeCounts[index] > 0 ? `文本近似 ${changeCounts[index]} 处` : "无文本近似"}</span>
              <strong>{label}</strong>
            </div>
          ))}
        </div>
        <div className="diagnosis-summary-statement">
          <span>未验证</span>
          <p>平台当前未接入变更类数据源（已登记的数据源类型只有
            nginx / tomcat / redis / kafka / skywalking / os / storage），
            以上四类均未经变更数据验证，不能作为结论使用。</p>
        </div>
        {totalChangeHits > 0 ? (
          <div className="diagnosis-summary-statement">
            <span>文本近似说明</span>
            <p>上方共 {totalChangeHits} 处命中，来源是证据文本与类别关键词的模糊包含匹配
              （例如证据标签里的「storage/TEMP」会命中「容量水位」）。
              这是文本近似，不是变更记录，不能据此判定本次故障与变更相关。</p>
          </div>
        ) : (
          <div className="diagnosis-summary-statement">
            <span>无文本近似</span>
            <p>本次证据链里没有出现上述四类变更的关键词，也不代表已排除变更因素。</p>
          </div>
        )}
      </div>
    );
  }

  if (cardKey === "lab") {
    return (
      <div className="diagnosis-lab-card">
        <div className="diagnosis-lab-focus">
          <span>当前优先验证</span>
          <strong>{compactText(topCause?.cause || detail?.root_cause_summary, "数据库等待与应用超时之间的传播关系")}</strong>
          <p>{compactText(topCause?.suggestion, "对比故障窗口前后指标，确认支撑证据和反证。")}</p>
        </div>
        <div className="diagnosis-check-list">
          <div><span>支撑证据</span><strong>{evidenceRows.length} 条</strong></div>
          <div><span>反证检查</span><strong>未计算</strong></div>
          <div><span>方法论</span><strong>{methodologies.length} 个</strong></div>
        </div>
        <div className="diagnosis-summary-statement">
          <span>反证未执行</span>
          <p>平台目前没有自动反证校验能力，本卡片不提供反证结论。请人工核对是否存在与当前根因假设相矛盾的现象，
            确认后再将结论写入报告。</p>
        </div>
      </div>
    );
  }

  if (cardKey === "retest") {
    return (
      <>
        <div className="diagnosis-check-list diagnosis-retest-list">
          {[
            "接口超时率回落到故障前基线",
            "数据库锁等待 / 慢 SQL 指标回落",
            "连接池占用低于预警阈值",
            "核心业务链路复测通过",
          ].map((item) => (
            <div key={item}>
              <span>plan_only</span>
              <strong>{item}</strong>
            </div>
          ))}
        </div>
        <div className="diagnosis-summary-statement">
          <span>未执行复测</span>
          <p>以上四项是本平台给出的复测建议，全部为待执行状态。当前没有自动复测通道，诊断完成本身不代表指标已回落；
            需人工按上述项核对后，再把结果补进报告和工单。</p>
        </div>
      </>
    );
  }

  if (cardKey === "report") {
    return (
      <div className="diagnosis-report-card">
        {["客户现象", "证据链", "根因候选", "处置与复测", "复盘建议"].map((item, index) => (
          <article key={item}>
            <span>0{index + 1}</span>
            <strong>{item}</strong>
            <p>
              {index === 0 && compactText(detail?.title, "记录客户原始报障描述。")}
              {index === 1 && `${evidenceRows.length} 条证据可写入报告。`}
              {index === 2 && compactText(topCause?.cause || detail?.root_cause_summary, "根因仍需确认。")}
              {index === 3 && "默认输出 plan_only 处置和复测步骤。"}
              {index === 4 && "沉淀可复用的售后排查路径。"}
            </p>
          </article>
        ))}
      </div>
    );
  }

  return methodologies.length || dataSources.length || autoPolicy ? (
    <div className="diagnosis-knowledge-card">
      {[...methodologies.slice(0, 4), ...dataSources.slice(0, 2)].map((item) => {
        // methodology 返回 title/symptom/troubleshoot_path/solution（methodology_kb.py:314-334），
        // 数据源返回 name/source_type/endpoint/remark（diagnosis_center.py:532-544）；
        // 两者都没有 description/summary/type，按旧写法正文会全部落同一句兜底文案。
        const isDataSource = Boolean(item.source_type);
        const body = isDataSource
          ? compactText(item.endpoint || item.remark, `数据源类型 ${item.source_type}`)
          : compactText(
            item.symptom || (item.troubleshoot_path || []).join(" → "),
            "用于补充当前案例的排查路径。",
          );
        return (
          <article key={stableContentKey("knowledge", item, ["id", "name", "title", "source_type"])}>
            <strong>{compactText(item.name || item.title || item.source, "诊断知识")}</strong>
            <p>{body}</p>
          </article>
        );
      })}
      {autoPolicy && (
        <article>
          <strong>自动诊断策略</strong>
          <p>{autoPolicy.enabled ? "当前实例已启用自动策略，可持续捕捉异常上下文。" : "当前实例尚未启用自动策略，可按需打开。"} </p>
        </article>
      )}
    </div>
  ) : (
    <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无知识库内容。" />
  );
}

function ReportBatchModal({ open, onClose, batch }) {
  return (
    <Modal title="统一运维报告 / 售后分析报告" open={open} onCancel={onClose}
      footer={<Button onClick={onClose}>关闭</Button>} centered width={760}>
      {batch?.status === "running" && <p>正在生成统一运维报告和售后分析报告，请稍候…</p>}
      {batch?.status === "failed" && <p role="alert">{batch.error}</p>}
      {batch?.status === "completed" && (
        <div>
          {batch.items.map((item, index) => (
            <div key={item.artifact_id || `${item.report_type}-${index}`} style={{ marginBottom: 10 }}>
              <strong>{item.report_type === "after_sales" ? "售后分析报告" : "统一运维报告"}</strong>
              {item.artifact_id ? (
                <>
                  <span> · {item.analysis_source === "ai" ? `AI（${item.model || "模型"}）` : "规则引擎"}</span>
                  <Button size="small" style={{ marginLeft: 12 }} onClick={() => {
                    const format = item.format === "html" ? "html" : "markdown";
                    downloadReportArtifact(
                      item.artifact_id, format,
                      `${item.report_type === "after_sales" ? "售后分析报告" : "统一运维报告"}.${format === "html" ? "html" : "md"}`,
                    );
                  }}>下载</Button>
                </>
              ) : (
                <span> · 未生成（{item.reason_code === "no_evidence" ? "时间窗内没有巡检/告警/容量证据" : "缺少证据"}）</span>
              )}
            </div>
          ))}
        </div>
      )}
    </Modal>
  );
}

function DiagnosisDock({ activeCards, minimizedCards, recommendations, onOpenCard }) {
  const recommendedKeys = new Set(recommendations.filter((item) => item.level !== "可选").map((item) => item.key));
  return (
    <nav className="diagnosis-dock" aria-label="诊断流程">
      {DOCK_FLOW.map((key) => {
        const definition = CARD_DEFINITIONS.find((item) => item.key === key);
        const opened = activeCards.includes(key);
        const minimized = minimizedCards.includes(key);
        const status = opened ? (minimized ? "minimized" : "opened") : recommendedKeys.has(key) ? "recommended" : "pending";
        return (
          <button key={key} type="button" className={`diagnosis-dock-node status-${status}`} onClick={() => onOpenCard(key)}>
            <span>{definition.dock}</span>
            <em>{status === "opened" ? "opened" : status === "recommended" ? "recommended" : status === "minimized" ? "minimized" : "pending"}</em>
          </button>
        );
      })}
    </nav>
  );
}

export function DiagnosisCenterPage({ instances, selectedInstanceId, menuItems, onNavigate, onOpenRuleDiagnosis }) {
  const dbInstances = useMemo(
    () => instances.filter((item) => ["oracle", "mysql", "postgresql", "sqlserver"].includes(item.db_type)),
    [instances],
  );
  const [instanceId, setInstanceId] = useSyncedInstanceId(selectedInstanceId, dbInstances, { fallbackToFirst: false });
  const selectedInstance = dbInstances.find((item) => item.id === instanceId);
  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [running, setRunning] = useState(false);
  const assistantDispatchBusyRef = useRef(null);
  const [detail, setDetail] = useState(null);
  const [methodologies, setMethodologies] = useState([]);
  const [dataSources, setDataSources] = useState([]);
  const [archProfile, setArchProfile] = useState(null);
  const [autoPolicy, setAutoPolicy] = useState(null);
  const [problemText, setProblemText] = useState("");
  const [entryMode, setEntryMode] = useState("customer");
  const [inspectionBatch, setInspectionBatch] = useState(null);
  const [inspectionModalOpen, setInspectionModalOpen] = useState(false);
  const [assistantActionTask, setAssistantActionTask] = useState(null);
  const [assistantActionModalOpen, setAssistantActionModalOpen] = useState(false);
  const [planWorkspaceOpen, setPlanWorkspaceOpen] = useState(false);
  const [planRequest, setPlanRequest] = useState(null);
  const [reportBatch, setReportBatch] = useState(null);
  const [reportModalOpen, setReportModalOpen] = useState(false);
  const [clarification, setClarification] = useState(null);
  const [pendingDispatch, setPendingDispatch] = useState(null);
  const recoveryStartedRef = useRef(false);
  const recoveryInstanceRef = useRef(instanceId);
  const incidentContextRef = useRef(null);
  useEffect(() => {
    const next = instanceId || null;
    if (recoveryInstanceRef.current !== next) incidentContextRef.current = null;
    recoveryInstanceRef.current = next;
  }, [instanceId]);
  const [aiDegradationNotice, setAiDegradationNotice] = useState(null);
  const assistantRequestEpoch = useRef(0);
  const dispatchEpochRef = assistantRequestEpoch;
  useEffect(() => {
    assistantRequestEpoch.current += 1;
    assistantDispatchBusyRef.current = null;
    setRunning(false);
    setClarification(null);
    setPendingDispatch((current) => current?.instanceId === (instanceId || null) ? current : null);
  }, [instanceId]);
  const [workspaceStarted, setWorkspaceStarted] = useState(false);
  const [ticketSource, setTicketSource] = useState(null);
  const [activeCards, setActiveCards] = useState(["summary", "timeline", "rootCause"]);
  const [minimizedCards, setMinimizedCards] = useState([]);
  const [pinnedCards, setPinnedCards] = useState(["summary"]);
  useEffect(() => {
    if (recoveryStartedRef.current) return;
    recoveryStartedRef.current = true;
    let alive = true;
    const recoverySession = getAuthSession();
    const saved = readAssistantDispatchJournal();
    if (!saved || saved.loginEpoch !== recoverySession.loginEpoch
      || saved.instanceId !== (recoveryInstanceRef.current || null)) return undefined;
    const recoveryEpoch = dispatchEpochRef.current;
    const current = () => alive && recoveryEpoch === dispatchEpochRef.current
      && isAuthSessionCurrent(recoverySession)
      && (recoveryInstanceRef.current || null) === saved.instanceId
      && currentAssistantDispatchJournal({ requestId: saved.requestId, instanceId: saved.instanceId, session: recoverySession });
    if (saved.text) setProblemText((currentText) => current() && !currentText.trim() ? saved.text : currentText);
    resolveCustomerRequirement({ text: saved.text, instanceId: saved.instanceId,
      requestId: saved.requestId, recoverOnly: true, dispatch: dispatchAssistantIntent,
      readReceipt: getDispatchReceipt, resolveNavigation: () => null }).then((result) => {
      if (!current()) return;
      if (result.mode === "plan" || result.mode === "input") {
        setPlanRequest({ capability: result.capability, instanceId: saved.instanceId, requirementText: saved.text,
          planId: result.planId, params: result.params || {}, requestId: result.requestId || saved.requestId });
        setPlanWorkspaceOpen(true);
      } else if (result.mode === "batch") {
        setPlanRequest({ batchId: result.batchId, instanceId: saved.instanceId, requirementText: saved.text,
          requestId: result.requestId || saved.requestId });
        setPlanWorkspaceOpen(true);
      } else if (result.mode === "clarify") {
        setClarification({ ...result, text: saved.text, instanceId: saved.instanceId });
      } else if (result.mode === "pending") {
        setPendingDispatch({ ...result, instanceId: saved.instanceId, text: saved.text });
        setAiDegradationNotice({ tone: "warning", text: result.reason });
      } else {
        clearAssistantDispatchJournal(saved.requestId);
      }
    }).catch((error) => {
      if (!current()) return;
      clearAssistantDispatchJournal(saved.requestId);
      setPendingDispatch(null);
      setAiDegradationNotice({ tone: "warning", text: error.message || "原请求恢复失败，请重新发起。" });
    });
    return () => { alive = false; };
  }, []);

  const incidentContextCurrent = (context) => Boolean(context && incidentContextRef.current === context
    && isAuthSessionCurrent(context.session) && (recoveryInstanceRef.current || null) === context.instanceId);

  const loadIncidents = async (targetInstanceId = instanceId, context = null) => {
    if (!targetInstanceId) {
      setItems([]);
      setTotal(0);
      return false;
    }
    setLoading(true);
    try {
      const data = await loadDiagnosisCenterIncidents(targetInstanceId, INCIDENT_PAGE_SIZE, 1);
      if (context ? !incidentContextCurrent(context) : recoveryInstanceRef.current !== targetInstanceId) return false;
      setItems(data.items || []);
      setTotal(data.total || 0);
      return true;
    } catch (e) {
      if (!context || incidentContextCurrent(context)) message.error(e.message || "加载诊断事件失败");
      return false;
    } finally {
      if (!context || incidentContextCurrent(context)) setLoading(false);
    }
  };

  // 后端 list_incidents 的 page 默认 1（diagnosis_center.py:356），不显式传就永远只有第一页。
  const hasMoreIncidents = items.length < total;
  const loadMoreIncidents = async () => {
    if (!instanceId || loadingMore) return;
    const nextPage = Math.floor(items.length / INCIDENT_PAGE_SIZE) + 1;
    setLoadingMore(true);
    try {
      const data = await loadDiagnosisCenterIncidents(instanceId, INCIDENT_PAGE_SIZE, nextPage);
      if (recoveryInstanceRef.current !== instanceId) return;
      setItems((prev) => [...prev, ...(data.items || [])]);
      setTotal(data.total || total);
    } catch (e) {
      message.error(e.message || "加载更多诊断事件失败");
    } finally {
      setLoadingMore(false);
    }
  };

  const loadContext = async () => {
    try {
      const [methodologyResp, sourceResp] = await loadDiagnosisCenterContext();
      setMethodologies(methodologyResp.items || []);
      setDataSources(sourceResp.items || []);
    } catch (e) {
      message.warning(e.message || "诊断知识库加载失败");
    }
  };

  const loadInstanceContext = async (id) => {
    if (!id) {
      setArchProfile(null);
      setAutoPolicy(null);
      return;
    }
    try {
      const [arch, policy] = await loadDiagnosisCenterInstanceContext(id);
      // 快速切换实例时旧响应会后到，无条件写入会让当前实例显示成另一个实例的架构类型
      // （工作台标题直接渲染 archProfile?.arch_type），这里沿用 loadIncidents 的守卫口径。
      if (recoveryInstanceRef.current !== id) return;
      setArchProfile(arch);
      setAutoPolicy(policy);
    } catch (e) {
      if (recoveryInstanceRef.current !== id) return;
      setArchProfile(null);
      setAutoPolicy(null);
      message.warning(e.message || "实例架构画像 / 自动策略加载失败");
    }
  };

  const openDetail = async (row, suppliedContext = null) => {
    const context = suppliedContext || { session: getAuthSession(), instanceId: instanceId || null };
    if (!suppliedContext) incidentContextRef.current = context;
    try {
      const data = await getDiagnosisCenterIncident(row.id);
      if (!incidentContextCurrent(context)) return false;
      setDetail(data);
      setWorkspaceStarted(true);
      setActiveCards(["summary", "timeline", "rootCause"]);
      setMinimizedCards([]);
      return true;
    } catch (e) {
      if (incidentContextCurrent(context)) message.error(e.message || "加载诊断详情失败");
      return false;
    }
  };

  const openPlanIncident = async ({ incidentId, instanceId: targetInstanceId }) => {
    const target = dbInstances.find((item) => item.id === targetInstanceId);
    if (!Number.isSafeInteger(incidentId) || incidentId <= 0 || !target) {
      message.error("诊断事件或目标实例不在当前账号可见范围内");
      return;
    }
    const context = { session: getAuthSession(), instanceId: targetInstanceId };
    incidentContextRef.current = context;
    recoveryInstanceRef.current = targetInstanceId;
    if (targetInstanceId !== instanceId) setInstanceId(targetInstanceId);
    const [opened] = await Promise.all([
      openDetail({ id: incidentId }, context),
      loadIncidents(targetInstanceId, context),
    ]);
    if (!opened || !incidentContextCurrent(context)) return;
    setPlanWorkspaceOpen(false);
  };

  const navigateFromIntent = (intent) => {
    const targetKey = normalizeMenuKey(intent?.target_key || intent?.targetKey);
    if (!intent || intent.action !== "navigate" || !targetKey || targetKey === "diagnose.smart") {
      return false;
    }
    if (!onNavigate) return false;
    const targetDbType = String(intent.target_db_type || intent.targetDbType || "").toLowerCase();
    const nextInstanceId = targetDbType && selectedInstance?.db_type !== targetDbType ? null : instanceId;
    const targetName = intent.target_name || "对应功能";
    const confidence = intent.source === "ai" && intent.confidence ? ` · 置信度 ${Math.round(intent.confidence * 100)}%` : "";
    message.info(`${intent.source === "ai" ? "AI 判断" : "快速匹配"}：打开${targetName}${confidence}${intent.manual ? "，请手动操作" : ""}`);
    onNavigate(targetKey, nextInstanceId, intent.manual ? { kind: "manual_requirement",
      text: intent.requirement_text || problemText, targetKey, title: targetName,
      reason: intent.reason || "请在功能页确认参数后操作。", requestId: intent.request_id || null } : null);
    setWorkspaceStarted(false);
    return true;
  };

  const runDiagnosis = async ({ answers = {}, targetInstanceId = instanceId, recoverOnly = false,
    requestId: providedRequestId = null, text: providedText = null } = {}) => {
    const text = String(providedText ?? problemText).trim();
    if (!recoverOnly && !text) {
      message.warning("请先描述需求");
      return;
    }
    const target = targetInstanceId || null;
    const session = getAuthSession();
    if (assistantDispatchBusyRef.current) {
      message.warning("当前请求仍在处理中，请稍候或回查原请求");
      return;
    }
    if (!recoverOnly && pendingDispatch && (pendingDispatch.text == null || pendingDispatch.text === text)
      && pendingDispatch.instanceId === target
      && currentAssistantDispatchJournal({ requestId: pendingDispatch.requestId, instanceId: target, session })) {
      message.warning("原请求状态未确认，请先回查原请求");
      return;
    }

    const requestId = recoverOnly ? (providedRequestId || pendingDispatch?.requestId)
      : (globalThis.crypto?.randomUUID?.() || `req-${Date.now()}-${Math.random().toString(16).slice(2)}`);
    if (!requestId) {
      message.error("没有可回查的原请求编号");
      return;
    }
    if (recoverOnly) {
      if (!currentAssistantDispatchJournal({ requestId, instanceId: target, session })) {
        message.error("原请求编号已失效，请重新发起需求");
        return;
      }
    } else if (!writeAssistantDispatchJournal({ requestId, instanceId: target, session, text })) {
      message.error("无法保存请求编号，请检查浏览器存储后重试");
      return;
    }

    const epoch = ++assistantRequestEpoch.current;
    assistantDispatchBusyRef.current = requestId;
    setRunning(true);
    setAiDegradationNotice(null);
    const current = () => epoch === assistantRequestEpoch.current
      && isAuthSessionCurrent(session)
      && currentAssistantDispatchJournal({ requestId, instanceId: target, session });
    try {
      const targetInstance = dbInstances.find((item) => item.id === target) || null;
      const result = await resolveCustomerRequirement({
        text,
        instanceId: target,
        requestId,
        answers,
        recoverOnly,
        dispatch: dispatchAssistantIntent,
        readReceipt: getDispatchReceipt,
        resolveNavigation: (value) => resolveSmartNavigation(value, {
          selectedInstance: targetInstance,
          ...(Array.isArray(menuItems) ? { menuItems } : {}),
        }),
      });
      if (!current()) return;
      if (target !== (instanceId || null)) setInstanceId(target);
      if (result.mode === "pending") {
        setClarification(null);
        setPendingDispatch({ ...result, requestId, instanceId: target, text });
        setAiDegradationNotice({ tone: "warning", text: result.reason });
        return;
      }
      if (result.mode === "clarify") {
        setPendingDispatch(null);
        setClarification({ ...result, requestId, text, instanceId: target });
        return;
      }
      setPendingDispatch(null);
      setClarification(null);
      if (result.mode === "batch") {
        setPlanRequest({ batchId: result.batchId, instanceId: target, requirementText: text, requestId: result.requestId || requestId });
        setPlanWorkspaceOpen(true);
        if (result.reason) message.info(result.reason);
        return;
      }
      if (result.mode === "plan" || result.mode === "input") {
        setPlanRequest({ capability: result.capability, instanceId: target, requirementText: text,
          planId: result.planId, params: result.params || {}, requestId: result.requestId || requestId });
        setPlanWorkspaceOpen(true);
        if (result.reason) message.info(result.reason);
        return;
      }
      clearAssistantDispatchJournal(requestId);
      setAiDegradationNotice({ tone: "warning", text: result.reason });
      const visibleMenu = (items) => items?.some((item) => normalizeMenuKey(item.key) === "asset.center"
        || visibleMenu(item.children));
      if (isInstanceProvisioningCommand(text) && onNavigate
          && (!Array.isArray(menuItems) || visibleMenu(menuItems))) {
        try {
          window.sessionStorage.setItem("mesdb:open-instance-drawer", "1");
        } catch {
          void 0;
        }
        message.info(`${result.reason} 已打开新增实例表单。`);
        onNavigate("asset.center", target, { kind: "manual_requirement", text,
          targetKey: "asset.center", title: "资产中心", reason: result.reason, requestId });
        setWorkspaceStarted(false);
        return;
      }
      const route = result.navigation;
      if (route && navigateFromIntent({
        action: "navigate", source: "local", target_key: route.targetKey,
        target_name: route.title, target_db_type: route.targetDbType,
        manual: true, requirement_text: text, reason: result.reason, request_id: requestId,
      })) return;
      message.info(`${result.reason} 请选择要使用的能力卡片。`);
      setPlanRequest({ requirementText: text, manualReason: result.reason });
      setPlanWorkspaceOpen(true);
    } catch (error) {
      if (!current()) return;
      clearAssistantDispatchJournal(requestId);
      setPendingDispatch(null);
      setClarification(null);
      message.error(error.message || "需求处理失败，请手动选择功能");
    } finally {
      if (assistantDispatchBusyRef.current === requestId) assistantDispatchBusyRef.current = null;
      if (epoch === assistantRequestEpoch.current && isAuthSessionCurrent(session)) setRunning(false);
    }
  };

  const createTicket = (incident) => {
    if (!incident?.id) return;
    setTicketSource({
      sourceType: "AI_DIAGNOSIS",
      sourceId: incident.id,
      sourceRefId: incident.id,
      instanceId: incident.instance_id || instanceId,
      instanceName: incident.instance_name || selectedInstance?.instance_name,
      dbType: incident.db_type || selectedInstance?.db_type,
      title: incident.title || problemText || "AI 智能诊断",
      description: [
        incident.symptom,
        incident.root_cause_summary,
        incident.ai_summary,
        ...(incident.root_causes || []).slice(0, 5).map((item, index) => {
          // 后端 rca_engine.to_dict 只返回 cause / suggestion（rca_engine.py:147-161），
          // 取 summary/reason/title 会全为 undefined，导致根因候选被 filter(Boolean) 丢空。
          const cause = item.cause || item.summary || item.reason || "";
          const suggestion = item.suggestion || "";
          if (!cause && !suggestion) return "";
          return `根因候选 ${index + 1}：${cause || "待确认"}${suggestion ? `｜建议：${suggestion}` : ""}`;
        }),
      ].filter(Boolean).join("\n"),
      priority: String(incident.severity || "").toLowerCase() === "critical" ? "P1" : "P2",
      severity: incident.severity || "warning",
    });
  };

  const notifyIncident = async (incident) => {
    if (!incident?.id) return;
    try {
      await notifyDiagnosisCenterIncident(incident.id);
      message.success("已发送售后通知");
    } catch (e) {
      message.error(e.message || "通知失败");
    }
  };

  const openCard = (key) => {
    setActiveCards((prev) => (prev.includes(key) ? prev : [...prev, key]));
    setMinimizedCards((prev) => prev.filter((item) => item !== key));
  };

  const closeCard = (key) => {
    setActiveCards((prev) => prev.filter((item) => item !== key));
    setMinimizedCards((prev) => prev.filter((item) => item !== key));
  };

  const toggleMinimize = (key) => {
    setMinimizedCards((prev) => (prev.includes(key) ? prev.filter((item) => item !== key) : [...prev, key]));
  };

  const togglePin = (key) => {
    setPinnedCards((prev) => (prev.includes(key) ? prev.filter((item) => item !== key) : [...prev, key]));
  };

  useEffect(() => {
    loadContext();
  }, []);

  useEffect(() => {
    loadIncidents();
    loadInstanceContext(instanceId);
  }, [instanceId]);

  const evidenceRows = detail?.evidences || [];
  const rootCauseRows = detail?.root_causes || [];
  const timelineRows = detail?.timeline || [];
  const faultNodes = buildFaultChain({ detail, selectedInstance, evidenceRows, timelineRows });
  const executionView = diagnosisExecutionPresentation(detail);
  const recommendations = buildRecommendations({
    evidenceRows,
    rootCauseRows,
    timelineRows,
    dataSources,
    activeCards,
  });

  if (!workspaceStarted || !detail) {
    return (
      <>
      {pendingDispatch ? (
        <Alert
          type="warning"
          showIcon
          style={{ margin: "8px 16px 0" }}
          message="请求状态尚未确认"
          description={<Space wrap>
            <span>{pendingDispatch.reason || "原请求仍在处理中，请使用原编号回查。"}</span>
            <Text code>{pendingDispatch.requestId}</Text>
            {pendingDispatch.planId ? <Text type="secondary">任务 {pendingDispatch.planId}</Text> : null}
            <Button size="small" loading={running} onClick={() => runDiagnosis({
              recoverOnly: true,
              requestId: pendingDispatch.requestId,
              targetInstanceId: pendingDispatch.instanceId,
              text: pendingDispatch.text || problemText,
            })}>回查原请求</Button>
          </Space>}
        />
      ) : null}
      {aiDegradationNotice && !pendingDispatch ? (
        <Alert
          type={aiDegradationNotice.tone}
          showIcon
          closable
          onClose={() => setAiDegradationNotice(null)}
          style={{ margin: "8px 16px 0" }}
          message={aiDegradationNotice.text}
          description={aiDegradationNotice.hint || undefined}
        />
      ) : null}
      <LandingPage
        entryMode={entryMode}
        setEntryMode={setEntryMode}
        dbInstances={dbInstances}
        menuItems={menuItems}
        instanceId={instanceId}
        setInstanceId={setInstanceId}
        onNavigate={onNavigate}
        problemText={problemText}
        setProblemText={setProblemText}
        onOpenRuleDiagnosis={onOpenRuleDiagnosis}
        inspectionBatch={inspectionBatch}
        inspectionModalOpen={inspectionModalOpen}
        onInspectionModalOpenChange={setInspectionModalOpen}
        onOpenInspectionTasks={() => {
          setInspectionModalOpen(false);
          onNavigate?.("inspectionTasks", instanceId);
        }}
        assistantActionTask={assistantActionTask}
        assistantActionModalOpen={assistantActionModalOpen}
        onAssistantActionModalOpenChange={setAssistantActionModalOpen}
        onOpenAssistantActionTarget={() => {
          setAssistantActionModalOpen(false);
          onNavigate?.(assistantActionTask?.targetKey, assistantActionTask?.instanceId || instanceId, assistantActionTask?.executionResult);
        }}
        onOpenPlans={() => { assistantRequestEpoch.current += 1; assistantDispatchBusyRef.current = null;
          setRunning(false); setPlanRequest(entryMode === "customer" ? { initialTab: "history" } : null); setPlanWorkspaceOpen(true); }}
        running={running}
        items={items}
        total={total}
        loading={loading}
        loadingMore={loadingMore}
        hasMoreIncidents={hasMoreIncidents}
        onRun={() => runDiagnosis()}
        onOpenIncident={openDetail}
        onReload={loadIncidents}
        onLoadMore={loadMoreIncidents}
      />
      <AssistantPlanWorkspace open={planWorkspaceOpen} onClose={() => { assistantRequestEpoch.current += 1;
        assistantDispatchBusyRef.current = null; setRunning(false); setPlanWorkspaceOpen(false); }}
        customerMode={entryMode === "customer"}
        instances={dbInstances} instanceId={instanceId} request={planRequest} onIncidentReady={openPlanIncident} />
      <RequirementClarificationModal
        open={Boolean(clarification)}
        questions={clarification?.questions || []}
        initialAnswers={clarification?.answers || {}}
        goal={clarification?.goal}
        reason={clarification?.reason}
        requirementText={clarification?.text || problemText}
        instances={dbInstances}
        selectedInstanceId={clarification?.instanceId || instanceId}
        loading={running}
        onCancel={() => {
          if (clarification?.requestId) clearAssistantDispatchJournal(clarification.requestId);
          setClarification(null);
        }}
        onSubmit={({ answers, instanceId: clarifiedInstanceId }) => runDiagnosis({
          answers,
          targetInstanceId: clarifiedInstanceId,
          text: clarification?.text || problemText,
        })}
      />
      <ReportBatchModal open={reportModalOpen} onClose={() => setReportModalOpen(false)} batch={reportBatch} />
      </>
    );
  }

  return (
    <>
    {aiDegradationNotice ? (
      <Alert
        type={aiDegradationNotice.tone}
        showIcon
        closable
        onClose={() => setAiDegradationNotice(null)}
        style={{ margin: "8px 16px 0" }}
        message={aiDegradationNotice.text}
        description={aiDegradationNotice.hint || undefined}
      />
    ) : null}
    <div className="diagnosis-cockpit-shell diagnosis-workspace-screen">
      <header className="diagnosis-context-bar">
        <div className="diagnosis-context-main">
          <Tag color={severityColor(detail.severity)}>{severityLabel(detail.severity)}</Tag>
          <div>
            <strong>{compactText(detail.title || problemText, "现场问题智能诊断")}</strong>
            <span>
              {compactText(detail.incident_no, "新案例")} · {compactText(detail.instance_name || selectedInstance?.instance_name, "未知实例")} · {compactText(archProfile?.arch_type || detail.arch_type || detail.arch_profile?.arch_type, "架构待识别")}
            </span>
          </div>
        </div>
        <Space wrap className="diagnosis-context-actions">
          <Tag color={executionView.color}>{executionView.label}</Tag>
          <Tag color="blue">评分 {detail.score ?? "-"}</Tag>
          <Button onClick={() => setWorkspaceStarted(false)}>重新描述问题</Button>
          <Button icon={<ReloadOutlined />} loading={loading} onClick={loadIncidents}>刷新</Button>
          <Button disabled={!detail?.id} onClick={() => createTicket(detail)}>生成工单</Button>
          <Button disabled={!detail?.id} type="primary" onClick={() => notifyIncident(detail)}>通知售后</Button>
        </Space>
      </header>

      <div className="diagnosis-workspace-grid">
        <main className="diagnosis-workspace-main">
          <FaultChain nodes={faultNodes} />
          <CardWorkspace
            activeCards={activeCards}
            minimizedCards={minimizedCards}
            pinnedCards={pinnedCards}
            detail={detail}
            evidenceRows={evidenceRows}
            rootCauseRows={rootCauseRows}
            timelineRows={timelineRows}
            methodologies={methodologies}
            dataSources={dataSources}
            autoPolicy={autoPolicy}
            onOpenCard={openCard}
            onCloseCard={closeCard}
            onToggleMinimize={toggleMinimize}
            onTogglePin={togglePin}
          />
        </main>
        <RecommendationRail recommendations={recommendations} onOpenCard={openCard} />
      </div>

      <DiagnosisDock
        activeCards={activeCards}
        minimizedCards={minimizedCards}
        recommendations={recommendations}
        onOpenCard={openCard}
      />
    </div>
    <ItsmTicketDrawer
      open={Boolean(ticketSource)}
      source={ticketSource}
      onClose={() => setTicketSource(null)}
      onCreated={loadIncidents}
    />
    <ReportBatchModal open={reportModalOpen} onClose={() => setReportModalOpen(false)} batch={reportBatch} />
    </>
  );
}
