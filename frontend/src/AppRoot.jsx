import React from "react";
import {
  App as AntApp,
} from "antd";
import { AppPageRouter } from "./AppPageRouter";
import { DEFAULT_ENGINE, useAppRootState } from "./AppRootState";
import { MESGUARD_ADMIN_THEME } from "./framework/adminTheme";
import { watchFrontendVersion } from "./framework/frontendVersion";
import { findMenuItem, normalizeMenuKey } from "./menuConfig";
import { LoginPage, SystemInitPage } from "./modules";
import { routeDiagnosisIntent } from "./modules/diagnosisCenterApi";
import { resolveSmartNavigation, shouldUseDirectSmartNavigation } from "./modules/smartNavigation";
import { OperationsShell } from "./OpsLayout";

const { Text } = Typography;

function LoadingScreen({ text }) {
  return (
    <div className="login-shell">
      <Space direction="vertical" align="center" size={12}>
        <Spin size="large" />
        <Text type="secondary">{text}</Text>
      </Space>
    </div>
  );
}

export function AppRoot() {
  const appState = useAppRootState();

  React.useEffect(() => watchFrontendVersion(), []);

  const openNavigationTarget = (result) => {
    if (!result?.targetKey) return null;
    const targetKey = normalizeMenuKey(result.targetKey);
    const targetDbType = String(result.targetDbType || "").toLowerCase();
    if (targetDbType) {
      const current = appState.instances.find((item) => item.id === appState.headerInstanceId);
      // Never auto-pick another engine's first instance: clear it and let the user choose.
      appState.setHeaderInstanceId(current?.db_type === targetDbType ? current.id : null);
    }
    const target = findMenuItem(appState.visibleMenuItems, targetKey);
    const requiredDbTypes = target?.dbTypes || [];
    if (!targetDbType && requiredDbTypes.length) {
      const currentInstance = appState.instances.find((item) => item.id === appState.headerInstanceId);
      // Keep a compatible selection; otherwise clear instead of silently choosing another.
      if (!currentInstance || !requiredDbTypes.includes(currentInstance.db_type)) {
        appState.setHeaderInstanceId(null);
      }
    }

    if (Array.isArray(appState.visibleMenuItems) && !target) return null;
    appState.setActiveKey(targetKey);
    return { ...result, targetKey };
  };

  const handleSmartNavigate = async (command) => {
    const selectedInstance = appState.instances.find((item) => item.id === appState.headerInstanceId);
    const localResult = resolveSmartNavigation(command, {
      currentKey: appState.activeKey,
      menuItems: appState.visibleMenuItems,
      selectedInstance,
    });
    if (shouldUseDirectSmartNavigation(localResult, command)) {
      return openNavigationTarget({
        ...localResult,
        message: `已快速打开${localResult.path?.join(" / ") || localResult.title}`,
      });
    }

    try {
      const intent = await routeDiagnosisIntent({
        text: command,
        current_key: appState.activeKey,
        selected_instance_id: selectedInstance?.id || null,
        selected_db_type: selectedInstance?.db_type || null,
      });
      const targetKey = intent.action === "diagnose" ? "diagnose.smart" : intent.target_key;
      if (targetKey) {
        return openNavigationTarget({
          targetKey,
          targetDbType: intent.target_db_type,
          title: intent.target_name,
          message: `${intent.source === "ai" ? "AI 判断" : "已匹配"}：打开${intent.target_name || "对应功能"}`,
        });
      }
    } catch (error) {
      void error;
    }

    if (!localResult) return null;
    return openNavigationTarget(localResult);
  };

  if (!appState.initChecked) {
    return <LoadingScreen text="正在检查系统状态..." />;
  }

  if (!appState.authChecked) {
    return <LoadingScreen text="正在验证登录状态..." />;
  }

  if (appState.needInit) {
    return <SystemInitPage />;
  }

  if (!appState.authToken) {
    return <LoginPage onLogin={appState.doLogin} onMfaLogin={appState.doMfaLogin} onExternalLogin={appState.doExternalLogin} />;
  }

  return (
    <ConfigProvider theme={MESGUARD_ADMIN_THEME}>
      <AntApp>
        <OperationsShell
          title={appState.title}
          menuItems={appState.visibleMenuItems}
          activeKey={appState.activeKey}
          openKeys={appState.openKeys}
          instances={appState.headerInstances}
          selectedInstanceId={appState.headerInstanceId}
          loadingInstances={appState.loadingInstances}
          riskAlerts={appState.capacityAlerts}
          onMenuClick={appState.setActiveKey}
          onOpenKeysChange={appState.setOpenKeys}
          onInstanceChange={appState.setHeaderInstanceId}
          onRefresh={appState.refreshInstances}
          onLogout={appState.doLogout}
          onSmartNavigate={handleSmartNavigate}
        >
          <AppPageRouter
            activeKey={appState.activeKey}
            instances={appState.instances}
            headerInstanceId={appState.headerInstanceId}
            loadingInstances={appState.loadingInstances}
            capacityAlerts={appState.capacityAlerts}
            engine={DEFAULT_ENGINE}
            routeDbType={appState.routeDbType}
            menuItems={appState.visibleMenuItems}
            onActiveKeyChange={appState.setActiveKey}
            onCapacityAlertsClosed={appState.clearCapacityAlerts}
            onHeaderInstanceChange={appState.setHeaderInstanceId}
            onRefreshInstances={appState.refreshInstances}
          />
        </OperationsShell>
      </AntApp>
    </ConfigProvider>
  );
}
