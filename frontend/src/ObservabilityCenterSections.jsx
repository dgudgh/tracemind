import React, { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Progress,
  Row,
  Segmented,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  Typography,
  message,
} from "antd";
import { DeleteOutlined, PlayCircleOutlined, ReloadOutlined, SaveOutlined, SettingOutlined } from "@ant-design/icons";
import {
  autoBindObservability,
  buildObservabilityFaultTimeline,
  evaluateObservabilityDataQuality,
  exportGrafanaTemplate,
  getObservabilityDataQuality,
  getObservabilityLinkageHealth,
  listObservabilityDataQualityHistory,
  listObservabilityDataQualityPolicies,
  listObservabilityInstances,
  listGrafanaTemplates,
  linkApmSqlEvidence,
  queryApmTrace,
  queryObservabilityLogs,
  runObservabilityTask,
  runObservabilityRetention,
  runZabbixAction,
  saveCollectPolicy,
  saveMetricMapping,
  saveObservabilityBinding,
  saveObservabilityDataQualityPolicy,
  saveObservabilitySource,
  syncPrometheusStandardMetrics,
  testObservabilitySource,
} from "./modules/observabilityApi";
import { saveJsonFile } from "./modules/fileExport";
import { SEV_COLOR, SOURCE_TYPE_LABEL, sourceTypeOptions, statusTag } from "./ObservabilityCenterConfig";
import { BinaryStatusSwitch } from "./framework/BinaryStatusSwitch";

const { Text, Title } = Typography;

function linkageStatusColor(status) {
  if (status === "ok" || status === "success") return "green";
  if (status === "failed") return "red";
  if (status === "warning") return "orange";
  return "blue";
}

function linkageAlertType(status) {
  if (status === "ok") return "success";
  if (status === "failed") return "error";
  if (status === "warning") return "warning";
  return "info";
}

function compactDetails(value) {
  if (!value || typeof value !== "object") return "-";
  const parts = [];
  if (value.binding_count !== undefined) parts.push(`绑定 ${value.binding_count}`);
  if (value.source_count !== undefined) parts.push(`源 ${value.source_count}`);
  if (value.target_count !== undefined) parts.push(`Target ${value.target_count}`);
  if (value.binding_mapping_count !== undefined) parts.push(`绑定映射 ${value.binding_mapping_count}`);
  if (value.legacy_mapping_count !== undefined) parts.push(`历史映射 ${value.legacy_mapping_count}`);
  if (value.total_count !== undefined) parts.push(`证据 ${value.total_count}`);
  if (value.failed_count !== undefined) parts.push(`失败 ${value.failed_count}`);
  if (value.last_time) parts.push(`最近 ${value.last_time}`);
  return parts.length ? parts.join(" / ") : JSON.stringify(value);
}

const QUALITY_STATUS_META = {
  healthy: { color: "success", label: "可信" },
  degraded: { color: "warning", label: "降级" },
  stale: { color: "orange", label: "陈旧" },
  missing: { color: "error", label: "缺失" },
  unknown: { color: "default", label: "未验证" },
  disabled: { color: "default", label: "已停用" },
};

function qualityStatusTag(status) {
  const meta = QUALITY_STATUS_META[status] || { color: "default", label: status || "未知" };
  return <Tag color={meta.color}>{meta.label}</Tag>;
}

function qualityLag(value) {
  if (value === null || value === undefined) return "无样本";
  if (value < 60) return `${value}s`;
  if (value < 3600) return `${Math.floor(value / 60)}m ${value % 60}s`;
  return `${Math.floor(value / 3600)}h ${Math.floor((value % 3600) / 60)}m`;
}

