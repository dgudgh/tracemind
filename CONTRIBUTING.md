# 参与贡献

感谢你愿意花时间。这个项目偏向"少而准"，比起功能数量，更在意**结论是否可复核**。

## 提交之前

1. **先开 issue 说明问题**，尤其是涉及数据采集、权限、诊断结论的改动。
   这类改动一旦方向错了，返工成本很高。
2. 一个 PR 只做一件事。混合重构与功能改动会让评审无法判断行为变化。
3. 涉及行为变更时，请同时给出**能证伪这次改动的测试**——只写"改完测试通过"不算。

## 开发环境

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt -r backend/requirements-dev.txt
cd frontend && npm ci && cd ..
```

## 提交前自检

```bash
PYTHONPATH=$PWD/backend DATABASE_URL="sqlite:///./test.db" DB_TYPE=sqlite \
  python -m pytest backend/tests -q
python3 scripts/check_all.py --fast
cd frontend && npm run build
```

三项全绿再提 PR。若某项在你的环境本来就红，请在 PR 描述里写清楚是哪一项、报什么错、
以及为什么与你这次改动无关——不要用"历史问题"一句带过。

## 代码约定

- **不要静默吞异常**。判断标准是：这个失败会不会让结论从"无法判断"翻成"正常"。
  会翻的必须抛出或显式降级；不会翻的窄异常捕获（如 `except (TypeError, ValueError)`）可以保留。
- **失败要能定位**。错误信息带上下文（哪个实例、哪条 SQL、第几个条目），不要只抛类型。
- **默认值要表达"未计算"**。数值为 0 与"没算"必须能区分，必要时加 `*_status` 字段。
- **数据库改动**要同时提供迁移脚本，并保证对已有库是幂等的。
- 前端新页面必须接入模块开关与权限，不允许"菜单藏了但接口能调"。

## 提交信息

采用 `type(scope): subject` 形式，例如：

```
fix(inspection): 巡检任务失败时保留原始错误而非空结果
feat(observability): 支持按采集源过滤指标
```

## 报告安全问题

不要开公开 issue，见 [SECURITY.md](SECURITY.md)。
