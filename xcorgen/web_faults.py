"""JavaScript exception families, with the message and stack format each browser engine produces.

Exceptions are recorded the way EmbraceLogManager records them: exception.type is the
constructor name, exception.name is error.name, the body and exception.message are the
message, and exception.stacktrace is error.stack (Chromium prefixes "Name: message" and
uses "    at fn (url:line:col)"; WebKit and Gecko use "fn@url:line:col" with no header).
The backend picks the stack parser from the first matching line, so a Chromium message
never contains "@".

Function names, positions and browser-specific messages come from exceptions actually
raised by compiled application modules. Source maps resolve every emitted file frame.
Some browser-native fetch/import errors genuinely have no application stack.
"""
from .web_sites import content_hash

# Public family names used by spec validation. Messages and frames live in the
# browser-captured corpus, not a separate hand-maintained approximation.
FAMILIES = dict.fromkeys((
    "contract_null", "view_state", "property_undefined", "not_a_function", "chunk_load", "webpack_chunk_load",
    "json_html", "failed_to_fetch", "abort", "quota_exceeded", "hydration", "script_error",
))
# Default handling per family: how the error reaches the SDK.
HANDLING = {"failed_to_fetch": "unhandled_rejection", "abort": "unhandled_rejection", "json_html": "unhandled_rejection",
            "chunk_load": "unhandled_rejection", "webpack_chunk_load": "unhandled_rejection",
            "script_error": "unhandled_error", "hydration": "unhandled_error"}


def exception(family, engine, release, page_bundle, vendor_bundle, chunk_url=None, chunk_id=None):
    """(exception.type, exception.name, message, exception.stacktrace) for one occurrence."""
    if family == "script_error":
        return "String", "String", "Script error.", ""
    from .web_symbols import captured_exception, format_frames
    sample = captured_exception(page_bundle, family, engine)
    message = sample["message"].replace("{chunk}", chunk_url or "").replace("{chunk_id}", chunk_id or "")
    stack = format_frames(sample["frames"], page_bundle, engine)
    if engine == "chromium":
        stack = f'{sample["name"]}: {message}' + ("\n" + stack if stack else "")
    return sample["type"], sample["name"], message, stack


def chunk_url(cdn, shell, release, route_chunk):
    return f"{cdn}/assets/{shell}/{route_chunk}-{content_hash(shell, release, route_chunk)}.js"
