from flask import Flask, jsonify, render_template, request, send_file
from flask_cors import CORS
import pyodbc
import json
import subprocess
import sys
import os
import io
import hashlib
import re
from datetime import datetime, timedelta
import threading
from collections import defaultdict, Counter
#
# 部分环境的 OpenSSL 后端不支持 hashlib.md5(..., usedforsecurity=False)
# 报错信息：'usedforsecurity' is an invalid keyword argument for openssl_md5()
# 先行兼容：吞掉该参数，保证 reportlab 等库正常生成 PDF
#
_orig_md5 = hashlib.md5


def _md5_compat(*args, **kwargs):
    kwargs.pop("usedforsecurity", None)
    return _orig_md5(*args, **kwargs)


hashlib.md5 = _md5_compat

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    from reportlab.lib.units import mm
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.lib import colors
    from reportlab.graphics.shapes import Drawing, String
    from reportlab.graphics.charts.barcharts import VerticalBarChart
    _reportlab_available = True
except ImportError:
    _reportlab_available = False

app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'front', 'templates'),
    static_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'front', 'static')
)
CORS(app)

_crawl_lock = threading.Lock()
_crawl_proc = None

class DatabaseManager:
    """数据库管理器，用于查询专利和预警数据"""
    
    def __init__(self):
        self.driver = self._select_driver()
        self.conn_str = (
            f"DRIVER={{{self.driver}}};"
            "SERVER=localhost;"
            "DATABASE=zlzx;"
            "Trusted_Connection=yes;"
        )
        self.ensure_columns()
    
    def _select_driver(self):
        """选择 SQL Server ODBC 驱动"""
        preferred = ["ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"]
        available = pyodbc.drivers()
        for driver in preferred:
            if driver in available:
                return driver
        if available:
            return available[-1]
        raise RuntimeError("未检测到可用的 SQL Server ODBC 驱动")
    
    def connect(self):
        """建立数据库连接"""
        return pyodbc.connect(self.conn_str, autocommit=True)

    def ensure_columns(self):
        """确保表中存在 source_keyword 列"""
        with self.connect() as conn:
            cursor = conn.cursor()
            # patents
            cursor.execute("""
                IF COL_LENGTH('patents','source_keyword') IS NULL
                BEGIN
                    ALTER TABLE patents ADD source_keyword NVARCHAR(200) NULL;
                END;
            """)
            # risk_alerts
            cursor.execute("""
                IF COL_LENGTH('risk_alerts','source_keyword') IS NULL
                BEGIN
                    ALTER TABLE risk_alerts ADD source_keyword NVARCHAR(200) NULL;
                END;
            """)
            # tag_analysis_results
            cursor.execute("""
                IF OBJECT_ID('tag_analysis_results','U') IS NOT NULL
                BEGIN
                    IF COL_LENGTH('tag_analysis_results','source_keyword') IS NULL
                    BEGIN
                        ALTER TABLE tag_analysis_results ADD source_keyword NVARCHAR(200) NULL;
                    END;
                END;
                -- 关键词分类表
                IF OBJECT_ID('keyword_categories','U') IS NULL
                BEGIN
                    CREATE TABLE keyword_categories(
                        keyword NVARCHAR(200) PRIMARY KEY,
                        category NVARCHAR(100) NULL,
                        updated_at DATETIME DEFAULT GETDATE()
                    );
                END;
            """)

    def generate_tags_from_keyword(self, keyword):
        """
        兼容占位：生成关键词专属标签（若未来有实现可替换）。
        目前仅返回空结果，避免接口调用时报错。
        """
        return {"keyword": keyword, "total_count": 0}

    def upsert_keyword_category(self, keyword, category):
        """写入或更新关键词分类"""
        if not keyword:
            return
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                MERGE keyword_categories AS t
                USING (SELECT ? AS k, ? AS c) AS s
                ON t.keyword = s.k
                WHEN MATCHED THEN UPDATE SET category = s.c, updated_at = GETDATE()
                WHEN NOT MATCHED THEN INSERT(keyword, category) VALUES(s.k, s.c);
                """,
                (keyword, category)
            )

    def list_keyword_categories(self):
        """获取全部关键词及分类，附带每个关键词的专利数量"""
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT p.source_keyword AS keyword, kc.category, COUNT(*) AS patent_count
                FROM patents p
                LEFT JOIN keyword_categories kc ON kc.keyword = p.source_keyword
                WHERE p.source_keyword IS NOT NULL AND p.source_keyword <> ''
                GROUP BY p.source_keyword, kc.category
                ORDER BY patent_count DESC
            """)
            rows = cursor.fetchall()
            keywords = [{"keyword": r[0], "category": r[1], "patentCount": r[2]} for r in rows]

            cursor.execute("SELECT DISTINCT category FROM keyword_categories WHERE category IS NOT NULL AND category <> ''")
            cats = [r[0] for r in cursor.fetchall()]

            return {"keywords": keywords, "categories": cats}

    def get_keyword_category(self, keyword):
        """获取关键词所属分类"""
        if not keyword:
            return None
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT category FROM keyword_categories WHERE keyword = ?",
                (keyword,),
            )
            row = cursor.fetchone()
            return row[0] if row else None

    def _split_names(self, raw):
        """按常见分隔符拆分申请人/公司名称"""
        if not raw:
            return []
        seps = [";", "；", ",", "，", "、", "/", "|"]
        for sep in seps:
            if sep in raw:
                return [p.strip() for p in raw.split(sep) if p.strip()]
        return [raw.strip()]

    def get_threat_companies(self, keyword=None, limit=10):
        """
        基于全部预警记录统计潜在威胁公司（全局视角）：
        - 全量 risk_alerts 汇总，按公司出现次数和平均风险分排序
        - 如传入 keyword，仅用于排除自身/相似名称
        """
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT applicants, risk_score
                FROM risk_alerts
                """
            )
            rows = cursor.fetchall()
        stats = {}
        kw_norm = None
        if keyword:
            kw_norm = re.sub(r"[\W_]+", "", keyword).lower()

        for applicants, score in rows:
            names = self._split_names(applicants)
            for name in names:
                if not name:
                    continue
                nm_norm = re.sub(r"[\W_]+", "", name).lower()
                # 排除自身（包含/被包含）
                if kw_norm and (kw_norm in nm_norm or nm_norm in kw_norm):
                    continue
                s = stats.get(name, {"count": 0, "sum": 0})
                s["count"] += 1
                s["sum"] += score or 0
                stats[name] = s
        result = []
        for name, s in stats.items():
            avg = s["sum"] / s["count"] if s["count"] else 0
            result.append({"company": name, "count": s["count"], "avg_score": round(avg, 2)})
        result.sort(key=lambda x: (x["avg_score"], x["count"]), reverse=True)
        # 过滤更有威胁的公司：高分或高频
        filtered = [r for r in result if r["avg_score"] >= 55 or r["count"] >= 3]
        if not filtered:
            filtered = result[:max(3, limit)]  # 兜底提供少量结果
        return filtered[:limit]
    
    def get_patents(self, page=1, page_size=20, search=None, keyword=None):
        """获取专利列表（分页）"""
        try:
            with self.connect() as conn:
                cursor = conn.cursor()
                
                # 构建查询条件
                where_clauses = []
                params = []
                if search:
                    where_clauses.append("(title LIKE ? OR applicant LIKE ? OR abstract LIKE ?)")
                    search_param = f"%{search}%"
                    params = [search_param, search_param, search_param]
                if keyword:
                    where_clauses.append("source_keyword = ?")
                    params.append(keyword)

                where_clause = ""
                if where_clauses:
                    where_clause = "WHERE " + " AND ".join(where_clauses)

                # 获取总数
                count_sql = f"SELECT COUNT(*) FROM patents {where_clause}"
                cursor.execute(count_sql, params)
                total = cursor.fetchone()[0]
                
                # 获取分页数据
                offset = (page - 1) * page_size
                sql = f"""
                    SELECT id, ane, title, application_no, application_date, 
                           publication_no, publication_date, grant_no, grant_date,
                           main_class, applicant, patentee, inventors, abstract, source_keyword, created_at
                    FROM patents
                    {where_clause}
                    ORDER BY id DESC
                    OFFSET ? ROWS FETCH NEXT ? ROWS ONLY
                """
                cursor.execute(sql, params + [offset, page_size])
                
                columns = [column[0] for column in cursor.description]
                rows = cursor.fetchall()
                
                patents = []
                for row in rows:
                    patent = dict(zip(columns, row))
                    # 处理日期格式
                    if patent.get('created_at'):
                        patent['created_at'] = patent['created_at'].strftime('%Y-%m-%d %H:%M:%S')
                    patents.append(patent)
                
                return {
                    'data': patents,
                    'total': total,
                    'page': page,
                    'page_size': page_size,
                    'total_pages': (total + page_size - 1) // page_size
                }
        except Exception as e:
            raise Exception(f"查询专利数据失败: {str(e)}")
    
    def get_patent_detail(self, patent_id):
        """获取专利详细信息"""
        try:
            with self.connect() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, ane, title, application_no, application_date,
                           publication_no, publication_date, grant_no, grant_date,
                           main_class, applicant, patentee, inventors, abstract, raw_json, created_at
                    FROM patents
                    WHERE id = ?
                """, (patent_id,))
                
                row = cursor.fetchone()
                if not row:
                    return None
                
                columns = [column[0] for column in cursor.description]
                patent = dict(zip(columns, row))
                
                # 处理日期
                if patent.get('created_at'):
                    patent['created_at'] = patent['created_at'].strftime('%Y-%m-%d %H:%M:%S')
                
                # 解析 raw_json
                if patent.get('raw_json'):
                    try:
                        patent['raw_json'] = json.loads(patent['raw_json'])
                    except:
                        pass
                
                # 获取关联的PDF数量
                cursor.execute("SELECT COUNT(*) FROM pdfs WHERE patent_id = ?", (patent_id,))
                patent['pdf_count'] = cursor.fetchone()[0]
                
                return patent
        except Exception as e:
            raise Exception(f"查询专利详情失败: {str(e)}")
    
    def get_risk_alerts(self, page=1, page_size=20, risk_level=None, search=None, keyword=None):
        """获取预警分析结果（分页）"""
        try:
            with self.connect() as conn:
                cursor = conn.cursor()
                
                # 构建查询条件
                where_clauses = []
                params = []
                
                if risk_level:
                    where_clauses.append("risk_level = ?")
                    params.append(risk_level)
                
                if search:
                    where_clauses.append("(title LIKE ? OR applicants LIKE ? OR risk_tags LIKE ?)")
                    search_param = f"%{search}%"
                    params.extend([search_param, search_param, search_param])

                if keyword:
                    where_clauses.append("source_keyword = ?")
                    params.append(keyword)
                
                where_clause = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""
                
                # 获取总数
                count_sql = f"SELECT COUNT(*) FROM risk_alerts {where_clause}"
                cursor.execute(count_sql, params)
                total = cursor.fetchone()[0]
                
                # 获取分页数据
                offset = (page - 1) * page_size
                sql = f"""
                    SELECT id, patent_id, ane, title, applicants, inventors, publication_date,
                           risk_score, risk_level, risk_tags, risk_reason, risk_confidence,
                           risk_delta, source_keyword, created_at, updated_at
                    FROM risk_alerts
                    {where_clause}
                    ORDER BY risk_score DESC, updated_at DESC
                    OFFSET ? ROWS FETCH NEXT ? ROWS ONLY
                """
                cursor.execute(sql, params + [offset, page_size])
                
                columns = [column[0] for column in cursor.description]
                rows = cursor.fetchall()
                
                alerts = []
                for row in rows:
                    alert = dict(zip(columns, row))
                    # 处理日期
                    if alert.get('created_at'):
                        alert['created_at'] = alert['created_at'].strftime('%Y-%m-%d %H:%M:%S')
                    if alert.get('updated_at'):
                        alert['updated_at'] = alert['updated_at'].strftime('%Y-%m-%d %H:%M:%S')
                    alerts.append(alert)
                
                return {
                    'data': alerts,
                    'total': total,
                    'page': page,
                    'page_size': page_size,
                    'total_pages': (total + page_size - 1) // page_size
                }
        except Exception as e:
            raise Exception(f"查询预警数据失败: {str(e)}")
    
    def get_statistics(self, keyword=None):
        """获取统计数据"""
        try:
            with self.connect() as conn:
                cursor = conn.cursor()
                
                stats = {}
                
                kw_filter = ""
                params_kw = []
                if keyword:
                    kw_filter = "WHERE source_keyword = ?"
                    params_kw = [keyword]

                # 专利总数
                cursor.execute(f"SELECT COUNT(*) FROM patents {kw_filter}", params_kw)
                stats['total_patents'] = cursor.fetchone()[0]
                
                # 预警总数
                cursor.execute(f"SELECT COUNT(*) FROM risk_alerts {kw_filter}", params_kw)
                stats['total_alerts'] = cursor.fetchone()[0]
                
                # 按风险等级统计
                cursor.execute(f"""
                    SELECT risk_level, COUNT(*) as count
                    FROM risk_alerts
                    {kw_filter if kw_filter else ""}
                    {"AND" if kw_filter else "WHERE"} risk_level IS NOT NULL
                    GROUP BY risk_level
                """, params_kw)
                risk_stats = {}
                for row in cursor.fetchall():
                    risk_stats[row[0]] = row[1]
                stats['risk_distribution'] = risk_stats
                
                # 平均风险分数
                cursor.execute(f"""
                    SELECT AVG(risk_score) FROM risk_alerts
                    {kw_filter if kw_filter else ""}
                    {"AND" if kw_filter else "WHERE"} risk_score IS NOT NULL
                """, params_kw)
                avg_score = cursor.fetchone()[0]
                stats['avg_risk_score'] = round(avg_score, 2) if avg_score else 0
                
                # 最近更新的预警数量（最近7天）
                cursor.execute(f"""
                    SELECT COUNT(*) FROM risk_alerts
                    {kw_filter if kw_filter else ""}
                    {"AND" if kw_filter else "WHERE"} updated_at >= DATEADD(day, -7, GETDATE())
                """, params_kw)
                stats['recent_alerts'] = cursor.fetchone()[0]
                
                # 趋势：默认最近3个月按月；公司类关键词显示最近12个月
                category = self.get_keyword_category(keyword) if keyword else None
                window_months = 12 if category == "公司" else 3
                trend_start = f"DATEADD(month, -{window_months-1}, DATEFROMPARTS(YEAR(GETDATE()), MONTH(GETDATE()), 1))"
                trend_conditions = [
                    f"COALESCE(TRY_CONVERT(date, LEFT(p.publication_date, 8)), CAST(r.updated_at AS date)) >= {trend_start}"
                ]
                if keyword:
                    trend_conditions.insert(0, "r.source_keyword = ?")
                trend_where = "WHERE " + " AND ".join(trend_conditions)
                cursor.execute(f"""
                    WITH trend AS (
                        SELECT
                            CONVERT(varchar(7),
                                COALESCE(
                                    TRY_CONVERT(date, LEFT(p.publication_date, 8)),
                                    CAST(r.updated_at AS date)
                                ), 120
                            ) AS ym,
                            COUNT(*) AS cnt
                        FROM risk_alerts r
                        LEFT JOIN patents p ON p.id = r.patent_id
                        {trend_where}
                        GROUP BY CONVERT(varchar(7),
                                COALESCE(
                                    TRY_CONVERT(date, LEFT(p.publication_date, 8)),
                                    CAST(r.updated_at AS date)
                                ), 120)
                    )
                    SELECT ym AS month, cnt FROM trend ORDER BY month;
                """, params_kw)
                month_rows = cursor.fetchall()
                month_map = {row[0]: row[1] for row in month_rows}
                today = datetime.now().date().replace(day=1)
                months = []
                for i in range(window_months - 1, -1, -1):
                    m = (today - timedelta(days=i * 30)).replace(day=1)
                    ym = m.strftime("%Y-%m")
                    months.append(ym)
                stats['daily_alerts'] = [
                    {"date": m, "count": month_map.get(m, 0)} for m in months
                ]
                
                # 风险等级分布用于可视化
                stats['risk_distribution_list'] = [
                    {"level": k, "count": v} for k, v in risk_stats.items()
                ]
                
                # PDF总数
                if keyword:
                    cursor.execute("""
                        SELECT COUNT(*) FROM pdfs pdf
                        INNER JOIN patents p ON p.id = pdf.patent_id
                        WHERE p.source_keyword = ?
                    """, params_kw)
                else:
                    cursor.execute("SELECT COUNT(*) FROM pdfs")
                stats['total_pdfs'] = cursor.fetchone()[0]
                
                return stats
        except Exception as e:
            raise Exception(f"查询统计数据失败: {str(e)}")

    def get_top_risk_alerts(self, keyword=None, limit=20):
        """获取高风险预警TOP列表"""
        try:
            with self.connect() as conn:
                cursor = conn.cursor()
                params = []
                where = []
                if keyword:
                    where.append("source_keyword = ?")
                    params.append(keyword)
                where_sql = "WHERE " + " AND ".join(where) if where else ""
                cursor.execute(
                    f"""
                    SELECT TOP {int(limit)}
                        id, patent_id, ane, title, applicants, inventors, publication_date,
                        risk_score, risk_level, risk_tags, risk_reason, risk_confidence,
                        source_keyword, created_at, updated_at
                    FROM risk_alerts
                    {where_sql}
                    ORDER BY risk_score DESC, updated_at DESC
                    """,
                    params,
                )
                cols = [c[0] for c in cursor.description]
                rows = cursor.fetchall()
                data = []
                for r in rows:
                    item = dict(zip(cols, r))
                    for fld in ("created_at", "updated_at"):
                        if item.get(fld):
                            item[fld] = item[fld].strftime("%Y-%m-%d %H:%M:%S")
                    data.append(item)
                return data
        except Exception as e:
            raise Exception(f"查询高风险预警失败: {str(e)}")

    def get_patent_overview(self, keyword=None, limit=20):
        """获取专利概览（按风险与时间排序）"""
        try:
            with self.connect() as conn:
                cursor = conn.cursor()
                params = []
                where = []
                if keyword:
                    where.append("p.source_keyword = ?")
                    params.append(keyword)
                where_sql = "WHERE " + " AND ".join(where) if where else ""
                cursor.execute(
                    f"""
                    SELECT TOP {int(limit)}
                        p.id, p.ane, p.title, p.application_no, p.publication_date,
                        p.applicant, p.patentee, p.main_class, p.created_at,
                        r.risk_score, r.risk_level, r.risk_confidence
                    FROM patents p
                    LEFT JOIN risk_alerts r ON r.patent_id = p.id
                    {where_sql}
                    ORDER BY ISNULL(r.risk_score, 0) DESC, p.created_at DESC
                    """,
                    params,
                )
                cols = [c[0] for c in cursor.description]
                rows = cursor.fetchall()
                data = []
                for r in rows:
                    item = dict(zip(cols, r))
                    if item.get("created_at"):
                        item["created_at"] = item["created_at"].strftime("%Y-%m-%d %H:%M:%S")
                    data.append(item)
                return data
        except Exception as e:
            raise Exception(f"查询专利概览失败: {str(e)}")

    def get_applicant_risk(self, keyword=None, limit=15):
        """按申请人聚合风险情况"""
        try:
            with self.connect() as conn:
                cursor = conn.cursor()
                params = []
                where = []
                if keyword:
                    where.append("source_keyword = ?")
                    params.append(keyword)
                where_sql = "WHERE " + " AND ".join(where) if where else ""
                cursor.execute(
                    f"""
                    SELECT TOP {int(limit)}
                        applicants,
                        COUNT(*) AS cnt,
                        AVG(CAST(risk_score AS FLOAT)) AS avg_score
                    FROM risk_alerts
                    {where_sql}
                    GROUP BY applicants
                    HAVING applicants IS NOT NULL AND applicants <> ''
                    ORDER BY avg_score DESC, cnt DESC
                    """,
                    params,
                )
                rows = cursor.fetchall()
                data = []
                for applicants, cnt, avg_score in rows:
                    data.append(
                        {
                            "applicants": applicants,
                            "count": cnt,
                            "avg_score": round(avg_score or 0, 2),
                        }
                    )
                return data
        except Exception as e:
            raise Exception(f"查询申请人风险聚合失败: {str(e)}")

    def build_report_data(self, keyword=None):
        """汇总报告所需数据"""
        stats = self.get_statistics(keyword=keyword)
        alerts_top = self.get_top_risk_alerts(keyword=keyword, limit=15)
        patents_top = self.get_patent_overview(keyword=keyword, limit=15)
        applicants = self.get_applicant_risk(keyword=keyword, limit=15)
        tag_summary = self.get_tag_summary(keyword=keyword)
        cooccurrence_summary = self.get_cooccurrence_summary(keyword=keyword, top_n=20)
        label_overlap_threats = self.compute_label_overlap_threats(target_keyword=keyword, top_n=8)
        category = self.get_keyword_category(keyword) if keyword else None
        threats = self.get_threat_companies(keyword=keyword, limit=10) if (category == "公司" or not category) else []
        return {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "keyword": keyword,
            "category": category,
            "stats": stats,
            "alerts_top": alerts_top,
            "patents_top": patents_top,
            "applicants": applicants,
            "tag_summary": tag_summary,
            "cooccurrence_summary": cooccurrence_summary,
            "label_overlap_threats": label_overlap_threats,
            "threats": threats,
        }

    def _compute_hot_cold(self, rows):
        """在后端按需计算热门/冷门/共现（可按关键词过滤后的数据）"""
        total_third = Counter()
        co_occurrence = Counter()
        for item in rows:
            themes_json = item.get("themes_json") or "{}"
            try:
                themes = json.loads(themes_json)
            except Exception:
                themes = {}
            for theme_data in themes.values():
                third_counts = theme_data.get("third_level_counts", {})
                if not isinstance(third_counts, dict):
                    continue
                # 单标签计数
                for lbl, cnt in third_counts.items():
                    try:
                        total_third[lbl] += int(cnt)
                    except Exception:
                        continue
                # 共现
                present = [k for k, v in third_counts.items() if v]
                for i, a in enumerate(present):
                    for b in present[i + 1 :]:
                        key = tuple(sorted((a, b)))
                        co_occurrence[key] += 1

        if not total_third:
            return {"hot": [], "cold": [], "co_occurrence_top": []}

        counts_sorted = sorted(total_third.values())
        idx_hot = max(int(len(counts_sorted) * 0.75) - 1, 0)
        idx_cold = max(int(len(counts_sorted) * 0.25) - 1, 0)
        hot_thr = counts_sorted[idx_hot]
        cold_thr = counts_sorted[idx_cold]

        hot = [{"label": k, "count": v} for k, v in total_third.items() if v >= hot_thr]
        cold = [{"label": k, "count": v} for k, v in total_third.items() if 0 < v <= cold_thr]
        co_top = sorted(co_occurrence.items(), key=lambda x: x[1], reverse=True)[:30]
        co_top_fmt = [{"pair": list(pair), "count": cnt} for pair, cnt in co_top]

        hot.sort(key=lambda x: x["count"], reverse=True)
        cold.sort(key=lambda x: x["count"])
        return {"hot": hot, "cold": cold, "co_occurrence_top": co_top_fmt}

    def get_tag_summary(self, keyword=None):
        """
        获取标签热门/冷门/共现汇总。
        - 有 keyword: 动态基于 tag_analysis_results + patents.source_keyword 过滤后计算
        - 无 keyword: 动态计算全部（排除已删除的关键词，只统计当前存在的关键词）
        """
        try:
            with self.connect() as conn:
                cursor = conn.cursor()
                if keyword:
                    # 有关键词，只统计该关键词的数据
                    cursor.execute(
                        """
                        SELECT tar.themes_json
                        FROM tag_analysis_results tar
                        INNER JOIN patents p ON p.id = tar.patent_id
                        WHERE p.source_keyword = ?
                        """,
                        (keyword,),
                    )
                    rows = [{"themes_json": r[0]} for r in cursor.fetchall()]
                    summary = self._compute_hot_cold(rows)
                    return {
                        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "summary": summary,
                    }
                # 无关键词，动态计算全部（只统计当前存在的关键词对应的专利）
                # 先获取当前存在的所有关键词列表
                cursor.execute("""
                    SELECT DISTINCT source_keyword 
                    FROM patents 
                    WHERE source_keyword IS NOT NULL AND source_keyword <> ''
                """)
                existing_keywords = [row[0] for row in cursor.fetchall()]
                
                if not existing_keywords:
                    # 如果没有关键词，返回空结果
                    return {
                        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "summary": {"hot": [], "cold": [], "co_occurrence_top": []},
                    }
                
                # 只统计当前存在的关键词对应的专利数据
                placeholders = ','.join(['?'] * len(existing_keywords))
                cursor.execute(
                    f"""
                    SELECT tar.themes_json
                    FROM tag_analysis_results tar
                    INNER JOIN patents p ON p.id = tar.patent_id
                    WHERE p.source_keyword IN ({placeholders})
                    """,
                    existing_keywords
                )
                rows = [{"themes_json": r[0]} for r in cursor.fetchall()]
                summary = self._compute_hot_cold(rows)
                return {
                    "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "summary": summary,
                }
        except Exception as e:
            raise Exception(f"查询标签汇总失败: {str(e)}")

    def _aggregate_tags_for_keyword(self, keyword):
        """
        聚合指定关键词的三级标签出现次数，返回 Counter，同时返回样本数量（专利篇数）。
        """
        if not keyword:
            return Counter(), 0
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT tar.themes_json
                FROM tag_analysis_results tar
                INNER JOIN patents p ON p.id = tar.patent_id
                WHERE p.source_keyword = ?
                """,
                (keyword,),
            )
            rows = cursor.fetchall()
        counter = Counter()
        for (themes_json,) in rows:
            try:
                themes = json.loads(themes_json or "{}")
            except Exception:
                themes = {}
            for tdata in themes.values():
                third = tdata.get("third_level_counts") or {}
                for k, v in third.items():
                    try:
                        counter[k] += int(v)
                    except Exception:
                        continue
        return counter, len(rows)

    def compute_label_overlap_threats(self, target_keyword, top_n=10):
        """
        针对公司类关键词，基于标签重合度（cosine+Jaccard）识别潜在威胁公司。
        返回排序后的列表：company, score, cosine, jaccard, common_top。
        """
        if not target_keyword:
            return []
        category = self.get_keyword_category(target_keyword)
        if category != "公司":
            return []

        # 目标标签向量
        target_vec, target_docs = self._aggregate_tags_for_keyword(target_keyword)
        if not target_vec or target_docs == 0:
            return []

        # 获取其他公司类关键词
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT keyword FROM keyword_categories
                WHERE category = '公司' AND keyword <> ?
                """,
                (target_keyword,),
            )
            kw_rows = [r[0] for r in cursor.fetchall()]
        if not kw_rows:
            return []

        def cosine(a: Counter, b: Counter):
            if not a or not b:
                return 0.0
            dot = sum(a[k] * b.get(k, 0) for k in a)
            if dot == 0:
                return 0.0
            na = sum(v * v for v in a.values()) ** 0.5
            nb = sum(v * v for v in b.values()) ** 0.5
            if na == 0 or nb == 0:
                return 0.0
            return dot / (na * nb)

        results = []
        tgt_keys = set(target_vec.keys())
        for kw in kw_rows:
            vec, cand_docs = self._aggregate_tags_for_keyword(kw)
            if not vec or cand_docs == 0:
                continue
            cand_keys = set(vec.keys())
            inter = tgt_keys & cand_keys
            union = tgt_keys | cand_keys
            jaccard = len(inter) / len(union) if union else 0.0
            cos = cosine(target_vec, vec)
            # 增加篇数权重，样本越少可信度越低
            doc_factor = min(target_docs, cand_docs) / max(target_docs, cand_docs) if max(target_docs, cand_docs) else 0
            score = 0.6 * cos + 0.25 * jaccard + 0.15 * doc_factor
            # 取共同标签 Top5，按 min(count)
            commons = []
            for lbl in inter:
                commons.append({
                    "label": lbl,
                    "target_count": target_vec.get(lbl, 0),
                    "candidate_count": vec.get(lbl, 0),
                    "min_count": min(target_vec.get(lbl, 0), vec.get(lbl, 0))
                })
            commons.sort(key=lambda x: x["min_count"], reverse=True)
            results.append({
                "company": kw,
                "score": round(score, 3),
                "cosine": round(cos, 3),
                "jaccard": round(jaccard, 3),
                "doc_factor": round(doc_factor, 3),
                "target_docs": target_docs,
                "candidate_docs": cand_docs,
                "common_top": commons[:5],
                "common_size": len(inter),
                "target_tags": len(tgt_keys),
                "candidate_tags": len(cand_keys)
            })

        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:top_n]

    def get_cooccurrence_summary(self, keyword=None, top_n=20):
        """
        基于当前数据库中的标签分析结果动态计算共现 TopN（完全不依赖“共现曲线”目录）。
        keyword 可选：若提供，则限定该关键词的数据。
        """
        summary = self.get_tag_summary(keyword=keyword) or {}
        co_list = (summary.get("summary") or {}).get("co_occurrence_top") or []
        pairs = []
        for item in co_list[:top_n]:
            pair = item.get("pair") or []
            count = item.get("count") or 0
            if len(pair) == 2:
                pairs.append({"labels": pair, "count": count})
        return {"pairs": pairs}

    def get_tag_results(self, page=1, page_size=20, search=None, keyword=None):
        """获取标签分析单篇结果（分页）"""
        try:
            with self.connect() as conn:
                cursor = conn.cursor()

                where_clauses = []
                params = []
                
                # 如果没有指定关键词，只显示当前存在的关键词对应的数据
                if not keyword:
                    cursor.execute("""
                        SELECT DISTINCT source_keyword 
                        FROM patents 
                        WHERE source_keyword IS NOT NULL AND source_keyword <> ''
                    """)
                    existing_keywords = [row[0] for row in cursor.fetchall()]
                    if existing_keywords:
                        placeholders = ','.join(['?'] * len(existing_keywords))
                        where_clauses.append(f"p.source_keyword IN ({placeholders})")
                        params.extend(existing_keywords)
                    else:
                        # 如果没有关键词，返回空结果
                        return {
                            "data": [],
                            "total": 0,
                            "page": page,
                            "page_size": page_size,
                            "total_pages": 0
                        }
                else:
                    where_clauses.append("p.source_keyword = ?")
                    params.append(keyword)
                
                if search:
                    where_clauses.append("(tar.title LIKE ? OR tar.ane LIKE ?)")
                    search_param = f"%{search}%"
                    params.extend([search_param, search_param])

                where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

                count_sql = f"""
                    SELECT COUNT(*)
                    FROM tag_analysis_results tar
                    INNER JOIN patents p ON p.id = tar.patent_id
                    {where_sql}
                """
                cursor.execute(count_sql, params)
                total = cursor.fetchone()[0]

                offset = (page - 1) * page_size
                sql = f"""
                    SELECT tar.patent_id, tar.ane, tar.title, tar.language, tar.total_matches,
                           tar.themes_json, tar.updated_at, p.source_keyword
                    FROM tag_analysis_results tar
                    INNER JOIN patents p ON p.id = tar.patent_id
                    {where_sql}
                    ORDER BY tar.total_matches DESC, tar.updated_at DESC
                    OFFSET ? ROWS FETCH NEXT ? ROWS ONLY
                """
                cursor.execute(sql, params + [offset, page_size])
                cols = [c[0] for c in cursor.description]
                rows = cursor.fetchall()

                data = []
                for r in rows:
                    item = dict(zip(cols, r))
                    if item.get("updated_at"):
                        item["updated_at"] = item["updated_at"].strftime("%Y-%m-%d %H:%M:%S")
                    try:
                        item["themes"] = json.loads(item.pop("themes_json") or "{}")
                    except Exception:
                        item["themes"] = {}
                    data.append(item)

                return {
                    "data": data,
                    "total": total,
                    "page": page,
                    "page_size": page_size,
                    "total_pages": (total + page_size - 1) // page_size
                }
        except Exception as e:
            raise Exception(f"查询标签分析结果失败: {str(e)}")

    def delete_keyword(self, keyword):
        """删除关键词及其所有相关数据"""
        try:
            with self.connect() as conn:
                cursor = conn.cursor()
                
                # 1. 获取该关键词关联的所有专利ID
                cursor.execute("SELECT id FROM patents WHERE source_keyword = ?", (keyword,))
                patent_ids = [row[0] for row in cursor.fetchall()]
                
                deleted_counts = {
                    "patents": 0,
                    "risk_alerts": 0,
                    "tag_analysis_results": 0,
                    "pdfs": 0
                }
                
                if patent_ids:
                    placeholders = ','.join(['?'] * len(patent_ids))
                    
                    # 2. 删除 tag_analysis_results 中关联的记录（如果表存在）
                    try:
                        cursor.execute("SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = 'tag_analysis_results'")
                        if cursor.fetchone()[0] > 0:
                            cursor.execute(f"DELETE FROM tag_analysis_results WHERE patent_id IN ({placeholders})", patent_ids)
                            deleted_counts["tag_analysis_results"] = cursor.rowcount
                    except Exception:
                        pass
                    
                    # 3. 删除 pdfs 中关联的记录（如果表存在）
                    try:
                        cursor.execute("SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = 'pdfs'")
                        if cursor.fetchone()[0] > 0:
                            cursor.execute(f"DELETE FROM pdfs WHERE patent_id IN ({placeholders})", patent_ids)
                            deleted_counts["pdfs"] = cursor.rowcount
                    except Exception:
                        pass
                
                # 4. 删除 risk_alerts 中关联的记录
                cursor.execute("DELETE FROM risk_alerts WHERE source_keyword = ?", (keyword,))
                deleted_counts["risk_alerts"] = cursor.rowcount
                
                # 5. 删除 patents 中关联的记录
                cursor.execute("DELETE FROM patents WHERE source_keyword = ?", (keyword,))
                deleted_counts["patents"] = cursor.rowcount
                
                return {
                    "success": True,
                    "keyword": keyword,
                    "deleted_counts": deleted_counts,
                    "total_deleted": sum(deleted_counts.values())
                }
        except Exception as e:
            raise Exception(f"删除关键词失败: {str(e)}")

    def get_monthly_patent_trends(self, keyword=None, months=12):
        """获取近N个月的专利/预警趋势数据"""
        try:
            with self.connect() as conn:
                cursor = conn.cursor()

                where_clauses = ["created_at >= DATEADD(month, -{}, GETDATE())".format(months)]
                params_kw = []
                if keyword:
                    where_clauses.append("source_keyword = ?")
                    params_kw = [keyword]

                where_clause = "WHERE " + " AND ".join(where_clauses)

                cursor.execute(f"""
                    SELECT
                        YEAR(created_at) as year,
                        MONTH(created_at) as month,
                        COUNT(*) as count
                    FROM patents
                    {where_clause}
                    GROUP BY YEAR(created_at), MONTH(created_at)
                    ORDER BY year, month
                """, params_kw)

                results = []
                for row in cursor.fetchall():
                    results.append({
                        "month": f"{row[0]}-{row[1]:02d}",
                        "count": row[2]
                    })

                # 填充缺失月份
                from datetime import datetime, timedelta
                month_list = []
                current = datetime.now()
                for i in range(months - 1, -1, -1):
                    d = current - timedelta(days=30 * i)
                    month_list.append(f"{d.year}-{d.month:02d}")

                data_map = {r["month"]: r["count"] for r in results}
                filled_results = [{"month": m, "count": data_map.get(m, 0)} for m in month_list]

                return filled_results
        except Exception as e:
            raise Exception(f"查询月度趋势失败: {str(e)}")

    def get_hourly_alert_distribution(self, keyword=None):
        """获取每小时预警分布（8:00-22:00）"""
        try:
            with self.connect() as conn:
                cursor = conn.cursor()

                where_clauses = [
                    "updated_at IS NOT NULL",
                    "DATEPART(HOUR, updated_at) BETWEEN 8 AND 22"
                ]
                params_kw = []
                if keyword:
                    where_clauses.append("source_keyword = ?")
                    params_kw = [keyword]

                where_clause = "WHERE " + " AND ".join(where_clauses)

                cursor.execute(f"""
                    SELECT
                        DATEPART(HOUR, updated_at) as hour,
                        COUNT(*) as count
                    FROM risk_alerts
                    {where_clause}
                    GROUP BY DATEPART(HOUR, updated_at)
                    ORDER BY hour
                """, params_kw)

                results = {}
                for row in cursor.fetchall():
                    results[f"{row[0]}:00"] = row[1]

                # 填充8:00-22:00的所有小时
                filled_results = []
                for h in range(8, 23):
                    hour_key = f"{h}:00"
                    filled_results.append({
                        "hour": hour_key,
                        "count": results.get(hour_key, 0)
                    })

                return filled_results
        except Exception as e:
            raise Exception(f"查询小时预警分布失败: {str(e)}")

    def get_keyword_category_stats(self):
        """获取各分类关键词数量统计"""
        try:
            with self.connect() as conn:
                cursor = conn.cursor()

                cursor.execute("""
                    SELECT
                        kc.category,
                        COUNT(DISTINCT p.source_keyword) as count
                    FROM patents p
                    LEFT JOIN keyword_categories kc ON kc.keyword = p.source_keyword
                    WHERE p.source_keyword IS NOT NULL AND p.source_keyword <> ''
                    GROUP BY kc.category
                    ORDER BY count DESC
                """)

                results = []
                total = 0
                for row in cursor.fetchall():
                    category = row[0] or "未分类"
                    count = row[1]
                    total += count
                    results.append({"category": category, "count": count})

                return {"stats": results, "total": total}
        except Exception as e:
            raise Exception(f"查询关键词分类统计失败: {str(e)}")

    def get_dashboard_data(self, keyword=None):
        """获取数据大屏所需的全部统计数据"""
        try:
            # 基础统计
            stats = self.get_statistics(keyword)

            # 月度趋势
            monthly_trends = self.get_monthly_patent_trends(keyword, 12)

            # 小时分布
            hourly_alerts = self.get_hourly_alert_distribution(keyword)

            # 关键词分类统计
            category_stats = self.get_keyword_category_stats()

            # 公司威胁榜Top5
            threat_companies = self.get_threat_companies(keyword, limit=5)

            # 标签汇总
            tag_summary_data = self.get_tag_summary(keyword)
            tag_summary = (tag_summary_data.get("summary") or {}) if tag_summary_data else {}

            return {
                "stats": stats,
                "monthly_trends": monthly_trends,
                "hourly_alerts": hourly_alerts,
                "category_stats": category_stats,
                "threat_companies": threat_companies,
                "tag_summary": tag_summary
            }
        except Exception as e:
            raise Exception(f"获取大屏数据失败: {str(e)}")

