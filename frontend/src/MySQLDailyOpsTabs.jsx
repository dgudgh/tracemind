import React, { useState } from "react";
import { Alert, Button, Card, Col, Collapse, Descriptions, Input, Modal, Row, Space, Statistic, Table, Tabs, Tag, Typography } from "antd";
import { mysqlOpsApi } from "./apiClients";
import { MySQLDeadlockAnalysisView } from "./MySQLDeadlockAnalysisView";
import { MYSQL_INNODB_HEALTH_LABELS } from "./modules/mysqlOpsConfig";
import { stableRecordKey } from "./modules/stableKeys";

const { Text } = Typography;

export function MySQLVariablesTab({ data }) {
  const [search, setSearch] = useState("");
  const items = (data.items || []).filter(v => !search || v.name.toLowerCase().includes(search.toLowerCase()));
  const showPlan = (row) => {
    const sql = row.dynamic
      ? `-- Plan Only: 请在维护窗口评审后手工执行\n-- 当前值: ${row.value}\nSET GLOBAL ${row.name} = <new_value>;\n${row.persist_supported ? `SET PERSIST ${row.name} = <new_value>;` : "-- 当前版本或参数不支持 SET PERSIST，请同步写入 my.cnf。"}\nSHOW GLOBAL VARIABLES LIKE '${row.name}';`
      : `-- Plan Only: 静态参数需要修改 my.cnf 并重启 MySQL\n-- 当前值: ${row.value}\n# my.cnf\n${row.name}=<new_value>\n\n# 维护窗口重启后验证\nSHOW GLOBAL VARIABLES LIKE '${row.name}';`;
    Modal.info({
      title: `${row.name} 参数修改方案`,
      width: 760,
      content: (
        <Space direction="vertical" style={{ width: "100%" }}>
          <Alert type="warning" showIcon message="参数修改属于高风险操作，平台只生成 plan_only 方案，不直接执行 SET GLOBAL / SET PERSIST。" />
          <pre className="ops-output">{sql}</pre>
        </Space>
      ),
    });
  };
  return (
    <Card title="参数管理" extra={<Input.Search style={{ width: 250 }} placeholder="搜索参数" value={search} onChange={(e) => setSearch(e.target.value)} onSearch={setSearch} allowClear />}>
      <Space direction="vertical" size={12} style={{ width: "100%" }}>
        <Alert type="info" showIcon message="只读展示 MySQL GLOBAL 参数，并按动态/静态、作用域、版本持久化能力和高风险参数分类；所有修改仅生成 plan_only 方案。" />
        <Table dataSource={items} rowKey="name" size="small" pagination={{ defaultPageSize: 20, showSizeChanger: true, pageSizeOptions: ["20", "50", "100"] }} scroll={{ x: 1300 }} columns={[
          { title: "参数名", dataIndex: "name", width: 300, ellipsis: true },
          { title: "当前值", dataIndex: "value", width: 300, ellipsis: true },
          { title: "分类", dataIndex: "category", width: 140 },
          { title: "作用域", dataIndex: "scope", width: 90, render: (v) => <Tag>{v || "GLOBAL"}</Tag> },
          { title: "动态", dataIndex: "dynamic", width: 90, render: (v) => <Tag color={v ? "green" : "default"}>{v ? "是" : "否"}</Tag> },
          { title: "持久化", dataIndex: "persist_supported", width: 100, render: (v) => <Tag color={v ? "blue" : "default"}>{v ? "SET PERSIST" : "配置文件"}</Tag> },
          { title: "风险", dataIndex: "risk_level", width: 90, render: (v) => <Tag color={v === "high" ? "orange" : "green"}>{v === "high" ? "高风险" : "普通"}</Tag> },
          { title: "建议方式", dataIndex: "change_method", width: 180 },
          { title: "操作", width: 110, fixed: "right", render: (_, row) => <Button size="small" onClick={() => showPlan(row)}>生成方案</Button> },
        ]} />
      </Space>
    </Card>
  );
}

