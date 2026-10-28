import React, { useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Space,
  Tag,
  Typography,
  message,
} from "antd";
import {
  ApiOutlined,
  BellOutlined,
  CheckCircleFilled,
  CloudServerOutlined,
  ControlOutlined,
  DashboardOutlined,
  DatabaseOutlined,
  FileTextOutlined,
  HistoryOutlined,
  ApartmentOutlined,
  LineChartOutlined,
  LinkOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
  SearchOutlined,
} from "@ant-design/icons";
import { getObservabilityBundle } from "./modules/observabilityApi";
import {
  OBSERVABILITY_NAV_GROUPS,
  findObservabilityGroup,
  findObservabilitySection,
} from "./modules/observabilityNavigation";
import {
  AlertIngestPage,
  CollectLogPage,
  CollectPolicyPage,
  CollectTaskPage,
  CurrentMetricPage,
  DataQualityPage,
  EvidenceIntegrationPage,
  ExporterManagementPage,
  IntegrationPage,
  InstanceBindingPage,
  LinkageHealthPage,
  MetricHistoryPage,
  MetricMappingPage,
  SourceManagementPage,
  ApmTracePage,
  GrafanaTemplatePage,
  LogAnalysisPage,
} from "./ObservabilityCenterSections";
import { SOURCE_TYPES, SOURCE_TYPE_LABEL } from "./ObservabilityCenterConfig";
import { consumeRouteSection } from "./modules/routeSectionNavigation";

const { Text, Title } = Typography;

const QUALITY_HEADER_META = {
  healthy: { icon: CheckCircleFilled, label: "采集数据可信" },
  degraded: { icon: BellOutlined, label: "采集数据降级" },
  stale: { icon: HistoryOutlined, label: "采集数据陈旧" },
  missing: { icon: DatabaseOutlined, label: "采集数据缺失" },
  unknown: { icon: SafetyCertificateOutlined, label: "采集状态未验证" },
  disabled: { icon: SafetyCertificateOutlined, label: "采集已停用" },
};

const GROUP_ICONS = {
  overview: DashboardOutlined,
  dataQuality: SafetyCertificateOutlined,
  sources: DatabaseOutlined,
  integrations: ApiOutlined,
  collection: ControlOutlined,
  metrics: LineChartOutlined,
};

const SECTION_ICONS = {
  overview: DashboardOutlined,
  linkageHealth: CheckCircleFilled,
  sources: DatabaseOutlined,
  bindings: LinkOutlined,
  zabbix: ApiOutlined,
  prometheus: LineChartOutlined,
  alertmanager: BellOutlined,
  grafana: LineChartOutlined,
  exporters: CloudServerOutlined,
  skywalking: ApiOutlined,
  nginx: ApiOutlined,
  tomcat: CloudServerOutlined,
  elasticsearch: DatabaseOutlined,
  policies: ControlOutlined,
  tasks: CloudServerOutlined,
  logs: FileTextOutlined,
  mappings: LinkOutlined,
  currentMetrics: LineChartOutlined,
  metricHistory: HistoryOutlined,
  alerts: BellOutlined,
  logAnalysis: SearchOutlined,
  faultTimeline: HistoryOutlined,
  apmTrace: ApartmentOutlined,
  sqlTrace: DatabaseOutlined,
};

function ObservabilityNavigation({ activeSection, onChange }) {
  const activeGroup = findObservabilityGroup(activeSection) || OBSERVABILITY_NAV_GROUPS[0];

  return (
    <nav className="observability-navigation" aria-label="可观测中心功能导航">
      <div className="observability-nav-groups" role="tablist" aria-label="功能分类">
        {OBSERVABILITY_NAV_GROUPS.map((group) => {
          const Icon = GROUP_ICONS[group.key] || ApiOutlined;
          const selected = group.key === activeGroup.key;
          return (
            <button
              type="button"
              role="tab"
              aria-selected={selected}
              className={`observability-nav-group${selected ? " is-active" : ""}`}
              key={group.key}
              onClick={() => {
                if (!selected) onChange(group.items[0].key);
              }}
            >
              <span className="observability-nav-group-icon"><Icon /></span>
              <span className="observability-nav-group-copy">
                <strong>{group.label}</strong>
                <small>{group.description}</small>
              </span>
            </button>
          );
        })}
      </div>

      <div className="observability-nav-sections">
        <div className="observability-nav-context">
          <span>当前分类</span>
          <strong>{activeGroup.label}</strong>
        </div>
        <div className="observability-nav-section-list" role="tablist" aria-label={`${activeGroup.label}功能`}>
          {activeGroup.items.map((item) => {
            const Icon = SECTION_ICONS[item.key] || ApiOutlined;
            const selected = item.key === activeSection;
            return (
              <button
                type="button"
                role="tab"
                aria-selected={selected}
                aria-controls="observability-active-panel"
                className={`observability-nav-section${selected ? " is-active" : ""}`}
                key={item.key}
                onClick={() => onChange(item.key)}
              >
                <Icon />
                <span>{item.label}</span>
              </button>
            );
          })}
        </div>
      </div>
    </nav>
  );
}