# 初始化数据库管理器
db = DatabaseManager()

_crawl_lock = threading.Lock()
_crawl_proc = None
_last_crawl_log = ""
_current_crawl_keyword = None  # 保存当前正在爬取的关键词
_analysis_lock = threading.Lock()  # 一键运行预警分析锁
_update_cancel_flag = False  # monthly update 取消标记
_update_running = False      # monthly update 运行标记


def _generate_personalized_recommendations(report):
    """
    基于报告数据生成针对本公司的个性化建议与警告
    返回: (warnings, recommendations)
    """
    warnings = []
    recommendations = []
    
    stats = report.get("stats", {})
    alerts = report.get("alerts_top", []) or []
    threats = report.get("threats") or []
    applicants = report.get("applicants", []) or []
    overlap_threats = report.get("label_overlap_threats") or []
    tag_summary = (report.get("tag_summary") or {}).get("summary") or {}
    keyword = report.get("keyword")
    category = report.get("category")
    
    # 统计数据
    total_alerts = stats.get("total_alerts", 0)
    avg_risk_score = stats.get("avg_risk_score", 0)
    recent_alerts = stats.get("recent_alerts", 0)
    risk_dist = stats.get("risk_distribution", {}) or {}
    high_risk_count = risk_dist.get("高", 0)
    mid_risk_count = risk_dist.get("中", 0)
    low_risk_count = risk_dist.get("低", 0)
    
    # ========== 警告信息 ==========
    
    # 1. 高风险专利数量警告
    if high_risk_count >= 10:
        warnings.append({
            "level": "严重",
            "title": "高风险专利数量较多",
            "content": f"当前检测到{high_risk_count}个高风险专利（风险分≥60），建议立即启动专利风险评估流程，优先对Top5高风险专利进行权利要求比对分析。"
        })
    elif high_risk_count >= 5:
        warnings.append({
            "level": "中等",
            "title": "存在多个高风险专利",
            "content": f"检测到{high_risk_count}个高风险专利，建议在30天内完成初步风险评估，重点关注风险分≥70的专利。"
        })
    
    # 2. 平均风险分警告
    if avg_risk_score >= 60:
        warnings.append({
            "level": "严重",
            "title": "整体风险水平偏高",
            "content": f"平均风险分达到{avg_risk_score:.1f}分，表明竞争环境较为激烈。建议加强专利监控频率，建立每周风险回顾机制。"
        })
    elif avg_risk_score >= 50:
        warnings.append({
            "level": "中等",
            "title": "风险水平需关注",
            "content": f"平均风险分为{avg_risk_score:.1f}分，建议每月进行风险趋势分析，关注新公开专利的动态变化。"
        })
    
    # 3. 最近新增预警警告
    if recent_alerts >= 10:
        warnings.append({
            "level": "严重",
            "title": "近期预警数量激增",
            "content": f"最近7天新增{recent_alerts}个预警，可能存在竞争对手集中布局的情况。建议立即分析新增预警的申请人分布和技术领域。"
        })
    elif recent_alerts >= 5:
        warnings.append({
            "level": "中等",
            "title": "近期预警增加",
            "content": f"最近7天新增{recent_alerts}个预警，建议关注这些新预警的风险等级分布和申请人信息。"
        })
    
    # 4. 威胁公司警告
    if threats:
        top_threat = threats[0]
        top_score = top_threat.get("avg_score", 0)
        top_count = top_threat.get("count", 0)
        if top_score >= 75 and top_count >= 5:
            warnings.append({
                "level": "严重",
                "title": "发现高威胁竞争对手",
                "content": f"检测到威胁公司「{top_threat.get('company', '未知')}」平均风险分{top_score:.1f}分，预警数量{top_count}个。建议成立专项应对小组，深入分析其专利布局策略和技术路线。"
            })
        elif top_score >= 65:
            warnings.append({
                "level": "中等",
                "title": "存在潜在威胁公司",
                "content": f"威胁公司「{top_threat.get('company', '未知')}」平均风险分{top_score:.1f}分，建议持续监控其专利动态，评估技术重叠度。"
            })
    
    # 5. 标签重合度威胁警告（仅公司类）
    if category == "公司" and overlap_threats:
        top_overlap = overlap_threats[0]
        overlap_score = top_overlap.get("score", 0)
        if overlap_score >= 0.7:
            warnings.append({
                "level": "严重",
                "title": "技术标签高度重合",
                "content": f"检测到公司「{top_overlap.get('company', '未知')}」与本公司技术标签重合度达{overlap_score:.2f}，共同标签{top_overlap.get('common_size', 0)}个。这表明双方技术路线高度相似，存在直接竞争风险，建议优先分析其核心专利。"
            })
        elif overlap_score >= 0.5:
            warnings.append({
                "level": "中等",
                "title": "技术标签存在重合",
                "content": f"公司「{top_overlap.get('company', '未知')}」与本公司标签重合度{overlap_score:.2f}，建议关注其技术发展方向，评估潜在竞争关系。"
            })
    
    # 6. 高风险专利集中警告
    if alerts:
        top3_scores = [a.get("risk_score", 0) for a in alerts[:3]]
        if all(s >= 80 for s in top3_scores if s):
            warnings.append({
                "level": "严重",
                "title": "存在极高风险专利",
                "content": f"Top3高风险专利风险分均≥80分，这些专利可能对业务构成直接威胁。建议在15天内完成详细的权利要求分析，评估侵权风险并制定应对策略。"
            })
    
    # ========== 个性化建议 ==========
    
    # 1. 基于风险分布的建议
    if high_risk_count > 0:
        recommendations.append({
            "category": "风险应对",
            "priority": "高",
            "content": f"针对{high_risk_count}个高风险专利，建议采取以下措施：\n"
                      f"① 立即启动专利风险评估流程，优先分析风险分≥70的专利；\n"
                      f"② 对每个高风险专利进行权利要求比对，评估与本公司产品的技术重叠度；\n"
                      f"③ 对于存在侵权风险的专利，制定技术绕开方案或无效策略；\n"
                      f"④ 建立高风险专利跟踪清单，定期更新风险评估结果。"
        })
    
    if mid_risk_count >= 10:
        recommendations.append({
            "category": "风险监控",
            "priority": "中",
            "content": f"当前有{mid_risk_count}个中风险专利，建议：\n"
                      f"① 建立中风险专利监控清单，每月更新一次；\n"
                      f"② 关注这些专利的授权状态变化，及时评估授权后的风险；\n"
                      f"③ 分析中风险专利的技术领域分布，识别潜在的技术竞争热点。"
        })
    
    # 2. 基于威胁公司的建议
    if threats:
        high_risk_companies = [t for t in threats if t.get("avg_score", 0) >= 70]
        if high_risk_companies:
            company_names = [t.get("company", "") for t in high_risk_companies[:3]]
            recommendations.append({
                "category": "竞争分析",
                "priority": "高",
                "content": f"针对高风险威胁公司（{', '.join(company_names)}），建议：\n"
                          f"① 深入分析其专利布局策略，识别核心技术领域和研发重点；\n"
                          f"② 建立竞争对手专利监控机制，实时跟踪其新公开/授权专利；\n"
                          f"③ 评估双方技术路线重叠度，制定差异化竞争策略；\n"
                          f"④ 与法务部门协同，准备必要的专利防御和无效策略。"
            })
        
        if len(threats) >= 5:
            recommendations.append({
                "category": "战略规划",
                "priority": "中",
                "content": f"检测到{len(threats)}个潜在威胁公司，建议：\n"
                          f"① 建立竞争对手情报库，定期更新威胁公司榜单；\n"
                          f"② 分析威胁公司的专利申请趋势，预测其技术发展方向；\n"
                          f"③ 制定专利布局策略，在关键技术领域加强专利申请；\n"
                          f"④ 建立跨部门协作机制，将专利风险信息及时传递给研发和业务部门。"
            })
    
    # 3. 基于标签重合度的建议（仅公司类）
    if category == "公司" and overlap_threats:
        top_overlap = overlap_threats[0]
        common_labels = [c.get("label", "") for c in top_overlap.get("common_top", [])[:3]]
        if common_labels:
            recommendations.append({
                "category": "技术路线",
                "priority": "高",
                "content": f"基于标签重合度分析，与「{top_overlap.get('company', '未知')}」在以下技术标签高度重合：{', '.join(common_labels)}。建议：\n"
                          f"① 深入分析双方在这些技术领域的专利布局差异；\n"
                          f"② 评估技术路线的优劣，考虑是否需要调整研发方向；\n"
                          f"③ 加强重合技术领域的专利保护，建立技术壁垒；\n"
                          f"④ 关注竞争对手在这些标签下的新专利动态。"
            })
    
    # 4. 基于申请人风险画像的建议
    if applicants:
        top_applicant = applicants[0]
        if top_applicant.get("avg_score", 0) >= 65:
            recommendations.append({
                "category": "申请人监控",
                "priority": "中",
                "content": f"申请人「{top_applicant.get('applicants', '未知')}」平均风险分{top_applicant.get('avg_score', 0):.1f}分，预警数量{top_applicant.get('count', 0)}个。建议：\n"
                          f"① 将该申请人纳入重点监控名单；\n"
                          f"② 分析其专利技术领域分布，识别核心技术；\n"
                          f"③ 关注其专利申请趋势，评估未来竞争风险。"
            })
    
    # 5. 基于标签热度的建议
    hot_tags = tag_summary.get("hot", []) or []
    if hot_tags:
        top_tags = [t.get("label", "") for t in hot_tags[:5]]
        recommendations.append({
            "category": "技术趋势",
            "priority": "中",
            "content": f"当前热门技术标签：{', '.join(top_tags)}。建议：\n"
                      f"① 分析这些热门标签下的专利竞争态势；\n"
                      f"② 评估本公司在这些技术领域的专利布局情况；\n"
                      f"③ 考虑在热门技术领域加强专利申请，抢占技术制高点；\n"
                      f"④ 关注热门标签的专利共现关系，识别技术组合趋势。"
        })
    
    # 6. 基于预警趋势的建议
    if recent_alerts > 0:
        recommendations.append({
            "category": "监控频率",
            "priority": "中",
            "content": f"最近7天新增{recent_alerts}个预警，建议：\n"
                      f"① 提高专利监控频率，建议每周至少查看一次新预警；\n"
                      f"② 建立预警分级处理机制，高风险预警优先处理；\n"
                      f"③ 定期分析预警趋势，识别风险变化规律；\n"
                      f"④ 将预警信息及时同步给相关部门，形成快速响应机制。"
        })
    
    # 7. 通用建议
    if total_alerts > 0:
        recommendations.append({
            "category": "制度建设",
            "priority": "中",
            "content": "建议建立完善的专利风险管理制度：\n"
                      f"① 制定专利风险评估标准流程，明确不同风险等级的应对措施；\n"
                      f"② 建立专利风险定期报告机制，建议每月生成一次风险报告；\n"
                      f"③ 加强跨部门协作，确保专利风险信息及时传递；\n"
                      f"④ 定期复盘风险应对效果，持续优化风险管理策略。"
        })
    else:
        recommendations.append({
            "category": "基础建议",
            "priority": "低",
            "content": "当前预警数量较少，建议：\n"
                      f"① 保持常规监控频率，关注新公开的相关专利；\n"
                      f"② 定期更新关键词和监控范围，确保覆盖主要技术领域；\n"
                      f"③ 建立预警响应机制，为未来可能出现的风险做好准备。"
        })
    
    return warnings, recommendations


