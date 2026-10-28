# TraceMind AI 客户现场发布、补丁与回滚

本文定义独立平台在客户现场的版本管理架构和标准操作流程。目标是在离线网络、有限维护窗口和数据库不可丢失的条件下，支持完整安装包、增量补丁、升级预检、自动回退和审计追溯。MES 嵌入与交互继续冻结，不属于本轮升级体系。

## 1. 架构原则

1. 程序、配置和数据分离。版本目录只读，配置、平台数据库、日志和客户数据放在共享目录。
2. 更新器独立。`TraceMindRelease` 从安装介质或固定维护目录运行，不依赖当前正在运行的应用版本。
3. 包必须可信。正式整包和补丁使用 Ed25519 私钥签名，客户现场只保存公钥。
4. 补丁必须精确匹配基线。补丁同时绑定源 `release_id` 和目标 `release_id`，不能凭版本名模糊覆盖。
5. 先验证后切换。补丁先在临时目录合成为完整目标版本并逐文件校验，不直接修改当前版本。
6. 先停写后备份。涉及 Schema 的补丁必须完成停写，再生成一致性备份，再迁移和切换。
7. 切换必须原子化。活动版本由一个小型 JSON 指针表示，禁止覆盖正在运行的目录。
8. 健康检查决定成败。补丁必须通过 `/health/ready`，失败自动恢复旧程序、配置和平台数据库。
9. 数据库变更采用 Expand、Migrate、Contract。可回滚窗口内不得立即删除旧字段、旧索引或旧协议。
10. 所有动作可审计。发布编号、补丁编号、签名 Key ID、源版本、目标版本、备份编号和结果均写入本地发布历史。

## 2. 客户现场目录

```text
<install-root>/
├── active-release.json          # 当前活动版本指针
├── release-state.json           # 最近 200 次发布、补丁和回滚记录
├── .release.lock                # 防止并发升级和回滚
├── trusted-keys/
│   ├── release-2026.pub         # 客户现场只保存公钥
│   └── release-2027.pub         # 密钥轮换期可并存
├── updater/
│   └── TraceMindRelease         # 固定维护入口，可由新介质替换
├── releases/
│   ├── 1.0.0-<digest>/          # 不可变完整版本
│   └── 1.0.1-<digest>/
├── backups/
│   └── <backup-id>/
│       ├── backup-manifest.json
│       ├── database/
│       ├── external-database/
│       └── config/
└── shared/
    ├── config/app.env
    ├── data/
    ├── db/
    └── logs/
```

升级不会覆盖 `shared/config/app.env`。备份清单记录 SQLite、外部数据库导出物和配置文件的逐文件 SHA-256、大小、权限或符号链接目标；恢复前先完整验算，任何损坏都会拒绝恢复。

### 2.1 升级体系的组件边界

| 组件 | 部署位置 | 责任 | 故障边界 |
| --- | --- | --- | --- |
| Release Center | 平台控制面 | 现场版本盘点、兼容评估、审批、灰度批次、进度和验收报告 | 不直接覆盖程序，不保存签名私钥 |
| Artifact Verifier | 现场更新器 | 验签、SBOM/来源检查、基线与兼容性校验、临时物化 | 验证失败时无副作用 |
| Upgrade Executor | 每个控制面节点 | 排空、备份、并排版本安装、原子切换、健康检查和回退 | 独立于当前应用进程，应用启动失败也能运行 |
| Migration Runner | 单次升级租约持有者 | Schema 预检、Expand/Migrate/Contract、检查点和恢复判定 | 同一 revision 全集群只执行一次 |
| Agent Updater | 受管主机 | Agent/探针包验签、分批更新、本地健康回报和上一版缓存 | 动态探针、eBPF 失败不能拖垮 Agent 主进程 |

现场更新器是一个小而稳定的独立程序，应用自身不得担任唯一更新器。更新器协议版本必须写入制品，旧更新器不理解新协议时应在预检阶段拒绝，不能边更新自己边修改业务程序。

### 2.2 生产环境双交付模式

生产平台同时支持原生二进制包和 Docker/Podman 容器部署，但两种方式必须共用同一份版本身份、兼容合同、数据库迁移、健康检查、回滚和审计逻辑，禁止形成两套功能不一致的产品。

