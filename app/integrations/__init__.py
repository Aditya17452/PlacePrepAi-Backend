"""Third-party service clients.

One module per vendor, holding only the wire protocol: building a request, parsing a
response, verifying a signature. Business rules stay in `app.services`, so swapping a
vendor does not touch them.
"""

from __future__ import annotations

__all__: list[str] = []