def _render_report_pdf(report):
    """使用 reportlab 生成PDF，若库不可用返回 None"""
    if not _reportlab_available:
        return None
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=18 * mm, bottomMargin=18 * mm)
    styles = getSampleStyleSheet()

    # 注册中文字体，避免中文显示为方块
    try:
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        base_font = "STSong-Light"
    except Exception:
        base_font = styles["Normal"].fontName  # 兜底

    for name in ["Title", "Heading1", "Heading2", "Heading3", "Normal"]:
        if name in styles:
            styles[name].fontName = base_font

    # 样式微调：字号、行距、颜色、段前后距
    styles["Normal"].fontSize = 11
    styles["Normal"].leading = 15
    styles["Normal"].textColor = colors.HexColor("#111111")

    styles["Heading2"].fontSize = 14
    styles["Heading2"].leading = 18
    styles["Heading2"].spaceBefore = 8
    styles["Heading2"].spaceAfter = 6
    styles["Heading2"].textColor = colors.HexColor("#0f172a")

    styles["Heading3"].fontSize = 12
    styles["Heading3"].leading = 16
    styles["Heading3"].spaceBefore = 6
    styles["Heading3"].spaceAfter = 4
    styles["Heading3"].textColor = colors.HexColor("#1f2937")

    def set_table_font(tbl):
        tbl.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), base_font),
        ]))

    story = []
    story.append(Paragraph("关键词风险预警报告", styles["Title"]))
    meta = f"关键词: {report.get('keyword') or '全部'} | 生成时间: {report.get('generated_at')}"
    story.append(Paragraph(meta, styles["Normal"]))
    story.append(Spacer(1, 10))

    # 风险评分说明
    story.append(Paragraph("评分方法与意义", styles["Heading2"]))
    story.append(Paragraph(
        "综合因子：关键词命中、语义近似、时效性（公开/公告日）、竞争领域词、权利要求信号、申请人历史风险、标签热度/共现。"
        " 各因子加权后形成 1-99 分的风险分，分级：高(≥60)、中(35-59)、低(<35)。",
        styles["Normal"]
    ))
    story.append(Paragraph(
        "意义：分数越高，表示潜在侵权或竞争压力越大；高分通常伴随强信号（关键词/语义/权利要求/热门标签/高风险申请人）。",
        styles["Normal"]
    ))
    story.append(Paragraph(
        "行动建议：对高风险专利/公司优先做权利要求比对与设计绕开评估；中风险纳入监测清单，关注后续公开/授权；低风险保持常规监控。",
        styles["Normal"]
    ))
    story.append(Spacer(1, 8))

    stats = report.get("stats", {})
    overview_lines = [
        f"专利总数: {stats.get('total_patents', 0)}",
        f"预警总数: {stats.get('total_alerts', 0)}",
        f"平均风险分: {stats.get('avg_risk_score', 0)}",
        f"最近7天新增预警: {stats.get('recent_alerts', 0)}",
        f"PDF数量: {stats.get('total_pdfs', 0)}",
    ]
    story.append(Paragraph("一、总体概览", styles["Heading2"]))
    for line in overview_lines:
        story.append(Paragraph(line, styles["Normal"]))
    story.append(Spacer(1, 6))

    # 风险等级分布
    dist = stats.get("risk_distribution", {}) or {}
    table_data = [["风险等级", "数量"]]
    for lvl in ("高", "中", "低"):
        table_data.append([lvl, dist.get(lvl, 0)])
    table = Table(table_data, colWidths=[50 * mm, 30 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f2f2f2")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d6d6d6")),
        ("ALIGN", (1, 1), (-1, -1), "CENTER"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    set_table_font(table)
    story.append(Paragraph("风险等级分布", styles["Heading3"]))
    story.append(table)
    story.append(Spacer(1, 8))

    # 高风险Top
    story.append(Paragraph("二、高风险清单（Top15）", styles["Heading2"]))
    alerts = report.get("alerts_top", []) or []
    if alerts:
        rows = [["标题", "风险分", "等级", "申请人", "公开日"]]
        for a in alerts:
            rows.append([
                Paragraph(a.get("title", "") or "-", styles["Normal"]),
                a.get("risk_score", ""),
                a.get("risk_level", ""),
                Paragraph(a.get("applicants", "") or "-", styles["Normal"]),
                a.get("publication_date", "") or "-",
            ])
        # 总宽控制在约 174mm 内，避免重叠
        table = Table(rows, colWidths=[80 * mm, 18 * mm, 14 * mm, 42 * mm, 20 * mm])
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f2f2f2")),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d6d6d6")),
            ("ALIGN", (1, 1), (2, -1), "CENTER"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        set_table_font(table)
        story.append(table)
    else:
        story.append(Paragraph("暂无数据", styles["Normal"]))
    story.append(Spacer(1, 8))

    # 申请人画像
    story.append(Paragraph("三、申请人风险画像", styles["Heading2"]))
    applicants = report.get("applicants", []) or []
    if applicants:
        rows = [["申请人", "预警数量", "平均风险分"]]
        for ap in applicants:
            rows.append([Paragraph(ap.get("applicants", "") or "-", styles["Normal"]), ap.get("count", 0), ap.get("avg_score", 0)])
        table = Table(rows, colWidths=[80 * mm, 30 * mm, 30 * mm])
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f2f2f2")),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d6d6d6")),
            ("ALIGN", (1, 1), (-1, -1), "CENTER"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        set_table_font(table)
        story.append(table)
    else:
        story.append(Paragraph("暂无申请人画像数据", styles["Normal"]))
    story.append(Spacer(1, 8))

    # 威胁公司与建议（面向公司类关键词）
    threats = report.get("threats") or []
    story.append(Paragraph("四、潜在威胁公司与建议", styles["Heading2"]))
    if threats:
        rows = [["公司", "预警数量", "平均风险分"]]
        for t in threats:
            rows.append([Paragraph(t.get("company", "") or "-", styles["Normal"]), t.get("count", 0), t.get("avg_score", 0)])
        table = Table(rows, colWidths=[80 * mm, 30 * mm, 30 * mm])
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f2f2f2")),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d6d6d6")),
            ("ALIGN", (1, 1), (-1, -1), "CENTER"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        set_table_font(table)
        story.append(table)
    else:
        story.append(Paragraph("暂无可识别的威胁公司", styles["Normal"]))
    story.append(Spacer(1, 6))

    # 建议生成
    suggestions = []
    if threats:
        top_avg = threats[0].get("avg_score", 0)
        high_risk = [t for t in threats if t.get("avg_score", 0) >= 70]
        mid_risk = [t for t in threats if 50 <= t.get("avg_score", 0) < 70]
        if high_risk:
            suggestions.append("优先研判高风险公司（平均风险分≥70），对其核心专利与权利要求进行比对，必要时准备无效或绕开方案。")
        if mid_risk:
            suggestions.append("对中风险公司（50-69）建立监控清单，关注其新公开/授权动向，提前评估潜在侵权场景。")
        if top_avg >= 80:
            suggestions.append("针对头部威胁公司，建议成立专项小组，与法务/业务协同推进竞争情报与防御策略。")
        if len(threats) >= 5:
            suggestions.append("建立定期复盘机制，每月更新威胁公司榜单与风险分布，动态调整应对优先级。")
    else:
        suggestions.append("当前未识别高风险竞争公司，建议保持常规监控，关注新近公开的相关专利。")

    story.append(Paragraph("建议：", styles["Heading3"]))
    for s in suggestions:
        story.append(Paragraph(f"• {s}", styles["Normal"]))
    story.append(Spacer(1, 8))

    # 标签重合度威胁公司（仅公司类关键词）：基于标签重合度的相似公司
    overlap_threats = report.get("label_overlap_threats") or []
    story.append(Paragraph("五、标签重合度威胁公司（公司类关键词）", styles["Heading2"]))
    if overlap_threats:
        rows = [["公司", "重合度(0-1)", "共同标签数", "Top共同标签"]]
        for item in overlap_threats:
            common_labels = ", ".join([c.get("label", "") for c in item.get("common_top", [])])
            rows.append([
                Paragraph(item.get("company", "-") or "-", styles["Normal"]),
                item.get("score", 0),
                item.get("common_size", 0),
                Paragraph(common_labels or "-", styles["Normal"])
            ])
        table = Table(rows, colWidths=[70 * mm, 25 * mm, 25 * mm, 50 * mm])
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f2f2f2")),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d6d6d6")),
            ("ALIGN", (1, 1), (2, -1), "CENTER"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6), 
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("WORDWRAP", (0, 0), (-1, -1), None),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        set_table_font(table)
        story.append(table)
        story.append(Paragraph(
            "重合度综合了标签向量余弦与标签集合Jaccard，代表与本公司的技术标签相似度；共同标签越多、重合度越高，潜在竞争或威胁越大。",
            styles["Normal"]
        ))
    else:
        story.append(Paragraph("当前关键词非公司类或缺少标签数据，暂未生成标签重合度威胁公司。", styles["Normal"]))
    story.append(Spacer(1, 8))

    # 标签摘要
    story.append(Paragraph("六、标签摘要", styles["Heading2"]))
    tag_summary = (report.get("tag_summary") or {}).get("summary") or {}
    hot = tag_summary.get("hot") or []
    cold = tag_summary.get("cold") or []
    if hot:
        story.append(Paragraph("热门标签（前10）", styles["Heading3"]))
        story.append(Paragraph(", ".join([f"{x.get('label')}({x.get('count')})" for x in hot[:10]]), styles["Normal"]))
    if cold:
        story.append(Paragraph("冷门标签（前10）", styles["Heading3"]))
        story.append(Paragraph(", ".join([f"{x.get('label')}({x.get('count')})" for x in cold[:10]]), styles["Normal"]))
    if not hot and not cold:
        story.append(Paragraph("暂无标签摘要数据", styles["Normal"]))
    story.append(Spacer(1, 8))

    # 专利概览
    story.append(Paragraph("五、专利概览（Top15）", styles["Heading2"]))
    patents = report.get("patents_top", []) or []
    if patents:
        rows = [["标题", "风险分", "等级", "申请人", "公开日"]]
        for p in patents:
            rows.append([
                Paragraph(p.get("title", "") or "-", styles["Normal"]),
                p.get("risk_score", "") or "-",
                p.get("risk_level", "") or "-",
                Paragraph((p.get("applicant") or p.get("patentee") or "") or "-", styles["Normal"]),
                p.get("publication_date", "") or "-",
            ])
        table = Table(rows, colWidths=[80 * mm, 18 * mm, 14 * mm, 42 * mm, 20 * mm])
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f2f2f2")),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d6d6d6")),
            ("ALIGN", (1, 1), (2, -1), "CENTER"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        set_table_font(table)
        story.append(table)
    else:
        story.append(Paragraph("暂无专利概览数据", styles["Normal"]))

    # 标签共现摘要：基于系统内最新的标签分析共现 Top
    story.append(Spacer(1, 8))
    story.append(Paragraph("六、标签共现摘要", styles["Heading2"]))
    co_summary = (report.get("cooccurrence_summary") or {}).get("pairs") or []
    if co_summary:
        rows = [["标签对", "共现次数"]]
        for item in co_summary[:12]:
            labels = item.get("labels") or []
            pair_name = " / ".join(labels) if labels else "-"
            rows.append([Paragraph(pair_name, styles["Normal"]), item.get("count", 0)])
        table = Table(rows, colWidths=[90 * mm, 40 * mm])
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f2f2f2")),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d6d6d6")),
            ("ALIGN", (1, 1), (-1, -1), "CENTER"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        set_table_font(table)
        story.append(table)
        story.append(Paragraph(
            "以上共现对来源于系统内最新的标签分析结果，体现当前语料中标签组合的高频关联关系。",
            styles["Normal"]
        ))
    else:
        story.append(Paragraph("当前未计算到标签共现数据。", styles["Normal"]))
    
    # 个性化建议与警告
    story.append(Spacer(1, 12))
    story.append(Paragraph("七、个性化建议与警告", styles["Heading2"]))
    
    warnings, recommendations = _generate_personalized_recommendations(report)
    
    # 警告信息
    if warnings:
        story.append(Paragraph("⚠ 重要警告", styles["Heading3"]))
        for warning in warnings:
            level = warning.get("level", "一般")
            title = warning.get("title", "")
            content = warning.get("content", "")
            
            # 根据警告级别设置颜色
            if level == "严重":
                warning_color = colors.HexColor("#dc2626")  # 红色
            elif level == "中等":
                warning_color = colors.HexColor("#ea580c")  # 橙色
            else:
                warning_color = colors.HexColor("#ca8a04")  # 黄色
            
            # 创建警告样式
            warning_style = ParagraphStyle(
                name="WarningStyle",
                parent=styles["Normal"],
                fontName=base_font,
                fontSize=11,
                leading=15,
                textColor=warning_color,
                spaceAfter=6,
            )
            
            warning_text = f"【{level}】{title}"
            story.append(Paragraph(warning_text, warning_style))
            story.append(Paragraph(content, styles["Normal"]))
            story.append(Spacer(1, 4))
    else:
        story.append(Paragraph("当前未检测到需要特别关注的警告信息。", styles["Normal"]))
    
    story.append(Spacer(1, 8))
    
    # 个性化建议
    if recommendations:
        story.append(Paragraph("💡 个性化建议", styles["Heading3"]))
        
        # 按优先级分组
        high_priority = [r for r in recommendations if r.get("priority") == "高"]
        mid_priority = [r for r in recommendations if r.get("priority") == "中"]
        low_priority = [r for r in recommendations if r.get("priority") == "低"]
        
        # 高优先级建议
        if high_priority:
            story.append(Paragraph("高优先级建议：", styles["Heading3"]))
            for rec in high_priority:
                category = rec.get("category", "")
                content = rec.get("content", "")
                story.append(Paragraph(f"【{category}】", styles["Normal"]))
                # 处理多行内容
                for line in content.split("\n"):
                    if line.strip():
                        story.append(Paragraph(f"  {line.strip()}", styles["Normal"]))
                story.append(Spacer(1, 4))
        
        # 中优先级建议
        if mid_priority:
            story.append(Spacer(1, 4))
            story.append(Paragraph("中优先级建议：", styles["Heading3"]))
            for rec in mid_priority:
                category = rec.get("category", "")
                content = rec.get("content", "")
                story.append(Paragraph(f"【{category}】", styles["Normal"]))
                for line in content.split("\n"):
                    if line.strip():
                        story.append(Paragraph(f"  {line.strip()}", styles["Normal"]))
                story.append(Spacer(1, 4))
        
        # 低优先级建议
        if low_priority:
            story.append(Spacer(1, 4))
            story.append(Paragraph("低优先级建议：", styles["Heading3"]))
            for rec in low_priority:
                category = rec.get("category", "")
                content = rec.get("content", "")
                story.append(Paragraph(f"【{category}】", styles["Normal"]))
                for line in content.split("\n"):
                    if line.strip():
                        story.append(Paragraph(f"  {line.strip()}", styles["Normal"]))
                story.append(Spacer(1, 4))
    else:
        story.append(Paragraph("当前暂无个性化建议。", styles["Normal"]))
    
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        "注：以上建议与警告基于当前报告数据自动生成，请结合实际情况和业务需求进行决策。",
        styles["Normal"]
    ))

    doc.build(story)
    buffer.seek(0)
    return buffer


