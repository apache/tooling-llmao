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

