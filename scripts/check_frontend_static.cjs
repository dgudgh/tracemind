const fs = require("fs");
const path = require("path");
const { createRequire } = require("module");

const repoRoot = path.resolve(__dirname, "..");
const frontendRoot = path.join(repoRoot, "frontend");
const frontendRequire = createRequire(path.join(frontendRoot, "package.json"));
const parser = frontendRequire("@babel/parser");
const traverse = frontendRequire("@babel/traverse").default;

const sourceRoot = path.join(frontendRoot, "src");

// 2026-10-23: the customer-requirement entry now uses unified server dispatch.
// These retired browser paths remain temporarily because they contain pre-existing work
// and historical regression coverage. See docs/blueprint/customer-requirement-entry-2026-10-23.md.
// Keep this baseline exact and visible: new findings fail, and removed findings must also
// be removed here instead of leaving a stale exception behind.
const knownLegacyOrphanSourceFiles = new Set([
  "src/modules/assistantInspection.js",
  "src/modules/reportDocumentApi.js",
  "src/modules/reportGeneration.js",
]);
const knownLegacyUnusedNamedExports = new Set([
  "src/modules/assistantActions.js#resolveAssistantActionRequest",
  "src/modules/assistantActions.js#runAssistantActionProgress",
  "src/modules/assistantActions.js#executeAssistantActionRequest",
  "src/modules/assistantInspection.js#resolveAssistantInspectionCommand",
  "src/modules/assistantInspection.js#selectAssistantInspectionTargets",
  "src/modules/assistantInspection.js#attachDefaultInspectionPolicies",
  "src/modules/assistantInspection.js#runAssistantInspectionSequence",
  "src/modules/diagnosisCenterApi.js#runDiagnosisCenter",
  "src/modules/reportDocumentApi.js#generateReportDocuments",
  "src/modules/reportGeneration.js#isReportGenerationCommand",
]);
const globals = new Set([
  "Array",
  "Blob",
  "Boolean",
  "Date",
  "DOMParser",
  "Error",
  "Event",
  "File",
  "FileReader",
  "FormData",
  "Infinity",
  "Intl",
  "JSON",
  "Map",
  "Math",
  "Number",
  "Object",
  "Promise",
  "React",
  "RegExp",
  "Set",
  "String",
  "URL",
  "URLSearchParams",
  "WeakMap",
  "WeakSet",
  "clearInterval",
  "clearTimeout",
  "console",
  "decodeURIComponent",
  "document",
  "encodeURIComponent",
  "fetch",
  "isNaN",
  "localStorage",
  "location",
  "navigator",
  "parseFloat",
  "parseInt",
  "sessionStorage",
  "setInterval",
  "setTimeout",
  "undefined",
  "window",
]);

function listFiles(dir) {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) return listFiles(full);
    return /\.(jsx|js|tsx|ts)$/.test(entry.name) ? [full] : [];
  });
}

function parseSource(file) {
  const source = fs.readFileSync(file, "utf8");
  return parser.parse(source, {
    sourceType: "module",
    plugins: ["jsx", "typescript"],
  });
}

function fileExists(file) {
  try {
    return fs.statSync(file).isFile();
  } catch {
    return false;
  }
}

function resolveLocalSource(fromFile, sourceValue) {
  if (!sourceValue?.startsWith(".")) return null;
  const base = path.resolve(path.dirname(fromFile), sourceValue);
  return [
    base,
    `${base}.js`,
    `${base}.jsx`,
    `${base}.ts`,
    `${base}.tsx`,
    path.join(base, "index.js"),
    path.join(base, "index.jsx"),
    path.join(base, "index.ts"),
    path.join(base, "index.tsx"),
  ].find(fileExists) || null;
}

function addMissing(map, name, line) {
  if (!map.has(name)) map.set(name, new Set());
  map.get(name).add(line || "?");
}

function checkMissingBindings(files) {
  const reports = [];
  for (const file of files) {
    let ast;
    try {
      ast = parseSource(file);
    } catch (error) {
      reports.push({
        file: path.relative(frontendRoot, file),
        parseError: `${error.message} @ ${error.loc?.line}:${error.loc?.column}`,
      });
      continue;
    }

    const missing = new Map();
    traverse(ast, {
      JSXOpeningElement(pathRef) {
        const node = pathRef.node.name;
        if (node.type !== "JSXIdentifier") return;
        const name = node.name;
        if (/^[A-Z]/.test(name) && !pathRef.scope.hasBinding(name) && !globals.has(name)) {
          addMissing(missing, name, node.loc?.start?.line);
        }
      },
      ReferencedIdentifier(pathRef) {
        const name = pathRef.node.name;
        if (!pathRef.scope.hasBinding(name) && !globals.has(name)) {
          addMissing(missing, name, pathRef.node.loc?.start?.line);
        }
      },
    });

    if (missing.size) {
      reports.push({
        file: path.relative(frontendRoot, file),
        missing: Object.fromEntries(
          [...missing]
            .sort()
            .map(([key, value]) => [key, [...value].sort((a, b) => a - b)]),
        ),
      });
    }
  }
  return reports;
}

function checkOrphanSourceFiles(files) {
  const entryFiles = new Set([path.join(sourceRoot, "main.jsx")]);
  const fileSet = new Set(files);
  const referenced = new Map(files.map((file) => [file, new Set()]));
  const reports = [];

  for (const file of files) {
    let ast;
    try {
      ast = parseSource(file);
    } catch (error) {
      reports.push({ file: path.relative(frontendRoot, file), parseError: error.message });
      continue;
    }

    const markReference = (sourceValue) => {
      const resolved = resolveLocalSource(file, sourceValue);
      if (resolved && fileSet.has(resolved)) {
        referenced.get(resolved)?.add(file);
      }
    };

    traverse(ast, {
      ImportDeclaration(pathRef) {
        markReference(pathRef.node.source.value);
      },
      ExportNamedDeclaration(pathRef) {
        if (pathRef.node.source?.value) markReference(pathRef.node.source.value);
      },
      ExportAllDeclaration(pathRef) {
        if (pathRef.node.source?.value) markReference(pathRef.node.source.value);
      },
    });
  }

  for (const file of files) {
    if (entryFiles.has(file)) continue;
    if ((referenced.get(file)?.size || 0) === 0) {
      reports.push({
        file: path.relative(frontendRoot, file),
        message: "source file is not reachable from any local import/export entry",
      });
    }
  }

  return reports;
}

