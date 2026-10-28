export function syncedInstanceSelection(selectedInstanceId, instances, fallbackToFirst = true) {
  if (selectedInstanceId && instances.some((item) => item.id === selectedInstanceId)) return selectedInstanceId;
  return fallbackToFirst ? instances[0]?.id : undefined;
}