| 组件 | 原生模式 | 容器模式 | 正式决策 |
| --- | --- | --- | --- |
| Web/API/Worker/Scheduler | 签名离线包 + systemd/Windows Service/launchd | 签名 OCI 镜像 + Compose/Podman/Kubernetes | 两种部署都是一等公民 |
| 高可用组件 | 独立原生二进制，由 OS 服务管理 | 仍可作为宿主机二进制，通过受控端口与平台通信 | 默认不依赖容器运行时，避免平台容器故障时失去仲裁与恢复能力 |
| Agent/eBPF/主机采集 | 原生二进制 | 宿主机原生二进制 | 需要访问内核、进程和本机工具，不随平台容器化 |
| Updater/rescue | 原生二进制 | 宿主机原生二进制 | 平台和容器引擎异常时仍能恢复 |
| 平台元数据库 | 外置 MySQL/PostgreSQL | 外置 MySQL/PostgreSQL | 生产不把数据库生命周期绑定到应用包或容器 |

更新器内部提供统一 `DeploymentDriver` 接口。`NativeDriver` 将新包解压到新的不可变版本目录，验证后原子切换活动指针；`ContainerDriver` 按已验签的 image digest 拉起新容器，通过 readiness 后切换服务，失败时恢复上一 digest。所谓“替换安装包”不表示就地覆盖正在运行的文件。

## 3. 发布制品

### 3.1 完整发布包

完整包包含应用、静态资源、默认配置模板、独立更新器和 `tracemind-release.json`。清单包含：

- 产品、`MAJOR.MINOR.PATCH` 版本和唯一 `release_id`
- 目标操作系统、CPU 架构、入口文件和 Schema Revision
- 每个文件的路径、类型、大小、权限和 SHA-256
- Ed25519 签名、签名算法和 Key ID

完整包可以有两种载体：

```text
native-offline-bundle/
├── bin/
├── static/
├── migrations/
├── service/
├── updater/
└── tracemind-release.json

container-offline-bundle/
├── images/*.oci.tar
├── deploy/compose|podman|helm/
├── migrations/
├── updater/
└── tracemind-release.json
```

容器包的清单记录镜像 digest，不依赖可覆盖的 tag 作为唯一版本身份。两种载体使用同一 `release_id` 和 Schema Revision，以保证功能、迁移和验收结果一致。

### 3.2 增量补丁

一个补丁目录包含：

```text
patch-1.0.0-to-1.0.1/
├── tracemind-patch.json
└── payload/
    └── 仅保存新增或变化的文件
```

补丁清单包含：

- 精确源 `base_release_id` 和目标 `target_release_id`
- 源版本、目标版本和更新器协议版本
- 新增、替换、删除操作
- 目标完整发布清单
- Schema 起止版本和是否需要迁移
- 是否需要重启、发布说明和补丁唯一编号
- 补丁签名和目标发布清单签名

删除操作只删除清单中的程序文件，不允许删除目录、共享配置或共享数据。

### 3.3 制品分类与使用策略

| 制品 | 用途 | 基线约束 | 建议 |
| --- | --- | --- | --- |
| 签名完整包 | 首次安装、跨大版本、现场偏离修复 | 校验平台、架构、Schema 起点和支持路径 | 作为每个受支持版本的最终兜底制品 |
| 累计服务包 | 常规季度/月度升级 | 允许从一组明确基线到同一目标版本 | 客户现场默认选项，避免串行安装多个小补丁 |
| 精确基线热补丁 | P0/P1 缺陷紧急修复 | 必须精确匹配 `base_release_id` | 只在已知现场基线上使用，不长期堆叠 |
| 内容/规则包 | 巡检模板、状态规则、知识库、报告样式 | 绑定内容 Schema 和最低/最高产品版本 | 与代码升级分离，支持独立回滚 |
| Agent/探针包 | Agent、Parser、eBPF 对象和签名探针 | 绑定 Agent 协议、OS/架构/内核能力 | 按站点和资产组分批，不与控制面一次全量切换 |

原则上，大版本使用完整包，常规维护使用累计服务包，紧急缺陷才使用精确基线热补丁。不允许客户现场依赖“从 1.0.1 连续打到 1.0.19”的长补丁链。

