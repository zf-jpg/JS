import sys
import json
import pyodbc
import requests
import threading
import time
import argparse
import random
import re
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

DEFAULT_ACCOUNT = "2146895354"
DEFAULT_PASSWORD = "320325qwe"
DEFAULT_THREAD = 1
DEFAULT_MAX_PAGE = 60  # 默认最大页数 60
DEFAULT_INTERVAL = 0.0  # 统一关闭固定间隔，改用可配置随机延迟
# 默认日期范围：最近一年
DEFAULT_DATE_RANGE_DAYS = 365
# 延迟配置默认值
DEFAULT_REQUEST_DELAY = 0.5
DEFAULT_REQUEST_RMIN = 3.0
DEFAULT_REQUEST_RMAX = 5.0
DEFAULT_PAGE_DELAY = 0.5
DEFAULT_PAGE_RMIN = 3.0
DEFAULT_PAGE_RMAX = 5.0
DEFAULT_DOWNLOAD_DELAY = 0.5
DEFAULT_DOWNLOAD_RMIN = 3.0
DEFAULT_DOWNLOAD_RMAX = 5.0
DEFAULT_ENABLE_RANDOM_REQ = True
DEFAULT_ENABLE_RANDOM_PAGE = False
DEFAULT_ENABLE_RANDOM_DL = False


def get_default_date_range(days=DEFAULT_DATE_RANGE_DAYS):
    """返回最近 days 天的起止日期（含今天），格式 YYYYMMDD 整数"""
    end = datetime.now()
    start = end - timedelta(days=days - 1)
    return int(start.strftime("%Y%m%d")), int(end.strftime("%Y%m%d"))


def safe_sleep(base_delay, rmin=0.0, rmax=0.0, enable_random=False):
    """带随机的安全休眠"""
    delay = base_delay
    if enable_random and rmax > rmin:
        delay = random.uniform(base_delay + rmin, base_delay + rmax)
    if delay > 0:
        time.sleep(delay)


