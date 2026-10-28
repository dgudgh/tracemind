import React, { useEffect, useState } from "react";
import { Spin, message } from "antd";
import { mysqlOpsApi } from "./apiClients";
import { OpsEmptyState } from "./OpsLayout";
import { MySQLLockTab } from "./MySQLLockTab";
import { MySQLSlowSqlTab } from "./MySQLSlowSqlTab";
import { MySQLBasicTab, MySQLBinlogCenterTab, MySQLCapacityTab } from "./MySQLCoreOpsTabs";
import {
  MySQLDeadlockTab,
  MySQLErrorLogTab,
  MySQLInnoDBHealthTab,
  MySQLMGRTab,
  MySQLRelayLogTab,
  MySQLReplicationTab,
  MySQLSecurityTab,
  MySQLUserTab,
  MySQLVariablesTab,
} from "./MySQLDailyOpsTabs";
import { MySQLStatisticsTab, MySQLTableIndexCenterTab } from "./MySQLTableIndexStatsTabs";
import { MYSQL_DAILY_ENDPOINTS } from "./modules/mysqlOpsConfig";

export { MySQLLockTab } from "./MySQLLockTab";
export { MySQLSlowSqlTab } from "./MySQLSlowSqlTab";

export function MySQLDailyOperationsPage({ instanceId, initialTab }) {
  const [tab, setTab] = useState(initialTab || "basic");
  const [data, setData] = useState({});
  const [loading, setLoading] = useState(false);

  const load = async (endpoint) => {
    if (!instanceId || !endpoint) return;
    setLoading(true);
    try {
      const r = await mysqlOpsApi.section(instanceId, endpoint);
      setData(r);
    } catch (e) {
      message.error(e.message);
      setData({ error: e.message });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load(MYSQL_DAILY_ENDPOINTS[tab]);
  }, [tab, instanceId]);

  if (!instanceId) {
    return <OpsEmptyState title="请选择 MySQL 实例" description="在右上角选择一个 MySQL 实例，或前往「实例配置」添加 MySQL 实例。" />;
  }

  return (
    <div>
      <Spin spinning={loading}>
        {tab === "basic" && <MySQLBasicTab data={data} />}
        {tab === "variables" && <MySQLVariablesTab data={data} />}
        {tab === "capacity" && <MySQLCapacityTab data={data} instanceId={instanceId} />}
        {tab === "binlog" && <MySQLBinlogCenterTab data={data} instanceId={instanceId} />}
        {tab === "relaylog" && <MySQLRelayLogTab data={data} />}
        {tab === "errorlog" && <MySQLErrorLogTab data={data} />}
        {tab === "innodb" && <MySQLInnoDBHealthTab data={data} />}
        {tab === "locks" && <MySQLLockTab data={data} instanceId={instanceId} />}
        {tab === "slowsql" && <MySQLSlowSqlTab data={data} instanceId={instanceId} />}
        {tab === "deadlocks" && <MySQLDeadlockTab data={data} />}
        {tab === "users" && <MySQLUserTab data={data} instanceId={instanceId} />}
        {tab === "security" && <MySQLSecurityTab data={data} />}
        {tab === "tables" && <MySQLTableIndexCenterTab data={data} instanceId={instanceId} />}
        {tab === "statistics" && <MySQLStatisticsTab data={data} instanceId={instanceId} />}
        {tab === "replication" && <MySQLReplicationTab data={data} />}
        {tab === "mgr" && <MySQLMGRTab data={data} />}
      </Spin>
    </div>
  );
}
