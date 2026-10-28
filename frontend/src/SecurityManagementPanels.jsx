import React, { useEffect, useMemo, useState } from "react";
import {
  Alert, Button, Checkbox, Descriptions, Form, Input, Modal, Popconfirm,
  Select, Space, Table, Tag, Typography, message,
} from "antd";
import {
  CheckCircleOutlined, DeleteOutlined, EditOutlined, KeyOutlined, PlusOutlined,
  ReloadOutlined, SafetyCertificateOutlined,
} from "@ant-design/icons";
import {
  confirmMfa, createOrganization, createSecurityRole, createTenant,
  deleteOrganization, deleteSecurityRole, deleteTenant, disableMfa, enrollMfa, getAuthSessions, getMfaStatus,
  getOrganizations, getScopeOptions, getSecurityActions, getSecurityAudit,
  getSecurityRoles, getTenants, getUserAccessRoles, getUserAssetScopes,
  getUserOrganizations, revokeAuthSession, saveUserAccessRoles,
  saveUserAssetScopes, saveUserOrganizations, updateOrganization, updateSecurityRole, updateTenant,
  verifySecurityAudit,
} from "./modules/userManagementApi";

const { Text, Paragraph } = Typography;
const modalProps = {
  centered: true,
  styles: { body: { maxHeight: "min(68vh, 720px)", overflowY: "auto" } },
};