function checkUnusedImports(files) {
  const reports = [];
  for (const file of files) {
    let ast;
    try {
      ast = parseSource(file);
    } catch (error) {
      reports.push({ file: path.relative(frontendRoot, file), parseError: error.message });
      continue;
    }

    const imported = new Map();
    const used = new Set();
    traverse(ast, {
      ImportSpecifier(pathRef) {
        imported.set(pathRef.node.local.name, pathRef.node.loc.start.line);
      },
      ImportDefaultSpecifier(pathRef) {
        imported.set(pathRef.node.local.name, pathRef.node.loc.start.line);
      },
      ImportNamespaceSpecifier(pathRef) {
        imported.set(pathRef.node.local.name, pathRef.node.loc.start.line);
      },
      JSXIdentifier(pathRef) {
        used.add(pathRef.node.name);
      },
      ReferencedIdentifier(pathRef) {
        used.add(pathRef.node.name);
      },
    });

    const unused = [...imported]
      .filter(([name]) => !used.has(name) && name !== "React")
      .map(([name, line]) => ({ name, line }));
    if (unused.length) reports.push({ file: path.relative(frontendRoot, file), unused });
  }
  return reports;
}

function checkUnusedLocalFunctions(files) {
  const reports = [];
  for (const file of files) {
    let ast;
    try {
      ast = parseSource(file);
    } catch (error) {
      reports.push({ file: path.relative(frontendRoot, file), parseError: error.message });
      continue;
    }

    const unused = [];
    traverse(ast, {
      Program(programPath) {
        const declared = new Map();
        const exported = new Set();
        for (const node of programPath.node.body) {
          if (node.type === "FunctionDeclaration" && node.id?.name) {
            declared.set(node.id.name, node.id.loc?.start?.line);
          }
          if (node.type === "VariableDeclaration") {
            for (const declaration of node.declarations || []) {
              if (
                declaration.id?.type === "Identifier" &&
                (declaration.init?.type === "ArrowFunctionExpression" || declaration.init?.type === "FunctionExpression")
              ) {
                declared.set(declaration.id.name, declaration.id.loc?.start?.line);
              }
            }
          }
          if (node.type === "ExportNamedDeclaration") {
            if (node.declaration?.type === "FunctionDeclaration" && node.declaration.id?.name) {
              exported.add(node.declaration.id.name);
            }
            if (node.declaration?.type === "VariableDeclaration") {
              for (const declaration of node.declaration.declarations || []) {
                if (declaration.id?.type === "Identifier") exported.add(declaration.id.name);
              }
            }
            for (const specifier of node.specifiers || []) {
              if (specifier.local?.name) exported.add(specifier.local.name);
            }
          }
          if (node.type === "ExportDefaultDeclaration" && node.declaration?.id?.name) {
            exported.add(node.declaration.id.name);
          }
        }

        for (const [name, line] of declared) {
          const binding = programPath.scope.getBinding(name);
          if (!exported.has(name) && binding && !binding.referenced) {
            unused.push({ name, line });
          }
        }
      },
    });

    if (unused.length) reports.push({ file: path.relative(frontendRoot, file), unused });
  }
  return reports;
}

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function checkUnusedNamedExports(files) {
  const exportsByName = [];
  const sources = new Map(files.map((file) => [file, fs.readFileSync(file, "utf8")]));

  for (const file of files) {
    let ast;
    try {
      ast = parseSource(file);
    } catch (error) {
      exportsByName.push({ file: path.relative(frontendRoot, file), parseError: error.message });
      continue;
    }

    traverse(ast, {
      ExportNamedDeclaration(pathRef) {
        const { declaration, specifiers } = pathRef.node;
        if (declaration?.type === "FunctionDeclaration" && declaration.id?.name) {
          exportsByName.push({
            file,
            name: declaration.id.name,
            line: declaration.id.loc?.start?.line || "?",
          });
        }
        if (declaration?.type === "VariableDeclaration") {
          for (const item of declaration.declarations || []) {
            if (item.id?.type === "Identifier") {
              exportsByName.push({
                file,
                name: item.id.name,
                line: item.id.loc?.start?.line || "?",
              });
            }
          }
        }
        for (const specifier of specifiers || []) {
          if (specifier.exported?.type === "Identifier") {
            exportsByName.push({
              file,
              name: specifier.exported.name,
              line: specifier.exported.loc?.start?.line || "?",
            });
          }
        }
      },
    });
  }

  const reports = [];
  for (const item of exportsByName) {
    if (item.parseError) {
      reports.push(item);
      continue;
    }

    let usageCount = 0;
    for (const source of sources.values()) {
      const matches = source.match(new RegExp(`\\b${escapeRegExp(item.name)}\\b`, "g"));
      usageCount += matches?.length || 0;
    }
    if (usageCount <= 1) {
      reports.push({
        file: path.relative(frontendRoot, item.file),
        name: item.name,
        line: item.line,
        usageCount,
      });
    }
  }

  return reports;
}

function checkSuspiciousText(files) {
  const patterns = [
    { name: "replacement-character", regex: /\uFFFD/g },
    { name: "mojibake", regex: /\u951F/g },
    { name: "common-mojibake", regex: /[\u9427\u93ba\u9422\u7487\u95bf\u935a\u7035\u6ae5\u7d8d\u7e43\u93c8\u71c2\u7d1d\u5678\u93c2\u626e\u8930\u7e3a\u5a32]/g },
    { name: "question-placeholder", regex: /\?{4,}/g },
  ];
  const reports = [];
  for (const file of files) {
    const source = fs.readFileSync(file, "utf8");
    const lines = source.split(/\r?\n/);
    const hits = [];
    for (const pattern of patterns) {
      lines.forEach((line, index) => {
        if (pattern.regex.test(line)) {
          hits.push({
            pattern: pattern.name,
            line: index + 1,
            text: line.trim().slice(0, 160),
          });
        }
        pattern.regex.lastIndex = 0;
      });
    }
    if (hits.length) reports.push({ file: path.relative(frontendRoot, file), hits });
  }
  return reports;
}

function checkDirectTransportUsage(files) {
  const reports = [];
  const allowedFile = path.join(sourceRoot, "api.js");
  for (const file of files) {
    if (file === allowedFile) continue;
    let ast;
    try {
      ast = parseSource(file);
    } catch (error) {
      reports.push({ file: path.relative(frontendRoot, file), parseError: error.message });
      continue;
    }

    const hits = [];
    traverse(ast, {
      CallExpression(pathRef) {
        const callee = pathRef.node.callee;
        if (callee.type === "Identifier" && callee.name === "fetch") {
          hits.push({ kind: "fetch", line: callee.loc?.start?.line || "?" });
        }
        if (
          callee.type === "MemberExpression" &&
          callee.object.type === "Identifier" &&
          callee.object.name === "api" &&
          callee.property.type === "Identifier" &&
          callee.property.name === "raw"
        ) {
          hits.push({ kind: "api.raw", line: callee.loc?.start?.line || "?" });
        }
      },
    });

    if (hits.length) reports.push({ file: path.relative(frontendRoot, file), hits });
  }
  return reports;
}

