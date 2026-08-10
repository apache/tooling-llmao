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

"""JSON API routes for llmao (STeVe-style module registration)."""

from __future__ import annotations

import asfquart
import asfquart.auth
from asfquart.auth import Requirements as R
from quart import jsonify, Response

from llmao.auth import current_identity
from llmao.seam import AuthzError

APP = asfquart.APP


def _err(status: int, message: str) -> Response:
    resp = jsonify({"error": {"message": message, "type": "llmao_error", "code": status}})
    resp.status_code = status
    return resp


@APP.get("/healthz")
async def healthz():
    return jsonify({"status": "ok"})


@APP.get("/v1/projects/<project>/usage")
@asfquart.auth.require
async def project_usage(project: str):
    seam = APP.config["LLMAO_SEAM"]
    ident = await current_identity(APP.cfg)
    assert ident is not None
    try:
        rows = await seam.project_activity(ident, project)
    except AuthzError as e:
        return _err(403, str(e))
    total = round(sum(r.get("cost_usd", 0.0) for r in rows), 6)
    return jsonify({
        "project": project,
        "entries": rows,
        "total_cost_usd": total,
        "count": len(rows),
    })


@APP.get("/v1/projects/<project>/budget")
@asfquart.auth.require({R.committer})
async def project_budget(project: str):
    seam = APP.config["LLMAO_SEAM"]
    ident = await current_identity(APP.cfg)
    assert ident is not None
    try:
        info = await seam.team_status(ident, project)
    except AuthzError as e:
        return _err(403, str(e))
    if info is None:
        return jsonify({"project": project, "provisioned": False})
    return jsonify({
        "project": project,
        "provisioned": True,
        "max_budget_usd": info.max_budget,
        "spend_usd": info.spend,
        "remaining_usd": round(max(0.0, info.max_budget - info.spend), 6),
    })
