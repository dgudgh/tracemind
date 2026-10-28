#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AWR报告解析器模块 - 完善版
用于解析Oracle AWR报告（HTML和TEXT格式），提取关键信息

作者: 青学会MOP技术社区
日期: 2025-04-16
"""

import re
import os
import logging
from datetime import datetime

# 优雅处理依赖包缺失的情况
try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False
    BeautifulSoup = None

try:
    import pandas as pd
    PANDAS_AVAILABLE = True
except ImportError:
    PANDAS_AVAILABLE = False
    pd = None

# 设置日志
logger = logging.getLogger("青学会MOP技术社区")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

class AWRReportParser:
    """
    AWR报告解析器类
    支持解析HTML和TEXT格式的AWR报告
    """
    
    def __init__(self, file_path):
        """
        初始化解析器
        
        参数:
            file_path (str): AWR报告文件路径
        """
        self.file_path = file_path
        self.file_type = self._determine_file_type()
        self.content = None
        self.soup = None
        self.parsed_data = {
            'database_info': {},
            'snapshot_info': {},
            'load_profile': {},
            'instance_efficiency': {},
            'top_timed_events': [],
            'host_cpu': {},
            'instance_cpu': {},
            'memory_stats': {},
            'io_profile': {},
            'sql_stats': [],
            'wait_events': [],
            'advisory_stats': [],
            'execution_plans': []
        }
        logger.info(f"初始化AWR报告解析器，文件: {file_path}, 类型: {self.file_type}")
    
    def _determine_file_type(self):
        """
        确定文件类型（HTML或TEXT）
        
        返回:
            str: 'html' 或 'text'
        """
        _, ext = os.path.splitext(self.file_path)
        if ext.lower() in ['.html', '.htm']:
            return 'html'
        else:
            return 'text'
    
    def load_file(self):
        """
        加载AWR报告文件
        
        返回:
            bool: 加载成功返回True，否则返回False
        """
        try:
            with open(self.file_path, 'rb') as f:
                first_bytes = f.read(4)
            if first_bytes[:3] == b'\xEF\xBB\xBF':
                with open(self.file_path, 'r', encoding='utf-8') as f:
                    self.content = f.read()
            else:
                try:
                    with open(self.file_path, 'r', encoding='utf-8') as f:
                        self.content = f.read()
                except UnicodeDecodeError:
                    with open(self.file_path, 'r', encoding='utf-8', errors='replace') as f:
                        self.content = f.read()
            
            if self.file_type == 'html':
                if not BS4_AVAILABLE:
                    logger.warning("BeautifulSoup4未安装，无法解析HTML格式的AWR报告")
                    return False
                self.soup = BeautifulSoup(self.content, 'html.parser')  # 使用内置解析器
            
            logger.info(f"成功加载AWR报告文件: {self.file_path}")
            return True
        except Exception as e:
            logger.error(f"加载AWR报告文件失败: {str(e)}")
            return False
    
    def parse(self):
        """
        解析AWR报告
        
        返回:
            dict: 解析后的数据
        """
        if not self.content:
            success = self.load_file()
            if not success:
                return None
        
        logger.info("开始解析AWR报告")
        
        if self.file_type == 'html':
            self._parse_html()
        else:
            self._parse_text()
        
        logger.info("AWR报告解析完成")
        return self.parsed_data
    
    def _parse_html(self):
        """解析HTML格式的AWR报告"""
        self._parse_database_info_html()
        self._parse_snapshot_info_html()
        self._parse_load_profile_html()
        self._parse_instance_efficiency_html()
        self._parse_top_timed_events_html()
        self._parse_host_cpu_html()
        self._parse_instance_cpu_html()
        self._parse_memory_stats_html()
        self._parse_io_profile_html()
        self._parse_sql_stats_html()
        self._parse_wait_events_html()
        self._parse_advisory_stats_html()
        self._parse_execution_plans_html()
    
    def _parse_text(self):
        """解析TEXT格式的AWR报告"""
        self._parse_database_info_text()
        self._parse_snapshot_info_text()
        self._parse_load_profile_text()
        self._parse_instance_efficiency_text()
        self._parse_top_timed_events_text()
        self._parse_host_cpu_text()
        self._parse_instance_cpu_text()
        self._parse_memory_stats_text()
        self._parse_io_profile_text()
        self._parse_sql_stats_text()
        self._parse_wait_events_text()
        self._parse_advisory_stats_text()
        self._parse_execution_plans_text()
    
    def _parse_database_info_html(self):
        """解析HTML格式的数据库信息部分"""
        try:
            # 初始化数据库信息字典
            db_info = {}
            
            tables = self.soup.find_all('table')
            
            # 方法1: 查找包含DB Name的表格
            for table in tables:
                # 查找包含DB Name, DB Id, Instance等关键字的表头
                header_row = None
                data_row = None
                
                rows = table.find_all('tr')
                for i, row in enumerate(rows):
                    cells = row.find_all(['th', 'td'])
                    row_text = ' '.join([cell.get_text().strip() for cell in cells])
                    
                    # 检查是否包含数据库信息的关键字
                    if re.search(r'DB Name|Database|Instance', row_text, re.IGNORECASE):
                        if any(cell.name == 'th' for cell in cells):  # 这是表头行
                            header_row = cells
                            # 查找对应的数据行
                            if i + 1 < len(rows):
                                data_row = rows[i + 1].find_all(['th', 'td'])
                        break
                
                # 如果找到表头和数据行，进行解析
                if header_row and data_row:
                    for j, header_cell in enumerate(header_row):
                        if j < len(data_row):
                            key = header_cell.get_text().strip()
                            value = data_row[j].get_text().strip()
                            if key and value:
                                db_info[key] = value
                    break  # 找到第一个匹配的表格就退出
            
            # 方法2: 如果方法1失败，尝试查找其他格式的数据库信息
            if not db_info:
                for table in tables:
                    rows = table.find_all('tr')
                    for row in rows:
                        cells = row.find_all(['th', 'td'])
                        if len(cells) >= 2:
                            key = cells[0].get_text().strip()
                            value = cells[1].get_text().strip()
                            
                            # 检查是否是数据库相关信息
                            if re.search(r'DB Name|Database|Instance|Host', key, re.IGNORECASE):
                                db_info[key] = value
            
            # 方法3: 从文本中直接提取
            if not db_info:
                content_text = self.soup.get_text()
                
                # 使用正则表达式提取数据库名和实例名
                db_name_match = re.search(r'DB Name[:\s]+(\w+)', content_text, re.IGNORECASE)
                if db_name_match:
                    db_info['DB Name'] = db_name_match.group(1)
                
                instance_match = re.search(r'Instance[:\s]+(\w+)', content_text, re.IGNORECASE)
                if instance_match:
                    db_info['Instance'] = instance_match.group(1)
                
                host_match = re.search(r'Host[:\s]+([^\s\n]+)', content_text, re.IGNORECASE)
                if host_match:
                    db_info['Host'] = host_match.group(1)
            
            # 标准化键名并保存到parsed_data
            self.parsed_data['database_info'] = {
                'database_name': db_info.get('DB Name') or db_info.get('Database') or db_info.get('database_name'),
                'instance_name': db_info.get('Instance') or db_info.get('instance_name'),
                'host_name': db_info.get('Host') or db_info.get('Host Name') or db_info.get('host_name'),
                'db_id': db_info.get('DB Id') or db_info.get('db_id'),
                'release': db_info.get('Release') or db_info.get('release'),
                'rac': db_info.get('RAC') or db_info.get('rac')
            }
            
            # 移除None值
            self.parsed_data['database_info'] = {k: v for k, v in self.parsed_data['database_info'].items() if v}
            
            logger.info(f"成功解析数据库信息部分: {self.parsed_data['database_info']}")
            
        except Exception as e:
            logger.error(f"解析数据库信息部分失败: {str(e)}")
            # 确保至少有空字典
            self.parsed_data['database_info'] = {}
    
    def _parse_snapshot_info_html(self):
        """解析HTML格式的快照信息部分"""
        try:
            from datetime import datetime
            import re
            
            # 初始化快照信息字典
            snap_info = {}
            
            tables = self.soup.find_all('table')
            
            # 方法1: 查找包含快照信息的表格
            for table in tables:
                if table.find('th', string=re.compile('Snap Id|Snapshot|Begin Snap|End Snap')):
                    rows = table.find_all('tr')
                    for row in rows:
                        cells = row.find_all(['th', 'td'])
                        if len(cells) >= 2:
                            key = cells[0].get_text().strip()
                            value = cells[1].get_text().strip()
                            if key and value:
                                snap_info[key] = value
            
            # 方法2: 查找包含Begin Snap和End Snap的表格
            for table in tables:
                rows = table.find_all('tr')
                for row in rows:
                    cells = row.find_all(['th', 'td'])
                    row_text = ' '.join([cell.get_text().strip() for cell in cells])
                    
                    # 查找Begin Snap行
                    if 'Begin Snap:' in row_text:
                        parts = [cell.get_text().strip() for cell in cells]
                        if len(parts) >= 4:
                            snap_info['Begin Snap Id'] = parts[1] if len(parts) > 1 else ''
                            snap_info['Begin Snap Time'] = parts[2] if len(parts) > 2 else ''
                    
                    # 查找End Snap行
                    elif 'End Snap:' in row_text:
                        parts = [cell.get_text().strip() for cell in cells]
                        if len(parts) >= 4:
                            snap_info['End Snap Id'] = parts[1] if len(parts) > 1 else ''
                            snap_info['End Snap Time'] = parts[2] if len(parts) > 2 else ''
                    
                    # 查找Elapsed和DB Time
                    elif 'Elapsed:' in row_text or 'DB Time:' in row_text:
                        row_data = [cell.get_text().strip() for cell in cells]
                        for i in range(0, len(row_data), 2):
                            if i + 1 < len(row_data):
                                snap_info[row_data[i]] = row_data[i + 1]
            
            # 方法3: 从文本内容中直接提取快照时间
            if not snap_info.get('Begin Snap Time') or not snap_info.get('End Snap Time'):
                content_text = self.soup.get_text()
                
                # 使用正则表达式提取快照时间
                begin_match = re.search(r'Begin Snap:\s*\d+\s+([^\s]+\s+[^\s]+)', content_text)
                if begin_match:
                    snap_info['Begin Snap Time'] = begin_match.group(1)
                
                end_match = re.search(r'End Snap:\s*\d+\s+([^\s]+\s+[^\s]+)', content_text)
                if end_match:
                    snap_info['End Snap Time'] = end_match.group(1)
            
            # 标准化时间格式并保存
            self.parsed_data['snapshot_info'] = self._standardize_snapshot_info(snap_info)
            
            logger.info(f"成功解析快照信息部分: {self.parsed_data['snapshot_info']}")
            
        except Exception as e:
            logger.error(f"解析快照信息部分失败: {str(e)}")
            # 确保至少有空字典
            self.parsed_data['snapshot_info'] = {}
    
    def _parse_load_profile_html(self):
        """解析HTML格式的负载概况部分"""
        try:
            # 查找包含负载概况的表格
            tables = self.soup.find_all('table')
            for table in tables:
                if table.find('th', text=re.compile('Load Profile|Per Second|Per Transaction')):
                    rows = table.find_all('tr')
                    headers = []
                    for row in rows:
                        cells = row.find_all(['th', 'td'])
                        if len(cells) > 1:
                            if not headers and 'Per Second' in cells[1].get_text():
                                headers = [cell.get_text().strip() for cell in cells]
                            else:
                                metric = cells[0].get_text().strip()
                                values = [cell.get_text().strip() for cell in cells[1:]]
                                if metric and values:
                                    metric_data = {}
                                    for i, value in enumerate(values):
                                        if i < len(headers) - 1:
                                            metric_data[headers[i+1]] = value
                                    self.parsed_data['load_profile'][metric] = metric_data
            
            logger.info("成功解析负载概况部分")
        except Exception as e:
            logger.error(f"解析负载概况部分失败: {str(e)}")
    
    def _parse_instance_efficiency_html(self):
        """解析HTML格式的实例效率部分"""
        try:
            # 查找包含实例效率的表格
            """
            tables = self.soup.find_all('table')
            for table in tables:
                if table.find('th', text=re.compile('Instance Efficiency|Percentage')):
                    rows = table.find_all('tr')
                    for row in rows:
                        cells = row.find_all(['th', 'td'])
                        if len(cells) >= 2:
                            key = cells[0].get_text().strip()
                            value = cells[1].get_text().strip()
                            if key and value:
                                self.parsed_data['instance_efficiency'][key] = value
            """
            instance_list = []
            tables = self.soup.find_all('table')
            for table in tables:
                if table.find('td', string=re.compile('Buffer Nowait %:')):
                    rows = table.find_all('tr')
                    for row in rows:
                        cells = row.find_all(['th', 'td'])
                        row_data = [cell.get_text().strip() for cell in cells]
                        instance_list.append(row_data)

            result_dict = {}
            for sublist in instance_list:
                keys = sublist[::2]  # 提取索引0,2的元素作为键
                values = sublist[1::2]  # 提取索引1,3的元素作为值
                result_dict.update(zip(keys, values))
                self.parsed_data['instance_efficiency'] = result_dict

            logger.info("成功解析实例效率部分")
        except Exception as e:
            logger.error(f"解析实例效率部分失败: {str(e)}")
    
    def _parse_top_timed_events_html(self):
        """解析HTML格式的前5个耗时事件部分"""
        try:
            # 查找包含前5个耗时事件的表格
            """
            tables = self.soup.find_all('table')
            for table in tables:
                if table.find('th', string=re.compile('Total Wait Time (sec)|Wait Avg(ms)|% DB time')):
                    rows = table.find_all('tr')
                    headers = []
                    for row in rows:
                        cells = row.find_all(['th', 'td'])
                        if len(cells) > 1:
                            if not headers and 'Event' in cells[0].get_text():
                                headers = [cell.get_text().strip() for cell in cells]
                            elif headers:
                                event_data = {}
                                for i, cell in enumerate(cells):
                                    if i < len(headers):
                                        event_data[headers[i]] = cell.get_text().strip()
                                if event_data:
                                    self.parsed_data['top_timed_events'].append(event_data)
            """
            data_list = []
            tables = self.soup.find_all('table')
            for table in tables:
                if table.find('th', string=re.compile('Total Wait Time (sec)|Wait Avg(ms)|% DB time')):
                    rows = table.find_all('tr')
                    headers = []
                    for row in rows:
                        cells = row.find_all(['th', 'td'])
                        if len(cells) > 1:
                            if not headers and 'Event' in cells[0].get_text():
                                headers = [cell.get_text().strip() for cell in cells]
                            elif headers:
                                event_data = {}
                                for i, cell in enumerate(cells):
                                    if i < len(headers):
                                        event_data[headers[i]] = cell.get_text().strip()
                                if event_data:
                                    data_list.append(event_data)
            new_list = data_list[:10]
            self.parsed_data['top_timed_events'] = (new_list)

            logger.info("成功解析前5个耗时事件部分")
        except Exception as e:
            logger.error(f"解析前5个耗时事件部分失败: {str(e)}")
    
    def _parse_host_cpu_html(self):
        """解析HTML格式的主机CPU部分"""
        try:
            """
            # 查找包含主机CPU信息的表格
            tables = self.soup.find_all('table')
            for table in tables:
                if table.find('th', text=re.compile('Host CPU|CPUs|Cores|Sockets')):
                    rows = table.find_all('tr')
                    for row in rows:
                        cells = row.find_all(['th', 'td'])
                        if len(cells) >= 2:
                            key = cells[0].get_text().strip()
                            value = cells[1].get_text().strip()
                            if key and value:
                                self.parsed_data['host_cpu'][key] = value
            
            # 查找CPU使用率表格
            for table in tables:
                if table.find('th', text=re.compile('Load Average|%User|%System|%Idle')):
                    rows = table.find_all('tr')
                    headers = []
                    for row in rows:
                        cells = row.find_all(['th', 'td'])
                        if len(cells) > 1:
                            if not headers and 'Load Average' in cells[0].get_text():
                                headers = [cell.get_text().strip() for cell in cells]
                            elif headers and len(cells) == len(headers):
                                for i, cell in enumerate(cells):
                                    self.parsed_data['host_cpu'][headers[i]] = cell.get_text().strip()
            """
            data_list = []
            tables = self.soup.find_all('table')
            for table in tables:
                if table.find('th', string=re.compile('Load Average|%User|%System|%Idle')):
                    rows = table.find_all('tr')
                    for row in rows:
                        cells = row.find_all(['th', 'td'])
                        row_data = [cell.get_text().strip() for cell in cells]
                        data_list.append(row_data)
            new_data_list = data_list[:2]
            keys = new_data_list[0]  # 第一行作为 Key
            values = new_data_list[1]  # 第二行作为 Value
            cpu_dict = dict(zip(keys, values))  # 转换字典

            self.parsed_data['host_cpu'] = cpu_dict
            
            logger.info("成功解析主机CPU部分")
        except Exception as e:
            logger.error(f"解析主机CPU部分失败: {str(e)}")
    
    def _parse_instance_cpu_html(self):
        """解析HTML格式的实例CPU部分"""
        try:
            # 查找包含实例CPU信息的表格
            tables = self.soup.find_all('table')
            for table in tables:
                if table.find('th', text=re.compile('Instance CPU|%Total CPU|%Busy CPU')):
                    rows = table.find_all('tr')
                    headers = []
                    for row in rows:
                        cells = row.find_all(['th', 'td'])
                        if len(cells) > 1:
                            if not headers and '%Total CPU' in cells[0].get_text():
                                headers = [cell.get_text().strip() for cell in cells]
                            elif headers and len(cells) == len(headers):
                                for i, cell in enumerate(cells):
                                    self.parsed_data['instance_cpu'][headers[i]] = cell.get_text().strip()
            
            logger.info("成功解析实例CPU部分")
        except Exception as e:
            logger.error(f"解析实例CPU部分失败: {str(e)}")
    
    def _parse_memory_stats_html(self):
        """解析HTML格式的内存统计部分"""
        try:
            memory_stats = {}
            tables = self.soup.find_all('table')
            logger.info(f"开始解析HTML内存统计，找到 {len(tables)} 个表格")
            
            # 方法1: 查找包含"Memory Statistics"的表格
            for table in tables:
                # 查找Memory Statistics表格
                prev_text = ""
                if table.previous_sibling:
                    prev_elements = table.find_all_previous(['p', 'h2', 'h3'], limit=3)
                    for elem in prev_elements:
                        prev_text = elem.get_text().strip()
                        if "Memory Statistics" in prev_text:
                            logger.info("找到Memory Statistics表格")
                            self._parse_memory_basic_table(table, memory_stats)
                            break
                        if "Cache Sizes" in prev_text:
                            logger.info("找到Cache Sizes表格")
                            self._parse_cache_sizes_table(table, memory_stats)
                            break
            
            # 方法2: 通过表格内容直接匹配
            for table in tables:
                rows = table.find_all('tr')
                for row in rows:
                    cells = row.find_all(['th', 'td'])
                    if len(cells) >= 2:
                        first_cell_text = cells[0].get_text().strip()
                        
                        # 匹配主机内存相关信息
                        if re.match(r'Host Mem \(MB\):', first_cell_text):
                            logger.info("找到Host Memory信息")
                            self._parse_memory_basic_table(table, memory_stats)
                            break
                        
                        # 匹配Buffer Cache信息
                        elif re.match(r'Buffer Cache:', first_cell_text):
                            logger.info("找到Buffer Cache信息")
                            self._parse_cache_sizes_table(table, memory_stats)
                            break
                        
                        # 匹配SGA相关信息
                        elif re.match(r'SGA use \(MB\):|Database Buffers|Fixed Size', first_cell_text):
                            logger.info(f"找到SGA相关信息: {first_cell_text}")
                            self._parse_memory_basic_table(table, memory_stats)
                            break
            
            # 方法3: 查找Memory Dynamic Components表格
            self._parse_memory_dynamic_components(memory_stats)
            
            # 保存解析结果
            self.parsed_data['memory_stats'] = memory_stats
            logger.info(f"成功解析内存统计部分，获得 {len(memory_stats)} 个内存指标")
            
            # 调试输出
            if memory_stats:
                logger.info(f"内存统计指标: {list(memory_stats.keys())}")
            else:
                logger.warning("未能解析到任何内存统计信息")
                
        except Exception as e:
            logger.error(f"解析内存统计部分失败: {str(e)}")
            import traceback
            logger.error(f"错误详情: {traceback.format_exc()}")
            self.parsed_data['memory_stats'] = {}
    
    def _parse_memory_basic_table(self, table, memory_stats):
        """解析基本内存统计表格"""
        try:
            rows = table.find_all('tr')
            headers = []
            
            for row in rows:
                cells = row.find_all(['th', 'td'])
                if len(cells) >= 2:
                    # 检查是否是表头行
                    if not headers and any(cell.name == 'th' for cell in cells):
                        headers = [cell.get_text().strip() for cell in cells]
                        logger.info(f"找到表头: {headers}")
                        continue
                    
                    # 解析数据行
                    key = cells[0].get_text().strip()
                    if key and ':' in key:  # 确保是有效的键名
                        # 获取Begin和End值（如果有多列）
                        if len(cells) >= 3:
                            begin_value = cells[1].get_text().strip()
                            end_value = cells[2].get_text().strip()
                            memory_stats[f"{key} (Begin)"] = begin_value
                            memory_stats[f"{key} (End)"] = end_value
                            logger.debug(f"解析内存指标: {key} Begin={begin_value}, End={end_value}")
                        else:
                            value = cells[1].get_text().strip()
                            memory_stats[key] = value
                            logger.debug(f"解析内存指标: {key} = {value}")
                            
        except Exception as e:
            logger.error(f"解析基本内存表格失败: {str(e)}")
    
    def _parse_cache_sizes_table(self, table, memory_stats):
        """解析缓存大小表格"""
        try:
            rows = table.find_all('tr')
            
            for row in rows:
                cells = row.find_all(['th', 'td'])
                if len(cells) >= 3:
                    key = cells[0].get_text().strip()
                    if key and ':' in key:
                        begin_value = cells[1].get_text().strip()
                        end_value = cells[2].get_text().strip()
                        
                        # 处理额外的列（如Std Block Size）
                        if len(cells) >= 5:
                            extra_key = cells[3].get_text().strip()
                            extra_value = cells[4].get_text().strip()
                            if extra_key and ':' in extra_key:
                                memory_stats[extra_key] = extra_value
                        
                        memory_stats[f"{key} (Begin)"] = begin_value
                        memory_stats[f"{key} (End)"] = end_value
                        logger.debug(f"解析缓存指标: {key} Begin={begin_value}, End={end_value}")
                        
        except Exception as e:
            logger.error(f"解析缓存大小表格失败: {str(e)}")
    
    def _parse_memory_dynamic_components(self, memory_stats):
        """解析内存动态组件信息"""
        try:
            tables = self.soup.find_all('table')
            
            for table in tables:
                # 查找前面的文本，确认这是Memory Dynamic Components表格
                prev_elements = table.find_all_previous(['h3', 'p'], limit=5)
                is_memory_dynamic = False
                
                for elem in prev_elements:
                    if "Memory Dynamic Components" in elem.get_text():
                        is_memory_dynamic = True
                        break
                
                if is_memory_dynamic:
                    logger.info("找到Memory Dynamic Components表格")
                    rows = table.find_all('tr')
                    headers = []
                    
                    for row in rows:
                        cells = row.find_all(['th', 'td'])
                        if len(cells) >= 2:
                            if not headers and any(cell.name == 'th' for cell in cells):
                                headers = [cell.get_text().strip() for cell in cells]
                                continue
                            
                            # 解析组件数据
                            component_name = cells[0].get_text().strip()
                            if component_name and len(cells) >= 3:
                                current_size = cells[2].get_text().strip() if len(cells) > 2 else ""
                                if current_size and current_size != "0.00":
                                    memory_stats[f"Component {component_name} Size (MB)"] = current_size
                                    logger.debug(f"解析内存组件: {component_name} = {current_size}MB")
                    break
                    
        except Exception as e:
            logger.error(f"解析内存动态组件失败: {str(e)}")
    
    def _parse_io_profile_html(self):
        """解析HTML格式的IO概况部分"""
        try:
            # 查找包含IO概况的表格
            tables = self.soup.find_all('table')
            for table in tables:
                if table.find('th', text=re.compile('Read+Write Per Second|Read per Second')):
                    rows = table.find_all('tr')
                    headers = []
                    for row in rows:
                        cells = row.find_all(['th', 'td'])
                        if len(cells) > 1:
                            if not headers and ('Per Second' in cells[1].get_text() or 'Read IO' in cells[0].get_text()):
                                headers = [cell.get_text().strip() for cell in cells]
                            else:
                                metric = cells[0].get_text().strip()
                                values = [cell.get_text().strip() for cell in cells[1:]]
                                if metric and values:
                                    metric_data = {}
                                    for i, value in enumerate(values):
                                        if i < len(headers) - 1:
                                            metric_data[headers[i+1]] = value
                                    self.parsed_data['io_profile'][metric] = metric_data
            
            logger.info("成功解析IO概况部分")
        except Exception as e:
            logger.error(f"解析IO概况部分失败: {str(e)}")
    
    def _parse_sql_stats_html(self):
        """解析HTML格式的SQL统计部分"""
        try:
            # 查找包含SQL统计的表格
            tables = self.soup.find_all('table')
            for table in tables:
                if table.find('th', text=re.compile('SQL ordered by|SQL Statement|Executions|CPU Time')):
                    rows = table.find_all('tr')
                    headers = []
                    for row in rows:
                        cells = row.find_all(['th', 'td'])
                        if len(cells) > 1:
                            if not headers and 'SQL Id' in ' '.join([cell.get_text() for cell in cells]):
                                headers = [cell.get_text().strip() for cell in cells]
                            elif headers:
                                sql_data = {}
                                for i, cell in enumerate(cells):
                                    if i < len(headers):
                                        sql_data[headers[i]] = cell.get_text().strip()
                                if sql_data:
                                    self.parsed_data['sql_stats'].append(sql_data)
            
            logger.info("成功解析SQL统计部分")
        except Exception as e:
            logger.error(f"解析SQL统计部分失败: {str(e)}")
    
    def _parse_wait_events_html(self):
        """解析HTML格式的等待事件部分"""
        try:
            # 查找包含等待事件的表格
            tables = self.soup.find_all('table')
            for table in tables:
                if table.find('th', text=re.compile('Wait Events|Event|Waits|Time')):
                    rows = table.find_all('tr')
                    headers = []
                    for row in rows:
                        cells = row.find_all(['th', 'td'])
                        if len(cells) > 1:
                            if not headers and 'Event' in cells[0].get_text():
                                headers = [cell.get_text().strip() for cell in cells]
                            elif headers:
                                event_data = {}
                                for i, cell in enumerate(cells):
                                    if i < len(headers):
                                        event_data[headers[i]] = cell.get_text().strip()
                                if event_data:
                                    self.parsed_data['wait_events'].append(event_data)
            
            logger.info("成功解析等待事件部分")
        except Exception as e:
            logger.error(f"解析等待事件部分失败: {str(e)}")
    
    def _parse_advisory_stats_html(self):
        """解析HTML格式的建议统计部分"""
        try:
            # 查找包含建议统计的表格
            tables = self.soup.find_all('table')
            for table in tables:
                if table.find('th', text=re.compile('Advisory|Buffer|PGA|SGA|Shared Pool')):
                    rows = table.find_all('tr')
                    headers = []
                    for row in rows:
                        cells = row.find_all(['th', 'td'])
                        if len(cells) > 1:
                            if not headers:
                                headers = [cell.get_text().strip() for cell in cells]
                            else:
                                advisory_data = {}
                                for i, cell in enumerate(cells):
                                    if i < len(headers):
                                        advisory_data[headers[i]] = cell.get_text().strip()
                                if advisory_data:
                                    self.parsed_data['advisory_stats'].append(advisory_data)
            
            logger.info("成功解析建议统计部分")
        except Exception as e:
            logger.error(f"解析建议统计部分失败: {str(e)}")
    
    def _parse_database_info_text(self):
        """解析TEXT格式的数据库信息部分"""
        try:
            # 初始化数据库信息字典
            db_info = {}
            
            # 方法1: 使用正则表达式查找数据库信息部分
            db_info_pattern = r'DB Name\s+DB Id\s+Instance\s+Inst Num\s+Release\s+RAC\s+Host\s+([\s\S]+?)(?=\n\n)'
            match = re.search(db_info_pattern, self.content)
            if match:
                db_info_text = match.group(1)
                # 解析数据库信息
                lines = db_info_text.strip().split('\n')
                for line in lines:
                    parts = re.split(r'\s{2,}', line.strip())
                    if len(parts) >= 6:
                        db_info['DB Name'] = parts[0]
                        db_info['DB Id'] = parts[1]
                        db_info['Instance'] = parts[2]
                        db_info['Inst Num'] = parts[3]
                        db_info['Release'] = parts[4]
                        db_info['RAC'] = parts[5]
                        if len(parts) > 6:
                            db_info['Host'] = parts[6]
                        break  # 找到第一行数据后退出
            
            # 方法2: 如果方法1失败，尝试其他模式
            if not db_info:
                # 查找简化的数据库信息格式
                patterns = [
                    r'Database\s*:\s*(\w+)',
                    r'DB Name\s*:\s*(\w+)',
                    r'Instance\s*:\s*(\w+)',
                    r'Host\s*:\s*([^\s\n]+)'
                ]
                
                for pattern in patterns:
                    matches = re.findall(pattern, self.content, re.IGNORECASE)
                    if matches:
                        if 'Database' in pattern or 'DB Name' in pattern:
                            db_info['DB Name'] = matches[0]
                        elif 'Instance' in pattern:
                            db_info['Instance'] = matches[0]
                        elif 'Host' in pattern:
                            db_info['Host'] = matches[0]
            
            # 方法3: 从AWR报告标题中提取信息
            if not db_info:
                # 查找AWR报告标题中的数据库信息
                title_patterns = [
                    r'WORKLOAD REPOSITORY report for\s+(\w+)',
                    r'AWR Report for DB:\s*(\w+)',
                    r'Instance:\s*(\w+)'
                ]
                
                for pattern in title_patterns:
                    match = re.search(pattern, self.content, re.IGNORECASE)
                    if match:
                        if 'DB:' in pattern or 'WORKLOAD' in pattern:
                            db_info['DB Name'] = match.group(1)
                        elif 'Instance:' in pattern:
                            db_info['Instance'] = match.group(1)
            
            # 标准化键名并保存到parsed_data
            self.parsed_data['database_info'] = {
                'database_name': db_info.get('DB Name') or db_info.get('Database') or db_info.get('database_name'),
                'instance_name': db_info.get('Instance') or db_info.get('instance_name'),
                'host_name': db_info.get('Host') or db_info.get('Host Name') or db_info.get('host_name'),
                'db_id': db_info.get('DB Id') or db_info.get('db_id'),
                'release': db_info.get('Release') or db_info.get('release'),
                'rac': db_info.get('RAC') or db_info.get('rac')
            }
            
            # 移除None值
            self.parsed_data['database_info'] = {k: v for k, v in self.parsed_data['database_info'].items() if v}
            
            logger.info(f"成功解析数据库信息部分: {self.parsed_data['database_info']}")
            
        except Exception as e:
            logger.error(f"解析数据库信息部分失败: {str(e)}")
            # 确保至少有空字典
            self.parsed_data['database_info'] = {}
    
    def _parse_snapshot_info_text(self):
        """解析TEXT格式的快照信息部分"""
        try:
            # 初始化快照信息字典
            snap_info = {}
            
            # 使用正则表达式查找快照信息部分
            snap_info_pattern = r'Snap Id\s+Snap Time\s+Sessions\s+Cursors/Session\s+([\s\S]+?)(?=\n\n)'
            match = re.search(snap_info_pattern, self.content)
            if match:
                snap_info_text = match.group(1)
                # 解析快照信息
                lines = snap_info_text.strip().split('\n')
                begin_snap_line = None
                end_snap_line = None
                
                for line in lines:
                    if 'Begin Snap:' in line:
                        begin_snap_line = line
                    elif 'End Snap:' in line:
                        end_snap_line = line
                    elif 'Elapsed:' in line:
                        parts = re.split(r'\s{2,}', line.strip())
                        if len(parts) >= 2:
                            snap_info['Elapsed'] = parts[1]
                    elif 'DB Time:' in line:
                        parts = re.split(r'\s{2,}', line.strip())
                        if len(parts) >= 2:
                            snap_info['DB Time'] = parts[1]
                
                if begin_snap_line:
                    parts = re.split(r'\s{2,}', begin_snap_line.strip())
                    if len(parts) >= 5:
                        snap_info['Begin Snap Id'] = parts[1]
                        snap_info['Begin Snap Time'] = parts[2]
                        snap_info['Begin Sessions'] = parts[3]
                        snap_info['Begin Cursors/Session'] = parts[4]
                
                if end_snap_line:
                    parts = re.split(r'\s{2,}', end_snap_line.strip())
                    if len(parts) >= 5:
                        snap_info['End Snap Id'] = parts[1]
                        snap_info['End Snap Time'] = parts[2]
                        snap_info['End Sessions'] = parts[3]
                        snap_info['End Cursors/Session'] = parts[4]
            
            # 标准化时间格式并保存
            self.parsed_data['snapshot_info'] = self._standardize_snapshot_info(snap_info)
            
            logger.info(f"成功解析快照信息部分: {self.parsed_data['snapshot_info']}")
            
        except Exception as e:
            logger.error(f"解析快照信息部分失败: {str(e)}")
            # 确保至少有空字典
            self.parsed_data['snapshot_info'] = {}
    
    def _parse_load_profile_text(self):
        """解析TEXT格式的负载概况部分"""
        try:
            # 使用正则表达式查找负载概况部分
            load_profile_pattern = r'Load Profile\s+Per Second\s+Per Transaction\s+Per Exec\s+Per Call\s+~+\s+([\s\S]+?)(?=\n\n)'
            match = re.search(load_profile_pattern, self.content)
            if match:
                load_profile_text = match.group(1)
                # 解析负载概况
                lines = load_profile_text.strip().split('\n')
                for line in lines:
                    parts = re.split(r'\s{2,}', line.strip())
                    if len(parts) >= 2:
                        metric = parts[0]
                        values = parts[1:]
                        if metric and values:
                            metric_data = {}
                            headers = ['Per Second', 'Per Transaction', 'Per Exec', 'Per Call']
                            for i, value in enumerate(values):
                                if i < len(headers):
                                    metric_data[headers[i]] = value
                            self.parsed_data['load_profile'][metric] = metric_data
            
            logger.info("成功解析负载概况部分")
        except Exception as e:
            logger.error(f"解析负载概况部分失败: {str(e)}")
    
    def _parse_instance_efficiency_text(self):
        """解析TEXT格式的实例效率部分"""
        try:
            # 使用正则表达式查找实例效率部分
            efficiency_pattern = r'Instance Efficiency Percentages[^\n]+\s+~+\s+([\s\S]+?)(?=\n\n)'
            match = re.search(efficiency_pattern, self.content)
            if match:
                efficiency_text = match.group(1)
                # 解析实例效率
                lines = efficiency_text.strip().split('\n')
                for line in lines:
                    parts = re.split(r'\s{2,}', line.strip())
                    if len(parts) >= 4:
                        self.parsed_data['instance_efficiency'][parts[0]] = parts[1]
                        self.parsed_data['instance_efficiency'][parts[2]] = parts[3]
            
            logger.info("成功解析实例效率部分")
        except Exception as e:
            logger.error(f"解析实例效率部分失败: {str(e)}")
    
    def _parse_top_timed_events_text(self):
        """解析TEXT格式的前5个耗时事件部分"""
        try:
            # 使用正则表达式查找前5个耗时事件部分
            events_pattern = r'Top 5 Timed Events[^\n]+\s+~+\s+[^\n]+\s+[^\n]+\s+([\s\S]+?)(?=\n\n)'
            match = re.search(events_pattern, self.content)
            if match:
                events_text = match.group(1)
                # 解析前5个耗时事件
                lines = events_text.strip().split('\n')
                headers = ['Event', 'Waits', 'Time(s)', 'Avg wait (ms)', '% DB time', 'Wait Class']
                for line in lines:
                    parts = re.split(r'\s{2,}', line.strip())
                    if len(parts) >= 1:
                        event_data = {}
                        event_name = parts[0]
                        event_data['Event'] = event_name
                        
                        # 处理特殊情况：DB CPU行没有Waits值
                        if event_name == 'DB CPU':
                            if len(parts) >= 2:
                                event_data['Time(s)'] = parts[1]
                            if len(parts) >= 3:
                                event_data['% DB time'] = parts[2]
                        else:
                            if len(parts) >= 2:
                                event_data['Waits'] = parts[1]
                            if len(parts) >= 3:
                                event_data['Time(s)'] = parts[2]
                            if len(parts) >= 4:
                                event_data['Avg wait (ms)'] = parts[3]
                            if len(parts) >= 5:
                                event_data['% DB time'] = parts[4]
                            if len(parts) >= 6:
                                event_data['Wait Class'] = parts[5]
                        
                        if event_data:
                            self.parsed_data['top_timed_events'].append(event_data)
            
            logger.info("成功解析前5个耗时事件部分")
        except Exception as e:
            logger.error(f"解析前5个耗时事件部分失败: {str(e)}")
    
    def _parse_host_cpu_text(self):
        """解析TEXT格式的主机CPU部分"""
        try:
            # 使用正则表达式查找主机CPU部分
            host_cpu_pattern = r'Host CPU[^\n]+\s+~+\s+[^\n]+\s+[^\n]+\s+([\s\S]+?)(?=\n\n)'
            match = re.search(host_cpu_pattern, self.content)
            if match:
                host_cpu_text = match.group(1)
                # 解析主机CPU信息
                lines = host_cpu_text.strip().split('\n')
                if lines:
                    parts = re.split(r'\s{2,}', lines[0].strip())
                    if len(parts) >= 6:
                        self.parsed_data['host_cpu']['Load Average Begin'] = parts[0]
                        self.parsed_data['host_cpu']['Load Average End'] = parts[1]
                        self.parsed_data['host_cpu']['%User'] = parts[2]
                        self.parsed_data['host_cpu']['%System'] = parts[3]
                        self.parsed_data['host_cpu']['%WIO'] = parts[4]
                        self.parsed_data['host_cpu']['%Idle'] = parts[5]
            
            # 提取CPU核心信息
            cpu_cores_pattern = r'Host CPU \(CPUs: (\d+) Cores: (\d+) Sockets: (\d+)\)'
            match = re.search(cpu_cores_pattern, self.content)
            if match:
                self.parsed_data['host_cpu']['CPUs'] = match.group(1)
                self.parsed_data['host_cpu']['Cores'] = match.group(2)
                self.parsed_data['host_cpu']['Sockets'] = match.group(3)
            
            logger.info("成功解析主机CPU部分")
        except Exception as e:
            logger.error(f"解析主机CPU部分失败: {str(e)}")
    
    def _parse_instance_cpu_text(self):
        """解析TEXT格式的实例CPU部分"""
        try:
            # 使用正则表达式查找实例CPU部分
            instance_cpu_pattern = r'Instance CPU\s+~+\s+[^\n]+\s+[^\n]+\s+([\s\S]+?)(?=\n\n)'
            match = re.search(instance_cpu_pattern, self.content)
            if match:
                instance_cpu_text = match.group(1)
                # 解析实例CPU信息
                lines = instance_cpu_text.strip().split('\n')
                if lines:
                    parts = re.split(r'\s{2,}', lines[0].strip())
                    if len(parts) >= 3:
                        self.parsed_data['instance_cpu']['%Total CPU'] = parts[0]
                        self.parsed_data['instance_cpu']['%Busy CPU'] = parts[1]
                        self.parsed_data['instance_cpu']['%DB time waiting for CPU'] = parts[2]
            
            logger.info("成功解析实例CPU部分")
        except Exception as e:
            logger.error(f"解析实例CPU部分失败: {str(e)}")
    
    def _parse_memory_stats_text(self):
        """解析TEXT格式的内存统计部分"""
        try:
            memory_stats = {}
            logger.info("开始解析TEXT内存统计")
            
            # 方法1: 查找Memory Statistics部分
            memory_pattern = r'Memory Statistics\s+~+\s+([\s\S]+?)(?=\n\n|\n[A-Z]|\Z)'
            match = re.search(memory_pattern, self.content)
            if match:
                memory_text = match.group(1)
                logger.info("找到Memory Statistics部分")
                self._parse_memory_text_table(memory_text, memory_stats)
            
            # 方法2: 查找Cache Sizes部分
            cache_pattern = r'Cache Sizes\s+~+\s+([\s\S]+?)(?=\n\n|\n[A-Z]|\Z)'
            cache_match = re.search(cache_pattern, self.content)
            if cache_match:
                cache_text = cache_match.group(1)
                logger.info("找到Cache Sizes部分")
                self._parse_memory_text_table(cache_text, memory_stats)
            
            # 方法3: 查找Host Memory, SGA, PGA相关信息
            self._parse_memory_text_scattered(memory_stats)
            
            # 方法4: 查找Memory Dynamic Components
            self._parse_memory_dynamic_text(memory_stats)
            
            # 保存解析结果
            self.parsed_data['memory_stats'] = memory_stats
            logger.info(f"成功解析TEXT内存统计部分，获得 {len(memory_stats)} 个内存指标")
            
            # 调试输出
            if memory_stats:
                logger.info(f"内存统计指标: {list(memory_stats.keys())}")
            else:
                logger.warning("未能解析到任何内存统计信息")
                
        except Exception as e:
            logger.error(f"解析内存统计部分失败: {str(e)}")
            import traceback
            logger.error(f"错误详情: {traceback.format_exc()}")
            self.parsed_data['memory_stats'] = {}
    
    def _parse_memory_text_table(self, text_content, memory_stats):
        """解析TEXT格式的内存统计表格内容"""
        try:
            lines = text_content.strip().split('\n')
            headers = []
            
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                
                # 检查是否是表头行
                if not headers and ('Begin' in line and 'End' in line):
                    headers = re.split(r'\s{2,}', line)
                    logger.debug(f"找到TEXT表头: {headers}")
                    continue
                
                # 解析数据行
                parts = re.split(r'\s{2,}', line)
                if len(parts) >= 2:
                    key = parts[0].strip()
                    if key and (':' in key or 'MB' in key or '%' in key or 'Size' in key):
                        # 如果有多列数据（Begin, End）
                        if len(parts) >= 3 and headers and len(headers) >= 3:
                            begin_value = parts[1].strip()
                            end_value = parts[2].strip()
                            memory_stats[f"{key} (Begin)"] = begin_value
                            memory_stats[f"{key} (End)"] = end_value
                            logger.debug(f"解析TEXT内存指标: {key} Begin={begin_value}, End={end_value}")
                        else:
                            value = parts[1].strip()
                            memory_stats[key] = value
                            logger.debug(f"解析TEXT内存指标: {key} = {value}")
                            
        except Exception as e:
            logger.error(f"解析TEXT内存表格失败: {str(e)}")
    
    def _parse_memory_text_scattered(self, memory_stats):
        """解析散布在文本中的内存信息"""
        try:
            # 查找Host Memory信息
            host_mem_patterns = [
                r'Host Mem \(MB\):\s*([0-9,\.]+)',
                r'Host Memory.*?:\s*([0-9,\.]+)\s*MB',
            ]
            
            for pattern in host_mem_patterns:
                match = re.search(pattern, self.content, re.IGNORECASE)
                if match:
                    memory_stats['Host Mem (MB):'] = match.group(1)
                    logger.debug(f"找到主机内存: {match.group(1)}")
                    break
            
            # 查找SGA和PGA信息
            sga_pga_patterns = [
                (r'SGA use \(MB\):\s*([0-9,\.]+)', 'SGA use (MB):'),
                (r'PGA use \(MB\):\s*([0-9,\.]+)', 'PGA use (MB):'),
                (r'SGA.*?Size.*?:\s*([0-9,\.]+)\s*MB?', 'SGA Size (MB):'),
                (r'PGA.*?Size.*?:\s*([0-9,\.]+)\s*MB?', 'PGA Size (MB):'),
                (r'% Host Mem used for SGA\+PGA:\s*([0-9,\.]+)', '% Host Mem used for SGA+PGA:'),
            ]
            
            for pattern, key in sga_pga_patterns:
                match = re.search(pattern, self.content, re.IGNORECASE)
                if match:
                    memory_stats[key] = match.group(1)
                    logger.debug(f"找到内存指标: {key} = {match.group(1)}")
            
            # 查找缓存大小信息
            cache_patterns = [
                (r'Buffer Cache:\s*([0-9,\.]+[MG])', 'Buffer Cache:'),
                (r'Shared Pool Size:\s*([0-9,\.]+[MG])', 'Shared Pool Size:'),
                (r'Large Pool:\s*([0-9,\.]+[MG])', 'Large Pool:'),
                (r'Java Pool:\s*([0-9,\.]+[MG])', 'Java Pool:'),
                (r'Log Buffer:\s*([0-9,\.]+[KMG])', 'Log Buffer:'),
                (r'Std Block Size:\s*([0-9,\.]+[KMG]?)', 'Std Block Size:'),
            ]
            
            for pattern, key in cache_patterns:
                match = re.search(pattern, self.content, re.IGNORECASE)
                if match:
                    memory_stats[key] = match.group(1)
                    logger.debug(f"找到缓存指标: {key} = {match.group(1)}")
                    
        except Exception as e:
            logger.error(f"解析散布内存信息失败: {str(e)}")
    
    def _parse_memory_dynamic_text(self, memory_stats):
        """解析TEXT格式的内存动态组件"""
        try:
            # 查找Memory Dynamic Components部分
            dynamic_pattern = r'Memory Dynamic Components[^\n]*\n[^\n]*\n[^\n]*\n[^\n]*\n([\s\S]+?)(?=\n\n|\n[A-Z]|\Z)'
            match = re.search(dynamic_pattern, self.content)
            if match:
                dynamic_text = match.group(1)
                logger.info("找到Memory Dynamic Components部分")
                
                lines = dynamic_text.strip().split('\n')
                headers = []
                
                for line in lines:
                    line = line.strip()
                    if not line or line.startswith('-'):
                        continue
                    
                    # 检查是否是表头行
                    if not headers and ('Component' in line and 'Size' in line):
                        headers = re.split(r'\s{2,}', line)
                        continue
                    
                    # 解析组件数据行
                    parts = re.split(r'\s{2,}', line)
                    if len(parts) >= 3:
                        component_name = parts[0].strip()
                        current_size = parts[2].strip() if len(parts) > 2 else ""
                        
                        # 过滤掉大小为0的组件
                        if (component_name and current_size and 
                            current_size not in ['0', '0.00', '0.0', 'N/A']):
                            memory_stats[f"Component {component_name} Size (MB)"] = current_size
                            logger.debug(f"解析TEXT内存组件: {component_name} = {current_size}MB")
                            
        except Exception as e:
            logger.error(f"解析TEXT内存动态组件失败: {str(e)}")
    
    def _parse_io_profile_text(self):
        """解析TEXT格式的IO概况部分"""
        try:
            # 使用正则表达式查找IO概况部分
            io_pattern = r'IO Profile\s+~+\s+([\s\S]+?)(?=\n\n)'
            match = re.search(io_pattern, self.content)
            if match:
                io_text = match.group(1)
                # 解析IO概况
                lines = io_text.strip().split('\n')
                for line in lines:
                    parts = re.split(r'\s{2,}', line.strip())
                    if len(parts) >= 2:
                        metric = parts[0]
                        values = parts[1:]
                        if metric and values:
                            self.parsed_data['io_profile'][metric] = values[0]
            
            logger.info("成功解析IO概况部分")
        except Exception as e:
            logger.error(f"解析IO概况部分失败: {str(e)}")
    
    def _parse_sql_stats_text(self):
        """解析TEXT格式的SQL统计部分"""
        try:
            # 使用正则表达式查找SQL统计部分
            sql_pattern = r'SQL ordered by[^\n]+\s+~+\s+[^\n]+\s+[^\n]+\s+([\s\S]+?)(?=\n\n)'
            match = re.search(sql_pattern, self.content)
            if match:
                sql_text = match.group(1)
                # 解析SQL统计信息
                sql_blocks = re.split(r'\n\s*\n', sql_text.strip())
                for block in sql_blocks:
                    lines = block.strip().split('\n')
                    if len(lines) >= 2:
                        sql_data = {}
                        sql_data['SQL ID'] = lines[0].strip()
                        sql_data['SQL Text'] = lines[1].strip()
                        
                        # 提取统计信息
                        for i in range(2, len(lines)):
                            parts = re.split(r'\s{2,}', lines[i].strip())
                            if len(parts) >= 2:
                                sql_data[parts[0]] = parts[1]
                        
                        if sql_data:
                            self.parsed_data['sql_stats'].append(sql_data)
            
            logger.info("成功解析SQL统计部分")
        except Exception as e:
            logger.error(f"解析SQL统计部分失败: {str(e)}")
    
    def _parse_wait_events_text(self):
        """解析TEXT格式的等待事件部分"""
        try:
            # 使用正则表达式查找等待事件部分
            wait_pattern = r'Wait Events[^\n]+\s+~+\s+[^\n]+\s+[^\n]+\s+([\s\S]+?)(?=\n\n)'
            match = re.search(wait_pattern, self.content)
            if match:
                wait_text = match.group(1)
                # 解析等待事件
                lines = wait_text.strip().split('\n')
                headers = ['Event', 'Waits', 'Total Wait Time (s)', 'Avg wait (ms)', 'Waits /txn', '% bg time', 'Wait Class']
                for line in lines:
                    parts = re.split(r'\s{2,}', line.strip())
                    if len(parts) >= 1:
                        event_data = {}
                        for i, part in enumerate(parts):
                            if i < len(headers):
                                event_data[headers[i]] = part
                        
                        if event_data:
                            self.parsed_data['wait_events'].append(event_data)
            
            logger.info("成功解析等待事件部分")
        except Exception as e:
            logger.error(f"解析等待事件部分失败: {str(e)}")
    
    def _parse_advisory_stats_text(self):
        """解析TEXT格式的建议统计部分"""
        try:
            # 使用正则表达式查找建议统计部分
            advisory_pattern = r'Advisory Statistics\s+~+\s+([\s\S]+?)(?=\n\n)'
            match = re.search(advisory_pattern, self.content)
            if match:
                advisory_text = match.group(1)
                # 解析建议统计
                sections = re.split(r'\n\s*\n', advisory_text.strip())
                for section in sections:
                    lines = section.strip().split('\n')
                    if lines:
                        section_name = lines[0].strip()
                        self.parsed_data['advisory_stats'].append({'Section': section_name, 'Details': lines[1:]})
            
            logger.info("成功解析建议统计部分")
        except Exception as e:
            logger.error(f"解析建议统计部分失败: {str(e)}")
    
    def get_database_info(self):
        """获取数据库信息"""
        return self.parsed_data['database_info']
    
    def get_snapshot_info(self):
        """获取快照信息"""
        return self.parsed_data['snapshot_info']
    
    def get_load_profile(self):
        """获取负载概况"""
        return self.parsed_data['load_profile']
    
    def get_instance_efficiency(self):
        """获取实例效率"""
        return self.parsed_data['instance_efficiency']
    
    def get_top_timed_events(self):
        """获取前5个耗时事件"""
        return self.parsed_data['top_timed_events']
    
    def get_host_cpu(self):
        """获取主机CPU信息"""
        return self.parsed_data['host_cpu']
    
    def get_instance_cpu(self):
        """获取实例CPU信息"""
        return self.parsed_data['instance_cpu']
    
    def get_memory_stats(self):
        """获取内存统计信息"""
        return self.parsed_data['memory_stats']
    
    def get_io_profile(self):
        """获取IO概况"""
        return self.parsed_data['io_profile']
    
    def get_sql_stats(self):
        """获取SQL统计信息"""
        return self.parsed_data['sql_stats']
    
    def get_wait_events(self):
        """获取等待事件信息"""
        return self.parsed_data['wait_events']
    
    def get_advisory_stats(self):
        """获取建议统计信息"""
        return self.parsed_data['advisory_stats']
    
    def get_all_data(self):
        """获取所有解析数据"""
        return self.parsed_data
    
    def _standardize_snapshot_info(self, snap_info):
        """
        标准化快照信息格式
        
        参数:
            snap_info (dict): 原始快照信息
            
        返回:
            dict: 标准化后的快照信息
        """
        from datetime import datetime
        import re
        
        standardized = {}
        
        # 提取开始和结束时间
        begin_time_str = snap_info.get('Begin Snap Time', '')
        end_time_str = snap_info.get('End Snap Time', '')
        
        # 尝试解析时间格式
        time_formats = [
            '%d-%b-%y %H:%M:%S',  # Oracle默认格式: 29-Jul-25 14:30:15
            '%Y-%m-%d %H:%M:%S',  # 标准格式: 2025-07-29 14:30:15
            '%d/%m/%Y %H:%M:%S',  # 另一种格式: 29/07/2025 14:30:15
            '%d-%m-%Y %H:%M:%S',  # 29-07-2025 14:30:15
        ]
        
        def parse_time(time_str):
            """解析时间字符串为datetime对象"""
            if not time_str:
                return None
                
            # 清理时间字符串
            time_str = time_str.strip()
            
            for fmt in time_formats:
                try:
                    return datetime.strptime(time_str, fmt)
                except ValueError:
                    continue
            
            # 如果所有格式都失败，尝试提取部分信息
            try:
                # 使用正则表达式提取日期时间部分
                date_match = re.search(r'(\d{1,2}[-/]\w{3,}[-/]\d{2,4}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|\d{4}[-/]\d{1,2}[-/]\d{1,2})\s+(\d{1,2}:\d{2}(?::\d{2})?)', time_str)
                if date_match:
                    date_part = date_match.group(1)
                    time_part = date_match.group(2)
                    
                    # 如果时间部分没有秒，添加默认的秒
                    if len(time_part.split(':')) == 2:
                        time_part += ':00'
                    
                    full_time_str = f"{date_part} {time_part}"
                    
                    # 再次尝试解析
                    for fmt in time_formats:
                        try:
                            return datetime.strptime(full_time_str, fmt)
                        except ValueError:
                            continue
            except:
                pass
            
            return None
        
        # 解析开始和结束时间
        begin_time = parse_time(begin_time_str)
        end_time = parse_time(end_time_str)
        
        # 构建标准化的快照信息
        standardized = {
            'begin_time': begin_time,
            'end_time': end_time,
            'begin_snap_id': snap_info.get('Begin Snap Id', ''),
            'end_snap_id': snap_info.get('End Snap Id', ''),
            'elapsed_time': snap_info.get('Elapsed', ''),
            'db_time': snap_info.get('DB Time', ''),
            'begin_sessions': snap_info.get('Begin Sessions', ''),
            'end_sessions': snap_info.get('End Sessions', ''),
        }
        
        # 移除None值和空字符串
        standardized = {k: v for k, v in standardized.items() if v is not None and v != ''}
        
        return standardized
    
    def _parse_execution_plans_html(self):
        """解析HTML格式的执行计划部分"""
        try:
            # 查找包含执行计划的部分
            tables = self.soup.find_all('table')
            for table in tables:
                # 查找执行计划表格的标识
                if table.find('th', text=re.compile('Execution Plan|Plan Hash Value|Operation|Object')):
                    rows = table.find_all('tr')
                    current_plan = None
                    
                    for row in rows:
                        cells = row.find_all(['th', 'td'])
                        if len(cells) > 0:
                            row_text = ' '.join([cell.get_text().strip() for cell in cells])
                            
                            # 查找SQL ID行，表示新的执行计划开始
                            sql_id_match = re.search(r'SQL ID:\s*([a-zA-Z0-9]+)', row_text)
                            if sql_id_match:
                                if current_plan:
                                    self.parsed_data['execution_plans'].append(current_plan)
                                
                                current_plan = {
                                    'sql_id': sql_id_match.group(1),
                                    'plan_hash_value': '',
                                    'sql_text': '',
                                    'plan_steps': []
                                }
                            
                            # 查找Plan Hash Value
                            elif current_plan and re.search(r'Plan Hash Value:\s*(\d+)', row_text):
                                hash_match = re.search(r'Plan Hash Value:\s*(\d+)', row_text)
                                if hash_match:
                                    current_plan['plan_hash_value'] = hash_match.group(1)
                            
                            # 查找SQL文本
                            elif current_plan and not current_plan['sql_text'] and len(cells) == 1:
                                cell_text = cells[0].get_text().strip()
                                if cell_text and not cell_text.startswith('Plan') and not cell_text.startswith('SQL'):
                                    current_plan['sql_text'] = cell_text
                            
                            # 解析执行计划步骤
                            elif current_plan and len(cells) >= 4:
                                step_data = {}
                                step_text = [cell.get_text().strip() for cell in cells]
                                
                                # 尝试解析标准的执行计划格式
                                if len(step_text) >= 4:
                                    step_data = {
                                        'id': step_text[0] if step_text[0] else '',
                                        'operation': step_text[1] if len(step_text) > 1 else '',
                                        'object_name': step_text[2] if len(step_text) > 2 else '',
                                        'cost': step_text[3] if len(step_text) > 3 else '',
                                        'cardinality': step_text[4] if len(step_text) > 4 else '',
                                        'bytes': step_text[5] if len(step_text) > 5 else '',
                                        'time': step_text[6] if len(step_text) > 6 else ''
                                    }
                                    
                                if step_data and step_data['id']:
                                    current_plan['plan_steps'].append(step_data)
                    
                    # 添加最后一个计划
                    if current_plan:
                        self.parsed_data['execution_plans'].append(current_plan)
            
            logger.info(f"成功解析执行计划部分，找到 {len(self.parsed_data['execution_plans'])} 个执行计划")
        except Exception as e:
            logger.error(f"解析执行计划部分失败: {str(e)}")
    
    def _parse_execution_plans_text(self):
        """解析TEXT格式的执行计划部分"""
        try:
            # 使用正则表达式查找执行计划部分
            plan_pattern = r'SQL ID:\s*([a-zA-Z0-9]+)[^\n]*\n([^\n]*)\n\nPlan Hash Value:\s*(\d+)\n([\s\S]*?)(?=\n\nSQL ID:|\n\n[A-Z]|\Z)'
            matches = re.finditer(plan_pattern, self.content, re.MULTILINE)
            
            for match in matches:
                sql_id = match.group(1)
                sql_text = match.group(2).strip()
                plan_hash_value = match.group(3)
                plan_content = match.group(4)
                
                # 解析执行计划步骤
                plan_steps = []
                plan_lines = plan_content.strip().split('\n')
                
                # 跳过表头行，查找实际的计划步骤
                header_found = False
                for line in plan_lines:
                    line = line.strip()
                    if not line:
                        continue
                    
                    # 查找表头行
                    if not header_found and ('Id' in line and 'Operation' in line):
                        header_found = True
                        continue
                    
                    # 解析计划步骤行
                    if header_found and line and not line.startswith('-'):
                        # 使用正则表达式解析执行计划行
                        step_match = re.match(r'\s*(\d+)\s+([A-Z\s]+?)\s+([A-Za-z0-9_]*)\s*\|\s*(\d*)\s*\|\s*(\d*)\s*\|\s*(\d*)\s*\|\s*(\d*:\d*:\d*)?', line)
                        if step_match:
                            step_data = {
                                'id': step_match.group(1),
                                'operation': step_match.group(2).strip(),
                                'object_name': step_match.group(3),
                                'cost': step_match.group(4),
                                'cardinality': step_match.group(5),
                                'bytes': step_match.group(6),
                                'time': step_match.group(7) if step_match.group(7) else ''
                            }
                            plan_steps.append(step_data)
                        else:
                            # 简化版解析：按空格分割
                            parts = re.split(r'\s{2,}', line)
                            if len(parts) >= 2 and parts[0].isdigit():
                                step_data = {
                                    'id': parts[0],
                                    'operation': parts[1] if len(parts) > 1 else '',
                                    'object_name': parts[2] if len(parts) > 2 else '',
                                    'cost': parts[3] if len(parts) > 3 else '',
                                    'cardinality': parts[4] if len(parts) > 4 else '',
                                    'bytes': parts[5] if len(parts) > 5 else '',
                                    'time': parts[6] if len(parts) > 6 else ''
                                }
                                plan_steps.append(step_data)
                
                execution_plan = {
                    'sql_id': sql_id,
                    'sql_text': sql_text,
                    'plan_hash_value': plan_hash_value,
                    'plan_steps': plan_steps
                }
                
                self.parsed_data['execution_plans'].append(execution_plan)
            
            logger.info(f"成功解析执行计划部分，找到 {len(self.parsed_data['execution_plans'])} 个执行计划")
        except Exception as e:
            logger.error(f"解析执行计划部分失败: {str(e)}")
    
    def get_execution_plans(self):
        """获取执行计划信息"""
        return self.parsed_data['execution_plans']
