"""Instance-local asyncua server correction used only by the project's simulator and protocol fixtures.

asyncua 2.0.1 emits an idle keep-alive after MaxKeepAliveCount+2 publishing cycles.  Production clients and
third-party servers are untouched; this adapter corrects only Server instances created by this project.
"""

from __future__ import annotations

from importlib.metadata import version
from types import MethodType

ASYNCUA_VERSION = "2.0.1"


def install_keepalive_adapter(server) -> None:
    """Make one initialized asyncua Server emit idle keep-alives at the negotiated count.

    This intentionally uses the locked server's private per-subscription fields.  It patches the one
    SubscriptionService instance, never the asyncua class or site-packages.
    """
    found = version("asyncua")
    if found != ASYNCUA_VERSION:
        raise RuntimeError(f"HTSG simulator keep-alive adapter requires asyncua {ASYNCUA_VERSION}; found {found}")
    service = server.iserver.subscription_service
    if getattr(service, "_htsg_keepalive_adapter", False):
        return
    create = service.create_subscription

    async def create_subscription(self, params, callback, session_id, request_callback=None):
        result = await create(params, callback, session_id, request_callback)
        subscription = self.subscriptions[result.SubscriptionId]

        def has_published_results(instance):
            if instance._startup or instance._triggered_datachanges or instance._triggered_events:
                return True
            instance._keep_alive_count += 1
            return instance._keep_alive_count >= instance.data.RevisedMaxKeepAliveCount

        subscription.has_published_results = MethodType(has_published_results, subscription)
        return result

    service.create_subscription = MethodType(create_subscription, service)
    service._htsg_keepalive_adapter = True
