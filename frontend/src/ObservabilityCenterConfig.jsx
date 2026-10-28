import React from "react";
import { Tag } from "antd";

export const SOURCE_TYPES = [
  "zabbix",
  "prometheus",
  "alertmanager",
  "skywalking",
  "nginx",
  "tomcat_jvm",
  "elasticsearch",
  "exporter",
  "database_direct",
  "ssh_host",
  "webhook",
];

export const SOURCE_TYPE_LABEL = {
  zabbix: "Zabbix",
  prometheus: "Prometheus",
  alertmanager: "Alertmanager",
  skywalking: "SkyWalking",
  nginx: "Nginx",
  tomcat_jvm: "Tomcat / JVM",
  elasticsearch: "Elasticsearch",
  exporter: "Exporter",
  database_direct: "数据库直连",
  ssh_host: "SSH 主机",
  webhook: "Webhook",
};

export const SEV_COLOR = {
  critical: "red",
  disaster: "red",
  high: "volcano",
  warning: "orange",
  medium: "orange",
  info: "blue",
  low: "default",
};

export function sourceTypeOptions() {
  return SOURCE_TYPES.map((value) => ({ value, label: SOURCE_TYPE_LABEL[value] || value }));
}

export function statusTag(value) {
  const color = value === "success" || value === "ok" || value === "enabled" || value === "firing" ? "green" : value === "failed" ? "red" : "blue";
  return <Tag color={color}>{value || "-"}</Tag>;
}
