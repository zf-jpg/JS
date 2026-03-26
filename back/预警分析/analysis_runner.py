import json
import math
import re
from datetime import datetime
from collections import Counter
from io import BytesIO
from typing import Optional, List
import pyodbc

try:
    import fitz  # PyMuPDF, 与技术点2保持一致，提取质量更好
except ImportError:
    fitz = None

try:
    import PyPDF2
except ImportError:
    PyPDF2 = None


# ============================================================
# 数据库管理
# ============================================================

class DatabaseManager:
    """负责连接 SQL Server、建表、读写预警结果"""

    def __init__(self, log):
        self.log = log
        self.driver = self._select_driver()
        self.conn_str = (
            'DRIVER={' + self.driver + '};'
            'SERVER=localhost;'
            'DATABASE=zlzx;'
            'Trusted_Connection=yes;'
        )
        self.ensure_tables()

    def _select_driver(self):
        preferred = ['ODBC Driver 18 for SQL Server', 'ODBC Driver 17 for SQL Server']
        available = pyodbc.drivers()
        for driver in preferred:
            if driver in available:
                return driver
        if available:
            return available[-1]
        raise RuntimeError('未检测到可用的 SQL Server ODBC 驱动')

    def connect(self):
        return pyodbc.connect(self.conn_str, autocommit=True)

    def ensure_tables(self):
        stmts = [
            (
                "IF OBJECT_ID('risk_alerts', 'U') IS NULL "
                "BEGIN "
                "CREATE TABLE risk_alerts ("
                "id INT IDENTITY(1,1) PRIMARY KEY, "
                "patent_id INT NOT NULL, "
                "ane NVARCHAR(100), "
                "title NVARCHAR(MAX), "
                "applicants NVARCHAR(MAX), "
                "inventors NVARCHAR(MAX), "
                "publication_date NVARCHAR(50), "
                "risk_score INT, "
                "risk_level NVARCHAR(20), "
                "risk_tags NVARCHAR(200), "
                "risk_reason NVARCHAR(MAX), "
                "risk_confidence NVARCHAR(20), "
                "risk_delta INT, "
                "source_keyword NVARCHAR(200), "
                "created_at DATETIME DEFAULT GETDATE(), "
                "updated_at DATETIME DEFAULT GETDATE()"
                "); "
                "CREATE UNIQUE INDEX IX_risk_alerts_patent_id ON risk_alerts(patent_id); "
                "END"
            ),
            "IF COL_LENGTH('risk_alerts','risk_confidence') IS NULL ALTER TABLE risk_alerts ADD risk_confidence NVARCHAR(20) NULL",
            "IF COL_LENGTH('risk_alerts','risk_delta') IS NULL ALTER TABLE risk_alerts ADD risk_delta INT NULL",
            "IF COL_LENGTH('risk_alerts','source_keyword') IS NULL ALTER TABLE risk_alerts ADD source_keyword NVARCHAR(200) NULL",
            (
                "IF OBJECT_ID('patent_texts','U') IS NULL "
                "BEGIN "
                "CREATE TABLE patent_texts("
                "patent_id INT PRIMARY KEY, "
                "full_text NVARCHAR(MAX), "
                "extracted_at DATETIME DEFAULT GETDATE()"
                "); "
                "END"
            ),
        ]
        with self.connect() as conn:
            cursor = conn.cursor()
            for stmt in stmts:
                cursor.execute(stmt)

    def fetch_patents(self, limit=None):
        # main_class 用于 IPC 重叠度计算（新增因子 F8）
        sql = (
            'SELECT id, ane, title, applicant, patentee, inventors, abstract,'
            ' publication_date, raw_json, source_keyword, main_class'
            ' FROM patents ORDER BY id DESC'
        )
        if limit:
            sql += ' OFFSET 0 ROWS FETCH NEXT ' + str(int(limit)) + ' ROWS ONLY'
        with self.connect() as conn:
            cursor = conn.cursor()
            rows = cursor.execute(sql).fetchall()
            cols = [c[0] for c in cursor.description]
        return [dict(zip(cols, row)) for row in rows]

    def fetch_pdf_bytes(self, patent_id):
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT content FROM pdfs WHERE patent_id=? ORDER BY file_index',
                (patent_id,),
            )
            return [r[0] for r in cursor.fetchall()]

    def fetch_cached_text(self, patent_id):
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT full_text FROM patent_texts WHERE patent_id=?', (patent_id,))
            row = cursor.fetchone()
            return row[0] if row else None

    def fetch_risk_distribution(self):
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT risk_score FROM risk_alerts WHERE risk_score IS NOT NULL')
            return [r[0] for r in cursor.fetchall()]

    def fetch_applicant_history(self):
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT applicants, risk_level FROM risk_alerts'
                " WHERE applicants IS NOT NULL AND applicants <> ''"
            )
            rows = cursor.fetchall()
        score_map = {}
        for applicants, level in rows:
            parts = []
            for sep in [';', '\uff1b', ',', '\uff0c', '\u3001']:
                if sep in applicants:
                    parts = [p.strip() for p in applicants.split(sep) if p.strip()]
                    break
            if not parts:
                parts = [applicants.strip()]
            level_w = {'\u9ad8': 3.0, '\u4e2d': 1.5, '\u4f4e': 0.5}.get(level, 1.0)
            for name in parts:
                score_map[name] = score_map.get(name, 0) + level_w
        return score_map

    def fetch_applicant_activity(self):
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT applicant, COUNT(*) AS total,'
                ' SUM(CASE WHEN publication_date IS NOT NULL'
                '          AND TRY_CAST(publication_date AS BIGINT) BETWEEN 19000101 AND 29991231'
                '          AND DATEDIFF(day, TRY_CONVERT(date, LEFT(publication_date,8)), GETDATE()) <= 365'
                '     THEN 1 ELSE 0 END) AS recent'
                ' FROM patents'
                " WHERE applicant IS NOT NULL AND applicant <> ''"
                ' GROUP BY applicant'
            )
            rows = cursor.fetchall()
        activity = {}
        for applicant, total, recent in rows:
            parts = []
            for sep in [';', '\uff1b', ',', '\uff0c', '\u3001']:
                if sep in applicant:
                    parts = [p.strip() for p in applicant.split(sep) if p.strip()]
                    break
            if not parts:
                parts = [applicant.strip()]
            for name in parts:
                a = activity.get(name, {'total': 0, 'recent': 0})
                a['total'] += total or 0
                a['recent'] += recent or 0
                activity[name] = a
        return activity

    def fetch_ipc_profile(self):
        # F8: 聚合所有高风险专利的 IPC 大组（前4字符）分布
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT p.main_class'
                ' FROM patents p'
                ' INNER JOIN risk_alerts r ON r.patent_id = p.id'
                " WHERE r.risk_level = '\u9ad8'"
                " AND p.main_class IS NOT NULL AND p.main_class <> ''"
            )
            rows = cursor.fetchall()
        counter = Counter()
        for (mc,) in rows:
            for part in re.split(r'[;\uff1b,\uff0c ]', mc or ''):
                part = part.strip()
                if len(part) >= 4:
                    counter[part[:4]] += 1
        return counter

    def fetch_high_risk_tag_vectors(self):
        # F9: 获取所有高风险专利的三级标签向量，用于余弦相似度
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT tar.themes_json'
                ' FROM tag_analysis_results tar'
                ' INNER JOIN risk_alerts ra ON ra.patent_id = tar.patent_id'
                " WHERE ra.risk_level = '\u9ad8' AND tar.themes_json IS NOT NULL"
            )
            rows = cursor.fetchall()
        vectors = []
        for (themes_json,) in rows:
            try:
                themes = json.loads(themes_json or '{}')
            except Exception:
                continue
            vec = Counter()
            for tdata in themes.values():
                for lbl, cnt in (tdata.get('third_level_counts') or {}).items():
                    try:
                        vec[lbl] += int(cnt)
                    except Exception:
                        pass
            if vec:
                vectors.append(vec)
        return vectors

    def fetch_tag_analysis(self, patent_id):
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT themes_json, total_matches FROM tag_analysis_results WHERE patent_id=?',
                (patent_id,)
            )
            row = cursor.fetchone()
            if row:
                themes_json, total_matches = row
                try:
                    themes = json.loads(themes_json) if themes_json else {}
                    return {'themes': themes, 'total_matches': total_matches or 0}
                except Exception:
                    return None
        return None

    def fetch_tag_summary(self):
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT TOP 1 summary_json FROM tag_hot_cold_summary ORDER BY generated_at DESC'
            )
            row = cursor.fetchone()
            if row:
                try:
                    return json.loads(row[0])
                except Exception:
                    return None
        return None

    def cache_text(self, patent_id, text):
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'MERGE patent_texts AS t'
                ' USING (SELECT ? AS pid, ? AS txt) AS s ON t.patent_id = s.pid'
                ' WHEN MATCHED THEN UPDATE SET full_text = s.txt, extracted_at = GETDATE()'
                ' WHEN NOT MATCHED THEN INSERT (patent_id, full_text) VALUES (s.pid, s.txt);',
                (patent_id, text),
            )

    def upsert_alert(self, alert):
        with self.connect() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT id FROM risk_alerts WHERE patent_id=?', (alert['patent_id'],))
            existing = cursor.fetchone()
            if existing:
                cursor.execute('SELECT risk_score FROM risk_alerts WHERE patent_id=?', (alert['patent_id'],))
                prev = cursor.fetchone()
                prev_score = prev[0] if prev else None
                delta = None
                if prev_score is not None and alert.get('risk_score') is not None:
                    delta = alert['risk_score'] - prev_score
                cursor.execute(
                    'UPDATE risk_alerts'
                    ' SET ane=?, title=?, applicants=?, inventors=?, publication_date=?,'
                    '     risk_score=?, risk_level=?, risk_tags=?, risk_reason=?,'
                    '     risk_confidence=?, risk_delta=?, source_keyword=?, updated_at=GETDATE()'
                    ' WHERE patent_id=?',
                    (
                        alert['ane'], alert['title'], alert['applicants'],
                        alert['inventors'], alert['publication_date'],
                        alert['risk_score'], alert['risk_level'], alert['risk_tags'],
                        alert['risk_reason'], alert.get('risk_confidence'), delta,
                        alert.get('source_keyword'), alert['patent_id'],
                    ),
                )
                return existing[0]
            else:
                cursor.execute(
                    'INSERT INTO risk_alerts'
                    ' (patent_id, ane, title, applicants, inventors, publication_date,'
                    '  risk_score, risk_level, risk_tags, risk_reason,'
                    '  risk_confidence, risk_delta, source_keyword)'
                    ' OUTPUT INSERTED.id'
                    ' VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (
                        alert['patent_id'], alert['ane'], alert['title'],
                        alert['applicants'], alert['inventors'], alert['publication_date'],
                        alert['risk_score'], alert['risk_level'], alert['risk_tags'],
                        alert['risk_reason'], alert.get('risk_confidence'), None,
                        alert.get('source_keyword'),
                    ),
                )
                new_row = cursor.fetchone()
                if not new_row or new_row[0] is None:
                    return 0
                return int(new_row[0])


