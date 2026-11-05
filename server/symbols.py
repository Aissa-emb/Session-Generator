"""Review, explicitly upload and record the complete release asset set."""

from xcorgen import web_symbols
from xcorgen.security import TransportError
from . import secrets


def selected(spec):
    return [
        (t, a)
        for t in spec["targets"]
        if t["platform"] == "web" and t["web"].get("symbolication")
        for a in web_symbols.assets(t)
    ]


def plan(spec):
    return {
        "endpoint": web_symbols.UPLOAD_URL,
        "assets": [
            {
                "target_id": t["id"],
                "app_id": t["embrace_app_id"],
                "release": a["release"],
                "url": a["url"],
                "bundle_id": a["bundle_id"],
                "bytes": len(a["bundle"]) + len(a["sourcemap"]),
            }
            for t, a in selected(spec)
        ],
    }


def ready(store, spec):
    missing = []
    for t, a in selected(spec):
        try:
            row = store.get(
                "settings", "symbol:" + t["embrace_app_id"] + ":" + a["bundle_id"]
            )
        except KeyError:
            row = {}
        if row.get("http_status") != 200:
            missing.append(a["bundle_id"])
    if missing:
        raise ValueError(
            f"Upload {len(missing)} release source maps before starting a live symbolicated run. Configure the Embrace Symbol Upload token on the server."
        )


def upload(svc, spec_id, confirm=False, author="user", transport=None):
    if confirm is not True:
        raise ValueError(
            "Review the source-map plan and provide confirm: true before upload."
        )
    spec = svc.store.get("scenario_specs", spec_id)["spec"]
    token = secrets.get(svc.store, "symbol_upload")
    if not token:
        raise ValueError("Configure the Embrace Symbol Upload token on the server.")
    receipts = []
    for t, a in selected(spec):
        key = "symbol:" + t["embrace_app_id"] + ":" + a["bundle_id"]
        try:
            prior = svc.store.get("settings", key)
        except KeyError:
            prior = {}
        if prior.get("http_status") == 200:
            receipts.append(prior)
            continue
        try:
            receipt = web_symbols.upload(
                a,
                t["embrace_app_id"],
                token,
                **({"transport": transport} if transport else {}),
            )
        except TransportError as error:
            if error.status in (401, 403):
                raise ValueError(
                    f"XCOR RUM rejected the Symbol Upload token for app {t['embrace_app_id']} (HTTP {error.status}). "
                    "Use a Symbol Upload token from Settings → API in the organization that owns this app."
                ) from None
            raise
        receipt["http_status"] = (
            200 if 200 <= receipt["http_status"] < 300 else receipt["http_status"]
        )
        receipts.append(
            svc.store.put(
                "settings", dict(receipt, id=key, author=author, release=a["release"])
            )
        )
    return {"uploaded": len(receipts), "receipts": receipts}
