"""Browser automation tool using Playwright."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import platform
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from ragnarbot.agent.tools.base import Tool

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page, Playwright

STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--disable-infobars",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-ipc-flooding-protection",
]

REALISTIC_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

GOTO_TIMEOUT_MS = 30_000  # 30s navigation timeout
LAUNCH_TIMEOUT_MS = 60_000  # 60s browser launch timeout (user profiles need more)


def _find_chrome_user_data_dir() -> Path | None:
    system = platform.system()
    if system == "Darwin":
        p = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    elif system == "Linux":
        p = Path.home() / ".config" / "google-chrome"
    elif system == "Windows":
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        p = local / "Google" / "Chrome" / "User Data"
    else:
        return None
    return p if p.exists() else None


def _create_symlinked_profile(chrome_data: Path) -> Path:
    """Create a temp dir with symlinks to Chrome profile contents.

    Chrome refuses remote debugging on the default data directory.
    A symlinked copy at a different path bypasses this restriction
    while preserving access to cookies, logins, and extensions.
    """
    import shutil

    temp_dir = Path("/tmp/ragnarbot_chrome_profile")
    if temp_dir.exists():
        for item in temp_dir.iterdir():
            if item.is_symlink():
                item.unlink()
            elif item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
        temp_dir.rmdir()
    temp_dir.mkdir(parents=True)

    for item in chrome_data.iterdir():
        os.symlink(item, temp_dir / item.name)

    return temp_dir


def _cleanup_symlinked_profile() -> None:
    """Remove the symlinked profile directory."""
    temp_dir = Path("/tmp/ragnarbot_chrome_profile")
    if not temp_dir.exists():
        return
    for item in temp_dir.iterdir():
        if item.is_symlink():
            item.unlink()
    import shutil
    shutil.rmtree(temp_dir, ignore_errors=True)


@dataclass
class BrowserSession:
    session_id: str
    context: BrowserContext
    page: Page
    profile: str
    persistent: bool
    created_at: float
    last_activity: float
    dom_index: dict[int, dict] = field(default_factory=dict)
    idle_task: asyncio.Task | None = None
    _browser: Any = None  # Reference to browser for non-persistent sessions


class BrowserSessionManager:
    """Manages browser sessions with idle timeout and stealth."""

    def __init__(self, config: Any):
        self._config = config
        self._playwright: Playwright | None = None
        self._sessions: dict[str, BrowserSession] = {}

    async def _ensure_playwright(self):
        if self._playwright is not None:
            return self._playwright
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError(
                "Playwright is not installed. Run: pip install playwright && playwright install"
            )
        pw = await async_playwright().start()
        self._playwright = pw
        return pw

    async def _apply_stealth(self, page):
        try:
            from playwright_stealth import stealth_async
            await stealth_async(page)
        except ImportError:
            logger.warning("playwright-stealth not installed, skipping stealth patches")

    def _build_args(self) -> list[str]:
        vw, vh = self._config.viewport_width, self._config.viewport_height
        return [*STEALTH_ARGS, f"--window-size={vw},{vh}"]

    def _reset_idle_timer(self, session: BrowserSession) -> None:
        session.last_activity = time.time()
        if session.idle_task and not session.idle_task.done():
            session.idle_task.cancel()
        session.idle_task = asyncio.create_task(
            self._idle_watchdog(session.session_id)
        )

    async def _idle_watchdog(self, session_id: str) -> None:
        try:
            await asyncio.sleep(self._config.idle_timeout)
            logger.info(f"Browser session {session_id} idle timeout, closing")
            await self.close(session_id)
        except asyncio.CancelledError:
            pass

    def _get_session(self, session_id: str | None) -> BrowserSession:
        if not session_id:
            if len(self._sessions) == 1:
                return next(iter(self._sessions.values()))
            raise ValueError(
                "session_id required when multiple sessions are open. "
                "Use browser(action='list_sessions') to see active sessions."
            )
        if session_id not in self._sessions:
            raise ValueError(
                f"No browser session '{session_id}'. "
                "Use browser(action='open') first."
            )
        return self._sessions[session_id]

    # ── Session lifecycle ──────────────────────────────────────────

    async def open(
        self,
        url: str | None = None,
        profile: str = "clean",
        headless: bool | None = None,
    ) -> str:
        from ragnarbot.agent.browser_js import STEALTH_INIT_JS

        pw = await self._ensure_playwright()
        h = headless if headless is not None else self._config.headless
        vw = self._config.viewport_width
        vh = self._config.viewport_height
        args = self._build_args()
        session_id = str(uuid.uuid4())[:8]

        try:
            if profile == "user":
                user_data = _find_chrome_user_data_dir()
                if not user_data:
                    return "Error: Chrome user profile not found."
                # Chrome refuses remote debugging on its default data dir.
                # Create a symlinked copy at a different path to bypass this.
                symlinked = _create_symlinked_profile(user_data)
                context = await pw.chromium.launch_persistent_context(
                    user_data_dir=str(symlinked),
                    channel="chrome",
                    headless=h,
                    args=args,
                    viewport={"width": vw, "height": vh},
                    timeout=LAUNCH_TIMEOUT_MS,
                )
                page = context.pages[0] if context.pages else await context.new_page()
                await context.add_init_script(STEALTH_INIT_JS)
                await self._apply_stealth(page)
                session = BrowserSession(
                    session_id=session_id,
                    context=context,
                    page=page,
                    profile="user",
                    persistent=True,
                    created_at=time.time(),
                    last_activity=time.time(),
                )
            else:
                browser = await pw.chromium.launch(
                    channel="chrome", headless=h, args=args,
                    timeout=LAUNCH_TIMEOUT_MS,
                )
                context = await browser.new_context(
                    viewport={"width": vw, "height": vh},
                    user_agent=REALISTIC_UA,
                    locale="en-US",
                    timezone_id="America/New_York",
                )
                await context.add_init_script(STEALTH_INIT_JS)
                page = await context.new_page()
                await self._apply_stealth(page)
                session = BrowserSession(
                    session_id=session_id,
                    context=context,
                    page=page,
                    profile="clean",
                    persistent=False,
                    created_at=time.time(),
                    last_activity=time.time(),
                    _browser=browser,
                )
        except Exception as e:
            msg = str(e)
            lower = msg.lower()
            if "chrome" in lower and ("not found" in lower or "executable" in lower):
                return (
                    "Error: Chrome not found. "
                    "Install Google Chrome to use the browser tool."
                )
            if profile == "user" and (
                "user data" in lower
                or "already running" in lower
                or "lock" in lower
            ):
                return (
                    "Error: Could not open Chrome with user profile. "
                    "Chrome may already be running with this profile. "
                    "Close Chrome first, or use profile='clean' for a fresh session."
                )
            raise

        if url:
            await page.goto(
                url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS,
            )

        self._sessions[session_id] = session
        self._reset_idle_timer(session)

        title = await page.title() if url else ""
        parts = [f"Session `{session_id}` opened ({profile} profile)."]
        if url:
            parts.append(f"Page: {title} — {page.url}")
        return " ".join(parts)

    async def connect(self, cdp_url: str) -> str:
        from ragnarbot.agent.browser_js import STEALTH_INIT_JS

        pw = await self._ensure_playwright()
        browser = await pw.chromium.connect_over_cdp(cdp_url)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = context.pages[0] if context.pages else await context.new_page()
        await context.add_init_script(STEALTH_INIT_JS)
        await self._apply_stealth(page)

        session_id = str(uuid.uuid4())[:8]
        session = BrowserSession(
            session_id=session_id,
            context=context,
            page=page,
            profile="connect",
            persistent=False,
            created_at=time.time(),
            last_activity=time.time(),
            _browser=browser,
        )
        self._sessions[session_id] = session
        self._reset_idle_timer(session)

        title = await page.title()
        return f"Session `{session_id}` connected. Page: {title} — {page.url}"

    async def close(self, session_id: str) -> str:
        session = self._sessions.pop(session_id, None)
        if not session:
            return f"No session '{session_id}' to close."
        if session.idle_task and not session.idle_task.done():
            session.idle_task.cancel()
        try:
            if session.persistent:
                await session.context.close()
            elif session._browser:
                await session._browser.close()
            else:
                await session.context.close()
        except Exception as e:
            logger.warning(f"Error closing browser session {session_id}: {e}")
        if session.profile == "user":
            _cleanup_symlinked_profile()
        return f"Session `{session_id}` closed."

    async def close_all(self) -> None:
        for sid in list(self._sessions.keys()):
            await self.close(sid)
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    def list_sessions(self) -> str:
        if not self._sessions:
            return "No active browser sessions."
        lines = []
        for s in self._sessions.values():
            age = int(time.time() - s.created_at)
            url = s.page.url if s.page else "about:blank"
            lines.append(
                f"- `{s.session_id}` ({s.profile}) — {url} — age: {age}s"
            )
        return "\n".join(lines)

    # ── Navigation ─────────────────────────────────────────────────

    async def navigate(self, session_id: str | None, url: str) -> str:
        session = self._get_session(session_id)
        self._reset_idle_timer(session)
        await session.page.goto(
            url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS,
        )
        title = await session.page.title()
        return f"Navigated to: {title} — {session.page.url}"

    async def back(self, session_id: str | None) -> str:
        session = self._get_session(session_id)
        self._reset_idle_timer(session)
        await session.page.go_back()
        title = await session.page.title()
        return f"Back to: {title} — {session.page.url}"

    async def forward(self, session_id: str | None) -> str:
        session = self._get_session(session_id)
        self._reset_idle_timer(session)
        await session.page.go_forward()
        title = await session.page.title()
        return f"Forward to: {title} — {session.page.url}"

    # ── Content & DOM ──────────────────────────────────────────────

    async def content(
        self, session_id: str | None, selector: str | None = None,
    ) -> str:
        from ragnarbot.agent.browser_js import DOM_INDEX_JS, DOM_REMOVE_BADGES_JS

        session = self._get_session(session_id)
        self._reset_idle_timer(session)

        # Remove old badges first
        await session.page.evaluate(DOM_REMOVE_BADGES_JS)

        # Run DOM indexing
        elements = await session.page.evaluate(DOM_INDEX_JS)
        session.dom_index = {e["index"]: e for e in elements}

        title = await session.page.title()
        url = session.page.url

        # Get truncated text content
        if selector:
            text = await session.page.locator(selector).inner_text()
        else:
            text = await session.page.inner_text("body")
        text = text[:3000] if len(text) > 3000 else text

        # Format element map
        elem_lines = []
        for e in elements:
            parts = [f"[{e['index']}]", f"<{e['tag']}>"]
            if e.get("role"):
                parts.append(f"role={e['role']}")
            if e.get("label"):
                parts.append(f'"{e["label"]}"')
            if e.get("href"):
                href = e["href"][:60]
                parts.append(f"→ {href}")
            elem_lines.append(" ".join(parts))

        return (
            f"# {title}\n"
            f"URL: {url}\n\n"
            f"## Page Text (truncated)\n{text}\n\n"
            f"## Interactive Elements ({len(elements)})\n"
            + "\n".join(elem_lines)
        )

    # ── Screenshot ─────────────────────────────────────────────────

    async def screenshot(
        self,
        session_id: str | None,
        selector: str | None = None,
        full_page: bool = False,
    ) -> str | list[dict[str, Any]]:
        session = self._get_session(session_id)
        self._reset_idle_timer(session)

        if selector:
            img_bytes = await session.page.locator(selector).screenshot()
        else:
            img_bytes = await session.page.screenshot(full_page=full_page)

        b64 = base64.b64encode(img_bytes).decode()
        size_kb = len(img_bytes) / 1024
        title = await session.page.title()

        return [
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            },
            {"type": "text", "text": f"Screenshot: {title} — {session.page.url} ({size_kb:.0f} KB)"},
        ]

    # ── Interaction ────────────────────────────────────────────────

    async def click(
        self,
        session_id: str | None,
        index: int | None = None,
        selector: str | None = None,
        x: int | None = None,
        y: int | None = None,
    ) -> str:
        session = self._get_session(session_id)
        self._reset_idle_timer(session)

        if index is not None:
            elem = session.dom_index.get(index)
            if not elem:
                return f"Error: Index {index} not found. Run browser(action='content') to refresh."
            sel = elem["selector"]
            await session.page.locator(sel).first.click()
            return f"Clicked [{index}] {elem.get('tag', '')} \"{elem.get('label', '')}\""
        elif selector:
            await session.page.locator(selector).first.click()
            return f"Clicked: {selector}"
        elif x is not None and y is not None:
            await session.page.mouse.click(x, y)
            return f"Clicked at ({x}, {y})"
        else:
            return "Error: Provide index, selector, or x/y coordinates."

    async def type_text(
        self,
        session_id: str | None,
        text: str,
        index: int | None = None,
        selector: str | None = None,
        clear: bool = False,
    ) -> str:
        session = self._get_session(session_id)
        self._reset_idle_timer(session)

        if index is not None:
            elem = session.dom_index.get(index)
            if not elem:
                return f"Error: Index {index} not found. Run browser(action='content') to refresh."
            loc = session.page.locator(elem["selector"]).first
        elif selector:
            loc = session.page.locator(selector).first
        else:
            return "Error: Provide index or selector for type."

        if clear:
            await loc.click(click_count=3)
        await loc.type(text)
        return f"Typed {len(text)} chars."

    async def scroll(
        self,
        session_id: str | None,
        direction: str = "down",
        amount: int = 500,
    ) -> str:
        session = self._get_session(session_id)
        self._reset_idle_timer(session)
        dy = amount if direction == "down" else -amount
        await session.page.mouse.wheel(0, dy)
        return f"Scrolled {direction} {amount}px."

    async def wait(
        self,
        session_id: str | None,
        selector: str,
        timeout: int = 10000,
    ) -> str:
        session = self._get_session(session_id)
        self._reset_idle_timer(session)
        await session.page.wait_for_selector(selector, timeout=timeout)
        return f"Element `{selector}` appeared."

    async def js(self, session_id: str | None, code: str) -> str:
        session = self._get_session(session_id)
        self._reset_idle_timer(session)
        result = await session.page.evaluate(code)
        return json.dumps(result, indent=2, default=str)

    # ── Tabs ───────────────────────────────────────────────────────

    async def tabs(self, session_id: str | None) -> str:
        session = self._get_session(session_id)
        self._reset_idle_timer(session)
        pages = session.context.pages
        lines = []
        for i, p in enumerate(pages):
            current = " (active)" if p == session.page else ""
            title = await p.title()
            lines.append(f"[{i}] {title} — {p.url}{current}")
        return "\n".join(lines) or "No tabs open."

    async def tab_open(
        self, session_id: str | None, url: str | None = None,
    ) -> str:
        session = self._get_session(session_id)
        self._reset_idle_timer(session)
        new_page = await session.context.new_page()
        await self._apply_stealth(new_page)
        if url:
            await new_page.goto(
                url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS,
            )
        session.page = new_page
        title = await new_page.title()
        return f"New tab opened. {title} — {new_page.url}"

    async def tab_switch(self, session_id: str | None, tab_id: int) -> str:
        session = self._get_session(session_id)
        self._reset_idle_timer(session)
        pages = session.context.pages
        if tab_id < 0 or tab_id >= len(pages):
            return f"Error: Tab {tab_id} not found. {len(pages)} tabs open."
        session.page = pages[tab_id]
        await session.page.bring_to_front()
        title = await session.page.title()
        return f"Switched to tab [{tab_id}] {title} — {session.page.url}"

    async def tab_close(self, session_id: str | None, tab_id: int) -> str:
        session = self._get_session(session_id)
        self._reset_idle_timer(session)
        pages = session.context.pages
        if tab_id < 0 or tab_id >= len(pages):
            return f"Error: Tab {tab_id} not found."
        target = pages[tab_id]
        was_current = target == session.page
        await target.close()
        if was_current:
            remaining = session.context.pages
            if remaining:
                session.page = remaining[-1]
            else:
                return "All tabs closed. Session still alive — open a new tab or close session."
        title = await session.page.title()
        return f"Tab [{tab_id}] closed. Active: {title} — {session.page.url}"


# ── BrowserTool ────────────────────────────────────────────────────

ACTIONS = [
    "open", "connect", "close", "close_all", "list_sessions",
    "navigate", "back", "forward",
    "content", "screenshot",
    "click", "type", "scroll", "wait", "js",
    "tabs", "tab_open", "tab_switch", "tab_close",
]


class BrowserTool(Tool):
    """Browser automation: open pages, interact, take screenshots."""

    name = "browser"
    description = (
        "Control a real Chrome browser. Actions: "
        "open, connect, close, close_all, list_sessions, "
        "navigate, back, forward, "
        "content, screenshot, "
        "click, type, scroll, wait, js, "
        "tabs, tab_open, tab_switch, tab_close. "
        "Call content first to get a numbered element map, then use index to click/type."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ACTIONS,
                "description": "Browser action to perform.",
            },
            "session_id": {
                "type": "string",
                "description": "Session ID. Optional if only one session is open.",
            },
            "url": {
                "type": "string",
                "description": "URL for open/navigate/tab_open.",
            },
            "headless": {
                "type": "boolean",
                "description": "Override headless mode for open.",
            },
            "profile": {
                "type": "string",
                "enum": ["clean", "user"],
                "description": "Browser profile for open. 'user' reuses Chrome cookies/logins.",
            },
            "cdp_url": {
                "type": "string",
                "description": "Chrome DevTools Protocol URL for connect.",
            },
            "index": {
                "type": "integer",
                "description": "DOM element index from content map, for click/type.",
            },
            "selector": {
                "type": "string",
                "description": "CSS selector for click/type/screenshot/content/wait.",
            },
            "x": {
                "type": "integer",
                "description": "X coordinate for click.",
            },
            "y": {
                "type": "integer",
                "description": "Y coordinate for click.",
            },
            "text": {
                "type": "string",
                "description": "Text to type.",
            },
            "clear": {
                "type": "boolean",
                "description": "Clear field before typing (triple-click select all).",
            },
            "direction": {
                "type": "string",
                "enum": ["up", "down"],
                "description": "Scroll direction.",
            },
            "amount": {
                "type": "integer",
                "description": "Scroll amount in pixels (default 500).",
            },
            "code": {
                "type": "string",
                "description": "JavaScript code for js action.",
            },
            "full_page": {
                "type": "boolean",
                "description": "Full-page screenshot (default false).",
            },
            "tab_id": {
                "type": "integer",
                "description": "Tab index for tab_switch/tab_close.",
            },
            "timeout": {
                "type": "integer",
                "description": "Wait timeout in milliseconds (default 10000).",
            },
        },
        "required": ["action"],
    }

    def __init__(self, manager: BrowserSessionManager):
        self._manager = manager

    async def execute(self, action: str, **kwargs: Any) -> str | list[dict[str, Any]]:
        dispatch = {
            "open": self._action_open,
            "connect": self._action_connect,
            "close": self._action_close,
            "close_all": self._action_close_all,
            "list_sessions": self._action_list_sessions,
            "navigate": self._action_navigate,
            "back": self._action_back,
            "forward": self._action_forward,
            "content": self._action_content,
            "screenshot": self._action_screenshot,
            "click": self._action_click,
            "type": self._action_type,
            "scroll": self._action_scroll,
            "wait": self._action_wait,
            "js": self._action_js,
            "tabs": self._action_tabs,
            "tab_open": self._action_tab_open,
            "tab_switch": self._action_tab_switch,
            "tab_close": self._action_tab_close,
        }

        handler = dispatch.get(action)
        if not handler:
            return f"Error: Unknown browser action '{action}'."

        try:
            return await handler(**kwargs)
        except Exception as e:
            logger.error(f"Browser.{action} error: {e}")
            return f"Error in browser.{action}: {e}"

    async def _action_open(self, **kw) -> str:
        return await self._manager.open(
            url=kw.get("url"),
            profile=kw.get("profile", "clean"),
            headless=kw.get("headless"),
        )

    async def _action_connect(self, **kw) -> str:
        cdp_url = kw.get("cdp_url")
        if not cdp_url:
            return "Error: cdp_url required for connect action."
        return await self._manager.connect(cdp_url)

    async def _action_close(self, **kw) -> str:
        sid = kw.get("session_id")
        if not sid:
            sessions = self._manager._sessions
            if len(sessions) == 1:
                sid = next(iter(sessions.keys()))
            else:
                return "Error: session_id required. Use list_sessions to see active sessions."
        return await self._manager.close(sid)

    async def _action_close_all(self, **kw) -> str:
        await self._manager.close_all()
        return "All browser sessions closed."

    async def _action_list_sessions(self, **kw) -> str:
        return self._manager.list_sessions()

    async def _action_navigate(self, **kw) -> str:
        url = kw.get("url")
        if not url:
            return "Error: url required for navigate."
        return await self._manager.navigate(kw.get("session_id"), url)

    async def _action_back(self, **kw) -> str:
        return await self._manager.back(kw.get("session_id"))

    async def _action_forward(self, **kw) -> str:
        return await self._manager.forward(kw.get("session_id"))

    async def _action_content(self, **kw) -> str:
        return await self._manager.content(
            kw.get("session_id"), kw.get("selector"),
        )

    async def _action_screenshot(self, **kw) -> str | list[dict[str, Any]]:
        return await self._manager.screenshot(
            kw.get("session_id"),
            selector=kw.get("selector"),
            full_page=kw.get("full_page", False),
        )

    async def _action_click(self, **kw) -> str:
        return await self._manager.click(
            kw.get("session_id"),
            index=kw.get("index"),
            selector=kw.get("selector"),
            x=kw.get("x"),
            y=kw.get("y"),
        )

    async def _action_type(self, **kw) -> str:
        text = kw.get("text")
        if not text:
            return "Error: text required for type."
        return await self._manager.type_text(
            kw.get("session_id"),
            text=text,
            index=kw.get("index"),
            selector=kw.get("selector"),
            clear=kw.get("clear", False),
        )

    async def _action_scroll(self, **kw) -> str:
        return await self._manager.scroll(
            kw.get("session_id"),
            direction=kw.get("direction", "down"),
            amount=kw.get("amount", 500),
        )

    async def _action_wait(self, **kw) -> str:
        selector = kw.get("selector")
        if not selector:
            return "Error: selector required for wait."
        return await self._manager.wait(
            kw.get("session_id"),
            selector=selector,
            timeout=kw.get("timeout", 10000),
        )

    async def _action_js(self, **kw) -> str:
        code = kw.get("code")
        if not code:
            return "Error: code required for js action."
        return await self._manager.js(kw.get("session_id"), code)

    async def _action_tabs(self, **kw) -> str:
        return await self._manager.tabs(kw.get("session_id"))

    async def _action_tab_open(self, **kw) -> str:
        return await self._manager.tab_open(
            kw.get("session_id"), url=kw.get("url"),
        )

    async def _action_tab_switch(self, **kw) -> str:
        tab_id = kw.get("tab_id")
        if tab_id is None:
            return "Error: tab_id required for tab_switch."
        return await self._manager.tab_switch(kw.get("session_id"), int(tab_id))

    async def _action_tab_close(self, **kw) -> str:
        tab_id = kw.get("tab_id")
        if tab_id is None:
            return "Error: tab_id required for tab_close."
        return await self._manager.tab_close(kw.get("session_id"), int(tab_id))
