import assert from "node:assert/strict";
import test from "node:test";

import { formatDatabaseVersion } from "../src/DatabaseVersion.js";

test("keeps concise MySQL versions and removes package suffixes", () => {
  assert.equal(formatDatabaseVersion("8.0.44", "MySQL"), "8.0.44");
  assert.equal(formatDatabaseVersion("8.0.44-0ubuntu0.22.04.1", "MySQL"), "8.0.44");
});

test("uses the Oracle release update without redundant zero groups", () => {
  assert.equal(formatDatabaseVersion("19.3.0.0.0", "Oracle"), "19.3");
  assert.equal(formatDatabaseVersion("19.0.0.0.0", "Oracle"), "19.0");
  assert.equal(formatDatabaseVersion("19c", "Oracle"), "19.0");
  assert.equal(formatDatabaseVersion("11.2.0.4.0", "Oracle"), "11.2.0.4");
});

test("extracts PostgreSQL and SQL Server versions from verbose banners", () => {
  assert.equal(
    formatDatabaseVersion("PostgreSQL 15.4 on x86_64-pc-linux-gnu, compiled by gcc 11.3.1, 64-bit", "PostgreSQL"),
    "15.4",
  );
  assert.equal(
    formatDatabaseVersion("Microsoft SQL Server 2022 (RTM-CU15) - 16.0.4155.4 (X64)", "SQLServer"),
    "16.0.4155.4",
  );
});

test("preserves empty and unrecognized values safely", () => {
  assert.equal(formatDatabaseVersion(null, "PostgreSQL"), "");
  assert.equal(formatDatabaseVersion("待采集", "Oracle"), "待采集");
});
