import json
import os
import re
from collections import defaultdict, Counter
from datetime import datetime
from io import BytesIO
from typing import Dict, List, Tuple, Any, Iterable, Optional

import pyodbc
try:
    from pyecharts import options as opts
    from pyecharts.charts import Graph
    _pyecharts_available = True
except Exception:
    _pyecharts_available = False

try:
    import fitz  # PyMuPDF
except Exception:  # pragma: no cover - 运行环境缺少依赖时给出友好提示
    fitz = None


# ----------------------------
# 日志工具
# ----------------------------
def log(msg: str) -> None:
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] {msg}")


# ----------------------------
# 数据库访问
# ----------------------------
class DatabaseClient:
    """负责从 SQL Server 读取 PDF 二进制、缓存提取文本。"""

    def __init__(self, server: str = "localhost", database: str = "zlzx"):
        self.server = server
        self.database = database
        self.driver = self._select_driver()
        self.conn_str = (
            f"DRIVER={{{self.driver}}};SERVER={self.server};DATABASE={self.database};Trusted_Connection=yes;"
        )
        self.ensure_tables()

    def _select_driver(self) -> str:
        preferred = ["ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"]
        available = pyodbc.drivers()
        for d in preferred:
            if d in available:
                return d
        if not available:
            raise RuntimeError("未检测到可用的 SQL Server ODBC 驱动")
        return available[-1]

    def connect(self):
        return pyodbc.connect(self.conn_str, autocommit=True)

    def ensure_tables(self) -> None:
        """创建用于标签分析的结果表、汇总表（若不存在）。"""
        with self.connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                IF OBJECT_ID('tag_analysis_results', 'U') IS NULL
                BEGIN
                    CREATE TABLE tag_analysis_results(
                        patent_id INT PRIMARY KEY,
                        ane NVARCHAR(200),
                        title NVARCHAR(500),
                        language NVARCHAR(50),
                        total_matches INT,
                        themes_json NVARCHAR(MAX),
                        updated_at DATETIME DEFAULT GETDATE()
                    );
                END;

                IF OBJECT_ID('tag_hot_cold_summary', 'U') IS NULL
                BEGIN
                    CREATE TABLE tag_hot_cold_summary(
                        id INT IDENTITY(1,1) PRIMARY KEY,
                        generated_at DATETIME,
                        summary_json NVARCHAR(MAX)
                    );
                END;
                """
            )

    def fetch_patents(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = """
            SELECT id, ane, title, applicant, patentee, inventors, abstract, publication_date, source_keyword
            FROM patents
            ORDER BY id DESC
        """
        if limit:
            sql += f" OFFSET 0 ROWS FETCH NEXT {int(limit)} ROWS ONLY"
        with self.connect() as conn:
            cur = conn.cursor()
            rows = cur.execute(sql).fetchall()
            cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in rows]

    def fetch_pdf_bytes(self, patent_id: int) -> List[bytes]:
        with self.connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT content FROM pdfs WHERE patent_id=? ORDER BY file_index", (patent_id,))
            return [r[0] for r in cur.fetchall()]

    def fetch_cached_text(self, patent_id: int) -> Optional[str]:
        with self.connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT full_text FROM patent_texts WHERE patent_id=?", (patent_id,))
            row = cur.fetchone()
            return row[0] if row else None

    def cache_text(self, patent_id: int, text: str) -> None:
        with self.connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                MERGE patent_texts AS t
                USING (SELECT ? AS pid, ? AS txt) AS s
                ON t.patent_id = s.pid
                WHEN MATCHED THEN UPDATE SET full_text = s.txt, extracted_at = GETDATE()
                WHEN NOT MATCHED THEN INSERT (patent_id, full_text) VALUES (s.pid, s.txt);
                """,
                (patent_id, text),
            )

    def upsert_tag_result(self, patent: Dict[str, Any], analysis: Dict[str, Any]) -> None:
        """写入单个专利的标签分析结果（themes_json 存储 JSON 串），包含关键词。"""
        themes_json = json.dumps(analysis.get("themes", {}), ensure_ascii=False)
        with self.connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                MERGE tag_analysis_results AS t
                USING (SELECT ? AS pid) AS s
                ON t.patent_id = s.pid
                WHEN MATCHED THEN
                    UPDATE SET ane=?, title=?, language=?, total_matches=?, themes_json=?, source_keyword=?, updated_at=GETDATE()
                WHEN NOT MATCHED THEN
                    INSERT (patent_id, ane, title, language, total_matches, themes_json, source_keyword)
                    VALUES (?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    patent["id"],
                    patent.get("ane"),
                    patent.get("title"),
                    analysis.get("language"),
                    analysis.get("total_matches"),
                    themes_json,
                    patent.get("source_keyword"),
                    patent["id"],
                    patent.get("ane"),
                    patent.get("title"),
                    analysis.get("language"),
                    analysis.get("total_matches"),
                    themes_json,
                    patent.get("source_keyword"),
                ),
            )

    def insert_summary(self, summary: Dict[str, Any], generated_at: str) -> None:
        """存储热门/冷门/共现汇总，保留历史。"""
        summary_json = json.dumps(summary, ensure_ascii=False)
        with self.connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO tag_hot_cold_summary (generated_at, summary_json)
                VALUES (?, ?);
                """,
                (generated_at, summary_json),
            )


# ----------------------------
# 标签加载
# ----------------------------
class LabelRepository:
    """加载三级标签、关键字及正则，加速匹配。支持关键词专属标签。"""

    def __init__(self, data_root: str, keyword: Optional[str] = None, db_client=None):
        self.data_root = data_root
        self.labels_content_path = os.path.join(data_root, "标签内容")
        self.keyword = keyword
        self.db_client = db_client
        self.all_labels = self._load_all_labels()

    def _load_all_labels(self) -> Dict[str, Dict[str, Any]]:
        labels_data: Dict[str, Dict[str, Any]] = {}
        
        # 加载基础标签数据
        if not os.path.exists(self.labels_content_path):
            raise FileNotFoundError(f"未找到标签数据目录: {self.labels_content_path}")

        for theme in os.listdir(self.labels_content_path):
            theme_path = os.path.join(self.labels_content_path, theme)
            if not os.path.isdir(theme_path):
                continue

            hierarchy_file = None
            keywords_file = None
            for f in os.listdir(theme_path):
                if any(k in f for k in ["标签树", "标签层级"]):
                    hierarchy_file = os.path.join(theme_path, f)
                elif any(k in f for k in ["标签", "关键词"]) and "树" not in f:
                    keywords_file = os.path.join(theme_path, f)

            if not hierarchy_file or not keywords_file:
                continue

            with open(hierarchy_file, "r", encoding="utf-8") as hf:
                hierarchy_raw = json.load(hf)
            hierarchy = self._normalize_hierarchy(hierarchy_raw)

            with open(keywords_file, "r", encoding="utf-8") as kf:
                keywords = json.load(kf)

            mapping = self._build_mapping(hierarchy, theme)
            compiled = self._compile_keywords(keywords)
            labels_data[theme] = {
                "hierarchy": hierarchy,
                "keywords": keywords,
                "compiled_keywords": compiled,
                "mapping": mapping,
            }
        return labels_data

    def _normalize_hierarchy(self, raw: Dict[str, Any]) -> List[Dict[str, Any]]:
        # 尝试常见顶级键，否则返回首个 list
        for v in raw.values():
            if isinstance(v, list):
                return v
        return []

    def _build_mapping(self, hierarchy: List[Dict[str, Any]], theme: str) -> Dict[str, Dict[str, str]]:
        mapping: Dict[str, Dict[str, str]] = {}
        for lvl1 in hierarchy:
            lv1_name = lvl1.get("name", "")
            for lvl2 in lvl1.get("children", []):
                lv2_name = lvl2.get("name", "")
                for lvl3 in lvl2.get("children", []):
                    lv3_name = lvl3.get("name", "")
                    if lv3_name:
                        mapping[lv3_name] = {"first_level": lv1_name, "second_level": lv2_name, "theme": theme}
        return mapping

    def _compile_keywords(self, keywords: Dict[str, Dict[str, List[str]]]) -> Dict[str, Dict[str, re.Pattern]]:
        compiled: Dict[str, Dict[str, re.Pattern]] = {}
        for third, langs in keywords.items():
            compiled_langs: Dict[str, re.Pattern] = {}
            for lang, words in langs.items():
                if not isinstance(words, list) or not words:
                    continue
                pattern = "|".join(re.escape(w.lower()) for w in words if w)
                if pattern:
                    compiled_langs[lang] = re.compile(r"\b(?:%s)\b" % pattern)
            compiled[third] = compiled_langs
        return compiled


# ----------------------------
# PDF 标签分析
# ----------------------------
class PDFTagAnalyzer:
    """将 PDF 文本映射到标签出现频次，并生成共现度。"""

    chinese_pattern = re.compile(r"[\u4e00-\u9fff]")
    english_pattern = re.compile(r"[a-zA-Z]")
    punctuation_pattern = re.compile(r"[^\w\u4e00-\u9fff\s]")
    space_pattern = re.compile(r"\s+")
    word_pattern = re.compile(r"\b\w+\b")

    def __init__(self, label_repo: LabelRepository):
        self.label_repo = label_repo

    def detect_language(self, text: str) -> str:
        c = len(self.chinese_pattern.findall(text))
        e = len(self.english_pattern.findall(text))
        if c > e * 2:
            return "中文文献"
        if e > c * 2:
            return "外文文献"
        return "中文文献" if c >= e else "外文文献"

    def extract_pdf_text(self, pdf_blobs: Iterable[bytes]) -> str:
        if not fitz:
            raise RuntimeError("缺少 PyMuPDF，请先安装: pip install pymupdf")
        texts: List[str] = []
        for blob in pdf_blobs:
            try:
                with fitz.open(stream=BytesIO(blob), filetype="pdf") as doc:
                    for page in doc:
                        texts.append(page.get_text() or "")
            except Exception:
                continue
        return "\n".join(texts)

    def preprocess(self, text: str) -> Tuple[str, List[str]]:
        lowered = text.lower()
        lowered = self.punctuation_pattern.sub(" ", lowered)
        lowered = self.space_pattern.sub(" ", lowered).strip()
        words = [w for w in self.word_pattern.findall(lowered) if len(w) >= 2]
        return lowered, words

    def _contains_chinese(self, s: str) -> bool:
        return bool(self.chinese_pattern.search(s))

    def _count_keyword_occurrences(self, text: str, keyword: str) -> int:
        """针对中英文分别计数：中文用简单子串计数，英文用单词边界。"""
        if not keyword:
            return 0
        if self._contains_chinese(keyword):
            return text.count(keyword)
        pattern = re.compile(r"\b%s\b" % re.escape(keyword))
        return len(pattern.findall(text))

    def analyze_text(self, text: str) -> Dict[str, Any]:
        processed, words = self.preprocess(text)
        language = self.detect_language(text)
        themes_result: Dict[str, Any] = {}

        word_counter = Counter(words)

        for theme, data in self.label_repo.all_labels.items():
            third_counts: Dict[str, int] = {}
            second_counts: Dict[str, int] = defaultdict(int)
            first_counts: Dict[str, int] = defaultdict(int)
            for third, lang_words in data["keywords"].items():
                mapping = data["mapping"].get(third)
                if not mapping:
                    continue
                all_keywords = []
                for kw_list in lang_words.values():
                    if isinstance(kw_list, list):
                        all_keywords.extend([kw.lower() for kw in kw_list if kw])
                if not all_keywords:
                    continue

                # 计数：中文用子串，英文用单词边界
                total = 0
                for kw in all_keywords:
                    total += self._count_keyword_occurrences(processed, kw)
                if total <= 0:
                    continue

                third_counts[third] = total
                second_counts[mapping["second_level"]] += total
                first_counts[mapping["first_level"]] += total

            if third_counts:
                themes_result[theme] = {
                    "third_level_counts": third_counts,
                    "second_level_counts": dict(second_counts),
                    "first_level_counts": dict(first_counts),
                    "total_matches": sum(third_counts.values()),
                }

        return {
            "language": language,
            "themes": themes_result,
            "total_matches": sum(t["total_matches"] for t in themes_result.values()),
        }


# ----------------------------
# 热门 / 冷门标签计算
# ----------------------------
def compute_hot_cold(results: List[Dict[str, Any]], hot_percentile: float = 0.75, cold_percentile: float = 0.25):
    total_third: Counter[str] = Counter()
    co_occurrence: Counter[Tuple[str, str]] = Counter()

    for r in results:
        for theme in r.get("themes", {}).values():
            third_labels = [k for k, v in theme.get("third_level_counts", {}).items() if v > 0]
            for lbl, cnt in theme.get("third_level_counts", {}).items():
                total_third[lbl] += cnt
            # 共现
            for i, a in enumerate(third_labels):
                for b in third_labels[i + 1 :]:
                    key = tuple(sorted((a, b)))
                    co_occurrence[key] += 1

    if not total_third:
        return {"hot": [], "cold": [], "co_occurrence_top": []}

    counts = total_third.values()
    sorted_counts = sorted(counts)
    idx_hot = max(int(len(sorted_counts) * hot_percentile) - 1, 0)
    idx_cold = max(int(len(sorted_counts) * cold_percentile) - 1, 0)
    hot_thr = sorted_counts[idx_hot]
    cold_thr = sorted_counts[idx_cold]

    hot = [{"label": lbl, "count": cnt} for lbl, cnt in total_third.items() if cnt >= hot_thr]
    cold = [{"label": lbl, "count": cnt} for lbl, cnt in total_third.items() if 0 < cnt <= cold_thr]

    co_top = sorted(co_occurrence.items(), key=lambda x: x[1], reverse=True)[:30]
    co_top_fmt = [{"pair": list(pair), "count": cnt} for pair, cnt in co_top]

    hot.sort(key=lambda x: x["count"], reverse=True)
    cold.sort(key=lambda x: x["count"])

    return {"hot": hot, "cold": cold, "co_occurrence_top": co_top_fmt}


# ----------------------------
# 主流程
# ----------------------------
def run(
    data_root: Optional[str] = None,
    output_dir: Optional[str] = None,
    limit: Optional[int] = None,
    write_files: bool = False,
    keyword: Optional[str] = None,
    generate_graphs: bool = True,
):
    """
    读取数据库中所有 PDF，提取标签热度/冷门度和共现关系。
    结果默认仅写入数据库；若 write_files=True，则额外输出到 output_dir。
    
    Args:
        keyword: 如果提供关键词，则只使用该关键词的专属标签进行分析
    """
    # 解析数据根目录，默认使用当前目录下的标签数据
    if data_root is None:
        script_dir = os.path.abspath(os.path.dirname(__file__))
        data_root = os.path.abspath(os.path.join(script_dir, "标签数据"))

    if write_files:
        out_dir = output_dir or "output"
        os.makedirs(out_dir, exist_ok=True)

    db = DatabaseClient()
    
    # 如果有关键词，只分析该关键词的专利
    if keyword:
        patents = [p for p in db.fetch_patents(limit=limit) if p.get("source_keyword") == keyword]
        log(f"获取关键词 '{keyword}' 的专利记录 {len(patents)} 条")
    else:
        patents = db.fetch_patents(limit=limit)
        log(f"获取专利记录 {len(patents)} 条")

    # 加载全局标签
    label_repo = LabelRepository(data_root=data_root, keyword=None, db_client=None)
    analyzer = PDFTagAnalyzer(label_repo)

    analyzed_results = []
    # 聚合主题级的三/four级标签计数与共现，用于生成知识图谱
    theme_stats: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"third_counts": Counter(), "co_counts": Counter()})
    for p in patents:
        cached = db.fetch_cached_text(p["id"])
        text = cached or ""
        if not text:
            pdf_blobs = db.fetch_pdf_bytes(p["id"])
            if pdf_blobs:
                text = analyzer.extract_pdf_text(pdf_blobs)
                # 文本过长截断，避免缓存过大
                if len(text) > 150_000:
                    text = text[:150_000]
                if text:
                    db.cache_text(p["id"], text)

        if not text:
            analyzed_results.append({"patent_id": p["id"], "error": "未获取到 PDF 文本"})
            continue

        analysis = analyzer.analyze_text(text)
        analyzed_results.append(
            {
                "patent_id": p["id"],
                "ane": p.get("ane"),
                "title": p.get("title"),
                "language": analysis["language"],
                "themes": analysis["themes"],
                "total_matches": analysis["total_matches"],
            }
        )
        # 写入数据库
        db.upsert_tag_result(p, analysis)

        # 主题级聚合：third level 计数与共现
        for theme_name, tdata in analysis.get("themes", {}).items():
            third_counts = tdata.get("third_level_counts", {}) or {}
            third_labels = [k for k, v in third_counts.items() if v > 0]
            theme_stats[theme_name]["third_counts"].update(third_counts)
            for i, a in enumerate(third_labels):
                for b in third_labels[i + 1 :]:
                    key = tuple(sorted((a, b)))
                    theme_stats[theme_name]["co_counts"][key] += 1

    hot_cold = compute_hot_cold(analyzed_results)

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    output_analysis = {
        "generated_at": generated_at,
        "patent_count": len(patents),
        "results": analyzed_results,
        "summary": hot_cold,
    }

    # 汇总存入数据库
    db.insert_summary(hot_cold, generated_at)

    # 可选写文件
    if write_files:
        out_dir = output_dir or "output"
        with open(os.path.join(out_dir, "tag_analysis_results.json"), "w", encoding="utf-8") as f:
            json.dump(output_analysis, f, ensure_ascii=False, indent=2)
        with open(os.path.join(out_dir, "tag_hot_cold_summary.json"), "w", encoding="utf-8") as f:
            json.dump(hot_cold, f, ensure_ascii=False, indent=2)
        log(f"分析完成，结果已输出到 {out_dir}")
    else:
        log("分析完成，结果已写入数据库（未输出文件）")

    # 生成共现知识图谱（HTML），存放于 预警分析技术点2/knowledge_graphs
    if generate_graphs:
        generate_knowledge_graphs(theme_stats, base_dir=os.path.abspath(os.path.dirname(__file__)))


def _scale_size(val: int, min_size: int = 8, max_size: int = 38) -> int:
    if val <= 0:
        return min_size
    return max(min_size, min(max_size, int(min_size + (max_size - min_size) * (val / (val + 20)))))


def generate_knowledge_graphs(theme_stats: Dict[str, Dict[str, Any]], base_dir: str):
    """
    根据聚合的主题级共现数据生成 pyecharts 知识图谱 HTML。
    输出目录：<base_dir>/knowledge_graphs
    """
    if not _pyecharts_available:
        log("跳过知识图谱生成：未安装 pyecharts (pip install pyecharts)")
        return

    out_dir = os.path.join(base_dir, "knowledge_graphs")
    os.makedirs(out_dir, exist_ok=True)

    for theme, data in theme_stats.items():
        counts: Counter = data.get("third_counts", Counter())
        co_counts: Counter = data.get("co_counts", Counter())
        if not counts:
            continue

        nodes = []
        for name, cnt in counts.items():
            nodes.append(
                {
                    "name": name,
                    "symbolSize": _scale_size(cnt),
                    "value": cnt,
                }
            )

        links = []
        for (a, b), cnt in co_counts.items():
            links.append(
                {
                    "source": a,
                    "target": b,
                    "value": cnt,
                    "lineStyle": {"width": max(1, min(6, cnt))}
                }
            )

        g = (
            Graph(init_opts=opts.InitOpts(width="1200px", height="900px", bg_color="#ffffff"))
            .add(
                "",
                nodes,
                links,
                layout="force",
                repulsion=200,
                edge_length=[80, 200],
                linestyle_opts=opts.LineStyleOpts(curve=0.1, opacity=0.7),
                label_opts=opts.LabelOpts(position="right", font_size=10),
            )
            .set_global_opts(
                title_opts=opts.TitleOpts(title=f"{theme} 标签共现网络", pos_left="center", pos_top="5px"),
                tooltip_opts=opts.TooltipOpts(trigger="item")
            )
        )
        out_file = os.path.join(out_dir, f"{theme}_标签共现网络.html")
        g.render(out_file)
        log(f"知识图谱已生成: {out_file}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='标签分析工具')
    parser.add_argument('--keyword', type=str, help='关键词，如果提供则只使用该关键词的专属标签')
    parser.add_argument('--limit', type=int, help='限制处理的专利数量')
    parser.add_argument('--no-graph', action='store_true', help='不生成知识图谱 HTML')
    args = parser.parse_args()
    
    run(keyword=args.keyword, limit=args.limit, generate_graphs=not args.no_graph)

