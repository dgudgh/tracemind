import React from "react";
import { Alert, Button, Card, Checkbox, Col, Form, Input, InputNumber, Row, Select, Space, Table, Tag } from "antd";
import { CodeOutlined, FileSearchOutlined, SearchOutlined } from "@ant-design/icons";

export function DataOperationsCreateIndexPanel({
  activeTab,
  createForm,
  createColumns,
  createLoading,
  instanceId,
  loadCreateColumns,
  checkDuplicateIndex,
  generateCreateIndexSQL,
}) {
  if (activeTab !== "create") return null;
  return (

        <Card title="索引创建向导" size="small">
          <Alert type="info" showIcon style={{ marginBottom: 16 }} message="创建索引只生成 SQL，不直接执行。" description="请先加载字段，人工选择索引列，再检查重复索引和维护窗口。" />
          <Form form={createForm} layout="vertical" initialValues={{ index_type: "NORMAL", online: true, parallel: 1, compress: false }}>
            <Row gutter={16}>
              <Col xs={24} md={6}><Form.Item name="owner" label="Schema" rules={[{ required: true, message: "请输入 Schema" }]}><Input placeholder="MES" /></Form.Item></Col>
              <Col xs={24} md={6}><Form.Item name="table_name" label="表名" rules={[{ required: true, message: "请输入表名" }]}><Input placeholder="TT_WO_WIP" /></Form.Item></Col>
              <Col xs={24} md={6}><Form.Item name="index_name" label="索引名" rules={[{ required: true, message: "请输入索引名" }]}><Input placeholder="IDX_TT_WO_WIP_01" /></Form.Item></Col>
              <Col xs={24} md={6}><Form.Item name="index_type" label="索引类型"><Select options={["NORMAL", "UNIQUE", "BITMAP"].map((v) => ({ value: v, label: v }))} /></Form.Item></Col>
            </Row>
            <Row gutter={16}>
              <Col xs={24} md={10}><Form.Item name="columns" label="索引列（按选择顺序生成）" rules={[{ required: true, message: "请选择索引列" }]}><Select mode="multiple" placeholder="先加载字段" options={createColumns.map((c) => ({ value: c.column_name, label: `${c.column_name} (${c.data_type})` }))} /></Form.Item></Col>
              <Col xs={24} md={5}><Form.Item name="tablespace_name" label="表空间"><Input placeholder="可选，例如 IDX_TS" /></Form.Item></Col>
              <Col xs={12} md={3}><Form.Item name="online" label="ONLINE" valuePropName="checked"><Checkbox>启用</Checkbox></Form.Item></Col>
              <Col xs={12} md={3}><Form.Item name="compress" label="COMPRESS" valuePropName="checked"><Checkbox>启用</Checkbox></Form.Item></Col>
              <Col xs={24} md={3}><Form.Item name="parallel" label="并行度"><InputNumber min={1} max={16} className="full-width" /></Form.Item></Col>
            </Row>
            <Space wrap>
              <Button icon={<SearchOutlined />} loading={createLoading} disabled={!instanceId} onClick={loadCreateColumns}>加载字段</Button>
              <Button icon={<FileSearchOutlined />} loading={createLoading} disabled={!instanceId || createColumns.length === 0} onClick={checkDuplicateIndex}>检查重复索引</Button>
              <Button type="primary" icon={<CodeOutlined />} loading={createLoading} disabled={!instanceId || createColumns.length === 0} onClick={generateCreateIndexSQL}>生成 CREATE INDEX SQL</Button>
            </Space>
          </Form>
          {createColumns.length > 0 && <Table style={{ marginTop: 16 }} dataSource={createColumns} rowKey="column_id" size="small" pagination={{ defaultPageSize: 10 }}
            columns={[
              { title: "#", dataIndex: "column_id", width: 60 },
              { title: "列名", dataIndex: "column_name", width: 180 },
              { title: "类型", dataIndex: "data_type", width: 120 },
              { title: "可空", dataIndex: "nullable", width: 80, render: (v) => v === "Y" ? <Tag color="green">是</Tag> : <Tag color="red">否</Tag> },
              { title: "不同值数", dataIndex: "num_distinct", width: 120, render: (v) => v != null ? Number(v).toLocaleString() : "-" },
              { title: "密度", dataIndex: "density", width: 120, render: (v) => v ?? "-" },
              { title: "直方图", dataIndex: "histogram", width: 140, render: (v) => v || "-" },
            ]} />}
        </Card>
  );
}