export function MySQLInnoDBHealthTab({ data }) {
  const summary = data.summary || data.groups?.summary || {};
  const innodb = data.innodb || {};
  const metrics = data.metrics || {};
  const warnings = data.warnings || [];
  const riskItems = data.risk_items || [];
  const rawMonitor = innodb.raw_monitor || data.raw || "";
  const labelMap = MYSQL_INNODB_HEALTH_LABELS;
  const riskColor = { critical: "red", high: "volcano", warning: "orange", info: "blue", normal: "green" };
  const valueText = (value) => {
    if (value === undefined || value === null || value === "") return "-";
    if (typeof value === "boolean") return value ? "是" : "否";
    const n = Number(value);
    return Number.isFinite(n) && String(value).length > 4 ? n.toLocaleString() : String(value);
  };
  const hasData = (obj) => Object.values(obj || {}).some((value) => value !== undefined && value !== null && value !== "");
  const renderInfoCards = (obj) => {
    const entries = Object.entries(obj || {}).filter(([, value]) => value !== undefined && value !== null && value !== "");
    if (!entries.length) return <Alert type="info" showIcon message="暂无数据" />;
    return (
      <Row gutter={[12, 12]}>
        {entries.map(([key, value]) => (
          <Col xs={24} sm={12} xl={8} xxl={6} key={key}>
            <div style={{ minHeight: 86, padding: "12px 14px", border: "1px solid #f0f0f0", borderRadius: 8, background: "#fff" }}>
              <Text type="secondary" style={{ display: "block", marginBottom: 8 }}>{labelMap[key] || key}</Text>
              <Text strong style={{ wordBreak: "break-all", overflowWrap: "anywhere" }}>{valueText(value)}</Text>
            </div>
          </Col>
        ))}
      </Row>
    );
  };
  const renderRows = (rows, columns) => (rows || []).length ? (
    <Table dataSource={rows} rowKey={(row) => stableRecordKey("mysql-innodb-row", row, ["trx_id", "waiting_trx_id", "blocking_trx_id", "lock_schema", "lock_table", "trx_mysql_thread_id"])} size="small" pagination={{ defaultPageSize: 10, showSizeChanger: true }} scroll={{ x: 1200 }} columns={columns} />
  ) : <Alert type="success" showIcon message="暂无活动数据" />;
  const summaryCards = [
    ["Buffer Pool 命中率", summary.buffer_pool_hit_pct !== undefined ? `${valueText(summary.buffer_pool_hit_pct)}%` : "-"],
    ["脏页比例", summary.buffer_pool_dirty_pct !== undefined ? `${valueText(summary.buffer_pool_dirty_pct)}%` : "-"],
    ["长事务", valueText(summary.long_transaction_count)],
    ["锁等待", valueText(summary.lock_wait_count)],
    ["最近死锁", valueText(summary.deadlock_detected)],
    ["History List", valueText(summary.history_list_length)],
    ["Redo 等待", valueText(summary.innodb_log_waits)],
    ["风险等级", summary.risk_level || "normal"],
  ];
  const tabItems = [
    { key: "overview", label: "状态总览", children: <Space direction="vertical" size={12} style={{ width: "100%" }}>
      {warnings.length > 0 && <Alert type="warning" showIcon message="部分 InnoDB 信息降级采集" description={warnings.join("\n")} />}
      {renderInfoCards(summary)}
    </Space> },
    { key: "buffer", label: "Buffer Pool", children: renderInfoCards(innodb.buffer_pool || { ...data.groups?.capacity, ...data.groups?.pages, ...data.groups?.read_write }) },
    { key: "transactions", label: "事务与 Undo", children: <Space direction="vertical" size={12} style={{ width: "100%" }}>
      {renderInfoCards({ ...(innodb.transactions || {}), ...(innodb.undo_purge || {}) })}
      {renderRows(innodb.transactions?.transactions || [], [
        { title: "事务 ID", dataIndex: "trx_id", width: 140 },
        { title: "状态", dataIndex: "trx_state", width: 100 },
        { title: "开始时间", dataIndex: "trx_started", width: 170 },
        { title: "持续秒数", dataIndex: "trx_seconds", width: 100, render: valueText },
        { title: "线程", dataIndex: "trx_mysql_thread_id", width: 100 },
        { title: "锁定行", dataIndex: "trx_rows_locked", width: 100, render: valueText },
        { title: "修改行", dataIndex: "trx_rows_modified", width: 100, render: valueText },
        { title: "SQL", dataIndex: "trx_query", ellipsis: true },
      ])}
    </Space> },
    { key: "locks", label: "锁等待与死锁", children: <Space direction="vertical" size={12} style={{ width: "100%" }}>
      {renderInfoCards({ ...(innodb.locks || {}), ...(innodb.deadlocks || {}) })}
      {renderRows(innodb.locks?.items || [], [
        { title: "等待事务", dataIndex: "waiting_trx_id", width: 150 },
        { title: "阻塞事务", dataIndex: "blocking_trx_id", width: 150 },
        { title: "Schema", dataIndex: "lock_schema", width: 140 },
        { title: "表", dataIndex: "lock_table", width: 160 },
        { title: "索引", dataIndex: "lock_index", width: 160 },
        { title: "等待锁", dataIndex: "waiting_lock_mode", width: 160 },
        { title: "阻塞锁", dataIndex: "blocking_lock_mode", width: 160 },
      ])}
    </Space> },
    { key: "redo", label: "Redo 与刷盘", children: renderInfoCards(innodb.redo_log || data.groups?.redo_log) },
    { key: "io", label: "I/O 状态", children: renderInfoCards(innodb.io || data.groups?.io) },
    { key: "rows", label: "行操作", children: renderInfoCards(innodb.row_operations || data.groups?.row_operations) },
    { key: "config", label: "配置参数", children: renderInfoCards(innodb.config || data.variables) },
    { key: "risks", label: "风险建议", children: riskItems.length ? <Row gutter={[12, 12]}>
      {riskItems.map((item, index) => (
        <Col xs={24} lg={12} xl={8} key={stableRecordKey("mysql-innodb-risk", item, ["level", "title", "finding"])}>
          <Card size="small" title={<Space><Tag color={riskColor[item.level] || "blue"}>{item.level}</Tag><Text strong>{item.title}</Text></Space>}>
            <Space direction="vertical" size={8}>
              <Text><Text strong>发现：</Text>{item.finding}</Text>
              <Text><Text strong>影响：</Text>{item.impact}</Text>
              <Text><Text strong>建议：</Text>{item.suggestion}</Text>
            </Space>
          </Card>
        </Col>
      ))}
    </Row> : <Alert type="success" showIcon message="暂无明显 InnoDB 风险" /> },
    { key: "raw", label: "原始 InnoDB Monitor", children: <Collapse bordered={false} items={[{ key: "raw", label: "SHOW ENGINE INNODB STATUS", children: <pre style={{ maxHeight: 520, overflow: "auto", fontSize: 12 }}>{rawMonitor || "暂无原始输出"}</pre> }]} /> },
    { key: "raw_metrics", label: "原始指标", children: renderInfoCards(metrics) },
  ].filter((item) => item.key === "raw" || item.key === "raw_metrics" || item.key === "overview" || item.key === "risks" || hasData(item.children?.props?.source) || true);
  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Row gutter={[12, 12]}>
        {summaryCards.map(([title, value]) => (
          <Col xs={24} sm={12} xl={6} xxl={3} key={title}>
            <Card size="small"><Statistic title={title} value={value} /></Card>
          </Col>
        ))}
      </Row>
      <Card title="InnoDB 运行健康驾驶舱">
        <Tabs items={tabItems} />
      </Card>
    </Space>
  );
}