class DatabaseManager:
    """SQL Server 管理器，负责建库/建表和数据写入"""

    def __init__(self, log_callback=None, current_keyword=""):
        self.driver = self._select_driver()
        # Windows 身份验证连接字符串
        self.conn_str = (
            f"DRIVER={{{self.driver}}};"
            "SERVER=localhost;"
            "DATABASE=master;"
            "Trusted_Connection=yes;"
        )
        self.db_name = "zlzx"
        self.lock = threading.Lock()
        self.log_callback = log_callback or (lambda msg: None)
        self.current_keyword = current_keyword or ""
        self.ensure_database_and_tables()

    def _select_driver(self):
        """优先选择已安装的 SQL Server ODBC 驱动"""
        preferred = ["ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"]
        available = pyodbc.drivers()
        for driver in preferred:
            if driver in available:
                return driver
        # 兜底使用列表中第一个驱动
        if available:
            return available[-1]
        # 若无可用驱动，抛出异常
        raise RuntimeError("未检测到可用的 SQL Server ODBC 驱动，请先安装驱动。")

    def _connect(self, database=None):
        db = database or self.db_name
        return pyodbc.connect(
            self.conn_str.replace("DATABASE=master", f"DATABASE={db}"),
            autocommit=True,
        )

    def ensure_database_and_tables(self):
        """创建数据库和表（若不存在）"""
        with pyodbc.connect(self.conn_str, autocommit=True) as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"IF DB_ID('{self.db_name}') IS NULL "
                f"BEGIN CREATE DATABASE {self.db_name}; END;"
            )

        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                IF OBJECT_ID('patents', 'U') IS NULL
                BEGIN
                    CREATE TABLE patents (
                        id INT IDENTITY(1,1) PRIMARY KEY,
                        ane NVARCHAR(100),
                        title NVARCHAR(MAX),
                        application_no NVARCHAR(100),
                        application_date NVARCHAR(50),
                        publication_no NVARCHAR(100),
                        publication_date NVARCHAR(50),
                        grant_no NVARCHAR(100),
                        grant_date NVARCHAR(50),
                        main_class NVARCHAR(200),
                        applicant NVARCHAR(MAX),
                        patentee NVARCHAR(MAX),
                        inventors NVARCHAR(MAX),
                        abstract NVARCHAR(MAX),
                        raw_json NVARCHAR(MAX),
                        source_keyword NVARCHAR(200),
                        created_at DATETIME DEFAULT GETDATE()
                    );
                    CREATE UNIQUE INDEX IX_patents_ane ON patents(ane);
                END;
                IF COL_LENGTH('patents','source_keyword') IS NULL
                BEGIN
                    ALTER TABLE patents ADD source_keyword NVARCHAR(200) NULL;
                END;
                """
            )
            cursor.execute(
                """
                IF OBJECT_ID('pdfs', 'U') IS NULL
                BEGIN
                    CREATE TABLE pdfs (
                        id INT IDENTITY(1,1) PRIMARY KEY,
                        patent_id INT NOT NULL,
                        file_index INT NOT NULL,
                        file_name NVARCHAR(255),
                        content VARBINARY(MAX),
                        created_at DATETIME DEFAULT GETDATE(),
                        FOREIGN KEY(patent_id) REFERENCES patents(id)
                    );
                    CREATE INDEX IX_pdfs_patent_id ON pdfs(patent_id);
                END;
                """
            )

    def upsert_patent(self, patent):
        """插入或获取专利记录，返回 patent_id"""
        with self.lock, self._connect() as conn:
            cursor = conn.cursor()
            ane = patent.get("ANE", "")
            cursor.execute("SELECT id FROM patents WHERE ane=?", (ane,))
            row = cursor.fetchone()
            if row:
                return row[0]

            cursor.execute(
                """
                INSERT INTO patents (
                    ane, title, application_no, application_date,
                    publication_no, publication_date, grant_no, grant_date,
                    main_class, applicant, patentee, inventors, abstract, raw_json, source_keyword
                )
                OUTPUT INSERTED.id
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    ane,
                    patent.get("TI", "无"),
                    patent.get("AN", "无"),
                    patent.get("AD", "无"),
                    patent.get("PN", "无"),
                    patent.get("PD", "无"),
                    patent.get("GN", "无"),
                    patent.get("GD", "无"),
                    patent.get("MC", "无"),
                    patent.get("PA", "无"),
                    patent.get("PE", "无"),
                    patent.get("IN", "无"),
                    patent.get("AB", "无"),
                    json.dumps(patent, ensure_ascii=False),
                    getattr(self, "current_keyword", "")
                ),
            )
            identity_row = cursor.fetchone()
            if not identity_row or identity_row[0] is None:
                self.log_callback("⚠️ 未获取到新增专利ID，返回0")
                return 0
            return int(identity_row[0])

    def save_pdf(self, patent_id, file_index, file_name, content_bytes):
        """保存单个PDF到数据库"""
        with self.lock, self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO pdfs (patent_id, file_index, file_name, content)
                VALUES (?, ?, ?, ?);
                """,
                (patent_id, file_index, file_name, content_bytes),
            )


class PDFDownloader:
    """PDF下载器，支持多线程下载"""
    
    def __init__(self, session, log_callback, db_manager, max_workers=5, dl_delay=0.0, dl_rmin=0.0, dl_rmax=0.0, enable_random=False):
        self.session = session
        self.log_callback = log_callback
        self.db_manager = db_manager
        self.max_workers = max_workers
        self.dl_delay = dl_delay
        self.dl_rmin = dl_rmin
        self.dl_rmax = dl_rmax
        self.enable_random = enable_random
        self.downloaded_count = 0
        self.failed_count = 0
        
    def download_pdf(self, pdf_info):
        """下载单个PDF文件并写入数据库"""
        pdf_url, patent_title, idx, patent_id = pdf_info
        # 下载前延迟
        safe_sleep(self.dl_delay, self.dl_rmin, self.dl_rmax, self.enable_random)
        try:
            response = self.session.get(pdf_url, stream=True, timeout=30)
            if response.status_code == 200:
                content_bytes = b"".join(response.iter_content(chunk_size=8192))
                file_name = f"{patent_title}_{idx+1}.pdf"
                self.db_manager.save_pdf(
                    patent_id=patent_id,
                    file_index=idx + 1,
                    file_name=file_name,
                    content_bytes=content_bytes,
                )
                self.downloaded_count += 1
                return True, f"✓ 成功下载PDF并写入数据库: {file_name}"
            else:
                self.failed_count += 1
                return False, f"× PDF下载失败: {pdf_url}"
        except Exception as e:
            self.failed_count += 1
            return False, f"× PDF下载过程中出错: {str(e)}"
    
    def download_all_pdfs(self, pdf_urls, patent_title, patent_id):
        """多线程下载所有PDF文件"""
        if not pdf_urls:
            return 0, 0
        
        # 准备下载任务
        download_tasks = []
        for idx, pdf_url in enumerate(pdf_urls):
            download_tasks.append((pdf_url, patent_title, idx, patent_id))
        
        # 使用线程池并发下载
        successful_downloads = 0
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # 提交所有下载任务
            future_to_task = {
                executor.submit(self.download_pdf, task): task 
                for task in download_tasks
            }
            
            # 处理完成的任务
            for future in as_completed(future_to_task):
                task = future_to_task[future]
                try:
                    success, message = future.result()
                    if self.log_callback:
                        self.log_callback(message)
                    if success:
                        successful_downloads += 1
                except Exception as e:
                    if self.log_callback:
                        self.log_callback(f"× 下载任务异常: {str(e)}")
        
        return successful_downloads, len(pdf_urls)

class PatentCrawler:
    """专利爬虫 - 命令行版本"""
    
    def __init__(self, config):
        self.config = config
        self.is_running = True
        self.lock = threading.Lock()
        self.pdf_downloader = None
        self.db_manager = None
        
    def stop(self):
        """停止爬虫"""
        with self.lock:
            self.is_running = False
        
    def log(self, message):
        """输出日志到终端"""
        timestamp = datetime.now().strftime('%H:%M:%S')
        print(f"[{timestamp}] {message}")
        
    def run(self):
        """运行爬虫"""
        try:
            # 执行爬虫逻辑
            success, message = self.run_patent_crawler()
            if success:
                self.log(f"✓ {message}")
            else:
                self.log(f"× {message}")
            return success, message
        except Exception as e:
            error_msg = f"发生错误: {str(e)}"
            self.log(f"× {error_msg}")
            return False, error_msg
    
    def run_patent_crawler(self):
        """专利爬虫主逻辑 - 多线程版本"""
        try:
            self.log("开始登录专利之星网站...")
            
            # 创建会话
            session = requests.Session()
            session.headers.update({
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "DNT": "1",
                "Origin": "https://www.patentstar.com.cn",
                "Pragma": "no-cache",
                "Referer": "https://www.patentstar.com.cn/Search/ResultList?CurrentQuery=5Y2h5aWXL1lZ&type=cn",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0",
                "X-Requested-With": "XMLHttpRequest",
                "sec-ch-ua": "\"Microsoft Edge\";v=\"143\", \"Chromium\";v=\"143\", \"Not A(Brand\";v=\"24\"",
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": "\"Windows\""
            })
            
            # 设置超时和重试
            session.mount('http://', requests.adapters.HTTPAdapter(max_retries=3))
            session.mount('https://', requests.adapters.HTTPAdapter(max_retries=3))
            
            # 初始化数据库
            self.log("检查并创建数据库/表: zlzx...")
            self.db_manager = DatabaseManager(log_callback=self.log, current_keyword=self.config.get("search", ""))
            self.log("数据库准备完成。")
            
            # 登录
            login_url = "https://www.patentstar.com.cn/Account/UserLogin"
            login_data = {
                "loginname": self.config['account'],
                "password": self.config['password']
            }
            
            login_headers = {
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "DNT": "1",
                "Origin": "https://www.patentstar.com.cn",
                "Pragma": "no-cache",
                "Referer": "https://www.patentstar.com.cn/Account/LoginOut",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0",
                "X-Requested-With": "XMLHttpRequest",
                "sec-ch-ua": "\"Microsoft Edge\";v=\"143\", \"Chromium\";v=\"143\", \"Not A(Brand\";v=\"24\"",
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": "\"Windows\""
            }
            
            response = session.post(login_url, headers=login_headers, data=login_data, timeout=30)
            
            if response.status_code != 200:
                return False, f"登录失败，状态码: {response.status_code}"
                
            self.log("登录成功！")
                
            # 初始化PDF下载器
            self.pdf_downloader = PDFDownloader(
                session,
                self.log,
                db_manager=self.db_manager,
                max_workers=self.config.get('thread_count', 5),
                dl_delay=self.config.get("dl_delay", 0),
                dl_rmin=self.config.get("dl_rmin", 0),
                dl_rmax=self.config.get("dl_rmax", 0),
                enable_random=self.config.get("enable_random_dl", False),
            )
                
            # 搜索专利
            search_url = "https://www.patentstar.com.cn/Search/SearchByQuery"
            total_patents = 0
            downloaded_patents = 0
            max_pages = max(1, safe_int(self.config.get('max_page'), DEFAULT_MAX_PAGE))
            page = 1
            
            while True:
                # 检查是否停止
                with self.lock:
                    if not self.is_running:
                        self.log("用户中止操作")
                        return True, "操作已中止"
                    
                self.log(f"正在搜索第 {page}/{max_pages} 页...")
                
                search_data = {
                    "CurrentQuery": f"F XX {self.config['search']}/YY",
                    "OrderBy": "AD",
                    "OrderByType": "DESC",
                    "PageNum": str(page),
                    "DBType": "CN",
                    "RowCount": "20",  # 提升单页数量，减少漏抓
                    "Filter": "{\"CO\":\"\",\"PT\":\"\",\"LG\":\"\"}",
                    "SecSearch": "",
                    "IdList": ""
                }
                
                try:
                    response = session.post(search_url, data=search_data, timeout=30)
                    
                    if response.status_code != 200:
                        self.log(f"第 {page} 页搜索失败，状态码: {response.status_code}")
                        continue
                        
                    js = response.json()
                    patent_list = js.get('Data', {}).get('List', [])
                except Exception as e:
                    self.log(f"第 {page} 页数据获取失败: {str(e)}")
                    continue
                    
                if not patent_list:
                    self.log(f"第 {page} 页没有数据，停止搜索")
                    break
                    
                self.log(f"第 {page} 页找到 {len(patent_list)} 条专利")
                
                # 处理当前页的所有专利
                page_downloaded = 0
                for i, patent in enumerate(patent_list):
                    # 检查是否停止
                    with self.lock:
                        if not self.is_running:
                            self.log("用户中止操作")
                            return True, "操作已中止"
                        
                    total_patents += 1
                    
                    # 检查公开日/公告日范围（两者视为等价，任一符合则通过）
                    pd_raw = patent.get('PD')
                    gd_raw = patent.get('GD')
                    pd_val = normalize_pd(pd_raw)
                    gd_val = normalize_pd(gd_raw)
                    date_val = pd_val or gd_val
                    if date_val == 0:
                        self.log(f"专利公开日/公告日格式异常: PD={pd_raw}, GD={gd_raw}")
                        continue
                    if date_val < self.config['gkr_start'] or date_val > self.config['gkr_end']:
                        self.log(f"跳过专利: {patent.get('TI', '未知')} (日期 {date_val} 不在范围内)")
                        continue
                    
                    # 构建专利信息
                    patent_title = patent.get('TI', '无标题').replace('/', '_').replace('\\', '_')[:100]  # 限制文件名长度
                    self.log(f"处理专利 [{i+1}/{len(patent_list)}]: {patent_title}")

                    # 保存专利信息到数据库
                    patent_id = self.db_manager.upsert_patent(patent)
                    self.log(f"已写入专利基础信息 (ID: {patent_id})")
                    
                    # 获取PDF下载链接
                    try:
                        # 请求间隔（获取PDF链接前）
                        safe_sleep(
                            self.config.get("req_delay", 0),
                            self.config.get("req_rmin", 0),
                            self.config.get("req_rmax", 0),
                            self.config.get("enable_random_delay", False),
                        )
                        pdf_url = "https://www.patentstar.com.cn/WebService/GetPDFUrl"
                        pdf_data = {"ANE": patent.get('ANE', '')}
                        
                        pdf_response = session.post(pdf_url, data=pdf_data, timeout=30)
                        
                        if pdf_response.status_code == 200:
                            pdf_info = pdf_response.json()
                            pdf_urls = pdf_info.get('Data', [])
                            
                            if pdf_urls:
                                # 多线程下载PDF文件并保存到数据库
                                self.log(f"开始多线程下载PDF文件 (共{len(pdf_urls)}个文件)...")
                                successful_downloads, total_pdfs = self.pdf_downloader.download_all_pdfs(
                                    pdf_urls, patent_title, patent_id
                                )
                                
                                if successful_downloads > 0:
                                    downloaded_patents += 1
                                    page_downloaded += 1
                                    self.log(f"✓ 成功下载专利: {patent_title} (成功{successful_downloads}/{total_pdfs}个PDF文件)")
                                else:
                                    self.log(f"× 专利PDF下载失败: {patent_title}")
                            else:
                                self.log(f"× 无法获取PDF下载链接: {patent_title}")
                        else:
                            self.log(f"× PDF链接请求失败: {patent_title}")
                            
                    except Exception as e:
                        self.log(f"× 处理专利时出错: {str(e)}")
                
                self.log(f"第 {page} 页处理完成，成功下载 {page_downloaded} 条专利")
                
                # 更新进度
                progress = int((page / max_pages) * 100)
                self.log(f"进度: {progress}%")
                
                # 页间延迟
                safe_sleep(
                    self.config.get("page_delay", 0),
                    self.config.get("page_rmin", 0),
                    self.config.get("page_rmax", 0),
                    self.config.get("enable_random_delay", False),
                )
                
                # 下一页或达上限
                page += 1
                if page > max_pages:
                    self.log("已达到设定的最大页数，停止搜索")
                    break
            
            self.log(f"搜索完成！总共处理 {total_patents} 条专利，成功下载 {downloaded_patents} 条")
            return True, f"操作完成！成功下载 {downloaded_patents} 条专利"
            
        except Exception as e:
            return False, f"爬虫执行过程中发生错误: {str(e)}"

def safe_int(value, default=0):
    """安全地转换为整数"""
    if value is None:
        return default
    value = str(value).strip()
    if not value:
        return default
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return default


def normalize_pd(value):
    """将公开日规范为 YYYYMMDD 整数；若失败返回 0"""
    if value is None:
        return 0
    s = str(value)
    # 提取数字
    digits = "".join(re.findall(r"\d", s))
    if len(digits) >= 8:
        try:
            return int(digits[:8])
        except ValueError:
            return 0
    try:
        return int(float(s))
    except Exception:
        return 0

def parse_arguments():
    """解析命令行参数"""
    default_start, default_end = get_default_date_range(DEFAULT_DATE_RANGE_DAYS)
    parser = argparse.ArgumentParser(
        description='专利之星数据采集工具 - 命令行版本',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
示例:
  python 专利之星数据爬取.py --account 2146895354 --password 320325qwe --search 仪器 --max-page 20
  python 专利之星数据爬取.py -a 2146895354 -p 320325qwe -s 仪器 -m 20 -g 20240101 -e 20251231 -t 1
  
如果不提供参数，程序将进入交互式输入模式。
        '''
    )
    
    parser.add_argument('-a', '--account', default=DEFAULT_ACCOUNT, help=f'专利之星账号 (默认: {DEFAULT_ACCOUNT})')
    parser.add_argument('-p', '--password', default=DEFAULT_PASSWORD, help=f'专利之星密码 (默认: {DEFAULT_PASSWORD})')
    parser.add_argument('-s', '--search', help='搜索关键词')
    parser.add_argument('-m', '--max-page', type=int, default=DEFAULT_MAX_PAGE, help=f'最大页数 (0 表示不限，默认: {DEFAULT_MAX_PAGE})')
    parser.add_argument('-g', '--gkr-start', type=int, default=default_start, help=f'公开日开始日期 (默认: {default_start}，最近一年)')
    parser.add_argument('-e', '--gkr-end', type=int, default=default_end, help=f'公开日结束日期 (默认: {default_end}，最近一年)')
    parser.add_argument('-t', '--thread-count', type=int, default=DEFAULT_THREAD, help=f'下载线程数 (默认: {DEFAULT_THREAD})')
    parser.add_argument('-i', '--interval', type=float, default=DEFAULT_INTERVAL, help=f'爬取时间间隔(秒) 防止过快 (默认: {DEFAULT_INTERVAL}s，已停用固定间隔)')
    parser.add_argument('--req-delay', type=float, default=DEFAULT_REQUEST_DELAY, help='请求基础延迟(秒)')
    parser.add_argument('--req-rmin', type=float, default=DEFAULT_REQUEST_RMIN, help='请求随机延迟最小增量(秒)')
    parser.add_argument('--req-rmax', type=float, default=DEFAULT_REQUEST_RMAX, help='请求随机延迟最大增量(秒)')
    parser.add_argument('--page-delay', type=float, default=DEFAULT_PAGE_DELAY, help='页间基础延迟(秒)')
    parser.add_argument('--page-rmin', type=float, default=DEFAULT_PAGE_RMIN, help='页间随机延迟最小增量(秒)')
    parser.add_argument('--page-rmax', type=float, default=DEFAULT_PAGE_RMAX, help='页间随机延迟最大增量(秒)')
    parser.add_argument('--dl-delay', type=float, default=DEFAULT_DOWNLOAD_DELAY, help='下载基础延迟(秒)')
    parser.add_argument('--dl-rmin', type=float, default=DEFAULT_DOWNLOAD_RMIN, help='下载随机延迟最小增量(秒)')
    parser.add_argument('--dl-rmax', type=float, default=DEFAULT_DOWNLOAD_RMAX, help='下载随机延迟最大增量(秒)')
    parser.add_argument('--enable-random-req', action='store_true', default=DEFAULT_ENABLE_RANDOM_REQ, help='启用请求随机延迟')
    parser.add_argument('--enable-random-page', action='store_true', default=DEFAULT_ENABLE_RANDOM_PAGE, help='启用页间随机延迟')
    parser.add_argument('--enable-random-dl', action='store_true', default=DEFAULT_ENABLE_RANDOM_DL, help='启用下载随机延迟')
    
    return parser.parse_args()

def interactive_input():
    """交互式输入配置信息"""
    print("进入交互式输入模式...")
    print()
    
    default_start, default_end = get_default_date_range(DEFAULT_DATE_RANGE_DAYS)
    config = {
        'account': DEFAULT_ACCOUNT,
        'password': DEFAULT_PASSWORD,
        'max_page': DEFAULT_MAX_PAGE,
        'gkr_start': default_start,
        'gkr_end': default_end,
        'thread_count': DEFAULT_THREAD,
        'interval': DEFAULT_INTERVAL,
        'req_delay': DEFAULT_REQUEST_DELAY,
        'req_rmin': DEFAULT_REQUEST_RMIN,
        'req_rmax': DEFAULT_REQUEST_RMAX,
        'page_delay': DEFAULT_PAGE_DELAY,
        'page_rmin': DEFAULT_PAGE_RMIN,
        'page_rmax': DEFAULT_PAGE_RMAX,
        'dl_delay': DEFAULT_DOWNLOAD_DELAY,
        'dl_rmin': DEFAULT_DOWNLOAD_RMIN,
        'dl_rmax': DEFAULT_DOWNLOAD_RMAX,
        'enable_random_req': DEFAULT_ENABLE_RANDOM_REQ,
        'enable_random_page': DEFAULT_ENABLE_RANDOM_PAGE,
        'enable_random_dl': DEFAULT_ENABLE_RANDOM_DL,
    }
    
    print(f"默认账号: {DEFAULT_ACCOUNT}")
    print(f"默认密码: {DEFAULT_PASSWORD}")
    default_page_desc = "不限" if DEFAULT_MAX_PAGE <= 0 else DEFAULT_MAX_PAGE
    print(f"默认页数: {default_page_desc}")
    print(f"默认公开日范围: {default_start} - {default_end} (最近一年)")
    print(f"默认线程数: {DEFAULT_THREAD}")
    print(f"默认爬取间隔: {DEFAULT_INTERVAL} 秒（已停用固定间隔，改用随机延迟配置）")
    print(f"默认请求延迟: {DEFAULT_REQUEST_DELAY} 秒，随机区间: [{DEFAULT_REQUEST_RMIN}, {DEFAULT_REQUEST_RMAX}]，随机开关: {DEFAULT_ENABLE_RANDOM_REQ}")
    print(f"默认页间延迟: {DEFAULT_PAGE_DELAY} 秒，随机区间: [{DEFAULT_PAGE_RMIN}, {DEFAULT_PAGE_RMAX}]，随机开关: {DEFAULT_ENABLE_RANDOM_PAGE}")
    print(f"默认下载延迟: {DEFAULT_DOWNLOAD_DELAY} 秒，随机区间: [{DEFAULT_DOWNLOAD_RMIN}, {DEFAULT_DOWNLOAD_RMAX}]，随机开关: {DEFAULT_ENABLE_RANDOM_DL}")
    print()
    
    # 搜索关键词（唯一必填）
    while True:
        search = input("请输入搜索关键词（必填）: ").strip()
        if search:
            config['search'] = search
            break
        print("搜索关键词不能为空，请重新输入。")
    
    return config

def main():
    """主函数"""
    print("=" * 60)
    print("专利之星数据采集工具 - 命令行版本")
    print("=" * 60)
    print()
    
    # 解析命令行参数
    args = parse_arguments()
    
    # 检查是否提供了搜索关键词，如果没有则使用交互式输入
    if not args.search:
        config = interactive_input()
        print()
    else:
        # 验证参数
        if args.gkr_start > args.gkr_end:
            print("错误: 开始日期不能大于结束日期")
            sys.exit(1)
        
        if args.thread_count < 1 or args.thread_count > 20:
            print("错误: 线程数应在1-20之间")
            sys.exit(1)
        
        # 构建配置（使用默认值或命令行参数）
        config = {
            'account': args.account or DEFAULT_ACCOUNT,
            'password': args.password or DEFAULT_PASSWORD,
            'search': args.search,
            'max_page': args.max_page or DEFAULT_MAX_PAGE,
            'gkr_start': args.gkr_start,
            'gkr_end': args.gkr_end,
            'thread_count': args.thread_count or DEFAULT_THREAD,
            'interval': args.interval or DEFAULT_INTERVAL,
            'req_delay': args.req_delay,
            'req_rmin': args.req_rmin,
            'req_rmax': args.req_rmax,
            'page_delay': args.page_delay,
            'page_rmin': args.page_rmin,
            'page_rmax': args.page_rmax,
            'dl_delay': args.dl_delay,
            'dl_rmin': args.dl_rmin,
            'dl_rmax': args.dl_rmax,
            'enable_random_req': args.enable_random_req,
            'enable_random_page': args.enable_random_page,
            'enable_random_dl': args.enable_random_dl,
        }
    
    print(f"配置信息:")
    print(f"  账号: {config['account']}")
    print(f"  搜索关键词: {config['search']}")
    max_page_desc = "不限" if config['max_page'] <= 0 else config['max_page']
    print(f"  最大页数: {max_page_desc}")
    print(f"  公开日范围: {config['gkr_start']} - {config['gkr_end']}")
    print(f"  下载线程数: {config['thread_count']}")
    print(f"  请求延迟: {config.get('req_delay',0)}s 随机[{config.get('req_rmin',0)}, {config.get('req_rmax',0)}] 开关: {config.get('enable_random_req', False)}")
    print(f"  页间延迟: {config.get('page_delay',0)}s 随机[{config.get('page_rmin',0)}, {config.get('page_rmax',0)}] 开关: {config.get('enable_random_page', False)}")
    print(f"  下载延迟: {config.get('dl_delay',0)}s 随机[{config.get('dl_rmin',0)}, {config.get('dl_rmax',0)}] 开关: {config.get('enable_random_dl', False)}")
    print()
    print("数据将直接写入 SQL Server 数据库: zlzx (Windows 身份验证)")
    print()
    print("-" * 60)
    print()
    
    # 创建爬虫并运行
    crawler = PatentCrawler(config)
    
    try:
        success, message = crawler.run()
        print()
        print("-" * 60)
        if success:
            print(f"✓ {message}")
            sys.exit(0)
        else:
            print(f"× {message}")
            sys.exit(1)
    except KeyboardInterrupt:
        print()
        print("-" * 60)
        print("用户中断操作")
        crawler.stop()
        sys.exit(1)
    except Exception as e:
        print()
        print("-" * 60)
        print(f"× 发生未预期的错误: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    main()