import React, { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Collapse,
  Input,
  Popconfirm,
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
import { AlertOutlined, ReloadOutlined } from "@ant-design/icons";
import {
  closeCapacityAlert,
  closeCapacityAlerts,
  getCapacityAlertBundle,
  getCapacityAlertRuleBundle,
  isolateCapacityAlert,
  setCapacityAlertRuleEnabled,
} from "./modules/capacityAlertsApi";

const { Text } = Typography;

export function AlertCenterPage({ instances, selectedInstanceId }) {
  const [alerts, setAlerts] = useState([]);
  const [stats, setStats] = useState({ total: 0, open: 0, critical: 0, warning: 0, closed: 0, isolated: 0 });
  const [rules, setRules] = useState([]);
  const [ruleStats, setRuleStats] = useState({ total: 0, enabled: 0, disabled: 0, critical: 0, warning: 0, types: [] });
  const [loading, setLoading] = useState(false);
  const [ruleLoading, setRuleLoading] = useState(false);
  const [statusFilter, setStatusFilter] = useState("open");
  const [instanceFilter, setInstanceFilter] = useState(null);

  useEffect(() => {
    if (selectedInstanceId && instances.some((item) => item.id === selectedInstanceId)) {
      setInstanceFilter(selectedInstanceId);
    }
  }, [selectedInstanceId, instances]);

  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [ruleTypeFilter, setRuleTypeFilter] = useState(null);
  const [ruleLevelFilter, setRuleLevelFilter] = useState(null);
  const [ruleEnabledFilter, setRuleEnabledFilter] = useState("enabled");
  const [ruleKeyword, setRuleKeyword] = useState("");
  const [ruleExpanded, setRuleExpanded] = useState(false);

  const loadData = async () => {
    setLoading(true);
    try {
      const [alertData, statsData] = await getCapacityAlertBundle({
        status: statusFilter,
        instanceId: instanceFilter,
        dateFrom,
        dateTo,
      });
      setAlerts(alertData.items || []);
      setStats(statsData);
    } catch (e) { message.error(e.message); }
    finally { setLoading(false); }
  };

  useEffect(() => { loadData(); }, [statusFilter, instanceFilter, dateFrom, dateTo]);

  const loadRules = async () => {
    setRuleLoading(true);
    try {
      const [ruleData, ruleStatsData] = await getCapacityAlertRuleBundle({
        ruleType: ruleTypeFilter,
        level: ruleLevelFilter,
        enabled: ruleEnabledFilter,
        keyword: ruleKeyword,
      });
      setRules(ruleData.items || []);
      setRuleStats(ruleStatsData);
    } catch (e) { message.error(e.message); }
    finally { setRuleLoading(false); }
  };

  useEffect(() => { loadRules(); }, [ruleTypeFilter, ruleLevelFilter, ruleEnabledFilter, ruleKeyword]);

  const closeAlert = async (id) => {
    try {
      await closeCapacityAlert(id);
      message.success("告警已关闭");
      await loadData();
    } catch (e) { message.error(e.message); }
  };

  const closeAll = async () => {
    try {
      const result = await closeCapacityAlerts({ instanceId: instanceFilter, dateFrom, dateTo });
      message.success(`已关闭 ${result.closed_count || 0} 条告警`);
      await loadData();
    } catch (e) { message.error(e.message); }
  };

  const isolateAlert = async (alert, days) => {
    try {
      await isolateCapacityAlert(alert.id, days, `隔离 ${days} 天`);
      message.success(`已隔离 ${days} 天`);
      await loadData();
    } catch (e) { message.error(e.message); }
  };

  const levelTag = (level) => {
    const map = { critical: { color: "red", text: "严重" }, warning: { color: "gold", text: "警告" }, normal: { color: "green", text: "正常" } };
    const cfg = map[level] || { color: "default", text: level };
    return <Tag color={cfg.color}>{cfg.text}</Tag>;
  };

  const alertTypeLabel = (t) => {
    const map = { forecast_shortage: "空间不足预警", abnormal_growth: "异常增长" };
    return map[t] || t;
  };

  const statusTag = (status) => {
    const map = {
      open: { color: "red", text: "待处理" },
      closed: { color: "default", text: "已关闭" },
      isolated: { color: "purple", text: "已隔离" },
    };
    const cfg = map[status] || { color: "default", text: status };
    return <Tag color={cfg.color}>{cfg.text}</Tag>;
  };

  const operatorLabel = (operator) => {
    const map = { ">": "大于", "<": "小于", "!=": "不等于", abnormal: "异常" };
    return map[operator] || operator || "-";
  };

  const toggleRuleEnabled = async (rule) => {
    try {
      await setCapacityAlertRuleEnabled(rule.id, !rule.enabled);
      message.success(rule.enabled ? "规则已停用" : "规则已启用");
      loadRules();
    } catch (e) { message.error(e.message); }
  };

  return (
    <Space direction="vertical" size={16} className="full-width">
      <Card>
        <Space wrap>
          <Select style={{ width: 200 }} placeholder={"筛选实例"} allowClear value={instanceFilter}
            options={instances.map((i) => ({ value: i.id, label: `${i.instance_name} (${i.host}:${i.port})` }))}
            onChange={(v) => setInstanceFilter(v || null)} />
          <Segmented value={statusFilter} onChange={setStatusFilter} options={[
            { value: "open", label: `待处理 (${stats.open})` },
            { value: "isolated", label: `已隔离 (${stats.isolated})` },
            { value: "closed", label: `已关闭 (${stats.closed})` },
            { value: "all", label: `全部 (${stats.total})` },
          ]} />
          <Input type="date" style={{ width: 160 }} value={dateFrom} onChange={(e) => setDateFrom(e.target.value)} />
          <Input type="date" style={{ width: 160 }} value={dateTo} onChange={(e) => setDateTo(e.target.value)} />
          {(dateFrom || dateTo) && <Button onClick={() => { setDateFrom(""); setDateTo(""); }}>{"清除时间"}</Button>}
          <Button icon={<ReloadOutlined />} loading={loading} onClick={loadData}>{"刷新"}</Button>
          {statusFilter === "open" && stats.open > 0 && (
            <Popconfirm title={`确定关闭全部 ${stats.open} 条告警？`} onConfirm={closeAll} okText={"确定"} cancelText={"取消"}>
              <Button danger>{"全部关闭"}</Button>
            </Popconfirm>
          )}
        </Space>
      </Card>

      <Row gutter={[16, 16]}>
        <Col xs={12} md={5}><Card size="small"><Statistic title={"待处理告警"} value={stats.open} valueStyle={{ color: stats.open > 0 ? "#cf1322" : "#3f8600" }} prefix={<AlertOutlined />} /></Card></Col>
        <Col xs={12} md={5}><Card size="small"><Statistic title={"严重"} value={stats.critical} valueStyle={{ color: "#cf1322" }} /></Card></Col>
        <Col xs={12} md={5}><Card size="small"><Statistic title={"警告"} value={stats.warning} valueStyle={{ color: "#fa8c16" }} /></Card></Col>
        <Col xs={12} md={5}><Card size="small"><Statistic title={"已隔离"} value={stats.isolated} valueStyle={{ color: "#722ed1" }} /></Card></Col>
        <Col xs={12} md={4}><Card size="small"><Statistic title={"已关闭"} value={stats.closed} valueStyle={{ color: "#8c8c8c" }} /></Card></Col>
      </Row>

      <Card title={"告警规则"} size="small"
        extra={<Text type="secondary">{`共 ${ruleStats.total} 条，启用 ${ruleStats.enabled} 条`}</Text>}>
        <Collapse ghost activeKey={ruleExpanded ? ["rules"] : []} onChange={(k) => setRuleExpanded(k.length > 0)}>
          <Collapse.Panel header={<Space><Text strong>{"告警规则列表"}</Text><Button size="small" type="link">{ruleExpanded ? "收起" : "展开"}</Button></Space>} key="rules">
            <Space direction="vertical" size={12} className="full-width">
              <Alert type="info" showIcon message={"已导入告警规则 xlsx"}
                description={"当前作为告警规则库展示和启停管理；规则自动触发还需要对应主机、数据库性能指标采集接入规则引擎。"} />
              <Space wrap>
                <Select style={{ width: 220 }} placeholder={"规则类型"} allowClear value={ruleTypeFilter}
                  options={(ruleStats.types || []).map((type) => ({ value: type, label: type }))}
                  onChange={(v) => setRuleTypeFilter(v || null)} />
                <Select style={{ width: 120 }} placeholder={"级别"} allowClear value={ruleLevelFilter}
                  options={[{ value: "critical", label: "严重" }, { value: "warning", label: "警告" }]}
                  onChange={(v) => setRuleLevelFilter(v || null)} />
                <Segmented value={ruleEnabledFilter} onChange={setRuleEnabledFilter} options={[
                  { value: "enabled", label: `启用 (${ruleStats.enabled})` },
                  { value: "disabled", label: `停用 (${ruleStats.disabled})` },
                  { value: "all", label: `全部 (${ruleStats.total})` },
                ]} />
                <Input.Search style={{ width: 260 }} allowClear placeholder={"搜索规则/指标"} onSearch={(v) => setRuleKeyword(v)} />
                <Button icon={<ReloadOutlined />} loading={ruleLoading} onClick={loadRules}>{"刷新规则"}</Button>
              </Space>
              <Table dataSource={rules} rowKey="id" size="small" loading={ruleLoading} pagination={{ defaultPageSize: 12 }} scroll={{ x: 1200 }}
                columns={[
                  { title: "级别", dataIndex: "level", width: 80, render: levelTag },
                  { title: "规则名称", dataIndex: "rule_name", width: 220, ellipsis: true },
                  { title: "规则类型", dataIndex: "rule_type", width: 180, render: (v) => <Tag color="blue">{v}</Tag> },
                  { title: "指标名称", dataIndex: "metric_name", width: 190, ellipsis: true },
                  { title: "连续次数", dataIndex: "consecutive_times", width: 90, render: (v) => `${v || 1} 次` },
                  { title: "条件", width: 150, render: (_, r) => `${operatorLabel(r.operator)} ${r.threshold || ""}`.trim() },
                  { title: "规则描述", dataIndex: "description", ellipsis: true },
                  { title: "状态", dataIndex: "enabled", width: 80, render: (v) => <Tag color={v ? "green" : "default"}>{v ? "启用" : "停用"}</Tag> },
                  { title: "操作", width: 90, render: (_, r) => <Button size="small" onClick={() => toggleRuleEnabled(r)}>{r.enabled ? "停用" : "启用"}</Button> },
                ]} />
            </Space>
          </Collapse.Panel>
        </Collapse>
      </Card>

      <Card title={"告警列表"} size="small">
        <Table dataSource={alerts} rowKey="id" size="small" pagination={{ defaultPageSize: 15 }} scroll={{ x: 1500 }}
          columns={[
            { title: "级别", dataIndex: "level", width: 80, render: levelTag, sorter: (a, b) => (a.level === "critical" ? 0 : 1) - (b.level === "critical" ? 0 : 1) },
            { title: "告警类型", dataIndex: "alert_type", width: 130, render: alertTypeLabel },
            { title: "告警对象", dataIndex: "object_name", width: 180, ellipsis: true },
            { title: "对象属性", dataIndex: "object_type", width: 100, render: (v) => <Tag color={v === "asm_diskgroup" ? "purple" : "blue"}>{v === "asm_diskgroup" ? "磁盘组" : "表空间"}</Tag> },
            { title: "告警信息", dataIndex: "message", ellipsis: true },
            { title: "指标值", dataIndex: "metric_value", width: 100, render: (v) => v != null ? Number(v).toFixed(1) : "-" },
            { title: "阈值", dataIndex: "threshold_value", width: 100, render: (v) => v != null ? Number(v).toFixed(1) : "-" },
            { title: "状态", dataIndex: "status", width: 90, render: statusTag },
            { title: "隔离至", dataIndex: "isolate_until", width: 170, render: (v) => v || "-" },
            { title: "时间", dataIndex: "create_time", width: 170, sorter: (a, b) => new Date(a.create_time) - new Date(b.create_time), defaultSortOrder: "descend" },
            {
              title: "操作", width: 220, render: (_, r) => r.status === "open" ? (
                <Space size={4}>
                  <Popconfirm title={"隔离此告警 10 天？隔离期间同类告警不会重复生成。"} onConfirm={() => isolateAlert(r, 10)} okText={"隔离"} cancelText={"取消"}>
                    <Button size="small">{"10天"}</Button>
                  </Popconfirm>
                  <Popconfirm title={"隔离此告警 15 天？隔离期间同类告警不会重复生成。"} onConfirm={() => isolateAlert(r, 15)} okText={"隔离"} cancelText={"取消"}>
                    <Button size="small">{"15天"}</Button>
                  </Popconfirm>
                  <Popconfirm title={"确定关闭此告警？"} onConfirm={() => closeAlert(r.id)} okText={"关闭"} cancelText={"取消"}>
                    <Button size="small" type="link">{"关闭"}</Button>
                  </Popconfirm>
                </Space>
              ) : "-"
            },
          ]}
        />
      </Card>
    </Space>
  );
}
