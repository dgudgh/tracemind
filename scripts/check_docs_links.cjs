const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
// Directories scanned for markdown links.
// NOTE: `work/` used to be listed here. On 2026-09-20 the historical docs moved into
// docs/archive/, which is already inside `docs`, so the entry was replaced rather than kept
// as a dead path. The repository root README and agent/README are scanned too -- they cite
// docs by relative path and used to be outside this gate's coverage entirely.
const contentRoots = [
  path.join(root, "docs"),
  path.join(root, "agent"),
];
// Individual files outside those roots.
const contentFiles = [
  path.join(root, "README.md"),
  path.join(root, "backend/scripts/README.md"),
];
const markdownLink = /\[[^\]]+\]\(([^)]+)\)/g;
const staleReferences = [
  "mysql_backup_recovery",
  "backend/app/api/v1/example.py",
  "backend/app/models/example.py",
  "backend/app/schemas/example.py",
  "backend/app/services/example.py",
  "fix_encoding.py",
  "DummyInstance",
  "build_plan_from_engine",
];

function listMarkdownFiles(dir) {
  if (!fs.existsSync(dir)) return [];
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) return listMarkdownFiles(full);
    return entry.name.endsWith(".md") ? [full] : [];
  });
}

function normalizeTarget(rawTarget) {
  let target = rawTarget.trim().replace(/^<|>$/g, "");
  if (!target || /^(https?:|mailto:|#)/i.test(target)) return null;
  target = target.split("#")[0];
  if (!target || target.includes("{")) return null;
  return decodeURIComponent(target);
}

// Heading anchors, approximated the way GitHub generates them.
function slugify(text) {
  let s = text.replace(/`/g, "");
  s = s.replace(/\[([^\]]*)\]\([^)]*\)/g, "$1");
  s = s.trim().toLowerCase();
  s = s.replace(/[^0-9a-z_\u4e00-\u9fff\- ]/g, "");
  s = s.replace(/ /g, "-");
  return s.normalize("NFKC");
}

const headingCache = new Map();
function headingsOf(file) {
  if (headingCache.has(file)) return headingCache.get(file);
  const set = new Set();
  const re = /^#{1,6}\s+(.*?)\s*$/gm;
  const text = fs.readFileSync(file, "utf8");
  let m;
  while ((m = re.exec(text))) set.add(slugify(m[1]));
  headingCache.set(file, set);
  return set;
}

const missing = [];
const brokenAnchors = [];
const stale = [];
const files = [...contentRoots.flatMap(listMarkdownFiles), ...contentFiles.filter((f) => fs.existsSync(f))];
for (const file of files) {
  const text = fs.readFileSync(file, "utf8");
  let match;
  while ((match = markdownLink.exec(text))) {
    const raw = match[1];
    const target = normalizeTarget(raw);
    if (!target) continue;
    const full = path.resolve(path.dirname(file), target);
    if (!fs.existsSync(full)) {
      missing.push({
        file: path.relative(root, file),
        target: raw,
      });
      continue;
    }
    // A "#section" that does not exist is a broken navigation just as much as a missing file.
    const hash = raw.indexOf("#");
    if (hash !== -1 && full.endsWith(".md") && fs.statSync(full).isFile()) {
      const anchor = raw.slice(hash + 1);
      if (anchor && !headingsOf(full).has(slugify(anchor))) {
        brokenAnchors.push({
          file: path.relative(root, file),
          target: raw,
        });
      }
    }
  }
  for (const token of staleReferences) {
    const index = text.indexOf(token);
    if (index !== -1) {
      stale.push({
        file: path.relative(root, file),
        token,
        line: text.slice(0, index).split(/\r?\n/).length,
      });
    }
  }
}

const report = {
  checkedFileCount: files.length,
  missingLinkCount: missing.length,
  missing,
  brokenAnchorCount: brokenAnchors.length,
  brokenAnchors,
  staleReferenceCount: stale.length,
  stale,
};
console.log(JSON.stringify(report, null, 2));

if (missing.length || brokenAnchors.length || stale.length) process.exitCode = 1;
