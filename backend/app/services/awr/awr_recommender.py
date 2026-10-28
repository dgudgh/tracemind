#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AWR报告建议生成器模块
基于分析结果生成优化建议

作者: 青学会MOP技术社区
日期: 2025-04-16
"""

import logging
import pandas as pd
from datetime import datetime

# 设置日志
logger = logging.getLogger("青学会MOP技术社区")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

class AWRRecommendationGenerator:
    """
    AWR报告建议生成器类
    基于分析结果生成优化建议
    """
    
    def __init__(self, analysis_results):
        """
        初始化建议生成器
        
        参数:
            analysis_results (dict): 分析结果
        """
        self.analysis_results = analysis_results
        self.recommendations = {
            'summary': {},
            'cpu_recommendations': [],
            'memory_recommendations': [],
            'io_recommendations': [],
            'wait_recommendations': [],
            'sql_recommendations': [],
            'instance_efficiency_recommendations': [],
            'general_recommendations': [],
            'priority_recommendations': []
        }
        logger.info("初始化AWR报告建议生成器")
    
    def generate_recommendations(self):
        """
        生成全面建议
        
        返回:
            dict: 建议结果
        """
        logger.info("开始生成优化建议")
        
        self._generate_summary()
        self._generate_cpu_recommendations()
        self._generate_memory_recommendations()
        self._generate_io_recommendations()
        self._generate_wait_recommendations()
        self._generate_sql_recommendations()
        self._generate_instance_efficiency_recommendations()
        self._generate_general_recommendations()
        self._prioritize_recommendations()
        
        logger.info("优化建议生成完成")
        return self.recommendations
    
    def _generate_summary(self):
        """生成概要建议"""
        try:
            # 提取分析概要
            summary = self.analysis_results.get('summary', {})
            performance_score = self.analysis_results.get('performance_score', 0)
            performance_level = self.analysis_results.get('performance_level', 'Unknown')
            
            # 设置概要建议
            self.recommendations['summary'] = {
                'database_name': summary.get('database_name', 'Unknown'),
                'instance_name': summary.get('instance_name', 'Unknown'),
                'version': summary.get('version', 'Unknown'),
                'host': summary.get('host', 'Unknown'),
                'snapshot_period': summary.get('snapshot_period', 'Unknown'),
                'performance_score': performance_score,
                'performance_level': performance_level,
                'overall_assessment': self._get_overall_assessment(performance_score)
            }
            
            logger.info("成功生成概要建议")
        except Exception as e:
            logger.error(f"生成概要建议失败: {str(e)}")
    
    def _get_overall_assessment(self, score):
        """根据性能评分获取整体评估"""
        if score >= 90:
            return "数据库性能优秀，无需进行重大优化。"
        elif score >= 80:
            return "数据库性能良好，可以进行一些小的优化来提高性能。"
        elif score >= 70:
            return "数据库性能一般，建议进行一些优化来提高性能。"
        elif score >= 60:
            return "数据库性能较差，需要进行多项优化来解决性能问题。"
        else:
            return "数据库性能严重不足，需要立即进行全面优化。"
    
    def _generate_cpu_recommendations(self):
        """生成CPU相关建议"""
        try:
            # 提取CPU分析结果
            cpu_analysis = self.analysis_results.get('cpu_analysis', {})
            
            # 检查CPU瓶颈
            if cpu_analysis.get('cpu_bottleneck', False):
                cpu_utilization = cpu_analysis.get('cpu_utilization', 0)
                
                # 添加CPU瓶颈建议
                self.recommendations['cpu_recommendations'].append({
                    'title': 'CPU使用率过高',
                    'severity': 'High',
                    'finding': f"CPU利用率达到{cpu_utilization}%，存在明显的CPU瓶颈。",
                    'impact': "CPU瓶颈会导致查询响应时间增加，系统吞吐量下降，用户体验变差。",
                    'recommendations': [
                        "识别并优化高CPU消耗的SQL语句，减少执行计划中的全表扫描和排序操作。",
                        "考虑使用绑定变量减少硬解析，降低CPU消耗。",
                        "检查并优化PL/SQL代码中的循环和计算密集型操作。",
                        "如果可能，增加CPU资源或升级硬件。",
                        "考虑使用Oracle Resource Manager限制高消耗会话的CPU使用。"
                    ]
                })
            
            # 检查CPU使用分布
            host_cpu = cpu_analysis.get('host_cpu_usage', {})
            user_cpu = host_cpu.get('user', 0)
            system_cpu = host_cpu.get('system', 0)
            
            if system_cpu > 20:
                # 添加系统CPU使用率高的建议
                self.recommendations['cpu_recommendations'].append({
                    'title': '系统CPU使用率高',
                    'severity': 'Medium',
                    'finding': f"系统CPU使用率达到{system_cpu}%，高于正常水平（通常应低于10%）。",
                    'impact': "高系统CPU使用率表明操作系统内核活动频繁，可能影响数据库性能。",
                    'recommendations': [
                        "检查操作系统参数配置，特别是共享内存和信号量设置。",
                        "检查是否存在频繁的上下文切换，可能需要调整进程优先级。",
                        "检查I/O子系统性能，高系统CPU可能与I/O驱动程序有关。",
                        "考虑升级操作系统或应用补丁。"
                    ]
                })
            
            # 检查数据库CPU使用率
            db_cpu_usage = cpu_analysis.get('db_cpu_usage', {})
            db_cpu_pct = db_cpu_usage.get('total_cpu_pct', 0)
            
            if db_cpu_pct > 80:
                # 添加数据库CPU使用率高的建议
                self.recommendations['cpu_recommendations'].append({
                    'title': '数据库CPU占用率高',
                    'severity': 'Medium',
                    'finding': f"数据库占用了主机{db_cpu_pct}%的CPU资源，可能影响其他应用程序。",
                    'impact': "数据库CPU占用过高可能导致服务器上其他应用程序资源不足，影响整体系统性能。",
                    'recommendations': [
                        "检查是否有不必要的后台进程或作业消耗CPU。",
                        "考虑使用Oracle Resource Manager限制数据库CPU使用。",
                        "评估是否需要将数据库迁移到专用服务器。",
                        "检查数据库参数配置，特别是与并行处理相关的参数。"
                    ]
                })
            
            logger.info("成功生成CPU相关建议")
        except Exception as e:
            logger.error(f"生成CPU相关建议失败: {str(e)}")
    
    def _generate_memory_recommendations(self):
        """生成内存相关建议"""
        try:
            # 提取内存分析结果
            memory_analysis = self.analysis_results.get('memory_analysis', {})
            memory_issues = memory_analysis.get('memory_issues', {})
            
            # 检查缓冲区命中率
            buffer_hit_ratio = memory_analysis.get('buffer_hit_ratio', 0)
            if memory_issues.get('low_buffer_hit', False):
                # 添加缓冲区命中率低的建议
                self.recommendations['memory_recommendations'].append({
                    'title': '缓冲区命中率低',
                    'severity': 'Medium',
                    'finding': f"缓冲区命中率为{buffer_hit_ratio}%，低于推荐的95%。",
                    'impact': "低缓冲区命中率会导致频繁的物理I/O，增加查询响应时间，降低系统吞吐量。",
                    'recommendations': [
                        "增加数据库缓冲区大小（DB_CACHE_SIZE参数）。",
                        "检查是否存在大量全表扫描操作，考虑创建适当的索引。",
                        "使用多块读取参数（DB_FILE_MULTIBLOCK_READ_COUNT）优化全表扫描。",
                        "考虑使用自动内存管理（MEMORY_TARGET参数）让Oracle自动调整内存分配。",
                        "对于OLTP系统，考虑使用多个缓冲池，将不同访问模式的对象分配到不同的缓冲池。"
                    ]
                })
            
            # 检查库缓存命中率
            library_hit_ratio = memory_analysis.get('library_hit_ratio', 0)
            if memory_issues.get('low_library_hit', False):
                # 添加库缓存命中率低的建议
                self.recommendations['memory_recommendations'].append({
                    'title': '库缓存命中率低',
                    'severity': 'Medium',
                    'finding': f"库缓存命中率为{library_hit_ratio}%，低于推荐的95%。",
                    'impact': "低库缓存命中率会导致频繁的硬解析，增加CPU使用率，降低系统响应速度。",
                    'recommendations': [
                        "增加共享池大小（SHARED_POOL_SIZE参数）。",
                        "使用绑定变量减少硬解析。",
                        "检查应用程序是否使用了动态SQL，考虑使用存储过程或预编译语句。",
                        "设置合适的游标共享参数（CURSOR_SHARING=FORCE或SIMILAR）。",
                        "检查是否存在库缓存Pin或Invalidation问题。"
                    ]
                })
            
            # 检查内存排序百分比
            in_memory_sort_pct = memory_analysis.get('in_memory_sort_pct', 0)
            if memory_issues.get('low_memory_sort', False):
                # 添加内存排序百分比低的建议
                self.recommendations['memory_recommendations'].append({
                    'title': '内存排序百分比低',
                    'severity': 'Medium',
                    'finding': f"内存排序百分比为{in_memory_sort_pct}%，低于推荐的95%。",
                    'impact': "低内存排序百分比会导致频繁的磁盘排序，增加I/O负载，降低查询性能。",
                    'recommendations': [
                        "增加PGA内存大小（PGA_AGGREGATE_TARGET参数）。",
                        "检查是否存在大量排序操作的SQL，考虑优化查询或添加索引减少排序。",
                        "使用SORT_AREA_SIZE和SORT_AREA_RETAINED_SIZE参数调整排序内存（对于非自动PGA管理）。",
                        "考虑使用自动PGA内存管理让Oracle自动调整排序内存。",
                        "监控WORKAREA_SIZE_POLICY参数，确保设置为AUTO。"
                    ]
                })
            
            logger.info("成功生成内存相关建议")
        except Exception as e:
            logger.error(f"生成内存相关建议失败: {str(e)}")
    
    def _generate_io_recommendations(self):
        """生成IO相关建议"""
        try:
            # 提取IO分析结果
            io_analysis = self.analysis_results.get('io_analysis', {})
            
            # 检查IO瓶颈
            if io_analysis.get('io_bottleneck', False):
                total_io_requests = io_analysis.get('total_io_requests_per_sec', 0)
                
                # 添加IO瓶颈建议
                self.recommendations['io_recommendations'].append({
                    'title': 'I/O请求过多',
                    'severity': 'High',
                    'finding': f"每秒I/O请求数为{total_io_requests}，超过系统I/O能力。",
                    'impact': "过多的I/O请求会导致I/O等待增加，查询响应时间延长，系统吞吐量下降。",
                    'recommendations': [
                        "检查高I/O消耗的SQL语句，优化查询减少物理I/O。",
                        "增加数据库缓冲区大小，减少物理读取。",
                        "考虑使用更快的存储设备，如SSD或闪存存储。",
                        "使用RAID配置提高I/O性能和可靠性。",
                        "检查是否存在热点文件或表空间，考虑重新分布I/O负载。",
                        "使用Oracle ASM进行I/O负载均衡。"
                    ]
                })
            
            # 检查读写比例
            read_write_ratio = io_analysis.get('read_write_ratio', 0)
            physical_reads = io_analysis.get('physical_reads_per_sec', 0)
            physical_writes = io_analysis.get('physical_writes_per_sec', 0)
            
            if read_write_ratio > 10:
                # 添加读密集型建议
                self.recommendations['io_recommendations'].append({
                    'title': '读密集型I/O模式',
                    'severity': 'Medium',
                    'finding': f"读写比例为{read_write_ratio}:1，系统呈现明显的读密集型特征。",
                    'impact': "读密集型系统可能面临读取性能瓶颈，影响查询响应时间。",
                    'recommendations': [
                        "考虑使用读缓存优化的存储设备。",
                        "增加数据库缓冲区大小，提高缓冲区命中率。",
                        "检查是否可以使用Oracle结果缓存（RESULT_CACHE_MODE参数）。",
                        "对于频繁访问的只读数据，考虑使用物化视图。",
                        "评估是否适合使用Oracle In-Memory选项（对于企业版）。"
                    ]
                })
            elif read_write_ratio < 1:
                # 添加写密集型建议
                self.recommendations['io_recommendations'].append({
                    'title': '写密集型I/O模式',
                    'severity': 'Medium',
                    'finding': f"读写比例为{read_write_ratio}:1，系统呈现明显的写密集型特征。",
                    'impact': "写密集型系统可能面临写入性能瓶颈，影响事务处理速度。",
                    'recommendations': [
                        "考虑使用写缓存优化的存储设备。",
                        "检查重做日志配置，确保大小和数量适当。",
                        "考虑使用NOLOGGING选项进行批量数据加载。",
                        "评估是否需要增加DBWR进程数（DB_WRITER_PROCESSES参数）。",
                        "检查是否可以批处理更新操作，减少写入频率。"
                    ]
                })
            
            # 检查物理读写
            if physical_reads > 1000:
                # 添加物理读取过多建议
                self.recommendations['io_recommendations'].append({
                    'title': '物理读取过多',
                    'severity': 'Medium',
                    'finding': f"每秒物理读取数为{physical_reads}块，可能影响系统性能。",
                    'impact': "过多的物理读取会增加I/O等待时间，降低查询响应速度。",
                    'recommendations': [
                        "增加数据库缓冲区大小，提高缓冲区命中率。",
                        "检查是否存在全表扫描操作，考虑创建适当的索引。",
                        "使用分区表减少扫描数据量。",
                        "考虑使用索引组织表或压缩表减少I/O。",
                        "评估是否适合使用Oracle In-Memory选项（对于企业版）。"
                    ]
                })
            
            if physical_writes > 500:
                # 添加物理写入过多建议
                self.recommendations['io_recommendations'].append({
                    'title': '物理写入过多',
                    'severity': 'Medium',
                    'finding': f"每秒物理写入数为{physical_writes}块，可能影响系统性能。",
                    'impact': "过多的物理写入会增加I/O等待时间，降低事务处理速度。",
                    'recommendations': [
                        "检查是否存在频繁的COMMIT操作，考虑批量提交。",
                        "评估是否需要所有的索引，删除不必要的索引可减少写入。",
                        "检查重做日志配置，确保大小和位置适当。",
                        "考虑使用NOLOGGING选项进行批量数据加载。",
                        "使用表分区减少索引维护开销。"
                    ]
                })
            
            logger.info("成功生成IO相关建议")
        except Exception as e:
            logger.error(f"生成IO相关建议失败: {str(e)}")
    
    def _generate_wait_recommendations(self):
        """生成等待事件相关建议"""
        try:
            # 提取等待事件分析结果
            wait_analysis = self.analysis_results.get('wait_analysis', {})
            top_events = wait_analysis.get('top_events', [])
            
            # 处理前5个等待事件
            for event in top_events:
                event_name = event.get('event_name', '')
                wait_time = event.get('wait_time', 0)
                db_time_pct = event.get('db_time_pct', 0)
                wait_class = event.get('wait_class', '')
                
                # 跳过DB CPU事件，它在CPU建议中已处理
                if event_name == 'DB CPU':
                    continue
                
                # 根据等待事件类型生成建议
                if wait_class == 'User I/O' and db_time_pct > 5:
                    # 添加用户I/O等待建议
                    self.recommendations['wait_recommendations'].append({
                        'title': f"用户I/O等待事件: {event_name}",
                        'severity': 'High' if db_time_pct > 20 else 'Medium',
                        'finding': f"等待事件'{event_name}'占数据库时间的{db_time_pct}%，表明存在I/O瓶颈。",
                        'impact': "I/O等待会直接影响查询响应时间和系统吞吐量。",
                        'recommendations': self._get_io_wait_recommendations(event_name)
                    })
                elif wait_class == 'Concurrency' and db_time_pct > 5:
                    # 添加并发等待建议
                    self.recommendations['wait_recommendations'].append({
                        'title': f"并发等待事件: {event_name}",
                        'severity': 'High' if db_time_pct > 15 else 'Medium',
                        'finding': f"等待事件'{event_name}'占数据库时间的{db_time_pct}%，表明存在锁竞争问题。",
                        'impact': "并发等待会导致事务阻塞，降低系统并发能力。",
                        'recommendations': self._get_concurrency_wait_recommendations(event_name)
                    })
                elif wait_class == 'Network' and db_time_pct > 5:
                    # 添加网络等待建议
                    self.recommendations['wait_recommendations'].append({
                        'title': f"网络等待事件: {event_name}",
                        'severity': 'Medium',
                        'finding': f"等待事件'{event_name}'占数据库时间的{db_time_pct}%，表明存在网络瓶颈。",
                        'impact': "网络等待会增加客户端-服务器通信时间，降低应用程序响应速度。",
                        'recommendations': [
                            "检查网络配置和带宽，确保网络资源充足。",
                            "考虑使用更大的SDU和TDU大小减少网络往返次数。",
                            "检查是否存在网络拥塞或硬件问题。",
                            "评估是否可以使用连接池减少网络连接开销。",
                            "考虑将应用服务器和数据库服务器放置在同一网段或更近的位置。"
                        ]
                    })
                elif wait_class == 'Configuration' and db_time_pct > 3:
                    # 添加配置等待建议
                    self.recommendations['wait_recommendations'].append({
                        'title': f"配置等待事件: {event_name}",
                        'severity': 'Medium',
                        'finding': f"等待事件'{event_name}'占数据库时间的{db_time_pct}%，表明存在配置问题。",
                        'impact': "配置等待通常表明数据库参数设置不当，影响整体性能。",
                        'recommendations': [
                            "检查与该等待事件相关的数据库参数设置。",
                            "参考Oracle文档调整特定参数。",
                            "考虑使用Oracle推荐的默认参数值。",
                            "使用ADDM或其他诊断工具获取具体的参数调整建议。"
                        ]
                    })
                elif db_time_pct > 5:
                    # 添加其他等待事件建议
                    self.recommendations['wait_recommendations'].append({
                        'title': f"等待事件: {event_name}",
                        'severity': 'Medium',
                        'finding': f"等待事件'{event_name}'占数据库时间的{db_time_pct}%。",
                        'impact': "此等待事件占用了显著的数据库时间，可能影响系统性能。",
                        'recommendations': [
                            f"深入分析'{event_name}'等待事件的具体原因。",
                            "查阅Oracle文档了解此等待事件的详细信息。",
                            "使用ASH或其他诊断工具识别导致此等待的具体会话和SQL。",
                            "考虑联系Oracle支持获取针对此等待事件的专业建议。"
                        ]
                    })
            
            logger.info("成功生成等待事件相关建议")
        except Exception as e:
            logger.error(f"生成等待事件相关建议失败: {str(e)}")
    
    def _get_io_wait_recommendations(self, event_name):
        """根据I/O等待事件名称获取建议"""
        if 'db file sequential read' in event_name:
            return [
                "检查索引设计，确保查询使用适当的索引。",
                "分析产生大量单块读取的SQL语句，考虑优化查询。",
                "考虑使用更快的存储设备，如SSD。",
                "检查存储I/O配置，确保没有I/O瓶颈。",
                "使用索引组织表减少索引查找的I/O。"
            ]
        elif 'db file scattered read' in event_name:
            return [
                "检查是否存在大量全表扫描，考虑创建适当的索引。",
                "增加DB_FILE_MULTIBLOCK_READ_COUNT参数值优化全表扫描。",
                "考虑使用表分区减少扫描数据量。",
                "增加数据库缓冲区大小，提高缓冲区命中率。",
                "使用并行查询处理大表扫描。"
            ]
        elif 'direct path read' in event_name or 'direct path write' in event_name:
            return [
                "检查并行查询设置，调整并行度。",
                "考虑使用APPEND提示进行直接路径插入。",
                "检查临时表空间配置，确保大小和位置适当。",
                "使用表分区减少直接路径操作的数据量。",
                "考虑使用更快的存储设备处理直接路径I/O。"
            ]
        elif 'log file sync' in event_name:
            return [
                "检查是否存在频繁的COMMIT操作，考虑批量提交。",
                "确保重做日志文件位于快速存储设备上。",
                "增加重做日志文件大小，减少日志切换频率。",
                "检查是否存在重做日志I/O瓶颈。",
                "考虑使用NOLOGGING选项进行批量数据加载。"
            ]
        else:
            return [
                "分析产生此I/O等待的SQL语句，优化查询减少I/O。",
                "检查存储系统性能，确保没有I/O瓶颈。",
                "考虑使用更快的存储设备，如SSD或闪存存储。",
                "使用Oracle ASM进行I/O负载均衡。",
                "检查数据库缓冲区大小，提高缓冲区命中率减少物理I/O。"
            ]
    
    def _get_concurrency_wait_recommendations(self, event_name):
        """根据并发等待事件名称获取建议"""
        if 'enq: TX - row lock contention' in event_name:
            return [
                "识别频繁锁定的表和行，考虑优化应用程序逻辑减少锁竞争。",
                "检查是否存在长时间运行的事务，尽快提交事务释放锁。",
                "考虑使用SELECT FOR UPDATE NOWAIT避免长时间等待。",
                "使用行级锁而非表级锁，减少锁范围。",
                "检查索引设计，确保UPDATE和DELETE操作使用索引。"
            ]
        elif 'latch' in event_name:
            return [
                "检查共享池大小，增加SHARED_POOL_SIZE参数值。",
                "使用绑定变量减少硬解析，降低共享池latch竞争。",
                "考虑增加latch相关参数，如_LATCH_CLASS_0。",
                "检查是否存在热块访问，考虑使用多个缓冲池。",
                "使用CURSOR_SHARING=FORCE减少游标数量。"
            ]
        elif 'library cache' in event_name:
            return [
                "使用绑定变量减少硬解析，降低库缓存竞争。",
                "增加共享池大小，提高库缓存命中率。",
                "检查是否存在频繁的DDL操作，减少对象失效。",
                "使用CURSOR_SHARING=FORCE减少游标数量。",
                "考虑使用会话游标缓存，减少库缓存访问。"
            ]
        elif 'buffer busy' in event_name:
            return [
                "检查是否存在热块访问，考虑使用多个缓冲池。",
                "使用表分区或哈希分区减少热块竞争。",
                "检查是否存在频繁更新的索引，考虑使用反向键索引。",
                "增加INITRANS参数值，允许更多并发事务。",
                "检查是否存在高水位线（HWM）问题，考虑重建表。"
            ]
        else:
            return [
                "分析产生此并发等待的SQL语句和会话，识别锁竞争来源。",
                "检查应用程序设计，减少长事务，优化锁定策略。",
                "考虑使用适当的隔离级别减少锁竞争。",
                "使用Oracle Enterprise Manager或其他工具监控锁等待。",
                "评估是否需要调整初始化参数以减少并发等待。"
            ]
    
    def _generate_sql_recommendations(self):
        """生成SQL相关建议"""
        try:
            # 提取SQL分析结果
            sql_analysis = self.analysis_results.get('sql_analysis', {})
            top_cpu_sql = sql_analysis.get('top_cpu_sql', [])
            top_buffer_gets_sql = sql_analysis.get('top_buffer_gets_sql', [])
            top_executions_sql = sql_analysis.get('top_executions_sql', [])
            top_elapsed_time_sql = sql_analysis.get('top_elapsed_time_sql', [])
            
            # 处理高CPU消耗的SQL
            if top_cpu_sql:
                sql_info = top_cpu_sql[0]
                sql_id = sql_info.get('sql_id', '')
                sql_text = sql_info.get('sql_text', '')
                cpu_time = sql_info.get('cpu_time', 0)
                
                if sql_id and cpu_time > 0:
                    # 添加高CPU消耗SQL建议
                    self.recommendations['sql_recommendations'].append({
                        'title': '高CPU消耗SQL',
                        'severity': 'High',
                        'finding': f"SQL ID '{sql_id}'消耗了大量CPU时间({cpu_time}秒)。",
                        'impact': "高CPU消耗的SQL会占用大量CPU资源，影响系统整体性能。",
                        'sql_text': sql_text[:200] + ('...' if len(sql_text) > 200 else ''),
                        'recommendations': [
                            "使用EXPLAIN PLAN分析执行计划，检查是否存在全表扫描或排序操作。",
                            "考虑创建适当的索引优化查询。",
                            "使用绑定变量减少硬解析。",
                            "检查是否可以重写SQL，避免使用函数应用于索引列。",
                            "考虑使用SQL Profile或SQL Plan Baseline固定执行计划。"
                        ]
                    })
            
            # 处理高缓冲区访问的SQL
            if top_buffer_gets_sql:
                sql_info = top_buffer_gets_sql[0]
                sql_id = sql_info.get('sql_id', '')
                sql_text = sql_info.get('sql_text', '')
                buffer_gets = sql_info.get('buffer_gets', 0)
                
                if sql_id and buffer_gets > 10000:
                    # 添加高缓冲区访问SQL建议
                    self.recommendations['sql_recommendations'].append({
                        'title': '高缓冲区访问SQL',
                        'severity': 'Medium',
                        'finding': f"SQL ID '{sql_id}'产生了大量缓冲区访问({buffer_gets}次)。",
                        'impact': "高缓冲区访问的SQL会消耗大量内存资源，可能导致缓冲区争用。",
                        'sql_text': sql_text[:200] + ('...' if len(sql_text) > 200 else ''),
                        'recommendations': [
                            "使用EXPLAIN PLAN分析执行计划，检查是否存在不必要的全表扫描。",
                            "考虑创建适当的索引减少缓冲区访问。",
                            "检查是否可以重写SQL，减少处理的数据量。",
                            "使用分区表减少扫描数据量。",
                            "考虑使用物化视图预计算结果。"
                        ]
                    })
            
            # 处理高执行次数的SQL
            if top_executions_sql:
                sql_info = top_executions_sql[0]
                sql_id = sql_info.get('sql_id', '')
                sql_text = sql_info.get('sql_text', '')
                executions = sql_info.get('executions', 0)
                
                if sql_id and executions > 1000:
                    # 添加高执行次数SQL建议
                    self.recommendations['sql_recommendations'].append({
                        'title': '高执行次数SQL',
                        'severity': 'Medium',
                        'finding': f"SQL ID '{sql_id}'执行次数过多({executions}次)。",
                        'impact': "频繁执行的SQL即使单次消耗资源少，累积效果也会显著影响系统性能。",
                        'sql_text': sql_text[:200] + ('...' if len(sql_text) > 200 else ''),
                        'recommendations': [
                            "检查应用程序逻辑，考虑批处理操作减少SQL执行次数。",
                            "使用绑定变量减少硬解析开销。",
                            "考虑使用PL/SQL批量操作（FORALL, BULK COLLECT）。",
                            "评估是否可以使用缓存减少数据库访问。",
                            "检查是否存在循环中执行的SQL，考虑优化循环逻辑。"
                        ]
                    })
            
            # 处理高耗时的SQL
            if top_elapsed_time_sql:
                sql_info = top_elapsed_time_sql[0]
                sql_id = sql_info.get('sql_id', '')
                sql_text = sql_info.get('sql_text', '')
                elapsed_time = sql_info.get('elapsed_time', 0)
                
                if sql_id and elapsed_time > 10:
                    # 添加高耗时SQL建议
                    self.recommendations['sql_recommendations'].append({
                        'title': '高耗时SQL',
                        'severity': 'High',
                        'finding': f"SQL ID '{sql_id}'执行时间过长({elapsed_time}秒)。",
                        'impact': "长时间运行的SQL会占用系统资源，延长用户等待时间，降低系统响应速度。",
                        'sql_text': sql_text[:200] + ('...' if len(sql_text) > 200 else ''),
                        'recommendations': [
                            "使用EXPLAIN PLAN分析执行计划，识别耗时环节。",
                            "检查是否存在全表扫描、排序或哈希连接操作。",
                            "考虑创建适当的索引优化查询。",
                            "使用分区表减少扫描数据量。",
                            "评估是否可以使用并行查询处理大数据量。",
                            "考虑使用SQL Profile或SQL Plan Baseline固定执行计划。"
                        ]
                    })
            
            logger.info("成功生成SQL相关建议")
        except Exception as e:
            logger.error(f"生成SQL相关建议失败: {str(e)}")
    
    def _generate_instance_efficiency_recommendations(self):
        """生成实例效率相关建议"""
        try:
            # 提取实例效率分析结果
            instance_efficiency_analysis = self.analysis_results.get('instance_efficiency_analysis', {})
            efficiency_issues = instance_efficiency_analysis.get('efficiency_issues', {})
            
            # 检查软解析率
            soft_parse = instance_efficiency_analysis.get('soft_parse', 0)
            if efficiency_issues.get('low_soft_parse', False):
                # 添加软解析率低的建议
                self.recommendations['instance_efficiency_recommendations'].append({
                    'title': '软解析率低',
                    'severity': 'Medium',
                    'finding': f"软解析率为{soft_parse}%，低于推荐的95%。",
                    'impact': "低软解析率会导致频繁的硬解析，增加CPU使用率，降低系统响应速度。",
                    'recommendations': [
                        "使用绑定变量而非硬编码的文本值。",
                        "检查应用程序是否使用了动态SQL，考虑使用存储过程或预编译语句。",
                        "设置合适的游标共享参数（CURSOR_SHARING=FORCE或SIMILAR）。",
                        "增加共享池大小，提高库缓存命中率。",
                        "使用会话游标缓存，减少解析操作。"
                    ]
                })
            
            # 检查执行解析比率
            execute_to_parse = instance_efficiency_analysis.get('execute_to_parse', 0)
            if efficiency_issues.get('low_execute_to_parse', False):
                # 添加执行解析比率低的建议
                self.recommendations['instance_efficiency_recommendations'].append({
                    'title': '执行解析比率低',
                    'severity': 'Low',
                    'finding': f"执行解析比率为{execute_to_parse}%，低于推荐的90%。",
                    'impact': "低执行解析比率表明存在过多的解析操作，增加CPU开销，降低系统效率。",
                    'recommendations': [
                        "使用会话游标缓存，减少不必要的解析操作。",
                        "检查应用程序是否在循环中重复解析SQL，考虑在循环外解析。",
                        "使用绑定变量提高游标重用率。",
                        "设置SESSION_CACHED_CURSORS参数增加会话游标缓存大小。",
                        "检查应用程序是否正确关闭游标，避免游标泄漏。"
                    ]
                })
            
            # 检查latch命中率
            latch_hit = instance_efficiency_analysis.get('latch_hit', 0)
            if efficiency_issues.get('low_latch_hit', False):
                # 添加latch命中率低的建议
                self.recommendations['instance_efficiency_recommendations'].append({
                    'title': 'Latch命中率低',
                    'severity': 'Medium',
                    'finding': f"Latch命中率为{latch_hit}%，低于推荐的99%。",
                    'impact': "低Latch命中率会导致进程等待latch，增加CPU使用率，降低系统并发能力。",
                    'recommendations': [
                        "增加共享池大小，减少latch竞争。",
                        "使用绑定变量减少硬解析，降低共享池latch竞争。",
                        "考虑增加latch相关参数，如_LATCH_CLASS_0。",
                        "检查是否存在热块访问，考虑使用多个缓冲池。",
                        "减少并发会话数，降低latch竞争。"
                    ]
                })
            
            logger.info("成功生成实例效率相关建议")
        except Exception as e:
            logger.error(f"生成实例效率相关建议失败: {str(e)}")
    
    def _generate_general_recommendations(self):
        """生成一般性建议"""
        try:
            # 添加一般性建议
            self.recommendations['general_recommendations'] = [
                {
                    'title': '定期收集统计信息',
                    'severity': 'Medium',
                    'finding': "数据库统计信息对优化器生成高效执行计划至关重要。",
                    'impact': "过时的统计信息可能导致优化器选择次优执行计划，影响查询性能。",
                    'recommendations': [
                        "使用DBMS_STATS包定期收集表和索引统计信息。",
                        "考虑使用自动统计信息收集功能。",
                        "对于频繁变化的表，增加统计信息收集频率。",
                        "使用分区表时，确保收集分区级别的统计信息。",
                        "监控统计信息收集作业的执行情况。"
                    ]
                },
                {
                    'title': '定期进行AWR分析',
                    'severity': 'Medium',
                    'finding': "定期分析AWR报告有助于及时发现性能问题。",
                    'impact': "缺乏定期性能监控可能导致性能问题长时间未被发现，影响系统稳定性。",
                    'recommendations': [
                        "建立定期AWR报告分析机制，如每周或每月分析一次。",
                        "关注AWR报告中的性能趋势变化，及时发现异常。",
                        "使用ADDM获取更详细的性能诊断信息。",
                        "建立性能基准，便于比较不同时期的性能变化。",
                        "针对发现的问题及时采取优化措施。"
                    ]
                },
                {
                    'title': '优化数据库参数设置',
                    'severity': 'Medium',
                    'finding': "数据库参数设置对性能有显著影响。",
                    'impact': "不当的参数设置可能导致资源浪费或性能瓶颈。",
                    'recommendations': [
                        "使用Oracle推荐的参数设置作为基准。",
                        "根据系统特点调整关键参数，如内存参数、优化器参数等。",
                        "避免过度调整参数，保持简单性。",
                        "记录参数变更历史，便于回溯问题。",
                        "使用Oracle提供的参数调优建议。"
                    ]
                },
                {
                    'title': '实施分区策略',
                    'severity': 'Medium',
                    'finding': "大型表和索引可能导致性能问题。",
                    'impact': "大型对象会增加I/O负载，降低查询和维护操作的效率。",
                    'recommendations': [
                        "对大型表实施分区策略，如范围分区、列表分区或哈希分区。",
                        "根据查询模式选择适当的分区键。",
                        "考虑使用本地分区索引提高维护效率。",
                        "使用分区裁剪技术减少扫描数据量。",
                        "定期维护分区，如合并、拆分或删除过期分区。"
                    ]
                },
                {
                    'title': '优化索引策略',
                    'severity': 'Medium',
                    'finding': "索引设计对查询性能有显著影响。",
                    'impact': "不当的索引设计可能导致查询性能下降或维护开销增加。",
                    'recommendations': [
                        "根据查询模式创建适当的索引，避免过度索引。",
                        "考虑使用复合索引减少索引数量。",
                        "监控索引使用情况，删除不使用的索引。",
                        "对于频繁更新的表，权衡索引维护开销和查询性能。",
                        "考虑使用函数索引、位图索引或反向键索引解决特定问题。"
                    ]
                }
            ]
            
            logger.info("成功生成一般性建议")
        except Exception as e:
            logger.error(f"生成一般性建议失败: {str(e)}")
    
    def _prioritize_recommendations(self):
        """优先排序建议"""
        try:
            # 收集所有建议
            all_recommendations = []
            
            # 添加CPU建议
            for rec in self.recommendations['cpu_recommendations']:
                rec['category'] = 'CPU'
                all_recommendations.append(rec)
            
            # 添加内存建议
            for rec in self.recommendations['memory_recommendations']:
                rec['category'] = 'Memory'
                all_recommendations.append(rec)
            
            # 添加IO建议
            for rec in self.recommendations['io_recommendations']:
                rec['category'] = 'I/O'
                all_recommendations.append(rec)
            
            # 添加等待事件建议
            for rec in self.recommendations['wait_recommendations']:
                rec['category'] = 'Wait Events'
                all_recommendations.append(rec)
            
            # 添加SQL建议
            for rec in self.recommendations['sql_recommendations']:
                rec['category'] = 'SQL'
                all_recommendations.append(rec)
            
            # 添加实例效率建议
            for rec in self.recommendations['instance_efficiency_recommendations']:
                rec['category'] = 'Instance Efficiency'
                all_recommendations.append(rec)
            
            # 按严重程度排序
            severity_order = {'High': 0, 'Medium': 1, 'Low': 2}
            all_recommendations.sort(key=lambda x: severity_order.get(x.get('severity', 'Low'), 3))
            
            # 选择前10个高优先级建议
            self.recommendations['priority_recommendations'] = all_recommendations[:10]
            
            logger.info("成功优先排序建议")
        except Exception as e:
            logger.error(f"优先排序建议失败: {str(e)}")
    
    def get_summary(self):
        """获取概要建议"""
        return self.recommendations['summary']
    
    def get_cpu_recommendations(self):
        """获取CPU相关建议"""
        return self.recommendations['cpu_recommendations']
    
    def get_memory_recommendations(self):
        """获取内存相关建议"""
        return self.recommendations['memory_recommendations']
    
    def get_io_recommendations(self):
        """获取IO相关建议"""
        return self.recommendations['io_recommendations']
    
    def get_wait_recommendations(self):
        """获取等待事件相关建议"""
        return self.recommendations['wait_recommendations']
    
    def get_sql_recommendations(self):
        """获取SQL相关建议"""
        return self.recommendations['sql_recommendations']
    
    def get_instance_efficiency_recommendations(self):
        """获取实例效率相关建议"""
        return self.recommendations['instance_efficiency_recommendations']
    
    def get_general_recommendations(self):
        """获取一般性建议"""
        return self.recommendations['general_recommendations']
    
    def get_priority_recommendations(self):
        """获取优先级建议"""
        return self.recommendations['priority_recommendations']
    
    def get_all_recommendations(self):
        """获取所有建议"""
        return self.recommendations
