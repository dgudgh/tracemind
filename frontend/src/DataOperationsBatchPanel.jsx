import React from "react";
import { Button, Card, Col, Empty, Input, InputNumber, Row, Select, Space, Table, Tag } from "antd";
import { CodeOutlined, SearchOutlined } from "@ant-design/icons";

export function DataOperationsBatchPanel({
  activeTab,
  batchPattern,
  setBatchPattern,
  loadBatchTables,
  batchLoading,
  instanceId,
  selectedOwner,
  batchTables,
  batchSelectedKeys,
  setBatchSelectedKeys,
  batchForm,
  setBatchForm,
  generateBatchSQL,
}) {
  if (activeTab !== "batch") return null;
  return (
        <>
          <Card size="small">
            <Space wrap>
              <Input style={{ width: 220 }} placeholder="表名模糊匹配关键词" value={batchPattern} onChange={(e) => setBatchPattern(e.target.value)}
                onPressEnter={loadBatchTables} />
              <Button type="primary" icon={<SearchOutlined />} loading={batchLoading} disabled={!instanceId || !selectedOwner || !batchPattern.trim()} onClick={loadBatchTables}>搜索表</Button>
              {batchTables.length > 0 && <Tag color="blue">匹配 {batchTables.length} 张表，已选 {batchSelectedKeys.length} 张</Tag>}
            </Space>
          </Card>

          {batchTables.length > 0 && (
            <>
              <Card title={`匹配的表 (${batchTables.length})`} size="small" extra={
                <Space>
                  <a onClick={() => setBatchSelectedKeys(batchTables.map((t) => t.table_name))}>全选</a>
                  <a onClick={() => setBatchSelectedKeys([])}>清空</a>
                </Space>
              }>
                <Table dataSource={batchTables} rowKey="table_name" size="small"
                  pagination={{ defaultPageSize: 20 }}
                  rowSelection={{
                    selectedRowKeys: batchSelectedKeys,
                    onChange: (keys) => setBatchSelectedKeys(keys),
                  }}
                  scroll={{ x: 800 }}
                  expandable={{
                    expandedRowRender: (record) => record.columns && record.columns.length > 0 ? (
                      <Table dataSource={record.columns} rowKey="column_id" size="small" pagination={false}
                        columns={[
                          { title: "#", dataIndex: "column_id", width: 40 },
                          { title: "列名", dataIndex: "column_name", width: 150 },
                          { title: "类型", dataIndex: "data_type", width: 100 },
                          { title: "长度", dataIndex: "data_length", width: 60, render: (v) => v || "-" },
                          { title: "精度", dataIndex: "data_precision", width: 60, render: (v) => v ?? "-" },
                          { title: "小数", dataIndex: "data_scale", width: 60, render: (v) => v ?? "-" },
                          { title: "可空", dataIndex: "nullable", width: 50, render: (v) => v === "Y" ? <Tag color="green">是</Tag> : <Tag color="red">否</Tag> },
                          { title: "默认值", dataIndex: "data_default", ellipsis: true },
                        ]}
                      />
                    ) : <Empty description="无列信息" />,
                  }}
                  columns={[
                    { title: "表名", dataIndex: "table_name", width: 250, ellipsis: true },
                    { title: "行数", dataIndex: "num_rows", width: 100, render: (v) => v != null ? Number(v).toLocaleString() : "-" },
                    { title: "块数", dataIndex: "blocks", width: 80, render: (v) => v ?? "-" },
                    { title: "最后分析", dataIndex: "last_analyzed", width: 150, render: (v) => v || "-" },
                    { title: "列数", width: 60, render: (_, r) => r.columns ? r.columns.length : 0 },
                  ]}
                />
              </Card>

              <Card title="批量操作" size="small">
                <Space direction="vertical" size={12} className="full-width">
                  <Row gutter={16}>
                    <Col span={4}>
                      <div style={{ marginBottom: 4, fontWeight: 500 }}>操作类型</div>
                      <Select style={{ width: "100%" }} value={batchForm.action}
                        onChange={(v) => setBatchForm({ ...batchForm, action: v })}
                        options={[{ value: "ADD", label: "添加字段" }, { value: "MODIFY", label: "修改字段" }, { value: "DROP", label: "删除字段" }, { value: "INDEX", label: "创建索引" }]} />
                    </Col>
                    {batchForm.action === "INDEX" ? (
                      <>
                        <Col span={5}>
                          <div style={{ marginBottom: 4, fontWeight: 500 }}>索引列</div>
                          <Input placeholder="COL1,COL2" value={batchForm.idx_columns}
                            onChange={(e) => setBatchForm({ ...batchForm, idx_columns: e.target.value })} />
                        </Col>
                        <Col span={3}>
                          <div style={{ marginBottom: 4, fontWeight: 500 }}>索引后缀</div>
                          <Input placeholder="BATCH" value={batchForm.idx_suffix}
                            onChange={(e) => setBatchForm({ ...batchForm, idx_suffix: e.target.value })} />
                        </Col>
                        <Col span={3}>
                          <div style={{ marginBottom: 4, fontWeight: 500 }}>表空间</div>
                          <Input placeholder="可选" value={batchForm.idx_tablespace}
                            onChange={(e) => setBatchForm({ ...batchForm, idx_tablespace: e.target.value })} />
                        </Col>
                        <Col span={2}>
                          <div style={{ marginBottom: 4, fontWeight: 500 }}>唯一</div>
                          <Select style={{ width: "100%" }} value={batchForm.idx_unique}
                            onChange={(v) => setBatchForm({ ...batchForm, idx_unique: v })}
                            options={[{ value: false, label: "否" }, { value: true, label: "是" }]} />
                        </Col>
                        <Col span={2}>
                          <div style={{ marginBottom: 4, fontWeight: 500 }}>Online</div>
                          <Select style={{ width: "100%" }} value={batchForm.idx_online}
                            onChange={(v) => setBatchForm({ ...batchForm, idx_online: v })}
                            options={[{ value: false, label: "否" }, { value: true, label: "是" }]} />
                        </Col>
                        <Col span={2}>
                          <div style={{ marginBottom: 4, fontWeight: 500 }}>并行度</div>
                          <InputNumber style={{ width: "100%" }} min={0} placeholder="无" value={batchForm.idx_parallel || undefined}
                            onChange={(v) => setBatchForm({ ...batchForm, idx_parallel: v || "" })} />
                        </Col>
                      </>
                    ) : (
                      <>
                        <Col span={5}>
                          <div style={{ marginBottom: 4, fontWeight: 500 }}>字段名</div>
                          <Input placeholder="COLUMN_NAME" value={batchForm.column_name}
                            onChange={(e) => setBatchForm({ ...batchForm, column_name: e.target.value })} />
                        </Col>
                        <Col span={4}>
                          <div style={{ marginBottom: 4, fontWeight: 500 }}>数据类型</div>
                          <Select style={{ width: "100%" }} value={batchForm.data_type}
                            onChange={(v) => setBatchForm({ ...batchForm, data_type: v })}
                            options={["VARCHAR2", "NUMBER", "CHAR", "DATE", "TIMESTAMP", "CLOB", "BLOB", "FLOAT", "RAW", "NVARCHAR2", "NCHAR"].map((t) => ({ value: t, label: t }))} />
                        </Col>
                        {["VARCHAR2", "CHAR", "NVARCHAR2", "NCHAR", "RAW", "NUMBER", "FLOAT"].includes(batchForm.data_type) && (
                          <Col span={3}>
                            <div style={{ marginBottom: 4, fontWeight: 500 }}>
                              {["VARCHAR2", "CHAR", "NVARCHAR2", "NCHAR", "RAW"].includes(batchForm.data_type) ? "长度" : "精度"}
                            </div>
                            <InputNumber style={{ width: "100%" }} min={1} value={batchForm.data_length}
                              onChange={(v) => setBatchForm({ ...batchForm, data_length: v })} />
                          </Col>
                        )}
                        {batchForm.action !== "DROP" && (
                          <Col span={3}>
                            <div style={{ marginBottom: 4, fontWeight: 500 }}>允许为空</div>
                            <Select style={{ width: "100%" }} value={batchForm.nullable}
                              onChange={(v) => setBatchForm({ ...batchForm, nullable: v })}
                              options={[{ value: true, label: "是" }, { value: false, label: "否(NOT NULL)" }]} />
                          </Col>
                        )}
                        {batchForm.action === "ADD" && (
                          <Col span={4}>
                            <div style={{ marginBottom: 4, fontWeight: 500 }}>默认值</div>
                            <Input placeholder="可选" value={batchForm.default_value}
                              onChange={(e) => setBatchForm({ ...batchForm, default_value: e.target.value })} />
                          </Col>
                        )}
                      </>
                    )}
                  </Row>
                  <div>
                    <Button type="primary" icon={<CodeOutlined />}
                      disabled={batchSelectedKeys.length === 0 || (batchForm.action === "INDEX" ? !batchForm.idx_columns.trim() : !batchForm.column_name.trim())}
                      onClick={generateBatchSQL}>
                      生成 SQL（{batchSelectedKeys.length} 张表）
                    </Button>
                  </div>
                </Space>
              </Card>
            </>
          )}

          {batchTables.length === 0 && !batchLoading && activeTab === "batch" && (
            <Card><Empty description="输入表名关键词搜索，例如 ORDER_ 将匹配所有以 ORDER_ 开头的表" /></Card>
          )}
        </>
  );
}