export function RoleAccessPanel({ users }) {
  const [roles, setRoles] = useState([]);
  const [actions, setActions] = useState([]);
  const [roleModal, setRoleModal] = useState({ open: false, row: null });
  const [assignment, setAssignment] = useState({ open: false, user: null, roleIds: [] });
  const [form] = Form.useForm();
  const load = async () => {
    try {
      const [roleData, actionData] = await Promise.all([getSecurityRoles(), getSecurityActions()]);
      setRoles(roleData.items || []);
      setActions(actionData.items || []);
    } catch (error) { message.error(error.message || "加载动作权限失败"); }
  };
  useEffect(() => { load(); }, []);
  const groups = useMemo(() => actions.reduce((result, item) => {
    result[item.category] = result[item.category] || [];
    result[item.category].push(item);
    return result;
  }, {}), [actions]);
  const openRole = (row = null) => {
    form.setFieldsValue(row ? {
      role_key: row.role_key, name: row.name, description: row.description,
      action_keys: row.action_keys || [],
    } : { role_key: "", name: "", description: "", action_keys: [] });
    setRoleModal({ open: true, row });
  };
  const saveRole = async () => {
    const values = await form.validateFields();
    try {
      if (roleModal.row) await updateSecurityRole(roleModal.row.id, values);
      else await createSecurityRole(values);
      message.success("角色权限已保存");
      setRoleModal({ open: false, row: null });
      await load();
    } catch (error) { message.error(error.message || "保存角色失败"); }
  };
  const openAssignment = async (user) => {
    try {
      const result = await getUserAccessRoles(user.id);
      setAssignment({ open: true, user, roleIds: result.role_ids || [] });
    } catch (error) { message.error(error.message || "加载用户角色失败"); }
  };
  return (
    <Space direction="vertical" size={12} className="full-width">
      <Alert showIcon type="info" message="动作权限由后端强制校验。为用户分配动作角色后，将按所选权限执行最小授权。" />
      <Space><Button icon={<ReloadOutlined />} onClick={load}>刷新</Button><Button type="primary" icon={<PlusOutlined />} onClick={() => openRole()}>新建角色</Button></Space>
      <Table rowKey="id" size="small" dataSource={roles} pagination={false} scroll={{ x: 960 }} columns={[
        { title: "角色", dataIndex: "name", width: 170, render: (value, row) => <Space><strong>{value}</strong>{row.builtin && <Tag>内置</Tag>}</Space> },
        { title: "标识", dataIndex: "role_key", width: 160 },
        { title: "动作权限", dataIndex: "action_keys", render: (values) => <Space wrap size={[4, 4]}>{(values || []).map((value) => <Tag color={value === "*" ? "red" : "blue"} key={value}>{value}</Tag>)}</Space> },
        { title: "操作", width: 130, render: (_, row) => <Space><Button size="small" onClick={() => openRole(row)}>编辑</Button>{!row.builtin && <Popconfirm title="删除此角色？" onConfirm={async () => { await deleteSecurityRole(row.id); await load(); }}><Button size="small" danger icon={<DeleteOutlined />} /></Popconfirm>}</Space> },
      ]} />
      <Table rowKey="id" size="small" dataSource={users} pagination={false} columns={[
        { title: "用户", render: (_, row) => row.display_name || row.username },
        { title: "登录名", dataIndex: "username" },
        { title: "原有角色", dataIndex: "role", render: (value) => <Tag>{value}</Tag> },
        { title: "动作角色", width: 130, render: (_, row) => <Button size="small" onClick={() => openAssignment(row)}>分配角色</Button> },
      ]} />
      <Modal {...modalProps} width={760} title={roleModal.row ? "编辑动作角色" : "新建动作角色"} open={roleModal.open} onCancel={() => setRoleModal({ open: false, row: null })} onOk={saveRole}>
        <Form form={form} layout="vertical">
          <Form.Item name="role_key" label="角色标识" rules={[{ required: true }]}><Input disabled={Boolean(roleModal.row?.builtin)} placeholder="database_operator" /></Form.Item>
          <Form.Item name="name" label="角色名称" rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="description" label="说明"><Input.TextArea rows={2} /></Form.Item>
          <Form.Item name="action_keys" label="动作权限"><Checkbox.Group className="full-width">
            {Object.entries(groups).map(([category, items]) => <div key={category} className="security-action-group"><strong>{category}</strong><Space wrap>{items.map((item) => <Checkbox key={item.action_key} value={item.action_key}>{item.name}</Checkbox>)}</Space></div>)}
          </Checkbox.Group></Form.Item>
        </Form>
      </Modal>
      <Modal {...modalProps} width={560} title={"分配动作角色 - " + (assignment.user?.username || "")} open={assignment.open} onCancel={() => setAssignment({ open: false, user: null, roleIds: [] })} onOk={async () => {
        try {
          await saveUserAccessRoles(assignment.user.id, assignment.roleIds);
          message.success("角色已保存，该用户现有会话已撤销");
          setAssignment({ open: false, user: null, roleIds: [] });
        } catch (error) { message.error(error.message || "保存角色失败"); }
      }}>
        <Checkbox.Group value={assignment.roleIds} onChange={(roleIds) => setAssignment({ ...assignment, roleIds })}>
          <Space direction="vertical">{roles.map((role) => <Checkbox key={role.id} value={role.id}><strong>{role.name}</strong> <Text type="secondary">{role.role_key}</Text></Checkbox>)}</Space>
        </Checkbox.Group>
      </Modal>
    </Space>
  );
}

const scopeTypes = [
  ["all", "全部资产"], ["tenant", "租户"], ["organization", "组织"],
  ["region", "区域"], ["site", "机房"], ["zone", "可用区"],
  ["environment", "环境"], ["host", "主机"], ["cluster", "集群"], ["instance", "实例"],
].map(([value, label]) => ({ value, label }));
const optionKeys = {
  tenant: "tenants", organization: "organizations", region: "regions", site: "sites",
  zone: "zones", environment: "environments", host: "hosts", cluster: "clusters", instance: "instances",
};