“热补丁”只表示紧急且改动范围小的制品，不表示允许在运行中的 Python/EXE 进程内就地改代码。程序和依赖变更默认需要受控重启；只有经过内容 Schema 校验的模板、规则、知识和报告样式可热加载，且必须保留上一内容版本以便独立回滚。

### 3.4 版本身份与兼容合同

每个运行中的现场必须同时报告：

- 产品语义版本、构建号和内容哈希 `release_id`
- 平台 Schema Revision、API 协议版本、Agent 协议版本和内容 Schema 版本
- OS/架构、元数据库类型与版本、插件/适配器版本
- 站点 ID、环境、节点角色、当前补丁级别和配置指纹

发布制品必须携带可机器读取的升级图，明确 `from -> to`、是否支持跨版、需要的更新器协议、可兼容的 Agent N/N-1 范围、Schema 约束和是否允许程序回滚。缺少路径或跨越未支持中间版本时，预检必须给出明确的中转版本，不得盲目继续。

## 4. 发布密钥

密钥只在受控发布环境生成：

```bash
python scripts/release_manager.py keygen \
  --private-key /secure/release-2026.key \
  --public-key ./release-2026.pub
```

- 私钥进入 CI/CD Secret 或离线签名机，不进入 Git、安装包、日志和客户现场。
- 公钥随首次安装介质交付，并由实施人员核对独立渠道公布的指纹。
- 轮换时先交付新公钥，过渡期同时信任新旧公钥，确认全部现场升级后再撤销旧公钥。
- 私钥泄露时停止使用该 Key ID，发布吊销通知和新公钥，不允许继续签发补丁。

macOS 和 Windows 构建脚本通过环境变量启用正式签名：

```bash
export RELEASE_VERSION=1.0.1
export RELEASE_SIGNING_PRIVATE_KEY=/secure/release-2026.key
export RELEASE_TRUSTED_PUBLIC_KEY=/secure/release-2026.pub
./scripts/build_macos_app.sh
```

```bat
set RELEASE_VERSION=1.0.1
set RELEASE_SIGNING_PRIVATE_KEY=D:\secure\release-2026.key
set RELEASE_TRUSTED_PUBLIC_KEY=D:\secure\release-2026.pub
scripts\build-exe-sqlite-onefile.bat
```

只设置其中一个变量会直接终止构建。

## 5. 制作补丁

基础版本和目标版本都必须先生成并验证发布清单：

```bash
python scripts/release_manager.py patch-build \
  --base ./release-1.0.0 \
  --target ./release-1.0.1 \
  --output ./patch-1.0.0-to-1.0.1 \
  --signing-private-key /secure/release-2026.key \
  --release-notes "修复 Oracle 巡检结果解析与资产拓扑识别"
```

构建器只收集变化文件，并把目标完整清单一并签名。目标版本默认必须高于基础版本。仅在经过审批的灾难恢复场景中使用 `--allow-downgrade`。

发布前使用公钥复核：

```bash
python scripts/release_manager.py patch-verify \
  --package ./patch-1.0.0-to-1.0.1 \
  --trusted-public-key ./release-2026.pub
```

## 6. 客户现场补丁流程

### 6.1 上传与隔离

1. 将压缩包上传到客户现场的临时维护目录。
2. 校验外层 SHA-256，并解压到只允许运维账号写入的目录。
3. 不从应用进程目录直接运行更新器。
4. 确认客户现场公钥来自首次交付或已批准的密钥轮换。

### 6.2 无副作用预检

```bash
./TraceMindRelease patch-preflight \
  --install-root /opt/tracemind \
  --package ./patch-1.0.0-to-1.0.1 \
  --trusted-public-key /opt/tracemind/trusted-keys/release-2026.pub
```

预检会完成：

1. 校验补丁和目标发布清单双重签名。
2. 检查当前活动 `release_id` 是否精确等于补丁基线。
3. 在临时目录复制旧版本并应用增量操作。
4. 对合成后的目标版本执行完整文件、入口、平台、架构和磁盘空间检查。
5. 返回是否需要重启、迁移、停写和数据库备份。
6. 删除临时目录，不切换活动版本，不迁移数据库，不写发布历史。