export function MySQLUserTab({ data, instanceId }) {
  const [grants, setGrants] = useState(null);
  const [selUser, setSelUser] = useState(null);
  const showGrants = async (user, host) => { const r = await mysqlOpsApi.userGrants(instanceId, { user, host }); setGrants(r.grants||[]); setSelUser(user+"@"+host); };
  return (<Space direction="vertical" size={16} style={{width:"100%"}}>
    <Card title="用户列表"><Table dataSource={data.items||[]} rowKey={(row) => stableRecordKey("mysql-user", row, ["user", "host", "plugin"])} size="small" pagination={{pageSize:15}} columns={[
      {title:"用户", dataIndex:"user"},
      {title:"Host", dataIndex:"host"},
      {title:"插件", dataIndex:"plugin"},
      {title:"锁定", dataIndex:"account_locked", width:60},
      {title:"密码过期", dataIndex:"password_expired", width:80},
      {title:"操作", width:80, render:(_,r)=><Button size="small" onClick={()=>showGrants(r.user,r.host)}>查看权限</Button>},
    ]} /></Card>
    {selUser && <Card title={`${selUser} 权限`} size="small"><pre>{(grants||[]).join("\n\n")}</pre></Card>}
  </Space>);
}

export function MySQLReplicationTab({ data }) {
  const m = data.master || {};
  const reps = data.replicas || [];
  return (<Space direction="vertical" size={16} style={{width:"100%"}}>
    <Card title="Master 状态"><Descriptions bordered size="small" column={2}>
      {Object.entries(m).map(([k,v]) => <Descriptions.Item key={k} label={k}>{String(v)}</Descriptions.Item>)}
    </Descriptions></Card>
    {reps.length > 0 && <Card title="复制状态"><Table dataSource={reps} rowKey={(row) => stableRecordKey("mysql-replication", row, ["channel_name", "source_host", "master_host", "service_state", "thread_type"])} size="small" pagination={false} scroll={{x:2000}}
      columns={reps.length > 0 ? Object.keys(reps[0]).map(k => ({title:k, dataIndex:k, width:150, ellipsis:true})) : []} /></Card>}
    {data.error && <Alert type="error" message={data.error} />}
  </Space>);
}

