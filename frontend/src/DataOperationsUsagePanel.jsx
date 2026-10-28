import React from "react";
import { Alert, Button, Card, Space, Table, Tabs, Tag } from "antd";
import { PlayCircleOutlined, ReloadOutlined } from "@ant-design/icons";
import { stableRecordKey } from "./modules/stableKeys";

export function DataOperationsUsagePanel({
  activeTab,
  usageData,
  usageTab,
  setUsageTab,
  usageSource,
  usageLoading,
  instanceId,
  collectTableUsage,
  loadTableUsage,
  buildUsageIndexPlan,
}) {
  if (activeTab !== "usage") return null;
  return (

        <Space direction="vertical" className="full-width" size={12}>
          <Card title={"表访问画像"} size="small"
            extra={<Space wrap>
              <Tag color="blue">{usageData[usageTab]?.source || usageSource}</Tag>
              <Button size="small" icon={<PlayCircleOutlined />} loading={usageLoading} disabled={!instanceId} onClick={collectTableUsage}>{"即时分析"}</Button>
              <Button size="small" type="primary" icon={<ReloadOutlined />} loading={usageLoading} disabled={!instanceId} onClick={() => loadTableUsage()}>{"刷新当前页"}</Button>
            </Space>}
          >
            <Alert type="info" showIcon style={{ marginBottom: 12 }} message={"建立表 → SQL → 执行频率 → 访问路径 → 索引建议的证据链"} description={"默认使用 AWR 历史，AWR 权限不足时后端会自动降级为当前缓存分析。所有索引变更只生成 SQL，不直接执行。"} />
            <Tabs activeKey={usageTab} onChange={(key) => { setUsageTab(key); if (!usageData[key]) loadTableUsage(key); }} items={[
              { key: "hot", label: "热点表排行", children: <Table dataSource={usageData.hot?.items || []} rowKey={(r) => `${r.owner}.${r.table_name}`} size="small" loading={usageLoading} scroll={{ x: 1500 }} pagination={{ defaultPageSize: 20, showSizeChanger: true }} columns={[
                { title: "Owner", dataIndex: "owner", width: 110 },
                { title: "表名", dataIndex: "table_name", width: 220, ellipsis: true },
                { title: "SQL数", dataIndex: "sql_count", width: 90 },
                { title: "执行次数", dataIndex: "executions", width: 120, render: (v) => Number(v || 0).toLocaleString() },
                { title: "日均执行", dataIndex: "avg_exec_per_day", width: 120, render: (v) => Number(v || 0).toLocaleString() },
                { title: "总耗时(s)", dataIndex: "elapsed_sec", width: 110, render: (v) => Number(v || 0).toFixed(2) },
                { title: "逻辑读", dataIndex: "buffer_gets", width: 130, render: (v) => Number(v || 0).toLocaleString() },
                { title: "全表扫描", dataIndex: "full_scan_sql_count", width: 110 },
                { title: "表大小MB", dataIndex: "table_size_mb", width: 120, render: (v) => Number(v || 0).toFixed(2) },
                { title: "风险", dataIndex: "risk_level", width: 90, render: (v) => <Tag color={v === "critical" ? "red" : v === "warning" ? "gold" : "green"}>{v || "normal"}</Tag> },
                { title: "操作", width: 170, fixed: "right", render: (_, r) => <Space size={4}><Button size="small" onClick={() => loadTableUsage("tableSql", r)}>SQL</Button><Button size="small" onClick={() => loadTableUsage("adviceForTable", r)}>{"建议"}</Button></Space> },
              ]} /> },
              { key: "tableSql", label: "单表 SQL 明细", children: <Table dataSource={usageData.tableSql?.items || []} rowKey={(r) => `${r.sql_id}-${r.plan_hash_value}-${r.operation}`} size="small" loading={usageLoading} scroll={{ x: 1500 }} pagination={{ defaultPageSize: 20, showSizeChanger: true }} columns={[
                { title: "SQL_ID", dataIndex: "sql_id", width: 120 },
                { title: "Plan", dataIndex: "plan_hash_value", width: 120 },
                { title: "访问方式", render: (_, r) => `${r.operation || ""} ${r.options || ""}`.trim(), width: 180 },
                { title: "执行次数", dataIndex: "executions", width: 110, render: (v) => Number(v || 0).toLocaleString() },
                { title: "平均耗时(s)", dataIndex: "avg_elapsed_sec", width: 120, render: (v) => Number(v || 0).toFixed(4) },
                { title: "逻辑读", dataIndex: "buffer_gets", width: 120, render: (v) => Number(v || 0).toLocaleString() },
                { title: "最后活跃", dataIndex: "last_active_time", width: 170 },
                { title: "SQL", dataIndex: "sql_text", ellipsis: true },
              ]} /> },
              { key: "fullScan", label: "全表扫描 SQL", children: <Table dataSource={usageData.fullScan?.items || []} rowKey={(r) => `${r.owner}.${r.table_name}.${r.sql_id}.${r.plan_hash_value}`} size="small" loading={usageLoading} scroll={{ x: 1500 }} pagination={{ defaultPageSize: 20, showSizeChanger: true }} columns={[
                { title: "Owner", dataIndex: "owner", width: 100 }, { title: "表名", dataIndex: "table_name", width: 190, ellipsis: true }, { title: "SQL_ID", dataIndex: "sql_id", width: 120 },
                { title: "执行次数", dataIndex: "executions", width: 110, render: (v) => Number(v || 0).toLocaleString() }, { title: "逻辑读", dataIndex: "buffer_gets", width: 120, render: (v) => Number(v || 0).toLocaleString() },
                { title: "建议", dataIndex: "suggestion", width: 260, ellipsis: true }, { title: "SQL", dataIndex: "sql_text", ellipsis: true },
              ]} /> },
              { key: "indexUsage", label: "索引使用频率", children: <Table dataSource={usageData.indexUsage?.items || []} rowKey={(r) => `${r.owner}.${r.index_name}`} size="small" loading={usageLoading} scroll={{ x: 1500 }} pagination={{ defaultPageSize: 20, showSizeChanger: true }} columns={[
                { title: "Owner", dataIndex: "owner", width: 100 }, { title: "表名", dataIndex: "table_name", width: 180, ellipsis: true }, { title: "索引", dataIndex: "index_name", width: 220, ellipsis: true },
                { title: "索引列", dataIndex: "columns_text", width: 260, ellipsis: true }, { title: "SQL数", dataIndex: "used_sql_count", width: 90 }, { title: "执行次数", dataIndex: "executions", width: 120, render: (v) => Number(v || 0).toLocaleString() },
                { title: "约束", dataIndex: "is_constraint_index", width: 80, render: (v) => v ? <Tag color="blue">PK/UK</Tag> : "-" }, { title: "最后使用", dataIndex: "last_seen", width: 170 },
              ]} /> },
              { key: "unusedIndex", label: "疑似未使用索引", children: <Table dataSource={usageData.unusedIndex?.items || []} rowKey={(r) => `${r.owner}.${r.index_name}`} size="small" loading={usageLoading} scroll={{ x: 1500 }} pagination={{ defaultPageSize: 20, showSizeChanger: true }} columns={[
                { title: "Owner", dataIndex: "owner", width: 100 }, { title: "表名", dataIndex: "table_name", width: 180, ellipsis: true }, { title: "索引", dataIndex: "index_name", width: 220, ellipsis: true },
                { title: "索引列", dataIndex: "columns_text", width: 260, ellipsis: true }, { title: "大小MB", dataIndex: "index_size_mb", width: 100, render: (v) => Number(v || 0).toFixed(2) },
                { title: "建议", dataIndex: "suggestion", width: 240, ellipsis: true }, { title: "操作", width: 170, fixed: "right", render: (_, r) => <Space size={4}><Button size="small" onClick={() => buildUsageIndexPlan(r, "invisible")}>INVISIBLE</Button><Button size="small" danger onClick={() => buildUsageIndexPlan(r, "drop")}>DROP</Button></Space> },
              ]} /> },
              { key: "duplicateIndex", label: "重复索引", children: <Table dataSource={usageData.duplicateIndex?.items || []} rowKey={(r) => `${r.table_owner}.${r.table_name}.${r.index_name_1}.${r.index_name_2}`} size="small" loading={usageLoading} scroll={{ x: 1300 }} pagination={{ defaultPageSize: 20, showSizeChanger: true }} columns={[
                { title: "Owner", dataIndex: "table_owner", width: 100 }, { title: "表名", dataIndex: "table_name", width: 180, ellipsis: true }, { title: "索引1", dataIndex: "index_name_1", width: 190, ellipsis: true }, { title: "列1", dataIndex: "columns_1", width: 240, ellipsis: true },
                { title: "索引2", dataIndex: "index_name_2", width: 190, ellipsis: true }, { title: "列2", dataIndex: "columns_2", width: 240, ellipsis: true }, { title: "关系", dataIndex: "relation_type", width: 100 }, { title: "建议", dataIndex: "suggestion", width: 260, ellipsis: true },
              ]} /> },
              { key: "advice", label: "索引建议", children: <Table dataSource={usageData.advice?.items || []} rowKey={(row) => stableRecordKey("oracle-index-advice", row, ["advice_type", "owner", "table_name", "sql_id", "index_name", "evidence_summary"])} size="small" loading={usageLoading} scroll={{ x: 1500 }} pagination={{ defaultPageSize: 20, showSizeChanger: true }} columns={[
                { title: "类型", dataIndex: "advice_type", width: 190 }, { title: "级别", dataIndex: "advice_level", width: 90, render: (v) => <Tag color={v === "critical" ? "red" : v === "warning" ? "gold" : "blue"}>{v}</Tag> },
                { title: "Owner", dataIndex: "owner", width: 100 }, { title: "表名", dataIndex: "table_name", width: 180, ellipsis: true }, { title: "索引/SQL", render: (_, r) => r.index_name || r.sql_id || "-", width: 180, ellipsis: true },
                { title: "证据", dataIndex: "evidence_summary", width: 360, ellipsis: true }, { title: "建议", dataIndex: "suggestion", width: 320, ellipsis: true },
              ]} /> },
            ]} />
          </Card>
        </Space>
  );
}