预检不通过时不得进入维护窗口。现场版本偏离时重新生成对应基线补丁，或使用签名完整包升级，不允许手工改补丁清单。

### 6.3 执行补丁

不改 Schema 的补丁：

```bash
./TraceMindRelease patch-install \
  --install-root /opt/tracemind \
  --package ./patch-1.0.0-to-1.0.1 \
  --trusted-public-key /opt/tracemind/trusted-keys/release-2026.pub \
  --quiesce-command '/opt/tracemind/bin/stop-tracemind' \
  --activation-command '/opt/tracemind/bin/start-tracemind' \
  --health-url http://127.0.0.1:8800/health/ready
```

修改平台 Schema 的补丁：

```bash
./TraceMindRelease patch-install \
  --install-root /opt/tracemind \
  --package ./patch-1.0.0-to-1.1.0 \
  --trusted-public-key /opt/tracemind/trusted-keys/release-2026.pub \
  --database-url mysql+pymysql://configured-by-environment/platform \
  --quiesce-command '/opt/tracemind/bin/stop-tracemind' \
  --backup-command '/opt/tracemind/bin/backup-platform-db' \
  --restore-command '/opt/tracemind/bin/restore-platform-db' \
  --migration-command '/opt/tracemind/bin/migrate-platform-db' \
  --activation-command '/opt/tracemind/bin/start-tracemind' \
  --health-url http://127.0.0.1:8800/health/ready
```

数据库凭据必须由 Secret、受限环境变量或操作系统凭据存储提供，不得出现在脚本、发布清单、补丁清单和发布记录中。备份与恢复脚本通过 `TRACEMIND_BACKUP_DIR` 读取本次备份目录。

### 6.4 固定事务顺序

```text
验签和基线检查
  -> 临时物化完整目标版本
  -> 完整校验和兼容性预检
  -> 获取独占升级锁
  -> 停写或停止旧服务
  -> 一致性备份平台数据库与共享配置
  -> 原子切换 active-release.json
  -> 执行版本化数据库迁移
  -> 激活新版本
  -> 轮询 /health/ready
  -> 成功记账，或恢复数据库、配置和旧版本
```

补丁安装强制要求 readiness。声明需要重启的补丁强制要求激活命令。Schema 变化强制要求迁移命令、数据库地址和停写命令；外部 MySQL/PostgreSQL 元数据库还强制要求备份、恢复命令同时存在。

### 6.5 现场升级状态机

```text
IMPORTED -> ASSESSED -> APPROVED -> STAGED -> QUIESCING
         -> BACKING_UP -> MIGRATING -> ACTIVATING -> VERIFYING
         -> OBSERVING -> COMPLETED

评估不通过 -> REJECTED
停写前失败 -> FAILED_PRECHECK
停写后失败 -> ROLLING_BACK -> ROLLED_BACK
恢复钩子或旧版重启失败 -> NEEDS_INTERVENTION
人工取消 -> CANCELLED
```

每个状态都要记录开始/结束时间、操作人、审批号、节点、输入制品、检查结果、日志摘要和下一步。断电或更新器重启后，只能根据持久化检查点续跑或回滚，不能从头盲目重放。

### 6.6 客户配置与自定义迁移

升级必须区分四类数据：

1. 产品代码和内置默认值：随版本只读交付。
2. 客户配置和 Secret 引用：保存在 `shared/`，永不被新默认文件直接覆盖。
3. 客户内容：自定义巡检模板、规则、阈值、报告样式和插件独立版本化。
4. 运行数据：元数据库、对象存储、队列检查点和审计记录按对应恢复策略处理。

配置升级使用“旧版默认值 + 客户当前值 + 新版默认值”三方合并。无冲突项自动迁移，已删除、改名或类型变化的字段生成冲突报告，由实施人员确认后才进入维护窗口。Secret 只校验引用是否可解析，不写入评估报告。

## 7. 完整包升级

首次安装或无法生成匹配补丁时使用签名完整包：

