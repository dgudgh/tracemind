"""SkyWalking 采集器：GraphQL 查询服务响应时间（APM/Trace 层）。

数据源：endpoint 指向 SkyWalking GraphQL（如 http://host:12800/graphql）
auth：{token}（可选，OAP 鉴权）
config：service(服务名) minutes(回看分钟,默认15) resp_warn(ms,1000) resp_crit(3000)

说明：SkyWalking 版本众多，metrics schema 有差异。本采集器尽力获取，失败仅产出可达性信号。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.collectors.diag.base import CollectContext, Signal, http_post_json, status_of, LAYER_APP, LAYER_BUSINESS


def _headers(ctx: CollectContext) -> dict:
    h = {"Content-Type": "application/json"}
    token = (ctx.auth or {}).get("token")
    if token:
        h["Authorization"] = token
    return h


def _reachable(ctx: CollectContext) -> bool:
    data = http_post_json(ctx.endpoint, {"query": "{ version }"}, timeout=6, headers=_headers(ctx))
    return bool(data and "data" in data)


def collect(ctx: CollectContext) -> list[Signal]:
    if not ctx.endpoint:
        return [Signal(LAYER_APP, "skywalking", "config", 0, status="warn", node_type="apm",
                       phenomenon=f"SkyWalking 数据源未配置 endpoint：{ctx.name}",
                       judgement="APM 采集缺少 GraphQL 地址，无法读取调用链和服务响应时间。",
                       impact="缺少 Trace 视角，端到端诊断无法向上定位应用服务慢点。",
                       suggestion="配置 SkyWalking OAP GraphQL 地址，例如 http://host:12800/graphql。")]
    service = ctx.cfg("service")
    if not _reachable(ctx):
        return [Signal(LAYER_APP, "skywalking", "reachable", 0, status="warn", node_type="apm",
                       phenomenon=f"SkyWalking 不可达：{ctx.name}",
                       judgement="APM 探测失败，无法获取调用链时延。",
                       impact="缺少 Trace 视角，端到端定位能力下降。",
                       suggestion="确认 OAP GraphQL 地址与鉴权。")]

    if not service:
        # 仅可达，给一条信息信号（不算异常）
        return [Signal(LAYER_APP, "skywalking", "reachable", 1, status="ok", node_type="apm",
                       phenomenon="SkyWalking 可达（未配置目标服务名）",
                       judgement="可在数据源 config.service 指定服务以采集响应时间。")]

    # 查询服务响应时间（按版本尽力；失败则降级）
    now = datetime.now()
    start = now - timedelta(minutes=int(ctx.cfg("minutes", 15)))
    query = {
        "query": """query queryData($duration: Duration!, $condition: MetricsCondition!) {
            readMetricsValues(condition: $condition, duration: $duration) {
                values { values { value } }
            }
        }""",
        "variables": {
            "duration": {"start": start.strftime("%Y-%m-%d %H%M"), "end": now.strftime("%Y-%m-%d %H%M"), "step": "MINUTE"},
            "condition": {"name": "service_resp_time", "entity": {"scope": "Service", "serviceName": service, "normal": True}},
        },
    }
    data = http_post_json(ctx.endpoint, query, timeout=8, headers=_headers(ctx))
    try:
        vals = data["data"]["readMetricsValues"]["values"]["values"]
        nums = [v["value"] for v in vals if v.get("value") not in (None, 0)]
        avg = round(sum(nums) / len(nums), 1) if nums else None
    except Exception:
        avg = None

    if avg is None:
        return [Signal(LAYER_APP, "skywalking", "service_resp_time", None, status="ok", node_type="apm",
                       phenomenon=f"SkyWalking 服务 {service} 响应时间未取到",
                       judgement="metrics schema 不匹配或近期无数据。")]
    st = status_of(avg, ctx.cfg("resp_warn", 1000), ctx.cfg("resp_crit", 3000))
    return [Signal(
        LAYER_BUSINESS, "skywalking", "service_resp_time", avg, threshold=ctx.cfg("resp_warn", 1000),
        status=st, node_type="apm",
        phenomenon=f"服务 {service} 平均响应 {avg}ms",
        judgement="接口端到端时延高，需沿调用链下钻定位慢点（应用/DB/中间件）。" if st != "ok" else "接口响应正常。",
        impact="业务接口慢直接影响用户/MES 过站体验。",
        suggestion="按 Trace 下钻定位最慢 span，对照数据库慢 SQL/等待事件。",
    )]
