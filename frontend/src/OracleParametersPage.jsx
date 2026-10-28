import React, { useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Empty,
  Input,
  Modal,
  Select,
  Space,
  Spin,
  Table,
  Tabs,
  Tag,
  message,
} from "antd";
import { CopyOutlined } from "@ant-design/icons";
import { oracleOpsApi } from "./apiClients";
import { copyText } from "./modules/clipboard";
import { useFirstAvailableInstanceId as useSyncedInstanceId } from "./OpsLayout";
import { stableRecordKey } from "./modules/stableKeys";

export function ParametersPage({ instances, selectedInstanceId }) {
  const oracleInstances = useMemo(() => instances.filter((item) => item.db_type === "oracle"), [instances]);
  const [instanceId] = useSyncedInstanceId(selectedInstanceId, oracleInstances);
  const [activeTab, setActiveTab] = useState("current");
  const [loading, setLoading] = useState(false);
  const [paramsData, setParamsData] = useState(null);
  const [keyword, setKeyword] = useState("");
  const [alterModal, setAlterModal] = useState({ open: false, param: "", value: "", scope: "both" });
  const [alterResult, setAlterResult] = useState(null);

  const loadCurrentParams = async (kw) => {
    if (!instanceId) return;
    setLoading(true);
    try {
      const data = await oracleOpsApi.parameters(instanceId, { keyword: kw || undefined });
      setParamsData(data);
    } catch (e) { message.error(e.message); }
    finally { setLoading(false); }
  };

  const loadSection = async (section) => {
    if (!instanceId) return;
    setLoading(true);
    try {
      const data = await oracleOpsApi.parameterSection(instanceId, section);
      setParamsData(data);
    } catch (e) { message.error(e.message); }
    finally { setLoading(false); }
  };

  useEffect(() => {
    if (!instanceId) return;
    if (activeTab === "current") loadCurrentParams(keyword);
    else if (activeTab === "sga") loadSection("parameters/sga");
    else if (activeTab === "pga") loadSection("parameters/pga");
    else if (activeTab === "process") loadSection("parameters/process");
    else if (activeTab === "session") loadSection("parameters/session");
    else if (activeTab === "password") loadSection("password-policy");
    else if (activeTab === "audit") loadSection("audit-config");
    else if (activeTab === "nls") loadSection("nls-parameters");
    else if (activeTab === "dblink") loadSection("dblinks");
    else if (activeTab === "datafile") loadSection("parameters/datafile");
    else if (activeTab === "suplog") loadSection("supplemental-log");
  }, [instanceId, activeTab]);

  const generateAlterSQL = async () => {
    if (!instanceId || !alterModal.param || !alterModal.value) return;
    try {
      const data = await oracleOpsApi.alterSystemScript(instanceId, {
        param_name: alterModal.param,
        value: alterModal.value,
        scope: alterModal.scope,
      });
      setAlterResult(data);
    } catch (e) { message.error(e.message); }
  };

  const tabs = [
    { key: "current", label: "当前参数" },
    { key: "sga", label: "SGA 内存" },
    { key: "pga", label: "PGA 内存" },
    { key: "process", label: "进程参数" },
    { key: "session", label: "会话参数" },
    { key: "password", label: "密码策略" },
    { key: "audit", label: "审计配置" },
    { key: "nls", label: "NLS 参数" },
    { key: "dblink", label: "DB Link" },
    { key: "datafile", label: "数据文件参数" },
    { key: "suplog", label: "补充日志" },
  ];

  const renderContent = () => {
    if (!paramsData) return <Empty description="选择实例后点击标签页查看" />;

    if (activeTab === "current" && paramsData.items) {
      return (
        <Table dataSource={paramsData.items} rowKey="name" size="small" pagination={{ defaultPageSize: 20 }}
          columns={[
            { title: "参数名", dataIndex: "name", width: 250, render: (t) => <a onClick={() => setAlterModal({ open: true, param: t, value: "", scope: "both" })}>{t}</a> },
            { title: "当前值", dataIndex: "value", width: 200 },
            { title: "描述", dataIndex: "description", ellipsis: true },
            { title: "默认", dataIndex: "isdefault", width: 60, render: (v) => <Tag color={v === "TRUE" ? "blue" : "orange"}>{v}</Tag> },
            { title: "可修改", dataIndex: "issys_modifiable", width: 80, render: (v) => <Tag color={v === "IMMEDIATE" ? "green" : v === "DEFERRED" ? "orange" : "red"}>{v}</Tag> },
          ]}
        />
      );
    }

    if (activeTab === "sga" || activeTab === "pga" || activeTab === "process" || activeTab === "session" || activeTab === "datafile" || activeTab === "audit") {
      const entries = Object.entries(paramsData).filter(([k, v]) => typeof v === "object" && v !== null && !Array.isArray(v) && v.value !== undefined);
      const arrays = Object.entries(paramsData).filter(([k, v]) => Array.isArray(v));
      return (
        <Space direction="vertical" className="full-width">
          {entries.length > 0 && (
            <Descriptions column={2} bordered size="small" title="参数值">
              {entries.map(([name, obj]) => (
                <Descriptions.Item key={name} label={name}>
                  <Space><span>{obj.value}</span>
                    {obj.issys_modifiable === "IMMEDIATE" && <Button size="small" type="link" onClick={() => setAlterModal({ open: true, param: name, value: obj.value, scope: "both" })}>修改</Button>}
                  </Space>
                </Descriptions.Item>
              ))}
            </Descriptions>
          )}
          {arrays.map(([name, arr]) => arr.length > 0 && (
            <Card key={name} title={name} size="small">
              <Table dataSource={arr} rowKey={(row) => stableRecordKey(`oracle-parameter-${name}`, row)} size="small" pagination={false} scroll={{ x: true }}
                columns={Object.keys(arr[0]).map((k) => ({ title: k, dataIndex: k, ellipsis: true }))} />
            </Card>
          ))}
        </Space>
      );
    }

    if (activeTab === "password" && paramsData.profiles) {
      const grouped = {};
      paramsData.profiles.forEach((p) => { if (!grouped[p.profile]) grouped[p.profile] = []; grouped[p.profile].push(p); });
      return Object.entries(grouped).map(([profile, items]) => (
        <Card key={profile} title={profile} size="small" style={{ marginBottom: 8 }}>
          <Descriptions column={2} bordered size="small">
            {items.map((item) => <Descriptions.Item key={item.resource_name} label={item.resource_name}>{item.limit}</Descriptions.Item>)}
          </Descriptions>
        </Card>
      ));
    }

    if (activeTab === "nls" && paramsData.database_parameters) {
      return (
        <Space direction="vertical" className="full-width">
          <Card title="Database NLS 参数" size="small">
            <Descriptions column={2} bordered size="small">
              {paramsData.database_parameters.map((p) => <Descriptions.Item key={p.parameter} label={p.parameter}>{p.value}</Descriptions.Item>)}
            </Descriptions>
          </Card>
          {paramsData.instance_parameters && paramsData.instance_parameters.length > 0 && (
            <Card title="Instance NLS 参数" size="small">
              <Descriptions column={2} bordered size="small">
                {paramsData.instance_parameters.map((p) => <Descriptions.Item key={p.parameter} label={p.parameter}>{p.value}</Descriptions.Item>)}
              </Descriptions>
            </Card>
          )}
        </Space>
      );
    }

    if (activeTab === "dblink" && paramsData.links) {
      return (
        <Table dataSource={paramsData.links} rowKey={(r) => `${r.owner}-${r.db_link}`} size="small"
          columns={[
            { title: "Owner", dataIndex: "owner" },
            { title: "DB Link", dataIndex: "db_link" },
            { title: "Username", dataIndex: "username" },
            { title: "Host", dataIndex: "host" },
            { title: "创建时间", dataIndex: "created" },
          ]}
        />
      );
    }

    if (activeTab === "suplog" && paramsData.supplemental_log) {
      const sl = paramsData.supplemental_log;
      return (
        <Descriptions column={2} bordered size="small" title="补充日志状态">
          {Object.entries(sl).map(([k, v]) => <Descriptions.Item key={k} label={k}><Tag color={v === "YES" ? "green" : "default"}>{v}</Tag></Descriptions.Item>)}
        </Descriptions>
      );
    }

    return <pre className="ops-output">{JSON.stringify(paramsData, null, 2)}</pre>;
  };

  return (
    <Space direction="vertical" size={16} className="full-width">
      <Card>
        <Space wrap>
          
          {activeTab === "current" && (
            <Input.Search placeholder="搜索参数名" value={keyword} onChange={(e) => setKeyword(e.target.value)}
              onSearch={(v) => { setKeyword(v); loadCurrentParams(v); }} style={{ width: 250 }} />
          )}
        </Space>
      </Card>
      <Card>
        <Tabs activeKey={activeTab} onChange={setActiveTab} items={tabs.map((t) => ({ key: t.key, label: t.label }))} />
        <Spin spinning={loading}>{renderContent()}</Spin>
      </Card>

      <Modal title="生成参数修改 SQL" open={alterModal.open} width={700} onCancel={() => { setAlterModal({ open: false, param: "", value: "", scope: "both" }); setAlterResult(null); }}
        footer={[
          <Button key="gen" type="primary" onClick={generateAlterSQL}>生成 SQL</Button>,
          alterResult && <Button key="copy" icon={<CopyOutlined />} onClick={() => copyText(alterResult.sql).then(() => message.success("已复制")).catch(() => message.error("复制失败"))}>复制 SQL</Button>,
          <Button key="close" onClick={() => { setAlterModal({ open: false, param: "", value: "", scope: "both" }); setAlterResult(null); }}>关闭</Button>,
        ]}
      >
        <Descriptions column={1} size="small" bordered>
          <Descriptions.Item label="参数名">{alterModal.param}</Descriptions.Item>
          <Descriptions.Item label="新值"><Input value={alterModal.value} onChange={(e) => setAlterModal({ ...alterModal, value: e.target.value })} /></Descriptions.Item>
          <Descriptions.Item label="SCOPE">
            <Select value={alterModal.scope} onChange={(v) => setAlterModal({ ...alterModal, scope: v })}
              options={[{ value: "both", label: "BOTH (推荐)" }, { value: "spfile", label: "SPFILE (重启生效)" }, { value: "memory", label: "MEMORY (重启失效)" }]} />
          </Descriptions.Item>
        </Descriptions>
        {alterResult && (
          <div style={{ marginTop: 12 }}>
            {alterResult.warnings && <Alert type="warning" showIcon style={{ marginBottom: 8 }} description={<ul style={{ margin: 0, paddingLeft: 20 }}>{alterResult.warnings.map((w) => <li key={w}>{w}</li>)}</ul>} />}
            <pre className="ops-output">{alterResult.sql}</pre>
          </div>
        )}
      </Modal>
    </Space>
  );
}

