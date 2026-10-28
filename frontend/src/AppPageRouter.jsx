import React, { useEffect, useState } from "react";
import { EngineCatalogContextNotice } from "./EngineCatalogContextNotice";
import {



} from "./DatabaseAggregatePages";
import {
  AlertCenterPage,







  ObservabilityCenterPage,





} from "./modules";
import { OpsEmptyState } from "./OpsLayout";
import ManualRequirementHandoff from "./ManualRequirementHandoff";
import { getAuthSession } from "./modules/authStorage";
import { onAuthExpired } from "./modules/authEvents";
import { createManualRequirementHandoff, manualRequirementHandoffMatches } from "./modules/requirementManualHandoff";

// 运维能力 → 各引擎实现。值为组件 = Oracle/MySQL 逐页组件；值为字符串 = PG/SS 中心页的 section。
// 缺失的 (能力, 引擎) 组合表示该引擎不提供此能力（菜单已按 engines 隐藏，此处兜底空态）。
const OPS_DISPATCH = {
  overview: { oracle: OracleOverviewPage, mysql: MySQLOverviewPage, postgresql: "overview", sqlserver: "overview" },
  space: { oracle: OracleSpacePage, mysql: MySQLStoragePage, postgresql: "space", sqlserver: "space" },
  log: { oracle: OracleLogArchivePage, postgresql: "wal", sqlserver: "logs" },
  session: { oracle: OracleSessionPage, mysql: MySQLInnoDBTxPage, postgresql: "sessionTransaction", sqlserver: "sessionTransaction" },
  object: { oracle: OracleObjectPage, mysql: MySQLTableIndexPage, postgresql: "objectStats", sqlserver: "objectStats" },
  security: { mysql: MySQLUserSecurityPage },
};

function renderOpsCapability(capability, ctx) {
  const instance = ctx.instances.find((item) => item.id === ctx.headerInstanceId);
  if (!instance) {
    return <OpsEmptyState title="请选择实例" description="请在右上角选择要运维的数据库实例。" />;
  }
  const target = OPS_DISPATCH[capability]?.[instance.db_type];
  if (!target) {
    return (
      <OpsEmptyState
        title="该能力暂不适用"
        description={`${(instance.db_type || "当前").toUpperCase()} 暂不提供「${capability}」运维能力。`}
      />
    );
  }
  if (typeof target === "string") {
    const Center = instance.db_type === "postgresql"
      ? PostgreSQLOperationsCenterPage
      : SQLServerOperationsCenterPage;
    return (
      <Center
        activeKey={`ops.${instance.db_type}.${target}`}
        instanceId={ctx.headerInstanceId}
        instances={ctx.instances}
      />
    );
  }
  const Component = target;
  return <Component instanceId={ctx.headerInstanceId} instances={ctx.instances} />;
}

// 标准路由表：key → { c: 组件, data: 数据契约 }。
// data 决定 AppPageRouter 注入哪套 props（见 menuConfig.jsx 约定）。
const STATIC_ROUTES = {
  // special → switch 分支单独渲染；这里登记 key，确保菜单/路由/模块配置可静态核对。
  "home.dashboard": { c: EditionDashboard, data: "special" },
  "asset.center": { c: CmdbAssetPage, data: "special" },
  "perf.slowsql": { c: SlowSqlPage, data: "special" },
  "perf.lock": { c: LockAnalysisPage, data: "special" },
  "perf.awr.report": { c: OracleAwrReportPage, data: "special" },
  // static → { activeKey, onNavigate }
  "monitor.observability": { c: ObservabilityCenterPage, data: "static" },
  "inspect.reports": { c: InspectionReportPage, data: "static" },
  "inspect.tasks": { c: InspectionTaskPage, data: "static" },
  "report.unified": { c: UnifiedReportCenterPage, data: "selected" },
  "report.alert": { c: AlertReportsPage, data: "static" },
  "system.user": { c: UserManagementPage, data: "static" },
  "system.params": { c: SystemParamsPage, data: "static" },
  "system.ai": { c: AiConfigPage, data: "static" },

  // list → { instances }
  "monitor.alert.unified": { c: UnifiedAlertEventPage, data: "list" },
  "system.schedule": { c: SchedulePage, data: "list" },

  // 只读：引擎目录快照 + 本机驱动状态。不提供包安装/激活/回滚（主机侧 CLI 专有）。
  "platform.engines": { c: EngineCatalogPage, data: "static" },

  // 运维能力（ops.*）由 OPS_DISPATCH 按引擎分发，不在标准表中。

  // selected → { instances, selectedInstanceId, onNavigate }
  "inspect.overview": { c: InspectionOverviewPage, data: "selected" },
  "inspect.run": { c: InspectionCenterPage, data: "selected" },
  "perf.baseline": { c: PerfBaselinePage, data: "selected" },
  "perf.capacity": { c: CapacityPage, data: "selected" },
  "diagnose.smart": { c: UnifiedDiagnosisPage, data: "selected" },
  "monitor.alert.db": { c: AlertCenterPage, data: "selected" },
};