export function MySQLRelayLogTab({ data }) {
  const vars = data.variables || {};
  const reps = data.replicas || [];
  return (<Space direction="vertical" size={16} style={{width:"100%"}}>
    <Card title="Relay Log 参数"><Descriptions bordered size="small" column={2}>
      {Object.entries(vars).map(([k,v]) => <Descriptions.Item key={k} label={k}>{String(v || "-")}</Descriptions.Item>)}
    </Descriptions></Card>
    <Card title="复制线程 Relay 状态"><Table dataSource={reps} rowKey={(row) => stableRecordKey("mysql-replication-relay", row, ["channel_name", "source_host", "master_host", "service_state", "thread_type"])} size="small" pagination={{pageSize:10}} scroll={{x:1800}}
      columns={reps.length > 0 ? Object.keys(reps[0]).map(k => ({title:k, dataIndex:k, width:150, ellipsis:true})) : []} /></Card>
    {data.error && <Alert type="error" message={data.error} />}
  </Space>);
}

export function MySQLErrorLogTab({ data }) {
  const vars = data.variables || {};
  return (<Space direction="vertical" size={16} style={{width:"100%"}}>
    <Alert type="info" showIcon message={data.message || "已获取 Error Log 配置"} />
    <Card title="Error Log 配置"><Descriptions bordered size="small" column={1}>
      {Object.entries(vars).map(([k,v]) => <Descriptions.Item key={k} label={k}>{String(v || "-")}</Descriptions.Item>)}
    </Descriptions></Card>
    {data.error && <Alert type="error" message={data.error} />}
  </Space>);
}

