import React, { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Row,
  Select,
  Space,
  Statistic,
  Switch,
  Table,
  Tabs,
  Tag,
  Timeline,
  Tooltip,
  Typography,
  message,
} from "antd";
import {
  CheckCircleOutlined,
  DeleteOutlined,
  DownloadOutlined,
  EyeOutlined,
  PauseCircleOutlined,
  PlusOutlined,
  ReloadOutlined,
  UserSwitchOutlined,
} from "@ant-design/icons";
import { ItsmTicketDrawer } from "./framework/integration";
import { sendAlertNotification } from "./modules/workflowApi";
import {
  createMaintenanceWindow,
  createUnifiedAlertAction,
  deleteMaintenanceWindow,
  diagnoseUnifiedAlertEvent,
  getUnifiedAlertEvidenceChain,
  getUnifiedAlertBundle,
  getUnifiedAlertDetail,
  getUnifiedAlertMetricTrends,
  getUnifiedAlertTicketDescription,
  importCapacityAlerts,
  importExternalAlerts,
  isAlertIntegrationLocked,
  transitionUnifiedAlertEvent,
  updateMaintenanceWindow,
} from "./modules/unifiedAlertEventsApi";

const { Paragraph, Text } = Typography;

export function UnifiedAlertEventPage({ instances }) {
  const [events, setEvents] = useState([]);
  const [actions, setActions] = useState([]);
  const [stats, setStats] = useState({});
  const [zabbixProblems, setZabbixProblems] = useState([]);
  const [promAlerts, setPromAlerts] = useState([]);
  const [pushLogs, setPushLogs] = useState([]);
  const [maintenanceWindows, setMaintenanceWindows] = useState([]);
  // 外部告警源（Zabbix / Prometheus / 推送日志）被版本门禁锁定时为 true：
  // 页面只保留平台内告警，并就地说明"属企业版"，不把 403 文案当业务错误弹出。
  const [integrationLocked, setIntegrationLocked] = useState(false);
  const [loading, setLoading] = useState(false);
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailTitle, setDetailTitle] = useState("");
  const [detailContent, setDetailContent] = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [ticketSource, setTicketSource] = useState(null);
  const [notifyingId, setNotifyingId] = useState(null);
  const [eventDetail, setEventDetail] = useState(null);
  const [lifecycleOpen, setLifecycleOpen] = useState(false);
  const [transitionOpen, setTransitionOpen] = useState(false);
  const [transitionEvent, setTransitionEvent] = useState(null);
  const [transitionSubmitting, setTransitionSubmitting] = useState(false);
  const [windowOpen, setWindowOpen] = useState(false);
  const [editingWindow, setEditingWindow] = useState(null);
  const [windowSubmitting, setWindowSubmitting] = useState(false);
  const [transitionForm] = Form.useForm();
  const [windowForm] = Form.useForm();
  const transitionAction = Form.useWatch("action", transitionForm);

  const instanceName = (id) => instances.find((item) => item.id === id)?.instance_name || id || "-";
  const severityColor = (value) => {
    const key = String(value || "").toLowerCase();
    if (["critical", "disaster", "high", "严重", "灾难"].includes(key)) return "red";
    if (["warning", "average", "medium", "告警", "警告"].includes(key)) return "orange";
    if (["info", "information", "low"].includes(key)) return "blue";
    return "default";
  };
  const statusColor = (value) => {
    if (value === "firing") return "red";
    if (value === "closed" || value === "resolved") return "green";
    if (value === "suppressed") return "orange";
    return "default";
  };

  const load = async () => {
    setLoading(true);
    try {
      const [eventRes, statRes, actionRes, zbx, prom, logs, windows] = await getUnifiedAlertBundle();
      setEvents(eventRes.items || []);
      setStats(statRes || {});
      setActions(actionRes.items || []);
      setZabbixProblems(zbx.items || []);
      setPromAlerts(prom.items || []);
      setPushLogs(logs.items || []);
      setMaintenanceWindows(windows.items || []);
      setIntegrationLocked(isAlertIntegrationLocked(zbx, prom, logs));
    } catch (e) { message.error(e.message); }
    finally { setLoading(false); }
  };

  useEffect(() => { load(); }, []);

  const importCapacity = async () => {
    const res = await importCapacityAlerts();
    message.success(`已导入容量告警 ${res.imported || 0} 条`);
    await load();
  };

  const importExternal = async () => {
    const res = await importExternalAlerts();
    message.success(`已导入外部告警 ${res.imported || 0} 条`);
    await load();
  };

  const openLifecycle = async (event) => {
    setLifecycleOpen(true);
    setEventDetail(null);
    try {
      setEventDetail(await getUnifiedAlertDetail(event.id));
    } catch (error) {
      message.error(error.message || "事件详情加载失败");
    }
  };

  const openTransition = (event, action = "acknowledge") => {
    setTransitionEvent(event);
    transitionForm.setFieldsValue({ action, assignee: event.assigned_to, suppress_minutes: 60, reason: "" });
    setTransitionOpen(true);
  };

  const submitTransition = async () => {
    const values = await transitionForm.validateFields();
    setTransitionSubmitting(true);
    try {
      await transitionUnifiedAlertEvent(transitionEvent.id, values);
      message.success("事件状态已更新");
      setTransitionOpen(false);
      await load();
    } catch (error) {
      message.error(error.message || "事件处理失败");
    } finally {
      setTransitionSubmitting(false);
    }
  };

  const openWindow = (windowItem = null) => {
    const toLocalInput = (value) => value ? String(value).slice(0, 19) : undefined;
    setEditingWindow(windowItem);
    windowForm.setFieldsValue(windowItem ? {
      ...windowItem,
      starts_at: toLocalInput(windowItem.starts_at),
      ends_at: toLocalInput(windowItem.ends_at),
      enabled: Boolean(windowItem.enabled),
    } : {
      timezone: "Asia/Shanghai",
      enabled: true,
    });
    setWindowOpen(true);
  };

  const submitWindow = async () => {
    const values = await windowForm.validateFields();
    setWindowSubmitting(true);
    try {
      if (editingWindow) await updateMaintenanceWindow(editingWindow.id, values);
      else await createMaintenanceWindow(values);
      message.success(editingWindow ? "维护窗口已更新" : "维护窗口已创建");
      setWindowOpen(false);
      await load();
    } catch (error) {
      message.error(error.message || "维护窗口保存失败");
    } finally {
      setWindowSubmitting(false);
    }
  };

  const removeWindow = async (windowId) => {
    try {
      await deleteMaintenanceWindow(windowId);
      message.success("维护窗口已删除");
      await load();
    } catch (error) {
      message.error(error.message || "维护窗口删除失败");
    }
  };

  const createAction = async (event, actionType) => {
    await createUnifiedAlertAction(event, actionType);
    message.success("已生成 plan_only 动作日志");
    await load();
  };

  const notifyEvent = async (event) => {
    setNotifyingId(event.id);
    try {
      const result = await sendAlertNotification(event);
      const delivery = result.delivery || {};
      const sentCount = Number(delivery.sent_count) || 0;
      const failedCount = Number(delivery.failed_count) || 0;
      const pendingCount = Number(delivery.pending_count) || 0;
      const cancelledCount = Number(delivery.cancelled_count) || 0;
      const unknownCount = Number(delivery.unknown_count) || 0;
      if (failedCount + pendingCount + cancelledCount + unknownCount > 0) {
        const summary = `通知已发送 ${sentCount} 条，失败 ${failedCount} 条，待处理 ${pendingCount} 条，已取消 ${cancelledCount} 条，结果未知 ${unknownCount} 条`;
        if (failedCount > 0 && sentCount === 0 && pendingCount === 0) message.error(summary);
        else message.warning(summary);
      } else if (sentCount > 0) message.success(`通知发送完成，共 ${sentCount} 条`);
      else message.warning("通知事件已登记，本次没有新增投递；请查看发送日志和通知策略");
      await load();
    } catch (error) {
      if (error?.editionLocked) message.info("发送通知属集成中心，需要企业版");
      else message.error(error.message || "通知发送失败");
    } finally {
      setNotifyingId(null);
    }
  };

  const openTicket = (event) => {
    setTicketSource({
      sourceType: "ALERT",
      sourceId: event.id,
      sourceRefId: event.id,
      instanceId: event.instance_id,
      instanceName: instanceName(event.instance_id),
      dbType: event.db_type,
      title: event.title || "数据库告警",
      description: [event.message, event.description, event.object_name && `对象：${event.object_name}`, event.metric_name && `指标：${event.metric_name}`].filter(Boolean).join("\n"),
      priority: String(event.severity || "").toLowerCase() === "critical" ? "P1" : "P2",
      severity: event.severity || "warning",
    });
  };

  const showEvidence = async (event) => {
    setDetailOpen(true);
    setDetailTitle(`证据链 - ${event.title}`);
    setDetailLoading(true);
    try {
      const [chain, trends] = await Promise.all([
        getUnifiedAlertEvidenceChain(event.id),
        getUnifiedAlertMetricTrends(event.id),
      ]);
      setDetailContent({ mode: "evidence", chain, trends });
    } catch (e) {
      message.error(e.message);
    } finally {
      setDetailLoading(false);
    }
  };

  const runDiagnosis = async (event) => {
    setDetailOpen(true);
    setDetailTitle(`诊断分析 - ${event.title}`);
    setDetailLoading(true);
    try {
      const incident = await diagnoseUnifiedAlertEvent(event.id);
      setDetailContent({ mode: "diagnosis", incident });
      message.success("诊断分析已生成");
      await load();
    } catch (e) {
      message.error(e.message);
      setDetailContent({ mode: "error", message: e.message });
    } finally {
      setDetailLoading(false);
    }
  };

  const showTicketDescription = async (event) => {
    setDetailOpen(true);
    setDetailTitle(`工单描述 - ${event.title}`);
    setDetailLoading(true);
    try {
      const ticket = await getUnifiedAlertTicketDescription(event.id);
      setDetailContent({ mode: "ticket", ticket });
    } catch (e) {
      message.error(e.message);
    } finally {
      setDetailLoading(false);
    }
  };

  const eventColumns = [
    { title: "来源", dataIndex: "source", width: 110, render: (v, r) => <Tooltip title={`已聚合 ${r.source_count || 1} 个来源`}><Tag>{v}</Tag></Tooltip> },
    { title: "级别", dataIndex: "severity", width: 100, render: (v) => <Tag color={severityColor(v)}>{v || "-"}</Tag> },
    { title: "状态", dataIndex: "status", width: 100, render: (v) => <Tag color={statusColor(v)}>{v}</Tag> },
    { title: "实例", dataIndex: "instance_id", width: 150, render: instanceName },
    { title: "标题", dataIndex: "title", ellipsis: true },
    { title: "对象", dataIndex: "object_name", width: 180, ellipsis: true },
    { title: "发生", dataIndex: "occurrence_count", width: 78, render: (v) => `${v || 1} 次` },
    { title: "负责人", dataIndex: "assigned_to", width: 110, render: (v) => v || "未分派" },
    { title: "SLA", dataIndex: "sla_status", width: 90, render: (v, r) => <Tooltip title={r.sla_due_at ? `截止：${r.sla_due_at}` : "未设置 SLA"}><Tag color={v === "breached" ? "red" : v === "active" ? "blue" : "default"}>{v === "breached" ? "已超时" : v === "active" ? "计时中" : "已停止"}</Tag></Tooltip> },
    { title: "最近发生", dataIndex: "last_seen", width: 180 },
    { title: "操作", fixed: "right", width: 300, render: (_, r) => <Space size={4}>
      <Tooltip title="查看生命周期与来源"><Button size="small" icon={<EyeOutlined />} onClick={() => openLifecycle(r)} /></Tooltip>
      <Button size="small" icon={<CheckCircleOutlined />} disabled={["closed", "resolved"].includes(r.status)} onClick={() => openTransition(r)}>处理</Button>
      <Button size="small" type="primary" disabled={!r.instance_id} onClick={() => runDiagnosis(r)}>诊断</Button>
      <Button size="small" onClick={() => showEvidence(r)}>证据</Button>
      <Button size="small" loading={notifyingId === r.id} disabled={integrationLocked}
        title={integrationLocked ? "发送通知属集成中心，开源版不可用" : undefined}
        onClick={() => notifyEvent(r)}>通知</Button>
    </Space> },
  ];

  const windowColumns = [
    { title: "窗口", dataIndex: "name", width: 180 },
    { title: "实例", dataIndex: "instance_id", width: 150, render: (value) => value ? instanceName(value) : "全部实例" },
    { title: "来源", dataIndex: "source", width: 110, render: (value) => value || "全部" },
    { title: "开始", dataIndex: "starts_at", width: 180 },
    { title: "结束", dataIndex: "ends_at", width: 180 },
    { title: "状态", dataIndex: "enabled", width: 90, render: (value) => <Tag color={value ? "green" : "default"}>{value ? "启用" : "停用"}</Tag> },
    { title: "原因", dataIndex: "reason", ellipsis: true },
    { title: "操作", fixed: "right", width: 120, render: (_, row) => <Space>
      <Button size="small" onClick={() => openWindow(row)}>编辑</Button>
      <Popconfirm title="删除这个维护窗口？" onConfirm={() => removeWindow(row.id)}><Button size="small" danger icon={<DeleteOutlined />} /></Popconfirm>
    </Space> },
  ];

  const actionColumns = [
    { title: "事件", dataIndex: "event_id", width: 90 },
    { title: "动作", dataIndex: "action_type", width: 130, render: (v) => <Tag>{v}</Tag> },
    { title: "状态", dataIndex: "action_status", width: 120, render: (v) => <Tag color={v === "success" ? "green" : "blue"}>{v}</Tag> },
    { title: "目标", dataIndex: "target", width: 160 },
    { title: "操作人", dataIndex: "operator", width: 120 },
    { title: "时间", dataIndex: "create_time", width: 180 },
    { title: "错误", dataIndex: "error_message", ellipsis: true },
  ];

  return (
    <Space direction="vertical" size={16} className="full-width">
      <Card title="统一告警事件" extra={<Space>
        <Button icon={<ReloadOutlined />} loading={loading} onClick={load}>刷新</Button>
        <Button icon={<DownloadOutlined />} onClick={importCapacity}>导入容量</Button>
        {!integrationLocked && <Button icon={<DownloadOutlined />} onClick={importExternal}>导入外部监控</Button>}
      </Space>}>
        <Row gutter={12}>
          <Col xs={12} md={4}><Statistic title="统一事件" value={stats.total || events.length} /></Col>
          <Col xs={12} md={4}><Statistic title="活动告警" value={stats.active || 0} /></Col>
          <Col xs={12} md={4}><Statistic title="已确认" value={stats.acknowledged || 0} /></Col>
          <Col xs={12} md={4}><Statistic title="已分派" value={stats.assigned || 0} /></Col>
          <Col xs={12} md={4}><Statistic title="SLA 超时" value={stats.sla_breached || 0} valueStyle={{ color: stats.sla_breached ? "#cf1322" : undefined }} /></Col>
          <Col xs={12} md={4}><Statistic title="动作日志" value={actions.length} /></Col>
        </Row>
      </Card>
      {integrationLocked && (
        <Alert type="info" showIcon message="外部告警接入、通知与工单为企业版功能"
          description="Zabbix、Prometheus、推送日志、发送通知与工单管理属集成中心，开源版不可用；本页展示平台内产生的统一告警事件，诊断、证据与处置照常可用。" />
      )}
      <Tabs items={[
        { key: "events", label: `统一事件 ${events.length}`, children: <Table rowKey="id" size="small" loading={loading} dataSource={events} columns={eventColumns} scroll={{ x: 1450 }} /> },
        { key: "maintenance", label: `维护窗口 ${maintenanceWindows.length}`, children: <Space direction="vertical" size={12} className="full-width">
          <div className="toolbar-row"><Button type="primary" icon={<PlusOutlined />} onClick={() => openWindow()}>新建维护窗口</Button></div>
          <Table rowKey="id" size="small" dataSource={maintenanceWindows} columns={windowColumns} scroll={{ x: 1100 }} />
        </Space> },
        { key: "actions", label: "动作日志", children: <Table rowKey="id" size="small" dataSource={actions} columns={actionColumns} /> },
        // 外部告警源属「集成中心」，开源版被版本门禁挡下：整块隐藏，并用下方提示说明原因，
        // 避免把 403 文案当成页面加载失败弹出来。
        ...(integrationLocked ? [] : [
        { key: "zabbix", label: "Zabbix 缓存", children: <Table rowKey={(r) => r.id || r.eventid} size="small" dataSource={zabbixProblems} columns={[
          { title: "主机", dataIndex: "host_name" },
          { title: "问题", dataIndex: "problem_name" },
          { title: "级别", dataIndex: "severity_name", width: 100 },
          { title: "状态", dataIndex: "status", width: 100, render: (v) => <Tag color={v === "firing" ? "red" : "green"}>{v}</Tag> },
          { title: "同步时间", dataIndex: "sync_time", width: 180 },
        ]} /> },
        { key: "prometheus", label: "Prometheus 缓存", children: <Table rowKey="id" size="small" dataSource={promAlerts} columns={[
          { title: "告警", dataIndex: "alert_name" },
          { title: "实例", dataIndex: "instance_name" },
          { title: "级别", dataIndex: "severity", width: 100 },
          { title: "状态", dataIndex: "alert_status", width: 100, render: (v) => <Tag color={v === "firing" ? "red" : "green"}>{v}</Tag> },
          { title: "开始时间", dataIndex: "starts_at", width: 180 },
        ]} /> },
        { key: "push", label: "推送日志", children: <Table rowKey="id" size="small" dataSource={pushLogs} columns={[
          { title: "告警 ID", dataIndex: "alert_id" },
          { title: "Zabbix Key", dataIndex: "zabbix_key" },
          { title: "状态", dataIndex: "push_status", width: 100, render: (v) => <Tag color={v === "success" ? "green" : "red"}>{v}</Tag> },
          { title: "时间", dataIndex: "push_time", width: 180 },
          { title: "错误", dataIndex: "error_message" },
        ]} /> },
        ]),
      ]} />
      <Modal
        className="adaptive-center-modal"
        title="事件处理"
        open={transitionOpen}
        centered
        width={540}
        okText="确认处理"
        cancelText="取消"
        confirmLoading={transitionSubmitting}
        onOk={submitTransition}
        onCancel={() => setTransitionOpen(false)}
      >
        <Alert type="info" showIcon message={transitionEvent?.title || "告警事件"} description={`当前状态：${transitionEvent?.status || "-"}`} />
        <Form form={transitionForm} layout="vertical" style={{ marginTop: 16 }}>
          <Form.Item name="action" label="处理动作" rules={[{ required: true, message: "请选择处理动作" }]}>
            <Select options={[
              { value: "acknowledge", label: "确认告警" },
              { value: "assign", label: "分派负责人" },
              { value: "suppress", label: "临时抑制" },
              { value: "recover", label: "标记恢复" },
              { value: "resolve", label: "解决事件" },
              { value: "close", label: "关闭事件" },
              { value: "reopen", label: "重新打开" },
            ]} />
          </Form.Item>
          {transitionAction === "assign" && <Form.Item name="assignee" label="负责人" rules={[{ required: true, message: "请输入负责人" }]}><Input prefix={<UserSwitchOutlined />} maxLength={100} /></Form.Item>}
          {transitionAction === "suppress" && <Form.Item name="suppress_minutes" label="抑制时长（分钟）" rules={[{ required: true, message: "请输入抑制时长" }]}><InputNumber min={1} max={525600} className="full-width" prefix={<PauseCircleOutlined />} /></Form.Item>}
          <Form.Item name="reason" label="处理说明" rules={[{ required: ["suppress", "resolve", "close", "reopen"].includes(transitionAction), message: "请填写处理说明" }]}><Input.TextArea rows={3} maxLength={500} showCount /></Form.Item>
        </Form>
      </Modal>
      <Modal
        className="adaptive-center-modal"
        title={editingWindow ? "编辑维护窗口" : "新建维护窗口"}
        open={windowOpen}
        centered
        width={700}
        okText="保存"
        cancelText="取消"
        confirmLoading={windowSubmitting}
        onOk={submitWindow}
        onCancel={() => setWindowOpen(false)}
      >
        <Form form={windowForm} layout="vertical">
          <Row gutter={16}>
            <Col span={16}><Form.Item name="name" label="窗口名称" rules={[{ required: true, message: "请输入窗口名称" }]}><Input maxLength={200} /></Form.Item></Col>
            <Col span={8}><Form.Item name="enabled" label="启用" valuePropName="checked"><Switch /></Form.Item></Col>
            <Col span={12}><Form.Item name="starts_at" label="开始时间" rules={[{ required: true, message: "请选择开始时间" }]}><Input type="datetime-local" /></Form.Item></Col>
            <Col span={12}><Form.Item name="ends_at" label="结束时间" rules={[{ required: true, message: "请选择结束时间" }]}><Input type="datetime-local" /></Form.Item></Col>
            <Col span={12}><Form.Item name="instance_id" label="适用实例"><Select allowClear showSearch optionFilterProp="label" placeholder="全部实例" options={instances.map((item) => ({ value: item.id, label: item.instance_name }))} /></Form.Item></Col>
            <Col span={12}><Form.Item name="source" label="告警来源"><Select allowClear placeholder="全部来源" options={["zabbix", "prometheus", "capacity", "observability", "backup", "manual"].map((value) => ({ value, label: value }))} /></Form.Item></Col>
            <Col span={12}><Form.Item name="object_type" label="对象类型"><Input maxLength={100} placeholder="可留空" /></Form.Item></Col>
            <Col span={12}><Form.Item name="object_name_pattern" label="对象名称匹配"><Input maxLength={300} placeholder="支持通配模式" /></Form.Item></Col>
            <Col span={12}><Form.Item name="metric_name_pattern" label="指标匹配"><Input maxLength={200} placeholder="支持通配模式" /></Form.Item></Col>
            <Col span={12}><Form.Item name="timezone" label="时区" rules={[{ required: true }]}><Input maxLength={50} /></Form.Item></Col>
            <Col span={24}><Form.Item name="reason" label="维护说明"><Input.TextArea rows={3} maxLength={500} showCount /></Form.Item></Col>
          </Row>
        </Form>
      </Modal>
      <Modal
        className="adaptive-center-modal"
        title="事件详情"
        open={lifecycleOpen}
        centered
        width={920}
        footer={eventDetail ? <Space wrap>
          <Button onClick={() => showTicketDescription(eventDetail)}>工单描述</Button>
          <Button onClick={() => openTicket(eventDetail)} disabled={integrationLocked}
            title={integrationLocked ? "工单管理属集成中心，开源版不可用" : undefined}>创建工单</Button>
          <Button onClick={() => createAction(eventDetail, "zabbix_push")}>生成推送动作</Button>
          <Button type="primary" onClick={() => { setLifecycleOpen(false); openTransition(eventDetail); }}>处理事件</Button>
        </Space> : null}
        onCancel={() => setLifecycleOpen(false)}
      >
        {!eventDetail && <Alert type="info" showIcon message="正在加载事件详情..." />}
        {eventDetail && <Space direction="vertical" size={16} className="full-width">
          <Descriptions size="small" bordered column={3} items={[
            { key: "status", label: "状态", children: <Tag color={statusColor(eventDetail.status)}>{eventDetail.status}</Tag> },
            { key: "severity", label: "级别", children: <Tag color={severityColor(eventDetail.severity)}>{eventDetail.severity}</Tag> },
            { key: "instance", label: "实例", children: instanceName(eventDetail.instance_id) },
            { key: "occurrence", label: "发生次数", children: eventDetail.occurrence_count || 1 },
            { key: "sources", label: "聚合来源", children: eventDetail.source_count || 1 },
            { key: "assignee", label: "负责人", children: eventDetail.assigned_to || "未分派" },
            { key: "first", label: "首次发生", children: eventDetail.first_seen || "-" },
            { key: "last", label: "最近发生", children: eventDetail.last_seen || "-" },
            { key: "sla", label: "SLA", children: eventDetail.sla_breached_at ? <Tag color="red">已超时</Tag> : eventDetail.sla_due_at || "未设置" },
            { key: "title", label: "标题", span: 3, children: eventDetail.title },
            { key: "message", label: "详情", span: 3, children: eventDetail.message || "-" },
          ]} />
          <Tabs size="small" items={[
            { key: "timeline", label: `生命周期 ${eventDetail.lifecycle?.length || 0}`, children: <Timeline items={(eventDetail.lifecycle || []).map((item) => ({
              color: item.action === "sla_breached" ? "red" : item.to_status === "resolved" || item.to_status === "closed" ? "green" : "blue",
              children: <div><Text strong>{item.action}</Text><Text type="secondary"> {item.from_status || "-"} → {item.to_status || "-"} · {item.operator || "system"} · {item.create_time}</Text>{item.reason && <div>{item.reason}</div>}</div>,
            }))} /> },
            { key: "sources", label: `来源明细 ${eventDetail.sources?.length || 0}`, children: <Table rowKey="id" size="small" pagination={false} dataSource={eventDetail.sources || []} columns={[
              { title: "来源", dataIndex: "source", width: 120, render: (value) => <Tag>{value}</Tag> },
              { title: "来源事件", dataIndex: "source_event_uid", ellipsis: true },
              { title: "发生次数", dataIndex: "occurrence_count", width: 100 },
              { title: "首次", dataIndex: "first_seen", width: 180 },
              { title: "最近", dataIndex: "last_seen", width: 180 },
            ]} /> },
          ]} />
        </Space>}
      </Modal>
      <Modal
        className="adaptive-center-modal"
        title={detailTitle}
        open={detailOpen}
        width={860}
        centered
        footer={null}
        onCancel={() => setDetailOpen(false)}
      >
        {detailLoading && <Alert type="info" showIcon message="正在加载..." />}
        {!detailLoading && detailContent?.mode === "evidence" && (
          <Space direction="vertical" size={12} className="full-width">
            <Alert type="success" showIcon message={detailContent.chain?.summary || "证据链已生成"} />
            <Card size="small" title="根因判断">
              <Paragraph strong>{detailContent.chain?.root_cause?.summary}</Paragraph>
              <Text type="secondary">{detailContent.chain?.root_cause?.suggestion}</Text>
            </Card>
            <Card size="small" title="证据">
              <Table
                rowKey={(row) => row.id || [
                  row.source,
                  row.layer,
                  row.metric_name,
                  row.phenomenon,
                  row.evidence,
                  row.metric_time,
                ].filter(Boolean).join("|")}
                size="small"
                pagination={false}
                dataSource={detailContent.chain?.evidence || []}
                columns={[
                  { title: "来源", dataIndex: "source", width: 110, render: (v) => <Tag>{v}</Tag> },
                  { title: "层级", dataIndex: "layer", width: 100 },
                  { title: "级别", dataIndex: "severity", width: 90, render: (v) => <Tag color={severityColor(v)}>{v}</Tag> },
                  { title: "现象", dataIndex: "phenomenon", ellipsis: true },
                  { title: "证据", dataIndex: "evidence", ellipsis: true },
                ]}
              />
            </Card>
            <Card size="small" title="关联指标趋势">
              <pre className="ops-output">{JSON.stringify(detailContent.trends?.series || {}, null, 2)}</pre>
            </Card>
          </Space>
        )}
        {!detailLoading && detailContent?.mode === "diagnosis" && (
          <Space direction="vertical" size={12} className="full-width">
            <Alert type="success" showIcon message={`诊断事件：${detailContent.incident?.incident_no || detailContent.incident?.id}`} />
            <Card size="small" title="诊断摘要">
              <Paragraph>{detailContent.incident?.root_cause_summary || detailContent.incident?.ai_summary || "-"}</Paragraph>
              <pre className="ops-output">{JSON.stringify(detailContent.incident?.evidences || [], null, 2)}</pre>
            </Card>
          </Space>
        )}
        {!detailLoading && detailContent?.mode === "ticket" && (
          <Space direction="vertical" size={12} className="full-width">
            <Alert type="info" showIcon message={detailContent.ticket?.title} description="以下为 plan_only 工单描述，可复制到 ITSM 工单系统。" />
            <pre className="ops-output">{detailContent.ticket?.description}</pre>
          </Space>
        )}
        {!detailLoading && detailContent?.mode === "error" && (
          <Alert type="warning" showIcon message={detailContent.message} />
        )}
      </Modal>
      <ItsmTicketDrawer
        open={Boolean(ticketSource)}
        source={ticketSource}
        onClose={() => setTicketSource(null)}
        onCreated={load}
      />
    </Space>
  );
}
