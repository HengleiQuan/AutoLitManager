import json
import re
import time
import uuid
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from src.core.chrome_profile import close_profile_chrome_windows
from src.core.paper import Paper


class GeminiWebAgent:
    INPUT_SELECTORS = [
        "div[contenteditable='true'][role='textbox']",
        "div[contenteditable='true']",
        "textarea",
    ]
    SEND_SELECTORS = [
        "button[aria-label*='Send']",
        "button[aria-label*='发送']",
        "button.send-button",
        "button.submit",
        "button:has-text('Send')",
        "button:has-text('发送')",
        "button[type='submit']",
    ]
    FILE_INPUT_SELECTORS = [
        "input[type='file']",
    ]
    UPLOAD_TRIGGER_SELECTORS = [
        "button[aria-label*='打开文件上传菜单']",
        "button[aria-label*='上传']",
        "button[aria-label*='文件']",
        "button[aria-label*='Upload']",
        "button[aria-label*='Files']",
        "button[aria-label*='Add']",
        "button:has-text('Upload')",
        "button:has-text('Files')",
        "button:has-text('Add files')",
    ]
    MODEL_TRIGGER_SELECTORS = [
        "button[data-test-id='bard-mode-menu-button']",
        "button.input-area-switch",
        "button[aria-label='Open mode selector']",
        "button[aria-label='\u6253\u5f00\u6a21\u5f0f\u9009\u62e9\u5668']",
    ]
    LOGIN_SELECTORS = [
        "button:has-text('Sign in')",
        "a:has-text('Sign in')",
        "button:has-text('\u767b\u5f55')",
        "a:has-text('\u767b\u5f55')",
    ]

    def __init__(self, cfg: dict, run_dir: Path):
        self.cfg = cfg or {}
        self.run_dir = run_dir
        self.logs_dir = run_dir / "gemini_web_logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    def enrich(self, papers: List[Paper], focus_keywords: List[str]) -> Dict[str, int]:
        if not self.cfg.get("enabled", True):
            return self._stats(0, 0, 0, 0, 0, 0)

        batches = self._build_batches(papers)
        if not batches:
            return self._stats(1, 0, 0, 0, 0, 0)

        batch_success = 0
        batch_failed = 0
        papers_updated = 0
        image_batches = 0

        with sync_playwright() as pw:
            context = self._launch_context(pw)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                self._open_chat_page(page)
                if self._page_requires_login(page):
                    raise RuntimeError(
                        "Gemini automation profile is not logged in. "
                        "Run with --prepare-gemini-login and complete Google login "
                        "in the dedicated automation profile first."
                    )
                input_selector = self._wait_for_input(page)
                if not input_selector:
                    raise RuntimeError("Gemini input box not found after login wait.")

                self._ensure_preferred_model(page)

                for index, batch in enumerate(batches, start=1):
                    batch_id = f"B{index:02d}_{uuid.uuid4().hex[:6].upper()}"
                    prompt, begin_marker, end_marker, done_marker = self._build_prompt(
                        batch=batch,
                        batch_id=batch_id,
                        focus_keywords=focus_keywords,
                    )
                    (self.logs_dir / f"prompt_{batch_id}.md").write_text(
                        prompt,
                        encoding="utf-8",
                    )

                    batch_done = False
                    retries = max(0, int(self.cfg.get("retry_per_batch", 1)))
                    for attempt in range(retries + 1):
                        try:
                            self._start_new_chat(page)
                            self._ensure_preferred_model(page)
                            input_selector = self._wait_for_input(page) or input_selector

                            image_paths = self._collect_batch_images(batch)
                            if image_paths and attempt == 0:
                                image_batches += 1
                            if image_paths:
                                self._upload_images(page, image_paths)

                            baseline_count = self._body_count(page, done_marker)
                            self._send_prompt(page, prompt, input_selector)
                            body_text = self._wait_for_batch_done(page, done_marker, baseline_count + 2)
                            marked_text = self._extract_marked_payload(
                                body_text,
                                begin_marker,
                                end_marker,
                            )
                            records = self._extract_json_payloads(marked_text)
                            valid_records = self._filter_valid_records(batch, records)

                            if len(valid_records) < len(batch):
                                (self.logs_dir / f"response_{batch_id}_raw_try{attempt + 1}.txt").write_text(
                                    body_text,
                                    encoding="utf-8",
                                )
                                continue

                            (self.logs_dir / f"response_{batch_id}.json").write_text(
                                json.dumps(valid_records, ensure_ascii=False, indent=2),
                                encoding="utf-8",
                            )
                            papers_updated += self._apply_records(batch, valid_records)
                            batch_success += 1
                            batch_done = True
                            time.sleep(float(self.cfg.get("between_batches_sec", 2)))
                            break
                        except Exception as exc:
                            (self.logs_dir / f"batch_{batch_id}_error_try{attempt + 1}.txt").write_text(
                                str(exc),
                                encoding="utf-8",
                            )
                    if not batch_done:
                        batch_failed += 1
            finally:
                if not self.cfg.get("keep_browser_open", False):
                    context.close()

        return self._stats(
            1,
            len(batches),
            batch_success,
            batch_failed,
            papers_updated,
            image_batches,
        )

    def prepare_login_session(self) -> None:
        with sync_playwright() as pw:
            context = self._launch_context(pw)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                self._open_chat_page(page)
                self._click_login_if_present(page)
                selector = self._wait_for_input(page)
                if selector:
                    print("[Gemini] input ready in configured profile")
                    time.sleep(3.0)
                    return

                wait_sec = int(self.cfg.get("manual_login_wait_sec", 240))
                print(
                    "[Gemini] login page opened. "
                    f"Complete login or upgrade checks within {wait_sec}s."
                )
                deadline = time.time() + wait_sec
                while time.time() < deadline:
                    selector = self._find_input_selector(page, 2)
                    if selector:
                        print("[Gemini] login detected, session ready")
                        time.sleep(3.0)
                        return
                raise RuntimeError("Gemini login session was not established in time.")
            finally:
                if not self.cfg.get("keep_browser_open", False):
                    context.close()

    def _build_batches(self, papers: List[Paper]) -> List[List[Paper]]:
        text_batch_size = int(self.cfg.get("batch_size", 6))
        image_batch_size = int(self.cfg.get("batch_size_with_images", 1))
        use_images = bool(self.cfg.get("use_images", True))

        batches: List[List[Paper]] = []
        buffer: List[Paper] = []

        for paper in papers:
            if use_images and paper.figures:
                if buffer:
                    batches.extend(self._chunk(buffer, text_batch_size))
                    buffer = []
                batches.extend(self._chunk([paper], image_batch_size))
            else:
                buffer.append(paper)

        if buffer:
            batches.extend(self._chunk(buffer, text_batch_size))
        return batches

    def _launch_context(self, pw):
        user_data_dir = self.cfg.get("user_data_dir", "data/browser_profile")
        profile_directory = self.cfg.get("profile_directory", "Default")
        channel = self.cfg.get("channel", "chrome")
        executable_path = self.cfg.get("executable_path")
        headless = bool(self.cfg.get("headless", False))
        auto_close_existing = bool(self.cfg.get("auto_close_existing_profile", True))

        if auto_close_existing and user_data_dir:
            close_profile_chrome_windows(str(user_data_dir))

        try:
            return pw.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                channel=None if executable_path else channel,
                executable_path=executable_path,
                headless=headless,
                args=[f"--profile-directory={profile_directory}"],
                viewport={"width": 1440, "height": 900},
            )
        except PlaywrightError as exc:
            message = str(exc)
            if "launch_persistent_context" in message or "TargetClosedError" in message:
                raise RuntimeError(
                    "Failed to open the dedicated automation Chrome profile. "
                    "Close any Chrome windows that are using the same automation "
                    "profile directory, then rerun the command."
                ) from exc
            if channel:
                raise RuntimeError(
                    f"Failed to launch browser channel `{channel}`."
                ) from exc
            raise

    def _wait_for_input(self, page) -> Optional[str]:
        first_wait = int(self.cfg.get("input_wait_timeout_sec", 45))
        selector = self._find_input_selector(page, first_wait)
        if selector:
            return selector

        if self._page_requires_login(page):
            return None

        manual_wait = int(self.cfg.get("manual_login_wait_sec", 240))
        print(
            "[Gemini] Input box not ready. "
            f"Complete login or captcha within {manual_wait}s."
        )
        return self._find_input_selector(page, manual_wait)

    def _open_chat_page(self, page) -> None:
        url = self.cfg.get("chat_url", "https://gemini.google.com/app")
        navigation_timeout_ms = int(self.cfg.get("navigation_timeout_sec", 90)) * 1000

        try:
            page.set_default_navigation_timeout(navigation_timeout_ms)
        except PlaywrightError:
            pass

        try:
            page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=navigation_timeout_ms,
            )
        except PlaywrightTimeoutError:
            current_url = ""
            try:
                current_url = page.url or ""
            except PlaywrightError:
                current_url = ""

            # Google properties often keep background requests open for a long time.
            # If we already landed on Gemini or Google Account pages, continue.
            if "gemini.google.com" not in current_url and "accounts.google.com" not in current_url:
                try:
                    page.goto(url, wait_until="commit", timeout=min(15000, navigation_timeout_ms))
                except PlaywrightError as exc:
                    raise RuntimeError(f"Failed to open Gemini page: {exc}") from exc
        except PlaywrightError as exc:
            raise RuntimeError(f"Failed to open Gemini page: {exc}") from exc

        try:
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        except PlaywrightError:
            pass

    def _find_input_selector(self, page, wait_seconds: int) -> Optional[str]:
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            if self._page_requires_login(page):
                return None
            for selector in self.INPUT_SELECTORS:
                try:
                    locator = page.locator(selector).first
                    if locator.count() == 0:
                        continue
                    locator.wait_for(state="visible", timeout=1200)
                    return selector
                except PlaywrightTimeoutError:
                    continue
                except PlaywrightError:
                    continue
            time.sleep(1.0)
        return None

    def _page_requires_login(self, page) -> bool:
        try:
            current_url = page.url or ""
        except PlaywrightError:
            current_url = ""

        if "accounts.google.com" in current_url:
            return True

        for selector in self.LOGIN_SELECTORS:
            try:
                locator = page.locator(selector).first
                if locator.count() == 0:
                    continue
                if locator.is_visible(timeout=500):
                    return True
            except PlaywrightError:
                continue

        return False

    def _click_login_if_present(self, page) -> None:
        for selector in self.LOGIN_SELECTORS:
            try:
                locator = page.locator(selector).first
                if locator.count() == 0:
                    continue
                locator.click(timeout=2000)
                time.sleep(2.0)
                return
            except PlaywrightError:
                continue

    def _ensure_preferred_model(self, page) -> None:
        preferred = self._normalize_model_name(self.cfg.get("model_preference", "pro"))
        fallback = self._normalize_model_name(
            self.cfg.get("model_fallback_preference", "thinking")
        )
        desired = [name for name in (preferred, fallback) if name]
        if not desired:
            return

        current = self._get_current_model_name(page)
        if current == desired[0]:
            return

        for model_name in desired:
            if self._select_model(page, model_name):
                return

    def _normalize_model_name(self, value) -> str:
        text = str(value or "").strip().lower()
        mapping = {
            "pro": "pro",
            "thinking": "thinking",
            "think": "thinking",
            "\u601d\u8003": "thinking",
            "quick": "quick",
            "flash": "quick",
            "\u5feb\u901f": "quick",
        }
        return mapping.get(text, "")

    def _get_current_model_name(self, page) -> str:
        trigger = self._find_model_trigger(page)
        if trigger is None:
            return ""

        try:
            text = (trigger.inner_text(timeout=1000) or "").strip()
        except PlaywrightError:
            return ""
        return self._normalize_model_name(text)

    def _find_model_trigger(self, page):
        for selector in self.MODEL_TRIGGER_SELECTORS:
            try:
                locator = page.locator(selector).first
                if locator.count() == 0:
                    continue
                if locator.is_visible(timeout=800):
                    return locator
            except PlaywrightError:
                continue
        return None

    def _select_model(self, page, model_name: str) -> bool:
        current = self._get_current_model_name(page)
        if current == model_name:
            return True

        trigger = self._find_model_trigger(page)
        if trigger is None:
            return False

        try:
            trigger.click(timeout=2000)
            time.sleep(0.8)
        except PlaywrightError:
            return False

        option = self._find_model_option(page, model_name)
        if option is None:
            self._dismiss_overlays(page)
            return False

        try:
            checked = str(option.get_attribute("aria-checked") or "").lower() == "true"
        except PlaywrightError:
            checked = False
        if checked:
            self._dismiss_overlays(page)
            return True

        try:
            if option.is_disabled():
                self._dismiss_overlays(page)
                return False
        except PlaywrightError:
            pass

        try:
            option.click(timeout=2500)
            time.sleep(1.2)
        except PlaywrightError:
            self._dismiss_overlays(page)
            return False

        current = self._get_current_model_name(page)
        if current == model_name:
            return True

        # Some menu interactions keep the panel open; double-check via aria-checked.
        try:
            trigger = self._find_model_trigger(page)
            if trigger is not None:
                trigger.click(timeout=1500)
                time.sleep(0.6)
            option = self._find_model_option(page, model_name)
            if option is not None:
                checked = str(option.get_attribute("aria-checked") or "").lower() == "true"
                self._dismiss_overlays(page)
                return checked
        except PlaywrightError:
            pass
        return False

    def _find_model_option(self, page, model_name: str):
        label_map = {
            "pro": ["Pro", "PRO"],
            "thinking": ["\u601d\u8003", "Thinking"],
            "quick": ["\u5feb\u901f", "Quick", "Flash"],
        }
        labels = label_map.get(model_name, [])
        selectors = [
            f"button.bard-mode-list-button:has-text('{label}')"
            for label in labels
        ]
        selectors.extend(
            f"button:has-text('{label}')"
            for label in labels
        )

        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() == 0:
                    continue
                if locator.is_visible(timeout=800):
                    return locator
            except PlaywrightError:
                continue
        return None

    def _collect_batch_images(self, batch: Iterable[Paper]) -> List[str]:
        max_total = int(self.cfg.get("max_images_per_batch", 6))
        paths: List[str] = []
        for paper in batch:
            for figure in paper.figures:
                path = (figure.get("path") or "").strip()
                if not path:
                    continue
                full = (self.run_dir / path).resolve()
                if full.exists():
                    paths.append(str(full))
                if len(paths) >= max_total:
                    return paths
        return paths

    def _upload_images(self, page, paths: List[str]) -> None:
        if not paths:
            return
        file_input = self._find_file_input(page)
        if file_input is None:
            return
        try:
            file_input.set_input_files(paths, timeout=5000)
            time.sleep(float(self.cfg.get("upload_wait_sec", 2)))
        except PlaywrightError:
            return

    def _find_file_input(self, page):
        for selector in self.FILE_INPUT_SELECTORS:
            try:
                locator = page.locator(selector).first
                if locator.count() > 0:
                    return locator
            except PlaywrightError:
                continue

        for selector in self.UPLOAD_TRIGGER_SELECTORS:
            try:
                button = page.locator(selector).first
                if button.count() == 0:
                    continue
                button.click(timeout=1500)
                time.sleep(1.0)
            except PlaywrightError:
                continue

            for input_selector in self.FILE_INPUT_SELECTORS:
                try:
                    locator = page.locator(input_selector).first
                    if locator.count() > 0:
                        return locator
                except PlaywrightError:
                    continue
        return None

    def _send_prompt(self, page, prompt: str, input_selector: str) -> None:
        self._dismiss_overlays(page)
        box = page.locator(input_selector).first
        try:
            box.click(force=True, timeout=3000)
        except PlaywrightError:
            pass

        try:
            if "textarea" in input_selector:
                box.fill(prompt)
            else:
                box.evaluate(
                    """(node, value) => {
                        node.focus();
                        if ('value' in node) {
                            node.value = value;
                            node.dispatchEvent(new Event('input', { bubbles: true }));
                            return;
                        }
                        node.innerHTML = '';
                        const lines = value.split('\\n');
                        lines.forEach((line, index) => {
                            if (index > 0) {
                                node.appendChild(document.createElement('br'));
                            }
                            node.appendChild(document.createTextNode(line));
                        });
                        node.dispatchEvent(new InputEvent('input', { bubbles: true, data: value }));
                    }""",
                    prompt,
                )
        except PlaywrightError:
            self._dismiss_overlays(page)
            try:
                box.click(force=True, timeout=3000)
            except PlaywrightError:
                pass
            page.keyboard.insert_text(prompt)

        time.sleep(0.8)
        for selector in self.SEND_SELECTORS:
            try:
                button = page.locator(selector).first
                if button.count() > 0 and button.is_enabled():
                    button.click(force=True, timeout=2000)
                    time.sleep(1.0)
                    return
            except PlaywrightError:
                continue

        try:
            page.keyboard.press("Control+Enter")
        except PlaywrightError:
            page.keyboard.press("Enter")

    def _dismiss_overlays(self, page) -> None:
        try:
            page.keyboard.press("Escape")
        except PlaywrightError:
            pass

        close_selectors = [
            ".cdk-overlay-backdrop-showing",
            "button[aria-label='Close']",
            "button:has-text('Close')",
            "button:has-text('Got it')",
            "button:has-text('Not now')",
        ]
        for selector in close_selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() == 0:
                    continue
                locator.click(force=True, timeout=1000)
            except PlaywrightError:
                continue

    def _wait_for_batch_done(self, page, done_marker: str, target_count: int) -> str:
        deadline = time.time() + int(self.cfg.get("response_timeout_sec", 240))
        settle_sec = float(self.cfg.get("response_settle_sec", 8))
        min_wait_sec = float(self.cfg.get("response_min_wait_sec", 12))
        started_at = time.time()
        last_text = ""
        last_change_at = started_at
        while time.time() < deadline:
            body_text = page.evaluate("() => document.body ? document.body.innerText : ''")
            if body_text != last_text:
                last_text = body_text
                last_change_at = time.time()

            if body_text.count(done_marker) >= target_count:
                return body_text

            is_streaming = any(
                marker in body_text
                for marker in (
                    "Gemini 正在输入",
                    "Gemini is typing",
                    "正在生成",
                    "Generating",
                    "Stop generating",
                    "停止生成",
                )
            )
            if (
                last_text
                and time.time() - started_at >= min_wait_sec
                and time.time() - last_change_at >= settle_sec
                and not is_streaming
            ):
                return last_text
            time.sleep(2.0)
        raise TimeoutError(f"Gemini response timeout waiting for marker {done_marker}.")

    def _start_new_chat(self, page) -> None:
        selectors = [
            "a:has-text('发起新对话')",
            "button:has-text('发起新对话')",
            "text=发起新对话",
            "a:has-text('新对话')",
            "button:has-text('新对话')",
            "button:has-text('New chat')",
            "button:has-text('新 Gemini 对话')",
            "a:has-text('New chat')",
            "a:has-text('新 Gemini 对话')",
        ]
        for selector in selectors:
            try:
                control = page.locator(selector).first
                if control.count() == 0:
                    continue
                control.click(force=True, timeout=2500)
                time.sleep(2.0)
                self._dismiss_overlays(page)
                return
            except PlaywrightError:
                continue

    def _build_prompt(
        self,
        batch: Iterable[Paper],
        batch_id: str,
        focus_keywords: List[str],
    ) -> Tuple[str, str, str, str]:
        begin_marker = f"ALM_JSON_BEGIN_{batch_id}"
        end_marker = f"ALM_JSON_END_{batch_id}"
        done_marker = f"ALM_BATCH_DONE_{batch_id}"
        prompt_style = str(self.cfg.get("prompt_style", "full")).strip().lower()
        max_abstract_chars = int(self.cfg.get("max_abstract_chars", 5000))

        schema = {
            "uid": "<copy exact uid from input>",
            "title_cn": "<Chinese title>",
            "one_line_summary_cn": "<one-line Chinese summary>",
            "gemini_summary_cn": "<3-5 sentence Chinese summary>",
            "visual_summary_cn": "<Chinese summary of the main figures/models>",
            "model_flow_summary_cn": "<Chinese summary of the model or workflow>",
            "process_flow_steps": ["<step 1>", "<step 2>", "<step 3>"],
            "key_figure_points": ["<figure point 1>", "<figure point 2>"],
            "frontier_points": ["<frontier point 1>", "<frontier point 2>", "<frontier point 3>"],
            "interest_reason": "<why it matches the focus directions>",
            "categories": ["<category 1>", "<category 2>"],
            "methods": ["<method 1>", "<method 2>"],
            "applications": ["<application 1>", "<application 2>"],
        }

        records = []
        for paper in batch:
            records.append(
                {
                    "uid": paper.uid(),
                    "title": paper.title,
                    "journal": paper.journal,
                    "publish_date": paper.publish_date,
                    "issue": paper.issue,
                    "doi": paper.doi,
                    "abstract": (paper.abstract or "")[:max_abstract_chars],
                    "keywords_hit": paper.matched_keywords,
                    "current_categories": paper.categories,
                    "image_files": [Path(fig.get("path", "")).name for fig in paper.figures],
                    "image_captions": [
                        fig.get("caption", "")[:240] for fig in paper.figures
                    ],
                    "image_role_hints": [
                        {
                            "file": Path(fig.get("path", "")).name,
                            "role_hint": fig.get("role_hint_cn") or fig.get("role_hint") or "",
                            "reason": fig.get("selection_reason", "")[:120],
                        }
                        for fig in paper.figures
                    ],
                }
            )

        if prompt_style == "minimal":
            keys = [
                "uid",
                "title_cn",
                "one_line_summary_cn",
                "gemini_summary_cn",
                "model_flow_summary_cn",
                "process_flow_steps",
                "categories",
                "methods",
                "applications",
            ]
            lines = [
                "Please answer in Chinese and output compact valid JSON only.",
                f"Focus keywords: {', '.join(focus_keywords) if focus_keywords else 'None'}",
                "",
                "Required output format:",
                begin_marker,
                "[",
                "  { ... }",
                "]",
                end_marker,
                done_marker,
                "",
                "Rules:",
                f"- Return exactly {len(records)} object(s), one per input paper.",
                "- Use only these keys:",
                ", ".join(keys),
                "- `uid` must exactly equal the input `uid`.",
                "- All string values must be single-line Chinese text.",
                "- `process_flow_steps` should contain 2-3 short steps.",
                "- `categories`, `methods`, `applications` should each contain at most 2 short items.",
                "- No markdown fences, no explanations, no extra text.",
                "",
                "Input papers:",
                json.dumps(records, ensure_ascii=False, indent=2),
            ]
            return "\n".join(lines), begin_marker, end_marker, done_marker

        lines = [
            "Please analyze the following papers and answer in Chinese.",
            "If images are attached, use them to summarize the main model, diagram, workflow, or core visual evidence.",
            "The attached images have been pre-sorted with this preference: 1) abstract/overview image near the abstract when available, 2) model/method image, 3) main result/experiment image.",
            "If a role hint looks uncertain, correct it using the actual image content and caption before summarizing.",
            "When writing `visual_summary_cn`, prefer summarizing the images in that order.",
            f"Focus keywords: {', '.join(focus_keywords) if focus_keywords else 'None'}",
            "",
            "Strict output format:",
            f"1) First line must be {begin_marker}",
            "2) Then output one valid JSON array only",
            f"3) Then output {end_marker}",
            f"4) Final line must be {done_marker}",
            "",
            "Hard requirements:",
            f"- Return exactly {len(records)} JSON object(s), one object per input paper.",
            "- For each object, `uid` must exactly match one input `uid`.",
            "- Do not copy placeholder text from the schema such as `<...>`, `paper.uid()`, `中文标题`, `分类1`, `方法1`.",
            "- Do not explain the JSON. Do not add any prose before or after the required markers.",
            "",
            "Each JSON item must follow this schema:",
            "```json",
            json.dumps(schema, ensure_ascii=False, indent=2),
            "```",
            "",
            "Input papers:",
            "```json",
            json.dumps(records, ensure_ascii=False, indent=2),
            "```",
        ]
        return "\n".join(lines), begin_marker, end_marker, done_marker

    def _extract_marked_payload(self, body_text: str, begin_marker: str, end_marker: str) -> str:
        start = body_text.rfind(begin_marker)
        end = body_text.rfind(end_marker)
        if start != -1 and end != -1 and end > start:
            return body_text[start + len(begin_marker) : end].strip()
        return body_text

    def _extract_json_payloads(self, text: str) -> List[dict]:
        candidates = [text.strip()] if text.strip() else []
        candidates.extend(
            re.findall(r"```json\s*(.*?)\s*```", text, flags=re.I | re.S)
        )

        for raw in candidates:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return [data]
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict)]
        return []

    def _filter_valid_records(self, batch: List[Paper], records: List[dict]) -> List[dict]:
        expected_uids = {paper.uid() for paper in batch}
        valid: List[dict] = []
        seen = set()

        for record in records:
            uid = str(record.get("uid") or "").strip()
            if uid not in expected_uids or uid in seen:
                continue
            if self._record_looks_placeholder(record):
                continue
            valid.append(record)
            seen.add(uid)
        return valid

    @staticmethod
    def _record_looks_placeholder(record: dict) -> bool:
        placeholder_snippets = (
            "<copy exact uid from input>",
            "<Chinese title>",
            "<one-line Chinese summary>",
            "<3-5 sentence Chinese summary>",
            "<Chinese summary",
            "<step 1>",
            "<figure point 1>",
            "<frontier point 1>",
            "<why it matches the focus directions>",
            "<category 1>",
            "<method 1>",
            "<application 1>",
            "paper.uid()",
            "中文标题",
            "一句话摘要",
            "3-5句中文总结",
            "分类1",
            "方法1",
            "应用1",
        )

        fields = [
            str(record.get("uid") or ""),
            str(record.get("title_cn") or ""),
            str(record.get("one_line_summary_cn") or ""),
            str(record.get("gemini_summary_cn") or ""),
            str(record.get("visual_summary_cn") or ""),
            str(record.get("model_flow_summary_cn") or ""),
            str(record.get("interest_reason") or ""),
        ]
        for key in ("process_flow_steps", "key_figure_points", "frontier_points", "categories", "methods", "applications"):
            value = record.get(key)
            if isinstance(value, list):
                fields.extend(str(item) for item in value)

        merged = "\n".join(fields)
        return any(snippet in merged for snippet in placeholder_snippets)

    def _apply_records(self, batch: List[Paper], records: List[dict]) -> int:
        by_uid = {paper.uid(): paper for paper in batch}
        updated = 0

        for record in records:
            uid = str(record.get("uid") or "").strip()
            if uid not in by_uid:
                continue

            paper = by_uid[uid]
            paper.title_cn = record.get("title_cn") or paper.title_cn
            paper.one_line_summary_cn = record.get("one_line_summary_cn") or paper.one_line_summary_cn
            paper.gemini_summary_cn = record.get("gemini_summary_cn") or paper.gemini_summary_cn
            paper.visual_summary_cn = record.get("visual_summary_cn") or paper.visual_summary_cn
            paper.model_flow_summary_cn = (
                record.get("model_flow_summary_cn") or paper.model_flow_summary_cn
            )
            paper.gemini_interest_reason = (
                record.get("interest_reason") or paper.gemini_interest_reason
            )

            steps = record.get("process_flow_steps")
            if isinstance(steps, list):
                paper.process_flow_steps = [
                    str(item).strip() for item in steps if str(item).strip()
                ]

            for field in ("frontier_points", "key_figure_points"):
                values = record.get(field)
                if isinstance(values, list):
                    cleaned = [str(item).strip() for item in values if str(item).strip()]
                    setattr(paper, f"gemini_{field}" if field == "frontier_points" else field, cleaned)

            for field in ("categories", "methods", "applications"):
                values = record.get(field)
                if not isinstance(values, list):
                    continue
                merged = sorted(
                    set(getattr(paper, field) + [str(item).strip() for item in values if str(item).strip()])
                )
                setattr(paper, field, merged)

            updated += 1
        return updated

    @staticmethod
    def _chunk(items: List[Paper], size: int) -> List[List[Paper]]:
        size = max(1, size)
        return [items[index : index + size] for index in range(0, len(items), size)]

    @staticmethod
    def _body_contains(page, text: str) -> bool:
        try:
            return bool(
                page.evaluate(
                    "(needle) => document.body && document.body.innerText.includes(needle)",
                    text,
                )
            )
        except PlaywrightError:
            return False

    @staticmethod
    def _body_count(page, text: str) -> int:
        try:
            return int(
                page.evaluate(
                    """(needle) => {
                        const body = document.body ? document.body.innerText : '';
                        if (!needle || !body) return 0;
                        return body.split(needle).length - 1;
                    }""",
                    text,
                )
            )
        except PlaywrightError:
            return 0

    @staticmethod
    def _stats(
        enabled: int,
        batch_total: int,
        batch_success: int,
        batch_failed: int,
        papers_updated: int,
        image_batches: int,
    ) -> Dict[str, int]:
        return {
            "enabled": enabled,
            "batch_total": batch_total,
            "batch_success": batch_success,
            "batch_failed": batch_failed,
            "papers_updated": papers_updated,
            "image_batches": image_batches,
        }
