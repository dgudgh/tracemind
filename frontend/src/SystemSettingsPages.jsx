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
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  Tooltip,
  message,
} from "antd";
import {
  CloudServerOutlined,
  DatabaseOutlined,
  InfoCircleOutlined,
  LinkOutlined,
  MedicineBoxOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
  SaveOutlined,
  UserOutlined,
} from "@ant-design/icons";
import { aiConfigApi, alertReportApi, instanceApi, settingsApi, sshApi } from "./apiClients";
import { BinaryStatusSwitch } from "./framework/BinaryStatusSwitch";
function SystemParamsPage() {
  const [groups, setGroups] = useState([]);
  const [values, setValues] = useState({});
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const r = await settingsApi.list();
      setGroups(r.groups || []);
      const v = {};
      (r.items || []).forEach(it => { v[it.key] = it.value; });
      setValues(v);
    } catch (e) { message.error(e.message); }
    setLoading(false);
  };
  useEffect(() => { load(); }, []);

  const save = async () => {
    setSaving(true);
    try {
      await settingsApi.update(values);
      message.success("系统参数已保存");
      await load();
    } catch (e) { message.error(e.message); }
    setSaving(false);
  };

  const setVal = (k, v) => setValues(s => ({ ...s, [k]: v }));

  return (
    <Space direction="vertical" size={16} className="full-width">
      <Alert type="info" showIcon message="系统参数：慢SQL阈值、数据保留天数、平台信息、告警默认阈值等全局配置。修改后保存即生效（采集/统计/清理会读取最新值）。" />
      <Card>
        <Space>
          <Button icon={<ReloadOutlined />} loading={loading} onClick={load}>刷新</Button>
          <Button type="primary" icon={<SaveOutlined />} loading={saving} onClick={save}>保存全部</Button>
        </Space>
      </Card>
      {groups.map(g => (
        <Card key={g.category} title={g.label} size="small">
          <Form layout="vertical">
            <Row gutter={[16, 8]}>
              {g.items.map(it => (
                <Col xs={24} md={12} lg={8} key={it.key}>
                  <Form.Item label={it.label || it.key} help={it.description} style={{ marginBottom: 12 }}>
                    {it.value_type === "int" || it.value_type === "float"
                      ? <InputNumber style={{ width: "100%" }} value={values[it.key]}
                          onChange={v => setVal(it.key, v)} min={0} />
                      : <Input value={values[it.key]} onChange={e => setVal(it.key, e.target.value)} />}
                  </Form.Item>
                </Col>
              ))}
            </Row>
          </Form>
        </Card>
      ))}
    </Space>
  );
}


const renderTime = (value) => value ? String(value).replace("T", " ").slice(0, 19) : "-";

const SSH_DB_LABELS = {
  oracle: "Oracle",
  mysql: "MySQL",
  postgresql: "PostgreSQL",
  sqlserver: "SQL Server",
};

const SSH_ENV_LABELS = {
  prod: "生产",
  production: "生产",
  test: "测试",
  testing: "测试",
  dev: "开发",
  development: "开发",
  staging: "预发布",
};

