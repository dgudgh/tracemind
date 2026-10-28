import React from "react";
import { Alert, Button, Card, Col, Row, Space, Tag, Typography, message } from "antd";
import { stableRecordKey } from "./modules/stableKeys";

const { Text, Title } = Typography;

function riskImpact(item) {
  const text = `${item.item || ""} ${item.message || ""}`;
  if (text.includes("无主键")) return "可能影响复制、行定位、历史数据清理和大表归档效率。";
  if (text.includes("Select_full_join") || text.includes("JOIN")) return "可能存在 JOIN 条件未命中索引，容易造成慢 SQL、临时表和额外 CPU 消耗。";
  if (text.includes("Error Log")) return "平台暂时无法读取错误日志，故障定位时会缺少数据库错误上下文。";
  if (text.includes("root@%")) return "root 账号允许任意来源登录，存在较高安全暴露风险。";
  return "可能影响数据库稳定性、性能或后续治理效率。";
}

function riskActions(item) {
  const text = `${item.item || ""} ${item.message || ""}`;
  if (text.includes("无主键")) return ["查看无主键表", "生成整改清单"];
  if (text.includes("Select_full_join") || text.includes("JOIN")) return ["查看慢 SQL", "查看索引建议"];
  if (text.includes("Error Log")) return ["配置 SSH"];
  if (text.includes("root@%")) return ["查看用户权限", "生成整改清单"];
  return ["查看详情"];
}

export function MySQLBasicOverview({
  data,
  info,
  instance,
  summary,
  basic,
  checks,
  risks,
  riskCounts,
  riskColor,
  riskText,
  statusColor,
  num,
  boolLabel,
}) {
  const metricGroups = [
    { title: "实例状态", items: [["版本", `MySQL ${summary.mysql_version || basic.version || "-"}`], ["角色", summary.role_label || "-"], ["运行时间", summary.uptime_label || "-"]] },
    { title: "负载状态", items: [["连接使用率", `${num(summary.connection_used_pct || checks.connection_usage_pct || 0)}% ${summary.connection_status_label || ""}`], ["QPS", num(summary.qps || 0)], ["TPS", num(summary.tps_estimated || 0)]] },
    { title: "可靠性", items: [["Binlog", summary.binlog_label || (summary.binlog_enabled ? "已开启" : "未开启")], ["GTID", summary.gtid_label || boolLabel(summary.gtid_mode) || "-"], ["复制状态", summary.replication_status_label || "-"]] },
    { title: "性能状态", items: [["Buffer Pool", `${num(summary.buffer_pool_hit_pct ?? checks.buffer_pool_hit_pct ?? 0)}% ${summary.buffer_pool_status_label || ""}`], ["慢 SQL", summary.slow_query_log_label || boolLabel(summary.slow_query_log || basic.slow_query_log)], ["风险等级", summary.risk_level_label || riskText[summary.risk_level] || "-"]] },
  ];

  return (
    <>
      {data.error && <Alert type="error" showIcon message="基础信息采集失败" description={data.error} />}
      {!!(data.warnings || []).length && <Alert type="warning" showIcon message="部分信息已降级采集" description={(data.warnings || []).join("\n")} />}
      <Card>
        <Row gutter={[16, 16]} align="middle">
          <Col flex="auto">
            <Space direction="vertical" size={6}>
              <Title level={3} style={{ margin: 0 }}>{info.identity?.hostname || instance.instance_name || "MySQL 实例画像"}</Title>
              <Text type="secondary">
                {`MySQL ${summary.mysql_version || basic.version || "-"} · ${summary.role_label || "-"} · ${summary.uptime_label || "-"} · ${instance.host || info.identity?.hostname || "-"}:${instance.port || info.identity?.port || "-"}`
                }
              </Text>
              <Space wrap>
                {[summary.role_label, `Binlog ${summary.binlog_label || "-"}`, `GTID ${summary.gtid_label || "-"}`, summary.replication_status_label, `慢 SQL ${summary.slow_query_log_label || "-"}`, `风险：${summary.risk_level_label || "-"}`].filter(Boolean).map((tag) => (
                  <Tag key={tag} color={statusColor[tag] || (tag.includes("风险：警告") ? "orange" : tag.includes("已开启") || tag.includes("正常") ? "green" : "blue")}>{tag}</Tag>
                ))}
              </Space>
            </Space>
          </Col>
          <Col>
            <Space direction="vertical" align="end" size={4}>
              <Text>{[instance.env_type, instance.factory_name, instance.line_name].filter(Boolean).join(" / ") || "未标注环境"}</Text>
              <Text type="secondary">最近采集：{data.collected_at || "-"}</Text>
            </Space>
          </Col>
        </Row>
      </Card>
      <Alert
        type={summary.risk_level === "normal" ? "success" : summary.risk_level === "critical" || summary.risk_level === "high" ? "error" : "warning"}
        showIcon
        message={`整体状态：${summary.risk_level_label || "正常"}`}
        description={summary.health_summary_text || "实例基础信息已采集完成。"}
      />
      <Row gutter={[16, 16]}>
        {metricGroups.map((group) => (
          <Col xs={24} lg={12} xl={6} key={group.title}>
            <Card title={group.title} size="small">
              <Space direction="vertical" size={10} style={{ width: "100%" }}>
                {group.items.map(([name, value]) => (
                  <Row key={name} justify="space-between" align="middle">
                    <Text type="secondary">{name}</Text>
                    <Text strong>{value}</Text>
                  </Row>
                ))}
              </Space>
            </Card>
          </Col>
        ))}
      </Row>
      <Card title="重点风险与建议" extra={<Space wrap>
        {["critical", "high", "warning", "info"].map((level) => (
          <Tag key={level} color={riskColor[level]}>{riskText[level]} {riskCounts[level] || 0}</Tag>
        ))}
      </Space>}>
        {risks.length ? (
          <Row gutter={[16, 16]}>
            {risks.map((risk) => (
              <Col xs={24} lg={12} xl={8} key={stableRecordKey("mysql-basic-risk", risk, ["item", "level", "message"])}>
                <Card size="small" title={<Space><Tag color={riskColor[risk.level] || "default"}>{riskText[risk.level] || risk.level}</Tag><Text strong>{risk.item}</Text></Space>}>
                  <Space direction="vertical" size={8} style={{ width: "100%" }}>
                    <Text><Text strong>发现：</Text>{risk.message || "-"}</Text>
                    <Text><Text strong>影响：</Text>{riskImpact(risk)}</Text>
                    <Text><Text strong>建议：</Text>{risk.suggestion || "-"}</Text>
                    <Space wrap>{riskActions(risk).map((action) => <Button key={action} size="small" onClick={() => message.info(`${action} 功能将联动到对应模块`)}>{action}</Button>)}</Space>
                  </Space>
                </Card>
              </Col>
            ))}
          </Row>
        ) : <Alert type="success" showIcon message="暂未发现明显基础配置风险" />}
      </Card>
    </>
  );
}
