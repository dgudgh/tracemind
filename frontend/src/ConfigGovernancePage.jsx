import React, { useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  DatePicker,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Progress,
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
  CodeOutlined,
  DeleteOutlined,
  EditOutlined,
  FileProtectOutlined,
  PlusOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
  ScanOutlined,
  SendOutlined,
} from "@ant-design/icons";

import { configGovernanceApi } from "./modules/configGovernanceApi";

const { Text, Title } = Typography;

const DB_LABELS = {
  oracle: "Oracle",
  mysql: "MySQL",
  postgresql: "PostgreSQL",
  sqlserver: "SQL Server",
};

const DB_COLORS = {
  oracle: "red",
  mysql: "gold",
  postgresql: "blue",
  sqlserver: "cyan",
};

const STATUS_META = {
  draft: ["default", "草稿"],
  active: ["success", "已启用"],
  archived: ["default", "已归档"],
  success: ["success", "采集成功"],
  failed: ["error", "失败"],
  open: ["warning", "待处理"],
  accepted: ["blue", "已接受例外"],
  remediation_planned: ["processing", "已生成方案"],
  pending_restart: ["orange", "等待重启复核"],
  resolved: ["success", "已恢复"],
  pending_approval: ["gold", "待审批"],
  approval_failed: ["error", "提交失败"],
  pending_verification: ["processing", "待复核"],
  drift_remaining: ["warning", "仍有漂移"],
  verification_failed: ["error", "复核失败"],
  verified: ["success", "闭环完成"],
  applying: ["processing", "执行中"],
};

const CATEGORY_LABELS = {
  parameter: "数据库参数",
  capacity: "容量阈值",
  log_retention: "日志保留",
  object_lifecycle: "对象生命周期",
};

const COMPARATOR_LABELS = {
  eq: "等于",
  ne: "不等于",
  ge: "大于等于",
  le: "小于等于",
  gt: "大于",
  lt: "小于",
  in: "属于集合",
  contains: "包含",
};

const DEFAULT_ITEM = {
  category: "parameter",
  parameter_name: "",
  desired_value: "",
  comparator: "eq",
  value_type: "string",
  severity: "warning",
  restart_required: 0,
  description: "",
  remediation_hint: "",
};

function StatusTag({ value }) {
  const [color, label] = STATUS_META[value] || ["default", value || "未开始"];
  return <Tag color={color}>{label}</Tag>;
}

function DBTag({ value }) {
  return <Tag color={DB_COLORS[value]}>{DB_LABELS[value] || value || "-"}</Tag>;
}

function formatTime(value) {
  return value ? String(value).replace("T", " ").slice(0, 19) : "-";
}