# ============================================================
# PDF 文本提取（优先 PyMuPDF，退降 PyPDF2）
# ============================================================

def extract_pdf_text(pdf_bytes_list, max_chars=80000):
    """
    从 PDF 二进制列表中提取全文。
    优先使用 PyMuPDF(fitz)，退降 PyPDF2。
    额外单独提取权利要求书段落，供 F10 权利要求深度分析。
    返回: (full_text: str, claims_text: str)
    """
    texts = []
    for blob in pdf_bytes_list:
        if fitz:
            try:
                with fitz.open(stream=BytesIO(blob), filetype='pdf') as doc:
                    for page in doc:
                        texts.append(page.get_text() or '')
                continue
            except Exception:
                pass
        if PyPDF2:
            try:
                reader = PyPDF2.PdfReader(BytesIO(blob))
                for page in reader.pages[:15]:
                    texts.append(page.extract_text() or '')
            except Exception:
                pass

    full_text = '\n'.join(texts)
    if len(full_text) > max_chars:
        full_text = full_text[:max_chars]

    # 提取权利要求书段落（F10 深度分析用）
    claims_text = ''
    m = re.search('权利要求', full_text)
    if m:
        start = m.start()
        end_m = re.search('说明书', full_text[start + 10:])
        if end_m:
            end = start + 10 + end_m.start()
        else:
            end = start + 8000
        if end - start > 8000:
            end = start + 8000
        claims_text = full_text[start:end]

    return full_text, claims_text