function checkApiBoundaryUsage(files) {
  const reports = [];
  const allowedFile = path.join(sourceRoot, "api.js");
  const authStorageFile = path.join(sourceRoot, "modules", "authStorage.js");
  const apiMethods = new Set(["get", "post", "postForm", "put", "delete", "download", "downloadFile"]);
  for (const file of files) {
    if (file === allowedFile) continue;
    let ast;
    try {
      ast = parseSource(file);
    } catch (error) {
      reports.push({ file: path.relative(frontendRoot, file), parseError: error.message });
      continue;
    }

    const hits = [];
    traverse(ast, {
      CallExpression(pathRef) {
        const callee = pathRef.node.callee;
        if (
          callee.type === "MemberExpression" &&
          callee.object.type === "Identifier" &&
          callee.object.name === "localStorage" &&
          callee.property.type === "Identifier" &&
          callee.property.name === "getItem" &&
            file !== authStorageFile &&
            pathRef.node.arguments[0]?.type === "StringLiteral" &&
          pathRef.node.arguments[0].value === "mesguard_token"
        ) {
          hits.push({ kind: "token-read", line: callee.loc?.start?.line || "?" });
        }
        if (
          callee.type === "MemberExpression" &&
          callee.object.type === "Identifier" &&
          callee.object.name === "api" &&
          callee.property.type === "Identifier" &&
          apiMethods.has(callee.property.name)
        ) {
          const firstArg = pathRef.node.arguments[0];
          const startsWithApiPrefix =
            (firstArg?.type === "StringLiteral" && firstArg.value.startsWith("/api/v1")) ||
            (firstArg?.type === "TemplateLiteral" && firstArg.quasis[0]?.value?.cooked?.startsWith("/api/v1"));
          if (startsWithApiPrefix) {
            hits.push({ kind: "api-v1-prefix", method: callee.property.name, line: callee.loc?.start?.line || "?" });
          }
        }
      },
      ObjectProperty(pathRef) {
        const key = pathRef.node.key;
        if (
          (key.type === "Identifier" && key.name.toLowerCase() === "authorization") ||
          (key.type === "StringLiteral" && key.value.toLowerCase() === "authorization")
        ) {
          hits.push({ kind: "authorization-header", line: key.loc?.start?.line || "?" });
        }
      },
      StringLiteral(pathRef) {
        if (pathRef.node.value.includes("Bearer ")) {
          hits.push({ kind: "bearer-string", line: pathRef.node.loc?.start?.line || "?" });
        }
      },
      TemplateElement(pathRef) {
        if (pathRef.node.value.cooked?.includes("Bearer ")) {
          hits.push({ kind: "bearer-string", line: pathRef.node.loc?.start?.line || "?" });
        }
      },
    });

    if (hits.length) reports.push({ file: path.relative(frontendRoot, file), hits });
  }
  return reports;
}

function checkApiTransportImportBoundary(files) {
  const reports = [];
  const apiFile = path.join(sourceRoot, "api.js");
  const apiClientsFile = path.join(sourceRoot, "apiClients.js");
  const apiModuleDir = path.join(sourceRoot, "modules");
  const isTransportModule = (file) =>
    file === apiClientsFile || (path.dirname(file) === apiModuleDir && path.basename(file).endsWith("Api.js"));

  for (const file of files) {
    let ast;
    try {
      ast = parseSource(file);
    } catch (error) {
      reports.push({ file: path.relative(frontendRoot, file), parseError: error.message });
      continue;
    }

    const hits = [];
    traverse(ast, {
      ImportDeclaration(pathRef) {
        if (resolveLocalSource(file, pathRef.node.source.value) !== apiFile) return;
        const importedTransport = pathRef.node.specifiers
          .map((specifier) => {
            if (specifier.type === "ImportSpecifier") return specifier.imported.name || specifier.imported.value;
            if (specifier.type === "ImportDefaultSpecifier") return "default";
            if (specifier.type === "ImportNamespaceSpecifier") return "*";
            return "";
          })
          .filter((name) => name === "api" || name === "buildQuery" || name === "default" || name === "*");
        if (importedTransport.length && !isTransportModule(file)) {
          hits.push({ kind: "api-transport-import", imports: importedTransport, line: pathRef.node.loc?.start?.line || "?" });
        }
      },
    });

    if (hits.length) reports.push({ file: path.relative(frontendRoot, file), hits });
  }
  return reports;
}

function isMemberPath(node, names) {
  if (!node || names.length === 0) return false;
  if (names.length === 1) return node.type === "Identifier" && node.name === names[0];
  if (node.type !== "MemberExpression") return false;
  const property = node.property;
  const propertyName =
    property.type === "Identifier" && !node.computed
      ? property.name
      : property.type === "StringLiteral"
        ? property.value
        : null;
  return propertyName === names[names.length - 1] && isMemberPath(node.object, names.slice(0, -1));
}