def _render_report_html(report):
    """生成简易HTML报告（用于无PDF依赖时返回）"""
    stats = report.get("stats", {})
    alerts = report.get("alerts_top", []) or []
    applicants = report.get("applicants", []) or []
    tag_summary = (report.get("tag_summary") or {}).get("summary") or {}
    patents = report.get("patents_top", []) or []
    co_summary = (report.get("cooccurrence_summary") or {}).get("pairs") or []

    html = []
    html.append(f"<h2>关键词风险预警报告（{report.get('keyword') or '全部'}）</h2>")
    html.append(f"<p>生成时间: {report.get('generated_at')}</p>")
    html.append("<h3>总体概览</h3>")
    html.append(
        f"<ul><li>专利总数: {stats.get('total_patents',0)}</li>"
        f"<li>预警总数: {stats.get('total_alerts',0)}</li>"
        f"<li>平均风险分: {stats.get('avg_risk_score',0)}</li>"
        f"<li>最近7天新增预警: {stats.get('recent_alerts',0)}</li>"
        f"<li>PDF数量: {stats.get('total_pdfs',0)}</li></ul>"
    )

    html.append("<h3>高风险清单（Top15）</h3><ol>")
    for a in alerts:
        html.append(
            f"<li>[{a.get('risk_level')}] {a.get('title','')} - 分数 {a.get('risk_score')} - 申请人 {a.get('applicants','')}"
            f" - 公开日 {a.get('publication_date','')}</li>"
        )
    html.append("</ol>" if alerts else "<p>暂无数据</p>")

    html.append("<h3>申请人风险画像</h3><ol>")
    for ap in applicants:
        html.append(
            f"<li>{ap.get('applicants','')} - 预警 {ap.get('count',0)} - 平均分 {ap.get('avg_score',0)}</li>"
        )
    html.append("</ol>" if applicants else "<p>暂无数据</p>")

    html.append("<h3>标签摘要</h3>")
    hot = tag_summary.get("hot") or []
    cold = tag_summary.get("cold") or []
    if hot:
        html.append("<p>热门标签: " + ", ".join([x.get("label","") for x in hot[:10]]) + "</p>")
    if cold:
        html.append("<p>冷门标签: " + ", ".join([x.get("label","") for x in cold[:10]]) + "</p>")
    if not hot and not cold:
        html.append("<p>暂无标签数据</p>")

    html.append("<h3>专利概览（Top15）</h3><ol>")
    for p in patents:
        html.append(
            f"<li>[{p.get('risk_level') or '-'}] {p.get('title','')} - 分数 {p.get('risk_score') or '-'}"
            f" - 申请人 {p.get('applicant') or p.get('patentee') or ''} - 公开日 {p.get('publication_date') or '-'}"
            "</li>"
        )
    html.append("</ol>" if patents else "<p>暂无数据</p>")
    # 标签重合度威胁公司（仅公司关键词）
    overlap_threats = report.get("label_overlap_threats") or []
    html.append("<h3>标签重合度威胁公司（公司类关键词）</h3>")
    if overlap_threats:
        html.append("<ol>")
        for item in overlap_threats:
            common_labels = ", ".join([c.get("label","") for c in item.get("common_top", [])])
            html.append(
                f"<li>{item.get('company','-')} - 重合度 {item.get('score',0)} - 共同标签 {item.get('common_size',0)} - Top: {common_labels or '-'}</li>"
            )
        html.append("</ol>")
    else:
        html.append("<p>当前关键词非公司类或缺少标签数据，暂无标签重合度威胁公司。</p>")

    html.append("<h3>标签共现摘要（系统内计算）</h3>")
    if co_summary:
        html.append("<ol>")
        for item in co_summary[:12]:
            labels = item.get("labels") or []
            pair_name = " / ".join(labels) if labels else "-"
            html.append(f"<li>{pair_name} - 共现次数 {item.get('count', 0)}</li>")
        html.append("</ol>")
    else:
        html.append("<p>暂无共现数据</p>")
    return "\n".join(html)

