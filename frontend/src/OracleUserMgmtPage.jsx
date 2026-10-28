import React, { useEffect, useMemo, useState } from "react";
import { Button, Card, Space, Table, Tabs, Tag, message } from "antd";
import { oracleOpsApi } from "./apiClients";
import { PlanOnlyScriptModal, useFirstAvailableInstanceId as useSyncedInstanceId } from "./OpsLayout";

export function UserMgmtPage({ instances, selectedInstanceId }) {
  const oracleInstances = useMemo(() => instances.filter((item) => item.db_type === "oracle"), [instances]);
  const [instanceId] = useSyncedInstanceId(selectedInstanceId, oracleInstances);
  const [loading, setLoading] = useState(false);
  const [users, setUsers] = useState([]);
  const [roles, setRoles] = useState([]);
  const [privUser, setPrivUser] = useState(null);
  const [privs, setPrivs] = useState(null);
  const [scriptModal, setScriptModal] = useState({ open: false, title: "", content: "", warnings: [] });

  const loadUsers = async () => {
    if (!instanceId) return;
    setLoading(true);
    try {
      const [userResp, roleResp] = await Promise.all([
        oracleOpsApi.users(instanceId),
        oracleOpsApi.roles(instanceId),
      ]);
      setUsers(userResp.users);
      setRoles(roleResp.roles);
    } catch (e) { message.error(e.message); }
    finally { setLoading(false); }
  };
  useEffect(() => { if (instanceId) loadUsers(); }, [instanceId]);

  const showPrivs = async (username) => {
    try { setPrivs(await oracleOpsApi.userPrivileges(instanceId, username)); setPrivUser(username); }
    catch (e) { message.error(e.message); }
  };

  const genScript = (title, sql, warnings) => setScriptModal({ open: true, title, content: sql, warnings: warnings || [] });

  return (
    <Space direction="vertical" size={16} className="full-width">
      
      <Card title={`用户列表 (${users.length})`}>
        <Table dataSource={users} rowKey="username" size="small" pagination={{ defaultPageSize: 15 }} scroll={{ x: true }}
          columns={[
            { title: "用户名", dataIndex: "username" }, { title: "状态", dataIndex: "account_status", render: (v) => <Tag color={v === "OPEN" ? "green" : v.includes("LOCKED") ? "red" : "orange"}>{v}</Tag> },
            { title: "默认表空间", dataIndex: "default_tablespace" }, { title: "临时表空间", dataIndex: "temporary_tablespace" },
            { title: "Profile", dataIndex: "profile" }, { title: "创建时间", dataIndex: "created" },
            { title: "操作", width: 200, render: (_, r) => <Space>
              <Button size="small" onClick={() => showPrivs(r.username)}>权限</Button>
              <Button size="small" onClick={() => genScript("锁定/解锁", `ALTER USER ${r.username} ACCOUNT LOCK;`, [])}>锁定</Button>
              <Button size="small" onClick={() => genScript("锁定/解锁", `ALTER USER ${r.username} ACCOUNT UNLOCK;`, [])}>解锁</Button>
              <Button size="small" danger onClick={() => genScript("删除用户", `DROP USER ${r.username};`, ["此操作不可逆！"])}>删除</Button>
            </Space> },
          ]} />
      </Card>
      {roles.length > 0 && <Card title={`角色列表 (${roles.length})`}><Table dataSource={roles} rowKey="role" size="small"
        columns={[{ title: "角色", dataIndex: "role" }, { title: "需要密码", dataIndex: "password_required" }]} /></Card>}
      {privs && <Card title={`${privUser} 权限详情`} extra={<Button size="small" onClick={() => setPrivs(null)}>关闭</Button>}>
        <Tabs items={[
          { key: "sys", label: `系统权限 (${privs.system_privileges.length})`, children: <Table dataSource={privs.system_privileges} rowKey="privilege" size="small" columns={[{ title: "权限", dataIndex: "privilege" }, { title: "管理员", dataIndex: "admin_option" }]} /> },
          { key: "role", label: `角色 (${privs.role_privileges.length})`, children: <Table dataSource={privs.role_privileges} rowKey="granted_role" size="small" columns={[{ title: "角色", dataIndex: "granted_role" }, { title: "管理员", dataIndex: "admin_option" }, { title: "默认", dataIndex: "default_role" }]} /> },
          { key: "obj", label: `对象权限 (${privs.object_privileges.length})`, children: <Table dataSource={privs.object_privileges} rowKey={(r) => `${r.owner}.${r.table_name}.${r.privilege}`} size="small" columns={[{ title: "Owner", dataIndex: "owner" }, { title: "对象", dataIndex: "table_name" }, { title: "权限", dataIndex: "privilege" }]} /> },
        ]} />
      </Card>}
      <PlanOnlyScriptModal
        title={scriptModal.title}
        open={scriptModal.open}
        script={scriptModal.content}
        warnings={scriptModal.warnings}
        width={700}
        onClose={() => setScriptModal({ open: false, title: "", content: "", warnings: [] })}
      />
    </Space>
  );
}
