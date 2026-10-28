import React from "react";
import { Alert, Collapse, Descriptions, Empty, Space, Table } from "antd";

export function ReportEvidencePanel({ evidence }) {
  const { source, items, manifest, count } = evidence;
  return <Space direction="vertical" size={12} className="full-width">
    {source && <Descriptions size="small" bordered column={{ xs: 1, sm: 2 }} items={[
      { key: "sql", label: "SQL ID", children: source.sql_id ?? "未记录" },
      { key: "child", label: "子游标", children: source.child_number ?? "未记录" },
      { key: "con", label: "容器 ID", children: source.con_id ?? "未记录" },
      { key: "plan", label: "计划哈希", children: source.plan_hash_value ?? "未记录" },
      { key: "source", label: "采集来源", children: source.source ?? "未记录" },
      { key: "time", label: "采集时间", children: source.collected_at ?? "未记录" },
    ]} />}
    {source && source.plan_status !== "collected" && <Alert type="warning" showIcon message="执行计划未完整采集" description={source.plan_error || "本次分析未保存完整执行计划。"} />}
    {items.length > 0 && <Collapse defaultActiveKey={[items[0].key]} items={items.map((item) => ({
      key: item.key, label: item.label, children: <pre className="report-content-preview">{item.text}</pre>,
    }))} />}
    {manifest.length > 0 && <Table rowKey={(row) => row.key || row.uri || JSON.stringify(row)} size="small" pagination={false}
      dataSource={manifest} columns={[{ title: "证据索引", dataIndex: "name" }, { title: "类型", dataIndex: "type", width: 160 }, { title: "位置", dataIndex: "uri", ellipsis: true }]} />}
    {!count && <Empty description="本报告未保存独立采集证据或证据索引" />}
  </Space>;
}
