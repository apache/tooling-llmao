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

"""A site admin operates as a normal user from the name menu.

The checkbox is only offered to an admin. Ticked, the admin loses their
standing everywhere, not only in the navigation: the Admin link goes, and
/admin refuses them. Unticked, it all comes back. The flag belongs to the
uid that set it.
"""

import asyncio
import pathlib

import asfquart
import asfquart.session

from llmao import auth

THIS_DIR = pathlib.Path(__file__).resolve().parent.parent


def _app():
    if asfquart.APP is None:
        asfquart.construct(
            "llmao", app_dir=str(THIS_DIR), cfg_file="config.yaml.example", oauth=False, force_login=False
        )
    import pages

    return pages


class _Seam:
    """Enough seam for the page frame: nothing to do, nothing to tell."""

    async def work_count(self, _ident):
        return 0

    async def notices(self, _ident):
        return []


def _signed_in(monkeypatch, *, uid="someone", root=False):
    async def read(*_a, **_kw):
        return asfquart.session.ClientSession({"uid": uid, "pmcs": [], "projects": ["tooling"], "isRoot": root})

    monkeypatch.setattr(asfquart.session, "read", read)
    monkeypatch.setattr(_app().APP, "seam", _Seam(), raising=False)


async def _toggle(client, on: bool, referer="http://localhost/help"):
    form = {"on": "1"} if on else {}
    return await client.post("/do-act-as-user", form=form, headers={"Referer": referer})


async def _flag(client):
    async with client.session_transaction() as sess:
        return sess.get(auth.AS_USER)


def test_an_admin_sees_the_checkbox_and_a_user_does_not(monkeypatch):
    _signed_in(monkeypatch, root=True)
    client = _app().APP.test_client()
    html = asyncio.run(_text(client, "/help"))
    assert 'id="actAsUser"' in html
    assert 'href="/admin"' in html

    _signed_in(monkeypatch, root=False)
    html = asyncio.run(_text(client, "/help"))
    assert 'id="actAsUser"' not in html
    assert 'href="/admin"' not in html


async def _text(client, path):
    response = await client.get(path)
    return (await response.get_data()).decode()


def test_ticked_the_admin_is_a_normal_user_everywhere(monkeypatch):
    _signed_in(monkeypatch, root=True)
    client = _app().APP.test_client()

    async def run():
        response = await _toggle(client, True)
        assert response.status_code == 303
        assert response.headers["Location"] == "/help"
        assert await _flag(client) == "someone"
        html = await _text(client, "/help")
        # The navigation is a normal user's, with the checkbox still there to undo it.
        assert 'href="/admin"' not in html
        assert 'id="actAsUser" checked' in html
        assert ">as user<" in html
        # And so is the standing: /admin bounces like it would anyone.
        response = await client.get("/admin")
        assert response.status_code == 302

        response = await _toggle(client, False)
        assert response.status_code == 303
        assert await _flag(client) is None
        html = await _text(client, "/help")
        assert 'href="/admin"' in html
        assert 'id="actAsUser"' in html
        assert "checked" not in html.split('id="actAsUser"', 1)[1].split(">", 1)[0]

    asyncio.run(run())


def test_the_flag_belongs_to_the_uid_that_set_it(monkeypatch):
    _signed_in(monkeypatch, uid="first", root=True)
    client = _app().APP.test_client()

    async def run():
        _ = await _toggle(client, True)
        assert await _flag(client) == "first"
        # Another admin signs in on the same browser: they are an admin.
        _signed_in(monkeypatch, uid="second", root=True)
        html = await _text(client, "/help")
        assert 'href="/admin"' in html
        assert ">as user<" not in html

    asyncio.run(run())


def test_a_normal_user_cannot_set_the_flag(monkeypatch):
    _signed_in(monkeypatch, root=False)
    client = _app().APP.test_client()

    async def run():
        response = await _toggle(client, True)
        assert response.status_code == 303
        assert await _flag(client) is None

    asyncio.run(run())
