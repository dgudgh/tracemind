import React, { useCallback, useEffect, useState } from "react";
import { Alert, Button, Typography } from "antd";
import {
  ApartmentOutlined,
  CloudServerOutlined,
  DashboardOutlined,
  DatabaseOutlined,
  FileSearchOutlined,
  ReloadOutlined,
} from "@ant-design/icons";
import {
  ApmTracePage,
  GrafanaTemplatePage,
  LogAnalysisPage,
} from "./ObservabilityCenterSections";
import { listObservabilitySources } from "./modules/observabilityApi";

const { Text, Title } = Typography;

function CapabilityPageHeader({ icon: Icon, title, description, loading = false, onRefresh }) {
  return (
    <header className="capability-page-header">
      <div className="capability-page-identity">
        <span className="capability-page-icon"><Icon /></span>
        <div>
          <Title level={4}>{title}</Title>
          <Text type="secondary">{description}</Text>
        </div>
      </div>
      {onRefresh ? (
        <Button icon={<ReloadOutlined />} loading={loading} onClick={onRefresh}>刷新数据源</Button>
      ) : null}
    </header>
  );
}

function useObservabilitySources() {
  const [sources, setSources] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const result = await listObservabilitySources();
      setSources(result.items || []);
    } catch (loadError) {
      setError(loadError.message || "可观测数据源加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);
  return { sources, loading, error, load };
}

function SourceAwareCapabilityPage({ icon, title, description, children }) {
  const { sources, loading, error, load } = useObservabilitySources();
  return (
    <div className="capability-page">
      <CapabilityPageHeader
        icon={icon}
        title={title}
        description={description}
        loading={loading}
        onRefresh={load}
      />
      {error ? (
        <Alert
          type="error"
          showIcon
          message="数据源加载失败"
          description={error}
          action={<Button size="small" onClick={load}>重试</Button>}
        />
      ) : null}
      <section className="capability-page-workspace" aria-busy={loading}>
        {children(sources)}
      </section>
    </div>
  );
}

