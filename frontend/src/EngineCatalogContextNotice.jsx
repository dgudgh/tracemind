import React from "react";
import { Alert } from "antd";
import { useEngineCatalog } from "./modules/useEngineCatalog";
import { engineCatalogReason } from "./modules/engineCatalog";

// Read-only context. Navigation and the existing page remain untouched.
export function EngineCatalogContextNotice({ instanceId, instances, children }) {
  const catalog = useEngineCatalog(true, instanceId);
  const selected = instances.find((item) => item.id === instanceId);
  const entry = catalog.value?.engines.find((item) => item.engine_id === selected?.db_type);
  return <>
    <Alert type={catalog.error ? "warning" : "info"} showIcon
      message={catalog.value ? `扩展目录版本 ${catalog.value.catalog_generation} · ${selected ? engineCatalogReason(entry) : "请选择实例"}` : "扩展目录状态未确认"}
      description="现有专业页继续使用原有入口；扩展执行以服务端再次鉴权和运行状态为准。" />
    {children}
  </>;
}
