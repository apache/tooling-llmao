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


"""hosting/runpod/provision.sh under bash, with curl and supervisord stubbed.

The installer it fetches is a stand-in that writes one unit, so these test
the boot script's own job: provision once, retry until it works, and fetch
nothing once provisioned.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "hosting" / "runpod" / "provision.sh"

# Stands in for install_set.py: proves what it was handed, writes one unit.
FAKE_INSTALLER = """\
import os, sys
assert sys.argv[1:] == ["--no-reload"], sys.argv
assert os.environ["LLMAO_HOST"] == "198.51.100.8"
conf = os.environ["SUPERVISOR_CONF_DIR"]
open(os.path.join(conf, "vllm-model-a.conf"), "w").write("[program:vllm-model-a]\\n")
"""

# Fails the first $STUB_CURL_FAILS calls, then "downloads" the installer.
CURL = """\
#!/bin/bash
n=$(( $(cat "$STUB_DIR/curl.calls" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$STUB_DIR/curl.calls"
[ "$n" -le "${STUB_CURL_FAILS:-0}" ] && exit 22
while [ $# -gt 0 ]; do [ "$1" = -o ] && out="$2"; shift; done
cp "$STUB_DIR/installer.py" "$out"
"""

SUPERVISORD = """\
#!/bin/bash
echo "$*" > "$STUB_DIR/supervisord.args"
"""


@pytest.fixture
def box(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("no bash")
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name, text in (("curl", CURL), ("supervisord", SUPERVISORD)):
        (stubs / name).write_text(text)
        (stubs / name).chmod(0o755)
    (stubs / "installer.py").write_text(FAKE_INSTALLER)
    workspace = tmp_path / "workspace"
    env = {
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "STUB_DIR": str(stubs),
        "FLEET_KEY": "fleet-secret",
        "ASFQUART_URL": "https://llm.example.org",
        "RUNPOD_PUBLIC_IP": "198.51.100.8",
        "DATA_DIRECTORY": str(workspace),
        "LLMAO_SUPERVISORD_CONF": str(tmp_path / "etc" / "supervisord.conf"),
        "LLMAO_RUN_DIR": str(tmp_path / "run"),
        "LLMAO_RETRY_FIRST_S": "0",
    }

    def boot(**extra):
        return subprocess.run(["bash", str(SCRIPT)], env={**env, **extra}, capture_output=True, text=True, timeout=30)

    return boot, stubs, workspace, tmp_path


def _calls(stubs):
    return int((stubs / "curl.calls").read_text()) if (stubs / "curl.calls").exists() else 0


def test_first_boot_provisions_and_execs_supervisord(box):
    boot, stubs, workspace, tmp_path = box
    result = boot()
    assert result.returncode == 0, result.stderr
    state = workspace / ".llmao"
    assert (state / "provisioned").exists()
    assert (state / "conf.d" / "vllm-model-a.conf").exists()
    conf = tmp_path / "etc" / "supervisord.conf"
    assert (stubs / "supervisord.args").read_text().split() == ["-n", "-c", str(conf)]
    text = conf.read_text()
    assert f"files = {state / 'conf.d'}/*.conf" in text
    # The socket is on the container disk, never the volume.
    assert f"file={tmp_path / 'run'}/supervisor.sock" in text


def test_provisioned_boot_fetches_nothing(box):
    boot, stubs, workspace, _ = box
    assert boot().returncode == 0
    assert _calls(stubs) == 1
    result = boot()
    assert result.returncode == 0, result.stderr
    assert _calls(stubs) == 1
    assert "already provisioned" in result.stderr
    # The units survive: a restart serves the same set.
    assert (workspace / ".llmao" / "conf.d" / "vllm-model-a.conf").exists()


def test_retries_until_the_fetch_succeeds(box):
    boot, stubs, workspace, _ = box
    result = boot(STUB_CURL_FAILS="2")
    assert result.returncode == 0, result.stderr
    assert _calls(stubs) == 3
    assert result.stderr.count("provisioning failed") == 2
    assert (workspace / ".llmao" / "provisioned").exists()


def test_a_failing_installer_is_retried_and_leaves_no_marker_until_it_works(box):
    boot, stubs, workspace, _ = box
    good = (stubs / "installer.py").read_text()
    # Fails on its first run, as /vllm/config does before the fleet.hosts row exists.
    (stubs / "installer.py").write_text(
        "import os, sys\n"
        "flag = os.path.join(os.environ['STUB_DIR'], 'ran')\n"
        "if not os.path.exists(flag):\n"
        "    open(flag, 'w').close()\n"
        "    sys.exit('config fetch HTTP 403')\n" + good
    )
    result = boot()
    assert result.returncode == 0, result.stderr
    assert result.stderr.count("provisioning failed") == 1
    assert (workspace / ".llmao" / "provisioned").exists()


def test_no_public_ip_refuses_to_start(box):
    boot, stubs, workspace, _ = box
    result = boot(RUNPOD_PUBLIC_IP="")
    assert result.returncode != 0
    assert "RUNPOD_PUBLIC_IP" in result.stderr
    assert not (workspace / ".llmao" / "provisioned").exists()
    assert _calls(stubs) == 0
