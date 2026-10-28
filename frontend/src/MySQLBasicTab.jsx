import React, { useState } from "react";
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Collapse,
  Descriptions,
  Divider,
  Input,
  Col,
  Row,
  Space,
  Statistic,
  Table,
  Tabs,
  Typography,
  message,
} from "antd";
import {
  MYSQL_BASIC_LABELS,
  MYSQL_RISK_COLORS,
  MYSQL_RISK_TEXT,
  MYSQL_STATUS_COLORS,
} from "./modules/mysqlOpsConfig";
import { copyText } from "./modules/clipboard";
import { saveJsonFile } from "./modules/fileExport";
import { stableRecordKey } from "./modules/stableKeys";
import { MySQLBasicOverview } from "./MySQLBasicOverview";

const { Text } = Typography;

export function MySQLBasicTab({ data }) {
  const b = data.basic || {};
  const s = data.status || {};
  const checks = data.checks || {};
  const risks = data.risks || [];
  const summary = data.summary || {};
  const instance = data.instance || {};
  const backendInfo = data.basic_info || {};
  const info = Object.keys(backendInfo).length ? backendInfo : {
    version: b,
    runtime: s,
    variables_core: b,
    thread_connection: { ...s, ...checks },
    table_health: checks,
    risk_items: risks,
  };
  const raw = data.raw || {};
  const [rawSearch, setRawSearch] = useState("");
  const [rawOnlyAbnormal, setRawOnlyAbnormal] = useState(false);
  const riskColor = MYSQL_RISK_COLORS;
  const riskText = MYSQL_RISK_TEXT;
  const statusColor = MYSQL_STATUS_COLORS;
  const labelMap = MYSQL_BASIC_LABELS;
  const riskCounts = risks.reduce((acc, item) => {
    acc[item.level] = (acc[item.level] || 0) + 1;
    return acc;
  }, {});
  const hasDisplayValue = (value) => {
    if (value === undefined || value === null) return false;
    if (typeof value === "string") return value.trim() !== "";
    if (Array.isArray(value)) return value.length > 0;
    if (typeof value === "object") return Object.keys(value).length > 0;
    return true;
  };
  const hasDisplayEntry = (key, value) => key === "current_database" || hasDisplayValue(value);
  const num = (value, digits = 2) => {
    const n = Number(value);
    if (!Number.isFinite(n)) return value === undefined || value === null || value === "" ? "-" : String(value);
    return n.toFixed(digits).replace(/\.00$/, "");
  };
  const intText = (value) => {
    const n = Number(value);
    return Number.isFinite(n) ? n.toLocaleString() : valueText(value);
  };
  const boolLabel = (value) => {
    const text = String(value ?? "").toUpperCase();
    if (["ON", "1", "YES", "TRUE"].includes(text)) return "已开启";
    if (["OFF", "0", "NO", "FALSE"].includes(text)) return "未开启";
    return value;
  };
  const valueText = (value) => {
    if (value === undefined || value === null || value === "") return "-";
    const simple = boolLabel(value);
    if (simple !== value) return simple;
    if (Array.isArray(value)) return value.length ? JSON.stringify(value) : "-";
    if (typeof value === "object") return Object.keys(value).length ? JSON.stringify(value) : "-";
    return String(value);
  };
  const displayValue = (key, value) => {
    if (key === "read_only") return String(value) === "1" ? "只读" : "可写";
    if (key === "super_read_only") return String(value) === "1" ? "已开启" : "未开启";
    if (key === "current_database" && (value === undefined || value === null || value === "")) return "未选择默认库";
    return valueText(value);
  };
  const renderDescriptions = (obj, column = 2, extraLabels = {}) => {
    const entries = Object.entries(obj || {}).filter(([key, value]) => hasDisplayEntry(key, value));
    if (!entries.length) return <Alert type="info" showIcon message="暂无数据" />;
    return (
      <Descriptions
        bordered
        size="small"
        column={column}
        labelStyle={{ width: column === 1 ? 180 : 150, whiteSpace: "nowrap" }}
        contentStyle={{ wordBreak: "break-all", overflowWrap: "anywhere", minWidth: 0 }}
      >
        {entries.map(([key, value]) => (
          <Descriptions.Item key={key} label={extraLabels[key] || labelMap[key] || key}>{displayValue(key, value)}</Descriptions.Item>
        ))}
      </Descriptions>
    );
  };
  const renderInfoCards = (obj, extraLabels = {}) => {
    const entries = Object.entries(obj || {}).filter(([key, value]) => hasDisplayEntry(key, value));
    if (!entries.length) return <Alert type="info" showIcon message="暂无数据" />;
    return (
      <Row gutter={[12, 12]}>
        {entries.map(([key, value]) => (
          <Col xs={24} sm={12} xl={8} xxl={6} key={key}>
            <div style={{
              minHeight: 86,
              padding: "12px 14px",
              border: "1px solid #f0f0f0",
              borderRadius: 8,
              background: "#fff",
            }}>
              <Text type="secondary" style={{ display: "block", marginBottom: 8 }}>{extraLabels[key] || labelMap[key] || key}</Text>
              <Text strong style={{ wordBreak: "break-all", overflowWrap: "anywhere" }}>{displayValue(key, value)}</Text>
            </div>
          </Col>
        ))}
      </Row>
    );
  };
  const rawEntries = (obj) => Object.entries(obj || {})
    .filter(([name, value]) => !rawSearch || name.toLowerCase().includes(rawSearch.toLowerCase()) || String(value).toLowerCase().includes(rawSearch.toLowerCase()))
    .filter(([, value]) => !rawOnlyAbnormal || ["OFF", "0", "NO", "FALSE", ""].includes(String(value ?? "").toUpperCase()))
    .map(([name, value]) => ({ name, value }));
  const copyJson = async (payload) => {
    try {
      await copyText(JSON.stringify(payload || {}, null, 2));
      message.success("已复制 JSON");
    } catch {
      message.error("复制失败");
    }
  };
  const exportJson = (payload, filename) => {
    saveJsonFile(payload, filename);
  };
  const renderKeyValueTable = (obj) => (
    <Table
      dataSource={rawEntries(obj)}
      rowKey="name"
      size="small"
      pagination={{ defaultPageSize: 20, showSizeChanger: true }}
      columns={[
        { title: "\u540d\u79f0", dataIndex: "name", width: 320, ellipsis: true },
        { title: "\u503c", dataIndex: "value", ellipsis: true, render: valueText },
      ]}
    />
  );
  const renderRowsTable = (rows) => !(rows || []).length ? <Alert type="info" showIcon message="暂无数据" /> : (
    <Table
      dataSource={rows || []}
      rowKey={(row) => stableRecordKey("mysql-basic-row", row, ["MEMBER_ID", "Log_name", "table_schema", "TABLE_SCHEMA", "name"])}
      size="small"
      pagination={{ defaultPageSize: 10, showSizeChanger: true }}
      scroll={{ x: 1100 }}
      columns={Object.keys((rows || [])[0] || {}).map((key) => ({ title: key, dataIndex: key, ellipsis: true, render: valueText }))}
    />
  );
  const omitKeys = (obj, keys) => Object.fromEntries(Object.entries(obj || {}).filter(([key]) => !keys.includes(key)));
  const pickKeys = (obj, keys) => Object.fromEntries(keys.map((key) => [key, obj?.[key]]).filter(([, value]) => value !== undefined && value !== null && value !== ""));
  const renderInnoDBStatus = () => {
    const st = info.innodb_status || {};
    const readRequests = Number(st.innodb_buffer_pool_read_requests || 0);
    const diskReads = Number(st.innodb_buffer_pool_reads || 0);
    const hitPct = Number(st.buffer_pool_hit_pct ?? summary.buffer_pool_hit_pct ?? 0);
    const totalPages = Number(st.innodb_buffer_pool_pages_total || 0);
    const cards = [
      ["Buffer Pool 命中率", `${num(hitPct)}% ${summary.buffer_pool_status_label || ""}`],
      ["配置容量", `${num(st.buffer_pool_configured_gb)} GB`],
      ["已用容量", `${num(st.buffer_pool_used_gb)} GB / ${num(st.buffer_pool_used_pct)}%`],
      ["空闲页", `${intText(st.innodb_buffer_pool_pages_free)} / ${num(st.buffer_pool_free_pct)}%`],
      ["脏页", `${intText(st.innodb_buffer_pool_pages_dirty)} / ${num(st.buffer_pool_dirty_pct)}%`],
      ["磁盘读", `${intText(diskReads)} / ${num(st.buffer_pool_disk_reads_per_10k)} 次/万读`],
    ];
    return (
      <Space direction="vertical" size={12} style={{ width: "100%" }}>
        <Row gutter={[12, 12]}>
          {cards.map(([title, value]) => (
            <Col xs={24} sm={12} xl={4} key={title}>
              <Card size="small"><Statistic title={title} value={value} /></Card>
            </Col>
          ))}
        </Row>
        <Collapse bordered={false} defaultActiveKey={["capacity", "pages"]} items={[
          { key: "capacity", label: "Buffer Pool 容量", children: renderInfoCards(pickKeys(st, ["innodb_page_size", "buffer_pool_configured_gb", "buffer_pool_total_gb", "buffer_pool_used_gb", "buffer_pool_free_gb", "buffer_pool_data_gb", "buffer_pool_dirty_gb", "buffer_pool_used_pct", "buffer_pool_free_pct"])) },
          { key: "pages", label: "Buffer Pool 页状态", children: renderInfoCards(pickKeys(st, ["innodb_buffer_pool_pages_total", "buffer_pool_used_pages", "innodb_buffer_pool_pages_data", "innodb_buffer_pool_pages_free", "innodb_buffer_pool_pages_misc", "innodb_buffer_pool_pages_dirty", "innodb_buffer_pool_pages_flushed", "buffer_pool_data_pct", "buffer_pool_dirty_pct", "buffer_pool_misc_pct", "history_list_length"])) },
          { key: "buffer", label: "Buffer Pool 读写", children: renderInfoCards(pickKeys(st, ["buffer_pool_hit_pct", "buffer_pool_disk_read_pct", "buffer_pool_disk_reads_per_10k", "innodb_buffer_pool_read_requests", "innodb_buffer_pool_reads", "innodb_buffer_pool_write_requests", "innodb_buffer_pool_wait_free", "innodb_buffer_pool_read_ahead", "innodb_buffer_pool_read_ahead_evicted", "innodb_buffer_pool_read_ahead_rnd"])) },
          { key: "lock", label: "锁等待", children: renderInfoCards(pickKeys(st, ["innodb_row_lock_waits", "innodb_row_lock_time", "innodb_row_lock_time_avg", "has_deadlock"])) },
          { key: "io", label: "IO 与日志", children: renderInfoCards(pickKeys(st, ["innodb_log_waits", "innodb_os_log_written", "innodb_data_reads", "innodb_data_writes", "innodb_data_fsyncs"])) },
          { key: "raw", label: "原始 InnoDB 文本", children: <Input.TextArea value={st.raw || ""} readOnly autoSize={{ minRows: 8, maxRows: 18 }} /> },
        ]} />
      </Space>
    );
  };
  const renderSchemaOverview = () => {
    const schema = info.schema_overview || {};
    const schemas = schema.schemas || [];
    const schemaName = (row) => row?.table_schema || row?.TABLE_SCHEMA || row?.Table_schema || row?.schema_name || row?.database_name || "-";
    const schemaNumber = (row, key) => row?.[key]
      ?? row?.[key.toUpperCase()]
      ?? row?.[key.charAt(0).toUpperCase() + key.slice(1)]
      ?? row?.[key.replace(/_([a-z])/g, (_, p1) => p1.toUpperCase())];
    const summaryItems = [
      ["Schema 数量", intText(schema.schema_count)],
      ["表数量", intText(schema.table_count)],
      ["视图数量", intText(schema.view_count)],
      ["总容量", `${num(schema.total_size_gb)} GB`],
    ];
    const topSchema = schema.top_schema_by_size;
    return (
      <Space direction="vertical" size={12} style={{ width: "100%" }}>
        <Row gutter={[12, 12]}>
          {summaryItems.map(([title, value]) => (
            <Col xs={24} sm={12} xl={6} key={title}>
              <Card size="small"><Statistic title={title} value={value} /></Card>
            </Col>
          ))}
        </Row>
        {topSchema && (
          <Alert
            type="info"
            showIcon
            message={`最大 Schema：${schemaName(topSchema)}，总容量 ${num(schemaNumber(topSchema, "total_gb") ?? topSchema.total_size_gb)} GB`}
          />
        )}
        {schemas.length ? (
          <Table
            dataSource={schemas}
            rowKey={(row) => stableRecordKey("mysql-schema", row, ["table_schema", "TABLE_SCHEMA", "schema_name", "database_name"])}
            size="small"
            pagination={{ defaultPageSize: 10, showSizeChanger: true }}
            scroll={{ x: 900 }}
            columns={[
              { title: "Schema", key: "schema", width: 240, ellipsis: true, render: (_, row) => <Text strong>{schemaName(row)}</Text> },
              { title: "表数量", key: "table_count", width: 110, align: "right", render: (_, row) => intText(schemaNumber(row, "table_count")) },
              { title: "视图数量", key: "view_count", width: 110, align: "right", render: (_, row) => intText(schemaNumber(row, "view_count")) },
              { title: "总容量(GB)", key: "total_gb", width: 130, align: "right", render: (_, row) => num(schemaNumber(row, "total_gb")) },
              { title: "数据(GB)", key: "data_gb", width: 120, align: "right", render: (_, row) => num(schemaNumber(row, "data_gb")) },
              { title: "索引(GB)", key: "index_gb", width: 120, align: "right", render: (_, row) => num(schemaNumber(row, "index_gb")) },
            ]}
          />
        ) : (
          <Alert type="info" showIcon message="暂无 Schema 容量数据" />
        )}
      </Space>
    );
  };
  const collapse = (items) => <Collapse bordered={false} defaultActiveKey={items.slice(0, 1).map((item) => item.key)} items={items} />;
  const detailTabs = [
    { key: "basic", label: "基础信息", children: collapse([
      { key: "identity", label: "实例身份", children: renderInfoCards(info.identity) },
      { key: "version", label: "版本与编译", children: renderInfoCards(info.version) },
      { key: "paths", label: "目录与日志", children: renderInfoCards(info.paths) },
      { key: "charset", label: "字符集与时区", children: renderInfoCards(info.charset_timezone) },
    ]) },
    { key: "runtime", label: "运行状态", children: collapse([
      { key: "runtime", label: "运行指标", children: renderInfoCards(info.runtime) },
      { key: "thread", label: "线程与连接", children: renderInfoCards(info.thread_connection) },
      { key: "cache", label: "缓存与文件句柄", children: renderInfoCards(info.table_file_cache) },
    ]) },
    { key: "ha", label: "复制与高可用", children: collapse([
      { key: "binlog", label: "Binlog / GTID", children: <Space direction="vertical" size={12} style={{ width: "100%" }}>
        {renderInfoCards(omitKeys(info.binlog_gtid, ["current_status", "files"]))}
        <Divider orientation="left">当前 Binlog 状态</Divider>
        {renderInfoCards(info.binlog_gtid?.current_status || {})}
        <Divider orientation="left">Binlog 文件列表</Divider>
        {renderRowsTable(info.binlog_gtid?.files || [])}
      </Space> },
      { key: "replication", label: "复制状态", children: <Space direction="vertical" size={12} style={{ width: "100%" }}><Alert type={info.replication?.replica_detected ? "success" : "info"} showIcon message={summary.replication_status_label || "未配置复制"} />{renderRowsTable(info.replication?.replicas || [])}</Space> },
      { key: "mgr", label: "MGR 状态", children: <Space direction="vertical" size={12} style={{ width: "100%" }}>{renderInfoCards(info.mgr?.variables || {})}{renderRowsTable(info.mgr?.members || [])}</Space> },
    ]) },
    { key: "performance", label: "性能与 InnoDB", children: collapse([
      { key: "innodb_config", label: "InnoDB 参数", children: renderInfoCards(info.innodb_config) },
      { key: "innodb_status", label: "InnoDB 状态", children: renderInnoDBStatus() },
      { key: "temp", label: "临时表 / 排序 / JOIN", children: renderInfoCards(info.temp_sort_join) },
      { key: "switch", label: "性能开关", children: <Space direction="vertical" size={12} style={{ width: "100%" }}>{renderInfoCards(info.performance_switch)}{renderRowsTable(info.performance_switch?.statement_consumers || [])}</Space> },
    ]) },
    { key: "capacity", label: "容量与对象", children: collapse([
      { key: "schema", label: "Schema 概览", children: renderSchemaOverview() },
      { key: "table_health", label: "表健康", children: renderInfoCards(info.table_health) },
      { key: "objects", label: "过程 / 触发器 / 事件", children: renderInfoCards(info.program_objects) },
      { key: "engines", label: "引擎与插件", children: <Space direction="vertical" size={12} style={{ width: "100%" }}>{renderRowsTable(info.engines_plugins?.engines || [])}{renderRowsTable(info.engines_plugins?.plugins || [])}</Space> },
    ]) },
    { key: "security", label: "安全与账号", children: collapse([
      { key: "account", label: "当前连接与授权", children: renderInfoCards(info.connection_context) },
      { key: "security", label: "账号风险", children: <Space direction="vertical" size={12} style={{ width: "100%" }}>{info.security?.root_remote_login > 0 && <Alert type="error" showIcon message="存在 root@% 远程登录风险" />}{renderInfoCards(info.security)}{renderRowsTable(info.security?.users || [])}</Space> },
      { key: "ssl", label: "SSL / 认证", children: renderInfoCards(info.ssl_auth) },
    ]) },
    { key: "raw", label: "原始数据", children: <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <Space wrap>
        <Input.Search style={{ width: 280 }} placeholder="搜索变量名或值" allowClear onSearch={setRawSearch} onChange={(e) => setRawSearch(e.target.value)} />
        <Checkbox checked={rawOnlyAbnormal} onChange={(e) => setRawOnlyAbnormal(e.target.checked)}>只看异常/关闭项</Checkbox>
        <Button onClick={() => copyJson(raw)}>复制 JSON</Button>
        <Button onClick={() => exportJson(raw, `mysql_basic_raw_${instance.instance_name || "instance"}.json`)}>导出 JSON</Button>
      </Space>
      <Tabs items={[{ key: "variables", label: "原始变量", children: renderKeyValueTable(raw.variables || {}) }, { key: "status", label: "原始状态", children: renderKeyValueTable(raw.status || {}) }]} />
    </Space> },
  ];
  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <MySQLBasicOverview
        data={data}
        info={info}
        instance={instance}
        summary={summary}
        basic={b}
        checks={checks}
        risks={risks}
        riskCounts={riskCounts}
        riskColor={riskColor}
        riskText={riskText}
        statusColor={statusColor}
        num={num}
        boolLabel={boolLabel}
      />
      <Card title="详情分析">
        <Tabs items={detailTabs} />
      </Card>
    </Space>
  );
}
