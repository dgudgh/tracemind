import React, { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Collapse,
  Descriptions,
  Empty,
  Input,
  InputNumber,
  Row,
  Space,
  Statistic,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from "antd";
import { CopyOutlined } from "@ant-design/icons";
import { mysqlOpsApi } from "./apiClients";
import { mysqlSchemaName, mysqlTableName } from "./MySQLObjectNameHelpers";
import { copyText } from "./modules/clipboard";
import { MYSQL_BINLOG_VARIABLE_GROUPS } from "./modules/mysqlOpsConfig";
import { stableRecordKey, stableValueKey } from "./modules/stableKeys";

const { Text } = Typography;

export function MySQLCapacityTab({ data, instanceId }) {
  const [tables, setTables] = useState([]);
  const [tablesLoading, setTablesLoading] = useState(false);
  const [activeTab, setActiveTab] = useState("schemas");

  const loadTables = async () => {
    if (!instanceId) return;
    setTablesLoading(true);
    try {
      const r = await mysqlOpsApi.capacityTables(instanceId, { limit: 200 });
      setTables(r.items || []);
    } catch (e) {
      message.error(e.message);
    } finally {
      setTablesLoading(false);
    }
  };

  useEffect(() => {
    if (activeTab === "tables" && tables.length === 0) {
      loadTables();
    }
  }, [activeTab, instanceId]);

  return (
    <Card>
      <Tabs
        activeKey={activeTab}
        onChange={setActiveTab}
        items={[
          {
            key: "schemas",
            label: "数据库容量",
            children: (
              <Table
                dataSource={data.items || []}
                rowKey={(row) => stableRecordKey("mysql-capacity-schema", row, ["table_schema", "TABLE_SCHEMA", "schema_name", "database_name"])}
                size="small"
                pagination={{ defaultPageSize: 20, showSizeChanger: true, pageSizeOptions: ["20", "50", "100", "200"] }}
                columns={[
                  { title: "数据库", render: (_, r) => mysqlSchemaName(r) },
                  { title: "总大小(MB)", dataIndex: "total_mb" },
                  { title: "数据(MB)", dataIndex: "data_mb" },
                  { title: "索引(MB)", dataIndex: "index_mb" },
                  { title: "表数量", dataIndex: "table_count" },
                ]}
              />
            ),
          },
          {
            key: "tables",
            label: "大表排行",
            children: (
              <Table
                dataSource={tables}
                rowKey={(row) => stableRecordKey("mysql-capacity-table", row, ["table_schema", "TABLE_SCHEMA", "schema_name", "database_name", "table_name", "TABLE_NAME"])}
                size="small"
                loading={tablesLoading}
                pagination={{ defaultPageSize: 20, showSizeChanger: true, pageSizeOptions: ["20", "50", "100", "200"] }}
                title={() => <Button size="small" onClick={loadTables} loading={tablesLoading}>{"刷新"}</Button>}
                columns={[
                  { title: "数据库", render: (_, r) => <span style={{ whiteSpace: "nowrap" }}>{mysqlSchemaName(r)}</span>, width: 180, ellipsis: true },
                  { title: "表名", render: (_, r) => mysqlTableName(r), ellipsis: true },
                  { title: "引擎", dataIndex: "engine", width: 80 },
                  { title: "行数", dataIndex: "table_rows", width: 100 },
                  { title: "总计(MB)", dataIndex: "total_mb", width: 100 },
                  { title: "碎片(MB)", dataIndex: "free_mb", width: 100 },
                ]}
              />
            ),
          },
        ]}
      />
    </Card>
  );
}

export function MySQLBinlogCenterTab({ data, instanceId }) {
  const summary = data.summary || {};
  const variables = data.variables || {};
  const current = data.current_status || {};
  const fileSummary = data.file_summary || {};
  const cache = data.cache_status || {};
  const risks = data.risk_items || [];
  const warnings = data.warnings || [];
  const notices = data.notices || [];
  const [purgeFile, setPurgeFile] = useState("");
  const [retentionDays, setRetentionDays] = useState(7);
  const [planResult, setPlanResult] = useState(null);
  const [planLoading, setPlanLoading] = useState(false);
  const fmtNum = (value) => {
    const n = Number(value);
    return Number.isFinite(n) ? n.toLocaleString() : (value || "-");
  };
  const riskColor = (level) => level === "critical" ? "red" : level === "warning" ? "orange" : level === "info" ? "blue" : "green";
  const metricCards = [
    ["Binlog 状态", summary.enabled_label || "-"],
    ["当前格式", summary.format || "-"],
    ["GTID", summary.gtid_label || summary.gtid_mode || "-"],
    ["当前文件", summary.current_file || "-"],
    ["当前位置", fmtNum(summary.current_position)],
    ["文件数", fmtNum(summary.file_count)],
    ["总大小", `${summary.total_size_gb ?? 0} GB`],
    ["保留策略", summary.retention_label || "-"],
    ["最大单文件", `${summary.max_binlog_size_gb ?? 0} GB`],
    ["刷盘策略", `sync_binlog=${summary.sync_binlog ?? "-"}`],
    ["风险等级", summary.risk_level || "normal"],
  ];
  const variableGroups = MYSQL_BINLOG_VARIABLE_GROUPS;
  const renderVariableGroup = (title, keys) => (
    <Card title={title} size="small">
      <Table
        bordered
        size="small"
        tableLayout="fixed"
        pagination={false}
        rowKey="key"
        dataSource={keys.map((key) => ({ key, value: variables[key] }))}
        columns={[
          {
            title: "参数",
            dataIndex: "key",
            width: 380,
            render: (value) => <Text code style={{ whiteSpace: "nowrap" }}>{value}</Text>,
          },
          {
            title: "值",
            dataIndex: "value",
            render: (value) => value ? <Text style={{ wordBreak: "break-all" }}>{value}</Text> : <Text type="secondary">未采集</Text>,
          },
        ]}
      />
    </Card>
  );
  const genPurge = async (mode) => {
    const params = {
      before_file: mode === "file" && purgeFile ? purgeFile : undefined,
      retention_days: mode === "days" ? retentionDays || 7 : undefined,
    };
    setPlanLoading(true);
    try {
      const r = await mysqlOpsApi.binlogPurgePlan(instanceId, params);
      setPlanResult(r);
    } catch (e) {
      message.error(e.message || "生成清理方案失败");
    } finally {
      setPlanLoading(false);
    }
  };
  const overviewTab = (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Card title="Binlog 状态" size="small">
        <Row gutter={[12, 12]}>
          {metricCards.map(([title, value]) => (
            <Col xs={24} sm={12} md={8} xl={6} xxl={4} key={title}>
              <Card size="small" bodyStyle={{ minHeight: 82, display: "flex", alignItems: "center" }}>
                <Statistic title={title} value={value} valueStyle={{ fontSize: 18, wordBreak: "break-all" }} />
              </Card>
            </Col>
          ))}
        </Row>
      </Card>
      <Card title="当前位点" size="small">
        <Descriptions bordered size="small" column={2}>
          <Descriptions.Item label="当前 Binlog 文件">{current.file || "-"}</Descriptions.Item>
          <Descriptions.Item label="当前位置 Position">{fmtNum(current.position)}</Descriptions.Item>
          <Descriptions.Item label="Binlog_Do_DB">{current.binlog_do_db || "未配置"}</Descriptions.Item>
          <Descriptions.Item label="Binlog_Ignore_DB">{current.binlog_ignore_db || "未配置"}</Descriptions.Item>
          <Descriptions.Item label="Executed GTID Set" span={2}>
            {current.executed_gtid_set ? <Collapse size="small" ghost items={[{ key: "gtid", label: "点击查看完整 GTID 集合", children: <pre style={{ whiteSpace: "pre-wrap" }}>{current.executed_gtid_set}</pre> }]} /> : "未采集"}
          </Descriptions.Item>
        </Descriptions>
      </Card>
      <Card title="风险诊断" size="small">
        <Row gutter={[12, 12]}>
          {(risks.length ? risks : [{ level: "normal", item: "Binlog 状态", message: "当前未发现明显 Binlog 风险。" }]).map((item) => (
            <Col xs={24} md={12} xl={8} key={stableRecordKey("mysql-binlog-risk", item, ["item", "level", "message"])}>
              <Card size="small" bodyStyle={{ minHeight: 72 }}>
                <Space align="start">
                  <Tag color={riskColor(item.level)}>{item.level}</Tag>
                  <div><Text strong>{item.item}</Text><div style={{ marginTop: 6 }}>{item.message}</div></div>
                </Space>
              </Card>
            </Col>
          ))}
        </Row>
      </Card>
      <Card title="Binlog Cache 状态" size="small">
        <Descriptions bordered size="small" column={2}>
          <Descriptions.Item label="Binlog Cache 使用">{fmtNum(cache.binlog_cache_use)}</Descriptions.Item>
          <Descriptions.Item label="Binlog Cache 落盘">{fmtNum(cache.binlog_cache_disk_use)}</Descriptions.Item>
          <Descriptions.Item label="Binlog Cache 落盘比例">{cache.binlog_cache_disk_pct ?? 0}%</Descriptions.Item>
          <Descriptions.Item label="Stmt Cache 落盘比例">{cache.binlog_stmt_cache_disk_pct ?? 0}%</Descriptions.Item>
        </Descriptions>
      </Card>
    </Space>
  );
  const filesTab = (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <Alert
        type="info"
        showIcon
        message="SHOW BINARY LOGS 不提供创建/修改时间。未绑定 SSH 时，文件时间显示为“需配置 SSH”。"
        description={notices.length ? notices.join("；") : null}
      />
      <Card title="文件统计" size="small">
        <Row gutter={[12, 12]}>
          {[
            ["文件数", fileSummary.file_count ?? 0],
            ["总大小", `${fileSummary.total_size_gb ?? 0} GB`],
            ["当前文件", fileSummary.current_file || "-"],
            ["最老文件", fileSummary.first_file || "-"],
            ["最新文件", fileSummary.last_file || "-"],
            ["最大文件", `${fileSummary.largest_file || "-"} / ${fileSummary.largest_file_size_gb ?? 0} GB`],
          ].map(([label, value]) => (
            <Col xs={24} sm={12} xl={8} key={label}>
              <div style={{ minHeight: 72, padding: "10px 12px", border: "1px solid #f0f0f0", borderRadius: 6, background: "#fff" }}>
                <Text type="secondary" style={{ display: "block", marginBottom: 6 }}>{label}</Text>
                <Text strong style={{ wordBreak: "break-all", overflowWrap: "anywhere" }}>{value}</Text>
              </div>
            </Col>
          ))}
        </Row>
      </Card>
      <Card title="Binlog 文件列表" size="small">
        <Table dataSource={data.files || []} rowKey={(row) => row.log_name || row.file_name} size="small" tableLayout="fixed" pagination={{ defaultPageSize: 10, showSizeChanger: true, pageSizeOptions: ["10", "20", "50", "100"] }} scroll={{ x: 1500 }} columns={[
          { title: "序号", dataIndex: "seq", width: 70, align: "center" },
          { title: "文件名", dataIndex: "log_name", width: 240, ellipsis: true, render: v => <Text code style={{ fontSize: 12 }}>{v || "-"}</Text> },
          { title: "大小", dataIndex: "file_size_label", width: 120, align: "right" },
          { title: "占比", dataIndex: "size_pct", width: 90, align: "right", render: v => `${v ?? 0}%` },
          { title: "当前", dataIndex: "is_current", width: 90, align: "center", render: v => v ? <Tag color="blue">是</Tag> : <Tag>否</Tag> },
          { title: "加密", dataIndex: "encrypted", width: 90, align: "center", render: v => <Tag>{v || "unknown"}</Tag> },
          { title: "修改时间", dataIndex: "mtime", width: 180, align: "center", render: v => v || <Text type="secondary">需配置 SSH</Text> },
          { title: "保留", dataIndex: "age_days", width: 90, align: "right", render: v => v === null || v === undefined ? "-" : `${v} 天` },
          { title: "状态", dataIndex: "status_label", width: 130, align: "center", render: v => <Tag color={v === "当前写入文件" ? "blue" : "default"}>{v || "-"}</Tag> },
          { title: "建议", dataIndex: "suggestion", width: 360, render: v => <Text style={{ whiteSpace: "normal" }}>{v || "-"}</Text> },
        ]} />
      </Card>
    </Space>
  );
  const purgeTab = (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Alert type="warning" showIcon message="清理前必须确认备份完成、复制无延迟、GTID 位点安全。禁止使用 rm 直接删除 Binlog 文件。" />
      <Card title="清理方案生成" size="small">
        <Row gutter={[12, 12]}>
          <Col xs={24} md={10}><Input placeholder="清理到指定文件，例如 mysql-bin.000250" value={purgeFile} onChange={e => setPurgeFile(e.target.value)} /></Col>
          <Col xs={24} md={6}><InputNumber min={1} style={{ width: "100%" }} value={retentionDays} onChange={setRetentionDays} addonAfter="天" /></Col>
          <Col xs={24} md={8}><Space wrap><Button loading={planLoading} disabled={!purgeFile} onClick={() => genPurge("file")}>按文件生成</Button><Button loading={planLoading} onClick={() => genPurge("days")}>按保留天数生成</Button></Space></Col>
        </Row>
      </Card>
      <Card title="生成结果" size="small" extra={planResult?.sql && <Button size="small" icon={<CopyOutlined />} onClick={() => copyText(planResult.sql).then(() => message.success("已复制")).catch(() => message.error("复制失败"))}>复制 SQL</Button>}>
        {planResult ? <Space direction="vertical" style={{ width: "100%" }}>
          <Alert type="warning" showIcon message={planResult.message} description={(planResult.reason || []).join("；")} />
          <pre style={{ whiteSpace: "pre-wrap" }}>{planResult.sql || planResult.script}</pre>
          {(planResult.warnings || []).map((warning) => <Alert key={stableValueKey("mysql-binlog-plan-warning", warning)} type="info" showIcon message={warning} />)}
        </Space> : <Empty description="请选择清理方式生成方案" />}
      </Card>
    </Space>
  );
  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      {data.error && <Alert type="error" showIcon message={data.error} />}
      {notices.length > 0 && <Alert type="info" showIcon message="Binlog 采集说明" description={notices.join("；")} />}
      {warnings.length > 0 && <Alert type="warning" showIcon message="部分 Binlog 信息降级采集" description={warnings.join("；")} />}
      <Tabs items={[
        { key: "overview", label: "总览", children: overviewTab },
        { key: "variables", label: "配置参数", children: <Space direction="vertical" size={12} style={{ width: "100%" }}>{Object.entries(variableGroups).map(([title, keys]) => <div key={title}>{renderVariableGroup(title, keys)}</div>)}</Space> },
        { key: "files", label: "文件列表", children: filesTab },
        { key: "gtid", label: "GTID / 复制依赖", children: <Space direction="vertical" size={16} style={{ width: "100%" }}><Card title="GTID / 复制依赖" size="small"><Descriptions bordered size="small" column={1}><Descriptions.Item label="gtid_mode">{variables.gtid_mode || "-"}</Descriptions.Item><Descriptions.Item label="enforce_gtid_consistency">{variables.enforce_gtid_consistency || "-"}</Descriptions.Item><Descriptions.Item label="log_replica_updates / log_slave_updates">{variables.log_replica_updates || variables.log_slave_updates || "未采集"}</Descriptions.Item><Descriptions.Item label="复制依赖判断">{data.replication_dependency?.message || "当前未采集下游复制位点。"}</Descriptions.Item></Descriptions></Card><Card title="Executed GTID Set" size="small"><pre style={{ whiteSpace: "pre-wrap", maxHeight: 360, overflow: "auto" }}>{current.executed_gtid_set || variables.gtid_executed || "未采集"}</pre></Card></Space> },
        { key: "purge", label: "清理方案", children: purgeTab },
      ]} />
    </Space>
  );
}
