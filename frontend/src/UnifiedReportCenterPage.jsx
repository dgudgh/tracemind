import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ReportEvidencePanel } from "./ReportEvidencePanel";
import { ReportDocumentLauncher } from "./ReportDocumentLauncher";
import { reportEvidence } from "./reportEvidence.mjs";
import {
  Alert,
  Button,
  Checkbox,
  Descriptions,
  Dropdown,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Space,
  Switch,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  message,
} from "antd";
import {
  CheckCircleOutlined,
  CloudDownloadOutlined,
  DeleteOutlined,
  EditOutlined,
  FileProtectOutlined,
  FileSearchOutlined,
  InboxOutlined,
  MoreOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
  SyncOutlined,
} from "@ant-design/icons";
import {
  archiveReportArtifact,
  createReportSchedule,
  createReportSnapshot,
  createReportSubscription,
  deleteReportSchedule,
  deleteReportSubscription,
  downloadReportArtifact,
  getReportArtifact,
  getReportCatalog,
  getReportCatalogStats,
  getReportScheduleRuns,
  getReportSchedules,
  getReportSubscriptions,
  runReportSchedule,
  setReportArtifactLegalHold,
  syncReportCatalog,
  updateReportSchedule,
  updateReportSubscription,
  verifyReportArtifact,
} from "./modules/reportApi";

const { Text, Title } = Typography;

const REPORT_TYPE_LABELS = {
  inspection: "数据库巡检",
  awr: "AWR",
  performance: "性能基线",
  slow_sql: "慢 SQL",
  alert: "告警事件",
  ticket: "运维工单",
  backup: "备份恢复",
  governance: "数据治理",
  ai: "AI 分析",
  rescue: "数据抢救",
  all: "全部报告",
};

const REPORT_TYPE_OPTIONS = Object.entries(REPORT_TYPE_LABELS).map(([value, label]) => ({ value, label }));
const FORMAT_OPTIONS = ["markdown", "html", "json", "word"].map((value) => ({ value, label: value === "word" ? "Word" : value.toUpperCase() }));
const WEEKDAY_OPTIONS = [
  { label: "周一", value: 1 }, { label: "周二", value: 2 }, { label: "周三", value: 3 },
  { label: "周四", value: 4 }, { label: "周五", value: 5 }, { label: "周六", value: 6 }, { label: "周日", value: 7 },
];
const SCHEDULE_TYPE_OPTIONS = [
  { value: "manual", label: "仅手动" },
  { value: "interval", label: "固定间隔" },
  { value: "cron", label: "Cron" },
  { value: "daily", label: "每天" },
  { value: "workday", label: "工作日" },
  { value: "weekly", label: "每周" },
  { value: "monthly", label: "每月" },
  { value: "quarterly", label: "每季度" },
];

const riskColor = (value) => ({ critical: "red", high: "red", warning: "orange", info: "blue", normal: "green", low: "blue" }[String(value || "").toLowerCase()] || "default");
const statusColor = (value) => ({ success: "green", generated: "green", captured: "blue", active: "green", archived: "default", failed: "red", partial: "orange", skipped: "default", running: "blue" }[String(value || "").toLowerCase()] || "default");
const formatDate = (value) => value ? String(value).replace("T", " ").slice(0, 19) : "-";
const csvValues = (value) => String(value || "").split(/[,，\n]/).map((item) => item.trim()).filter(Boolean);
const csvNumbers = (value) => csvValues(value).map(Number).filter((item) => Number.isInteger(item) && item > 0);
const safeFileName = (value) => String(value || "report").replace(/[^A-Za-z0-9._-]+/g, "_").slice(0, 80);

function Metric({ label, value, accent }) {
  return (
    <div className="report-center-metric">
      <Text type="secondary">{label}</Text>
      <span className={accent ? `report-center-metric-value ${accent}` : "report-center-metric-value"}>{value ?? 0}</span>
    </div>
  );
}

