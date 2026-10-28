import { api } from "../api";

export async function dispatchAssistantIntent(payload) {
  const controller = new globalThis.AbortController();
  const timer = globalThis.setTimeout(() => controller.abort(), 120_000);
  try {
    return await api.request("/assistant/dispatch-intent", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
  } finally {
    globalThis.clearTimeout(timer);
  }
}

export async function getDispatchReceipt(requestId) {
  return api.request(`/assistant/dispatch-intent/${encodeURIComponent(requestId)}`);
}
