import React, { useEffect, useRef, useState } from "react";
import { Alert, Button, Checkbox, Descriptions, Empty, List, Space, Steps, Tag, Typography } from "antd";
import { ReloadOutlined, RobotOutlined } from "@ant-design/icons";
import { analyzeOracleSql, getSlowSqlAiCapability, listSlowSqlAiReports } from "./modules/inspectionApi";

export function SlowSqlAIAnalysisPanel({ instanceId, row }) {
  const [provider, setProvider] = useState(null);
  const [approved, setApproved] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [reports, setReports] = useState([]);
  const [result, setResult] = useState(null);
  const request = useRef(null);
  const alive = useRef(false);
  const generation = useRef(0);
  const approvedRevision = useRef(null);

  const refresh = async () => {
    const token = ++generation.current;
    try {
      const [config, history] = await Promise.all([getSlowSqlAiCapability(), listSlowSqlAiReports(instanceId, row.sql_id)]);
      if (!alive.current || token !== generation.current) return;
      if (approvedRevision.current !== config.provider_revision) setApproved(false);
      setProvider(config);
      setReports(history.items || []);
    } catch (reason) {
      if (alive.current && token === generation.current) setError(reason.message || "分析记录加载失败");
    }
  };
  useEffect(() => {
    alive.current = true;
    refresh();
    return () => { alive.current = false; generation.current += 1; request.current?.abort(); };
  }, [instanceId, row.sql_id]);

  const analyze = async () => {
    const controller = new window.AbortController();
    request.current = controller;
    setLoading(true);
    setError("");
    setResult(null);
    const timer = setTimeout(() => controller.abort(), 200_000);
    try {
      const response = await analyzeOracleSql(instanceId, {
        sql_id: row.sql_id, child_number: row.child_number, con_id: row.con_id,
        plan_hash_value: row.plan_hash_value, provider_config_id: provider.provider_config_id,
        provider_revision: provider.provider_revision,
        allow_model_context: approved,
      }, { signal: controller.signal });
      if (alive.current) setResult(response);
    } catch (reason) {
      if (alive.current) setError(reason.name === "AbortError"
        ? "请求超时，后端可能仍在运行；请查看分析记录，勿重复提交。" : reason.message || "AI 分析失败");
    } finally {
      clearTimeout(timer);
      if (alive.current) { setLoading(false); await refresh(); }
    }
  };
  const identityComplete = [row.child_number, row.con_id, row.plan_hash_value].every((value) => Number.isInteger(value) && value >= 0);
  const conclusion = result?.model_result;
  return <Space direction="vertical" size={12} className="full-width">
    <Space wrap>
      <Typography.Text>模型：{provider?.configured ? `${provider.model_name} (${provider.host})` : provider ? "未配置" : "读取中"}</Typography.Text>
      <Button icon={<ReloadOutlined />} disabled={loading} onClick={refresh}>刷新记录</Button>
    </Space>
    {provider && !provider.configured && <Alert type="warning" showIcon message={provider.message} />}
    {!identityComplete && <Alert type="warning" showIcon message="本条记录缺少子游标、容器或计划身份，无法发起精确分析" />}
    <Checkbox checked={approved} disabled={loading} onChange={(event) => {
      approvedRevision.current = event.target.checked ? provider?.provider_revision : null;
      setApproved(event.target.checked);
    }}>
      允许将本条 SQL、执行计划及采样指标发送给上述模型
    </Checkbox>
    <Button type="primary" icon={<RobotOutlined />} disabled={!approved || !provider?.configured || !identityComplete}
      loading={loading} onClick={analyze}>开始 AI 分析</Button>
    {(loading || result) && <Steps size="small" current={loading ? 1 : result?.persisted ? 3 : 2}
      status={result?.status === "failed" ? "error" : "process"}
      items={[{ title: "确认模型" }, { title: "读取证据与分析" }, { title: "保存报告" }]} />}
    {error && <Alert type="error" showIcon message={error} />}
    {result && <>
      <Descriptions bordered size="small" column={1} items={[
        { key: "id", label: "分析报告", children: `#${result.report_id}` },
        { key: "status", label: "状态", children: result.status === "failed" ? "分析失败" : result.status === "partial" ? "已保存，证据不完整" : "分析已保存" },
        { key: "model", label: "模型", children: result.provider?.model },
        { key: "identity", label: "证据身份", children: `${result.evidence?.sql_id} / child ${result.evidence?.child_number ?? "未知"} / con ${result.evidence?.con_id ?? "未知"} / plan ${result.evidence?.plan_hash_value ?? "未知"}` },
        { key: "time", label: "完成时间", children: result.finished_at ? new Date(result.finished_at).toLocaleString() : "-" },
      ]} />
      {result.error && <Alert type="error" showIcon message={result.error} />}
      {conclusion && <>
        <Alert type="info" showIcon message="模型建议待验证，未执行数据库变更" />
        <Typography.Title level={5}>分析结论</Typography.Title>
        <Typography.Paragraph>{conclusion.summary}</Typography.Paragraph>
        {(conclusion.hypotheses || []).map((item, index) => <div key={`${index}:${item.cause}`}>
          <Typography.Text strong>待验证假设 {index + 1}</Typography.Text>
          <Typography.Paragraph>{item.cause}</Typography.Paragraph>
          <Space>{item.evidence_ids.map((id) => <Tag key={id}>{id}</Tag>)}</Space>
          <Typography.Paragraph>{item.validation}</Typography.Paragraph>
        </div>)}
        <Typography.Title level={5}>建议与缺失证据</Typography.Title>
        {[...(conclusion.recommendations || []), ...(conclusion.missing_evidence || [])].map((item, index) => <Typography.Paragraph key={`${index}:${item}`}>{item}</Typography.Paragraph>)}
      </>}
    </>}
    <Typography.Title level={5}>已保存的分析记录</Typography.Title>
    <List size="small" dataSource={reports} locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无分析记录" /> }}
      renderItem={(item) => <List.Item><Button type="link" onClick={() => setResult(item)}>
        #{item.report_id} · {item.status === "failed" ? "失败" : item.status === "partial" ? "证据不完整" : "已保存"} · {new Date(item.finished_at).toLocaleString()}
      </Button></List.Item>} />
  </Space>;
}
