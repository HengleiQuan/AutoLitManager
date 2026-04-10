import re
import time
from pathlib import Path
from typing import Dict, List

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from src.analysis.figure_prioritizer import FigurePrioritizer
from src.core.chrome_profile import close_profile_chrome_windows
from src.core.paper import Paper


class ArticleMediaCollector:
    FIGURE_SELECTORS = [
        "main figure",
        "article figure",
        "figure",
        "img",
    ]

    def __init__(self, cfg: dict, run_dir: Path):
        self.cfg = cfg or {}
        self.run_dir = run_dir
        self.media_dir = run_dir / "article_media"
        self.media_dir.mkdir(parents=True, exist_ok=True)

    def collect(self, papers: List[Paper]) -> Dict[str, int]:
        if not self.cfg.get("enabled", True):
            return {
                "enabled": 0,
                "papers_attempted": 0,
                "papers_with_figures": 0,
                "images_saved": 0,
            }

        max_papers = int(self.cfg.get("max_papers", len(papers) or 0))
        candidates = papers[:max_papers]
        if not candidates:
            return {
                "enabled": 1,
                "papers_attempted": 0,
                "papers_with_figures": 0,
                "images_saved": 0,
            }

        papers_with_figures = 0
        images_saved = 0

        with sync_playwright() as pw:
            context, owns_browser = self._launch_context(pw)
            page = context.pages[0] if context.pages else context.new_page()
            timeout_ms = int(self.cfg.get("page_timeout_sec", 45)) * 1000
            page.set_default_timeout(timeout_ms)

            try:
                for paper in candidates:
                    try:
                        if page.is_closed():
                            page = context.new_page()
                            page.set_default_timeout(timeout_ms)
                    except PlaywrightError:
                        break
                    try:
                        saved = self._collect_for_paper(page, paper)
                    except PlaywrightError:
                        saved = 0
                        try:
                            if page.is_closed() and not context.pages:
                                page = context.new_page()
                                page.set_default_timeout(timeout_ms)
                        except PlaywrightError:
                            break
                    if saved:
                        papers_with_figures += 1
                        images_saved += saved
            finally:
                context.close()
                if owns_browser:
                    try:
                        owns_browser.close()
                    except PlaywrightError:
                        pass

        return {
            "enabled": 1,
            "papers_attempted": len(candidates),
            "papers_with_figures": papers_with_figures,
            "images_saved": images_saved,
        }

    def _collect_for_paper(self, page, paper: Paper) -> int:
        if not paper.url:
            return 0

        print(f"[Media] opening article: {paper.url}")
        try:
            page.goto(paper.url, wait_until="domcontentloaded")
        except (PlaywrightTimeoutError, PlaywrightError):
            return 0

        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except (PlaywrightTimeoutError, PlaywrightError):
            pass

        if self._is_blocked(page):
            wait_sec = int(self.cfg.get("manual_challenge_wait_sec", 120))
            if wait_sec > 0:
                print(
                    "[Media] Cloudflare challenge detected. "
                    "Click the checkbox in the opened browser window, "
                    f"then wait for auto-resume. Timeout={wait_sec}s URL={paper.url}"
                )
                if not self._wait_until_unblocked(page, wait_sec):
                    print("[Media] challenge not cleared in time, skip current article")
                    return 0
            else:
                return 0

        paper_dir = self.media_dir / self._safe_name(paper.uid())[:48]
        paper_dir.mkdir(parents=True, exist_ok=True)

        max_images = int(self.cfg.get("max_images_per_paper", 3))
        min_width = int(self.cfg.get("min_width", 260))
        min_height = int(self.cfg.get("min_height", 180))

        seen = set()
        candidates: List[Dict[str, str]] = []

        for selector in self.FIGURE_SELECTORS:
            locator = page.locator(selector)
            try:
                count = min(locator.count(), max_images * 5)
            except PlaywrightError:
                continue

            for index in range(count):
                target = locator.nth(index)
                image = self._resolve_image_target(target)
                if image is None:
                    continue

                try:
                    meta = image.evaluate(
                        """node => {
                            const img = node.tagName === 'IMG' ? node : node.querySelector('img');
                            if (!img) return null;
                            const figure = img.closest('figure');
                            const figcaption = figure ? figure.querySelector('figcaption') : null;
                            return {
                                src: img.currentSrc || img.src || '',
                                alt: (img.alt || '').trim(),
                                caption: figcaption ? figcaption.innerText.trim() : '',
                                width: img.naturalWidth || img.clientWidth || 0,
                                height: img.naturalHeight || img.clientHeight || 0
                            };
                        }"""
                    )
                except PlaywrightError:
                    continue

                if not meta:
                    continue

                src = (meta.get("src") or "").strip()
                caption = (meta.get("caption") or meta.get("alt") or "").strip()
                width = int(meta.get("width") or 0)
                height = int(meta.get("height") or 0)
                key = src or f"{selector}:{index}"

                if key in seen:
                    continue
                if width < min_width or height < min_height:
                    continue
                if self._is_noise_image(src, caption):
                    continue
                seen.add(key)
                candidates.append(
                    {
                        "caption": caption[:400],
                        "source_url": src,
                        "image_locator": image,
                        "selector": selector,
                        "source_index": str(len(candidates) + 1),
                    }
                )

        selected = FigurePrioritizer.prioritize(candidates, limit=max_images)
        figures: List[Dict[str, str]] = []
        for saved, figure in enumerate(selected, start=1):
            image = figure.get("image_locator")
            if image is None:
                continue
            try:
                image.scroll_into_view_if_needed(timeout=3000)
                image_path = paper_dir / f"figure_{saved:02d}.png"
                image.screenshot(path=str(image_path))
            except PlaywrightError:
                continue

            rel_path = image_path.relative_to(self.run_dir).as_posix()
            figures.append(
                {
                    "path": rel_path,
                    "caption": str(figure.get("caption") or ""),
                    "source_url": str(figure.get("source_url") or ""),
                    "role_hint": str(figure.get("display_role") or figure.get("role_hint") or ""),
                    "role_hint_cn": str(figure.get("display_role_cn") or figure.get("role_hint_cn") or ""),
                    "selection_reason": str(figure.get("selection_reason") or ""),
                    "priority_rank": str(figure.get("priority_rank") or saved),
                    "source_index": str(figure.get("source_index") or ""),
                }
            )

        if figures:
            paper.figures = figures
        return len(figures)

    def _launch_context(self, pw):
        channel = self.cfg.get("channel", "chrome")
        executable_path = self.cfg.get("executable_path")
        headless = bool(self.cfg.get("headless", False))
        user_data_dir = self.cfg.get("user_data_dir")
        profile_directory = self.cfg.get("profile_directory", "Default")
        auto_close_existing = bool(self.cfg.get("auto_close_existing_profile", True))

        if user_data_dir:
            if auto_close_existing:
                close_profile_chrome_windows(str(user_data_dir))
            try:
                context = pw.chromium.launch_persistent_context(
                    user_data_dir=user_data_dir,
                    channel=None if executable_path else channel,
                    executable_path=executable_path,
                    headless=headless,
                    args=[f"--profile-directory={profile_directory}"],
                    viewport={"width": 1440, "height": 900},
                )
            except PlaywrightError as exc:
                raise RuntimeError(
                    "Failed to open the dedicated automation Chrome profile for "
                    "article media capture. Close any Chrome windows that are using "
                    "the same automation profile directory, then rerun the command."
                ) from exc
            return context, None

        browser = pw.chromium.launch(
            channel=None if executable_path else channel,
            executable_path=executable_path,
            headless=headless,
        )
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        return context, browser

    def _resolve_image_target(self, target):
        try:
            target.wait_for(state="visible", timeout=1500)
        except PlaywrightError:
            return None

        try:
            tag_name = target.evaluate("node => node.tagName")
        except PlaywrightError:
            return None

        if str(tag_name).upper() == "IMG":
            return target

        inner = target.locator("img").first
        try:
            if inner.count() > 0:
                inner.wait_for(state="visible", timeout=1500)
                return inner
        except PlaywrightError:
            return None
        return None

    @staticmethod
    def _is_noise_image(src: str, caption: str) -> bool:
        text = f"{src} {caption}".lower()
        patterns = [
            "logo",
            "icon",
            "avatar",
            "author",
            "orcid",
            "award",
            "banner",
            "cover",
            "advert",
            "social",
        ]
        return any(p in text for p in patterns)

    @staticmethod
    def _is_blocked(page) -> bool:
        try:
            text = (page.locator("body").inner_text(timeout=2000) or "").lower()
        except PlaywrightError:
            return False
        blocked_patterns = [
            "are you a robot",
            "captcha challenge",
            "problem providing the content",
            "please confirm you are a human",
        ]
        return any(pattern in text for pattern in blocked_patterns)

    def _wait_until_unblocked(self, page, wait_sec: int) -> bool:
        deadline = time.time() + wait_sec
        while time.time() < deadline:
            try:
                page.wait_for_load_state("networkidle", timeout=3000)
            except PlaywrightTimeoutError:
                pass
            except PlaywrightError:
                return False
            if not self._is_blocked(page):
                print("[Media] challenge cleared, continue collecting figures")
                return True
            time.sleep(2.0)
        return False

    @staticmethod
    def _safe_name(text: str) -> str:
        text = re.sub(r"[^A-Za-z0-9._-]+", "_", text)
        return text.strip("_") or "paper"
