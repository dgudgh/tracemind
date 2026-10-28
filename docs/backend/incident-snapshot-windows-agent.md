# TraceMind Incident Snapshot Windows Agent 设计

版本：v1.1
日期：2026-08-13
目标运行环境：Windows Server 2016/2019/2022/2025、Windows 10/11 x64

## 0. 当前实现与后续边界

当前 `agent/` 已形成可运行 MVP：Windows Service/CLI、身份、本地分段缓存、pin/unpin、基础主机/进程/服务/事件采集、本地触发、Snapshot 打包、分片上传、重试、签名校验和脱敏均有实现与 Go 测试。

本文同时保留生产目标，因此部分目录和接口比当前代码更细。以下能力仍属于后续开发，不能按已上线理解：

- CA/mTLS 与非对称命令签名。
- Windows Event Log 原生订阅与 bookmark 完整恢复。
- 连续线程栈、WCT 等待链和 ETW 深度关联。
- M0-M4 内存快照、Process/Heap/Kernel Dump 编排。
- 受控动态打印/探针模板和租约。
- Linux Agent、eBPF、离线符号服务和数据库专用 Snapshot 插件矩阵。

扩展设计见 [State Intelligence 与现场重建](../architecture/state-intelligence-and-scene-reconstruction.md)、[eBPF 内核动态观测](../architecture/ebpf-kernel-observability.md) 和 [历史开发计划](../archive/plans/2026-08-13-tracemind-development-plan.md)（已归档）。

## 1. 技术决策

- 语言：Go 1.24+，单一 x64 exe，后续增加 arm64 构建。
- 运行方式：Windows Service；同一二进制提供安装、诊断和离线检查 CLI。
- 本地缓存：自研分段 WAL，不引入需要 CGO 的嵌入式数据库。
- 采集：`gopsutil/v4` 提供跨平台基础数据，关键 Windows 指标通过 PDH、IP Helper、SCM 和 Windows Event Log API 补齐。
- 当前传输：HTTPS；一次性注册后使用 Agent token，Windows 端通过 DPAPI 本机保护；平台命令使用 HMAC-SHA256 签名；manifest 使用 JSON；数据流使用 UTF-8 JSON + gzip。
- 可靠性：at-least-once 上传，平台按幂等键和分片序号去重。
- 时间：协议统一 UTC RFC3339Nano，同时保留 Windows 单调时钟偏移信息。

不采用“所有样本放内存”的环形队列。60 分钟进程、事件和磁盘维度数据可能达到数百 MB，必须使用磁盘分段缓存，内存只保留当前 segment 的有限缓冲。

## 2. 工程结构

```text
agent/
├── cmd/
│   └── tracemind-agent/
│       └── main.go
├── internal/
│   ├── buildinfo/          # version/commit/build time/protocol
│   ├── config/             # file + remote config, validation, hot reload
│   ├── identity/           # agent ID, enrollment, Windows cert store
│   ├── service/            # Windows Service lifecycle
│   ├── supervisor/         # collectors, trigger, exporter orchestration
│   ├── collector/
│   │   ├── contract.go
│   │   ├── cpu_windows.go
│   │   ├── memory_windows.go
│   │   ├── disk_windows.go
│   │   ├── network_windows.go
│   │   ├── process_windows.go
│   │   ├── services_windows.go
│   │   ├── eventlog_windows.go
│   │   └── dbplugin/
│   ├── sample/             # canonical metric/event/state models
│   ├── buffer/             # segment WAL, index, pin, recovery, compaction
│   ├── trigger/            # rule AST, window evaluation, cooldown, dedup
│   ├── snapshot/           # freezer, manifest, quality, packager
│   ├── redact/             # secrets and PII redaction
│   ├── exporter/           # upload session, chunks, resume, backoff
│   ├── gateway/            # heartbeat/config/command HTTP client
│   ├── health/             # self metrics and adaptive degradation
│   └── logging/            # rotating local logs, no secret fields
├── api/
│   └── v1/                 # versioned Go protocol structs
├── testdata/
│   ├── golden/             # shared contract payloads
│   └── eventlog/
├── packaging/
│   ├── windows/
│   │   ├── install.ps1
│   │   ├── uninstall.ps1
│   │   └── tracemind-agent.man
│   └── config.example.yaml
├── go.mod
├── go.sum
└── Makefile
```

