export function saveTextFile(content, filename, mimeType = "text/plain;charset=utf-8") {
  const blob = new Blob([content ?? ""], { type: mimeType });
  saveBlobFile(blob, filename);
}

export function saveBlobFile(blob, filename) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  try {
    anchor.href = url;
    anchor.download = filename || "download";
    anchor.style.display = "none";
    document.body.appendChild(anchor);
    anchor.click();
  } finally {
    anchor.remove();
    URL.revokeObjectURL(url);
  }
}

export function filenameFromContentDisposition(value) {
  const contentDisposition = value || "";
  const encoded = contentDisposition.match(/filename\*=UTF-8''([^;]+)/i);
  if (encoded) return decodeURIComponent(encoded[1].replace(/^"|"$/g, ""));
  const plain = contentDisposition.match(/filename="?([^";]+)"?/i);
  return plain ? decodeURIComponent(plain[1]) : "";
}

export function saveJsonFile(payload, filename) {
  saveTextFile(JSON.stringify(payload || {}, null, 2), filename, "application/json;charset=utf-8");
}

export function saveCsvFile(rows, filename) {
  saveTextFile(`\uFEFF${(rows || []).join("\n")}`, filename, "text/csv;charset=utf-8");
}