```bash
./TraceMindRelease install \
  --install-root /opt/tracemind \
  --package ./release-1.1.0 \
  --trusted-public-key /opt/tracemind/trusted-keys/release-2026.pub \
  --require-signature \
  --quiesce-command '/opt/tracemind/bin/stop-tracemind' \
  --activation-command '/opt/tracemind/bin/start-tracemind' \
  --health-url http://127.0.0.1:8800/health/ready
```

同版本同 `release_id` 重复执行返回 `unchanged`。默认禁止降级，避免误把旧安装包覆盖到新 Schema 上。

## 8. 数据库迁移兼容策略

生产平台数据库为外置 MySQL/PostgreSQL。不论平台采用原生包还是容器部署，数据库升级都由同一 Migration Runner 执行，不把迁移隐藏在应用启动过程中。

每个增量迁移单元至少包含：

```text
migrations/<revision>/
├── migration.json   # from/to、数据库引擎、checksum、是否可逆、超时和备份约束
├── up.sql           # 幂等或可检查已完成的向前迁移
├── down.sql         # 仅对经证明可逆的变更提供
└── verify.sql       # Schema、数据与业务前置验证
```

发布时先生成带脚本大小和 SHA-256 的迁移清单：

```bash
./TraceMindRelease migration-manifest \
  --bundle ./migrations/001-to-002 \
  --migration-id 001-to-002 \
  --from-revision 001 \
  --to-revision 002 \
  --engine mysql \
  --engine postgresql \
  --reversible

./TraceMindRelease migration-plan \
  --root ./migrations \
  --from-revision 001 \
  --to-revision 002 \
  --engine postgresql
```

现场升级不再要求人工拼接 `pg_dump`、`mysqldump`、`psql` 或 `mysql` 命令。设置外置数据库连接后，更新器会根据驱动自动备份、验证、迁移和恢复；密码只通过客户端环境变量传递，不进入命令、活动指针或发布历史：

```bash
export PLATFORM_DATABASE_URL='postgresql+psycopg://<DB_USER>:<DB_PASSWORD>@db.internal/platform'

./TraceMindRelease install \
  --install-root /opt/tracemind \
  --package ./release-1.1.0 \
  --database-url-env PLATFORM_DATABASE_URL \
  --quiesce-command '/opt/tracemind/bin/stop-tracemind' \
  --activation-command '/opt/tracemind/bin/start-tracemind' \
  --health-url http://127.0.0.1:8800/health/ready
```

执行前的固定顺序是：获取升级租约、停止新写入和高风险任务、等待在途事务结束、自动备份外置数据库、验证备份可读和完整、记录 migration journal，然后才执行 `up.sql`。只创建了备份文件但没有通过独立验证，不允许进入数据库迁移。

一个需要删除旧结构的变更至少跨两个版本完成：

1. Expand 版本先增加新表、新列或新索引，旧程序仍可运行。
2. Migrate 阶段分批回填数据，记录游标并支持幂等重跑。
3. 新程序在一个发布周期内同时兼容新旧字段。
4. 验证全部节点和任务都已升级后，Contract 版本再删除旧结构。

迁移脚本必须：

- 使用唯一 revision，成功状态可重复检查。
- 每一步幂等，失败后可从检查点恢复。
- 大表 DDL 有锁等待上限、容量估算和执行窗口。
- 不把应用启动时自动建表当作正式迁移机制。
- 在补丁说明中标注是否可以仅回滚程序，还是必须同时恢复数据库。

回滚时优先依据 `migration.json` 判定可逆性。可逆变更在验证过的 `down.sql` 成功后回切旧程序；数据回填、非可逆 DDL 或 `down.sql` 验证失败时，使用升级前备份恢复数据库，再激活旧版本。Contract 类删除操作延迟到回滚窗口结束后的后续版本，从架构上保留回退条件。

## 9. 自动回滚与人工回滚

下列任一条件触发自动回滚：

- 补丁物化或二次校验失败
- 一致性备份失败
- 数据库迁移失败
- 新服务激活失败
- readiness 在规定次数内未通过

自动回滚顺序为恢复旧活动指针、对完整执行成功且声明可逆的迁移运行 `down.sql`、恢复配置、重新激活旧版本并记录 `rolled_back`。迁移不可逆、向下迁移失败或迁移未完整执行时改为恢复已验证的数据库备份。失败目标版本保留在不可变目录中，便于离线排查。