function buildProps(data, ctx) {
  switch (data) {
    case "static":
      return { activeKey: ctx.activeKey, onNavigate: ctx.navigateTo };
    case "list":
      return { instances: ctx.instances };
    case "instanceId":
      return { instanceId: ctx.headerInstanceId, instances: ctx.instances };
    case "selected":
      return {
        instances: ctx.instances,
        selectedInstanceId: ctx.headerInstanceId,
        menuItems: ctx.menuItems,
        onNavigate: ctx.navigateTo,
      };
    default:
      return {};
  }
}

function AppPageContent({
  activeKey,
  instances,
  headerInstanceId,
  loadingInstances,
  capacityAlerts,
  engine,
  routeDbType,
  menuItems,
  onActiveKeyChange,
  onCapacityAlertsClosed,
  onHeaderInstanceChange,
  onRefreshInstances,
  onManualRequirementHandoff,
}) {
  const [executionResult, setExecutionResult] = useState(null);
  const navigateTo = (key, instanceId, result = null) => {
    const manual = result?.kind === "manual_requirement";
    setExecutionResult(result && !manual ? { key, instanceId, result } : null);
    onManualRequirementHandoff(manual ? result : null, key, instanceId);
    if (instanceId !== undefined) onHeaderInstanceChange(instanceId ?? undefined);
    onActiveKeyChange(key);
  };

  const ctx = { activeKey, instances, headerInstanceId, menuItems, navigateTo };

  // 1) 前缀路由：报告/抢救/PG/SQLServer 运维由各自“中心页”按 activeKey 内部分发。
  if (activeKey === "ops.configGovernance") {
    return <ConfigGovernancePage instances={instances} onNavigate={navigateTo} />;
  }
  // 运维能力组：ops.<capability> 按选中实例引擎自适应分发（PG/SS 走中心页）。
  if (activeKey.startsWith("ops.") && activeKey !== "ops.governance.objectStorage") {
    return <EngineCatalogContextNotice instanceId={headerInstanceId} instances={instances}>
      {renderOpsCapability(activeKey.slice("ops.".length), ctx)}
    </EngineCatalogContextNotice>;
  }

  // 2) 特殊页：props 形态独特，单独渲染。
  switch (activeKey) {
    case "home.dashboard":
      return (
        <EditionDashboard
          instances={instances}
          capacityAlerts={capacityAlerts}
          onCapacityAlertsClosed={onCapacityAlertsClosed}
          onNavigate={navigateTo}
        />
      );
    case "asset.center":
      return (
        <CmdbAssetPage
          instances={instances}
          loading={loadingInstances}
          onChanged={onRefreshInstances}
          engine={engine}
        />
      );
    case "perf.overview":
      return <PerformanceOverviewPage instances={instances} onNavigate={navigateTo} />;
    case "perf.slowsql":
      return <SlowSqlPage instances={instances} engine={routeDbType} selectedInstanceId={headerInstanceId}
        executionResult={executionResult?.key === activeKey && String(executionResult.instanceId) === String(headerInstanceId) ? executionResult.result : null} />;
    case "perf.lock":
      return <LockAnalysisPage instanceId={headerInstanceId} instances={instances} engine={routeDbType} />;
    case "perf.awr.report":
      return <OracleAwrReportPage instanceId={headerInstanceId} />;
    default:
      break;
  }

  // 3) 标准路由表 + data 契约注入。
  const route = STATIC_ROUTES[activeKey];
  if (route) {
    const Component = route.c;
    return <Component {...buildProps(route.data, { ...ctx, routeDbType })} />;
  }

  return null;
}

export function AppPageRouter(props) {
  const { activeKey, headerInstanceId, menuItems } = props;
  const [manualHandoff, setManualHandoff] = useState(null);
  const session = getAuthSession();
  const visible = manualRequirementHandoffMatches(manualHandoff, { activeKey, instanceId: headerInstanceId, session, menuItems });
  useEffect(() => {
    if (manualHandoff && !visible) setManualHandoff(null);
  }, [manualHandoff, visible]);
  useEffect(() => {
    const clear = () => setManualHandoff(null);
    const removeExpired = onAuthExpired(clear);
    globalThis.addEventListener?.("storage", clear);
    return () => { removeExpired(); globalThis.removeEventListener?.("storage", clear); };
  }, []);
  const receiveManualHandoff = (payload, targetKey, instanceId) => {
    setManualHandoff(createManualRequirementHandoff(payload, {
      targetKey, instanceId: instanceId === undefined ? headerInstanceId : instanceId, menuItems, session: getAuthSession(),
    }));
  };
  return (
    <div className="app-page-frame" data-page-key={activeKey}>
      {visible && <ManualRequirementHandoff handoff={manualHandoff} onClose={() => setManualHandoff(null)} />}
      <AppPageContent {...props} onManualRequirementHandoff={receiveManualHandoff} />
    </div>
  );
}