export function AssetScopePanel({ users }) {
  const [options, setOptions] = useState({});
  const [editor, setEditor] = useState({ open: false, user: null });
  const [form] = Form.useForm();
  const load = async () => {
    try { setOptions(await getScopeOptions()); }
    catch (error) { message.error(error.message || "加载数据范围失败"); }
  };
  useEffect(() => { load(); }, []);
  const open = async (user) => {
    try {
      const result = await getUserAssetScopes(user.id);
      form.setFieldsValue({ scopes: result.scopes || [] });
      setEditor({ open: true, user });
    } catch (error) { message.error(error.message || "加载用户范围失败"); }
  };
  return (
    <Space direction="vertical" size={12} className="full-width">
      <Alert showIcon type="warning" message="未配置范围时兼容原有访问；配置任意范围后，实例列表、资产树和资产直链都会由后端强制限制。" />
      <Button icon={<ReloadOutlined />} onClick={load}>刷新范围字典</Button>
      <Table rowKey="id" size="small" dataSource={users} pagination={false} columns={[
        { title: "用户", render: (_, row) => row.display_name || row.username },
        { title: "登录名", dataIndex: "username" },
        { title: "状态", dataIndex: "status", render: (value) => <Tag color={value === "active" ? "green" : "default"}>{value}</Tag> },
        { title: "操作", width: 130, render: (_, row) => <Button size="small" onClick={() => open(row)}>配置范围</Button> },
      ]} />
      <Modal {...modalProps} width={780} title={"资产数据范围 - " + (editor.user?.username || "")} open={editor.open} onCancel={() => setEditor({ open: false, user: null })} onOk={async () => {
        const values = await form.validateFields();
        try {
          await saveUserAssetScopes(editor.user.id, values.scopes || []);
          message.success("数据范围已保存");
          setEditor({ open: false, user: null });
        } catch (error) { message.error(error.message || "保存数据范围失败"); }
      }}>
        <Form form={form} layout="vertical"><Form.List name="scopes">{(fields, { add, remove }) => <Space direction="vertical" className="full-width">
          {fields.map((field) => <ScopeRow key={field.key} field={field} form={form} options={options} remove={remove} />)}
          <Button type="dashed" icon={<PlusOutlined />} onClick={() => add({ scope_type: "environment", access_level: "read" })}>添加数据范围</Button>
        </Space>}</Form.List></Form>
      </Modal>
    </Space>
  );
}

function ScopeRow({ field, form, options, remove }) {
  const scopeType = Form.useWatch(["scopes", field.name, "scope_type"], form);
  const items = (options[optionKeys[scopeType]] || []).map((item) => ({
    value: item.id, label: item.detail ? item.name + " · " + item.detail : item.name,
  }));
  return <div className="security-scope-row">
    <Form.Item name={[field.name, "scope_type"]} label="范围类型" rules={[{ required: true }]}><Select options={scopeTypes} /></Form.Item>
    {scopeType !== "all" && <Form.Item name={[field.name, "scope_id"]} label="资产" rules={[{ required: true }]}><Select showSearch optionFilterProp="label" options={items} /></Form.Item>}
    <Form.Item name={[field.name, "access_level"]} label="访问级别" rules={[{ required: true }]}><Select options={[
      { value: "read", label: "只读" }, { value: "operate", label: "可操作" }, { value: "admin", label: "范围管理员" },
    ]} /></Form.Item>
    <Button type="text" danger icon={<DeleteOutlined />} title="删除范围" onClick={() => remove(field.name)} />
  </div>;
}

