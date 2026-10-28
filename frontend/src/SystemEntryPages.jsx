import React, { useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Form,
  Input,
  InputNumber,
  Modal,
  Row,
  Space,
  Tag,
  Typography,
  message,
} from "antd";
import {
  CheckCircleOutlined,
  LockOutlined,
  TeamOutlined,
  ThunderboltOutlined,
  UploadOutlined,
} from "@ant-design/icons";
import { authApi, setupApi } from "./apiClients";
import { copyText } from "./modules/clipboard";
import { navigateCurrent, reloadPage, reloadToLogin } from "./modules/navigation";

const { Text } = Typography;
function LoginPage({ onLogin, onMfaLogin, onExternalLogin }) {
  const [loading, setLoading] = useState(false);
  const [enterpriseLoading, setEnterpriseLoading] = useState(false);
  const [enterpriseLoginOpen, setEnterpriseLoginOpen] = useState(false);
  const [oidcProviders, setOidcProviders] = useState([]);
  const [oidcLoading, setOidcLoading] = useState(null);
  const [mfaChallenge, setMfaChallenge] = useState(null);
  const [mfaCode, setMfaCode] = useState("");
  const [mfaLoading, setMfaLoading] = useState(false);
  const [licenseInfo, setLicenseInfo] = useState(null);
  const licenseInputRef = useRef(null);
  const [form] = Form.useForm();
  const [enterpriseForm] = Form.useForm();

  useEffect(() => {
    setupApi.license().then((d) => setLicenseInfo(d)).catch(() => setLicenseInfo(null));
    authApi.oidcProviders()
      .then((data) => setOidcProviders(data.items || []))
      .catch(() => setOidcProviders([]));
  }, []);

  const beginOidcLogin = async (providerId) => {
    setOidcLoading(providerId);
    try {
      const data = await authApi.oidcStart(providerId);
      navigateCurrent(data.authorization_url);
    } catch (error) {
      message.error(error.message || "单点登录启动失败");
      setOidcLoading(null);
    }
  };

  const handleLogin = async () => {
    const values = await form.validateFields();
    setLoading(true);
    try {
      const result = await onLogin(values.username, values.password);
      if (result?.mfa_required) {
        setMfaChallenge(result);
        setMfaCode("");
      }
    } catch (e) {
      if (e.licenseBlocked) {
        message.error("授权未激活，请先配置授权文件");
        return;
      }
      message.error(e.message || "登录失败");
    } finally {
      setLoading(false);
    }
  };

  const handleMfaLogin = async () => {
    if (!mfaCode.trim()) {
      message.error("请输入动态验证码或恢复码");
      return;
    }
    setMfaLoading(true);
    try {
      await onMfaLogin(mfaChallenge.challenge_token, mfaCode.trim());
      setMfaChallenge(null);
      setMfaCode("");
    } catch (error) {
      message.error(error.message || "MFA 验证失败");
    } finally {
      setMfaLoading(false);
    }
  };

  const handleEnterpriseLogin = async () => {
    const values = await enterpriseForm.validateFields();
    setEnterpriseLoading(true);
    try {
      await onExternalLogin(values.username, values.password);
      setEnterpriseLoginOpen(false);
    } catch (error) {
      message.error(error.message || "企业账号登录失败");
    } finally {
      setEnterpriseLoading(false);
    }
  };

  const licenseReason = licenseInfo?.reason === "license_not_found" ? "未检测到授权文件，请导入 license.json 后继续。"
    : licenseInfo?.reason === "expired" ? `授权已于 ${licenseInfo.expire_date} 过期，请联系管理员续期。`
    : licenseInfo?.reason === "machine_mismatch" ? "授权文件与当前机器不匹配，请联系管理员重新授权。"
    : licenseInfo?.reason === "signature_invalid" ? "授权文件签名无效，文件可能被篡改。"
    : "授权未激活，请联系管理员获取授权。";

  const handleUploadLicense = (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = async (ev) => {
      try {
        const data = await setupApi.uploadLicenseContent(ev.target.result);
        if (data.ok) {
          message.success(data.message);
          reloadPage(1000);
        } else {
          message.error(data.message);
        }
      } catch (err) {
        message.error("上传失败: " + err.message);
      }
      e.target.value = "";
    };
    reader.readAsText(file);
  };

  return (
    <main className="tracemind-login-shell">
      <aside className="tracemind-login-panel" aria-label="登录">
        <div className="tracemind-login-compact-brand">
          <span className="tracemind-login-brand-icon"><ThunderboltOutlined /></span>
          <div>
            <strong>TraceMind AI</strong>
          </div>
        </div>
        <div className="tracemind-login-panel-head">
          <span><LockOutlined /></span>
          <div>
            <Text>安全入口</Text>
            <h2>进入 AI 诊断工作台</h2>
          </div>
        </div>
        <Form form={form} layout="vertical" className="tracemind-login-form">
          <Form.Item name="username" label="用户名" rules={[{ required: true }]}>
            <Input prefix={<TeamOutlined />} placeholder="admin" autoComplete="username" />
          </Form.Item>
          <Form.Item name="password" label="密码" rules={[{ required: true }]}>
            <Input.Password prefix={<LockOutlined />} placeholder="请输入密码" onPressEnter={handleLogin} autoComplete="current-password" />
          </Form.Item>
          <Button type="primary" block loading={loading} onClick={handleLogin}>
            打开诊断工作台
          </Button>
          <Button
            type="link"
            className="tracemind-enterprise-login-link"
            icon={<TeamOutlined />}
            onClick={() => setEnterpriseLoginOpen(true)}
          >
            企业账号登录
          </Button>
        </Form>

        {licenseInfo && !licenseInfo.valid && (
          <Alert
            className="tracemind-license-alert"
            type="warning"
            showIcon
            message="授权未激活"
            description={
              <div className="tracemind-license-body">
                <p>{licenseReason}</p>
                <div className="tracemind-machine-code">
                  <span>机器码</span>
                  <code>{licenseInfo.machine_code}</code>
                  <Button size="small" type="link" onClick={() => copyText(licenseInfo.machine_code || "").then(() => message.success("机器码已复制到剪贴板")).catch(() => message.error("复制失败"))}>
                    复制
                  </Button>
                </div>
                <input type="file" accept=".json" ref={licenseInputRef} style={{ display: "none" }} onChange={handleUploadLicense} />
                <Button ghost icon={<UploadOutlined />} onClick={() => licenseInputRef.current?.click()}>
                  导入授权文件
                </Button>
              </div>
            }
          />
        )}
        {licenseInfo && licenseInfo.valid && (
          <div className="tracemind-license-ok">
            <CheckCircleOutlined />
            <span>已授权 · {licenseInfo.customer || "未命名客户"} · 有效期至 {licenseInfo.expire_date}</span>
          </div>
        )}
      </aside>
      <Modal
        centered
        width={440}
        title="双重身份验证"
        open={Boolean(mfaChallenge)}
        destroyOnHidden
        maskClosable={false}
        onCancel={() => { setMfaChallenge(null); setMfaCode(""); }}
        footer={[
          <Button key="cancel" onClick={() => { setMfaChallenge(null); setMfaCode(""); }}>返回</Button>,
          <Button key="verify" type="primary" loading={mfaLoading} onClick={handleMfaLogin}>验证并登录</Button>,
        ]}
      >
        <Alert
          showIcon
          type="info"
          message={`账号 ${mfaChallenge?.username || ""} 已启用 MFA`}
          description="请输入身份验证器生成的 6 位动态验证码，也可以使用一次性恢复码。"
          style={{ marginBottom: 16 }}
        />
        <Input
          autoFocus
          value={mfaCode}
          maxLength={20}
          placeholder="动态验证码或恢复码"
          autoComplete="one-time-code"
          onChange={(event) => setMfaCode(event.target.value)}
          onPressEnter={handleMfaLogin}
        />
      </Modal>
      <Modal
        className="tracemind-enterprise-login-modal"
        title="企业账号登录"
        open={enterpriseLoginOpen}
        footer={null}
        destroyOnHidden
        onCancel={() => setEnterpriseLoginOpen(false)}
      >
        <Form form={enterpriseForm} layout="vertical" className="tracemind-enterprise-login-form">
          <Form.Item name="username" label="用户名" rules={[{ required: true }]}>
            <Input prefix={<TeamOutlined />} autoComplete="username" />
          </Form.Item>
          <Form.Item name="password" label="密码" rules={[{ required: true }]}>
            <Input.Password
              prefix={<LockOutlined />}
              autoComplete="current-password"
              onPressEnter={handleEnterpriseLogin}
            />
          </Form.Item>
          <Button type="primary" block loading={enterpriseLoading} onClick={handleEnterpriseLogin}>
            企业账号登录
          </Button>
          {oidcProviders.length > 0 && (
            <div className="tracemind-oidc-actions" aria-label="企业单点登录">
              <div className="tracemind-login-divider"><span>或使用单点登录</span></div>
              <Space direction="vertical" size={8} style={{ width: "100%" }}>
                {oidcProviders.map((provider) => (
                  <Button
                    key={provider.id}
                    block
                    icon={<TeamOutlined />}
                    loading={oidcLoading === provider.id}
                    disabled={Boolean(oidcLoading && oidcLoading !== provider.id)}
                    onClick={() => beginOidcLogin(provider.id)}
                  >
                    使用 {provider.name} 单点登录
                  </Button>
                ))}
              </Space>
            </div>
          )}
        </Form>
      </Modal>
    </main>
  );
}

