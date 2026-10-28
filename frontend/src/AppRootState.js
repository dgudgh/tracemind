import { useCallback, useEffect, useMemo, useState, useTransition } from "react";
import { authApi, capacityApi, instanceApi, moduleApi, setupApi, systemApi } from "./apiClients";
import {
  DEFAULT_ACTIVE_KEY,
  DEFAULT_OPEN_KEYS,
  collectMenuKeys,
  filterMenuByEngine,
  filterMenuItems,
  findMenuItem,
  findMenuLabel,
  getMenuItems,
  normalizeMenuKey,
} from "./menuConfig";
import { onAuthExpired } from "./modules/authEvents";
import { clearAuthToken, getAuthToken, saveAuthToken } from "./modules/authStorage";
import { findMenuKeyPath } from "./modules/menuNavigation";
import { consumeQueryParam } from "./modules/navigation";

export const DEFAULT_ENGINE = "oracle";

const ignoreError = () => undefined;

export function useAppRootState() {
  const [authToken, setAuthToken] = useState(() => getAuthToken());
  const [authChecked, setAuthChecked] = useState(() => !getAuthToken());
  const [activeKey, setActiveKey] = useState(DEFAULT_ACTIVE_KEY);
  const [instances, setInstances] = useState([]);
  const [loadingInstances, setLoadingInstances] = useState(false);
  const [refreshToken, setRefreshToken] = useState(0);
  const [disabledModules, setDisabledModules] = useState(() => new Set());
  const [capacityAlerts, setCapacityAlerts] = useState([]);
  const [headerInstanceId, setHeaderInstanceId] = useState();
  const [needInit, setNeedInit] = useState(false);
  const [initChecked, setInitChecked] = useState(false);
  const [openKeys, setOpenKeys] = useState(DEFAULT_OPEN_KEYS);
  const [, startRouteTransition] = useTransition();

  const editionMenuItems = useMemo(() => getMenuItems(), []);
  const selectedDbType = useMemo(
    () => instances.find((item) => item.id === headerInstanceId)?.db_type || null,
    [instances, headerInstanceId],
  );
  const routeDbType = selectedDbType || instances[0]?.db_type || DEFAULT_ENGINE;
  const headerInstances = useMemo(() => {
    const found = findMenuItem(editionMenuItems, activeKey);
    const dbTypes = found?.dbTypes;
    if (dbTypes?.length) return instances.filter((item) => dbTypes.includes(item.db_type));
    return instances;
  }, [instances, activeKey, editionMenuItems]);
  const visibleMenuItems = useMemo(
    () => filterMenuByEngine(filterMenuItems(editionMenuItems, disabledModules), selectedDbType),
    [editionMenuItems, disabledModules, selectedDbType],
  );
  const allMenuKeys = useMemo(() => collectMenuKeys(editionMenuItems), [editionMenuItems]);
  const title = findMenuLabel(visibleMenuItems, activeKey) || "TraceMind AI 智能运维平台";

  const resetAuthSession = useCallback(() => {
    clearAuthToken();
    setAuthToken("");
    setInstances([]);
    setCapacityAlerts([]);
    setLoadingInstances(false);
    setAuthChecked(true);
  }, []);

  const doLogin = useCallback(async (username, password) => {
    const data = await authApi.login(username, password);
    if (data.mfa_required) return data;
    saveAuthToken(data.token);
    setAuthToken(data.token);
    setAuthChecked(true);
    return data;
  }, []);

  const doMfaLogin = useCallback(async (challengeToken, code) => {
    const data = await authApi.verifyMfa(challengeToken, code);
    saveAuthToken(data.token);
    setAuthToken(data.token);
    setAuthChecked(true);
    return data;
  }, []);

  const doExternalLogin = useCallback(async (username, password) => {
    const data = await authApi.externalLogin(username, password);
    saveAuthToken(data.token);
    setAuthToken(data.token);
    setAuthChecked(true);
  }, []);

  const doOidcExchange = useCallback(async (code) => {
    const data = await authApi.oidcExchange(code);
    saveAuthToken(data.token);
    setAuthToken(data.token);
    setAuthChecked(true);
  }, []);

  const doMesLogin = useCallback(async (mesToken) => {
    const data = await authApi.mesLogin(mesToken);
    saveAuthToken(data.token);
    setAuthToken(data.token);
    setAuthChecked(true);
  }, []);

  const doLogout = useCallback(() => {
    const token = getAuthToken();
    if (token) authApi.logout(token).catch(ignoreError);
    resetAuthSession();
  }, [resetAuthSession]);

  const refreshInstances = useCallback(() => setRefreshToken((value) => value + 1), []);
  const clearCapacityAlerts = useCallback(() => setCapacityAlerts([]), []);
  const navigateActiveKey = useCallback((key) => {
    startRouteTransition(() => setActiveKey(normalizeMenuKey(key)));
  }, [startRouteTransition]);

  useEffect(() => {
    (async () => {
      try {
        const response = await systemApi.initStatus();
        if (!response.initialized) setNeedInit(true);
      } catch {
        try {
          const data = await setupApi.initStatus();
          if (!data.initialized) setNeedInit(true);
        } catch {
          setNeedInit(false);
        }
      }
      setInitChecked(true);
    })();
  }, []);

  useEffect(() => {
    const handler = () => resetAuthSession();
    return onAuthExpired(handler);
  }, [resetAuthSession]);

  useEffect(() => {
    if (!authToken) {
      setAuthChecked(true);
      return undefined;
    }

    let cancelled = false;
    setAuthChecked(false);
    authApi
      .currentUser()
      .then(() => {
        if (!cancelled) setAuthChecked(true);
      })
      .catch(() => {
        if (!cancelled) {
          resetAuthSession();
        }
      });

    return () => {
      cancelled = true;
    };
  }, [authToken, resetAuthSession]);

  useEffect(() => {
    if (!authToken) {
      const oidcCode = consumeQueryParam("external_login_code");
      if (oidcCode) {
        doOidcExchange(oidcCode).catch(() => {
          resetAuthSession();
        });
        return;
      }
      const mesToken = consumeQueryParam("token");
      if (!mesToken) return;
      doMesLogin(mesToken).catch(() => {
        resetAuthSession();
      });
    }
  }, [authToken, doMesLogin, doOidcExchange, resetAuthSession]);

  useEffect(() => {
    if (!authToken || !authChecked) return;
    let cancelled = false;
    moduleApi
      .list()
      .then((data) => {
        if (cancelled) return;
        const items = data.items || [];
        setDisabledModules(new Set(items.filter((item) => !item.enabled).map((item) => item.module_key)));
      })
      .catch(() => {
        if (cancelled) return;
        setDisabledModules(new Set());
      });
    return () => {
      cancelled = true;
    };
  }, [authToken, authChecked, refreshToken]);

  useEffect(() => {
    const normalized = normalizeMenuKey(activeKey);
    if (normalized !== activeKey) {
      setActiveKey(normalized);
      return;
    }
    if (!allMenuKeys.has(activeKey)) {
      setActiveKey(DEFAULT_ACTIVE_KEY);
    }
  }, [activeKey, allMenuKeys]);

  useEffect(() => {
    const nextOpenKeys = findMenuKeyPath(visibleMenuItems, activeKey).slice(0, -1);
    setOpenKeys((currentOpenKeys) => {
      const unchanged = currentOpenKeys.length === nextOpenKeys.length
        && currentOpenKeys.every((key, index) => key === nextOpenKeys[index]);
      return unchanged ? currentOpenKeys : nextOpenKeys;
    });
  }, [activeKey, visibleMenuItems]);

  useEffect(() => {
    if (!authToken || !authChecked) return;
    let cancelled = false;
    setLoadingInstances(true);
    instanceApi
      .list()
      .then((data) => {
        if (!cancelled) setInstances(data.items || []);
      })
      .catch(() => {
        if (!cancelled) setInstances([]);
      })
      .finally(() => {
        if (!cancelled) setLoadingInstances(false);
      });
    return () => {
      cancelled = true;
    };
  }, [authToken, authChecked, refreshToken]);

  useEffect(() => {
    if (!authToken || !authChecked) return;
    let cancelled = false;
    capacityApi
      .alerts({ limit: 20 })
      .then((data) => {
        if (!cancelled) setCapacityAlerts(data.items || []);
      })
      .catch(() => {
        if (!cancelled) setCapacityAlerts([]);
      });
    return () => {
      cancelled = true;
    };
  }, [authToken, authChecked, refreshToken]);

  useEffect(() => {
    if (headerInstanceId && !headerInstances.some((item) => item.id === headerInstanceId)) {
      setHeaderInstanceId(undefined);
    }
  }, [headerInstanceId, headerInstances]);

  return {
    activeKey,
    authChecked,
    authToken,
    capacityAlerts,
    clearCapacityAlerts,
    doLogin,
    doMfaLogin,
    doExternalLogin,
    doLogout,
    headerInstanceId,
    headerInstances,
    initChecked,
    instances,
    loadingInstances,
    needInit,
    openKeys,
    refreshInstances,
    routeDbType,
    setActiveKey: navigateActiveKey,
    setHeaderInstanceId,
    setOpenKeys,
    title,
    visibleMenuItems,
  };
}
