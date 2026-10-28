# 交给 dsh 的简单工作：映射与交付文档

2026-10-26。用户已改为让Codex直接实现核心，dsh处理简单工作。共享目录为 `/Users/zhaolongjie/codex/mesdbtools`。

1. 只读原批次/续做单和 `docs/operations/engine-host-implementation-interfaces.md`，整理CB01-01–22到实际pytest node ID/runner叶/原始证据的映射。
2. 可写文件仅：`docs/operations/engine-host-runtime.md`、`reports/engine-extension-core-batch-01-20261026/codex-delivery/dsh-case-map-draft.json`、`dsh-delivery-draft.md`（后两项在同一codex-delivery目录）。
3. 未执行/证据缺失用pending/not_run，不能填passed、QA/OPS签字或正式EP案例状态；不要用旧49/111等总数当本轮证据。
4. 文档写清命令参数、单/双容量、八kill点、旧任务绑定、cleanup口径、当前OS与fixture边界。依据真实代码和证据，不猜已实现。
5. 禁止修改backend/scripts/现有测试、包/签名/运行时/持久化代码、原v3机器基线、配置/前端；不提交推送部署、不开DB/SSH/模型或现有服务，不运行会清理进程的脚本。
6. 核心仍在开发，可以先生成映射骨架与文档稿；等Codex冻结实际测试/报告后补准node ID和证据。完成后报所写文件和未完成引用，Codex审核整合。

当前DSH Desktop界面访问被工具权限拒绝；本文件已准备，**尚未实际发进DSH**。恢复界面授权后按上述文件范围派发，不能与核心写入冲突。