function SystemInitPage() {
  const [loading, setLoading] = useState(false);
  const [testing, setTesting] = useState(false);
  const [initStatus, setInitStatus] = useState(null);
  const [adminUser, setAdminUser] = useState("admin");
  const [adminPass, setAdminPass] = useState("");
  const [adminPass2, setAdminPass2] = useState("");
  const [message, setMessage] = useState(null);
  const [mysqlConfig, setMysqlConfig] = useState({
    host: "127.0.0.1",
    port: 3306,
    username: "root",
    password: "",
    database: "mestools",
  });

  const checkStatus = async () => {
    try {
      const r = await setupApi.initStatus();
      setInitStatus(r);
      if (r.initialized) {
        setMessage({ type: "success", text: "系统已初始化，即将跳转登录页..." });
        reloadToLogin(2000);
      }
    } catch (e) { setInitStatus({ initialized: false, app_mode: "exe_sqlite", db_type: "sqlite" }); }
  };

  useEffect(() => { checkStatus(); }, []);

  const isSqliteMode = !initStatus || initStatus?.db_type === "sqlite" || initStatus?.app_mode === "exe_sqlite";
  const updateMysqlConfig = (key, value) => setMysqlConfig((prev) => ({ ...prev, [key]: value }));
  const validateAdmin = () => {
    if (!adminUser.trim()) { setMessage({ type: "error", text: "请输入管理员账号" }); return false; }
    if (!adminPass || adminPass.length < 8) { setMessage({ type: "error", text: "密码至少 8 位" }); return false; }
    if (adminPass !== adminPass2) { setMessage({ type: "error", text: "两次密码不一致" }); return false; }
    return true;
  };

  const doSetup = async () => {
    if (!validateAdmin()) return;
    setLoading(true);
    try {
      const r = await setupApi.setupAdmin({ admin_username: adminUser, admin_password: adminPass });
      if (r.status === "success") {
        setMessage({ type: "success", text: "初始化成功，即将跳转登录页..." });
        reloadToLogin(2000);
      } else {
        setMessage({ type: "error", text: r.message });
      }
    } catch (e) { setMessage({ type: "error", text: e.message }); }
    setLoading(false);
  };

  const testMysqlConnection = async () => {
    setTesting(true);
    try {
      const r = await setupApi.testConnection(mysqlConfig);
      setMessage({ type: r.ok ? "success" : "error", text: r.message });
    } catch (e) {
      setMessage({ type: "error", text: e.message });
    } finally {
      setTesting(false);
    }
  };

  const doMysqlSetup = async () => {
    if (!validateAdmin()) return;
    if (!mysqlConfig.host || !mysqlConfig.username || !mysqlConfig.database) {
      setMessage({ type: "error", text: "请完整填写数据库地址、账号和库名" });
      return;
    }
    setLoading(true);
    try {
      const r = await setupApi.initDatabase({
        ...mysqlConfig,
        admin_username: adminUser,
        admin_password: adminPass,
      });
      if (r.ok) {
        setMessage({ type: "success", text: "初始化成功，正在重启服务..." });
        try {
          await setupApi.restart();
        } catch {
          setMessage({ type: "success", text: "初始化成功，请手工重启服务后登录" });
        }
        reloadToLogin(3000);
      } else {
        setMessage({ type: "error", text: r.message });
      }
    } catch (e) {
      setMessage({ type: "error", text: e.message });
    } finally {
      setLoading(false);
    }
  };

  return (
    <main className="system-init-shell">
      <Card title="系统初始化" className="system-init-card">
        <Space direction="vertical" size={16} style={{ width: "100%" }}>
          {initStatus && (
            <Descriptions bordered size="small" column={1}>
              <Descriptions.Item label="当前模式"><Tag color="blue">{initStatus.app_mode === "exe_sqlite" ? "SQLite 测试模式" : initStatus.app_mode}</Tag></Descriptions.Item>
              <Descriptions.Item label="数据库类型"><Tag>{initStatus.db_type}</Tag></Descriptions.Item>
              {initStatus.database_file && <Descriptions.Item label="数据库文件"><Text code>{initStatus.database_file}</Text></Descriptions.Item>}
            </Descriptions>
          )}
          <Alert type="info" showIcon message={isSqliteMode ? "SQLite 仅用于 EXE 测试、单机演示和功能验证。正式部署请使用外部 MySQL / MariaDB。" : "正式部署模式将连接 MySQL / MariaDB，创建业务库、表结构和管理员账号。"} />
          {message && <Alert type={message.type} showIcon message={message.text} />}
          {!isSqliteMode && (
            <>
              <Row gutter={[12, 12]}>
                <Col span={16}>
                  <div style={{ marginBottom: 8 }}><Text strong>数据库地址</Text></div>
                  <Input value={mysqlConfig.host} onChange={(e) => updateMysqlConfig("host", e.target.value)} placeholder="127.0.0.1" />
                </Col>
                <Col span={8}>
                  <div style={{ marginBottom: 8 }}><Text strong>端口</Text></div>
                  <InputNumber value={mysqlConfig.port} min={1} max={65535} onChange={(value) => updateMysqlConfig("port", value || 3306)} style={{ width: "100%" }} />
                </Col>
                <Col span={12}>
                  <div style={{ marginBottom: 8 }}><Text strong>数据库账号</Text></div>
                  <Input value={mysqlConfig.username} onChange={(e) => updateMysqlConfig("username", e.target.value)} placeholder="root" />
                </Col>
                <Col span={12}>
                  <div style={{ marginBottom: 8 }}><Text strong>数据库密码</Text></div>
                  <Input.Password value={mysqlConfig.password} onChange={(e) => updateMysqlConfig("password", e.target.value)} placeholder="请输入密码" />
                </Col>
                <Col span={24}>
                  <div style={{ marginBottom: 8 }}><Text strong>业务库名</Text></div>
                  <Input value={mysqlConfig.database} onChange={(e) => updateMysqlConfig("database", e.target.value)} placeholder="mestools" />
                </Col>
              </Row>
              <Button block loading={testing} onClick={testMysqlConnection}>
                测试数据库连接
              </Button>
            </>
          )}
          <div>
            <div style={{ marginBottom: 8 }}><Text strong>管理员账号</Text></div>
            <Input value={adminUser} onChange={(e) => setAdminUser(e.target.value)} placeholder="admin" />
          </div>
          <div>
            <div style={{ marginBottom: 8 }}><Text strong>管理员密码</Text></div>
            <Input.Password value={adminPass} onChange={(e) => setAdminPass(e.target.value)} placeholder="至少 8 位" />
          </div>
          <div>
            <div style={{ marginBottom: 8 }}><Text strong>确认密码</Text></div>
            <Input.Password value={adminPass2} onChange={(e) => setAdminPass2(e.target.value)} placeholder="再次输入密码" />
          </div>
          <Button type="primary" block size="large" loading={loading} onClick={isSqliteMode ? doSetup : doMysqlSetup} disabled={initStatus?.initialized}>
            {initStatus?.initialized ? "已初始化" : "初始化系统"}
          </Button>
        </Space>
      </Card>
    </main>
  );
}

export { LoginPage, SystemInitPage };
