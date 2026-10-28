import React, { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  InputNumber,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from "antd";
import {
  PlayCircleOutlined,
  ReloadOutlined,
} from "@ant-design/icons";
import {
  getSchedulerStatus,
  getSchedulerTasks,
  runSchedulerTaskNow,
  updateSchedulerTask,
} from "./modules/schedulerApi";

const { Text } = Typography;
const TYPE_COLOR = { capacity: "blue", awr: "purple", perf_baseline: "green" };

export function SchedulePage() {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(false);
  const [status, setStatus] = useState(null);
  const [edits, setEdits] = useState({});

  const load = async () => {
    setLoading(true);
    try {
      const [tasks, schedulerStatus] = await Promise.all([
        getSchedulerTasks(),
        getSchedulerStatus().catch(() => null),
      ]);
      setItems(tasks.items || []);
      setStatus(schedulerStatus);
    } catch (e) {
      message.error(e.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  const rowKey = (record) => `${record.task_type}:${record.instance_id}`;

  const saveInterval = async (record) => {
    const key = rowKey(record);
    const interval = edits[key];
    try {
      await updateSchedulerTask(record.task_type, record.instance_id, {
        interval_minutes: interval ?? record.interval_minutes,
      });
      setEdits((current) => {
        const next = { ...current };
        delete next[key];
        return next;
      });
      message.success("已保存");
      await load();
    } catch (e) {
      message.error(e.message);
    }
  };

  const toggle = async (record) => {
    try {
      await updateSchedulerTask(record.task_type, record.instance_id, {
        enabled: record.enabled ? 0 : 1,
      });
      message.success(record.enabled ? "已停用" : "已启用");
      await load();
    } catch (e) {
      message.error(e.message);
    }
  };

  const runNow = async (record) => {
    setLoading(true);
    try {
      const result = await runSchedulerTaskNow(record.task_type, record.instance_id);
      (result.status === "error" ? message.warning : message.success)(result.message || "已执行");
      await load();
    } catch (e) {
      message.error(e.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <Space direction="vertical" size={16} className="full-width">
      <Alert
        type="info"
        showIcon
        message="统一定时任务：所有按时间自动执行的后台作业（容量巡检 / AWR 自动采集 / 性能基线采集）集中在此，可开关、调整间隔、立即执行。"
        description={status ? `调度器：${status.running ? "运行中" : "未运行"}` : null}
      />
      <Card>
        <Space wrap>
          <Button type="primary" icon={<ReloadOutlined />} loading={loading} onClick={load}>刷新</Button>
        </Space>
      </Card>
      <Card size="small">
        <Table
          dataSource={items}
          rowKey={rowKey}
          size="small"
          loading={loading}
          pagination={{ pageSize: 15 }}
          scroll={{ x: 1100 }}
          columns={[
            { title: "任务类型", dataIndex: "task_label", width: 130, render: (value, record) => <Tag color={TYPE_COLOR[record.task_type]}>{value}</Tag> },
            { title: "实例", dataIndex: "instance_name", ellipsis: true, render: (value, record) => <span>{value} <Text type="secondary" style={{ fontSize: 12 }}>{record.host || ""}</Text></span> },
            { title: "类型", dataIndex: "db_type", width: 70, render: (value) => <Tag>{(value || "").toUpperCase()}</Tag> },
            { title: "状态", dataIndex: "enabled", width: 80, render: (value) => value ? <Tag color="green">已启用</Tag> : <Tag>未启用</Tag> },
            { title: "间隔(分钟)", width: 120, render: (_, record) => <InputNumber min={1} max={20160} size="small" value={edits[rowKey(record)] ?? record.interval_minutes} onChange={(value) => setEdits((current) => ({ ...current, [rowKey(record)]: value }))} /> },
            { title: "上次运行", dataIndex: "last_run_time", width: 160, render: (value) => value || "-" },
            { title: "上次结果", dataIndex: "last_status", width: 90, render: (value, record) => value ? <Tag color={value === "success" ? "green" : value === "error" ? "red" : "orange"} title={record.last_message}>{value}</Tag> : "-" },
            { title: "下次运行", dataIndex: "next_run_time", width: 160, render: (value) => value || "-" },
            { title: "操作", width: 260, render: (_, record) => <Space size={4} wrap>
              <Button size="small" onClick={() => saveInterval(record)}>保存间隔</Button>
              {record.enabled
                ? <Button size="small" danger onClick={() => toggle(record)}>停用</Button>
                : <Button size="small" type="primary" onClick={() => toggle(record)}>启用</Button>}
              <Button size="small" icon={<PlayCircleOutlined />} onClick={() => runNow(record)}>立即执行</Button>
            </Space> },
          ]}
        />
      </Card>
    </Space>
  );
}
