# Engine Pack Trust / Payload Contract（S2-2）

日期：2026-10-26。本模块负责纯合同、文件校验与验签；隔离Provider运行、调度和持久事务见[Engine Host运行说明](engine-host-runtime.md)。完整安装/解压、真实引擎及生产根交接仍是后续独立验收。

## 公开接口

```python
from app.runtime.engine_packs import (
    TrustedEngineKey, verify_engine_manifest_signature,
    scan_payload_directory, build_payload_inventory, validate_c73_receipt,
)
```

### 信任域

- namespace：`tmengine.test` / `tmengine.production`
- channel：`test` / `production`，严格一一对应，无自动回落
- package_kind：`tmengine`
- purpose：`tmengine.manifest-signing.v1`
- Key ID：`namespace + ":" + SHA256(32-byte raw Ed25519 public key)`

调用方必须显式传 `trusted_keys`；manifest 内公钥/Key ID 不能成为信任来源。同一公钥跨域登记由 `assert_no_cross_domain_key_reuse` 拒绝。

验签成功只返回身份和摘要，不签发 E07、`production-trust-accepted` 或任何发布就绪状态。

稳定错误码包括：`trust_domain_mismatch`、`trust_channel_mismatch`、`purpose_mismatch`、`package_kind_mismatch`、`publisher_scope_mismatch`、`engine_scope_mismatch`、`unknown_key`、`key_id_mismatch`、`signature_encoding_invalid`、`signature_length_invalid`、`signature_invalid`、`cross_domain_key_reuse`。

### Payload inventory

- control files 仅为根目录的 `ENGINE-MANIFEST.json`、`ENGINE-MANIFEST.sig`，不进入 inventory。
- `package_file_inventory` 必须精确覆盖其余全部 regular 文件，含 `SBOM.json`；每项记录 path/size/SHA-256。
- `whole_package_signed_files` 无重复且精确等于 inventory。
- 每个文件恰好属于 `self-built` 或 `third-party-opaque`。
- `self_payload_digest` 只覆盖 self-built 的路径/size/实际 SHA-256。
- 整包签名覆盖规范化 manifest 中的全部 inventory、身份、用途和 signed-file 列表。
- 拒绝路径穿越、绝对路径、Windows drive/UNC、symlink/hardlink、大小写/Unicode 归一化碰撞、额外/缺失文件和实际 hash 不符。

### 第三方 opaque

必须提供 upstream origin、完整版本/ABI、raw hash、packaged raw hash、`sbom_path`、SBOM 实际文件摘要、许可证、再分发状态和上游签名状态。opaque 文件实际 SHA-256 必须同时等于 raw/packaged 摘要；`sbom_path` 必须存在于 inventory 且摘要匹配。运行时只接受 `recorded-unsigned` / `unverified`；任何 `verified*` 声明返回 `third_party_signature_claim_unproven`。本轮不实现厂商验签器，也不支持重打包/成员映射。

### C73 receipts

三 variant：`self-payload`、`third-party-integrity`、`incident-rebuild`。每个通过的 receipt 必须有 `oracle_receipt`，且variant匹配；调用方必须传受控 `build_roots`，receipt自报列表不能代替实际文件。self核两个payload根、实际lock/toolchain/recipe及独立builder provenance；third按每个opaque实扫并验证独立 `tmengine.sbom/v1` 的组件字节/版本/许可；incident实扫rebuilt payload，核真实公钥指纹及独立新测试key背书。digest统一为 `sha256:`+64位小写十六进制，generic evidence/QA字符串不能替代。

## 边界

- 只读临时目录，不执行文件、不联网、不解压归档。
- 不修改 S2-1 migrator contract/verify 模式。
- 不生成生产包、不读取真实密钥/连接配置、不重启服务。
- E07 保持 `waiting_external`；生产根真正的交接、TUF 续链、真实 clean builder 和真实驱动验收仍待后续任务。

## 后续离线容器与文件安装

上述只读边界针对 `trust.py` / `contract.py`。后续独立模块 `engine_packs.archive` 与 `engine_packs.repository` 已提供真实 `.tmengine` 容器、安全解包和 owner-only 实验安装仓库，复用本合同的实际字节 oracle。CLI、崩溃恢复与生产边界见[离线安装说明](/Users/zhaolongjie/codex/mesdbtools/docs/operations/engine-pack-offline-installation.md)。这不表示生产信任根或完整 T13a 管理鉴权已经签发。