export function UnifiedReportCenterPage({ instances = [], selectedInstanceId }) {
  const [activeTab, setActiveTab] = useState("catalog");
  const [catalog, setCatalog] = useState([]);
  const [stats, setStats] = useState(null);
  const [catalogError, setCatalogError] = useState("");
  const [schedulesLoaded, setSchedulesLoaded] = useState(false);
  const [schedules, setSchedules] = useState([]);
  const [runs, setRuns] = useState([]);
  const [subscriptions, setSubscriptions] = useState([]);
  const [loading, setLoading] = useState(false);
  const [runsLoading, setRunsLoading] = useState(false);
  const [schedulesLoading, setSchedulesLoading] = useState(false);
  const mounted = useRef(false);
  const catalogRequest = useRef(0);
  const schedulesRequest = useRef(0);
  const runsRequest = useRef(0);
  const [filters, setFilters] = useState({ reportType: "all", archiveState: "all", keyword: "" });
  const [detail, setDetail] = useState(null);
  const detailEvidence = useMemo(() => reportEvidence(detail), [detail]);
  const [detailLoading, setDetailLoading] = useState(false);
  const detailRequest = useRef(0);
  const [scheduleOpen, setScheduleOpen] = useState(false);
  const [editingSchedule, setEditingSchedule] = useState(null);
  const [snapshotOpen, setSnapshotOpen] = useState(false);
  const [subscriptionOpen, setSubscriptionOpen] = useState(false);
  const [editingSubscription, setEditingSubscription] = useState(null);
  const [submitting, setSubmitting] = useState({ snapshot: false, schedule: false, subscription: false });
  const formSessions = useRef({
    snapshot: { generation: 0, open: false, pending: null },
    schedule: { generation: 0, open: false, pending: null },
    subscription: { generation: 0, open: false, pending: null },
  });
  const formPageGeneration = useRef(0);
  const latestLoads = useRef(null);
  const [scheduleForm] = Form.useForm();
  const [snapshotForm] = Form.useForm();
  const [subscriptionForm] = Form.useForm();
  const watchedScheduleType = Form.useWatch("schedule_type", scheduleForm);

  const loadCatalog = async (refreshSources = false, syncSources = false) => {
    if (!mounted.current) return;
    const token = ++catalogRequest.current;
    const current = () => mounted.current && token === catalogRequest.current;
    setLoading(true);
    setCatalogError("");
    setCatalog([]);
    setStats(null);
    try {
      if (syncSources) {
        const result = await syncReportCatalog();
        if (!current()) return;
        message.success(`已同步 ${result.synced} 份来源报告`);
      }
      // Source synchronization must commit before the totals are read.
      const catalogResult = await getReportCatalog({ ...filters, refreshSources, limit: 500 });
      if (!current()) return;
      const statResult = await getReportCatalogStats();
      if (!current()) return;
      setCatalog(catalogResult.items || []);
      setStats(statResult || null);
    } catch (error) {
      if (!current()) return;
      setCatalogError(error.message || "报告目录请求失败");
      setStats(null);
      message.error(error.message);
    } finally {
      if (current()) setLoading(false);
    }
  };

  const loadSchedules = async () => {
    if (!mounted.current) return;
    const token = ++schedulesRequest.current;
    const current = () => mounted.current && token === schedulesRequest.current;
    setSchedulesLoading(true);
    try {
      const [scheduleResult, subscriptionResult] = await Promise.all([
        getReportSchedules(),
        getReportSubscriptions(),
      ]);
      if (!current()) return;
      setSchedules(scheduleResult.items || []);
      setSubscriptions(subscriptionResult.items || []);
      setSchedulesLoaded(true);
    } catch (error) {
      if (current()) message.error(error.message);
    } finally {
      if (current()) setSchedulesLoading(false);
    }
  };

  const loadRuns = async (scheduleRows = schedules) => {
    if (!mounted.current) return;
    const token = ++runsRequest.current;
    const current = () => mounted.current && token === runsRequest.current;
    setRunsLoading(true);
    try {
      const sourceSchedules = scheduleRows.length ? scheduleRows : (await getReportSchedules()).items || [];
      if (!current()) return;
      const results = await Promise.all(sourceSchedules.map(async (schedule) => {
        const response = await getReportScheduleRuns(schedule.id, 100);
        return (response.items || []).map((item) => ({ ...item, schedule_name: schedule.name }));
      }));
      if (!current()) return;
      setRuns(results.flat().sort((a, b) => String(b.planned_at || "").localeCompare(String(a.planned_at || ""))));
    } catch (error) {
      if (current()) message.error(error.message);
    } finally {
      if (current()) setRunsLoading(false);
    }
  };

  useLayoutEffect(() => {
    latestLoads.current = { loadCatalog, loadSchedules };
  });

  useEffect(() => {
    mounted.current = true;
    loadCatalog(false);
    loadSchedules();
    return () => {
      mounted.current = false;
      formPageGeneration.current += 1;
      catalogRequest.current += 1;
      schedulesRequest.current += 1;
      runsRequest.current += 1;
      detailRequest.current += 1;
    };
  }, []);
  useEffect(() => {
    if (activeTab === "schedules" || activeTab === "subscriptions") loadSchedules();
    if (activeTab === "runs") loadRuns();
  }, [activeTab]);

  const refreshCurrent = async () => {
    if (activeTab === "catalog") await loadCatalog(false);
    if (activeTab === "schedules" || activeTab === "subscriptions") await loadSchedules();
    if (activeTab === "runs") await loadRuns();
  };

  const openDetail = async (row) => {
    const token = ++detailRequest.current;
    setDetail(null);
    setDetailLoading(true);
    try {
      const response = await getReportArtifact(row.id);
      if (token === detailRequest.current) setDetail(response);
    } catch (error) {
      if (token === detailRequest.current) message.error(error.message);
    } finally {
      if (token === detailRequest.current) setDetailLoading(false);
    }
  };

  const closeDetail = () => {
    detailRequest.current += 1;
    setDetail(null);
    setDetailLoading(false);
  };

  const verifyArtifact = async (row) => {
    try {
      const result = await verifyReportArtifact(row.id);
      if (result.valid) message.success("签名、内容摘要和全部格式校验通过");
      else message.error(`完整性校验失败：${result.error || "内容已变化"}`);
      return result;
    } catch (error) {
      message.error(error.message);
      return null;
    }
  };

  const downloadArtifact = async (row, format) => {
    try {
      const suffix = format === "word" ? "docx" : format === "markdown" ? "md" : format;
      await downloadReportArtifact(row.id, format, `${safeFileName(row.title)}.${suffix}`);
    } catch (error) {
      message.error(error.message);
    }
  };

  const changeFormSession = (kind, open) => {
    const session = formSessions.current[kind];
    session.generation += 1;
    session.open = open;
    session.pending = null;
    setSubmitting((value) => ({ ...value, [kind]: false }));
  };

  const startFormSubmission = (kind) => {
    const session = formSessions.current[kind];
    if (!mounted.current || !session.open || session.pending) return null;
    const ticket = { generation: session.generation, pageGeneration: formPageGeneration.current };
    // Lock synchronously, before validation yields or React updates the button.
    session.pending = ticket;
    setSubmitting((value) => ({ ...value, [kind]: true }));
    return ticket;
  };

  const isFormSubmissionCurrent = (kind, ticket) => {
    const session = formSessions.current[kind];
    return mounted.current && ticket.pageGeneration === formPageGeneration.current
      && session.open && session.generation === ticket.generation && session.pending === ticket;
  };

  const finishFormSubmission = (kind, ticket) => {
    if (!isFormSubmissionCurrent(kind, ticket)) return;
    formSessions.current[kind].pending = null;
    setSubmitting((value) => ({ ...value, [kind]: false }));
  };

  const closeSnapshot = () => { changeFormSession("snapshot", false); setSnapshotOpen(false); };
  const closeSchedule = () => { changeFormSession("schedule", false); setScheduleOpen(false); };
  const closeSubscription = () => { changeFormSession("subscription", false); setSubscriptionOpen(false); };

  const openSnapshot = () => {
    changeFormSession("snapshot", true);
    snapshotForm.resetFields();
    snapshotForm.setFieldsValue({ title: "统一运维报告快照", report_type: "all", retention_days: 365, limit: 500 });
    setSnapshotOpen(true);
  };

  const submitSnapshot = async () => {
    const ticket = startFormSubmission("snapshot");
    if (!ticket) return;
    try {
      const values = await snapshotForm.validateFields();
      if (!isFormSubmissionCurrent("snapshot", ticket)) return;
      const created = await createReportSnapshot({
        ...values,
        instance_ids: csvNumbers(values.instance_ids),
      });
      if (isFormSubmissionCurrent("snapshot", ticket)) {
        message.success(`报告快照已生成：${created.artifact_uid}`);
        snapshotForm.resetFields();
        closeSnapshot();
      }
      if (mounted.current && ticket.pageGeneration === formPageGeneration.current) await latestLoads.current.loadCatalog(false);
    } catch (error) {
      if (!isFormSubmissionCurrent("snapshot", ticket) || error?.errorFields) return;
      message.error(error.message);
    } finally {
      finishFormSubmission("snapshot", ticket);
    }
  };

  const openSchedule = (row = null) => {
    changeFormSession("schedule", true);
    setEditingSchedule(row);
    scheduleForm.resetFields();
    scheduleForm.setFieldsValue(row ? {
      ...row,
      enabled: Boolean(row.enabled),
      filter_limit: row.filter?.limit || 500,
      instance_ids: (row.filter?.instance_ids || []).join(","),
    } : {
      name: "运维报告日报",
      report_type: "all",
      output_formats: ["markdown", "html", "json", "word"],
      schedule_type: "workday",
      run_time: "08:00",
      weekdays: [1, 2, 3, 4, 5],
      timezone: "Asia/Shanghai",
      misfire_policy: "fire_once",
      misfire_grace_seconds: 3600,
      max_catchup_runs: 1,
      retention_days: 365,
      filter_limit: 500,
      enabled: true,
    });
    setScheduleOpen(true);
  };

  const submitSchedule = async () => {
    const ticket = startFormSubmission("schedule");
    if (!ticket) return;
    const scheduleId = editingSchedule?.id;
    try {
      const values = await scheduleForm.validateFields();
      if (!isFormSubmissionCurrent("schedule", ticket)) return;
      const payload = {
        ...values,
        enabled: values.enabled ? 1 : 0,
        filter_json: {
          limit: values.filter_limit || 500,
          instance_ids: csvNumbers(values.instance_ids),
        },
      };
      delete payload.filter_limit;
      delete payload.instance_ids;
      if (scheduleId != null) await updateReportSchedule(scheduleId, payload);
      else await createReportSchedule(payload);
      if (isFormSubmissionCurrent("schedule", ticket)) {
        message.success(scheduleId != null ? "调度策略已更新" : "调度策略已创建");
        scheduleForm.resetFields();
        closeSchedule();
      }
      if (mounted.current && ticket.pageGeneration === formPageGeneration.current) await latestLoads.current.loadSchedules();
    } catch (error) {
      if (!isFormSubmissionCurrent("schedule", ticket) || error?.errorFields) return;
      message.error(error.message);
    } finally {
      finishFormSubmission("schedule", ticket);
    }
  };

  const executeSchedule = async (row) => {
    try {
      const result = await runReportSchedule(row.id);
      result.status === "success" ? message.success("报告已生成并完成签名归档") : message.error(result.message || "报告生成失败");
      await loadSchedules();
    } catch (error) {
      message.error(error.message);
    }
  };

  const openSubscription = (row = null) => {
    changeFormSession("subscription", true);
    setEditingSubscription(row);
    subscriptionForm.resetFields();
    subscriptionForm.setFieldsValue(row ? {
      ...row,
      enabled: Boolean(row.enabled),
      channels: (row.channels || []).join(","),
      recipients: (row.recipients || []).join(","),
    } : {
      name: "报告生成通知",
      report_types: ["all"],
      enabled: true,
    });
    setSubscriptionOpen(true);
  };

  const submitSubscription = async () => {
    const ticket = startFormSubmission("subscription");
    if (!ticket) return;
    const subscriptionId = editingSubscription?.id;
    try {
      const values = await subscriptionForm.validateFields();
      if (!isFormSubmissionCurrent("subscription", ticket)) return;
      const payload = {
        ...values,
        channels: csvValues(values.channels),
        recipients: csvValues(values.recipients),
        enabled: values.enabled ? 1 : 0,
        filter_json: {},
      };
      if (subscriptionId != null) await updateReportSubscription(subscriptionId, payload);
      else await createReportSubscription(payload);
      if (isFormSubmissionCurrent("subscription", ticket)) {
        message.success(subscriptionId != null ? "订阅已更新" : "订阅已创建");
        subscriptionForm.resetFields();
        closeSubscription();
      }
      if (mounted.current && ticket.pageGeneration === formPageGeneration.current) await latestLoads.current.loadSchedules();
    } catch (error) {
      if (!isFormSubmissionCurrent("subscription", ticket) || error?.errorFields) return;
      message.error(error.message);
    } finally {
      finishFormSubmission("subscription", ticket);
    }
  };

  const scheduleNameById = useMemo(() => Object.fromEntries(schedules.map((row) => [row.id, row.name])), [schedules]);

  const catalogColumns = [
    {
      title: "报告",
      dataIndex: "title",
      width: 340,
      render: (value, row) => <button type="button" className="report-center-title-button" onClick={() => openDetail(row)}><strong>{value}</strong><span>{row.artifact_uid}</span></button>,
    },
    { title: "类型", dataIndex: "report_type", width: 120, render: (value) => <Tag>{REPORT_TYPE_LABELS[value] || value}</Tag> },
    { title: "实例", dataIndex: "instance_name", width: 150, ellipsis: true, render: (value) => value || "平台级" },
    { title: "状态", dataIndex: "status", width: 100, render: (value) => <Tag color={statusColor(value)}>{value || "-"}</Tag> },
    { title: "风险", dataIndex: "risk_level", width: 90, render: (value) => value ? <Tag color={riskColor(value)}>{value}</Tag> : "-" },
    { title: "完整性", width: 110, render: (_, row) => row.signature_algorithm ? <Tag icon={<SafetyCertificateOutlined />} color="green">已签名</Tag> : <Tag>未签名</Tag> },
    { title: "归档", dataIndex: "archive_state", width: 100, render: (value, row) => <Space size={4}><Tag color={value === "active" ? "blue" : "default"}>{value === "active" ? "在线" : "已归档"}</Tag>{row.legal_hold ? <Tooltip title="司法保全"><FileProtectOutlined /></Tooltip> : null}</Space> },
    { title: "时间", dataIndex: "create_time", width: 170, render: formatDate },
    {
      title: "操作", width: 170, fixed: "right",
      render: (_, row) => <Space size={4}>
        <Button type="link" size="small" icon={<FileSearchOutlined />} onClick={() => openDetail(row)}>详情</Button>
        <Dropdown trigger={["click"]} menu={{ items: [
          ...(row.formats || []).map((format) => ({ key: `download:${format}`, label: `下载 ${format === "word" ? "Word" : format.toUpperCase()}`, icon: <CloudDownloadOutlined /> })),
          { type: "divider" },
          { key: "verify", label: "校验完整性", icon: <CheckCircleOutlined /> },
          { key: "hold", label: row.legal_hold ? "解除保全" : "启用保全", icon: <FileProtectOutlined /> },
          { key: "archive", label: "归档", icon: <InboxOutlined />, disabled: row.archive_state === "archived" },
        ], onClick: async ({ key }) => {
          if (key.startsWith("download:")) await downloadArtifact(row, key.split(":")[1]);
          if (key === "verify") await verifyArtifact(row);
          if (key === "hold" || key === "archive") {
            const pageGeneration = formPageGeneration.current;
            const current = () => mounted.current && pageGeneration === formPageGeneration.current;
            if (!current()) return;
            try {
              if (key === "hold") {
                await setReportArtifactLegalHold(row.id, { enabled: !row.legal_hold, reason: "报告中心人工操作" });
              } else {
                await archiveReportArtifact(row.id, { reason: "报告中心人工归档" });
              }
              if (!current()) return;
              message.success(key === "archive" ? "报告已归档" : row.legal_hold ? "已解除报告保全" : "已启用报告保全");
              await latestLoads.current.loadCatalog(false);
            } catch (error) {
              if (current()) message.error(error.message || "报告操作失败");
            }
          }
        } }}><Button type="text" size="small" icon={<MoreOutlined />} aria-label="更多操作" /></Dropdown>
      </Space>,
    },
  ];

  const scheduleColumns = [
    { title: "策略名称", dataIndex: "name", width: 220, render: (value, row) => <Space direction="vertical" size={0}><Text strong>{value}</Text><Text type="secondary">{row.schedule_uid}</Text></Space> },
    { title: "报告范围", dataIndex: "report_type", width: 130, render: (value) => <Tag>{REPORT_TYPE_LABELS[value] || value}</Tag> },
    { title: "周期", width: 180, render: (_, row) => <Space direction="vertical" size={0}><Text>{SCHEDULE_TYPE_OPTIONS.find((item) => item.value === row.schedule_type)?.label || row.schedule_type}</Text><Text type="secondary">{row.schedule_type === "interval" ? `${row.interval_minutes} 分钟` : row.schedule_type === "cron" ? row.cron_expression : row.run_time}</Text></Space> },
    { title: "时区", dataIndex: "timezone", width: 130 },
    { title: "下次执行", dataIndex: "next_run_at", width: 180, render: formatDate },
    { title: "最近结果", width: 150, render: (_, row) => <Space direction="vertical" size={0}><Tag color={statusColor(row.last_status)}>{row.last_status || "尚未执行"}</Tag><Text type="secondary">{formatDate(row.last_run_at)}</Text></Space> },
    { title: "启用", dataIndex: "enabled", width: 80, render: (value) => value ? <Tag color="green">启用</Tag> : <Tag>停用</Tag> },
    { title: "操作", width: 190, fixed: "right", render: (_, row) => <Space size={2}>
      <Tooltip title="立即执行"><Button type="text" icon={<PlayCircleOutlined />} onClick={() => executeSchedule(row)} /></Tooltip>
      <Tooltip title="编辑"><Button type="text" icon={<EditOutlined />} onClick={() => openSchedule(row)} /></Tooltip>
      <Popconfirm title="确认删除该调度策略？" onConfirm={async () => { await deleteReportSchedule(row.id); message.success("调度策略已删除"); await loadSchedules(); }}><Tooltip title="删除"><Button type="text" danger icon={<DeleteOutlined />} /></Tooltip></Popconfirm>
    </Space> },
  ];

  const runColumns = [
    { title: "执行编号", dataIndex: "run_uid", width: 240, ellipsis: true },
    { title: "调度策略", dataIndex: "schedule_name", width: 190, ellipsis: true },
    { title: "触发方式", dataIndex: "trigger_type", width: 150 },
    { title: "计划时间", dataIndex: "planned_at", width: 180, render: formatDate },
    { title: "开始时间", dataIndex: "started_at", width: 180, render: formatDate },
    { title: "状态", dataIndex: "status", width: 100, render: (value) => <Tag color={statusColor(value)}>{value}</Tag> },
    { title: "执行节点", dataIndex: "worker_id", width: 190, ellipsis: true },
    { title: "结果", dataIndex: "message", ellipsis: true },
    { title: "报告制品", dataIndex: "artifact_id", width: 110, render: (value) => value ? `#${value}` : "-" },
  ];

  const subscriptionColumns = [
    { title: "订阅名称", dataIndex: "name", width: 230, render: (value) => <Text strong>{value}</Text> },
    { title: "关联策略", dataIndex: "schedule_id", width: 190, render: (value) => value ? scheduleNameById[value] || `#${value}` : "全部匹配策略" },
    { title: "报告范围", dataIndex: "report_types", width: 220, render: (values) => <Space size={[0, 4]} wrap>{(values || []).map((value) => <Tag key={value}>{REPORT_TYPE_LABELS[value] || value}</Tag>)}</Space> },
    { title: "通知通道", dataIndex: "channels", width: 180, render: (values) => (values || []).join("、") || "按通知策略路由" },
    { title: "接收人", dataIndex: "recipients", ellipsis: true, render: (values) => (values || []).join("、") || "-" },
    { title: "状态", dataIndex: "enabled", width: 90, render: (value) => value ? <Tag color="green">启用</Tag> : <Tag>停用</Tag> },
    { title: "操作", width: 120, fixed: "right", render: (_, row) => <Space size={2}>
      <Tooltip title="编辑"><Button type="text" icon={<EditOutlined />} onClick={() => openSubscription(row)} /></Tooltip>
      <Popconfirm title="确认删除该订阅？" onConfirm={async () => { await deleteReportSubscription(row.id); message.success("订阅已删除"); await loadSchedules(); }}><Tooltip title="删除"><Button type="text" danger icon={<DeleteOutlined />} /></Tooltip></Popconfirm>
    </Space> },
  ];

  const catalogView = <div className="report-center-panel">
    <div className="report-center-toolbar">
      <Space wrap>
        <Input.Search allowClear placeholder="搜索报告标题或摘要" style={{ width: 280 }} value={filters.keyword} onChange={(event) => setFilters((value) => ({ ...value, keyword: event.target.value }))} onSearch={() => loadCatalog(false)} />
        <Select value={filters.reportType} options={REPORT_TYPE_OPTIONS} style={{ width: 150 }} onChange={(value) => setFilters((current) => ({ ...current, reportType: value }))} />
        <Select value={filters.archiveState} style={{ width: 120 }} options={[{ value: "all", label: "全部状态" }, { value: "active", label: "在线" }, { value: "archived", label: "已归档" }]} onChange={(value) => setFilters((current) => ({ ...current, archiveState: value }))} />
        <Button icon={<FileSearchOutlined />} onClick={() => loadCatalog(false)}>查询</Button>
      </Space>
      <Space wrap>
        <Button icon={<SyncOutlined />} onClick={() => loadCatalog(false, true)}>同步来源</Button>
        <ReportDocumentLauncher instances={instances} instanceId={selectedInstanceId} />
        <Button type="primary" icon={<PlusOutlined />} onClick={openSnapshot}>生成快照</Button>
      </Space>
    </div>
    <Table rowKey="id" size="middle" loading={loading} dataSource={catalog} columns={catalogColumns} pagination={{ pageSize: 15, showSizeChanger: true }} scroll={{ x: 1420 }} />
  </div>;

  const tabs = [
    { key: "catalog", label: "报告目录", children: catalogView },
    { key: "schedules", label: "调度策略", children: <div className="report-center-panel"><div className="report-center-toolbar"><Text type="secondary">工作日、周期、季度与 Cron 调度均按策略时区计算，漏跑支持跳过、补一次或限量追补。</Text><Button type="primary" icon={<PlusOutlined />} onClick={() => openSchedule()}>新增策略</Button></div><Table rowKey="id" loading={schedulesLoading} dataSource={schedules} columns={scheduleColumns} pagination={{ pageSize: 12 }} scroll={{ x: 1250 }} /></div> },
    { key: "runs", label: "运行历史", children: <div className="report-center-panel"><Table rowKey="id" loading={runsLoading} dataSource={runs} columns={runColumns} pagination={{ pageSize: 15, showSizeChanger: true }} scroll={{ x: 1350 }} /></div> },
    { key: "subscriptions", label: "报告订阅", children: <div className="report-center-panel"><div className="report-center-toolbar"><Text type="secondary">报告生成后进入统一通知策略，保留订阅目标和派送错误证据。</Text><Button type="primary" icon={<PlusOutlined />} onClick={() => openSubscription()}>新增订阅</Button></div><Table rowKey="id" loading={schedulesLoading} dataSource={subscriptions} columns={subscriptionColumns} pagination={{ pageSize: 12 }} scroll={{ x: 1120 }} /></div> },
  ];

  return (
    <div className="unified-report-center-page">
      <section className="report-center-heading">
        <div className="report-center-heading-copy">
          <div className="report-center-icon"><FileProtectOutlined /></div>
          <div><Title level={4}>统一报告中心</Title><Text type="secondary">跨巡检、性能、告警、备份和治理的可信报告目录</Text></div>
        </div>
        <Button icon={<ReloadOutlined />} loading={activeTab === "catalog" ? loading : activeTab === "runs" ? runsLoading : schedulesLoading} onClick={refreshCurrent}>刷新</Button>
      </section>

      <section className="report-center-metrics" aria-label="报告统计" aria-busy={loading}>
        <Metric label="报告总数" value={stats?.total ?? "待加载"} />
        <Metric label="已签名" value={stats?.signed ?? "待加载"} accent="success" />
        <Metric label="在线" value={stats?.by_archive_state ? (stats.by_archive_state.active ?? 0) : "待加载"} />
        <Metric label="已归档" value={stats?.by_archive_state ? (stats.by_archive_state.archived ?? 0) : "待加载"} />
        <Metric label="调度策略" value={schedulesLoaded ? schedules.length : "待加载"} />
      </section>
      {catalogError && <Alert type="error" showIcon message="报告目录加载失败" description={catalogError} />}

      <Tabs activeKey={activeTab} onChange={setActiveTab} items={tabs} className="report-center-tabs" />

      <Modal key={detail?.id ?? "closed"} open={Boolean(detail) || detailLoading} onCancel={closeDetail} footer={null} width={960} centered title="报告制品详情" className="adaptive-center-modal" loading={detailLoading}>
        {detail ? <Space direction="vertical" size={16} className="full-width">
          <div className="report-detail-title"><div><Title level={4}>{detail.title}</Title><Text type="secondary">{detail.artifact_uid}</Text></div><Space wrap><Tag color={detail.verification?.valid ? "green" : "red"} icon={<SafetyCertificateOutlined />}>{detail.verification?.valid ? "完整性通过" : "完整性异常"}</Tag>{detail.legal_hold ? <Tag color="gold">保全中</Tag> : null}</Space></div>
          {!detail.verification?.valid ? <Alert type="error" showIcon message="该报告完整性校验失败" description={detail.verification?.error || "报告内容或签名与归档记录不一致，下载已被阻止。"} /> : null}
          <Descriptions size="small" bordered column={{ xs: 1, sm: 2, md: 3 }} items={[
            { key: "type", label: "报告类型", children: REPORT_TYPE_LABELS[detail.report_type] || detail.report_type },
            { key: "instance", label: "实例", children: detail.instance_name || "平台级" },
            { key: "db", label: "数据库", children: detail.db_type || "-" },
            { key: "status", label: "状态", children: detail.status },
            { key: "hash", label: "SHA256", span: 2, children: <Text key={detail.content_sha256} copyable ellipsis>{detail.content_sha256}</Text> },
            { key: "key", label: "签名密钥", children: detail.signing_key_id || "-" },
            { key: "signed", label: "签名时间", children: formatDate(detail.signed_at) },
            { key: "retention", label: "保留至", children: formatDate(detail.retention_until) },
          ]} />
          <Tabs size="small" items={[
            { key: "markdown", label: "Markdown", children: <pre className="report-content-preview">{detail.content_markdown || "暂无 Markdown 内容"}</pre> },
            { key: "json", label: "JSON", children: <pre className="report-content-preview">{JSON.stringify(detail.content_json || {}, null, 2)}</pre> },
            { key: "evidence", label: `证据 ${detailEvidence.count}`, children: <ReportEvidencePanel evidence={detailEvidence} /> },
          ]} />
          <div className="report-detail-actions"><Button icon={<CheckCircleOutlined />} onClick={() => verifyArtifact(detail)}>重新校验</Button><Space wrap>{(detail.formats || []).map((format) => <Button key={format} icon={<CloudDownloadOutlined />} onClick={() => downloadArtifact(detail, format)}>下载 {format === "word" ? "Word" : format.toUpperCase()}</Button>)}</Space></div>
        </Space> : null}
      </Modal>

      <Modal open={snapshotOpen} onCancel={closeSnapshot} onOk={submitSnapshot} okButtonProps={{ loading: submitting.snapshot }} okText="生成并签名" width={640} centered title="生成统一报告快照" className="adaptive-center-modal">
        <Form form={snapshotForm} layout="vertical">
          <Form.Item name="title" label="快照名称" rules={[{ required: true, message: "请输入快照名称" }]}><Input maxLength={500} /></Form.Item>
          <div className="report-form-grid">
            <Form.Item name="report_type" label="报告范围"><Select options={REPORT_TYPE_OPTIONS} /></Form.Item>
            <Form.Item name="retention_days" label="保留天数"><InputNumber min={1} max={36500} className="full-width" /></Form.Item>
          </div>
          <Form.Item name="instance_ids" label="实例范围" extra="留空表示当前权限范围内全部实例；多个 ID 用逗号分隔。"><Input placeholder="例如：1,2,8" /></Form.Item>
          <Form.Item name="watermark" label="水印"><Input placeholder="例如：内部使用 / 生产审计" maxLength={255} /></Form.Item>
          <Form.Item name="limit" hidden><InputNumber /></Form.Item>
        </Form>
      </Modal>

      <Modal open={scheduleOpen} onCancel={closeSchedule} onOk={submitSchedule} okButtonProps={{ loading: submitting.schedule }} okText={editingSchedule ? "保存" : "创建"} width={760} centered title={editingSchedule ? "编辑报告调度" : "新增报告调度"} className="adaptive-center-modal">
        <Form form={scheduleForm} layout="vertical">
          <div className="report-form-grid">
            <Form.Item name="name" label="策略名称" rules={[{ required: true, message: "请输入策略名称" }]}><Input maxLength={255} /></Form.Item>
            <Form.Item name="report_type" label="报告范围"><Select options={REPORT_TYPE_OPTIONS} /></Form.Item>
            <Form.Item name="schedule_type" label="调度类型"><Select options={SCHEDULE_TYPE_OPTIONS} /></Form.Item>
            <Form.Item name="timezone" label="时区"><Select showSearch options={[{ value: "Asia/Shanghai", label: "Asia/Shanghai" }, { value: "UTC", label: "UTC" }]} /></Form.Item>
          </div>
          {watchedScheduleType === "interval" ? <Form.Item name="interval_minutes" label="间隔分钟" rules={[{ required: true, message: "请输入执行间隔" }]}><InputNumber min={1} max={525600} className="full-width" /></Form.Item> : null}
          {watchedScheduleType === "cron" ? <Form.Item name="cron_expression" label="Cron 表达式" rules={[{ required: true, message: "请输入五段式 Cron 表达式" }]}><Input placeholder="0 8 * * 1-5" /></Form.Item> : null}
          {["daily", "workday", "weekly", "monthly", "quarterly"].includes(watchedScheduleType) ? <Form.Item name="run_time" label="执行时间" rules={[{ pattern: /^(?:[01]\d|2[0-3]):[0-5]\d$/, message: "请输入 HH:MM" }]}><Input placeholder="08:00" /></Form.Item> : null}
          {["workday", "weekly"].includes(watchedScheduleType) ? <Form.Item name="weekdays" label="执行日"><Checkbox.Group options={WEEKDAY_OPTIONS} /></Form.Item> : null}
          {["monthly", "quarterly"].includes(watchedScheduleType) ? <Form.Item name="day_of_month" label="每月日期"><InputNumber min={1} max={31} className="full-width" /></Form.Item> : null}
          <Form.Item name="output_formats" label="输出格式"><Checkbox.Group options={FORMAT_OPTIONS} /></Form.Item>
          <div className="report-form-grid">
            <Form.Item name="misfire_policy" label="漏跑处理"><Select options={[{ value: "skip", label: "跳过" }, { value: "fire_once", label: "补跑一次" }, { value: "catch_up", label: "限量追补" }]} /></Form.Item>
            <Form.Item name="max_catchup_runs" label="单轮最多补跑"><InputNumber min={1} max={10} className="full-width" /></Form.Item>
            <Form.Item name="misfire_grace_seconds" label="容忍延迟（秒）"><InputNumber min={0} max={604800} className="full-width" /></Form.Item>
            <Form.Item name="retention_days" label="保留天数"><InputNumber min={1} max={36500} className="full-width" /></Form.Item>
          </div>
          <Form.Item name="instance_ids" label="实例范围" extra="管理员可留空表示全部；受限用户必须填写实例 ID。"><Input placeholder="1,2,8" /></Form.Item>
          <div className="report-form-grid">
            <Form.Item name="filter_limit" label="单次最多纳管报告"><InputNumber min={1} max={1000} className="full-width" /></Form.Item>
            <Form.Item name="watermark" label="水印"><Input maxLength={255} /></Form.Item>
          </div>
          <Form.Item name="enabled" label="启用" valuePropName="checked"><Switch /></Form.Item>
        </Form>
      </Modal>

      <Modal open={subscriptionOpen} onCancel={closeSubscription} onOk={submitSubscription} okButtonProps={{ loading: submitting.subscription }} okText={editingSubscription ? "保存" : "创建"} width={660} centered title={editingSubscription ? "编辑报告订阅" : "新增报告订阅"} className="adaptive-center-modal">
        <Form form={subscriptionForm} layout="vertical">
          <Form.Item name="name" label="订阅名称" rules={[{ required: true, message: "请输入订阅名称" }]}><Input /></Form.Item>
          <Form.Item name="schedule_id" label="关联调度"><Select allowClear placeholder="留空匹配全部调度" options={schedules.map((row) => ({ value: row.id, label: row.name }))} /></Form.Item>
          <Form.Item name="report_types" label="报告范围"><Select mode="multiple" options={REPORT_TYPE_OPTIONS} /></Form.Item>
          <Form.Item name="channels" label="通知通道" extra="填写通知通道 ID 或别名，多个值用逗号分隔；留空时按平台通知策略路由。"><Input placeholder="例如：email,dingtalk" /></Form.Item>
          <Form.Item name="recipients" label="接收人"><Input.TextArea rows={3} placeholder="邮箱、手机号或用户标识，多个值用逗号或换行分隔" /></Form.Item>
          <Form.Item name="enabled" label="启用" valuePropName="checked"><Switch /></Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
