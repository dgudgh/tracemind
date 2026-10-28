# TraceMind AI

面向数据库与关键业务系统的**开源智能运维与故障分析平台**。

它把资产拓扑、数据库实例、巡检、指标、告警、日志和报告组织成一条可复核的证据链：
每个结论都能指回具体的执行记录、SQL 与结果行，而不是只给一句"系统正常"。

支持 **Oracle / MySQL / PostgreSQL / SQL Server** 四种引擎。

---

## 开源版包含什么

共 **30 个模块**，覆盖日常运维的主干：

| 领域 | 能力 |
|---|---|
| 资产管理 | 五层资产模型（Region/Site/Zone → Environment → Host → Cluster → Instance）、自动发现、CMDB 对接、人工校正、审计 |
| 巡检 | 模板、调度、执行、结果证据、报告、历史 |
| 数据库运维 | 运行概览、会话事务、空间管理、日志管理、对象统计、用户安全、配置基线（含漂移检测与整改流程） |
| 性能（基础） | 慢 SQL、锁等待、性能基线、容量分析、AWR 综合报告 |
| 告警 | 统一告警事件、容量预警、可观测总览、外部推送接入 |
| 报告 | 报告中心、巡检报告、导出 |
| 系统管理 | 用户与权限、模块开关、系统参数、AI Provider 配置、定时任务 |
| AI 能力调度 | 能力目录 + 执行计划；只读能力可直接执行，写动作须走审批 |

## 不包含什么

以下属企业版，本仓库**不含其实现**：

- 性能深度分析：工作负载报告、SQL 画像、索引/表画像、问题趋势、优化建议、AWR 历史与对比、自动采集
- 数据保护：备份恢复中心、数据抢救、归档对象存储
- 数据治理：治理候选扫描、执行计划
- 集成中心：Zabbix、Prometheus/Alertmanager、通知渠道、ITSM 工单
- 可观测进阶：日志关联、Grafana 模板、APM 链路
- 企业认证：LDAP / OIDC 单点登录
- 智能诊断进阶：状态与现场、事故中心、快照策略、现场 Agent、诊断准确度评测
- 高可用：HA 拓扑与切换编排

## 快速开始

**依赖**：Python 3.12+、Node.js 20+。默认使用 SQLite 作为平台元数据库，无需额外准备。

```bash
git clone https://github.com/dgudgh/tracemind.git
cd tracemind

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r backend/requirements.txt

cd frontend && npm ci && cd ..

FRONTEND_DEV=true python3 run.py
```

启动后：

- 前端 <http://127.0.0.1:3300> — 首次访问按引导创建管理员账号
- 后端 <http://127.0.0.1:8800>
- OpenAPI 文档 <http://127.0.0.1:8800/docs>

开源版**不需要授权文件**：`TRACEMIND_EDITION` 默认即为 `oss`。

### 仅启动后端

```bash
PYTHONPATH=$PWD/backend DATABASE_URL="sqlite:///./tracemind.db" DB_TYPE=sqlite \
  python -m uvicorn app.main:app --host 127.0.0.1 --port 8800
```

### 使用 MySQL / PostgreSQL 作为元数据库

```bash
DATABASE_URL="mysql+pymysql://user:pass@127.0.0.1:3306/tracemind" DB_TYPE=mysql \
  python3 run.py
```

生产环境请通过环境变量注入元数据库、目标库、SSH 与 AI 凭据，不要写进配置文件。

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python 3.12 · FastAPI · SQLAlchemy（229 张表）· Alembic 风格版本化迁移 |
| 前端 | React 18 · Vite · Ant Design |
| 元数据库 | SQLite（默认）/ MySQL / PostgreSQL |
| 被管引擎 | Oracle · MySQL · PostgreSQL · SQL Server |

## 目录结构

```
backend/app/api/v1/     REST 路由（按领域分模块，registry.py 统一挂载）
backend/app/services/   业务逻辑
backend/app/models/     SQLAlchemy 模型
backend/app/runtime/    引擎宿主与 Provider 协议（引擎包执行）
backend/tests/          后端测试
frontend/src/           前端页面与组件
docs/                   架构、后端、前端、运维文档
scripts/                静态检查与文档门禁
```

## 测试与门禁

```bash
# 后端
PYTHONPATH=$PWD/backend DATABASE_URL="sqlite:///./test.db" DB_TYPE=sqlite \
  python -m pytest backend/tests -q

# 静态检查 + 契约基线 + 文档门禁
python3 scripts/check_all.py --fast

# 前端
cd frontend && npm run build
```

## 参与贡献

见 [CONTRIBUTING.md](CONTRIBUTING.md)。安全问题请按 [SECURITY.md](SECURITY.md) 私下报告，不要开公开 issue。

## 许可证

[GNU Affero General Public License v3.0](LICENSE)（AGPL-3.0）。

你可以自由使用、修改、分发本项目，包括用于商业用途。**若你把它作为网络服务提供给第三方使用，
必须按 AGPL-3.0 公开你对本项目的修改**。

如需在不承担 AGPL 义务的前提下集成（例如闭源分发、OEM 嵌入），需要商业授权，
请通过仓库 Issues 联系维护者。
