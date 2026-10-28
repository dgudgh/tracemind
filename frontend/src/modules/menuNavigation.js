export function findMenuKeyPath(items = [], targetKey, parentKeys = []) {
  for (const item of items) {
    const path = [...parentKeys, item.key];
    if (item.key === targetKey) return path;
    const childPath = findMenuKeyPath(item.children || [], targetKey, path);
    if (childPath.length) return childPath;
  }
  return [];
}

export function resolveAccordionOpenKeys(items = [], currentOpenKeys = [], nextOpenKeys = []) {
  const rootGroupKeys = new Set(
    items.filter((item) => item.children?.length).map((item) => item.key),
  );
  const newlyOpenedRoot = nextOpenKeys.find(
    (key) => rootGroupKeys.has(key) && !currentOpenKeys.includes(key),
  );

  return newlyOpenedRoot ? [newlyOpenedRoot] : nextOpenKeys;
}
