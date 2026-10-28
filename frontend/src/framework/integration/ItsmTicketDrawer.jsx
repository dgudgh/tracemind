import { Alert, Drawer } from "antd";

// 工单抽屉属企业版集成中心。开源版保留组件名并渲染一条说明，
// 避免三个开源页面因缺少该组件无法构建、或点了按钮后 404。
export function ItsmTicketDrawer({ open, onClose }) {
  return (
    <Drawer title="创建工单" open={!!open} onClose={onClose} width={480} destroyOnClose>
      <Alert
        type="info"
        showIcon
        message="工单管理属集成中心（企业版）"
        description="开源版不包含 ITSM 工单对接。问题记录与证据仍可在当前页面查看和导出。"
      />
    </Drawer>
  );
}
