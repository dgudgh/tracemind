import { fetchFrontendHtml } from "../api";
import { currentLocationHref, reloadPage } from "../modules/navigation";

const VERSION_CHECK_INTERVAL_MS = 60_000;

function entryAssetFromDocument(documentValue, baseHref) {
  const entry = documentValue?.querySelector?.('script[type="module"][src]');
  if (!entry) return "";
  try {
    return new URL(entry.getAttribute("src"), baseHref).pathname;
  } catch {
    return entry.getAttribute("src") || "";
  }
}

export async function refreshWhenFrontendChanges() {
  const baseHref = currentLocationHref();
  const currentAsset = entryAssetFromDocument(document, baseHref);
  if (!currentAsset) return false;

  const html = await fetchFrontendHtml();
  if (!html) return false;
  const latestDocument = new DOMParser().parseFromString(html, "text/html");
  const latestAsset = entryAssetFromDocument(latestDocument, baseHref);
  if (!latestAsset || latestAsset === currentAsset) return false;

  reloadPage();
  return true;
}

export function watchFrontendVersion() {
  let checking = false;
  const check = async () => {
    if (checking) return;
    checking = true;
    try {
      await refreshWhenFrontendChanges();
    } catch {
      return false;
    } finally {
      checking = false;
    }
  };
  const checkWhenVisible = () => {
    if (document.visibilityState === "visible") void check();
  };

  const timer = window.setInterval(check, VERSION_CHECK_INTERVAL_MS);
  window.addEventListener("focus", check);
  document.addEventListener("visibilitychange", checkWhenVisible);

  return () => {
    window.clearInterval(timer);
    window.removeEventListener("focus", check);
    document.removeEventListener("visibilitychange", checkWhenVisible);
  };
}
