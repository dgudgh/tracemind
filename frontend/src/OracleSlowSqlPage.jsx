import React, { useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Card, Descriptions, Empty, Modal, Segmented, Space, Table, Tabs, Typography } from "antd";
import { DownloadOutlined, SearchOutlined, ThunderboltOutlined } from "@ant-design/icons";
import { collectSlowSql, getOracleSlowSqlRealtime, getOracleSqlDetail } from "./modules/inspectionApi";
import { saveTextFile } from "./modules/fileExport";
import { SlowSqlAIAnalysisPanel } from "./SlowSqlAIAnalysisPanel";
import { matchingSlowSqlSnapshot, SLOW_SQL_CATEGORIES, slowSqlConclusion, slowSqlFields, slowSqlReportHtml, slowSqlSnapshot } from "./SlowSqlResult";

export function OracleSlowSqlPage({ instances, selectedInstanceId, executionResult }) {
  const instance = instances.find((item) => String(item.id) === String(selectedInstanceId) && item.db_type === "oracle")
    || (!selectedInstanceId ? instances.find((item) => item.db_type === "oracle") : null);
  const instanceId = instance?.id;
  const incoming = matchingSlowSqlSnapshot(executionResult?.slowSql, instanceId);
  const [category, setCategory] = useState(incoming?.category || "top_avg");
  const [hours, setHours] = useState(incoming?.hours || 1);
  const [refresh, setRefresh] = useState(0);
  const [snapshot, setSnapshot] = useState(incoming);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [collection, setCollection] = useState({ loading: false, message: "", failed: false });
  const [detail, setDetail] = useState(null);
  const detailRequest = useRef(0);
  const detailAbort = useRef(null);
  const collectionRequest = useRef(0);

  useEffect(() => {
    setDetail(null);
    detailRequest.current += 1;
    detailAbort.current?.abort();
    return () => { detailRequest.current += 1; detailAbort.current?.abort(); };
  }, [instanceId]);

  useEffect(() => {
    collectionRequest.current += 1;
    setCollection({ loading: false, message: "", failed: false });
    return () => { collectionRequest.current += 1; };
  }, [instanceId, hours]);

  useEffect(() => {
    let disposed = false;
    const controller = new window.AbortController();
    setError("");
    const reused = refresh === 0 && incoming?.category === category && incoming?.hours === hours ? incoming : null;
    if (reused) {
      setSnapshot(reused);
      setLoading(false);
      return () => { disposed = true; };
    }
    setSnapshot(null);
    if (!instanceId) { setLoading(false); return undefined; }
    setLoading(true);
    const timer = setTimeout(() => controller.abort(), 60_000);
    getOracleSlowSqlRealtime(instanceId, { category, hours }, { signal: controller.signal })
      .then((data) => { if (!disposed) setSnapshot(slowSqlSnapshot(data, instanceId, { category, hours })); })
      .catch((reason) => { if (!disposed) setError(reason.name === "AbortError" ? "慢 SQL 查询超时，未取得结果" : reason.message || "慢 SQL 查询失败"); })
      .finally(() => { clearTimeout(timer); if (!disposed) setLoading(false); });
    return () => { disposed = true; clearTimeout(timer); controller.abort(); };
  }, [instanceId, category, hours, refresh, incoming]);

  const current = matchingSlowSqlSnapshot(snapshot, instanceId);
  const data = current?.data;
  const conclusion = useMemo(() => current ? slowSqlConclusion(current) : null, [current]);
  const rows = useMemo(() => (data?.rows || []).map((row, index) => ({ ...row, _rowKey: `${index}:${row.sql_id || row.sid || "row"}` })), [data]);

  const fetchDetail = async (row) => {
    const token = ++detailRequest.current;
    detailAbort.current?.abort();
    const controller = new window.AbortController();
    detailAbort.current = controller;
    const timer = setTimeout(() => controller.abort(), 60_000);
    setDetail({ row, loading: true, data: null, error: "" });
    try {
      const result = await getOracleSqlDetail(instanceId, row.sql_id, row, { signal: controller.signal });
      if (["failed", "degraded", "error"].includes(result?.status)) throw new Error(result.message || "SQL 详情查询失败");
      if (token === detailRequest.current) setDetail({ row, loading: false, data: result, error: "" });
    } catch (reason) {
      if (token === detailRequest.current) setDetail({ row, loading: false, data: null, error: reason.name === "AbortError" ? "SQL 详情查询超时" : reason.message || "SQL 详情查询失败" });
    } finally {
      clearTimeout(timer);
    }
  };
  const closeDetail = () => { detailRequest.current += 1; detailAbort.current?.abort(); setDetail(null); };
  const columns = slowSqlFields(data?.rows).map(({ key, title }) => ({
    title, dataIndex: key, width: key === "sql_text" ? 400 : key === "sql_id" ? 150 : 140,
    ellipsis: !["sql_id", "sql_text"].includes(key),
    render: key === "sql_id" ? (value, row) => value ? <Button type="link" size="small" onClick={() => fetchDetail(row)}>{value}</Button> : "-"
      : key === "sql_text" ? (value) => <Typography.Paragraph ellipsis={{ rows: 2, expandable: true, symbol: "展开" }} style={{ margin: 0 }}>{value || "-"}</Typography.Paragraph>
        : (value) => value ?? "-",
  }));

  const collect = async () => {
    const token = ++collectionRequest.current;
    setCollection({ loading: true, message: "", failed: false });
    try {
      const response = await collectSlowSql(instanceId, { hours });
      if (token !== collectionRequest.current) return;
      if (["failed", "degraded", "error", "partial", "unsupported"].includes(response?.status)) throw new Error(response.message || "慢 SQL 采集未成功完成");
      setCollection({ loading: false, message: response.message || "慢 SQL 采集已返回，正在刷新查询", failed: false });
      setRefresh((value) => value + 1);
    } catch (reason) {
      if (token !== collectionRequest.current) return;
      setCollection({ loading: false, message: reason.message || "慢 SQL 采集失败", failed: true });
    }
  };

  return <Space direction="vertical" size={16} className="full-width">
    <Card size="small">
      <Space wrap>
        <Segmented value={category} onChange={setCategory} options={SLOW_SQL_CATEGORIES} />
        <Space wrap>{[1, 3, 12, 24, 72, 168].map((value) => <Button key={value} size="small" type={hours === value ? "primary" : "default"}
          disabled={["active", "waiting"].includes(category)} onClick={() => setHours(value)}>{value < 24 ? `${value}小时` : `${value / 24}天`}</Button>)}</Space>
        <Button type="primary" icon={<SearchOutlined />} loading={loading} disabled={!instanceId} onClick={() => setRefresh((value) => value + 1)}>查询</Button>
        <Button icon={<ThunderboltOutlined />} loading={collection.loading} disabled={!instanceId || loading} onClick={collect}>采集慢 SQL</Button>
        <Button icon={<DownloadOutlined />} disabled={!current || loading || Boolean(error)} onClick={() => saveTextFile(slowSqlReportHtml(current, instance.instance_name), `slow-sql-${instanceId}-${Date.now()}.html`, "text/html;charset=utf-8")}>导出查询报告</Button>
      </Space>
    </Card>
    {!instanceId && <Alert showIcon type="info" message="请选择 Oracle 实例" />}
    {error && <Alert type="error" showIcon message="慢 SQL 查询失败" description={error} action={<Button onClick={() => setRefresh((value) => value + 1)}>重试</Button>} />}
    {collection.message && <Alert showIcon type={collection.failed ? "error" : "info"} message={collection.message} />}
    {conclusion && <Card size="small" title="查询结论">
      {conclusion.lines.map((line) => <Typography.Paragraph key={line}>{line}</Typography.Paragraph>)}
      <Typography.Paragraph type="secondary">{conclusion.scope}</Typography.Paragraph>
      <Typography.Paragraph type="secondary">{conclusion.limitation}</Typography.Paragraph>
      <Typography.Text type="secondary">结果接收时间：{new Date(current.receivedAt).toLocaleString()} · 未归档到巡检报告</Typography.Text>
    </Card>}
    {instanceId && <Card size="small" title={data ? `${data.title || "SQL 查询记录"}（${rows.length} 条）` : loading ? "正在读取慢 SQL" : "SQL 查询记录"}>
      <Table loading={loading} dataSource={rows} rowKey="_rowKey" size="small" tableLayout="fixed" columns={columns}
        scroll={{ x: Math.max(900, columns.reduce((sum, column) => sum + column.width, 0)) }} pagination={{ defaultPageSize: 20 }}
        locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={error ? "查询失败，未取得记录" : loading ? "正在读取数据库" : "当前条件未返回记录"} /> }} />
    </Card>}
    <Modal title={`SQL 详情 · ${detail?.row.sql_id || ""}`} open={Boolean(detail)} centered width="min(1100px, calc(100vw - 32px))" onCancel={closeDetail} footer={<Button onClick={closeDetail}>关闭</Button>}>
      {detail?.error && <Alert type="error" showIcon message={detail.error} />}
      {detail?.loading && <Alert type="info" showIcon message="正在查询 SQL 文本和执行计划" />}
      {detail?.data?.plan_error && <Alert type="warning" showIcon message="执行计划未取得" description={detail.data.plan_error} />}
      {detail && <Tabs items={[
        { key: "sql", label: "SQL 文本", children: <pre className="ops-output" style={{ maxHeight: "45vh", overflow: "auto", whiteSpace: "pre-wrap" }}>{detail.data?.sql_fulltext || detail.row.sql_text || "未采集 SQL 文本"}</pre> },
        { key: "plan", label: "执行计划", children: detail.data?.plan_text ? <pre className="ops-output" style={{ maxHeight: "45vh", overflow: "auto" }}>{detail.data.plan_text}</pre> : <Empty description={detail.loading ? "正在获取执行计划" : "未取得执行计划，不能确认计划风险"} /> },
        { key: "metrics", label: "本次查询指标", children: <Descriptions bordered size="small" column={1}
          items={slowSqlFields([detail.row]).filter(({ key }) => key !== "sql_text").map(({ key, title }) => ({
            key, label: title, children: String(detail.row[key] ?? "-"),
          }))} /> },
        { key: "ai", label: "AI 分析", children: <SlowSqlAIAnalysisPanel
          key={`${instanceId}:${detail.row.sql_id}:${detail.row.child_number}:${detail.row.con_id}:${detail.row.plan_hash_value}`}
          instanceId={instanceId} row={detail.row} /> },
      ]} />}
    </Modal>
  </Space>;
}
