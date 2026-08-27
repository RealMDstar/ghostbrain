# =============================================================================
# Bean's Ghost Brain - Gemini Web -> OpenAI-compatible API  (FINAL PRODUCTION)
# =============================================================================
# One-file monolith that turns your Gemini web account(s) into a local
# OpenAI-compatible API, built on top of the finalized "Ghost Browser"
# receiver (Playwright persistent context) plus the unfinished features:
#   - Token Pool  : multi-profile (multi-account) workers + round-robin failover
#   - Bean's Brain: system prompt injection, tool-call bridging, session stability
#   - Model Routing: /v1/models + per-request model switching in the Gemini UI
#   - Web UI      : status, accounts, test chat, request inspector
#   - Robustness  : login/account/model change detection, rate-limit handling
#
# Run:
#   pip install fastapi uvicorn playwright
#   playwright install chromium
#   python Gemini_Ghost_Brain.py --port 8000 --workers 2
# Then open http://127.0.0.1:8000  (the UI) - first run: log into Google in
# the opened Chrome window(s). Point any OpenAI client at
# http://127.0.0.1:8000/v1  (e.g. Claude Code / Roo / Cline).
# =============================================================================

import sys
import os
import json
import time
import logging
import asyncio
import hashlib
import re
import secrets
import argparse
import fnmatch
import queue
import threading
import urllib.request
import urllib.error
from datetime import datetime
from typing import Optional, List, Dict, Tuple, AsyncGenerator

try:
    from fastapi import FastAPI, Request
    from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse
    from fastapi.middleware.cors import CORSMiddleware
    import uvicorn
    from playwright.async_api import async_playwright, BrowserContext, Page
except ImportError:
    print("[!] FATAL: Missing dependencies.")
    print("    Run: pip install fastapi uvicorn playwright")
    print("    Then run: playwright install chromium")
    sys.exit(1)

