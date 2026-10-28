import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from "antd";
import { DeleteOutlined, PlusOutlined, ReloadOutlined, SettingOutlined } from "@ant-design/icons";
import { catTag, getModuleDesc, roleTag, statusTag } from "./UserManagementConfig";
import { emitAuthExpired } from "./modules/authEvents";
import {
  AccountSecurityPanel,
  AssetScopePanel,
  OrganizationSecurityPanel,
  RoleAccessPanel,
  SecurityAuditPanel,
} from "./SecurityManagementPanels";
import {
  createUser,
  deleteUserAccount,
  getUserManagementBundle,
  getUserPermissions,
  resetModulesConfig,
  resetUserPassword,
  saveModulesConfig,
  savePasswordPolicy,
  saveUserPermissions,
  setModuleEnabled,
  unlockUserAccount,
  updateUserStatus,
} from "./modules/userManagementApi";

const { Text } = Typography;

export function UserManagementPage({ initialTab = "users" }) {
  const [users, setUsers] = useState([]);
  const [modules, setModules] = useState([]);
  const [loading, setLoading] = useState(false);
  const [createModal, setCreateModal] = useState(false);
  const [createForm] = Form.useForm();
  const [permModal, setPermModal] = useState({ open: false, userId: null, username: "" });
  const [permKeys, setPermKeys] = useState([]);
  const [resetModal, setResetModal] = useState({ open: false, userId: null, username: "" });
  const [resetForm] = Form.useForm();
  const [currentUser, setCurrentUser] = useState(null);
  const [resetSaving, setResetSaving] = useState(false);
  const resetInFlight = useRef(false);
  const resettingSelf = currentUser?.id === resetModal.userId;
  const [policy, setPolicy] = useState({});
  const [policyModal, setPolicyModal] = useState(false);
  const [policyForm] = Form.useForm();

  const [activeTab, setActiveTab] = useState(initialTab);
  const [userPerms, setUserPerms] = useState({});
  const permissionModules = useMemo(() => modules.filter((m) => m.module_key !== "moduleConfig"), [modules]);
  const permissionModuleKeys = useMemo(() => permissionModules.map((m) => m.module_key), [permissionModules]);

  const loadUserPerms = async () => {
    try {
      const permsMap = {};
      for (const u of users) {
        const data = await getUserPermissions(u.id);
        const keys = data.module_keys || [];
        permsMap[u.id] = new Set(u.username === "admin" && keys.length === 0 ? permissionModuleKeys : keys);
      }
      setUserPerms(permsMap);
    } catch (e) { message.warning(e.message || "加载用户权限失败"); }
  };

  useEffect(() => { if (users.length > 0 && activeTab === "modules") loadUserPerms(); }, [users, activeTab]);

  const toggleUserModule = async (userId, moduleKey, checked) => {
    const current = userPerms[userId] || new Set();
    if (checked) current.add(moduleKey); else current.delete(moduleKey);
    setUserPerms({ ...userPerms, [userId]: new Set(current) });
    try {
      await saveUserPermissions(userId, Array.from(current));
    } catch (e) { message.error(e.message); await loadData(); }
  };

  const selectAllForUser = async (userId) => {
    const allKeys = permissionModuleKeys;
    setUserPerms({ ...userPerms, [userId]: new Set(allKeys) });
    try {
      await saveUserPermissions(userId, allKeys);
      message.success("已全选");
    } catch (e) { message.error(e.message); }
  };

  const clearAllForUser = async (userId) => {
    setUserPerms({ ...userPerms, [userId]: new Set() });
    try {
      await saveUserPermissions(userId, []);
      message.success("已清空");
    } catch (e) { message.error(e.message); }
  };

  const toggleModule = async (key, enabled) => {
    try {
      await setModuleEnabled(key, !enabled);
      setModules((prev) => prev.map((m) => m.module_key === key ? { ...m, enabled: enabled ? 0 : 1 } : m));
      message.success(enabled ? "已隐藏" : "已显示");
    } catch (e) { message.error(e.message); }
  };

  const saveModuleConfig = async () => {
    try {
      await saveModulesConfig(modules);
      message.success("模块配置已保存，刷新页面生效");
    } catch (e) { message.error(e.message); }
  };

  const resetModuleConfig = async () => {
    Modal.confirm({
      title: "重置模块配置",
      content: "将恢复为默认配置，是否继续？",
      onOk: async () => {
        await resetModulesConfig();
        await loadData();
        message.success("已重置");
      },
    });
  };

  const loadData = async () => {
    setLoading(true);
    try {
      const [userData, moduleData, policyData, identity] = await getUserManagementBundle();
      setUsers(userData.items || []);
      setModules(moduleData.items || []);
      setPolicy(policyData);
      setCurrentUser(identity);
    } catch (e) { message.error(e.message); }
    finally { setLoading(false); }
  };
  useEffect(() => { loadData(); }, []);

  const doCreate = async () => {
    const values = await createForm.validateFields();
    try {
      await createUser(values);
      message.success("用户已创建");
      setCreateModal(false);
      await loadData();
    } catch (e) { message.error(e.message); }
  };

  const toggleStatus = async (user) => {
    try {
      const newStatus = user.status === "active" ? "disabled" : "active";
      await updateUserStatus(user.id, newStatus);
      message.success(newStatus === "active" ? "已启用" : "已停用");
      await loadData();
    } catch (e) { message.error(e.message); }
  };

  const unlockUser = async (userId) => {
    try {
      await unlockUserAccount(userId);
      message.success("用户已解锁");
      await loadData();
    } catch (e) { message.error(e.message); }
  };

  const deleteUser = async (user) => {
    try {
      await deleteUserAccount(user.id);
      message.success("用户已删除");
      await loadData();
    } catch (e) { message.error(e.message); }
  };

  const openPermModal = async (user) => {
    try {
      const data = await getUserPermissions(user.id);
      const keys = data.module_keys || [];
      setPermKeys(user.username === "admin" && keys.length === 0 ? permissionModuleKeys : keys);
      setPermModal({ open: true, userId: user.id, username: user.username });
    } catch (e) { message.error(e.message); }
  };

  const savePerms = async () => {
    try {
      await saveUserPermissions(permModal.userId, permKeys);
      message.success("权限已保存");
      setPermModal({ open: false, userId: null, username: "" });
      await loadData();
    } catch (e) { message.error(e.message); }
  };

  const closeResetModal = () => {
    if (resetInFlight.current) return;
    resetForm.resetFields();
    setResetModal({ open: false, userId: null, username: "" });
  };

  const doResetPassword = async () => {
    if (resetInFlight.current) return;
    resetInFlight.current = true;
    setResetSaving(true);
    try {
      const values = await resetForm.validateFields();
      const result = await resetUserPassword(resetModal.userId, values);
      resetForm.resetFields();
      setResetModal({ open: false, userId: null, username: "" });
      message.success(result.detail);
      if (result.reauthenticate) {
        emitAuthExpired();
        return;
      }
      await loadData();
    } catch (e) {
      if (!e.errorFields) {
        resetForm.setFieldsValue({ current_password: "" });
        message.error(e.message || "密码修改失败");
      }
    } finally {
      resetInFlight.current = false;
      setResetSaving(false);
    }
  };

  const openPolicyModal = () => {
    policyForm.setFieldsValue(policy);
    setPolicyModal(true);
  };

  const savePolicy = async () => {
    const values = await policyForm.validateFields();
    try {
      await savePasswordPolicy(values);
      message.success("密码策略已更新");
      setPolicyModal(false);
      await loadData();
    } catch (e) { message.error(e.message); }
  };



  return (
    <Space direction="vertical" size={16} className="full-width">
      <Tabs activeKey={activeTab} onChange={setActiveTab} items={[
        {
          key: "users",
          label: "用户管理",
          children: (
            <Space direction="vertical" size={16} className="full-width">
              <Card extra={
                <Space>
                  <Button icon={<ReloadOutlined />} loading={loading} onClick={loadData}>刷新</Button>
                  <Button type="primary" icon={<PlusOutlined />} onClick={() => { createForm.resetFields(); setCreateModal(true); }}>新增用户</Button>
                  <Button icon={<SettingOutlined />} onClick={openPolicyModal}>密码策略</Button>
                </Space>
              }>
                <Table dataSource={users} rowKey="id" size="small" pagination={false} scroll={{ x: 1200 }}
                  columns={[
                    { title: "用户名", dataIndex: "username", width: 120 },
                    { title: "显示名", dataIndex: "display_name", width: 120 },
                    { title: "角色", dataIndex: "role", width: 100, render: roleTag },
                    { title: "状态", dataIndex: "status", width: 90, render: (_, r) => statusTag(r.status, r.locked_until) },
                    { title: "登录失败次数", dataIndex: "login_fail_count", width: 100 },
                    { title: "密码修改时间", dataIndex: "password_changed_at", width: 160, render: (v) => v || "-" },
                    { title: "锁定至", dataIndex: "locked_until", width: 160, render: (v) => v || "-" },
                    { title: "上次登录", dataIndex: "last_login_at", width: 160, render: (v) => v || "-" },
                    {
                      title: "操作", width: 300, render: (_, r) => (
                        <Space>
                          {r.username !== "admin" && <Button size="small" onClick={() => toggleStatus(r)}>{r.status === "active" ? "停用" : "启用"}</Button>}
                          <Button size="small" type="primary" ghost onClick={() => openPermModal(r)}>模块权限</Button>
                          <Button size="small" disabled={!currentUser} onClick={() => { resetForm.resetFields(); setResetModal({ open: true, userId: r.id, username: r.username }); }}>{currentUser?.id === r.id ? "修改密码" : "重置密码"}</Button>
                          {r.username !== "admin" && (r.status === "locked" || r.locked_until) && <Button size="small" type="primary" onClick={() => unlockUser(r.id)}>解锁</Button>}
                          {r.username !== "admin" && (
                            <Popconfirm title={`确定删除用户 ${r.username}？`} onConfirm={() => deleteUser(r)} okText="删除" cancelText="取消">
                              <Button size="small" danger icon={<DeleteOutlined />} />
                            </Popconfirm>
                          )}
                        </Space>
                      ),
                    },
                  ]}
                />
              </Card>
            </Space>
          ),
        },
        {
          key: "actionRoles",
          label: "角色与动作",
          children: <RoleAccessPanel users={users} />,
        },
        {
          key: "assetScopes",
          label: "资产范围",
          children: <AssetScopePanel users={users} />,
        },
        {
          key: "organizations",
          label: "组织租户",
          children: <OrganizationSecurityPanel users={users} />,
        },
        {
          key: "accountSecurity",
          label: "登录安全",
          children: <AccountSecurityPanel />,
        },
        {
          key: "securityAudit",
          label: "安全审计",
          children: <SecurityAuditPanel />,
        },
        {
          key: "modules",
          label: "模块权限",
          children: (
            <Space direction="vertical" size={16} className="full-width">
              <Alert type="info" showIcon message="为每个用户分配可访问的功能模块。admin 也可以自定义显示模块；admin 未配置任何权限时默认显示全部模块。" />
              <Card size="small">
                <Space>
                  <Button icon={<ReloadOutlined />} loading={loading} onClick={async () => { await Promise.all([loadData(), loadUserPerms()]); }}>刷新</Button>
                </Space>
              </Card>
              {users.map((u) => {
                const perms = userPerms[u.id] || new Set();
                return (
                  <Card key={u.id} title={
                    <Space>
                      <span>{u.display_name || u.username}</span>
                      <Tag color={u.role === "admin" ? "purple" : "blue"}>{u.role === "admin" ? "管理员" : "普通用户"}</Tag>
                      <a style={{ fontSize: 12 }} onClick={() => selectAllForUser(u.id)}>全选</a>
                      <a style={{ fontSize: 12 }} onClick={() => clearAllForUser(u.id)}>清空</a>
                    </Space>
                  } size="small">
                    <Checkbox.Group value={Array.from(perms)} onChange={(keys) => {
                      setUserPerms({ ...userPerms, [u.id]: new Set(keys) });
                      saveUserPermissions(u.id, keys).catch(async (e) => { message.error(e.message); await loadData(); });
                    }} style={{ width: "100%" }}>
                      <table style={{ width: "100%", borderCollapse: "collapse" }}>
                        {permissionModules.map((m) => (
                          <tr key={m.module_key} style={{ borderBottom: "1px solid #f0f0f0", height: 36 }}>
                            <td style={{ width: 30, textAlign: "center" }}><Checkbox value={m.module_key} /></td>
                            <td style={{ width: 120, fontWeight: 500 }}>{m.module_name}</td>
                            <td style={{ width: 70 }}>{catTag(m.category)}</td>
                            <td style={{ color: "#999", fontSize: 12 }}>{getModuleDesc(m.module_key)}</td>
                          </tr>
                        ))}
                      </table>
                    </Checkbox.Group>
                  </Card>
                );
              })}
              {users.length === 0 && (
                <Card size="small"><Empty description="暂无用户，请先在用户管理中创建用户" /></Card>
              )}
            </Space>
          ),
        },
        {
          key: "moduleConfig",
          label: "模块配置",
          children: (
            <Space direction="vertical" size={16} className="full-width">
              <Alert type="info" showIcon message="全局控制功能模块的显示/隐藏。隐藏后所有用户菜单都不显示该模块（API 接口仍可用）。与「模块权限」区别：这里是系统级开关，模块权限是按用户授权。" />
              <Card size="small">
                <Space wrap>
                  <Button icon={<ReloadOutlined />} loading={loading} onClick={loadData}>刷新</Button>
                  <Button type="primary" ghost onClick={saveModuleConfig}>保存配置</Button>
                  <Button danger onClick={resetModuleConfig}>重置默认</Button>
                </Space>
              </Card>
              {[["通用/首页", "overview"], ["资产与连接", "assets"], ["巡检", "inspection"],
                ["日常运维", "operations"], ["备份恢复", "backup"], ["性能诊断", "performance"],
                ["监控告警", "monitoring"], ["AI 诊断", "ai"], ["报告中心", "reports"],
                ["系统设置", "settings"], ["其它", "__other"]]
                .map(([title, cat]) => {
                  const known = ["overview", "assets", "inspection", "operations", "backup", "performance", "monitoring", "ai", "reports", "settings"];
                  const list = cat === "__other"
                    ? modules.filter((m) => !known.includes(m.category))
                    : modules.filter((m) => m.category === cat);
                  if (list.length === 0) return null;
                  return (
                    <Card key={cat} title={`${title}（${list.length}）`} size="small">
                      <Table dataSource={list} rowKey="module_key" size="small" pagination={false}
                        columns={[
                          { title: "排序", dataIndex: "sort_order", width: 60 },
                          { title: "模块标识", dataIndex: "module_key", width: 180 },
                          { title: "模块名称", dataIndex: "module_name" },
                          { title: "状态", dataIndex: "enabled", width: 90, render: (v) => <Tag color={v ? "green" : "default"}>{v ? "显示" : "隐藏"}</Tag> },
                          { title: "操作", width: 90, render: (_, r) => (
                            <Button size="small" type={r.enabled ? "default" : "primary"} onClick={() => toggleModule(r.module_key, r.enabled)}>
                              {r.enabled ? "隐藏" : "显示"}
                            </Button>
                          )},
                        ]} />
                    </Card>
                  );
                })}
            </Space>
          ),
        },
      ]} />

      <Modal title="新增用户" open={createModal} width={500} onCancel={() => setCreateModal(false)} onOk={doCreate}>
        <Form form={createForm} layout="horizontal" labelCol={{ span: 6 }} wrapperCol={{ span: 16 }}>
          <Form.Item name="username" label="用户名" rules={[{ required: true, message: "请输入用户名" }]}>
            <Input placeholder="登录用户名" />
          </Form.Item>
          <Form.Item name="password" label="密码" rules={[{ required: true, message: "请输入密码" }]}
            extra="至少 8 位，需包含大小写字母和数字">
            <Input.Password placeholder="登录密码" />
          </Form.Item>
          <Form.Item name="display_name" label="显示名">
            <Input placeholder="显示名称" />
          </Form.Item>
          <Form.Item name="role" label="角色" rules={[{ required: true }]}>
            <Select options={[{ value: "user", label: "普通用户" }, { value: "admin", label: "管理员" }]} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal title={`模块权限 - ${permModal.username}`} open={permModal.open} width={600} onCancel={() => setPermModal({ open: false, userId: null, username: "" })} onOk={savePerms}>
        <Alert type="info" showIcon style={{ marginBottom: 12 }} message="选择该用户可以访问的功能模块。admin 未配置任何权限时默认显示全部模块；保存后按勾选结果显示。" />
        <Checkbox.Group value={permKeys} onChange={setPermKeys} style={{ width: "100%" }}>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            {permissionModules.map((m) => (
              <tr key={m.module_key} style={{ borderBottom: "1px solid #f0f0f0", height: 36 }}>
                <td style={{ width: 30, textAlign: "center" }}><Checkbox value={m.module_key} /></td>
                <td style={{ width: 120, fontWeight: 500 }}>{m.module_name}</td>
                <td style={{ width: 70 }}>{catTag(m.category)}</td>
                <td style={{ color: "#999", fontSize: 12 }}>{getModuleDesc(m.module_key)}</td>
              </tr>
            ))}
          </table>
        </Checkbox.Group>
      </Modal>

      <Modal title={`${resettingSelf ? "修改密码" : "重置密码"} - ${resetModal.username}`} open={resetModal.open}
        centered width={480} style={{ maxWidth: "calc(100vw - 32px)" }}
        onCancel={closeResetModal} onOk={doResetPassword} confirmLoading={resetSaving}
        cancelButtonProps={{ disabled: resetSaving }} closable={!resetSaving} maskClosable={false}
        keyboard={!resetSaving} okText={resettingSelf ? "确认修改" : "确认重置"} cancelText="取消">
        <Form form={resetForm} layout="vertical" disabled={resetSaving} preserve={false}>
          <Form.Item name="current_password" label={resettingSelf ? "当前密码" : `操作管理员当前密码（${currentUser?.username || ""}）`}
            rules={[{ required: true, message: "请输入当前登录账号的密码" }]}>
            <Input.Password autoComplete="current-password" maxLength={1024} placeholder="当前登录账号的密码" />
          </Form.Item>
          <Form.Item name="new_password" label="新密码" rules={[{ required: true, message: "请输入新密码" }]}
            extra={policy.enforce_complexity ? "至少 8 位，需包含大小写字母和数字" : undefined}>
            <Input.Password autoComplete="new-password" maxLength={1024} placeholder="新密码" />
          </Form.Item>
          <Form.Item name="confirm_password" label="确认密码" dependencies={["new_password"]} rules={[
            { required: true, message: "请确认密码" },
            ({ getFieldValue }) => ({
              validator(_, value) {
                if (!value || getFieldValue("new_password") === value) return Promise.resolve();
                return Promise.reject(new Error("两次密码不一致"));
              },
            }),
          ]}>
            <Input.Password autoComplete="new-password" maxLength={1024} placeholder="再次输入新密码" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal title="密码策略配置" open={policyModal} width={500} onCancel={() => setPolicyModal(false)} onOk={savePolicy}>
        <Form form={policyForm} layout="horizontal" labelCol={{ span: 8 }} wrapperCol={{ span: 14 }}>
          <Form.Item name="password_expire_days" label="密码过期天数"
            extra="超过此天数未修改密码，登录时将提示过期。设为 0 表示不限制">
            <InputNumber min={0} max={365} style={{ width: "100%" }} />
          </Form.Item>
          <Form.Item name="max_login_fail" label="最大登录失败次数"
            extra="连续登录失败达到此数值后自动锁定账号">
            <InputNumber min={1} max={20} style={{ width: "100%" }} />
          </Form.Item>
          <Form.Item name="lock_duration_minutes" label="锁定时长(分钟)"
            extra="账号被自动锁定后的持续时间">
            <InputNumber min={1} max={1440} style={{ width: "100%" }} />
          </Form.Item>
          <Form.Item name="enforce_complexity" label="强制密码复杂度" valuePropName="checked"
            extra="开启后密码需至少 8 位，包含大小写字母和数字">
            <Checkbox />
          </Form.Item>
        </Form>
      </Modal>
    </Space>
  );
}
