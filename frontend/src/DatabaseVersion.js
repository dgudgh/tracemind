const numericVersion = (value, pattern) => {
  const match = String(value).match(pattern);
  return match?.[1] || "";
};

const trimOracleVersion = (value) => {
  const parts = value.split(".");
  while (parts.length > 2 && parts.at(-1) === "0") parts.pop();
  return parts.join(".");
};

export function formatDatabaseVersion(value, dbType) {
  const raw = String(value ?? "").trim();
  if (!raw) return "";

  const type = String(dbType || "").toLowerCase().replaceAll(/[^a-z]/g, "");
  let version = "";

  if (type === "postgresql" || type === "postgres" || type === "pgsql" || type === "pg") {
    version = numericVersion(raw, /(?:postgresql|postgres)\s+(\d+(?:\.\d+){0,3})/i)
      || numericVersion(raw, /\b(\d+(?:\.\d+){1,3})\b/);
  } else if (type === "sqlserver" || type === "mssql" || type === "microsoftsqlserver") {
    version = numericVersion(raw, /-\s*(\d+\.\d+(?:\.\d+){1,2})\s*(?:\(|$)/)
      || numericVersion(raw, /\b(\d+\.\d+\.\d+(?:\.\d+)?)\b/)
      || numericVersion(raw, /\b(\d+\.\d+)\b/);
  } else if (type === "oracle") {
    version = numericVersion(raw, /\b(\d+(?:\.\d+){1,4})\b/);
    if (!version) {
      const release = raw.match(/^\s*(\d+)\s*c\s*$/i);
      version = release ? `${release[1]}.0` : "";
    }
    if (version) version = trimOracleVersion(version);
  } else {
    version = numericVersion(raw, /\b(\d+(?:\.\d+){1,3})\b/);
  }

  return version || raw;
}
