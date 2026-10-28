import React, { useMemo, useState } from "react";
import { Alert, Button, Card, Col, Empty, Modal, Row, Space, Statistic, Table, Tag, message } from "antd";
import { AlertOutlined, SearchOutlined } from "@ant-design/icons";
import { oracleOpsApi } from "./apiClients";
import { useFirstAvailableInstanceId as useSyncedInstanceId } from "./OpsLayout";

export function LockSessionPage({ instances, selectedInstanceId }) {
  const oracleInstances = useMemo(() => instances.filter((item) => item.db_type === "oracle"), [instances]);
  const [instanceId] = useSyncedInstanceId(selectedInstanceId, oracleInstances);
  const [loading, setLoading] = useState(false);
  const [data, setData] = useState(null);
  const [blockingData, setBlockingData] = useState(null);
  const [blockingLoading, setBlockingLoading] = useState(false);
  const [activeTab, setActiveTab] = useState("sessions");

  const loadData = async () => {
    if (!instanceId) return;
    setLoading(true);
    try { setData(await oracleOpsApi.lockedObjects(instanceId)); } catch (e) { message.error(e.message); }
    finally { setLoading(false); }
  };

  const loadBlocking = async () => {
    if (!instanceId) return;
    setBlockingLoading(true);
    try { setBlockingData(await oracleOpsApi.blockingTree(instanceId)); } catch (e) { message.error(e.message); }
    finally { setBlockingLoading(false); }
  };

  const killScript = (sid, serial) => {
    const sql = `ALTER SYSTEM KILL SESSION '${sid},${serial}' IMMEDIATE;`;
    Modal.info({ title: "Kill Session 脚本", width: 600, content: <div><Alert type="warning" showIcon style={{ marginBottom: 8 }} message="Kill Session 会强制终止会话，未提交事务将回滚。" /><pre className="ops-output">{sql}</pre></div> });
  };

  const osKillScript = async (row) => {
    try {
      const data = await oracleOpsApi.osKillScript(instanceId, { spid: row.spid, sid: row.sid, serial: row.serial_num });
      Modal.info({
        title: "OS kill -9 方案",
        width: 720,
        content: (
          <div>
            <Alert type="error" showIcon style={{ marginBottom: 8 }} message="kill -9 会强制终止 Oracle server process，请先确认 SID/Serial/SPID、业务影响和事务回滚风险。" />
            {(data.warnings || []).map((item) => <Alert key={item} type="warning" showIcon style={{ marginBottom: 8 }} message={item} />)}
            <pre className="ops-output">{data.sql || data.script}</pre>
          </div>
        ),
      });
    } catch (e) {
      message.error(e.message);
    }
  };

  const formatWait = (seconds) => {
    if (!seconds) return "-";
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    return h > 0 ? `${h}h ${m}m ${s}s` : m > 0 ? `${m}m ${s}s` : `${s}s`;
  };

  return (
    <Space direction="vertical" size={16} className="full-width">
      <Card>
        <Space wrap>
          
          <Button type="primary" icon={<SearchOutlined />} loading={loading} onClick={loadData}>查询会话</Button>
          <Button icon={<AlertOutlined />} loading={blockingLoading} onClick={loadBlocking} style={{ borderColor: "#fa541c", color: "#fa541c" }}>阻塞查询</Button>
        </Space>
      </Card>

      {blockingData && (
        <>
          <Row gutter={[16, 16]}>
            <Col xs={6}><Card size="small"><Statistic title="阻塞源" value={blockingData.summary.blocker_count} valueStyle={{ color: "#cf1322" }} suffix="个" /></Card></Col>
            <Col xs={6}><Card size="small"><Statistic title="被阻塞" value={blockingData.summary.waiter_count} valueStyle={{ color: "#fa8c16" }} suffix="个" /></Card></Col>
            <Col xs={6}><Card size="small"><Statistic title="涉及会话" value={blockingData.summary.total} suffix="个" /></Card></Col>
            <Col xs={6}><Card size="small"><Statistic title="最长等待" value={formatWait(blockingData.summary.max_wait_seconds)} /></Card></Col>
          </Row>

          {blockingData.tree.length > 0 ? (
            <Card title="阻塞树（BLOCKING TREE）" size="small">
              <Table dataSource={blockingData.tree} rowKey="sid" size="small" pagination={false} scroll={{ x: true }}
                columns={[
                  { title: "阻塞树", dataIndex: "tree", width: 400,
                    render: (v, r) => (
                      <div style={{ fontFamily: "monospace", fontSize: 12, whiteSpace: "pre",
                        color: r.is_blocker ? "#cf1322" : "#fa8c16", fontWeight: r.is_blocker ? 600 : 400 }}>
                        {v}
                      </div>
                    )
                  },
                  { title: "用户", dataIndex: "username", width: 90 },
                  { title: "状态", dataIndex: "status", width: 70, render: (v) => <Tag color={v === "ACTIVE" ? "green" : "default"}>{v}</Tag> },
                  { title: "机器", dataIndex: "machine", width: 130, ellipsis: true },
                  { title: "程序", dataIndex: "program", width: 130, ellipsis: true },
                  { title: "等待事件", dataIndex: "event", width: 200, ellipsis: true },
                  { title: "等待时间", dataIndex: "wait_s", width: 90, render: (v) => <span style={{ color: v > 300 ? "#cf1322" : "#fa8c16" }}>{formatWait(v)}</span> },
                  { title: "SQL ID", dataIndex: "sql_id", width: 110 },
                  { title: "角色", dataIndex: "is_blocker", width: 80,
                    render: (v, r) => v ? <Tag color="red">阻塞源(${r.waiter_count})</Tag> : <Tag color="orange">等待</Tag>
                  },
                  { title: "Kill SQL", dataIndex: "kill_sql", width: 400,
                    render: (v) => <code style={{ fontSize: 11, color: "#cf1322" }}>{v}</code>
                  },
                  { title: "操作", width: 160, render: (_, r) => <Space size={4}><Button size="small" danger onClick={() => killScript(r.sid, r.serial_num)}>Kill</Button><Button size="small" danger disabled={!r.spid} onClick={() => osKillScript(r)}>kill -9</Button></Space> },
                ]}
              />
            </Card>
          ) : (
            <Card><Empty description="当前无阻塞会话" image={Empty.PRESENTED_IMAGE_SIMPLE} /></Card>
          )}

          {blockingData.lock_pairs.length > 0 && (
            <Card title="锁冲突详情（LOCK CONFLICT）" size="small">
              <Table dataSource={blockingData.lock_pairs} rowKey={(r) => `${r.blocker_sid}-${r.waiter_sid}-${r.lock_type}`} size="small" pagination={false}
                columns={[
                  { title: "阻塞源 SID", dataIndex: "blocker_sid", width: 100 },
                  { title: "持有模式", dataIndex: "blocker_mode_name", width: 100, render: (v) => <Tag color="red">{v}</Tag> },
                  { title: "等待 SID", dataIndex: "waiter_sid", width: 100 },
                  { title: "请求模式", dataIndex: "waiter_request_name", width: 100, render: (v) => <Tag color="orange">{v}</Tag> },
                  { title: "锁类型", dataIndex: "lock_type", width: 80 },
                  { title: "ID1", dataIndex: "id1", width: 80 },
                  { title: "ID2", dataIndex: "id2", width: 80 },
                ]}
              />
            </Card>
          )}
        </>
      )}

      {data && <>
        <Card title={`活跃会话 (${data.sessions.length})`}>
          <Table dataSource={data.sessions} rowKey="sid" size="small" pagination={{ defaultPageSize: 15 }} scroll={{ x: true }}
            columns={[
              { title: "SID", dataIndex: "sid", width: 70 }, { title: "Serial#", dataIndex: "serial_num", width: 80 },
              { title: "用户名", dataIndex: "username" }, { title: "状态", dataIndex: "status", render: (v) => <Tag color={v === "ACTIVE" ? "green" : "default"}>{v}</Tag> },
              { title: "机器", dataIndex: "machine", ellipsis: true }, { title: "程序", dataIndex: "program", ellipsis: true },
              { title: "等待秒", dataIndex: "seconds_in_wait", width: 80 },
              { title: "阻塞会话", dataIndex: "blocking_session", width: 90, render: (v) => v ? <Tag color="red">{v}</Tag> : "-" },
              { title: "SPID", dataIndex: "spid", width: 90 },
              { title: "操作", width: 160, render: (_, r) => <Space size={4}><Button size="small" danger onClick={() => killScript(r.sid, r.serial_num)}>Kill</Button><Button size="small" danger disabled={!r.spid} onClick={() => osKillScript(r)}>kill -9</Button></Space> },
            ]} />
        </Card>
        {data.locked_objects.length > 0 && (
          <Card title={`锁对象(${data.locked_objects.length})`}>
            <Table dataSource={data.locked_objects} rowKey={(r) => `${r.session_id}-${r.object_name}`} size="small"
              columns={[{ title: "会话ID", dataIndex: "session_id" }, { title: "用户", dataIndex: "oracle_username" }, { title: "对象", dataIndex: "object_name" }, { title: "类型", dataIndex: "object_type" }, { title: "锁模式", dataIndex: "locked_mode" }]} />
          </Card>
        )}
      </>}
    </Space>
  );
}