function SourceForm({ onSaved, initialSourceType }) {
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);

  const save = async () => {
    const values = await form.validateFields();
    setSaving(true);
    try {
      await saveObservabilitySource(values);
      message.success("数据源已保存");
      form.resetFields();
      onSaved?.();
    } catch (e) {
      message.error(e.message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card size="small" title="新增数据源">
      <Form
        form={form}
        layout="vertical"
        initialValues={{ source_type: initialSourceType || "prometheus", auth_type: "none", enabled: 1, timeout_seconds: 10, collect_interval_seconds: 60 }}
      >
        <Row gutter={[12, 0]}>
          <Col xs={24} md={6}><Form.Item name="source_name" label="数据源名称" rules={[{ required: true }]}><Input /></Form.Item></Col>
          <Col xs={24} md={5}><Form.Item name="source_type" label="类型" rules={[{ required: true }]}><Select options={sourceTypeOptions()} /></Form.Item></Col>
          <Col xs={24} md={8}><Form.Item name="api_url" label="访问地址"><Input placeholder="http://host:port" /></Form.Item></Col>
          <Col xs={12} md={3}><Form.Item name="timeout_seconds" label="超时"><InputNumber min={1} max={120} className="full-width" /></Form.Item></Col>
          <Col xs={12} md={2}><Form.Item name="enabled" label="启用"><BinaryStatusSwitch /></Form.Item></Col>
          <Col xs={24} md={4}><Form.Item name="auth_type" label="认证方式"><Select options={["none", "basic", "bearer", "token", "api_key"].map((v) => ({ value: v, label: v }))} /></Form.Item></Col>
          <Col xs={24} md={5}><Form.Item name="username" label="账号"><Input /></Form.Item></Col>
          <Col xs={24} md={5}><Form.Item name="password" label="密码"><Input.Password /></Form.Item></Col>
          <Col xs={24} md={6}><Form.Item name="token" label="Token / API Key"><Input.Password /></Form.Item></Col>
          <Col xs={12} md={4}><Form.Item name="collect_interval_seconds" label="采集周期"><InputNumber min={15} max={86400} className="full-width" /></Form.Item></Col>
          <Col xs={24} md={12}><Form.Item name="remark" label="备注"><Input /></Form.Item></Col>
        </Row>
        <Button type="primary" icon={<SaveOutlined />} loading={saving} onClick={save}>保存数据源</Button>
      </Form>
    </Card>
  );
}

export function SourceManagementPage({ sources, load }) {
  const testSource = async (row) => {
    try {
      const res = await testObservabilitySource(row.id);
      message[res.status === "success" ? "success" : "warning"](res.message || res.status);
      await load();
    } catch (e) {
      message.error(e.message);
    }
  };
  return (
    <Space direction="vertical" size={12} className="full-width">
      <SourceForm onSaved={load} />
      <Card size="small" title="数据源列表">
        <Table
          rowKey="id"
          size="small"
          dataSource={sources}
          columns={[
            { title: "名称", dataIndex: "source_name", width: 180 },
            { title: "类型", dataIndex: "source_type", width: 130, render: (v) => <Tag>{SOURCE_TYPE_LABEL[v] || v}</Tag> },
            { title: "地址", dataIndex: "api_url", ellipsis: true },
            { title: "认证", dataIndex: "auth_type", width: 100 },
            { title: "启用", dataIndex: "enabled", width: 80, render: (v) => <Tag color={v ? "green" : "default"}>{v ? "启用" : "停用"}</Tag> },
            { title: "周期", dataIndex: "collect_interval_seconds", width: 90, render: (v) => `${v || 0}s` },
            {
              title: "操作",
              width: 110,
              render: (_, row) => <Button size="small" onClick={() => testSource(row)}>连接测试</Button>,
            },
          ]}
        />
      </Card>
    </Space>
  );
}

export function IntegrationPage({ type, sources, load }) {
  const matched = sources.filter((item) => item.source_type === type);
  const runManual = async (row) => {
    try {
      const res = await runObservabilityTask({
        sourceId: row.id,
        taskName: `${SOURCE_TYPE_LABEL[type] || type} 手动采集`,
        collectType: type,
      });
      message[res.task_status === "success" ? "success" : "warning"](`任务 ${res.task_status}`);
      await load();
    } catch (e) {
      message.error(e.message);
    }
  };
  const runZabbix = async (row, action) => {
    try {
      const res = await runZabbixAction(row.id, action);
      message[res.task_status === "success" ? "success" : "warning"](`任务 ${res.task_status}`);
      await load();
    } catch (e) {
      message.error(e.message);
    }
  };
  const runPrometheusStandard = async (row) => {
    try {
      const res = await syncPrometheusStandardMetrics(row.id);
      message[res.status === "success" ? "success" : "warning"](`标准指标 ${res.stored_count || 0} 条`);
      await load();
    } catch (e) {
      message.error(e.message);
    }
  };
  const actionColumn = type === "zabbix"
    ? {
        title: "操作",
        width: 300,
        render: (_, row) => (
          <Space size={6} wrap>
            <Button size="small" icon={<PlayCircleOutlined />} onClick={() => runManual(row)}>连接测试</Button>
            <Button size="small" onClick={() => runZabbix(row, "hosts")}>同步Host</Button>
            <Button size="small" onClick={() => runZabbix(row, "problems")}>同步Problem</Button>
            <Button size="small" onClick={() => runZabbix(row, "metrics")}>同步指标</Button>
          </Space>
        ),
      }
    : type === "prometheus"
      ? {
          title: "操作",
          width: 220,
          render: (_, row) => (
            <Space size={6} wrap>
              <Button size="small" icon={<PlayCircleOutlined />} onClick={() => runManual(row)}>只读测试</Button>
              <Button size="small" type="primary" onClick={() => runPrometheusStandard(row)}>同步标准指标</Button>
            </Space>
          ),
        }
    : { title: "操作", width: 110, render: (_, row) => <Button size="small" icon={<PlayCircleOutlined />} onClick={() => runManual(row)}>只读测试</Button> };
  return (
    <Space direction="vertical" size={12} className="full-width">
      <Alert
        showIcon
        type="info"
        message={`${SOURCE_TYPE_LABEL[type] || type} 接入`}
        description="当前模块作为统一接入层，所有采集动作只读执行；高频指标优先从外部监控拉取，TraceMind AI 智能运维平台 深度分析保持低频、可关闭。"
      />
      <SourceForm onSaved={load} initialSourceType={type} />
      <Card size="small" title="接入状态">
        {matched.length ? (
          <Table
            rowKey="id"
            size="small"
            dataSource={matched}
            pagination={false}
            columns={[
              { title: "名称", dataIndex: "source_name" },
              { title: "地址", dataIndex: "api_url", ellipsis: true },
              { title: "认证", dataIndex: "auth_type", width: 100 },
              { title: "Token", dataIndex: "has_token", width: 90, render: (v) => <Tag color={v ? "green" : "default"}>{v ? "已配置" : "未配置"}</Tag> },
              { title: "超时", dataIndex: "timeout_seconds", width: 90, render: (v) => `${v}s` },
              actionColumn,
            ]}
          />
        ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无接入配置" />}
      </Card>
    </Space>
  );
}

export function MetricMappingPage({ mappings, standardMetrics = [], load }) {
  const [form] = Form.useForm();
  const save = async () => {
    const values = await form.validateFields();
    await saveMetricMapping(values);
    message.success("指标映射已保存");
    form.resetFields();
    await load();
  };
  return (
    <Space direction="vertical" size={12} className="full-width">
      <Card size="small" title="新增指标映射">
        <Form form={form} layout="vertical" initialValues={{ source_type: "prometheus", enabled: 1 }}>
          <Row gutter={[12, 0]}>
            <Col xs={24} md={4}><Form.Item name="source_type" label="来源类型"><Select options={sourceTypeOptions()} /></Form.Item></Col>
            <Col xs={24} md={6}><Form.Item name="source_metric" label="来源指标" rules={[{ required: true }]}><Input /></Form.Item></Col>
            <Col xs={24} md={6}><Form.Item name="standard_metric" label="标准指标" rules={[{ required: true }]}>
              <Select
                showSearch
                allowClear
                placeholder="host.cpu.used_pct"
                options={standardMetrics.map((item) => ({ value: item.standard_metric, label: item.standard_metric }))}
              />
            </Form.Item></Col>
            <Col xs={12} md={3}><Form.Item name="metric_unit" label="单位"><Input /></Form.Item></Col>
            <Col xs={12} md={3}><Form.Item name="enabled" label="启用"><BinaryStatusSwitch /></Form.Item></Col>
            <Col xs={24} md={2}><Button type="primary" style={{ marginTop: 30 }} onClick={save}>保存</Button></Col>
          </Row>
        </Form>
      </Card>
      <Card size="small" title="指标映射">
        <Table rowKey="id" size="small" dataSource={mappings} columns={[
          { title: "来源类型", dataIndex: "source_type", width: 130, render: (v) => <Tag>{v}</Tag> },
          { title: "来源指标", dataIndex: "source_metric" },
          { title: "标准指标", dataIndex: "standard_metric" },
          { title: "单位", dataIndex: "metric_unit", width: 90, render: (v) => v || "-" },
          { title: "说明", dataIndex: "description", ellipsis: true },
        ]} />
      </Card>
    </Space>
  );
}

export function InstanceBindingPage({ bindings, sources, load }) {
  const [form] = Form.useForm();
  const [instances, setInstances] = useState([]);
  const [loadingInstances, setLoadingInstances] = useState(false);

  const loadInstances = async () => {
    setLoadingInstances(true);
    try {
      const res = await listObservabilityInstances();
      setInstances(res.items || []);
    } catch (e) {
      message.error(e.message);
    } finally {
      setLoadingInstances(false);
    }
  };

  useEffect(() => { loadInstances(); }, []);

  const save = async () => {
    const values = await form.validateFields();
    await saveObservabilityBinding(values);
    message.success("实例绑定已保存");
    form.resetFields();
    await load();
  };

  const autoBind = async () => {
    const res = await autoBindObservability();
    message.success(`自动绑定完成：${res.matched_count || 0} 个实例`);
    await load();
  };

  const instanceOptions = instances.map((item) => ({
    value: item.id,
    label: `${item.instance_name} / ${item.db_type} / ${item.host}:${item.port}`,
  }));
  const sourceOptions = sources.map((item) => ({
    value: item.id,
    label: `${item.source_name} / ${item.source_type}`,
  }));

  return (
    <Space direction="vertical" size={12} className="full-width">
      <Alert
        showIcon
        type="info"
        message="实例绑定"
        description="打通数据库实例、主机、Zabbix Host、Prometheus Target 和业务系统。告警进入综合告警时会按绑定关系自动关联数据库实例。"
      />
      <Card size="small" title="新增绑定" extra={<Button onClick={autoBind}>自动绑定</Button>}>
        <Form form={form} layout="vertical" initialValues={{ enabled: 1, confidence: 80, binding_status: "active" }}>
          <Row gutter={[12, 0]}>
            <Col xs={24} md={6}><Form.Item name="instance_id" label="数据库实例" rules={[{ required: true }]}>
              <Select showSearch loading={loadingInstances} options={instanceOptions} />
            </Form.Item></Col>
            <Col xs={24} md={4}><Form.Item name="host_ip" label="主机 IP"><Input placeholder="例如：192.168.1.10" /></Form.Item></Col>
            <Col xs={24} md={4}><Form.Item name="host_identifier" label="主机标识"><Input placeholder="hostname / ip" /></Form.Item></Col>
            <Col xs={24} md={5}><Form.Item name="zabbix_source_id" label="Zabbix 数据源"><Select allowClear options={sourceOptions.filter((item) => item.label.includes("/ zabbix"))} /></Form.Item></Col>
            <Col xs={24} md={5}><Form.Item name="zabbix_hostid" label="Zabbix HostID"><Input /></Form.Item></Col>
            <Col xs={24} md={5}><Form.Item name="zabbix_host_name" label="Zabbix 主机名"><Input /></Form.Item></Col>
            <Col xs={24} md={5}><Form.Item name="prometheus_source_id" label="Prometheus 数据源"><Select allowClear options={sourceOptions.filter((item) => item.label.includes("/ prometheus"))} /></Form.Item></Col>
            <Col xs={24} md={6}><Form.Item name="prometheus_target" label="Prometheus Target"><Input placeholder="host:9100" /></Form.Item></Col>
            <Col xs={24} md={4}><Form.Item name="business_system" label="业务系统"><Input /></Form.Item></Col>
            <Col xs={12} md={2}><Form.Item name="confidence" label="置信度"><InputNumber min={0} max={100} className="full-width" /></Form.Item></Col>
            <Col xs={12} md={2}><Form.Item name="enabled" label="启用"><BinaryStatusSwitch /></Form.Item></Col>
            <Col xs={24} md={2}><Button type="primary" style={{ marginTop: 30 }} onClick={save}>保存</Button></Col>
          </Row>
        </Form>
      </Card>
      <Card size="small" title="绑定关系">
        <Table rowKey="id" size="small" dataSource={bindings} columns={[
          { title: "实例", dataIndex: "instance_id", width: 90 },
          { title: "主机 IP", dataIndex: "host_ip", width: 140, render: (v) => v || "-" },
          { title: "主机标识", dataIndex: "host_identifier", width: 160, render: (v) => v || "-" },
          { title: "Zabbix HostID", dataIndex: "zabbix_hostid", width: 130, render: (v) => v || "-" },
          { title: "Zabbix 主机", dataIndex: "zabbix_host_name", width: 180, ellipsis: true, render: (v) => v || "-" },
          { title: "Prometheus Target", dataIndex: "prometheus_target", width: 220, ellipsis: true, render: (v) => v || "-" },
          { title: "业务系统", dataIndex: "business_system", width: 130, render: (v) => v || "-" },
          { title: "规则", dataIndex: "match_rule", width: 150, render: (v) => <Tag>{v || "manual"}</Tag> },
          { title: "置信度", dataIndex: "confidence", width: 90, render: (v) => `${v || 0}%` },
          { title: "状态", dataIndex: "enabled", width: 80, render: (v) => <Tag color={v ? "green" : "default"}>{v ? "启用" : "停用"}</Tag> },
        ]} />
      </Card>
    </Space>
  );
}

export function LinkageHealthPage({ sources }) {
  const [instances, setInstances] = useState([]);
  const [instanceId, setInstanceId] = useState();
  const [health, setHealth] = useState(null);
  const [loadingInstances, setLoadingInstances] = useState(false);
  const [loading, setLoading] = useState(false);
  const [probing, setProbing] = useState(false);

  const loadInstances = async () => {
    setLoadingInstances(true);
    try {
      const res = await listObservabilityInstances();
      const items = res.items || [];
      setInstances(items);
      if (!instanceId && items.length) {
        const preferred = items[0];
        setInstanceId(preferred.id);
        await loadHealth(preferred.id, false);
      }
    } catch (e) {
      message.error(e.message);
    } finally {
      setLoadingInstances(false);
    }
  };

  const loadHealth = async (nextInstanceId = instanceId, probeSources = false) => {
    if (!nextInstanceId) return;
    if (probeSources) setProbing(true);
    setLoading(true);
    try {
      const res = await getObservabilityLinkageHealth({
        instanceId: nextInstanceId,
        probeSources,
        probeTimeoutSeconds: 2,
      });
      setHealth(res);
      if (probeSources) {
        message[res.overall_status === "failed" ? "warning" : "success"](res.summary || "探测完成");
      }
    } catch (e) {
      message.error(e.message);
    } finally {
      setLoading(false);
      setProbing(false);
    }
  };

  useEffect(() => { loadInstances(); }, []);

  const instanceOptions = instances.map((item) => ({
    value: item.id,
    label: `${item.instance_name} / ${item.db_type} / ${item.host}:${item.port}`,
  }));
  const sourceCount = sources.filter((item) => ["zabbix", "prometheus", "alertmanager"].includes(item.source_type)).length;
  const checks = health?.checks || [];
  const sourceProbes = health?.source_probes || [];
  const recommendations = (health?.recommendations || []).map((text, index) => ({ id: index + 1, text }));

  return (
    <Space direction="vertical" size={12} className="full-width">
      <Alert
        showIcon
        type="info"
        message="实例级联动健康"
        description="把数据库实例、Zabbix Host、Prometheus Target、Alertmanager、最近告警和指标证据合并检查。"
      />
      <Card size="small">
        <Space wrap>
          <Select
            showSearch
            style={{ width: 420, maxWidth: "100%" }}
            loading={loadingInstances}
            placeholder="选择数据库实例"
            value={instanceId}
            onChange={(value) => {
              setInstanceId(value);
              loadHealth(value, false);
            }}
            options={instanceOptions}
            optionFilterProp="label"
          />
          <Button icon={<ReloadOutlined />} loading={loading && !probing} onClick={() => loadHealth(instanceId, false)}>刷新</Button>
          <Button type="primary" icon={<PlayCircleOutlined />} loading={probing} onClick={() => loadHealth(instanceId, true)}>测试源连通性</Button>
          <Tag color="blue">外部源 {sourceCount}</Tag>
        </Space>
      </Card>
      {health && (
        <>
          <Alert
            showIcon
            type={linkageAlertType(health.overall_status)}
            message={health.summary}
            description={`${health.instance?.instance_name || "-"} / ${health.instance?.db_type || "-"} / ${health.instance?.host || "-"}:${health.instance?.port || "-"}`}
          />
          <Row gutter={[12, 12]}>
            <Col xs={12} md={6}>
              <Card size="small"><Statistic title="整体状态" value={health.overall_status || "-"} valueStyle={{ color: linkageStatusColor(health.overall_status) === "red" ? "#cf1322" : undefined }} /></Card>
            </Col>
            <Col xs={12} md={6}>
              <Card size="small"><Statistic title="检查项" value={checks.length} /></Card>
            </Col>
            <Col xs={12} md={6}>
              <Card size="small"><Statistic title="启用绑定" value={health.bindings?.length || 0} /></Card>
            </Col>
            <Col xs={12} md={6}>
              <Card size="small"><Statistic title="证据数" value={health.recent_evidence?.total_count || 0} /></Card>
            </Col>
          </Row>
          <Card size="small" title="检查项明细">
            <Table
              rowKey="key"
              size="small"
              dataSource={checks}
              pagination={false}
              columns={[
                { title: "检查项", dataIndex: "label", width: 170 },
                { title: "状态", dataIndex: "status", width: 110, render: (v) => <Tag color={linkageStatusColor(v)}>{v}</Tag> },
                { title: "结论", dataIndex: "message", ellipsis: true },
                { title: "关键数据", dataIndex: "details", width: 260, ellipsis: true, render: compactDetails },
              ]}
            />
          </Card>
          <Card size="small" title="修复建议">
            <Table
              rowKey="id"
              size="small"
              dataSource={recommendations}
              pagination={false}
              columns={[
                { title: "序号", dataIndex: "id", width: 80 },
                { title: "建议", dataIndex: "text", render: (v) => <Text>{v}</Text> },
              ]}
            />
          </Card>
          <Card size="small" title="数据源探测">
            <Table
              rowKey={(row) => `${row.source_type}-${row.source_id}`}
              size="small"
              dataSource={sourceProbes}
              locale={{ emptyText: "未主动探测" }}
              pagination={false}
              columns={[
                { title: "类型", dataIndex: "source_type", width: 130, render: (v) => <Tag>{SOURCE_TYPE_LABEL[v] || v}</Tag> },
                { title: "名称", dataIndex: "source_name", width: 180, ellipsis: true },
                { title: "状态", dataIndex: "status", width: 110, render: (v) => <Tag color={linkageStatusColor(v)}>{v}</Tag> },
                { title: "耗时", dataIndex: "elapsed_ms", width: 100, render: (v) => v === undefined ? "-" : `${v} ms` },
                { title: "地址", dataIndex: "probe_url", ellipsis: true, render: (v, row) => v || row.api_url || "-" },
                { title: "返回", dataIndex: "message", ellipsis: true },
              ]}
            />
          </Card>
        </>
      )}
    </Space>
  );
}

export function DataQualityPage({ initialQuality, onQualityChange }) {
  const [quality, setQuality] = useState(initialQuality || null);
  const [policies, setPolicies] = useState([]);
  const [history, setHistory] = useState([]);
  const [view, setView] = useState("current");
  const [loading, setLoading] = useState(false);
  const [evaluating, setEvaluating] = useState(false);
  const [policyOpen, setPolicyOpen] = useState(false);
  const [policySource, setPolicySource] = useState(null);
  const [savingPolicy, setSavingPolicy] = useState(false);
  const [retentionOpen, setRetentionOpen] = useState(false);
  const [retentionPlan, setRetentionPlan] = useState(null);
  const [retentionRunning, setRetentionRunning] = useState(false);
  const [policyForm] = Form.useForm();

  const load = async () => {
    setLoading(true);
    try {
      const [qualityResult, policyResult, historyResult] = await Promise.all([
        getObservabilityDataQuality(),
        listObservabilityDataQualityPolicies(),
        listObservabilityDataQualityHistory(null, 100),
      ]);
      setQuality(qualityResult);
      setPolicies(policyResult.items || []);
      setHistory(historyResult.items || []);
      onQualityChange?.(qualityResult);
    } catch (error) {
      message.error(error.message || "数据质量加载失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  const evaluate = async () => {
    setEvaluating(true);
    try {
      const result = await evaluateObservabilityDataQuality();
      setQuality(result);
      onQualityChange?.(result);
      const historyResult = await listObservabilityDataQualityHistory(null, 100);
      setHistory(historyResult.items || []);
      message.success("数据质量评估完成，结果已留存");
    } catch (error) {
      message.error(error.message || "数据质量评估失败");
    } finally {
      setEvaluating(false);
    }
  };

  const openPolicy = (item = null) => {
    const policy = item?.policy || policies.find((row) => row.scope_key === "global") || {};
    setPolicySource(item);
    policyForm.setFieldsValue({
      policy_name: policy.policy_name || (item ? `${item.source_name} 数据质量策略` : "平台默认数据质量策略"),
      freshness_warn_seconds: policy.freshness_warn_seconds ?? 180,
      freshness_critical_seconds: policy.freshness_critical_seconds ?? 600,
      completeness_window_minutes: policy.completeness_window_minutes ?? 60,
      min_completeness_pct: policy.min_completeness_pct ?? 90,
      max_clock_skew_seconds: policy.max_clock_skew_seconds ?? 60,
      cardinality_budget: policy.cardinality_budget ?? 5000,
      hot_retention_days: policy.hot_retention_days ?? 7,
      warm_retention_days: policy.warm_retention_days ?? 30,
      archive_retention_days: policy.archive_retention_days ?? 180,
      anomaly_sensitivity: policy.anomaly_sensitivity ?? 3,
      enabled: policy.enabled ?? 1,
    });
    setPolicyOpen(true);
  };

  const savePolicy = async () => {
    const values = await policyForm.validateFields();
    setSavingPolicy(true);
    try {
      await saveObservabilityDataQualityPolicy({
        ...values,
        source_id: policySource?.source_id || null,
        source_type: policySource?.source_id ? policySource.source_type : null,
      });
      message.success("数据质量策略已保存，下次评估生效");
      setPolicyOpen(false);
      await load();
    } catch (error) {
      if (!error?.errorFields) message.error(error.message || "策略保存失败");
    } finally {
      setSavingPolicy(false);
    }
  };

  const previewRetention = async () => {
    setRetentionRunning(true);
    try {
      const result = await runObservabilityRetention({ dryRun: true });
      setRetentionPlan(result);
      setRetentionOpen(true);
    } catch (error) {
      message.error(error.message || "保留计划生成失败");
    } finally {
      setRetentionRunning(false);
    }
  };

  const executeRetention = async () => {
    setRetentionRunning(true);
    try {
      const result = await runObservabilityRetention({
        dryRun: false,
        confirmation: "DELETE_EXPIRED_OBSERVABILITY",
      });
      setRetentionPlan(result);
      message.success(`已清理 ${result.deleted_count || 0} 条过期指标样本`);
      await load();
    } catch (error) {
      message.error(error.message || "过期样本清理失败");
    } finally {
      setRetentionRunning(false);
    }
  };

  const rows = quality?.items || [];
  const statusCounts = quality?.status_counts || {};
  const currentColumns = [
    {
      title: "数据源",
      key: "source",
      width: 190,
      fixed: "left",
      render: (_, row) => (
        <Space direction="vertical" size={1}>
          <Text strong>{row.source_name}</Text>
          <Text type="secondary">{SOURCE_TYPE_LABEL[row.source_type] || row.source_type} · {row.source_kind}</Text>
        </Space>
      ),
    },
    { title: "可信状态", dataIndex: "status", width: 100, render: qualityStatusTag },
    {
      title: "质量分",
      dataIndex: "score",
      width: 130,
      render: (value, row) => <Progress percent={value || 0} size="small" status={row.status === "missing" ? "exception" : "normal"} />,
    },
    {
      title: "新鲜度",
      key: "freshness",
      width: 120,
      render: (_, row) => <Space direction="vertical" size={0}><Text>{qualityLag(row.lag_seconds)}</Text><Text type="secondary">{row.freshness_status}</Text></Space>,
    },
    {
      title: "完整率",
      dataIndex: "completeness_pct",
      width: 110,
      render: (value, row) => value === null || value === undefined ? "不适用" : `${value}% (${row.sample_count}/${row.expected_sample_count})`,
    },
    {
      title: "时钟偏差",
      dataIndex: "clock_skew_seconds",
      width: 100,
      render: (value) => value === null || value === undefined ? "无时间戳" : `${value}s`,
    },
    { title: "采集任务", dataIndex: "task_status", width: 110, render: (value) => <Tag color={value === "success" ? "green" : value === "failed" ? "red" : "default"}>{value}</Tag> },
    {
      title: "指标基数",
      key: "cardinality",
      width: 120,
      render: (_, row) => `${row.cardinality}/${row.cardinality_budget}`,
    },
    {
      title: "异常基线",
      dataIndex: "baseline_ready",
      width: 100,
      render: (value) => <Tag color={value ? "green" : "gold"}>{value ? "可分析" : "样本不足"}</Tag>,
    },
    {
      title: "保留分层",
      key: "retention",
      width: 170,
      render: (_, row) => `热 ${row.retention?.hot || 0} / 温 ${row.retention?.warm || 0} / 归档 ${row.retention?.archive || 0} / 过期 ${row.retention?.expired || 0}`,
    },
    {
      title: "操作",
      key: "action",
      width: 90,
      fixed: "right",
      render: (_, row) => <Button type="link" icon={<SettingOutlined />} onClick={() => openPolicy(row)}>策略</Button>,
    },
  ];

  const historyColumns = [
    { title: "评估时间", dataIndex: "evaluated_at", width: 190 },
    { title: "来源 ID", dataIndex: "source_id", width: 90 },
    { title: "类型", dataIndex: "source_type", width: 130, render: (value) => <Tag>{SOURCE_TYPE_LABEL[value] || value}</Tag> },
    { title: "状态", dataIndex: "status", width: 100, render: qualityStatusTag },
    { title: "质量分", dataIndex: "score", width: 90 },
    { title: "延迟", dataIndex: "lag_seconds", width: 110, render: qualityLag },
    { title: "完整率", dataIndex: "completeness_pct", width: 100, render: (value) => value === null ? "不适用" : `${value}%` },
    { title: "时钟偏差", dataIndex: "clock_skew_seconds", width: 100, render: (value) => value === null ? "-" : `${value}s` },
    { title: "任务", dataIndex: "task_status", width: 110 },
    { title: "基数", dataIndex: "cardinality", width: 90 },
    { title: "过期样本", dataIndex: "expired_sample_count", width: 100 },
  ];

  const alertType = quality?.overall_status === "healthy" ? "success"
    : ["missing", "stale"].includes(quality?.overall_status) ? "error"
      : quality?.overall_status === "degraded" ? "warning" : "info";

  return (
    <Space direction="vertical" size={12} className="full-width observability-data-quality">
      <section className="data-quality-heading">
        <div>
          <Text type="secondary">可信数据入口</Text>
          <Title level={5}>采集数据质量</Title>
          <Text type="secondary">连接成功不等于数据可信，按样本新鲜度、完整率、时钟、任务和基数共同判定。</Text>
        </div>
        <Space wrap>
          <Button icon={<SettingOutlined />} onClick={() => openPolicy()}>默认策略</Button>
          <Button icon={<DeleteOutlined />} loading={retentionRunning} onClick={previewRetention}>保留计划</Button>
          <Button type="primary" icon={<PlayCircleOutlined />} loading={evaluating} onClick={evaluate}>立即评估</Button>
          <Button icon={<ReloadOutlined />} loading={loading} onClick={load}>刷新</Button>
        </Space>
      </section>

      <Alert
        showIcon
        type={alertType}
        message={`整体数据状态：${QUALITY_STATUS_META[quality?.overall_status]?.label || "未评估"}`}
        description={`质量分 ${quality?.score || 0}；可信 ${quality?.trusted_source_count || 0}/${quality?.enabled_source_count || 0} 个启用数据源。缺失与陈旧数据不会进入健康结论。`}
      />

      <section className="data-quality-summary" aria-label="数据质量摘要">
        <div><Text type="secondary">启用数据源</Text><strong>{quality?.enabled_source_count || 0}</strong></div>
        <div><Text type="secondary">可信</Text><strong>{quality?.trusted_source_count || 0}</strong></div>
        <div><Text type="secondary">降级</Text><strong>{statusCounts.degraded || 0}</strong></div>
        <div><Text type="secondary">陈旧</Text><strong>{statusCounts.stale || 0}</strong></div>
        <div><Text type="secondary">缺失</Text><strong>{statusCounts.missing || 0}</strong></div>
      </section>

      <div className="data-quality-view-switch">
        <Segmented value={view} onChange={setView} options={[{ label: "当前质量", value: "current" }, { label: `评估历史 ${history.length}`, value: "history" }]} />
        <Text type="secondary">评估只在点击“立即评估”时留存快照</Text>
      </div>

      <Table
        rowKey={view === "current" ? "source_id" : "id"}
        size="small"
        loading={loading}
        dataSource={view === "current" ? rows : history}
        columns={view === "current" ? currentColumns : historyColumns}
        pagination={view === "history" ? { pageSize: 20, showSizeChanger: false } : false}
        scroll={{ x: view === "current" ? 1330 : 1150 }}
        locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={view === "current" ? "尚未配置可观测数据源" : "尚无评估快照"} /> }}
      />

      <Modal
        centered
        width={760}
        open={policyOpen}
        title={policySource ? `${policySource.source_name} · 数据质量策略` : "平台默认数据质量策略"}
        onCancel={() => setPolicyOpen(false)}
        onOk={savePolicy}
        okText="保存策略"
        confirmLoading={savingPolicy}
        destroyOnHidden
      >
        <Alert type="info" showIcon message={policySource ? "该策略只覆盖当前数据源" : "未配置源级策略时使用平台默认值"} style={{ marginBottom: 16 }} />
        <Form form={policyForm} layout="vertical">
          <Row gutter={12}>
            <Col span={24}><Form.Item name="policy_name" label="策略名称" rules={[{ required: true }]}><Input /></Form.Item></Col>
            <Col xs={12} md={8}><Form.Item name="freshness_warn_seconds" label="新鲜度预警（秒）" rules={[{ required: true }]}><InputNumber min={15} className="full-width" /></Form.Item></Col>
            <Col xs={12} md={8}><Form.Item name="freshness_critical_seconds" label="陈旧阈值（秒）" rules={[{ required: true }]}><InputNumber min={30} className="full-width" /></Form.Item></Col>
            <Col xs={12} md={8}><Form.Item name="max_clock_skew_seconds" label="最大时钟偏差（秒）" rules={[{ required: true }]}><InputNumber min={0} className="full-width" /></Form.Item></Col>
            <Col xs={12} md={8}><Form.Item name="completeness_window_minutes" label="完整率窗口（分钟）" rules={[{ required: true }]}><InputNumber min={5} className="full-width" /></Form.Item></Col>
            <Col xs={12} md={8}><Form.Item name="min_completeness_pct" label="最低完整率（%）" rules={[{ required: true }]}><InputNumber min={1} max={100} className="full-width" /></Form.Item></Col>
            <Col xs={12} md={8}><Form.Item name="cardinality_budget" label="指标基数预算" rules={[{ required: true }]}><InputNumber min={1} className="full-width" /></Form.Item></Col>
            <Col xs={12} md={8}><Form.Item name="hot_retention_days" label="热数据（天）" rules={[{ required: true }]}><InputNumber min={1} className="full-width" /></Form.Item></Col>
            <Col xs={12} md={8}><Form.Item name="warm_retention_days" label="温数据（天）" rules={[{ required: true }]}><InputNumber min={1} className="full-width" /></Form.Item></Col>
            <Col xs={12} md={8}><Form.Item name="archive_retention_days" label="归档保留（天）" rules={[{ required: true }]}><InputNumber min={1} className="full-width" /></Form.Item></Col>
            <Col xs={12} md={8}><Form.Item name="anomaly_sensitivity" label="异常灵敏度（σ）" rules={[{ required: true }]}><InputNumber min={0.5} max={10} step={0.5} className="full-width" /></Form.Item></Col>
            <Col xs={12} md={8}><Form.Item name="enabled" label="策略启用"><BinaryStatusSwitch /></Form.Item></Col>
          </Row>
        </Form>
      </Modal>

      <Modal
        centered
        width={760}
        open={retentionOpen}
        title="指标保留计划"
        onCancel={() => setRetentionOpen(false)}
        footer={[
          <Button key="close" onClick={() => setRetentionOpen(false)}>关闭</Button>,
          <Popconfirm
            key="execute"
            title="确认清理超过归档保留期的指标样本？"
            description="该操作只删除计划中明确列出的过期历史样本。"
            okText="确认清理"
            cancelText="取消"
            onConfirm={executeRetention}
          >
            <Button danger icon={<DeleteOutlined />} loading={retentionRunning} disabled={!retentionPlan?.expired_sample_count}>清理过期样本</Button>
          </Popconfirm>,
        ]}
      >
        <Alert
          type={retentionPlan?.expired_sample_count ? "warning" : "success"}
          showIcon
          message={`可清理 ${retentionPlan?.expired_sample_count || 0} 条过期指标样本`}
          description="预览不会删除数据；实际清理需要再次确认，并写入采集日志。"
          style={{ marginBottom: 16 }}
        />
        <Table
          rowKey="source_id"
          size="small"
          pagination={false}
          dataSource={retentionPlan?.items || []}
          columns={[
            { title: "数据源", dataIndex: "source_name" },
            { title: "类型", dataIndex: "source_type", width: 130, render: (value) => <Tag>{SOURCE_TYPE_LABEL[value] || value}</Tag> },
            { title: "保留天数", dataIndex: "archive_retention_days", width: 100 },
            { title: "截止时间", dataIndex: "cutoff", width: 190 },
            { title: "过期样本", dataIndex: "expired_sample_count", width: 100 },
          ]}
        />
      </Modal>
    </Space>
  );
}

export function CollectPolicyPage({ policies, load }) {
  const [form] = Form.useForm();
  const save = async () => {
    const values = await form.validateFields();
    await saveCollectPolicy(values);
    message.success("采集策略已保存");
    form.resetFields();
    await load();
  };
  return (
    <Space direction="vertical" size={12} className="full-width">
      <Card size="small" title="新增采集策略">
        <Form form={form} layout="vertical" initialValues={{ source_type: "prometheus", collect_type: "minute_monitor", interval_seconds: 300, timeout_seconds: 10, max_concurrency: 2, retry_times: 1, enabled: 1 }}>
          <Row gutter={[12, 0]}>
            <Col xs={24} md={5}><Form.Item name="policy_name" label="策略名称" rules={[{ required: true }]}><Input /></Form.Item></Col>
            <Col xs={24} md={4}><Form.Item name="source_type" label="来源类型"><Select options={sourceTypeOptions()} /></Form.Item></Col>
            <Col xs={24} md={4}><Form.Item name="collect_type" label="策略类型"><Select options={["realtime_light", "minute_monitor", "hourly_inspection", "daily_deep_analysis", "manual"].map((v) => ({ value: v, label: v }))} /></Form.Item></Col>
            <Col xs={12} md={3}><Form.Item name="interval_seconds" label="周期秒"><InputNumber min={15} className="full-width" /></Form.Item></Col>
            <Col xs={12} md={3}><Form.Item name="timeout_seconds" label="超时秒"><InputNumber min={1} className="full-width" /></Form.Item></Col>
            <Col xs={12} md={3}><Form.Item name="max_concurrency" label="并发"><InputNumber min={1} max={20} className="full-width" /></Form.Item></Col>
            <Col xs={12} md={2}><Button type="primary" style={{ marginTop: 30 }} onClick={save}>保存</Button></Col>
          </Row>
        </Form>
      </Card>
      <Card size="small" title="采集策略">
        <Table rowKey="id" size="small" dataSource={policies} columns={[
          { title: "策略", dataIndex: "policy_name" },
          { title: "来源", dataIndex: "source_type", width: 130, render: (v) => <Tag>{v}</Tag> },
          { title: "类型", dataIndex: "collect_type", width: 150 },
          { title: "周期", dataIndex: "interval_seconds", width: 90, render: (v) => `${v}s` },
          { title: "超时", dataIndex: "timeout_seconds", width: 80, render: (v) => `${v}s` },
          { title: "并发", dataIndex: "max_concurrency", width: 80 },
          { title: "状态", dataIndex: "enabled", width: 80, render: (v) => <Tag color={v ? "green" : "default"}>{v ? "启用" : "停用"}</Tag> },
        ]} />
      </Card>
    </Space>
  );
}

export function AlertIngestPage({ alerts }) {
  return (
    <Card size="small" title="告警接入">
      <Alert showIcon type="info" message="Alertmanager Webhook" description="/api/v1/observability/alertmanager/webhook；通用 Webhook 为 /api/v1/observability/alerts/ingest。接入后会同步进入统一告警事件。" style={{ marginBottom: 12 }} />
      <Table rowKey="id" size="small" dataSource={alerts} columns={[
        { title: "来源", dataIndex: "source_type", width: 120, render: (v) => <Tag>{v}</Tag> },
        { title: "级别", dataIndex: "severity", width: 90, render: (v) => <Tag color={SEV_COLOR[v] || "default"}>{v}</Tag> },
        { title: "标题", dataIndex: "title", ellipsis: true },
        { title: "指标", dataIndex: "metric_name", width: 170, render: (v) => v || "-" },
        { title: "状态", dataIndex: "status", width: 100, render: statusTag },
        { title: "最后时间", dataIndex: "last_time", width: 180, render: (v) => v || "-" },
      ]} />
    </Card>
  );
}

export function CollectTaskPage({ tasks, sources, load }) {
  const [sourceId, setSourceId] = useState();
  const run = async () => {
    if (!sourceId) return message.warning("请选择数据源");
    const res = await runObservabilityTask({ sourceId, taskName: "手动只读采集", collectType: "manual" });
    message[res.task_status === "success" ? "success" : "warning"](`任务 ${res.task_status}`);
    await load();
  };
  return (
    <Space direction="vertical" size={12} className="full-width">
      <Card size="small">
        <Space wrap>
          <Select style={{ width: 260 }} placeholder="选择数据源" value={sourceId} onChange={setSourceId} options={sources.map((item) => ({ value: item.id, label: `${item.source_name} / ${item.source_type}` }))} />
          <Button type="primary" icon={<PlayCircleOutlined />} onClick={run}>运行只读采集</Button>
        </Space>
      </Card>
      <Card size="small" title="采集任务">
        <Table rowKey="id" size="small" dataSource={tasks} columns={[
          { title: "任务", dataIndex: "task_name", ellipsis: true },
          { title: "类型", dataIndex: "collect_type", width: 130 },
          { title: "状态", dataIndex: "task_status", width: 100, render: statusTag },
          { title: "成功", dataIndex: "success_count", width: 80 },
          { title: "失败", dataIndex: "fail_count", width: 80 },
          { title: "耗时", dataIndex: "duration_ms", width: 100, render: (v) => v ? `${v} ms` : "-" },
          { title: "开始时间", dataIndex: "start_time", width: 180, render: (v) => v || "-" },
          { title: "错误", dataIndex: "error_message", ellipsis: true },
        ]} />
      </Card>
    </Space>
  );
}

export function CollectLogPage({ logs }) {
  return (
    <Card size="small" title="采集日志">
      <Table rowKey="id" size="small" dataSource={logs} columns={[
        { title: "级别", dataIndex: "log_level", width: 90, render: (v) => <Tag color={v === "error" ? "red" : "blue"}>{v}</Tag> },
        { title: "类型", dataIndex: "log_type", width: 140 },
        { title: "消息", dataIndex: "message", ellipsis: true },
        { title: "数据源", dataIndex: "source_id", width: 90, render: (v) => v || "-" },
        { title: "任务", dataIndex: "task_id", width: 90, render: (v) => v || "-" },
        { title: "时间", dataIndex: "create_time", width: 180 },
      ]} />
    </Card>
  );
}

export function CurrentMetricPage({ metrics }) {
  return (
    <Card size="small" title="当前标准指标">
      <Table rowKey={(row) => `${row.source_type}-${row.source_id || "source"}-${row.host_id || "host"}-${row.standard_metric}`} size="small" dataSource={metrics} columns={[
        { title: "来源", dataIndex: "source_type", width: 120, render: (v) => <Tag>{v}</Tag> },
        { title: "标准指标", dataIndex: "standard_metric", width: 220 },
        { title: "值", dataIndex: "metric_value", width: 120, render: (v) => v ?? "-" },
        { title: "单位", dataIndex: "metric_unit", width: 90, render: (v) => v || "-" },
        { title: "主机/对象", dataIndex: "host_id", ellipsis: true, render: (v) => v || "-" },
        { title: "实例", dataIndex: "instance_id", width: 90, render: (v) => v || "-" },
        { title: "状态", dataIndex: "status", width: 90, render: statusTag },
        { title: "采集时间", dataIndex: "collect_time", width: 180, render: (v) => v || "-" },
      ]} />
    </Card>
  );
}

export function MetricHistoryPage({ history }) {
  return (
    <Card size="small" title="历史指标证据">
      <Table rowKey="id" size="small" dataSource={history} columns={[
        { title: "来源", dataIndex: "source_type", width: 120, render: (v) => <Tag>{v}</Tag> },
        { title: "标准指标", dataIndex: "standard_metric", width: 220 },
        { title: "值", dataIndex: "metric_value", width: 120, render: (v) => v ?? "-" },
        { title: "单位", dataIndex: "metric_unit", width: 90, render: (v) => v || "-" },
        { title: "主机/对象", dataIndex: "host_id", ellipsis: true, render: (v) => v || "-" },
        { title: "时间", dataIndex: "metric_time", width: 180, render: (v) => v || "-" },
      ]} />
    </Card>
  );
}

export function ExporterManagementPage({ exporters, sources }) {
  return (
    <Space direction="vertical" size={12} className="full-width">
      <Alert showIcon type="info" message="Exporter 管理" description="统一登记 node_exporter、数据库 exporter、nginx_exporter、jmx_exporter、elasticsearch_exporter 等采集端点；当前版本以数据源登记和只读测试为主。" />
      <Row gutter={[12, 12]}>
        {exporters.map((item) => (
          <Col xs={24} md={6} key={item.name}>
            <Card size="small">
              <Statistic title={item.name} value={item.status} />
            </Card>
          </Col>
        ))}
      </Row>
      <Card size="small" title="已登记 Exporter 数据源">
        <Table rowKey="id" size="small" dataSource={sources.filter((item) => item.source_type === "exporter")} columns={[
          { title: "名称", dataIndex: "source_name" },
          { title: "地址", dataIndex: "api_url", ellipsis: true },
          { title: "最后配置", dataIndex: "update_time", width: 180, render: (v) => v || "-" },
        ]} />
      </Card>
    </Space>
  );
}

export function EvidenceIntegrationPage({ title, type, sources, load }) {
  return (
    <Space direction="vertical" size={12} className="full-width">
      <Alert showIcon type="info" message={title} description="支持数据源登记、连接测试、手动只读采集、采集任务和日志追踪，采集结果会写入当前指标和历史指标。Nginx、Tomcat/JVM、SkyWalking、Elasticsearch 均已接入后端采集器。" />
      <IntegrationPage type={type} sources={sources} load={load} />
    </Space>
  );
}

function normalizedRange(values) {
  if (!values.start_time || !values.end_time) throw new Error("请选择完整时间范围");
  return {
    start_time: new Date(values.start_time).toISOString(),
    end_time: new Date(values.end_time).toISOString(),
  };
}

export function LogAnalysisPage({ sources, timelineOnly = false }) {
  const [form] = Form.useForm();
  const [loading, setLoading] = useState(false);
  const [events, setEvents] = useState([]);
  const logSources = sources.filter((item) => ["elasticsearch", "opensearch", "loki", "graylog"].includes(item.source_type));

  const run = async () => {
    const values = await form.validateFields();
    setLoading(true);
    try {
      const range = normalizedRange(values);
      const result = await queryObservabilityLogs({
        ...range,
        source_id: values.source_id,
        instance_id: values.instance_id,
        application: values.application,
        host: values.host,
        severity: values.severity,
        trace_id: values.trace_id,
        keyword: values.keyword,
        limit: 500,
      });
      let nextEvents = result.items || [];
      if (timelineOnly) {
        const timeline = await buildObservabilityFaultTimeline({
          ...range,
          instance_id: values.instance_id,
          events: nextEvents,
        });
        nextEvents = timeline.items || [];
      }
      setEvents(nextEvents);
      message.success(`查询完成，共 ${nextEvents.length} 条`);
    } catch (error) {
      if (!error?.errorFields) message.error(error.message || "日志查询失败");
    } finally {
      setLoading(false);
    }
  };

  return (
    <Space direction="vertical" size={16} className="full-width integration-workspace observability-log-analysis">
      <Alert showIcon type="info" message={timelineOnly ? "故障时间线" : "日志关联分析"} description="查询窗口最长 24 小时，单次最多返回 2,000 条；服务端仅保存查询条件和数量，不保存原始日志正文。" />
      <Form form={form} layout="vertical">
        <Row gutter={12}>
          <Col xs={24} md={5}><Form.Item name="source_id" label="日志数据源" rules={[{ required: true }]}><Select options={logSources.map((item) => ({ value: item.id, label: `${item.source_name} / ${item.source_type}` }))} /></Form.Item></Col>
          <Col xs={12} md={4}><Form.Item name="start_time" label="开始时间" rules={[{ required: true }]}><Input type="datetime-local" /></Form.Item></Col>
          <Col xs={12} md={4}><Form.Item name="end_time" label="结束时间" rules={[{ required: true }]}><Input type="datetime-local" /></Form.Item></Col>
          <Col xs={12} md={3}><Form.Item name="instance_id" label="实例 ID"><InputNumber min={1} className="full-width" /></Form.Item></Col>
          <Col xs={12} md={3}><Form.Item name="severity" label="级别"><Select allowClear options={["debug", "info", "warning", "error", "critical"].map((value) => ({ value, label: value.toUpperCase() }))} /></Form.Item></Col>
          <Col xs={24} md={6}><Form.Item name="trace_id" label="Trace ID"><Input /></Form.Item></Col>
          <Col xs={12} md={4}><Form.Item name="application" label="应用"><Input /></Form.Item></Col>
          <Col xs={12} md={4}><Form.Item name="host" label="主机"><Input /></Form.Item></Col>
          <Col xs={24} md={10}><Form.Item name="keyword" label="关键字"><Input.Search enterButton="查询" loading={loading} onSearch={run} /></Form.Item></Col>
        </Row>
      </Form>
      <Table rowKey={(row) => row.raw_ref || `${row.event_time}:${row.source_type}:${row.message}`} loading={loading} dataSource={events} size="small" scroll={{ x: "max-content" }} locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无日志结果" /> }} columns={[
        { title: "时间", dataIndex: "event_time", width: 190 },
        { title: "来源", dataIndex: "source_type", width: 130, render: (value) => <Tag>{value}</Tag> },
        { title: "级别", dataIndex: "severity", width: 100, render: (value) => <Tag color={SEV_COLOR[value] || "default"}>{value || "-"}</Tag> },
        { title: "主机", dataIndex: "host", width: 150, ellipsis: true },
        { title: "应用", dataIndex: "application", width: 150, ellipsis: true },
        { title: "Trace ID", dataIndex: "trace_id", width: 190, ellipsis: true },
        { title: "消息", dataIndex: "message", ellipsis: true },
      ]} />
    </Space>
  );
}

export function GrafanaTemplatePage() {
  const [templates, setTemplates] = useState([]);
  const [preview, setPreview] = useState(null);
  const [loading, setLoading] = useState(false);
  const [form] = Form.useForm();

  useEffect(() => {
    listGrafanaTemplates()
      .then((result) => setTemplates(result.items || []))
      .catch((error) => message.error(error.message || "模板加载失败"));
  }, []);

  const generate = async () => {
    const values = await form.validateFields();
    setLoading(true);
    try {
      const result = await exportGrafanaTemplate(values);
      setPreview(result);
      message.success("Dashboard JSON 已生成");
    } catch (error) {
      message.error(error.message || "Dashboard 生成失败");
    } finally {
      setLoading(false);
    }
  };

  return (
    <Space direction="vertical" size={16} className="full-width integration-workspace">
      <Alert showIcon type="info" message="Grafana 模板库" description="导出前会显示模板指标要求；文件不包含 SQL、密码或 Token。" />
      <Form form={form} layout="vertical" initialValues={{ refresh: "30s" }}>
        <Row gutter={12}>
          <Col xs={24} md={8}><Form.Item name="template_key" label="模板" rules={[{ required: true }]}><Select options={templates.map((item) => ({ value: item.key, label: item.title }))} /></Form.Item></Col>
          <Col xs={24} md={8}><Form.Item name="datasource_uid" label="Prometheus 数据源 UID" rules={[{ required: true }]}><Input /></Form.Item></Col>
          <Col xs={12} md={4}><Form.Item name="refresh" label="刷新频率"><Select options={["10s", "30s", "1m", "5m"].map((value) => ({ value, label: value }))} /></Form.Item></Col>
          <Col xs={12} md={4}><Form.Item label="生成"><Button type="primary" loading={loading} onClick={generate}>生成并预览</Button></Form.Item></Col>
        </Row>
      </Form>
      {templates.length ? <Table rowKey="key" size="small" dataSource={templates} pagination={false} columns={[
        { title: "模板", dataIndex: "title" },
        { title: "版本", dataIndex: "version", width: 100 },
        { title: "所需指标", dataIndex: "required_metrics", render: (items) => (items || []).map((item) => <Tag key={item}>{item}</Tag>) },
      ]} /> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无模板" />}
      {preview ? <Space wrap><Text type="secondary">已生成 {preview.dashboard?.panels?.length || 0} 个面板</Text><Button onClick={() => saveJsonFile(preview, `${form.getFieldValue("template_key")}.grafana.json`)}>下载 JSON</Button></Space> : null}
    </Space>
  );
}

export function ApmTracePage({ sources, linkSql = false }) {
  const [form] = Form.useForm();
  const [loading, setLoading] = useState(false);
  const [trace, setTrace] = useState(null);
  const [evidence, setEvidence] = useState([]);
  const apmSources = sources.filter((item) => ["skywalking", "jaeger", "zipkin", "opentelemetry"].includes(item.source_type));

  const run = async () => {
    const values = await form.validateFields();
    setLoading(true);
    try {
      if (linkSql) {
        const range = normalizedRange(values);
        const linked = await linkApmSqlEvidence({
          ...range,
          source_id: values.source_id,
          instance_id: values.instance_id,
          trace_ids: [values.trace_id],
        });
        setEvidence(linked.items || []);
        setTrace(null);
      } else {
        const traceResult = await queryApmTrace({ source_id: values.source_id, trace_id: values.trace_id });
        setTrace(traceResult);
      }
      message.success("链路查询完成");
    } catch (error) {
      if (!error?.errorFields) message.error(error.message || "链路查询失败");
    } finally {
      setLoading(false);
    }
  };

  return (
    <Space direction="vertical" size={16} className="full-width integration-workspace">
      <Alert showIcon type="info" message={linkSql ? "链路与 SQL 关联" : "APM 链路查询"} description={linkSql ? "链路由服务端直接向已启用的 APM 数据源查询；仅将匹配置信度达到 60 的 SQL 证据纳入诊断。" : "服务端只读查询 APM 链路，原始链路正文不会落库。"} />
      <Form form={form} layout="vertical">
        <Row gutter={12}>
          <Col xs={24} md={6}><Form.Item name="source_id" label="APM 数据源" rules={[{ required: true }]}><Select options={apmSources.map((item) => ({ value: item.id, label: `${item.source_name} / ${item.source_type}` }))} /></Form.Item></Col>
          <Col xs={24} md={linkSql ? 6 : 12}><Form.Item name="trace_id" label="Trace ID" rules={[{ required: true }]}><Input /></Form.Item></Col>
          {linkSql ? <Col xs={12} md={4}><Form.Item name="instance_id" label="实例 ID" rules={[{ required: true }]}><InputNumber min={1} className="full-width" /></Form.Item></Col> : null}
          {linkSql ? <Col xs={12} md={4}><Form.Item name="start_time" label="开始时间" rules={[{ required: true }]}><Input type="datetime-local" /></Form.Item></Col> : null}
          {linkSql ? <Col xs={12} md={4}><Form.Item name="end_time" label="结束时间" rules={[{ required: true }]}><Input type="datetime-local" /></Form.Item></Col> : null}
        </Row>
        <Button type="primary" loading={loading} onClick={run}>{linkSql ? "查询并关联 SQL" : "查询链路"}</Button>
      </Form>
      <Table rowKey={(row) => row.span_id || row.sql_fingerprint} size="small" loading={loading} dataSource={linkSql ? evidence : (trace?.spans || [])} locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无链路证据" /> }} scroll={{ x: "max-content" }} columns={linkSql ? [
        { title: "服务", dataIndex: "service", width: 160 }, { title: "端点", dataIndex: "endpoint", width: 180, ellipsis: true }, { title: "SQL 指纹", dataIndex: "sql_fingerprint", width: 220, ellipsis: true },
        { title: "实例", dataIndex: "instance_id", width: 100, render: (value) => value || "未匹配" }, { title: "置信度", dataIndex: "match_confidence", width: 100, render: (value) => `${value || 0}%` }, { title: "说明", dataIndex: "unmatched_reason", ellipsis: true, render: (value) => value || "已匹配" },
      ] : [
        { title: "Span", dataIndex: "span_id", width: 180, ellipsis: true }, { title: "服务", dataIndex: "service", width: 160 }, { title: "端点", dataIndex: "endpoint", width: 200, ellipsis: true },
        { title: "耗时", dataIndex: "duration_ms", width: 100, render: (value) => `${value || 0} ms` }, { title: "状态", dataIndex: "status", width: 100, render: statusTag },
      ]} />
    </Space>
  );
}