# ============================================================
# 辅助：余弦相似度（Counter 向量）
# ============================================================

def _cosine(a, b):
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


# ============================================================
# 风险评估（增强版 10 因子）
# ============================================================

class RiskAnalyzer:
    # 10 个因子说明：
    # F1  风险关键词命中          权重 0.50  max_raw=35
    # F2  语义短语匹配            权重 0.50  max_raw=26
    # F3  时效性衰减              权重 0.55  max_raw=15
    # F4  竞争领域词              权重 0.55  max_raw=15
    # F5  权利要求结构信号         权重 0.55  max_raw=15
    # F6  申请人历史画像+活跃度    权重 0.52  max_raw=25  (+申请人倍数)
    # F7  标签热冷度              权重 0.50  max_raw=30
    # F8  IPC 分类重叠度【新增】   权重固定   max_contrib=12
    # F9  标签余弦相似度【新增】   权重固定   max_contrib=15
    # F10 权利要求深度分析【新增】 权重固定   max_contrib=10
    # 理论总分上限约 100，实际因子不会同时满分，校准后映射到 1-99

    def __init__(self, keywords=None, competitor_keywords=None, risk_phrases=None,
                 tag_summary=None, ipc_profile=None, high_risk_vectors=None):
        self.keywords = keywords or [
            '侵权', '纠纷', '诉讼', '仲裁', '禁令', '无效', '侵害',
            '专利权', '商业秘密', '警告', '警示', '风险', '权利要求',
            '侵占', '许可', '强制许可', '专利池', '标准必要专利',
        ]
        self.competitor_keywords = competitor_keywords or [
            '新能源', '电池', '储能', '车载', '锂', '镁', '材料',
            '半导体', '芯片', '光伏', '燃料电池', '固态电池',
        ]
        self.risk_phrases = risk_phrases or [
            '侵权风险', '权利要求覆盖', '专利无效', '临界侵害', '绕开设计',
            '技术规避', '商业秘密泄露', '禁令', '诉讼风险', '案号',
            '法院', '法庭', '仲裁委员会', '法院判决', '权利要求1',
            '独立权项', '保护范围', '等同侵权', '字面侵权',
        ]
        # 标签汇总（来自技术点2）
        self.tag_summary = tag_summary or {}
        self.hot_labels = set()
        self.cold_labels = set()
        if tag_summary:
            self.hot_labels = {item.get('label', '') for item in tag_summary.get('hot', [])}
            self.cold_labels = {item.get('label', '') for item in tag_summary.get('cold', [])}

        # F8: 高风险 IPC 大组分布 Counter
        self.ipc_profile = ipc_profile or Counter()
        self._ipc_total = sum(self.ipc_profile.values()) or 1

        # F9: 高风险专利标签向量列表（每项为 Counter）
        self.high_risk_vectors = high_risk_vectors or []

    # ----------------------------------------------------------
    # 内部辅助
    # ----------------------------------------------------------

    def _parse_pd(self, pd_str):
        if not pd_str:
            return None
        s = str(pd_str)
        if len(s) == 8 and s.isdigit():
            try:
                return datetime.strptime(s, '%Y%m%d')
            except ValueError:
                return None
        return None

    def _recency_score(self, pd):
        if not pd:
            return 0
        days = (datetime.now() - pd).days
        score = 12 * math.exp(-days / 365)
        return max(0, min(15, score))

    def _semantic_score(self, text):
        score = 0
        hits = []
        legal_hits = 0
        for phrase in self.risk_phrases:
            if phrase in text:
                hits.append(phrase)
                score += 8
                if phrase in ('案号', '法院', '法庭', '仲裁委员会', '法院判决'):
                    legal_hits += 1
            else:
                inter = len(set(phrase) & set(text[:2000]))
                ratio = inter / max(1, len(set(phrase)))
                if ratio > 0.6:
                    hits.append('~' + phrase)
                    score += 4
        if legal_hits:
            score += 6
        return min(26, score), hits

    def _applicant_score(self, applicants, applicant_hist, applicant_activity, text):
        names = []
        if applicants:
            for sep in [';', '\uff1b', ',', '\uff0c', '\u3001']:
                if sep in applicants:
                    names = [p.strip() for p in applicants.split(sep) if p.strip()]
                    break
        if not names and applicants:
            names = [applicants.strip()]
        score = 0
        hits = []
        mult = 1.0
        for n in names:
            base = applicant_hist.get(n, 0)
            if base > 0:
                hits.append(n)
                score += min(15, base * 2)
            act = applicant_activity.get(n, {})
            recent = act.get('recent', 0)
            total = act.get('total', 0)
            if recent >= 10:
                score += 8
                hits.append(n + '(近365天' + str(recent) + ')')
            elif recent >= 5:
                score += 4
                hits.append(n + '(近365天' + str(recent) + ')')
            if total >= 50:
                mult = max(mult, 1.25)
            elif total >= 20:
                mult = max(mult, 1.1)
            if recent >= 3:
                score += 2
        for n in names:
            if n and n in text:
                score += 1
        return min(25, score), hits, mult

    def _competitor_score(self, text):
        hits = [kw for kw in self.competitor_keywords if kw in text]
        return min(15, len(hits) * 3), hits

    def _claim_score(self, text):
        score = 0
        hits = []
        if '权利要求' in text:
            hits.append('权利要求')
            score += 6
        if '独立权利要求' in text:
            hits.append('独立权利要求')
            score += 4
        if '保护范围' in text:
            hits.append('保护范围')
            score += 3
        return min(15, score), hits

    def _tag_score(self, tag_analysis):
        if not tag_analysis or not tag_analysis.get('themes'):
            return 0, []
        themes = tag_analysis.get('themes', {})
        total_matches = tag_analysis.get('total_matches', 0)
        hits = []
        score = 0
        if total_matches >= 200:
            score += 10; hits.append('标签命中极高(' + str(total_matches) + ')')
        elif total_matches >= 100:
            score += 8;  hits.append('标签命中高(' + str(total_matches) + ')')
        elif total_matches >= 50:
            score += 5;  hits.append('标签命中中等(' + str(total_matches) + ')')
        elif total_matches >= 20:
            score += 3;  hits.append('标签命中(' + str(total_matches) + ')')
        elif total_matches > 0:
            score += 1
        hot_count = 0
        cold_count = 0
        all_labels = set()
        for tdata in themes.values():
            third_counts = tdata.get('third_level_counts', {})
            for lbl, cnt in third_counts.items():
                if cnt > 0:
                    all_labels.add(lbl)
                    if lbl in self.hot_labels:
                        hot_count += cnt
                    elif lbl in self.cold_labels:
                        cold_count += cnt
        if hot_count >= 50:
            score += 12; hits.append('热门标签极高(' + str(hot_count) + '次)')
        elif hot_count >= 30:
            score += 9;  hits.append('热门标签高(' + str(hot_count) + '次)')
        elif hot_count >= 15:
            score += 6;  hits.append('热门标签中等(' + str(hot_count) + '次)')
        elif hot_count >= 5:
            score += 3;  hits.append('热门标签(' + str(hot_count) + '次)')
        elif hot_count > 0:
            score += 1
        if cold_count >= 20:
            score -= 3; hits.append('冷门标签(' + str(cold_count) + '次，机会领域)')
        elif cold_count >= 10:
            score -= 2
        elif cold_count >= 5:
            score -= 1
        theme_count = len(themes)
        if theme_count >= 3:
            score += 5; hits.append('多主题(' + str(theme_count) + '主题)')
        elif theme_count >= 2:
            score += 3
        if len(all_labels) >= 20:
            score += 4; hits.append('标签共现强(' + str(len(all_labels)) + '个)')
        elif len(all_labels) >= 10:
            score += 2
        elif len(all_labels) >= 5:
            score += 1
        return min(30, max(0, score)), hits

    def _ipc_overlap_score(self, main_class):
        # F8: IPC 大组与高风险专利库的重叠度
        if not main_class or not self.ipc_profile:
            return 0, []
        my_groups = set()
        for part in re.split(r'[;\uff1b,\uff0c ]', main_class or ''):
            part = part.strip()
            if len(part) >= 4:
                my_groups.add(part[:4])
        if not my_groups:
            return 0, []
        overlap_weight = 0.0
        hits = []
        for grp in my_groups:
            if grp in self.ipc_profile:
                w = self.ipc_profile[grp] / self._ipc_total
                overlap_weight += w
                hits.append(grp)
        # 线性映射到 0-12
        score = int(min(12, overlap_weight * 120))
        return score, hits

    def _cosine_similarity_score(self, tag_analysis):
        # F9: 当前专利标签向量与高风险专利群均值向量的余弦相似度
        if not self.high_risk_vectors or not tag_analysis:
            return 0, []
        themes = tag_analysis.get('themes', {})
        cur_vec = Counter()
        for tdata in themes.values():
            for lbl, cnt in (tdata.get('third_level_counts') or {}).items():
                try:
                    cur_vec[lbl] += int(cnt)
                except Exception:
                    pass
        if not cur_vec:
            return 0, []
        # 与每个高风险向量求余弦，取最大值（最近邻策略）
        max_sim = max(_cosine(cur_vec, v) for v in self.high_risk_vectors)
        # 映射到 0-15
        score = int(min(15, max_sim * 20))
        hits = ['与高风险专利标签相似度' + '{:.2f}'.format(max_sim)] if max_sim > 0.1 else []
        return score, hits

    def _claims_depth_score(self, claims_text):
        # F10: 权利要求书深度分析（基于 PDF 提取的专用段落）
        if not claims_text:
            return 0, []
        score = 0
        hits = []
        # 独立权项数量
        independent = len(re.findall(r'(?:^|\n)\s*1[.、．]', claims_text))
        claim_count = len(re.findall(r'(?:^|\n)\s*\d+[.、．]', claims_text))
        if claim_count >= 20:
            score += 4; hits.append('权利要求数' + str(claim_count))
        elif claim_count >= 10:
            score += 3; hits.append('权利要求数' + str(claim_count))
        elif claim_count >= 5:
            score += 2
        # 功能性限定词（means-plus-function，易被宽泛解释）
        functional = len(re.findall(
            r'(?:用于|一种|包括|包含|具有|设置有|配置为|能够)[^。]{2,20}(?:的装置|的系统|的方法|的步骤)',
            claims_text
        ))
        if functional >= 5:
            score += 3; hits.append('功能性限定' + str(functional) + '处')
        elif functional >= 2:
            score += 2
        # 宽泛上位概念词
        broad_terms = ['至少', '一个或多个', '任意', '多种',
                       '等效', '类似', '相关', '及其组合']
        broad_count = sum(1 for t in broad_terms if t in claims_text)
        if broad_count >= 4:
            score += 3; hits.append('宽泛上位概念词' + str(broad_count) + '个')
        elif broad_count >= 2:
            score += 1
        return min(10, score), hits

    # ----------------------------------------------------------
    # 主评估入口
    # ----------------------------------------------------------

    def evaluate(self, patent, applicant_hist, applicant_activity, tag_analysis=None):
        text_parts = [
            patent.get('title', '') or '',
            patent.get('abstract', '') or '',
            patent.get('applicant', '') or '',
            patent.get('patentee', '') or '',
            patent.get('inventors', '') or '',
            patent.get('pdf_text', '') or '',
        ]
        raw_json = patent.get('raw_json')
        if raw_json:
            try:
                raw_obj = json.loads(raw_json)
                text_parts.append(json.dumps(raw_obj, ensure_ascii=False))
            except Exception:
                text_parts.append(str(raw_json))
        full_text = ' '.join(text_parts)

        # F1: 关键词
        hits_kw = [kw for kw in self.keywords if kw in full_text]
        f1 = min(35, len(hits_kw) * 6)

        # F2: 语义短语
        f2, sem_hits = self._semantic_score(full_text)

        # F3: 时效
        pd = self._parse_pd(patent.get('publication_date'))
        f3 = self._recency_score(pd)

        # F4: 竞争领域词
        f4, competitor_hits = self._competitor_score(full_text)

        # F5: 权利要求结构信号（粗）
        f5, claim_hits = self._claim_score(full_text)

        # F6: 申请人历史+活跃度
        f6, applicant_hits, applicant_mult = self._applicant_score(
            patent.get('applicant', ''), applicant_hist, applicant_activity, full_text
        )

        # F7: 标签热冷度
        f7, tag_hits = self._tag_score(tag_analysis) if tag_analysis else (0, [])

        # F8: IPC 重叠度
        f8, ipc_hits = self._ipc_overlap_score(patent.get('main_class', ''))

        # F9: 标签余弦相似度
        f9, cosine_hits = self._cosine_similarity_score(tag_analysis)

        # F10: 权利要求深度（PDF 权利要求书段落）
        claims_text = patent.get('claims_text', '')
        f10, claims_depth_hits = self._claims_depth_score(claims_text)

        # ---- 加权求和 ----
        # 原有 7 因子加权（与之前版本一致，保持分布连续性）
        weighted = (
            f1 * 0.50   # 17.5 max
            + f2 * 0.50   # 13.0 max
            + f3 * 0.55   #  8.3 max
            + f4 * 0.55   #  8.3 max
            + f5 * 0.55   #  8.3 max
            + f6 * 0.52   # 13.0 max
            + f7 * 0.50   # 15.0 max
        ) * applicant_mult

        # 新增 3 因子直接加（上限已在各方法内控制）
        bonus = f8 + f9 + f10  # max = 12+15+10 = 37

        # 合并校准：总分理论最大 ≈ 83*1.25 + 37 = 141 -> 压缩到 1-99
        raw_total = weighted + bonus
        # Sigmoid 压缩：映射 [0, 140] -> [1, 99]
        # 用简单线性分段：<50 -> 低区，50-100 -> 中区，>100 -> 高区
        if raw_total <= 0:
            total = 1
        elif raw_total >= 110:
            total = 95 + min(4, int((raw_total - 110) * 0.1))
        else:
            total = int(raw_total * 0.88)
        total = max(1, min(99, total))

        # 动态阈值
        high_thr = getattr(self, 'dyn_high', 65)
        mid_thr = getattr(self, 'dyn_mid', 38)
        if high_thr >= 99 or high_thr <= mid_thr:
            high_thr, mid_thr = 65, 38

        if total >= high_thr:
            risk_level = '高'
        elif total >= mid_thr:
            risk_level = '中'
        else:
            risk_level = '低'

        # 原因汇总
        reasons = []
        if hits_kw:
            reasons.append('风险关键词: ' + ', '.join(hits_kw[:5]))
        if sem_hits:
            reasons.append('语义短语: ' + ', '.join(sem_hits[:5]))
        if competitor_hits:
            reasons.append('竞争领域词: ' + ', '.join(competitor_hits[:5]))
        if claim_hits:
            reasons.append('权利要求信号: ' + ', '.join(claim_hits[:5]))
        if applicant_hits:
            reasons.append('申请人历史风险: ' + ', '.join(applicant_hits[:3]))
        if f3 > 0:
            reasons.append('时效加分: ' + '{:.1f}'.format(f3))
        if tag_hits:
            reasons.append('标签分析: ' + '; '.join(tag_hits[:5]))
        if ipc_hits:
            reasons.append('IPC重叠高风险技术领域: ' + ', '.join(ipc_hits[:4]))
        if cosine_hits:
            reasons.append('; '.join(cosine_hits))
        if claims_depth_hits:
            reasons.append('权利要求深度: ' + '; '.join(claims_depth_hits[:3]))
        if not reasons:
            reasons.append('未命中明显风险特征，基础风险较低')

        risk_tags = ','.join(
            (hits_kw + competitor_hits + claim_hits + sem_hits
             + applicant_hits + tag_hits[:3] + ipc_hits[:2] + cosine_hits)[:12]
        )[:200]

        # 置信度：强信号数量
        strong = (
            (len(hits_kw) >= 2)
            + (len(sem_hits) >= 1)
            + (len(claim_hits) >= 1)
            + (len(applicant_hits) >= 1)
            + (len(tag_hits) >= 2)
            + (f8 >= 6)          # IPC 高度重叠
            + (f9 >= 8)          # 余弦相似度高
            + (len(claims_depth_hits) >= 1)
        )
        if total >= 80 and strong >= 4:
            risk_confidence = '高'
        elif total >= 55 and strong >= 2:
            risk_confidence = '中'
        else:
            risk_confidence = '低'

        return {
            'risk_score': int(total),
            'risk_level': risk_level,
            'risk_tags': risk_tags,
            'risk_reason': '；'.join(reasons),
            'risk_confidence': risk_confidence,
        }


