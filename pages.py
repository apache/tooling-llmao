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

"""HTML pages and static file routes for llmao (STeVe-style)."""

from __future__ import annotations

import pathlib

import asfquart
import asfquart.session
import quart
from easydict import EasyDict as edict

APP = asfquart.APP

THIS_DIR = pathlib.Path(__file__).resolve().parent
TEMPLATES = THIS_DIR / "templates"
STATICDIR = THIS_DIR / "static"


async def basic_info() -> edict:
    """Base-level EZT template data shared by HTML pages."""
    basic = edict()
    basic.title = "llmao"
    basic.flashes = []

    settings = APP.config["LLMAO_SETTINGS"]
    basic.llm_mode = settings.litellm_mode

    client_session = await asfquart.session.read()
    if client_session is not None and getattr(client_session, "uid", None):
        basic.uid = client_session.uid
        basic.name = getattr(client_session, "fullname", None) or client_session.uid
        projects = list(
            dict.fromkeys(
                list(getattr(client_session, "committees", None) or [])
                + list(getattr(client_session, "projects", None) or [])
            )
        )
        basic.projects_label = ", ".join(projects) if projects else None
    else:
        basic.uid = None
        basic.name = None
        basic.projects_label = None

    return basic


@APP.get("/")
@APP.use_template(TEMPLATES / "home.ezt")
async def home_page():
    result = await basic_info()
    result.title = "Home"
    return result


@APP.get("/static/<path:filename>")
async def serve_static(filename: str):
    return await quart.send_from_directory(STATICDIR, filename)
