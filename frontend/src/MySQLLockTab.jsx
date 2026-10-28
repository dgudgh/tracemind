import React, { useState } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Empty,
  Row,
  Space,
  Statistic,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from "antd";
import { mysqlOpsApi } from "./apiClients";
import { PlanOnlyScriptModal } from "./OpsLayout";
import { MySQLDeadlockAnalysisView } from "./MySQLDeadlockAnalysisView";
import { stableRecordKey } from "./modules/stableKeys";

const { Text } = Typography;

export function MySQLLockTab({ data, instanceId }) {
  const [planModal, setPlanModal] = useState({ open: false, title: "", content: "", warnings: [] });
  const sections = data.sections || {};
  const summary = data.summary || {};
  const risks = data.risk_items || [];
  const valueText = (v) => (v === undefined || v === null || v === "" ? "-" : String(v));
  const riskColor = (level) => level === "critical" ? "red" : level === "warning" ? "orange" : level === "normal" ? "green" : "blue";
  const genKill = async (threadId, action = "kill_session") => {
    if (!threadId) {
      message.warning("缺少线程 ID，无法生成处理方案");
      return;
    }
    const r = await mysqlOpsApi.killSessionPlan(instanceId, { threadId, action });
    setPlanModal({ open: true, title: r.message || "处理方案", content: r.sql || r.script || "", warnings: r.warnings || [] });
  };
  const renderMetricCards = () => (
    <Row gutter={[12, 12]}>
      {[
        ["当前连接数", summary.session_count],
        ["活跃会话", summary.active_session_count],
        ["当前事务", summary.transaction_count],
        ["长事务", summary.long_transaction_count],
        ["锁等待", summary.lock_wait_count],
        ["阻塞会话", summary.blocking_session_count],
        ["MDL 等待", summary.metadata_lock_wait_count],
        ["最近死锁", summary.deadlock_detected ? "存在" : "无"],
        ["最长事务", summary.max_trx_label || `${summary.max_trx_seconds || 0} 秒`],
        ["风险等级", summary.risk_level || "normal"],
      ].map(([title, value]) => (
        <Col xs={24} sm={12} md={8} xl={6} xxl={4} key={title}>
          <Card size="small" bodyStyle={{ minHeight: 82, display: "flex", alignItems: "center" }}>
            <Statistic title={title} value={valueText(value)} valueStyle={{ fontSize: 18 }} />
          </Card>
        </Col>
      ))}
    </Row>
  );
  const riskCards = (
    <Row gutter={[12, 12]}>
      {(risks.length ? risks : [{ level: "normal", title: "当前未发现明显锁事务风险", message: "当前未发现明显锁等待、长事务、MDL 等待或死锁。", suggestion: "继续观察慢 SQL 和 InnoDB 状态。" }]).map((item) => (
        <Col xs={24} lg={12} xl={8} key={stableRecordKey("mysql-lock-risk", item, ["title", "item", "level", "message"])}>
          <Card size="small" bodyStyle={{ minHeight: 118 }}>
            <Space direction="vertical" size={6} style={{ width: "100%" }}>
              <Space>
                <Tag color={riskColor(item.level)}>{item.level || "normal"}</Tag>
                <Text strong>{item.title || item.item}</Text>
              </Space>
              <Text>{item.message}</Text>
              {item.suggestion && <Text type="secondary">{item.suggestion}</Text>}
            </Space>
          </Card>
        </Col>
      ))}
    </Row>
  );
  const sessionColumns = [
    { title: "线程 ID", dataIndex: "process_id", width: 90 },
    { title: "用户", dataIndex: "user", width: 120 },
    { title: "来源主机", dataIndex: "host", width: 180, ellipsis: true },
    { title: "数据库", dataIndex: "db", width: 130, render: valueText },
    { title: "命令", dataIndex: "command", width: 100 },
    { title: "持续秒数", dataIndex: "time_seconds", width: 100 },
    { title: "状态", dataIndex: "state", width: 180, ellipsis: true, render: valueText },
    { title: "SQL", dataIndex: "sql_text", ellipsis: true, render: valueText },
  ];
  const transactionColumns = [
    { title: "事务 ID", dataIndex: "trx_id", width: 160, ellipsis: true },
    { title: "状态", dataIndex: "trx_state", width: 90 },
    { title: "线程", dataIndex: "trx_mysql_thread_id", width: 90 },
    { title: "用户", dataIndex: "user", width: 110, render: valueText },
    { title: "来源", dataIndex: "host", width: 160, ellipsis: true, render: valueText },
    { title: "开始时间", dataIndex: "trx_started", width: 170 },
    { title: "持续", dataIndex: "duration_label", width: 110, render: (_, r) => r.duration_label || `${r.trx_seconds || r.duration_sec || 0} 秒` },
    { title: "锁定行", dataIndex: "trx_rows_locked", width: 90, render: valueText },
    { title: "修改行", dataIndex: "trx_rows_modified", width: 90, render: valueText },
    { title: "风险", dataIndex: "risk_level", width: 90, render: (v) => <Tag color={riskColor(v)}>{v || "normal"}</Tag> },
    { title: "当前 SQL", dataIndex: "trx_query", ellipsis: true, render: valueText },
    { title: "方案", width: 170, fixed: "right", render: (_, r) => <Space>
      <Button size="small" onClick={() => genKill(r.trx_mysql_thread_id, "kill_query")}>KILL QUERY</Button>
      <Button size="small" danger onClick={() => genKill(r.trx_mysql_thread_id, "kill_session")}>KILL</Button>
    </Space> },
  ];
  const lockWaitColumns = [
    { title: "等待线程", dataIndex: "waiting_process_id", width: 100, render: (_, r) => r.waiting_process_id || r.waiting_thread_id || "-" },
    { title: "阻塞线程", dataIndex: "blocking_process_id", width: 100, render: (_, r) => r.blocking_process_id || r.blocking_thread_id || "-" },
    { title: "等待事务", dataIndex: "waiting_trx_id", width: 160, ellipsis: true },
    { title: "阻塞事务", dataIndex: "blocking_trx_id", width: 160, ellipsis: true },
    { title: "对象", width: 220, render: (_, r) => [r.object_schema, r.object_name].filter(Boolean).join(".") || "-" },
    { title: "索引", dataIndex: "index_name", width: 130, render: valueText },
    { title: "等待锁", dataIndex: "waiting_lock_mode", width: 130, render: valueText },
    { title: "阻塞锁", dataIndex: "blocking_lock_mode", width: 130, render: valueText },
    { title: "等待时长", dataIndex: "duration_label", width: 120, render: valueText },
    { title: "等待 SQL", dataIndex: "waiting_sql", ellipsis: true, render: valueText },
    { title: "阻塞 SQL", dataIndex: "blocking_sql", ellipsis: true, render: valueText },
  ];
  const mdlColumns = [
    { title: "状态", dataIndex: "lock_status", width: 90, render: (v) => <Tag color={String(v).toUpperCase() === "PENDING" ? "orange" : "green"}>{v || "-"}</Tag> },
    { title: "对象", width: 260, render: (_, r) => [r.object_schema, r.object_name].filter(Boolean).join(".") || "-" },
    { title: "类型", dataIndex: "lock_type", width: 160 },
    { title: "持续", dataIndex: "lock_duration", width: 110 },
    { title: "线程", dataIndex: "process_id", width: 90 },
    { title: "用户", dataIndex: "user", width: 110, render: valueText },
    { title: "来源", dataIndex: "host", width: 160, ellipsis: true, render: valueText },
    { title: "等待秒数", dataIndex: "time_seconds", width: 100, render: valueText },
    { title: "SQL", dataIndex: "sql_text", ellipsis: true, render: valueText },
  ];
  const overviewTab = (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      {data.error && <Alert type="error" showIcon message="锁与事务采集失败" description={data.error} />}
      {(data.warnings || []).length > 0 && <Alert type="warning" showIcon message="部分信息降级采集" description={(data.warnings || []).join("；")} />}
      <Card title="锁与事务诊断总览" size="small">{renderMetricCards()}</Card>
      <Card title="风险诊断" size="small">{riskCards}</Card>
      <Card title="行锁 / 表锁统计" size="small">
        <Row gutter={[12, 12]}>
          {[...(Object.entries(sections.row_lock_stats || {})), ...(Object.entries(sections.table_lock_stats || {}))].map(([k, v]) => (
            <Col xs={24} sm={12} lg={8} xl={6} key={k}>
              <Card size="small"><Statistic title={k} value={valueText(v)} valueStyle={{ fontSize: 16 }} /></Card>
            </Col>
          ))}
        </Row>
      </Card>
    </Space>
  );
  const blockingTab = (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      {(sections.blocking_chains || []).length ? (sections.blocking_chains || []).map((chain, idx) => (
        <Card key={stableRecordKey("mysql-blocking-chain", chain, ["root_blocker_thread_id", "blocked_count", "risk_level"])} size="small" title={`根阻塞会话 ${chain.root_blocker_thread_id || "-"} 阻塞 ${chain.blocked_count || 0} 个会话`} extra={<Tag color={riskColor(chain.risk_level)}>{chain.risk_level}</Tag>}>
          <Alert type={chain.risk_level === "critical" ? "error" : "warning"} showIcon message={chain.suggestion} style={{ marginBottom: 12 }} />
          <Table dataSource={chain.chain || []} rowKey={(row) => stableRecordKey("mysql-blocking-chain-row", row, ["waiting_thread_id", "blocking_thread_id", "object", "index", "wait_seconds"])} size="small" pagination={false} columns={[
            { title: "等待线程", dataIndex: "waiting_thread_id", width: 100 },
            { title: "阻塞线程", dataIndex: "blocking_thread_id", width: 100 },
            { title: "对象", dataIndex: "object", width: 200, render: valueText },
            { title: "索引", dataIndex: "index", width: 130, render: valueText },
            { title: "等待秒数", dataIndex: "wait_seconds", width: 100, render: valueText },
            { title: "等待 SQL", dataIndex: "waiting_sql", ellipsis: true, render: valueText },
            { title: "阻塞 SQL", dataIndex: "blocking_sql", ellipsis: true, render: valueText },
          ]} />
        </Card>
      )) : <Empty description="当前未发现阻塞链路" />}
    </Space>
  );
  const deadlockTab = (
    <MySQLDeadlockAnalysisView
      deadlock={(sections.deadlocks || [])[0] || {}}
      raw={data.raw?.innodb_status || ""}
      hasDeadlock={summary.deadlock_detected}
      warnings={data.warnings || []}
    />
  );
  const actionTab = (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <Alert type="warning" showIcon message="处理动作只生成脚本，不会直接执行。" description="KILL QUERY 只终止当前 SQL；KILL SESSION 会断开会话并可能触发事务回滚。执行前必须确认业务来源和影响范围。" />
      <Card title="可从事务列表或阻塞链路中选择线程生成处理方案" size="small">
        <Text type="secondary">建议优先确认根阻塞会话、长事务来源、修改行数和业务模块，再生成 KILL QUERY 或 KILL SESSION 脚本。</Text>
      </Card>
    </Space>
  );
  return (<Space direction="vertical" size={16} style={{ width: "100%" }}>
    <Tabs items={[
      { key: "overview", label: "总览", children: overviewTab },
      { key: "blocking", label: "阻塞链路", children: blockingTab },
      { key: "sessions", label: "当前会话", children: <Table dataSource={sections.sessions || []} rowKey={(row) => stableRecordKey("mysql-lock-session", row, ["process_id", "user", "host", "db", "state"])} size="small" pagination={{ defaultPageSize: 20, showSizeChanger: true }} scroll={{ x: 1300 }} columns={sessionColumns} /> },
      { key: "transactions", label: "事务列表", children: <Table dataSource={sections.transactions || data.items || []} rowKey={(row) => stableRecordKey("mysql-transaction", row, ["trx_id", "trx_mysql_thread_id", "trx_started"])} size="small" pagination={{ defaultPageSize: 20, showSizeChanger: true }} scroll={{ x: 1800 }} columns={transactionColumns} /> },
      { key: "long", label: "长事务", children: <Table dataSource={sections.long_transactions || []} rowKey={(row) => stableRecordKey("mysql-long-transaction", row, ["trx_id", "trx_mysql_thread_id", "trx_started"])} size="small" pagination={{ defaultPageSize: 20, showSizeChanger: true }} scroll={{ x: 1800 }} columns={transactionColumns} /> },
      { key: "waits", label: "锁等待", children: <Table dataSource={sections.lock_waits || []} rowKey={(row) => stableRecordKey("mysql-lock-wait", row, ["waiting_trx_id", "blocking_trx_id", "waiting_thread_id", "blocking_thread_id", "object"])} size="small" pagination={{ defaultPageSize: 20, showSizeChanger: true }} scroll={{ x: 1800 }} columns={lockWaitColumns} /> },
      { key: "metadata", label: "元数据锁 MDL", children: <Table dataSource={sections.metadata_locks || []} rowKey={(row) => stableRecordKey("mysql-mdl-lock", row, ["owner_thread_id", "object_schema", "object_name", "lock_type", "lock_status"])} size="small" pagination={{ defaultPageSize: 20, showSizeChanger: true }} scroll={{ x: 1500 }} columns={mdlColumns} /> },
      { key: "deadlock", label: "死锁分析", children: deadlockTab },
      { key: "waitEvents", label: "等待事件", children: <Table dataSource={sections.wait_events || []} rowKey="event_name" size="small" pagination={{ defaultPageSize: 20, showSizeChanger: true }} columns={[
        { title: "事件", dataIndex: "event_name", ellipsis: true },
        { title: "次数", dataIndex: "count_star", width: 120 },
        { title: "总耗时(s)", dataIndex: "total_seconds", width: 120 },
        { title: "平均(s)", dataIndex: "avg_seconds", width: 120 },
        { title: "最大(s)", dataIndex: "max_seconds", width: 120 },
      ]} /> },
      { key: "actions", label: "处理方案", children: actionTab },
    ]} />
    <PlanOnlyScriptModal
      title={planModal.title}
      open={planModal.open}
      script={planModal.content}
      warnings={planModal.warnings}
      width={760}
      onClose={() => setPlanModal({ open: false, title: "", content: "", warnings: [] })}
    />
  </Space>);
}