function BaselineItemEditor({ items, onChange }) {
  const update = (index, key, value) => {
    const next = items.map((item, itemIndex) => (itemIndex === index ? { ...item, [key]: value } : item));
    onChange(next);
  };
  return (
    <div className="config-baseline-items">
      <div className="config-baseline-items-head">
        <Text strong>期望状态字段</Text>
        <Button
          icon={<PlusOutlined />}
          onClick={() => onChange([...items, { ...DEFAULT_ITEM }])}
        >
          添加字段
        </Button>
      </div>
      <div className="config-baseline-item-grid config-baseline-item-labels" aria-hidden="true">
        <span>类型</span><span>字段名</span><span>比较</span><span>期望值</span><span>值类型</span><span>风险</span><span>重启</span><span />
      </div>
      {items.map((item, index) => (
        <div className="config-baseline-item" key={`${index}-${item.id || "new"}`}>
          <div className="config-baseline-item-grid">
            <Select
              value={item.category}
              onChange={(value) => update(index, "category", value)}
              options={Object.entries(CATEGORY_LABELS).map(([value, label]) => ({ value, label }))}
            />
            <Input
              value={item.parameter_name}
              onChange={(event) => update(index, "parameter_name", event.target.value)}
              placeholder="例如 max_connections"
            />
            <Select
              value={item.comparator}
              onChange={(value) => update(index, "comparator", value)}
              options={Object.entries(COMPARATOR_LABELS).map(([value, label]) => ({ value, label }))}
            />
            <Input
              value={item.desired_value}
              onChange={(event) => update(index, "desired_value", event.target.value)}
              placeholder="期望值"
            />
            <Select
              value={item.value_type}
              onChange={(value) => update(index, "value_type", value)}
              options={[
                { value: "string", label: "文本" },
                { value: "integer", label: "整数" },
                { value: "number", label: "数值" },
                { value: "boolean", label: "开关" },
              ]}
            />
            <Select
              value={item.severity}
              onChange={(value) => update(index, "severity", value)}
              options={[
                { value: "info", label: "提示" },
                { value: "warning", label: "警告" },
                { value: "critical", label: "严重" },
              ]}
            />
            <Switch
              checked={Boolean(item.restart_required)}
              onChange={(checked) => update(index, "restart_required", checked ? 1 : 0)}
            />
            <Tooltip title="删除字段">
              <Button
                type="text"
                danger
                icon={<DeleteOutlined />}
                aria-label="删除字段"
                onClick={() => onChange(items.filter((_, itemIndex) => itemIndex !== index))}
              />
            </Tooltip>
          </div>
          <div className="config-baseline-item-notes">
            <Input
              value={item.description}
              onChange={(event) => update(index, "description", event.target.value)}
              placeholder="字段说明：为什么要检查、正常范围是什么"
            />
            <Input
              value={item.remediation_hint}
              onChange={(event) => update(index, "remediation_hint", event.target.value)}
              placeholder="整改说明：异常后如何处理"
            />
          </div>
        </div>
      ))}
      {!items.length ? <div className="config-empty-hint">尚未添加字段，草稿可保存，但启用前应补齐期望状态。</div> : null}
    </div>
  );
}

