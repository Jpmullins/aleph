"""The composition root: capabilities every Aleph process boots.

Lives in a package rather than in one of the apps because two processes need
it — the API and the arq workers — and an app may not import another app. It
was duplicated between them before, which is why the worker's teardown had the
same bare-await bug the API's did, fixed twice or not at all.

A process picks what it needs with a boot manifest. Nothing here decides what
runs; the manifest does.
"""

from aleph_runtime.capabilities import (
    AGENT_STORE,
    AGENT_STORE_POOL,
    ASSET_STORE,
    BOUND_KEYS,
    CHANGE_BROKER,
    DB_ENGINE,
    DB_SESSIONS,
    GATEWAY_CATALOG,
    HTTP_AUTH,
    HTTP_CONSENSUS,
    HTTP_GATEWAY,
    LITELLM,
    NOTIFY_LISTENER,
    PRICING,
    REDIS,
    SCHOLAR,
    SETTINGS,
    bind_to_app_state,
)

__all__ = [
    "AGENT_STORE",
    "AGENT_STORE_POOL",
    "ASSET_STORE",
    "BOUND_KEYS",
    "CHANGE_BROKER",
    "DB_ENGINE",
    "DB_SESSIONS",
    "GATEWAY_CATALOG",
    "HTTP_AUTH",
    "HTTP_CONSENSUS",
    "HTTP_GATEWAY",
    "LITELLM",
    "NOTIFY_LISTENER",
    "PRICING",
    "REDIS",
    "SCHOLAR",
    "SETTINGS",
    "bind_to_app_state",
]
