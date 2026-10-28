# TraceMind Incident Snapshot Engine 前端原型与交互设计

版本：v1.1
日期：2026-08-13
适用技术栈：React 18、Ant Design 5、React Flow、ECharts（按页面懒加载）

> 当前事故中心、现场 Agent 和快照策略已接入菜单与页面。状态时间线、连续线程、内存快照、动态探针、Dump 制品和 eBPF 内核事件属于下一阶段扩展，交互遵循本文同一调查工作台，不再增加新的一级菜单。详见 [State Intelligence 与现场重建](../architecture/state-intelligence-and-scene-reconstruction.md) 和 [eBPF 内核动态观测](../architecture/ebpf-kernel-observability.md)。

## 1. 产品入口

不新增独立一级产品，也不把 Agent、规则、快照拆成多个侧边栏菜单。首版复用现有“智能诊断”入口，在统一诊断工作台内增加四个视图：

```text
智能诊断
├── 诊断工作台（现有 diagnose.smart）
├── 事故中心
├── Agent 管理
└── 快照策略
```

兼容路由：

```text
diagnose.smart
diagnose.incidents
diagnose.snapshot.agents
diagnose.snapshot.policies
```

事故详情使用内部路由状态和工作区标签，不作为菜单项：

```text
/diagnosis/incidents/:incidentId
```

没有现场快照的旧诊断事件仍能打开，页面明确显示“本事故无现场快照”，保留现有规则/AI 诊断能力。

## 2. 设计原则

- 安静、专业、偏苹果式工具界面，但不使用营销 Hero、装饰渐变或大面积单色。
- 信息层次通过留白、分隔线、字体和状态点建立，不把每个区块做成悬浮卡片。
- 页面区域不嵌套卡片；卡片只用于重复的证据项和根因候选。
- 所有业务详情和表单使用居中 Modal，不再使用右侧 Drawer。
- Modal 最大宽度 `min(1180px, calc(100vw - 48px))`，最大高度 `calc(100vh - 48px)`，头尾固定、内容滚动。
- 时间轴、指标图和因果图有稳定高度；加载、空数据和标签变化不能引起页面抖动。
- 颜色只表达状态：绿色正常、橙色警告、红色严重、蓝色信息、灰色未知；数据库类型颜色不覆盖严重度。
- 所有 RCA 结论旁必须有“查看证据”，没有证据时显示“证据不足”。

## 3. 页面信息架构

### 3.1 事故中心

工作目标：快速回答“发生了什么、影响谁、是否已冻结现场、当前处理到哪一步”。

```text
┌──────────────────────────────────────────────────────────────────────────┐
│ 事故中心                                   [刷新] [人工采集现场]          │
│ 现场冻结 12  · 分析中 2 · 严重 3 · Agent 离线 1                          │
├──────────────────────────────────────────────────────────────────────────┤
│ 搜索事故/主机/实例  [状态] [等级] [环境] [时间范围] [有无快照]            │
├──────────────────────────────────────────────────────────────────────────┤
│ 事故号       标题/影响对象        等级  故障时间   快照状态  质量  状态   │
│ INC-...      存储路径异常         严重  22:10      已就绪    96%   分析中 │
│ INC-...      SQL Server 提交延迟  警告  21:42      部分      71%   已定位 │
└──────────────────────────────────────────────────────────────────────────┘
```

字段：

- 事故号、标题、影响资产、环境/区域。
- 严重度、触发来源、故障时间。
- 快照状态、完整度、质量分、缺失流。
- 首要根因和置信度（未分析时为空，不填假值）。
- 事故状态、负责人、工单、最后更新时间。

交互：

- 点击行进入事故详情工作区。
- “人工采集现场”打开居中 Modal，必须选择 Host/Agent、前后窗口和原因；提交后显示可关闭并可重新打开的进度 Modal。
- 表格按服务端分页，筛选条件进入 URL query，刷新后可恢复。
- 快照 `partial` 通过橙色状态和缺失摘要展示，不与执行失败混为一谈。

### 3.2 事故详情

顶部是紧凑上下文栏，不使用巨型概览卡：

```text
INC-20260803-00001 · 存储路径异常          严重 · 分析中
db-win-01 / MES生产 / 上海IDC A区          22:10:03 ~ 22:40:10
[现场完整 96%] [时钟偏移 37ms] [Agent 在线] [创建工单] [导出]
```

主内容采用固定视图标签：

```text
概览 | 故障时间线 | 证据 | 因果与根因 | 原始现场 | 处置与复盘
```

#### 概览