function checkBrowserUtilityBoundaryUsage(files) {
  const reports = [];
  const mainFile = path.join(sourceRoot, "main.jsx");
  const apiUrlFile = path.join(sourceRoot, "modules", "apiUrl.js");
  const authEventsFile = path.join(sourceRoot, "modules", "authEvents.js");
  const authStorageFile = path.join(sourceRoot, "modules", "authStorage.js");
  const clipboardFile = path.join(sourceRoot, "modules", "clipboard.js");
  const fileExportFile = path.join(sourceRoot, "modules", "fileExport.js");
  const navigationFile = path.join(sourceRoot, "modules", "navigation.js");
  const downloadAllowed = new Set([fileExportFile]);
  const createElementAllowed = new Set([clipboardFile, fileExportFile]);
  const documentLookupAllowed = new Set([mainFile]);
  const windowNavigationAllowed = new Set([navigationFile]);
  const browserLocationAllowed = new Set([apiUrlFile, navigationFile]);
  const authEventAllowed = new Set([authEventsFile]);
  const browserStorageAllowed = new Set([authStorageFile]);

  for (const file of files) {
    let ast;
    try {
      ast = parseSource(file);
    } catch (error) {
      reports.push({ file: path.relative(frontendRoot, file), parseError: error.message });
      continue;
    }

    const hits = [];
    traverse(ast, {
      CallExpression(pathRef) {
        const callee = pathRef.node.callee;
        if (isMemberPath(callee, ["navigator", "clipboard", "writeText"]) && file !== clipboardFile) {
          hits.push({ kind: "clipboard-write", line: callee.loc?.start?.line || "?" });
        }
        if (isMemberPath(callee, ["URL", "createObjectURL"]) && !downloadAllowed.has(file)) {
          hits.push({ kind: "object-url-create", line: callee.loc?.start?.line || "?" });
        }
        if (isMemberPath(callee, ["URL", "revokeObjectURL"]) && !downloadAllowed.has(file)) {
          hits.push({ kind: "object-url-revoke", line: callee.loc?.start?.line || "?" });
        }
        if (isMemberPath(callee, ["document", "createElement"]) && !createElementAllowed.has(file)) {
          hits.push({ kind: "document-create-element", line: callee.loc?.start?.line || "?" });
        }
        if (isMemberPath(callee, ["document", "getElementById"]) && !documentLookupAllowed.has(file)) {
          hits.push({ kind: "document-get-element-by-id", line: callee.loc?.start?.line || "?" });
        }
        if (isMemberPath(callee, ["document", "execCommand"]) && file !== clipboardFile) {
          hits.push({ kind: "document-exec-command", line: callee.loc?.start?.line || "?" });
        }
        if (isMemberPath(callee, ["window", "location", "reload"]) && !windowNavigationAllowed.has(file)) {
          hits.push({ kind: "window-location-reload", line: callee.loc?.start?.line || "?" });
        }
        if (isMemberPath(callee, ["window", "open"]) && !windowNavigationAllowed.has(file)) {
          hits.push({ kind: "window-open", line: callee.loc?.start?.line || "?" });
        }
        if (isMemberPath(callee, ["window", "history", "replaceState"]) && !windowNavigationAllowed.has(file)) {
          hits.push({ kind: "window-history-replace-state", line: callee.loc?.start?.line || "?" });
        }
        if (
          (isMemberPath(callee, ["localStorage", "getItem"]) ||
            isMemberPath(callee, ["localStorage", "setItem"]) ||
            isMemberPath(callee, ["localStorage", "removeItem"]) ||
            isMemberPath(callee, ["sessionStorage", "getItem"]) ||
            isMemberPath(callee, ["sessionStorage", "setItem"]) ||
            isMemberPath(callee, ["sessionStorage", "removeItem"])) &&
          !browserStorageAllowed.has(file)
        ) {
          hits.push({ kind: "browser-storage-access", line: callee.loc?.start?.line || "?" });
        }
        const firstArg = pathRef.node.arguments[0];
        if (
          ["addEventListener", "removeEventListener"].some((method) => isMemberPath(callee, ["window", method])) &&
          firstArg?.type === "StringLiteral" &&
          firstArg.value === "auth-expired" &&
          !authEventAllowed.has(file)
        ) {
          hits.push({ kind: "auth-event-listener", line: callee.loc?.start?.line || "?" });
        }
        if (isMemberPath(callee, ["window", "dispatchEvent"]) && !authEventAllowed.has(file)) {
          const eventArg = pathRef.node.arguments[0];
          if (
            eventArg?.type === "NewExpression" &&
            eventArg.callee.type === "Identifier" &&
            eventArg.callee.name === "Event" &&
            eventArg.arguments[0]?.type === "StringLiteral" &&
            eventArg.arguments[0].value === "auth-expired"
          ) {
            hits.push({ kind: "auth-event-dispatch", line: callee.loc?.start?.line || "?" });
          }
        }
      },
      MemberExpression(pathRef) {
        if (isMemberPath(pathRef.node, ["window", "location"]) && !browserLocationAllowed.has(file)) {
          hits.push({ kind: "window-location-access", line: pathRef.node.loc?.start?.line || "?" });
        }
        if (isMemberPath(pathRef.node, ["window", "history"]) && file !== navigationFile) {
          hits.push({ kind: "window-history-access", line: pathRef.node.loc?.start?.line || "?" });
        }
      },
      NewExpression(pathRef) {
        if (pathRef.node.callee.type === "Identifier" && pathRef.node.callee.name === "Blob" && !downloadAllowed.has(file)) {
          hits.push({ kind: "blob-create", line: pathRef.node.callee.loc?.start?.line || "?" });
        }
      },
      AssignmentExpression(pathRef) {
        if (isMemberPath(pathRef.node.left, ["window", "location", "hash"]) && !windowNavigationAllowed.has(file)) {
          hits.push({ kind: "window-location-hash", line: pathRef.node.left.loc?.start?.line || "?" });
        }
      },
      StringLiteral(pathRef) {
        if (pathRef.node.value === "auth-expired" && !authEventAllowed.has(file)) {
          hits.push({ kind: "auth-event-name", line: pathRef.node.loc?.start?.line || "?" });
        }
      },
    });

    if (hits.length) reports.push({ file: path.relative(frontendRoot, file), hits });
  }
  return reports;
}

function checkDebugArtifacts(files) {
  const reports = [];
  for (const file of files) {
    let ast;
    try {
      ast = parseSource(file);
    } catch (error) {
      reports.push({ file: path.relative(frontendRoot, file), parseError: error.message });
      continue;
    }

    const hits = [];
    traverse(ast, {
      DebuggerStatement(pathRef) {
        hits.push({ kind: "debugger", line: pathRef.node.loc?.start?.line || "?" });
      },
      CallExpression(pathRef) {
        const callee = pathRef.node.callee;
        if (
          callee.type === "MemberExpression" &&
          callee.object.type === "Identifier" &&
          callee.object.name === "console" &&
          callee.property.type === "Identifier" &&
          ["debug", "error", "info", "log", "warn"].includes(callee.property.name)
        ) {
          hits.push({ kind: `console.${callee.property.name}`, line: callee.loc?.start?.line || "?" });
        }
      },
    });

    if (hits.length) reports.push({ file: path.relative(frontendRoot, file), hits });
  }
  return reports;
}

function checkEmptyErrorHandlers(files) {
  const reports = [];
  for (const file of files) {
    let ast;
    try {
      ast = parseSource(file);
    } catch (error) {
      reports.push({ file: path.relative(frontendRoot, file), parseError: error.message });
      continue;
    }

    const hits = [];
    traverse(ast, {
      CatchClause(pathRef) {
        if (pathRef.node.body.body.length === 0) {
          hits.push({ kind: "empty-catch", line: pathRef.node.loc?.start?.line || "?" });
        }
      },
      CallExpression(pathRef) {
        const { callee } = pathRef.node;
        if (
          callee.type !== "MemberExpression" ||
          callee.property.type !== "Identifier" ||
          callee.property.name !== "catch"
        ) {
          return;
        }
        const handler = pathRef.node.arguments[0];
        if (
          (handler?.type === "ArrowFunctionExpression" || handler?.type === "FunctionExpression") &&
          handler.body.type === "BlockStatement" &&
          handler.body.body.length === 0
        ) {
          hits.push({ kind: "empty-promise-catch", line: handler.loc?.start?.line || "?" });
        }
      },
    });

    if (hits.length) reports.push({ file: path.relative(frontendRoot, file), hits });
  }
  return reports;
}

