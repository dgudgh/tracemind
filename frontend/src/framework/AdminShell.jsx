import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  Breadcrumb,
  Button,
  Drawer,
  Input,
  Layout,
  Menu,
  Select,
  Space,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  message,
} from "antd";
import {
  AppstoreOutlined,
  DatabaseOutlined,
  LogoutOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  PushpinOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import { findAdminBreadcrumb, getAdminMenuLabel } from "./menuModel";
import { getAdminShellCollapsed, saveAdminShellCollapsed } from "../modules/authStorage";
import { findMenuKeyPath, resolveAccordionOpenKeys } from "../modules/menuNavigation";
import {
  QUICK_ACTION_STORAGE_KEY,
  QUICK_ACCESS_UPDATED_EVENT,
  isQuickAccessPagePinned,
  pinQuickAccessPage,
} from "../modules/quickAccess";

const { Header, Sider, Content } = Layout;
const { Text, Title } = Typography;

const MAX_WORKSPACE_TABS = 10;
const COMPACT_NAVIGATION_QUERY = "(max-width: 1099.98px)";

function AdminRiskSummary({ alerts = [] }) {
  const critical = alerts.filter((item) => item.level === "critical" || item.level === "high").length;
  const warning = alerts.length - critical;
  if (!alerts.length) {
    return (
      <Tag className="admin-risk-tag admin-risk-tag-healthy" icon={<SafetyCertificateOutlined />}>
        风险 0
      </Tag>
    );
  }
  return (
    <Space size={6} className="admin-risk-summary">
      {critical > 0 && <Tag className="admin-risk-tag admin-risk-tag-critical">严重 {critical}</Tag>}
      {warning > 0 && <Tag className="admin-risk-tag admin-risk-tag-warning">预警 {warning}</Tag>}
    </Space>
  );
}

function buildInstanceOptions(instances = []) {
  return instances.map((item) => ({
    value: item.id,
    label: `[${(item.db_type || "DB").toUpperCase()}] ${item.instance_name} (${item.host}:${item.port})`,
  }));
}

export function AdminShell({
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
  const [collapsed, setCollapsed] = useState(() => getAdminShellCollapsed());
  const [compactNavigation, setCompactNavigation] = useState(
    () => typeof window !== "undefined" && window.matchMedia(COMPACT_NAVIGATION_QUERY).matches,
  );
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const [expertToolsOpen, setExpertToolsOpen] = useState(false);
  const [workspaceTabs, setWorkspaceTabs] = useState([]);
  const [smartCommand, setSmartCommand] = useState("");
  const [smartNavigating, setSmartNavigating] = useState(false);
  const [quickAccessRevision, setQuickAccessRevision] = useState(0);
  const contentRef = useRef(null);
  const breadcrumb = useMemo(() => findAdminBreadcrumb(menuItems, activeKey), [activeKey, menuItems]);
  const instanceOptions = useMemo(() => buildInstanceOptions(instances), [instances]);
  const selectedInstance = useMemo(
    () => instances.find((item) => item.id === selectedInstanceId),
    [instances, selectedInstanceId],
  );
  const quickAccessPinned = useMemo(
    () => isQuickAccessPagePinned(activeKey),
    [activeKey, quickAccessRevision],
  );
  const cockpitMode = activeKey === "diagnose.smart";
  const effectiveCollapsed = compactNavigation || collapsed;
  const handleMenuOpenChange = (nextOpenKeys) => {
    onOpenKeysChange?.(resolveAccordionOpenKeys(menuItems, openKeys, nextOpenKeys));
  };
  const handleMenuClick = (key) => {
    setMobileMenuOpen(false);
    onOpenKeysChange?.(findMenuKeyPath(menuItems, key).slice(0, -1));
    onMenuClick?.(key);
  };

  useEffect(() => {
    saveAdminShellCollapsed(collapsed);
  }, [collapsed]);

  useLayoutEffect(() => {
    contentRef.current?.scrollTo({ top: 0, left: 0, behavior: "auto" });
  }, [activeKey]);

  useEffect(() => {
    if (typeof window === "undefined") return undefined;
    const mediaQuery = window.matchMedia(COMPACT_NAVIGATION_QUERY);
    const syncCompactNavigation = () => {
      setCompactNavigation(mediaQuery.matches);
      setMobileMenuOpen(false);
    };
    syncCompactNavigation();
    mediaQuery.addEventListener("change", syncCompactNavigation);
    return () => mediaQuery.removeEventListener("change", syncCompactNavigation);
  }, []);

  useEffect(() => {
    if (effectiveCollapsed) return;
    onOpenKeysChange?.(findMenuKeyPath(menuItems, activeKey).slice(0, -1));
  }, [activeKey, effectiveCollapsed, menuItems]);

  useEffect(() => {
    const markChanged = () => setQuickAccessRevision((value) => value + 1);
    const markStorageChanged = (event) => {
      if (event.key && event.key !== QUICK_ACTION_STORAGE_KEY) return;
      markChanged();
    };
    window.addEventListener(QUICK_ACCESS_UPDATED_EVENT, markChanged);
    window.addEventListener("storage", markStorageChanged);
    return () => {
      window.removeEventListener(QUICK_ACCESS_UPDATED_EVENT, markChanged);
      window.removeEventListener("storage", markStorageChanged);
    };
  }, []);

  useEffect(() => {
    const label = getAdminMenuLabel(menuItems, activeKey, title || "TraceMind AI");
    setWorkspaceTabs((prev) => {
      const exists = prev.some((item) => item.key === activeKey);
      const next = exists
        ? prev.map((item) => (item.key === activeKey ? { ...item, label } : item))
        : [...prev, { key: activeKey, label }];
      return next.slice(Math.max(0, next.length - MAX_WORKSPACE_TABS));
    });
  }, [activeKey, menuItems, title]);

  const closeWorkspaceTab = (targetKey) => {
    setWorkspaceTabs((prev) => {
      if (prev.length <= 1) return prev;
      const targetIndex = prev.findIndex((item) => item.key === targetKey);
      const next = prev.filter((item) => item.key !== targetKey);
      if (targetKey === activeKey && next.length) {
        const fallback = next[Math.max(0, targetIndex - 1)] || next[0];
        setTimeout(() => onMenuClick?.(fallback.key), 0);
      }
      return next;
    });
  };

  const tabItems = workspaceTabs.map((item) => ({
    key: item.key,
    label: item.label,
    closable: item.key !== "home.dashboard" && workspaceTabs.length > 1,
  }));

  const submitSmartCommand = async (value) => {
    const command = String(value || smartCommand || "").trim();
    if (!command) {
      message.warning("请输入要打开的功能或现场问题");
      return;
    }
    setSmartNavigating(true);
    try {
      const result = await onSmartNavigate?.(command);
      if (result) {
        message.success(result.message);
        setSmartCommand("");
        return;
      }
      message.warning("没有匹配到功能，请补充数据库类型或操作动作");
    } catch (error) {
      message.error(error?.message || "智能分诊失败");
    } finally {
      setSmartNavigating(false);
    }
  };

  const pinCurrentPage = () => {
    const pageTitle = getAdminMenuLabel(menuItems, activeKey, title || "快速入口");
    const result = pinQuickAccessPage({
      targetKey: activeKey,
      title: pageTitle,
      breadcrumb,
      instance: selectedInstance,
    });
    setQuickAccessRevision((value) => value + 1);
    message.success(result.updated ? "已更新首页快速卡片" : "已固定到首页快速卡片");
  };

  return (
    <Layout className={`app-shell admin-shell${effectiveCollapsed ? " admin-shell-collapsed" : ""}${cockpitMode ? " admin-shell-cockpit" : ""}`}>
      {!cockpitMode && (
        <Sider
          width={252}
          collapsedWidth={compactNavigation ? 0 : 76}
          breakpoint="lg"
          collapsed={effectiveCollapsed}
          trigger={null}
          className="sidebar admin-sidebar"
        >
          <div className="brand admin-brand">
            <div className="admin-brand-mark">
              <DatabaseOutlined />
            </div>
            <div className="admin-brand-text">
              <div className="brand-name">TraceMind AI</div>
              <div className="brand-subtitle">智能运维平台</div>
            </div>
          </div>
          <Menu
            className="admin-menu"
            mode="inline"
            inlineCollapsed={effectiveCollapsed}
            selectedKeys={[activeKey]}
            openKeys={effectiveCollapsed ? [] : openKeys}
            onOpenChange={handleMenuOpenChange}
            items={menuItems}
            onClick={({ key }) => handleMenuClick(key)}
          />
        </Sider>
      )}

      <Layout className="admin-main">
        <Header className="topbar admin-topbar">
          <Space className="admin-topbar-left" size={14}>
            {cockpitMode ? (
              <Tooltip title="打开专家工具抽屉">
                <Button
                  className="admin-icon-button"
                  icon={<MenuUnfoldOutlined />}
                  onClick={() => setExpertToolsOpen(true)}
                />
              </Tooltip>
            ) : (
              <Tooltip title={compactNavigation ? "打开导航菜单" : collapsed ? "展开菜单" : "收起菜单"}>
                <Button
                  className="admin-icon-button"
                  icon={effectiveCollapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
                  onClick={() => {
                    if (compactNavigation) setMobileMenuOpen(true);
                    else setCollapsed((value) => !value);
                  }}
                />
              </Tooltip>
            )}
            <div className="admin-title-block">
              <Breadcrumb
                className="admin-breadcrumb"
                items={(breadcrumb.length ? breadcrumb : ["TraceMind AI"]).map((item) => ({ title: item }))}
              />
              <Title level={4}>{title}</Title>
            </div>
          </Space>

          <Space className="admin-topbar-actions" size={10}>
            <Input.Search
              className="admin-smart-command"
              allowClear
              value={smartCommand}
              placeholder="描述要打开的功能"
              enterButton={<ThunderboltOutlined />}
              loading={smartNavigating}
              onChange={(event) => setSmartCommand(event.target.value)}
              onSearch={submitSmartCommand}
            />
            <Tooltip title={quickAccessPinned ? "已固定到首页快速卡片，点击更新" : "固定当前页到首页快速卡片"}>
              <Button
                className={`admin-icon-button admin-pin-button${quickAccessPinned ? " is-pinned" : ""}`}
                icon={<PushpinOutlined />}
                onClick={pinCurrentPage}
              />
            </Tooltip>
            <AdminRiskSummary alerts={riskAlerts} />
            <Select
              className="admin-instance-select"
              placeholder="选择实例"
              allowClear
              showSearch
              value={selectedInstanceId}
              loading={loadingInstances}
              optionFilterProp="label"
              options={instanceOptions}
              onChange={onInstanceChange}
            />
            <Tooltip title="刷新实例与告警">
              <Button className="admin-icon-button" icon={<ReloadOutlined />} onClick={onRefresh} />
            </Tooltip>
            <Tooltip title="退出登录">
              <Button className="admin-icon-button" icon={<LogoutOutlined />} onClick={onLogout} />
            </Tooltip>
          </Space>
        </Header>

        {!cockpitMode && <div className="admin-workspace-bar">
          <Space size={8} className="admin-workspace-label">
            <AppstoreOutlined />
            <Text>当前空间</Text>
          </Space>
          <Tabs
            className="admin-page-tabs"
            type="editable-card"
            hideAdd
            size="small"
            activeKey={activeKey}
            items={tabItems}
            onChange={(key) => onMenuClick?.(key)}
            onEdit={(targetKey, action) => {
              if (action === "remove") closeWorkspaceTab(targetKey);
            }}
          />
        </div>}

        <Content ref={contentRef} className="content admin-content">{children}</Content>
      </Layout>

      <Drawer
        title="导航菜单"
        open={mobileMenuOpen}
        onClose={() => setMobileMenuOpen(false)}
        placement="left"
        width="min(320px, 88vw)"
        className="admin-expert-drawer"
      >
        <Menu
          className="admin-menu"
          mode="inline"
          selectedKeys={[activeKey]}
          openKeys={openKeys}
          onOpenChange={handleMenuOpenChange}
          items={menuItems}
          onClick={({ key }) => handleMenuClick(key)}
        />
      </Drawer>

      <Drawer
        title="专家工具"
        open={expertToolsOpen}
        onClose={() => setExpertToolsOpen(false)}
        placement="left"
        width={320}
        className="admin-expert-drawer"
      >
        <Menu
          className="admin-menu"
          mode="inline"
          selectedKeys={[activeKey]}
          openKeys={openKeys}
          onOpenChange={handleMenuOpenChange}
          items={menuItems}
          onClick={({ key }) => {
            setExpertToolsOpen(false);
            window.setTimeout(() => handleMenuClick(key), 0);
          }}
        />
      </Drawer>
    </Layout>
  );
}