```text
┌──────────────────────────────┬───────────────────────────────────────────┐
│ 首要结论                     │ 故障传播链                                │
│ 存储路径故障  92%            │ Storage -> Disk IO -> DB Wait -> Timeout  │
│ 支持证据 5 · 反证 3          │ [打开因果图]                              │
├──────────────────────────────┴───────────────────────────────────────────┤
│ 关键时刻                                                                  │
│ 22:10:03 延迟升高 | 22:10:10 StorPort 129 | 22:10:15 WRITELOG | 22:12 超时│
├──────────────────────────────────────────────────────────────────────────┤
│ 关键趋势：磁盘写延迟 / DB IO 等待 / 应用超时率（共用时间游标）            │
└──────────────────────────────────────────────────────────────────────────┘
```

- 首要结论必须显示置信度等级和数据质量影响。
- “排除 CPU / 内存 / 网络”不做成功徽章堆砌，在结论下以一行反证摘要展示。
- 三张趋势图共用时间范围、缩放和垂直游标，选择某时刻会同步时间线。

#### 故障时间线

```text
时间范围 [T-30m | T-10m | 全窗口]  [层级] [来源] [等级]  [聚合相似事件]

22:10:03.120  存储  disk.write_latency 5ms -> 180ms             严重
22:10:10.042  系统  StorPort Event 129: Reset to device          严重
22:10:15.300  数据库 SQL Server WRITELOG waits +420%             警告
22:12:01.010  应用  MES API timeout rate 0.2% -> 18%             严重
```

- 默认聚合同类高频事件，显示数量和时间范围；用户可展开原始记录。
- 左侧时间列固定宽度，正文自然换行，避免压缩成一行。
- 点击事件打开居中证据 Modal，展示原始字段、来源 chunk、hash 和相邻事件。
- 数据源时间偏移时显示小型“时间已校正”图标和说明，不修改原始时间。

#### 证据

证据卡用于重复项，可按“支持 / 反证 / 上下文”分组：

```text
┌ 磁盘写延迟异常                                      严重 · 96% ┐
│ 22:10:03 ~ 22:16:42  峰值 180ms  基线 5ms  增长 36 倍           │
│ 影响：SQL Server 日志写入；支持：指标 720 点、事件 2 条         │
│ [查看原始证据] [在时间线定位]                                  │
└─────────────────────────────────────────────────────────────────┘
```

- 卡片默认只显示结论和关键量化信息，字段说明在 info 图标弹窗中展示。
- 原始证据 Modal 内用表格展示 source、时间、实际值、基线、单位、hash；指标用图，事件用结构化字段。
- 证据不存在或对应 chunk 缺失时，按钮禁用并显示明确原因。

#### 因果与根因

```text
┌─────────────────────────────────────────────┬────────────────────────────┐
│ 因果图（React Flow）                        │ 根因候选                   │
│ Storage -> Disk latency -> DB wait -> API   │ 1 存储路径故障       92%  │
│                                             │   支持 5 / 反证 0          │
│ [适应视图] [支持证据] [显示反证]            │ 2 数据库内部锁       18%  │
│                                             │   已排除：无阻塞会话       │
└─────────────────────────────────────────────┴────────────────────────────┘
```

- 主因从左到右，影响链顺序固定；节点宽 `240px`，不随内容变化。
- 边显示因果关系和置信度，不使用动画流光干扰阅读。
- 候选点击后只高亮相关节点、边和证据，不重新布局导致画布跳动。
- “确认根因 / 排除候选”属于人工判断，打开居中确认 Modal，要求填写原因并审计。

#### 原始现场

内部二级标签：

```text
主机指标 | 进程 | 服务 | Windows 事件 | 数据库状态 | 快照文件
```

- 主机指标：指标选择器 + 单个共享图表区，不一次绘制几十张图。
- 进程：时间点选择 + CPU/内存/IO Top-N 表格，PID 与启动时间一同展示。
- 服务：状态变化时间线、退出码、重启次数和 PID。
- Windows 事件：provider、event ID、level、结构化 data；默认不展示 Security 正文。
- 数据库状态：按 Oracle/MySQL/PostgreSQL/SQL Server 插件渲染结构化结果。
- 快照文件：manifest 和 chunk 完整性；原始包下载需要专门权限。

#### 处置与复盘

复用现有 Plan Only、ITSM、通知和复盘组件：

- 推荐动作必须引用证据卡和风险。
- 执行前后指标使用同一时间范围对比。
- 事故解决后显示验证窗口；指标未恢复时不能自动标记“验证通过”。
- 复盘可把已确认因果链沉淀到 `DiagMethodology`。

### 3.3 Agent 管理

