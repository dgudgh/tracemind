import { useEffect, useState } from "react";
import { readEngineCatalog } from "../apiClients";
import { catalogEpoch } from "./catalogEpoch";
import { getAuthSession, isAuthSessionCurrent } from "./authStorage";

// No response is cached across an instance, window, authentication, or refresh epoch.
export function useEngineCatalog(active, instanceId) {
  const [value, setValue] = useState(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    const epoch = catalogEpoch();
    setValue(null); setError(false);
    if (!active) return () => epoch.close();
    const refresh = () => {
      const current = epoch.next();
      const session = getAuthSession();
      readEngineCatalog(session).then((result) => {
        if (current() && isAuthSessionCurrent(session)) { setValue(result); setError(false); }
      }).catch(() => {
        if (current() && isAuthSessionCurrent(session)) { setValue(null); setError(true); }
      });
    };
    refresh();
    const timer = setInterval(refresh, 15000);
    return () => { clearInterval(timer); epoch.close(); };
  }, [active, instanceId]);
  return { value, error };
}