平台代码落在：

```text
backend/app/incident_snapshot/
├── api_agent.py
├── api_console.py
├── constants.py
├── models.py
├── schemas.py
├── enrollment_service.py
├── command_service.py
├── upload_service.py
├── ingest_service.py
├── normalizer.py
├── trigger_service.py
├── evidence_service.py
├── rca_service.py
├── retention_service.py
├── storage/
└── tests/
```

## 3. 进程与服务模型

```text
Windows SCM
  -> Service.Run
      -> Identity Bootstrap
      -> Buffer Recovery
      -> Config Manager
      -> Collector Supervisor
      -> Trigger Engine
      -> Snapshot Manager
      -> Heartbeat Loop
      -> Exporter Loop
```

- 所有 goroutine 从根 `context.Context` 派生。
- 服务停止先停止新采集，再 flush 当前 segment，最后保存 Event Log bookmark 和上传游标。
- 单个采集器 panic 由 supervisor 捕获并重启，不允许拖垮 Agent。
- 连续失败触发熔断，心跳上报 degraded；核心采集器 CPU/内存/磁盘不能静默失效。

## 4. 核心接口

```go
type Collector interface {
    Name() string
    Priority() Priority
    Interval() time.Duration
    Start(ctx context.Context, out chan<- Record) error
    Health() CollectorHealth
}

type SegmentStore interface {
    Append(ctx context.Context, record Record) error
    Query(ctx context.Context, from, to time.Time, streams []StreamType) ([]SegmentRef, error)
    Pin(ctx context.Context, refs []SegmentRef, owner string) error
    Unpin(ctx context.Context, owner string) error
    Recover(ctx context.Context) error
    EnforceCapacity(ctx context.Context) error
}

type TriggerEvaluator interface {
    Evaluate(ctx context.Context, record Record) ([]TriggerMatch, error)
    LoadRules(ctx context.Context, rules []Rule) error
}

type SnapshotManager interface {
    Trigger(ctx context.Context, request TriggerRequest) (SnapshotRef, error)
    Advance(ctx context.Context, now time.Time) error
    Package(ctx context.Context, snapshotID string) (Manifest, []ChunkRef, error)
}

type Exporter interface {
    Enqueue(ctx context.Context, manifest Manifest, chunks []ChunkRef) error
    Resume(ctx context.Context) error
}
```

`Record` 是封闭联合类型：`MetricPoint`、`EventRecord`、`ProcessState`、`ServiceState`、`DBState`。未知类型必须拒绝，不允许把任意 JSON 直接写入 WAL。

## 5. 本地目录

默认安装目录：

```text
C:\Program Files\TraceMind\IncidentAgent\tracemind-agent.exe
C:\ProgramData\TraceMind\IncidentAgent\
├── config\agent.yaml
├── identity\agent-id
├── buffer\segments\
├── snapshots\pending\
├── snapshots\uploaded\
├── state\event-bookmarks\
├── state\upload-cursors\
└── logs\agent.log
```

- `Program Files` 只放程序，不在升级时覆盖数据。
- `ProgramData` ACL 仅允许 `SYSTEM`、Administrators 和专用服务账号。
- 私钥只放 Windows Certificate Store；`identity` 目录不保存私钥。
- 上传成功后的包按 `uploaded_grace_period` 保留，默认 24 小时，再由清理器删除。

## 6. 配置模型

