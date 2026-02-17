"""JavaScript constants for browser tool: DOM indexing, stealth, and badge removal."""

STEALTH_INIT_JS = """
// Patch navigator.webdriver
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

// Ensure window.chrome exists (headless Chrome sometimes omits this)
if (!window.chrome) {
    window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){} };
}

// Patch navigator.plugins to report realistic plugins
Object.defineProperty(navigator, 'plugins', {
    get: () => [1, 2, 3, 4, 5],
});

// Patch navigator.languages
Object.defineProperty(navigator, 'languages', {
    get: () => ['en-US', 'en'],
});

// Patch permissions API to prevent 'denied' on notifications
const originalQuery = window.Permissions?.prototype?.query;
if (originalQuery) {
    window.Permissions.prototype.query = (parameters) =>
        parameters.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : originalQuery(parameters);
}
"""

DOM_INDEX_JS = """
(() => {
    const SELECTORS = [
        'a[href]', 'button', 'input', 'select', 'textarea',
        '[role="button"]', '[role="link"]', '[role="tab"]',
        '[role="menuitem"]', '[role="checkbox"]', '[role="radio"]',
        '[onclick]', '[tabindex]', 'summary', '[contenteditable="true"]'
    ];

    function isVisible(el) {
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden') return false;
        if (parseFloat(style.opacity) === 0) return false;
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 && rect.height === 0) return false;
        const margin = 100;
        if (rect.bottom < -margin || rect.top > window.innerHeight + margin) return false;
        if (rect.right < -margin || rect.left > window.innerWidth + margin) return false;
        return true;
    }

    function getLabel(el) {
        const label = el.getAttribute('aria-label')
            || el.getAttribute('title')
            || el.innerText
            || el.getAttribute('placeholder')
            || '';
        return label.trim().substring(0, 80);
    }

    function getUniqueSelector(el) {
        if (el.id) return '#' + CSS.escape(el.id);
        const testId = el.getAttribute('data-testid');
        if (testId) return '[data-testid="' + CSS.escape(testId) + '"]';

        const parts = [];
        let current = el;
        for (let i = 0; i < 4 && current && current !== document.body; i++) {
            const tag = current.tagName.toLowerCase();
            const parent = current.parentElement;
            if (parent) {
                const siblings = Array.from(parent.children).filter(
                    c => c.tagName === current.tagName
                );
                if (siblings.length > 1) {
                    const idx = siblings.indexOf(current) + 1;
                    parts.unshift(tag + ':nth-of-type(' + idx + ')');
                } else {
                    parts.unshift(tag);
                }
            } else {
                parts.unshift(tag);
            }
            current = parent;
        }
        return parts.join(' > ');
    }

    const seen = new Set();
    const results = [];
    let index = 0;

    for (const sel of SELECTORS) {
        for (const el of document.querySelectorAll(sel)) {
            if (seen.has(el)) continue;
            seen.add(el);
            if (!isVisible(el)) continue;

            const info = {
                index: index,
                tag: el.tagName.toLowerCase(),
                type: el.getAttribute('type') || '',
                role: el.getAttribute('role') || '',
                label: getLabel(el),
                href: el.getAttribute('href') || '',
                selector: getUniqueSelector(el),
            };

            // Inject visual badge overlay
            const rect = el.getBoundingClientRect();
            const badge = document.createElement('div');
            badge.setAttribute('data-ragnar-idx', String(index));
            badge.style.cssText = [
                'position:fixed',
                'z-index:2147483647',
                'background:#e74c3c',
                'color:#fff',
                'font:bold 11px monospace',
                'padding:1px 4px',
                'border-radius:3px',
                'pointer-events:none',
                'left:' + rect.left + 'px',
                'top:' + Math.max(0, rect.top - 16) + 'px',
            ].join(';');
            badge.textContent = String(index);
            document.body.appendChild(badge);

            results.push(info);
            index++;
        }
    }

    return results;
})()
"""

DOM_REMOVE_BADGES_JS = """
(() => {
    const badges = document.querySelectorAll('[data-ragnar-idx]');
    badges.forEach(b => b.remove());
    return badges.length;
})()
"""
