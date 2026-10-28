import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";


const page = fs.readFileSync(new URL("../src/DataGovernancePage.jsx", import.meta.url), "utf8");
const panel = fs.readFileSync(new URL("../src/DataGovernanceExecutionPanel.jsx", import.meta.url), "utf8");
const api = fs.readFileSync(new URL("../src/modules/dataGovernanceApi.js", import.meta.url), "utf8");


test("data governance separates planning from real execution", () => {
  assert.match(page, /方案与执行分层/);
  assert.match(page, /key: "execution", label: "执行与验证"/);
  assert.match(page, /生产源表不会自动清理/);
  assert.match(panel, /源表清理始终关闭/);
});


test("execution UI exposes precheck, approval, encrypted export, and recovery", () => {
  assert.match(panel, /只读预检/);
  assert.match(panel, /批准/);
  assert.match(panel, /启动加密归档/);
  assert.match(panel, /AES-256-GCM/);
  assert.match(panel, /抽样恢复/);
  assert.match(panel, /恢复演练/);
});


test("legal hold is visible and centered", () => {
  assert.match(panel, /设置法律保留/);
  assert.match(panel, /删除申请会被后端强制拒绝/);
  assert.match(panel, /\n      centered\n/);
  assert.match(panel, /calc\(100vw - 32px\)/);
});


test("frontend API covers the complete archive execution lifecycle", () => {
  for (const contract of [
    "precheckArchiveJob",
    "setArchiveJobStatus",
    "executeArchiveJob",
    "runArchiveRecoveryTest",
    "createArchiveLegalHold",
    "releaseArchiveLegalHold",
  ]) {
    assert.match(api, new RegExp(contract));
  }
});

function loadApi(transport) {
  const names = [...api.matchAll(/export (?:async )?(?:const|function) (\w+)/g)].map((match) => match[1]);
  const source = api.replace(/^import .*;\n/gm, "").replace(/\bexport /g, "");
  const query = (values) => {
    const pairs = Object.entries(values).filter(([, value]) => value != null);
    return pairs.length ? `?${new URLSearchParams(pairs)}` : "";
  };
  return new Function("api", "buildQuery", `${source}\nreturn { ${names.join(", ")} };`)(transport, query);
}

function approvalFixture() {
  const job = { id: 71, instance_id: 101, status: "approved" };
  const precheck = {
    id: 91, job_id: 71, instance_id: 101, execution_kind: "precheck", status: "passed",
    manifest: { row_limit: 1000, approval: {
      precheck_execution_id: 91, approval_id: "approval-1", approved_by: "reviewer",
      approved_at: "2026-09-08T01:00:00", evidence_sha256: "server-verified-digest",
    } },
  };
  return { job, precheck };
}

test("execution evidence uses only selected job and its artifact IDs", async () => {
  const calls = [];
  const service = loadApi({ get: async (path) => {
    calls.push(path);
    if (path === "/data-governance/executions?job_id=71") return { items: [
      { id: 1, job_id: 71, artifact_id: 111 }, { id: 2, job_id: 71, artifact_id: 111 },
      { id: 3, job_id: 72, artifact_id: 999 }, { id: 4, job_id: 71, artifact_id: null },
    ] };
    assert.ok(path.endsWith("?artifact_id=111"), path);
    return { items: [{ id: 1, artifact_id: 111 }] };
  } });
  const result = await service.getArchiveJobEvidence(71);
  assert.deepEqual(result.executions.map((row) => row.id), [4, 2, 1]);
  assert.deepEqual(calls, ["/data-governance/executions?job_id=71", "/data-governance/recovery-tests?artifact_id=111", "/data-governance/legal-holds?artifact_id=111"]);
  assert.equal(result.recoveries.status, "ready");
  assert.equal(result.holds.status, "ready");
});

test("a job without artifacts makes no global artifact queries", async () => {
  const calls = [];
  const service = loadApi({ get: async (path) => { calls.push(path); return { items: [] }; } });
  const result = await service.getArchiveJobEvidence(71);
  assert.deepEqual(calls, ["/data-governance/executions?job_id=71"]);
  assert.deepEqual(result.holds, { status: "ready", items: [] });
  for (const value of [null, undefined, "", 0, -1]) {
    await assert.rejects(service.getArchiveJobEvidence(value));
    assert.throws(() => service.getGovernanceBundle(value));
  }
  assert.equal(calls.length, 1);
});

test("optional forbidden globals are unavailable, not successful zero totals", async () => {
  const service = loadApi({ get: async () => { throw Object.assign(new Error("scope denied"), { status: 403 }); } });
  for (const read of [service.getOptionalArchiveExecutionSummary, service.getOptionalArchiveStorageConfigs]) {
    assert.deepEqual(await read(), { status: "forbidden", error: "scope denied" });
  }
  const failed = loadApi({ get: async () => { throw Object.assign(new Error("offline"), { status: 500 }); } });
  assert.deepEqual(await failed.getOptionalArchiveExecutionSummary(), { status: "error", error: "offline" });
  const unavailable = loadApi({ get: async () => null });
  assert.equal((await unavailable.getOptionalArchiveStorageConfigs()).status, "error");
  const ready = loadApi({ get: async () => ({ executions: 0 }) });
  assert.deepEqual(await ready.getOptionalArchiveExecutionSummary(), { status: "ready", data: { executions: 0 } });
});

