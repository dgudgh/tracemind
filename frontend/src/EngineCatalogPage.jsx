import React, { useCallback, useEffect, useState } from "react";
import { Alert, Button, Card, Space, Table, Tag, Typography } from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import { readEngineCatalog, readEngineDriverStatus } from "./apiClients";
import { getAuthSession, isAuthSessionCurrent } from "./modules/authStorage";
import { engineCatalogReason } from "./modules/engineCatalog";

const { Text } = Typography;

const FAMILY_LABELS = {
  oracle: "Oracle", mysql: "MySQL", postgresql: "PostgreSQL",
  sqlserver: "SQL Server", dm: "达梦", kingbasees: "人大金仓",
};

function engineLabel(engineId) {
  return FAMILY_LABELS[engineId] ? `${FAMILY_LABELS[engineId]}（${engineId}）` : engineId;
}

/**
 * 引擎与版本（只读）。
 *
 * 这里刻意不提供任何"安装 / 激活 / 回滚"按钮：包生命周期按设计只走主机侧
 * CLI（scripts/engine_pack_manager.py --mode production），平台侧只做只读投影。
 * 页面上同时列"引擎目录状态"和"本机驱动状态"，是因为这两件事经常被混为一谈：
 *   目录 available=false 可能是"扩展包没装"，
 *   驱动 installed=false 才是"本机缺 Python 驱动"。
 * 两者都不等于"服务端已认证"。
 */
export function EngineCatalogPage() {
  const [catalog, setCatalog] = useState(null);
  const [drivers, setDrivers] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    const session = getAuthSession();
    setLoading(true);
    setError("");
    try {
      const [catalogValue, driverValue] = await Promise.all([
        readEngineCatalog(session),
        readEngineDriverStatus(session),
      ]);
      if (!isAuthSessionCurrent(session)) return;
      setCatalog(catalogValue);
      setDrivers(driverValue);
    } catch (e) {
      if (isAuthSessionCurrent(session)) {
        setError(e?.message || "读取引擎状态失败");
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const driverByEngine = new Map((drivers?.drivers || []).map((row) => [row.engine_id, row]));

  const catalogColumns = [
    { title: "引擎", dataIndex: "engine_id", key: "engine_id", render: (v) => engineLabel(v) },
    { title: "族", dataIndex: "family", key: "family", width: 120 },
    { title: "协议", dataIndex: "protocol", key: "protocol", width: 110 },
    { title: "Provider 版本", dataIndex: "provider_version", key: "provider_version", width: 130,
      render: (v) => (v ? <Text code>{v}</Text> : <Text type="secondary">未安装</Text>) },
    { title: "目录状态", key: "available", width: 150,
      render: (_, row) => (row.available
        ? <Tag color="green">可用</Tag>
        : <Tag color="default">{engineCatalogReason(row)}</Tag>) },
    { title: "授权能力数", key: "capabilities", width: 110,
      render: (_, row) => (row.capabilities || []).length },
  ];

  const driverColumns = [
    { title: "引擎", dataIndex: "engine_id", key: "engine_id", render: (v) => engineLabel(v) },
    { title: "声明驱动", dataIndex: "declared_driver", key: "declared_driver",
      render: (v) => <Text code>{v || "-"}</Text> },
    { title: "本机是否可导入", key: "installed", width: 150,
      render: (_, row) => (row.installed
        ? <Tag color="green">可导入</Tag>
        : <Tag color="red">缺失</Tag>) },
    { title: "实际可用模块", key: "available_via", width: 200,
      render: (_, row) => ((row.available_via || []).length
        ? (row.available_via || []).map((name) => <Tag key={name}>{name}</Tag>)
        : <Text type="secondary">无</Text>) },
    { title: "说明", dataIndex: "note", key: "note",
      render: (v) => (v ? <Text type="warning">{v}</Text> : <Text type="secondary">—</Text>) },
  ];

  return (
    <>
      <Space direction="vertical" size={14} className="full-width">
        <Card
          size="small"
          title="引擎与版本（只读）"
          extra={<Button size="small" icon={<ReloadOutlined />} loading={loading} onClick={load}>刷新</Button>}
        >
          <Alert
            type="info"
            showIcon
            message="本页只读，不提供安装、激活或回滚"
            description={
              <span>
                引擎包的生命周期按设计只在主机侧执行（<Text code>scripts/engine_pack_manager.py --mode production</Text>）。
                平台侧仅展示服务端发布的目录快照；<strong>目录状态与本机驱动状态都不等于服务端已认证</strong>。
              </span>
            }
          />
          {error ? (
            <Alert style={{ marginTop: 12 }} type="error" showIcon message="读取失败" description={error} />
          ) : null}
          <div style={{ marginTop: 12 }}>
            <Text type="secondary">
              {catalog
                ? `目录代次 ${catalog.catalog_generation} · 快照 ${catalog.catalog_boot_id.slice(0, 8)} · 共 ${catalog.engines.length} 个引擎`
                : "目录状态未确认"}
            </Text>
          </div>
          <Table
            style={{ marginTop: 10 }}
            size="small"
            rowKey="engine_id"
            loading={loading}
            columns={catalogColumns}
            dataSource={catalog?.engines || []}
            pagination={false}
          />
        </Card>

        <Card size="small" title="本机驱动状态">
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message={drivers?.disclaimer || "驱动状态未确认"}
          />
          <Table
            size="small"
            rowKey="engine_id"
            loading={loading}
            columns={driverColumns}
            dataSource={drivers?.drivers || []}
            pagination={false}
          />
        </Card>

        <Card size="small" title="想让它变成可执行，还差什么">
          <ul style={{ margin: 0, paddingLeft: 20 }}>
            <li>引擎目录全部显示"尚未安装扩展包"时：服务端没有发布真实目录（发布者未接线），
              不是"引擎坏了"。</li>
            <li>驱动显示"缺失"时：该引擎连真实连接都建立不了，任何能力都无从谈起。</li>
            <li>两者都满足后，仍需要逐条 SQL 在目标库上实测，才谈"认证"。</li>
          </ul>
        </Card>
      </Space>
    </>
  );
}