export function OrganizationSecurityPanel({ users }) {
  const [tenants, setTenants] = useState([]);
  const [organizations, setOrganizations] = useState([]);
  const [modal, setModal] = useState({ type: null, user: null, row: null });
  const [form] = Form.useForm();
  const load = async () => {
    try {
      const [tenantData, organizationData] = await Promise.all([getTenants(), getOrganizations()]);
      setTenants(tenantData.items || []);
      setOrganizations(organizationData.items || []);
    } catch (error) { message.error(error.message || "加载组织租户失败"); }
  };
  useEffect(() => { load(); }, []);
  const openAssignment = async (user) => {
    try {
      form.setFieldsValue(await getUserOrganizations(user.id));
      setModal({ type: "assignment", user, row: null });
    } catch (error) { message.error(error.message || "加载用户组织失败"); }
  };
  const save = async () => {
    const values = await form.validateFields();
    try {
      if (modal.type === "tenant") {
        if (modal.row) await updateTenant(modal.row.id, values);
        else await createTenant(values);
      }
      if (modal.type === "organization") {
        if (modal.row) await updateOrganization(modal.row.id, values);
        else await createOrganization(values);
      }
      if (modal.type === "assignment") await saveUserOrganizations(modal.user.id, values.organization_ids || [], values.primary_organization_id);
      message.success("组织租户配置已保存");
      setModal({ type: null, user: null, row: null });
      await load();
    } catch (error) { message.error(error.message || "保存组织租户失败"); }
  };
  return (
    <Space direction="vertical" size={12} className="full-width">
      <Space wrap><Button icon={<ReloadOutlined />} onClick={load}>刷新</Button><Button type="primary" icon={<PlusOutlined />} onClick={() => { form.setFieldsValue({ code: "", name: "", status: "active", remark: "" }); setModal({ type: "tenant", user: null, row: null }); }}>新建租户</Button><Button icon={<PlusOutlined />} disabled={!tenants.length} onClick={() => { form.setFieldsValue({ tenant_id: tenants[0]?.id, parent_id: null, code: "", name: "", organization_type: "department", status: "active", remark: "" }); setModal({ type: "organization", user: null, row: null }); }}>新建组织</Button></Space>
      <Table rowKey="id" size="small" dataSource={tenants} pagination={false} columns={[
        { title: "租户", dataIndex: "name" }, { title: "编码", dataIndex: "code" },
        { title: "状态", dataIndex: "status", width: 100, render: (value) => <Tag color={value === "active" ? "green" : "default"}>{value === "active" ? "启用" : "停用"}</Tag> },
        { title: "操作", width: 130, render: (_, row) => <Space size={4}>
          <Button type="text" icon={<EditOutlined />} title="编辑租户" onClick={() => { form.setFieldsValue(row); setModal({ type: "tenant", user: null, row }); }} />
          <Popconfirm title="确认删除租户？" description="仍有关联组织、用户或资产时后端会拒绝删除。" onConfirm={async () => { try { await deleteTenant(row.id); message.success("租户已删除"); await load(); } catch (error) { message.error(error.message || "删除租户失败"); } }}><Button type="text" danger icon={<DeleteOutlined />} title="删除租户" /></Popconfirm>
        </Space> },
      ]} />
      <Table rowKey="id" size="small" dataSource={organizations} pagination={false} columns={[
        { title: "组织", dataIndex: "name" }, { title: "编码", dataIndex: "code" },
        { title: "类型", dataIndex: "organization_type", render: (value) => <Tag>{value}</Tag> },
        { title: "租户", dataIndex: "tenant_id", render: (id) => tenants.find((item) => item.id === id)?.name || id },
        { title: "操作", width: 130, render: (_, row) => <Space size={4}>
          <Button type="text" icon={<EditOutlined />} title="编辑组织" onClick={() => { form.setFieldsValue(row); setModal({ type: "organization", user: null, row }); }} />
          <Popconfirm title="确认删除组织？" description="仍有关联下级、用户或资产时后端会拒绝删除。" onConfirm={async () => { try { await deleteOrganization(row.id); message.success("组织已删除"); await load(); } catch (error) { message.error(error.message || "删除组织失败"); } }}><Button type="text" danger icon={<DeleteOutlined />} title="删除组织" /></Popconfirm>
        </Space> },
      ]} />
      <Table rowKey="id" size="small" dataSource={users} pagination={false} columns={[
        { title: "用户", render: (_, row) => row.display_name || row.username },
        { title: "登录名", dataIndex: "username" },
        { title: "操作", width: 130, render: (_, row) => <Button size="small" onClick={() => openAssignment(row)}>分配组织</Button> },
      ]} />
      <Modal {...modalProps} width={560} title={modal.type === "tenant" ? (modal.row ? "编辑租户" : "新建租户") : modal.type === "organization" ? (modal.row ? "编辑组织" : "新建组织") : "分配组织 - " + (modal.user?.username || "")} open={Boolean(modal.type)} onCancel={() => setModal({ type: null, user: null, row: null })} onOk={save}>
        <Form form={form} layout="vertical">
          {modal.type === "tenant" && <><Form.Item name="code" label="租户编码" rules={[{ required: true }]}><Input placeholder="enterprise" /></Form.Item><Form.Item name="name" label="租户名称" rules={[{ required: true }]}><Input /></Form.Item><Form.Item name="status" label="状态"><Select options={[{ value: "active", label: "启用" }, { value: "disabled", label: "停用" }]} /></Form.Item><Form.Item name="remark" label="备注"><Input.TextArea rows={2} /></Form.Item></>}
          {modal.type === "organization" && <>
            <Form.Item name="tenant_id" label="所属租户" rules={[{ required: true }]}><Select options={tenants.map((item) => ({ value: item.id, label: item.name }))} /></Form.Item>
            <Form.Item name="parent_id" label="上级组织"><Select allowClear options={organizations.map((item) => ({ value: item.id, label: item.name }))} /></Form.Item>
            <Form.Item name="code" label="组织编码" rules={[{ required: true }]}><Input /></Form.Item>
            <Form.Item name="name" label="组织名称" rules={[{ required: true }]}><Input /></Form.Item>
            <Form.Item name="organization_type" label="组织类型" initialValue="department"><Select options={["group", "company", "factory", "department", "team", "custom"].map((value) => ({ value, label: value }))} /></Form.Item>
            <Form.Item name="status" label="状态"><Select options={[{ value: "active", label: "启用" }, { value: "disabled", label: "停用" }]} /></Form.Item>
            <Form.Item name="remark" label="备注"><Input.TextArea rows={2} /></Form.Item>
          </>}
          {modal.type === "assignment" && <>
            <Form.Item name="organization_ids" label="所属组织"><Select mode="multiple" options={organizations.map((item) => ({ value: item.id, label: item.name }))} /></Form.Item>
            <Form.Item name="primary_organization_id" label="主组织"><Select allowClear options={organizations.map((item) => ({ value: item.id, label: item.name }))} /></Form.Item>
          </>}
        </Form>
      </Modal>
    </Space>
  );
}

