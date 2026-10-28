import { useEffect, useState } from "react";
import { Tag } from "antd";
import { syncedInstanceSelection } from "./modules/instanceSelection";

export function useSyncedInstanceId(selectedInstanceId, availableInstances, { fallbackToFirst = true } = {}) {
  const [instanceId, setInstanceId] = useState(() => syncedInstanceSelection(selectedInstanceId, availableInstances, fallbackToFirst));

  useEffect(() => {
    setInstanceId(syncedInstanceSelection(selectedInstanceId, availableInstances, fallbackToFirst));
  }, [selectedInstanceId, availableInstances, fallbackToFirst]);

  return [instanceId, setInstanceId];
}

export function statusTag(value) {
  const normalized = String(value || "info").toLowerCase();
  const color = {
    critical: "red",
    high: "volcano",
    warning: "orange",
    warn: "orange",
    info: "blue",
    normal: "green",
    success: "green",
    error: "red",
    failed: "red",
  }[normalized] || "default";

  return <Tag color={color}>{value || "-"}</Tag>;
}
