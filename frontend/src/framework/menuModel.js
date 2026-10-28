const labelToText = (label) => {
  if (typeof label === "string") return label;
  if (typeof label === "number") return String(label);
  return "";
};

export function findAdminMenuNode(items, key) {
  for (const item of items || []) {
    if (item.key === key) return item;
    const child = findAdminMenuNode(item.children, key);
    if (child) return child;
  }
  return null;
}

export function findAdminBreadcrumb(items, key, parentPath = []) {
  for (const item of items || []) {
    const nextPath = [...parentPath, labelToText(item.label) || item.key].filter(Boolean);
    if (item.key === key) return nextPath;
    const child = findAdminBreadcrumb(item.children, key, nextPath);
    if (child.length) return child;
  }
  return [];
}

export function getAdminMenuLabel(items, key, fallback) {
  const node = findAdminMenuNode(items, key);
  return labelToText(node?.label) || fallback || key;
}