function checkUnstableRandomKeys(files) {
  const reports = [];
  for (const file of files) {
    let ast;
    try {
      ast = parseSource(file);
    } catch (error) {
      reports.push({ file: path.relative(frontendRoot, file), parseError: error.message });
      continue;
    }

    const hits = [];
    traverse(ast, {
      CallExpression(pathRef) {
        const { callee } = pathRef.node;
        if (
          callee.type !== "MemberExpression" ||
          callee.object.type !== "Identifier" ||
          callee.object.name !== "Math" ||
          callee.property.type !== "Identifier" ||
          callee.property.name !== "random"
        ) {
          return;
        }

        const objectKey = pathRef.findParent((parent) => (
          parent.isObjectProperty() &&
          parent.node.key.type === "Identifier" &&
          parent.node.key.name === "key"
        ));
        const jsxKey = pathRef.findParent((parent) => (
          parent.isJSXAttribute() &&
          parent.node.name?.type === "JSXIdentifier" &&
          parent.node.name.name === "key"
        ));
        if (objectKey || jsxKey) {
          hits.push({ kind: "random-key", line: pathRef.node.loc?.start?.line || "?" });
        }
      },
    });

    if (hits.length) reports.push({ file: path.relative(frontendRoot, file), hits });
  }
  return reports;
}

function checkBackupOpsMenuSplit() {
  const checks = [
    {
      file: path.join(sourceRoot, "menuConfig.jsx"),
      patterns: [
        { regex: /ops\.(postgresql|sqlserver)\.backupRecovery/g, kind: "db-ops-backup-menu-key" },
      ],
    },
    {
      file: path.join(sourceRoot, "GenericOperationsCenterPage.jsx"),
      patterns: [
        { regex: /\b(PostgreSQL|SQLServer)BackupRecoveryPanel\b/g, kind: "db-ops-backup-panel" },
        { regex: /\bbackupRecovery\s*:/g, kind: "db-ops-backup-meta" },
      ],
    },
  ];

  return checks.flatMap(({ file, patterns }) => {
    const source = fs.readFileSync(file, "utf8");
    const hits = [];
    for (const { regex, kind } of patterns) {
      let match;
      while ((match = regex.exec(source))) {
        hits.push({
          kind,
          line: source.slice(0, match.index).split(/\r?\n/).length,
          message: "backup/recovery should stay in the independent backup center, not PG/SQL Server ops menus",
        });
      }
    }
    return hits.length ? [{ file: path.relative(frontendRoot, file), hits }] : [];
  });
}

function checkGenericOpsUnknownSectionFallback() {
  const file = path.join(sourceRoot, "GenericOperationsCenterPage.jsx");
  const source = fs.readFileSync(file, "utf8");
  const patterns = [
    {
      regex: /return\s+<(?:PostgreSQL|SQLServer)GovernancePanel\b/g,
      kind: "generic-ops-unknown-section-governance-panel",
    },
    {
      regex: /const\s+section\s*=\s*activeKey\.split\("\."\)\.pop\(\)\s*\|\|\s*"governance"/g,
      kind: "generic-ops-empty-section-governance-default",
    },
    {
      regex: /POSTGRESQL_OPS_META\[section\]\s*\|\|\s*POSTGRESQL_OPS_META\.governance/g,
      kind: "postgresql-ops-meta-governance-fallback",
    },
    {
      regex: /SQLSERVER_OPS_META\[section\]\s*\|\|\s*SQLSERVER_OPS_META\.governance/g,
      kind: "sqlserver-ops-meta-governance-fallback",
    },
  ];
  const hits = [];
  for (const { regex, kind } of patterns) {
    let match;
    while ((match = regex.exec(source))) {
      hits.push({
        kind,
        line: source.slice(0, match.index).split(/\r?\n/).length,
        message: "unknown PG/SQL Server ops sections must render an explicit empty state instead of silently opening governance",
      });
    }
  }
  return hits.length ? [{ file: path.relative(frontendRoot, file), hits }] : [];
}

function checkGenericOpsDynamicRowKeys() {
  const file = path.join(sourceRoot, "GenericOperationsCenterPage.jsx");
  const source = fs.readFileSync(file, "utf8");
  const patterns = [
    {
      regex: /rowKey=\{\([^)]*\bindex\b[^)]*\)\s*=>/g,
      kind: "generic-ops-index-row-key",
    },
  ];
  const hits = [];
  for (const { regex, kind } of patterns) {
    let match;
    while ((match = regex.exec(source))) {
      hits.push({
        kind,
        line: source.slice(0, match.index).split(/\r?\n/).length,
        message: "generic ops dynamic tables should use content-derived stable row keys, not array indexes",
      });
    }
  }
  return hits.length ? [{ file: path.relative(frontendRoot, file), hits }] : [];
}

function checkProfileDiagnosisStableKeys() {
  const checks = [
    path.join(sourceRoot, "SQLProfilePanels.jsx"),
    path.join(sourceRoot, "SQLProfileCenterPage.jsx"),
    path.join(sourceRoot, "RuleDiagnosisPage.jsx"),
    path.join(sourceRoot, "MySQLBasicTab.jsx"),
    path.join(sourceRoot, "MySQLDailyOpsTabs.jsx"),
  ];
  return checks.flatMap((file) => {
    const source = fs.readFileSync(file, "utf8");
    const patterns = [
      { regex: /rowKey=\{\([^)]*\b(?:index|i)\b[^)]*\)\s*=>/g, kind: "index-row-key" },
      { regex: /key=\{\s*index\s*\}/g, kind: "index-jsx-key" },
    ];
    const hits = [];
    for (const { regex, kind } of patterns) {
      let match;
      while ((match = regex.exec(source))) {
        hits.push({
          kind,
          line: source.slice(0, match.index).split(/\r?\n/).length,
          message: "profile and diagnosis views should use content-derived stable keys, not array indexes",
        });
      }
    }
    return hits.length ? [{ file: path.relative(frontendRoot, file), hits }] : [];
  });
}

