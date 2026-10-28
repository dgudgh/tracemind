import React from "react";
import { Badge, Space, Tag, Typography } from "antd";
import {
  CheckCircleOutlined,
  ClockCircleOutlined,
  CloseCircleOutlined,
  ExclamationCircleOutlined,
  StopOutlined,
} from "@ant-design/icons";

const STATUS_META = {
  active: { label: "运行正常", status: "success", icon: <CheckCircleOutlined /> },
  verified: { label: "验证通过", status: "success", icon: <CheckCircleOutlined /> },
  configured: { label: "已配置", status: "processing", icon: <ClockCircleOutlined /> },
  degraded: { label: "部分异常", status: "warning", icon: <ExclamationCircleOutlined /> },
  disabled: { label: "已停用", status: "default", icon: <StopOutlined /> },
  planned: { label: "待配置", status: "default", icon: <ClockCircleOutlined /> },
  failed: { label: "连接失败", status: "error", icon: <CloseCircleOutlined /> },
};

export function IntegrationHealthStrip({
  status = "planned",
  title = "接入状态",
  description,
  lastChecked,
  actions,
}) {
  const meta = STATUS_META[status] || STATUS_META.planned;
  return (
    <div className="integration-health-strip" data-status={status}>
      <Space size={10} wrap>
        <Badge status={meta.status} />
        <Typography.Text strong>{title}</Typography.Text>
        <Tag icon={meta.icon}>{meta.label}</Tag>
        {description ? <Typography.Text type="secondary">{description}</Typography.Text> : null}
        {lastChecked ? (
          <Typography.Text type="secondary">最近检查：{lastChecked}</Typography.Text>
        ) : null}
      </Space>
      {actions ? <Space size={8}>{actions}</Space> : null}
    </div>
  );
}
