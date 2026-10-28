export const OBSERVABILITY_NAV_GROUPS = [
  {
    key: "overview",
    label: "运行概览",
    description: "状态与健康",
    items: [
      { key: "overview", label: "综合总览" },
      { key: "dataQuality", label: "数据质量" },
      { key: "linkageHealth", label: "联动健康" },
    ],
  },
  {
    key: "sources",
    label: "数据源",
    description: "连接与绑定",
    items: [
      { key: "sources", label: "数据源管理" },
      { key: "bindings", label: "实例绑定" },
    ],
  },
  {
    key: "integrations",
    label: "集成接入",
    description: "外部平台",
    items: [
      { key: "zabbix", label: "Zabbix" },
      { key: "prometheus", label: "Prometheus" },
      { key: "alertmanager", label: "Alertmanager" },
      { key: "grafana", label: "Grafana 模板" },
      { key: "exporters", label: "Exporter" },
      { key: "skywalking", label: "SkyWalking" },
      { key: "nginx", label: "Nginx" },
      { key: "tomcat", label: "Tomcat / JVM" },
      { key: "elasticsearch", label: "Elasticsearch" },
    ],
  },
  {
    key: "collection",
    label: "采集管理",
    description: "策略与任务",
    items: [
      { key: "policies", label: "采集策略" },
      { key: "tasks", label: "采集任务" },
      { key: "logs", label: "采集日志" },
    ],
  },
  {
    key: "metrics",
    label: "指标告警",
    description: "数据与事件",
    items: [
      { key: "mappings", label: "指标映射" },
      { key: "currentMetrics", label: "当前指标" },
      { key: "metricHistory", label: "历史指标" },
      { key: "alerts", label: "告警接入" },
      { key: "logAnalysis", label: "日志关联" },
      { key: "faultTimeline", label: "故障时间线" },
      { key: "apmTrace", label: "APM 链路" },
      { key: "sqlTrace", label: "SQL 关联" },
    ],
  },
];

export function findObservabilityGroup(sectionKey) {
  return OBSERVABILITY_NAV_GROUPS.find(
    (group) => group.items.some((item) => item.key === sectionKey),
  ) || null;
}

export function findObservabilitySection(sectionKey) {
  for (const group of OBSERVABILITY_NAV_GROUPS) {
    const section = group.items.find((item) => item.key === sectionKey);
    if (section) return section;
  }
  return null;
}
