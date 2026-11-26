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

"""The two key tables render, from rows the page's own helper produced.

/keys and /keys/other both died on `result.keys = _key_rows(...)`: Box
refuses an attribute named after a dict method, nothing catches BoxKeyError,
and the 500 handler bounced the person home with a flash. Every test passed
throughout, because nothing rendered either template.

These tests go through `pages._key_rows` rather than hand-built rows, so the
row shape is the real one and a field the template reads cannot quietly stop
being produced.
"""

import io
import pathlib

import ezt
from box import Box

from llmao.proxy.client import KeyInfo

THIS_DIR = pathlib.Path(__file__).resolve().parent.parent


def _pages():
    import asfquart

    if asfquart.APP is None:
        asfquart.construct(
            "llmao", app_dir=str(THIS_DIR), cfg_file="config.yaml.example", oauth=False, force_login=False
        )
    import pages

    return pages


def _chrome(**fields):
    """What header, footer, flashes and admin-nav read on every page."""
    base = Box(
        title="My Keys",
        nav="keys",
        admin_nav="keys",
        login_back="/keys",
        uid="someone@example.apache.org",
        name="Some One",
        is_site_admin=ezt.boolean(False),
        admin_at_heart=ezt.boolean(False),
        as_user=ezt.boolean(False),
        commit="deadbeef",
        repo="https://github.com/apache/tooling-llmao",
        restarted_at="2026-11-25 18:30 UTC",
        restarted_ago="1h ago",
        flashes=[],
        notices=[],
        work=None,
    )
    base.update(fields)
    return base


def _render(template_name, data):
    buf = io.StringIO()
    template = ezt.Template(
        str(THIS_DIR / "templates" / template_name), compress_whitespace=0, base_format=ezt.FORMAT_HTML
    )
    template.generate(buf, data)
    return buf.getvalue()


def _personal_key(**over):
    fields = dict(
        token_id="tok-personal",  # noqa: S106 -- an id, not a secret
        project="tooling",
        user="someone",
        purpose="laptop",
        team_id="team-1",
        spend=1.5,
        max_budget=10.0,
        created_at="2026-11-01T00:00:00Z",
        last_used="2026-11-24T00:00:00Z",
    )
    fields.update(over)
    return KeyInfo(**fields)


def _automation_key(**over):
    fields = dict(
        token_id="tok-service",  # noqa: S106 -- an id, not a secret
        project="tooling",
        user=None,
        purpose="gofannon",
        team_id="team-1",
        spend=0.25,
        max_budget=None,
        created_at="2026-11-02T00:00:00Z",
        last_used=None,
        created_by="akm",
    )
    fields.update(over)
    return KeyInfo(**fields)


def test_my_keys_table_renders_a_personal_key():
    pages = _pages()
    rows = pages._key_rows([_personal_key()], after_path="/keys", used_by_token={"tok-personal": 12345})
    data = _chrome(
        has_project_keys=ezt.boolean(True),
        allowance=None,
        projects=[],
        approvals=[],
        my_requests=[],
    )
    data["keys"] = rows
    html = _render("keys.ezt", data)
    assert "laptop" in html
    assert "tok-personal" in html
    # The used column came from used_by_token rather than the dash.
    assert "—" not in html.split("laptop")[1].split("</tr>")[0]


def test_my_keys_table_renders_when_there_are_none():
    pages = _pages()
    data = _chrome(
        has_project_keys=ezt.boolean(False),
        allowance=None,
        projects=[],
        approvals=[],
        my_requests=[],
    )
    data["keys"] = pages._key_rows([])
    html = _render("keys.ezt", data)
    assert "No personal keys yet" in html


def test_service_keys_table_renders_an_automation_key():
    pages = _pages()
    rows = pages._key_rows([_automation_key()], after_path="/keys/other")
    data = _chrome(title="Service keys", can_create_automation=ezt.boolean(True))
    data["keys"] = rows
    html = _render("keys_other.ezt", data)
    assert "gofannon" in html
    assert "tok-service" in html
    # created_by is the only record of who saw the secret, so it must show.
    assert "akm" in html


def test_service_keys_table_renders_when_there_are_none():
    pages = _pages()
    data = _chrome(title="Service keys", can_create_automation=ezt.boolean(False))
    data["keys"] = pages._key_rows([])
    html = _render("keys_other.ezt", data)
    assert "No automation keys" in html