export function AccountSecurityPanel() {
  const [status, setStatus] = useState({});
  const [sessions, setSessions] = useState([]);
  const [enrollment, setEnrollment] = useState(null);
  const [code, setCode] = useState("");
  const load = async () => {
    try {
      const [mfa, sessionData] = await Promise.all([getMfaStatus(), getAuthSessions()]);
      setStatus(mfa);
      setSessions(sessionData.items || []);
    } catch (error) { message.error(error.message || "加载登录安全信息失败"); }
  };
  useEffect(() => { load(); }, []);
  return (
    <Space direction="vertical" size={12} className="full-width">
      <Descriptions bordered size="small" column={{ xs: 1, md: 3 }} items={[
        { key: "mfa", label: "MFA", children: <Tag color={status.enabled ? "green" : "orange"}>{status.enabled ? "已启用" : "未启用"}</Tag> },
        { key: "verified", label: "验证时间", children: status.verified_at || "-" },
        { key: "recovery", label: "剩余恢复码", children: status.recovery_codes_remaining ?? 0 },
      ]} />
      <Space wrap>
        <Button type="primary" icon={<SafetyCertificateOutlined />} onClick={async () => { try { setEnrollment(await enrollMfa()); setCode(""); } catch (error) { message.error(error.message); } }}>{status.enabled ? "重新绑定 MFA" : "绑定 MFA"}</Button>
        {status.enabled && <Button danger onClick={() => {
          let value = "";
          Modal.confirm({ centered: true, title: "停用 MFA", content: <Input placeholder="动态验证码或恢复码" onChange={(event) => { value = event.target.value; }} />, onOk: async () => { await disableMfa(value); await load(); } });
        }}>停用 MFA</Button>}
        <Button icon={<ReloadOutlined />} onClick={load}>刷新</Button>
      </Space>
      <Table rowKey="id" size="small" dataSource={sessions} pagination={false} scroll={{ x: 900 }} columns={[
        { title: "来源", dataIndex: "source", width: 100 },
        { title: "MFA", dataIndex: "mfa_verified", width: 70, render: (value) => value ? <CheckCircleOutlined style={{ color: "#239b56" }} /> : "-" },
        { title: "客户端", dataIndex: "client_ip", width: 140, render: (value) => value || "-" },
        { title: "最后活动", dataIndex: "last_seen_at", width: 180 },
        { title: "到期时间", dataIndex: "expires_at", width: 180 },
        { title: "状态", dataIndex: "revoked_at", width: 90, render: (value) => <Tag color={value ? "default" : "green"}>{value ? "已撤销" : "有效"}</Tag> },
        { title: "操作", width: 90, render: (_, row) => !row.revoked_at && <Button size="small" danger onClick={async () => { await revokeAuthSession(row.id); await load(); }}>撤销</Button> },
      ]} />
      <Modal {...modalProps} width={620} title="绑定身份验证器" open={Boolean(enrollment)} onCancel={() => setEnrollment(null)} footer={[
        <Button key="cancel" onClick={() => setEnrollment(null)}>取消</Button>,
        <Button key="ok" type="primary" icon={<KeyOutlined />} onClick={async () => {
          try { await confirmMfa(code); message.success("MFA 已启用"); setEnrollment(null); await load(); }
          catch (error) { message.error(error.message || "MFA 验证失败"); }
        }}>验证并启用</Button>,
      ]}>
        <Alert showIcon type="warning" message="密钥和恢复码只显示一次，请存入受控密码库。" style={{ marginBottom: 12 }} />
        <Paragraph copyable code>{enrollment?.secret}</Paragraph>
        <Text type="secondary">身份验证器 URI</Text>
        <Paragraph copyable ellipsis={{ rows: 2, expandable: true }}>{enrollment?.provisioning_uri}</Paragraph>
        <Text strong>恢复码</Text>
        <Paragraph copyable code>{(enrollment?.recovery_codes || []).join("  ")}</Paragraph>
        <Input value={code} onChange={(event) => setCode(event.target.value)} placeholder="输入 6 位动态验证码" maxLength={20} />
      </Modal>
    </Space>
  );
}

