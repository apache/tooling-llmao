#!/usr/bin/env -S uv run --script
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

"""Prepare a local PostgreSQL database for LiteLLM (developer laptop).

Assumes Ubuntu-style system PostgreSQL is installed and running (not
containers). Creates role + database if needed, generates a password,
runs prisma generate + db push against LiteLLM's schema.

Usage:
  uv run python bin/setup_litellm_db.py

Then paste the printed database_url into litellm.yaml (general_settings).
"""
from __future__ import annotations

import os
import pathlib
import secrets
import shutil
import subprocess
import sys
import urllib.parse

# Defaults aligned with litellm.yaml.example
DB_USER = "llmao"
DB_NAME = "litellm"
DB_HOST = "127.0.0.1"
DB_PORT = "5432"


def _die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def _run(cmd: list[str], *, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    print("+", " ".join(cmd), file=sys.stderr)
    return subprocess.run(cmd, env=env, check=check)


def _psql_as_postgres(sql: str) -> None:
    """Run SQL as the postgres OS user (peer auth on Ubuntu)."""
    if shutil.which("sudo") is None:
        _die("sudo is required to run psql as the postgres user")
    if shutil.which("psql") is None:
        _die("psql not found; install postgresql client/server packages")
    _run(["sudo", "-u", "postgres", "psql", "-v", "ON_ERROR_STOP=1", "-c", sql])


def _ensure_role_and_db(password: str) -> None:
    # Dollar-quote delimiters only (not part of the password): $pwXXXX$…$pwXXXX$
    tag = "pw" + secrets.token_hex(4)
    # CREATE ROLE if missing; always set password so re-runs stay usable.

    _psql_as_postgres(
        f"""
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{DB_USER}') THEN
    CREATE ROLE {DB_USER} LOGIN PASSWORD ${tag}${password}${tag}$;
  ELSE
    ALTER ROLE {DB_USER} WITH LOGIN PASSWORD ${tag}${password}${tag}$;
  END IF;
END
$$;
"""
    )
    # CREATE DATABASE cannot run inside a DO block.
    r = subprocess.run(
        ["sudo", "-u", "postgres", "psql", "-tAc",
         f"SELECT 1 FROM pg_database WHERE datname = '{DB_NAME}'"],
        check=True,
        capture_output=True,
        text=True,
    )
    if r.stdout.strip() != "1":
        _psql_as_postgres(
            f"CREATE DATABASE {DB_NAME} OWNER {DB_USER};"
        )
    else:
        _psql_as_postgres(
            f"ALTER DATABASE {DB_NAME} OWNER TO {DB_USER};"
        )
    _psql_as_postgres(
        f"GRANT ALL PRIVILEGES ON DATABASE {DB_NAME} TO {DB_USER};"
    )


def _litellm_schema_path() -> pathlib.Path:
    import litellm

    path = pathlib.Path(litellm.__file__).resolve().parent / "proxy" / "schema.prisma"
    if not path.is_file():
        _die(f"LiteLLM schema not found at {path}")
    return path


def _prisma_bin() -> str:
    # Prefer venv prisma from uv/path
    which = shutil.which("prisma")
    if which:
        return which
    _die("prisma CLI not found on PATH; run: uv sync  (needs litellm[proxy,extra-proxy])")


def main() -> None:
    print("Checking PostgreSQL (system server, not containers)…", file=sys.stderr)
    try:
        _psql_as_postgres("SELECT 1;")
    except subprocess.CalledProcessError as e:
        _die(
            "Cannot talk to PostgreSQL as user postgres. "
            "Install and start Ubuntu postgresql, then re-run.\n"
            f"Detail: {e}"
        )

    password = secrets.token_urlsafe(24)
    print("Creating role/database if needed…", file=sys.stderr)
    _ensure_role_and_db(password)

    # URL-encode password for connection string
    userenc = urllib.parse.quote(DB_USER, safe="")
    pwenc = urllib.parse.quote(password, safe="")
    database_url = f"postgresql://{userenc}:{pwenc}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

    schema = _litellm_schema_path()
    prisma = _prisma_bin()
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url

    print(f"prisma generate --schema {schema}", file=sys.stderr)
    _run([prisma, "generate", "--schema", str(schema)], env=env)

    print(f"prisma db push --schema {schema}", file=sys.stderr)
    _run([prisma, "db", "push", "--schema", str(schema), "--skip-generate"], env=env)

    print()
    print("Database ready. Put this in litellm.yaml under general_settings:")
    print()
    print(f"  database_url: {database_url}")
    print()
    print("Password (store securely; not printed again by this script):")
    print(password)
    print()
    print("Next: ensure master_key is set, then: make proxy")


if __name__ == "__main__":
    main()
