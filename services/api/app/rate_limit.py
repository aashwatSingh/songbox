from __future__ import annotations

import os

from slowapi import Limiter
from slowapi.util import get_remote_address

# Per-IP, not per-tenant: X-Dev-Tenant-Id is a spoofable dev-only header (docs/PLAN.md open
# question 9), so keying on it would protect nothing against a deliberate abuser -- IP is the
# real, if coarse, backstop today. REDIS_URL defaults to the docker-compose Redis instance, which
# nothing else in this codebase uses yet (see the M7b design spec -- RQ was named in the original
# architecture doc but never actually wired up). headers_enabled=True so a 429 carries a real
# Retry-After header -- this is NOT slowapi's default.
limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=os.environ.get("REDIS_URL", "redis://localhost:6379"),
    headers_enabled=True,
)
