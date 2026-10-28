import React from "react";
import { Button, Tag } from "antd";
import { CloseOutlined } from "@ant-design/icons";
import "./manualRequirementHandoff.css";

export default function ManualRequirementHandoff({ handoff, onClose }) {
  if (!handoff) return null;
  return (
    <section className="manual-requirement-handoff" aria-label="需求手动操作指引">
      <div className="manual-requirement-handoff__heading">
        <div>
          <Tag>等待手动操作</Tag>
          <strong>已打开{handoff.title}</strong>
          <span>请在下方页面继续，需求任务尚未启动。</span>
        </div>
        <Button type="text" icon={<CloseOutlined />} aria-label="关闭手动操作指引" onClick={onClose} />
      </div>
      <p className="manual-requirement-handoff__request"><span>你的需求</span>{handoff.text}</p>
      <ol aria-label="下一步">
        {handoff.steps.map((step) => <li key={step}>{step}</li>)}
      </ol>
    </section>
  );
}
