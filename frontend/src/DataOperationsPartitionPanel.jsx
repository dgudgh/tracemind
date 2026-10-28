import React from "react";
import { Alert, Button, Card, Col, Form, Input, InputNumber, Progress, Row, Select, Space, Statistic, Table, Tag } from "antd";
import { CodeOutlined, SearchOutlined } from "@ant-design/icons";
import { partitionIntervalDefault } from "./modules/dataOperationsHelpers";

export function DataOperationsPartitionPanel({
  activeTab,
  partitionLoading,
  instanceId,
  loadPartitionCandidates,
  partitionMinRows,
  partitionData,
  loadPartitionProfile,
  loadPartitionKeys,
  partitionKeys,
  partitionForm,
  partitionProfile,
  generatePartitionPlan,
}) {
  if (activeTab !== "partition") return null;
  return (

        <Space direction="vertical" size={16} className="full-width">
          <Card title="分区表治理" size="small">
            <Alert type="warning" showIcon style={{ marginBottom: 16 }} message="分区表改造是高风险 DDL，当前模块只生成方案和脚本，不直接执行。" description="系统会先查看数据库版本，再只推荐一种改造方式：Oracle 12.2 及以上推荐 ALTER ONLINE，低版本或版本未知推荐 DBMS_REDEFINITION。" />
            <Space wrap>
              <Button type="primary" icon={<SearchOutlined />} loading={partitionLoading} disabled={!instanceId} onClick={loadPartitionCandidates}>扫描非分区大表</Button>
              <Tag color="blue">默认阈值 {Number(partitionMinRows || 0).toLocaleString()} 行</Tag>
              {partitionData?.database_version && <Tag color="geekblue">数据库版本 {partitionData.database_version.version || "未知"}</Tag>}
              {partitionData?.recommendation && <Tag color="purple">推荐方式 {partitionData.recommendation.label}</Tag>}
            </Space>
          </Card>
          {partitionData && <Card title={`非分区大表候选 (${partitionData.total || 0})`} size="small">
            <Table dataSource={partitionData.items || []} rowKey={(r) => `${r.owner}.${r.table_name}`} size="small" pagination={{ defaultPageSize: 20, showSizeChanger: true }} scroll={{ x: 1400 }}
              columns={[
                { title: "Owner", dataIndex: "owner", width: 120 },
                { title: "表名", dataIndex: "table_name", width: 220, ellipsis: true },
                { title: "统计行数", dataIndex: "num_rows", width: 120, render: (v) => v != null ? Number(v).toLocaleString() : "-" },
                { title: "大小(MB)", dataIndex: "size_mb", width: 110, render: (v, r) => Number(v || r.approx_size_mb || 0).toFixed(2) },
                { title: "表空间", dataIndex: "tablespace_name", width: 130 },
                { title: "分区", dataIndex: "partitioned", width: 80, render: (v) => <Tag color={v === "NO" ? "orange" : "blue"}>{v}</Tag> },
                { title: "推荐方式", dataIndex: "recommended_method", width: 150, render: (v) => <Tag color={v === "ALTER_ONLINE" ? "purple" : "geekblue"}>{v || "-"}</Tag> },
                { title: "最后统计", dataIndex: "last_analyzed", width: 170, render: (v) => v || "-" },
                { title: "级别", dataIndex: "candidate_level", width: 90, render: (v) => <Tag color={v === "high" ? "red" : "gold"}>{v}</Tag> },
                { title: "建议", dataIndex: "suggestion", width: 260, ellipsis: true },
                { title: "操作", width: 260, fixed: "right", render: (_, r) => <Space size={4} wrap>
                  <Button size="small" onClick={() => loadPartitionProfile(r, false)}>画像</Button>
                  <Button size="small" onClick={() => loadPartitionProfile(r, true)}>精确统计</Button>
                  <Button size="small" type="primary" ghost onClick={() => loadPartitionKeys(r)}>推荐分区键</Button>
                </Space> },
              ]} />
          </Card>}
          {partitionKeys && <Card title={`${partitionKeys.owner}.${partitionKeys.table_name} 分区键推荐`} size="small">
            <Table dataSource={partitionKeys.items || []} rowKey="column_name" size="small" pagination={false}
              columns={[
                { title: "字段", dataIndex: "column_name", width: 180 },
                { title: "类型", dataIndex: "data_type", width: 160 },
                { title: "不同值", dataIndex: "num_distinct", width: 100, render: (v) => v != null ? Number(v).toLocaleString() : "-" },
                { title: "空值", dataIndex: "num_nulls", width: 100, render: (v) => v != null ? Number(v).toLocaleString() : "-" },
                { title: "评分", dataIndex: "score", width: 100, render: (v) => <Progress percent={v} size="small" style={{ width: 80 }} /> },
                { title: "理由", dataIndex: "reason", ellipsis: true },
                { title: "操作", width: 120, render: (_, r) => <Button size="small" onClick={() => partitionForm.setFieldsValue({ partition_column: r.column_name })}>选为分区键</Button> },
              ]} />
          </Card>}
          {partitionProfile && <Card title={`${partitionProfile.table.owner}.${partitionProfile.table.table_name} 风险检查`} size="small">
            <Row gutter={[16, 16]}>
              <Col xs={12} md={6}><Statistic title="统计行数" value={partitionProfile.table.num_rows || 0} /></Col>
              <Col xs={12} md={6}><Statistic title="精确行数" value={partitionProfile.exact_count ?? "-"} /></Col>
              <Col xs={12} md={6}><Statistic title="索引数" value={partitionProfile.indexes?.length || 0} suffix="个" /></Col>
              <Col xs={12} md={6}><Statistic title="触发器" value={partitionProfile.triggers?.length || 0} suffix="个" /></Col>
              <Col xs={12} md={6}><Statistic title="数据库版本" value={partitionProfile.database_version?.version || "未知"} /></Col>
              <Col xs={12} md={6}><Statistic title="推荐方式" value={partitionProfile.recommendation?.method || "-"} /></Col>
            </Row>
            {partitionProfile.risks?.length > 0 && <Alert type="warning" showIcon style={{ marginTop: 12 }} message="改造风险" description={<ul style={{ margin: 0, paddingLeft: 20 }}>{partitionProfile.risks.map((item) => <li key={item}>{item}</li>)}</ul>} />}
          </Card>}
          <Card title="生成分区改造方案" size="small">
            <Form form={partitionForm} layout="vertical" initialValues={{ partition_type: "RANGE_INTERVAL", interval_unit: "MONTH", interval_value: 1, start_less_than: "2025-01-01", method: "DBMS_REDEFINITION", parallel: 4 }}>
              <Row gutter={16}>
                <Col xs={24} md={5}><Form.Item name="owner" label="Schema" rules={[{ required: true }]}><Input placeholder="MES" /></Form.Item></Col>
                <Col xs={24} md={5}><Form.Item name="table_name" label="表名" rules={[{ required: true }]}><Input placeholder="TT_WO_WIP" /></Form.Item></Col>
                <Col xs={24} md={5}><Form.Item name="partition_column" label="分区字段" rules={[{ required: true }]}><Input placeholder="CREATE_DATE" /></Form.Item></Col>
                <Col xs={24} md={4}><Form.Item name="partition_type" label="分区类型"><Select options={["RANGE_INTERVAL", "RANGE", "HASH", "LIST"].map((v) => ({ value: v, label: v }))} /></Form.Item></Col>
                <Col xs={24} md={5}><Form.Item name="method" label="推荐方式"><Input disabled placeholder="先扫描或查看画像" /></Form.Item></Col>
              </Row>
              <Row gutter={16}>
                <Col xs={24} md={4}><Form.Item name="interval_unit" label="间隔单位"><Select onChange={(unit) => partitionForm.setFieldsValue({ interval_value: partitionIntervalDefault(unit) })} options={[{ value: "MONTH", label: "月" }, { value: "DAY", label: "日" }, { value: "YEAR", label: "年" }]} /></Form.Item></Col>
                <Col xs={24} md={4}><Form.Item name="interval_value" label="间隔数值"><InputNumber min={1} max={999} className="full-width" /></Form.Item></Col>
                <Col xs={24} md={5}><Form.Item name="start_less_than" label="初始小于"><Input placeholder="2025-01-01" /></Form.Item></Col>
                <Col xs={24} md={4}><Form.Item name="parallel" label="并行度"><InputNumber min={1} max={64} className="full-width" /></Form.Item></Col>
                <Col xs={24} md={5}><Form.Item name="tablespace_name" label="表空间"><Input placeholder="可选" /></Form.Item></Col>
                <Col xs={24} md={5}><Form.Item name="index_tablespace_name" label="索引表空间"><Input placeholder="可选" /></Form.Item></Col>
              </Row>
              <Space wrap>
                <Button type="primary" icon={<CodeOutlined />} loading={partitionLoading} disabled={!instanceId} onClick={() => generatePartitionPlan()}>按数据库版本生成推荐方案</Button>
              </Space>
            </Form>
          </Card>
        </Space>
  );
}