@app.route('/api/cooccurrence-graphs', methods=['GET'])
def api_cooccurrence_graphs():
    """
    列出当前系统生成的共现知识图谱 HTML 文件（预警分析技术点2生成）。
    """
    try:
        base_dir = os.path.abspath(os.path.dirname(__file__))
        graphs_dir = os.path.join(base_dir, '预警分析技术点2', 'knowledge_graphs')
        if not os.path.isdir(graphs_dir):
            return jsonify({'success': True, 'result': []})

        files = []
        for name in os.listdir(graphs_dir):
            if not name.lower().endswith('.html'):
                continue
            full_path = os.path.join(graphs_dir, name)
            if not os.path.isfile(full_path):
                continue
            files.append({
                'name': name,
                'url': f"/cooccurrence-graph/{name}"
            })
        files.sort(key=lambda x: x['name'])
        return jsonify({'success': True, 'result': files})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/cooccurrence-graph/<path:filename>', methods=['GET'])
def view_cooccurrence_graph(filename):
    """
    直接在浏览器中打开指定的共现知识图谱 HTML（pyecharts 输出），实现“一键打开共现图谱”。
    """
    try:
        # 安全限制：仅允许访问 knowledge_graphs 下的 html 文件
        if '/' in filename or '\\' in filename or not filename.lower().endswith('.html'):
            return "非法文件名", 400

        base_dir = os.path.abspath(os.path.dirname(__file__))
        graphs_dir = os.path.join(base_dir, '预警分析技术点2', 'knowledge_graphs')
        file_path = os.path.join(graphs_dir, filename)

        if not os.path.isfile(file_path):
            return "文件不存在", 404

        return send_file(file_path, mimetype='text/html')
    except Exception as e:
        return f"打开知识图谱失败: {e}", 500