export function SecurityAuditPanel() {
  const [rows, setRows] = useState([]);
  const [verification, setVerification] = useState(null);
  const load = async () => {
    try { setRows((await getSecurityAudit()).items || []); }
    catch (error) { message.error(error.message || "加载安全审计失败"); }
  };
  useEffect(() => { load(); }, []);
  return (
    <Space direction="vertical" size={12} className="full-width">
      {verification && <Alert showIcon type={verification.valid ? "success" : "error"} message={verification.valid ? "审计哈希链完整，已核验 " + verification.checked + " 条" : "审计链在事件 #" + verification.broken_event_id + " 处损坏"} />}
      <Space><Button icon={<ReloadOutlined />} onClick={load}>刷新</Button><Button icon={<SafetyCertificateOutlined />} onClick={async () => setVerification(await verifySecurityAudit())}>核验完整性</Button></Space>
      <Table rowKey="id" size="small" dataSource={rows} pagination={{ pageSize: 20 }} scroll={{ x: 1160 }} columns={[
        { title: "时间", dataIndex: "create_time", width: 180 },
        { title: "类别", dataIndex: "category", width: 120 },
        { title: "动作", dataIndex: "action", width: 180 },
        { title: "操作人", dataIndex: "actor", width: 120 },
        { title: "目标", width: 170, render: (_, row) => (row.target_type || "-") + " #" + (row.target_id || "-") },
        { title: "结果", dataIndex: "outcome", width: 90, render: (value) => <Tag color={value === "success" ? "green" : "red"}>{value}</Tag> },
        { title: "事件哈希", dataIndex: "event_hash", ellipsis: true },
        { title: "保留至", dataIndex: "retention_until", width: 180 },
      ]} />
    </Space>
  );
}
