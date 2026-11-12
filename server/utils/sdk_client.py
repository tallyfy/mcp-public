"""
The one way the server builds a Tallyfy SDK client (tallyfy/mcp#1545).

``TallyfySDK`` here is the SDK's own ``tallyfy.TallyfySDK``, with the server's
metrics request hook (``utils.sdk_metrics_patch.record_request``) registered on
every instance through the SDK's public ``add_request_hook``. It takes exactly
the SDK's arguments and behaves exactly as the SDK does otherwise.

Every module under ``server/`` imports ``TallyfySDK`` from here, never from
``tallyfy``, so every call it makes is counted in ``tallyfy_api_*``. A
module that must build the SDK's own class instead (``connect_membership``
looks ``tallyfy.TallyfySDK`` up at call time) passes
``on_request=record_request`` itself. ``tests/unit/server/utils/test_sdk_client.py``
sweeps ``server/`` and fails on a client built any other way.

The name is kept, rather than a ``make_sdk()`` function, so every tool keeps
its ``with TallyfySDK(api_key=..., base_url=...) as sdk:`` line and every test
that patches ``tools.<module>.TallyfySDK`` keeps working.
"""

import tallyfy

from utils.sdk_metrics_patch import record_request


class TallyfySDK(tallyfy.TallyfySDK):
    """``tallyfy.TallyfySDK`` with the server's metrics hook registered."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.add_request_hook(record_request)