```yaml
schema_version: "1.0"
agent:
  gateway_url: "https://tracemind.example.com"
  heartbeat_interval: 30s
  config_poll_interval: 60s
  labels:
    environment: production
    site: shanghai-idc

buffer:
  directory: 'C:\ProgramData\TraceMind\IncidentAgent\buffer'
  capacity_mb: 512
  history_minutes: 60
  segment_seconds: 60
  min_free_disk_pct: 5

collectors:
  cpu: { enabled: true, interval: 5s }
  memory: { enabled: true, interval: 5s }
  disk: { enabled: true, interval: 5s }
  network: { enabled: true, interval: 5s }
  process: { enabled: true, interval: 15s, top_n: 10 }
  service: { enabled: true, interval: 15s }
  eventlog:
    enabled: true
    channels: [System, Application]
    providers:
      - Microsoft-Windows-StorPort
      - disk
      - Ntfs
      - Microsoft-Windows-FailoverClustering

snapshot:
  default_pre_window: 30m
  default_post_window: 30m
  max_package_size_mb: 4096

exporter:
  chunk_size_mb: 8
  max_bandwidth_mbps: 20
  max_parallel_chunks: 2
  retry_min: 5s
  retry_max: 10m

privacy:
  collect_full_command_line: false
  collect_security_log_body: false
  redaction_profile: default
```

远程配置包含 `config_version` 和 SHA-256。Agent 必须先完整校验，再原子替换；失败时继续使用上一版并在心跳中返回错误。平台不能远程配置任意命令或任意文件路径。

## 7. 采集器设计

### 7.1 CPU

标准指标：

```text
cpu.total_pct
cpu.user_pct
cpu.privileged_pct
cpu.interrupt_pct
cpu.dpc_pct
cpu.queue_length
system.context_switches_per_sec
system.thread_count
system.process_count
```

PDH 第一次读取通常没有有效差值，第一批样本标记 `warmup`，不得参与触发。

### 7.2 Memory

```text
memory.available_bytes
memory.committed_bytes
memory.commit_limit_bytes
memory.page_faults_per_sec
memory.pages_per_sec
memory.pagefile_used_pct
memory.pool_paged_bytes
memory.pool_nonpaged_bytes
```

内存泄漏规则使用趋势和进程关联，不能仅凭 available memory 低直接判定泄漏。

### 7.3 Disk

按 Windows volume、physical disk 和路径保存标签，不以易变化的序号作为唯一键：

```text
disk.read_iops
disk.write_iops
disk.read_latency_ms
disk.write_latency_ms
disk.queue_length
disk.busy_pct
disk.free_bytes
disk.free_pct
```

采集器保存 `device_id`、volume GUID、drive letter、mount path 和可用的存储总线信息，便于 StorPort/MPIO/iSCSI 事件关联。

### 7.4 Network

```text
network.rx_bytes_per_sec
network.tx_bytes_per_sec
network.rx_errors_per_sec
network.tx_errors_per_sec
network.rx_drops_per_sec
network.tx_drops_per_sec
tcp.retransmits_per_sec
tcp.connection_resets_per_sec
tcp.time_wait
tcp.close_wait
```

接口稳定键使用 LUID/GUID，不使用显示名称；名称变化只更新标签。

### 7.5 Process 和 Service

- Top-N 分别按 CPU、工作集、私有内存、读 IO、写 IO 计算，再去重合并。
- PID 必须与 start time 组合，避免 PID 复用把两个进程误认为同一对象。
- 命令行默认只保存 executable 和参数键，参数值按规则脱敏。
- Service 保存状态变化而非每次重复全量；快照打包时补一份窗口末端全量状态。

### 7.6 Event Log

- 使用 `EvtSubscribe` 推送订阅和 bookmark，不使用全量轮询。
- 事件唯一键：channel + record_id + provider + computer。
- bookmark 在成功写 WAL 后再推进，保证 at-least-once；恢复时由唯一键去重。
- 消息格式化失败仍保存 provider、event ID、level、time 和原始 event data。

## 8. WAL 与环形缓存

### 8.1 Segment 格式

