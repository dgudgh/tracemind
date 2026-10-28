import React from "react";
import { Button, Card, Checkbox, Input, InputNumber, Segmented, Select, Space } from "antd";
import { ReloadOutlined } from "@ant-design/icons";

const DATA_OPERATION_TABS = [
  { value: "table", label: "表碎片" },
  { value: "index", label: "索引碎片" },
  { value: "unused", label: "未使用索引" },
  { value: "invalid", label: "失效索引" },
  { value: "create", label: "索引创建向导" },
  { value: "stats", label: "统计信息管理" },
  { value: "partition", label: "分区表治理" },
  { value: "usage", label: "表访问画像" },
  { value: "batch", label: "批量操作" },
];

export function DataOperationsToolbar({
  activeTab,
  setActiveTab,
  schemas,
  selectedOwner,
  onOwnerChange,
  tableFilter,
  setTableFilter,
  staleOnly,
  setStaleOnly,
  partitionMinRows,
  setPartitionMinRows,
  usageSource,
  setUsageSource,
  usageDays,
  setUsageDays,
  minSizeMb,
  setMinSizeMb,
  queryLoading,
  disabled,
  doQuery,
}) {
  return (
    <Card>
      <Space wrap>
        {schemas.length > 0 && (
          <Select
            style={{ width: 200 }}
            placeholder="选择 Schema"
            allowClear
            value={selectedOwner}
            options={schemas.map((schema) => ({ value: schema, label: schema }))}
            onChange={onOwnerChange}
          />
        )}
        {(activeTab === "invalid" || activeTab === "stats" || activeTab === "partition" || activeTab === "usage") && (
          <Input
            style={{ width: 180 }}
            placeholder="表名过滤（可选）"
            value={tableFilter}
            onChange={(event) => setTableFilter(event.target.value)}
          />
        )}
        {activeTab === "stats" && (
          <Checkbox checked={staleOnly} onChange={(event) => setStaleOnly(event.target.checked)}>仅过期</Checkbox>
        )}
        {activeTab === "partition" && (
          <InputNumber
            min={1}
            style={{ width: 170 }}
            value={partitionMinRows}
            onChange={(value) => setPartitionMinRows(value || 5000000)}
            addonAfter="行"
          />
        )}
        {activeTab === "usage" && (
          <Select
            style={{ width: 120 }}
            value={usageSource}
            onChange={setUsageSource}
            options={[{ value: "AWR", label: "AWR" }, { value: "CURRENT", label: "当前缓存" }]}
          />
        )}
        {activeTab === "usage" && (
          <InputNumber
            min={1}
            max={365}
            style={{ width: 130 }}
            value={usageDays}
            onChange={(value) => setUsageDays(value || 30)}
            addonAfter="天"
          />
        )}
        {activeTab !== "create" && activeTab !== "stats" && activeTab !== "usage" && (
          <InputNumber
            min={0}
            step={1}
            style={{ width: 140 }}
            placeholder="最小大小MB"
            value={minSizeMb}
            onChange={(value) => setMinSizeMb(value)}
            addonAfter="MB"
          />
        )}
        <Segmented value={activeTab} onChange={(value) => setActiveTab(value)} options={DATA_OPERATION_TABS} />
        <Button type="primary" icon={<ReloadOutlined />} loading={queryLoading} disabled={disabled} onClick={doQuery}>
          {activeTab === "create" ? "加载字段" : "查询"}
        </Button>
      </Space>
    </Card>
  );
}