test("artifact history failure stays explicit while job executions remain available", async () => {
  const service = loadApi({ get: async (path) => {
    if (path.includes("/executions?")) return { items: [{ id: 91, job_id: 71, artifact_id: 111 }] };
    if (path.includes("/legal-holds?")) throw new Error("hold history unavailable");
    return { items: [] };
  } });
  const result = await service.getArchiveJobEvidence(71);
  assert.equal(result.executions.length, 1);
  assert.deepEqual(result.holds, { status: "error", error: "hold history unavailable" });
  assert.equal(result.holds.items, undefined);
  assert.equal(result.recoveries.status, "ready");
});

test("license and authentication failures are not optional scope denials", async () => {
  for (const error of [
    { status: 403, licenseBlocked: true, message: "license expired" },
    Object.assign(new Error("session expired"), { status: 401 }),
  ]) {
    const service = loadApi({ get: async () => { throw error; } });
    const result = await service.getOptionalArchiveStorageConfigs();
    assert.equal(result.status, "error");
    assert.equal(result.error, error.message);
    assert.equal(result.data, undefined);
  }
});

test("approval guard is synchronous and does not require WebCrypto on HTTP LAN", () => {
  const service = loadApi({});
  const { job, precheck } = approvalFixture();
  assert.equal(service.archivePrecheckApproved(job, precheck, 1000), true);
  assert.doesNotMatch(api + panel, /crypto\.subtle|evidenceJson|TextEncoder|archiveEvidenceDigest/);
  assert.equal(service.archivePrecheckApproved(job, precheck, 1001), false);
  assert.equal(service.archivePrecheckApproved({ ...job, status: "plan_only" }, precheck, 1000), false);
  assert.equal(service.archivePrecheckApproved({ ...job, id: 72 }, precheck, 1000), false);
  assert.equal(service.archivePrecheckApproved(job, { ...precheck, manifest: { row_limit: 1000 } }, 1000), false);
  precheck.manifest.approval.precheck_execution_id = 90;
  assert.equal(service.archivePrecheckApproved(job, precheck, 1000), false);
});

test("newer failed or unapproved precheck cannot reuse an earlier approval", () => {
  const service = loadApi({});
  const { job, precheck } = approvalFixture();
  const newer = { ...precheck, id: 92, status: "failed" };
  const state = service.archivePrecheckState(job, [precheck, newer], 1000);
  assert.equal(state.latestPrecheck.id, 92);
  assert.equal(state.matches, false);
  newer.status = "passed";
  assert.equal(service.archivePrecheckApproved(job, newer, 1000), false);
});

test("approval and execution requests preserve exact reviewed precheck and row limit", async () => {
  const calls = [];
  const service = loadApi({ post: async (...args) => { calls.push(args); return {}; } });
  await service.setArchiveJobStatus(71, "approved", 91);
  await service.executeArchiveJob(71, { precheck_execution_id: 91, row_limit: 1000, storage_id: null, encrypt: true });
  assert.deepEqual(calls[0], ["/data-governance/jobs/71/status", { status: "approved", precheck_execution_id: 91 }]);
  assert.equal(calls[1][1].row_limit, 1000);
  assert.equal(calls[1][1].precheck_execution_id, 91);
});

test("job selection and mutation callbacks reject stale request generations", () => {
  assert.match(panel, /requestId === evidenceRequest\.current && selectedJobId === currentJobId\.current/);
  assert.match(panel, /selectionVersion\.current === version/);
  assert.match(panel, /evidence\?\.jobId === selectedJobId/);
  assert.match(panel, /if \(!canExecute\) return/);
  assert.match(panel, /disabled=\{!canExecute\}/);
  assert.match(panel, /getArchiveJobEvidence\(selectedJobId\)/);
  assert.doesNotMatch(panel, /listArchiveExecutions\(\)|listArchiveRecoveryTests\(\)|listArchiveLegalHolds\(\)/);
  assert.match(panel, /summaryAccess\.status === "ready" \?/);
});

test("page scopes instance data and gates the global object storage panel", () => {
  assert.match(page, /instanceId !== currentInstanceId\.current/);
  assert.match(page, /requestId === bundleRequest\.current/);
  assert.match(page, /dataInstanceId === instanceId \? jobs : \[\]/);
  assert.match(page, /key=\{instanceId \|\| "no-instance"\}/);
  assert.match(page, /storageAccess\.status === "ready"\s*\? <DataGovernanceObjectStoragePanel/);
  assert.match(page, /return selectedInstanceId \? requestedSection : "objectStorage"/);
});
