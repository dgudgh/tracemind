# 离线 .tmengine 文件与实验室安装仓库

2026-10-26。本模块实现 T03 的安全容器和 T13a 的文件仓储基础，复用已有签名、inventory、SBOM 与来源合同。它不启动 Provider、不连接目标库、不修改平台 Schema。当前命令使用外部公开实验室信任策略与 owner-only 本机目录；生产信任根生命周期、受保护管理 IPC 和正式初激门禁仍独立交付。

## 文件格式与稳定接口

`.tmengine` v1 使用无压缩 ZIP_STORED，根目录恰好包含 `ENGINE-MANIFEST.json`、`ENGINE-MANIFEST.sig` 和签名 inventory 中的全部 regular 文件。路径与原合同一致，runtime、drivers、schemas、content、SBOM 等由已签清单声明；容器不增加 payload 前缀。输出固定排序、1980 时间与固定权限属性，相同已签输入得到相同 archive SHA-256。

三个摘要独立：package digest 是 canonical manifest SHA-256，payload digest 沿用 self-built 实际字节合同，archive SHA-256 是完整容器字节。安装请求绑定实际归档摘要与 package digest，不能混用。构建工具接收已签 manifest/signature，不生成签名 key。

- `engine_packs.archive.build_archive`：读取已签目录，核对实际字节，以独占目标名发布文件；不覆盖旧输出。
- `open_verified_archive`：固定 regular 文件描述符，流式复制到私有匿名 snapshot，验证后只从同一 snapshot 读取；外部重命名/置换导入路径不会改变已绑定字节。
- `VerifiedArchive.extract`：只向新建绝对私有目录写入受控成员。文件、目录均为本次新对象，不采用 ZIP 中的权限或链接属性；完成后重新运行实际字节/SBOM oracle。
- `LabRepository`：只在带 owner marker 的本机实验仓库中写 SQLite 安装 journal、package metadata 和 audit；文件安装成功始终 `activated=false`。

默认上限：archive 256 MiB，展开文件总量 512 MiB，单文件 64 MiB，单个 control 4 MiB，4096 个成员，central directory 8 MiB。EOCD 的文件数与中心目录预算在 stdlib 建立成员列表前检查。v1 拒绝压缩、ZIP64、加密、extra/comment、目录成员、非 regular、链接、特殊/提权权限、路径穿越、大小写/Unicode/父子路径冲突、额外/缺失文件、坏 CRC、局部头与中心目录不一致。

安装不执行 hook，不调用 shell、pip 或网络。当前只接受已登记固定入口 `python.sdk.fixture.v1`；新增真实运行 profile 必须先由 SDK/Host 实现并认证。`.env`、SSH 私钥常用文件名、PEM 私钥标记会拒绝，包含跨 chunk 标记；这类规则不替代发布者对任意二进制凭据内容的审核。

## CLI 与外部信任

解释器使用项目已有 `.venv-mac-primary/bin/python`。所有源/目标路径使用绝对路径，`/ABS/...` 是本次目录占位。公开策略由操作方在包外提供，不能从包内自报公钥建立信任；文件须属于当前 UID，且 group/world 不可写。

```json
{
  "schema": "tmengine.lab-trust/v1",
  "keys": [{"public_key_hex": "实际32字节Ed25519公钥的64位hex", "publisher": "fixture", "engine_scopes": ["test.fake"]}]
}
```

公钥身份仍由 trust 模块重算，域固定 `tmengine.test/test`，不回落到平台 release/patch key。当前实验 CLI 拒绝生产策略，不能用它签发 test-trust-ready、production-trust-accepted 或 production-release-ready。

```bash
./.venv-mac-primary/bin/python scripts/tmengine.py --trust-policy /ABS/lab-public-trust.json build --source /ABS/SIGNED/SOURCE --manifest /ABS/manifest.json --signature /ABS/manifest.sig --output /ABS/provider.tmengine
./.venv-mac-primary/bin/python scripts/tmengine.py --trust-policy /ABS/lab-public-trust.json verify --archive /ABS/provider.tmengine
./.venv-mac-primary/bin/python scripts/tmengine.py --trust-policy /ABS/lab-public-trust.json --repository /ABS/NEW/LAB-REPO list
```

