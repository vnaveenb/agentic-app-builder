"""Console-capture injection for preview HTML.

The app edge injects this snippet into preview HTML so runtime ``console.error``/
``console.warn`` and uncaught errors in *generated* code are forwarded to the
parent window (the UI), where they surface in the terminal panel and a preview
error banner instead of failing silently inside the iframe.
"""

from __future__ import annotations

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
