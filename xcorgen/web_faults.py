"""JavaScript exception families, with the message and stack format each browser engine produces.

Exceptions are recorded the way EmbraceLogManager records them: exception.type is the
constructor name, exception.name is error.name, the body and exception.message are the
message, and exception.stacktrace is error.stack (Chromium prefixes "Name: message" and
uses "    at fn (url:line:col)"; WebKit and Gecko use "fn@url:line:col" with no header).
The backend picks the stack parser from the first matching line, so a Chromium message
never contains "@".

Function names and frame positions are derived from the release and bundle, so an issue
(exception name plus the chosen frame's method and file) is stable within a release and a
new release produces new issues, as minified production builds do.
"""
import hashlib

from .web_sites import content_hash

MINIFIED = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _minified(seed):
    h = hashlib.sha256(seed.encode()).digest()
    return MINIFIED[h[0] % 52] + MINIFIED[h[1] % 52] + ("" if h[2] % 3 else MINIFIED[h[3] % 52])


def _position(seed, line_max=2):
    h = int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16)
    return 1 + h % line_max, 1000 + h % 90000


# family -> (constructor, name, {engine: message}, where it is raised)
FAMILIES = {
    "property_undefined": ("TypeError", "TypeError", {
        "chromium": "Cannot read properties of undefined (reading 'price')",
        "webkit": "undefined is not an object (evaluating 't.price')",
        "gecko": "can't access property \"price\", t is undefined"}, "page"),
    "not_a_function": ("TypeError", "TypeError", {
        "chromium": "t.formatPrice is not a function",
        "webkit": "t.formatPrice is not a function. (In 't.formatPrice(e)', 't.formatPrice' is undefined)",
        "gecko": "t.formatPrice is not a function"}, "page"),
    "chunk_load": ("TypeError", "TypeError", {
        "chromium": "Failed to fetch dynamically imported module: {chunk}",
        "webkit": "Importing a module script failed.",
        "gecko": "error loading dynamically imported module: {chunk}"}, "router"),
    "webpack_chunk_load": ("ChunkLoadError", "ChunkLoadError", {
        "chromium": "Loading chunk {chunk_id} failed.\n(error: {chunk})",
        "webkit": "Loading chunk {chunk_id} failed.\n(error: {chunk})",
        "gecko": "Loading chunk {chunk_id} failed.\n(error: {chunk})"}, "router"),
    "json_html": ("SyntaxError", "SyntaxError", {
        "chromium": "Unexpected token '<', \"<!DOCTYPE \"... is not valid JSON",
        "webkit": "JSON Parse error: Unrecognized token '<'",
        "gecko": "JSON.parse: unexpected character at line 1 column 1 of the JSON data"}, "api"),
    "failed_to_fetch": ("TypeError", "TypeError", {
        "chromium": "Failed to fetch", "webkit": "Load failed",
        "gecko": "NetworkError when attempting to fetch resource."}, "api"),
    "abort": ("DOMException", "AbortError", {
        "chromium": "signal is aborted without reason", "webkit": "Fetch is aborted",
        "gecko": "The operation was aborted. "}, "api"),
    "quota_exceeded": ("DOMException", "QuotaExceededError", {
        "chromium": "Failed to execute 'setItem' on 'Storage': Setting the value of 'vantra:recently-viewed' exceeded the quota.",
        "webkit": "The quota has been exceeded.", "gecko": "The quota has been exceeded."}, "page"),
    "hydration": ("Error", "Error", {
        "chromium": "Minified React error #418; visit https://react.dev/errors/418?args[]=text&args[]= for the full message "
                    "or use the non-minified dev environment for full errors and additional helpful warnings.",
        "webkit": "Minified React error #418; visit https://react.dev/errors/418?args[]=text&args[]= for the full message "
                  "or use the non-minified dev environment for full errors and additional helpful warnings.",
        "gecko": "Minified React error #418; visit https://react.dev/errors/418?args[]=text&args[]= for the full message "
                 "or use the non-minified dev environment for full errors and additional helpful warnings."}, "framework"),
    "script_error": ("String", "String", {"chromium": "Script error.", "webkit": "Script error.", "gecko": "Script error."},
                     "third_party"),
}
# Default handling per family: how the error reaches the SDK.
HANDLING = {"failed_to_fetch": "unhandled_rejection", "abort": "unhandled_rejection", "json_html": "unhandled_rejection",
            "chunk_load": "unhandled_rejection", "webpack_chunk_load": "unhandled_rejection",
            "script_error": "unhandled_error", "hydration": "unhandled_error"}


def _frames(engine, frames):
    if engine == "chromium":
        return "\n".join(f"    at {fn} ({url}:{line}:{col})" if url else f"    at {fn} (<anonymous>)" for fn, url, line, col in frames)
    out = []
    for fn, url, line, col in frames:
        if url is None:
            if engine == "webkit":
                out.append(f"{fn.split('.')[-1]}@[native code]")
            continue
        out.append(f"{fn.split('.')[-1] if engine == 'webkit' else fn}@{url}:{line}:{col}")
    return "\n".join(out)


def exception(family, engine, release, page_bundle, vendor_bundle, chunk_url=None, chunk_id=None):
    """(exception.type, exception.name, message, exception.stacktrace) for one occurrence."""
    ctor, name, messages, where = FAMILIES[family]
    message = messages[engine].format(chunk=chunk_url or "", chunk_id=chunk_id or "")
    if family == "script_error":
        return ctor, name, message, ""
    from .web_symbols import frame
    seed = f"{family}:{release}:{page_bundle}"
    fn, line, col = frame(family)
    caller, cline, ccol = frame(family, "caller")
    react, vline, vcol = frame(family, "framework")
    if where == "api":
        frames = [(fn, page_bundle, line, col), (f"async {caller}", page_bundle, cline, ccol)]
        if family in ("failed_to_fetch", "abort"):
            frames = [("q", vendor_bundle, 7, 22)] + frames
        if family == "json_html":
            frames = [("JSON.parse", None, 0, 0)] + frames
    elif where == "router":
        frames = [(fn, vendor_bundle, line, col), (f"async {caller}", page_bundle, cline, ccol)]
    elif where == "framework":
        frames = [(fn, vendor_bundle, line, col), (react, vendor_bundle, vline, vcol)]
    else:
        frames = [(fn, page_bundle, line, col), ("Array.map", None, 0, 0), (caller, page_bundle, cline, ccol),
                  (react, vendor_bundle, vline, vcol)]
    stack = _frames(engine, frames)
    if engine == "chromium":
        stack = f"{name}: {message}\n{stack}"
    return ctor, name, message, stack


def chunk_url(cdn, shell, release, route_chunk):
    return f"{cdn}/assets/{shell}/{route_chunk}-{content_hash(shell, release, route_chunk)}.js"
