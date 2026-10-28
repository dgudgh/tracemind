import React, { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Modal,
  Row,
  Segmented,
  Space,
  Statistic,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from "antd";
import { mysqlOpsApi } from "./apiClients";
import { stableRecordKey } from "./modules/stableKeys";

const { Text } = Typography;

export function MySQLSlowSqlTab({ data: initialData, instanceId }) {
  const [detail, setDetail] = useState(null);
  const [data, setData] = useState(initialData || {});
  const [timeRange, setTimeRange] = useState(initialData?.time_range?.key || "24h");
  const [rangeLoading, setRangeLoading] = useState(false);

  useEffect(() => {
    setData(initialData || {});
    if (initialData?.time_range?.key) {
      setTimeRange(initialData.time_range.key);
    }
  }, [initialData]);

  const timeRangeOptions = [
    { label: "1小时", value: "1h" },
    { label: "3小时", value: "3h" },
    { label: "12小时", value: "12h" },
    { label: "24小时", value: "24h" },
    { label: "3天", value: "3d" },
    { label: "7天", value: "7d" },
  ];
  const loadTimeRange = async (value) => {
    setTimeRange(value);
    if (!instanceId) return;
    setRangeLoading(true);
    try {
      const r = await mysqlOpsApi.slowSqlTop(instanceId, { time_range: value, limit: 200 });
      setData(r);
    } catch (e) {
      message.error(e.message || "慢 SQL 数据加载失败");
      setData({ ...(data || {}), error: e.message || "慢 SQL 数据加载失败" });
    } finally {
      setRangeLoading(false);
    }
  };
  const summary = data.summary || {};
  const rankings = data.rankings || {};
  const rows = data.digest_details || data.items || [];
  const risks = data.risk_items || [];
  const recommendations = data.recommendations || [];
  const config = data.config || {};
  const dataSourceText = data.data_source === "events_statements_history_long"
    ? "真实时间窗口样本"
    : "Digest 累计摘要";
  const valueText = (v) => (v === undefined || v === null || v === "" ? "-" : String(v));
  const fmtNum = (v) => {
    const n = Number(v || 0);
    if (Number.isNaN(n)) return valueText(v);
    return n.toLocaleString();
  };
  const riskColor = (level) => level === "critical" ? "red" : level === "warning" ? "orange" : "green";
  const rankingLabels = {
    by_avg_time: "平均耗时 Top",
    by_total_time: "总耗时 Top",
    by_max_time: "最大耗时 Top",
    by_executions: "执行次数 Top",
    by_rows_examined: "扫描行数 Top",
    by_rows_sent: "返回行数 Top",
    by_examined_sent_ratio: "扫描/返回比 Top",
    by_tmp_disk_tables: "磁盘临时表 Top",
    by_sort_rows: "排序行数 Top",
    by_full_join: "全表 Join Top",
    by_no_index_used: "无索引风险 Top",
  };
  const metricItems = [
    ["慢 SQL 数量", summary.slow_sql_count || rows.length || 0],
    ["严重 SQL", summary.critical_count || 0],
    ["警告 SQL", summary.warning_count || 0],
    ["最大平均耗时", `${summary.max_avg_seconds || 0} s`],
    ["最大单次耗时", `${summary.max_max_seconds || 0} s`],
    ["最高总耗时", `${summary.max_total_seconds || 0} s`],
    ["最高扫描行数", fmtNum(summary.max_rows_examined || 0)],
    ["磁盘临时表 SQL", summary.tmp_disk_sql_count || 0],
    ["全表 Join 风险", summary.full_join_sql_count || 0],
    ["无索引风险", summary.no_index_sql_count || 0],
    ["风险等级", summary.risk_level || "normal"],
    ["数据来源", dataSourceText],
  ];
  const columns = [
    { title: "风险", dataIndex: "risk_level", width: 90, fixed: "left", render: (v, r) => <Tag color={riskColor(v)}>{v || "normal"} / {r.risk_score ?? 0}</Tag> },
    { title: "Schema", dataIndex: "schema_name", width: 130, fixed: "left", render: valueText },
    { title: "SQL 指纹", dataIndex: "digest_text", width: 420, ellipsis: true, render: valueText },
    { title: "执行次数", dataIndex: "executions", width: 110, render: fmtNum },
    { title: "总耗时(s)", dataIndex: "total_seconds", width: 110 },
    { title: "平均(s)", dataIndex: "avg_seconds", width: 100 },
    { title: "最大(s)", dataIndex: "max_seconds", width: 100 },
    { title: "扫描行", dataIndex: "rows_examined", width: 120, render: fmtNum },
    { title: "返回行", dataIndex: "rows_sent", width: 120, render: fmtNum },
    { title: "扫描/返回", dataIndex: "examined_sent_ratio", width: 120, render: valueText },
    { title: "临时表", dataIndex: "tmp_tables", width: 100, render: fmtNum },
    { title: "磁盘临时表", dataIndex: "tmp_disk_tables", width: 120, render: fmtNum },
    { title: "排序行", dataIndex: "sort_rows", width: 120, render: fmtNum },
    { title: "全表 Join", dataIndex: "select_full_join", width: 110, render: fmtNum },
    { title: "无索引", dataIndex: "no_index_used", width: 100, render: fmtNum },
    { title: "首次出现", dataIndex: "first_seen", width: 170, render: valueText },
    { title: "最后出现", dataIndex: "last_seen", width: 170, render: valueText },
    { title: "操作", width: 90, fixed: "right", render: (_, r) => <Button size="small" onClick={() => setDetail(r)}>详情</Button> },
  ];
  const renderTable = (items) => (
    <Table
      dataSource={items || []}
      rowKey={(row) => stableRecordKey("mysql-slow-sql", row, ["digest", "schema_name", "digest_text", "first_seen", "last_seen"])}
      size="small"
      pagination={{ defaultPageSize: 20, showSizeChanger: true }}
      scroll={{ x: 2200 }}
      columns={columns}
    />
  );
  const riskCards = (
    <Row gutter={[12, 12]}>
      {(risks.length ? risks : [{ level: "normal", title: "当前未发现明显慢 SQL 风险", message: "当前没有明显慢 SQL 风险项。", suggestion: "继续观察慢日志、执行计划和表索引变化。" }]).map((item, idx) => (
        <Col xs={24} lg={12} xl={8} key={stableRecordKey("mysql-slow-risk", item, ["level", "title", "message"])}>
          <Card size="small" bodyStyle={{ minHeight: 120 }}>
            <Space direction="vertical" size={6} style={{ width: "100%" }}>
              <Space><Tag color={riskColor(item.level)}>{item.level || "normal"}</Tag><Text strong>{item.title}</Text></Space>
              <Text>{item.message}</Text>
              {item.suggestion && <Text type="secondary">{item.suggestion}</Text>}
            </Space>
          </Card>
        </Col>
      ))}
    </Row>
  );
  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Card size="small" bodyStyle={{ padding: 12 }}>
        <Space wrap>
          <Text strong>慢 SQL 时间范围</Text>
          <Segmented value={timeRange} options={timeRangeOptions} onChange={loadTimeRange} disabled={rangeLoading} />
          {rangeLoading && <Text type="secondary">加载中...</Text>}
        </Space>
      </Card>
      {data.error && <Alert type="error" showIcon message="慢 SQL 采集失败" description={data.error} />}
      {(data.warnings || []).length > 0 && <Alert type="warning" showIcon message="部分信息降级采集" description={(data.warnings || []).join("；")} />}
      {data.data_source !== "events_statements_history_long" && (
        <Alert
          type="info"
          showIcon
          message="当前使用累计摘要"
          description="该实例未能读取 events_statements_history_long，时间切换只能按最近出现时间过滤 Digest，执行次数和耗时仍可能是累计值。开启 Performance Schema statement history long 后可按时间窗口精确统计。"
        />
      )}
      <Card title="慢 SQL 诊断总览" size="small">
        <Row gutter={[12, 12]}>
          {metricItems.map(([title, value]) => (
            <Col xs={24} sm={12} md={8} xl={6} xxl={4} key={title}>
              <Card size="small" bodyStyle={{ minHeight: 78 }}>
                <Statistic title={title} value={valueText(value)} valueStyle={{ fontSize: 16 }} />
              </Card>
            </Col>
          ))}
        </Row>
      </Card>
      <Card title="风险诊断" size="small">{riskCards}</Card>
      <Card title="多维 SQL 排行" size="small">
        <Tabs
          items={Object.entries(rankingLabels).map(([key, label]) => ({
            key,
            label,
            children: renderTable(rankings[key] || rows),
          }))}
        />
      </Card>
      <Card title="优化建议" size="small">
        <Table
          dataSource={recommendations}
          rowKey={(row) => stableRecordKey("mysql-slow-recommendation", row, ["digest", "schema_name", "digest_text", "risk_level"])}
          size="small"
          pagination={{ defaultPageSize: 10, showSizeChanger: true }}
          scroll={{ x: 1400 }}
          columns={[
            { title: "风险", dataIndex: "risk_level", width: 90, render: (v, r) => <Tag color={riskColor(v)}>{v} / {r.risk_score}</Tag> },
            { title: "Schema", dataIndex: "schema_name", width: 120 },
            { title: "SQL 指纹", dataIndex: "digest_text", width: 420, ellipsis: true },
            { title: "涉及表", dataIndex: "tables", width: 260, render: (v) => (v || []).join(", ") || "-" },
            { title: "建议", dataIndex: "suggestions", render: (v) => (v || []).join("；") },
          ]}
        />
      </Card>
      <Card title="当前正在执行的慢 SQL" size="small">
        <Table
          dataSource={data.current_running || []}
          rowKey={(row) => stableRecordKey("mysql-running-slow-sql", row, ["process_id", "user", "host", "db", "time_seconds", "sql_text"])}
          size="small"
          pagination={{ defaultPageSize: 10, showSizeChanger: true }}
          scroll={{ x: 1300 }}
          columns={[
            { title: "线程", dataIndex: "process_id", width: 90 },
            { title: "用户", dataIndex: "user", width: 110 },
            { title: "来源", dataIndex: "host", width: 180, ellipsis: true },
            { title: "库", dataIndex: "db", width: 120, render: valueText },
            { title: "执行秒数", dataIndex: "time_seconds", width: 100 },
            { title: "状态", dataIndex: "state", width: 200, ellipsis: true, render: valueText },
            { title: "SQL", dataIndex: "sql_text", ellipsis: true, render: valueText },
          ]}
        />
      </Card>
      <Row gutter={[16, 16]}>
        <Col xs={24} xl={12}>
          <Card title="慢 SQL 配置检查" size="small">
            <Descriptions bordered size="small" column={1}>
              {Object.entries(config.variables || {}).map(([k, v]) => <Descriptions.Item key={k} label={k}>{valueText(v)}</Descriptions.Item>)}
            </Descriptions>
            {(config.suggestions || []).length > 0 && <Alert style={{ marginTop: 12 }} type="warning" showIcon message="配置建议" description={(config.suggestions || []).join("；")} />}
          </Card>
        </Col>
        <Col xs={24} xl={12}>
          <Card title="Performance Schema Consumers" size="small">
            <Table
              dataSource={config.consumers || []}
              rowKey="name"
              size="small"
              pagination={false}
              columns={[
                { title: "Consumer", dataIndex: "name" },
                { title: "启用", dataIndex: "enabled", width: 100, render: (v) => <Tag color={String(v).toUpperCase() === "YES" ? "green" : "orange"}>{valueText(v)}</Tag> },
              ]}
            />
          </Card>
        </Col>
      </Row>
      <Card title="表访问画像" size="small">
        <Table
          dataSource={data.table_profile || []}
          rowKey="table"
          size="small"
          pagination={false}
          columns={[
            { title: "表", dataIndex: "table", ellipsis: true },
            { title: "累计扫描行数", dataIndex: "rows_examined", width: 180, render: fmtNum },
          ]}
        />
      </Card>
      <Row gutter={[16, 16]}>
        <Col xs={24} xl={12}><Card title="趋势分析" size="small"><Alert type="info" showIcon message={data.trend?.message || "趋势需要快照入库后展示。"} /></Card></Col>
        <Col xs={24} xl={12}><Card title="慢日志文件采集" size="small"><Alert type="info" showIcon message={data.slow_log?.message || "未配置 SSH，当前仅基于 Performance Schema 分析。"} /></Card></Col>
      </Row>
      <Modal
        className="adaptive-center-modal"
        title="SQL 指纹详情"
        open={!!detail}
        width={920}
        centered
        footer={null}
        onCancel={() => setDetail(null)}
      >
        {detail && <Space direction="vertical" size={16} style={{ width: "100%" }}>
          <Card title="SQL 摘要" size="small"><pre style={{ whiteSpace: "pre-wrap" }}>{detail.digest_text}</pre></Card>
          <Card title="执行统计" size="small">
            <Descriptions bordered size="small" column={2}>
              {[
                ["Schema", detail.schema_name],
                ["执行次数", fmtNum(detail.executions)],
                ["总耗时(s)", detail.total_seconds],
                ["平均耗时(s)", detail.avg_seconds],
                ["最大耗时(s)", detail.max_seconds],
                ["扫描行", fmtNum(detail.rows_examined)],
                ["返回行", fmtNum(detail.rows_sent)],
                ["扫描/返回比", detail.examined_sent_ratio],
                ["临时表", fmtNum(detail.tmp_tables)],
                ["磁盘临时表", fmtNum(detail.tmp_disk_tables)],
                ["排序行", fmtNum(detail.sort_rows)],
                ["全表 Join", fmtNum(detail.select_full_join)],
                ["无索引使用", fmtNum(detail.no_index_used)],
                ["风险评分", `${detail.risk_score} / ${detail.risk_level}`],
              ].map(([label, value]) => <Descriptions.Item key={label} label={label}>{valueText(value)}</Descriptions.Item>)}
            </Descriptions>
          </Card>
          <Card title="涉及表与建议" size="small">
            <Descriptions bordered size="small" column={1}>
              <Descriptions.Item label="涉及表">{(detail.tables || []).join(", ") || "-"}</Descriptions.Item>
              <Descriptions.Item label="优化建议"><ol style={{ margin: 0, paddingLeft: 18 }}>{(detail.recommendations || []).map((item) => <li key={item}>{item}</li>)}</ol></Descriptions.Item>
            </Descriptions>
          </Card>
          <Alert type="info" showIcon message="执行计划" description="EXPLAIN 详情需要基于真实 SQL 原文或等价 SELECT。Digest Text 是归一化指纹，不能直接安全执行 EXPLAIN。" />
        </Space>}
      </Modal>
    </Space>
  );
}
