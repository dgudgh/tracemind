import React from "react";
import { Alert, Descriptions, Typography } from "antd";

const RESULT_TYPE = {
  success: "success",
  failed: "error",
  error: "error",
  warning: "warning",
  testing: "info",
};

export function ConnectionTestResult({ result, emptyText = "尚未执行连接测试" }) {
  if (!result) {
    return <Typography.Text type="secondary">{emptyText}</Typography.Text>;
  }
  const status = result.status || "warning";
  return (
    <Alert
      showIcon
      type={RESULT_TYPE[status] || "info"}
      message={result.message || (status === "success" ? "连接测试通过" : "连接测试未通过")}
      description={(
        <Descriptions size="small" column={{ xs: 1, sm: 2, lg: 3 }}>
          {result.latency_ms !== undefined ? (
            <Descriptions.Item label="耗时">{result.latency_ms} ms</Descriptions.Item>
          ) : null}
          {result.checked_at ? (
            <Descriptions.Item label="检查时间">{result.checked_at}</Descriptions.Item>
          ) : null}
          {result.error_code ? (
            <Descriptions.Item label="错误码">{result.error_code}</Descriptions.Item>
          ) : null}
          {result.detail ? (
            <Descriptions.Item label="详情" span={3}>{result.detail}</Descriptions.Item>
          ) : null}
        </Descriptions>
      )}
    />
  );
}
