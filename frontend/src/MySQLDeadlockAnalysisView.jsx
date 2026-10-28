import React from "react";
import { Alert, Card, Col, Collapse, Descriptions, Empty, Row, Space, Statistic, Table, Tag } from "antd";
import { MYSQL_DEADLOCK_TYPE_NAMES } from "./modules/mysqlOpsConfig";
import { stableRecordKey } from "./modules/stableKeys";

export function MySQLDeadlockAnalysisView({ deadlock = {}, raw = "", hasDeadlock = false, message = "", warnings = [] }) {
  const summary = deadlock.summary || {};
  const transactions = deadlock.transactions || [];
  const lockObjects = deadlock.lock_objects || [];
  const analysis = deadlock.analysis || {};
  const history = deadlock.history || {};
  const rawText = deadlock.raw_deadlock_text || raw || "";
  const detected = hasDeadlock || summary.deadlock_detected || !!rawText;
  const valueText = (v) => (v === undefined || v === null || v === "" ? "-" : String(v));
  const typeName = MYSQL_DEADLOCK_TYPE_NAMES;
  const lockText = (items) => (items || []).map((item) => [item.table, item.index, item.lock_mode, item.lock_scope].filter(Boolean).join(" / ")).join("\n") || "-";
  const summaryItems = [
    ["死锁状态", detected ? "已检测到" : "未发现"],
    ["发生时间", summary.deadlock_time || "日志中未解析到"],
    ["涉及事务", summary.transaction_count || transactions.length || 0],
    ["涉及对象", [summary.object_schema, summary.object_name].filter(Boolean).join(".") || (deadlock.involved_tables || [])[0] || "-"],
    ["涉及索引", summary.index_name || (deadlock.involved_indexes || [])[0] || "-"],
    ["回滚事务", summary.victim_transaction || "-"],
    ["根因类型", typeName[summary.deadlock_type] || summary.deadlock_type || "-"],
    ["风险等级", summary.risk_level || (detected ? "warning" : "normal")],
  ];
  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Alert
        type={detected ? "warning" : "success"}
        showIcon
        message={message || (detected ? "检测到最近一次 InnoDB 死锁" : "当前 InnoDB 状态未发现最近死锁记录")}
        description={detected ? "页面已按 DBA 诊断视角解析死锁摘要、事务、锁对象、SQL、根因和建议。原始日志放在最后折叠展示。" : "如需持续分析历史死锁，请开启 innodb_print_all_deadlocks 并采集 MySQL Error Log。"}
      />
      {(warnings || deadlock.warnings || []).length > 0 && (
        <Alert type="info" showIcon message="解析提示" description={[...(warnings || []), ...(deadlock.warnings || [])].join("；")} />
      )}
      <Card title="死锁摘要" size="small">
        <Row gutter={[12, 12]}>
          {summaryItems.map(([title, value]) => (
            <Col xs={24} sm={12} lg={8} xl={6} key={title}>
              <Card size="small" bodyStyle={{ minHeight: 78 }}>
                <Statistic title={title} value={valueText(value)} valueStyle={{ fontSize: 16 }} />
              </Card>
            </Col>
          ))}
        </Row>
      </Card>
      <Card title="参与事务对比" size="small">
        <Table
          dataSource={transactions}
          rowKey={(row) => stableRecordKey("mysql-deadlock-transaction", row, ["trx_no", "trx_id", "mysql_thread_id", "status"])}
          size="small"
          pagination={false}
          scroll={{ x: 1300 }}
          locale={{ emptyText: "未从死锁日志中解析到事务明细" }}
          columns={[
            { title: "事务", dataIndex: "trx_no", width: 80, render: (v) => `事务 ${v || "-"}` },
            { title: "事务 ID", dataIndex: "trx_id", width: 150, ellipsis: true, render: valueText },
            { title: "MySQL Thread ID", dataIndex: "mysql_thread_id", width: 140, render: valueText },
            { title: "状态", dataIndex: "status", width: 140, render: (v) => v === "被 InnoDB 回滚" ? <Tag color="orange">{v}</Tag> : <Tag>{v || "-"}</Tag> },
            { title: "持有锁", width: 260, render: (_, r) => <pre style={{ margin: 0, whiteSpace: "pre-wrap" }}>{lockText(r.holds)}</pre> },
            { title: "等待锁", width: 260, render: (_, r) => <pre style={{ margin: 0, whiteSpace: "pre-wrap" }}>{lockText(r.waits_for)}</pre> },
            { title: "SQL", dataIndex: "query", ellipsis: true, render: valueText },
          ]}
        />
      </Card>
      <Card title="锁对象解析" size="small">
        <Table
          dataSource={lockObjects}
          rowKey={(row) => stableRecordKey("mysql-deadlock-lock-object", row, ["table", "index", "status", "lock_mode", "lock_type", "raw"])}
          size="small"
          pagination={false}
          scroll={{ x: 1000 }}
          locale={{ emptyText: "未解析到锁对象明细" }}
          columns={[
            { title: "状态", dataIndex: "status", width: 100, render: (v) => <Tag color={v === "waiting" ? "orange" : "blue"}>{v === "waiting" ? "等待" : "持有"}</Tag> },
            { title: "表", dataIndex: "table", width: 220, ellipsis: true, render: valueText },
            { title: "索引", dataIndex: "index", width: 140, render: valueText },
            { title: "锁模式", dataIndex: "lock_mode", width: 140, render: valueText },
            { title: "锁类型", dataIndex: "lock_type", width: 120, render: valueText },
            { title: "锁范围", dataIndex: "lock_scope", width: 180, render: valueText },
            { title: "原始行", dataIndex: "raw", ellipsis: true, render: valueText },
          ]}
        />
      </Card>
      <Card title="SQL 分析" size="small">
        {(transactions.length ? transactions : (deadlock.transaction_sqls || []).map((sql, idx) => ({ trx_no: idx + 1, query: sql }))).length ? (
          <Space direction="vertical" size={10} style={{ width: "100%" }}>
            {(transactions.length ? transactions : (deadlock.transaction_sqls || []).map((sql, idx) => ({ trx_no: idx + 1, query: sql }))).map((trx) => (
              <Card key={trx.trx_no} size="small" title={`事务 ${trx.trx_no || "-"}`}>
                <pre style={{ margin: 0, whiteSpace: "pre-wrap" }}>{trx.query || "-"}</pre>
              </Card>
            ))}
          </Space>
        ) : <Empty description="未解析到 SQL" />}
      </Card>
      <Card title="根因判断与处理建议" size="small">
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <Alert type="warning" showIcon message="根因判断" description={analysis.root_cause || "未能自动识别明确根因，请结合原始日志和业务调用链人工分析。"} />
          <Descriptions bordered size="small" column={1}>
            <Descriptions.Item label="业务影响">
              <ul style={{ margin: 0, paddingLeft: 18 }}>{(analysis.impact || ["InnoDB 会自动回滚其中一个事务，业务侧需要捕获并重试死锁错误。"]).map((item) => <li key={item}>{item}</li>)}</ul>
            </Descriptions.Item>
            <Descriptions.Item label="处理建议">
              <ol style={{ margin: 0, paddingLeft: 18 }}>{(analysis.suggestions || ["统一事务访问顺序", "检查 UPDATE/DELETE 条件是否命中索引", "缩短事务持有时间", "应用层增加死锁重试"]).map((item) => <li key={item}>{item}</li>)}</ol>
            </Descriptions.Item>
          </Descriptions>
        </Space>
      </Card>
      <Card title="历史趋势" size="small">
        <Row gutter={[12, 12]}>
          <Col xs={24} md={8}><Card size="small"><Statistic title="最近 24 小时" value={history.last_24h_count || 0} suffix="次" /></Card></Col>
          <Col xs={24} md={8}><Card size="small"><Statistic title="最近 7 天" value={history.last_7d_count || 0} suffix="次" /></Card></Col>
          <Col xs={24} md={8}><Card size="small"><Statistic title="历史来源" value="Error Log" valueStyle={{ fontSize: 16 }} /></Card></Col>
        </Row>
        <Alert style={{ marginTop: 12 }} type="info" showIcon message={history.message || "历史死锁需要开启 innodb_print_all_deadlocks，并通过 SSH/Error Log 定时采集后统计。"} />
      </Card>
      <Collapse
        items={[{
          key: "raw",
          label: "原始死锁日志",
          children: rawText ? <pre style={{ whiteSpace: "pre-wrap", maxHeight: 520, overflow: "auto" }}>{rawText}</pre> : <Empty description="暂无原始死锁日志" />,
        }]}
      />
    </Space>
  );
}