# ============================================================
# 日志 & 主流程
# ============================================================

def log(msg):
    now = datetime.now().strftime('%H:%M:%S')
    print('[' + now + '] ' + msg)


def run(limit=None):
    db = DatabaseManager(log)
    applicant_hist = db.fetch_applicant_history()
    applicant_activity = db.fetch_applicant_activity()

    # 加载标签汇总（来自技术点2）
    tag_summary = db.fetch_tag_summary()
    if tag_summary:
        log('已加载标签汇总：热门' + str(len(tag_summary.get('hot', []))) +
            ' 冷门' + str(len(tag_summary.get('cold', []))))
    else:
        log('未找到标签汇总数据，将仅使用其他风险因子')

    # F8: IPC 高风险分布
    ipc_profile = db.fetch_ipc_profile()
    log('已加载高风险 IPC 大组分布：' + str(len(ipc_profile)) + ' 个大组')

    # F9: 高风险标签向量
    high_risk_vectors = db.fetch_high_risk_tag_vectors()
    log('已加载高风险专利标签向量：' + str(len(high_risk_vectors)) + ' 条')

    analyzer = RiskAnalyzer(
        tag_summary=tag_summary,
        ipc_profile=ipc_profile,
        high_risk_vectors=high_risk_vectors,
    )

    # 动态阈值
    history_scores = db.fetch_risk_distribution()
    dyn_high, dyn_mid = 65, 38
    if history_scores and len(history_scores) > 10:
        import statistics
        sorted_scores = sorted(history_scores)
        n = len(sorted_scores)
        high_idx = int(n * 0.65)
        dyn_high = min(80, max(55, sorted_scores[high_idx] if high_idx < n else dyn_high))
        mid_idx = int(n * 0.35)
        dyn_mid = min(55, max(30, sorted_scores[mid_idx] if mid_idx < n else dyn_mid))
        if dyn_high <= dyn_mid + 5:
            dyn_high = dyn_mid + 10
    analyzer.dyn_high = dyn_high
    analyzer.dyn_mid = dyn_mid
    log('风险等级阈值：高>=' + str(dyn_high) + ' 中>=' + str(dyn_mid))

    patents = db.fetch_patents(limit=limit)
    log('共获取 ' + str(len(patents)) + ' 条专利记录，开始分析...')

    saved = 0
    for p in patents:
        # 获取 / 缓存 PDF 全文
        pdf_text = db.fetch_cached_text(p['id'])
        claims_text = ''
        if not pdf_text:
            pdf_bytes_list = db.fetch_pdf_bytes(p['id'])
            if pdf_bytes_list:
                pdf_text, claims_text = extract_pdf_text(pdf_bytes_list)
                if pdf_text:
                    db.cache_text(p['id'], pdf_text)
        else:
            # 从缓存全文中重新提取权利要求段落
            m = re.search('权利要求', pdf_text)
            if m:
                start = m.start()
                end_m = re.search('说明书', pdf_text[start + 10:])
                end = start + 10 + end_m.start() if end_m else start + 8000
                if end - start > 8000:
                    end = start + 8000
                claims_text = pdf_text[start:end]

        p['pdf_text'] = pdf_text or ''
        p['claims_text'] = claims_text or ''

        # 获取标签分析结果（来自技术点2）
        tag_analysis = db.fetch_tag_analysis(p['id'])

        result = analyzer.evaluate(p, applicant_hist, applicant_activity, tag_analysis=tag_analysis)
        alert = {
            'patent_id': p.get('id'),
            'ane': p.get('ane'),
            'title': p.get('title'),
            'applicants': p.get('applicant'),
            'inventors': p.get('inventors'),
            'publication_date': p.get('publication_date'),
            'risk_score': result['risk_score'],
            'risk_level': result['risk_level'],
            'risk_tags': result['risk_tags'],
            'risk_reason': result['risk_reason'],
            'risk_confidence': result.get('risk_confidence'),
            'source_keyword': p.get('source_keyword'),
        }
        db.upsert_alert(alert)
        saved += 1

    log('分析完成，写入/更新预警记录 ' + str(saved) + ' 条')
    log('结果保存在数据库表 risk_alerts 中。')


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='专利风险预警分析（增强版10因子）')
    parser.add_argument('--limit', type=int, help='限制处理专利数量')
    args = parser.parse_args()
    run(limit=args.limit)
