import React, { useState } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Empty,
  Form,
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
import { stableRecordKey } from "./modules/stableKeys";

const { Text } = Typography;

export function MySQLTableIndexCenterTab({ data, instanceId }) {
  const summary = data.summary || {};
  const tables = data.tables || {};
  const indexes = data.indexes || {};
  const access = data.access_profile || {};
  const risks = data.risk_items || [];
  const [plan, setPlan] = useState(null);
  const [planOpen, setPlanOpen] = useState(false);
  const [planLoading, setPlanLoading] = useState(false);
  const [form] = Form.useForm();
  const fmt = (v, suffix = "") => (v === null || v === undefined || v === "" ? "-" : `${v}${suffix}`);
  const riskColor = (level) => level === "critical" ? "red" : level === "high" ? "orange" : level === "warning" ? "gold" : "green";
  const normalizeRows = (rows) => (rows || []).map((row) => ({
    ...row,
    table_schema: mysqlSchemaName(row),
    table_name: mysqlTableName(row),
    engine: row.engine || row.ENGINE || row.Engine,
    table_rows: row.table_rows ?? row.TABLE_ROWS ?? row.Table_rows,
    update_time: row.update_time || row.UPDATE_TIME || row.Update_time || row.create_time || row.CREATE_TIME || row.Create_time || "-",
    create_time: row.create_time || row.CREATE_TIME || row.Create_time,
  }));
  const rowKey = (r, i) => `${mysqlSchemaName(r)}.${mysqlTableName(r)}.${r.index_name || r.INDEX_NAME || ""}.${i}`;

  const renderTable = (title, rows, columns) => (
    <Card title={title} size="small">
      <Table dataSource={normalizeRows(rows)} rowKey={rowKey} size="small" pagination={{ defaultPageSize: 10, showSizeChanger: true, pageSizeOptions: ["10", "20", "50", "100"] }} scroll={{ x: 1100 }} columns={columns} />
    </Card>
  );

  const metricCards = [
    ["Schema 数", summary.schema_count],
    ["表总数", summary.table_count],
    ["索引总数", summary.index_count],
    ["总容量", summary.total_size_gb, " GB"],
    ["数据容量", summary.data_size_gb, " GB"],
    ["索引容量", summary.index_size_gb, " GB"],
    ["大表数量", summary.large_table_count],
    ["碎片空间", summary.fragmentation_gb, " GB"],
    ["无主键表", summary.no_primary_key_table_count],
    ["非 InnoDB 表", summary.non_innodb_table_count],
    ["重复索引", summary.duplicate_index_count],
    ["未使用索引", summary.unused_index_count],
  ];
  const tableColumns = [
    { title: "库名", dataIndex: "table_schema", width: 190, ellipsis: true },
    { title: "表名", dataIndex: "table_name", width: 240, ellipsis: true },
    { title: "引擎", dataIndex: "engine", width: 95 },
    { title: "行数", dataIndex: "table_rows", width: 120 },
    { title: "总大小(MB)", dataIndex: "total_mb", width: 120 },
    { title: "数据(MB)", dataIndex: "data_mb", width: 110 },
    { title: "索引(MB)", dataIndex: "index_mb", width: 110 },
    { title: "更新时间", dataIndex: "update_time", width: 170 },
  ];
  const indexColumns = [
    { title: "库名", dataIndex: "table_schema", width: 180, ellipsis: true },
    { title: "表名", dataIndex: "table_name", width: 220, ellipsis: true },
    { title: "索引名", dataIndex: "index_name", width: 220, ellipsis: true },
    { title: "列", dataIndex: "columns", width: 320, ellipsis: true },
    { title: "类型", dataIndex: "unique_text", width: 100 },
    { title: "基数", dataIndex: "cardinality", width: 110 },
  ];

  const runPlan = async (path) => {
    const values = form.getFieldsValue();
    const payload = {
      schema: values.schema,
      table: values.table,
      index_name: values.index_name,
      archive_column: values.archive_column,
      keep_days: values.keep_days,
      columns: (values.columns || "").split(",").map(x => x.trim()).filter(Boolean),
    };
    setPlanLoading(true);
    try {
      setPlan(await mysqlOpsApi.tableIndexPlan(instanceId, path, payload));
    } catch (e) {
      message.error(e.message || "生成方案失败");
    } finally {
      setPlanLoading(false);
    }
  };

  const overviewTab = (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Row gutter={[12, 12]}>
        {metricCards.map(([label, value, suffix]) => (
          <Col xs={12} sm={8} md={6} xl={4} key={label}>
            <Card size="small"><Statistic title={label} value={value ?? 0} suffix={suffix || ""} /></Card>
          </Col>
        ))}
      </Row>
      <Alert
        type={summary.risk_level === "critical" ? "error" : summary.risk_level === "warning" ? "warning" : "success"}
        showIcon
        message={`综合风险：${summary.risk_level || "normal"}`}
        description="基于表容量、碎片、主键、存储引擎、索引重复、未使用索引和访问画像生成。所有维护动作只生成脚本，不直接执行。"
      />
      <Row gutter={[12, 12]}>
        {(risks.length ? risks : [{ level: "normal", item: "表与索引健康", message: "当前未发现明显风险。" }]).map((r) => (
          <Col xs={24} md={12} xl={8} key={stableRecordKey("mysql-table-index-risk", r, ["item", "level", "message"])}>
            <Card size="small">
              <Space align="start"><Tag color={riskColor(r.level)}>{r.level}</Tag><div><Text strong>{r.item}</Text><div style={{ marginTop: 6 }}>{r.message}</div></div></Space>
            </Card>
          </Col>
        ))}
      </Row>
      {renderTable("Schema 概览", tables.schema_overview, [
        { title: "库名", dataIndex: "table_schema", width: 220, ellipsis: true },
        { title: "表数", dataIndex: "table_count", width: 100 },
        { title: "总容量(GB)", dataIndex: "total_gb", width: 120 },
        { title: "数据(GB)", dataIndex: "data_gb", width: 110 },
        { title: "索引(GB)", dataIndex: "index_gb", width: 110 },
        { title: "碎片(GB)", dataIndex: "free_gb", width: 110 },
        { title: "统计行数", dataIndex: "table_rows", width: 140 },
      ])}
    </Space>
  );

  const tableHealthTab = (
    <Tabs type="card" items={[
      { key: "large", label: "大表分析", children: renderTable("大表排行", tables.large_tables, tableColumns) },
      { key: "frag", label: "表碎片", children: renderTable("表碎片分析", tables.fragmentation, [...tableColumns.slice(0, 5), { title: "碎片(MB)", dataIndex: "free_mb", width: 120 }, { title: "碎片率", dataIndex: "frag_pct", width: 100, render: v => fmt(v, "%") }]) },
      { key: "nopk", label: "无主键表", children: renderTable("无主键表", tables.no_primary_key, tableColumns.slice(0, 5)) },
      { key: "engine", label: "非 InnoDB 表", children: renderTable("非 InnoDB 表", tables.non_innodb, tableColumns.slice(0, 5)) },
      { key: "part", label: "分区表", children: renderTable("分区表分析", tables.partitioned, [
        { title: "库名", dataIndex: "table_schema", width: 190, ellipsis: true },
        { title: "表名", dataIndex: "table_name", width: 240, ellipsis: true },
        { title: "分区数", dataIndex: "partition_count", width: 100 },
        { title: "分区方式", dataIndex: "partition_method", width: 140 },
        { title: "分区表达式", dataIndex: "partition_expression", width: 360, ellipsis: true },
      ]) },
      { key: "auto", label: "自增风险", children: renderTable("自增列风险", tables.auto_increment_risk, [
        { title: "库名", dataIndex: "table_schema", width: 190, ellipsis: true },
        { title: "表名", dataIndex: "table_name", width: 240, ellipsis: true },
        { title: "列名", dataIndex: "column_name", width: 180 },
        { title: "类型", dataIndex: "column_type", width: 160 },
        { title: "当前值", dataIndex: "auto_increment", width: 140 },
        { title: "使用率", dataIndex: "usage_pct", width: 120, render: v => fmt(v, "%") },
      ]) },
      { key: "stats", label: "表统计信息", children: renderTable("表统计信息", tables.statistics, tableColumns) },
    ]} />
  );

  const indexHealthTab = (
    <Tabs type="card" items={[
      { key: "overview", label: "索引总览", children: renderTable("索引总览", indexes.index_overview, indexColumns) },
      { key: "dup", label: "重复索引", children: renderTable("重复索引", indexes.duplicate_indexes, [...indexColumns, { title: "重复组", dataIndex: "duplicate_group", width: 260, ellipsis: true }, { title: "风险", dataIndex: "risk", width: 200 }]) },
      { key: "red", label: "冗余索引", children: renderTable("冗余索引", indexes.redundant_indexes, [...indexColumns.slice(0, 4), { title: "覆盖索引", dataIndex: "covered_by_index", width: 220, ellipsis: true }, { title: "覆盖列", dataIndex: "covered_by_columns", width: 320, ellipsis: true }, { title: "建议", dataIndex: "risk", width: 280 }]) },
      { key: "low", label: "低选择性索引", children: renderTable("低选择性索引", indexes.low_cardinality_indexes, [...indexColumns, { title: "表行数", dataIndex: "table_rows", width: 120 }, { title: "选择性", dataIndex: "selectivity_pct", width: 120, render: v => fmt(v, "%") }]) },
      { key: "unused", label: "未使用索引", children: renderTable("未使用索引", indexes.unused_indexes, [
        { title: "库名", dataIndex: "table_schema", width: 190, ellipsis: true },
        { title: "表名", dataIndex: "table_name", width: 240, ellipsis: true },
        { title: "索引名", dataIndex: "index_name", width: 240, ellipsis: true },
      ]) },
      { key: "missing", label: "缺失索引建议", children: renderTable("缺失索引候选", indexes.missing_index_suggestions, [
        { title: "库名", dataIndex: "table_schema", width: 190, ellipsis: true },
        { title: "表名", dataIndex: "table_name", width: 240, ellipsis: true },
        { title: "证据", dataIndex: "evidence", width: 420 },
        { title: "建议", dataIndex: "suggestion", width: 420 },
      ]) },
    ]} />
  );

  const accessTab = (
    <Tabs type="card" items={[
      { key: "hotTables", label: "热点表", children: renderTable("热点表访问画像", access.hot_tables, [
        { title: "库名", dataIndex: "table_schema", width: 190, ellipsis: true },
        { title: "表名", dataIndex: "table_name", width: 240, ellipsis: true },
        { title: "读次数", dataIndex: "count_read", width: 120 },
        { title: "写次数", dataIndex: "count_write", width: 120 },
        { title: "Fetch", dataIndex: "count_fetch", width: 120 },
        { title: "Insert", dataIndex: "count_insert", width: 120 },
        { title: "Update", dataIndex: "count_update", width: 120 },
        { title: "Delete", dataIndex: "count_delete", width: 120 },
        { title: "等待秒", dataIndex: "wait_sec", width: 120 },
      ]) },
      { key: "hotIndexes", label: "热点索引", children: renderTable("索引访问画像", access.hot_indexes, [
        { title: "库名", dataIndex: "table_schema", width: 190, ellipsis: true },
        { title: "表名", dataIndex: "table_name", width: 240, ellipsis: true },
        { title: "索引名", dataIndex: "index_name", width: 240, ellipsis: true },
        { title: "读次数", dataIndex: "count_read", width: 120 },
        { title: "Fetch", dataIndex: "count_fetch", width: 120 },
        { title: "等待秒", dataIndex: "wait_sec", width: 120 },
      ]) },
      { key: "fullScan", label: "全表扫描风险", children: renderTable("全表扫描风险", access.full_scan_risk, [
        { title: "库名", dataIndex: "table_schema", width: 190, ellipsis: true },
        { title: "表名", dataIndex: "table_name", width: 240, ellipsis: true },
        { title: "读次数", dataIndex: "count_read", width: 120 },
        { title: "Fetch", dataIndex: "count_fetch", width: 120 },
        { title: "等待秒", dataIndex: "wait_sec", width: 120 },
      ]) },
    ]} />
  );

  const planTab = (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Card title="维护方案生成" size="small">
        <Form form={form} layout="vertical" initialValues={{ keep_days: 180 }}>
          <Row gutter={[12, 0]}>
            <Col xs={24} md={8}>
              <Form.Item label="库名" name="schema" rules={[{ required: true, message: "请输入库名" }]}><Input placeholder="例如 MES" /></Form.Item>
            </Col>
            <Col xs={24} md={8}>
              <Form.Item label="表名" name="table" rules={[{ required: true, message: "请输入表名" }]}><Input placeholder="例如 TT_WO_WIP" /></Form.Item>
            </Col>
            <Col xs={24} md={8}>
              <Form.Item label="索引名" name="index_name"><Input placeholder="新增或删除索引时填写" /></Form.Item>
            </Col>
            <Col xs={24} md={8}>
              <Form.Item label="列名" name="columns"><Input placeholder="多个列用英文逗号分隔" /></Form.Item>
            </Col>
            <Col xs={24} md={8}>
              <Form.Item label="归档时间列" name="archive_column"><Input placeholder="默认 create_time" /></Form.Item>
            </Col>
            <Col xs={24} md={8}>
              <Form.Item label="保留天数" name="keep_days"><InputNumber min={1} style={{ width: "100%" }} /></Form.Item>
            </Col>
          </Row>
          <Space wrap>
            <Button loading={planLoading} onClick={() => runPlan("analyze-table-plan")}>ANALYZE</Button>
            <Button loading={planLoading} onClick={() => runPlan("optimize-table-plan")}>OPTIMIZE</Button>
            <Button loading={planLoading} onClick={() => runPlan("add-index-plan")}>新增索引</Button>
            <Button loading={planLoading} danger onClick={() => runPlan("drop-index-plan")}>删除索引</Button>
            <Button loading={planLoading} onClick={() => runPlan("add-primary-key-plan")}>添加主键</Button>
            <Button loading={planLoading} onClick={() => runPlan("archive-suggestion")}>归档建议</Button>
          </Space>
        </Form>
      </Card>
      <Card title="生成结果" size="small" extra={plan?.script && <Button size="small" icon={<CopyOutlined />} onClick={() => copyText(plan.script).then(() => message.success("已复制")).catch(() => message.error("复制失败"))}>复制脚本</Button>}>
        {plan ? (
          <Space direction="vertical" style={{ width: "100%" }}>
            <Alert type="info" showIcon message={plan.message || "已生成方案"} description={(plan.warnings || []).join("；")} />
            <pre style={{ whiteSpace: "pre-wrap", maxHeight: 460, overflow: "auto" }}>{plan.script || plan.sql || JSON.stringify(plan, null, 2)}</pre>
          </Space>
        ) : <Empty description="填写库表后生成维护方案" />}
      </Card>
    </Space>
  );

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      {data.error && <Alert type="error" showIcon message={data.error} />}
      {(data.warnings || []).length > 0 && <Alert type="warning" showIcon message="部分数据采集失败" description={(data.warnings || []).join("；")} />}
      <Tabs items={[
        { key: "overview", label: "总览", children: overviewTab },
        { key: "tables", label: "表健康", children: tableHealthTab },
        { key: "indexes", label: "索引健康", children: indexHealthTab },
        { key: "access", label: "表访问画像", children: accessTab },
        { key: "plan", label: "维护方案", children: planTab },
      ]} />
    </Space>
  );
}

export { MySQLStatisticsTab } from "./MySQLStatisticsTab";
