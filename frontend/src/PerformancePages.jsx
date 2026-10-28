import React, { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Empty,
  Modal,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  Typography,
  message,
} from "antd";
import { CodeOutlined, ReloadOutlined, RocketOutlined } from "@ant-design/icons";
import { CurrentInstanceSummary } from "./OpsLayout";
import { GRADE_COLOR, SEV_COLOR, SEV_LABEL, SOURCE_LABEL, STATUS_LABEL, STATUS_COLOR } from "./PerformancePageConstants";
import {
  createPerformanceSuggestionTicket,
  getDatabaseProfile,
  getPerformanceOverview,
  getPerformanceSuggestionPlan,
  getPerformanceSuggestions,
  getPerformanceSuggestionStats,
  refreshPerformanceSuggestions,
  updatePerformanceSuggestionStatus,
} from "./modules/performanceApi";

const { Text } = Typography;


export { AwrReportView } from "./PerformanceAwrPages";
export { DatabaseProfilePage, PerfBaselinePage } from "./PerformanceAnalysisPages";
