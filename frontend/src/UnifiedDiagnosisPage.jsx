import React, { useState } from "react";
import { Modal } from "antd";
import { DiagnosisCenterPage } from "./DiagnosisCenterPage";
import { RuleDiagnosisPage } from "./RuleDiagnosisPage";

export function UnifiedDiagnosisPage({ instances, selectedInstanceId, menuItems, onNavigate }) {
  const [ruleAnalysisOpen, setRuleAnalysisOpen] = useState(false);

  return (
    <div className="unified-diagnosis-shell">
      <DiagnosisCenterPage
        instances={instances}
        selectedInstanceId={selectedInstanceId}
        menuItems={menuItems}
        onNavigate={onNavigate}
        onOpenRuleDiagnosis={() => setRuleAnalysisOpen(true)}
      />
      <Modal
        className="adaptive-center-modal unified-diagnosis-rule-modal"
        title="规则分析"
        width="min(1180px, calc(100vw - 24px))"
        open={ruleAnalysisOpen}
        centered
        footer={null}
        destroyOnHidden
        onCancel={() => setRuleAnalysisOpen(false)}
      >
        <RuleDiagnosisPage
          instances={instances}
          selectedInstanceId={selectedInstanceId}
        />
      </Modal>
    </div>
  );
}
