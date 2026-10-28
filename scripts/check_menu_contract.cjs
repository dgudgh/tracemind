const fs = require("fs");
const path = require("path");
const { createRequire } = require("module");

const repoRoot = path.resolve(__dirname, "..");
const frontendRoot = path.join(repoRoot, "frontend");
const frontendRequire = createRequire(path.join(frontendRoot, "package.json"));
const parser = frontendRequire("@babel/parser");
const sourceFile = path.join(frontendRoot, "src", "menuConfig.jsx");
const baselineFile = path.join(repoRoot, "contracts", "baselines", "frontend-menu-v1.json");

function parseSource() {
  return parser.parse(fs.readFileSync(sourceFile, "utf8"), {
    sourceType: "module",
    plugins: ["jsx"],
  });
}

function variableInitializer(program, variableName) {
  for (const statement of program.body) {
    const declaration = statement.type === "ExportNamedDeclaration" ? statement.declaration : statement;
    if (declaration?.type !== "VariableDeclaration") continue;
    for (const item of declaration.declarations) {
      if (item.id?.type === "Identifier" && item.id.name === variableName) return item.init;
    }
  }
  throw new Error(`Variable ${variableName} was not found in ${sourceFile}`);
}

function propertyName(property) {
  if (property.key?.type === "Identifier" && !property.computed) return property.key.name;
  if (property.key?.type === "StringLiteral") return property.key.value;
  return null;
}

function literalValue(node) {
  if (!node) return null;
  if (["StringLiteral", "NumericLiteral", "BooleanLiteral"].includes(node.type)) return node.value;
  if (node.type === "NullLiteral") return null;
  if (node.type === "ArrayExpression") return node.elements.map(literalValue);
  throw new Error(`Unsupported contract literal: ${node.type}`);
}

function objectProperty(node, name) {
  return node.properties.find((property) => property.type === "ObjectProperty" && propertyName(property) === name);
}

function collectEntries(arrayNode, parentKey = null, depth = 0, entries = []) {
  if (arrayNode?.type !== "ArrayExpression") throw new Error("MENU_CONFIG must be an array literal");
  for (const element of arrayNode.elements) {
    if (element?.type !== "ObjectExpression") throw new Error("Every menu item must be an object literal");
    const key = literalValue(objectProperty(element, "key")?.value);
    const label = literalValue(objectProperty(element, "label")?.value);
    if (!key || !label) throw new Error("Every menu item must have literal key and label values");
    const children = objectProperty(element, "children")?.value;
    const entry = {
      key,
      label,
      parent_key: parentKey,
      depth,
      data: literalValue(objectProperty(element, "data")?.value),
      db_types: literalValue(objectProperty(element, "dbTypes")?.value) || [],
      engines: literalValue(objectProperty(element, "engines")?.value) || [],
      child_keys: children?.elements?.map((child) => literalValue(objectProperty(child, "key")?.value)) || [],
    };
    entries.push(entry);
    if (children) collectEntries(children, key, depth + 1, entries);
  }
  return entries;
}

function stringMap(node) {
  if (node?.type !== "ObjectExpression") throw new Error("LEGACY_KEY_MAP must be an object literal");
  return Object.fromEntries(node.properties.map((property) => [propertyName(property), literalValue(property.value)]));
}

function buildMenuContract() {
  const ast = parseSource();
  const entries = collectEntries(variableInitializer(ast.program, "MENU_CONFIG"));
  const keys = entries.map((entry) => entry.key);
  const duplicates = keys.filter((key, index) => keys.indexOf(key) !== index);
  if (duplicates.length) throw new Error(`Duplicate menu keys: ${[...new Set(duplicates)].join(", ")}`);

  const legacyKeyMap = stringMap(variableInitializer(ast.program, "LEGACY_KEY_MAP"));
  const missingTargets = [...new Set(Object.values(legacyKeyMap).filter((key) => !keys.includes(key)))];
  if (missingTargets.length) throw new Error(`Legacy menu targets are missing: ${missingTargets.join(", ")}`);

  const defaultActiveKey = literalValue(variableInitializer(ast.program, "DEFAULT_ACTIVE_KEY"));
  if (!keys.includes(defaultActiveKey)) throw new Error(`Default menu key does not exist: ${defaultActiveKey}`);
  return {
    format_version: 1,
    default_active_key: defaultActiveKey,
    default_open_keys: literalValue(variableInitializer(ast.program, "DEFAULT_OPEN_KEYS")),
    entries,
    legacy_key_map: legacyKeyMap,
  };
}

function compareContracts(baseline, current) {
  const baselineEntries = new Map(baseline.entries.map((entry) => [entry.key, entry]));
  const currentEntries = new Map(current.entries.map((entry) => [entry.key, entry]));
  const shared = [...baselineEntries.keys()].filter((key) => currentEntries.has(key));
  return {
    added_keys: [...currentEntries.keys()].filter((key) => !baselineEntries.has(key)),
    removed_keys: [...baselineEntries.keys()].filter((key) => !currentEntries.has(key)),
    changed_keys: shared.filter(
      (key) => JSON.stringify(baselineEntries.get(key)) !== JSON.stringify(currentEntries.get(key)),
    ),
    default_changed: baseline.default_active_key !== current.default_active_key,
    legacy_map_changed: JSON.stringify(baseline.legacy_key_map) !== JSON.stringify(current.legacy_key_map),
    document_changed: JSON.stringify(baseline) !== JSON.stringify(current),
  };
}

function main() {
  const update = process.argv.includes("--update");
  const current = buildMenuContract();
  if (update) {
    fs.mkdirSync(path.dirname(baselineFile), { recursive: true });
    fs.writeFileSync(baselineFile, `${JSON.stringify(current, null, 2)}\n`, "utf8");
    console.log(JSON.stringify({ updated: true, menu_entries: current.entries.length }));
    return 0;
  }
  if (!fs.existsSync(baselineFile)) {
    console.error(JSON.stringify({ ok: false, missing_baseline: path.relative(repoRoot, baselineFile) }, null, 2));
    return 1;
  }
  const baseline = JSON.parse(fs.readFileSync(baselineFile, "utf8"));
  const diff = compareContracts(baseline, current);
  console.log(JSON.stringify({ ok: !diff.document_changed, ...diff }, null, 2));
  if (diff.document_changed) {
    console.error("Menu contract drift detected. Review it, then use --update for an intentional change.");
  }
  return diff.document_changed ? 1 : 0;
}

module.exports = { buildMenuContract, compareContracts };

if (require.main === module) process.exitCode = main();