```text
Agent 管理                            [生成安装令牌] [刷新]
在线 28 · 降级 2 · 离线 1 · 待升级 6

主机        状态  版本  最近心跳  缓存  上传积压  时钟  采集器  操作
db-win-01   在线  1.0.0  12s       38%   0         37ms  7/7     查看
```

详情 Modal：

- 身份：Agent ID、Host 资产、证书序列号和到期时间。
- 健康：资源占用、缓存、丢弃记录、上传积压、时钟偏移。
- 采集器：状态、上次成功、耗时、连续失败和降级原因。
- 配置：当前版本、期望版本、最近应用结果；只读展示脱敏配置。
- 操作：采集现场、重新下发配置、升级、吊销；每项显示进度和审计确认。

### 3.4 快照策略

页面分为“规则列表”和“版本详情”，不在一行塞入整个规则表达式。

规则列表字段：名称、范围、模式、等级、触发次数、快照次数、误报确认、版本、更新时间。

规则编辑 Modal：

```text
基本信息
  名称 / 适用 OS / 资产范围 / 等级

触发条件
  [全部满足 v]
  指标  disk.write_latency_ms  >=  100  持续 30s
  事件  Microsoft-Windows-StorPort  Event ID [129,153]
  [+ 添加条件] [+ 添加条件组]

窗口与抑制
  故障前 30m / 故障后 30m / 冷却 30m

发布
  Shadow 模式 / 命中预览 / 版本说明
```

- 指标、运算符、单位和阈值使用结构化控件，不让用户手写 JSON。
- 每个字段的说明通过统一 info 图标弹出表格，不直接铺满页面。
- “测试规则”针对历史指标运行，展示每个条件实际值，不创建快照。
- 发布生成不可变版本；修改已发布规则必须新建版本。

## 4. 居中 Modal 规范

统一组件 `AdaptiveModal`：

```jsx
<Modal
  centered
  width="min(1180px, calc(100vw - 48px))"
  styles={{
    body: { maxHeight: "calc(100vh - 180px)", overflow: "auto" },
  }}
/>
```

断点：

- `>= 1280px`：详情 `1180px`，表单 `760px`，确认 `480px`。
- `768-1279px`：`calc(100vw - 40px)`。
- `< 768px`：`calc(100vw - 24px)`，最大高度 `calc(100vh - 24px)`。
- Modal 内表格在自己的稳定容器横向滚动，不拉宽页面。
- 主操作按钮固定在 footer 右侧；关闭按钮始终可见。

## 5. 组件结构

```text
IncidentCenterPage
├── IncidentContextBar
├── IncidentFilterBar
├── IncidentTable
├── CaptureSnapshotModal
└── SnapshotProgressModal

IncidentDetailPage
├── IncidentHeader
├── SnapshotQualityStrip
├── IncidentViewTabs
├── IncidentOverview
│   ├── PrimaryCausePanel
│   ├── PropagationSummary
│   └── SynchronizedMetricTrends
├── IncidentTimeline
├── EvidenceWorkspace
├── CausalGraphWorkspace
├── RawSnapshotWorkspace
└── ResolutionReviewWorkspace

IncidentAgentPage
├── AgentHealthSummary
├── AgentTable
└── AgentDetailModal

SnapshotPolicyPage
├── RuleTable
├── RuleEditorModal
├── RuleTestModal
└── RuleVersionModal
```

复用现有：`AppPageRouter`、`OpsLayout`、统一报告 Modal、ITSM、审批、通知、React Flow 主题和 API 错误处理。不要复制一套独立 Shell。

## 6. 前端数据契约

### 6.1 事故列表

```ts
type IncidentListItem = {
  id: number;
  incident_no: string;
  title: string;
  severity: "info" | "warning" | "critical";
  status: string;
  started_at: string;
  impacted_assets: AssetSummary[];
  snapshot?: {
    snapshot_id: string;
    status: SnapshotStatus;
    completeness_pct: number;
    quality_score: number;
    missing_streams: string[];
  };
  primary_root_cause?: { title: string; confidence: number };
};
```

### 6.2 时间线

```ts
type TimelinePage = {
  items: TimelineItem[];
  next_cursor?: string;
  aggregation: { enabled: boolean; raw_count: number; displayed_count: number };
  clock_corrections: ClockCorrection[];
};
```

API 必须返回稳定 `event_id`，前端不得使用数组下标作为 key。分页追加时保持当前滚动和时间游标。

### 6.3 证据

```ts
type EvidenceCard = {
  evidence_id: string;
  title: string;
  role: "support" | "contradict" | "context";
  severity: string;
  confidence: number;
  start_time?: string;
  peak_time?: string;
  end_time?: string;
  baseline?: MetricValue;
  observed?: MetricValue;
  source_count: number;
  source_available: boolean;
  source_unavailable_reason?: string;
};
```

