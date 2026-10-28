import React, { useState } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Collapse,
  Descriptions,
  Empty,
  Modal,
  Progress,
  Row,
  Space,
  Statistic,
  Table,
  Tabs,
  Tag,
  Typography,
} from "antd";
import { DownloadOutlined, FileTextOutlined, RobotOutlined } from "@ant-design/icons";
import { stableRecordKey, stableValueKey } from "./modules/stableKeys";
import { ItsmTicketDrawer } from "./framework/integration";
import { inspectionOutcome, evidenceSqlList, evidenceExecutionStatus, evidenceEmptyText } from "./InspectionExecutionState.js";

const { Paragraph, Text, Title } = Typography;

const PAGE_SIZE_OPTIONS = [10, 20, 50, 100];

const RISK_COLOR = {
  critical: "red",
  high: "red",
  warning: "orange",
  info: "blue",
  normal: "green",
};

const RISK_TEXT = {
  critical: "严重",
  high: "严重",
  warning: "警告",
  info: "提示",
  normal: "健康",
};

function normalizeRiskLevel(level) {
  const value = String(level || "normal").toLowerCase();
  if (["critical", "high", "disaster"].includes(value)) return "critical";
  if (["warning", "average"].includes(value)) return "warning";
  if (["info", "low", "notice"].includes(value)) return "info";
  return "normal";
}

function riskColor(level) {
  return RISK_COLOR[normalizeRiskLevel(level)] || "default";
}

function riskText(level) {
  return RISK_TEXT[normalizeRiskLevel(level)] || "-";
}

function alertType(level) {
  const risk = normalizeRiskLevel(level);
  if (risk === "critical") return "error";
  if (risk === "normal") return "success";
  if (risk === "info") return "info";
  return "warning";
}

function riskPenalty(item = {}) {
  const score = Number(item.risk_score ?? item.score);
  if (Number.isFinite(score) && score > 0) return score;
  return { critical: 15, warning: 5, info: 2, normal: 0 }[normalizeRiskLevel(item.severity || item.risk_level)] || 0;
}