# windowed exe has no console: give uvicorn/logging a valid stdout/stderr
# (uvicorn's log formatter calls sys.stdout.isatty() and crashes on None)
if getattr(sys, "frozen", False):
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [Ghost-Brain] [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
HOST = os.environ.get("GHOST_HOST", "127.0.0.1")
PORT = int(os.environ.get("GHOST_PORT", "8000"))
WORKERS = int(os.environ.get("GHOST_WORKERS", "2"))
API_KEY = os.environ.get("GHOST_API_KEY", "")

# when packaged as an exe, keep data next to the exe and use the bundled
# playwright browsers from the extraction dir
def _app_base_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.getcwd()

BASE_DIR = _app_base_dir()
PROFILES_ROOT = os.path.join(BASE_DIR, "Gemini_Profiles")

if getattr(sys, "frozen", False):
    os.environ.setdefault(
        "PLAYWRIGHT_BROWSERS_PATH",
        os.path.join(getattr(sys, "_MEIPASS", ""), "browsers"),
    )
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
IDLE_TIMEOUT_S = int(os.environ.get("GHOST_IDLE_TIMEOUT", "120"))
RATE_LIMIT_COOLDOWN_S = int(os.environ.get("GHOST_COOLDOWN", "60"))
MAX_PROMPT_CHARS = int(os.environ.get("GHOST_MAX_PROMPT", "60000"))

os.makedirs(PROFILES_ROOT, exist_ok=True)

# --- single instance guard ------------------------------------------------
LOCK_FILE = os.path.join(BASE_DIR, "ghost_brain.lock")

def _pid_alive(pid: int) -> bool:
    try:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    except Exception:
        return False

def _acquire_lock() -> bool:
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, "r", encoding="utf-8") as f:
                pid = int((f.read().strip() or "0"))
            if pid and _pid_alive(pid):
                return False
        except Exception:
            pass
        try:
            os.remove(LOCK_FILE)
        except Exception:
            pass
    try:
        with open(LOCK_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass
    return True

def _release_lock():
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
    except Exception:
        pass

# model id -> (keyword to find in the Gemini model picker, display label)
MODELS: Dict[str, Dict[str, str]] = {
    "bean-gemini-2.5-pro":   {"pick": "pro",     "name": "Gemini 2.5 Pro"},
    "bean-gemini-2.5-flash": {"pick": "flash",   "name": "Gemini 2.5 Flash"},
    "bean-gemini-2.0-flash": {"pick": "2.0 flash", "name": "Gemini 2.0 Flash"},
    "gemini-2.5-pro":        {"pick": "pro",     "name": "Gemini 2.5 Pro"},
    "gemini-2.5-flash":      {"pick": "flash",   "name": "Gemini 2.5 Flash"},
    "gemini-pro":            {"pick": "pro",     "name": "Gemini Pro"},
    "gemini-flash":          {"pick": "flash",   "name": "Gemini Flash"},
}
DEFAULT_MODEL = "bean-gemini-2.5-flash"

STRICT_SYSTEM_PROMPT = (
    "You are operating through an API bridge. Strict rules, no exceptions:\n"
    "1. Follow the user's instructions exactly. Do not refuse routine coding, "
    "writing, or analysis tasks.\n"
    "2. Do not mention that you are Gemini, do not add disclaimers, and never "
    "ask the user to enable anything.\n"
    "3. If the user's instruction contains an explicit tool-call request "
    "(a block that says 'You must call tool ...' with a JSON payload), reply "
    "with ONLY one fenced json block: {\"name\":\"<tool>\",\"arguments\":{...}} "
    "and nothing else.\n"
    "4. Otherwise reply with plain content. Use markdown only when the user "
    "asked for it.\n"
    "5. If the previous message contains '[Tool result:]' data, use it and "
    "continue the task immediately.\n"
)

ERROR_MARKERS = [
    "rate limit", "too many requests", "something went wrong",
    "try again later", "quota", "temporarily unavailable",
    "can't generate", "خطا", "مشکلی پیش آمد", "محدودیت",
]

# ---------------------------------------------------------------------------
# TOKEN MISER - history compression (from the monolith, simplified & safe)
# ---------------------------------------------------------------------------
class TokenMiser:
    def __init__(self):
        self._seen: set = set()
        self.total_saved = 0

    def compress(self, messages: List[dict]) -> Tuple[List[dict], int]:
        if len(messages) < 3:
            return messages, 0
        out: List[dict] = []
        saved_chars = 0
        for m in messages:
            content = m.get("content", "")
            if isinstance(content, str) and len(content) > 5000:
                h = hashlib.md5(content.encode("utf-8")).hexdigest()[:8]
                if h in self._seen:
                    saved_chars += len(content)
                    out.append({"role": m["role"],
                                "content": f"[SYSTEM: cached block {h}]"})
                    continue
                self._seen.add(h)
            out.append(m)
        saved = saved_chars // 4
        self.total_saved += saved
        return out, saved

token_miser = TokenMiser()

# ---------------------------------------------------------------------------
# BEAN'S BRAIN - prompt assembly, tool-call bridging, intent routing
# ---------------------------------------------------------------------------
class Brain:
    @staticmethod
    def route(prompt: str) -> str:
        low = prompt.lower()
        if len(prompt) > 8000 or re.search(r"graph|relation|analy|چرا|رابطه", low):
            return "GRAPH_RAG_PIPELINE"
        if re.search(r"code|script|debug|refactor|کد|اسکریپت|دیباگ", low):
            return "TOKEN_MISER_PIPELINE"
        return "GHOST_DIRECT"

    @staticmethod
    def build_prompt(messages: List[dict]) -> str:
        """Translate OpenAI-style messages into one Gemini-web prompt."""
        parts: List[str] = [STRICT_SYSTEM_PROMPT]
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            if role == "system":
                parts.append(f"[System instruction]: {content}")
            elif role == "assistant":
                tc = m.get("tool_calls")
                if tc:
                    for call in tc:
                        args = call.get("function", {}).get("arguments", "{}")
                        name = call.get("function", {}).get("name", "tool")
                        parts.append(
                            "You must call tool now. Reply with ONLY one fenced "
                            f"json block: {{\"name\":\"{name}\",\"arguments\":"
                            f"{args}}}")
                elif content:
                    parts.append(f"[Assistant]: {content}")
            elif role == "tool":
                parts.append(f"[Tool result]: {content}")
            else:
                parts.append(f"[User]: {content}")
        return "\n\n".join(parts)

    @staticmethod
    def extract_tool_call(text: str) -> Optional[Dict]:
        """Find a tool-call json block in Gemini's answer."""
        for m in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```",
                             text, re.DOTALL):
            try:
                obj = json.loads(m.group(1))
                if isinstance(obj, dict) and "name" in obj \
                        and "arguments" in obj:
                    return obj
            except Exception:
                continue
        i = 0
        while True:
            start = text.find("{", i)
            if start < 0:
                return None
            depth = 0
            for j in range(start, len(text)):
                if text[j] == "{":
                    depth += 1
                elif text[j] == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            obj = json.loads(text[start:j + 1])
                        except Exception:
                            break
                        if isinstance(obj, dict) and "name" in obj \
                                and "arguments" in obj:
                            return obj
                        break
            i = start + 1

brain = Brain()

# ---------------------------------------------------------------------------
# GHOST WORKER - one persistent browser profile (one Google account)
# ---------------------------------------------------------------------------
class GhostWorker:
    def __init__(self, worker_id: int, profile_dir: str):
        self.worker_id = worker_id
        self.profile_dir = profile_dir
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.status = "offline"          # healthy|busy|rate_limited|needs_login|failed|offline
        self.failures = 0
        self.current_model: Optional[str] = None
        self.available_models: List[str] = []
        self.last_error: Optional[str] = None
        self._lock = asyncio.Lock()
        self._cooldown_until = 0.0

    # -- lifecycle ----------------------------------------------------------
    async def launch(self, pw):
        os.makedirs(self.profile_dir, exist_ok=True)
        logging.info(f"Waking up Ghost Worker #{self.worker_id} "
                     f"({os.path.basename(self.profile_dir)})...")
        kwargs = dict(
            user_data_dir=self.profile_dir,
            headless=False,
            viewport={"width": 1280, "height": 800},
            args=["--disable-blink-features=AutomationControlled"],
        )
        channel = os.environ.get("GHOST_CHANNEL", "chrome")
        if channel and channel != "chromium":
            kwargs["channel"] = channel   # real Chrome => Google allows login
        else:
            kwargs["user_agent"] = UA     # bundled Chromium fallback
        last_err: Optional[Exception] = None
        for attempt in (1, 2, 3):
            try:
                self.context = await pw.chromium.launch_persistent_context(**kwargs)
                last_err = None
                break
            except Exception as e:
                last_err = e
                logging.warning(f"Worker #{self.worker_id} launch attempt "
                                f"{attempt}/3 failed: {e}")
                await asyncio.sleep(3)
        if self.context is None and kwargs.get("channel"):
            # final fallback: bundled Chromium
            kwargs.pop("channel", None)
            kwargs["user_agent"] = UA
            logging.warning(f"Worker #{self.worker_id}: retrying with "
                            f"bundled Chromium...")
            try:
                self.context = await pw.chromium.launch_persistent_context(**kwargs)
                last_err = None
            except Exception as e2:
                last_err = e2
        if self.context is None:
            self.status = "failed"
            self.last_error = str(last_err)
            logging.error(f"Worker #{self.worker_id} failed to start: "
                          f"{last_err}")
            return
        self.page = self.context.pages[0] if self.context.pages \
            else await self.context.new_page()
        await self.page.goto("https://gemini.google.com/app")
        self.status = "healthy" if await self._logged_in() else "needs_login"
        try:
            await self.discover_models()
        except Exception:
            pass
        logging.info(f"Worker #{self.worker_id} ready (status={self.status}, "
                     f"models={len(self.available_models)}).")

    async def close(self):
        try:
            if self.context:
                await self.context.close()
        except Exception:
            pass
        self.status = "offline"

    # -- state checks -------------------------------------------------------
    async def _logged_in(self) -> bool:
        try:
            if "accounts.google.com" in self.page.url:
                return False
            sign_in = self.page.locator("button:has-text('Sign in'), "
                                        "button:has-text('ورود')").first
            return not await sign_in.is_visible(timeout=1500)
        except Exception:
            return False

    async def refresh_status(self):
        if self.status in ("offline",):
            return
        if time.time() < self._cooldown_until:
            self.status = "rate_limited"
            return
        ok = await self._logged_in()
        self.status = "healthy" if ok else "needs_login"

    # -- model switching ----------------------------------------------------
    async def _detect_model(self) -> Optional[str]:
        try:
            picker = self.page.locator(
                "button[aria-label*='mode picker'], "
                "button[aria-label*='model picker']").first
            label = (await picker.get_attribute("aria-label")) or ""
            return label.replace("Open mode picker, currently", "").strip() \
                or None
        except Exception:
            return None

    async def discover_models(self) -> List[str]:
        """Open the model picker and scrape the labels actually available
        on this account (no hardcoded list)."""
        try:
            picker = self.page.locator(
                "button[aria-label*='mode picker'], "
                "button[aria-label*='model picker']").first
            await picker.click(timeout=4000)
            await asyncio.sleep(0.6)
            options = self.page.locator(
                "[role='menuitem'], [role='option'], mat-option")
            count = await options.count()
            labels: List[str] = []
            for i in range(min(count, 80)):
                try:
                    txt = (await options.nth(i).inner_text(timeout=600)).strip()
                except Exception:
                    continue
                if not txt:
                    continue
                # the picker options carry a tagline + the real model name;
                # keep the line that looks like a model (contains a digit) or
                # a known thinking-mode, drop description lines like
                # "Fastest answers" / "Sign in for all models".
                lines = [l.strip() for l in txt.splitlines() if l.strip()]
                if not lines:
                    continue
                cand = next((l for l in reversed(lines)
                             if re.search(r"\d", l)), None)
                if cand is None:
                    cand = next((l for l in lines
                                 if "thinking" in l.lower()
                                 or "personalization" in l.lower()), None)
                if cand is None:
                    cand = lines[-1]
                low = cand.lower()
                if ("sign in" in low or "all models" in low
                        or "try the latest" in low
                        or len(cand) > 40):
                    continue
                if cand not in labels:
                    labels.append(cand)
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
            if labels:
                self.available_models = labels
            logging.info(f"Worker #{self.worker_id} models in account: "
                         f"{self.available_models}")
            return self.available_models
        except Exception as e:
            logging.warning(f"discover_models failed (worker "
                            f"#{self.worker_id}): {e}")
            return self.available_models or []

    async def switch_model(self, model_id: str) -> bool:
        if self.current_model == model_id:
            return True
        labels = self.available_models or await self.discover_models()
        if not labels:
            logging.warning(f"No model list for worker #{self.worker_id}; "
                            f"keeping current model.")
            return False

        def _slug(s: str) -> str:
            return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")

        target: Optional[str] = None
        if model_id in labels:
            target = model_id
        else:
            s = _slug(model_id)
            target = next((l for l in labels if _slug(l) == s), None)
            if target is None:
                spec = MODELS.get(model_id)
                kw = (spec or {}).get("pick")
                if kw:
                    target = next((l for l in labels if kw in l.lower()),
                                  None)
        if target is None:
            logging.warning(f"Model '{model_id}' not in account ({labels}); "
                            f"keeping current model.")
            return False
        try:
            picker = self.page.locator(
                "button[aria-label*='mode picker'], "
                "button[aria-label*='model picker']").first
            await picker.click(timeout=5000)
            await asyncio.sleep(0.6)
            options = self.page.locator(
                "[role='menuitem'], [role='option'], mat-option")
            count = await options.count()
            for i in range(min(count, 80)):
                opt = options.nth(i)
                try:
                    txt = (await opt.inner_text(timeout=800)).strip()
                except Exception:
                    continue
                if txt == target:
                    await opt.click(timeout=5000)
                    await asyncio.sleep(0.5)
                    try:
                        await self.page.keyboard.press("Escape")
                    except Exception:
                        pass
                    self.current_model = model_id
                    logging.info(f"Worker #{self.worker_id} switched to "
                                 f"'{target}' ({model_id})")
                    return True
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
            logging.warning(f"Option '{target}' not clickable; "
                            f"keeping current model.")
            return False
        except Exception as e:
            logging.warning(f"Model switch failed (worker "
                            f"#{self.worker_id}): {e}")
            return False

    # -- streaming chat -----------------------------------------------------
    async def _body_text(self) -> str:
        """Rendered page text (innerText) PLUS the raw textContent of the
        LAST <pre> block: collapsed code blocks in the Gemini UI hide their
        full text from innerText, and the newest code block is the current
        response's one."""
        try:
            text = await self.page.locator("body").inner_text(timeout=5000)
        except Exception:
            text = ""
        try:
            pres = self.page.locator("pre")
            n = await pres.count()
            if n:
                t = await pres.nth(n - 1).text_content(timeout=800)
                if t and t.strip():
                    text += "\n" + t
        except Exception:
            pass
        return text

    def _debug_dump(self, tag: str, *parts) -> None:
        """Optional diagnostics when GHOST_DEBUG_DUMP=1 (file next to the app)."""
        if not os.environ.get("GHOST_DEBUG_DUMP"):
            return
        try:
            with open(os.path.join(BASE_DIR, "stream_debug.log"),
                      "a", encoding="utf-8") as f:
                f.write(f"--- {tag} ---\n")
                for p in parts:
                    f.write(str(p) + "\n")
        except Exception:
            pass

    async def _stop_visible(self) -> bool:
        try:
            return await self.page.locator(
                "button[aria-label*='Stop' i], "
                "button[aria-label*='توقف']").first.is_visible(timeout=1000)
        except Exception:
            return False

    async def _find_editor(self):
        for sel in ("div[role='textbox']", "rich-textarea",
                    ".ql-editor", "div[contenteditable='true']"):
            try:
                loc = self.page.locator(sel).first
                if await loc.is_visible(timeout=1500):
                    return loc
            except Exception:
                continue
        return None

    @staticmethod
    def _norm_text(s: str) -> str:
        """Normalise DOM text so the prompt tail matches the rendered echo:
        strip bidi/zero-width control chars (RTL wraps Persian text) and
        collapse blank lines (innerText collapses \n\n\n to a single newline)."""
        if not s:
            return ""
        s = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]",
                   "", s)
        s = re.sub(r"[ \t]+\n", "\n", s)
        s = re.sub(r"\n{2,}", "\n", s)
        return s.strip()

    async def _response_text(self, prompt_tail: str) -> str:
        """Return the conversation text AFTER the last occurrence of the
        prompt tail (i.e. only the current response region, so old
        conversation content can never leak into the diff)."""
        try:
            body = await self._body_text()
        except Exception:
            return ""
        body = self._norm_text(body)
        for tail in (prompt_tail, prompt_tail[-60:], prompt_tail[-30:]):
            if not tail:
                continue
            tail = self._norm_text(tail)
            if not tail:
                continue
            idx = body.rfind(tail)
            if idx >= 0:
                return body[idx + len(tail):]
        return ""

    @staticmethod
    def _clean_noise(text: str) -> str:
        """Strip Gemini UI metadata (model chip label, disclaimers, status
        lines like 'Gemini said') from the captured response region."""
        frags = ("gemini is typing", "gemini is analysing",
                 "gemini is thinking", "gemini is ai and can make mistakes",
                 "check important info", "analyzing", "analysing",
                 "typing\u2026", "\u2026", "show code")
        lines = []
        for ln in text.splitlines():
            s = ln.strip()
            if not s:
                if lines and lines[-1] != "":
                    lines.append("")
                continue
            low = s.lower()
            if any(f in low for f in frags):
                continue
            if re.match(r"^(gemini\s+)?(said|replied|is)\b", low):
                continue
            # the model chip label that Gemini prints on the response card
            # (e.g. "Flash-Lite", "3.5 Flash-Lite", "Extended thinking")
            if re.match(r"^[\d.]*\s*(flash|pro|nano|thinking|extended|"
                        r"personalization)[\w-]*$", low):
                continue
            lines.append(s)
        return "\n".join(lines)

    async def stream_chat(self, prompt: str, model_id: str,
                          sse: Optional[dict] = None
                          ) -> AsyncGenerator[str, None]:
        """Yield SSE data lines. UI-agnostic: captures ONLY the region after
        the current prompt echo, filters Gemini placeholder text, and diffs
        append-only with a common-prefix fallback for re-renders."""
        async with self._lock:
            self.status = "busy"
            try:
                if not await self._logged_in():
                    self.status = "needs_login"
                    raise GhostError(
                        f"Worker #{self.worker_id} is not logged in. "
                        f"Sign in to Google in the open Chrome window "
                        f"and try again.",
                        status=503, code="needs_login",
                        type_="server_error")

                # Fresh navigation per request: restored tabs can have dead
                # React handlers (fill works but Enter never submits). A full
                # page load guarantees a properly initialised composer, exactly
                # like a manual open. Stateless app -> reload is harmless.
                try:
                    await self.page.goto(
                        "https://gemini.google.com/app",
                        wait_until="domcontentloaded", timeout=45000)
                except Exception:
                    pass
                await asyncio.sleep(3)

                if model_id:
                    await self.switch_model(model_id)
                # make sure no picker menu is left open over the editor
                try:
                    await self.page.keyboard.press("Escape")
                except Exception:
                    pass
                await asyncio.sleep(0.4)

                logging.info(f"Typing payload into Gemini DOM "
                             f"(Worker #{self.worker_id}, len={len(prompt)})...")
                editor = await self._find_editor()
                if editor is None:
                    # last resort: force a fresh conversation page, retry once
                    try:
                        await self.page.goto(
                            "https://gemini.google.com/app",
                            wait_until="domcontentloaded", timeout=45000)
                        await asyncio.sleep(3)
                        editor = await self._find_editor()
                    except Exception:
                        pass
                if editor is None:
                    raise GhostError(
                        f"Worker #{self.worker_id}: Gemini editor not found "
                        f"on the page.",
                        status=503, code="editor_not_found",
                        type_="server_error")

                # focus + fill + verify the text actually landed
                try:
                    await editor.click(timeout=3000)
                except Exception:
                    pass
                await asyncio.sleep(0.3)
                await editor.fill(prompt)
                await asyncio.sleep(0.3)
                try:
                    filled = await editor.inner_text()
                except Exception:
                    filled = ""
                tail40 = prompt.strip()[-40:]
                if tail40 and tail40 not in filled.replace("\n", " ") \
                        and tail40 not in filled:
                    await editor.fill(prompt)
                    await asyncio.sleep(0.3)

                prompt_tail = prompt.strip()[-80:]

                # submit: Enter first, then verify visibly
                async def _submitted() -> bool:
                    try:
                        if await self._stop_visible():
                            return True
                    except Exception:
                        pass
                    try:
                        return bool((await self._response_text(
                            prompt_tail)).strip())
                    except Exception:
                        return False

                await self.page.keyboard.press("Enter")
                await asyncio.sleep(1.2)
                started = False
                for _ in range(12):
                    if await _submitted():
                        started = True
                        break
                    await asyncio.sleep(0.25)
                if not started:
                    # explicit send button (enabled now that text is in)
                    try:
                        send_btn = self.page.locator(
                            "button[aria-label*='Send' i], "
                            "button[aria-label*='\u0627\u0631\u0633\u0627\u0644']").first
                        if await send_btn.is_visible(timeout=1500):
                            await send_btn.click()
                            await asyncio.sleep(1.2)
                    except Exception:
                        pass
                    for _ in range(10):
                        if await _submitted():
                            started = True
                            break
                        await asyncio.sleep(0.25)
                if not started:
                    # full reload + one retry (restored tabs sometimes have
                    # dead React handlers)
                    try:
                        await self.page.goto(
                            "https://gemini.google.com/app",
                            wait_until="domcontentloaded", timeout=45000)
                        await asyncio.sleep(3)
                        editor = await self._find_editor()
                        if editor is not None:
                            await editor.fill(prompt)
                            await asyncio.sleep(0.3)
                            await self.page.keyboard.press("Enter")
                            await asyncio.sleep(1.2)
                            for _ in range(12):
                                if await _submitted():
                                    started = True
                                    break
                                await asyncio.sleep(0.25)
                    except Exception:
                        pass
                if not started:
                    logging.warning(
                        f"Worker #{self.worker_id}: send did not visibly "
                        f"start; streaming empty result.")
                    if sse:
                        yield _chunk("", sse["model"], sse["created"],
                                     sse["id"])
                    else:
                        yield _chunk("")
                    return

                # stream only the current response region
                last_resp = ""
                last_grow = time.time()
                emitted = 0
                while True:
                    await asyncio.sleep(0.25)
                    generating = await self._stop_visible()
                    region = self._clean_noise(
                        await self._response_text(prompt_tail))
                    if region.strip():
                        if last_resp and region.startswith(last_resp):
                            delta = region[len(last_resp):]
                        elif last_resp:
                            cp = os.path.commonprefix([last_resp, region])
                            delta = region[len(cp):]
                        else:
                            delta = region
                        last_resp = region
                        if delta.strip():
                            emitted += 1
                            if sse:
                                yield _chunk(delta.replace("\n\n", "\n"),
                                             sse["model"], sse["created"],
                                             sse["id"])
                            else:
                                yield _chunk(delta.replace("\n\n", "\n"))
                        last_grow = time.time()
                    if not generating:
                        break
                    if time.time() - last_grow > IDLE_TIMEOUT_S:
                        logging.warning(f"Worker #{self.worker_id} stalled; "
                                        f"forcing stop.")
                        try:
                            await self.page.locator(
                                "button[aria-label*='Stop' i]").first.click()
                        except Exception:
                            pass
                        break

                # post-generation health checks
                if os.environ.get("GHOST_DEBUG_DUMP"):
                    body_now = await self._body_text()
                    region_now = ""
                    try:
                        region_now = await self._response_text(prompt_tail)
                    except Exception:
                        pass
                    self._debug_dump(
                        f"req tail={prompt_tail!r}",
                        f"tail_hex={prompt_tail.encode('utf-8', 'replace').hex()}",
                        f"url={self.page.url}",
                        f"body_len={len(body_now)} last_resp_len={len(last_resp or '')}",
                        f"BODY_RAW={body_now[:1500]!r}",
                        f"region_len={len(region_now or '')}",
                        f"region_tail={repr((region_now or '')[-400:])}",
                        f"last_resp_tail={repr((last_resp or '')[-300:])}",
                        f"emitted={emitted}")
                await self._check_post_health(last_resp)

                if emitted == 0 and last_resp.strip():
                    if sse:
                        yield _chunk(last_resp.replace("\n\n", "\n"),
                                     sse["model"], sse["created"], sse["id"])
                    else:
                        yield _chunk(last_resp.replace("\n\n", "\n"))
                    emitted = 1

                logging.info(f"Stream completed (Worker #{self.worker_id}, "
                             f"{emitted} chunks).")
            except GhostError:
                raise
            except Exception as e:
                logging.error(f"Ghost Browser error on worker "
                              f"#{self.worker_id}: {e}")
                self.failures += 1
                raise GhostError(
                    f"Ghost browser error on worker #{self.worker_id}: {e}",
                    status=500, code="ghost_browser_error",
                    type_="server_error")
            finally:
                if self.status == "busy":
                    self.status = "healthy"

    async def _check_post_health(self, last_text: str):
        # account changed / login lost?
        if not await self._logged_in():
            self.status = "needs_login"
            return
        # rate-limit / error banners?
        try:
            body_text = await self.page.locator("body").inner_text(timeout=3000)
        except Exception:
            return
        low = body_text.lower()
        if any(marker in low for marker in ERROR_MARKERS) and len(last_text) < 60:
            self.failures += 1
            self.status = "rate_limited"
            self._cooldown_until = time.time() + RATE_LIMIT_COOLDOWN_S
            logging.warning(f"Worker #{self.worker_id} hit a limit; "
                            f"cooldown {RATE_LIMIT_COOLDOWN_S}s.")

class GhostError(Exception):
    """App-level error that is rendered as a standard OpenAI error body:
    {"error": {"message", "type", "code"}} with a real HTTP status."""
    def __init__(self, message: str, status: int = 500,
                 code: str = "server_error", type_: str = "server_error"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.type = type_

    def response(self) -> JSONResponse:
        return JSONResponse({"error": {"message": self.message,
                                        "type": self.type,
                                        "code": self.code}},
                            status_code=self.status)


_chunk_counter = 0


def _chunk(content: str, model: str = "", created: int = 0,
           chunk_id: str = "") -> str:
    """One SSE data line with the full OpenAI chunk shape (id/object/created/
    model/choices[].index + delta.content) so strict SDKs (openai-python,
    OpenClaw providers) parse it."""
    global _chunk_counter
    _chunk_counter += 1
    if not chunk_id:
        chunk_id = f"chatcmpl-{int(time.time())}-{_chunk_counter}"
    data = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created or int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {"content": content},
                      "finish_reason": None}],
    }
    return f"data: {json.dumps(data)}\n\n"


def _finish_chunk(model: str, created: int, chunk_id: str,
                  reason: str = "stop") -> str:
    data = {"id": chunk_id, "object": "chat.completion.chunk",
            "created": created, "model": model,
            "choices": [{"index": 0, "delta": {},
                          "finish_reason": reason}]}
    return f"data: {json.dumps(data)}\n\n"


def _tool_calls_chunk(model: str, created: int, chunk_id: str,
                      name: str, arguments: str,
                      call_id: str = "call_ghost_1") -> str:
    data = {"id": chunk_id, "object": "chat.completion.chunk",
            "created": created, "model": model,
            "choices": [{"index": 0,
                          "delta": {"tool_calls": [
                              {"index": 0, "id": call_id,
                               "type": "function",
                               "function": {"name": name,
                                             "arguments": arguments}}]},
                          "finish_reason": "tool_calls"}]}
    return f"data: {json.dumps(data)}\n\n"


def _usage_chunk(model: str, created: int, chunk_id: str,
                 usage: dict) -> str:
    data = {"id": chunk_id, "object": "chat.completion.chunk",
            "created": created, "model": model, "choices": [],
            "usage": usage}
    return f"data: {json.dumps(data)}\n\n"


def _error_chunk(message: str) -> str:
    data = {"error": {"message": message, "type": "upstream_error",
                       "code": "upstream_error"}}
    return f"data: {json.dumps(data)}\n\n"

# ---------------------------------------------------------------------------
# TOKEN POOL - multi-account round-robin with failover
# ---------------------------------------------------------------------------
class WorkerPool:
    def __init__(self, count: int):
        self.count = count
        self.workers: List[GhostWorker] = []
        self.playwright = None
        self._rr = 0

    async def login_watch(self):
        """Periodically re-check login state so a worker that was
        needs_login at launch auto-flips to healthy after the user signs in
        (no restart needed)."""
        while True:
            await asyncio.sleep(45)
            for w in self.workers:
                if w.status in ("needs_login",) and w.page is not None:
                    try:
                        if await w._logged_in():
                            w.status = "healthy"
                            w.last_error = None
                            logging.info(f"Worker #{w.worker_id} re-logged "
                                         f"in; status -> healthy")
                            try:
                                await w.discover_models()
                            except Exception:
                                pass
                    except Exception:
                        pass

    async def initialize_all(self):
        self.playwright = await async_playwright().start()
        for i in range(1, self.count + 1):
            w = GhostWorker(i, os.path.join(PROFILES_ROOT, f"profile_{i}"))
            try:
                await w.launch(self.playwright)
            except Exception as e:
                w.status = "failed"
                w.last_error = str(e)
            self.workers.append(w)
        logging.info(f"Pool initialized: {len(self.workers)} workers, "
                     f"{sum(1 for w in self.workers if w.status == 'healthy')} "
                     f"healthy.")

    async def close_all(self):
        for w in self.workers:
            await w.close()
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception:
                pass

    def _pick(self) -> Optional[GhostWorker]:
        for _ in range(len(self.workers)):
            w = self.workers[self._rr % len(self.workers)]
            self._rr += 1
            if w.status == "healthy":
                return w
        for w in self.workers:
            if w.status in ("healthy", "busy", "needs_login"):
                return w
        return None

    async def get_worker(self) -> Optional[GhostWorker]:
        # refresh statuses cheaply, then round-robin
        for w in self.workers:
            await w.refresh_status()
        return self._pick()

    def status(self) -> List[dict]:
        return [{
            "id": w.worker_id,
            "profile": os.path.basename(w.profile_dir),
            "status": w.status,
            "failures": w.failures,
            "model": w.current_model,
            "models": w.available_models,
            "last_error": w.last_error,
            "cooldown_until": w._cooldown_until,
        } for w in self.workers]

    async def add_account(self) -> Optional[GhostWorker]:
        """Create a new profile and open its login window."""
        if not self.playwright:
            return None
        n = len(self.workers) + 1
        w = GhostWorker(n, os.path.join(PROFILES_ROOT, f"profile_{n}"))
        await w.launch(self.playwright)
        self.workers.append(w)
        return w

pool = WorkerPool(WORKERS)

# ---------------------------------------------------------------------------
# API LAYER
# ---------------------------------------------------------------------------
app = FastAPI(title="Bean's Ghost Brain - Gemini API Bridge")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


def _check_auth(request: Request) -> Optional[JSONResponse]:
    """Optional bearer auth: only enforced when GHOST_API_KEY is set."""
    if not API_KEY:
        return None
    auth = request.headers.get("Authorization", "")
    if auth == "Bearer " + API_KEY:
        return None
    return GhostError("Invalid API key.", status=401,
                      code="invalid_api_key",
                      type_="invalid_request_error").response()

# --- external model routing ----------------------------------------------
# ghost_routes.json (next to the app, or GHOST_ROUTES=<path>):
#   {"routes": [
#     {"match": "gpt-*", "base_url": "https://api.openai.com/v1",
#      "api_key": "sk-...", "models": ["gpt-4o"], "label": "OpenAI"},
#     {"match": "my-custom-llm", "base_url": "http://127.0.0.1:9999/v1",
#      "api_key": "", "models": ["my-custom-llm"], "label": "Local LLM"}
#   ]}
# Requests whose model id matches a route are forwarded to that external
# OpenAI-compatible endpoint (stream and non-stream both passthrough).

ROUTES_FILE = os.environ.get(
    "GHOST_ROUTES", os.path.join(BASE_DIR, "ghost_routes.json"))


def _load_routes() -> List[dict]:
    try:
        with open(ROUTES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        routes = data.get("routes", [])
        if not isinstance(routes, list):
            return []
        return [r for r in routes if isinstance(r, dict) and r.get("match")]
    except Exception:
        return []


def _route_for(model_id: str) -> Optional[dict]:
    for r in _load_routes():
        pat = r.get("match", "")
        if model_id == pat or fnmatch.fnmatch(model_id, pat):
            return r
    return None


def _external_models() -> List[dict]:
    out = []
    for r in _load_routes():
        label = r.get("label") or r.get("match") or "external"
        for m in (r.get("models") or []):
            if m and m not in [x["id"] for x in out]:
                out.append({"id": m, "object": "model",
                            "owned_by": "ghost-external:" + label,
                            "name": m})
    return out


async def _forward_external(route: dict, model_id: str,
                            messages: list, stream: bool,
                            body: dict):
    """Forward a chat request to an external OpenAI-compatible API.
    Extra OpenAI params (temperature, max_tokens, tools, ...) pass through;
    SSE is relayed as raw bytes; every path ends with data: [DONE]."""
    base = (route.get("base_url") or "").rstrip("/")
    if not base:
        return JSONResponse({"error": {"message":
                            "External route has no base_url."}},
                            status_code=502)
    url = base + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    key = route.get("api_key")
    if key:
        headers["Authorization"] = "Bearer " + key
    payload = {"model": model_id, "messages": messages, "stream": stream}
    for k in ("temperature", "top_p", "max_tokens", "max_completion_tokens",
              "stop", "tools", "tool_choice", "user", "n",
              "presence_penalty", "frequency_penalty", "seed",
              "stream_options", "response_format"):
        if k in body:
            payload[k] = body[k]

    def _mk_req():
        return urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers=headers)

    if not stream:
        def _blocking():
            try:
                with urllib.request.urlopen(_mk_req(), timeout=600) as r:
                    return r.status, r.read().decode("utf-8",
                                                     errors="replace")
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode("utf-8", errors="replace")
            except Exception as e:
                return 502, json.dumps({"error": {"message": str(e)}})

        status, resp_body = await asyncio.to_thread(_blocking)
        try:
            return JSONResponse(json.loads(resp_body), status_code=status)
        except Exception:
            return JSONResponse(
                {"error": {"message": resp_body[:500],
                            "type": "upstream_error",
                            "code": str(status)}},
                status_code=status if 400 <= status < 600 else 502)

    # streaming passthrough (raw bytes; UTF-8 safe across chunk boundaries)
    q: queue.Queue = queue.Queue()

    def _relay_worker():
        try:
            with urllib.request.urlopen(_mk_req(), timeout=600) as r:
                while True:
                    chunk = r.read(4096)
                    if not chunk:
                        break
                    q.put(chunk)
            q.put(None)
        except Exception as e:
            err = _error_chunk("External route failed: " + str(e))
            q.put(err.encode("utf-8"))
            q.put(None)

    threading.Thread(target=_relay_worker, daemon=True).start()

    async def gen():
        while True:
            item = await asyncio.to_thread(q.get)
            if item is None:
                break
            yield item
        yield b"data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


def _dynamic_models() -> List[dict]:
    """ALL models: external routes first, then the models actually present in
    the logged-in account(s) (read from the browser's model picker), then the
    static legacy aliases. No dead list is ever served."""
    out: List[dict] = []
    seen = set()
    for m in _external_models():
        if m["id"] not in seen:
            seen.add(m["id"])
            out.append(m)
    labels: List[str] = []
    for w in pool.workers:
        for m in (w.available_models or []):
            if m not in labels:
                labels.append(m)
    static_dash = {m.replace(".", "-") for m in MODELS}
    for lbl in labels:
        slug = re.sub(r"[^a-z0-9]+", "-", lbl.lower()).strip("-")
        if not slug or slug in seen or slug in static_dash:
            continue
        seen.add(slug)
        out.append({"id": slug, "object": "model",
                    "owned_by": "ghost-brain", "name": lbl})
    for mid, spec in MODELS.items():
        if mid not in seen:
            seen.add(mid)
            out.append({"id": mid, "object": "model",
                        "owned_by": "ghost-brain", "name": spec["name"]})
    return out

def _build_usage(prompt: str, answer: str) -> dict:
    return {"prompt_tokens": len(prompt) // 4,
            "completion_tokens": len(answer) // 4,
            "total_tokens": (len(prompt) + len(answer)) // 4}

@app.on_event("startup")
async def _startup():
    await pool.initialize_all()
    asyncio.create_task(pool.login_watch())
    print("\n" + "=" * 64)
    print("  GHOST BRAIN ONLINE  |  UI: http://%s:%d  |  API: /v1" % (HOST, PORT))
    print("  Workers: %d   Profiles: %s" % (len(pool.workers), PROFILES_ROOT))
    print("=" * 64 + "\n")

@app.on_event("shutdown")
async def _shutdown():
    await pool.close_all()

@app.get("/", response_class=HTMLResponse)
@app.get("/ui", response_class=HTMLResponse)
async def ui():
    return HTMLResponse(_ui_html())

@app.get("/health")
async def health():
    return {"status": "ok", "workers": len(pool.workers),
            "saved_tokens": token_miser.total_saved}

@app.get("/api/status")
async def api_status():
    return {"workers": pool.status(),
            "saved_tokens": token_miser.total_saved,
            "models": [m["id"] for m in _dynamic_models()],
            "model_options": _dynamic_models()}

@app.post("/api/accounts")
async def api_add_account():
    w = await pool.add_account()
    return {"ok": w is not None, "worker": w.worker_id if w else None}

@app.get("/v1/models")
async def list_models():
    return {"object": "list", "data": _dynamic_models()}

def _resolve_model(model_id: str) -> str:
    """Resolve a requested model id to one we can serve. External routes are
    handled before this; anything unknown here is a real 404 (no silent
    remap to another model)."""
    if _route_for(model_id):
        return model_id
    dyn = _dynamic_models()
    ids = [m["id"] for m in dyn]
    raw_labels = {l for w in pool.workers
                  for l in (w.available_models or [])}
    if model_id in ids or model_id in MODELS or model_id in raw_labels:
        return model_id
    raise GhostError(
        f"Model '{model_id}' not found. "
        f"See GET /v1/models for the available models.",
        status=404, code="model_not_found",
        type_="invalid_request_error")

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    try:
        body = await request.json()
    except Exception:
        return GhostError("Invalid JSON body.", status=400,
                          code="invalid_request_error",
                          type_="invalid_request_error").response()
    if not isinstance(body, dict) or not body.get("messages"):
        return GhostError("'messages' is required.", status=400,
                          code="invalid_request_error",
                          type_="invalid_request_error").response()
    messages = body["messages"]
    model_id = body.get("model") or DEFAULT_MODEL
    stream = body.get("stream", False)   # OpenAI spec default: false
    include_usage = bool((body.get("stream_options") or {}).get(
        "include_usage"))
    want_tools = bool(body.get("tools") or body.get("tool_choice"))

    auth_err = _check_auth(request)
    if auth_err:
        return auth_err

    route = _route_for(model_id)
    if route:
        logging.info(f"external route '{route.get('match')}' -> "
                     f"{route.get('base_url')} (model={model_id}, "
                     f"stream={stream})")
        return await _forward_external(route, model_id, messages, stream,
                                       body)

    try:
        model_id = _resolve_model(model_id)
    except GhostError as e:
        return e.response()

    compressed, saved = token_miser.compress(messages)
    prompt = brain.build_prompt(compressed)
    if len(prompt) > MAX_PROMPT_CHARS:
        mid = len(prompt) // 2
        prompt = (prompt[:mid] + "\n\n[SYSTEM: middle of the payload was "
                  "elided to fit the web client]\n\n" + prompt[-mid:])

    pipeline = brain.route(prompt)
    worker = await pool.get_worker()
    if worker is None:
        rl = all(w.status == "rate_limited" for w in pool.workers)
        return GhostError(
            "All workers are rate limited; try again later." if rl else
            "No worker available.",
            status=429 if rl else 503,
            code="rate_limit_exceeded" if rl else "no_worker",
            type_="rate_limit_error" if rl else "server_error").response()

    logging.info(f"request -> worker #{worker.worker_id} | "
                 f"pipeline={pipeline} | model={model_id} | saved={saved}")

    sse = {"id": f"chatcmpl-{int(time.time())}-{secrets.token_hex(3)}",
           "created": int(time.time()), "model": model_id}

    async def gen():
        answer = ""
        try:
            async for line in worker.stream_chat(prompt, model_id, sse):
                if line.startswith("data: ") and "[DONE]" not in line:
                    try:
                        answer += json.loads(line[6:])["choices"][0] \
                            ["delta"].get("content", "")
                    except Exception:
                        pass
                if want_tools:
                    continue   # buffer; decide content vs tool_calls at end
                yield line
            if want_tools:
                tool_call = brain.extract_tool_call(answer)
                if tool_call:
                    yield _tool_calls_chunk(
                        sse["model"], sse["created"], sse["id"],
                        tool_call.get("name", "tool"),
                        json.dumps(tool_call.get("arguments", {})))
                elif answer.strip():
                    yield _chunk(answer, sse["model"], sse["created"],
                                 sse["id"])
                yield _finish_chunk(sse["model"], sse["created"], sse["id"],
                                    "tool_calls" if tool_call else "stop")
            else:
                yield _finish_chunk(sse["model"], sse["created"], sse["id"],
                                    "stop")
            if include_usage:
                yield _usage_chunk(sse["model"], sse["created"], sse["id"],
                                   _build_usage(prompt, answer))
            yield "data: [DONE]\n\n"
        except GhostError as e:
            yield _error_chunk(str(e))
            yield "data: [DONE]\n\n"

    try:
        if not stream:
            chunks = []
            async for line in gen():
                if line.startswith("data: ") and "[DONE]" not in line:
                    try:
                        obj = json.loads(line[6:])
                        if "error" in obj:
                            raise GhostError(
                                obj["error"].get("message",
                                                  "upstream error"),
                                status=502)
                        chunks.append(obj["choices"][0]["delta"]
                                      .get("content", ""))
                    except GhostError:
                        raise
                    except Exception:
                        pass
            answer = "".join(chunks)
            tool_call = brain.extract_tool_call(answer)
            msg: dict = {"role": "assistant",
                         "content": "" if tool_call else answer}
            if tool_call:
                msg["tool_calls"] = [{
                    "id": "call_ghost_1", "type": "function",
                    "function": {"name": tool_call["name"],
                                  "arguments": json.dumps(
                                      tool_call.get("arguments", {}))}}]
            return JSONResponse({
                "id": sse["id"],
                "object": "chat.completion",
                "created": sse["created"],
                "model": model_id,
                "choices": [{"index": 0, "message": msg,
                              "finish_reason": "tool_calls" if tool_call
                              else "stop"}],
                "usage": _build_usage(prompt, answer),
            })
        return StreamingResponse(gen(), media_type="text/event-stream")
    except GhostError as e:
        return e.response()

# ---------------------------------------------------------------------------
# WEB UI (embedded, dark pastel, RTL Persian, no external assets)
# ---------------------------------------------------------------------------
UI_HTML = r"""<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ghost Brain</title>
<style>
  :root{--bg:#0b0c10;--panel:#151821;--card:#1c2130;--ink:#e6e9f0;
        --muted:#8b93a7;--accent:#66fcf1;--pink:#ff8fa3;--amber:#ffa502;
        --red:#ff4757;--green:#2ed573;--radius:14px}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--ink);font-family:"Segoe UI",Tahoma,sans-serif;
       min-height:100vh;padding:18px}
  h1{font-size:20px;font-weight:800;letter-spacing:.3px}
  h1 small{color:var(--muted);font-weight:400;font-size:12px;display:block;margin-top:2px}
  .row{display:flex;gap:12px;flex-wrap:wrap}
  .card{background:var(--panel);border:1px solid #232a3b;border-radius:var(--radius);
        padding:16px;margin-top:14px}
  .card h2{font-size:13px;color:var(--accent);margin-bottom:10px;font-weight:700}
  .pill{display:inline-block;padding:4px 10px;border-radius:99px;font-size:11px;
        font-weight:700;margin:2px 4px 2px 0;background:#232a3b}
  .pill.healthy{background:#123524;color:var(--green)}
  .pill.busy{background:#3a2f12;color:var(--amber)}
  .pill.rate_limited,.pill.offline{background:#3a1518;color:var(--red)}
  .pill.needs_login{background:#331a2b;color:var(--pink)}
  .btn{background:linear-gradient(135deg,#8e44ad,#6c3483);color:#fff;border:none;
       border-radius:10px;padding:10px 16px;font-weight:700;cursor:pointer;font-size:13px}
  .btn:hover{filter:brightness(1.15)}
  .btn.ghost{background:#232a3b;color:var(--ink)}
  select,input[type=text],textarea{background:#0f1220;color:var(--ink);border:1px solid #2a3247;
       border-radius:10px;padding:10px;font-size:13px;width:100%}
  textarea{min-height:70px;resize:vertical}
  label{font-size:11px;color:var(--muted);display:block;margin:8px 0 4px}
  #out{background:#0a0c14;border:1px solid #232a3b;border-radius:10px;padding:12px;
       min-height:120px;max-height:280px;overflow:auto;font-size:13px;white-space:pre-wrap;
       font-family:Consolas,monospace;color:#c9f2ef;margin-top:10px}
  table{width:100%;border-collapse:collapse;font-size:12px}
  th,td{text-align:right;padding:7px 8px;border-bottom:1px solid #232a3b}
  th{color:var(--muted);font-weight:700}
  .saved{color:var(--green);font-weight:800}
  .grid2{display:grid;grid-template-columns:1.4fr 1fr;gap:14px}
  @media(max-width:900px){.grid2{grid-template-columns:1fr}}
</style>
</head>
<body>
  <div class="row" style="justify-content:space-between;align-items:center">
    <h1>Ghost Brain <small>پل Gemini Web به API سازگار با OpenAI — پورت 8000</small></h1>
    <div>
      <button class="btn ghost" onclick="addAccount()">+ افزودن اکانت جدید</button>
      <button class="btn" onclick="refresh()">بروزرسانی</button>
    </div>
  </div>

  <div class="card">
    <h2>وضعیت استخر توکن (اکانت‌ها)</h2>
    <div id="workers">در حال بارگذاری...</div>
    <div style="margin-top:8px;font-size:12px;color:var(--muted)">
      توکن ذخیره‌شده: <span id="saved" class="saved">0</span>
    </div>
  </div>

  <div class="grid2">
    <div class="card">
      <h2>تست چت</h2>
      <label>مدل</label>
      <select id="model"></select>
      <label>پیام</label>
      <textarea id="msg" placeholder="مثلاً: یه تابع پایتون بنویس که فایل JSON رو بخونه..."></textarea>
      <div style="margin-top:10px"><button class="btn" onclick="send()">ارسال (Stream)</button></div>
      <div id="out"></div>
    </div>
    <div class="card">
      <h2>ترافیک و خط لوله</h2>
      <table id="traffic">
        <thead><tr><th>زمان</th><th>خط لوله</th><th>کارگر</th><th>وضعیت</th></tr></thead>
        <tbody></tbody>
      </table>
    </div>
  </div>

<script>
const $=id=>document.getElementById(id);
let traffic=[];
async function refresh(){
  try{
    const r=await fetch('/api/status'); const s=await r.json();
    $('workers').innerHTML=s.workers.map(w=>
      `<span class="pill ${w.status}">${w.profile} — ${w.status}${w.model?' · '+w.model:''}</span>`
    ).join('')||'—';
    $('saved').textContent=s.saved_tokens;
    const sel=$('model');
    if(sel.options.length===0 && s.model_options && s.model_options.length){
      s.model_options.forEach(m=>{
        const o=document.createElement('option');o.value=m.id;o.textContent=m.name;sel.add(o);
      });
    } else if(sel.options.length===0 && s.models && s.models.length){
      s.models.forEach(m=>{
        const o=document.createElement('option');o.value=m;o.textContent=m;sel.add(o);
      });
    }
  }catch(e){$('workers').textContent='خطا در ارتباط با سرور';}
}
async function addAccount(){
  await fetch('/api/accounts',{method:'POST'});
  alert('پروفایل جدید ساخته شد. پنجره مرورگر باز شده — وارد گوگل شوید.');
  refresh();
}
function logRow(pipeline,worker,status){
  traffic.unshift({t:new Date().toLocaleTimeString('fa-IR'),p:pipeline,w:worker,s:status});
  traffic=traffic.slice(0,40);
  const tb=$('traffic').querySelector('tbody');
  tb.innerHTML=traffic.map(r=>`<tr><td>${r.t}</td><td>${r.p}</td><td>${r.w}</td><td>${r.s}</td></tr>`).join('');
}
async function send(){
  const msg=$('msg').value.trim(); if(!msg) return;
  const model=$('model').value; const out=$('out'); out.textContent='';
  logRow('GHOST_DIRECT','—','در حال ارسال');
  try{
    const res=await fetch('/v1/chat/completions',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({model,messages:[{role:'user',content:msg}],stream:true})
    });
    const reader=res.body.getReader(); const dec=new TextDecoder();
    let buf='';
    while(true){
      const {done,value}=await reader.read(); if(done) break;
      buf+=dec.decode(value,{stream:true});
      const lines=buf.split('\n'); buf=lines.pop();
      for(const line of lines){
        if(!line.startsWith('data: ')) continue;
        const payload=line.slice(6);
        if(payload==='[DONE]'){logRow('GHOST_DIRECT','—','پایان');continue;}
        try{const j=JSON.parse(payload);
          const d=j.choices&&j.choices[0]&&j.choices[0].delta;
          if(d&&d.content) out.textContent+=d.content;
        }catch(e){}
      }
    }
  }catch(e){out.textContent='خطا: '+e.message;}
}
refresh(); setInterval(refresh,3000);
</script>
</body>
</html>"""

# ---------------------------------------------------------------------------
# UI LOADER - prefer the real ui.html file next to the app; fallback embedded
# ---------------------------------------------------------------------------
_UI_CACHE: Optional[str] = None

def _ui_html() -> str:
    global _UI_CACHE
    if _UI_CACHE is not None:
        return _UI_CACHE
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(os.path.join(getattr(sys, "_MEIPASS", ""), "ui.html"))
    candidates.append(os.path.join(BASE_DIR, "ui.html"))
    for c in candidates:
        if os.path.exists(c):
            try:
                with open(c, "r", encoding="utf-8") as f:
                    _UI_CACHE = f.read()
                    return _UI_CACHE
            except Exception:
                pass
    _UI_CACHE = UI_HTML
    return _UI_CACHE

# ---------------------------------------------------------------------------
# ENTRY POINT - server in a thread + visible control window
# ---------------------------------------------------------------------------
_server_holder = {}

def run_server():
    config = uvicorn.Config(app, host=HOST, port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    _server_holder["server"] = server
    server.run()

def stop_server():
    s = _server_holder.get("server")
    if s:
        s.should_exit = True

def _run_control_window():
    import tkinter as tk
    import webbrowser

    root = tk.Tk()
    root.title("Ghost Brain - Gemini API Bridge")
    root.geometry("480x280")
    root.configure(bg="#0b0c10")
    root.resizable(False, False)

    url = f"http://{HOST}:{PORT}"

    tk.Label(root, text="Ghost Brain", bg="#0b0c10", fg="#66fcf1",
             font=("Segoe UI", 18, "bold")).pack(pady=(16, 0))
    tk.Label(root, text="API و داشبورد روی این آدرس بالا آمد:",
             bg="#0b0c10", fg="#8b93a7",
             font=("Segoe UI", 9)).pack(pady=(8, 0))
    url_lbl = tk.Label(root, text=url, bg="#151821", fg="#ff8fa3",
                       font=("Consolas", 14, "bold"), cursor="hand2")
    url_lbl.pack(pady=(6, 0), ipadx=16, ipady=8)
    url_lbl.bind("<Button-1>", lambda e: webbrowser.open(url))

    status_lbl = tk.Label(root, text="در حال راه‌اندازی...", bg="#0b0c10",
                          fg="#ffa502", font=("Segoe UI", 10))
    status_lbl.pack(pady=(10, 0))

    auto_opened = {"v": False}

    def poll():
        try:
            st = pool.status()
            if st:
                ok = sum(1 for w in st if w["status"] in ("healthy", "busy"))
                models_n = len(_dynamic_models())
                text = f"ورکرها: {ok}/{len(st)} فعال  ·  مدل‌های اکانت: {models_n}"
                status_lbl.config(text=text,
                                 fg="#2ed573" if ok else "#ffa502")
                if ok and not auto_opened["v"]:
                    auto_opened["v"] = True
                    if os.environ.get("GHOST_AUTO_OPEN", "1") != "0":
                        webbrowser.open(url)
            else:
                status_lbl.config(text="در حال راه‌اندازی...", fg="#ffa502")
        except Exception:
            status_lbl.config(text="در حال راه‌اندازی...", fg="#ffa502")
        root.after(2000, poll)

    def close():
        root.destroy()

    tk.Button(root, text="بستن و خاموش کردن سرور", command=close,
              bg="#8e44ad", fg="white", font=("Segoe UI", 10, "bold"),
              relief="flat", padx=12, pady=7).pack(pady=(12, 0))
    tk.Label(root, text="بستن این پنجره = توقف کامل برنامه (و بستن مرورگرها)",
             bg="#0b0c10", fg="#8b93a7", font=("Segoe UI", 8)).pack(pady=(4, 0))

    root.protocol("WM_DELETE_WINDOW", close)
    poll()
    root.mainloop()

def main():
    global PORT, WORKERS
    p = argparse.ArgumentParser(description="Ghost Brain - Gemini API bridge")
    p.add_argument("--port", type=int, default=PORT)
    p.add_argument("--workers", type=int, default=WORKERS)
    args = p.parse_args()
    PORT, WORKERS = args.port, args.workers
    pool.count = WORKERS

    if not _acquire_lock():
        import tkinter.messagebox as mb
        mb.showerror("Ghost Brain",
                     "Ghost Brain is already running.\n\n"
                     "یک نسخه از برنامه در حال اجراست؛ اول آن را ببندید.")
        return

    t = threading.Thread(target=run_server, daemon=True)
    t.start()
    _run_control_window()   # closes -> full shutdown below

    stop_server()
    t.join(timeout=15)
    _release_lock()
    os._exit(0)             # guarantee: no leftover process

if __name__ == "__main__":
    main()
