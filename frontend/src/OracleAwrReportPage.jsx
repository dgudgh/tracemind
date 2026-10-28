import React, { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Dropdown,
  Empty,
  Modal,
  Popconfirm,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tag,
  Upload,
  Typography,
  message,
} from "antd";
import { UploadOutlined } from "@ant-design/icons";
import {
  deleteAwrReport,
  downloadAwrAnalysisExport as downloadAwrAnalysisExportFile,
  downloadOriginalAwrReport as downloadOriginalAwrReportFile,
  exportAwrReport,
  getAwrDashboard,
  getAwrDbInfo,
  getAwrEnvironment,
  getAwrReportDetail,
  getAwrReports,
  getAwrSnapshots,
  runAwrAiAnalysis,
  runLocalAwrAnalysis,
  uploadAwrReport,
} from "./modules/performanceAwrApi";
import { OpsEmptyState } from "./OpsLayout";
import { degradationBanner } from "./modules/aiDegradation";
import { AwrReportView } from "./PerformancePages";
import { stableRecordKey, stableValueKey } from "./modules/stableKeys";

const { Text } = Typography;

export function OracleAwrReportPage({ instanceId }) {
  const [dbInfo, setDbInfo] = useState({});
  const [snapshots, setSnapshots] = useState([]);
  const [reports, setReports] = useState([]);
  const [loading, setLoading] = useState(false);
  const [days, setDays] = useState(7);
  const [beginSnap, setBeginSnap] = useState(null);
  const [endSnap, setEndSnap] = useState(null);
  const [instNum, setInstNum] = useState(1);
  const [fmt, setFmt] = useState("HTML");
  const [exporting, setExporting] = useState(false);
  const [reportType, setReportType] = useState("INSTANCE");
  const [envData, setEnvData] = useState(null);
  const [analyzing, setAnalyzing] = useState(false);
  const [aiResult, setAiResult] = useState(null);

  const loadDbInfo = async () => {
    if (!instanceId) return;
    try {
      const r = await getAwrDbInfo(instanceId);
      setDbInfo(r);
      if (r.db?.dbid) {
        const insts = r.instances || [];
        if (insts.length > 0) setInstNum(insts[0].instance_number);
      }
    } catch (e) { message.error(e.message || "加载 AWR 数据库信息失败"); }
  };

  const loadSnapshots = async () => {
    if (!instanceId) return;
    setLoading(true);
    try {
      const r = await getAwrSnapshots(instanceId, days);
      const items = r.items || [];
      setSnapshots(items);
      if (items.length >= 2) {
        setBeginSnap(items[items.length - 2].snap_id);
        setEndSnap(items[items.length - 1].snap_id);
      } else if (items.length === 1) {
        setBeginSnap(items[0].snap_id);
        setEndSnap(null);
      }
    } catch (e) { message.error(e.message || "加载 AWR 快照失败"); }
    setLoading(false);
  };

  const loadReports = async () => {
    if (!instanceId) return;
    try {
      const r = await getAwrReports(instanceId);
      setReports(r.items || []);
    } catch (e) { message.error(e.message || "加载 AWR 报告失败"); }
  };

  useEffect(() => { loadDbInfo(); loadReports(); }, [instanceId]);
  useEffect(() => { if (instanceId) loadSnapshots(); }, [instanceId, days]);

  const doExport = async () => {
    if (!beginSnap || !endSnap) { message.warning("请选择开始和结束快照"); return; }
    if (beginSnap >= endSnap) { message.warning("开始快照必须小于结束快照"); return; }
    if (!dbInfo.db?.dbid) { message.warning("请等待数据库信息加载完成"); return; }
    setExporting(true);
    try {
      const r = await exportAwrReport(instanceId, {
        dbid: dbInfo.db.dbid.toString(), instance_number: instNum,
        begin_snap: beginSnap, end_snap: endSnap, format: fmt,
        report_type: reportType,
      });
      if (r.status === "success") {
        message.success("AWR 报告导出成功");
        loadReports();
      } else {
        message.error(r.message || "导出失败");
      }
    } catch (e) {
      const detail = e.response?.data?.detail || e.message || "导出失败";
      message.error(detail);
    }
    setExporting(false);
  };

  const doEnvAnalyze = async (report) => {
    setAnalyzing(true);
    try {
      const r = await getAwrEnvironment(instanceId, report);
      setEnvData(r);
      message.success("环境分析完成");
    } catch (e) { message.error(e.message); }
    setAnalyzing(false);
  };

  const doAiAnalyze = async (report) => {
    setAnalyzing(true);
    try {
      const r = await runAwrAiAnalysis(instanceId, report);
      setEnvData({ environment: r.environment, cpu_risk: r.cpu_risk });
      setAiResult(r.ai_analysis || degradationBanner(r.degradation)?.text || "AI 分析完成");
      message.success("AI 分析完成");
    } catch (e) { message.error(e.message); }
    setAnalyzing(false);
  };

  const [localAnalysis, setLocalAnalysis] = useState(null);
  const doLocalAnalyze = async (report) => {
    setAnalyzing(true);
    try {
      const r = await runLocalAwrAnalysis(instanceId, report.id);
      setLocalAnalysis(r);
      message.success("本地分析完成，性能评分: " + (r.performance_score || "N/A"));
    } catch (e) { message.error(e.message); }
    setAnalyzing(false);
  };

  const doDelete = async (id) => {
    await deleteAwrReport(instanceId, id);
    message.success("已删除");
    loadReports();
  };

  // AWR Dashboard
  const [awrStats, setAwrStats] = useState({total_reports:0, analyzed_reports:0, avg_performance_score:0, issues_count:0});
  const [awrTrend, setAwrTrend] = useState([]);
  const loadDashboard = async () => {
    if (!instanceId) return;
    try {
      const r = await getAwrDashboard(instanceId);
      setAwrStats(r.statistics || {});
      setAwrTrend(r.trend_data || []);
    } catch (e) {
      setAwrStats({ total_reports: 0, analyzed_reports: 0, avg_performance_score: 0, issues_count: 0 });
      setAwrTrend([]);
    }
  };
  useEffect(() => { loadDashboard(); }, [instanceId]);

  // Upload handler
  const handleUpload = async (file) => {
    try {
      const r = await uploadAwrReport(instanceId, file);
      message.success(`上传成功，评分: ${r.performance_score || 'N/A'}`);
      loadReports();
      loadDashboard();
    } catch(e) { message.error('上传失败: ' + e.message); }
    return false;
  };

  // Export analysis report to md/pdf/txt/doc and download
  const downloadAnalysisExport = async (report, fmt) => {
    const hide = message.loading(`正在生成 ${fmt.toUpperCase()} 报告...`, 0);
    try {
      await downloadAwrAnalysisExportFile(instanceId, report, fmt);
      message.success("报告已导出下载");
    } catch (e) { message.error(e.message); }
    finally { hide(); }
  };

  const downloadOriginalReport = async (report) => {
    try {
      await downloadOriginalAwrReportFile(instanceId, report);
    } catch (e) {
      message.error(e.message);
    }
  };

  // Report detail (stored analysis + recommendations)
  const [detailData, setDetailData] = useState(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const doDetail = async (report) => {
    try {
      const r = await getAwrReportDetail(instanceId, report.id);
      setDetailData(r);
      setDetailOpen(true);
    } catch (e) { message.error(e.message); }
  };

  if (!instanceId) return <OpsEmptyState title="请选择 Oracle 实例" description="在右上角统一实例选择器中选择 Oracle 实例后再导出或上传 AWR。" />;

  const db = dbInfo.db || {};
  const insts = dbInfo.instances || [];
  const snapOptions = snapshots.map((s, i) => ({ value: s.snap_id, label: reportType === "RAC_GLOBAL" ? `#${s.snap_id} (${s.begin_time})` : `#${s.snap_id} inst${s.instance_number} (${s.begin_time})` }));
  // deduplicate by value (snap_id may repeat across instances)
  const seen = new Set();
  const uniqueSnapOptions = snapOptions.filter(o => { if (seen.has(o.value)) return false; seen.add(o.value); return true; });

  return (<Space direction="vertical" size={16} style={{width:"100%"}}>
    <Row gutter={[16,16]} style={{marginBottom:8}}>
      <Col span={6}><Card size="small"><Statistic title="报告总数" value={awrStats.total_reports} /></Card></Col>
      <Col span={6}><Card size="small"><Statistic title="已分析" value={awrStats.analyzed_reports} /></Card></Col>
      <Col span={6}><Card size="small"><Statistic title="平均评分" value={awrStats.avg_performance_score} precision={1} /></Card></Col>
      <Col span={6}><Card size="small"><Statistic title="低分报告" value={awrStats.issues_count} valueStyle={{color: awrStats.issues_count > 0 ? '#cf1322' : '#3f8600'}} /></Card></Col>
    </Row>
    {awrTrend.length > 0 && <Card title="评分趋势" size="small">
      <Table dataSource={awrTrend} rowKey={(row) => stableRecordKey("awr-trend", row, ["date", "file_name", "performance_score"])} size="small" pagination={false} scroll={{x:600}} columns={[
        {title:"时间", dataIndex:"date", width:160},
        {title:"文件", dataIndex:"file_name", ellipsis:true},
        {title:"综合", dataIndex:"performance_score", width:80, render:v=>v==null?"-":<Tag color={v>=85?"green":v>=70?"blue":"red"}>{Number(v).toFixed(1)}</Tag>},
        {title:"CPU", dataIndex:"cpu_score", width:70, render:v=>v==null?"-":Number(v).toFixed(0)},
        {title:"内存", dataIndex:"memory_score", width:70, render:v=>v==null?"-":Number(v).toFixed(0)},
        {title:"IO", dataIndex:"io_score", width:70, render:v=>v==null?"-":Number(v).toFixed(0)},
      ]} />
    </Card>}
    <Card title="数据库信息" size="small"><Descriptions bordered size="small" column={3}>
      <Descriptions.Item label="DBID">{db.dbid}</Descriptions.Item>
      <Descriptions.Item label="数据库名">{db.name}</Descriptions.Item>
      <Descriptions.Item label="角色">{db.database_role}</Descriptions.Item>
      <Descriptions.Item label="Open Mode">{db.open_mode}</Descriptions.Item>
      <Descriptions.Item label="实例数">{insts.length}</Descriptions.Item>
      <Descriptions.Item label="版本">{insts[0]?.version}</Descriptions.Item>
    </Descriptions></Card>

    <Card title="AWR 快照选择" extra={<Space>
      <Text>查询范围:</Text>
      <Select value={days} onChange={setDays} style={{width:120}} options={[
        {value:1, label:"1 天"}, {value:3, label:"3 天"},
        {value:7, label:"7 天"}, {value:30, label:"30 天"},
      ]} />
      <Button onClick={loadSnapshots} loading={loading}>刷新快照</Button>
    </Space>}>
      {snapshots.length === 0 && !loading && <Empty description="未查询到快照数据" />}
      {snapshots.length > 0 && <div style={{marginTop:4, marginBottom:12}}>
        <Space wrap>
          <Text>报告类型:</Text>
          <Select value={reportType} onChange={(v) => setReportType(v)} style={{width:150}} options={[
            {value:"INSTANCE", label:"单实例 AWR"},
            {value:"RAC_GLOBAL", label:"RAC 全局 AWR"},
          ]} />
          {reportType === "INSTANCE" && <>
            <Text>实例号:</Text>
            <Select value={instNum} onChange={setInstNum} style={{width:100}} options={insts.map(i => ({value: i.instance_number, label: `${i.instance_number} (${i.instance_name})`}))} />
          </>}
          <Text>开始快照:</Text>
          <Select showSearch value={beginSnap} onChange={setBeginSnap} style={{width:280}} options={uniqueSnapOptions} placeholder="选择开始快照" />
          <Text>结束快照:</Text>
          <Select showSearch value={endSnap} onChange={setEndSnap} style={{width:280}} options={uniqueSnapOptions} placeholder="选择结束快照" />
          <Text>格式:</Text>
          <Select value={fmt} onChange={setFmt} style={{width:100}} options={[
            {value:"HTML", label:"HTML"}, {value:"TEXT", label:"TEXT"},
          ]} />
        </Space>
      </div>}
      <div style={{marginTop:8}}>
        <Space>
          <Button type="primary" onClick={doExport} loading={exporting} disabled={!beginSnap || !endSnap}>导出 AWR</Button>
        </Space>
      </div>
      {snapshots.length > 0 && <Table dataSource={snapshots} rowKey={(row) => stableRecordKey("awr-snapshot", row, ["snap_id", "instance_number", "begin_time", "end_time"])} size="small" pagination={{pageSize:8}} style={{marginTop:12}}
        columns={[
          {title:"Snap ID", dataIndex:"snap_id", width:80},
          {title:"实例号", dataIndex:"instance_number", width:70},
          {title:"开始时间", dataIndex:"begin_time", width:150},
          {title:"结束时间", dataIndex:"end_time", width:150},
        ]}
        onRow={(r) => ({onClick: () => { if (!beginSnap || (beginSnap && endSnap)) { setBeginSnap(r.snap_id); setEndSnap(null); } else if (r.snap_id > beginSnap) { setEndSnap(r.snap_id); } }})}
        rowClassName={(r) => r.snap_id === beginSnap ? "ant-table-row-selected" : r.snap_id === endSnap ? "ant-table-row-selected" : ""}
      />}
    </Card>

    <Card title="导出历史" extra={<Upload accept=".html,.htm,.txt" showUploadList={false} beforeUpload={handleUpload}><Button type="primary" icon={<UploadOutlined />} size="small">上传 AWR</Button></Upload>}>
      <Table dataSource={reports} rowKey="id" size="small" pagination={{pageSize:10}} scroll={{x:1200}} columns={[
        {title:"ID", dataIndex:"id", width:60},
        {title:"文件名", dataIndex:"file_name", width:200, ellipsis:true},
        {title:"格式", dataIndex:"report_format", width:60},
        {title:"Snap范围", width:120, render:(_,r)=>`${r.begin_snap} - ${r.end_snap}`},
        {title:"大小", dataIndex:"file_size", width:100, render:v=>v?`${(v/1024).toFixed(1)} KB`:""},
        {title:"状态", dataIndex:"status", width:80, render:(v,r)=> v==="success"?<Tag color="green">成功</Tag>:<Tag color="red" title={r.error_message}>失败</Tag>},
        {title:"时间", dataIndex:"create_time", width:160},
        {title:"操作", width:480, render:(_,r)=><Space wrap size={4}>
          {r.status==="success" && <Button size="small" onClick={() => downloadOriginalReport(r)}>下载原文</Button>}
          {r.status==="success" && <Button size="small" onClick={()=>doDetail(r)}>详情</Button>}
          {r.status==="success" && <Dropdown trigger={["click"]} menu={{items:[
            {key:"md", label:"Markdown (.md)"},
            {key:"pdf", label:"PDF (.pdf)"},
            {key:"txt", label:"纯文本 (.txt)"},
            {key:"doc", label:"Word (.docx)"},
          ], onClick:({key})=>downloadAnalysisExport(r, key)}}><Button size="small" type="primary">导出报告 ▾</Button></Dropdown>}
          {r.status==="success" && <Button size="small" onClick={()=>doEnvAnalyze(r)} loading={analyzing}>环境分析</Button>}
          {r.status==="success" && <Button size="small" danger onClick={()=>doAiAnalyze(r)} loading={analyzing}>AI分析</Button>}
          {r.status==="success" && <Button size="small" onClick={()=>doLocalAnalyze(r)} loading={analyzing}>本地分析</Button>}
          <Popconfirm title="确定删除?" onConfirm={()=>doDelete(r.id)}><Button size="small" danger>删除</Button></Popconfirm>
        </Space>},
      ]} />
    </Card>

    {envData && (() => {
      const env = envData.environment || {};
      const cpuRisk = envData.cpu_risk || {};
      const riskColor = cpuRisk.level === "CRITICAL" ? "red" : cpuRisk.level === "HIGH" ? "orange" : cpuRisk.level === "MEDIUM" ? "gold" : "green";
      const relColor = env.analysis_reliability === "LOW" ? "red" : env.analysis_reliability === "MEDIUM" ? "gold" : "green";
      const sqColor = env.snapshot_quality === "TOO_SHORT" ? "red" : env.snapshot_quality === "TOO_LONG" ? "orange" : "green";
      return (<>
        <Card title="运行环境分析" extra={<Tag color={riskColor}>CPU 风险: {cpuRisk.level}</Tag>}>
          <Descriptions bordered size="small" column={3}>
            <Descriptions.Item label="服务器类型"><Tag color={env.server_type === "UNKNOWN" ? "gold" : "blue"}>{env.server_type_label}</Tag></Descriptions.Item>
            <Descriptions.Item label="置信度"><Tag color={env.server_type_confidence === "HIGH" ? "green" : env.server_type_confidence === "MEDIUM" ? "gold" : "red"}>{env.server_type_confidence}</Tag></Descriptions.Item>
            <Descriptions.Item label="CPU 核数">{env.cpu_count}</Descriptions.Item>
            <Descriptions.Item label="CPU Cores">{env.cpu_cores}</Descriptions.Item>
            <Descriptions.Item label="CPU Sockets">{env.cpu_sockets}</Descriptions.Item>
            <Descriptions.Item label="物理内存">{env.physical_memory_gb} GB</Descriptions.Item>
            <Descriptions.Item label="数据库启动">{env.startup_time}</Descriptions.Item>
            <Descriptions.Item label="运行时长">{env.db_uptime_hours} 小时</Descriptions.Item>
            <Descriptions.Item label="AWR 快照时长">{env.snapshot_elapsed_minutes} 分钟</Descriptions.Item>
            <Descriptions.Item label="AAS">{env.aas}</Descriptions.Item>
            <Descriptions.Item label="AAS/CPU">{env.aas_per_cpu}</Descriptions.Item>
            <Descriptions.Item label="DB CPU/CPU"><span style={{color: env.db_cpu_per_cpu >= 0.7 ? "red" : env.db_cpu_per_cpu >= 0.5 ? "orange" : "green"}}>{env.db_cpu_per_cpu}</span></Descriptions.Item>
            <Descriptions.Item label="快照质量"><Tag color={sqColor}>{env.snapshot_quality}</Tag></Descriptions.Item>
            <Descriptions.Item label="分析可信度"><Tag color={relColor}>{env.analysis_reliability}</Tag></Descriptions.Item>
          </Descriptions>
          {env.server_type_evidence && env.server_type_evidence.length > 0 && <div style={{marginTop:8,fontSize:12,color:"#888"}}>判断依据: {env.server_type_evidence.join(" | ")}</div>}
          {(env.messages || []).map((msg) => <Alert key={stableValueKey("awr-env-message", msg)} style={{marginTop:8}} message={msg} type={env.analysis_reliability === "LOW" ? "error" : "warning"} showIcon />)}
          {(cpuRisk.messages || []).map((msg) => <Alert key={stableValueKey("awr-cpu-risk-message", msg)} style={{marginTop:8}} message={msg} type="warning" showIcon />)}
        </Card>
      </>);
    })()}

    {aiResult && <Card title="AI 智能分析">
      {typeof aiResult === "string" ? <pre style={{whiteSpace:"pre-wrap",maxHeight:500,overflow:"auto"}}>{aiResult}</pre> : <pre style={{whiteSpace:"pre-wrap",maxHeight:500,overflow:"auto"}}>{JSON.stringify(aiResult, null, 2)}</pre>}
    </Card>}
    {localAnalysis && <Card title="本地分析报告">
      <AwrReportView analysis={localAnalysis} recommendations={localAnalysis.recommendations} />
    </Card>}

    <Modal open={detailOpen} onCancel={()=>setDetailOpen(false)} footer={null} width={920}
      title={detailData ? `报告详情 #${detailData.id} - ${detailData.file_name||""}` : "报告详情"}>
      {detailData && (detailData.analysis_results
        ? <AwrReportView analysis={detailData.analysis_results} recommendations={detailData.recommendations} />
        : <Empty description="该报告尚未分析，请先点击「本地分析」或「导出报告」生成分析结果" />)}
    </Modal>
  </Space>);
}


// ====== T-071 Oracle Aggregated Pages ======
