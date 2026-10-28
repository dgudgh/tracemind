# 巡检报告页面 (Report)

## 概述
执行 Oracle 数据库健康巡检，查看历史报告，导出 Word 报告。通过数据库连接直接执行 SQL 查询，不依赖 SSH。

## 功能点

### 执行巡检
- 下拉选择 Oracle 实例（仅显示 db_type=oracle 的实例）
- 点击"执行巡检"按钮
- 调用 `POST /api/v1/reports/dbcheck`
- 执行 27 项巡检 SQL，结果自动保存到数据库

### 报告展示
- 顶部统计卡片：检查项总数、通过数、失败数
- 分类筛选标签：全部/基础信息/参数配置/存储管理/安全与用户/性能诊断/对象与任务
- 每项检查以卡片形式展示：
  - 左侧彩色边框（绿色=通过，红色=失败）
  - 状态图标（✓/✗）、标题、行数
  - 点击展开查看详细数据表格
- 全部展开/全部折叠快捷操作

### 导出 Word
- 巡检完成后工具栏出现"导出 Word"按钮
- 通过 fetch + Authorization header 下载
- 调用 `GET /api/v1/reports/{id}/export-word`
- 生成格式化的 .docx 文件

### 历史报告列表
- 页面底部显示所有历史巡检报告
- 表格列：#、实例、检查项、通过、失败、巡检时间
- 操作：查看（重新加载报告详情）、导出（下载 Word）、删除

## 涉及 API
| API | 方法 | 说明 |
|-----|------|------|
| /api/v1/reports/dbcheck | POST | 执行巡检并保存 |
| /api/v1/reports | GET | 获取历史报告列表 |
| /api/v1/reports/{id} | GET | 查看报告详情 |
| /api/v1/reports/{id} | DELETE | 删除报告 |
| /api/v1/reports/{id}/export-word | GET | 导出 Word |
| /api/v1/instances | GET | 获取实例列表 |
