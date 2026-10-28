# 设计审核与 S1 应用记录

日期：2026-10-26。当前基线 0.3 / 追踪格式 v3。S1 同步计划/合同/案例和检查器；业务实现、制品、目标库、安全演练、容量、预生产与现场全部待执行。

## 本轮实际落地

| 用户指出的问题 | S1 已写入的当前合同与责任 |
| --- | --- |
| T09 被多节点卡住 | 六硬依赖改为 T05/T05a/T07/T08/T13b/T06a-core，单机路径不等 T06b/DM/E07；T14b 最终汇总仍要求真实/多节点 |
| T06a 被 DM 绑住 | core/real 两任务，旧 EP-T06a 仅不可调度历史映射；M1-base/M1-DM 独立，T10 早期不等 DM，金仓首包不等 UI |
| C70 文案分格、机器仍一条 | 六 variant+cell/context 的实际叶模型与精确 selector，父不计通过，T14a 不替发布范围作汇总 |
| 迁移没有启动接线 | T05a migrator-contract，T08 明确依赖，T09 回执/结构 readiness；MySQL 部分 DDL reconcile，hot 不 down/自动 restore |
| 容量太晚 | T04/core/T08 三 v0 是 T09 前置；C67 实际 profile 是 C84 的案例级输入，无 T14b→T09 反向条件 |
| 生产信任根归属 | OPS 托管、独立 SEC 批准；C83 rehearsal 与真实 E07 分开，test 输出不等生产仪式，正式封装须同已测 payload/验签/smoke |
| 第三方无法重现构建 | C73 三 variant，self-built 与 opaque 穷尽互斥；第三方原始 hash/来源/SBOM/许可，整包完整性仍覆盖全部文件 |

0.3 包含 24 项任务、109 个工作包、84 个案例条目（含 5 个父分组）；实际 test_instances/output_instances 均为空。S1 检查器运行的合成样例独立存储，不能算业务案例通过。

## S1 检查范围

- v3 schema、支持 core/real ID、无活动旧 ID、任务/案例唯一主责及工作包/Markdown 一致。
- whole/output/conditional 依赖和 C67→C84 案例前置；部分输出消费不要求提供方整任务 accepted。
- 叶执行键、variant/cell/context/output 精确匹配、父不可执行、可执行叶 passed 需 executed=true、证据/验收角色与 SEC 分离。
- 合成正反例：DM 失败不影响 native.mysql；DM/金仓/多节点/E07 缺失不阻断 core/单机；缺迁移/三 v0、摘要不符、部分 DDL 未核对、生产证据不足均不就绪。
- C84 为第五个父分组，逐六引擎真实 pair/C67/多节点验叶实例；派生 test/prod 输出复核完整前置，不能以单条自报 passed 收据或仅 MySQL 作为全矩阵。
- C73 的自研/opaque/incident 结构字段与拒绝语义明确，文件分类穷尽互斥且全部文件仍验整包完整性。
- --self-test 同时执行完整文件校验与合成例；完整校验失败不能被自测通过掩盖。

[当前 S1 证据目录](../../../reports/engine-extension-s1-20261026/) 保留 staging 检查、主目录检查、独立复核和应用 journal。实际完成以主目录重跑结果为准；旧投影报告只作历史输入。

## 历史与快照

[0.2 逐文件摘要及快照位置](../../../reports/engine-extension-s1-20261026/snapshot-manifest.json) 保存应用前 12 份原件及上级索引。原 0.2 审核处理全文见 [snapshot-v0.2/review-resolution.md](../../../reports/engine-extension-s1-20261026/snapshot-v0.2/review-resolution.md)。其中“当时复核通过”已被本轮新增依赖/案例粒度问题补充，不作为当前 S1 或功能验收依据。

[0.3 增量变更依据](change-plan-0.3.md) 的 S1 已应用；S2/S3/S4 仍 planned。没有连接数据库/SSH、重启服务、生成包或执行迁移/密钥操作。
