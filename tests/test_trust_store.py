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

"""Roots distrusted on the host must also be absent from certifi.

certifi ships its own copy of the Mozilla roots, and httpx uses it for every
outbound request, so `update-ca-certificates` does not reach it. In October
2026 that gap meant the gateway trusted emSign ECC Root CA - G3 for a day
after the host had distrusted it.
"""

import pathlib

import certifi

# Roots distrusted in p6 / /etc/ca-certificates.conf. Keep in step.
DISTRUSTED = ("emSign ECC Root CA - G3",)


def test_certifi_does_not_carry_a_distrusted_root():
    bundle = pathlib.Path(certifi.where()).read_text(encoding="utf-8")
    present = [label for label in DISTRUSTED if f'Label: "{label}"' in bundle]
    assert not present, f"certifi {certifi.__version__} still trusts {present}"
