import argparse
import copy
import json
import re
import shutil
import subprocess
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import yaml

from src.analysis.article_media_collector import ArticleMediaCollector
from src.analysis.chatgpt_web_agent import ChatGPTWebAgent
from src.analysis.elsevier_content_collector import ElsevierContentCollector
from src.analysis.figure_prioritizer import FigurePrioritizer
from src.analysis.keyword_analyzer import KeywordAnalyzer
from src.core.config_loader import ConfigLoader
from src.core.paper import Paper
from src.core.storage import PaperStore
from src.crawler.factory import build_crawler
from src.crawler.journal_url_resolver import JournalUrlResolver
from src.report.diagram_renderer import DiagramRenderer
from src.report.report_builder import ReportBuilder


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run full AutoLit flow once: URL->crawl->classify->ChatGPT->report."
    )
    parser.add_argument(
        "--config",
        type=str,
        default="config/run_once.yaml",
        help="Path to one-shot config yaml.",
    )
    parser.add_argument(
        "--journal-url",
        action="append",
        help="Override journals with URL(s). Can be repeated.",
    )
    parser.add_argument(
        "--journal",
        action="append",
        help="Run only configured journal(s) by alias, name, or URL fragment. Can be repeated.",
    )
    parser.add_argument(
        "--list-journals",
        action="store_true",
        help="List configured journals and exit.",
    )
    parser.add_argument(
        "--issue-index",
        type=int,
        default=None,
        help="Override issue index for all journals. 0=latest, 1=second latest.",
    )
    parser.add_argument(
        "--max-items",
        type=int,
        default=None,
        help="Override max items for all journals.",
    )
    parser.add_argument(
        "--min-issue-size",
        type=int,
        default=None,
        help="Override minimal issue size filter for all journals.",
    )
    parser.add_argument(
        "--disable-chatgpt",
        "--disable-gemini",
        dest="disable_chatgpt",
        action="store_true",
        help="Skip ChatGPT web automation for this run.",
    )
    parser.add_argument(
        "--disable-media",
        action="store_true",
        help="Skip article figure capture for this run.",
    )
    parser.add_argument(
        "--prepare-chatgpt-login",
        "--prepare-gemini-login",
        dest="prepare_chatgpt_login",
        action="store_true",
        help="Open ChatGPT in the configured browser profile and wait for manual login.",
    )
    parser.add_argument(
        "--open-chatgpt-profile",
        "--open-gemini-profile",
        dest="open_chatgpt_profile",
        action="store_true",
        help="Open the configured Chrome profile in a normal browser window for ChatGPT login.",
    )
    parser.add_argument(
        "--retry-failed-run",
        type=str,
        help="Retry ChatGPT only for incomplete papers in an existing run directory or run id.",
    )
    parser.add_argument(
        "--rebuild-report-run",
        type=str,
        help="Rebuild report only for an existing run directory or run id.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    run_cfg = load_yaml(Path(args.config))
    if args.retry_failed_run:
        retry_failed_run(run_cfg, args.retry_failed_run)
        return
    if args.rebuild_report_run:
        rebuild_report_run(run_cfg, args.rebuild_report_run)
        return
    apply_cli_overrides(run_cfg, args)
    chatgpt_cfg = resolve_chatgpt_cfg(run_cfg)
    run_cfg["chatgpt_web"] = copy.deepcopy(chatgpt_cfg)
    if args.list_journals:
        print_configured_journals(run_cfg)
        return
    if args.open_chatgpt_profile:
        open_chatgpt_profile(chatgpt_cfg)
        print("[Done] ChatGPT profile opened in native Chrome")
        return
    if args.prepare_chatgpt_login:
        ChatGPTWebAgent(
            cfg=chatgpt_cfg,
            run_dir=Path(run_cfg.get("output", {}).get("runs_dir", "data/runs")),
        ).prepare_login_session()
        print("[Done] ChatGPT login preparation finished")
        return

    loader = ConfigLoader()
    taxonomy_cfg = loader.load("taxonomy")
    keywords_cfg = loader.load("keywords")
    output_cfg = run_cfg.get("output", {})
    store = PaperStore(db_file=output_cfg.get("db_file", "data/db/papers.jsonl"))
    existing_records = store.load_all()

    run_dir = build_run_dir(run_cfg)
    print(f"[Run] output_dir={run_dir}")
    save_yaml(run_dir / "effective_config.yaml", run_cfg)

    journals_raw = run_cfg.get("journals") or []
    if not journals_raw:
        raise RuntimeError("No journals configured in run_once.yaml (`journals` is empty).")

    resolver = JournalUrlResolver(defaults=run_cfg.get("defaults", {}))
    journal_cfgs = resolver.resolve_many(journals_raw)
    save_json(run_dir / "resolved_journals.json", journal_cfgs)
    print(f"[Resolve] journals={len(journal_cfgs)}")

    papers, crawl_stats = crawl_from_resolved(journal_cfgs)
    print(f"[Crawl] papers={len(papers)}")
    history_root = Path(output_cfg.get("report_history_dir", "reports/history"))
    history_registry = load_issue_history_registry(history_root)
    journal_issue_status = build_journal_issue_status(journal_cfgs, crawl_stats, history_registry)
    save_json(run_dir / "journal_issue_status.json", journal_issue_status)
    papers, issue_history_stats = filter_archived_issue_papers(papers, journal_issue_status)
    crawl_stats.setdefault("summary", {})
    crawl_stats["summary"]["papers_after_issue_history_filter"] = len(papers)
    crawl_stats["summary"]["issue_history_skipped_papers"] = issue_history_stats.get("skipped_paper_count", 0)
    crawl_stats["summary"]["issue_history_skipped_journals"] = issue_history_stats.get("skipped_journal_count", 0)
    print(
        "[IssueHistory] "
        f"skipped_journals={issue_history_stats.get('skipped_journal_count', 0)}, "
        f"skipped_papers={issue_history_stats.get('skipped_paper_count', 0)}, "
        f"active_papers={len(papers)}"
    )

    hydrate_papers_from_store(papers, existing_records)
    processing_papers, seen_stats = filter_seen_papers(
        papers,
        existing_records=existing_records,
        skip_seen=bool((run_cfg.get("analysis") or {}).get("skip_seen_papers", True)),
    )
    crawl_stats["summary"]["papers_after_seen_filter"] = len(processing_papers)
    crawl_stats["summary"]["seen_skipped_count"] = seen_stats.get("skipped_count", 0)
    crawl_stats["summary"]["seen_incomplete_reused_count"] = seen_stats.get("incomplete_reused_count", 0)
    print(
        "[Seen] "
        f"skipped={seen_stats.get('skipped_count', 0)}, "
        f"reused_incomplete={seen_stats.get('incomplete_reused_count', 0)}, "
        f"pending={len(processing_papers)}, "
        f"report_scope={len(papers)}"
    )

    focus_keywords = (
        (run_cfg.get("analysis") or {}).get("focus_keywords")
        or keywords_cfg.get("focus_keywords", [])
    )
    analyzer = KeywordAnalyzer(
        taxonomy_cfg=taxonomy_cfg,
        keywords_cfg={"focus_keywords": focus_keywords},
    )
    api_media_error = None
    api_media_stats = {
        "enabled": 0,
        "papers_attempted": 0,
        "metadata_updated": 0,
        "papers_with_figures": 0,
        "images_saved": 0,
        "papers_skipped": 0,
    }
    elsevier_cfg = run_cfg.get("elsevier_api", {})
    if elsevier_cfg.get("enabled", True):
        try:
            api_media_stats = ElsevierContentCollector(
                cfg=elsevier_cfg,
                run_dir=run_dir,
            ).collect(processing_papers)
        except Exception as exc:
            api_media_error = str(exc)
            err_file = run_dir / "article_media" / "elsevier_api_error.txt"
            err_file.parent.mkdir(parents=True, exist_ok=True)
            err_file.write_text(api_media_error, encoding="utf-8")

    analyzer.analyze(papers)

    media_stats = api_media_stats
    media_error = api_media_error
    if media_stats.get("images_saved", 0) == 0 and run_cfg.get("article_media", {}).get("enabled", False):
        media_candidates = sorted(
            processing_papers,
            key=lambda paper: (
                1 if paper.related_to_focus else 0,
                paper.value_score or 0,
                paper.publish_date or "",
            ),
            reverse=True,
        )
        media_cfg = run_cfg.get("article_media", {})
        if "channel" not in media_cfg and chatgpt_cfg.get("channel"):
            media_cfg["channel"] = chatgpt_cfg.get("channel")
        try:
            media_stats = ArticleMediaCollector(
                cfg=media_cfg,
                run_dir=run_dir,
            ).collect(media_candidates)
            media_error = None
        except Exception as exc:
            media_error = str(exc)
            media_stats = {
                "enabled": 1,
                "papers_attempted": 0,
                "papers_with_figures": 0,
                "images_saved": 0,
            }
            err_file = run_dir / "article_media" / "media_error.txt"
            err_file.parent.mkdir(parents=True, exist_ok=True)
            err_file.write_text(media_error, encoding="utf-8")

    print(
        "[Media] "
        f"papers_with_figures={media_stats.get('papers_with_figures', 0)}, "
        f"images_saved={media_stats.get('images_saved', 0)}"
    )
    prioritized_figure_papers = FigurePrioritizer.prioritize_papers(papers)
    if prioritized_figure_papers:
        print(f"[Media] prioritized_figure_sets={prioritized_figure_papers}")
    if api_media_stats.get("metadata_updated", 0):
        print(
            "[ElsevierAPI] "
            f"metadata_updated={api_media_stats.get('metadata_updated', 0)}, "
            f"images_saved={api_media_stats.get('images_saved', 0)}"
        )
    if media_error:
        print(f"[Media] fallback_without_images, reason={media_error}")

    llm_error = None
    llm_second_pass_stats = {
        "enabled": 0,
        "batch_total": 0,
        "batch_success": 0,
        "batch_failed": 0,
        "papers_updated": 0,
        "image_batches": 0,
    }
    llm_second_pass_error = None
    llm_candidates = select_papers_for_llm_retry(processing_papers, max_papers=0)
    if llm_candidates:
        try:
            llm_stats = ChatGPTWebAgent(
                cfg=chatgpt_cfg,
                run_dir=run_dir,
            ).enrich(llm_candidates, focus_keywords=focus_keywords)
        except Exception as exc:
            llm_error = str(exc)
            llm_stats = {
                "enabled": 1,
                "batch_total": 0,
                "batch_success": 0,
                "batch_failed": 0,
                "papers_updated": 0,
            }
            err_file = run_dir / "chatgpt_web_logs" / "automation_error.txt"
            err_file.parent.mkdir(parents=True, exist_ok=True)
            err_file.write_text(llm_error, encoding="utf-8")
    else:
        llm_stats = {
            "enabled": 1 if chatgpt_cfg.get("enabled", True) else 0,
            "batch_total": 0,
            "batch_success": 0,
            "batch_failed": 0,
            "papers_updated": 0,
        }

    second_pass_cfg = ((chatgpt_cfg or {}).get("second_pass") or {})
    if not llm_error and second_pass_cfg.get("enabled", True) and chatgpt_cfg.get("enabled", True):
        incomplete_papers = select_papers_for_llm_retry(
            papers,
            max_papers=int(second_pass_cfg.get("max_papers", 0) or 0),
        )
        if incomplete_papers:
            try:
                second_cfg = build_second_pass_llm_cfg(chatgpt_cfg)
                llm_second_pass_stats = ChatGPTWebAgent(
                    cfg=second_cfg,
                    run_dir=run_dir,
                ).enrich(incomplete_papers, focus_keywords=focus_keywords)
                llm_stats = merge_llm_stats(llm_stats, llm_second_pass_stats)
            except Exception as exc:
                llm_second_pass_error = str(exc)
                err_file = run_dir / "chatgpt_web_logs" / "second_pass_error.txt"
                err_file.parent.mkdir(parents=True, exist_ok=True)
                err_file.write_text(llm_second_pass_error, encoding="utf-8")

    print(
        "[ChatGPT] "
        f"success={llm_stats.get('batch_success', 0)}/"
        f"{llm_stats.get('batch_total', 0)}, "
        f"papers_updated={llm_stats.get('papers_updated', 0)}"
    )
    if llm_error:
        print(f"[ChatGPT] fallback_without_chatgpt, reason={llm_error}")
    if llm_second_pass_stats.get("enabled", 0):
        print(
            "[ChatGPT-2nd] "
            f"success={llm_second_pass_stats.get('batch_success', 0)}/"
            f"{llm_second_pass_stats.get('batch_total', 0)}, "
            f"papers_updated={llm_second_pass_stats.get('papers_updated', 0)}"
        )
    if llm_second_pass_error:
        print(f"[ChatGPT-2nd] fallback_without_second_pass, reason={llm_second_pass_error}")

    diagram_stats = DiagramRenderer(run_dir).render(papers)
    print(f"[Diagram] created={diagram_stats.get('diagrams_created', 0)}")

    for paper in papers:
        paper.processed = is_paper_fully_processed(paper)
    store_stats = store.upsert_many(papers)

    save_json(run_dir / "papers.json", [p.to_dict() for p in papers])
    save_json(run_dir / "crawl_stats.json", crawl_stats)

    settings_for_report = {
        "general": {"language": "zh", "report_format": "markdown"},
        "analysis": {"focus_keywords": focus_keywords},
    }
    report_builder = ReportBuilder(settings_for_report, run_dir)
    report_path = report_builder.build(
        papers=papers,
        crawl_stats=crawl_stats,
        store_stats=store_stats,
        llm_stats=llm_stats,
        media_stats=media_stats,
    )

    latest = Path(output_cfg.get("latest_report", "reports/latest_report.md"))
    latest.parent.mkdir(parents=True, exist_ok=True)
    publish_latest_report(
        run_dir,
        report_path,
        latest,
        output_cfg=output_cfg,
        papers=papers,
        crawl_stats=crawl_stats,
        journal_cfgs=journal_cfgs,
        journal_issue_status=journal_issue_status,
        settings_for_report=settings_for_report,
        store_stats=store_stats,
        llm_stats=llm_stats,
        media_stats=media_stats,
    )

    manifest = {
        "run_dir": str(run_dir),
        "paper_count": len(papers),
        "report": str(report_path),
        "latest_report": str(latest),
        "store_stats": store_stats,
        "media_stats": media_stats,
        "media_error": media_error,
        "seen_stats": seen_stats,
        "elsevier_api_stats": api_media_stats,
        "elsevier_api_error": api_media_error,
        "llm_provider": "chatgpt_web",
        "llm_stats": llm_stats,
        "llm_second_pass_stats": llm_second_pass_stats,
        "diagram_stats": diagram_stats,
        "journal_issue_status": journal_issue_status,
        "issue_history_stats": issue_history_stats,
        "comparison": report_builder.last_comparison,
        "trends": report_builder.last_trends,
        "llm_error": llm_error,
        "llm_second_pass_error": llm_second_pass_error,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    save_json(run_dir / "manifest.json", manifest)

    print(f"[Done] report={report_path}")
    print(f"[Done] latest={latest}")


def crawl_from_resolved(journals: List[dict]) -> tuple[List[Paper], Dict]:
    all_papers: List[Paper] = []
    crawl_stats = {"journals": {}, "summary": {}}

    for cfg in journals:
        crawler = build_crawler(cfg)
        fetched = crawler.fetch()
        all_papers.extend(fetched)
        issue_meta = summarize_selected_issue_batch(fetched)
        crawl_stats["journals"][cfg["id"]] = {
            "name": cfg["name"],
            "source_type": cfg["source_type"],
            "issue_index": cfg.get("issue_index", 0),
            "fetched_count": len(fetched),
            "issue_samples": sorted({p.issue or "unknown" for p in fetched})[:5],
            "selected_issue_label": issue_meta["issue_label"],
            "selected_issue_key": issue_meta["issue_key"],
            "selected_issue_publish_date": issue_meta["publish_date"],
        }
        print(
            f"[Crawl] {cfg['name']} "
            f"(issue_index={cfg.get('issue_index', 0)}, issue={issue_meta['issue_label']}): {len(fetched)}"
        )

    deduped = dedupe(all_papers)
    crawl_stats["summary"] = {
        "journals_count": len(journals),
        "papers_before_dedup": len(all_papers),
        "papers_after_dedup": len(deduped),
    }
    return deduped, crawl_stats


def summarize_selected_issue_batch(papers: List[Paper]) -> Dict[str, str]:
    issue_counts = Counter((paper.issue or "").strip() or "unknown" for paper in papers)
    issue_label = issue_counts.most_common(1)[0][0] if issue_counts else "unknown"
    publish_dates = sorted(
        {str(paper.publish_date or "").strip() for paper in papers if str(paper.publish_date or "").strip()}
    )
    publish_date = publish_dates[-1] if publish_dates else ""
    if issue_label == "unknown" and publish_date:
        issue_label = f"Published {publish_date}"

    issue_key = slugify_issue_label(issue_label)
    if issue_key in {"", "unknown"} and publish_date:
        issue_key = f"published_{publish_date.replace('-', '_')}"
    issue_key = issue_key or "unknown_issue"
    return {
        "issue_label": issue_label,
        "issue_key": issue_key,
        "publish_date": publish_date,
    }


def slugify_issue_label(value: str) -> str:
    text = re.sub(r"[^0-9A-Za-z]+", "_", str(value or "").strip().lower())
    return text.strip("_")


def issue_history_registry_path(history_root: Path) -> Path:
    return history_root / "_registry.json"


def load_issue_history_registry(history_root: Path) -> Dict:
    return load_json(issue_history_registry_path(history_root), default={"journals": {}}) or {"journals": {}}


def save_issue_history_registry(history_root: Path, registry: Dict) -> None:
    save_json(issue_history_registry_path(history_root), registry)


def build_journal_issue_status(
    journal_cfgs: List[dict],
    crawl_stats: Dict,
    registry: Dict,
) -> List[Dict]:
    statuses: List[Dict] = []
    journals_stats = (crawl_stats or {}).get("journals") or {}
    registry_journals = (registry or {}).get("journals") or {}

    for cfg in journal_cfgs:
        journal_id = cfg["id"]
        stat = journals_stats.get(journal_id, {})
        issue_samples = [str(item).strip() for item in (stat.get("issue_samples") or []) if str(item).strip()]
        fallback_issue_label = next((item for item in issue_samples if item.lower() != "unknown"), issue_samples[0] if issue_samples else "unknown")
        issue_label = str(stat.get("selected_issue_label") or fallback_issue_label or "unknown").strip() or "unknown"
        issue_key = str(stat.get("selected_issue_key") or slugify_issue_label(issue_label) or "unknown_issue").strip()
        publish_date = str(stat.get("selected_issue_publish_date") or "").strip()
        fetched_count = int(stat.get("fetched_count", 0) or 0)
        journal_entry = registry_journals.get(journal_id) or {}
        archived_issue = ((journal_entry.get("issues") or {}).get(issue_key) or {}) if issue_key else {}

        status = "new_issue"
        if fetched_count <= 0:
            status = "no_papers"
        elif archived_issue:
            status = "unchanged_issue"

        statuses.append(
            {
                "journal_id": journal_id,
                "journal_name": cfg.get("name") or journal_id,
                "journal_alias": cfg.get("alias") or journal_id,
                "home_url": cfg.get("home_url") or cfg.get("url") or "",
                "issue_index": int(cfg.get("issue_index", 0) or 0),
                "issue_label": issue_label,
                "issue_key": issue_key,
                "publish_date": publish_date,
                "fetched_count": fetched_count,
                "status": status,
                "existing_report_path": archived_issue.get("report_path", ""),
                "existing_generated_at": archived_issue.get("generated_at", ""),
                "existing_run_id": archived_issue.get("run_id", ""),
                "existing_issue_label": archived_issue.get("issue_label", ""),
            }
        )

    return statuses


def filter_archived_issue_papers(
    papers: List[Paper],
    journal_issue_status: List[Dict],
) -> tuple[List[Paper], Dict[str, int]]:
    status_by_journal = {item["journal_name"]: item for item in journal_issue_status}
    pending: List[Paper] = []
    skipped_count = 0
    skipped_journals = 0

    for item in journal_issue_status:
        if item.get("status") == "unchanged_issue":
            skipped_journals += 1

    for paper in papers:
        journal_status = status_by_journal.get(paper.journal or "")
        if journal_status and journal_status.get("status") == "unchanged_issue":
            skipped_count += 1
            continue
        pending.append(paper)

    return pending, {
        "skipped_paper_count": skipped_count,
        "skipped_journal_count": skipped_journals,
    }


def hydrate_papers_from_store(papers: List[Paper], existing_records: Dict[str, dict]) -> None:
    for paper in papers:
        record = existing_records.get(paper.uid())
        if not record:
            continue
        merge_stored_fields_into_paper(paper, record)
        if is_stored_record_fully_processed(record):
            paper.processed = True


def open_chatgpt_profile(cfg: dict) -> None:
    executable_path = cfg.get("executable_path") or "chrome.exe"
    user_data_dir = cfg.get("user_data_dir")
    profile_directory = cfg.get("profile_directory", "Default")
    chat_url = cfg.get("chat_url", "https://chatgpt.com/")

    if not user_data_dir:
        raise RuntimeError("chatgpt_web.user_data_dir is required to open the native Chrome profile.")

    subprocess.Popen(
        [
            executable_path,
            f"--user-data-dir={user_data_dir}",
            f"--profile-directory={profile_directory}",
            "--new-window",
            "--start-maximized",
            chat_url,
        ]
    )
    print(
        "[ChatGPT] native Chrome opened with profile "
        f"`{profile_directory}` from `{user_data_dir}`"
    )


def print_configured_journals(run_cfg: dict) -> None:
    journals = run_cfg.get("journals") or []
    if not journals:
        print("No journals configured.")
        return

    print("Configured journals:")
    for index, journal in enumerate(journals, start=1):
        alias = str(journal.get("alias") or "").strip() or "-"
        enabled = bool(journal.get("enabled", True))
        status = "enabled" if enabled else "disabled"
        name = journal.get("name") or journal.get("url") or f"journal_{index}"
        issue_index = journal.get("issue_index", run_cfg.get("defaults", {}).get("issue_index", 0))
        print(
            f"{index:02d}. alias={alias} | {status} | issue_index={issue_index} | {name}"
        )


def select_papers_for_llm_retry(papers: List[Paper], max_papers: int = 0) -> List[Paper]:
    selected = [paper for paper in papers if paper_needs_llm_retry(paper)]
    selected.sort(
        key=lambda paper: (
            1 if paper.related_to_focus else 0,
            1 if paper.figures else 0,
            paper.value_score or 0,
            paper.publish_date or "",
        ),
        reverse=True,
    )
    if max_papers > 0:
        return selected[:max_papers]
    return selected


def paper_needs_llm_retry(paper: Paper) -> bool:
    required_text = [
        paper.title_cn,
        paper.one_line_summary_cn,
        paper.gemini_summary_cn,
    ]
    if any(not str(value or "").strip() for value in required_text):
        return True
    if paper.figures and not str(paper.visual_summary_cn or "").strip():
        return True
    if not paper.process_flow_steps and not str(paper.model_flow_summary_cn or "").strip():
        return True
    return False


def is_paper_fully_processed(paper: Paper) -> bool:
    return not paper_needs_llm_retry(paper)


def is_stored_record_fully_processed(record: dict) -> bool:
    if bool((record or {}).get("processed")):
        return True
    try:
        return is_paper_fully_processed(Paper.from_dict(record or {}))
    except Exception:
        return False


def merge_stored_fields_into_paper(paper: Paper, record: dict) -> None:
    try:
        previous = Paper.from_dict(record or {})
    except Exception:
        return

    text_fields = (
        "abstract",
        "publish_date",
        "issue",
        "source",
        "title_cn",
        "one_line_summary_cn",
        "value_reason",
        "gemini_summary_cn",
        "gemini_interest_reason",
        "visual_summary_cn",
        "model_flow_summary_cn",
    )
    for field in text_fields:
        current = getattr(paper, field, None)
        if not str(current or "").strip():
            setattr(paper, field, getattr(previous, field, current))

    list_fields = (
        "authors",
        "keywords",
        "categories",
        "methods",
        "applications",
        "matched_keywords",
        "gemini_frontier_points",
        "key_figure_points",
        "process_flow_steps",
    )
    for field in list_fields:
        current_values = list(getattr(paper, field, []) or [])
        previous_values = list(getattr(previous, field, []) or [])
        merged = []
        seen = set()
        for item in current_values + previous_values:
            text = str(item).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            merged.append(text)
        setattr(paper, field, merged)

    if paper.related_to_focus is None:
        paper.related_to_focus = previous.related_to_focus
    if paper.value_score is None:
        paper.value_score = previous.value_score
    if not paper.figures and previous.figures:
        paper.figures = copy.deepcopy(previous.figures)
    if not paper.generated_diagram_path and previous.generated_diagram_path:
        paper.generated_diagram_path = previous.generated_diagram_path
    if not paper.processed and previous.processed:
        paper.processed = previous.processed
    if not paper.added_at:
        paper.added_at = previous.added_at


def filter_seen_papers(
    papers: List[Paper],
    existing_records: Dict[str, dict],
    *,
    skip_seen: bool = True,
) -> tuple[List[Paper], Dict[str, int]]:
    if not skip_seen:
        return papers, {"enabled": 0, "skipped_count": 0, "incomplete_reused_count": 0}

    pending: List[Paper] = []
    skipped_count = 0
    incomplete_reused_count = 0

    for paper in papers:
        record = existing_records.get(paper.uid())
        if not record:
            pending.append(paper)
            continue
        if is_stored_record_fully_processed(record):
            skipped_count += 1
            continue
        merge_stored_fields_into_paper(paper, record)
        incomplete_reused_count += 1
        pending.append(paper)

    return pending, {
        "enabled": 1,
        "skipped_count": skipped_count,
        "incomplete_reused_count": incomplete_reused_count,
    }


def build_second_pass_llm_cfg(base_cfg: dict) -> dict:
    cfg = copy.deepcopy(base_cfg or {})
    second_pass = cfg.pop("second_pass", {}) or {}
    cfg["enabled"] = True
    for key, value in second_pass.items():
        if key == "enabled":
            continue
        cfg[key] = value
    return cfg


def merge_llm_stats(primary: dict, secondary: dict) -> dict:
    merged = dict(primary or {})
    for key in ("batch_total", "batch_success", "batch_failed", "papers_updated", "image_batches"):
        merged[key] = int(merged.get(key, 0)) + int((secondary or {}).get(key, 0))
    merged["enabled"] = 1 if merged.get("enabled", 0) or (secondary or {}).get("enabled", 0) else 0
    return merged


def retry_failed_run(base_cfg: dict, run_ref: str) -> None:
    run_dir = resolve_run_reference(run_ref, base_cfg)
    run_cfg = load_effective_run_config(run_dir, base_cfg)
    manifest = load_json(run_dir / "manifest.json", default={}) or {}
    papers = load_papers_from_run(run_dir)
    if not papers:
        raise RuntimeError(f"No papers found in existing run: {run_dir}")

    print(f"[Repair] target_run={run_dir}")
    prioritized = FigurePrioritizer.prioritize_papers(papers)
    if prioritized:
        print(f"[Repair] reprioritized_figure_sets={prioritized}")

    pending = select_papers_for_llm_retry(papers, max_papers=0)
    print(f"[Repair] papers_pending={len(pending)}")

    focus_keywords = resolve_focus_keywords(run_cfg)
    retry_stats = {
        "enabled": 1,
        "batch_total": 0,
        "batch_success": 0,
        "batch_failed": 0,
        "papers_updated": 0,
        "image_batches": 0,
    }
    retry_error = None

    if pending:
        try:
            retry_stats = ChatGPTWebAgent(
                cfg=build_retry_llm_cfg(resolve_chatgpt_cfg(run_cfg)),
                run_dir=run_dir,
            ).enrich(pending, focus_keywords=focus_keywords)
        except Exception as exc:
            retry_error = str(exc)
            err_file = run_dir / "chatgpt_web_logs" / "retry_error.txt"
            err_file.parent.mkdir(parents=True, exist_ok=True)
            err_file.write_text(retry_error, encoding="utf-8")
    else:
        print("[Repair] no incomplete papers found, continue to rebuild report only")

    merged_llm_stats = merge_llm_stats(
        manifest.get("llm_stats", manifest.get("gemini_stats", {})),
        retry_stats,
    )
    if retry_error:
        print(f"[Repair] ChatGPT retry failed: {retry_error}")
    else:
        print(
            "[Repair] ChatGPT retry "
            f"success={retry_stats.get('batch_success', 0)}/"
            f"{retry_stats.get('batch_total', 0)}, "
            f"papers_updated={retry_stats.get('papers_updated', 0)}"
        )

    diagram_stats = DiagramRenderer(run_dir).render(papers)
    print(f"[Repair] diagrams_created={diagram_stats.get('diagrams_created', 0)}")

    retry_store_stats = {}
    if pending and not retry_error:
        output_cfg = run_cfg.get("output", {})
        for paper in papers:
            paper.processed = is_paper_fully_processed(paper)
        retry_store_stats = PaperStore(
            db_file=output_cfg.get("db_file", "data/db/papers.jsonl")
        ).upsert_many(papers)

    save_json(run_dir / "papers.json", [paper.to_dict() for paper in papers])
    report_path, latest_path, manifest = rebuild_report_artifacts(
        run_cfg=run_cfg,
        run_dir=run_dir,
        papers=papers,
        manifest=manifest,
        llm_stats=merged_llm_stats,
        diagram_stats=diagram_stats,
    )

    history = list(manifest.get("repair_history") or [])
    history.append(
        {
            "mode": "retry_failed_run",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "papers_pending": len(pending),
            "papers_updated": retry_stats.get("papers_updated", 0),
            "batch_success": retry_stats.get("batch_success", 0),
            "batch_total": retry_stats.get("batch_total", 0),
            "error": retry_error or "",
        }
    )
    manifest["repair_history"] = history
    manifest["llm_provider"] = "chatgpt_web"
    manifest["llm_stats"] = merged_llm_stats
    if retry_store_stats:
        manifest["retry_store_stats"] = retry_store_stats
    manifest["llm_retry_error"] = retry_error
    save_json(run_dir / "manifest.json", manifest)

    print(f"[Repair] report={report_path}")
    print(f"[Repair] latest={latest_path}")


def rebuild_report_run(base_cfg: dict, run_ref: str) -> None:
    run_dir = resolve_run_reference(run_ref, base_cfg)
    run_cfg = load_effective_run_config(run_dir, base_cfg)
    manifest = load_json(run_dir / "manifest.json", default={}) or {}
    papers = load_papers_from_run(run_dir)
    if not papers:
        raise RuntimeError(f"No papers found in existing run: {run_dir}")

    print(f"[Rebuild] target_run={run_dir}")
    prioritized = FigurePrioritizer.prioritize_papers(papers)
    if prioritized:
        print(f"[Rebuild] reprioritized_figure_sets={prioritized}")

    diagram_stats = DiagramRenderer(run_dir).render(papers)
    print(f"[Rebuild] diagrams_created={diagram_stats.get('diagrams_created', 0)}")

    save_json(run_dir / "papers.json", [paper.to_dict() for paper in papers])
    report_path, latest_path, manifest = rebuild_report_artifacts(
        run_cfg=run_cfg,
        run_dir=run_dir,
        papers=papers,
        manifest=manifest,
        diagram_stats=diagram_stats,
    )

    history = list(manifest.get("rebuild_history") or [])
    history.append(
        {
            "mode": "rebuild_report_run",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "papers_count": len(papers),
        }
    )
    manifest["rebuild_history"] = history
    save_json(run_dir / "manifest.json", manifest)

    print(f"[Rebuild] report={report_path}")
    print(f"[Rebuild] latest={latest_path}")


def rebuild_report_artifacts(
    run_cfg: dict,
    run_dir: Path,
    papers: List[Paper],
    manifest: Optional[dict] = None,
    *,
    store_stats: Optional[dict] = None,
    llm_stats: Optional[dict] = None,
    media_stats: Optional[dict] = None,
    diagram_stats: Optional[dict] = None,
) -> tuple[Path, Path, dict]:
    manifest = dict(manifest or {})
    crawl_stats = load_json(run_dir / "crawl_stats.json", default={}) or {}
    focus_keywords = resolve_focus_keywords(run_cfg)
    settings_for_report = {
        "general": {"language": "zh", "report_format": "markdown"},
        "analysis": {"focus_keywords": focus_keywords},
    }
    report_builder = ReportBuilder(settings_for_report, run_dir)
    report_path = report_builder.build(
        papers=papers,
        crawl_stats=crawl_stats,
        store_stats=store_stats if store_stats is not None else manifest.get("store_stats", {}),
        llm_stats=llm_stats if llm_stats is not None else manifest.get("llm_stats", manifest.get("gemini_stats", {})),
        media_stats=media_stats if media_stats is not None else manifest.get("media_stats", {}),
    )
    latest = Path(
        (run_cfg.get("output") or {}).get(
            "latest_report",
            manifest.get("latest_report") or "reports/latest_report.md",
        )
    )
    latest.parent.mkdir(parents=True, exist_ok=True)
    publish_latest_report(run_dir, report_path, latest, output_cfg=run_cfg.get("output", {}))

    manifest["report"] = str(report_path)
    manifest["latest_report"] = str(latest)
    manifest["comparison"] = report_builder.last_comparison
    manifest["trends"] = report_builder.last_trends
    manifest["generated_at"] = datetime.now().isoformat(timespec="seconds")
    if store_stats is not None:
        manifest["store_stats"] = store_stats
    if llm_stats is not None:
        manifest["llm_stats"] = llm_stats
    if media_stats is not None:
        manifest["media_stats"] = media_stats
    if diagram_stats is not None:
        manifest["diagram_stats"] = diagram_stats
    return report_path, latest, manifest


def resolve_run_reference(run_ref: str, run_cfg: dict) -> Path:
    raw = str(run_ref or "").strip()
    if not raw:
        raise RuntimeError("Run reference cannot be empty.")

    candidates = [Path(raw)]
    runs_dir = Path((run_cfg.get("output") or {}).get("runs_dir", "data/runs"))
    candidates.append(runs_dir / raw)

    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()

    raise FileNotFoundError(f"Run directory not found: {run_ref}")


def load_effective_run_config(run_dir: Path, fallback_cfg: dict) -> dict:
    effective_path = run_dir / "effective_config.yaml"
    if effective_path.exists():
        run_cfg = load_yaml(effective_path)
    else:
        run_cfg = copy.deepcopy(fallback_cfg or {})

    if not (run_cfg.get("output") or {}).get("latest_report"):
        run_cfg.setdefault("output", {})
        run_cfg["output"]["latest_report"] = (
            (fallback_cfg.get("output") or {}).get("latest_report")
            or "reports/latest_report.md"
        )
    if "chatgpt_web" not in run_cfg and run_cfg.get("gemini_web"):
        run_cfg["chatgpt_web"] = copy.deepcopy(run_cfg["gemini_web"])
    return run_cfg


def load_papers_from_run(run_dir: Path) -> List[Paper]:
    payload = load_json(run_dir / "papers.json", default=[]) or []
    return [Paper.from_dict(item) for item in payload if isinstance(item, dict)]


def resolve_focus_keywords(run_cfg: dict) -> List[str]:
    keywords_cfg = ConfigLoader().load("keywords")
    return (
        (run_cfg.get("analysis") or {}).get("focus_keywords")
        or keywords_cfg.get("focus_keywords", [])
    )


def build_retry_llm_cfg(base_cfg: dict) -> dict:
    cfg = copy.deepcopy(base_cfg or {})
    second_pass = cfg.pop("second_pass", {}) or {}
    cfg["enabled"] = True
    for key, value in second_pass.items():
        if key in {"enabled", "max_papers", "use_images", "prompt_style"}:
            continue
        cfg[key] = value
    cfg["use_images"] = bool((base_cfg or {}).get("use_images", True))
    cfg["prompt_style"] = "full" if cfg["use_images"] else str((base_cfg or {}).get("prompt_style", "full"))
    return cfg


def dedupe(papers: Iterable[Paper]) -> List[Paper]:
    by_uid: Dict[str, Paper] = {}
    for p in papers:
        by_uid[p.uid()] = p
    return list(by_uid.values())


def build_run_dir(run_cfg: dict) -> Path:
    output_cfg = run_cfg.get("output", {})
    base_dir = Path(output_cfg.get("runs_dir", "data/runs"))
    run_dir = base_dir / datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def load_yaml(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_json(path: Path, default=None):
    if not path.exists():
        return copy.deepcopy(default)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return copy.deepcopy(default)


def save_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def resolve_chatgpt_cfg(run_cfg: dict) -> dict:
    return copy.deepcopy(run_cfg.get("chatgpt_web") or run_cfg.get("gemini_web") or {})


def apply_cli_overrides(run_cfg: dict, args) -> None:
    if args.journal_url:
        run_cfg["journals"] = [{"url": u} for u in args.journal_url]
    elif args.journal:
        run_cfg["journals"] = filter_configured_journals(run_cfg.get("journals") or [], args.journal)

    if args.issue_index is not None:
        journals = run_cfg.get("journals") or []
        for j in journals:
            j["issue_index"] = int(args.issue_index)

    if args.max_items is not None:
        journals = run_cfg.get("journals") or []
        for j in journals:
            j["max_items"] = int(args.max_items)

    if args.min_issue_size is not None:
        journals = run_cfg.get("journals") or []
        for j in journals:
            j["min_issue_size"] = int(args.min_issue_size)

    if args.disable_chatgpt:
        run_cfg["chatgpt_web"] = resolve_chatgpt_cfg(run_cfg)
        run_cfg["chatgpt_web"]["enabled"] = False
        if "gemini_web" in run_cfg:
            run_cfg["gemini_web"]["enabled"] = False

    if args.disable_media:
        run_cfg.setdefault("article_media", {})
        run_cfg["article_media"]["enabled"] = False
        run_cfg.setdefault("elsevier_api", {})
        run_cfg["elsevier_api"]["max_images_per_paper"] = 0


def filter_configured_journals(journals: List[dict], selectors: List[str]) -> List[dict]:
    normalized = [str(item).strip().lower() for item in selectors if str(item).strip()]
    if not normalized:
        return journals

    matched = []
    for journal in journals:
        alias = str(journal.get("alias") or "").strip().lower()
        name = str(journal.get("name") or "").strip().lower()
        url = str(journal.get("url") or "").strip().lower()
        source = f"{alias} {name} {url}"
        if any(token in source for token in normalized):
            matched.append(journal)

    if not matched:
        requested = ", ".join(selectors)
        raise RuntimeError(f"No configured journals matched selectors: {requested}")
    return matched


def save_yaml(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def publish_latest_report(
    run_dir: Path,
    report_path: Path,
    latest_path: Path,
    *,
    output_cfg: Optional[dict] = None,
    papers: Optional[List[Paper]] = None,
    crawl_stats: Optional[Dict] = None,
    journal_cfgs: Optional[List[dict]] = None,
    journal_issue_status: Optional[List[Dict]] = None,
    settings_for_report: Optional[dict] = None,
    store_stats: Optional[dict] = None,
    llm_stats: Optional[dict] = None,
    media_stats: Optional[dict] = None,
) -> None:
    output_cfg = output_cfg or {}
    reports_dir = latest_path.parent
    history_root = Path(output_cfg.get("report_history_dir", reports_dir / "history"))
    history_root.mkdir(parents=True, exist_ok=True)

    latest_bundle_dir = reports_dir / "latest"
    if latest_bundle_dir.exists():
        shutil.rmtree(latest_bundle_dir)
    latest_bundle_dir.mkdir(parents=True, exist_ok=True)
    for folder_name in ("assets", "article_media", "generated_diagrams"):
        src = run_dir / folder_name
        if src.exists():
            shutil.copytree(src, latest_bundle_dir / folder_name, dirs_exist_ok=True)

    shutil.copy2(report_path, latest_bundle_dir / "report.md")
    combined_report_rel = f"{latest_bundle_dir.name}/report.md"

    if papers is None:
        papers = [
            Paper.from_dict(item)
            for item in (load_json(run_dir / "papers.json", default=[]) or [])
            if isinstance(item, dict)
        ]
    if crawl_stats is None:
        crawl_stats = load_json(run_dir / "crawl_stats.json", default={}) or {}
    if journal_cfgs is None:
        journal_cfgs = load_json(run_dir / "resolved_journals.json", default=[]) or []
    if journal_issue_status is None:
        if journal_cfgs:
            journal_issue_status = build_journal_issue_status(
                journal_cfgs,
                crawl_stats,
                load_issue_history_registry(history_root),
            )
        else:
            journal_issue_status = load_json(run_dir / "journal_issue_status.json", default=[]) or []
    if settings_for_report is None:
        effective_cfg = load_yaml(run_dir / "effective_config.yaml") if (run_dir / "effective_config.yaml").exists() else {}
        focus_keywords = ((effective_cfg.get("analysis") or {}).get("focus_keywords") or [])
        settings_for_report = {
            "general": {"language": "zh", "report_format": "markdown"},
            "analysis": {"focus_keywords": focus_keywords},
        }

    registry = load_issue_history_registry(history_root)
    published_status = publish_journal_issue_reports(
        run_dir=run_dir,
        reports_dir=reports_dir,
        history_root=history_root,
        papers=papers,
        crawl_stats=crawl_stats,
        journal_issue_status=journal_issue_status,
        settings_for_report=settings_for_report,
        store_stats=store_stats or {},
        llm_stats=llm_stats or {},
        media_stats=media_stats or {},
        registry=registry,
    )
    save_issue_history_registry(history_root, registry)

    index_path = Path(output_cfg.get("report_history_index", history_root / "index.md"))
    update_issue_history_indexes(
        reports_dir=reports_dir,
        history_root=history_root,
        registry=registry,
        index_path=index_path,
    )
    latest_text = build_latest_overview_report(
        reports_dir=reports_dir,
        run_dir=run_dir,
        journal_issue_status=published_status,
        combined_report_rel=combined_report_rel,
    )
    latest_path.write_text(latest_text, encoding="utf-8-sig")


def publish_journal_issue_reports(
    *,
    run_dir: Path,
    reports_dir: Path,
    history_root: Path,
    papers: List[Paper],
    crawl_stats: Dict,
    journal_issue_status: List[Dict],
    settings_for_report: dict,
    store_stats: dict,
    llm_stats: dict,
    media_stats: dict,
    registry: Dict,
) -> List[Dict]:
    papers_by_journal: Dict[str, List[Paper]] = defaultdict(list)
    for paper in papers:
        papers_by_journal[paper.journal or "Unknown Journal"].append(paper)

    registry.setdefault("journals", {})
    published_status: List[Dict] = []
    generated_at = datetime.now().isoformat(timespec="seconds")

    for item in journal_issue_status:
        status = dict(item)
        journal_id = status["journal_id"]
        journal_name = status["journal_name"]
        journal_entry = registry["journals"].setdefault(
            journal_id,
            {
                "journal_id": journal_id,
                "journal_name": journal_name,
                "journal_alias": status.get("journal_alias", journal_id),
                "home_url": status.get("home_url", ""),
                "issues": {},
            },
        )

        if status.get("status") != "new_issue":
            published_status.append(status)
            continue

        journal_papers = papers_by_journal.get(journal_name, [])
        if not journal_papers:
            status["status"] = "no_papers"
            published_status.append(status)
            continue

        journal_run_dir = run_dir / "journal_reports" / journal_id
        journal_run_dir.mkdir(parents=True, exist_ok=True)
        journal_crawl_stats = {
            "journals": {journal_id: (crawl_stats.get("journals") or {}).get(journal_id, {})},
            "summary": {
                "journals_count": 1,
                "papers_after_dedup": len(journal_papers),
                "papers_after_issue_history_filter": len(journal_papers),
                "papers_after_seen_filter": len(journal_papers),
            },
        }
        journal_store_stats = {
            "inserted": len(journal_papers),
            "updated": 0,
            "total": int((store_stats or {}).get("total", len(journal_papers)) or len(journal_papers)),
        }
        journal_builder = ReportBuilder(settings_for_report, journal_run_dir)
        journal_report_path = journal_builder.build(
            papers=journal_papers,
            crawl_stats=journal_crawl_stats,
            store_stats=journal_store_stats,
            llm_stats=llm_stats,
            media_stats=media_stats,
        )

        target_dir = history_root / journal_id / status["issue_key"]
        issue_manifest = {
            "journal_id": journal_id,
            "journal_name": journal_name,
            "issue_key": status["issue_key"],
            "issue_label": status["issue_label"],
            "paper_count": len(journal_papers),
            "generated_at": generated_at,
            "run_id": run_dir.name,
            "source_run_report": str(report_path_relative(run_dir, journal_report_path)),
        }
        archive_journal_issue_snapshot(
            run_dir=run_dir,
            journal_run_dir=journal_run_dir,
            journal_report_path=journal_report_path,
            target_dir=target_dir,
            papers=journal_papers,
            issue_manifest=issue_manifest,
        )

        rel_report_path = (target_dir / "report.md").relative_to(reports_dir).as_posix()
        journal_entry["home_url"] = status.get("home_url", journal_entry.get("home_url", ""))
        journal_entry["latest_issue_key"] = status["issue_key"]
        journal_entry["latest_issue_label"] = status["issue_label"]
        journal_entry.setdefault("issues", {})
        journal_entry["issues"][status["issue_key"]] = {
            "issue_key": status["issue_key"],
            "issue_label": status["issue_label"],
            "paper_count": len(journal_papers),
            "generated_at": generated_at,
            "run_id": run_dir.name,
            "report_path": rel_report_path,
        }

        status["report_path"] = rel_report_path
        status["generated_at"] = generated_at
        published_status.append(status)

    return published_status


def archive_journal_issue_snapshot(
    *,
    run_dir: Path,
    journal_run_dir: Path,
    journal_report_path: Path,
    target_dir: Path,
    papers: List[Paper],
    issue_manifest: Dict,
) -> None:
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    shutil.copy2(journal_report_path, target_dir / "report.md")
    save_json(target_dir / "manifest.json", issue_manifest)
    save_json(target_dir / "papers.json", [paper.to_dict() for paper in papers])

    assets_src = journal_run_dir / "assets"
    if assets_src.exists():
        shutil.copytree(assets_src, target_dir / "assets", dirs_exist_ok=True)

    copy_selected_paper_artifacts(run_dir, target_dir, papers)


def copy_selected_paper_artifacts(run_dir: Path, target_dir: Path, papers: List[Paper]) -> None:
    rel_files = set()
    for paper in papers:
        for figure in paper.figures or []:
            rel_path = str(figure.get("path") or "").strip()
            if rel_path:
                rel_files.add(rel_path.replace("\\", "/"))
        diagram_path = str(paper.generated_diagram_path or "").strip()
        if diagram_path:
            rel_files.add(diagram_path.replace("\\", "/"))

    for rel_file in sorted(rel_files):
        src = run_dir / Path(rel_file)
        if not src.exists() or not src.is_file():
            continue
        dst = target_dir / Path(rel_file)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def build_latest_overview_report(
    *,
    reports_dir: Path,
    run_dir: Path,
    journal_issue_status: List[Dict],
    combined_report_rel: str,
) -> str:
    new_count = sum(1 for item in journal_issue_status if item.get("status") == "new_issue")
    unchanged_count = sum(1 for item in journal_issue_status if item.get("status") == "unchanged_issue")
    no_papers_count = sum(1 for item in journal_issue_status if item.get("status") == "no_papers")

    lines = [
        "# Latest Journal Update",
        "",
        f"- Updated at: {datetime.now().isoformat(timespec='seconds')}",
        f"- Run id: {run_dir.name}",
        f"- Selected journals: {len(journal_issue_status)}",
        f"- New issues archived: {new_count}",
        f"- Unchanged journals: {unchanged_count}",
        f"- Journals with no papers: {no_papers_count}",
        f"- Combined run report: [{combined_report_rel}]({combined_report_rel})",
        "",
        "## Journal Status",
        "",
    ]

    if not journal_issue_status:
        lines.append("- No journal status available.")
        return "\n".join(lines) + "\n"

    for item in journal_issue_status:
        lines.extend(
            [
                f"### {item.get('journal_name', item.get('journal_id', 'Unknown Journal'))}",
                f"- Detected issue: {item.get('issue_label', 'unknown')}",
                f"- Fetched papers: {item.get('fetched_count', 0)}",
            ]
        )
        if item.get("status") == "new_issue":
            report_path = item.get("report_path", "")
            lines.append("- Status: new issue archived in history.")
            if report_path:
                lines.append(f"- Current issue report: [{item.get('issue_label', 'report')}]({report_path})")
        elif item.get("status") == "unchanged_issue":
            report_path = item.get("existing_report_path", "")
            lines.append("- Status: no new issue; this issue was already recorded before.")
            if report_path:
                lines.append(f"- Existing issue report: [{item.get('existing_issue_label') or item.get('issue_label', 'report')}]({report_path})")
            if item.get("existing_generated_at"):
                lines.append(f"- Previously archived at: {item['existing_generated_at']}")
            if item.get("existing_run_id"):
                lines.append(f"- Source run: {item['existing_run_id']}")
        else:
            lines.append("- Status: no papers were fetched for this journal in the current run.")
        lines.append("")

    return "\n".join(lines) + "\n"


def update_issue_history_indexes(
    *,
    reports_dir: Path,
    history_root: Path,
    registry: Dict,
    index_path: Path,
) -> None:
    journals = (registry or {}).get("journals") or {}
    root_lines = [
        "# Journal Issue History",
        "",
        f"- Updated at: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "## Journals",
        "",
    ]

    if not journals:
        root_lines.append("- No archived issue reports yet.")
    else:
        for journal_id, entry in sorted(journals.items(), key=lambda item: item[1].get("journal_name", item[0]).lower()):
            journal_dir = history_root / journal_id
            journal_dir.mkdir(parents=True, exist_ok=True)
            issues = list((entry.get("issues") or {}).values())
            issues.sort(key=lambda item: item.get("generated_at", ""), reverse=True)

            journal_index_lines = [
                f"# {entry.get('journal_name', journal_id)}",
                "",
                f"- Journal id: {journal_id}",
                f"- Home: {entry.get('home_url', 'N/A')}",
                f"- Latest issue: {entry.get('latest_issue_label', 'N/A')}",
                f"- Archived issues: {len(issues)}",
                "",
                "## Issues",
                "",
            ]
            if not issues:
                journal_index_lines.append("- No archived issues yet.")
            else:
                for issue in issues:
                    report_path = issue.get("report_path", "")
                    try:
                        rel_from_journal = Path(report_path).relative_to(Path("history") / journal_id).as_posix()
                    except Exception:
                        rel_from_journal = Path(report_path).name if report_path else ""
                    journal_index_lines.append(
                        f"- {issue.get('issue_label', issue.get('issue_key', 'unknown'))} | "
                        f"papers={issue.get('paper_count', 0)} | "
                        f"archived={issue.get('generated_at', 'N/A')} | "
                        f"[report]({rel_from_journal})"
                    )
            (journal_dir / "index.md").write_text("\n".join(journal_index_lines) + "\n", encoding="utf-8")

            root_lines.append(
                f"- [{entry.get('journal_name', journal_id)}]({journal_id}/index.md) | "
                f"latest={entry.get('latest_issue_label', 'N/A')} | "
                f"issues={len(issues)}"
            )

    history_index_path = history_root / "index.md"
    history_index_path.write_text("\n".join(root_lines) + "\n", encoding="utf-8")
    if index_path != history_index_path:
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text("\n".join(root_lines) + "\n", encoding="utf-8")


def report_path_relative(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path).replace("\\", "/")


if __name__ == "__main__":
    main()