function ObservabilityOverview({ overview, currentMetrics, typeCounts, dataQuality }) {
  const metrics = [
    {
      key: "sources",
      label: "接入数据源",
      value: overview.source_count ?? 0,
      detail: `启用 ${overview.enabled_source_count ?? 0}`,
      icon: ApiOutlined,
      tone: "blue",
    },
    {
      key: "trusted",
      label: "可信数据源",
      value: dataQuality?.trusted_source_count ?? 0,
      detail: `启用 ${dataQuality?.enabled_source_count ?? overview.enabled_source_count ?? 0}`,
      icon: SafetyCertificateOutlined,
      tone: "green",
    },
    {
      key: "alerts",
      label: "活动告警",
      value: overview.active_alert_count ?? 0,
      detail: "待处理事件",
      icon: BellOutlined,
      tone: "orange",
    },
    {
      key: "tasks",
      label: "近期任务",
      value: overview.recent_task_count ?? 0,
      detail: "采集执行",
      icon: CloudServerOutlined,
      tone: "neutral",
    },
    {
      key: "metrics",
      label: "当前指标",
      value: overview.current_metric_count ?? currentMetrics.length,
      detail: "标准化数据",
      icon: LineChartOutlined,
      tone: "violet",
    },
  ];

  return (
    <div className="observability-overview">
      <section className="observability-kpi-grid" aria-label="采集运行概览">
        {metrics.map((item) => {
          const Icon = item.icon;
          return (
            <article className={`observability-kpi observability-tone-${item.tone}`} key={item.key}>
              <span className="observability-kpi-icon"><Icon /></span>
              <div className="observability-kpi-copy">
                <Text type="secondary">{item.label}</Text>
                <div className="observability-kpi-value-row">
                  <strong>{item.value}</strong>
                  <span>{item.detail}</span>
                </div>
              </div>
            </article>
          );
        })}
      </section>

      <section className="observability-principles" aria-labelledby="observability-principles-title">
        <div className="observability-section-heading">
          <div>
            <Text type="secondary">采集边界</Text>
            <Title level={5} id="observability-principles-title">数据进入诊断中心前的处理原则</Title>
          </div>
          <Tag color="blue">统一证据入口</Tag>
        </div>
        <div className="observability-principle-grid">
          <article>
            <strong>外部监控优先</strong>
            <span>Zabbix 与 Prometheus 承接高频指标。</span>
          </article>
          <article>
            <strong>自采保持轻量</strong>
            <span>低频、限流，并支持按策略关闭。</span>
          </article>
          <article>
            <strong>输出统一归一</strong>
            <span>形成标准指标、告警、日志和诊断证据。</span>
          </article>
        </div>
      </section>

      <section className="observability-coverage" aria-labelledby="observability-coverage-title">
        <div className="observability-section-heading">
          <div>
            <Text type="secondary">接入覆盖</Text>
            <Title level={5} id="observability-coverage-title">外部平台连接状态</Title>
          </div>
          <Text type="secondary">共 {SOURCE_TYPES.length} 类</Text>
        </div>
        <div className="observability-coverage-grid">
          {SOURCE_TYPES.map((type) => {
            const count = typeCounts[type] || 0;
            return (
              <article className={`observability-coverage-item${count > 0 ? " is-connected" : ""}`} key={type}>
                <span className="observability-coverage-dot" />
                <span>{SOURCE_TYPE_LABEL[type] || type}</span>
                <strong>{count}</strong>
              </article>
            );
          })}
        </div>
      </section>
    </div>
  );
}

