export function engineCatalogReason(entry) {
  if (!entry) return "目录中无此引擎";
  if (entry.available) return "扩展运行时可用";
  return ({ not_installed: "尚未安装扩展包", disabled: "扩展包已禁用", isolated: "扩展包已隔离",
    incompatible: "扩展包不兼容", driver_unavailable: "驱动不可用", unsupported: "当前不支持",
    fixture_only: "仅测试包", requires_runtime_validation: "等待运行时验证" })[entry.reason] || "状态未确认";
}
