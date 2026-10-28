import React, { useEffect, useState } from "react";
import { Button, Form, Input, Modal, Segmented, Select, Space, Table, Tag, message } from "antd";
import { CheckCircleOutlined, DeleteOutlined, EditOutlined, PlusOutlined, ReloadOutlined } from "@ant-design/icons";
import { connectionSecurityApi } from "./apiClients";


export function ConnectionSecurityModal({ open, onClose, onChanged }) {
  const [profiles, setProfiles] = useState([]);
  const [loading, setLoading] = useState(false);
  const [editorOpen, setEditorOpen] = useState(false);
  const [editing, setEditing] = useState(null);
  const [saving, setSaving] = useState(false);
  const [rowAction, setRowAction] = useState(null);
  const [form] = Form.useForm();
  const profileType = Form.useWatch("profile_type", form) || "pem";
  const privateKeyMode = Form.useWatch("private_key_mode", form) || "external";
  const walletPasswordMode = Form.useWatch("wallet_password_mode", form) || "external";

  const load = async () => {
    setLoading(true);
    try {
      setProfiles(await connectionSecurityApi.certificates());
    } catch (error) {
      message.error(error.message || "证书配置加载失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (open) load();
  }, [open]);

  const openEditor = (row = null) => {
    setEditing(row);
    form.resetFields();
    form.setFieldsValue(row ? {
      name: row.name,
      profile_type: row.profile_type,
      enabled: row.enabled,
      remark: row.remark,
      private_key_mode: ["vault", "environment"].includes(row.private_key_source) ? "external" : "local",
      wallet_password_mode: ["vault", "environment"].includes(row.wallet_password_source) ? "external" : "local",
    } : {
      profile_type: "pem",
      enabled: 1,
      private_key_mode: "external",
      wallet_password_mode: "external",
    });
    setEditorOpen(true);
  };

  const save = async (values) => {
    const payload = { ...values };
    delete payload.private_key_mode;
    delete payload.wallet_password_mode;
    if (privateKeyMode === "external") delete payload.private_key;
    else delete payload.private_key_secret_ref;
    if (walletPasswordMode === "external") delete payload.wallet_password;
    else delete payload.wallet_password_secret_ref;
    if (editing) delete payload.profile_type;
    Object.keys(payload).forEach((key) => {
      if (editing && (payload[key] === "" || payload[key] == null)) delete payload[key];
    });
    setSaving(true);
    try {
      if (editing) await connectionSecurityApi.updateCertificate(editing.id, payload);
      else await connectionSecurityApi.createCertificate(payload);
      message.success(editing ? "证书配置已更新" : "证书配置已创建");
      setEditorOpen(false);
      await load();
      await onChanged?.();
    } catch (error) {
      message.error(error.message || "证书配置保存失败");
    } finally {
      setSaving(false);
    }
  };

  const testProfile = async (row) => {
    setRowAction(`test-${row.id}`);
    try {
      const result = await connectionSecurityApi.testCertificate(row.id);
      Modal.success({ title: "证书配置校验通过", content: result.not_after ? `有效期至 ${formatDateTime(result.not_after)}` : result.message });
      await load();
    } catch (error) {
      Modal.error({ title: "证书配置校验失败", content: error.message || "校验失败" });
    } finally {
      setRowAction(null);
    }
  };

  const remove = (row) => {
    Modal.confirm({
      title: "删除证书配置",
      content: `确认删除“${row.name}”？仍被数据库连接引用时后端会拒绝删除。`,
      okText: "删除",
      okButtonProps: { danger: true },
      cancelText: "取消",
      centered: true,
      onOk: async () => {
        setRowAction(`delete-${row.id}`);
        try {
          await connectionSecurityApi.deleteCertificate(row.id);
          message.success("证书配置已删除");
          await load();
          await onChanged?.();
        } catch (error) {
          message.error(error.message || "删除失败");
        } finally {
          setRowAction(null);
        }
      },
    });
  };

  return (
    <>
      <Modal
        title="连接证书管理"
        open={open}
        onCancel={onClose}
        width={980}
        centered
        footer={<Button onClick={onClose}>关闭</Button>}
        styles={{ body: { maxHeight: "calc(100vh - 180px)", overflowY: "auto" } }}
      >
        <div className="connection-security-toolbar">
          <span>统一管理 MySQL/PostgreSQL PEM 证书与 Oracle Wallet，秘密材料不会通过接口回显。</span>
          <Space>
            <Button icon={<ReloadOutlined />} loading={loading} onClick={load}>刷新</Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => openEditor()}>新增证书</Button>
          </Space>
        </div>
        <Table
          rowKey="id"
          loading={loading}
          dataSource={profiles}
          pagination={{ pageSize: 8, hideOnSinglePage: true }}
          scroll={{ x: 900 }}
          columns={[
            { title: "名称", dataIndex: "name", width: 190 },
            { title: "类型", dataIndex: "profile_type", width: 120, render: (value) => value === "oracle_wallet" ? "Oracle Wallet" : "PEM" },
            { title: "状态", dataIndex: "lifecycle_status", width: 110, render: renderLifecycle },
            { title: "有效期至", dataIndex: "not_after", width: 180, render: (value) => value ? formatDateTime(value) : "-" },
            { title: "秘密来源", width: 120, render: (_, row) => sourceLabel(row.private_key_source !== "none" ? row.private_key_source : row.wallet_password_source) },
            { title: "指纹", dataIndex: "fingerprint_sha256", width: 150, ellipsis: true, render: (value) => value ? `${value.slice(0, 12)}...` : "-" },
            {
              title: "操作",
              fixed: "right",
              width: 240,
              render: (_, row) => (
                <Space>
                  <Button type="link" icon={<CheckCircleOutlined />} loading={rowAction === `test-${row.id}`} onClick={() => testProfile(row)}>校验</Button>
                  <Button type="link" icon={<EditOutlined />} onClick={() => openEditor(row)}>编辑</Button>
                  <Button type="link" danger icon={<DeleteOutlined />} loading={rowAction === `delete-${row.id}`} onClick={() => remove(row)}>删除</Button>
                </Space>
              ),
            },
          ]}
        />
      </Modal>

      <Modal
        title={editing ? "编辑证书配置" : "新增证书配置"}
        open={editorOpen}
        onCancel={() => setEditorOpen(false)}
        onOk={() => form.submit()}
        okText="保存"
        cancelText="取消"
        confirmLoading={saving}
        width={720}
        centered
        styles={{ body: { maxHeight: "calc(100vh - 180px)", overflowY: "auto" } }}
      >
        <Form form={form} layout="vertical" onFinish={save}>
          <Form.Item name="name" label="配置名称" rules={[{ required: true, message: "请输入配置名称" }]}>
            <Input placeholder="例如：生产 PostgreSQL 双向 TLS" />
          </Form.Item>
          <Form.Item name="profile_type" label="证书类型" rules={[{ required: true }]}>
            <Select disabled={Boolean(editing)} options={[{ value: "pem", label: "PEM 证书" }, { value: "oracle_wallet", label: "Oracle Wallet" }]} />
          </Form.Item>
          {profileType === "pem" ? (
            <>
              <Form.Item name="ca_certificate_pem" label="CA 证书 PEM" extra={editing ? "留空保持现有 CA 证书" : "单向 TLS 只需填写 CA 证书"}>
                <Input.TextArea rows={5} placeholder="-----BEGIN CERTIFICATE-----" />
              </Form.Item>
              <Form.Item name="client_certificate_pem" label="客户端证书 PEM" extra="双向 TLS 时填写；编辑留空保持现有客户端证书">
                <Input.TextArea rows={5} placeholder="-----BEGIN CERTIFICATE-----" />
              </Form.Item>
              <Form.Item name="private_key_mode" label="客户端私钥来源">
                <Segmented block options={[{ value: "external", label: "外部 Secrets" }, { value: "local", label: "本地加密" }]} />
              </Form.Item>
              {privateKeyMode === "external" ? (
                <Form.Item name="private_key_secret_ref" label="私钥秘密引用" extra="支持 env:// 或 vault://；编辑留空保持现有私钥">
                  <Input.Password autoComplete="off" placeholder="vault://secret/certificates/postgresql#private_key" />
                </Form.Item>
              ) : (
                <Form.Item name="private_key" label="客户端私钥 PEM" extra="提交后立即加密，接口不会回显">
                  <Input.TextArea rows={5} placeholder="粘贴客户端私钥 PEM 内容" />
                </Form.Item>
              )}
            </>
          ) : (
            <>
              <Form.Item name="wallet_directory" label="服务器 Wallet 绝对目录" rules={editing ? [] : [{ required: true, message: "请输入 Wallet 目录" }]}>
                <Input placeholder="/opt/tracemind/wallets/oracle-prod" />
              </Form.Item>
              <Form.Item name="wallet_password_mode" label="Wallet 密码来源">
                <Segmented block options={[{ value: "external", label: "外部 Secrets" }, { value: "local", label: "本地加密" }]} />
              </Form.Item>
              {walletPasswordMode === "external" ? (
                <Form.Item name="wallet_password_secret_ref" label="Wallet 密码引用" extra="编辑留空保持现有密码">
                  <Input.Password autoComplete="off" placeholder="env://ORACLE_WALLET_PASSWORD" />
                </Form.Item>
              ) : (
                <Form.Item name="wallet_password" label="Wallet 密码">
                  <Input.Password autoComplete="new-password" />
                </Form.Item>
              )}
            </>
          )}
          <Form.Item name="enabled" label="状态">
            <Segmented block options={[{ value: 1, label: "启用" }, { value: 0, label: "停用" }]} />
          </Form.Item>
          <Form.Item name="remark" label="备注"><Input.TextArea rows={2} /></Form.Item>
        </Form>
      </Modal>
    </>
  );
}

function renderLifecycle(value) {
  const config = {
    valid: ["green", "有效"],
    expiring: ["orange", "即将到期"],
    expired: ["red", "已过期"],
    disabled: ["default", "已停用"],
    not_applicable: ["blue", "目录型"],
    unknown: ["default", "未知"],
  }[value] || ["default", value || "未知"];
  return <Tag color={config[0]}>{config[1]}</Tag>;
}

function sourceLabel(value) {
  return ({ local_encrypted: "本地加密", local_legacy: "历史密文", environment: "环境变量", vault: "Vault", none: "无" })[value] || "无";
}

function formatDateTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN", { hour12: false });
}
