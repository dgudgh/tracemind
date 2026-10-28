import React, { useState } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Empty,
  Form,
  Input,
  InputNumber,
  Row,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from "antd";
import { mysqlOpsApi } from "./apiClients";
import { PlanOnlyScriptModal } from "./OpsLayout";
import { mysqlSchemaName, mysqlTableName } from "./MySQLObjectNameHelpers";
import { stableRecordKey } from "./modules/stableKeys";

const { Text } = Typography;

export function MySQLStatisticsTab({ data, instanceId }) {
  const summary = data.summary || {};
  const sections = data.sections || {};
  const risks = data.risk_items || [];
  const warnings = data.warnings || [];
  const [plan, setPlan] = useState(null);
  const [planOpen, setPlanOpen] = useState(false);
  const [planLoading, setPlanLoading] = useState(false);
  const [form] = Form.useForm();
  const fmt = (v, suffix = "") => (v === null || v === undefined || v === "" ? "-" : `${v}${suffix}`);
  const fmtNum = (v) => {
    const n = Number(v || 0);
    return Number.isNaN(n) ? fmt(v) : n.toLocaleString();
  };
  const riskColor = (level) => level === "critical" ? "red" : level === "warning" ? "orange" : level === "info" ? "blue" : "green";
  const normalizeStatsRows = (rows) => (rows || []).map((row) => ({
    ...row,
    table_schema: mysqlSchemaName(row),
    table_name: mysqlTableName(row),
  }));
  const rowKey = (r, i) => `${mysqlSchemaName(r)}.${mysqlTableName(r)}.${r.index_name || r.INDEX_NAME || r.partition_name || r.PARTITION_NAME || r.column_name || r.COLUMN_NAME || ""}.${i}`;
  const planRequest = async (path, payload) => {
    setPlanLoading(true);
    try {
      setPlan(await mysqlOpsApi.tableStatisticsPlan(instanceId, path, payload));
      setPlanOpen(true);
    } catch (e) {
      message.error(e.message || "生成方案失败");
    } finally {
      setPlanLoading(false);
    }
  };
  const tableColumns = [
    { title: "Schema", dataIndex: "table_schema", width: 170, ellipsis: true },
    { title: "表名", dataIndex: "table_name", width: 220, ellipsis: true },
    { title: "引擎", dataIndex: "engine", width: 90 },
    { title: "估算行数", dataIndex: "estimated_rows", width: 120, render: fmtNum },
    { title: "总大小(MB)", dataIndex: "total_mb", width: 120 },
    { title: "数据(MB)", dataIndex: "data_mb", width: 110 },
    { title: "索引(MB)", dataIndex: "index_mb", width: 110 },
    { title: "碎片(MB)", dataIndex: "free_mb", width: 110 },
    { title: "统计状态", dataIndex: "stats_status", width: 220, ellipsis: true },
    { title: "风险", dataIndex: "risk_level", width: 100, render: (v) => <Tag color={riskColor(v)}>{v || "normal"}</Tag> },
    { title: "更新时间", dataIndex: "update_time", width: 170, render: fmt },
    { title: "字符集", dataIndex: "table_collation", width: 170, ellipsis: true },
  ];
  const cardinalityColumns = [
    { title: "Schema", dataIndex: "table_schema", width: 160, ellipsis: true },
    { title: "表名", dataIndex: "table_name", width: 210, ellipsis: true },
    { title: "索引", dataIndex: "index_name", width: 220, ellipsis: true },
    { title: "序号", dataIndex: "seq_in_index", width: 80 },
    { title: "列", dataIndex: "column_name", width: 180, ellipsis: true },
    { title: "Cardinality", dataIndex: "cardinality", width: 120, render: fmtNum },
    { title: "估算行数", dataIndex: "table_rows", width: 120, render: fmtNum },
    { title: "选择性", dataIndex: "selectivity_pct", width: 110, render: (v) => fmt(v, "%") },
    { title: "类型", dataIndex: "index_type", width: 110 },
    { title: "风险", dataIndex: "risk_level", width: 100, render: (v) => <Tag color={riskColor(v)}>{v || "normal"}</Tag> },
    { title: "说明", dataIndex: "risk", width: 260, ellipsis: true },
  ];
  const renderTable = (rows, columns, x = 1400) => (
    <Table dataSource={normalizeStatsRows(rows)} rowKey={rowKey} size="small" pagination={{ defaultPageSize: 20, showSizeChanger: true, pageSizeOptions: ["20", "50", "100"] }} scroll={{ x }} tableLayout="fixed" columns={columns} />
  );
  const metricCards = [
    ["表总数", summary.table_count],
    ["已分析表", summary.analyzed_table_count],
    ["陈旧统计表", summary.stale_stat_table_count],
    ["行数估算风险", summary.row_estimation_risk_count],
    ["低选择性索引", summary.low_cardinality_index_count],
    ["已有直方图", summary.histogram_count],
    ["直方图候选", summary.histogram_candidate_count],
    ["分区统计异常", summary.partition_stat_risk_count],
    ["风险等级", summary.risk_level || "normal"],
  ];
  const overview = (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Row gutter={[12, 12]}>
        {metricCards.map(([label, value]) => (
          <Col xs={12} sm={8} md={6} xl={4} key={label}>
            <Card size="small" bodyStyle={{ minHeight: 78, padding: 12 }}>
              <Text type="secondary" style={{ display: "block", fontSize: 12 }}>{label}</Text>
              <Text strong style={{ display: "block", marginTop: 8, fontSize: 18, wordBreak: "break-all" }}>{fmt(value)}</Text>
            </Card>
          </Col>
        ))}
      </Row>
      <Alert
        type={summary.risk_level === "critical" ? "error" : summary.risk_level === "warning" ? "warning" : "success"}
        showIcon
        message={`表统计信息健康：${summary.risk_level || "normal"}`}
        description="InnoDB table_rows 是估算行数，不是精确 COUNT。平台默认不对大表执行 COUNT(*)，所有维护动作只生成脚本。"
      />
      <Row gutter={[12, 12]}>
        {(risks.length ? risks : [{ level: "normal", item: "统计信息健康", message: "当前未发现明显统计信息风险。" }]).map((item) => (
          <Col xs={24} md={12} xl={8} key={stableRecordKey("mysql-statistics-risk", item, ["item", "level", "message"])}>
            <Card size="small">
              <Space align="start">
                <Tag color={riskColor(item.level)}>{item.level || "normal"}</Tag>
                <div><Text strong>{item.item}</Text><div style={{ marginTop: 6 }}>{item.message}</div>{item.suggestion && <Text type="secondary">{item.suggestion}</Text>}</div>
              </Space>
            </Card>
          </Col>
        ))}
      </Row>
    </Space>
  );
  const maintenance = (
    <Card title="维护方案生成" size="small">
      <Form form={form} layout="vertical" initialValues={{ buckets: 100 }}>
        <Row gutter={12}>
          <Col xs={24} md={6}><Form.Item name="schema" label="Schema" rules={[{ required: true }]}><Input placeholder="MES" /></Form.Item></Col>
          <Col xs={24} md={8}><Form.Item name="table_name" label="表名" rules={[{ required: true }]}><Input placeholder="TT_WO_WIP" /></Form.Item></Col>
          <Col xs={24} md={6}><Form.Item name="column_name" label="列名"><Input placeholder="可选，直方图使用" /></Form.Item></Col>
          <Col xs={24} md={4}><Form.Item name="buckets" label="Buckets"><InputNumber min={1} max={1024} style={{ width: "100%" }} /></Form.Item></Col>
        </Row>
        <Space wrap>
          <Button type="primary" loading={planLoading} onClick={() => {
            const v = form.getFieldsValue();
            planRequest("analyze-plan", { schema: v.schema, table_name: v.table_name });
          }}>生成 ANALYZE TABLE</Button>
          <Button loading={planLoading} onClick={() => planRequest("histogram-plan", form.getFieldsValue())}>生成 Histogram</Button>
          <Button danger loading={planLoading} onClick={() => planRequest("drop-histogram-plan", form.getFieldsValue())}>删除 Histogram 方案</Button>
        </Space>
      </Form>
    </Card>
  );
  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      {data.error && <Alert type="error" showIcon message="表统计信息采集失败" description={data.error} />}
      {warnings.length > 0 && <Alert type="warning" showIcon message="部分信息降级采集" description={warnings.join("；")} />}
      <Tabs
        items={[
          { key: "overview", label: "总览", children: overview },
          { key: "tables", label: "统计健康", children: renderTable(sections.table_stats, tableColumns) },
          { key: "stale", label: "陈旧统计", children: renderTable(sections.stale_stats, tableColumns) },
          { key: "cardinality", label: "索引基数", children: renderTable(sections.cardinality, cardinalityColumns, 1700) },
          { key: "histogram", label: "直方图", children: <Space direction="vertical" size={12} style={{ width: "100%" }}>
            <Alert
              type="info"
              showIcon
              message="MySQL 只有执行过 UPDATE HISTOGRAM 后才会产生已有 Histogram。"
              description="这里为空表示当前实例未发现已创建的列直方图；可先查看候选列，再按需生成 Histogram 方案，所有操作只生成脚本。"
            />
            <Card title="已有 Histogram" size="small">
              {(sections.histograms || []).length ? renderTable(sections.histograms, [
                { title: "Schema", dataIndex: "table_schema", width: 180 },
                { title: "表名", dataIndex: "table_name", width: 220 },
                { title: "列名", dataIndex: "column_name", width: 180 },
                { title: "Buckets", dataIndex: "buckets", width: 120 },
                { title: "类型", dataIndex: "histogram_type", width: 160 },
                { title: "更新时间", dataIndex: "last_updated", width: 200 },
              ], 1100) : <Empty description="当前未发现已创建的 Histogram" />}
            </Card>
            <Card title="候选列" size="small">{renderTable(sections.histogram_candidates, [
              { title: "Schema", dataIndex: "table_schema", width: 180 },
              { title: "表名", dataIndex: "table_name", width: 220 },
              { title: "列名", dataIndex: "column_name", width: 180 },
              { title: "风险", dataIndex: "risk_level", width: 100, render: (v) => <Tag color={riskColor(v)}>{v}</Tag> },
              { title: "原因", dataIndex: "reason" },
            ], 1100)}</Card>
          </Space> },
          { key: "partitions", label: "分区统计", children: renderTable(sections.partition_stats, [
            { title: "Schema", dataIndex: "table_schema", width: 170 },
            { title: "表名", dataIndex: "table_name", width: 220 },
            { title: "分区", dataIndex: "partition_name", width: 180 },
            { title: "估算行数", dataIndex: "table_rows", width: 120, render: fmtNum },
            { title: "大小(MB)", dataIndex: "total_mb", width: 120 },
            { title: "不均衡比", dataIndex: "imbalance_ratio", width: 110 },
            { title: "风险", dataIndex: "risk_level", width: 100, render: (v) => <Tag color={riskColor(v)}>{v || "normal"}</Tag> },
            { title: "更新时间", dataIndex: "update_time", width: 170, render: fmt },
          ], 1300) },
          { key: "planRisk", label: "执行计划风险", children: renderTable(sections.plan_risks, [
            { title: "级别", dataIndex: "level", width: 100, render: (v) => <Tag color={riskColor(v)}>{v}</Tag> },
            { title: "Schema", dataIndex: "table_schema", width: 170 },
            { title: "表名", dataIndex: "table_name", width: 220 },
            { title: "对象", dataIndex: "item", width: 220, ellipsis: true },
            { title: "说明", dataIndex: "message", width: 420 },
            { title: "建议", dataIndex: "suggestion", width: 420 },
          ], 1500) },
          { key: "config", label: "统计配置", children: <Space direction="vertical" size={12} style={{ width: "100%" }}>
            <Descriptions bordered size="small" column={1}>{Object.entries(sections.config?.variables || {}).map(([k, v]) => <Descriptions.Item key={k} label={k}>{fmt(v)}</Descriptions.Item>)}</Descriptions>
            {renderTable(sections.config?.risks, [{ title: "级别", dataIndex: "level", width: 100, render: (v) => <Tag color={riskColor(v)}>{v}</Tag> }, { title: "参数", dataIndex: "item", width: 260 }, { title: "说明", dataIndex: "message" }], 900)}
          </Space> },
          { key: "maintenance", label: "维护方案", children: maintenance },
        ]}
      />
      <PlanOnlyScriptModal
        title="统计信息维护方案"
        open={planOpen}
        script={plan?.sql}
        warnings={plan?.warnings || []}
        width={860}
        onClose={() => setPlanOpen(false)}
      />
    </Space>
  );
}
