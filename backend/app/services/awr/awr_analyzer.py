#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AWR报告分析引擎模块
基于解析后的AWR报告数据进行性能分析

作者: 青学会MOP技术社区
日期: 2025-04-16
"""

import logging
from typing import TypedDict, Any

# 设置日志
logger = logging.getLogger("青学会MOP技术社区")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

class AWRAnalysisEngine:
    """
    AWR报告分析引擎类
    基于解析后的AWR报告数据进行性能分析
    """
    
    def __init__(self, parsed_data):
        """
        初始化分析引擎
        
        参数:
            parsed_data (dict): 解析后的AWR报告数据
        """
        self.parsed_data = parsed_data
        self.analysis_results = {
            'summary': {},
            'cpu_analysis': {},
            'memory_analysis': {},
            'io_analysis': {},
            'wait_analysis': {},
            'sql_analysis': {},
            'execution_plan_analysis': {},
            'instance_efficiency_analysis': {},
            'potential_issues': [],
            'performance_score': 0
        }
        logger.info("初始化AWR报告分析引擎")
    
    def analyze(self):
        """
        执行全面分析
        
        返回:
            dict: 分析结果
        """
        logger.info("开始分析AWR报告")
        
        self._analyze_summary()
        self._analyze_cpu()
        self._analyze_memory()
        self._analyze_io()
        self._analyze_wait_events()
        self._analyze_sql()
        self._analyze_execution_plans()
        self._analyze_instance_efficiency()
        self._identify_potential_issues()
        self._calculate_performance_score()
        
        logger.info("AWR报告分析完成")
        return self.analysis_results



    """
    def _analyze_summary(self):
        #分析概要信息
        try:
            # 提取数据库信息
            db_info = self.parsed_data.get('database_info', {})
            snapshot_info = self.parsed_data.get('snapshot_info', {})
            
            # 计算关键指标
            db_time = snapshot_info.get('DB Time', '0')
            elapsed_time = snapshot_info.get('Elapsed', '0')
            
            # 提取数值部分
            db_time_value = float(db_time.split()[0]) if isinstance(db_time, str) and ' ' in db_time else float(db_time)
            elapsed_time_value = float(elapsed_time.split()[0]) if isinstance(elapsed_time, str) and ' ' in elapsed_time else float(elapsed_time)
            
            # 计算数据库负载
            db_load = db_time_value / elapsed_time_value if elapsed_time_value > 0 else 0

            # 设置概要信息
            self.analysis_results['summary'] = {
                'database_name': str(db_info.get('DB Name', 'Unknown')),
                'instance_name': str(db_info.get('Instance', 'Unknown')),
                'version': str(db_info.get('Release', 'Unknown')),
                'host': str(db_info.get('Host', 'Unknown')),
                'snapshot_period': f"{snapshot_info.get('Begin Snap Time', 'Unknown')} - {snapshot_info.get('End Snap Time', 'Unknown')}",
                'elapsed_time': f"{elapsed_time_value:.2f} sec",
                'db_time': f"{db_time_value:.2f} sec",
                'db_load': round(db_load, 2)
            }
            logger.info("成功分析概要信息")
        except Exception as e:
            logger.error(f"分析概要信息失败: {str(e)}")
    """


    def _analyze_summary(self) -> None:
        """分析概要信息"""
        try:
            # 提取数据库信息
            db_info = self.parsed_data.get('database_info', {})
            snapshot_info = self.parsed_data.get('snapshot_info', {})

            # 计算关键指标
            db_time = snapshot_info.get('db_time', '0')
            elapsed_time = snapshot_info.get('elapsed_time', '0')

            # 提取数值部分
            db_time_value = 0
            elapsed_time_value = 0
            
            try:
                if isinstance(db_time, (int, float)):
                    db_time_value = float(db_time)
                elif isinstance(db_time, str) and db_time:
                    db_time_value = float(db_time.split()[0]) if ' ' in db_time else float(db_time)
            except (ValueError, IndexError):
                db_time_value = 0
                
            try:
                if isinstance(elapsed_time, (int, float)):
                    elapsed_time_value = float(elapsed_time)
                elif isinstance(elapsed_time, str) and elapsed_time:
                    elapsed_time_value = float(elapsed_time.split()[0]) if ' ' in elapsed_time else float(elapsed_time)
            except (ValueError, IndexError):
                elapsed_time_value = 0

            # 计算数据库负载
            db_load = db_time_value / elapsed_time_value if elapsed_time_value > 0 else 0

            # 提取快照时间
            begin_time = snapshot_info.get('begin_time')
            end_time = snapshot_info.get('end_time')
            
            # 格式化快照时间
            snapshot_period = 'Unknown'
            if begin_time and end_time:
                if hasattr(begin_time, 'strftime') and hasattr(end_time, 'strftime'):
                    snapshot_period = f"{begin_time.strftime('%Y-%m-%d %H:%M')} - {end_time.strftime('%Y-%m-%d %H:%M')}"
                else:
                    snapshot_period = f"{begin_time} - {end_time}"

            # 设置概要信息
            self.analysis_results['summary'] = {
                'database_name': str(db_info.get('database_name', 'Unknown')),
                'instance_name': str(db_info.get('instance_name', 'Unknown')),
                'version': str(db_info.get('release', 'Unknown')),
                'host': str(db_info.get('host_name', 'Unknown')),
                'snapshot_period': snapshot_period,
                'elapsed_time': f"{elapsed_time_value:.2f} sec" if elapsed_time_value > 0 else 'Unknown',
                'db_time': f"{db_time_value:.2f} sec" if db_time_value > 0 else 'Unknown',
                'db_load': round(db_load, 2) if db_load > 0 else 0
            }

            logger.info("成功分析概要信息")
        except Exception as e:
            logger.error(f"分析概要信息失败: {e}")
            # 设置默认值
            self.analysis_results['summary'] = {
                'database_name': 'Unknown',
                'instance_name': 'Unknown', 
                'version': 'Unknown',
                'host': 'Unknown',
                'snapshot_period': 'Unknown',
                'elapsed_time': 'Unknown',
                'db_time': 'Unknown',
                'db_load': 0
            }



    def _analyze_cpu(self):
        """分析CPU使用情况"""
        try:
            # 提取CPU相关数据
            host_cpu = self.parsed_data.get('host_cpu', {})
            instance_cpu = self.parsed_data.get('instance_cpu', {})
            load_profile = self.parsed_data.get('load_profile', {})
            
            # 安全地提取CPU核心数
            cpu_cores = 1
            try:
                if 'CPUs' in host_cpu:
                    cpu_cores = int(str(host_cpu.get('CPUs', '1')).strip())
                else:
                    # 从可能的其他字段获取CPU核心数
                    for key in ['Cores', 'CPU Cores', 'Logical CPUs']:
                        if key in host_cpu:
                            cpu_cores = int(str(host_cpu.get(key, '1')).strip())
                            break
            except (ValueError, TypeError):
                cpu_cores = 1  # 默认值
            
            # 安全地提取CPU使用率
            def safe_extract_percentage(value, default=0.0):
                try:
                    if isinstance(value, (int, float)):
                        return float(value)
                    elif isinstance(value, str):
                        return float(value.replace('%', '').strip())
                    return default
                except (ValueError, AttributeError):
                    return default
            
            user_cpu_pct = safe_extract_percentage(host_cpu.get('%User', '0'))
            system_cpu_pct = safe_extract_percentage(host_cpu.get('%System', '0'))
            idle_cpu_pct = safe_extract_percentage(host_cpu.get('%Idle', '0'))
            wio_cpu_pct = safe_extract_percentage(host_cpu.get('%WIO', '0'))
            
            # 计算总CPU使用率
            total_cpu_usage = user_cpu_pct + system_cpu_pct + wio_cpu_pct
            
            # 提取数据库CPU使用率
            db_cpu_pct = safe_extract_percentage(instance_cpu.get('%Total CPU', '0'))
            busy_cpu_pct = safe_extract_percentage(instance_cpu.get('%Busy CPU', '0'))
            cpu_wait_pct = safe_extract_percentage(instance_cpu.get('%DB time waiting for CPU', '0'))
            
            # 提取每秒DB CPU
            db_cpu_per_sec = 0
            try:
                # 尝试多种可能的键名
                cpu_keys = ['DB CPU(s):', 'DB CPU:', 'CPU used by this session']
                for key in cpu_keys:
                    if key in load_profile:
                        per_sec_data = load_profile[key]
                        if isinstance(per_sec_data, dict):
                            db_cpu_per_sec_str = per_sec_data.get('Per Second', '0')
                        else:
                            db_cpu_per_sec_str = str(per_sec_data)
                        db_cpu_per_sec = float(str(db_cpu_per_sec_str).replace(',', ''))
                        break
            except (ValueError, AttributeError, KeyError):
                db_cpu_per_sec = 0
            
            # 计算CPU利用率（每秒DB CPU / CPU核心数）
            cpu_utilization = (db_cpu_per_sec / cpu_cores) if cpu_cores > 0 else 0

            # 计算CPU评分
            cpu_score = 100
            
            # 基于CPU利用率扣分
            if cpu_utilization > 0.9:
                cpu_score -= 50
            elif cpu_utilization > 0.8:
                cpu_score -= 30
            elif cpu_utilization > 0.7:
                cpu_score -= 15
            elif cpu_utilization > 0.6:
                cpu_score -= 10
            
            # 基于主机CPU使用率扣分
            if total_cpu_usage > 95:
                cpu_score -= 40
            elif total_cpu_usage > 90:
                cpu_score -= 25
            elif total_cpu_usage > 80:
                cpu_score -= 15
            elif total_cpu_usage > 70:
                cpu_score -= 10
            
            # 基于数据库CPU等待时间扣分
            if cpu_wait_pct > 10:
                cpu_score -= 20
            elif cpu_wait_pct > 5:
                cpu_score -= 10
            
            cpu_score = max(0, min(100, cpu_score))
            
            # 判断是否存在CPU瓶颈
            cpu_bottleneck = (
                cpu_utilization > 0.8 or 
                total_cpu_usage > 90 or 
                cpu_wait_pct > 10
            )

            # 设置CPU分析结果
            self.analysis_results['cpu_analysis'] = {
                'cpu_cores': cpu_cores,
                'host_cpu_usage': {
                    'user': round(user_cpu_pct, 2),
                    'system': round(system_cpu_pct, 2),
                    'idle': round(idle_cpu_pct, 2),
                    'wio': round(wio_cpu_pct, 2),
                    'total': round(total_cpu_usage, 2)
                },
                'db_cpu_usage': {
                    'total_cpu_pct': round(db_cpu_pct, 2),
                    'busy_cpu_pct': round(busy_cpu_pct, 2),
                    'per_second': round(db_cpu_per_sec, 2),
                    'cpu_wait_pct': round(cpu_wait_pct, 2)
                },
                'cpu_utilization': round(cpu_utilization * 100, 2),
                'cpu_bottleneck': cpu_bottleneck,
                'score': cpu_score,
                'recommendations': self._get_cpu_recommendations(cpu_utilization, total_cpu_usage, cpu_wait_pct)
            }

            logger.info("成功分析CPU使用情况")
        except Exception as e:
            logger.error(f"分析CPU使用情况失败: {str(e)}")
            # 设置默认值
            self.analysis_results['cpu_analysis'] = {
                'cpu_cores': 1,
                'host_cpu_usage': {'user': 0, 'system': 0, 'idle': 100, 'wio': 0, 'total': 0},
                'db_cpu_usage': {'total_cpu_pct': 0, 'busy_cpu_pct': 0, 'per_second': 0, 'cpu_wait_pct': 0},
                'cpu_utilization': 0,
                'cpu_bottleneck': False,
                'score': 100,
                'recommendations': []
            }
    
    def _get_cpu_recommendations(self, cpu_utilization, total_cpu_usage, cpu_wait_pct):
        """生成CPU相关的优化建议"""
        recommendations = []
        
        if cpu_utilization > 0.9:
            recommendations.extend([
                "🔥 严重：数据库CPU利用率超过90%，存在严重性能瓶颈",
                "⚡ 立即优化TOP CPU消耗SQL语句",
                "📊 检查执行计划，避免全表扫描和笛卡尔积",
                "🔧 考虑添加合适的索引"
            ])
        elif cpu_utilization > 0.8:
            recommendations.extend([
                "⚠️  警告：数据库CPU利用率过高(>80%)",
                "🔍 分析TOP SQL的执行频率和复杂度",
                "📈 监控CPU趋势，制定扩容计划"
            ])
        elif cpu_utilization > 0.6:
            recommendations.append("💡 数据库CPU使用率较高，建议定期监控和优化")
        
        if total_cpu_usage > 95:
            recommendations.extend([
                "🚨 主机CPU使用率危险(>95%)，系统可能不稳定",
                "🛠️  立即检查系统进程，考虑紧急扩容"
            ])
        elif total_cpu_usage > 90:
            recommendations.extend([
                "⚠️  主机CPU使用率过高，影响数据库性能",
                "💻 检查操作系统级别的CPU消耗进程"
            ])
        
        if cpu_wait_pct > 15:
            recommendations.extend([
                "⏱️  CPU等待时间过长(>15%)，存在严重调度问题",
                "🔄 检查CPU绑定策略和操作系统调度参数"
            ])
        elif cpu_wait_pct > 10:
            recommendations.append("⏰ CPU等待时间较长，检查资源竞争情况")
        
        if cpu_utilization > 0.6 and total_cpu_usage < 70:
            recommendations.extend([
                "🎯 数据库CPU高但主机CPU正常，问题集中在数据库层",
                "🔧 重点优化数据库参数和SQL语句",
                "📋 检查是否存在低效的数据库操作"
            ])
        
        # 添加通用CPU优化建议
        if cpu_utilization > 0.5 or total_cpu_usage > 80:
            recommendations.extend([
                "📝 建议启用SQL绑定变量减少硬解析",
                "⚙️  检查数据库参数cursor_sharing设置",
                "🔍 使用AWR报告分析TOP SQL执行计划"
            ])
        
        return recommendations

    def _analyze_memory(self):
        """分析内存使用情况"""
        try:
            # 提取内存相关数据
            instance_efficiency = self.parsed_data.get('instance_efficiency', {})
            load_profile = self.parsed_data.get('load_profile', {})
            
            # 安全提取百分比数据
            def safe_extract_percentage(data, key, default=0.0):
                try:
                    value = data.get(key, default)
                    if isinstance(value, (int, float)):
                        return float(value)
                    elif isinstance(value, str):
                        return float(value.replace('%', '').strip())
                    return default
                except (ValueError, AttributeError):
                    return default
            
            # 提取缓冲区命中率
            buffer_hit_ratio = safe_extract_percentage(instance_efficiency, 'Buffer  Hit   %:', 0)
            if buffer_hit_ratio == 0:
                buffer_hit_ratio = safe_extract_percentage(instance_efficiency, 'Buffer Hit %:', 0)
            
            # 提取库缓存命中率
            library_hit_ratio = safe_extract_percentage(instance_efficiency, 'Library Hit   %:', 0)
            if library_hit_ratio == 0:
                library_hit_ratio = safe_extract_percentage(instance_efficiency, 'Library Hit %:', 0)
            
            # 提取内存排序百分比
            in_memory_sort_pct = safe_extract_percentage(instance_efficiency, 'In-memory Sort %:', 0)
            
            # 提取数据库块大小相关的指标
            redo_nowait_pct = safe_extract_percentage(instance_efficiency, 'Redo NoWait %:', 100)
            
            # 提取内存相关的负载指标
            logical_reads_per_sec = 0
            physical_reads_per_sec = 0
            
            try:
                # 逻辑读取
                if 'Logical read (blocks):' in load_profile:
                    logical_reads_str = load_profile['Logical read (blocks):'].get('Per Second', '0')
                    logical_reads_per_sec = float(str(logical_reads_str).replace(',', ''))
                
                # 物理读取
                if 'Physical read (blocks):' in load_profile:
                    physical_reads_str = load_profile['Physical read (blocks):'].get('Per Second', '0')
                    physical_reads_per_sec = float(str(physical_reads_str).replace(',', ''))
            except (ValueError, KeyError, AttributeError):
                pass
            
            # 计算实际缓冲区命中率
            actual_buffer_hit_ratio = 0
            if logical_reads_per_sec > 0:
                actual_buffer_hit_ratio = ((logical_reads_per_sec - physical_reads_per_sec) / logical_reads_per_sec) * 100
                actual_buffer_hit_ratio = max(0, min(100, actual_buffer_hit_ratio))
            
            # 计算内存评分
            memory_score = 100
            
            # 缓冲区命中率评分
            effective_buffer_hit = max(buffer_hit_ratio, actual_buffer_hit_ratio)
            if effective_buffer_hit < 85:
                memory_score -= 40
            elif effective_buffer_hit < 90:
                memory_score -= 30
            elif effective_buffer_hit < 95:
                memory_score -= 15
            elif effective_buffer_hit < 98:
                memory_score -= 10
            
            # 库缓存命中率评分
            if library_hit_ratio < 85:
                memory_score -= 35
            elif library_hit_ratio < 90:
                memory_score -= 25
            elif library_hit_ratio < 95:
                memory_score -= 15
            elif library_hit_ratio < 98:
                memory_score -= 8
            
            # 内存排序评分
            if in_memory_sort_pct < 80:
                memory_score -= 25
            elif in_memory_sort_pct < 90:
                memory_score -= 15
            elif in_memory_sort_pct < 95:
                memory_score -= 10
            elif in_memory_sort_pct < 98:
                memory_score -= 5
            
            # Redo日志等待评分
            if redo_nowait_pct < 95:
                memory_score -= 15
            elif redo_nowait_pct < 98:
                memory_score -= 8
            
            memory_score = max(0, min(100, memory_score))
            
            # 判断内存问题的严重程度
            memory_issues = {
                'critical_buffer_hit': effective_buffer_hit < 85,
                'low_buffer_hit': effective_buffer_hit < 95,
                'critical_library_hit': library_hit_ratio < 85,
                'low_library_hit': library_hit_ratio < 95,
                'critical_memory_sort': in_memory_sort_pct < 80,
                'low_memory_sort': in_memory_sort_pct < 95,
                'redo_contention': redo_nowait_pct < 95
            }
            
            # 生成内存优化建议
            memory_recommendations = self._get_memory_recommendations(
                effective_buffer_hit, library_hit_ratio, in_memory_sort_pct, redo_nowait_pct, memory_issues
            )
            
            # 设置内存分析结果
            self.analysis_results['memory_analysis'] = {
                'buffer_hit_ratio': round(buffer_hit_ratio, 2),
                'actual_buffer_hit_ratio': round(actual_buffer_hit_ratio, 2),
                'effective_buffer_hit_ratio': round(effective_buffer_hit, 2),
                'library_hit_ratio': round(library_hit_ratio, 2),
                'in_memory_sort_pct': round(in_memory_sort_pct, 2),
                'redo_nowait_pct': round(redo_nowait_pct, 2),
                'logical_reads_per_sec': logical_reads_per_sec,
                'physical_reads_per_sec': physical_reads_per_sec,
                'memory_issues': memory_issues,
                'score': memory_score,
                'recommendations': memory_recommendations
            }
            
            logger.info("成功分析内存使用情况")
        except Exception as e:
            logger.error(f"分析内存使用情况失败: {str(e)}")
            # 设置默认值
            self.analysis_results['memory_analysis'] = {
                'buffer_hit_ratio': 0,
                'actual_buffer_hit_ratio': 0,
                'effective_buffer_hit_ratio': 0,
                'library_hit_ratio': 0,
                'in_memory_sort_pct': 0,
                'redo_nowait_pct': 100,
                'logical_reads_per_sec': 0,
                'physical_reads_per_sec': 0,
                'memory_issues': {},
                'score': 100,
                'recommendations': []
            }
    
    def _get_memory_recommendations(self, buffer_hit, library_hit, memory_sort, redo_nowait, issues):
        """生成内存相关的优化建议"""
        recommendations = []
        
        # 缓冲区命中率建议
        if issues.get('critical_buffer_hit', False):
            recommendations.extend([
                "🚨 紧急：缓冲区命中率低于85%，严重影响性能",
                "💰 立即增加db_cache_size参数",
                "🔍 检查是否存在大量全表扫描操作",
                "⚡ 优先优化高物理读取的SQL语句"
            ])
        elif issues.get('low_buffer_hit', False):
            recommendations.extend([
                "⚠️  缓冲区命中率低于95%，建议优化",
                "📊 监控缓冲区使用情况，适当调整大小",
                "🔧 检查索引的有效性和使用情况"
            ])
        
        # 库缓存命中率建议
        if issues.get('critical_library_hit', False):
            recommendations.extend([
                "🚨 严重：库缓存命中率低于85%，存在大量硬解析",
                "📝 强制使用绑定变量减少硬解析",
                "💰 增加shared_pool_size参数",
                "🔄 考虑启用cursor_sharing=FORCE"
            ])
        elif issues.get('low_library_hit', False):
            recommendations.extend([
                "⚠️  库缓存命中率低于95%，存在解析问题",
                "📝 检查应用程序是否使用绑定变量",
                "📊 监控共享池使用情况"
            ])
        
        # 内存排序建议
        if issues.get('critical_memory_sort', False):
            recommendations.extend([
                "🚨 严重：内存排序比例低于80%，大量磁盘排序",
                "💰 立即增加pga_aggregate_target参数",
                "🔍 检查大量排序操作的SQL语句",
                "⚡ 优化ORDER BY和GROUP BY语句"
            ])
        elif issues.get('low_memory_sort', False):
            recommendations.extend([
                "⚠️  内存排序比例低于95%，存在磁盘排序",
                "📊 监控PGA内存使用情况",
                "🔧 检查排序操作的效率"
            ])
        
        # Redo日志等待建议
        if issues.get('redo_contention', False):
            recommendations.extend([
                "⚠️  Redo日志等待问题，影响事务提交性能",
                "💾 检查Redo日志文件的I/O性能",
                "⚙️  考虑增加Redo日志组数量或大小"
            ])
        
        # 添加通用内存优化建议
        if buffer_hit < 98 or library_hit < 98 or memory_sort < 98:
            recommendations.extend([
                "📋 定期监控内存使用趋势和命中率",
                "📈 建立内存性能基线和告警机制",
                "🔧 考虑使用自动内存管理(ASMM)"
            ])
        
        return recommendations
    
    def _analyze_io(self):
        """分析IO使用情况"""
        try:
            load_profile = self.parsed_data.get('load_profile', {})
            wait_events = self.parsed_data.get('top_timed_events', [])
            
            # 安全提取数值
            def safe_extract_number(data, key, per_sec_key='Per Second', default=0):
                try:
                    if key in data:
                        value_data = data[key]
                        if isinstance(value_data, dict):
                            value_str = value_data.get(per_sec_key, '0')
                        else:
                            value_str = str(value_data)
                        return float(str(value_str).replace(',', ''))
                    return default
                except (ValueError, AttributeError, KeyError):
                    return default
            
            # 提取IO指标
            physical_reads_per_sec = safe_extract_number(load_profile, 'Physical read (blocks):')
            physical_writes_per_sec = safe_extract_number(load_profile, 'Physical write (blocks):')
            logical_reads_per_sec = safe_extract_number(load_profile, 'Logical read (blocks):')
            
            read_io_requests_per_sec = safe_extract_number(load_profile, 'Read IO requests:')
            write_io_requests_per_sec = safe_extract_number(load_profile, 'Write IO requests:')
            
            # 提取更多 IO 相关指标
            redo_size_per_sec = safe_extract_number(load_profile, 'Redo size (bytes):')
            user_calls_per_sec = safe_extract_number(load_profile, 'User calls:')
            
            # 计算派生指标
            total_io_requests_per_sec = read_io_requests_per_sec + write_io_requests_per_sec
            total_physical_io = physical_reads_per_sec + physical_writes_per_sec
            
            # 计算读写比例
            read_write_ratio = read_io_requests_per_sec / write_io_requests_per_sec if write_io_requests_per_sec > 0 else float('inf')
            
            # 计算缓冲区命中率(基于实际IO数据)
            buffer_hit_ratio_actual = 0
            if logical_reads_per_sec > 0:
                buffer_hit_ratio_actual = ((logical_reads_per_sec - physical_reads_per_sec) / logical_reads_per_sec) * 100
                buffer_hit_ratio_actual = max(0, min(100, buffer_hit_ratio_actual))
            
            # 计算平均IO大小
            avg_read_size_kb = 0
            avg_write_size_kb = 0
            if read_io_requests_per_sec > 0:
                avg_read_size_kb = (physical_reads_per_sec * 8) / read_io_requests_per_sec  # 假设8KB块大小
            if write_io_requests_per_sec > 0:
                avg_write_size_kb = (physical_writes_per_sec * 8) / write_io_requests_per_sec
            
            # 分析IO等待事件
            io_wait_events = []
            total_io_wait_time = 0
            
            for event in wait_events:
                if isinstance(event, dict) and 'Event' in event:
                    event_name = event.get('Event', '').lower()
                    wait_class = event.get('Wait Class', '').lower()
                    
                    if 'i/o' in wait_class or any(keyword in event_name for keyword in 
                        ['db file', 'log file', 'direct path', 'control file', 'read', 'write']):
                        
                        try:
                            wait_time_pct = event.get('% DB time', '0')
                            if isinstance(wait_time_pct, str) and wait_time_pct:
                                wait_time_pct = float(wait_time_pct.split()[0])
                            else:
                                wait_time_pct = float(wait_time_pct)
                            
                            total_io_wait_time += wait_time_pct
                            io_wait_events.append({
                                'event_name': event.get('Event', ''),
                                'wait_time_pct': wait_time_pct,
                                'wait_class': event.get('Wait Class', '')
                            })
                        except (ValueError, TypeError):
                            pass
            
            # 按等待时间排序
            io_wait_events.sort(key=lambda x: x['wait_time_pct'], reverse=True)
            
            # IO性能评分算法
            io_score = 100
            
            # 基于IO请求数量评分
            if total_io_requests_per_sec > 5000:
                io_score -= 40
            elif total_io_requests_per_sec > 3000:
                io_score -= 30
            elif total_io_requests_per_sec > 2000:
                io_score -= 20
            elif total_io_requests_per_sec > 1000:
                io_score -= 15
            elif total_io_requests_per_sec > 500:
                io_score -= 10
            
            # 基于物理IO量评分
            if total_physical_io > 20000:
                io_score -= 35
            elif total_physical_io > 15000:
                io_score -= 25
            elif total_physical_io > 10000:
                io_score -= 20
            elif total_physical_io > 5000:
                io_score -= 15
            
            # 基于IO等待时间评分
            if total_io_wait_time > 50:
                io_score -= 40
            elif total_io_wait_time > 30:
                io_score -= 25
            elif total_io_wait_time > 20:
                io_score -= 15
            elif total_io_wait_time > 10:
                io_score -= 10
            
            # 基于Redo大小评分(过多的Redo可能导致IO压力)
            if redo_size_per_sec > 50 * 1024 * 1024:  # 50MB/s
                io_score -= 15
            elif redo_size_per_sec > 20 * 1024 * 1024:  # 20MB/s
                io_score -= 10
            
            io_score = max(0, min(100, io_score))
            
            # 判断IO问题类型
            io_issues = {
                'high_io_requests': total_io_requests_per_sec > 2000,
                'critical_io_requests': total_io_requests_per_sec > 5000,
                'high_physical_io': total_physical_io > 10000,
                'critical_physical_io': total_physical_io > 20000,
                'io_wait_dominant': total_io_wait_time > 30,
                'read_heavy': read_write_ratio > 10,
                'write_heavy': read_write_ratio < 0.5,
                'large_redo': redo_size_per_sec > 20 * 1024 * 1024,
                'small_io_size': avg_read_size_kb < 32 and avg_read_size_kb > 0
            }
            
            # 生成IO优化建议
            io_recommendations = self._get_io_recommendations(
                total_io_requests_per_sec, total_physical_io, total_io_wait_time, 
                read_write_ratio, redo_size_per_sec, io_issues, io_wait_events
            )
            
            # 设置IO分析结果
            self.analysis_results['io_analysis'] = {
                'physical_reads_per_sec': round(physical_reads_per_sec, 2),
                'physical_writes_per_sec': round(physical_writes_per_sec, 2),
                'logical_reads_per_sec': round(logical_reads_per_sec, 2),
                'read_io_requests_per_sec': round(read_io_requests_per_sec, 2),
                'write_io_requests_per_sec': round(write_io_requests_per_sec, 2),
                'total_io_requests_per_sec': round(total_io_requests_per_sec, 2),
                'total_physical_io': round(total_physical_io, 2),
                'read_write_ratio': round(read_write_ratio, 2) if read_write_ratio != float('inf') else 'Read Only',
                'buffer_hit_ratio_actual': round(buffer_hit_ratio_actual, 2),
                'avg_read_size_kb': round(avg_read_size_kb, 2),
                'avg_write_size_kb': round(avg_write_size_kb, 2),
                'redo_size_per_sec': round(redo_size_per_sec / (1024*1024), 2),  # MB/s
                'total_io_wait_time_pct': round(total_io_wait_time, 2),
                'top_io_wait_events': io_wait_events[:5],
                'io_issues': io_issues,
                'io_bottleneck': total_io_requests_per_sec > 1000 or total_io_wait_time > 20,
                'score': io_score,
                'recommendations': io_recommendations
            }
            
            logger.info("成功分析IO使用情况")
        except Exception as e:
            logger.error(f"分析IO使用情况失败: {str(e)}")
            # 设置默认值
            self.analysis_results['io_analysis'] = {
                'physical_reads_per_sec': 0,
                'physical_writes_per_sec': 0,
                'logical_reads_per_sec': 0,
                'read_io_requests_per_sec': 0,
                'write_io_requests_per_sec': 0,
                'total_io_requests_per_sec': 0,
                'total_physical_io': 0,
                'read_write_ratio': 0,
                'buffer_hit_ratio_actual': 0,
                'avg_read_size_kb': 0,
                'avg_write_size_kb': 0,
                'redo_size_per_sec': 0,
                'total_io_wait_time_pct': 0,
                'top_io_wait_events': [],
                'io_issues': {},
                'io_bottleneck': False,
                'score': 100,
                'recommendations': []
            }
    
    def _get_io_recommendations(self, total_io_requests, total_physical_io, io_wait_time, 
                               read_write_ratio, redo_size, issues, wait_events):
        """生成IO相关的优化建议"""
        recommendations = []
        
        # 高IO请求建议
        if issues.get('critical_io_requests', False):
            recommendations.extend([
                "🚨 紧急：IO请求数超过5000/秒，系统存在严重IO瓶颈",
                "💾 立即检查存储子系统性能和配置",
                "⚡ 优先优化高IO消耗的SQL语句",
                "🔍 检查是否存在不必要的全表扫描"
            ])
        elif issues.get('high_io_requests', False):
            recommendations.extend([
                "⚠️  IO请求数过高(>2000/秒)，存在性能风险",
                "📊 监控IO趋势，制定优化计划",
                "🔧 检查索引使用效率和表连接策略"
            ])
        
        # 物理IO建议
        if issues.get('critical_physical_io', False):
            recommendations.extend([
                "🚨 物理IO量超过20000块/秒，系统存储压力巨大",
                "💰 考虑升级到更快的存储设备(SSD/NVMe)",
                "🛠️  检查存储的RAID配置和缓存设置"
            ])
        elif issues.get('high_physical_io', False):
            recommendations.extend([
                "⚠️  物理IO量较高(>10000块/秒)",
                "📊 监控存储设备的利用率和响应时间",
                "🔧 优化数据库缓冲区大小和命中率"
            ])
        
        # IO等待事件建议
        if issues.get('io_wait_dominant', False):
            recommendations.extend([
                "🚨 IO等待事件占数据库时间>30%，主要瓶颈在IO",
                "🔍 分析具体的IO等待事件类型",
                "💾 检查数据文件和日志文件的放置策略"
            ])
            
            # 针对具体的IO等待事件给出建议
            for event in wait_events[:3]:
                event_name = event['event_name'].lower()
                if 'db file sequential read' in event_name:
                    recommendations.append("📄 存在大量单块读取，检查索引扫描和随机访问")
                elif 'db file scattered read' in event_name:
                    recommendations.append("📄 存在大量多块读取，检查全表扫描和范围扫描")
                elif 'log file sync' in event_name:
                    recommendations.append("📝 日志文件同步等待过高，检查Redo日志性能")
                elif 'direct path read' in event_name or 'direct path write' in event_name:
                    recommendations.append("📦 存在大量直接路径IO，检查大表操作和并行处理")
        
        # 读写比例建议
        if issues.get('read_heavy', False):
            recommendations.extend([
                "📚 系统以读操作为主(读写比>10:1)",
                "📈 考虑优化查询缓存和读取策略",
                "🔍 检查是否可以使用物化视图减少读取"
            ])
        elif issues.get('write_heavy', False):
            recommendations.extend([
                "📝 系统以写操作为主(读写比<0.5:1)",
                "💾 重点优化写入性能和Redo日志配置",
                "⚙️  考虑使用批量处理减少写入次数"
            ])
        
        # Redo日志大小建议
        if issues.get('large_redo', False):
            recommendations.extend([
                "📝 Redo日志产生量过大(>20MB/s)",
                "🔄 检查是否存在不必要的DML操作",
                "⚙️  考虑优化事务大小和提交频率"
            ])
        
        # 小IO大小建议
        if issues.get('small_io_size', False):
            recommendations.extend([
                "📄 IO操作块大小较小(<32KB)，存在碎片化问题",
                "🛠️  检查数据库块大小配置和数据布局",
                "🔧 考虑使用更大的读取块大小"
            ])
        
        # 添加通用IO优化建议
        if total_io_requests > 1000 or total_physical_io > 5000:
            recommendations.extend([
                "📋 建立 IO 性能基线和监控体系",
                "📈 定期分析AWR中的IO相关统计信息",
                "🔧 考虑使用自动存储管理(ASM)优化IO分布"
            ])
        
        return recommendations
    
    def _analyze_wait_events(self):
        """分析等待事件"""
        try:
            # 提取等待事件数据
            top_timed_events = self.parsed_data.get('top_timed_events', [])
            #wait_events = self.parsed_data.get('wait_events', [])
            
            # 分析前5个耗时事件
            top_events = []
            for event in top_timed_events:
                if isinstance(event, dict) and 'Event' in event:
                    event_name = event.get('Event', '')
                    if event_name and event_name not in ['----------------------------------------- ------------ ----------- ------ ------ ----------']:
                        wait_time = event.get('Total Wait Time (sec)', '0')
                        #wait_time = float(wait_time.replace(',', '')) if isinstance(wait_time, str) else 0

                        # 安全地转换DB时间百分比
                        try:
                            db_time_pct_raw = event.get('% DB time', '0')
                            if isinstance(db_time_pct_raw, (int, float)):
                                db_time_pct = float(db_time_pct_raw)
                            elif isinstance(db_time_pct_raw, str) and db_time_pct_raw.strip():
                                # 移除百分号和其他字符，取第一个数字
                                cleaned_value = db_time_pct_raw.replace('%', '').strip()
                                if ' ' in cleaned_value:
                                    cleaned_value = cleaned_value.split()[0]
                                db_time_pct = float(cleaned_value) if cleaned_value else 0.0
                            else:
                                db_time_pct = 0.0
                        except (ValueError, AttributeError):
                            db_time_pct = 0.0
                        wait_class = event.get('Wait Class', '')
                        
                        top_events.append({
                            'event_name': event_name,
                            'wait_time': wait_time,
                            'db_time_pct': db_time_pct,
                            'wait_class': wait_class
                        })
            
            # 按等待时间百分比排序
            top_events.sort(key=lambda x: x['db_time_pct'], reverse=True)

            # 分析等待类别分布
            wait_class_distribution = {}
            for event in top_events:
                wait_class = event.get('wait_class', 'Other')
                if wait_class and wait_class != 'Other':
                    if wait_class not in wait_class_distribution:
                        wait_class_distribution[wait_class] = 0
                    wait_class_distribution[wait_class] += event.get('db_time_pct', 0)
                    
            # 安全的条件检查函数
            def safe_check_wait_condition(condition_func):
                try:
                    return any(condition_func(event) for event in top_events if isinstance(event, dict))
                except (TypeError, ValueError, KeyError):
                    return False

            # 设置等待事件分析结果
            self.analysis_results['wait_analysis'] = {
                'top_events': top_events[:5],  # 只保留前5个用于显示
                'wait_class_distribution': wait_class_distribution,
                'cpu_bound': safe_check_wait_condition(
                    lambda e: e.get('event_name', '') == 'DB CPU' and e.get('db_time_pct', 0) > 50
                ),
                'io_bound': safe_check_wait_condition(
                    lambda e: ('User I/O' in e.get('wait_class', '') or 
                              any(keyword in e.get('event_name', '').lower() 
                                  for keyword in ['db file', 'direct path', 'log file sync'])) 
                              and e.get('db_time_pct', 0) > 20
                ),
                'concurrency_issues': safe_check_wait_condition(
                    lambda e: ('Concurrency' in e.get('wait_class', '') or
                              any(keyword in e.get('event_name', '').lower()
                                  for keyword in ['enq:', 'latch', 'buffer busy', 'row lock']))
                              and e.get('db_time_pct', 0) > 10
                ),
                'network_issues': safe_check_wait_condition(
                    lambda e: ('Network' in e.get('wait_class', '') or
                              'SQL*Net' in e.get('event_name', ''))
                              and e.get('db_time_pct', 0) > 10
                ),
                'total_analyzed_events': len(top_events)
            }
            
            logger.info("成功分析等待事件")
        except Exception as e:
            logger.error(f"分析等待事件失败: {str(e)}")
    
    def _analyze_sql(self):
        """分析SQL语句性能"""
        try:
            # 提取SQL统计数据
            sql_stats = self.parsed_data.get('sql_stats', [])
            
            # 分析SQL语句
            top_cpu_sql = []
            top_buffer_gets_sql = []
            top_executions_sql = []
            top_elapsed_time_sql = []
            
            for sql in sql_stats:
                if isinstance(sql, dict) and 'SQL Id' in sql:
                    sql_id = sql.get('SQL Id', '')
                    sql_text = sql.get('SQL Text', '')
                    
                    # 提取关键指标
                    cpu_time = float(sql.get('CPU Time(s)', '0').replace(',', '')) if 'CPU Time(s)' in sql else 0
                    buffer_gets = float(sql.get('Buffer Gets', '0').replace(',', '')) if 'Buffer Gets' in sql else 0
                    executions = float(sql.get('Executions', '0').replace(',', '')) if 'Executions' in sql else 0
                    elapsed_time = float(sql.get('Elapsed  Time (s)', '0').replace(',', '')) if 'Elapsed  Time (s)' in sql else 0
                    
                    # 计算每次执行的指标
                    cpu_per_exec = cpu_time / executions if executions > 0 else 0
                    buffer_gets_per_exec = buffer_gets / executions if executions > 0 else 0
                    elapsed_per_exec = elapsed_time / executions if executions > 0 else 0
                    
                    sql_info = {
                        'sql_id': sql_id,
                        'sql_text': sql_text,
                        'cpu_time': cpu_time,
                        'buffer_gets': buffer_gets,
                        'executions': executions,
                        'elapsed_time': elapsed_time,
                        'cpu_per_exec': cpu_per_exec,
                        'buffer_gets_per_exec': buffer_gets_per_exec,
                        'elapsed_per_exec': elapsed_per_exec
                    }
                    
                    # 添加到相应列表
                    top_cpu_sql.append(sql_info)
                    top_buffer_gets_sql.append(sql_info)
                    top_executions_sql.append(sql_info)
                    top_elapsed_time_sql.append(sql_info)
            
            # 按不同指标排序
            top_cpu_sql.sort(key=lambda x: x['cpu_time'], reverse=True)
            top_buffer_gets_sql.sort(key=lambda x: x['buffer_gets'], reverse=True)
            top_executions_sql.sort(key=lambda x: x['executions'], reverse=True)
            top_elapsed_time_sql.sort(key=lambda x: x['elapsed_time'], reverse=True)
            
            # 设置SQL分析结果
            self.analysis_results['sql_analysis'] = {
                'top_cpu_sql': top_cpu_sql[:5],
                'top_buffer_gets_sql': top_buffer_gets_sql[:5],
                'top_executions_sql': top_executions_sql[:5],
                'top_elapsed_time_sql': top_elapsed_time_sql[:5]
            }
            
            logger.info("成功分析SQL语句性能")
        except Exception as e:
            logger.error(f"分析SQL语句性能失败: {str(e)}")
    
    def _analyze_execution_plans(self):
        """分析执行计划"""
        try:
            # 提取执行计划数据
            execution_plans = self.parsed_data.get('execution_plans', [])
            
            if not execution_plans:
                logger.info("未发现执行计划数据")
                self.analysis_results['execution_plan_analysis'] = {
                    'total_plans': 0,
                    'inefficient_operations': [],
                    'optimization_suggestions': [],
                    'cost_analysis': {},
                    'recommendations': []
                }
                return
            
            # 分析执行计划
            total_plans = len(execution_plans)
            inefficient_operations = []
            high_cost_plans = []
            optimization_suggestions = []
            
            for plan in execution_plans:
                sql_id = plan.get('sql_id', 'Unknown')
                sql_text = plan.get('sql_text', '')
                plan_hash_value = plan.get('plan_hash_value', '')
                plan_steps = plan.get('plan_steps', [])
                
                # 分析执行计划步骤
                plan_issues = self._analyze_plan_steps(plan_steps, sql_id)
                if plan_issues:
                    inefficient_operations.extend(plan_issues)
                
                # 计算计划成本
                total_cost = self._calculate_plan_cost(plan_steps)
                if total_cost > 10000:  # 高成本阈值
                    high_cost_plans.append({
                        'sql_id': sql_id,
                        'sql_text': sql_text[:100] + '...' if len(sql_text) > 100 else sql_text,
                        'plan_hash_value': plan_hash_value,
                        'total_cost': total_cost
                    })
                
                # 生成优化建议
                suggestions = self._generate_plan_optimization_suggestions(plan_steps, sql_id, sql_text)
                if suggestions:
                    optimization_suggestions.extend(suggestions)
            
            # 按问题严重程度排序
            inefficient_operations.sort(key=lambda x: x.get('severity_score', 0), reverse=True)
            high_cost_plans.sort(key=lambda x: x['total_cost'], reverse=True)
            
            # 成本分析统计
            cost_ranges = {'low': 0, 'medium': 0, 'high': 0, 'very_high': 0}
            for plan in execution_plans:
                cost = self._calculate_plan_cost(plan.get('plan_steps', []))
                if cost < 100:
                    cost_ranges['low'] += 1
                elif cost < 1000:
                    cost_ranges['medium'] += 1
                elif cost < 10000:
                    cost_ranges['high'] += 1
                else:
                    cost_ranges['very_high'] += 1
            
            # 生成综合建议
            comprehensive_recommendations = self._get_execution_plan_recommendations(
                inefficient_operations, high_cost_plans, cost_ranges
            )
            
            # 设置执行计划分析结果
            self.analysis_results['execution_plan_analysis'] = {
                'total_plans': total_plans,
                'inefficient_operations': inefficient_operations[:10],  # 只保留前10个
                'high_cost_plans': high_cost_plans[:5],  # 只保留前5个
                'optimization_suggestions': optimization_suggestions[:15],  # 只保留前15个
                'cost_analysis': cost_ranges,
                'recommendations': comprehensive_recommendations
            }
            
            logger.info(f"成功分析执行计划，共分析 {total_plans} 个计划")
        except Exception as e:
            logger.error(f"分析执行计划失败: {str(e)}")
            # 设置默认值
            self.analysis_results['execution_plan_analysis'] = {
                'total_plans': 0,
                'inefficient_operations': [],
                'high_cost_plans': [],
                'optimization_suggestions': [],
                'cost_analysis': {'low': 0, 'medium': 0, 'high': 0, 'very_high': 0},
                'recommendations': []
            }
    
    def _analyze_plan_steps(self, plan_steps, sql_id):
        """分析执行计划步骤，识别性能问题"""
        issues = []
        
        for step in plan_steps:
            if not isinstance(step, dict):
                continue
                
            operation = step.get('operation', '').upper()
            object_name = step.get('object_name', '')
            cost = self._safe_int(step.get('cost', '0'))
            cardinality = self._safe_int(step.get('cardinality', '0'))
            
            # 检查全表扫描
            if 'TABLE ACCESS FULL' in operation:
                severity_score = 90 + min(cost // 100, 10)  # 基础90分，成本越高分数越高
                issues.append({
                    'sql_id': sql_id,
                    'operation': operation,
                    'object_name': object_name,
                    'issue_type': '全表扫描',
                    'description': f"对表 {object_name} 执行全表扫描，成本: {cost}",
                    'severity': 'High' if cost > 1000 else 'Medium',
                    'severity_score': severity_score,
                    'recommendation': '考虑添加合适的索引或优化WHERE条件'
                })
            
            # 检查笛卡尔积
            elif 'MERGE JOIN CARTESIAN' in operation or 'NESTED LOOPS' in operation:
                if cardinality > 100000:  # 大结果集的笛卡尔积
                    issues.append({
                        'sql_id': sql_id,
                        'operation': operation,
                        'object_name': object_name,
                        'issue_type': '笛卡尔积风险',
                        'description': f"可能存在笛卡尔积，预计行数: {cardinality}",
                        'severity': 'Critical',
                        'severity_score': 95,
                        'recommendation': '检查JOIN条件，确保连接条件正确'
                    })
            
            # 检查排序操作
            elif 'SORT' in operation:
                if cost > 5000:
                    issues.append({
                        'sql_id': sql_id,
                        'operation': operation,
                        'object_name': object_name,
                        'issue_type': '高成本排序',
                        'description': f"排序操作成本较高: {cost}，行数: {cardinality}",
                        'severity': 'Medium',
                        'severity_score': 60 + min(cost // 1000, 30),
                        'recommendation': '考虑使用索引避免排序或优化ORDER BY条件'
                    })
            
            # 检查索引扫描效率
            elif 'INDEX' in operation and 'RANGE SCAN' in operation:
                if cardinality > 50000:  # 大范围索引扫描
                    issues.append({
                        'sql_id': sql_id,
                        'operation': operation,
                        'object_name': object_name,
                        'issue_type': '低效索引扫描',
                        'description': f"索引范围扫描返回大量数据: {cardinality} 行",
                        'severity': 'Medium',
                        'severity_score': 50 + min(cardinality // 10000, 40),
                        'recommendation': '检查WHERE条件的选择性，考虑复合索引'
                    })
            
            # 检查哈希连接
            elif 'HASH JOIN' in operation:
                if cost > 10000:
                    issues.append({
                        'sql_id': sql_id,
                        'operation': operation,
                        'object_name': object_name,
                        'issue_type': '高成本哈希连接',
                        'description': f"哈希连接成本较高: {cost}",
                        'severity': 'Medium',
                        'severity_score': 55 + min(cost // 2000, 35),
                        'recommendation': '检查连接条件和统计信息是否准确'
                    })
        
        return issues
    
    def _calculate_plan_cost(self, plan_steps):
        """计算执行计划的总成本"""
        total_cost = 0
        for step in plan_steps:
            if isinstance(step, dict):
                cost = self._safe_int(step.get('cost', '0'))
                total_cost += cost
        return total_cost
    
    def _safe_int(self, value, default=0):
        """安全地转换为整数"""
        try:
            if isinstance(value, (int, float)):
                return int(value)
            elif isinstance(value, str):
                return int(value.replace(',', '').strip())
            return default
        except (ValueError, AttributeError):
            return default
    
    def _generate_plan_optimization_suggestions(self, plan_steps, sql_id, sql_text):
        """生成执行计划优化建议"""
        suggestions = []
        
        # 分析SQL类型
        sql_upper = sql_text.upper()
        
        # 检查是否有全表扫描
        has_full_scan = any('TABLE ACCESS FULL' in step.get('operation', '').upper() 
                           for step in plan_steps if isinstance(step, dict))
        
        # 检查是否有排序
        has_sort = any('SORT' in step.get('operation', '').upper() 
                      for step in plan_steps if isinstance(step, dict))
        
        # 检查是否有多表连接
        join_count = sum(1 for step in plan_steps if isinstance(step, dict) 
                        and any(join_type in step.get('operation', '').upper() 
                               for join_type in ['HASH JOIN', 'NESTED LOOPS', 'MERGE JOIN']))
        
        if has_full_scan:
            suggestions.append({
                'sql_id': sql_id,
                'suggestion_type': '索引优化',
                'description': '存在全表扫描操作',
                'recommendation': '为经常用作WHERE条件的列创建索引',
                'priority': 'High'
            })
        
        if has_sort and 'ORDER BY' in sql_upper:
            suggestions.append({
                'sql_id': sql_id,
                'suggestion_type': '排序优化',
                'description': '存在排序操作',
                'recommendation': '为ORDER BY列创建索引以避免排序',
                'priority': 'Medium'
            })
        
        if join_count > 2:
            suggestions.append({
                'sql_id': sql_id,
                'suggestion_type': '连接优化',
                'description': f'涉及 {join_count} 个表的复杂连接',
                'recommendation': '检查连接顺序和连接条件，考虑使用HINT',
                'priority': 'Medium'
            })
        
        if 'GROUP BY' in sql_upper and has_sort:
            suggestions.append({
                'sql_id': sql_id,
                'suggestion_type': '聚合优化',
                'description': 'GROUP BY操作可能导致排序',
                'recommendation': '为GROUP BY列创建索引或考虑使用哈希聚合',
                'priority': 'Medium'
            })
        
        return suggestions
    
    def _get_execution_plan_recommendations(self, inefficient_operations, high_cost_plans, cost_ranges):
        """生成执行计划的综合优化建议"""
        recommendations = []
        
        # 基于低效操作的建议
        if inefficient_operations:
            critical_issues = [op for op in inefficient_operations if op.get('severity') == 'Critical']
            high_issues = [op for op in inefficient_operations if op.get('severity') == 'High']
            
            if critical_issues:
                recommendations.extend([
                    f"🚨 发现 {len(critical_issues)} 个严重的执行计划问题",
                    "⚡ 立即检查和修复笛卡尔积问题",
                    "🔧 验证所有表连接条件的正确性"
                ])
            
            if high_issues:
                recommendations.extend([
                    f"⚠️  发现 {len(high_issues)} 个高影响的性能问题",
                    "📊 优先处理全表扫描操作",
                    "🔍 为频繁访问的列添加索引"
                ])
        
        # 基于高成本计划的建议  
        if high_cost_plans:
            recommendations.extend([
                f"💰 发现 {len(high_cost_plans)} 个高成本执行计划",
                "📈 重点优化成本最高的SQL语句",
                "📊 检查统计信息是否过期，运行ANALYZE"
            ])
        
        # 基于成本分布的建议
        total_plans = sum(cost_ranges.values())
        if total_plans > 0:
            high_cost_ratio = (cost_ranges['high'] + cost_ranges['very_high']) / total_plans
            if high_cost_ratio > 0.3:
                recommendations.extend([
                    f"📊 {high_cost_ratio*100:.1f}% 的计划成本较高",
                    "🔧 系统可能存在普遍的性能问题",
                    "💡 建议进行全面的数据库优化"
                ])
        
        # 通用优化建议
        if inefficient_operations or high_cost_plans:
            recommendations.extend([
                "📋 定期收集数据库统计信息",
                "🔍 使用SQL Trace分析具体的执行性能",
                "⚙️  考虑使用SQL Plan Management固定好的执行计划",
                "📚 为开发团队提供SQL优化培训"
            ])
        
        return recommendations
    
    def get_execution_plan_analysis(self):
        """获取执行计划分析结果"""
        return self.analysis_results['execution_plan_analysis']
    
    def _analyze_instance_efficiency(self):
        """分析实例效率"""
        try:
            instance_efficiency = self.parsed_data.get('instance_efficiency', {})
            load_profile = self.parsed_data.get('load_profile', {})
            
            # 安全提取百分比数据
            def safe_extract_percentage(data, key, default=0.0):
                try:
                    value = data.get(key, default)
                    if isinstance(value, (int, float)):
                        return float(value)
                    elif isinstance(value, str):
                        return float(value.replace('%', '').strip())
                    return default
                except (ValueError, AttributeError):
                    return default
            
            # 提取核心效率指标
            buffer_hit = safe_extract_percentage(instance_efficiency, 'Buffer  Hit   %:', 0)
            if buffer_hit == 0:
                buffer_hit = safe_extract_percentage(instance_efficiency, 'Buffer Hit %:', 0)
            
            buffer_nowait = safe_extract_percentage(instance_efficiency, 'Buffer Nowait %:', 100)
            library_hit = safe_extract_percentage(instance_efficiency, 'Library Hit   %:', 0)
            if library_hit == 0:
                library_hit = safe_extract_percentage(instance_efficiency, 'Library Hit %:', 0)
            
            execute_to_parse = safe_extract_percentage(instance_efficiency, 'Execute to Parse %:', 0)
            soft_parse = safe_extract_percentage(instance_efficiency, 'Soft Parse %:', 0)
            latch_hit = safe_extract_percentage(instance_efficiency, 'Latch Hit %:', 100)
            
            # 提取更多效率指标
            in_memory_sort = safe_extract_percentage(instance_efficiency, 'In-memory Sort %:', 0)
            redo_nowait = safe_extract_percentage(instance_efficiency, 'Redo NoWait %:', 100)
            
            # 从 Load Profile 提取额外数据
            def safe_extract_number(data, key, per_sec_key='Per Second', default=0):
                try:
                    if key in data:
                        value_data = data[key]
                        if isinstance(value_data, dict):
                            value_str = value_data.get(per_sec_key, '0')
                        else:
                            value_str = str(value_data)
                        return float(str(value_str).replace(',', ''))
                    return default
                except (ValueError, AttributeError, KeyError):
                    return default
            
            # 提取性能相关的数据
            executions_per_sec = safe_extract_number(load_profile, 'Executions:')
            parses_per_sec = safe_extract_number(load_profile, 'Hard parses:')
            if parses_per_sec == 0:
                parses_per_sec = safe_extract_number(load_profile, 'Parse count (hard):')
            
            transactions_per_sec = safe_extract_number(load_profile, 'Transactions:')
            user_calls_per_sec = safe_extract_number(load_profile, 'User calls:')
            
            # 计算派生效率指标
            parse_to_execute_ratio = 0
            if executions_per_sec > 0:
                parse_to_execute_ratio = (parses_per_sec / executions_per_sec) * 100
            
            # 计算实际的执行解析比
            actual_execute_to_parse = 0
            if parses_per_sec > 0:
                actual_execute_to_parse = ((executions_per_sec - parses_per_sec) / executions_per_sec) * 100
                actual_execute_to_parse = max(0, min(100, actual_execute_to_parse))
            
            # 计算每个用户调用的效率
            executions_per_user_call = executions_per_sec / user_calls_per_sec if user_calls_per_sec > 0 else 0
            
            # 效率评分算法
            efficiency_score = 100
            
            # 缓冲区命中率评分
            if buffer_hit < 85:
                efficiency_score -= 25
            elif buffer_hit < 90:
                efficiency_score -= 20
            elif buffer_hit < 95:
                efficiency_score -= 15
            elif buffer_hit < 98:
                efficiency_score -= 8
            
            # 库缓存命中率评分
            if library_hit < 85:
                efficiency_score -= 25
            elif library_hit < 90:
                efficiency_score -= 20
            elif library_hit < 95:
                efficiency_score -= 15
            elif library_hit < 98:
                efficiency_score -= 8
            
            # 软解析率评分
            if soft_parse < 80:
                efficiency_score -= 20
            elif soft_parse < 90:
                efficiency_score -= 15
            elif soft_parse < 95:
                efficiency_score -= 10
            elif soft_parse < 98:
                efficiency_score -= 5
            
            # 执行解析比评分
            effective_execute_to_parse = max(execute_to_parse, actual_execute_to_parse)
            if effective_execute_to_parse < 70:
                efficiency_score -= 15
            elif effective_execute_to_parse < 80:
                efficiency_score -= 10
            elif effective_execute_to_parse < 90:
                efficiency_score -= 8
            elif effective_execute_to_parse < 95:
                efficiency_score -= 5
            
            # 内存排序评分
            if in_memory_sort < 80:
                efficiency_score -= 15
            elif in_memory_sort < 90:
                efficiency_score -= 10
            elif in_memory_sort < 95:
                efficiency_score -= 8
            elif in_memory_sort < 98:
                efficiency_score -= 5
            
            # Latch命中率评分
            if latch_hit < 95:
                efficiency_score -= 15
            elif latch_hit < 98:
                efficiency_score -= 10
            elif latch_hit < 99:
                efficiency_score -= 5
            
            # 缓冲区等待评分
            if buffer_nowait < 95:
                efficiency_score -= 10
            elif buffer_nowait < 98:
                efficiency_score -= 5
            
            # Redo等待评分
            if redo_nowait < 95:
                efficiency_score -= 10
            elif redo_nowait < 98:
                efficiency_score -= 5
            
            efficiency_score = max(0, min(100, efficiency_score))
            
            # 判断效率问题
            efficiency_issues = {
                'critical_buffer_hit': buffer_hit < 85,
                'low_buffer_hit': buffer_hit < 95,
                'critical_library_hit': library_hit < 85,
                'low_library_hit': library_hit < 95,
                'critical_soft_parse': soft_parse < 80,
                'low_soft_parse': soft_parse < 95,
                'critical_execute_to_parse': effective_execute_to_parse < 70,
                'low_execute_to_parse': effective_execute_to_parse < 90,
                'low_memory_sort': in_memory_sort < 95,
                'latch_contention': latch_hit < 99,
                'buffer_contention': buffer_nowait < 98,
                'redo_contention': redo_nowait < 98,
                'high_parse_ratio': parse_to_execute_ratio > 20
            }
            
            # 生成效率优化建议
            efficiency_recommendations = self._get_efficiency_recommendations(
                buffer_hit, library_hit, soft_parse, effective_execute_to_parse,
                in_memory_sort, latch_hit, buffer_nowait, redo_nowait,
                parse_to_execute_ratio, efficiency_issues
            )
            
            # 设置实例效率分析结果
            self.analysis_results['instance_efficiency_analysis'] = {
                'buffer_hit': round(buffer_hit, 2),
                'buffer_nowait': round(buffer_nowait, 2),
                'library_hit': round(library_hit, 2),
                'execute_to_parse': round(execute_to_parse, 2),
                'actual_execute_to_parse': round(actual_execute_to_parse, 2),
                'effective_execute_to_parse': round(effective_execute_to_parse, 2),
                'soft_parse': round(soft_parse, 2),
                'latch_hit': round(latch_hit, 2),
                'in_memory_sort': round(in_memory_sort, 2),
                'redo_nowait': round(redo_nowait, 2),
                'parse_to_execute_ratio': round(parse_to_execute_ratio, 2),
                'executions_per_sec': round(executions_per_sec, 2),
                'parses_per_sec': round(parses_per_sec, 2),
                'transactions_per_sec': round(transactions_per_sec, 2),
                'executions_per_user_call': round(executions_per_user_call, 2),
                'efficiency_issues': efficiency_issues,
                'score': efficiency_score,
                'recommendations': efficiency_recommendations
            }
            
            logger.info("成功分析实例效率")
        except Exception as e:
            logger.error(f"分析实例效率失败: {str(e)}")
            # 设置默认值
            self.analysis_results['instance_efficiency_analysis'] = {
                'buffer_hit': 0,
                'buffer_nowait': 100,
                'library_hit': 0,
                'execute_to_parse': 0,
                'actual_execute_to_parse': 0,
                'effective_execute_to_parse': 0,
                'soft_parse': 0,
                'latch_hit': 100,
                'in_memory_sort': 0,
                'redo_nowait': 100,
                'parse_to_execute_ratio': 0,
                'executions_per_sec': 0,
                'parses_per_sec': 0,
                'transactions_per_sec': 0,
                'executions_per_user_call': 0,
                'efficiency_issues': {},
                'score': 100,
                'recommendations': []
            }
    
    def _get_efficiency_recommendations(self, buffer_hit, library_hit, soft_parse, execute_to_parse,
                                       memory_sort, latch_hit, buffer_nowait, redo_nowait,
                                       parse_ratio, issues):
        """生成效率相关的优化建议"""
        recommendations = []
        
        # 缓冲区效率建议
        if issues.get('critical_buffer_hit', False):
            recommendations.extend([
                "🚨 紧急：缓冲区命中率低于85%，系统效率极低",
                "💰 立即增加SGA中的db_cache_size",
                "🔍 检查是否存在不合理的全表扫描",
                "⚡ 优先优化高物理读取的SQL语句"
            ])
        elif issues.get('low_buffer_hit', False):
            recommendations.extend([
                "⚠️  缓冲区命中率低于95%，需要优化",
                "📊 监控缓冲区使用模式和命中率趋势"
            ])
        
        # 库缓存效率建议
        if issues.get('critical_library_hit', False):
            recommendations.extend([
                "🚨 严重：库缓存命中率低于85%，存在大量硬解析",
                "📝 强制使用绑定变量改造所有SQL",
                "💰 立即增加shared_pool_size参数",
                "⚙️  考虑设置cursor_sharing=FORCE"
            ])
        elif issues.get('low_library_hit', False):
            recommendations.extend([
                "⚠️  库缓存命中率低于95%，存在解析效率问题",
                "📝 检查应用程序的SQL使用模式"
            ])
        
        # 软解析效率建议
        if issues.get('critical_soft_parse', False):
            recommendations.extend([
                "🚨 严重：软解析率低于80%，存在大量硬解析",
                "📝 立即改造应用程序使用绑定变量",
                "🔄 考虑启用cursor_sharing参数",
                "💰 增加shared_pool_size和session_cached_cursors"
            ])
        elif issues.get('low_soft_parse', False):
            recommendations.extend([
                "⚠️  软解析率低于95%，存在解析效率问题",
                "📝 检查应用程序的游标管理策略"
            ])
        
        # 执行解析比建议
        if issues.get('critical_execute_to_parse', False):
            recommendations.extend([
                "🚨 执行解析比低于70%，存在严重的解析开销",
                "🔄 使用会话游标缓存减少重复解析",
                "📝 优化应用程序的SQL执行模式"
            ])
        elif issues.get('low_execute_to_parse', False):
            recommendations.extend([
                "⚠️  执行解析比低于90%，存在解析开销",
                "🔧 考虑使用准备语句或存储过程"
            ])
        
        # 内存排序效率建议
        if issues.get('low_memory_sort', False):
            recommendations.extend([
                "💾 内存排序比例低于95%，存在磁盘排序",
                "💰 增加PGA内存大小(pga_aggregate_target)",
                "🔧 优化排序操作的SQL语句"
            ])
        
        # Latch竞争建议
        if issues.get('latch_contention', False):
            recommendations.extend([
                "⚠️  Latch命中率低于99%，存在系统级竞争",
                "🔍 检查特定的Latch竞争类型",
                "🔧 优化高并发的数据库操作"
            ])
        
        # 缓冲区等待建议
        if issues.get('buffer_contention', False):
            recommendations.extend([
                "⚠️  缓冲区等待问题，影响数据块访问效率",
                "🔧 检查缓冲区的分配和使用模式",
                "📊 监控缓冲区相关的等待事件"
            ])
        
        # Redo等待建议
        if issues.get('redo_contention', False):
            recommendations.extend([
                "⚠️  Redo日志等待问题，影响事务提交效率",
                "💾 检查Redo日志文件的配置和性能",
                "🔧 考虑增加Redo日志组数量"
            ])
        
        # 解析比例建议
        if issues.get('high_parse_ratio', False):
            recommendations.extend([
                "🚨 解析执行比过高(>20%)，解析开销严重",
                "📝 重新设计应用程序的SQL执行模式",
                "🔄 使用连接池和游标缓存技术"
            ])
        
        # 添加通用效率优化建议
        if (buffer_hit < 98 or library_hit < 98 or soft_parse < 98 or 
            execute_to_parse < 95 or memory_sort < 98):
            recommendations.extend([
                "📈 建立数据库效率监控体系和基线",
                "📋 定期分析AWR报告中的效率指标趋势",
                "🔧 考虑使用自动内存管理(ASMM)优化内存分配",
                "📚 对开发团队进行数据库性能优化培训"
            ])
        
        return recommendations
    
    def _identify_potential_issues(self):
        """识别潜在问题"""
        try:
            potential_issues = []
            
            # CPU相关问题
            cpu_analysis = self.analysis_results.get('cpu_analysis', {})
            if cpu_analysis.get('cpu_bottleneck', False):
                potential_issues.append({
                    'category': 'CPU',
                    'severity': 'High',
                    'issue': 'CPU使用率过高',
                    'description': f"CPU利用率达到{cpu_analysis.get('cpu_utilization', 0)}%，可能存在CPU瓶颈。",
                    'recommendation': '检查高CPU消耗的SQL语句，考虑优化SQL或增加CPU资源。'
                })
            
            # 内存相关问题
            memory_analysis = self.analysis_results.get('memory_analysis', {})
            memory_issues = memory_analysis.get('memory_issues', {})
            
            if memory_issues.get('low_buffer_hit', False):
                potential_issues.append({
                    'category': 'Memory',
                    'severity': 'Medium',
                    'issue': '缓冲区命中率低',
                    'description': f"缓冲区命中率为{memory_analysis.get('buffer_hit_ratio', 0)}%，低于推荐的95%。",
                    'recommendation': '考虑增加数据库缓冲区大小，或检查是否存在全表扫描操作。'
                })
            
            if memory_issues.get('low_library_hit', False):
                potential_issues.append({
                    'category': 'Memory',
                    'severity': 'Medium',
                    'issue': '库缓存命中率低',
                    'description': f"库缓存命中率为{memory_analysis.get('library_hit_ratio', 0)}%，低于推荐的95%。",
                    'recommendation': '考虑增加共享池大小，或检查是否存在过多的硬解析操作。'
                })
            
            # IO相关问题
            io_analysis = self.analysis_results.get('io_analysis', {})
            if io_analysis.get('io_bottleneck', False):
                potential_issues.append({
                    'category': 'I/O',
                    'severity': 'High',
                    'issue': 'I/O请求过多',
                    'description': f"每秒I/O请求数为{io_analysis.get('total_io_requests_per_sec', 0)}，可能存在I/O瓶颈。",
                    'recommendation': '检查高I/O消耗的SQL语句，考虑优化索引或增加I/O资源。'
                })
            
            # 等待事件相关问题
            wait_analysis = self.analysis_results.get('wait_analysis', {})
            
            if wait_analysis.get('io_bound', False):
                potential_issues.append({
                    'category': 'Wait Events',
                    'severity': 'High',
                    'issue': 'I/O等待事件占比高',
                    'description': '用户I/O等待事件占数据库时间的比例超过20%，表明存在I/O瓶颈。',
                    'recommendation': '检查I/O子系统性能，考虑使用更快的存储设备或优化I/O密集型SQL。'
                })
            
            if wait_analysis.get('concurrency_issues', False):
                potential_issues.append({
                    'category': 'Wait Events',
                    'severity': 'Medium',
                    'issue': '并发等待事件占比高',
                    'description': '并发等待事件占数据库时间的比例超过10%，表明存在锁竞争问题。',
                    'recommendation': '检查应用程序设计，减少长事务，优化锁定策略。'
                })
            
            if wait_analysis.get('network_issues', False):
                potential_issues.append({
                    'category': 'Wait Events',
                    'severity': 'Medium',
                    'issue': '网络等待事件占比高',
                    'description': '网络等待事件占数据库时间的比例超过10%，表明存在网络瓶颈。',
                    'recommendation': '检查网络配置和带宽，考虑优化网络架构或减少网络传输数据量。'
                })
            
            # 实例效率相关问题
            instance_efficiency_analysis = self.analysis_results.get('instance_efficiency_analysis', {})
            efficiency_issues = instance_efficiency_analysis.get('efficiency_issues', {})
            
            if efficiency_issues.get('low_soft_parse', False):
                potential_issues.append({
                    'category': 'Parsing',
                    'severity': 'Medium',
                    'issue': '软解析率低',
                    'description': f"软解析率为{instance_efficiency_analysis.get('soft_parse', 0)}%，低于推荐的95%。",
                    'recommendation': '使用绑定变量，避免硬解析，考虑增加共享池大小。'
                })
            
            if efficiency_issues.get('low_execute_to_parse', False):
                potential_issues.append({
                    'category': 'Parsing',
                    'severity': 'Low',
                    'issue': '执行解析比率低',
                    'description': f"执行解析比率为{instance_efficiency_analysis.get('execute_to_parse', 0)}%，低于推荐的90%。",
                    'recommendation': '使用会话游标缓存，减少不必要的解析操作。'
                })
            
            # 设置潜在问题列表
            self.analysis_results['potential_issues'] = potential_issues
            
            logger.info("成功识别潜在问题")
        except Exception as e:
            logger.error(f"识别潜在问题失败: {str(e)}")
    
    def _calculate_performance_score(self):
        """计算性能评分"""
        try:
            # 初始分数为100
            score = 100
            
            # CPU相关扣分
            cpu_analysis = self.analysis_results.get('cpu_analysis', {})
            if cpu_analysis.get('cpu_bottleneck', False):
                cpu_utilization = cpu_analysis.get('cpu_utilization', 0)
                if cpu_utilization > 90:
                    score -= 20
                elif cpu_utilization > 80:
                    score -= 15
                elif cpu_utilization > 70:
                    score -= 10
            
            # 内存相关扣分
            memory_analysis = self.analysis_results.get('memory_analysis', {})
            buffer_hit_ratio = memory_analysis.get('buffer_hit_ratio', 0)
            library_hit_ratio = memory_analysis.get('library_hit_ratio', 0)
            
            if buffer_hit_ratio < 90:
                score -= 15
            elif buffer_hit_ratio < 95:
                score -= 10
            
            if library_hit_ratio < 90:
                score -= 10
            elif library_hit_ratio < 95:
                score -= 5
            
            # IO相关扣分
            io_analysis = self.analysis_results.get('io_analysis', {})
            if io_analysis.get('io_bottleneck', False):
                score -= 15
            
            # 等待事件相关扣分
            wait_analysis = self.analysis_results.get('wait_analysis', {})
            if wait_analysis.get('io_bound', False):
                score -= 15
            if wait_analysis.get('concurrency_issues', False):
                score -= 10
            if wait_analysis.get('network_issues', False):
                score -= 10
            
            # 实例效率相关扣分
            instance_efficiency_analysis = self.analysis_results.get('instance_efficiency_analysis', {})
            soft_parse = instance_efficiency_analysis.get('soft_parse', 0)
            execute_to_parse = instance_efficiency_analysis.get('execute_to_parse', 0)
            
            if soft_parse < 90:
                score -= 10
            elif soft_parse < 95:
                score -= 5
            
            if execute_to_parse < 85:
                score -= 10
            elif execute_to_parse < 90:
                score -= 5
            
            # 确保分数在0-100之间
            score = max(0, min(100, score))
            
            # 设置性能评分
            self.analysis_results['performance_score'] = score
            
            # 设置性能等级
            if score >= 90:
                performance_level = 'Excellent'
            elif score >= 80:
                performance_level = 'Good'
            elif score >= 70:
                performance_level = 'Fair'
            elif score >= 60:
                performance_level = 'Poor'
            else:
                performance_level = 'Critical'
            
            self.analysis_results['performance_level'] = performance_level
            
            logger.info(f"成功计算性能评分: {score} ({performance_level})")
        except Exception as e:
            logger.error(f"计算性能评分失败: {str(e)}")
    
    def get_summary(self):
        """获取分析概要"""
        return self.analysis_results['summary']
    
    def get_cpu_analysis(self):
        """获取CPU分析结果"""
        return self.analysis_results['cpu_analysis']
    
    def get_memory_analysis(self):
        """获取内存分析结果"""
        return self.analysis_results['memory_analysis']
    
    def get_io_analysis(self):
        """获取IO分析结果"""
        return self.analysis_results['io_analysis']
    
    def get_wait_analysis(self):
        """获取等待事件分析结果"""
        return self.analysis_results['wait_analysis']
    
    def get_sql_analysis(self):
        """获取SQL分析结果"""
        return self.analysis_results['sql_analysis']
    
    def get_instance_efficiency_analysis(self):
        """获取实例效率分析结果"""
        return self.analysis_results['instance_efficiency_analysis']
    
    def get_potential_issues(self):
        """获取潜在问题列表"""
        return self.analysis_results['potential_issues']
    
    def get_performance_score(self):
        """获取性能评分"""
        return self.analysis_results['performance_score']
    
    def get_performance_level(self):
        """获取性能等级"""
        return self.analysis_results['performance_level']
    
    def get_all_results(self):
        """获取所有分析结果"""
        return self.analysis_results