```text
segment header
  magic: TMIS
  format_version
  segment_id
  start_time/end_time
  record_count
  payload_crc32
records (length-prefixed canonical JSON)
segment footer
  index_offset
  sha256
```

- 当前 segment 写临时文件，完成后 `fsync + atomic rename`。
- 索引按 stream type 和最小/最大时间保存，不扫描全部文件即可选中窗口。
- 恢复时截断最后一个不完整 record，已完成 segment 必须通过 hash。
- `pin` 元数据单独原子持久化；容量清理只删除未 pin 的最旧 segment。

### 8.2 并发约束

- 单 writer goroutine 负责 WAL 顺序写，采集器通过有界 channel 提交。
- channel 满时：低优先级 process 明细先丢弃并记录 drop counter，核心 metric 不静默丢弃。
- Freeze 先获取 segment 引用和 pin，再创建 snapshot 状态，避免触发与覆盖竞争。
- 同一规则冷却窗口内的二次触发追加 trigger evidence，不重复 pin 相同 segment。

## 9. Snapshot Manifest

```json
{
  "schema_version": "1.0",
  "snapshot_id": "WIN-SNAPSHOT-20260803-00001",
  "agent_id": "agt_01J...",
  "host": {
    "asset_host_id": 12,
    "hostname": "db-win-01",
    "os_version": "Windows Server 2022 20348"
  },
  "trigger": {
    "type": "rule",
    "rule_key": "windows.storage_path_failure",
    "rule_version": 1,
    "time": "2026-08-03T14:10:10.123456Z",
    "condition_results": []
  },
  "window": {
    "start": "2026-08-03T13:40:10.123456Z",
    "end": "2026-08-03T14:40:10.123456Z"
  },
  "clock": {"offset_ms": 37, "quality": "good"},
  "streams": [
    {
      "type": "metric",
      "sequence": 0,
      "path": "chunks/metric-00000.ndjson.zst",
      "sha256": "...",
      "records": 7200,
      "compressed_bytes": 8123456
    }
  ],
  "missing_streams": [],
  "redaction_profile": "default-v1",
  "manifest_sha256": "..."
}
```

`manifest_sha256` 对移除该字段后的 canonical JSON 计算。平台验签、逐 chunk 校验和总 manifest 校验全部通过后才进入 `ingesting`。

## 10. Agent API 协议

所有请求头：

```text
X-TraceMind-Agent-ID
X-TraceMind-Protocol-Version
X-TraceMind-Request-ID
X-TraceMind-Config-Version
Idempotency-Key (写请求)
```

### 10.1 Enrollment

`POST /api/v1/incident-agents/enroll`

```json
{
  "enrollment_token": "one-time-secret",
  "agent_id": "agt_01J...",
  "hostname": "db-win-01",
  "agent_version": "1.0.0",
  "public_key_csr": "-----BEGIN CERTIFICATE REQUEST-----..."
}
```

响应只返回证书链、平台签名公钥、初始配置和服务器 UTC，不返回可复用 enrollment token。

### 10.2 Heartbeat

`POST /api/v1/incident-agents/{agent_id}/heartbeat`

```json
{
  "sent_at": "2026-08-03T14:00:00.000000Z",
  "monotonic_uptime_ms": 12345678,
  "status": "active",
  "cache": {"capacity_bytes": 536870912, "used_bytes": 201326592},
  "upload_backlog_count": 1,
  "collector_health": {},
  "last_error": null
}
```

响应带服务器接收/发送时间，Agent 用 RTT 中点估算时钟偏移。

### 10.3 Upload

1. `POST /incident-snapshots/uploads` 初始化并返回平台已有的 chunk 序号。
2. `PUT /uploads/{upload_id}/chunks/{sequence_no}` 上传压缩分片；平台在读取请求体前校验 `Content-Length`，并限制压缩后 32 MB、解压后 128 MB。
3. `POST /uploads/{upload_id}/complete` 校验 manifest 和缺片。

