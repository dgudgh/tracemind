import React, { useEffect, useState } from "react";
import {
  Button,
  Card,
  Dropdown,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from "antd";
import { DownloadOutlined, MoreOutlined, ReloadOutlined } from "@ant-design/icons";
import {
  downloadInspectionReportHtml,
  downloadInspectionReportMarkdown,
  downloadInspectionReportWord,
  getInspectionReportStructuredDetail,
  getInspectionReports,
} from "./modules/reportApi";
import { InspectionReportDetailPage } from "./InspectionReportDetailPage";

export { UnifiedReportCenterPage } from "./UnifiedReportCenterPage";

const { Text } = Typography;

const DB_TYPE_LABELS = {
  oracle: "Oracle",
  mysql: "MySQL",
  postgresql: "PostgreSQL",
  postgres: "PostgreSQL",
  sqlserver: "SQL Server",
  mssql: "SQL Server",
  host: "主机",
};

const normalizeRiskLevel = (level) => {
  const value = String(level || "normal").toLowerCase();
  if (["critical", "high", "disaster"].includes(value)) return "critical";
  if (["warning", "average"].includes(value)) return "warning";
  if (["info", "low", "notice"].includes(value)) return "info";
  return "normal";
};
const riskColorCn = (level) => ({ critical: "red", warning: "orange", info: "blue", normal: "green" }[normalizeRiskLevel(level)] || "default");
const riskTextCn = (level) => ({ critical: "严重", warning: "警告", info: "提示", normal: "健康" }[normalizeRiskLevel(level)] || "-");
const dbTypeText = (value) => DB_TYPE_LABELS[String(value || "").toLowerCase()] || String(value || "-").toUpperCase();

function scoreTag(score) {
  return <Tag color={score >= 90 ? "green" : score >= 75 ? "blue" : "red"}>{score ?? "-"}</Tag>;
}

function reportView(row = {}) {
  const generatedAt = row.generated_at || row.create_time || row.finished_at || "-";
  return {
    ...row,
    report_id: row.report_id || row.task_id,
    report_name: row.report_name || `${dbTypeText(row.db_type)} 巡检报告 - ${row.instance_name || row.instance_id || "-"} - ${generatedAt}`,
    generated_at: generatedAt,
    report_status: row.report_status || "已生成",
    score: row.overall_score ?? row.score,
    risk_level: normalizeRiskLevel(row.risk_level),
  };
}

function planOnly(action) {
  Modal.info({
    title: `${action}（Plan Only）`,
    content: "当前阶段仅生成方案或预览，不直接执行归档、外发或生产变更。",
  });
}

function ReportTitleCell({ row }) {
  return (
    <div className="report-title-cell">
      <Tooltip title={row.report_name}>
        <Text strong ellipsis className="report-title-main">{row.report_name}</Text>
      </Tooltip>
      <Text type="secondary" className="report-title-sub">
        {dbTypeText(row.db_type)} · {row.generated_at || "-"} · 任务号 {row.task_id || "-"}
      </Text>
    </div>
  );
}

export function ReportPage() {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(false);
  const [dbTypeFilter, setDbTypeFilter] = useState();
  const [detail, setDetail] = useState(null);
  const [detailOpen, setDetailOpen] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const r = await getInspectionReports(dbTypeFilter);
      setItems(r.items || []);
    } catch (e) { message.error(e.message); }
    setLoading(false);
  };
  useEffect(() => { load(); }, [dbTypeFilter]);
  const reportRows = items.map(reportView);

  const viewReport = async (r) => {
    try { setDetail(await getInspectionReportStructuredDetail(r.task_id, { instance_id: r.instance_id })); setDetailOpen(true); }
    catch (e) { message.error(e.message); }
  };
  const downloadMarkdown = async (r) => {
    try { await downloadInspectionReportMarkdown(r.task_id, { instance_id: r.instance_id }); }
    catch (e) { message.error(e.message); }
  };
  const downloadHtml = async (r) => {
    try { await downloadInspectionReportHtml(r.task_id, { instance_id: r.instance_id }); }
    catch (e) { message.error(e.message); }
  };
  const exportMenu = (row) => ({
    items: [
      { key: "markdown", label: "导出 Markdown" },
      { key: "html", label: "导出 HTML" },
      { key: "pdf", label: "PDF（预留）", disabled: true },
      { key: "word", label: "Word（预留）", disabled: true },
    ],
    onClick: ({ key }) => {
      if (key === "markdown") downloadMarkdown(row);
      if (key === "html") downloadHtml(row);
    },
  });
  const moreMenu = () => ({
    items: [
      { key: "archive", label: "归档" },
      { key: "copy", label: "复制链接" },
    ],
    onClick: ({ key }) => {
      if (key === "archive") planOnly("报告归档");
      if (key === "copy") planOnly("复制报告链接");
    },
  });
  const downloadWordFromDetail = async () => {
    const task = detail?.task || {};
    if (!task.instance_id || !task.task_id) return;
    try {
      await downloadInspectionReportWord(task.instance_id, task.task_id);
    } catch (e) { message.error(e.message); }
  };
  const downloadMarkdownFromDetail = async () => {
    const task = detail?.task || {};
    if (!task.task_id) return;
    try {
      await downloadInspectionReportMarkdown(task.task_id, { instance_id: task.instance_id });
    } catch (e) { message.error(e.message); }
  };
  const downloadHtmlFromDetail = async () => {
    const task = detail?.task || {};
    if (!task.task_id) return;
    try {
      await downloadInspectionReportHtml(task.task_id, { instance_id: task.instance_id });
    } catch (e) { message.error(e.message); }
  };

  return (
    <Space direction="vertical" size={16} className="full-width inspection-page-shell">
      <Card>
        <Space wrap>
          <Button type="primary" icon={<ReloadOutlined />} loading={loading} onClick={load}>刷新</Button>
          <Select style={{ width: 160 }} value={dbTypeFilter} onChange={setDbTypeFilter} allowClear placeholder="数据库类型"
            options={[
              { value: "oracle", label: "Oracle" },
              { value: "mysql", label: "MySQL" },
              { value: "postgresql", label: "PostgreSQL" },
              { value: "sqlserver", label: "SQL Server" },
              { value: "host", label: "Host" },
            ]} />
          <Text type="secondary">巡检报告是正式报告归档，用于查看、导出和后续归档；任务执行状态请进入「巡检任务」。</Text>
        </Space>
      </Card>
      <Card size="small" title="巡检报告归档">
        <Table dataSource={reportRows} rowKey="report_id" size="small" loading={loading} pagination={{ defaultPageSize: 10, showSizeChanger: true, pageSizeOptions: [10, 20, 50, 100] }} scroll={{ x: 1080 }}
          locale={{ emptyText: "暂无巡检报告。完成巡检后可在一键巡检或巡检任务中生成正式报告。" }}
          columns={[
            { title: "报告名称", dataIndex: "report_name", width: 420, render: (_, row) => <ReportTitleCell row={row} /> },
            { title: "实例", dataIndex: "instance_name", width: 170, ellipsis: true, render: (v) => <Tooltip title={v}><Text ellipsis style={{ maxWidth: 150 }}>{v || "-"}</Text></Tooltip> },
            { title: "数据库类型", dataIndex: "db_type", width: 110, render: v => <Tag>{dbTypeText(v)}</Tag> },
            { title: "健康分", dataIndex: "score", width: 85, render: scoreTag },
            { title: "风险等级", dataIndex: "risk_level", width: 100, render: v => <Tag color={riskColorCn(v)}>{riskTextCn(v)}</Tag> },
            { title: "生成时间", dataIndex: "generated_at", width: 165 },
            { title: "报告状态", dataIndex: "report_status", width: 105, render: v => <Tag color="green">{v || "已生成"}</Tag> },
            { title: "操作", width: 190, fixed: "right", render: (_, r) => <Space size={4}>
              <Button size="small" type="link" onClick={() => viewReport(r)}>查看</Button>
              <Dropdown menu={exportMenu(r)} trigger={["click"]}>
                <Button size="small" type="link" icon={<DownloadOutlined />}>导出</Button>
              </Dropdown>
              <Dropdown menu={moreMenu(r)} trigger={["click"]}>
                <Button size="small" type="link" icon={<MoreOutlined />}>更多</Button>
              </Dropdown>
            </Space> },
          ]} />
      </Card>
      <Modal
        className="inspection-report-modal"
        open={detailOpen}
        onCancel={() => setDetailOpen(false)}
        footer={null}
        centered
        width={1360}
        destroyOnHidden
        title={detail ? `巡检报告 · ${detail.instance?.name || detail.task?.instance_id} · ${(detail.task?.db_type || "").toUpperCase()}` : "巡检报告"}
      >
        {detail && (
          <InspectionReportDetailPage
            detail={detail}
            onExportHtml={downloadHtmlFromDetail}
            onExportMarkdown={downloadMarkdownFromDetail}
            onExportWord={downloadWordFromDetail}
          />
        )}
      </Modal>
    </Space>
  );
}
