import React, { useEffect, useRef, useState } from "react";
import { Alert, Button, Empty, Input, Select, Space, Table, Typography } from "antd";
import { HistoryOutlined, ReloadOutlined } from "@ant-design/icons";
import { AssistantControlPanel } from "./AssistantControlPanel";
import { getAssistantControlBatch, listAssistantControlBatches } from "./modules/assistantControlApi";
import { isAssistantBatchId } from "./modules/assistantBatchApi";
import { getAuthSession, isAuthSessionCurrent } from "./modules/authStorage";

export function AssistantBatchControlView({ initialBatchId = "", isActive = () => true }) {
  const session = getAuthSession();
  const [batchId, setBatchId] = useState(initialBatchId);
  const [saved, setSaved] = useState(null);
  const [executionId, setExecutionId] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [historySaved, setHistorySaved] = useState(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState("");
  const [selectionVersion, setSelectionVersion] = useState(0);
  const epoch = useRef(0);
  const historyEpoch = useRef(0);
  const mounted = useRef(true);
  const activity = useRef(isActive), draft = useRef(initialBatchId), detailPending = useRef(null), historyPending = useRef(null);
  const detailValue = useRef(null), selectedExecution = useRef(null), selectionEpoch = useRef(0), contextSession = useRef(session);
  activity.current = isActive;
  const active = () => {
    try { return typeof activity.current === "function" && activity.current() === true; }
    catch { return false; }
  };
  const batch = saved && active() && isAuthSessionCurrent(saved.session) ? saved.value : null;
  const history = historySaved && active() && isAuthSessionCurrent(historySaved.session) ? historySaved.value : null;
  const clearSelection = () => {
    selectedExecution.current = null; selectionEpoch.current += 1; setExecutionId(null); setSelectionVersion(selectionEpoch.current);
  };
  const selectExecution = (id) => {
    const entry = detailValue.current;
    if (!active() || !mounted.current || !isAuthSessionCurrent(session) || !entry || !isAuthSessionCurrent(entry.session)
      || !entry.value.items.some((item) => item.execution_id === id)) return;
    selectedExecution.current = id; selectionEpoch.current += 1; setExecutionId(id); setSelectionVersion(selectionEpoch.current);
  };
  const panelActive = () => active() && mounted.current && saved && isAuthSessionCurrent(saved.session)
    && selectedExecution.current === executionId && selectionEpoch.current === selectionVersion;
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; epoch.current += 1; historyEpoch.current += 1; detailPending.current = null; historyPending.current = null; };
  }, []);
  useEffect(() => {
    epoch.current += 1; historyEpoch.current += 1; detailPending.current = null; historyPending.current = null; detailValue.current = null;
    setSaved(null); clearSelection(); setLoading(false); setError(""); setHistorySaved(null); setHistoryLoading(false); setHistoryError("");
    if (!isAuthSessionCurrent(contextSession.current)) { contextSession.current = getAuthSession(); draft.current = ""; setBatchId(""); }
  }, [session.token, session.generation, session.loginEpoch]);
  const changeBatch = (value) => {
    if (!active() || !mounted.current || !isAuthSessionCurrent(session)) return;
    epoch.current += 1; detailPending.current = null; detailValue.current = null; draft.current = value;
    setBatchId(value); setSaved(null); clearSelection(); setLoading(false); setError("");
  };
  const read = async (requestedId = draft.current) => {
    if (!active() || !mounted.current || !isAuthSessionCurrent(session) || !isAssistantBatchId(requestedId) || detailPending.current) return;
    const ticket = ++epoch.current, auth = getAuthSession(), op = { ticket };
    detailPending.current = op; detailValue.current = null; draft.current = requestedId; setBatchId(requestedId);
    let timer;
    setLoading(true); setError(""); setSaved(null); clearSelection();
    const current = () => mounted.current && active() && ticket === epoch.current && detailPending.current === op && isAuthSessionCurrent(auth);
    try {
      const value = await Promise.race([getAssistantControlBatch(requestedId, { authSession: auth }), new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error("Control batch read timed out")), 90000);
      })]);
      if (!current()) return;
      detailValue.current = { value, session: auth }; setSaved(detailValue.current);
      if (value.items.length === 1) selectExecution(value.items[0].execution_id);
    } catch {
      if (current()) setError("控制批次读取失败；未推断为空、停止或成功，不会改用旧版或自动重试。");
    } finally { clearTimeout(timer); if (current()) { detailPending.current = null; setLoading(false); } }
  };
  // No mount effect calls this path. Only the history button or pagination does.
  const loadHistory = async (page = history?.page ?? 1, pageSize = history?.page_size ?? 10) => {
    if (!active() || !mounted.current || !isAuthSessionCurrent(session)
      || !Number.isSafeInteger(page) || page < 1 || page > 1000000 || !Number.isSafeInteger(pageSize) || pageSize < 1 || pageSize > 100) return;
    if (historyPending.current?.page === page && historyPending.current?.pageSize === pageSize) return;
    const ticket = ++historyEpoch.current, auth = getAuthSession(), op = { page, pageSize, ticket };
    historyPending.current = op; setHistoryLoading(true); setHistoryError("");
    let timer;
    const current = () => mounted.current && active() && ticket === historyEpoch.current && historyPending.current === op && isAuthSessionCurrent(auth);
    try {
      const value = await Promise.race([listAssistantControlBatches({ page, page_size: pageSize }, { authSession: auth }), new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error("Control history read timed out")), 90000);
      })]);
      if (current()) setHistorySaved({ value, session: auth });
    } catch (failure) {
      if (current()) {
        setHistorySaved(null);
        setHistoryError([401, 403].includes(failure?.status) ? "当前会话或 v2 历史读取权限被服务端拒绝；不会自动重试。"
          : "v2 控制历史读取失败；未推断为空、停止或成功，不会改用旧版或自动重试。");
      }
    } finally { clearTimeout(timer); if (current()) { historyPending.current = null; setHistoryLoading(false); } }
  };
  const changeHistoryPage = (page, size) => loadHistory(size === history?.page_size ? page : 1, size);
  return <div className="assistant-batch-section">
    <Space.Compact className="assistant-batch-id">
      <Input aria-label="控制批次号" value={batchId} maxLength={30} placeholder="CB + 28 位小写十六进制字符" onChange={(event) => changeBatch(event.target.value)} />
      <Button icon={<ReloadOutlined />} loading={loading} disabled={!isAssistantBatchId(batchId)} onClick={() => read()}>读取控制批次</Button>
    </Space.Compact>
    <Button icon={<HistoryOutlined />} loading={historyLoading} disabled={historyLoading} onClick={() => loadHistory()}>读取 v2 历史</Button>
    {historyError && <Alert type="error" showIcon message={historyError} />}
    {historyLoading && <Typography.Text type="secondary">正在读取 v2 控制历史</Typography.Text>}
    {history && <Table aria-label="v2 控制批次历史" size="small" rowKey="batch_id" dataSource={history.items} loading={historyLoading} scroll={{ x: 950 }}
      pagination={{ current: history.page, pageSize: history.page_size, total: history.total, showSizeChanger: true,
        pageSizeOptions: [10, 20, 50, 100], disabled: historyLoading, onChange: changeHistoryPage }} columns={[
        { title: "批次号", dataIndex: "batch_id", width: 270, render: (value) => <Typography.Text code>{value}</Typography.Text> },
        { title: "状态 / 结果", key: "status", width: 210, render: (_, row) => `${row.status} · ${row.outcome || "未结算"}` },
        { title: "已结算计划", key: "plans", width: 100, render: (_, row) => `${row.settled_plans} / ${row.total}` },
        { title: "投递计数", dataIndex: "queue_counts", width: 210, render: (counts) => Object.entries(counts).map(([state, count]) => `${state}: ${count}`).join(" · ") },
        { title: "创建（UTC）", dataIndex: "created_at", width: 190, render: (value) => value.slice(0, 19).replace("T", " ") },
        { title: "控制详情", key: "select", width: 130, render: (_, row) => <Button type="link" icon={<ReloadOutlined />} disabled={loading || historyLoading} onClick={() => read(row.batch_id)}>选择批次</Button> },
      ]} />}
    {error && <Alert type="error" showIcon message={error} />}
    {batch ? <>
      <Typography.Text code>{batch.batch_id}</Typography.Text>
      <label className="assistant-batch-label">选择执行成员
        <Select aria-label="控制执行成员" value={executionId} onChange={selectExecution} options={batch.items.map((item) => ({
          value: item.execution_id, label: `${item.seq} · ${item.plan_id} · ${item.queue_state}`,
        }))} />
      </label>
      {executionId !== null && <AssistantControlPanel key={`${executionId}:${selectionVersion}`} executionId={executionId} isActive={panelActive} />}
    </> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={loading ? "正在读取控制批次" : "尚未选择控制批次"} />}
  </div>;
}