function checkUnstableIndexKeys(files) {
  const reports = [];
  for (const file of files) {
    const source = fs.readFileSync(file, "utf8");
    const patterns = [
      {
        regex: /rowKey=\{\([^)]*\b(?:index|i|idx)\b[^)]*\)\s*=>/g,
        kind: "index-row-key",
      },
      {
        regex: /rowKey=\{\s*\([^)]*\)\s*=>\s*(?:index|i|idx)\s*\}/g,
        kind: "index-only-row-key",
      },
      {
        regex: /key=\{\s*(?:index|i|idx)\s*\}/g,
        kind: "index-jsx-key",
      },
      {
        regex: /key=\{[^}]*\|\|\s*(?:index|i|idx)\s*\}/g,
        kind: "index-fallback-jsx-key",
      },
      {
        regex: /key=\{[^}]*\+\s*(?:index|i|idx)\s*\}/g,
        kind: "index-concat-jsx-key",
      },
      {
        regex: /key=\{`\$\{[^}]+\}-\$\{(?:index|i|idx)\}`\}/g,
        kind: "index-template-jsx-key",
      },
    ];
    const hits = [];
    for (const { regex, kind } of patterns) {
      let match;
      while ((match = regex.exec(source))) {
        hits.push({
          kind,
          line: source.slice(0, match.index).split(/\r?\n/).length,
          message: "React keys should be derived from stable row/content identity instead of array position",
        });
      }
    }
    if (hits.length) reports.push({ file: path.relative(frontendRoot, file), hits });
  }
  return reports;
}

function checkMenuRoutes() {
  const menuAst = parseSource(path.join(sourceRoot, "menuConfig.jsx"));
  const routerAst = parseSource(path.join(sourceRoot, "AppPageRouter.jsx"));
  const routeMapNames = new Set([
    "STATIC_ROUTES",
    "INSTANCE_ID_ROUTES",
    "SELECTED_INSTANCE_ROUTES",
    "INSTANCES_ONLY_ROUTES",
  ]);
  const leafKeys = new Set();
  const backupKeys = new Set();
  const exactRoutes = new Set();
  const menuKeyLines = new Map();
  const backupKeyLines = new Map();
  const exactRouteLines = new Map();
  const prefixRoutes = [];

  const recordKey = (map, key, line) => {
    if (!map.has(key)) map.set(key, []);
    map.get(key).push(line || "?");
  };

  const duplicatesFrom = (map) =>
    [...map]
      .filter(([, lines]) => lines.length > 1)
      .map(([key, lines]) => ({ key, lines }))
      .sort((a, b) => a.key.localeCompare(b.key));

  traverse(menuAst, {
    ObjectExpression(pathRef) {
      const keyProp = pathRef.node.properties.find(
        (prop) =>
          prop.type === "ObjectProperty" &&
          prop.key.type === "Identifier" &&
          prop.key.name === "key" &&
          prop.value.type === "StringLiteral",
      );
      if (!keyProp) return;
      recordKey(menuKeyLines, keyProp.value.value, keyProp.value.loc?.start?.line);
      const hasChildren = pathRef.node.properties.some(
        (prop) =>
          prop.type === "ObjectProperty" &&
          prop.key.type === "Identifier" &&
          prop.key.name === "children",
      );
      if (!hasChildren) leafKeys.add(keyProp.value.value);
    },
    VariableDeclarator(pathRef) {
      if (
        pathRef.node.id.type !== "Identifier" ||
        pathRef.node.id.name !== "BACKUP_CENTER_KEYS" ||
        pathRef.node.init?.type !== "ArrayExpression"
      ) {
        return;
      }
      for (const item of pathRef.node.init.elements) {
        if (item?.type === "StringLiteral") {
          backupKeys.add(item.value);
          recordKey(backupKeyLines, item.value, item.loc?.start?.line);
        }
      }
    },
  });

  traverse(routerAst, {
    VariableDeclarator(pathRef) {
      if (
        pathRef.node.id.type !== "Identifier" ||
        !routeMapNames.has(pathRef.node.id.name) ||
        pathRef.node.init?.type !== "ObjectExpression"
      ) {
        return;
      }
      for (const prop of pathRef.node.init.properties) {
        if (prop.type !== "ObjectProperty") continue;
        const key =
          prop.key.type === "Identifier"
            ? prop.key.name
            : prop.key.type === "StringLiteral"
              ? prop.key.value
              : null;
        if (key) {
          exactRoutes.add(key);
          recordKey(exactRouteLines, key, prop.key.loc?.start?.line);
        }
      }
    },
    BinaryExpression(pathRef) {
      if (pathRef.node.operator !== "===") return;
      const { left, right } = pathRef.node;
      if (left.type === "Identifier" && left.name === "activeKey" && right.type === "StringLiteral") {
        exactRoutes.add(right.value);
        recordKey(exactRouteLines, right.value, right.loc?.start?.line);
      }
      if (right.type === "Identifier" && right.name === "activeKey" && left.type === "StringLiteral") {
        exactRoutes.add(left.value);
        recordKey(exactRouteLines, left.value, left.loc?.start?.line);
      }
    },
    CallExpression(pathRef) {
      const callee = pathRef.node.callee;
      if (
        callee.type === "MemberExpression" &&
        callee.object.type === "Identifier" &&
        callee.object.name === "activeKey" &&
        callee.property.type === "Identifier" &&
        callee.property.name === "startsWith"
      ) {
        const arg = pathRef.node.arguments[0];
        if (arg?.type === "StringLiteral") prefixRoutes.push(arg.value);
      }
    },
  });

  const isRouted = (key) =>
    exactRoutes.has(key) ||
    backupKeys.has(key) ||
    prefixRoutes.some((prefix) => key.startsWith(prefix));
  const isDeclaredRoute = (key) =>
    leafKeys.has(key) ||
    backupKeys.has(key) ||
    prefixRoutes.some((prefix) => key.startsWith(prefix));

  return {
    menuLeafCount: leafKeys.size,
    menuLeafKeys: [...leafKeys].sort(),
    exactRouteCount: exactRoutes.size,
    prefixRoutes,
    backupKeyCount: backupKeys.size,
    duplicateMenuKeys: duplicatesFrom(menuKeyLines),
    duplicateBackupKeys: duplicatesFrom(backupKeyLines),
    duplicateExactRoutes: duplicatesFrom(exactRouteLines),
    orphanExactRoutes: [...exactRoutes].filter((key) => !isDeclaredRoute(key)).sort(),
    unrouted: [...leafKeys].filter((key) => !isRouted(key)).sort(),
  };
}

function checkModuleConfigDefaults(menuRouteReport) {
  const moduleConfigFile = path.join(repoRoot, "backend", "app", "api", "v1", "module_config.py");
  const source = fs.readFileSync(moduleConfigFile, "utf8");
  const defaultKeys = new Set();
  const keyRegex = /\{\s*"module_key"\s*:\s*"([^"]+)"/g;
  let match;
  while ((match = keyRegex.exec(source))) {
    defaultKeys.add(match[1]);
  }

  const expectedKeys = new Set([...menuRouteReport.menuLeafKeys, "moduleConfig"]);
  return {
    defaultModuleCount: defaultKeys.size,
    expectedModuleCount: expectedKeys.size,
    missingDefaults: [...expectedKeys].filter((key) => !defaultKeys.has(key)).sort(),
    extraDefaults: [...defaultKeys].filter((key) => !expectedKeys.has(key)).sort(),
  };
}