export default function ObservabilityCenterPage() {
  const [loading, setLoading] = useState(false);
  const [activeSection, setActiveSection] = useState(() => {
    const requested = consumeRouteSection("monitor.observability", "overview");
    return findObservabilitySection(requested) ? requested : "overview";
  });
  const [overview, setOverview] = useState({});
  const [sources, setSources] = useState([]);
  const [standardMetrics, setStandardMetrics] = useState([]);
  const [mappings, setMappings] = useState([]);
  const [bindings, setBindings] = useState([]);
  const [policies, setPolicies] = useState([]);
  const [tasks, setTasks] = useState([]);
  const [logs, setLogs] = useState([]);
  // 日志关联属企业版：被版本门禁锁定时本页其余数据照常显示，仅该分区给出说明。
  const [logsLocked, setLogsLocked] = useState(false);
  const [alerts, setAlerts] = useState([]);
  const [exporters, setExporters] = useState([]);
  const [currentMetrics, setCurrentMetrics] = useState([]);
  const [metricHistory, setMetricHistory] = useState([]);
  const [dataQuality, setDataQuality] = useState(null);

  const load = async () => {
    setLoading(true);
    try {
      const [
        overviewData,
        sourceData,
        standardMetricData,
        mappingData,
        bindingData,
        policyData,
        taskData,
        logData,
        alertData,
        exporterData,
        currentMetricData,
        historyData,
        dataQualityData,
      ] = await getObservabilityBundle();
      setOverview(overviewData || {});
      setSources(sourceData.items || []);
      setStandardMetrics(standardMetricData.items || []);
      setMappings(mappingData.items || []);
      setBindings(bindingData.items || []);
      setPolicies(policyData.items || []);
      setTasks(taskData.items || []);
      setLogs(logData.items || []);
      setLogsLocked(logData.editionLocked === true);
      setAlerts(alertData.items || []);
      setExporters(exporterData.items || []);
      setCurrentMetrics(currentMetricData.items || []);
      setMetricHistory(historyData.items || []);
      setDataQuality(dataQualityData || null);
    } catch (error) {
      message.error(error.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  const typeCounts = useMemo(() => {
    const result = {};
    sources.forEach((item) => { result[item.source_type] = (result[item.source_type] || 0) + 1; });
    return result;
  }, [sources]);

  const sectionContent = {
    overview: <ObservabilityOverview overview={overview} currentMetrics={currentMetrics} typeCounts={typeCounts} dataQuality={dataQuality} />,
    dataQuality: <DataQualityPage initialQuality={dataQuality} onQualityChange={setDataQuality} />,
    linkageHealth: <LinkageHealthPage sources={sources} />,
    sources: <SourceManagementPage sources={sources} load={load} />,
    bindings: <InstanceBindingPage bindings={bindings} sources={sources} load={load} />,
    zabbix: <IntegrationPage type="zabbix" sources={sources} load={load} />,
    prometheus: <IntegrationPage type="prometheus" sources={sources} load={load} />,
    alertmanager: <IntegrationPage type="alertmanager" sources={sources} load={load} />,
    grafana: <GrafanaTemplatePage />,
    exporters: <ExporterManagementPage exporters={exporters} sources={sources} />,
    skywalking: <EvidenceIntegrationPage title="SkyWalking 接入" type="skywalking" sources={sources} load={load} />,
    nginx: <EvidenceIntegrationPage title="Nginx 接入" type="nginx" sources={sources} load={load} />,
    tomcat: <EvidenceIntegrationPage title="Tomcat / JVM 接入" type="tomcat_jvm" sources={sources} load={load} />,
    elasticsearch: <EvidenceIntegrationPage title="Elasticsearch 接入" type="elasticsearch" sources={sources} load={load} />,
    policies: <CollectPolicyPage policies={policies} load={load} />,
    tasks: <CollectTaskPage tasks={tasks} sources={sources} load={load} />,
    logs: logsLocked
      ? <Alert type="info" showIcon message="日志关联为企业版功能"
          description="日志筛选、关联与查询属可观测的进阶能力；本页其余指标、来源与采集任务不受影响。" />
      : <CollectLogPage logs={logs} />,
    mappings: <MetricMappingPage mappings={mappings} standardMetrics={standardMetrics} load={load} />,
    currentMetrics: <CurrentMetricPage metrics={currentMetrics} />,
    metricHistory: <MetricHistoryPage history={metricHistory} />,
    alerts: <AlertIngestPage alerts={alerts} />,
    logAnalysis: <LogAnalysisPage sources={sources} />,
    faultTimeline: <LogAnalysisPage sources={sources} timelineOnly />,
    apmTrace: <ApmTracePage sources={sources} />,
    sqlTrace: <ApmTracePage sources={sources} linkSql />,
  };
  const activeMeta = findObservabilitySection(activeSection) || findObservabilitySection("overview");
  const qualityMeta = QUALITY_HEADER_META[dataQuality?.overall_status] || QUALITY_HEADER_META.unknown;
  const QualityHeaderIcon = qualityMeta.icon;

  return (
    <div className="observability-page" aria-busy={loading}>
      <section className="observability-page-header">
        <div className="observability-page-identity">
          <span className="observability-page-icon"><DatabaseOutlined /></span>
          <div>
            <Space size={8} wrap>
              <Title level={4}>可观测与采集中心</Title>
              <Tag color="blue">统一入口层</Tag>
            </Space>
            <Text type="secondary">外部监控优先，深度诊断按需执行</Text>
          </div>
        </div>
        <div className="observability-page-actions">
          <span className={`observability-status is-${dataQuality?.overall_status || "unknown"}`}>
            <QualityHeaderIcon /> {qualityMeta.label}
          </span>
          <Button icon={<ReloadOutlined />} loading={loading} onClick={load}>刷新</Button>
        </div>
      </section>

      <ObservabilityNavigation activeSection={activeSection} onChange={setActiveSection} />

      <section
        className="observability-active-panel"
        id="observability-active-panel"
        role="tabpanel"
        aria-label={activeMeta?.label || "综合总览"}
      >
        {sectionContent[activeSection] || sectionContent.overview}
      </section>
    </div>
  );
}
