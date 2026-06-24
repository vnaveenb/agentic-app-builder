"""Console-capture injection for preview HTML.

The app edge injects this snippet into preview HTML so runtime ``console.error``/
``console.warn`` and uncaught errors in *generated* code are forwarded to the
parent window (the UI), where they surface in the terminal panel and a preview
error banner instead of failing silently inside the iframe.

It also normalizes single-file React/Babel previews: the weak default model often
emits ES-module ``import``/``export`` statements inside ``<script type="text/babel">``,
which Babel-standalone compiles as a *non-module* script and re-injects via
``document.appendChild`` — the native ``import`` then throws "Cannot use import
statement outside a module". The normalizer strips those statements, exposes React's
API as globals, and ensures the UMD CDN tags are present so the preview still renders.
"""

from __future__ import annotations

import re

_CONSOLE_CAPTURE_SCRIPT = b"""<script>
(function(){
  var _origError = console.error;
  var _origWarn = console.warn;
  function _send(level, args) {
    try {
      var msg = Array.prototype.slice.call(args).map(function(a) {
        return typeof a === 'object' ? JSON.stringify(a) : String(a);
      }).join(' ');
      window.parent.postMessage({type:'console', level:level, msg:msg}, '*');
    } catch(e) {}
  }
  console.error = function() { _send('error', arguments); _origError.apply(console, arguments); };
  console.warn = function() { _send('warn', arguments); _origWarn.apply(console, arguments); };
  window.onerror = function(msg, src, line, col, err) {
    _send('error', [msg + ' (' + (src||'') + ':' + line + ':' + col + ')']);
  };
  window.onunhandledrejection = function(e) {
    _send('error', ['Unhandled Promise: ' + (e.reason || e)]);
  };
})();
</script>
"""


def inject_console_capture(content: bytes) -> bytes:
    """Inject the console-capture script into HTML after <head> or <body>."""
    lower = content.lower()
    idx = lower.find(b"<head>")
    if idx != -1:
        insert_at = idx + len(b"<head>")
        return content[:insert_at] + _CONSOLE_CAPTURE_SCRIPT + content[insert_at:]
    idx = lower.find(b"<body")
    if idx != -1:
        close = lower.find(b">", idx)
        if close != -1:
            insert_at = close + 1
            return content[:insert_at] + _CONSOLE_CAPTURE_SCRIPT + content[insert_at:]
    return _CONSOLE_CAPTURE_SCRIPT + content


# ── React/Babel single-file preview normalization ─────────────────────────────

# Pinned CDN tags injected when a React preview drops them (exact versions per
# the project's pin-exact-versions rule).
_REACT_UMD = "https://unpkg.com/react@18.3.1/umd/react.production.min.js"
_REACT_DOM_UMD = "https://unpkg.com/react-dom@18.3.1/umd/react-dom.production.min.js"
_BABEL_STANDALONE = "https://unpkg.com/@babel/standalone@7.26.4/babel.min.js"

# React APIs exposed as locals when imports are stripped, so JSX that referenced
# them via `import { useState } from 'react'` keeps working against the UMD global.
_REACT_GLOBALS = (
    "const { useState, useEffect, useRef, useReducer, useMemo, useCallback, "
    "useContext, useLayoutEffect, Fragment, memo, createContext, createElement, "
    "Component } = (typeof React !== 'undefined' ? React : {});"
)

# A `<script type="text/babel">...</script>` block (case-insensitive, DOTALL body).
_BABEL_BLOCK_RE = re.compile(
    rb"(<script\b[^>]*\btype=[\"']text/babel[\"'][^>]*>)(.*?)(</script>)",
    re.IGNORECASE | re.DOTALL,
)

# ES import statements: side-effect (`import 'x'`), default/named, and multi-line
# `import {\n a,\n b\n} from 'x'`. Anchored to line starts (MULTILINE).
_IMPORT_RE = re.compile(
    rb"^[ \t]*import\b(?:[^;\n]*?\{[^}]*\})?[^;\n]*?(?:from[ \t]*[\"'][^\"']+[\"'])?[ \t]*;?[ \t]*$",
    re.MULTILINE | re.DOTALL,
)
# `export default ` / leading `export ` keywords (keep the declaration that follows).
_EXPORT_RE = re.compile(rb"^([ \t]*)export[ \t]+(default[ \t]+)?", re.MULTILINE)


def _normalize_babel_block(body: bytes) -> bytes:
    """Strip ES import/export from one text/babel block; globalize React if needed."""
    new_body, n_imports = _IMPORT_RE.subn(b"", body)
    new_body = _EXPORT_RE.sub(rb"\1", new_body)
    if n_imports and b"} = React" not in new_body:
        new_body = b"\n" + _REACT_GLOBALS.encode() + b"\n" + new_body
    return new_body


def normalize_react_preview(content: bytes) -> bytes:
    """Make single-file React/Babel previews resilient to ES `import`/`export`.

    No-op unless the HTML contains a ``<script type="text/babel">`` block. Idempotent:
    re-running on already-normalized HTML produces the same bytes.
    """
    if b'type="text/babel"' not in content.lower() and b"type='text/babel'" not in content.lower():
        return content

    stripped_any = False

    def _sub(m: re.Match[bytes]) -> bytes:
        nonlocal stripped_any
        open_tag, body, close_tag = m.group(1), m.group(2), m.group(3)
        new_body = _normalize_babel_block(body)
        if new_body != body:
            stripped_any = True
        return open_tag + new_body + close_tag

    content = _BABEL_BLOCK_RE.sub(_sub, content)

    # If we removed imports, the model may have omitted the UMD CDN tags. Ensure the
    # globals exist by injecting the pinned scripts into <head> (idempotent: only when
    # the corresponding CDN reference is absent).
    if stripped_any:
        head_scripts = b""
        if b"react@18" not in content and b"react.production" not in content:
            head_scripts += (
                f'<script src="{_REACT_UMD}"></script>'
                f'<script src="{_REACT_DOM_UMD}"></script>'
            ).encode()
        if b"@babel/standalone" not in content:
            head_scripts += f'<script src="{_BABEL_STANDALONE}"></script>'.encode()
        if head_scripts:
            lower = content.lower()
            idx = lower.find(b"<head>")
            if idx != -1:
                insert_at = idx + len(b"<head>")
                content = content[:insert_at] + head_scripts + content[insert_at:]
            else:
                content = head_scripts + content

    return content


def prepare_preview_html(content: bytes) -> bytes:
    """Full preview-HTML edge transform: React normalization, then console capture."""
    return inject_console_capture(normalize_react_preview(content))
