export const TOPOLOGY_NODE_WIDTH = 320;
export const TOPOLOGY_NODE_HEIGHT = 146;

export function buildAssetTopology(cluster, members) {
  const memberIds = new Set(members.map((item) => String(item.id)));
  const clusterNodeId = `cluster:${cluster?.id}`;
  const edges = [];
  const seen = new Set();
  for (const relation of Array.isArray(cluster?.relations) ? cluster.relations : []) {
    if (!relation || relation.status !== "active" || relation.source_type !== "instance") continue;
    const source = String(relation.source_id);
    if (!memberIds.has(source)) continue;
    let target;
    if (relation.relation_type === "replicates_to" && relation.target_type === "instance") {
      if (cluster?.architecture === "mgr") continue;
      target = String(relation.target_id);
      if (!memberIds.has(target) || source === target) continue;
    } else if (["rac", "mgr"].includes(cluster?.architecture) && relation.relation_type === "member_of"
      && relation.target_type === "cluster" && String(relation.target_id) === String(cluster.id)) {
      target = clusterNodeId;
    } else {
      continue;
    }
    const id = `${relation.relation_type}:${source}:${target}`;
    if (seen.has(id)) continue;
    seen.add(id);
    edges.push({ id, source, target, relation_type: relation.relation_type });
  }
  const hasClusterNode = edges.some((edge) => edge.target === clusterNodeId);
  const ids = [...memberIds, ...(hasClusterNode ? [clusterNodeId] : [])];
  const incoming = new Map(ids.map((id) => [id, 0]));
  const outgoing = new Map(ids.map((id) => [id, []]));
  const levels = new Map(ids.map((id) => [id, 0]));
  for (const edge of edges) {
    incoming.set(edge.target, incoming.get(edge.target) + 1);
    outgoing.get(edge.source).push(edge.target);
  }
  const queue = ids.filter((id) => incoming.get(id) === 0);
  for (let index = 0; index < queue.length; index += 1) {
    const source = queue[index];
    for (const target of outgoing.get(source)) {
      levels.set(target, Math.max(levels.get(target), levels.get(source) + 1));
      incoming.set(target, incoming.get(target) - 1);
      if (incoming.get(target) === 0) queue.push(target);
    }
  }
  // Cyclic components remain visible without changing or inventing their edges.
  const cycleLevel = Math.max(0, ...levels.values()) + 1;
  for (const id of ids) if (incoming.get(id) > 0) levels.set(id, cycleLevel);
  const columns = new Map();
  for (const id of ids) {
    const level = levels.get(id);
    if (!columns.has(level)) columns.set(level, []);
    columns.get(level).push(id);
  }
  const rows = Math.max(1, ...[...columns.values()].map((column) => column.length));
  const positions = {};
  [...columns.keys()].sort((a, b) => a - b).forEach((level, columnIndex) => {
    const column = columns.get(level);
    column.forEach((id, index) => {
      positions[id] = {
        x: columns.size === 1 ? 350 : 50 + columnIndex * 570,
        y: 24 + ((rows - column.length) / 2 + index) * (TOPOLOGY_NODE_HEIGHT + 22),
      };
    });
  });
  return { edges, positions, hasClusterNode, clusterNodeId };
}