function SshAssetsPage() {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(false);
  const [actionLoading, setActionLoading] = useState(null);
  const [connectionStates, setConnectionStates] = useState({});
  const [resultModal, setResultModal] = useState({ open: false, title: "", data: null });

  const load = async () => {
    setLoading(true);
    try {
      const res = await instanceApi.list({ page: 1, page_size: 500 });
      const rows = res.items || res || [];
      setItems(rows.filter((row) => row.ssh_host || row.ssh_username || row.host));
    } catch (e) {
      message.error(e.message || "加载 SSH 资产失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  const runAction = async (row, action, title, actionKey) => {
    setActionLoading(`${row.id}:${actionKey}`);
    try {
      const data = await action(row.id);
      const actionSucceeded = data?.ok ?? data?.success ?? true;
      if (actionKey === "test") {
        setConnectionStates((current) => ({
          ...current,
          [row.id]: actionSucceeded ? "success" : "failed",
        }));
      }
      setResultModal({ open: true, title: `${row.instance_name || row.host} · ${title}`, data });
      if (actionSucceeded) {
        message.success(data?.message || `${title}完成`);
      } else {
        message.warning(data?.message || `${title}未通过`);
      }
    } catch (e) {
      if (actionKey === "test") {
        setConnectionStates((current) => ({ ...current, [row.id]: "failed" }));
      }
      message.error(e.message || `${title}失败`);
    } finally {
      setActionLoading(null);
    }
  };

  const sshReady = (row) => Boolean(row.ssh_host && row.ssh_username);
  const actionIsLoading = (row, actionKey) => actionLoading === `${row.id}:${actionKey}`;
  const dbTypeKey = (row) => String(row.db_type || "database").toLowerCase();
  const dbTypeLabel = (row) => SSH_DB_LABELS[dbTypeKey(row)] || row.db_type || "数据库";
  const envLabel = (row) => {
    const value = String(row.env_type || "").trim();
    return SSH_ENV_LABELS[value.toLowerCase()] || value || "未标记环境";
  };
  const sshState = (row) => {
    if (!row.ssh_host) return { tone: "incomplete", label: "未配置主机" };
    if (!row.ssh_username) return { tone: "incomplete", label: "未配置账号" };
    if (connectionStates[row.id] === "success") return { tone: "success", label: "连接正常" };
    if (connectionStates[row.id] === "failed") return { tone: "failed", label: "连接失败" };
    return { tone: "pending", label: "待检测" };
  };
  const configuredCount = items.filter(sshReady).length;

  const columns = [
    {
      title: "资产",
      key: "asset",
      width: 230,
      render: (_, row) => {
        const engine = dbTypeKey(row);
        return (
          <div className="ssh-assets-identity">
            <span className={`ssh-assets-engine-icon is-${engine}`}><DatabaseOutlined /></span>
            <div className="ssh-assets-identity-copy">
              <strong title={row.instance_name || row.host}>{row.instance_name || row.host || "未命名实例"}</strong>
              <span>
                <span className={`ssh-assets-engine is-${engine}`}>{dbTypeLabel(row)}</span>
                <span className="ssh-assets-record-id">#{row.id}</span>
              </span>
            </div>
          </div>
        );
      },
    },
    {
      title: "连接目标",
      key: "target",
      width: 310,
      render: (_, row) => {
        const sshEndpoint = row.ssh_host ? `${row.ssh_host}:${row.ssh_port || 22}` : "未配置 SSH 地址";
        const databaseEndpoint = row.host ? `${row.host}${row.port ? `:${row.port}` : ""}` : "未配置数据库地址";
        return (
          <div className="ssh-assets-endpoint">
            <strong className="ssh-assets-endpoint-primary" title={sshEndpoint}>
              <CloudServerOutlined />
              <span>{sshEndpoint}</span>
            </strong>
            <span className="ssh-assets-endpoint-secondary" title={databaseEndpoint}>
              数据库 {databaseEndpoint}
            </span>
          </div>
        );
      },
    },
    {
      title: "登录身份",
      key: "identity",
      width: 175,
      render: (_, row) => (
        <div className="ssh-assets-account">
          <strong><UserOutlined /> {row.ssh_username || "未配置账号"}</strong>
          <span>{envLabel(row)}</span>
        </div>
      ),
    },
    {
      title: "SSH 状态",
      key: "ssh_status",
      width: 145,
      render: (_, row) => {
        const state = sshState(row);
        return (
          <span className={`ssh-assets-status is-${state.tone}`}>
            <span className="ssh-assets-status-dot" />
            {state.label}
          </span>
        );
      },
    },
    {
      title: "操作",
      key: "actions",
      width: 190,
      align: "right",
      render: (_, row) => (
        <Space size={6} wrap={false} className="ssh-assets-actions">
          <Button
            size="small"
            className="ssh-assets-test-action"
            icon={<LinkOutlined />}
            disabled={!sshReady(row)}
            loading={actionIsLoading(row, "test")}
            onClick={() => runAction(row, sshApi.test, "连接测试", "test")}
          >
            测试连接
          </Button>
          <Tooltip title="采集系统信息">
            <Button
              size="small"
              aria-label="采集系统信息"
              icon={<InfoCircleOutlined />}
              disabled={!sshReady(row)}
              loading={actionIsLoading(row, "system-info")}
              onClick={() => runAction(row, sshApi.systemInfo, "系统信息", "system-info")}
            />
          </Tooltip>
          <Tooltip title="执行主机诊断">
            <Button
              size="small"
              aria-label="执行主机诊断"
              icon={<MedicineBoxOutlined />}
              disabled={!sshReady(row)}
              loading={actionIsLoading(row, "diagnose")}
              onClick={() => runAction(row, sshApi.diagnose, "主机诊断", "diagnose")}
            />
          </Tooltip>
        </Space>
      ),
    },
  ];

  return (
    <div className="ssh-assets-page">
      <section className="ssh-assets-overview" aria-labelledby="ssh-assets-heading">
        <div className="ssh-assets-overview-identity">
          <span className="ssh-assets-overview-icon"><CloudServerOutlined /></span>
          <div className="ssh-assets-overview-copy">
            <h2 id="ssh-assets-heading">SSH 资产清单</h2>
            <p>来源：数据库实例配置 · SSH 只读采集</p>
          </div>
        </div>
        <div className="ssh-assets-overview-actions">
          <span className="ssh-assets-readonly"><SafetyCertificateOutlined /> 只读操作</span>
          <span className="ssh-assets-count"><strong>{configuredCount}</strong> / {items.length} 台已配置</span>
          <Button icon={<ReloadOutlined />} loading={loading} onClick={load}>刷新</Button>
        </div>
      </section>

      <section className="ssh-assets-table-shell" aria-label="SSH 资产连接信息">
        <Table
          className="ssh-assets-table"
          rowKey="id"
          loading={loading}
          dataSource={items}
          columns={columns}
          size="middle"
          tableLayout="fixed"
          pagination={{ defaultPageSize: 12, showSizeChanger: false }}
          scroll={{ x: 1050 }}
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无 SSH 资产" /> }}
        />
      </section>

      <Modal
        className="ssh-assets-result-modal"
        title={resultModal.title}
        open={resultModal.open}
        onCancel={() => setResultModal({ open: false, title: "", data: null })}
        footer={<Button onClick={() => setResultModal({ open: false, title: "", data: null })}>关闭</Button>}
        width={760}
      >
        <pre className="ssh-assets-result-output">
          {JSON.stringify(resultModal.data, null, 2)}
        </pre>
      </Modal>
    </div>
  );
}

function AlertReportsPage() {
  const [events, setEvents] = useState([]);
  const [actions, setActions] = useState([]);
  const [stats, setStats] = useState({});
  const [loading, setLoading] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const [eventRes, statRes, actionRes] = await Promise.all([
        alertReportApi.events({ status: "all", limit: 300 }),
        alertReportApi.stats(),
        alertReportApi.actions({ limit: 120 }),
      ]);
      setEvents(eventRes.items || []);
      setStats(statRes || {});
      setActions(actionRes.items || []);
    } catch (e) {
      message.error(e.message || "加载告警报告失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  const importAlerts = async (type) => {
    setLoading(true);
    try {
      const res = type === "capacity" ? await alertReportApi.importCapacity() : await alertReportApi.importExternal();
      message.success(`已导入 ${res.imported ?? 0} 条告警`);
      await load();
    } catch (e) {
      message.error(e.message || "导入失败");
    } finally {
      setLoading(false);
    }
  };

  const closeEvent = async (row) => {
    setLoading(true);
    try {
      await alertReportApi.close(row.id, "告警报告中心关闭");
      message.success("告警已关闭");
      await load();
    } catch (e) {
      message.error(e.message || "关闭告警失败");
    } finally {
      setLoading(false);
    }
  };

  const createTicketPlan = async (row) => {
    try {
      await alertReportApi.action(row.id, {
        action_type: "ticket",
        target: "work_order",
        operator: "frontend",
        plan_only: true,
        request: { title: row.title, severity: row.severity, source: row.source },
      });
      message.success("已生成 plan_only 工单动作");
      await load();
    } catch (e) {
      message.error(e.message || "生成工单动作失败");
    }
  };

  return (
    <Space direction="vertical" size={16} className="full-width">
      <Alert
        type="info"
        showIcon
        message="告警报告中心"
        description="统一汇总容量、Zabbix、Prometheus、可观测采集告警，支持导入、关闭和 plan_only 工单动作留痕。"
      />
      <Row gutter={[16, 16]}>
        <Col xs={12} md={6}><Card size="small"><Statistic title="告警总数" value={stats.total || 0} /></Card></Col>
        <Col xs={12} md={6}><Card size="small"><Statistic title="触发中" value={stats.firing || 0} valueStyle={{ color: "#cf1322" }} /></Card></Col>
        <Col xs={12} md={6}><Card size="small"><Statistic title="已关闭" value={stats.closed || 0} /></Card></Col>
        <Col xs={12} md={6}><Card size="small"><Statistic title="已抑制" value={stats.suppressed || 0} /></Card></Col>
      </Row>
      <Card>
        <Space wrap>
          <Button type="primary" icon={<ReloadOutlined />} loading={loading} onClick={load}>刷新报告</Button>
          <Button loading={loading} onClick={() => importAlerts("capacity")}>导入容量告警</Button>
          <Button loading={loading} onClick={() => importAlerts("external")}>导入外部告警</Button>
        </Space>
      </Card>
      <Card title="告警事件" size="small">
        <Table
          rowKey="id"
          loading={loading}
          dataSource={events}
          size="small"
          pagination={{ pageSize: 10 }}
          scroll={{ x: 1500 }}
          columns={[
            { title: "级别", dataIndex: "severity", width: 90, render: (v) => <Tag color={v === "critical" || v === "high" ? "red" : v === "warning" ? "gold" : "blue"}>{v || "-"}</Tag> },
            { title: "状态", dataIndex: "status", width: 90, render: (v) => <Tag color={v === "firing" ? "red" : v === "closed" ? "default" : "blue"}>{v}</Tag> },
            { title: "来源", dataIndex: "source", width: 120, render: (v) => <Tag>{v}</Tag> },
            { title: "标题", dataIndex: "title", width: 320, ellipsis: true },
            { title: "对象", dataIndex: "object_name", width: 160, ellipsis: true },
            { title: "指标", dataIndex: "metric_name", width: 160, ellipsis: true },
            { title: "首次发现", dataIndex: "first_seen", width: 170, render: renderTime },
            { title: "最近发现", dataIndex: "last_seen", width: 170, render: renderTime },
            {
              title: "操作",
              width: 180,
              render: (_, row) => (
                <Space size={4} wrap>
                  <Button size="small" onClick={() => createTicketPlan(row)}>转工单</Button>
                  {row.status !== "closed" && <Button size="small" danger onClick={() => closeEvent(row)}>关闭</Button>}
                </Space>
              ),
            },
          ]}
        />
      </Card>
      <Card title="动作留痕" size="small">
        <Table
          rowKey="id"
          loading={loading}
          dataSource={actions}
          size="small"
          pagination={{ pageSize: 8 }}
          columns={[
            { title: "事件ID", dataIndex: "event_id", width: 90 },
            { title: "动作", dataIndex: "action_type", width: 120, render: (v) => <Tag>{v}</Tag> },
            { title: "状态", dataIndex: "action_status", width: 120, render: (v) => <Tag color={v === "success" ? "green" : v === "plan_only" ? "blue" : "orange"}>{v}</Tag> },
            { title: "目标", dataIndex: "target", width: 160 },
            { title: "时间", dataIndex: "create_time", width: 170, render: renderTime },
          ]}
        />
      </Card>
    </Space>
  );
}


function AiConfigPage() {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [editing, setEditing] = useState(null);
  const [form] = Form.useForm();

  const loadData = async () => {
    setLoading(true);
    try {
      const res = await aiConfigApi.list();
      setItems(res.items || []);
    } catch (e) {
      message.error(e.message || "加载AI配置失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { loadData(); }, []);

  const openCreate = () => {
    setEditing(null);
    form.resetFields();
    form.setFieldsValue({ provider_type: "openai_compatible", timeout_seconds: 60, enabled: 1, is_default: 0 });
    setDrawerOpen(true);
  };

  const openEdit = (row) => {
    setEditing(row);
    form.setFieldsValue({
      provider_name: row.provider_name,
      provider_type: row.provider_type,
      base_url: row.base_url,
      model_name: row.model_name,
      timeout_seconds: row.timeout_seconds,
      enabled: row.enabled,
      is_default: row.is_default,
      remark: row.remark,
    });
    setDrawerOpen(true);
  };

  const saveConfig = async () => {
    const values = await form.validateFields();
    try {
      if (editing) {
        await aiConfigApi.update(editing.id, values);
        message.success("AI配置已更新");
      } else {
        await aiConfigApi.create(values);
        message.success("AI配置已创建");
      }
      setDrawerOpen(false);
      await loadData();
    } catch (e) {
      message.error(e.message || "保存失败");
    }
  };

  const testConfig = async () => {
    try {
      const payload = await form.validateFields();
      const res = await aiConfigApi.test(payload);
      if (res.ok) {
        message.success(res.message || "AI连接成功");
      } else {
        message.error(res.message || "AI连接失败");
      }
    } catch (e) {
      message.error(e.message || "AI连接测试失败");
    }
  };

  const deleteConfig = async (row) => {
    try {
      await aiConfigApi.remove(row.id);
      message.success("已删除");
      await loadData();
    } catch (e) {
      message.error(e.message || "删除失败");
    }
  };

  return (
    <Card title="AI 配置" extra={<Button type="primary" onClick={openCreate}>新增AI配置</Button>}>
      <Alert type="info" showIcon style={{ marginBottom: 16 }}
        message="配置 AI Base URL、API Key 和模型名称后，日常运维的一键巡检AI分析才会调用大模型。未配置时仅使用本地规则诊断。" />
      <Table rowKey="id" loading={loading} dataSource={items} columns={[
        { title: "配置名称", dataIndex: "provider_name" },
        { title: "类型", dataIndex: "provider_type", render: v => <Tag>{v}</Tag> },
        { title: "Base URL", dataIndex: "base_url" },
        { title: "模型", dataIndex: "model_name" },
        { title: "Key", dataIndex: "has_api_key", render: v => v ? <Tag color="green">已配置</Tag> : <Tag>未配置</Tag> },
        { title: "默认", dataIndex: "is_default", render: v => v ? <Tag color="blue">默认</Tag> : "-" },
        { title: "启用", dataIndex: "enabled", render: v => v ? <Tag color="green">启用</Tag> : <Tag color="red">停用</Tag> },
        { title: "操作", render: (_, row) => (
          <Space>
            <Button size="small" onClick={() => openEdit(row)}>编辑</Button>
            <Button size="small" danger onClick={() => deleteConfig(row)}>删除</Button>
          </Space>
        )},
      ]} />
      <Modal title={editing ? "编辑AI配置" : "新增AI配置"} open={drawerOpen} onCancel={() => setDrawerOpen(false)} width={520} centered
        footer={<Space><Button onClick={testConfig}>测试连接</Button><Button type="primary" onClick={saveConfig}>保存</Button><Button onClick={() => setDrawerOpen(false)}>取消</Button></Space>}>
        <Form form={form} layout="vertical">
          <Form.Item label="配置名称" name="provider_name" rules={[{ required: true, message: "请输入配置名称" }]}>
            <Input placeholder="例如：DeepSeek / OpenAI / 本地Ollama" />
          </Form.Item>
          <Form.Item label="Provider类型" name="provider_type" rules={[{ required: true }]}>
            <Select options={[{ label: "OpenAI兼容接口", value: "openai_compatible" }, { label: "Ollama本地模型", value: "ollama" }, { label: "自定义接口", value: "custom" }]} />
          </Form.Item>
          <Form.Item label="Base URL" name="base_url" rules={[{ required: true, message: "请输入Base URL" }]}>
            <Input placeholder="https://api.deepseek.com/v1" />
          </Form.Item>
          <Form.Item label="API Key" name="api_key" extra={editing ? "不填写则保持原 Key 不变" : ""}>
            <Input.Password placeholder="请输入API Key" />
          </Form.Item>
          <Form.Item label="模型名称" name="model_name" rules={[{ required: true, message: "请输入模型名称" }]}>
            <Input placeholder="deepseek-chat / gpt-4o-mini / qwen2.5:14b" />
          </Form.Item>
          <Form.Item label="超时时间（秒）" name="timeout_seconds">
            <InputNumber min={5} max={300} style={{ width: "100%" }} />
          </Form.Item>
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item label="是否启用" name="enabled">
                <BinaryStatusSwitch />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item label="是否默认" name="is_default">
                <BinaryStatusSwitch checkedChildren="默认" unCheckedChildren="非默认" />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item label="备注" name="remark">
            <Input.TextArea rows={3} />
          </Form.Item>
        </Form>
      </Modal>
    </Card>
  );
}

export { SystemParamsPage, SshAssetsPage, AlertReportsPage, AiConfigPage };
