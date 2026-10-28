export const DEFAULT_TABLE_PAGINATION = {
  defaultPageSize: 10,
  hideOnSinglePage: false,
  showSizeChanger: true,
  pageSizeOptions: ["10", "20", "50", "100"],
  showTotal: (total, range) => `${range[0]}-${range[1]} / ${total}`,
};
