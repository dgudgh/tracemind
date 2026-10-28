import { api, buildQuery } from "../api";

function ownerTableQuery({ owner, tableName, extra = {} } = {}) {
  return buildQuery({
    ...extra,
    owner,
    table_name: tableName,
  });
}

function ownerIndexQuery({ owner, indexName } = {}) {
  return buildQuery({
    owner,
    index_name: indexName,
  });
}

function toQuery(params = {}) {
  if (params instanceof URLSearchParams) {
    return buildQuery(Object.fromEntries(params.entries()));
  }
  return buildQuery(params);
}

export function getFragmentationSchemas(instanceId) {
  return api.get(`/oracle/${instanceId}/table-fragmentation${buildQuery({ min_size_mb: 0, limit: 10 })}`);
}

export function getTableFragmentation(instanceId, { owner, minSizeMb }) {
  return api.get(`/oracle/${instanceId}/table-fragmentation${buildQuery({
    min_size_mb: minSizeMb,
    limit: 200,
    owner,
  })}`);
}

export function getIndexFragmentation(instanceId, owner) {
  return api.get(`/oracle/${instanceId}/index-fragmentation${buildQuery({ owner })}`);
}

export function getUnusedIndexes(instanceId, { owner, days }) {
  return api.get(`/oracle/${instanceId}/unused-indexes${buildQuery({ owner, days })}`);
}

export async function getUnusableIndexBundle(instanceId, { owner, tableName }) {
  const suffix = ownerTableQuery({ owner, tableName });
  const [normal, partitions, abnormal] = await Promise.all([
    api.get(`/oracle/${instanceId}/indexes/unusable${suffix}`),
    api.get(`/oracle/${instanceId}/indexes/partitions/unusable${suffix}`),
    api.get(`/oracle/${instanceId}/indexes/abnormal${suffix}`),
  ]);
  const items = [
    ...(normal.items || []).map((item) => ({ ...item, item_type: "NORMAL" })),
    ...(partitions.items || []),
    ...(abnormal.items || []).map((item) => ({ ...item, item_type: "ABNORMAL" })),
  ];
  return { items, normal, partitions, abnormal, total: items.length };
}

export function buildRebuildIndexPlan(instanceId, payload) {
  return api.post(`/oracle/${instanceId}/indexes/rebuild-plan`, payload);
}

export function getTableColumns(instanceId, owner, tableName) {
  return api.get(`/oracle/${instanceId}/tables/${encodeURIComponent(owner)}/${encodeURIComponent(tableName)}/columns`);
}

export function checkDuplicateIndexPlan(instanceId, payload) {
  return api.post(`/oracle/${instanceId}/indexes/check-duplicate`, payload);
}

export function buildCreateIndexPlan(instanceId, payload) {
  return api.post(`/oracle/${instanceId}/indexes/create-plan`, payload);
}

export function getStatsTables(instanceId, { owner, tableName, staleOnly }) {
  const query = ownerTableQuery({
    owner,
    tableName,
    extra: staleOnly ? { stale_only: "true" } : {},
  });
  return api.get(`/oracle/${instanceId}/stats/tables${query}`);
}

export async function getStatsDetailBundle(instanceId, { owner, tableName }) {
  const query = ownerTableQuery({ owner, tableName });
  const [detail, columns, indexes, history] = await Promise.all([
    api.get(`/oracle/${instanceId}/stats/table-detail${query}`),
    api.get(`/oracle/${instanceId}/stats/columns${query}`),
    api.get(`/oracle/${instanceId}/stats/indexes${query}`),
    api.get(`/oracle/${instanceId}/stats/history${query}`),
  ]);
  return { detail: detail.detail, columns: columns.columns || [], indexes: indexes.indexes || [], history: history.history || [] };
}

export function getStatsOperations(instanceId) {
  return api.get(`/oracle/${instanceId}/stats/operations`);
}

export function buildStatsPlan(instanceId, endpoint, payload) {
  return api.post(`/oracle/${instanceId}/stats/${endpoint}`, payload);
}

export function getPartitionCandidates(instanceId, { owner, tableName, minRows }) {
  const query = ownerTableQuery({
    owner,
    tableName,
    extra: { min_rows: String(minRows || 5000000) },
  });
  return api.get(`/oracle/${instanceId}/partition/candidates${query}`);
}

export function getPartitionProfile(instanceId, { owner, tableName, exactCount = false }) {
  const query = ownerTableQuery({ owner, tableName, extra: { exact_count: String(exactCount) } });
  return api.get(`/oracle/${instanceId}/partition/table-profile${query}`);
}

export function getPartitionKeys(instanceId, { owner, tableName }) {
  const query = ownerTableQuery({ owner, tableName });
  return api.get(`/oracle/${instanceId}/partition/key-candidates${query}`);
}

export function buildPartitionConvertPlan(instanceId, payload) {
  return api.post(`/oracle/${instanceId}/partition/convert-plan`, payload);
}

export function getTableUsage(instanceId, endpoint, params) {
  return api.get(`/oracle/${instanceId}/table-usage/${endpoint}${toQuery(params)}`);
}

export function collectTableUsageSnapshot(instanceId, { days, source }) {
  return api.post(`/oracle/${instanceId}/table-usage/collect${buildQuery({ days: days || 30, source: source || "AWR" })}`);
}

export function buildTableUsageIndexPlan(instanceId, endpoint, payload) {
  return api.post(`/oracle/${instanceId}/table-usage/${endpoint}`, payload);
}

export function getSimilarTables(instanceId, { owner, pattern }) {
  return api.get(`/oracle/${instanceId}/similar-tables${buildQuery({ owner, pattern })}`);
}

export function buildBatchCreateIndexPlan(instanceId, payload) {
  return api.post(`/oracle/${instanceId}/batch-create-index`, payload);
}

export function buildBatchAlterColumnPlan(instanceId, payload) {
  return api.post(`/oracle/${instanceId}/batch-alter-column`, payload);
}

export function getTableShrinkScript(instanceId, { owner, tableName }) {
  return api.get(`/oracle/${instanceId}/table-shrink-script${buildQuery({ owner, table_name: tableName })}`);
}

export function getIndexRebuildScript(instanceId, { owner, indexName }) {
  return api.get(`/oracle/${instanceId}/index-rebuild-script${ownerIndexQuery({ owner, indexName })}`);
}

export function getDropIndexScript(instanceId, { owner, indexName }) {
  return api.get(`/oracle/${instanceId}/drop-index-script${ownerIndexQuery({ owner, indexName })}`);
}