@app.route('/api/stop-monthly-update', methods=['POST'])
def api_stop_monthly_update():
    """请求停止 monthly-update 循环（公司/全部均可）。"""
    global _update_cancel_flag
    _update_cancel_flag = True
    return jsonify({'success': True, 'message': '已请求停止更新，当前轮次将尽快中止'})


@app.route('/api/company-threats', methods=['GET'])
def api_company_threats():
    """基于标签重合度的公司威胁榜（仅公司类关键词）"""
    try:
        keyword = request.args.get('keyword', None)
        if not keyword:
            return jsonify({'success': False, 'error': 'keyword 不能为空'}), 400
        data = db.compute_label_overlap_threats(target_keyword=keyword, top_n=10)
        return jsonify({'success': True, 'result': data})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
@app.route('/')
def index():
    """主页面"""
    return render_template('index.html')

@app.route('/api/patents', methods=['GET'])
def api_patents():
    """获取专利列表API"""
    try:
        page = int(request.args.get('page', 1))
        page_size = int(request.args.get('page_size', 20))
        search = request.args.get('search', None)
        keyword = request.args.get('keyword', None)
        
        result = db.get_patents(page=page, page_size=page_size, search=search, keyword=keyword)
        return jsonify({'success': True, 'result': result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/patents/<int:patent_id>', methods=['GET'])
def api_patent_detail(patent_id):
    """获取专利详情API"""
    try:
        patent = db.get_patent_detail(patent_id)
        if patent:
            return jsonify({'success': True, 'result': patent})
        else:
            return jsonify({'success': False, 'error': '专利不存在'}), 404
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/alerts', methods=['GET'])
def api_alerts():
    """获取预警分析结果API"""
    try:
        page = int(request.args.get('page', 1))
        page_size = int(request.args.get('page_size', 20))
        risk_level = request.args.get('risk_level', None)
        search = request.args.get('search', None)
        keyword = request.args.get('keyword', None)
        
        result = db.get_risk_alerts(page=page, page_size=page_size, 
                                    risk_level=risk_level, search=search, keyword=keyword)
        return jsonify({'success': True, 'result': result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/statistics', methods=['GET'])
def api_statistics():
    """获取统计数据API"""
    try:
        keyword = request.args.get('keyword', None)
        stats = db.get_statistics(keyword=keyword)
        return jsonify({'success': True, 'result': stats})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/keywords', methods=['GET'])
def api_keywords():
    """获取已入库的关键词列表（去重）"""
    try:
        data = db.list_keyword_categories()
        return jsonify({'success': True, 'result': data})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/keywords/<path:keyword>', methods=['DELETE'])
def api_delete_keyword(keyword):
    """删除关键词及其所有相关数据"""
    try:
        # URL解码关键词
        from urllib.parse import unquote
        keyword = unquote(keyword)
        
        if not keyword:
            return jsonify({'success': False, 'error': '关键词不能为空'}), 400
        
        result = db.delete_keyword(keyword)
        return jsonify({'success': True, 'result': result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/tag-summary', methods=['GET'])
def api_tag_summary():
    """获取标签热门/冷门/共现汇总"""
    try:
        keyword = request.args.get('keyword', None)
        summary = db.get_tag_summary(keyword=keyword)
        return jsonify({'success': True, 'result': summary})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/tag-results', methods=['GET'])
def api_tag_results():
    """获取标签分析单篇结果"""
    try:
        page = int(request.args.get('page', 1))
        page_size = int(request.args.get('page_size', 20))
        search = request.args.get('search', None)
        keyword = request.args.get('keyword', None)
        result = db.get_tag_results(page=page, page_size=page_size, search=search, keyword=keyword)
        return jsonify({'success': True, 'result': result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/report', methods=['GET'])
def api_report():
    """关键词风险预警报告，仅返回 PDF（若缺 reportlab 则提示安装）"""
    try:
        keyword = request.args.get('keyword', None)
        report = db.build_report_data(keyword=keyword)

        pdf_buffer = _render_report_pdf(report)
        if pdf_buffer:
            filename = f"risk_report_{keyword or 'all'}.pdf"
            return send_file(
                pdf_buffer,
                mimetype='application/pdf',
                as_attachment=True,
                download_name=filename
            )
        return jsonify({
            'success': False,
            'error': '未安装 reportlab，无法生成 PDF，请先安装: pip install reportlab'
        }), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/run-analysis', methods=['POST'])
def api_run_analysis():
    """一键运行预警分析（analysis_runner.py）"""
    global _analysis_lock
    base_dir = os.path.abspath(os.path.dirname(__file__))
    analysis_script = os.path.join(base_dir, '预警分析', 'analysis_runner.py')

    def run_cmd(cmd):
        result = subprocess.run(cmd, cwd=base_dir, capture_output=True, text=True, encoding='utf-8', errors='ignore')
        return result.returncode == 0, result.stdout + result.stderr

    with _analysis_lock:
        ok, log_msg = run_cmd([sys.executable, analysis_script])
        if not ok:
            return jsonify({'success': False, 'error': f'预警分析执行失败: {log_msg[-800:]}' if log_msg else '预警分析执行失败', 'log': log_msg}), 500

    return jsonify({'success': True, 'message': '预警分析已完成', 'log': log_msg})


@app.route('/api/crawl', methods=['POST'])
def api_crawl():
    """
    触发关键词爬取 + 预警分析
    仅需传入 keyword，其他参数使用爬虫脚本默认值（账号/密码/页数/日期范围/线程）
    """
    global _crawl_proc, _last_crawl_log, _current_crawl_keyword
    data = request.get_json(force=True, silent=True) or {}
    keyword = (data.get('keyword') or '').strip()
    category = (data.get('category') or '').strip()
    if not keyword:
        return jsonify({'success': False, 'error': '关键词不能为空'}), 400
    
    _current_crawl_keyword = keyword  # 保存当前关键词

    base_dir = os.path.abspath(os.path.dirname(__file__))
    crawl_script = os.path.join(base_dir, '数据爬取与存储', '专利之星数据爬取.py')
    analysis_script = os.path.join(base_dir, '预警分析', 'analysis_runner.py')

    def run_cmd(cmd):
        result = subprocess.run(cmd, cwd=base_dir, capture_output=True, text=True, encoding='utf-8', errors='ignore')
        return result.returncode == 0, result.stdout + result.stderr

    # 确保列存在（防止旧库缺失 source_keyword）
    try:
        db.ensure_columns()
        if category:
            db.upsert_keyword_category(keyword, category)
    except Exception as e:
        return jsonify({'success': False, 'error': f'检查/更新数据表结构失败: {e}'}), 500

    # 自动生成该关键词的专属标签
    try:
        tags_result = db.generate_tags_from_keyword(keyword)
        if tags_result and tags_result.get('total_count', 0) > 0:
            # 标签已自动保存到 keyword_tags 表
            pass
    except Exception as e:
        # 标签生成失败不影响爬取，只记录日志
        print(f"生成关键词标签失败: {e}")

    with _crawl_lock:
        if _crawl_proc and _crawl_proc.poll() is None:
            return jsonify({'success': False, 'error': '已有爬取任务在进行，请先停止或等待完成'}), 409
        crawl_cmd = [sys.executable, crawl_script, '-s', keyword]
        try:
            _crawl_proc = subprocess.Popen(
                crawl_cmd,
                cwd=base_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8',
                errors='ignore'
            )
        except Exception as e:
            _crawl_proc = None
            return jsonify({'success': False, 'error': f'无法启动爬取进程: {e}'}), 500
        proc = _crawl_proc

    if not proc:
        return jsonify({'success': False, 'error': '爬取进程未启动'}), 500

    stdout, stderr = proc.communicate()
    log_crawl = (stdout or "") + (stderr or "")
    exit_code = proc.returncode if proc else -1
    with _crawl_lock:
        _crawl_proc = None
        _last_crawl_log = log_crawl
        _current_crawl_keyword = None  # 爬取完成后清空

    if exit_code != 0:
        return jsonify({'success': False, 'error': f'爬取失败: {log_crawl[-800:]}' if log_crawl else '爬取失败', 'log': log_crawl}), 500

    # 预警分析
    analysis_cmd = [sys.executable, analysis_script]
    ok_analysis, log_analysis = run_cmd(analysis_cmd)
    if not ok_analysis:
        return jsonify({'success': False, 'error': f'预警分析失败: {log_analysis[-800:]}' if log_analysis else '预警分析失败', 'log': log_analysis}), 500

    # 标签分析（使用该关键词的专属标签）
    tag_analysis_script = os.path.join(base_dir, '预警分析技术点2', 'tag_alert_analyzer.py')
    if os.path.exists(tag_analysis_script):
        tag_cmd = [sys.executable, tag_analysis_script, '--keyword', keyword]
        ok_tag, log_tag = run_cmd(tag_cmd)
        if not ok_tag:
            # 标签分析失败不影响整体流程，只记录
            print(f"标签分析失败: {log_tag[-500:]}")
    else:
        log_tag = "标签分析脚本不存在"

    return jsonify({'success': True, 'message': '爬取与分析完成', 'logs': {'crawl': log_crawl, 'analysis': log_analysis, 'tag_analysis': log_tag}})


@app.route('/api/stop-crawl', methods=['POST'])
def api_stop_crawl():
    """停止正在运行的爬取进程，生成标签，并对已爬数据执行分析"""
    global _crawl_proc, _last_crawl_log, _current_crawl_keyword
    base_dir = os.path.abspath(os.path.dirname(__file__))
    analysis_script = os.path.join(base_dir, '预警分析', 'analysis_runner.py')
    tag_analysis_script = os.path.join(base_dir, '预警分析技术点2', 'tag_alert_analyzer.py')

    def run_cmd(cmd):
        result = subprocess.run(cmd, cwd=base_dir, capture_output=True, text=True, encoding='utf-8', errors='ignore')
        return result.returncode == 0, result.stdout + result.stderr

    keyword = None
    with _crawl_lock:
        if not _crawl_proc or _crawl_proc.poll() is not None:
            return jsonify({'success': False, 'error': '当前没有正在运行的爬取任务'}), 400
        keyword = _current_crawl_keyword  # 获取当前关键词
        _crawl_proc.terminate()
        try:
            stdout, stderr = _crawl_proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            _crawl_proc.kill()
            stdout, stderr = _crawl_proc.communicate()
        log_crawl = (stdout or "") + (stderr or "")
        _last_crawl_log = log_crawl
        _crawl_proc = None
        _current_crawl_keyword = None  # 清空关键词

    # 对已爬数据执行预警分析
    ok_analysis, log_analysis = run_cmd([sys.executable, analysis_script])
    if not ok_analysis:
        return jsonify({'success': False, 'error': f'分析失败: {log_analysis[-800:]}' if log_analysis else '分析失败', 'log': log_analysis}), 500

    # 使用全局标签进行标签分析
    tag_analysis_log = ""
    if keyword and os.path.exists(tag_analysis_script):
        tag_cmd = [sys.executable, tag_analysis_script, '--keyword', keyword]
        ok_tag, log_tag = run_cmd(tag_cmd)
        tag_analysis_log = log_tag
        if not ok_tag:
            print(f"标签分析失败: {log_tag[-500:]}")

    return jsonify({
        'success': True,
        'message': '已停止爬取并完成分析',
        'logs': {
            'crawl': log_crawl,
            'analysis': log_analysis,
            'tag_analysis': tag_analysis_log
        }
    })


@app.route('/api/monthly-update', methods=['POST'])
def api_monthly_update():
    """
    定期更新：默认仅更新“公司”类别，可通过 payload 控制是否更新全部关键词。
    建议系统计划任务在每月1号调用 company_only=true 的方式。
    """
    global _update_cancel_flag, _update_running
    data = request.get_json(force=True, silent=True) or {}
    company_only = data.get("company_only", True)
    base_dir = os.path.abspath(os.path.dirname(__file__))
    crawl_script = os.path.join(base_dir, '数据爬取与存储', '专利之星数据爬取.py')
    analysis_script = os.path.join(base_dir, '预警分析', 'analysis_runner.py')
    tag_analysis_script = os.path.join(base_dir, '预警分析技术点2', 'tag_alert_analyzer.py')

    def run_cmd(cmd):
        result = subprocess.run(cmd, cwd=base_dir, capture_output=True, text=True, encoding='utf-8', errors='ignore')
        return result.returncode == 0, result.stdout + result.stderr

    # 若已有更新在跑，拒绝并提示
    if _update_running:
        return jsonify({'success': False, 'error': '已有更新任务在进行，请先停止或等待完成'}), 409

    # 获取所有公司类别关键词
    try:
        kc = db.list_keyword_categories()
        keywords = []
        for item in kc.get('keywords', []):
            name = item.get('keyword')
            if not name:
                continue
            category = (item.get('category') or '').strip()
            if company_only:
                if category == '公司':
                    keywords.append(name)
            else:
                keywords.append(name)
    except Exception as e:
        return jsonify({'success': False, 'error': f'获取关键词失败: {e}'}), 500

    if not keywords:
        return jsonify({'success': False, 'error': '没有符合条件的关键词'}), 400

    _update_cancel_flag = False
    _update_running = True

    results = []
    for kw in keywords:
        if _update_cancel_flag:
            item = {'keyword': kw, 'cancelled': True}
            results.append(item)
            break
        item = {'keyword': kw, 'crawl_ok': False, 'analysis_ok': False, 'tag_ok': False}
        try:
            # 确保结构
            db.ensure_columns()
            # 可选：为该关键词生成专属标签（当前为占位实现）
            try:
                db.generate_tags_from_keyword(kw)
            except Exception:
                pass

            # 爬取
            crawl_cmd = [sys.executable, crawl_script, '-s', kw]
            ok_crawl, log_crawl = run_cmd(crawl_cmd)
            item['crawl_log'] = (log_crawl or '')[-800:]
            item['crawl_ok'] = ok_crawl
            if not ok_crawl:
                results.append(item)
                continue

            # 预警分析
            ok_analysis, log_analysis = run_cmd([sys.executable, analysis_script])
            item['analysis_log'] = (log_analysis or '')[-800:]
            item['analysis_ok'] = ok_analysis
            if not ok_analysis:
                results.append(item)
                continue

            # 标签分析
            if os.path.exists(tag_analysis_script):
                tag_cmd = [sys.executable, tag_analysis_script, '--keyword', kw]
                ok_tag, log_tag = run_cmd(tag_cmd)
                item['tag_log'] = (log_tag or '')[-500:]
                item['tag_ok'] = ok_tag
            results.append(item)
        except Exception as e:
            item['error'] = str(e)
            results.append(item)

    _update_running = False
    success_any = any(r.get('crawl_ok') and r.get('analysis_ok') for r in results)
    cancelled = _update_cancel_flag
    return jsonify({'success': success_any, 'results': results, 'cancelled': cancelled})


@app.route('/api/dashboard', methods=['GET'])
def api_dashboard():
    """
    数据可视化大屏专用API
    返回大屏所需的全部统计数据
    """
    try:
        keyword = request.args.get('keyword')
        data = db.get_dashboard_data(keyword)
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    # 部分环境的旧版 watchdog 不支持 EVENT_TYPE_CLOSED，关闭自动重载以避免导入错误
    app.run(debug=True, host='0.0.0.0', port=5000, use_reloader=False)

