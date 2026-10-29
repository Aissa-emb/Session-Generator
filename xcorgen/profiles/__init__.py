"""App profiles: a fictional brand's web app, its API and its backend services."""

from ..backend import Service


class Page:
    def __init__(self, template, label, path=None, bundle="/assets/index.js", lcp_target="main img",
                 lcp_element="img", heavy=False, shifty=False, shift_target="div.content", doc_height=2400):
        self.template, self._label, self._path = template, label, path
        self.bundle, self.lcp_target, self.lcp_element = bundle, lcp_target, lcp_element
        self.heavy, self.shifty, self.shift_target, self.doc_height = heavy, shifty, shift_target, doc_height

    def path(self, params):
        return self._path(params) if self._path else self.template

    def label(self, params):
        return self._label(params) if callable(self._label) else self._label


class Endpoint:
    """build(ctx) -> (root Node, HTTP status, response data dict)."""

    def __init__(self, method, route, path, build, outcomes=(("ok", 1),), size=(900, 7000)):
        self.method, self.route, self._path, self.build = method, route, path, build
        self.outcomes, self.size = list(outcomes), size

    def path(self, params):
        return self._path(params) if callable(self._path) else self._path


class Profile:
    def __init__(self, **kw):
        self.__dict__.update(kw)


# When one Embrace app ID serves several profiles, the sessions come from one super app with a line of
# business per profile, told apart by the business_unit session property.
SHARED_ANDROID_APP = dict(
    package="com.vantra.android", app_name="Vantra", activity="com.vantra.android.ui.MainActivity",
    versions=[("10.28.0", "1028004", "9.3.1", 61), ("10.27.3", "1027032", "9.3.1", 30), ("10.25.1", "1025011", "9.3.0", 9)],
    user_agent="Vantra/{version} (Android {os}; {model}) okhttp/4.12.0", ui="compose",
)
ENTRY_POINTS = [("home_tab", 55), ("push_notification", 15), ("deep_link", 12), ("widget", 6), ("search", 12)]


def services(namespace, cluster, specs, region="us-east-1"):
    """specs: list of (name, lang, version, k8s namespace, replicas)."""
    return {name: Service(name, lang, version, k8s_ns, namespace, cluster, region, replicas,
                          app_scope=f"com.{namespace.replace('-', '.')}.{name.replace('-', '.')}" if lang == "java" else name)
            for name, lang, version, k8s_ns, replicas in specs}


def load(key):
    if key == "ecommerce":
        from .ecommerce import PROFILE
    elif key == "banking":
        from .banking import PROFILE
    elif key == "travel":
        from .travel import PROFILE
    else:
        raise ValueError(f"unknown profile {key!r}")
    return PROFILE


PROFILES = ("ecommerce", "banking", "travel")
