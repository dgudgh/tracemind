import React, { useEffect, useState } from "react";
import { Alert, Button, Col, Divider, Form, Input, InputNumber, Modal, Row, Segmented, Select, Switch, Table, Tag, message } from "antd";
import { CheckCircleOutlined, CodeOutlined, DatabaseOutlined, SafetyCertificateOutlined } from "@ant-design/icons";
import { connectionSecurityApi, instanceApi } from "./apiClients";
import { ConnectionSecurityModal } from "./ConnectionSecurityModal";
import { readEngineCatalog } from "./apiClients";
import { engineCatalogReason } from "./modules/engineCatalog";
import { getAuthSession, isAuthSessionCurrent } from "./modules/authStorage";
import { catalogEpoch } from "./modules/catalogEpoch";

export function InstanceDrawer({ open, editing, engine = "oracle", onClose, onSaved }) {
  const [form] = Form.useForm();
  const [engineCatalog, setEngineCatalog] = useState(null);
  const [catalogError, setCatalogError] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [rewrapping, setRewrapping] = useState(false);
  const [auditOpen, setAuditOpen] = useState(false);
  const [auditLoading, setAuditLoading] = useState(false);
  const [audits, setAudits] = useState([]);
  const [certificateProfiles, setCertificateProfiles] = useState([]);
  const [certificateManagerOpen, setCertificateManagerOpen] = useState(false);
  const dbType = Form.useWatch("db_type", form);
  const credentialMode = Form.useWatch("credential_mode", form) || "local";
  const sshCredentialMode = Form.useWatch("ssh_credential_mode", form) || "local";
  const accountTypeOptions = dbType === "oracle"
    ? [{ value: "system", label: "System / DBA" }, { value: "sysdba", label: "SYS (SYSDBA)" }, { value: "business", label: "业务用户" }]
    : [{ value: "system", label: "System / DBA" }, { value: "business", label: "业务用户" }];

  useEffect(() => {
    if (!open) return;
    form.resetFields();
    form.setFieldsValue(
      editing ? {
        account_type: "system",
        credential_mode: ["vault", "environment"].includes(editing.credential_source) ? "external" : "local",
        ssh_credential_mode: ["vault", "environment"].includes(editing.ssh_credential_source) ? "external" : "local",
        ...editing,
      } : {
        db_type: "oracle",
        port: 1521,
        enabled: 1,
        account_type: "system",
        oracle_connect_type: "service_name",
        architecture_type: "standalone",
        role_type: "primary",
        tls_verify: 1,
        credential_mode: "local",
        ssh_credential_mode: "local",
      }
    );
  }, [editing, form, open, engine]);

  useEffect(() => {
    // Each refresh supersedes pending responses; no cross-session cache.
    const epoch = catalogEpoch();
    setEngineCatalog(null);
    setCatalogError(false);
    if (!open) return () => epoch.close();
    const refresh = () => {
      const current = epoch.next();
      const session = getAuthSession();
      readEngineCatalog(session).then((catalog) => {
        if (current() && isAuthSessionCurrent(session)) { setEngineCatalog(catalog); setCatalogError(false); }
      }).catch(() => {
        if (current() && isAuthSessionCurrent(session)) { setEngineCatalog(null); setCatalogError(true); }
      });
    };
    refresh();
    const timer = setInterval(refresh, 15000);
    return () => { clearInterval(timer); epoch.close(); };
  }, [open, editing?.id, engine]);

  const loadCertificateProfiles = async () => {
    try {
      setCertificateProfiles(await connectionSecurityApi.certificates());
    } catch {
      setCertificateProfiles([]);
    }
  };

  useEffect(() => {
    if (open) loadCertificateProfiles();
  }, [open]);

  useEffect(() => {
    if (dbType && dbType !== "oracle" && form.getFieldValue("account_type") === "sysdba") {
      form.setFieldsValue({ account_type: "system" });
    }
  }, [dbType, form]);

  const preparePayload = (values) => {
    const payload = { ...values };
    delete payload.credential_mode;
    delete payload.ssh_credential_mode;
    if (credentialMode === "local") delete payload.password_secret_ref;
    else delete payload.password;
    if (sshCredentialMode === "local") delete payload.ssh_password_secret_ref;
    else delete payload.ssh_password;
    if (payload.db_type !== "oracle") {
      if (payload.account_type === "sysdba") payload.account_type = "system";
      payload.oracle_connect_type = undefined;
      payload.service_name = undefined;
      payload.sid = undefined;
    }
    if (editing && !payload.password) delete payload.password;
    if (editing && !payload.password_secret_ref) delete payload.password_secret_ref;
    if (editing && !payload.ssh_password) delete payload.ssh_password;
    if (editing && !payload.ssh_password_secret_ref) delete payload.ssh_password_secret_ref;
    return payload;
  };

  const handleRewrap = async () => {
    if (!editing?.id) return;
    setRewrapping(true);
    try {
      await instanceApi.rotateCredential(editing.id, {
        credential_kind: "database",
        reason: "管理员将历史密文重新封装到当前主密钥",
      });
      message.success("数据库凭据已使用当前主密钥重新封装");
      await onSaved?.();
    } catch (error) {
      message.error(error.message || "凭据重新封装失败");
    } finally {
      setRewrapping(false);
    }
  };

  const handleOpenAudit = async () => {
    if (!editing?.id) return;
    setAuditOpen(true);
    setAuditLoading(true);
    try {
      setAudits(await instanceApi.credentialAudits(editing.id));
    } catch (error) {
      message.error(error.message || "凭据审计加载失败");
    } finally {
      setAuditLoading(false);
    }
  };

  const handleSave = async (values) => {
    setSaving(true);
    try {
      const payload = preparePayload(values);
      if (editing) {
        await instanceApi.update(editing.id, payload);
      } else {
        await instanceApi.create(payload);
      }
      message.success("保存成功");
      onSaved();
      onClose();
    } catch (e) {
      message.error(e.message || "保存失败");
    } finally {
      setSaving(false);
    }
  };

  const handleTest = async () => {
    let values;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    setTesting(true);
    try {
      const payload = preparePayload(values);
      const result = await instanceApi.testConfig({ ...payload, instance_id: editing?.id });
      if (result.ok) {
        Modal.success({
          title: "数据库连接测试通过",
          content: `${payload.host}:${payload.port}${result.detail?.version ? ` · ${result.detail.version}` : ""}`,
        });
      } else {
        Modal.error({ title: "数据库连接测试失败", content: result.detail?.error || result.message || "连接失败" });
      }
    } catch (error) {
      Modal.error({ title: "数据库连接测试失败", content: error.message || "连接失败" });
    } finally {
      setTesting(false);
    }
  };

  return (
    <Modal
      title={null}
      open={open}
      onCancel={onClose}
      width={720}
      footer={null}
      destroyOnClose
      centered
      styles={{ body: { padding: 0, maxHeight: "calc(100vh - 96px)", overflowY: "auto" } }}
    >
      <div style={{ padding: "24px 28px 8px", borderBottom: "1px solid #f0f0f0" }}>
        <div style={{ fontSize: 18, fontWeight: 600 }}>{editing ? "编辑数据库连接" : "新增数据库连接"}</div>
        <div style={{ fontSize: 13, color: "#94a3b8", marginTop: 4 }}>配置数据库连接和 SSH 服务器信息</div>
      </div>
      <Form form={form} layout="vertical" onFinish={handleSave} style={{ padding: "20px 28px" }}>
        <div style={{ fontSize: 14, fontWeight: 600, color: "#0d9488", marginBottom: 12, display: "flex", alignItems: "center", gap: 6 }}>
          <DatabaseOutlined /> 数据库连接
        </div>
        {catalogError && <Alert type="warning" showIcon message="引擎扩展目录暂不可用" description="现有连接表单仍可使用；扩展运行状态未确认。" style={{ marginBottom: 12 }} />}
        {engineCatalog && <Alert type="info" showIcon style={{ marginBottom: 12 }}
          message={`扩展目录版本 ${engineCatalog.catalog_generation} · ${engineCatalogReason(engineCatalog.engines.find((item) => item.engine_id === dbType))}`}
          description="此状态仅表示扩展运行时；现有连接表单仍使用原有路径。目录尚未提供连接字段合同。" />}
        <Row gutter={16}>
          <Col span={12}>
            <Form.Item name="instance_name" label="连接名称" rules={[{ required: true, message: "请输入连接名称" }]}>
              <Input placeholder="例如：生产库-192.168.1.10" />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item name="db_type" label="数据库类型" rules={[{ required: true }]}>
              <Select
                options={[
                  { value: "oracle", label: "Oracle" },
                  { value: "mysql", label: "MySQL" },
                  { value: "postgresql", label: "PostgreSQL" },
                  { value: "sqlserver", label: "SQL Server" },
                  ...(engineCatalog?.engines || []).filter((item) => !["oracle", "mysql", "postgresql", "sqlserver"].includes(item.engine_id))
                    .map((item) => ({ value: item.engine_id, label: `${item.engine_id} · ${engineCatalogReason(item)} · 连接字段未接入`, disabled: true })),
                ]}
                placeholder="选择数据库类型"
                onChange={(value) => {
                  const defaults = {
                    oracle: { port: 1521, oracle_connect_type: "service_name", account_type: "system" },
                    mysql: { port: 3306, account_type: "system", oracle_connect_type: undefined, service_name: undefined, sid: undefined },
                    postgresql: { port: 5432, sslmode: "prefer", account_type: "system", oracle_connect_type: undefined, service_name: undefined, sid: undefined },
                    sqlserver: { port: 1433, database_name: "master", schema_name: "dbo", encrypt: "yes", account_type: "system", oracle_connect_type: undefined, service_name: undefined, sid: undefined },
                  };
                  form.setFieldsValue(defaults[value] || {});
                }}
              />
            </Form.Item>
          </Col>
          <Col span={8}>
            <Form.Item name="db_version" label="数据库版本">
              <Input placeholder="例如：19.25.0.0.250115" />
            </Form.Item>
          </Col>
        </Row>
        <Row gutter={16}>
          <Col span={24}>
            <Form.Item name="patch_info" label="补丁信息">
              <Input.TextArea rows={3} placeholder="例如：#10;Patch 35759069 : applied on 2026-01-15&#10;Patch 35685663 : applied on 2026-01-15" />
            </Form.Item>
          </Col>
        </Row>
        <Row gutter={16}>
          <Col span={16}>
            <Form.Item name="host" label="主机地址 / IP" rules={[{ required: true, message: "请输入主机地址" }]}>
              <Input placeholder="例如：192.168.1.10" />
            </Form.Item>
          </Col>
          <Col span={8}>
            <Form.Item name="port" label="端口" rules={[{ required: true }]}>
              <InputNumber className="full-width" />
            </Form.Item>
          </Col>
        </Row>
        {dbType === "oracle" ? (
          <Row gutter={16}>
            <Col span={8}>
              <Form.Item name="oracle_connect_type" label="连接模式">
                <Select options={[{ value: "service_name", label: "Service Name" }, { value: "sid", label: "SID" }]} />
              </Form.Item>
            </Col>
            <Col span={16}>
              {form.getFieldValue("oracle_connect_type") === "sid" ? (
                <Form.Item name="sid" label="Oracle SID" rules={[{ required: true, message: "请输入 SID" }]}>
                  <Input placeholder="例如：orcl" />
                </Form.Item>
              ) : (
                <Form.Item name="service_name" label="Service Name" rules={[{ required: true, message: "请输入 Service Name" }]}>
                  <Input placeholder="例如：orclpdb" />
                </Form.Item>
              )}
            </Col>
          </Row>
        ) : (
          <>
            <Row gutter={16}>
              <Col span={12}>
                <Form.Item name="database_name" label="默认数据库">
                  <Input placeholder={dbType === "postgresql" ? "postgres" : dbType === "sqlserver" ? "master" : "可为空"} />
                </Form.Item>
              </Col>
              <Col span={12}>
                <Form.Item name="schema_name" label="默认 Schema">
                  <Input placeholder={dbType === "postgresql" ? "public" : dbType === "sqlserver" ? "dbo" : "可为空"} />
                </Form.Item>
              </Col>
            </Row>
            {dbType === "postgresql" && (
              <Form.Item name="sslmode" label="SSL Mode">
                <Select
                  options={[
                    { value: "disable", label: "disable" },
                    { value: "allow", label: "allow" },
                    { value: "prefer", label: "prefer" },
                    { value: "require", label: "require" },
                    { value: "verify-ca", label: "verify-ca" },
                    { value: "verify-full", label: "verify-full" },
                  ]}
                  placeholder="prefer"
                />
              </Form.Item>
            )}
            {dbType === "sqlserver" && (
              <Row gutter={16}>
                <Col span={12}>
                  <Form.Item name="encrypt" label="传输加密">
                    <Select options={[{ value: "yes", label: "必须加密" }, { value: "request", label: "优先加密" }, { value: "no", label: "关闭" }]} />
                  </Form.Item>
                </Col>
              </Row>
            )}
          </>
        )}
        <Row gutter={16} align="bottom">
          <Col xs={24} md={10}>
            <Form.Item name="certificate_profile_id" label="TLS / Wallet 证书配置">
              <Select
                allowClear
                placeholder={dbType === "sqlserver" ? "SQL Server 使用操作系统信任链" : "可选"}
                disabled={dbType === "sqlserver"}
                options={certificateProfiles
                  .filter((profile) => dbType === "oracle" ? profile.profile_type === "oracle_wallet" : profile.profile_type === "pem")
                  .map((profile) => ({
                    value: profile.id,
                    label: `${profile.name} · ${certificateStatusLabel(profile.lifecycle_status)}`,
                    disabled: profile.enabled !== 1 || profile.lifecycle_status === "expired",
                  }))}
              />
            </Form.Item>
          </Col>
          <Col xs={24} md={8}>
            <Form.Item name="tls_server_name" label="TLS 服务端名称">
              <Input placeholder="证书 SAN / DNS，可选" />
            </Form.Item>
          </Col>
          <Col xs={12} md={3}>
            <Form.Item
              name="tls_verify"
              label="校验证书"
              valuePropName="checked"
              getValueFromEvent={(checked) => checked ? 1 : 0}
              getValueProps={(value) => ({ checked: value !== 0 })}
            >
              <Switch />
            </Form.Item>
          </Col>
          <Col xs={12} md={3}>
            <Form.Item label="证书库">
              <Button onClick={() => setCertificateManagerOpen(true)}>管理</Button>
            </Form.Item>
          </Col>
        </Row>
        {dbType === "sqlserver" && (
          <Alert type="info" showIcon style={{ marginBottom: 16 }} message="SQL Server 使用操作系统证书信任链；此处可配置强制加密，定制 CA 由运行主机统一管理。" />
        )}
        {editing && (
          <Alert
            type="info"
            showIcon
            icon={<SafetyCertificateOutlined />}
            style={{ marginBottom: 16 }}
            message={
              <span>
                数据库凭据 <Tag color="blue">{credentialSourceLabel(editing.credential_source)}</Tag>
                {editing.password_rotated_at ? `最近轮换 ${formatDateTime(editing.password_rotated_at)}` : "尚无轮换时间"}
              </span>
            }
            description="接口不会返回密码或外部秘密路径。切换模式并留空时保持原凭据不变。"
            action={(
              <div style={{ display: "flex", gap: 8 }}>
                <Button size="small" onClick={handleOpenAudit}>审计记录</Button>
                {editing.credential_requires_rotation && (
                  <Button size="small" loading={rewrapping} onClick={handleRewrap}>重新加密</Button>
                )}
              </div>
            )}
          />
        )}
        <Row gutter={16}>
          <Col xs={24} md={8}>
            <Form.Item name="username" label="用户名" rules={[{ required: true, message: "请输入用户名" }]}>
              <Input placeholder="例如：system" />
            </Form.Item>
          </Col>
          <Col xs={24} md={8}>
            <Form.Item name="credential_mode" label="凭据来源">
              <Segmented block options={[{ value: "local", label: "本地加密" }, { value: "external", label: "外部 Secrets" }]} />
            </Form.Item>
          </Col>
          <Col xs={24} md={8}>
            <Form.Item name="account_type" label="账号类型" rules={[{ required: true }]}>
              <Select options={accountTypeOptions} />
            </Form.Item>
          </Col>
        </Row>
        {credentialMode === "external" ? (
          <Form.Item
            name="password_secret_ref"
            label={editing ? "新的外部秘密引用（留空不修改）" : "外部秘密引用"}
            rules={editing ? [] : [{ required: true, message: "请输入外部秘密引用" }]}
            extra="支持 env://变量名 或 vault://挂载点/路径#字段；保存后不再回显引用路径"
          >
            <Input.Password autoComplete="off" placeholder="vault://secret/database/prod#password" />
          </Form.Item>
        ) : (
          <Form.Item
            name="password"
            label={editing ? "修改密码（留空不修改）" : "密码"}
            rules={editing ? [] : [{ required: true, message: "请输入密码" }]}
            extra={editing ? "输入新密码后会重新加密并记录轮换审计" : "使用平台主密钥进行版本化加密存储"}
          >
            <Input.Password autoComplete="new-password" placeholder={editing ? "输入新密码覆盖原密码" : "请输入密码"} />
          </Form.Item>
        )}
        <Row gutter={16}>
          <Col span={8}>
            <Form.Item name="env_type" label="环境">
              <Input placeholder="生产/测试/灾备" />
            </Form.Item>
          </Col>
          <Col span={8}>
            <Form.Item name="system_name" label="系统">
              <Input placeholder="MES / WMS / ERP" />
            </Form.Item>
          </Col>
          <Col span={8}>
            <Form.Item name="factory_name" label="角色">
              <Select placeholder="选择角色" allowClear options={[
                { value: "生产库", label: "生产库" },
                { value: "报表", label: "报表" },
                { value: "历史库", label: "历史库" },
                { value: "测试库", label: "测试库" },
              ]} />
            </Form.Item>
          </Col>
          <Col span={8}>
            <Form.Item name="line_name" label="产线">
              <Input placeholder="产线名称" />
            </Form.Item>
          </Col>
        </Row>
        <Row gutter={16}>
          <Col span={12}>
            <Form.Item name="architecture_type" label="数据库架构">
              <Select allowClear placeholder="选择架构类型" options={[
                { value: "standalone", label: "单机" },
                { value: "rac", label: "RAC / 集群" },
                { value: "primary_standby", label: "主备" },
                { value: "replication", label: "复制" },
                { value: "always_on", label: "Always On" },
                { value: "mgr", label: "MGR / Group Replication" },
              ]} />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item name="role_type" label="数据角色">
              <Select allowClear placeholder="选择实例角色" options={[
                { value: "primary", label: "主库" },
                { value: "standby", label: "备库" },
                { value: "replica", label: "复制从库" },
                { value: "readonly", label: "只读副本" },
                { value: "dr", label: "灾备库" },
                { value: "report", label: "报表库" },
                { value: "unknown", label: "未识别" },
              ]} />
            </Form.Item>
          </Col>
        </Row>
        <Form.Item name="remark" label="备注">
          <Input.TextArea rows={2} placeholder="备注信息" />
        </Form.Item>

        <Divider style={{ margin: "8px 0 16px" }} />
        <div style={{ fontSize: 14, fontWeight: 600, color: "#2563eb", marginBottom: 12, display: "flex", alignItems: "center", gap: 6 }}>
          <CodeOutlined /> Linux 服务器连接（SSH，可选）
        </div>
        <Row gutter={16}>
          <Col span={16}>
            <Form.Item name="ssh_host" label="服务器地址">
              <Input placeholder="例如：192.168.1.10" />
            </Form.Item>
          </Col>
          <Col span={8}>
            <Form.Item name="ssh_port" label="SSH 端口">
              <InputNumber className="full-width" placeholder="22" />
            </Form.Item>
          </Col>
        </Row>
        <Row gutter={16}>
          <Col xs={24} md={8}>
            <Form.Item name="ssh_username" label="SSH 用户名">
              <Input placeholder="例如：oracle" />
            </Form.Item>
          </Col>
          <Col xs={24} md={8}>
            <Form.Item name="ssh_credential_mode" label="SSH 凭据来源">
              <Segmented block options={[{ value: "local", label: "本地加密" }, { value: "external", label: "外部 Secrets" }]} />
            </Form.Item>
          </Col>
          <Col xs={24} md={8}>
            {sshCredentialMode === "external" ? (
              <Form.Item name="ssh_password_secret_ref" label="SSH 外部秘密引用" extra="留空则不修改已有 SSH 凭据">
                <Input.Password autoComplete="off" placeholder="env://TRACEMIND_SSH_PASSWORD" />
              </Form.Item>
            ) : (
              <Form.Item name="ssh_password" label={editing ? "新 SSH 密码" : "SSH 密码"}>
                <Input.Password autoComplete="new-password" placeholder="留空则不配置" />
              </Form.Item>
            )}
          </Col>
        </Row>

        <div style={{ display: "flex", justifyContent: "space-between", gap: 8, marginTop: 16, paddingTop: 16, borderTop: "1px solid #f0f0f0" }}>
          <Button icon={<CheckCircleOutlined />} loading={testing} disabled={saving} onClick={handleTest}>测试</Button>
          <div style={{ display: "flex", gap: 8 }}>
            <Button onClick={onClose}>取消</Button>
            <Button type="primary" htmlType="submit" loading={saving} disabled={testing}>保存</Button>
          </div>
        </div>
      </Form>
      <Modal
        title="凭据审计记录"
        open={auditOpen}
        onCancel={() => setAuditOpen(false)}
        footer={<Button onClick={() => setAuditOpen(false)}>关闭</Button>}
        width={760}
        centered
      >
        <Table
          rowKey="id"
          loading={auditLoading}
          dataSource={audits}
          pagination={{ pageSize: 8, hideOnSinglePage: true }}
          scroll={{ x: 680 }}
          columns={[
            { title: "时间", dataIndex: "create_time", width: 170, render: formatDateTime },
            { title: "类型", dataIndex: "credential_kind", width: 90, render: (value) => ({ database: "数据库", ssh: "SSH", configuration: "配置" })[value] || value },
            { title: "动作", dataIndex: "action", width: 110, render: credentialActionLabel },
            { title: "来源变化", width: 170, render: (_, row) => `${credentialSourceLabel(row.source_before)} → ${credentialSourceLabel(row.source_after)}` },
            { title: "操作人", dataIndex: "actor", width: 110 },
            { title: "说明", dataIndex: "reason", ellipsis: true },
          ]}
        />
      </Modal>
      <ConnectionSecurityModal
        open={certificateManagerOpen}
        onClose={() => setCertificateManagerOpen(false)}
        onChanged={loadCertificateProfiles}
      />
    </Modal>
  );
}

function credentialSourceLabel(value) {
  return ({
    local_encrypted: "本地加密",
    local_legacy: "历史密文",
    environment: "环境变量",
    vault: "Vault",
    none: "未配置",
  })[value] || "已配置";
}

function formatDateTime(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN", { hour12: false });
}

function credentialActionLabel(value) {
  return ({
    created: "创建",
    updated: "配置修改",
    rotated: "轮换",
    rewrapped: "重新加密",
    connection_tested: "连接测试",
    deleted: "删除",
  })[value] || value;
}

function certificateStatusLabel(value) {
  return ({ valid: "有效", expiring: "即将到期", expired: "已过期", disabled: "已停用", not_applicable: "目录型", unknown: "未知" })[value] || "未知";
}