每个 chunk 请求携带：`X-Stream-Type`、`X-Chunk-SHA256`、`X-Chunk-Codec: gzip`、`X-Uncompressed-Bytes`、`X-Record-Count` 和 `Content-Length`。同序号、同 hash 且元数据一致时返回幂等成功；内容或元数据不一致返回 409。

发布加固路线保留 mTLS、Ed25519 和 zstd；在这些能力正式接入并通过 Windows 实机回归前，不把它们描述为当前运行协议。

## 11. 本地触发和平台命令

平台 capture 命令：

```json
{
  "command_id": "cmd_01J...",
  "type": "capture_snapshot",
  "nonce": "...",
  "issued_at": "2026-08-03T14:00:00Z",
  "expires_at": "2026-08-03T14:02:00Z",
  "payload": {
    "title": "人工现场采集",
    "severity": "warning",
    "pre_window_seconds": 1800,
    "post_window_seconds": 600
  },
  "signature": "base64-ed25519-signature"
}
```

Agent 验证协议版本、agent ID、nonce、TTL 和签名。处理过的 nonce 至少保存 24 小时；过期或重放命令只回 ack，不执行。

## 12. 安全与脱敏

- 预置敏感键：password、passwd、pwd、token、secret、authorization、connectionString、accessKey。
- 预置模式：URI 用户密码、Bearer/Basic、JDBC/ODBC、私钥、邮箱、手机号、身份证。
- 脱敏发生在 WAL 写入前；高风险字段不能寄希望于平台二次清洗。
- Event Security channel 默认关闭；开启需独立策略和审计。
- 原始文件采集不在 v1 支持范围，防止任意文件外传。
- Agent 日志不得输出请求 body、证书、Token、完整命令行或事件正文。

## 13. 可观测性

Agent 自指标：

```text
agent.collector.duration_ms{collector}
agent.collector.errors_total{collector}
agent.records.written_total{stream}
agent.records.dropped_total{stream,reason}
agent.buffer.used_bytes
agent.buffer.pinned_bytes
agent.snapshot.active_count
agent.upload.backlog_count
agent.upload.bytes_per_sec
agent.clock.offset_ms
```

本地 `doctor` 输出人类可读摘要和脱敏 JSON；平台 Agent 详情页使用心跳中的同一组数据，不额外远程探测。

## 14. 构建与发布

```text
go test ./...
go test -race ./...
go vet ./...
staticcheck ./...
GOOS=windows GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -ldflags "-s -w ..."
```

- 二进制必须 Authenticode 签名。
- 升级先下载、校验签名和 SHA-256，写入 side-by-side 目录，再由服务控制器切换。
- 启动失败自动回滚上一版本；升级期间 ring buffer 不删除。
- 发布物包括 exe、安装/卸载 PowerShell、默认配置、SBOM、SHA-256 和签名说明。

## 15. 测试矩阵

| 类别 | 必测场景 |
| --- | --- |
| WAL | 崩溃中断、末尾坏记录、hash 错、磁盘满、并发 pin、循环覆盖 |
| Collector | 无权限、计数器缺失、磁盘热插拔、NIC 改名、PID 复用、服务快速重启 |
| Event Log | bookmark 恢复、重复事件、消息无法格式化、日志轮转 |
| Trigger | 窗口边界、连续时长、规则热更新、冷却、严重度升级、shadow mode |
| Snapshot | T-30/T+30、Agent 重启、重复触发、部分流缺失、时钟跳变 |
| Upload | 断网、代理、平台 429/500、乱序、重复、坏 hash、平台重启 |
| Security | token 重用、证书吊销、命令重放、敏感信息、压缩炸弹、路径穿越 |
| Performance | 7 天 soak、1000 进程、100 磁盘/NIC、事件风暴、4 GB 上限 |

完成标准：Windows Agent 在 7 天灰度中平均 CPU < 1%、内存 < 120 MB，无缓存损坏；四类故障注入均能在平台还原故障前后顺序。