list 首次创建专用新仓库。root 必须属于当前 UID、权限0700，已有非空未知目录、链接或 owner marker 不匹配都拒绝。将包作为 regular 文件放到该仓库的 `incoming/` 后，安装只接收叶文件名，禁止任意源路径：

```bash
./.venv-mac-primary/bin/python scripts/tmengine.py --trust-policy /ABS/lab-public-trust.json --repository /ABS/LAB-REPO install --import-name provider.tmengine --request-id install-001
./.venv-mac-primary/bin/python scripts/tmengine.py --trust-policy /ABS/lab-public-trust.json --repository /ABS/LAB-REPO status --request-id install-001
./.venv-mac-primary/bin/python scripts/tmengine.py --trust-policy /ABS/lab-public-trust.json --repository /ABS/LAB-REPO inspect --digest sha256:ACTUAL_MANIFEST_DIGEST
./.venv-mac-primary/bin/python scripts/tmengine.py --trust-policy /ABS/lab-public-trust.json --repository /ABS/LAB-REPO recover
./.venv-mac-primary/bin/python scripts/tmengine.py --trust-policy /ABS/lab-public-trust.json --repository /ABS/LAB-REPO audit
```

verify/install 返回完整 archive/package/payload 摘要。失败返回 JSON code 与非0退出（当前2）；recover 中任一 operation blocked 也非0，不把“命令运行过”算成功。

## 持久事务与恢复

仓库布局：incoming（受控导入）、.staging（operation 独占候选）、packages（按 manifest digest 的只读版本）、repository.sqlite、repository.lock、REPOSITORY.json。

OS flock 串行管理写入，SQLite FULL/WAL 保存 `prepared→staged→installed`。同 request_id 且同完整归档/package 身份返回原 receipt；异内容冲突。engine/publisher/version/namespace/OS/CPU/ABI 的同一身份不能被另一 manifest digest 覆盖。多版本文件可以共存，仓库不改 active generation。

候选完整验签和实际字节核对后才进入 staged。只读文件在私有父目录内完成原子 rename 和目录 fsync，只有目标封存为只读后才提交 installed 与 audit。macOS 的目录 rename 需要源根可写，因此 unpublished stage 根保留0700，发布后立刻封为0500；文件及子目录提前只读。这个过程由私有父目录和 OS lock 隔离，不宣称防恶意同 UID/管理员。

- after_prepare：新进程按持久 archive SHA 重新固定导入文件；被替换则 blocked，不换包续装。
- after_stage：复核已落盘候选后发布。
- after_rename：复核已存在的精确目标并完成 metadata/audit 提交，不重复解包或重写别的版本。
- after_commit：同请求回放原 operation，只有一条 installed audit。

audit 包含操作 UID、operation、阶段、错误码与前项摘要，和状态写入同一事务；`audit` 检查 hash chain。它可发现局部记录损坏，不是对本机管理员修改整库的外部不可抵赖签名日志。

## 可重复验收

```bash
./.venv-mac-primary/bin/python scripts/check_engine_pack_installation.py --workdir /ABS/NEW/PRIVATE/PROBE --report /ABS/REPORT/installation.json
PYTHONPATH=backend ./.venv-mac-primary/bin/python -m pytest -q backend/tests/test_engine_pack_archive.py backend/tests/test_engine_pack_repository.py
```

probe 生成临时测试 key（只保存公开材料），实际通过 CLI build/verify/install/readback，安装 V1/V2 后复用原 Supervisor/UDS 调用，再对4个安装断点实际 SIGKILL 并启动新 CLI recover。报告保留命令、PID/退出码、摘要、幂等回执、audit 和真实清理证据。

probe 还把实际安装目录交给原 Orchestrator，执行一轮 V1→V2→V1 的任务、CAS、排空和回退（generation 1→2→3）。这里明确使用 `explicit_fixture_harness_budget`，仅证明安装产物与更新执行链接通；它不复用旧 source layout 的容量证据，也不签发新安装布局的正式容量候选。仓库本身仍未自动激活。

当前实际运行格为 Darwin/arm64。Linux flock/文件语义需独立运行认证，Windows 的管理 IPC/owner 适配当前明确 unsupported。生产管理员鉴权、信任根轮换/吊销/可信时间、initial-activate、平台任务绑定与真实厂商驱动都不能由这份实验仓库结果代签。
