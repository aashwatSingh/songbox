from __future__ import annotations

import os

from slowapi import Limiter
from slowapi.util import get_remote_address

# Per-IP, not per-tenant/per-identity: real authentication landed in M8 (docs/PLAN.md open
# question 9, now resolved -- see docs/adr/0002-authentication-model.md), but per-IP limiting
# remains the right choice for unauthenticated, credential-bearing endpoints like
# POST /auth/login and POST /auth/signup regardless -- an anonymous attacker hasn't signed in yet,
# so there is no real identity to key on or spoof in the first place; IP is the real, if coarse,
# backstop for exactly this class of route. REDIS_URL defaults to the docker-compose Redis
# instance, which nothing else in this codebase uses yet. headers_enabled=True so a 429 carries a
# real Retry-After header -- this is NOT slowapi's default.
#
# Deployment note for whenever this runs behind a real reverse proxy/load balancer (not yet --
# see M7c): uvicorn's default proxy_headers=True with forwarded_allow_ips defaulting to
# "127.0.0.1" means request.client.host (what get_remote_address reads) becomes the PROXY's
# address for every request once a proxy is introduced, silently turning every "per-IP" limit
# here into one shared global limit -- and making the access-log client_ip field constant and
# useless. Set FORWARDED_ALLOW_IPS to the proxy's real address when that day comes.
limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=os.environ.get("REDIS_URL", "redis://localhost:6379"),
    headers_enabled=True,
    # slowapi's key_style defaults to "url", which scopes each rate-limit counter to the
    # LITERAL request path -- including the track_id in a path like /tracks/<uuid>/separate.
    # That means varying the track_id across requests would give each one a fresh bucket, making
    # every path-parameterized limit in this file trivially bypassable (verified during final
    # review: 25 wrong-admin-key takedown attempts with 25 different track_ids produced zero
    # 429s). "endpoint" scopes the counter to the view/dependency function instead, so all calls
    # to the same route -- regardless of path parameters -- share one bucket.
    key_style="endpoint",
)
