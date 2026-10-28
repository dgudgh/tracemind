import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { buildAssetTopology, TOPOLOGY_NODE_WIDTH, TOPOLOGY_NODE_HEIGHT } from "../src/AssetTopology.js";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const page = readFileSync(path.join(root, "src/CmdbAssetPage.jsx"), "utf8");
const styles = readFileSync(path.join(root, "src/styles.css"), "utf8");

test("common one-to-three-node topologies use fixed canvas and node dimensions", () => {
  assert.equal(TOPOLOGY_NODE_WIDTH, 320);
  assert.equal(TOPOLOGY_NODE_HEIGHT, 146);
  assert.match(page, /buildAssetTopology\(cluster, members\)/);
  assert.match(page, /function TopologyAutoFit\(\{ containerRef, topologyKey \}\)/);
  assert.match(page, /new window\.ResizeObserver\(refit\)/);
  assert.doesNotMatch(page, /const flowHeight =/);
  assert.match(styles, /\.asset-five-topology-map\s*\{[\s\S]*?height: 360px;[\s\S]*?max-height: 360px;/);
  assert.match(styles, /\.asset-five-node\s*\{[\s\S]*?height: 146px;[\s\S]*?max-height: 146px;/);
});

const members = [
  { id: 1, role: "primary" },
  { id: 2, role: "replica" },
  { id: 3, role: "replica" },
];
const replication = (source, target, extra = {}) => ({
  source_type: "instance", source_id: source, target_type: "instance", target_id: target,
  relation_type: "replicates_to", status: "active", ...extra,
});
const topology = (relations, extra = {}, nodes = members) => buildAssetTopology({ id: 10, relations, ...extra }, nodes);
const endpoints = (graph) => graph.edges.map(({ source, target }) => [source, target]);

test("cascading replication follows saved endpoints, not primary roles", () => {
  const graph = topology([replication(1, 2), replication(2, 3)]);
  assert.deepEqual(endpoints(graph), [["1", "2"], ["2", "3"]]);
  assert.ok(graph.positions[1].x < graph.positions[2].x);
  assert.ok(graph.positions[2].x < graph.positions[3].x);
  assert.deepEqual(endpoints(topology([replication(3, 2), replication(2, 1)])), [["3", "2"], ["2", "1"]]);
});

test("normal primary-replica fanout has centered fixed-size nodes", () => {
  const graph = topology([replication(1, 2), replication(1, 3)]);
  assert.equal(graph.positions[2].x, graph.positions[3].x);
  assert.equal(graph.positions[1].y, (graph.positions[2].y + graph.positions[3].y) / 2);
  assert.ok(graph.positions[3].y - graph.positions[2].y > TOPOLOGY_NODE_HEIGHT);
});

test("NULL, empty, missing, and malformed relations never invent edges", () => {
  for (const relations of [null, undefined, [], {}, "unknown", [null, {}]]) {
    const graph = topology(relations);
    assert.deepEqual(graph.edges, []);
    assert.equal(Object.keys(graph.positions).length, 3);
  }
  assert.match(page, /复制关系未知（未采集）/);
  assert.doesNotMatch(page, /const anchor =|right\.map/);
});

test("only active typed in-scope relations survive, with duplicates removed", () => {
  const valid = replication(1, 2);
  const graph = topology([
    valid, { ...valid, id: 99 }, replication(2, 99), replication(99, 1), replication(1, 1),
    replication(1, 3, { status: "inactive" }), replication(1, 3, { status: undefined }),
    replication(1, 3, { source_type: "host" }), replication(1, 3, { target_type: "cluster" }),
    replication(1, 3, { relation_type: "runs_on" }),
  ]);
  assert.deepEqual(endpoints(graph), [["1", "2"]]);
});

test("RAC membership is separate from replication and has no invented instance links", () => {
  const membership = members.map(({ id }) => replication(id, 10, { target_type: "cluster", relation_type: "member_of" }));
  const graph = topology(membership, { architecture: "rac" });
  assert.equal(graph.hasClusterNode, true);
  assert.deepEqual(endpoints(graph), [["1", "cluster:10"], ["2", "cluster:10"], ["3", "cluster:10"]]);
  assert.ok(graph.edges.every((edge) => edge.relation_type === "member_of"));
  assert.deepEqual(topology([], { architecture: "rac" }).edges, []);
  assert.deepEqual(topology(membership, { architecture: "primary_replica" }).edges, []);
  assert.deepEqual(topology([replication(1, 11, { target_type: "cluster", relation_type: "member_of" })], { architecture: "rac" }).edges, []);
  const mixed = topology([...membership, replication(1, 2)], { architecture: "rac" });
  assert.equal(mixed.edges.filter((edge) => edge.relation_type === "replicates_to").length, 1);
  assert.match(page, /markerEnd: isMembership \? undefined : \{ type: MarkerType.ArrowClosed/);
  assert.match(page, /strokeDasharray: isMembership \? "5 4" : undefined/);
});

test("MGR renders saved group membership without inventing replication directions", () => {
  const membership = members.map(({ id }) => replication(id, 10, { target_type: "cluster", relation_type: "member_of" }));
  const graph = topology([...membership, replication(1, 2)], { architecture: "mgr" });
  assert.equal(graph.hasClusterNode, true);
  assert.deepEqual(endpoints(graph), [["1", "cluster:10"], ["2", "cluster:10"], ["3", "cluster:10"]]);
  assert.ok(graph.edges.every(({ relation_type }) => relation_type === "member_of"));
  assert.deepEqual(topology([], { architecture: "mgr" }).edges, []);
  assert.match(page, /MySQL MGR 组成员关系/);
});

test("cycles, disconnected and single nodes remain finite, distinct, and stable", () => {
  for (const graph of [topology([replication(1, 2), replication(2, 1)]), topology([]), topology([], {}, [members[0]]), topology([], {}, [])]) {
    const positions = Object.values(graph.positions);
    assert.ok(positions.every(({ x, y }) => Number.isFinite(x) && Number.isFinite(y)));
    assert.equal(new Set(positions.map(({ x, y }) => `${x},${y}`)).size, positions.length);
  }
  assert.deepEqual(endpoints(topology([replication(1, 2), replication(2, 1)])), [["1", "2"], ["2", "1"]]);
});

test("compact screens retain the same stable topology canvas height", () => {
  assert.match(styles, /@media \(max-width: 760px\)[\s\S]*?\.asset-five-topology-map\s*\{[\s\S]*?height: 360px;[\s\S]*?max-height: 360px;/);
});
