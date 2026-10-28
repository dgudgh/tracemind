import { Alert, Drawer } from "antd";

// 工单抽屉不在本仓库内。保留组件名并渲染一条说明，
// 避免三个页面因缺少该组件无法构建、或点了按钮后 404。
export function ItsmTicketDrawer({ open, onClose }) {
  return (
    <Drawer title="创建工单" open={!!open} onClose={onClose} width={480} destroyOnClose>
      <Alert
        type="info"
        showIcon
        message="此功能暂不支持"
        description="问题记录与证据仍可在当前页面查看和导出。"
      />
    </Drawer>
  );
}
