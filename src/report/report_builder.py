import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager, rcParams

from src.core.paper import Paper


def _configure_cjk_font() -> None:
    candidates = [
        "Microsoft YaHei",
        "SimHei",
        "Noto Sans CJK SC",
        "Source Han Sans SC",
        "WenQuanYi Zen Hei",
    ]
    installed = {f.name for f in font_manager.fontManager.ttflist}
    for name in candidates:
        if name in installed:
            rcParams["font.sans-serif"] = [name]
            rcParams["axes.unicode_minus"] = False
            return


_configure_cjk_font()


class ReportBuilder:
    def __init__(self, settings_cfg: dict, run_dir: Path):
        self.settings = settings_cfg.get("general", {})
        self.analysis = settings_cfg.get("analysis", {})
        self.focus_keywords = [
            str(item).strip()
            for item in (self.analysis.get("focus_keywords") or [])
            if str(item).strip()
        ]
        self.run_dir = run_dir
        self.assets_dir = self.run_dir / "assets"
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        self.last_comparison: Dict = {}
        self.last_trends: Dict = {}

    def build(
        self,
        papers: List[Paper],
        crawl_stats: Dict,
        store_stats: Dict,
        llm_stats: Dict,
        media_stats: Optional[Dict] = None,
    ) -> Path:
        report_path = self.run_dir / "report.md"
        ranked = self._rank_papers(papers)
        comparison = self._build_comparison(ranked)
        trends = self._build_trend_data(ranked)
        charts = self._build_charts(ranked, trends)
        self.last_comparison = comparison
        if trends:
            trends["chart_refs"] = [
                {"title": title, "path": rel_path}
                for title, rel_path in charts
                if "趋势" in title or "变化图" in title
            ]
        self.last_trends = trends

        journal_groups = self._group_by_journal(ranked)
        focus_related = [paper for paper in ranked if paper.related_to_focus]
        visual_ranked = [paper for paper in ranked if paper.figures or paper.generated_diagram_path]
        shown_detail_uids = set()

        lines: List[str] = [
            "# AutoLitManager 自动文献报告",
            "",
            "## 本次概览",
            f"- 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"- 论文总数: {len(ranked)}",
            f"- 期刊数量: {len(journal_groups)}",
            f"- 数据库新增/更新/总数: {store_stats.get('inserted', 0)}/{store_stats.get('updated', 0)}/{store_stats.get('total', 0)}",
            f"- 图片采集: {self._format_media_stats(media_stats)}",
            f"- ChatGPT 成功批次/总批次: {llm_stats.get('batch_success', 0)}/{llm_stats.get('batch_total', 0)}",
            f"- ChatGPT 更新论文数: {llm_stats.get('papers_updated', 0)}",
            f"- 含图论文数: {sum(1 for paper in ranked if paper.figures)}",
            f"- 已生成流程图论文数: {sum(1 for paper in ranked if paper.generated_diagram_path)}",
            "",
            "## 期刊分布",
        ]

        for journal, items in journal_groups.items():
            lines.append(f"- {journal}: {len(items)} 篇")
        lines.append("")

        if comparison:
            lines.extend(self._comparison_section(comparison))

        lines.extend(
            [
                "## 抓取信息",
                "```json",
                self._pretty_dict(crawl_stats),
                "```",
                "",
                "## 图表概览",
            ]
        )

        for title, rel_path in charts:
            lines.extend([f"### {title}", f"![{title}]({rel_path})", ""])

        if trends:
            lines.extend(self._trend_section(trends))

        lines.extend(
            [
                "## 总体前沿进展",
                *self._frontier_lines(ranked),
                "",
                "## 重点关注论文",
            ]
        )

        if focus_related:
            for paper in self._select_new_detail_papers(focus_related, shown_detail_uids, limit=12):
                lines.extend(self._paper_block(paper, compact=False, show_images=True))
        else:
            lines.extend(["- 本轮没有命中你关注关键词的论文。", ""])

        lines.extend(["## 图像与模型摘要论文", ""])
        if visual_ranked:
            visual_detail_papers = self._select_new_detail_papers(
                visual_ranked,
                shown_detail_uids,
                limit=10,
            )
            if visual_detail_papers:
                for paper in visual_detail_papers:
                    lines.extend(self._paper_block(paper, compact=False, show_images=True))
            else:
                lines.extend(["- 带图或带流程图的论文已在前文展示，这里不再重复展开。", ""])
        else:
            lines.extend(["- 本轮没有可展示的论文图像。", ""])

        if self.focus_keywords:
            lines.extend(self._focus_topic_section(ranked, shown_detail_uids))

        lines.extend(["## 按期刊精读", ""])
        for journal, items in journal_groups.items():
            lines.extend(self._journal_section(journal, items, comparison, shown_detail_uids))

        lines.extend(["## 全量论文索引", ""])
        for paper in ranked:
            lines.extend(self._paper_block(paper, compact=True, show_images=False))

        report_path.write_text("\n".join(lines), encoding="utf-8-sig")
        return report_path

    def _rank_papers(self, papers: List[Paper]) -> List[Paper]:
        return sorted(
            papers,
            key=lambda item: (
                1 if item.related_to_focus else 0,
                1 if item.figures else 0,
                item.value_score or 0,
                item.publish_date or "",
            ),
            reverse=True,
        )

    @staticmethod
    def _group_by_journal(papers: List[Paper]) -> Dict[str, List[Paper]]:
        groups: Dict[str, List[Paper]] = defaultdict(list)
        for paper in papers:
            groups[paper.journal or "Unknown Journal"].append(paper)
        return dict(groups)

    def _build_charts(self, papers: List[Paper], trends: Optional[Dict] = None) -> List[tuple[str, str]]:
        outputs = []

        journal_counter = Counter(paper.journal for paper in papers)
        journal_path = self.assets_dir / "journals.png"
        self._bar_plot("Journal Distribution", journal_counter, journal_path)
        outputs.append(("期刊论文分布", f"assets/{journal_path.name}"))

        category_counter = Counter()
        for paper in papers:
            for category in paper.categories:
                category_counter[category] += 1
        category_path = self.assets_dir / "categories.png"
        self._bar_plot("Category Distribution", category_counter, category_path)
        outputs.append(("分类分布", f"assets/{category_path.name}"))

        keyword_counter = Counter()
        for paper in papers:
            for keyword in paper.matched_keywords:
                keyword_counter[keyword] += 1
        keyword_path = self.assets_dir / "focus_keywords.png"
        self._bar_plot("Focus Keyword Distribution", keyword_counter, keyword_path)
        outputs.append(("关注关键词分布", f"assets/{keyword_path.name}"))

        history = (trends or {}).get("history") or []
        if len(history) >= 2:
            run_labels = [item.get("label", item.get("run_id", "N/A")) for item in history]
            paper_series = {"论文数": [int(item.get("paper_count", 0)) for item in history]}
            paper_trend_path = self.assets_dir / "run_paper_trend.png"
            self._line_plot("Comparable Run Paper Trend", run_labels, paper_series, paper_trend_path)
            outputs.append(("多次运行论文数趋势", f"assets/{paper_trend_path.name}"))

            focus_series_names = (trends or {}).get("focus_series_names") or []
            if focus_series_names:
                focus_series = {
                    name: [
                        int((item.get("focus_counts") or {}).get(name, 0))
                        for item in history
                    ]
                    for name in focus_series_names
                }
                focus_trend_path = self.assets_dir / "focus_keyword_trend.png"
                self._line_plot(
                    "Focus Keyword Trend",
                    run_labels,
                    focus_series,
                    focus_trend_path,
                )
                outputs.append(("关注方向热点变化图", f"assets/{focus_trend_path.name}"))

            method_series_names = (trends or {}).get("method_series_names") or []
            if method_series_names:
                method_series = {
                    name: [
                        int((item.get("method_counts") or {}).get(name, 0))
                        for item in history
                    ]
                    for name in method_series_names
                }
                method_trend_path = self.assets_dir / "method_trend.png"
                self._line_plot(
                    "Method Trend",
                    run_labels,
                    method_series,
                    method_trend_path,
                )
                outputs.append(("高频方法变化图", f"assets/{method_trend_path.name}"))

        return outputs

    def _bar_plot(self, title: str, series: Counter, out_path: Path) -> None:
        items = series.most_common(12) or [("N/A", 0)]
        labels, values = zip(*items)
        plt.figure(figsize=(10, 4))
        plt.bar(labels, values)
        plt.title(title)
        plt.xticks(rotation=20, ha="right")
        plt.tight_layout()
        plt.savefig(out_path, dpi=150)
        plt.close()

    def _line_plot(
        self,
        title: str,
        labels: List[str],
        series_map: Dict[str, List[int]],
        out_path: Path,
    ) -> None:
        if not labels or not series_map:
            return
        plt.figure(figsize=(10, 4.8))
        for name, values in series_map.items():
            plt.plot(labels, values, marker="o", linewidth=2, label=name)
        plt.title(title)
        plt.xticks(rotation=20, ha="right")
        plt.ylabel("Count")
        plt.grid(axis="y", linestyle="--", alpha=0.25)
        if len(series_map) > 1:
            plt.legend(fontsize=8, ncol=2)
        plt.tight_layout()
        plt.savefig(out_path, dpi=150)
        plt.close()

    def _build_comparison(self, current_papers: List[Paper]) -> Dict:
        previous_run = self._find_previous_comparable_run(current_papers)
        if not previous_run:
            return {}

        previous_run_dir = previous_run["run_dir"]
        previous_papers = previous_run["papers"]
        prev_uids = {item["uid"] for item in previous_papers if item.get("uid")}
        curr_uids = {paper.uid() for paper in current_papers}

        new_papers = [paper for paper in current_papers if paper.uid() not in prev_uids]
        removed_count = len([uid for uid in prev_uids if uid not in curr_uids])

        prev_frontier = set()
        for item in previous_papers:
            for point in item.get("gemini_frontier_points") or []:
                point = str(point).strip()
                if point:
                    prev_frontier.add(point)

        current_frontier = []
        for paper in current_papers:
            current_frontier.extend(
                point.strip()
                for point in (paper.gemini_frontier_points or [])
                if point and point.strip()
            )

        new_frontier_counter = Counter(point for point in current_frontier if point not in prev_frontier)
        new_by_journal = Counter(paper.journal for paper in new_papers)

        generated_at = ""
        manifest_path = previous_run_dir / "manifest.json"
        if manifest_path.exists():
            try:
                generated_at = json.loads(manifest_path.read_text(encoding="utf-8")).get("generated_at", "")
            except json.JSONDecodeError:
                generated_at = ""

        return {
            "previous_run_dir": str(previous_run_dir),
            "previous_run_id": previous_run_dir.name,
            "previous_generated_at": generated_at,
            "previous_paper_count": len(previous_papers),
            "overlap_count": previous_run["overlap_count"],
            "new_papers_count": len(new_papers),
            "removed_count": removed_count,
            "new_by_journal": dict(new_by_journal),
            "new_papers": [
                {
                    "title": paper.title_cn or paper.title,
                    "journal": paper.journal,
                    "doi": paper.doi or "",
                }
                for paper in new_papers[:20]
            ],
            "new_frontier_points": [text for text, _ in new_frontier_counter.most_common(12)],
        }

    def _find_previous_comparable_run(self, current_papers: List[Paper]) -> Optional[Dict]:
        runs_root = self.run_dir.parent
        if not runs_root.exists():
            return None

        current_uids = {paper.uid() for paper in current_papers}
        current_journals = {paper.journal for paper in current_papers}
        candidates = []

        for candidate in runs_root.iterdir():
            if not candidate.is_dir() or candidate.resolve() == self.run_dir.resolve():
                continue
            papers_path = candidate / "papers.json"
            if not papers_path.exists():
                continue
            try:
                payload = json.loads(papers_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            candidate_uids = {item.get("uid") for item in payload if item.get("uid")}
            candidate_journals = {item.get("journal") for item in payload if item.get("journal")}
            overlap_count = len(current_uids & candidate_uids)
            journal_overlap = len(current_journals & candidate_journals)
            candidates.append(
                {
                    "run_dir": candidate,
                    "papers": payload,
                    "overlap_count": overlap_count,
                    "journal_overlap": journal_overlap,
                    "mtime": candidate.stat().st_mtime,
                    "paper_count": len(payload),
                }
            )

        if not candidates:
            return None

        candidates.sort(
            key=lambda item: (
                item["overlap_count"],
                item["journal_overlap"],
                item["paper_count"],
                item["mtime"],
            ),
            reverse=True,
        )
        return candidates[0]

    def _comparison_section(self, comparison: Dict) -> List[str]:
        lines = [
            "## 与上次可比运行相比",
            f"- 对比运行: {comparison.get('previous_run_id', 'N/A')}",
            f"- 对比运行时间: {comparison.get('previous_generated_at') or 'N/A'}",
            f"- 上次运行论文数: {comparison.get('previous_paper_count', 0)}",
            f"- 与本次重合论文数: {comparison.get('overlap_count', 0)}",
            f"- 本次新增论文数: {comparison.get('new_papers_count', 0)}",
            f"- 本次未再出现论文数: {comparison.get('removed_count', 0)}",
            "",
        ]

        new_by_journal = comparison.get("new_by_journal") or {}
        if new_by_journal:
            lines.append("### 新增论文按期刊分布")
            for journal, count in sorted(new_by_journal.items(), key=lambda item: item[1], reverse=True):
                lines.append(f"- {journal}: {count} 篇")
            lines.append("")

        new_frontier_points = comparison.get("new_frontier_points") or []
        if new_frontier_points:
            lines.append("### 本次新增前沿点")
            for text in new_frontier_points[:10]:
                lines.append(f"- {text}")
            lines.append("")

        new_papers = comparison.get("new_papers") or []
        if new_papers:
            lines.append("### 本次新增论文")
            for item in new_papers[:12]:
                doi = item.get("doi") or "N/A"
                lines.append(f"- {item.get('title', 'N/A')} | {item.get('journal', 'N/A')} | {doi}")
            lines.append("")

        return lines

    def _build_trend_data(self, current_papers: List[Paper]) -> Dict:
        runs_root = self.run_dir.parent
        if not runs_root.exists():
            return {}

        current_journals = {paper.journal for paper in current_papers if paper.journal}
        history = [self._summarize_run_papers(self.run_dir.name, current_papers, self.run_dir)]

        for candidate in runs_root.iterdir():
            if not candidate.is_dir() or candidate.resolve() == self.run_dir.resolve():
                continue
            papers_path = candidate / "papers.json"
            if not papers_path.exists():
                continue
            try:
                payload = json.loads(papers_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            candidate_journals = {
                item.get("journal")
                for item in payload
                if isinstance(item, dict) and item.get("journal")
            }
            if current_journals and not (candidate_journals & current_journals):
                continue
            history.append(self._summarize_run_payload(candidate.name, payload, candidate))

        if not history:
            return {}

        history.sort(key=lambda item: item.get("mtime", 0.0))
        max_runs = int(self.analysis.get("trend_max_runs", 6) or 6)
        if max_runs > 0 and len(history) > max_runs:
            history = history[-max_runs:]

        if len(history) < 2:
            return {
                "history": history,
                "focus_series_names": [],
                "method_series_names": [],
                "rising_focus": [],
                "persistent_methods": [],
                "average_paper_count": float(history[-1].get("paper_count", 0)) if history else 0.0,
                "paper_delta_vs_previous_avg": 0.0,
                "journal_scope": sorted(current_journals),
                "chart_refs": [],
            }

        current_entry = history[-1]
        previous_entries = history[:-1]
        previous_avg = (
            sum(int(item.get("paper_count", 0)) for item in previous_entries) / len(previous_entries)
            if previous_entries
            else float(current_entry.get("paper_count", 0))
        )
        delta = float(current_entry.get("paper_count", 0)) - previous_avg

        focus_names = self._trend_focus_names(history)
        method_names, persistent_methods = self._trend_method_names(history)
        rising_focus = self._rising_focus_topics(history, focus_names)

        return {
            "history": history,
            "focus_series_names": focus_names,
            "method_series_names": method_names,
            "rising_focus": rising_focus,
            "persistent_methods": persistent_methods,
            "average_paper_count": round(previous_avg, 2),
            "paper_delta_vs_previous_avg": round(delta, 2),
            "journal_scope": sorted(current_journals),
            "chart_refs": [],
        }

    def _summarize_run_papers(self, run_id: str, papers: List[Paper], run_dir: Path) -> Dict:
        generated_at = ""
        manifest_path = run_dir / "manifest.json"
        if manifest_path.exists():
            try:
                generated_at = json.loads(manifest_path.read_text(encoding="utf-8")).get("generated_at", "")
            except json.JSONDecodeError:
                generated_at = ""

        journal_counts = Counter((paper.journal or "Unknown") for paper in papers)
        focus_counts = Counter()
        method_counts = Counter()
        frontier_counts = Counter()
        for paper in papers:
            for keyword in paper.matched_keywords:
                if keyword:
                    focus_counts[str(keyword).strip()] += 1
            for method in paper.methods:
                if method:
                    method_counts[str(method).strip()] += 1
            for point in paper.gemini_frontier_points:
                if point:
                    frontier_counts[str(point).strip()] += 1

        return {
            "run_id": run_id,
            "label": run_id[4:] if len(run_id) >= 8 else run_id,
            "generated_at": generated_at,
            "mtime": run_dir.stat().st_mtime,
            "paper_count": len(papers),
            "journal_counts": dict(journal_counts),
            "focus_counts": dict(focus_counts),
            "method_counts": dict(method_counts),
            "frontier_counts": dict(frontier_counts),
        }

    def _summarize_run_payload(self, run_id: str, payload: List[dict], run_dir: Path) -> Dict:
        papers = [Paper.from_dict(item) for item in payload if isinstance(item, dict)]
        return self._summarize_run_papers(run_id, papers, run_dir)

    def _trend_focus_names(self, history: List[Dict]) -> List[str]:
        candidates = [name for name in self.focus_keywords if name]
        if candidates:
            names = [
                name
                for name in candidates
                if any(int((item.get("focus_counts") or {}).get(name, 0)) > 0 for item in history)
            ]
            if names:
                return names[:5]

        totals = Counter()
        for item in history:
            totals.update(item.get("focus_counts") or {})
        return [name for name, _ in totals.most_common(5)]

    def _trend_method_names(self, history: List[Dict]) -> tuple[List[str], List[Dict]]:
        totals = Counter()
        coverage = Counter()
        for item in history:
            method_counts = item.get("method_counts") or {}
            totals.update(method_counts)
            for name, count in method_counts.items():
                if int(count) > 0:
                    coverage[name] += 1

        names = [name for name, _ in totals.most_common(5)]
        persistent = []
        for name, total in totals.most_common(8):
            cover = int(coverage.get(name, 0))
            if cover < 2:
                continue
            persistent.append(
                {
                    "name": name,
                    "total": int(total),
                    "coverage": cover,
                }
            )
            if len(persistent) >= 5:
                break
        return names, persistent

    def _rising_focus_topics(self, history: List[Dict], focus_names: List[str]) -> List[Dict]:
        if len(history) < 2 or not focus_names:
            return []
        previous_entries = history[:-1]
        current_entry = history[-1]
        rising = []
        for name in focus_names:
            current_value = int((current_entry.get("focus_counts") or {}).get(name, 0))
            previous_avg = sum(
                int((item.get("focus_counts") or {}).get(name, 0))
                for item in previous_entries
            ) / len(previous_entries)
            delta = current_value - previous_avg
            if delta <= 0:
                continue
            rising.append(
                {
                    "name": name,
                    "current": current_value,
                    "previous_avg": round(previous_avg, 2),
                    "delta": round(delta, 2),
                }
            )
        rising.sort(key=lambda item: (item["delta"], item["current"]), reverse=True)
        return rising[:5]

    def _trend_section(self, trends: Dict) -> List[str]:
        history = trends.get("history") or []
        lines = ["## 多次运行趋势分析", ""]
        if len(history) < 2:
            lines.extend(["- 当前可比历史运行不足 2 次，暂无法形成趋势判断。", ""])
            return lines

        current = history[-1]
        avg = float(trends.get("average_paper_count", 0.0))
        delta = float(trends.get("paper_delta_vs_previous_avg", 0.0))
        journal_scope = trends.get("journal_scope") or []

        lines.extend(
            [
                f"- 纳入统计的可比运行数: {len(history)}",
                f"- 统计范围: {history[0].get('run_id', 'N/A')} -> {history[-1].get('run_id', 'N/A')}",
                f"- 当前论文数 / 历史均值: {current.get('paper_count', 0)} / {avg:.1f}",
                f"- 当前相对历史均值变化: {delta:+.1f}",
                f"- 期刊范围: {', '.join(journal_scope) if journal_scope else 'N/A'}",
                "",
            ]
        )

        rising_focus = trends.get("rising_focus") or []
        if rising_focus:
            lines.append("### 升温关注方向")
            for item in rising_focus:
                lines.append(
                    f"- {item['name']}: 当前 {item['current']}，历史均值 {item['previous_avg']:.1f}，增量 {item['delta']:+.1f}"
                )
            lines.append("")

        persistent_methods = trends.get("persistent_methods") or []
        if persistent_methods:
            lines.append("### 持续高频方法")
            for item in persistent_methods:
                lines.append(
                    f"- {item['name']}: 累计 {item['total']} 次，覆盖 {item['coverage']}/{len(history)} 次运行"
                )
            lines.append("")

        chart_refs = trends.get("chart_refs") or []
        if chart_refs:
            lines.append("### 趋势图")
            for item in chart_refs:
                lines.append(f"#### {item.get('title', 'N/A')}")
                lines.append(f"![{item.get('title', 'N/A')}]({item.get('path', '')})")
                lines.append("")

        return lines

    def _journal_section(
        self,
        journal: str,
        papers: List[Paper],
        comparison: Dict,
        shown_uids: set[str],
    ) -> List[str]:
        focus_count = sum(1 for paper in papers if paper.related_to_focus)
        figure_count = sum(1 for paper in papers if paper.figures)
        new_by_journal = comparison.get("new_by_journal") or {}
        new_count = int(new_by_journal.get(journal, 0))
        detail_papers = self._select_new_detail_papers(papers, shown_uids, limit=4)
        compact_papers = [paper for paper in papers if paper.uid() not in shown_uids]

        lines = [
            f"### {journal}",
            f"- 本次论文数: {len(papers)}",
            f"- 与关注方向相关: {focus_count}",
            f"- 含图论文数: {figure_count}",
            f"- 相比上次新增: {new_count}",
            "",
            "#### 本刊前沿要点",
        ]

        frontier_points = []
        for paper in papers:
            frontier_points.extend(paper.gemini_frontier_points or [])
        if frontier_points:
            for text, _ in Counter(frontier_points).most_common(6):
                lines.append(f"- {text}")
        else:
            lines.append("- 暂无结构化前沿点，建议查看下方重点论文。")
        lines.append("")

        lines.append("#### 本刊重点论文")
        if detail_papers:
            for paper in detail_papers:
                lines.extend(self._paper_block(paper, compact=False, show_images=True))
        else:
            lines.extend(["- 本刊重点论文已在前文展示，这里不再重复展开。", ""])

        if compact_papers:
            lines.append("#### 本刊其余论文速览")
            for paper in compact_papers:
                lines.extend(self._paper_block(paper, compact=True, show_images=False))

        return lines

    def _focus_topic_section(self, papers: List[Paper], shown_uids: set[str]) -> List[str]:
        lines = ["## 关注方向专题", ""]
        for keyword in self.focus_keywords:
            matched = [paper for paper in papers if keyword.lower() in [item.lower() for item in paper.matched_keywords]]
            lines.extend(self._single_focus_topic_section(keyword, matched, shown_uids))
        return lines

    def _single_focus_topic_section(
        self,
        keyword: str,
        papers: List[Paper],
        shown_uids: set[str],
    ) -> List[str]:
        lines = [f"### {keyword}", ""]
        if not papers:
            lines.extend(["- 本轮没有命中该关注方向的论文。", ""])
            return lines

        journal_counter = Counter(paper.journal for paper in papers)
        methods = Counter()
        frontier = Counter()
        for paper in papers:
            for item in paper.methods[:3]:
                if item:
                    methods[item] += 1
            for item in paper.gemini_frontier_points[:3]:
                if item:
                    frontier[item] += 1

        lines.append(f"- 命中文章数: {len(papers)}")
        lines.append(
            "- 涉及期刊: "
            + ", ".join(f"{journal}({count})" for journal, count in journal_counter.most_common())
        )
        if methods:
            lines.append(
                "- 高频方法: "
                + "；".join(text for text, _ in methods.most_common(5))
            )
        lines.append("")

        if frontier:
            lines.append("#### 该方向前沿要点")
            for text, _ in frontier.most_common(6):
                lines.append(f"- {text}")
            lines.append("")

        lines.append("#### 该方向重点论文")
        detail_papers = self._select_new_detail_papers(papers, shown_uids, limit=5)
        if detail_papers:
            for paper in detail_papers:
                lines.extend(self._paper_block(paper, compact=False, show_images=True))
        else:
            lines.extend(["- 该方向命中的重点论文已在前文展示，避免重复展开。", ""])
        return lines

    def _select_new_detail_papers(
        self,
        papers: List[Paper],
        shown_uids: set[str],
        limit: Optional[int] = None,
    ) -> List[Paper]:
        selected: List[Paper] = []
        for paper in papers:
            uid = paper.uid()
            if uid in shown_uids:
                continue
            shown_uids.add(uid)
            selected.append(paper)
            if limit is not None and len(selected) >= limit:
                break
        return selected

    def _paper_block(
        self,
        paper: Paper,
        compact: bool,
        show_images: bool,
    ) -> List[str]:
        title = paper.title_cn or paper.title
        lines = [
            f"#### {title}" if compact else f"#### {title}",
            f"- 英文标题: {paper.title}",
            f"- 期刊: {paper.journal}",
            f"- 日期/期次: {paper.publish_date or 'N/A'} / {paper.issue or 'N/A'}",
            f"- DOI: {paper.doi or 'N/A'}",
            f"- 分类: {', '.join(paper.categories) if paper.categories else 'N/A'}",
            f"- 关注关键词: {', '.join(paper.matched_keywords) if paper.matched_keywords else 'N/A'}",
            f"- 价值分: {paper.value_score if paper.value_score is not None else 'N/A'}",
            f"- 链接: {paper.url or 'N/A'}",
        ]

        if paper.one_line_summary_cn:
            lines.append(f"- 一句话摘要: {paper.one_line_summary_cn}")
        if paper.gemini_summary_cn:
            lines.append(f"- 核心内容: {paper.gemini_summary_cn}")

        if not compact:
            if paper.visual_summary_cn:
                lines.append(f"- 图像摘要: {paper.visual_summary_cn}")
            if paper.model_flow_summary_cn:
                lines.append(f"- 模型/流程摘要: {paper.model_flow_summary_cn}")
            if paper.gemini_interest_reason:
                lines.append(f"- 与关注方向的关系: {paper.gemini_interest_reason}")
            if paper.methods:
                lines.append(f"- 方法关键词: {', '.join(paper.methods)}")
            if paper.applications:
                lines.append(f"- 应用方向: {', '.join(paper.applications)}")
            if paper.gemini_frontier_points:
                lines.append(f"- 前沿要点: {'；'.join(paper.gemini_frontier_points)}")
            if paper.key_figure_points:
                lines.append(f"- 图像要点: {'；'.join(paper.key_figure_points)}")
            if paper.process_flow_steps:
                lines.append(f"- 研究流程: {' -> '.join(paper.process_flow_steps)}")
            elif paper.abstract and not paper.gemini_summary_cn:
                preview = paper.abstract.strip().replace("\n", " ")
                lines.append(f"- 摘要片段: {preview[:600]}")

        if show_images and not compact and paper.figures:
            lines.append("- 论文图像:")
            for figure in paper.figures[:3]:
                rel_path = figure.get("path", "")
                caption = figure.get("caption", "").strip()
                role_cn = (figure.get("role_hint_cn") or figure.get("display_role_cn") or "").strip()
                reason = (figure.get("selection_reason") or "").strip()
                if role_cn:
                    lines.append(f"图像角色: {role_cn}")
                if rel_path:
                    lines.append(f"![{title}]({rel_path})")
                if caption:
                    lines.append(f"_图注: {caption}_")
                if reason:
                    lines.append(f"_选图理由: {reason}_")

        if show_images and not compact and paper.generated_diagram_path:
            lines.append("- 自动生成流程图:")
            lines.append(f"![{title} 流程图]({paper.generated_diagram_path})")

        lines.append("")
        return lines

    def _frontier_lines(self, papers: List[Paper]) -> List[str]:
        points = []
        for paper in papers:
            points.extend(paper.gemini_frontier_points or [])
        if points:
            counter = Counter(points)
            return [f"- {text}" for text, _ in counter.most_common(12)]

        ranked = sorted(papers, key=lambda item: item.value_score or 0, reverse=True)
        fallback = [
            f"- {paper.title} ({paper.journal}, {paper.publish_date or 'N/A'})"
            for paper in ranked[:10]
        ]
        return fallback or ["- 暂无可提炼的前沿要点。"]

    @staticmethod
    def _format_media_stats(media_stats: Optional[Dict]) -> str:
        if not media_stats:
            return "尝试 0 篇 / 命中 0 篇 / 保存 0 张"
        return (
            f"尝试 {media_stats.get('papers_attempted', 0)} 篇 / "
            f"命中 {media_stats.get('papers_with_figures', 0)} 篇 / "
            f"保存 {media_stats.get('images_saved', 0)} 张"
        )

    @staticmethod
    def _pretty_dict(obj: Dict) -> str:
        return json.dumps(obj, ensure_ascii=False, indent=2)