function valueText(value) {
  if (value === null || value === undefined || value === "") return "-";
  if (typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

function normalizeRows(items) {
  if (!Array.isArray(items)) return [];
  return items.map((row, index) => {
    if (row && typeof row === "object" && !Array.isArray(row)) {
      return { ...row, __evidenceRowKey: `evidence-row-${index}` };
    }
    return { value: valueText(row), __evidenceRowKey: `evidence-row-${index}` };
  });
}

function dynamicColumns(rows, preferredColumns = []) {
  const keys = [];
  for (const key of preferredColumns || []) {
    const normalized = typeof key === "object" ? key.name || key.key || key.title : key;
    if (normalized && normalized !== "__evidenceRowKey" && !keys.includes(normalized)) keys.push(normalized);
  }
  for (const row of rows.slice(0, 20)) {
    if (!row || typeof row !== "object" || Array.isArray(row)) continue;
    for (const key of Object.keys(row)) {
      if (key !== "__evidenceRowKey" && !keys.includes(key)) keys.push(key);
    }
  }
  const finalKeys = keys.length ? keys : ["value"];
  return finalKeys.map((key) => ({
    title: key,
    dataIndex: key,
    width: 150,
    ellipsis: true,
    render: (value) => valueText(value),
  }));
}

function findingPayload(item) {
  if (item?.evidence && typeof item.evidence === "object") return item.evidence;
  if (item?.raw_data?.raw_data && typeof item.raw_data.raw_data === "object") return item.raw_data.raw_data;
  if (item?.raw_data && typeof item.raw_data === "object") return item.raw_data;
  return {};
}

function evidenceRowCount(payload, rows) {
  const count = payload.row_count == null || payload.row_count === "" ? NaN : Number(payload.row_count);
  return Number.isFinite(count) && count >= 0 ? count : rows.length || "-";
}

function evidenceStatusText(value, payload = {}) {
  const status = String(value || "").toLowerCase();
  if (status === "success" && evidenceSqlList(payload).length === 0) return "采集完成（无 SQL 记录）";
  if (["normal", "ok", "success"].includes(status)) return "执行成功";
  if (status === "skipped") return "已跳过";
  if (["critical", "error", "failed"].includes(status)) return "执行失败";
  if (status === "warning") return "有警告";
  if (status === "unknown") return "未确认";
  if (status === "summary") return "执行汇总（非查询）";
  if (status === "running") return "执行中";
  if (status === "partial") return "部分完成";
  return valueText(value);
}

function EvidenceBlock({ item }) {
  const payload = findingPayload(item);
  const rows = normalizeRows(Array.isArray(payload.rows) ? payload.rows : payload.items);
  const warnings = (Array.isArray(payload.warnings) ? payload.warnings : []).map(valueText);
  const sqlList = evidenceSqlList(payload);
  const columns = dynamicColumns(rows, payload.columns);
  const status = evidenceExecutionStatus(item, payload);
  const rowCount = evidenceRowCount(payload, rows);
  const displayedRowCount = rows.length;
  const hasRawPayload = payload && typeof payload === "object" && Object.keys(payload).length > 0;
  const emptyDescription = evidenceEmptyText(status, payload, item, sqlList.length > 0);

  return (
    <Space direction="vertical" size={12} className="full-width">
      <Descriptions bordered size="small" column={{ xs: 1, sm: 2, lg: 4 }}>
        <Descriptions.Item label="执行状态">{evidenceStatusText(status, payload)}</Descriptions.Item>
        <Descriptions.Item label="返回行数">{rowCount}</Descriptions.Item>
        <Descriptions.Item label="展示行数">{displayedRowCount}</Descriptions.Item>
        <Descriptions.Item label="结果字段">{rows.length || payload.columns?.length ? columns.length : "-"}</Descriptions.Item>
        <Descriptions.Item label="脚本行号">{payload.line_number || "-"}</Descriptions.Item>
        <Descriptions.Item label="结果截断">{payload.truncated ? `是，最多保留 ${payload.max_rows || rows.length} 行` : "否"}</Descriptions.Item>
        <Descriptions.Item label="检查项" span="filled">{item?.check_item || payload.title || "-"}</Descriptions.Item>
        <Descriptions.Item label="检查结论" span="filled">{item?.finding || "-"}</Descriptions.Item>
      </Descriptions>
      {payload.summary && (
        <Alert type={status === "failed" ? "error" : "info"} showIcon message={valueText(payload.summary)} />
      )}
      {payload.message && (
        <Alert
          type={["failed", "error", "critical", "skipped", "warning", "partial"].includes(String(status).toLowerCase()) ? "warning" : "info"}
          showIcon
          message={valueText(payload.message)}
        />
      )}
      {warnings.map((warning) => (
        <Alert key={stableValueKey("report-evidence-warning", warning)} type="warning" showIcon message={warning} />
      ))}
      {sqlList.length > 0 && (
        <div className="full-width">
          <Text strong>{String(payload.evidence_source || "").startsWith("legacy_") ? "参考 SQL（历史映射，未确认执行）" : "SQL / 采集命令"}</Text>
          <Space direction="vertical" size={8} className="full-width" style={{ marginTop: 8 }}>
            {sqlList.map((sql, index) => (
              <pre className="ops-output" key={stableValueKey(`report-sql-${index}`, sql)}>{sql}</pre>
            ))}
          </Space>
        </div>
      )}
      <Text strong>查询结果</Text>
      {rowCount > displayedRowCount && displayedRowCount > 0 && (
        <Alert
          type="info"
          showIcon
          message={`查询共返回 ${rowCount} 行，当前检查项展示其中 ${displayedRowCount} 行匹配结果。`}
        />
      )}
      {rows.length > 0 ? (
        <Table
          size="small"
          rowKey="__evidenceRowKey"
          dataSource={rows}
          columns={columns}
          pagination={{ defaultPageSize: 10, showSizeChanger: true, pageSizeOptions: PAGE_SIZE_OPTIONS }}
          scroll={{ x: Math.max(columns.length * 150, 900) }}
        />
      ) : (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={emptyDescription} />
      )}
      {hasRawPayload && sqlList.length === 0 && rows.length === 0 && (
        <Collapse
          size="small"
          items={[
            {
              key: "raw",
              label: "查看原始采集数据",
              children: <pre className="ops-output">{JSON.stringify(payload, null, 2)}</pre>,
            },
          ]}
        />
      )}
    </Space>
  );
}

function EvidenceExplorer({ items, moduleKey, onClearModule }) {
  const [selectedItem, setSelectedItem] = useState(null);
  const rows = moduleKey ? (items || []).filter((item) => item.module_key === moduleKey) : (items || []);
  const columns = [
    {
      title: "检查项",
      dataIndex: "check_item",
      width: 260,
      ellipsis: true,
      render: (value, row) => (
        <div className="report-evidence-item-cell">
          <div className="report-evidence-item-title" title={value || "巡检项"}>{value || "巡检项"}</div>
          <div className="report-evidence-item-summary" title={row.finding || "-"}>{row.finding || "-"}</div>
        </div>
      ),
    },
    { title: "模块", dataIndex: "module_key", width: 180, ellipsis: true },
    {
      title: "状态",
      dataIndex: "severity",
      width: 100,
      render: (value, row) => {
        const payload = findingPayload(row);
        const status = evidenceExecutionStatus(row, payload);
        const color = ["failed", "error", "critical"].includes(status) ? "red"
          : ["skipped", "unknown", "summary"].includes(status) ? "default" : status === "partial" ? "orange" : status === "success" ? "green" : riskColor(status);
        return <Tag color={color}>{evidenceStatusText(status, payload)}</Tag>;
      },
    },
    {
      title: "SQL",
      width: 90,
      render: (_, row) => {
        const payload = findingPayload(row);
        if (payload.evidence_kind === "execution_summary") return <Tag color="purple">执行汇总</Tag>;
        const recorded = evidenceSqlList(payload).length > 0;
        return <Tag color={recorded ? "blue" : "default"}>{recorded ? "已记录" : "无"}</Tag>;
      },
    },
    {
      title: "返回行数",
      width: 110,
      render: (_, row) => {
        const payload = findingPayload(row);
        const evidenceRows = normalizeRows(Array.isArray(payload.rows) ? payload.rows : payload.items);
        return evidenceRowCount(payload, evidenceRows);
      },
    },
    {
      title: "结果摘要",
      width: 300,
      ellipsis: true,
      render: (_, row) => {
        const payload = findingPayload(row);
        return payload.message || payload.summary || row.finding || "-";
      },
    },
    {
      title: "操作",
      fixed: "right",
      width: 140,
      render: (_, row) => (
        <Button type="link" size="small" onClick={() => setSelectedItem(row)}>
          查看 SQL 与结果
        </Button>
      ),
    },
  ];

  return (
    <Space direction="vertical" size={12} className="full-width">
      {moduleKey && (
        <Alert
          type="info"
          showIcon
          message={`正在查看模块：${moduleKey}`}
          action={<Button size="small" onClick={onClearModule}>查看全部</Button>}
        />
      )}
      <Table
        size="small"
        rowKey={(row) => stableRecordKey("report-evidence-item", row, ["id", "check_item", "module_key"])}
        dataSource={rows}
        columns={columns}
        pagination={{ defaultPageSize: 20, showSizeChanger: true, pageSizeOptions: PAGE_SIZE_OPTIONS, showTotal: (total) => `共 ${total} 项` }}
        scroll={{ x: 1180 }}
      />
      <Modal
        title={selectedItem ? `SQL 与查询结果 - ${selectedItem.check_item || "巡检项"}` : "SQL 与查询结果"}
        open={Boolean(selectedItem)}
        onCancel={() => setSelectedItem(null)}
        footer={<Button onClick={() => setSelectedItem(null)}>关闭</Button>}
        width={1180}
        centered
        destroyOnHidden
      >
        {selectedItem && <EvidenceBlock item={selectedItem} />}
      </Modal>
    </Space>
  );
}

function OperationPlanBlock({ item }) {
  const plan = item?.operation_plan || {};
  const sqlList = Array.isArray(plan.sql) ? plan.sql : [];
  return (
    <Space direction="vertical" size={12} className="full-width">
      <Descriptions bordered size="small" column={3}>
        <Descriptions.Item label="状态">{plan.status || "plan_only"}</Descriptions.Item>
        <Descriptions.Item label="只读">{plan.read_only === false ? "否" : "是"}</Descriptions.Item>
        <Descriptions.Item label="需要审批">{plan.approval_required ? "是" : "否"}</Descriptions.Item>
      </Descriptions>
      {(plan.steps || []).length > 0 && (
        <div className="report-ordered-list">
          {(plan.steps || []).map((step, index) => (
            <div key={stableValueKey(`report-plan-step-${index}`, step)}>
              <span>{index + 1}</span>
              <Text>{step}</Text>
            </div>
          ))}
        </div>
      )}
      {sqlList.length > 0 && (
        <Space direction="vertical" size={8} className="full-width">
          {sqlList.map((sql, index) => (
            <pre className="ops-output" key={stableValueKey(`report-plan-sql-${index}`, sql)}>{sql}</pre>
          ))}
        </Space>
      )}
      {(plan.rollback_plan || []).length > 0 && (
        <Alert
          type="info"
          showIcon
          message="回退说明"
          description={(plan.rollback_plan || []).join("；")}
        />
      )}
    </Space>
  );
}

function RiskList({ items, emptyDescription = "暂无严重或警告风险。当前巡检未发现需要立即处置的问题。" }) {
  if (!items || items.length === 0) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={emptyDescription} />;
  }
  return (
    <Space direction="vertical" size={12} className="full-width">
      {items.map((item, index) => (
        <div
          className={`report-risk-item risk-${normalizeRiskLevel(item.severity || item.risk_level)}`}
          key={stableRecordKey("report-risk-item", item, ["id", "check_item", "severity", "finding"])}
        >
          <div className="report-risk-head">
            <Space wrap>
              <Tag color={riskColor(item.severity)}>{riskText(item.severity)}</Tag>
              <Text strong>{index + 1}. {item.check_item || "巡检项"}</Text>
            </Space>
            <Text type="secondary">扣分 {riskPenalty(item)}</Text>
          </div>
          <Paragraph className="report-risk-finding">{item.finding || "-"}</Paragraph>
          {(item.suggestions || []).length > 0 && (
            <Alert type="info" showIcon message="建议" description={(item.suggestions || []).join("；")} />
          )}
          <Space size={6} wrap className="report-risk-actions">
            <Button size="small" disabled>查看证据</Button>
            <Button size="small" disabled>生成处置方案（Plan Only）</Button>
            <Button size="small" disabled>转告警</Button>
            <Button size="small" disabled>转工单</Button>
          </Space>
          <Collapse
            size="small"
            className="report-risk-collapse"
            items={[
              { key: "evidence", label: "展开证据链", children: <EvidenceBlock item={item} /> },
              { key: "plan", label: "处置方案（Plan Only）", children: <OperationPlanBlock item={item} /> },
            ]}
          />
        </div>
      ))}
    </Space>
  );
}

function CategoryTable({ items, onViewModule, complete }) {
  const columns = [
    { title: "分类", dataIndex: "module_name", width: 220 },
    {
      title: "得分",
      dataIndex: "score",
      width: 150,
      render: (value) => (
        complete && value != null ? <Progress percent={value} size="small" status={value < 60 ? "exception" : "normal"} /> : "未生成"
      ),
    },
    { title: "风险", dataIndex: "risk_level", width: 110, render: (v) => complete ? <Tag color={riskColor(v)}>{riskText(v)}</Tag> : <Tag>未确认</Tag> },
    {
      title: "异常分布",
      dataIndex: "counts",
      width: 220,
      render: (counts = {}) => (
        <Space size={4} wrap>
          <Tag color="red">严重 {counts.critical || 0}</Tag>
          <Tag color="orange">警告 {counts.warning || 0}</Tag>
          <Tag color="blue">提示 {counts.info || 0}</Tag>
          <Tag color="green">健康 {counts.normal || 0}</Tag>
        </Space>
      ),
    },
    { title: "结论", dataIndex: "summary", ellipsis: true, render: (value) => complete ? value : "采集不完整，不能判定健康" },
    {
      title: "操作",
      fixed: "right",
      width: 110,
      render: (_, row) => <Button type="link" size="small" onClick={() => onViewModule?.(row.module_key)}>查看明细</Button>,
    },
  ];
  return (
    <Table
      size="small"
      rowKey={(row) => row.module_key || row.module_name}
      dataSource={items || []}
      columns={columns}
      pagination={{ defaultPageSize: 20, showSizeChanger: true, pageSizeOptions: PAGE_SIZE_OPTIONS }}
      scroll={{ x: 1120 }}
    />
  );
}

export function InspectionReportDetailPage({
  detail,
  onExportHtml,
  onExportMarkdown,
  onExportWord,
  exportsDisabled = false,
}) {
  const [ticketOpen, setTicketOpen] = useState(false);
  const [activeTab, setActiveTab] = useState("summary");
  const [evidenceModule, setEvidenceModule] = useState(null);
  if (!detail) return <Empty description="暂无巡检报告" />;

  const task = detail.task || {};
  const instance = detail.instance || {};
  const summary = detail.summary || {};
  const outcome = inspectionOutcome(detail);
  const executionFailed = outcome.status === "failed";
  const ai = detail.ai_result || {};
  const riskItems = detail.risk_items || [];
  const allFindings = detail.all_findings || riskItems;
  const hintItems = allFindings.filter((item) => normalizeRiskLevel(item.severity || item.risk_level) === "info");
  const riskAndHints = [...riskItems, ...hintItems];
  const priorities = ai.recommended_priorities || riskItems.slice(0, 5);
  const noSevereWarning = riskItems.length === 0;

  return (
    <div className="inspection-report-page">
      <div className="report-paper">
        <div className="report-cover">
          <div className="report-cover-copy">
            <Text className="report-kicker">数据库健康巡检</Text>
            <Title level={3}>TraceMind AI 巡检报告</Title>
            <Space wrap>
              {outcome.complete && <Tag color={riskColor(summary.risk_level)}>{riskText(summary.risk_level)}</Tag>}
              {!outcome.complete && <Tag color={executionFailed ? "red" : "orange"}>{outcome.label}</Tag>}
              <Tag>{String(instance.db_type || task.db_type || "-").toUpperCase()}</Tag>
              <Text type="secondary">任务号：{task.task_id || detail.task_id || "-"}</Text>
            </Space>
          </div>
          <Space wrap className="report-cover-actions">
            <Button size="small" onClick={() => setTicketOpen(true)}>创建 ITSM 工单</Button>
            <Button size="small" icon={<FileTextOutlined />} disabled={exportsDisabled || typeof onExportMarkdown !== "function"} onClick={onExportMarkdown}>Markdown</Button>
            <Button size="small" icon={<DownloadOutlined />} disabled={exportsDisabled || typeof onExportHtml !== "function"} onClick={onExportHtml}>HTML</Button>
            <Button size="small" type="primary" icon={<DownloadOutlined />} disabled={exportsDisabled || typeof onExportWord !== "function"} onClick={onExportWord}>Word 报告</Button>
          </Space>
        </div>

        <Descriptions bordered size="small" column={{ xs: 1, sm: 2, lg: 3 }} className="report-meta">
          <Descriptions.Item label="实例">{instance.name || task.instance_id || "-"}</Descriptions.Item>
          <Descriptions.Item label="地址">{`${instance.host || "-"}:${instance.port || "-"}`}</Descriptions.Item>
          <Descriptions.Item label="数据库">{String(instance.db_type || task.db_type || "-").toUpperCase()}</Descriptions.Item>
          <Descriptions.Item label="巡检时间">
            {(detail.report || {}).inspection_time || task.finished_at || task.create_time || "-"}
          </Descriptions.Item>
          <Descriptions.Item label="生成时间">{detail.generated_at || "-"}</Descriptions.Item>
          <Descriptions.Item label="账号能力">
            {task.account_capability_label || (task.read_only ? "只读降级采集" : "账号能力未评估")}
          </Descriptions.Item>
          <Descriptions.Item label="巡检模板">{task.template_name || task.template_key || "未记录（历史任务）"}</Descriptions.Item>
          <Descriptions.Item label="模板修订">
            {task.template_revision
              ? `${task.template_revision}${task.template_item_count == null ? "" : ` · ${task.template_item_count} 项`}`
              : "未记录（历史任务）"}
          </Descriptions.Item>
          <Descriptions.Item label="执行状态">
            {outcome.label}
          </Descriptions.Item>
        </Descriptions>
        {!outcome.complete && (
          <Alert
            type={executionFailed ? "error" : "warning"}
            showIcon
            message={`巡检${outcome.label}，未生成完整健康分`}
            description={outcome.reason || "未获得完整有效的检查结果，不能据此判断数据库健康。"}
          />
        )}

        <Row gutter={[12, 12]} className="report-score-row">
          <Col xs={12} md={4}>
            <Card className="report-score-card">
              <Statistic
                title="健康分"
                value={outcome.score ?? "未生成"}
                suffix={outcome.score == null ? undefined : "/ 100"}
              />
            </Card>
          </Col>
          <Col xs={12} md={4}>
            <Card className="report-score-card">
              <Statistic title="总检查项" value={summary.total || 0} />
            </Card>
          </Col>
          <Col xs={12} md={4}>
            <Card className="report-score-card">
              <Statistic title="正常项" value={summary.normal || 0} valueStyle={{ color: "#389e0d" }} />
            </Card>
          </Col>
          <Col xs={12} md={4}>
            <Card className="report-score-card">
              <Statistic title="提示" value={summary.info || 0} valueStyle={{ color: "#1677ff" }} />
            </Card>
          </Col>
          <Col xs={12} md={4}>
            <Card className="report-score-card">
              <Statistic title="警告" value={summary.warning || 0} valueStyle={{ color: "#d48806" }} />
            </Card>
          </Col>
          <Col xs={12} md={4}>
            <Card className="report-score-card">
              <Statistic title="严重" value={summary.critical || 0} valueStyle={{ color: "#cf1322" }} />
            </Card>
          </Col>
        </Row>

        <Tabs
          className="report-tabs"
          activeKey={activeTab}
          onChange={setActiveTab}
          items={[
            {
              key: "summary",
              label: "报告摘要",
              children: (
                <Space direction="vertical" size={16} className="full-width">
                  <Alert type={outcome.complete ? alertType(summary.risk_level) : executionFailed ? "error" : "warning"} showIcon message={outcome.complete ? detail.conclusion || task.summary || "-" : outcome.reason || "采集不完整，不能判定健康。"} />
                  {outcome.complete && noSevereWarning && (summary.info || hintItems.length) > 0 && (
                    <Alert
                      type="info"
                      showIcon
                      message="暂无严重或警告风险。当前仅存在提示项，建议纳入观察。"
                      description={`提示项数量：${summary.info || hintItems.length}`}
                    />
                  )}
                  <Row gutter={[12, 12]}>
                    <Col xs={24} lg={12}>
                      <Card size="small" title="主要风险">
                        <RiskList
                          items={riskItems.slice(0, 5)}
                          emptyDescription={!outcome.complete ? "尚无完整采集结果，不能判断是否存在风险。" : noSevereWarning && (summary.info || hintItems.length) > 0
                            ? "暂无严重或警告风险。提示项已单独记录，建议纳入观察。"
                            : "暂无严重或警告风险。当前报告没有需要立即处置的问题。"}
                        />
                      </Card>
                    </Col>
                    <Col xs={24} lg={12}>
                      <Card size="small" title="优先处理建议 / 提示项">
                        {priorities.length > 0 ? (
                          <Space direction="vertical" size={10} className="full-width">
                            {priorities.map((item, index) => (
                              <Alert
                                key={stableRecordKey(`report-priority-${index}`, item, ["id", "check_item", "finding"])}
                                type={alertType(item.severity)}
                                showIcon
                                message={`${index + 1}. ${item.check_item || "待处理事项"}`}
                                description={item.finding || (item.suggestions || []).join("；")}
                              />
                            ))}
                          </Space>
                        ) : (
                          <RiskList items={hintItems.slice(0, 5)} emptyDescription={outcome.complete ? "暂无优先处置项，也没有提示项需要观察。" : "尚无完整采集结果，不能判断是否需要处置。"} />
                        )}
                      </Card>
                    </Col>
                  </Row>
                </Space>
              ),
            },
            {
              key: "risks",
              label: "风险明细",
              children: <RiskList items={riskAndHints} emptyDescription={outcome.complete ? "暂无严重、警告或提示项。本次巡检未发现需要关注的问题。" : "尚无完整采集结果，不能判断是否存在风险。"} />,
            },
            {
              key: "category",
              label: "模块得分",
              children: (
                <CategoryTable
                  complete={outcome.complete}
                  items={detail.category_summary || []}
                  onViewModule={(moduleKey) => {
                    setEvidenceModule(moduleKey);
                    setActiveTab("evidence");
                  }}
                />
              ),
            },
            {
              key: "evidence",
              label: "SQL 与结果",
              children: (
                <EvidenceExplorer
                  items={allFindings}
                  moduleKey={evidenceModule}
                  onClearModule={() => setEvidenceModule(null)}
                />
              ),
            },
            {
              key: "ai",
              label: "综合分析",
              children: (
                <Space direction="vertical" size={12} className="full-width">
                  <Alert
                    type="info"
                    showIcon
                    icon={<RobotOutlined />}
                    message="诊断证据范围"
                    description={(ai.evidence_scope || []).join(" / ") || "巡检结构化 Finding / 原始证据链"}
                  />
                  <Card size="small" title="风险成因">
                    {(ai.root_cause_analysis || []).length > 0 ? (
                      <Space direction="vertical" className="full-width">
                        {(ai.root_cause_analysis || []).map((item) => (
                          <Alert key={stableValueKey("report-ai-root", item)} type="info" showIcon message={item} />
                        ))}
                      </Space>
                    ) : (
                      <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无风险成因分析" />
                    )}
                  </Card>
                  <Card size="small" title="影响范围">
                    {(ai.business_impact || []).length > 0 ? (
                      <Space direction="vertical" className="full-width">
                        {(ai.business_impact || []).map((item) => (
                          <Alert key={stableValueKey("report-ai-impact", item)} type="warning" showIcon message={item} />
                        ))}
                      </Space>
                    ) : (
                      <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无业务影响" />
                    )}
                  </Card>
                </Space>
              ),
            },
            {
              key: "plans",
              label: "处置建议",
              children: (
                <Space direction="vertical" size={12} className="full-width">
                  <Alert type="info" showIcon message="所有处置动作均为 plan_only，不会直接执行生产变更。" />
                  <RiskList items={riskItems} />
                  <Card size="small" title="工单描述">
                    <pre className="report-ticket-preview">{ai.ticket_description || "-"}</pre>
                  </Card>
                </Space>
              ),
            },
            {
              key: "markdown",
              label: "Markdown原文",
              children: (
                <pre className="report-markdown">{detail.markdown || "暂无 Markdown 内容"}</pre>
              ),
            },
          ]}
        />
      </div>
      <ItsmTicketDrawer
        open={ticketOpen}
        source={{
          sourceType: "INSPECTION",
          sourceId: task.task_id || detail.task_id,
          sourceRefId: Number.isInteger(task.id) ? task.id : undefined,
          instanceId: task.instance_id,
          instanceName: instance.name,
          dbType: instance.db_type || task.db_type,
          title: `巡检报告风险：${instance.name || task.instance_id || "数据库实例"}`,
          description: ai.ticket_description || [detail.conclusion, ...riskItems.slice(0, 5).map((item) => `${item.check_item || "风险项"}：${item.finding || "-"}`)].filter(Boolean).join("\n"),
          priority: (summary.critical || 0) > 0 ? "P1" : (summary.warning || 0) > 0 ? "P2" : "P3",
          severity: (summary.critical || 0) > 0 ? "critical" : (summary.warning || 0) > 0 ? "warning" : "info",
        }}
        onClose={() => setTicketOpen(false)}
      />
    </div>
  );
}

export default InspectionReportDetailPage;
