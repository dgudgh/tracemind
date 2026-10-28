import React from "react";
import { Table, Tag } from "antd";

const STATUS_COLOR = {
  success: "green",
  active: "green",
  failed: "red",
  degraded: "orange",
  pending: "blue",
  plan_only: "gold",
};

export function IntegrationAuditTable({ items = [], loading = false, columns = [] }) {
  const baseColumns = [
    { title: "动作", dataIndex: "action", width: 160, ellipsis: true },
    { title: "对象", dataIndex: "target", width: 200, ellipsis: true },
    {
      title: "状态",
      dataIndex: "status",
      width: 110,
      render: (value) => <Tag color={STATUS_COLOR[value] || "default"}>{value || "未知"}</Tag>,
    },
    { title: "操作人", dataIndex: "operator", width: 120, ellipsis: true },
    { title: "结果", dataIndex: "message", ellipsis: true },
    { title: "时间", dataIndex: "create_time", width: 180 },
  ];
  return (
    <Table
      rowKey={(row) => row.id || row.audit_id || `${row.action}:${row.create_time}`}
      size="small"
      loading={loading}
      dataSource={items}
      columns={columns.length ? columns : baseColumns}
      pagination={{ pageSize: 10, showSizeChanger: false }}
      scroll={{ x: "max-content" }}
    />
  );
}
