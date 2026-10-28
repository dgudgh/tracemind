# 给 dsh 的开发任务：S2-2 / T03 测试信任域与 payload 分类

状态：**待 dsh 实现**。本任务单只分配开发与验收要求，不表示相关代码已经编写。工作目录：`/Users/zhaolongjie/codex/mesdbtools`。

## 直接执行指令

请按本单实现 S2-2，分 A→B→C→D 四个小批次交付可审查差异和测试证据。先读项目规则、0.3 合同与现有代码，保留工作区已有修改。只完成本单范围，遇到真实外部条件缺失则记录，不用虚构结果，也不要在每个常规实现步骤结束后再请求继续确认。

本轮目标：**引擎包测试/生产信任域有明确隔离；payload 分类能核验实际文件字节；C73 三 variant 有可运行合同与正反夹具；E07 保持 waiting_external。**

不在本单范围：完整 T03、完整 `.tmengine` 压缩包安装器、Supervisor/RPC、包激活/卸载/回退、热更新、真实数据库驱动、目标库连接、生产根生成/读取/交接、TUF 全生命周期或真实泄露演练。不得将这轮完成写成 `test-trust-ready` 整体签发或 `production-release-ready` 通过。

## 0. 已有依据与保护范围

实现前读取：

- `docs/blueprint/engine-extension-packs-2026-10-26/contracts.md`：§8 包/分类，§9 信任，C73/实例合同。
- 同目录 `traceability.json`、`test-case-ownership.json`、`readiness-fixtures.json`：C73 现有 variant、required fields 与错误语义。
- `reports/engine-extension-s2-20261026/cp04-migrator-contract.md`：S2-1 交付边界。
- `backend/app/runtime/release_manager.py` 与 `backend/tests/test_release_manager.py`：已有 `cryptography` / Ed25519 依赖与测试惯例。

当前事实：平台发布模块已有签名实现，但属于平台 release/patch 域，且相关签名函数是私有接口。引擎包不能默认继承平台 `trusted_public_keys`、不能直接调用 `_sign_document` / `_verify_document_signature` 来冒充引擎信任隔离。使用现有 `cryptography` 的公开接口，不另造签名算法。

**保护范围：**不修改 S2-1 的 `app.migrations.contract`、`TRACEMIND_SCHEMA_ROLE=verify`、`verify_schema_ready()` 和原启动行为；不修改前端、实例/能力路由、现有平台发布包格式或生产配置。不提交、推送、部署或重启服务。

## 1. 文件落点

新增的生产模块只包含纯合同、文件校验与验签逻辑，不在 import 时执行操作：

| 文件 | 内容 |
| --- | --- |
| `backend/app/runtime/engine_packs/__init__.py` | 明确导出的公开接口 |
| `backend/app/runtime/engine_packs/trust.py` | 信任域/Key ID/用途/渠道/发布者/引擎范围与 Ed25519 验签 |
| `backend/app/runtime/engine_packs/contract.py` | 清单 Schema、文件 inventory、分类、摘要和 C73 receipt 校验 |
| `backend/tests/test_engine_pack_trust.py` | 信任域及真实签名正反例 |
| `backend/tests/test_engine_pack_contract.py` | 实际临时文件、分类、完整性和 C73 实例正反例 |
| `backend/tests/fixtures/engine_packs/` | 公开合成清单/receipt；不保存私钥或真实驱动 |
| `docs/operations/engine-pack-contract.md` | 本轮接口、清单/摘要规则、错误码和边界 |
| `reports/engine-extension-s2-20261026/s2-2-test-trust-payload.md` | 最终实现与验证报告 |

若实际职责需要多拆一个纯工具模块，可以在 `engine_packs/` 内拆分并解释；不得借此扩到安装/执行链。新模块不能依赖 `docs/`、`reports/` 下的规划检查器作为运行时实现。

## A. 信任域与签名合同

### A1. 数据约束

- namespace 固定为 `tmengine.test` / `tmengine.production`；channel 固定为 `test` / `production`，对应关系严格校验，无自动回落。
- `package_kind=tmengine`、`purpose=tmengine.manifest-signing.v1` 为固定常量，必须同时出现在受信 key policy 和 signed manifest。连同 namespace、channel、publisher、engine_id 全部纳入签名并逐项校验；未知 purpose、平台 release/patch purpose 一律拒绝，不能照抄包内 purpose 作为 expected 值。
- Key ID 采用 `namespace + ':' + SHA256(raw_public_key)`，raw public key 指 Ed25519 的 32-byte raw encoding，使用完整小写十六进制摘要；从受信公钥重新计算，不信任包内自报 Key ID。
- 信任配置由调用方显式提供；包内附带的公钥不能自己成为信任来源。key 的 publisher/engine scope 必须显式限制，不能默认 `*`。
- test/prod 信任目录分开；同一公钥指纹跨域登记应拒绝，防止只改 namespace/Key ID 标签复用测试密钥。
- test verifier 拒绝 production 域，production verifier 必须拒绝 test 域；平台 release/patch 的签名不能作为引擎包授权。