const files = listFiles(sourceRoot);
const orphanSourceFileReports = checkOrphanSourceFiles(files);
const missingBindingReports = checkMissingBindings(files);
const unusedImportReports = checkUnusedImports(files);
const unusedLocalFunctionReports = checkUnusedLocalFunctions(files);
const unusedNamedExportReports = checkUnusedNamedExports(files);
const isKnownLegacyOrphan = (report) => report.message === "source file is not reachable from any local import/export entry"
  && knownLegacyOrphanSourceFiles.has(report.file);
const unusedNamedExportKey = (report) => `${report.file}#${report.name}`;
const knownLegacyOrphanSourceFileReports = orphanSourceFileReports.filter(isKnownLegacyOrphan);
const unexpectedOrphanSourceFileReports = orphanSourceFileReports.filter((report) => !isKnownLegacyOrphan(report));
const knownLegacyUnusedNamedExportReports = unusedNamedExportReports.filter((report) =>
  knownLegacyUnusedNamedExports.has(unusedNamedExportKey(report)));
const unexpectedUnusedNamedExportReports = unusedNamedExportReports.filter((report) =>
  !knownLegacyUnusedNamedExports.has(unusedNamedExportKey(report)));
const observedLegacyOrphans = new Set(knownLegacyOrphanSourceFileReports.map((report) => report.file));
const observedLegacyExports = new Set(knownLegacyUnusedNamedExportReports.map(unusedNamedExportKey));
const staleKnownLegacyOrphanSourceFiles = [...knownLegacyOrphanSourceFiles]
  .filter((file) => !observedLegacyOrphans.has(file));
const staleKnownLegacyUnusedNamedExports = [...knownLegacyUnusedNamedExports]
  .filter((key) => !observedLegacyExports.has(key));
const suspiciousTextReports = checkSuspiciousText(files);
const directTransportReports = checkDirectTransportUsage(files);
const apiBoundaryReports = checkApiBoundaryUsage(files);
const apiTransportImportReports = checkApiTransportImportBoundary(files);
const browserUtilityBoundaryReports = checkBrowserUtilityBoundaryUsage(files);
const debugArtifactReports = checkDebugArtifacts(files);
const emptyErrorHandlerReports = checkEmptyErrorHandlers(files);
const unstableRandomKeyReports = checkUnstableRandomKeys(files);
const backupOpsSplitReports = checkBackupOpsMenuSplit();
const genericOpsFallbackReports = checkGenericOpsUnknownSectionFallback();
const genericOpsRowKeyReports = checkGenericOpsDynamicRowKeys();
const profileDiagnosisKeyReports = checkProfileDiagnosisStableKeys();
const unstableIndexKeyReports = checkUnstableIndexKeys(files);
const menuRouteReport = checkMenuRoutes();
const moduleConfigReport = checkModuleConfigDefaults(menuRouteReport);
const publicMenuRouteReport = { ...menuRouteReport };
delete publicMenuRouteReport.menuLeafKeys;
const summary = {
  checkedFileCount: files.length,
  orphanSourceFileCount: orphanSourceFileReports.length,
  missingBindingCount: missingBindingReports.length,
  unusedImportFileCount: unusedImportReports.length,
  unusedImportCount: unusedImportReports.reduce((sum, report) => sum + (report.unused?.length || 0), 0),
  unusedLocalFunctionFileCount: unusedLocalFunctionReports.length,
  unusedLocalFunctionCount: unusedLocalFunctionReports.reduce((sum, report) => sum + (report.unused?.length || 0), 0),
  unusedNamedExportCount: unusedNamedExportReports.length,
  suspiciousTextFileCount: suspiciousTextReports.length,
  suspiciousTextCount: suspiciousTextReports.reduce((sum, report) => sum + (report.hits?.length || 0), 0),
  directTransportFileCount: directTransportReports.length,
  directTransportCount: directTransportReports.reduce((sum, report) => sum + (report.hits?.length || 0), 0),
  apiBoundaryFileCount: apiBoundaryReports.length,
  apiBoundaryCount: apiBoundaryReports.reduce((sum, report) => sum + (report.hits?.length || 0), 0),
  apiTransportImportFileCount: apiTransportImportReports.length,
  apiTransportImportCount: apiTransportImportReports.reduce((sum, report) => sum + (report.hits?.length || 0), 0),
  browserUtilityBoundaryFileCount: browserUtilityBoundaryReports.length,
  browserUtilityBoundaryCount: browserUtilityBoundaryReports.reduce((sum, report) => sum + (report.hits?.length || 0), 0),
  debugArtifactFileCount: debugArtifactReports.length,
  debugArtifactCount: debugArtifactReports.reduce((sum, report) => sum + (report.hits?.length || 0), 0),
  emptyErrorHandlerFileCount: emptyErrorHandlerReports.length,
  emptyErrorHandlerCount: emptyErrorHandlerReports.reduce((sum, report) => sum + (report.hits?.length || 0), 0),
  unstableRandomKeyFileCount: unstableRandomKeyReports.length,
  unstableRandomKeyCount: unstableRandomKeyReports.reduce((sum, report) => sum + (report.hits?.length || 0), 0),
  backupOpsSplitFileCount: backupOpsSplitReports.length,
  backupOpsSplitCount: backupOpsSplitReports.reduce((sum, report) => sum + (report.hits?.length || 0), 0),
  genericOpsFallbackFileCount: genericOpsFallbackReports.length,
  genericOpsFallbackCount: genericOpsFallbackReports.reduce((sum, report) => sum + (report.hits?.length || 0), 0),
  genericOpsRowKeyFileCount: genericOpsRowKeyReports.length,
  genericOpsRowKeyCount: genericOpsRowKeyReports.reduce((sum, report) => sum + (report.hits?.length || 0), 0),
  profileDiagnosisKeyFileCount: profileDiagnosisKeyReports.length,
  profileDiagnosisKeyCount: profileDiagnosisKeyReports.reduce((sum, report) => sum + (report.hits?.length || 0), 0),
  unstableIndexKeyFileCount: unstableIndexKeyReports.length,
  unstableIndexKeyCount: unstableIndexKeyReports.reduce((sum, report) => sum + (report.hits?.length || 0), 0),
  duplicateMenuKeyCount: menuRouteReport.duplicateMenuKeys.length,
  duplicateBackupKeyCount: menuRouteReport.duplicateBackupKeys.length,
  duplicateExactRouteCount: menuRouteReport.duplicateExactRoutes.length,
  orphanExactRouteCount: menuRouteReport.orphanExactRoutes.length,
  knownLegacy: {
    orphanSourceFiles: knownLegacyOrphanSourceFileReports.map((report) => report.file),
    unusedNamedExports: knownLegacyUnusedNamedExportReports.map(({ file, name, line }) => ({ file, name, line })),
    staleOrphanSourceFiles: staleKnownLegacyOrphanSourceFiles,
    staleUnusedNamedExports: staleKnownLegacyUnusedNamedExports,
  },
  menuRouteReport: publicMenuRouteReport,
  moduleConfigReport,
};

