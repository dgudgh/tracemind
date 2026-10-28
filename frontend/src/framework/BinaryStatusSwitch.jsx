import React from "react";
import { Switch } from "antd";

export function BinaryStatusSwitch({
  value,
  onChange,
  trueValue = 1,
  falseValue = 0,
  checkedChildren = "启用",
  unCheckedChildren = "停用",
  className = "",
  ...props
}) {
  const checked = value === trueValue
    || (trueValue === 1 && (value === true || value === "1"));

  return (
    <Switch
      {...props}
      className={`binary-status-switch ${className}`.trim()}
      checked={checked}
      checkedChildren={checkedChildren}
      unCheckedChildren={unCheckedChildren}
      onChange={(nextChecked) => onChange?.(nextChecked ? trueValue : falseValue)}
    />
  );
}