export default function ConfigGovernancePage({ instances = [], onNavigate }) {
  const [loading, setLoading] = useState(false);
  const [activeTab, setActiveTab] = useState("baselines");
  const [summary, setSummary] = useState({});
  const [baselines, setBaselines] = useState([]);
  const [snapshots, setSnapshots] = useState([]);
  const [drifts, setDrifts] = useState([]);
  const [remediations, setRemediations] = useState([]);
  const [audits, setAudits] = useState([]);
  const [dbFilter, setDbFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState("active");
  const [selectedDriftKeys, setSelectedDriftKeys] = useState([]);

  const [baselineOpen, setBaselineOpen] = useState(false);
  const [editingBaseline, setEditingBaseline] = useState(null);
  const [baselineItems, setBaselineItems] = useState([{ ...DEFAULT_ITEM }]);
  const [baselineForm] = Form.useForm();

  const [evaluateBaseline, setEvaluateBaseline] = useState(null);
  const [evaluateInstanceId, setEvaluateInstanceId] = useState(null);
  const [acceptingDrift, setAcceptingDrift] = useState(null);
  const [acceptReason, setAcceptReason] = useState("");
  const [planPreview, setPlanPreview] = useState(null);
  const [submitTarget, setSubmitTarget] = useState(null);
  const [maintenanceWindow, setMaintenanceWindow] = useState(null);
  const [rollbackScript, setRollbackScript] = useState("");

  const load = async () => {
    setLoading(true);
    try {
      const [summaryResult, baselineResult, snapshotResult, driftResult, remediationResult, auditResult] = await Promise.all([
        configGovernanceApi.summary(),
        configGovernanceApi.baselines(),
        configGovernanceApi.snapshots({ limit: 100 }),
        configGovernanceApi.drifts({ limit: 500 }),
        configGovernanceApi.remediations({ limit: 200 }),
        configGovernanceApi.audits({ limit: 200 }),
      ]);
      setSummary(summaryResult || {});
      setBaselines(baselineResult.items || []);
      setSnapshots(snapshotResult.items || []);
      setDrifts(driftResult.items || []);
      setRemediations(remediationResult.items || []);
      setAudits(auditResult.items || []);
    } catch (error) {
      message.error(error.message || "配置治理数据加载失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const filteredBaselines = useMemo(
    () => baselines.filter((item) => (
      (dbFilter === "all" || item.db_type === dbFilter)
      && (statusFilter === "all" || item.status === statusFilter)
    )),
    [baselines, dbFilter, statusFilter],
  );

  const openBaselineEditor = (baseline = null) => {
    setEditingBaseline(baseline);
    setBaselineItems((baseline?.items || [{ ...DEFAULT_ITEM }]).map((item) => ({ ...item })));
    baselineForm.setFieldsValue(baseline ? {
      name: baseline.name,
      db_type: baseline.db_type,
      version_pattern: baseline.version_pattern,
      scope_type: baseline.scope_type,
      scope_id: baseline.scope_id,
      status: baseline.status,
      is_default: Boolean(baseline.is_default),
      description: baseline.description,
    } : {
      db_type: "oracle",
      version_pattern: "*",
      scope_type: "global",
      status: "draft",
      is_default: false,
    });
    setBaselineOpen(true);
  };

  const saveBaseline = async () => {
    let values;
    try {
      values = await baselineForm.validateFields();
    } catch {
      return;
    }
    const invalidIndex = baselineItems.findIndex((item) => !item.parameter_name.trim() || String(item.desired_value) === "");
    if (invalidIndex >= 0) {
      message.error(`第 ${invalidIndex + 1} 个字段缺少字段名或期望值`);
      return;
    }
    const payload = {
      ...values,
      is_default: values.is_default ? 1 : 0,
      scope_id: values.scope_type === "global" ? null : values.scope_id,
      items: baselineItems.map((item, index) => ({ ...item, sort_order: index })),
    };
    setLoading(true);
    try {
      if (editingBaseline) await configGovernanceApi.updateBaseline(editingBaseline.id, payload);
      else await configGovernanceApi.createBaseline(payload);
      message.success(editingBaseline ? "基线版本已更新" : "配置基线已创建");
      setBaselineOpen(false);
      await load();
    } catch (error) {
      message.error(error.message || "配置基线保存失败");
    } finally {
      setLoading(false);
    }
  };

  const runEvaluation = async () => {
    if (!evaluateBaseline || !evaluateInstanceId) return;
    setLoading(true);
    try {
      const result = await configGovernanceApi.evaluateBaseline(evaluateBaseline.id, {
        instance_id: evaluateInstanceId,
        source: "live",
      });
      if (result.status === "failed") {
        message.error(`实时采集失败：${result.collector_error || "连接或权限异常"}`);
      } else if (result.drift_count) {
        message.warning(`比对完成，发现 ${result.drift_count} 项漂移`);
      } else {
        message.success("实时比对完成，全部字段符合基线");
      }
      setEvaluateBaseline(null);
      await load();
      setActiveTab("drifts");
    } catch (error) {
      message.error(error.message || "实时基线检查失败");
    } finally {
      setLoading(false);
    }
  };

  const createRemediation = async () => {
    const selected = drifts.filter((item) => selectedDriftKeys.includes(item.id));
    if (!selected.length) return;
    const groups = new Set(selected.map((item) => `${item.instance_id}:${item.baseline_id}`));
    if (groups.size !== 1) {
      message.error("一次整改只能选择同一实例、同一基线下的漂移项");
      return;
    }
    setLoading(true);
    try {
      const result = await configGovernanceApi.createRemediation({
        instance_id: selected[0].instance_id,
        baseline_id: selected[0].baseline_id,
        drift_ids: selected.map((item) => item.id),
      });
      message.success("整改方案已生成，尚未执行任何变更");
      setSelectedDriftKeys([]);
      setPlanPreview(result);
      await load();
    } catch (error) {
      message.error(error.message || "整改方案生成失败");
    } finally {
      setLoading(false);
    }
  };

  const submitRemediation = async () => {
    if (!submitTarget) return;
    if (submitTarget.risk_level === "high" && (!maintenanceWindow || maintenanceWindow.length !== 2)) {
      message.error("高风险整改必须选择维护窗口");
      return;
    }
    const payload = {
      rollback_script: rollbackScript || undefined,
      maintenance_window_start: maintenanceWindow?.[0]?.toISOString(),
      maintenance_window_end: maintenanceWindow?.[1]?.toISOString(),
    };
    setLoading(true);
    try {
      await configGovernanceApi.submitRemediation(submitTarget.id, payload);
      message.success("整改方案已提交统一审批中心");
      setSubmitTarget(null);
      await load();
    } catch (error) {
      message.error(error.message || "提交审批失败");
    } finally {
      setLoading(false);
    }
  };

  const applyRemediation = (row) => {
    Modal.confirm({
      centered: true,
      title: "执行已审批的配置整改",
      icon: <SafetyCertificateOutlined />,
      content: "系统将只执行方案中的结构化数据库参数动作，并在执行后进入待复核状态。非参数治理项仍保留为人工步骤。",
      okText: "确认执行",
      cancelText: "取消",
      onOk: async () => {
        const result = await configGovernanceApi.applyRemediation(row.id);
        if (result.status === "failed") throw new Error(result.apply_message || "整改执行失败");
        message.success(result.status === "pending_restart" ? "配置已写入，等待重启后复核" : "配置已提交，等待实时复核");
        await load();
      },
    });
  };

  const verifyRemediation = async (row) => {
    setLoading(true);
    try {
      const result = await configGovernanceApi.verifyRemediation(row.id);
      const status = result.remediation?.status;
      if (status === "verified") message.success("实时复核通过，整改已闭环");
      else message.warning(result.remediation?.apply_message || "复核完成，仍有未消除的漂移");
      await load();
    } catch (error) {
      message.error(error.message || "整改复核失败");
    } finally {
      setLoading(false);
    }
  };

  const baselineColumns = [
    {
      title: "基线",
      key: "name",
      width: 250,
      render: (_, row) => (
        <Space direction="vertical" size={2}>
          <Space size={6}><Text strong>{row.name}</Text>{row.is_default ? <Tag color="blue">默认</Tag> : null}</Space>
          <Text type="secondary">版本 v{row.version_no} · {row.item_count} 个字段</Text>
        </Space>
      ),
    },
    { title: "数据库", dataIndex: "db_type", width: 120, render: (value) => <DBTag value={value} /> },
    { title: "适用版本", dataIndex: "version_pattern", width: 130, render: (value) => value || "全部版本" },
    {
      title: "范围",
      key: "scope",
      width: 150,
      render: (_, row) => row.scope_type === "global" ? "全局" : `${row.scope_type === "instance" ? "实例" : "环境"} #${row.scope_id}`,
    },
    { title: "状态", dataIndex: "status", width: 110, render: (value) => <StatusTag value={value} /> },
    { title: "更新时间", dataIndex: "update_time", width: 170, render: formatTime },
    {
      title: "操作",
      key: "actions",
      width: 260,
      fixed: "right",
      render: (_, row) => (
        <Space size={4}>
          <Button
            type="link"
            icon={<ScanOutlined />}
            disabled={!row.items?.length || row.status === "archived"}
            onClick={() => {
              setEvaluateBaseline(row);
              const first = instances.find((item) => item.db_type === row.db_type);
              setEvaluateInstanceId(first?.id || null);
            }}
          >
            实时比对
          </Button>
          <Tooltip title="编辑基线"><Button type="text" icon={<EditOutlined />} onClick={() => openBaselineEditor(row)} /></Tooltip>
          <Popconfirm
            title="删除配置基线"
            description="已有检查历史的基线会转为归档，不会破坏审计记录。"
            onConfirm={async () => {
              const result = await configGovernanceApi.deleteBaseline(row.id);
              message.success(result.action === "archived" ? "基线已有历史，已归档" : "基线已删除");
              load();
            }}
          >
            <Tooltip title="删除或归档"><Button type="text" danger icon={<DeleteOutlined />} /></Tooltip>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  const snapshotColumns = [
    { title: "采集时间", dataIndex: "collected_at", width: 175, render: formatTime },
    { title: "实例", dataIndex: "instance_name", width: 150 },
    { title: "数据库", dataIndex: "db_type", width: 110, render: (value) => <DBTag value={value} /> },
    { title: "基线", dataIndex: "baseline_name", width: 190 },
    { title: "来源", dataIndex: "source", width: 90, render: (value) => value === "live" ? "实时连接" : value },
    { title: "状态", dataIndex: "status", width: 110, render: (value) => <StatusTag value={value} /> },
    {
      title: "结果",
      key: "result",
      width: 180,
      render: (_, row) => row.status === "failed"
        ? <Text type="danger" ellipsis={{ tooltip: row.collector_error }}>{row.collector_error || "采集失败"}</Text>
        : `${row.compliant_count}/${row.item_count} 合规 · ${row.drift_count} 漂移`,
    },
  ];

  const driftColumns = [
    { title: "实例", dataIndex: "instance_name", width: 145 },
    { title: "基线", dataIndex: "baseline_name", width: 190 },
    { title: "类别", dataIndex: "category", width: 125, render: (value) => CATEGORY_LABELS[value] || value },
    {
      title: "字段与差异",
      key: "difference",
      width: 280,
      render: (_, row) => (
        <Space direction="vertical" size={2}>
          <Text strong>{row.parameter_name}</Text>
          <Text type="secondary">当前 {row.actual_value ?? "未采集"} · {COMPARATOR_LABELS[row.comparator] || row.comparator} {row.desired_value}</Text>
        </Space>
      ),
    },
    { title: "级别", dataIndex: "severity", width: 90, render: (value) => <Tag color={value === "critical" ? "red" : value === "warning" ? "orange" : "blue"}>{value}</Tag> },
    { title: "状态", dataIndex: "status", width: 130, render: (value) => <StatusTag value={value} /> },
    { title: "最近发现", dataIndex: "last_detected_at", width: 175, render: formatTime },
    {
      title: "操作",
      key: "actions",
      width: 130,
      fixed: "right",
      render: (_, row) => (
        <Button
          type="link"
          disabled={!['open', 'accepted'].includes(row.status)}
          onClick={() => { setAcceptingDrift(row); setAcceptReason(""); }}
        >
          接受例外
        </Button>
      ),
    },
  ];

  const remediationColumns = [
    { title: "方案", dataIndex: "id", width: 80, render: (value) => `#${value}` },
    { title: "实例", dataIndex: "instance_name", width: 150 },
    { title: "基线", dataIndex: "baseline_name", width: 190 },
    { title: "漂移项", dataIndex: "drift_ids_json", width: 90, render: (value) => value?.length || 0 },
    { title: "风险", dataIndex: "risk_level", width: 90, render: (value) => <Tag color={value === "high" ? "red" : "orange"}>{value === "high" ? "高" : "中"}</Tag> },
    {
      title: "闭环状态",
      key: "status",
      width: 180,
      render: (_, row) => (
        <Space direction="vertical" size={2}>
          <StatusTag value={row.status} />
          {row.approval_status ? <Text type="secondary">审批 {row.approval_status}</Text> : null}
        </Space>
      ),
    },
    { title: "创建时间", dataIndex: "create_time", width: 175, render: formatTime },
    {
      title: "操作",
      key: "actions",
      width: 310,
      fixed: "right",
      render: (_, row) => (
        <Space size={3}>
          <Button type="link" icon={<CodeOutlined />} onClick={() => setPlanPreview(row)}>查看方案</Button>
          {!row.approval_request_id ? (
            <Button
              type="link"
              icon={<SendOutlined />}
              onClick={() => {
                setSubmitTarget(row);
                setMaintenanceWindow(null);
                setRollbackScript(row.rollback_script || "");
              }}
            >
              提交审批
            </Button>
          ) : (
            <Button type="link" onClick={() => onNavigate?.("protection.automation")}>审批中心</Button>
          )}
          {row.can_apply ? <Button type="link" danger onClick={() => applyRemediation(row)}>执行</Button> : null}
          {['pending_verification', 'pending_restart', 'drift_remaining', 'verification_failed'].includes(row.status) ? (
            <Button type="link" icon={<CheckCircleOutlined />} onClick={() => verifyRemediation(row)}>实时复核</Button>
          ) : null}
        </Space>
      ),
    },
  ];

  const auditColumns = [
    { title: "时间", dataIndex: "create_time", width: 180, render: formatTime },
    { title: "对象", key: "entity", width: 150, render: (_, row) => `${row.entity_type} #${row.entity_id}` },
    { title: "动作", dataIndex: "action", width: 150 },
    { title: "操作人", dataIndex: "actor", width: 130 },
    { title: "说明", dataIndex: "message", ellipsis: { showTitle: false }, render: (value) => <Tooltip title={value}><span>{value || "-"}</span></Tooltip> },
  ];

  const complianceKnown = summary.compliance_percent !== null && summary.compliance_percent !== undefined;
  const tabItems = [
    {
      key: "baselines",
      label: `基线库 ${baselines.length}`,
      children: (
        <section className="config-governance-section">
          <div className="config-governance-toolbar">
            <Space wrap>
              <Select value={dbFilter} onChange={setDbFilter} options={[
                { value: "all", label: "全部数据库" },
                ...Object.entries(DB_LABELS).map(([value, label]) => ({ value, label })),
              ]} />
              <Select value={statusFilter} onChange={setStatusFilter} options={[
                { value: "active", label: "已启用" },
                { value: "draft", label: "草稿" },
                { value: "archived", label: "已归档" },
                { value: "all", label: "全部状态" },
              ]} />
              <Text type="secondary">{filteredBaselines.length}/{baselines.length} 个基线</Text>
            </Space>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => openBaselineEditor()}>新建基线</Button>
          </div>
          <Table rowKey="id" loading={loading} columns={baselineColumns} dataSource={filteredBaselines} pagination={false} scroll={{ x: 1200, y: "calc(100vh - 490px)" }} />
        </section>
      ),
    },
    {
      key: "drifts",
      label: `采集与漂移 ${summary.active_drift_count || 0}`,
      children: (
        <Space direction="vertical" size={12} className="full-width">
          <section className="config-governance-section">
            <div className="config-governance-section-title"><Text strong>最近采集</Text><Text type="secondary">连接失败不会计为配置漂移</Text></div>
            <Table rowKey="id" size="small" loading={loading} columns={snapshotColumns} dataSource={snapshots.slice(0, 8)} pagination={false} scroll={{ x: 1050 }} />
          </section>
          <section className="config-governance-section">
            <div className="config-governance-toolbar">
              <Space><Text strong>当前漂移</Text><Text type="secondary">选择同一实例和基线下的字段生成整改方案</Text></Space>
              <Button type="primary" icon={<FileProtectOutlined />} disabled={!selectedDriftKeys.length} onClick={createRemediation}>生成整改方案</Button>
            </div>
            <Table
              rowKey="id"
              loading={loading}
              columns={driftColumns}
              dataSource={drifts}
              rowSelection={{
                selectedRowKeys: selectedDriftKeys,
                onChange: setSelectedDriftKeys,
                getCheckboxProps: (row) => ({ disabled: row.status === "resolved" }),
              }}
              pagination={{ pageSize: 20, showSizeChanger: false }}
              scroll={{ x: 1250, y: "calc(100vh - 700px)" }}
            />
          </section>
        </Space>
      ),
    },
    {
      key: "remediations",
      label: `整改闭环 ${remediations.length}`,
      children: (
        <section className="config-governance-section">
          <div className="config-governance-section-title">
            <Text strong>审批整改</Text>
            <Text type="secondary">方案生成、审批、结构化执行、实时复核全程留痕</Text>
          </div>
          <Table rowKey="id" loading={loading} columns={remediationColumns} dataSource={remediations} pagination={{ pageSize: 20, showSizeChanger: false }} scroll={{ x: 1300, y: "calc(100vh - 500px)" }} />
        </section>
      ),
    },
    {
      key: "audits",
      label: "审计记录",
      children: (
        <section className="config-governance-section">
          <div className="config-governance-section-title"><Text strong>配置治理审计</Text><Text type="secondary">保留基线版本、漂移处置和整改闭环证据</Text></div>
          <Table rowKey="id" loading={loading} columns={auditColumns} dataSource={audits} pagination={{ pageSize: 30, showSizeChanger: false }} scroll={{ x: 850, y: "calc(100vh - 500px)" }} />
        </section>
      ),
    },
  ];

  return (
    <Space direction="vertical" size={14} className="full-width config-governance-page">
      <section className="config-governance-header">
        <div className="config-governance-heading">
          <span className="config-governance-icon"><SafetyCertificateOutlined /></span>
          <div><Title level={4}>配置基线与漂移治理</Title><Text type="secondary">四引擎期望状态、实时采集、审批整改和复核闭环</Text></div>
        </div>
        <Button icon={<ReloadOutlined />} loading={loading} onClick={load}>刷新</Button>
      </section>

      <Alert
        type="info"
        showIcon
        message="平台不会因保存基线自动刷新或修改数据库"
        description="只有点击“实时比对”才连接实例采集；只有已审批方案在显式确认后才执行结构化参数动作。容量、日志保留和对象生命周期策略保留为人工步骤。"
      />

      <section className="config-governance-summary">
        <div><span>启用基线</span><strong>{summary.active_baseline_count || 0}</strong><small>共 {summary.baseline_count || 0}</small></div>
        <div><span>范围内实例</span><strong>{summary.scoped_instance_count || 0}</strong><small>已检查 {summary.evaluated_instance_count || 0}</small></div>
        <div><span>当前合规率</span><strong>{complianceKnown ? `${summary.compliance_percent}%` : "未验证"}</strong><Progress percent={complianceKnown ? summary.compliance_percent : 0} showInfo={false} status={complianceKnown ? "normal" : "exception"} /></div>
        <div><span>活动漂移</span><strong>{summary.active_drift_count || 0}</strong><small>严重 {summary.critical_drift_count || 0}</small></div>
        <div><span>采集失败</span><strong>{summary.collection_failure_count || 0}</strong><small>不计入漂移</small></div>
        <div><span>待闭环方案</span><strong>{summary.pending_remediation_count || 0}</strong><small>审批与复核</small></div>
      </section>

      <Tabs activeKey={activeTab} onChange={setActiveTab} items={tabItems} className="config-governance-tabs" />

      <Modal
        centered
        width={1080}
        open={baselineOpen}
        title={editingBaseline ? `编辑配置基线 · v${editingBaseline.version_no}` : "新建配置基线"}
        okText={editingBaseline ? "保存新版本" : "创建基线"}
        cancelText="取消"
        confirmLoading={loading}
        onOk={saveBaseline}
        onCancel={() => setBaselineOpen(false)}
        className="config-governance-modal"
      >
        <Form form={baselineForm} layout="vertical">
          <div className="config-baseline-form-grid">
            <Form.Item name="name" label="基线名称" rules={[{ required: true, message: "请输入基线名称" }]}><Input /></Form.Item>
            <Form.Item name="db_type" label="数据库类型" rules={[{ required: true }]}><Select disabled={Boolean(editingBaseline)} options={Object.entries(DB_LABELS).map(([value, label]) => ({ value, label }))} /></Form.Item>
            <Form.Item name="version_pattern" label="版本匹配"><Input placeholder="例如 19*、8.0.*，* 表示全部" /></Form.Item>
            <Form.Item name="status" label="状态"><Select options={[{ value: "draft", label: "草稿" }, { value: "active", label: "启用" }, { value: "archived", label: "归档" }]} /></Form.Item>
            <Form.Item name="scope_type" label="适用范围"><Select options={[{ value: "global", label: "全局" }, { value: "instance", label: "指定实例" }, { value: "environment", label: "指定环境" }]} /></Form.Item>
            <Form.Item noStyle shouldUpdate={(previous, current) => previous.scope_type !== current.scope_type}>
              {({ getFieldValue }) => getFieldValue("scope_type") === "global" ? <div /> : (
                <Form.Item name="scope_id" label={getFieldValue("scope_type") === "instance" ? "实例" : "环境 ID"} rules={[{ required: true }]}>
                  {getFieldValue("scope_type") === "instance" ? (
                    <Select showSearch optionFilterProp="label" options={instances.map((item) => ({ value: item.id, label: `${item.instance_name} · ${item.host}:${item.port}` }))} />
                  ) : <InputNumber min={1} className="full-width" />}
                </Form.Item>
              )}
            </Form.Item>
            <Form.Item name="is_default" label="默认基线" valuePropName="checked"><Switch /></Form.Item>
            <Form.Item name="description" label="基线说明" className="config-baseline-description"><Input.TextArea rows={2} /></Form.Item>
          </div>
        </Form>
        <BaselineItemEditor items={baselineItems} onChange={setBaselineItems} />
      </Modal>

      <Modal
        centered
        open={Boolean(evaluateBaseline)}
        title="实时配置比对"
        okText="开始采集并比对"
        cancelText="取消"
        confirmLoading={loading}
        onOk={runEvaluation}
        onCancel={() => setEvaluateBaseline(null)}
      >
        <Alert type="warning" showIcon message="该操作会实时连接数据库，但不会修改任何参数" className="config-modal-alert" />
        <Form layout="vertical">
          <Form.Item label="配置基线"><Input value={evaluateBaseline?.name} disabled /></Form.Item>
          <Form.Item label="目标实例" required>
            <Select
              value={evaluateInstanceId}
              onChange={setEvaluateInstanceId}
              options={instances.filter((item) => item.db_type === evaluateBaseline?.db_type).map((item) => ({ value: item.id, label: `${item.instance_name} · ${item.host}:${item.port}` }))}
            />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        centered
        open={Boolean(acceptingDrift)}
        title="接受配置例外"
        okText="确认接受"
        cancelText="取消"
        onCancel={() => setAcceptingDrift(null)}
        onOk={async () => {
          if (acceptReason.trim().length < 2) { message.error("请填写接受原因"); return; }
          await configGovernanceApi.acceptDrift(acceptingDrift.id, acceptReason.trim());
          message.success("已接受为例外；实际值变化或基线升级后会重新打开");
          setAcceptingDrift(null);
          load();
        }}
      >
        <Alert type="info" showIcon message={`${acceptingDrift?.parameter_name || "配置项"} 将保留偏差状态并记录操作人`} className="config-modal-alert" />
        <Input.TextArea rows={4} value={acceptReason} onChange={(event) => setAcceptReason(event.target.value)} placeholder="填写业务原因、有效期或替代控制措施" />
      </Modal>

      <Modal
        centered
        width={920}
        open={Boolean(planPreview)}
        title={`整改方案${planPreview ? ` #${planPreview.id}` : ""}`}
        footer={<Button onClick={() => setPlanPreview(null)}>关闭</Button>}
        onCancel={() => setPlanPreview(null)}
        className="config-plan-modal"
      >
        <Alert type="info" showIcon message={`风险等级 ${planPreview?.risk_level || "-"} · SHA256 ${planPreview?.script_sha256 || "-"}`} className="config-modal-alert" />
        <Tabs items={[
          { key: "change", label: "变更方案", children: <pre>{planPreview?.script}</pre> },
          { key: "rollback", label: "回滚方案", children: <pre>{planPreview?.rollback_script || "当前采集值不足，提交高风险审批前必须补充回滚脚本。"}</pre> },
          { key: "verify", label: "复核 SQL", children: <pre>{planPreview?.validation_script}</pre> },
        ]} />
      </Modal>

      <Modal
        centered
        width={760}
        open={Boolean(submitTarget)}
        title="提交配置整改审批"
        okText="提交审批"
        cancelText="取消"
        confirmLoading={loading}
        onOk={submitRemediation}
        onCancel={() => setSubmitTarget(null)}
      >
        <Alert
          type={submitTarget?.risk_level === "high" ? "error" : "warning"}
          showIcon
          message={submitTarget?.risk_level === "high" ? "高风险方案需要维护窗口和可执行回滚脚本" : "审批通过前不会执行任何变更"}
          className="config-modal-alert"
        />
        <Form layout="vertical">
          <Form.Item label="维护窗口" required={submitTarget?.risk_level === "high"}><DatePicker.RangePicker showTime className="full-width" value={maintenanceWindow} onChange={setMaintenanceWindow} /></Form.Item>
          <Form.Item label="回滚脚本" required={submitTarget?.risk_level === "high"}><Input.TextArea rows={8} value={rollbackScript} onChange={(event) => setRollbackScript(event.target.value)} /></Form.Item>
        </Form>
      </Modal>
    </Space>
  );
}
