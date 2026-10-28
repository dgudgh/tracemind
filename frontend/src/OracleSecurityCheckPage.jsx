import React, { useMemo, useState } from "react";
import { Alert, Button, Card, Space, Tag, message } from "antd";
import { SecurityScanOutlined } from "@ant-design/icons";
import { oracleOpsApi } from "./apiClients";
import { CurrentInstanceSummary, OpsEmptyState, useFirstAvailableInstanceId as useSyncedInstanceId } from "./OpsLayout";
import { stableRecordKey } from "./modules/stableKeys";

export function SecurityCheckPage({ instances, selectedInstanceId }) {
  const oracleInstances = useMemo(() => instances.filter((item) => item.db_type === "oracle"), [instances]);
  const [instanceId] = useSyncedInstanceId(selectedInstanceId, oracleInstances);
  const selectedInstance = oracleInstances.find((item) => item.id === instanceId);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState(null);

  const runCheck = async () => {
    if (!instanceId) return;
    setLoading(true);
    try {
      const data = await oracleOpsApi.ransomwareCheck(instanceId);
      setResult(data);
    } catch (e) { message.error(e.message); }
    finally { setLoading(false); }
  };

  const riskColor = { normal: "green", info: "blue", warning: "orange", critical: "red" };

  if (!instanceId) {
    return <OpsEmptyState title="请选择 Oracle 实例" description="选择实例后可运行勒索风险、弱配置和可恢复性检查。" />;
  }

  return (
    <Space direction="vertical" size={16} className="full-width">
      <Card>
        <Space wrap>
          <CurrentInstanceSummary instance={selectedInstance} emptyText="请在右上角选择 Oracle 实例" />
          <Button type="primary" icon={<SecurityScanOutlined />} loading={loading} onClick={runCheck}>运行安全检查</Button>
          <Tag color="blue">Plan Only</Tag>
        </Space>
      </Card>

      {result && (
        <Card title={`检查结果 - ${result.instance}`}>
          {result.findings.map((f) => (
            <Alert key={stableRecordKey("oracle-security-finding", f, ["risk", "check", "detail"])} type={f.risk === "normal" ? "success" : f.risk === "critical" ? "error" : f.risk === "warning" ? "warning" : "info"}
              showIcon style={{ marginBottom: 8 }}
              message={<Space><Tag color={riskColor[f.risk]}>{f.risk.toUpperCase()}</Tag><span>{f.check}</span></Space>}
              description={f.detail}
            />
          ))}
        </Card>
      )}
    </Space>
  );
}
