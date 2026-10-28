import React, { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Collapse,
  Descriptions,
  Modal,
  Row,
  Space,
  Statistic,
  Table,
  Tag,
  Typography,
  message,
} from "antd";
import { DownloadOutlined, FileSearchOutlined, ReloadOutlined } from "@ant-design/icons";
import { PlanOnlyScriptModal } from "./OpsLayout";
import { statusTag, useSyncedInstanceId } from "./DiagnosisPageHelpers";
import { saveCsvFile } from "./modules/fileExport";
import { stableRecordKey, stableValueKey } from "./modules/stableKeys";
import {
  generateDiagnosisAction,
  getDiagnosisRules,
  getRuleDiagnosisHistory,
  runRuleDiagnosis,
  updateDiagnosisRule,
} from "./modules/ruleDiagnosisApi";

const { Text } = Typography;

export function RuleDiagnosisPage({ instances, selectedInstanceId }) {
  const diagnosisInstances = instances.filter((item) => ["oracle", "mysql"].includes(item.db_type));
  const [instanceId] = useSyncedInstanceId(selectedInstanceId, diagnosisInstances);
  const [loading, setLoading] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [runResult, setRunResult] = useState(null);
  const [history, setHistory] = useState([]);
  const [rules, setRules] = useState([]);
  const [rulesLoading, setRulesLoading] = useState(false);
  const [detail, setDetail] = useState(null);
  const [planModal, setPlanModal] = useState({ open: false, title: "", content: "" });

  const selectedInstance = diagnosisInstances.find((item) => item.id === instanceId);

  const loadHistory = async () => {
    setHistoryLoading(true);
    try {
      const data = await getRuleDiagnosisHistory({ instance_id: instanceId, limit: 80 });
      setHistory(data.items || []);
    } catch (e) {
      message.error(e.message || "加载诊断历史失败");
    } finally {
      setHistoryLoading(false);
    }
  };

  useEffect(() => {
    loadHistory();
  }, [instanceId]);

  const loadRules = async () => {
    setRulesLoading(true);
    try {
      const data = await getDiagnosisRules();
      setRules(data.items || []);
    } catch (e) {
      message.error(e.message || "加载诊断规则失败");
    } finally {
      setRulesLoading(false);
    }
  };

  useEffect(() => {
    loadRules();
  }, []);

  const runDiagnosis = async () => {
    if (!instanceId) {
      message.warning("请先在右上角选择实例");
      return;
    }
    setLoading(true);
    try {
      const data = await runRuleDiagnosis(instanceId);
      setRunResult(data);
      setHistory(data.results || []);
      message.success("规则诊断完成");
    } catch (e) {
      message.error(e.message || "规则诊断失败");
    } finally {
      setLoading(false);
    }
  };

  const openPlan = async (record, action) => {
    try {
      const data = await generateDiagnosisAction(record.id, action);
      const content = data.sections
        ? data.sections.map((section) => `## ${section.title}\n${section.script}`).join("\n\n")
        : JSON.stringify(data, null, 2);
      setPlanModal({
        open: true,
        title: action === "create-ticket" ? "工单描述" : action === "action-plan" ? "处置方案" : "Zabbix 推送载荷",
        content,
      });
    } catch (e) {
      message.error(e.message || "生成方案失败");
    }
  };

  const toggleRule = async (record) => {
    try {
      await updateDiagnosisRule(record.id, { enabled: record.enabled ? 0 : 1 });
      message.success(record.enabled ? "规则已停用" : "规则已启用");
      loadRules();
    } catch (e) {
      message.error(e.message || "更新规则失败");
    }
  };

  const actionText = {
    generate_tablespace_extend_sql: "扩容 SQL",
    generate_archive_plan: "归档方案",
    create_ticket: "转工单",
    push_zabbix: "推 Zabbix",
    generate_rman_cleanup_plan: "RMAN 清理",
    generate_archive_backup_plan: "归档备份",
    open_slow_sql_analysis: "慢 SQL 分析",
    generate_index_advice: "索引建议",
    gather_stats_plan: "统计信息脚本",
    generate_rebuild_index_sql: "重建索引",
    show_blocking_session: "阻塞定位",
    open_undo_management: "UNDO 管理",
    generate_undo_retention_plan: "UNDO 保留方案",
    show_dataguard_status: "DG 状态",
    generate_verify_sql: "校验 SQL",
    merge_alert: "合并告警",
    export_report: "导出报告",
  };

  const categoryText = {
    space: "空间",
    performance: "性能",
    sql_index: "SQL/索引",
    transaction: "事务/锁",
    ha: "高可用",
    archive: "备份归档",
    external_monitor: "外部监控",
    overview: "总览",
  };

  const dataSource = runResult?.results?.length ? runResult.results : history;
  const summary = runResult?.summary;

  const columns = [
    { title: "级别", dataIndex: "severity", width: 90, render: (value) => statusTag(value) },
    { title: "规则", dataIndex: "rule_name", width: 180, ellipsis: true },
    { title: "分类", dataIndex: "category", width: 100, render: (value) => <Tag>{categoryText[value] || value || "-"}</Tag> },
    { title: "对象", dataIndex: "object_name", width: 160, ellipsis: true },
    { title: "置信度", dataIndex: "confidence", width: 90, render: (value) => `${value || 0}%` },
    { title: "根因", dataIndex: "root_cause", ellipsis: true },
    {
      title: "动作",
      dataIndex: "actions",
      width: 220,
      render: (actions = []) => (
        <Space wrap size={4}>
          {actions.slice(0, 3).map((item) => <Tag color="blue" key={item}>{actionText[item] || item}</Tag>)}
        </Space>
      ),
    },
    {
      title: "操作",
      width: 190,
      render: (_, record) => (
        <Space>
          <Button size="small" onClick={() => setDetail(record)}>详情</Button>
          {record.id && <Button size="small" type="primary" ghost onClick={() => openPlan(record, "action-plan")}>方案</Button>}
          {record.id && <Button size="small" onClick={() => openPlan(record, "create-ticket")}>工单</Button>}
          {record.id && <Button size="small" onClick={() => openPlan(record, "push-zabbix")}>Zabbix</Button>}
        </Space>
      ),
    },
  ];

  const ruleColumns = [
    { title: "状态", dataIndex: "enabled", width: 90, render: (value) => <Tag color={value ? "green" : "default"}>{value ? "启用" : "停用"}</Tag> },
    { title: "规则编码", dataIndex: "rule_code", width: 190, ellipsis: true },
    { title: "规则名称", dataIndex: "rule_name", width: 200, ellipsis: true },
    { title: "分类", dataIndex: "category", width: 110, render: (value) => <Tag>{categoryText[value] || value || "-"}</Tag> },
    { title: "默认级别", dataIndex: "severity", width: 100, render: (value) => statusTag(value) },
    { title: "说明", dataIndex: "description", ellipsis: true },
    {
      title: "操作",
      width: 100,
      render: (_, record) => (
        <Button size="small" onClick={() => toggleRule(record)}>
          {record.enabled ? "停用" : "启用"}
        </Button>
      ),
    },
  ];

  return (
    <Space direction="vertical" size={16} className="full-width">
      <Card title="规则诊断中心">
        <Space direction="vertical" size={12} className="full-width">
          <Alert
            type="info"
            showIcon
            message="规则引擎负责判断事实，AI 只基于规则证据链做总结。"
            description="诊断会读取当前实例的结构化巡检结果，生成风险等级、证据链、根因、业务影响和处置动作。"
          />
          <Space wrap>
            <Tag color={selectedInstance ? "blue" : "default"}>
              当前实例：{selectedInstance ? `${selectedInstance.instance_name} (${selectedInstance.host}:${selectedInstance.port})` : "请在右上角选择实例"}
            </Tag>
            <Button type="primary" icon={<FileSearchOutlined />} loading={loading} onClick={runDiagnosis} disabled={!instanceId}>
              运行规则诊断
            </Button>
            <Button icon={<ReloadOutlined />} loading={historyLoading} onClick={loadHistory}>
              刷新历史
            </Button>
            <Button icon={<DownloadOutlined />} disabled={dataSource.length === 0} onClick={() => {
              const header = "级别,规则,分类,对象,置信度,根因,建议,动作";
              const csvRows = [header];
              dataSource.forEach((row) => {
                csvRows.push([
                  row.severity || "",
                  (row.rule_name || "").replace(/,/g, " "),
                  categoryText[row.category] || row.category || "",
                  (row.object_name || "").replace(/,/g, " "),
                  row.confidence || 0,
                  (row.root_cause || "").replace(/,/g, " ").replace(/\n/g, " "),
                  (row.suggestion || "").replace(/,/g, " ").replace(/\n/g, " "),
                  (row.actions || []).map((a) => actionText[a] || a).join("; "),
                ].join(","));
              });
              saveCsvFile(csvRows, `rule_diagnosis_${selectedInstance?.instance_name || "report"}_${new Date().toISOString().slice(0, 10)}.csv`);
              message.success("报告已导出");
            }}>
              导出报告
            </Button>
          </Space>
        </Space>
      </Card>

      {summary && (
        <Row gutter={[16, 16]}>
          <Col xs={12} md={6}><Card size="small"><Statistic title="巡检项" value={summary.total_checks} /></Card></Col>
          <Col xs={12} md={6}><Card size="small"><Statistic title="诊断结论" value={summary.diagnosis_count} /></Card></Col>
          <Col xs={12} md={6}><Card size="small"><Statistic title="Critical" value={summary.critical_diagnosis} valueStyle={{ color: "#cf1322" }} /></Card></Col>
          <Col xs={12} md={6}><Card size="small"><Statistic title="Warning" value={summary.warning_diagnosis} valueStyle={{ color: "#d48806" }} /></Card></Col>
        </Row>
      )}

      <Card title={runResult ? `本次诊断结果：${runResult.run_id}` : "诊断历史"}>
        <Table
          size="small"
          rowKey={(row) => stableRecordKey("rule-diagnosis", row, ["id", "run_id", "rule_code", "object_name", "metric_name", "create_time"])}
          columns={columns}
          dataSource={dataSource}
          loading={loading || historyLoading}
          pagination={{ defaultPageSize: 10, showSizeChanger: true, pageSizeOptions: ["10", "20", "50", "100"] }}
          scroll={{ x: 1200 }}
        />
      </Card>

      <Collapse
        items={[
          {
            key: "rules",
            label: `诊断规则配置 (${rules.length})`,
            children: (
              <Table
                size="small"
                rowKey="id"
                columns={ruleColumns}
                dataSource={rules}
                loading={rulesLoading}
                pagination={{ defaultPageSize: 10, showSizeChanger: true, pageSizeOptions: ["10", "20", "50", "100"] }}
                scroll={{ x: 1000 }}
              />
            ),
          },
        ]}
      />

      <Modal title="诊断详情" open={!!detail} width="min(980px, 92vw)" footer={null} onCancel={() => setDetail(null)}>
        {detail && (
          <Space direction="vertical" size={12} className="full-width">
            <Descriptions bordered size="small" column={2}>
              <Descriptions.Item label="规则">{detail.rule_name}</Descriptions.Item>
              <Descriptions.Item label="级别">{statusTag(detail.severity)}</Descriptions.Item>
              <Descriptions.Item label="对象类型">{detail.object_type || "-"}</Descriptions.Item>
              <Descriptions.Item label="影响对象">{detail.object_name || "-"}</Descriptions.Item>
              <Descriptions.Item label="置信度">{detail.confidence}%</Descriptions.Item>
              <Descriptions.Item label="分类">{categoryText[detail.category] || detail.category || "-"}</Descriptions.Item>
            </Descriptions>
            <Alert type={detail.severity === "critical" ? "error" : detail.severity === "warning" ? "warning" : "success"} showIcon message="根因分析" description={detail.root_cause} />
            <Alert type="info" showIcon message="业务影响" description={detail.impact} />
            <Alert type="success" showIcon message="处理建议" description={detail.suggestion} />
            <div>
              <Text strong>证据链</Text>
              <Space direction="vertical" size={4} style={{ width: "100%", marginTop: 8 }}>
                {(detail.evidence || []).map((item) => <Text key={stableValueKey("diagnosis-evidence", item)}>- {item}</Text>)}
              </Space>
            </div>
            <div>
              <Text strong>可生成动作</Text>
              <div style={{ marginTop: 8 }}>
                <Space wrap>
                  {(detail.actions || []).map((item) => <Tag color="blue" key={item}>{actionText[item] || item}</Tag>)}
                </Space>
              </div>
            </div>
          </Space>
        )}
      </Modal>

      <PlanOnlyScriptModal
        title={planModal.title}
        open={planModal.open}
        script={planModal.content}
        width="min(820px, 92vw)"
        onClose={() => setPlanModal({ open: false, title: "", content: "" })}
      />
    </Space>
  );
}
