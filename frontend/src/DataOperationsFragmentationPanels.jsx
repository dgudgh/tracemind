import React from "react";
import { Alert, Button, Card, Col, Progress, Row, Space, Statistic, Table, Tag, message } from "antd";
import { getDropIndexScript, getIndexRebuildScript } from "./modules/dataOperationsApi";
import { stableRecordKey } from "./modules/stableKeys";

export function DataOperationsFragmentationPanels({
  activeTab,
  fragData,
  indexFragData,
  unusedData,
  unusableData,
  instanceId,
  setScriptModal,
  fetchShrinkScript,
  buildRebuildPlan,
  wasteColor,
}) {
  return (
    <>
      {fragData && activeTab === "table" && (
            <>
              <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
                <Col xs={12} md={6}><Card size="small"><Statistic title="碎片表数量" value={fragData.summary.total_tables} suffix="个" /></Card></Col>
                <Col xs={12} md={6}><Card size="small"><Statistic title="总分配(MB)" value={fragData.summary.total_allocated_mb} precision={2} /></Card></Col>
                <Col xs={12} md={6}><Card size="small"><Statistic title="总浪费(MB)" value={fragData.summary.total_waste_mb} precision={2} valueStyle={{ color: fragData.summary.total_waste_mb > 1000 ? "#cf1322" : "#fa8c16" }} /></Card></Col>
                <Col xs={12} md={6}><Card size="small"><Statistic title="浪费率" value={fragData.summary.waste_ratio_pct} suffix="%" valueStyle={{ color: wasteColor(fragData.summary.waste_ratio_pct) }} /></Card></Col>
              </Row>
              {fragData.summary.total_waste_mb > 1000 && (
                <Alert type="warning" showIcon message="碎片空间过大"
                  description={`当前碎片浪费空间共 ${fragData.summary.total_waste_mb.toFixed(2)} MB，建议对浪费率高的表执行 SHRINK SPACE 整理碎片。`} />
              )}
              <Card title="表碎片明细" size="small" style={{ marginTop: 16 }}>
                <Table dataSource={fragData.tables} rowKey={(r) => `${r.owner}.${r.table_name}`} size="small"
                  pagination={{ defaultPageSize: 20, showSizeChanger: true }}
                  scroll={{ x: 1200 }}
                  columns={[
                    { title: "Owner", dataIndex: "owner", width: 120 },
                    { title: "表名", dataIndex: "table_name", width: 200 },
                    { title: "分配(MB)", dataIndex: "allocated_mb", width: 100, render: (v) => parseFloat(v || 0).toFixed(2) },
                    { title: "已用(MB)", dataIndex: "used_mb", width: 100, render: (v) => parseFloat(v || 0).toFixed(2) },
                    { title: "保留(MB)", dataIndex: "reserved_mb", width: 100, render: (v) => parseFloat(v || 0).toFixed(2) },
                    { title: "浪费(MB)", dataIndex: "waste_mb", width: 100, render: (v) => <span style={{ color: parseFloat(v || 0) > 100 ? "#cf1322" : "inherit" }}>{parseFloat(v || 0).toFixed(2)}</span>, sorter: (a, b) => parseFloat(a.waste_mb || 0) - parseFloat(b.waste_mb || 0), defaultSortOrder: "descend" },
                    { title: "浪费率", dataIndex: "waste_pct", width: 140, render: (v) => <Space><Progress percent={parseFloat(v || 0)} size="small" strokeColor={wasteColor(v)} style={{ width: 60 }} /><span style={{ color: wasteColor(v), fontWeight: 600 }}>{parseFloat(v || 0).toFixed(1)}%</span></Space> },
                    { title: "行数", dataIndex: "num_rows", width: 100, render: (v) => v != null ? Number(v).toLocaleString() : "-" },
                    { title: "最后分析", dataIndex: "last_analyzed", width: 160, render: (v) => v || "-" },
                    { title: "操作", width: 90, render: (_, r) => <Button size="small" type="primary" ghost onClick={() => fetchShrinkScript(r.owner, r.table_name)}>整理</Button> },
                  ]}
                />
              </Card>
            </>
      )}

      {indexFragData && activeTab === "index" && (
        <>
          <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
            <Col xs={12} md={6}><Card size="small"><Statistic title="索引数量" value={indexFragData.summary.total_indexes} suffix="个" /></Card></Col>
            <Col xs={12} md={6}><Card size="small"><Statistic title="分析表数" value={indexFragData.summary.total_tables_analyzed} suffix="个" /></Card></Col>
            <Col xs={12} md={6}><Card size="small"><Statistic title="索引实际(MB)" value={indexFragData.summary.total_actual_mb} precision={2} /></Card></Col>
            <Col xs={12} md={6}><Card size="small"><Statistic title="浪费(MB)" value={indexFragData.summary.total_waste_mb} precision={2} valueStyle={{ color: indexFragData.summary.total_waste_mb > 100 ? "#cf1322" : "#fa8c16" }} /></Card></Col>
          </Row>
          <Card title="索引碎片明细" size="small">
            <Table dataSource={indexFragData.indexes} rowKey={(r) => `${r.owner}.${r.table_name}.${r.index_name}`} size="small"
              pagination={{ defaultPageSize: 20, showSizeChanger: true }}
              scroll={{ x: 1000 }}
              columns={[
                { title: "表名", dataIndex: "table_name", width: 200 },
                { title: "索引名", dataIndex: "index_name", width: 220 },
                { title: "应有大小(MB)", dataIndex: "expected_mb", width: 120, render: (v) => parseFloat(v || 0).toFixed(2) },
                { title: "实际大小(MB)", dataIndex: "actual_mb", width: 120, render: (v) => parseFloat(v || 0).toFixed(2) },
                { title: "浪费(MB)", dataIndex: "waste_mb", width: 100, render: (v) => <span style={{ color: parseFloat(v || 0) > 50 ? "#cf1322" : "inherit" }}>{parseFloat(v || 0).toFixed(2)}</span>, sorter: (a, b) => parseFloat(a.waste_mb || 0) - parseFloat(b.waste_mb || 0), defaultSortOrder: "descend" },
                { title: "膨胀率(%)", dataIndex: "frag_pct", width: 120, render: (v) => <span style={{ color: parseFloat(v || 0) > 200 ? "#cf1322" : parseFloat(v || 0) > 150 ? "#fa8c16" : "inherit" }}>{parseFloat(v || 0).toFixed(1)}%</span> },
                { title: "操作", width: 90, render: (_, r) => <Button size="small" type="primary" ghost onClick={async () => {
                  try {
                    const data = await getIndexRebuildScript(instanceId, { owner: r.owner, indexName: r.index_name });
                    setScriptModal({ open: true, title: `重建索引 - ${r.index_name}`, content: data.sql, warnings: data.warnings });
                  } catch (e) { message.error(e.message); }
                }}>重建</Button> },
              ]}
            />
          </Card>
        </>
      )}

      {unusedData && activeTab === "unused" && (
        <>
          <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
            <Col xs={12} md={6}><Card size="small"><Statistic title="未使用索引" value={unusedData.summary.total_unused} suffix="个" valueStyle={{ color: unusedData.summary.total_unused > 0 ? "#fa8c16" : "#52c41a" }} /></Card></Col>
            <Col xs={12} md={6}><Card size="small"><Statistic title="占用空间(MB)" value={unusedData.summary.unused_size_mb} precision={2} valueStyle={{ color: unusedData.summary.unused_size_mb > 100 ? "#cf1322" : "#fa8c16" }} /></Card></Col>
            <Col xs={12} md={6}><Card size="small"><Statistic title="Schema总索引(MB)" value={unusedData.summary.total_index_mb} precision={2} /></Card></Col>
            <Col xs={12} md={6}><Card size="small"><Statistic title="分析天数" value={unusedData.summary.days} suffix="天" /></Card></Col>
          </Row>
          {unusedData.summary.total_unused > 0 && (
            <Alert type="warning" showIcon style={{ marginBottom: 16 }}
              message={`发现 ${unusedData.summary.total_unused} 个未使用索引`}
              description={`过去 ${unusedData.summary.days} 天内未被任何 SQL 执行计划使用的索引（已排除约束索引和 LOB/DOMAIN 类型）。建议先设置 INVISIBLE 观察，确认无影响后再删除。`} />
          )}
          {unusedData.summary.total_unused === 0 && (
            <Alert type="success" showIcon style={{ marginBottom: 16 }}
              message="未发现未使用索引"
              description={`过去 ${unusedData.summary.days} 天内所有非约束索引均被使用过。`} />
          )}
          <Card title={`未使用索引明细(${unusedData.summary.owner})`} size="small">
            <Table dataSource={unusedData.indexes} rowKey={(r) => `${r.owner}.${r.index_name}`} size="small"
              pagination={{ defaultPageSize: 20, showSizeChanger: true }}
              scroll={{ x: 1400 }}
              columns={[
                { title: "表名", dataIndex: "table_name", width: 180, ellipsis: true },
                { title: "索引名", dataIndex: "index_name", width: 200, ellipsis: true },
                { title: "类型", dataIndex: "index_type", width: 100 },
                { title: "唯一性", dataIndex: "uniqueness", width: 80, render: (v) => <Tag color={v === "UNIQUE" ? "blue" : "default"}>{v}</Tag> },
                { title: "状态", dataIndex: "status", width: 70, render: (v) => <Tag color={v === "VALID" ? "green" : "red"}>{v}</Tag> },
                { title: "可见性", dataIndex: "visibility", width: 80, render: (v) => <Tag color={v === "VISIBLE" ? "green" : "gold"}>{v}</Tag> },
                { title: "行数", dataIndex: "num_rows", width: 100, render: (v) => v != null ? Number(v).toLocaleString() : "-" },
                { title: "叶块数", dataIndex: "leaf_blocks", width: 90, sorter: (a, b) => (a.leaf_blocks || 0) - (b.leaf_blocks || 0), defaultSortOrder: "descend" },
                { title: "不同键数", dataIndex: "distinct_keys", width: 100, render: (v) => v != null ? Number(v).toLocaleString() : "-" },
                { title: "聚合因子", dataIndex: "clustering_factor", width: 100, render: (v) => v != null ? Number(v).toLocaleString() : "-" },
                { title: "最后分析", dataIndex: "last_analyzed", width: 150, render: (v) => v || "-" },
                { title: "操作", width: 90, render: (_, r) => <Button size="small" type="primary" ghost onClick={async () => {
                  try {
                    const data = await getDropIndexScript(instanceId, { owner: r.owner, indexName: r.index_name });
                    setScriptModal({ open: true, title: `删除索引 - ${r.index_name}`, content: data.sql, warnings: data.warnings });
                  } catch (e) { message.error(e.message); }
                }}>生成脚本</Button> },
              ]}
            />
          </Card>
        </>
      )}

      {unusableData && activeTab === "invalid" && (
        <>
          <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
            <Col xs={12} md={6}><Card size="small"><Statistic title="失效索引" value={unusableData.normal?.total || 0} suffix="个" /></Card></Col>
            <Col xs={12} md={6}><Card size="small"><Statistic title="分区/子分区失效" value={unusableData.partitions?.total || 0} suffix="个" /></Card></Col>
            <Col xs={12} md={6}><Card size="small"><Statistic title="异常索引" value={unusableData.abnormal?.total || 0} suffix="个" /></Card></Col>
            <Col xs={12} md={6}><Card size="small"><Statistic title="合计" value={unusableData.total || 0} suffix="个" valueStyle={{ color: unusableData.total > 0 ? "#cf1322" : "#52c41a" }} /></Card></Col>
          </Row>
          <Card title="失效索引检查" size="small">
            <Table dataSource={unusableData.items || []} rowKey={(row) => stableRecordKey("oracle-unusable-index", row, ["item_type", "owner", "index_name", "partition_name", "subpartition_name", "status"])} size="small"
              pagination={{ defaultPageSize: 20, showSizeChanger: true }} scroll={{ x: 1500 }}
              columns={[
                { title: "类型", dataIndex: "item_type", width: 110, render: (v) => <Tag color={v === "NORMAL" ? "red" : v === "ABNORMAL" ? "purple" : "orange"}>{v === "NORMAL" ? "普通失效" : v === "ABNORMAL" ? "异常" : v === "SUBPARTITION" ? "子分区失效" : "分区失效"}</Tag> },
                { title: "Owner", dataIndex: "owner", width: 120 },
                { title: "表名", dataIndex: "table_name", width: 180, render: (v) => v || "-" },
                { title: "索引名", dataIndex: "index_name", width: 220, ellipsis: true },
                { title: "分区", dataIndex: "partition_name", width: 150, render: (v) => v || "-" },
                { title: "子分区", dataIndex: "subpartition_name", width: 160, render: (v) => v || "-" },
                { title: "状态", dataIndex: "status", width: 110, render: (v) => <Tag color={v === "VALID" ? "green" : "red"}>{v || "-"}</Tag> },
                { title: "表空间", dataIndex: "tablespace_name", width: 140, render: (v) => v || "-" },
                { title: "Domain", width: 180, render: (_, r) => [r.domidx_status, r.domidx_opstatus].filter(Boolean).join(" / ") || "-" },
                { title: "函数索引", dataIndex: "funcidx_status", width: 110, render: (v) => v || "-" },
                { title: "最后分析", dataIndex: "last_analyzed", width: 170, render: (v) => v || "-" },
                { title: "操作", width: 120, fixed: "right", render: (_, r) => <Button size="small" type="primary" ghost onClick={() => buildRebuildPlan(r)}>生成重建 SQL</Button> },
              ]} />
          </Card>
        </>
      )}
    </>
  );
}