### A2. 接口职责

提供可独立测试的信任配置校验与 `verify_engine_manifest_signature(...)` 一类公开接口。返回验签身份、域、用途、渠道及受保护文档摘要；**验签成功不产生 E07 或生产发布就绪状态。**

运行时模块只需要公钥/验证接口。本轮不增加生产私钥 loader、生产签名 CLI 或写入全局 truststore 的入口。测试中用内存临时生成的不同 Ed25519 keypair，production 名称下的样例也必须明确 `fixture_only`，不代表实际生产根。

必须区分并返回可断言的错误码：未知 key、Key ID 不符、域不符、用途/包类型不符、渠道不符、publisher/engine scope 不符、签名编码/长度错误、签名无效。不得只返回一个无法区分的 `False`，也不得在错误信息中输出私钥材料。

## B. Payload inventory、分类和字节完整性

### B1. 本轮处理对象

以**临时 payload 目录 + 清单**实现合同，沿用当前文档的包布局；不实现 archive 解压和安装。

本单冻结目录集合如下，直接按此实现，不再把关键集合留成待选择项：

1. control files 精确为 `{ENGINE-MANIFEST.json, ENGINE-MANIFEST.sig}`；二者不进入 inventory 或 `whole_package_signed_files`，避免 manifest/sig 自 hash。
2. `package_file_inventory` 枚举其余所有 regular payload/声明 metadata，**包括 SBOM.json**；目录不计。每项包含相对路径、size、SHA-256 和唯一分类。除 control files 和 inventory paths 外不得有其他 regular file。
3. `whole_package_signed_files` 保留现有 C73 的文件名列表形状，必须无重复且精确等于 inventory；签名同时覆盖 inventory 中的实际 size/hash，不能只签文件名。
4. detached signature 输入是 canonical manifest bytes：UTF-8、固定 JSON key 排序/分隔规则、禁止 NaN/Infinity。manifest 内不包含 signature value，包含完整 inventory、上述列表、namespace/channel/package_kind/purpose/publisher/engine_id/signing_key_id 等字段。
5. `.sig` 只装 algorithm、key_id、signature value；其 key_id 必须等于 signed manifest 的 signing_key_id，并按受信公钥重算。manifest 由 detached signature 保护，signature value 由 Ed25519 验证，不把签名值再放入自己的输入。

实现逐文件读取和哈希，不能仅比较 manifest 中两个自报字符串。拒绝未声明/缺失文件、重复路径、非法分类、绝对路径/`..`/Windows drive 或 UNC 越界、符号链接/硬链接、大小写或 Unicode 归一化碰撞。不得为校验而执行文件或联网获取依赖。

### B2. 分类规则

每个 payload 文件恰好属于一个类别：

- **self-built**：自研 Provider/Schema/脚本等，记录锁定输入、源码/工具链与构建来源。
- **third-party-opaque**：必须有真实上游来源依据，记录完整版本/ABI、锁定原始 hash、实际打入的 raw hash、SBOM 引用、许可证及再分发状态、上游签名状态。

分类必须穷尽且互斥。未知 blob、遗漏、重复分类、把不稳定自研输出标为 opaque 均拒绝；不能只按文件扩展名自动猜分类。第三方原始 hash 漂移或来源缺失必须阻断，不以“不可重现”为理由放行。

本轮不实现厂商签名 verifier，运行时上游签名记录状态只接受 `recorded-unsigned` / `unverified`。任何 `verified*` 声明（包括旧 planning fixture 的 `verified-upstream`）一律报 `third_party_signature_claim_unproven`，不得凭 evidence 字符串自行认证。旧 readiness fixture 继续仅供规划检查器，不作为运行时可接受证明；真实厂商验签器和其绑定 raw hash/signer/verifier-version 的可信 proof 留后续任务。许可字段校验不等于法律授权，未知再分发状态不得被报告为可正式发布。

### B3. 摘要规则

- `self_payload_digest` 只覆盖 self-built 的确定路径/size/实际文件 SHA-256；排序、UTF-8 JSON 规范化规则固定并测试。
- 不规范化文件内容本身；LF/CRLF 或字节改变应被观察到。
- `created_at` 等可变外层 metadata 若保留，不进 self digest，但仍在 canonical manifest 中被签；签名值自身不进签名输入。
- 整包签名输入覆盖**全部** payload/metadata 的路径、size、hash 和身份/域/用途，不能只签文件名集合或只签 self-built。
- 仅改 opaque 字节时 self digest 可以不变，但文件完整性/整包验签必须失败；补改其清单 hash 而不重签也必须失败。

## C. C73 三个 variant 的合同与夹具

保持现有 variant 名和字段映射。可以为本轮增加带版本的实际字节证据字段，但不得删除/放宽现有 required fields，不得改 S1 检查器来迁就缺失证据。

