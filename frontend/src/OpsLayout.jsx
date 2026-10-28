import React from "react";
import {
  Alert,
  Button,
  Empty,
  Modal,
  Space,
  Tabs,
  Tag,
  Typography,
  message,
} from "antd";
import { CopyOutlined } from "@ant-design/icons";
import { AdminShell } from "./framework/AdminShell";
import { copyText } from "./modules/clipboard";

const { Title, Text } = Typography;

export function useFirstAvailableInstanceId(selectedInstanceId, availableInstances = []) {
  const fallbackId = availableInstances[0]?.id;
  const matched = availableInstances.some((item) => item.id === selectedInstanceId);
  return [matched ? selectedInstanceId : fallbackId];
}

export function OpsRiskSummary({ alerts = [] }) {
  const critical = alerts.filter((item) => item.level === "critical").length;
  const warning = alerts.length - critical;
  if (!alerts.length) {
    return <Tag color="success">风险 0</Tag>;
  }
  return (
    <Space size={6} className="ops-risk-summary">
      {critical > 0 && <Tag color="error">严重 {critical}</Tag>}
      {warning > 0 && <Tag color="warning">预警 {warning}</Tag>}
    </Space>
  );
}

export function OpsEmptyState({ title = "暂无数据", description = "请选择实例或稍后刷新。", action }) {
  return (
    <div className="ops-empty-state">
      <Empty
        description={(
          <Space direction="vertical" size={4}>
            <Text strong>{title}</Text>
            <Text type="secondary">{description}</Text>
          </Space>
        )}
      >
        {action}
      </Empty>
    </div>
  );
}

export function CurrentInstanceSummary({ instance, emptyText = "请在右上角选择实例" }) {
  if (!instance) {
    return <Text type="secondary">{emptyText}</Text>;
  }
  return (
    <Space size={6} wrap>
      <Tag color={dbTypeColor(instance.db_type)}>
        {(instance.db_type || "DB").toUpperCase()}
      </Tag>
      <Text strong>{instance.instance_name}</Text>
      <Text type="secondary">{instance.host}:{instance.port}</Text>
    </Space>
  );
}

export function dbTypeColor(value) {
  return {
    oracle: "volcano",
    mysql: "blue",
    postgresql: "green",
    sqlserver: "purple",
  }[value] || "default";
}

export function OpsCenterTabs({ title, description, instance, emptyText, items, activeKey, onChange }) {
  const riskAlerts = (items || [])
    .filter((item) => item.riskLevel && item.riskLevel !== "normal")
    .map((item) => ({ level: item.riskLevel === "critical" || item.riskLevel === "high" ? "critical" : "warning" }));
  const tabItems = (items || []).map(({ riskLevel, ...item }) => item);
  return (
    <Space direction="vertical" size={16} className="full-width">
      <section className="ops-context-bar">
        <div className="ops-context-heading">
          <Title level={4}>{title}</Title>
          <Text type="secondary">{description}</Text>
        </div>
        <div className="ops-context-status">
          <OpsRiskSummary alerts={riskAlerts} />
          <Tag color="blue">方案预览</Tag>
          <Tag>{tabItems.length} 项能力</Tag>
        </div>
        <div className="ops-context-instance">
          <Text type="secondary">当前实例</Text>
          <CurrentInstanceSummary instance={instance} emptyText={emptyText} />
        </div>
      </section>
      <Tabs className="ops-center-tabs" activeKey={activeKey} onChange={onChange} items={tabItems} />
    </Space>
  );
}

export function PlanOnlyScriptModal({
  open,
  title = "变更方案预览",
  script,
  warnings = [],
  width = 860,
  onClose,
}) {
  const copyScript = async () => {
    try {
      await copyText(script || "");
      message.success("脚本已复制");
    } catch {
      message.error("复制失败");
    }
  };
  return (
    <Modal
      title={title}
      open={open}
      onCancel={onClose}
      width={width}
      footer={[
        <Button key="copy" icon={<CopyOutlined />} onClick={copyScript} disabled={!script}>复制脚本</Button>,
        <Button key="close" type="primary" onClick={onClose}>关闭</Button>,
      ]}
    >
      <Alert type="warning" showIcon message="Plan Only：仅生成方案，不直接执行生产变更。" />
      {warnings.length > 0 && (
        <Alert
          type="warning"
          showIcon
          style={{ marginTop: 12 }}
          message="注意事项"
          description={(
            <ul style={{ margin: 0, paddingLeft: 20 }}>
              {warnings.map((item) => <li key={item}>{item}</li>)}
            </ul>
          )}
        />
      )}
      <pre className="ops-output">{script || "暂无脚本"}</pre>
    </Modal>
  );
}

export function OperationsShell({
  title,
  menuItems,
  activeKey,
  openKeys,
  instances,
  selectedInstanceId,
  loadingInstances,
  riskAlerts,
  onMenuClick,
  onOpenKeysChange,
  onInstanceChange,
  onRefresh,
  onLogout,
  onSmartNavigate,
  children,
}) {
  return (
    <AdminShell
      title={title}
      menuItems={menuItems}
      activeKey={activeKey}
      openKeys={openKeys}
      instances={instances}
      selectedInstanceId={selectedInstanceId}
      loadingInstances={loadingInstances}
      riskAlerts={riskAlerts}
      onMenuClick={onMenuClick}
      onOpenKeysChange={onOpenKeysChange}
      onInstanceChange={onInstanceChange}
      onRefresh={onRefresh}
      onLogout={onLogout}
      onSmartNavigate={onSmartNavigate}
    >
      {children}
    </AdminShell>
  );
}
