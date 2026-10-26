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

"""A page that cannot be shown bounces the person somewhere with a flash.

Two ways out of a page: the handler raises something @page catches (a
redirect, through the template shim), and the handler raises something
nothing catches (the 500 handler).
"""

import asyncio
import pathlib

import asfquart
import asfquart.session

THIS_DIR = pathlib.Path(__file__).resolve().parent.parent


def _app():
    if asfquart.APP is None:
        asfquart.construct(
            "llmao", app_dir=str(THIS_DIR), cfg_file="config.yaml.example", oauth=False, force_login=False
        )
    import pages

    return pages


def _signed_in(monkeypatch, *, pmcs=()):
    async def read(*_a, **_kw):
        return asfquart.session.ClientSession({"uid": "someone", "pmcs": list(pmcs), "projects": ["tooling"]})

    monkeypatch.setattr(asfquart.session, "read", read)


async def _get(path):
    client = _app().APP.test_client()
    response = await client.get(path)
    async with client.session_transaction() as sess:
        flashes = list(sess.get("_flashes", []))
    return response, flashes


def test_a_caught_refusal_is_a_redirect_not_a_500(monkeypatch):
    _signed_in(monkeypatch)
    response, flashes = asyncio.run(_get("/admin/capacity"))
    assert response.status_code == 302
    assert response.headers["Location"] == "/"
    assert flashes == [("warning", "Capacity is limited to site admins.")]


def test_an_uncaught_exception_goes_home_with_a_flash(monkeypatch):
    pages = _app()
    _signed_in(monkeypatch)
    monkeypatch.setitem(pages.APP.config, "PROPAGATE_EXCEPTIONS", False)

    async def broken(_title):
        raise RuntimeError("an application bug")

    monkeypatch.setattr(pages, "basic_info", broken)
    response, flashes = asyncio.run(_get("/admin/capacity"))
    assert response.status_code == 302
    assert response.headers["Location"] == "/"
    assert flashes == [("danger", "Something went wrong on that page. It has been logged.")]


def test_the_home_page_failing_does_not_bounce_to_itself(monkeypatch):
    pages = _app()
    monkeypatch.setitem(pages.APP.config, "PROPAGATE_EXCEPTIONS", False)

    async def broken(_title):
        raise RuntimeError("an application bug")

    monkeypatch.setattr(pages, "basic_info", broken)
    response, _flashes = asyncio.run(_get("/"))
    assert response.status_code == 500
