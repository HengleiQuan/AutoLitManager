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


class ChatGPTWebAgent:
    INPUT_SELECTORS = [
        "#prompt-textarea",
        "textarea[data-testid='prompt-textarea']",
        "textarea[placeholder*='Message']",
        "textarea[placeholder*='Ask']",
        "div[contenteditable='true'][role='textbox']",
        "textarea",
    ]
    SEND_SELECTORS = [
        "button[data-testid='send-button']",
        "button[aria-label*='Send message']",
        "button[aria-label*='Send prompt']",
        "button[aria-label*='Send']",
        "button[type='submit']",
        "button:has-text('Send')",
    ]
    STOP_SELECTORS = [
        "button[data-testid='stop-button']",
        "button[aria-label*='Stop']",
        "button:has-text('Stop generating')",
        "button:has-text('Stop')",
    ]
    FILE_INPUT_SELECTORS = [
        "input[type='file']",
    ]
    UPLOAD_PENDING_SELECTORS = [
        "[role='progressbar']",
        "progress",
        "[data-testid*='progress']",
        "[data-testid*='upload']",
        "[aria-busy='true']",
    ]
    UPLOAD_TRIGGER_SELECTORS = [
        "button[aria-label*='Attach']",
        "button[aria-label*='Upload']",
        "button[aria-label*='Add photos']",
        "button:has-text('Attach')",
        "button:has-text('Upload')",
    ]
    MODEL_TRIGGER_SELECTORS = [
        "button[data-testid='model-switcher-dropdown-button']",
        "button[aria-label*='Model']",
        "button[aria-label*='model']",
        "button:has-text('GPT')",
        "button:has-text('Thinking')",
    ]
    LOGIN_SELECTORS = [
        "button:has-text('Log in')",
        "a:has-text('Log in')",
        "button:has-text('Sign up')",
        "a:has-text('Sign up')",
        "button:has-text('登录')",
        "a:has-text('登录')",
    ]
    NEW_CHAT_SELECTORS = [
        "button[data-testid='new-chat-button']",
        "a:has-text('New chat')",
        "button:has-text('New chat')",
        "a[href='/']",
        "button[aria-label*='New chat']",
    ]
    TEMPORARY_TOGGLE_SELECTORS = [
        "button:has-text('Temporary')",
        "[role='button']:has-text('Temporary')",
        "[aria-label*='Temporary']",
        "[data-testid*='temporary']",
    ]
    TEMPORARY_ACTIVE_SELECTORS = [
        "text=Temporary Chat",
        "button[aria-pressed='true']:has-text('Temporary')",
        "[data-testid*='temporary'][aria-pressed='true']",
    ]
    ASSISTANT_TEXT_SELECTORS = [
        "[data-message-author-role='assistant']",
        "article [data-message-author-role='assistant']",
        "main [data-message-author-role='assistant']",
        "article[data-testid^='conversation-turn-']",
    ]

    def __init__(self, cfg: dict, run_dir: Path):
        self.cfg = cfg or {}
        self.run_dir = run_dir
        self.logs_dir = run_dir / "chatgpt_web_logs"
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
                        "ChatGPT automation profile is not logged in. "
                        "Run with --prepare-chatgpt-login and complete your ChatGPT Plus login "
                        "in the dedicated automation profile first."
                    )
                input_selector = self._wait_for_input(page)
                if not input_selector:
                    raise RuntimeError("ChatGPT input box not found after login wait.")

                self._ensure_preferred_model(page)
                self._ensure_history_mode(page)
                if bool(self.cfg.get("start_new_chat_on_run_start", True)):
                    self._start_new_chat(page)
                    self._ensure_preferred_model(page)
                    self._ensure_history_mode(page)
                    input_selector = self._wait_for_input(page) or input_selector

                for index, batch in enumerate(batches, start=1):
                    batch_id = f"B{index:02d}_{uuid.uuid4().hex[:6].upper()}"
                    batch_titles = " | ".join(paper.title for paper in batch[:2])
                    if len(batch) > 2:
                        batch_titles += " | ..."
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
                            if bool(self.cfg.get("start_new_chat_each_batch", True)):
                                self._start_new_chat(page)
                                self._ensure_preferred_model(page)
                                self._ensure_history_mode(page)
                                input_selector = self._wait_for_input(page) or input_selector

                            image_paths = self._collect_batch_images(batch)
                            if image_paths and attempt == 0:
                                image_batches += 1
                            if image_paths:
                                self._upload_images(page, image_paths)

                            baseline_count = self._body_count(page, done_marker)
                            print(
                                f"[ChatGPT] submitting {index}/{len(batches)} "
                                f"({len(batch)} paper) {batch_titles}"
                            )
                            self._send_prompt(
                                page,
                                prompt,
                                input_selector,
                                expected_attachments=len(image_paths),
                            )
                            expected_uids = {paper.uid() for paper in batch}
                            body_text = self._wait_for_batch_done(
                                page,
                                done_marker,
                                baseline_count + 1,
                                begin_marker=begin_marker,
                                end_marker=end_marker,
                                expected_uids=expected_uids,
                            )
                            valid_records = self._extract_valid_records_from_text(
                                batch,
                                body_text,
                                begin_marker=begin_marker,
                                end_marker=end_marker,
                            )

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
                            print(
                                f"[ChatGPT] received {index}/{len(batches)} "
                                f"records={len(valid_records)}"
                            )
                            batch_success += 1
                            batch_done = True
                            time.sleep(float(self.cfg.get("between_batches_sec", 2)))
                            break
                        except TimeoutError as exc:
                            body_text = self._safe_body_text(page)
                            assistant_text = self._safe_last_assistant_text(page)
                            if body_text:
                                (self.logs_dir / f"response_{batch_id}_timeout_try{attempt + 1}.txt").write_text(
                                    body_text,
                                    encoding="utf-8",
                                )
                            if assistant_text:
                                (self.logs_dir / f"response_{batch_id}_assistant_timeout_try{attempt + 1}.txt").write_text(
                                    assistant_text,
                                    encoding="utf-8",
                                )

                            timeout_parse_text = assistant_text or body_text
                            if timeout_parse_text:
                                valid_records = self._extract_valid_records_from_text(
                                    batch,
                                    timeout_parse_text,
                                    begin_marker=begin_marker,
                                    end_marker=end_marker,
                                )
                                if len(valid_records) >= len(batch):
                                    (self.logs_dir / f"response_{batch_id}.json").write_text(
                                        json.dumps(valid_records, ensure_ascii=False, indent=2),
                                        encoding="utf-8",
                                    )
                                    papers_updated += self._apply_records(batch, valid_records)
                                    print(
                                        f"[ChatGPT] recovered after timeout {index}/{len(batches)} "
                                        f"records={len(valid_records)}"
                                    )
                                    batch_success += 1
                                    batch_done = True
                                    time.sleep(float(self.cfg.get("between_batches_sec", 2)))
                                    break

                            (self.logs_dir / f"batch_{batch_id}_error_try{attempt + 1}.txt").write_text(
                                str(exc),
                                encoding="utf-8",
                            )
                            if attempt < retries:
                                time.sleep(float(self.cfg.get("retry_backoff_sec", 20)))
                        except Exception as exc:
                            (self.logs_dir / f"batch_{batch_id}_error_try{attempt + 1}.txt").write_text(
                                str(exc),
                                encoding="utf-8",
                            )
                            if attempt < retries:
                                time.sleep(float(self.cfg.get("retry_backoff_sec", 20)))
                    if not batch_done:
                        batch_failed += 1
            finally:
                if not self.cfg.get("keep_browser_open", False):
                    self._close_context_safely(context)

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
                    print("[ChatGPT] input ready in configured profile")
                    time.sleep(3.0)
                    return

                wait_sec = int(self.cfg.get("manual_login_wait_sec", 240))
                print(
                    "[ChatGPT] login page opened. "
                    f"Complete login or upgrade checks within {wait_sec}s."
                )
                deadline = time.time() + wait_sec
                while time.time() < deadline:
                    selector = self._find_input_selector(page, 2)
                    if selector:
                        print("[ChatGPT] login detected, session ready")
                        time.sleep(3.0)
                        return
                raise RuntimeError("ChatGPT login session was not established in time.")
            finally:
                if not self.cfg.get("keep_browser_open", False):
                    self._close_context_safely(context)

    def _build_batches(self, papers: List[Paper]) -> List[List[Paper]]:
        if bool(self.cfg.get("one_paper_per_batch", False)):
            return [[paper] for paper in papers]

        text_batch_size = int(self.cfg.get("batch_size", 4))
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
            "[ChatGPT] Input box not ready. "
            f"Complete login or any challenge within {manual_wait}s."
        )
        return self._find_input_selector(page, manual_wait)

    def _open_chat_page(self, page) -> None:
        url = self.cfg.get("chat_url", "https://chatgpt.com/")
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

            if "chatgpt.com" not in current_url and "openai.com" not in current_url:
                try:
                    page.goto(url, wait_until="commit", timeout=min(15000, navigation_timeout_ms))
                except PlaywrightError as exc:
                    raise RuntimeError(f"Failed to open ChatGPT page: {exc}") from exc
        except PlaywrightError as exc:
            raise RuntimeError(f"Failed to open ChatGPT page: {exc}") from exc

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

        lowered = current_url.lower()
        if any(token in lowered for token in ("/auth/login", "/login", "auth.openai.com")):
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
        preferred = self._normalize_model_name(self.cfg.get("model_preference", "thinking"))
        fallback = self._normalize_model_name(
            self.cfg.get("model_fallback_preference", "gpt-5.3")
        )
        desired = [name for name in (preferred, fallback) if name and name != "auto"]
        if not desired:
            return

        current = self._get_current_model_name(page)
        if current == desired[0]:
            return

        for model_name in desired:
            if self._select_model(page, model_name):
                return

    def _ensure_history_mode(self, page) -> None:
        mode = str(self.cfg.get("history_mode", "temporary")).strip().lower()
        if mode in {"temporary", "temp", "ephemeral"}:
            self._enable_temporary_chat(page)

    def _enable_temporary_chat(self, page) -> bool:
        if self._temporary_chat_enabled(page):
            return True

        for selector in self.TEMPORARY_TOGGLE_SELECTORS:
            try:
                locator = page.locator(selector).first
                if locator.count() == 0:
                    continue
                if not locator.is_visible(timeout=800):
                    continue
                locator.click(timeout=2000)
                time.sleep(1.0)
                if self._temporary_chat_enabled(page):
                    return True
            except PlaywrightError:
                continue
        return False

    def _temporary_chat_enabled(self, page) -> bool:
        for selector in self.TEMPORARY_ACTIVE_SELECTORS:
            try:
                locator = page.locator(selector).first
                if locator.count() == 0:
                    continue
                if locator.is_visible(timeout=400):
                    return True
            except PlaywrightError:
                continue

        try:
            body_text = page.evaluate("() => document.body ? document.body.innerText : ''")
        except PlaywrightError:
            return False

        lowered = str(body_text or "").lower()
        return "temporary chat" in lowered

    @staticmethod
    def _normalize_model_name(value) -> str:
        text = str(value or "").strip().lower()
        mapping = {
            "auto": "auto",
            "default": "auto",
            "thinking": "thinking",
            "reasoning": "thinking",
            "advanced": "thinking",
            "advanced thinking": "thinking",
            "deep research": "thinking",
            "思考": "thinking",
            "进阶思考": "thinking",
            "深度思考": "thinking",
            "gpt-5": "gpt-5",
            "gpt5": "gpt-5",
            "gpt-5.3": "gpt-5.3",
            "gpt5.3": "gpt-5.3",
            "gpt-5.2": "gpt-5.2",
            "gpt5.2": "gpt-5.2",
            "gpt-4.1": "gpt-4.1",
            "gpt4.1": "gpt-4.1",
            "gpt-4o": "gpt-4o",
            "gpt4o": "gpt-4o",
        }
        return mapping.get(text, text)

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
            option.click(timeout=2500)
            time.sleep(1.2)
        except PlaywrightError:
            self._dismiss_overlays(page)
            return False

        current = self._get_current_model_name(page)
        if current == model_name:
            return True

        self._dismiss_overlays(page)
        return current == model_name

    def _find_model_option(self, page, model_name: str):
        label_map = {
            "thinking": [
                "GPT-5.3 Thinking",
                "Thinking",
                "GPT-5.2 Thinking",
                "GPT-5 Thinking",
                "Reasoning",
                "Advanced thinking",
                "思考",
                "进阶思考",
            ],
            "gpt-5.3": ["GPT-5.3", "GPT 5.3"],
            "gpt-5.2": ["GPT-5.2", "GPT 5.2"],
            "gpt-5": ["GPT-5", "GPT 5"],
            "gpt-4.1": ["GPT-4.1", "GPT 4.1"],
            "gpt-4o": ["GPT-4o", "GPT 4o"],
        }
        labels = label_map.get(model_name, [model_name])
        selectors = []
        for label in labels:
            selectors.extend(
                [
                    f"button:has-text('{label}')",
                    f"[role='option']:has-text('{label}')",
                    f"[role='menuitemradio']:has-text('{label}')",
                    f"[role='menuitem']:has-text('{label}')",
                    f"div:has-text('{label}')",
                ]
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
            time.sleep(float(self.cfg.get("upload_wait_sec", 3)))
            self._wait_for_uploads_ready(page)
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

    def _send_prompt(
        self,
        page,
        prompt: str,
        input_selector: str,
        *,
        expected_attachments: int = 0,
    ) -> None:
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

        baseline_input = self._read_input_text(page, input_selector)
        if expected_attachments > 0:
            self._wait_for_uploads_ready(page)

        attempts = max(1, int(self.cfg.get("send_dispatch_attempts", 3)))
        for _ in range(attempts):
            if self._click_send_button(page):
                if self._wait_for_prompt_dispatch(page, input_selector, baseline_input):
                    return
            self._dismiss_overlays(page)
            if expected_attachments > 0:
                self._wait_for_uploads_ready(page)

            try:
                page.keyboard.press("Enter")
            except PlaywrightError:
                try:
                    page.keyboard.press("Control+Enter")
                except PlaywrightError:
                    pass
            if self._wait_for_prompt_dispatch(page, input_selector, baseline_input):
                return

        raise RuntimeError(
            "Prompt dispatch not detected after send attempt. "
            "Image upload may still be pending or the message was not submitted."
        )

    def _click_send_button(self, page) -> bool:
        for selector in self.SEND_SELECTORS:
            try:
                button = page.locator(selector).first
                if button.count() > 0 and button.is_enabled():
                    button.click(force=True, timeout=2000)
                    time.sleep(0.8)
                    return True
            except PlaywrightError:
                continue
        return False

    def _wait_for_prompt_dispatch(self, page, input_selector: str, baseline_input: str) -> bool:
        deadline = time.time() + float(self.cfg.get("send_dispatch_timeout_sec", 20))
        baseline_norm = self._normalize_space(baseline_input)

        while time.time() < deadline:
            if self._is_generating(page):
                return True

            current_input = self._read_input_text(page, input_selector)
            current_norm = self._normalize_space(current_input)
            if not current_norm:
                return True

            if baseline_norm and current_norm != baseline_norm:
                if len(current_norm) <= max(32, len(baseline_norm) // 3):
                    return True

            time.sleep(0.5)
        return False

    def _wait_for_uploads_ready(self, page) -> bool:
        deadline = time.time() + float(self.cfg.get("upload_ready_timeout_sec", 45))
        settle_sec = float(self.cfg.get("upload_settle_sec", 2.5))
        ready_since = None

        while time.time() < deadline:
            if self._has_pending_uploads(page):
                ready_since = None
                time.sleep(0.8)
                continue

            if ready_since is None:
                ready_since = time.time()
            elif time.time() - ready_since >= settle_sec:
                return True
            time.sleep(0.5)

        return not self._has_pending_uploads(page)

    def _has_pending_uploads(self, page) -> bool:
        pending_markers = (
            "uploading",
            "upload failed",
            "preparing upload",
            "analyzing images",
            "上传中",
            "正在上传",
            "上传失败",
            "图片处理中",
        )
        for selector in self.UPLOAD_PENDING_SELECTORS:
            try:
                locator = page.locator(selector)
                count = locator.count()
                if count <= 0:
                    continue
                for idx in range(min(count, 8)):
                    item = locator.nth(idx)
                    if item.is_visible(timeout=150):
                        return True
            except PlaywrightError:
                continue

        try:
            body_text = self._safe_body_text(page).lower()
        except Exception:
            body_text = ""
        return any(marker in body_text for marker in pending_markers)

    def _read_input_text(self, page, input_selector: str) -> str:
        try:
            box = page.locator(input_selector).first
            if "textarea" in input_selector:
                return str(box.input_value(timeout=500) or "")
            return str(
                box.evaluate(
                    """(node) => {
                        if (!node) return '';
                        if ('value' in node && typeof node.value === 'string') return node.value;
                        return (node.innerText || node.textContent || '').trim();
                    }"""
                )
                or ""
            )
        except PlaywrightError:
            return ""

    @staticmethod
    def _normalize_space(text: str) -> str:
        return " ".join(str(text or "").split())

    def _dismiss_overlays(self, page) -> None:
        try:
            page.keyboard.press("Escape")
        except PlaywrightError:
            pass

        close_selectors = [
            "[data-radix-popper-content-wrapper]",
            "button[aria-label='Close']",
            "button:has-text('Close')",
            "button:has-text('Not now')",
            "button:has-text('Got it')",
        ]
        for selector in close_selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() == 0:
                    continue
                locator.click(force=True, timeout=1000)
            except PlaywrightError:
                continue

    def _wait_for_batch_done(
        self,
        page,
        done_marker: str,
        target_count: int,
        *,
        begin_marker: str = "",
        end_marker: str = "",
        expected_uids: Optional[set[str]] = None,
    ) -> str:
        deadline = time.time() + int(self.cfg.get("response_timeout_sec", 240))
        settle_sec = float(self.cfg.get("response_settle_sec", 20))
        min_wait_sec = float(self.cfg.get("response_min_wait_sec", 45))
        started_at = time.time()
        last_text = ""
        last_change_at = started_at

        while time.time() < deadline:
            body_text = self._safe_body_text(page)
            assistant_text = self._safe_last_assistant_text(page)
            observed_text = assistant_text or body_text
            if observed_text != last_text:
                last_text = observed_text
                last_change_at = time.time()

            # Do not treat static labels like "Reasoned for ..." or model names such as
            # "Thinking" as an active streaming signal. These often remain visible after the
            # answer is complete and would block progression forever.
            is_streaming = self._is_generating(page)
            has_structured_output = any(
                token and token in observed_text
                for token in (
                    begin_marker,
                    end_marker,
                    "```json",
                    '"uid"',
                    '"title_cn"',
                    '"gemini_summary_cn"',
                )
            )
            body_payload_ready = self._has_complete_payload(
                body_text,
                begin_marker=begin_marker,
                end_marker=end_marker,
                expected_uids=expected_uids or set(),
            )
            assistant_payload_ready = bool(assistant_text) and self._has_complete_payload(
                assistant_text,
                begin_marker=begin_marker,
                end_marker=end_marker,
                expected_uids=expected_uids or set(),
            )
            if (
                body_text.count(done_marker) >= target_count
                and (assistant_payload_ready or body_payload_ready)
                and not is_streaming
            ):
                return assistant_text if assistant_payload_ready else body_text
            if (
                last_text
                and time.time() - started_at >= min_wait_sec
                and time.time() - last_change_at >= settle_sec
                and not is_streaming
                and has_structured_output
                and (assistant_payload_ready or body_payload_ready)
            ):
                return assistant_text if assistant_payload_ready else body_text
            time.sleep(2.0)
        raise TimeoutError(f"ChatGPT response timeout waiting for marker {done_marker}.")

    def _has_complete_payload(
        self,
        body_text: str,
        *,
        begin_marker: str,
        end_marker: str,
        expected_uids: set[str],
    ) -> bool:
        if not expected_uids:
            return False

        marked_text = self._extract_marked_payload(body_text, begin_marker, end_marker)
        records = self._extract_json_payloads(marked_text)
        valid_records = self._filter_records_by_uids(records, expected_uids)
        return len(valid_records) >= len(expected_uids)

    def _filter_records_by_uids(self, records: List[dict], expected_uids: set[str]) -> List[dict]:
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

    def _is_generating(self, page) -> bool:
        for selector in self.STOP_SELECTORS:
            try:
                locator = page.locator(selector).first
                if locator.count() == 0:
                    continue
                if locator.is_visible(timeout=300):
                    return True
            except PlaywrightError:
                continue
        return False

    def _start_new_chat(self, page) -> None:
        for selector in self.NEW_CHAT_SELECTORS:
            try:
                control = page.locator(selector).first
                if control.count() == 0:
                    continue
                if not control.is_visible(timeout=800):
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
                "Treat this as a new standalone task and ignore previous tasks in this chat.",
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
            "Treat this as a new standalone task and ignore previous tasks in this chat.",
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
            "- Do not copy placeholder text from the schema such as `<...>`.",
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

    def _extract_valid_records_from_text(
        self,
        batch: List[Paper],
        body_text: str,
        *,
        begin_marker: str,
        end_marker: str,
    ) -> List[dict]:
        marked_text = self._extract_marked_payload(body_text, begin_marker, end_marker)
        records = self._extract_json_payloads(marked_text)
        return self._filter_valid_records(batch, records)

    def _extract_json_payloads(self, text: str) -> List[dict]:
        candidates = re.findall(r"```json\s*(.*?)\s*```", text, flags=re.I | re.S)
        candidates.extend(self._extract_balanced_json_fragments(text))
        if text.strip():
            candidates.append(text.strip())

        parsed_candidates: List[tuple[int, List[dict]]] = []
        for order, raw in enumerate(candidates):
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                records = [data]
            elif isinstance(data, list):
                records = [item for item in data if isinstance(item, dict)]
            else:
                continue
            if records:
                parsed_candidates.append((order, records))

        if not parsed_candidates:
            return []

        _, best_records = max(
            parsed_candidates,
            key=lambda item: (self._records_information_score(item[1]), item[0]),
        )
        return best_records

    def _extract_balanced_json_fragments(self, text: str) -> List[str]:
        sample = text[-20000:]
        starts = [idx for idx, ch in enumerate(sample) if ch in "[{"]
        fragments: List[str] = []

        for start in reversed(starts):
            fragment = self._extract_balanced_fragment(sample, start)
            if not fragment:
                continue
            fragment = fragment.strip()
            if len(fragment) < 2:
                continue
            fragments.append(fragment)
            if len(fragments) >= 40:
                break
        return fragments

    def _extract_balanced_fragment(self, text: str, start: int) -> str:
        stack: List[str] = []
        in_string = False
        escaping = False

        for idx in range(start, len(text)):
            ch = text[idx]

            if in_string:
                if escaping:
                    escaping = False
                elif ch == "\\":
                    escaping = True
                elif ch == '"':
                    in_string = False
                continue

            if ch == '"':
                in_string = True
                continue

            if ch in "[{":
                stack.append(ch)
                continue

            if ch in "]}":
                if not stack:
                    return ""
                last = stack.pop()
                if (last, ch) not in {("[", "]"), ("{", "}")}:
                    return ""
                if not stack:
                    return text[start : idx + 1]

        return ""

    def _filter_valid_records(self, batch: List[Paper], records: List[dict]) -> List[dict]:
        expected_uids = {paper.uid() for paper in batch}
        return self._filter_records_by_uids(records, expected_uids)

    def _records_information_score(self, records: List[dict]) -> int:
        return sum(self._record_information_score(record) for record in records)

    def _record_information_score(self, record: dict) -> int:
        score = 0
        weighted_fields = {
            "title_cn": 3,
            "one_line_summary_cn": 4,
            "gemini_summary_cn": 5,
            "visual_summary_cn": 3,
            "model_flow_summary_cn": 3,
            "interest_reason": 2,
            "process_flow_steps": 2,
            "key_figure_points": 2,
            "frontier_points": 2,
            "categories": 1,
            "methods": 1,
            "applications": 1,
        }
        for key, weight in weighted_fields.items():
            value = record.get(key)
            if isinstance(value, list):
                if any(str(item).strip() for item in value):
                    score += weight
            elif str(value or "").strip():
                score += weight

        if self._record_looks_placeholder(record):
            score -= 100
        return score

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

            frontier_points = record.get("frontier_points")
            if isinstance(frontier_points, list):
                paper.gemini_frontier_points = [
                    str(item).strip() for item in frontier_points if str(item).strip()
                ]

            key_figure_points = record.get("key_figure_points")
            if isinstance(key_figure_points, list):
                paper.key_figure_points = [
                    str(item).strip() for item in key_figure_points if str(item).strip()
                ]

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
    def _safe_body_text(page) -> str:
        try:
            return str(page.evaluate("() => document.body ? document.body.innerText : ''") or "")
        except PlaywrightError:
            return ""

    def _safe_last_assistant_text(self, page) -> str:
        try:
            return str(
                page.evaluate(
                    """(selectors) => {
                        const isVisible = (el) => {
                            if (!el) return false;
                            const style = window.getComputedStyle(el);
                            if (!style) return false;
                            if (style.display === 'none' || style.visibility === 'hidden') return false;
                            const rect = el.getBoundingClientRect();
                            return rect.width > 0 && rect.height > 0;
                        };

                        const texts = [];
                        for (const selector of selectors) {
                            const nodes = Array.from(document.querySelectorAll(selector));
                            for (const node of nodes) {
                                if (!isVisible(node)) continue;
                                const text = (node.innerText || '').trim();
                                if (!text) continue;
                                texts.push(text);
                            }
                        }
                        if (!texts.length) return '';

                        const interesting = texts.filter((text) =>
                            text.includes('title_cn') ||
                            text.includes('one_line_summary_cn') ||
                            text.includes('gemini_summary_cn') ||
                            text.includes('ALM_JSON_BEGIN_') ||
                            text.includes('ALM_BATCH_DONE_') ||
                            text.includes('"uid"')
                        );
                        const pool = interesting.length ? interesting : texts;
                        return pool[pool.length - 1];
                    }""",
                    self.ASSISTANT_TEXT_SELECTORS,
                )
                or ""
            )
        except PlaywrightError:
            return ""

    @staticmethod
    def _close_context_safely(context) -> None:
        try:
            context.close()
        except Exception:
            return

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