| variant | 本轮实现 | 必须拒绝的情况 |
| --- | --- | --- |
| `self-payload` | 两份独立临时构建目录、不同 builder identity、相同 locked inputs/toolchain/规则，计算真实 self digest 并比较 | 同 builder、缺锁定输入、源码/字节改变、两个自报 digest 相等但实际文件不同 |
| `third-party-integrity` | 合成 opaque bytes 的原始锁定 hash/实际 packaged hash、来源、ABI、SBOM、许可、签名状态及整包覆盖验证 | raw hash 漂移、来源/SBOM 缺失、假 verified、遗漏 opaque 签名覆盖、额外/被篡改 signed-file 摘要 |
| `incident-rebuild` | 纯合同评估：冻结/受污染 key 或 digest、干净 builder、新 key/重新背书的证据字段和拒绝规则 | 使用 compromised key、复活 revoked digest、只改版本号或重签就声称安全、缺 clean rebuild 依据 |

incident 仅评估合成状态/receipt，**不执行真实吊销、根更新、进程终止或生产重建**；C58/TUF/根门限仪式仍为后续任务。

每条通过叶必须带 `oracle_receipt` 并经过本轮实际合同校验；generic evidence + QA/REL 字段不能替代它。父 C73 保持不可执行；测试 fixture 不能写入 v3 的实际 `test_instances` / `output_instances`。本轮单元夹具通过不等于正式 C73 的真实独立构建机/制品验收已完成。

## D. 测试、证据与验收

### D1. 必跑正反例

| 组 | 必需覆盖 |
| --- | --- |
| Trust | 正确测试签名；test→production 和反向误用；伪造域/Key ID；同公钥跨域；错误 kind/purpose/channel/publisher/engine；未知 key；签名内容/编码/长度篡改；平台 key 不默认继承 |
| Inventory | 全文件唯一归类；遗漏/重复/未知类；额外/缺失文件；路径越界/链接/碰撞；实际 size/hash 不符 |
| Integrity | 只改 self bytes；只改 opaque bytes；只改清单摘要；遗漏或增加 signed-file；修改 namespace/channel 等签名字段 |
| C73 | 三 variant 各有正例；缺/错 oracle_receipt；错误 variant；same builder/缺 locked inputs；opaque provenance/hash/SBOM/签名状态负例；compromised key/revoked digest 不能复活 |
| Isolation | S2-1 verify 行为保持；旧平台 release/patch 验签/构建回归保持；E07 和所有真实 output instance 未被置通过 |

使用项目既有 Python 环境，在根目录执行（新文件名按本单）：

```bash
PYTHONPATH=backend python3 -m pytest -q backend/tests/test_engine_pack_trust.py backend/tests/test_engine_pack_contract.py backend/tests/test_release_manager.py backend/tests/test_release_config_merge.py backend/tests/test_migration_contract.py
python3 docs/blueprint/engine-extension-packs-2026-10-26/validate_plan.py --self-test --report reports/engine-extension-s2-20261026/s2-2-plan-validation.json
git diff --check
```

如 Python 环境不同，报告实际解释器和命令，不安装/升级全局依赖。测试只使用内存/临时目录/合成文件与临时测试密钥；不得读取真实平台连接配置来跑 live 测试。已有 `check_all.py --fast` 可在确认其默认步骤不访问真实库/服务后补跑，禁止附加 API/诊断 smoke。没有前端改动，不要求重跑整个前端套件来替代本轮安全用例。

### D2. 交付报告必须包含

1. 新增/修改文件和四批次实际完成项；关键接口/清单示例及稳定错误码。
2. 每组正反例实际结果、命令、退出码；失败/skip/未跑分别列，不能只报总数。
3. 测试全部为本机合同/字节/密码学验证，未生成可安装生产包、未认证真实驱动、未部署。
4. E07=`waiting_external`，真实生产根未操作，`production-trust-accepted` / `production-release-ready` 未签发；整个 T03 / `test-trust-ready` 尚未据此完成。
5. S2-1 verify、现有平台发布流程与前端未改；未连接 DB/SSH、未重启服务。
6. 剩余任务明确列出：完整包安装/运行时、离线根生命周期、真实构建机复现、生产组织交接等。

### D3. 本单退出条件

- 信任域/用途/渠道/Key ID 的双向隔离由实际验签正反例证明。
- 分类对真实临时文件字节穷尽互斥，opaque hash 漂移和覆盖遗漏确实拒绝。
- C73 三 variant 的合同/receipt 路径都可运行，实例不能绕过对应 oracle。
- 定向回归、S1 v3 检查器及差异检查通过；E07/实际实例状态保持未验收。
- 报告边界准确、没有越界实现或修改保护范围。

完成后只报告本单结果和剩余项，交由我复核；不要自动推进安装器、热更新、真实目标库或生产根交接。