## 7. API 调用和状态处理

- 列表加载、详情加载、后台刷新和用户操作使用独立 loading 状态，不因一行操作清空整表。
- 快照进度优先使用 SSE；连接中断后退回 3 秒轮询，并从 `last_event_id` 恢复。
- 捕获命令提交成功只代表“已受理”，不能直接 toast“采集成功”。
- 状态显示：`已触发 -> 采集故障后窗口 -> 打包 -> 上传 -> 校验 -> 分析 -> 已就绪`。
- 429 显示平台限流和预计重试时间；409 显示幂等结果或状态冲突；422 显示字段级错误。
- 后端不可用时保留已加载数据并显示陈旧时间，不把页面变成空白。
- 取消页面请求使用 `AbortController`，避免切页后旧响应覆盖新状态。

## 8. 图表和画布

- 时间序列使用 ECharts，按路由懒加载；服务端返回降采样点，前端不一次接收百万点。
- 多图共享 `dataZoom` 和 axis pointer；单位不同不放在同一 Y 轴误导比较。
- 因果图使用现有 `@xyflow/react`，节点尺寸固定，布局结果缓存。
- 一至三个节点的画布固定 `420px` 高；更多节点最大 `620px`，画布内部平移缩放，页面不无限增长。
- ResizeObserver 只在容器实际尺寸变化时重算，使用 requestAnimationFrame 合并，避免小窗口抖动。
- 图表加载和空状态保留同等高度，切换数据不改变页面布局。

## 9. 响应式设计

### 桌面 `1440x900`

- 左侧现有导航宽度不变。
- 事故列表默认显示完整字段，表格容器内部横向滚动作为兜底。
- 详情概览两列，因果图和候选根因按 `minmax(0, 1fr) 360px`。

### 小桌面 `1024x768`

- 筛选器换行但保持控件高度一致。
- 详情概览改为单列；因果候选在图下方。
- 时间线正文允许两行，操作列保持固定。

### 移动/窄屏 `390x844`

- 侧栏使用现有 overlay drawer，不挤压内容。
- 事故表切为列表行，不堆卡片；每行显示事故号、标题、状态、时间和快照质量。
- 时间轴时间放在事件标题上方，避免左列占据有效宽度。
- 图表保持固定高度并横向手势缩放。
- 因果图保持节点尺寸，允许画布平移，不压缩文字。

## 10. 空、错、降级状态

| 场景 | 页面表现 |
| --- | --- |
| 无 Agent | 显示安装入口和系统要求，不伪造示例数据 |
| Agent 离线 | 保留最近健康与缓存数据，标记最后心跳 |
| 快照无事件流 | 显示缺失流和质量扣分，其他指标仍可查看 |
| 上传失败 | 展示失败分片、重试状态和 Agent 积压，不显示“采集失败”笼统文案 |
| 分析失败 | 快照原始现场仍可用，提供重分析入口 |
| AI 不可用 | 展示规则 RCA 和证据，不阻断页面 |
| 原始证据已过期 | 保留证据摘要和 hash，按钮显示过期原因 |
| 无权限下载 | 隐藏下载命令并在权限说明中列出所需权限 |

## 11. 埋点与审计

产品埋点不包含事件正文或原始指标：

```text
incident_list_open
incident_detail_open
snapshot_capture_requested
snapshot_progress_reopened
evidence_source_opened
root_cause_confirmed
root_cause_rejected
raw_package_download_requested
trigger_rule_tested
trigger_rule_published
```

确认根因、排除候选、下载原始包、修改保留策略、触发快照和 Agent 吊销必须调用后端审计接口，前端埋点不能替代审计。

## 12. 验收场景

1. Zabbix 告警触发后，事故中心 5 秒内出现 `triggered` 状态并实时推进。
2. Agent 断网时本地快照继续完成，恢复网络后断点续传且不重复创建事故。
3. `partial` 快照明确列出缺失 Event Log，但 CPU/磁盘证据仍可查看。
4. 时间线能够按顺序展示 Disk latency、StorPort 129、DB wait、应用超时。
5. 根因“存储路径故障”能打开至少两类独立证据，CPU/内存排除项也有原始数据。
6. 人工确认根因后刷新仍保留，并显示操作人、时间和原因。
7. 旧诊断事件没有快照时仍可执行原有规则/AI 诊断。
8. 桌面、小桌面、窄屏无文字重叠、无页面抖动、无异常横向拖动。
9. 所有 Modal 居中、大小适配、关闭和主操作始终可见。
10. 浏览器控制台无错误，前端契约测试、生产构建和 Playwright 截图检查通过。