console.log(JSON.stringify(summary, null, 2));

if (
  unexpectedOrphanSourceFileReports.length ||
  staleKnownLegacyOrphanSourceFiles.length ||
  missingBindingReports.length ||
  unusedImportReports.length ||
  unusedLocalFunctionReports.length ||
  unexpectedUnusedNamedExportReports.length ||
  staleKnownLegacyUnusedNamedExports.length ||
  suspiciousTextReports.length ||
  directTransportReports.length ||
  apiBoundaryReports.length ||
  apiTransportImportReports.length ||
  browserUtilityBoundaryReports.length ||
  debugArtifactReports.length ||
  emptyErrorHandlerReports.length ||
  unstableRandomKeyReports.length ||
  backupOpsSplitReports.length ||
  genericOpsFallbackReports.length ||
  genericOpsRowKeyReports.length ||
  profileDiagnosisKeyReports.length ||
  unstableIndexKeyReports.length ||
  menuRouteReport.duplicateMenuKeys.length ||
  menuRouteReport.duplicateBackupKeys.length ||
  menuRouteReport.duplicateExactRoutes.length ||
  menuRouteReport.orphanExactRoutes.length ||
  menuRouteReport.unrouted.length ||
  moduleConfigReport.missingDefaults.length ||
  moduleConfigReport.extraDefaults.length
) {
  if (unexpectedOrphanSourceFileReports.length) {
    console.error("Orphan source files:");
    console.error(JSON.stringify(unexpectedOrphanSourceFileReports, null, 2));
  }
  if (staleKnownLegacyOrphanSourceFiles.length) {
    console.error("Stale known-legacy orphan source baseline entries:");
    console.error(JSON.stringify(staleKnownLegacyOrphanSourceFiles, null, 2));
  }
  if (missingBindingReports.length) {
    console.error("Missing bindings:");
    console.error(JSON.stringify(missingBindingReports, null, 2));
  }
  if (unusedImportReports.length) {
    console.error("Unused imports:");
    console.error(JSON.stringify(unusedImportReports, null, 2));
  }
  if (unusedLocalFunctionReports.length) {
    console.error("Unused local functions:");
    console.error(JSON.stringify(unusedLocalFunctionReports, null, 2));
  }
  if (unexpectedUnusedNamedExportReports.length) {
    console.error("Unused named exports:");
    console.error(JSON.stringify(unexpectedUnusedNamedExportReports, null, 2));
  }
  if (staleKnownLegacyUnusedNamedExports.length) {
    console.error("Stale known-legacy unused named export baseline entries:");
    console.error(JSON.stringify(staleKnownLegacyUnusedNamedExports, null, 2));
  }
  if (suspiciousTextReports.length) {
    console.error("Suspicious text:");
    console.error(JSON.stringify(suspiciousTextReports, null, 2));
  }
  if (directTransportReports.length) {
    console.error("Direct transport usage outside api.js:");
    console.error(JSON.stringify(directTransportReports, null, 2));
  }
  if (apiBoundaryReports.length) {
    console.error("API boundary usage outside api.js:");
    console.error(JSON.stringify(apiBoundaryReports, null, 2));
  }
  if (apiTransportImportReports.length) {
    console.error("API transport imports outside API modules:");
    console.error(JSON.stringify(apiTransportImportReports, null, 2));
  }
  if (browserUtilityBoundaryReports.length) {
    console.error("Browser utility boundary usage outside shared helpers:");
    console.error(JSON.stringify(browserUtilityBoundaryReports, null, 2));
  }
  if (debugArtifactReports.length) {
    console.error("Debug artifacts:");
    console.error(JSON.stringify(debugArtifactReports, null, 2));
  }
  if (emptyErrorHandlerReports.length) {
    console.error("Empty error handlers:");
    console.error(JSON.stringify(emptyErrorHandlerReports, null, 2));
  }
  if (unstableRandomKeyReports.length) {
    console.error("Unstable random keys:");
    console.error(JSON.stringify(unstableRandomKeyReports, null, 2));
  }
  if (backupOpsSplitReports.length) {
    console.error("Backup/recovery leaked back into database ops menus:");
    console.error(JSON.stringify(backupOpsSplitReports, null, 2));
  }
  if (genericOpsFallbackReports.length) {
    console.error("Generic PG/SQL Server ops fallback hides unknown sections:");
    console.error(JSON.stringify(genericOpsFallbackReports, null, 2));
  }
  if (genericOpsRowKeyReports.length) {
    console.error("Generic PG/SQL Server ops tables use unstable index row keys:");
    console.error(JSON.stringify(genericOpsRowKeyReports, null, 2));
  }
  if (profileDiagnosisKeyReports.length) {
    console.error("Profile and diagnosis views use unstable index keys:");
    console.error(JSON.stringify(profileDiagnosisKeyReports, null, 2));
  }
  if (unstableIndexKeyReports.length) {
    console.error("Frontend views use unstable array-position keys:");
    console.error(JSON.stringify(unstableIndexKeyReports, null, 2));
  }
  if (menuRouteReport.duplicateMenuKeys.length) {
    console.error("Duplicate menu keys:");
    console.error(JSON.stringify(menuRouteReport.duplicateMenuKeys, null, 2));
  }
  if (menuRouteReport.duplicateBackupKeys.length) {
    console.error("Duplicate backup center keys:");
    console.error(JSON.stringify(menuRouteReport.duplicateBackupKeys, null, 2));
  }
  if (menuRouteReport.duplicateExactRoutes.length) {
    console.error("Duplicate exact routes:");
    console.error(JSON.stringify(menuRouteReport.duplicateExactRoutes, null, 2));
  }
  if (menuRouteReport.orphanExactRoutes.length) {
    console.error("Orphan exact routes:");
    console.error(JSON.stringify(menuRouteReport.orphanExactRoutes, null, 2));
  }
  if (menuRouteReport.unrouted.length) {
    console.error("Unrouted menu keys:");
    console.error(JSON.stringify(menuRouteReport.unrouted, null, 2));
  }
  if (moduleConfigReport.missingDefaults.length || moduleConfigReport.extraDefaults.length) {
    console.error("Module config defaults do not match menu leaf keys:");
    console.error(JSON.stringify(moduleConfigReport, null, 2));
  }
  process.exitCode = 1;
}
