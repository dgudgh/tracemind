import React, { useEffect, useMemo, useState } from "react";
import { Button, Tooltip } from "antd";
import { FileTextOutlined } from "@ant-design/icons";
import { AssistantGenericCapabilityPanel } from "./AssistantGenericCapabilityPanel";
import { AssistantPlanWorkspace } from "./AssistantPlanWorkspace";
import { getAuthSession, isAuthSessionCurrent } from "./modules/authStorage";
import { listAssistantCapabilities } from "./modules/assistantApi";
import { reportDocumentLaunchState } from "./modules/reportDocumentLauncher";

const INITIAL_PARAMS = { report_type: "after_sales", format: "markdown", window_hours: 168, include_ai: true };

export function ReportDocumentLauncher({ instances = [], instanceId }) {
  const [capabilities, setCapabilities] = useState([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [formOpen, setFormOpen] = useState(false);
  const [workspaceOpen, setWorkspaceOpen] = useState(false);
  const [planRequest, setPlanRequest] = useState(null);
  const launch = useMemo(
    () => reportDocumentLaunchState(capabilities, instances, instanceId),
    [capabilities, instances, instanceId],
  );

  useEffect(() => {
    let active = true;
    const session = getAuthSession();
    setLoading(true); setLoadError(""); setCapabilities([]);
    listAssistantCapabilities()
      .then((value) => {
        if (active && isAuthSessionCurrent(session)) setCapabilities(value.items);
      })
      .catch((error) => {
        if (active && isAuthSessionCurrent(session)) setLoadError(error.message || "能力目录读取失败");
      })
      .finally(() => {
        if (active && isAuthSessionCurrent(session)) setLoading(false);
      });
    return () => { active = false; };
  }, []);

  const reason = loadError || launch.reason;
  return <>
    <Tooltip title={reason || "先核对报告参数，再提交审批"}>
      <span><Button icon={<FileTextOutlined />} loading={loading} disabled={loading || Boolean(reason)}
        onClick={() => setFormOpen(true)}>生成分析报告</Button></span>
    </Tooltip>
    <AssistantGenericCapabilityPanel open={formOpen} onClose={() => setFormOpen(false)}
      customerMode
      capabilities={launch.capability ? [launch.capability] : []} instances={instances}
      initialCapabilityId="report.document.generate" initialInstanceId={launch.target?.id}
      initialParams={INITIAL_PARAMS}
      onOpenPlan={(planId) => {
        setPlanRequest({ planId });
        setWorkspaceOpen(true);
      }} />
    <AssistantPlanWorkspace open={workspaceOpen} onClose={() => setWorkspaceOpen(false)}
      customerMode instances={instances} instanceId={launch.target?.id} request={planRequest} />
  </>;
}
