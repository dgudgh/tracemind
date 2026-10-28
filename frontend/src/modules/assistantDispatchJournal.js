export const ASSISTANT_DISPATCH_JOURNAL_KEY = "mesdb:lastDispatchRequest";
// The longest durable execution lease is 24 hours. Keep the same-tab locator
// slightly longer so admission and queue time cannot silently orphan it.
export const ASSISTANT_DISPATCH_JOURNAL_TTL = 25 * 60 * 60 * 1000;

const requestPattern = /^[A-Za-z0-9._:-]{8,128}$/;
const epochPattern = /^[a-f0-9]{32}$/;

function storageOrNull(storage) {
  if (storage) return storage;
  try { return globalThis.sessionStorage; } catch { return null; }
}

export function readAssistantDispatchJournal(storage, now = Date.now()) {
  try {
    const value = JSON.parse(storageOrNull(storage)?.getItem(ASSISTANT_DISPATCH_JOURNAL_KEY) || "null");
    if (!value || value.version !== 1 || !requestPattern.test(value.requestId)
      || !(value.instanceId === null || (Number.isSafeInteger(value.instanceId) && value.instanceId > 0))
      || !epochPattern.test(value.loginEpoch) || !Number.isFinite(value.at)
      || (value.text !== undefined && (typeof value.text !== "string" || value.text.length > 2000))
      || value.at > now + 60_000 || now - value.at > ASSISTANT_DISPATCH_JOURNAL_TTL) return null;
    return { version: 1, requestId: value.requestId, instanceId: value.instanceId,
      loginEpoch: value.loginEpoch, at: value.at, text: value.text || "" };
  } catch {
    return null;
  }
}

export function writeAssistantDispatchJournal({ requestId, instanceId, session, text = "", at = Date.now() }, storage) {
  if (!requestPattern.test(requestId) || !(instanceId == null || (Number.isSafeInteger(instanceId) && instanceId > 0))
    || !epochPattern.test(session?.loginEpoch) || typeof text !== "string" || text.length > 2000 || !Number.isFinite(at)) return false;
  const value = { version: 1, requestId, instanceId: instanceId || null, loginEpoch: session.loginEpoch, at, text };
  try {
    const target = storageOrNull(storage);
    target?.setItem(ASSISTANT_DISPATCH_JOURNAL_KEY, JSON.stringify(value));
    return target?.getItem(ASSISTANT_DISPATCH_JOURNAL_KEY) === JSON.stringify(value);
  } catch {
    return false;
  }
}

export function currentAssistantDispatchJournal({ requestId, instanceId, session }, storage, now = Date.now()) {
  const value = readAssistantDispatchJournal(storage, now);
  return Boolean(value && value.requestId === requestId && value.instanceId === (instanceId || null)
    && value.loginEpoch === session?.loginEpoch);
}

export function clearAssistantDispatchJournal(requestId, storage) {
  try {
    const target = storageOrNull(storage);
    const value = readAssistantDispatchJournal(target);
    if (!value || value.requestId !== requestId) return false;
    target.removeItem(ASSISTANT_DISPATCH_JOURNAL_KEY);
    return true;
  } catch {
    return false;
  }
}
