# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""Fleet reconciliation: the pure part. See docs/fleet-reconcile-design.md.

Today this holds the deployment key. The snapshots, the record and
``reconcile()`` arrive here as the design is worked through, and
``model_status.py`` is absorbed then.

Pure: nothing here does I/O, and nothing here imports ``llmao.fleet``.
"""

from __future__ import annotations

from typing import NamedTuple
from urllib.parse import urlparse


def normalize_peer_ip(addr: str | None) -> str:
    """A host as the box protocol keys it: trimmed, IPv4-mapped IPv6 unwrapped."""
    ip = (addr or "").strip()
    if ip.startswith("::ffff:"):
        ip = ip[7:]
    return ip


def norm_base(url: object) -> str:
    """The canonical form of a base URL.

    The same deployment turns up with a trailing ``/v1`` (``VllmServer.api_base``,
    and the ``litellm_params.api_base`` copied from it) and without it
    (``asf_api_base``, routes pushed before the suffix), with and without a
    trailing slash, and with the host in whatever case the hand that typed it
    used. Comparisons must match across all of those, so: scheme and host
    lowercased, an IPv4-mapped IPv6 host unwrapped, trailing slashes gone, and
    one trailing ``/v1`` gone. Only a trailing ``/v1``: a path that merely
    contains it is left alone.

    (The suffix was once actively wrong: an older LiteLLM appended
    ``/v1/chat/completions`` to api_base, so a ``/v1``-suffixed base resolved
    to ``/v1/v1/...``, 404'd at the origin, and surfaced as an OpenAI
    authentication error naming platform.openai.com, which sends the reader
    after their own key rather than the route. The current LiteLLM appends only
    ``/chat/completions``, so the suffix is what vLLM needs now.)
    """
    s = str(url or "").strip().rstrip("/")
    if s.endswith("/v1"):
        s = s[:-3].rstrip("/")
    parsed = urlparse(s)
    if not parsed.scheme or not parsed.netloc:
        # Not a URL. The string rules above are all that can be applied.
        return s
    host = normalize_peer_ip(parsed.hostname)
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{host}:{parsed.port}" if parsed.port else host
    return f"{parsed.scheme.lower()}://{netloc}{parsed.path}"


class DeploymentKey(NamedTuple):
    """What every source calls one deployment: a canonical base URL and our model name.

    One shape for our boxes and for the providers we buy from. A box's URL is
    built from its LISTEN port, not the public port a provider maps it to, so a
    port remap keeps the identity. The model name is our public ``model_name``,
    not the provider's model string, so two of our names on one provider model
    are two keys.

    A NamedTuple, not the edict the rest of the codebase prefers: the key's job
    is to join records across sources, so it is used as a dict key and a set
    member, and an edict is a dict, which is unhashable. A NamedTuple hashes,
    compares as a tuple, cannot be changed after it is built, and still reads
    as ``key.url`` and ``key.model_name``.
    """

    url: str
    model_name: str


def deployment_key(
    model_name: str,
    *,
    host: str | None = None,
    listen_port: int | None = None,
    api_base: str | None = None,
) -> DeploymentKey:
    """The one function that builds a key.

    A box: ``host`` and ``listen_port``. A provider: ``api_base``, the base
    from config.yaml. Either way the URL goes through ``norm_base``, so the
    stored key, the comparison, and the stamp in LiteLLM's ``model_info`` are
    the same bytes.
    """
    name = str(model_name or "").strip()
    if not name:
        raise ValueError("deployment_key: model_name is required")
    if api_base is not None:
        if host is not None or listen_port is not None:
            raise ValueError("deployment_key: api_base or host and listen_port, not both")
        url = norm_base(api_base)
        if not url:
            raise ValueError("deployment_key: api_base is empty")
        return DeploymentKey(url, name)
    peer = normalize_peer_ip(host)
    if not peer or listen_port is None:
        raise ValueError("deployment_key: host and listen_port are required for a box")
    if ":" in peer:
        peer = f"[{peer}]"
    return DeploymentKey(norm_base(f"http://{peer}:{int(listen_port)}"), name)
