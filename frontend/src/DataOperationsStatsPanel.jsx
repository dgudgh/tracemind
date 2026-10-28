import React from "react";
import { Button, Card, Checkbox, Col, Form, Input, InputNumber, Row, Select, Space, Table, Tabs, Tag } from "antd";
import { ClockCircleOutlined, CodeOutlined, DeleteOutlined, LockOutlined, ReloadOutlined } from "@ant-design/icons";
import { stableRecordKey } from "./modules/stableKeys";

export function DataOperationsStatsPanel({
  activeTab,
  statsLoading,
  instanceId,
  loadStatsTables,
  loadStatsOperations,
  statsData,
  loadStatsDetail,
  statsForm,
  generateStatsPlan,
  statsDetail,
  statsOperations,
}) {
  if (activeTab !== "stats") return null;
  return (

        <Space direction="vertical" size={16} className="full-width">
          <Card title="统计信息管理" size="small">
            <Space wrap>
              <Button type="primary" icon={<ReloadOutlined />} loading={statsLoading} disabled={!instanceId} onClick={loadStatsTables}>查询表统计信息</Button>
              <Button icon={<ClockCircleOutlined />} loading={statsLoading} disabled={!instanceId} onClick={loadStatsOperations}>DBMS_STATS 操作历史</Button>
            </Space>
          </Card>
          {statsData && <Card title={`表统计信息列表 (${statsData.items?.length || 0})`} size="small">
            <Table dataSource={statsData.items || []} rowKey={(r) => `${r.owner}.${r.table_name}`} size="small" pagination={{ defaultPageSize: 20, showSizeChanger: true }} scroll={{ x: 1400 }}
              columns={[
                { title: "Owner", dataIndex: "owner", width: 120 },
                { title: "表名", dataIndex: "table_name", width: 220, ellipsis: true },
                { title: "行数", dataIndex: "num_rows", width: 110, render: (v) => v != null ? Number(v).toLocaleString() : "-" },
                { title: "块数", dataIndex: "blocks", width: 90, render: (v) => v ?? "-" },
                { title: "采样", dataIndex: "sample_size", width: 100, render: (v) => v != null ? Number(v).toLocaleString() : "-" },
                { title: "最后分析", dataIndex: "last_analyzed", width: 170, render: (v) => v || "-" },
                { title: "过期", dataIndex: "stale_stats", width: 80, render: (v) => <Tag color={v === "YES" ? "red" : "green"}>{v || "-"}</Tag> },
                { title: "锁定", dataIndex: "stattype_locked", width: 100, render: (v) => v ? <Tag color="orange">{v}</Tag> : "-" },
                { title: "分区", dataIndex: "partitioned", width: 80, render: (v) => <Tag color={v === "YES" ? "blue" : "default"}>{v || "-"}</Tag> },
                { title: "操作", width: 160, fixed: "right", render: (_, r) => <Space size={4}><Button size="small" onClick={() => loadStatsDetail(r)}>详情</Button><Button size="small" type="primary" ghost onClick={() => { statsForm.setFieldsValue({ owner: r.owner, table_name: r.table_name, degree: 4, cascade: true, estimate_percent: "AUTO", method_opt: "FOR ALL COLUMNS SIZE AUTO", granularity: "AUTO", no_invalidate: "AUTO" }); generateStatsPlan("table"); }}>收集 SQL</Button></Space> },
              ]} />
          </Card>}
          <Card title="生成统计信息脚本" size="small">
            <Form form={statsForm} layout="vertical" initialValues={{ degree: 4, cascade: true, options: "GATHER STALE", estimate_percent: "AUTO", method_opt: "FOR ALL COLUMNS SIZE AUTO", granularity: "AUTO", no_invalidate: "AUTO" }}>
              <Row gutter={16}>
                <Col xs={24} md={5}><Form.Item name="owner" label="Schema" rules={[{ required: true }]}><Input placeholder="MES" /></Form.Item></Col>
                <Col xs={24} md={5}><Form.Item name="table_name" label="表名"><Input placeholder="表级操作填写" /></Form.Item></Col>
                <Col xs={24} md={4}><Form.Item name="partition_name" label="分区名"><Input placeholder="可选" /></Form.Item></Col>
                <Col xs={24} md={4}><Form.Item name="degree" label="并行度"><InputNumber min={1} max={32} className="full-width" /></Form.Item></Col>
                <Col xs={24} md={6}><Form.Item name="options" label="Schema 收集选项"><Select options={["GATHER STALE", "GATHER", "GATHER EMPTY"].map((v) => ({ value: v, label: v }))} /></Form.Item></Col>
              </Row>
              <Row gutter={16}>
                <Col xs={24} md={4}><Form.Item name="estimate_percent" label="采样比例"><Input placeholder="AUTO 或 1-100" /></Form.Item></Col>
                <Col xs={24} md={8}><Form.Item name="method_opt" label="Method Opt"><Input /></Form.Item></Col>
                <Col xs={24} md={4}><Form.Item name="granularity" label="Granularity"><Select options={["AUTO", "PARTITION", "GLOBAL", "ALL"].map((v) => ({ value: v, label: v }))} /></Form.Item></Col>
                <Col xs={24} md={4}><Form.Item name="no_invalidate" label="游标失效"><Select options={["AUTO", "TRUE", "FALSE"].map((v) => ({ value: v, label: v }))} /></Form.Item></Col>
                <Col xs={12} md={3}><Form.Item name="cascade" label="Cascade" valuePropName="checked"><Checkbox>启用</Checkbox></Form.Item></Col>
              </Row>
              <Space wrap>
                <Button type="primary" icon={<CodeOutlined />} loading={statsLoading} disabled={!instanceId} onClick={() => generateStatsPlan("table")}>生成表收集 SQL</Button>
                <Button icon={<CodeOutlined />} loading={statsLoading} disabled={!instanceId} onClick={() => generateStatsPlan("schema")}>生成 Schema 收集 SQL</Button>
                <Button icon={<LockOutlined />} loading={statsLoading} disabled={!instanceId} onClick={() => generateStatsPlan("lock")}>锁定统计信息</Button>
                <Button icon={<LockOutlined />} loading={statsLoading} disabled={!instanceId} onClick={() => generateStatsPlan("unlock")}>解锁统计信息</Button>
                <Button danger icon={<DeleteOutlined />} loading={statsLoading} disabled={!instanceId} onClick={() => generateStatsPlan("delete")}>删除统计信息 SQL</Button>
              </Space>
            </Form>
          </Card>
          {statsDetail && <Card title={`${statsDetail.record.owner}.${statsDetail.record.table_name} 统计详情`} size="small">
            <Tabs items={[
              { key: "columns", label: `列统计 (${statsDetail.columns.length})`, children: <Table dataSource={statsDetail.columns} rowKey="column_name" size="small" columns={[{ title: "列名", dataIndex: "column_name" }, { title: "类型", dataIndex: "data_type" }, { title: "不同值", dataIndex: "num_distinct" }, { title: "空值", dataIndex: "num_nulls" }, { title: "直方图", dataIndex: "histogram" }, { title: "最后分析", dataIndex: "last_analyzed" }]} /> },
              { key: "indexes", label: `索引统计 (${statsDetail.indexes.length})`, children: <Table dataSource={statsDetail.indexes} rowKey="index_name" size="small" columns={[{ title: "索引名", dataIndex: "index_name" }, { title: "BLEVEL", dataIndex: "blevel" }, { title: "叶块", dataIndex: "leaf_blocks" }, { title: "不同键", dataIndex: "distinct_keys" }, { title: "聚簇因子", dataIndex: "clustering_factor" }, { title: "状态", dataIndex: "status" }, { title: "最后分析", dataIndex: "last_analyzed" }]} /> },
              { key: "history", label: `历史 (${statsDetail.history.length})`, children: <Table dataSource={statsDetail.history} rowKey={(row) => stableRecordKey("oracle-stats-history", row, ["partition_name", "subpartition_name", "stats_update_time"])} size="small" columns={[{ title: "分区", dataIndex: "partition_name", render: (v) => v || "-" }, { title: "子分区", dataIndex: "subpartition_name", render: (v) => v || "-" }, { title: "更新时间", dataIndex: "stats_update_time" }]} /> },
            ]} />
          </Card>}
          {statsOperations && <Card title="DBMS_STATS 操作历史" size="small">
            <Table dataSource={statsOperations} rowKey={(row) => stableRecordKey("oracle-stats-operation", row, ["operation", "target", "start_time", "end_time", "status"])} size="small" pagination={{ defaultPageSize: 10 }}
              columns={[{ title: "操作", dataIndex: "operation" }, { title: "目标", dataIndex: "target", ellipsis: true }, { title: "开始", dataIndex: "start_time" }, { title: "结束", dataIndex: "end_time" }, { title: "状态", dataIndex: "status", render: (v) => <Tag color={v === "COMPLETED" ? "green" : "orange"}>{v}</Tag> }]} />
          </Card>}
        </Space>
  );
}