export function MySQLDeadlockTab({ data }) {
  return (
    <MySQLDeadlockAnalysisView
      deadlock={data.deadlock || {
        summary: data.summary || {},
        transactions: data.transactions || [],
        lock_objects: data.lock_objects || [],
        analysis: data.analysis || {},
        history: data.history || {},
        raw_deadlock_text: data.latest_deadlock || "",
      }}
      raw={data.latest_deadlock || ""}
      hasDeadlock={data.has_deadlock}
      message={data.message}
      warnings={data.warnings || []}
    />
  );
}

export function MySQLSecurityTab({ data }) {
  const risks = data.risks || [];
  return (<Space direction="vertical" size={16} style={{width:"100%"}}>
    <Card title="安全风险"><Table dataSource={risks} rowKey={(row) => stableRecordKey("mysql-security-risk", row, ["item", "level", "suggestion"])} size="small" pagination={false} columns={[
      {title:"等级", dataIndex:"level", width:100, render:v=><Tag color={v==='critical'?'red':v==='high'?'orange':'gold'}>{v}</Tag>},
      {title:"对象", dataIndex:"item", width:220},
      {title:"说明", dataIndex:"message"},
    ]} /></Card>
    <Card title="账号列表"><Table dataSource={data.users||[]} rowKey={(row) => stableRecordKey("mysql-security-user", row, ["user", "host", "plugin"])} size="small" pagination={{pageSize:15}} columns={[
      {title:"用户", dataIndex:"user"},
      {title:"Host", dataIndex:"host"},
      {title:"插件", dataIndex:"plugin"},
      {title:"锁定", dataIndex:"account_locked"},
      {title:"密码过期", dataIndex:"password_expired"},
      {title:"空密码", dataIndex:"empty_password", render:v=>v ? <Tag color="red">是</Tag> : <Tag>否</Tag>},
    ]} /></Card>
    {data.error && <Alert type="error" message={data.error} />}
  </Space>);
}

export function MySQLMGRTab({ data }) {
  const vars = data.variables || {};
  return (<Space direction="vertical" size={16} style={{width:"100%"}}>
    <Alert type={data.configured ? "success" : "info"} showIcon message={data.configured ? "检测到 MGR 配置" : "未检测到 MGR 配置"} />
    <Card title="MGR 参数"><Descriptions bordered size="small" column={2}>
      {Object.entries(vars).map(([k,v]) => <Descriptions.Item key={k} label={k}>{String(v || "-")}</Descriptions.Item>)}
    </Descriptions></Card>
    <Card title="MGR 成员"><Table dataSource={data.members||[]} rowKey={(row) => stableRecordKey("mysql-mgr-member", row, ["MEMBER_ID", "MEMBER_HOST", "MEMBER_PORT", "MEMBER_STATE"])} size="small" pagination={false} columns={[
      {title:"Host", dataIndex:"MEMBER_HOST"},
      {title:"Port", dataIndex:"MEMBER_PORT"},
      {title:"State", dataIndex:"MEMBER_STATE"},
      {title:"Role", dataIndex:"MEMBER_ROLE"},
      {title:"Version", dataIndex:"MEMBER_VERSION"},
    ]} /></Card>
    <Card title="MGR 应用统计"><Table dataSource={data.member_stats||[]} rowKey={(row) => stableRecordKey("mysql-mgr-member-stats", row, ["MEMBER_ID", "MEMBER_HOST", "MEMBER_PORT", "CHANNEL_NAME"])} size="small" pagination={false} scroll={{x:1200}}
      columns={(data.member_stats||[]).length > 0 ? Object.keys(data.member_stats[0]).map(k => ({title:k, dataIndex:k, width:180, ellipsis:true})) : []} /></Card>
    {data.error && <Alert type="error" message={data.error} />}
  </Space>);
}