人工回滚：

```bash
./TraceMindRelease rollback \
  --install-root /opt/tracemind \
  --trusted-public-key /opt/tracemind/trusted-keys/release-2026.pub \
  --require-signature \
  --quiesce-command '/opt/tracemind/bin/stop-tracemind' \
  --activation-command '/opt/tracemind/bin/start-tracemind' \
  --health-url http://127.0.0.1:8800/health/ready
```

需要恢复旧 Schema 时，额外传入历史记录中的 `backup_id` 和恢复命令。执行前必须确认该备份与目标程序版本匹配。数据库恢复属于维护窗口内的高风险操作，必须保留当前数据库的新备份。

### 9.1 回滚判定和更新器自救

| 失败时点 | 默认处置 | 是否需要恢复数据库 |
| --- | --- | --- |
| 切换前，验签/物化/预检失败 | 删除临时目录，保持当前版本 | 否 |
| 停写或备份失败 | 重新激活旧版本，不执行迁移 | 否 |
| 可逆 Expand 迁移后 readiness 失败 | 回切程序，执行经验证的 down migration | 按迁移声明 |
| 数据回填后或非可逆迁移失败 | 恢复一致性备份和旧版本 | 是 |
| Contract 结构已删除 | 不允许自动程序回滚 | 需专项恢复方案 |
| 旧版本重启或恢复钩子失败 | 进入 `NEEDS_INTERVENTION`，运行 rescue 命令 | 依检查点判定 |

更新器至少保留两个自身版本和一个不依赖 Web/API 的 `rescue status|activate|restore|support-bundle` 入口。更新器自更新必须先启动新副本完成自检，再原子替换维护入口；失败时保留旧更新器可执行。

## 10. 多节点与 Agent 升级

控制面多节点按以下顺序滚动：

1. 暂停调度领导节点的新任务领取，等待在途高风险任务结束或进入安全检查点。
2. 先升级非领导 API/Worker 节点，逐节点通过 readiness 后再继续。
3. 转移调度领导权，升级原领导节点。
4. 最后执行 Contract 类 Schema 清理。

Agent 协议遵循 N/N-1 兼容：

- 控制面先支持新旧两代 Agent 协议，再灰度升级 Agent。
- Agent 按站点、环境或资产组分批，每批失败率超过阈值立即停止。
- 内核 eBPF 能力必须先执行目标内核预检，加载失败时退回稳定采集器，不能拖垮 Agent 主进程。
- Agent 补丁同样需要签名、精确基线、健康回报和回滚版本缓存。

### 10.1 多节点协调门禁

- 升级协调器获取带租约的全局升级锁，数据库迁移和领导权转移必须使用 fence token。
- 混合版本窗口内，新旧 API、Worker、Schema 和队列消息必须在声明的 N/N-1 范围内兼容。
- 每次只排空一个控制面节点，可用容量、任务租约和最小剩余节点数不达标时禁止继续。
- 排队中的高风险任务暂停领取；在途任务必须结束或进入可恢复检查点，不能由新旧 Worker 重复执行。
- Agent 批次设置最大并发、失败率、掉线率、观察时间和自动停止阈值；批次越线后不得自动进入下一批。

## 11. 灰度发布和维护窗口

建议固定四个发布环：

| 发布环 | 范围 | 观察时间 | 放行条件 |
| --- | --- | --- | --- |
| Lab | 内部测试环境 | 1 个完整测试周期 | 自动化、迁移、回滚和故障注入通过 |
| Pilot | 1 个非核心客户或测试站点 | 24 小时 | 无 P0/P1 回归，资源和错误率正常 |
| Canary | 客户现场少量节点 | 2 至 4 小时 | readiness、任务成功率和告警稳定 |
| General | 其余节点 | 按维护窗口 | 分批完成，保留停止和回退条件 |

每次升级前冻结新高风险任务，升级后验证登录、资产、巡检、监控、诊断、备份、恢复计划、调度和审计主链路。

## 12. 保留和审计

- 至少保留当前版本、上一版本和最近一次已验证完整包。
- 数据库备份按客户策略保留，补丁成功后不能立即删除回滚备份。
- `release-state.json` 不保存数据库 URL、密码、Token 或私钥。
- 外层压缩包 SHA-256、补丁 ID、Key ID、操作人、审批号、维护窗口和验收结果进入交付记录。
- 过期版本清理必须确认不再被活动指针、回滚链和备份依赖引用。

### 12.1 Release Center 需要持久化的对象

| 对象 | 核心字段 |
| --- | --- |
| SiteInventory | site/node/role、release/schema/protocol、OS/arch、config fingerprint、last_seen |
| Artifact | artifact_id/type/version/release_id、platform/arch、signature key、SBOM、compatibility contract |
| UpgradeAssessment | site + artifact、supported path、conflicts、required capacity/window/hooks、risk |
| UpgradeCampaign | rings/batches、approval、maintenance window、stop thresholds、owner |
| UpgradeRun | durable state、node checkpoints、backup_id、migration revision、health and rollback result |
| UpgradeAudit | actor、approval、before/after、command digest、evidence、support bundle |

控制面最终应提供“制品库、现场版本盘点、兼容评估、升级活动、节点进度、回滚、审计与验收报告”页面和 API。离线现场可以不联网，但不能没有本地发布记录和可导出的验收证据。

## 13. 验收门禁

1. 签名整包首次安装成功，公钥不匹配时拒绝。
2. 签名补丁可预检，预检不改变活动版本和发布历史。
3. 补丁基线不是当前精确 `release_id` 时拒绝。
4. 篡改补丁清单、目标清单、文件内容、权限或符号链接后拒绝。
5. 停写发生在备份和迁移之前。
6. 备份产物被篡改后，恢复钩子不会执行。
7. 迁移、激活和 readiness 分别失败时均恢复旧程序、配置和数据库。
8. 同一补丁重复执行不会重复迁移。
9. 未批准的降级被拒绝。
10. Windows、macOS 和 Linux 目标平台分别完成真实打包与现场演练。
11. 多节点滚动时始终至少有一个控制面节点可用，调度任务不重复执行。
12. 发布记录、备份清单、服务日志和报告中没有明文秘密。

## 14. 当前实现边界和开发顺序

截至 2026-08-26，仓库已实现单节点发布事务主链路：完整清单、Ed25519 签名与 Key ID、多公钥验证、精确基线增量补丁、临时物化、无副作用预检、不可变版本、共享配置、激活、readiness 和失败恢复；并新增统一 `DeploymentDriver`、原生驱动、Docker/Podman Compose 驱动、不可变 OCI digest 约束、离线镜像导入、上一 digest 回切、外置 MySQL/PostgreSQL 自动 backup/verify/restore adapter、版本化迁移图、迁移 journal、脚本篡改拒绝和可逆 down migration。现有发布与新增链路共 29 项定向测试通过。

当前实现证据：

- `backend/app/runtime/release_manager.py`：发布事务、Native/Container DeploymentDriver、活动部署身份与回切。
- `backend/app/runtime/database_migrations.py`：MySQL/PostgreSQL 保护适配器、迁移包、revision path 和 journal。
- `scripts/release_manager.py`：原生/容器清单、迁移清单/预检、自动数据库保护与升级入口。
- `backend/tests/test_release_manager.py`、`backend/tests/test_deployment_and_migrations.py`：29 项自动化回归。

下一阶段不应重写这套底座，而应按以下顺序补齐：

1. **P0 现场可用**：升级评估报告、兼容图、累计服务包、配置三方合并、状态检查点和 rescue 入口。
2. **P0 生产验收**：补齐 Windows Service/macOS launchd/Linux systemd 服务包装、Kubernetes 驱动，并使用真实 MySQL/PostgreSQL 完成大库恢复、锁等待、断电续跑和回退演练。
3. **P0 集群能力**：分布式升级锁、fence token、任务排空、逐节点滚动、领导权转移和自动停止。
4. **P1 车队运营**：Agent 批次编排、站点版本盘点、公钥轮换/吊销、SBOM 与供应链证明、远程观察和验收报告。
5. **生产 Gate**：Windows/macOS/Linux 原生制品、Docker/Podman 离线镜像、单节点/多节点/外置数据库/断网/断电/磁盘满/损坏补丁全部演练，并在 Lab、Pilot、Canary 逐环放行。
