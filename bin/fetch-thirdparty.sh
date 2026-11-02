#!/usr/bin/env bash

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

# Vendor the third-party assets into static/ (STeVe-style): Bootstrap
# CSS/JS, Bootstrap Icons, and Apache ECharts. Does not fetch SortableJS.
#
#   bin/fetch-thirdparty.sh                 # everything
#   bin/fetch-thirdparty.sh echarts         # one or more of: bootstrap icons echarts
#
# Each is pinned to a version below. After a Bootstrap or ECharts fetch,
# put the printed integrity value on the <link> or <script> that loads it.

set -euo pipefail

VERSION="5.3.1"
DIST="bootstrap-${VERSION}-dist"
B_URL="https://github.com/twbs/bootstrap/releases/download/v${VERSION}/${DIST}.zip"

I_VERSION="1.13.1"
I_DIST="bootstrap-icons-${I_VERSION}"
I_URL="https://github.com/twbs/icons/releases/download/v${I_VERSION}/${I_DIST}.zip"

E_VERSION="6.1.0"
# jsDelivr's mirror of the official npm package.
E_URL="https://cdn.jsdelivr.net/npm/echarts@${E_VERSION}/dist/echarts.min.js"

THIS_DIR=$(dirname "$(realpath "$0")")
PARENT_DIR=$(dirname "$THIS_DIR")
STATIC_DIR="${PARENT_DIR}/static"

mkdir -p "${STATIC_DIR}/css/fonts" "${STATIC_DIR}/js"

integrity() {
  echo -n "sha384-" ; openssl dgst -sha384 -binary "$1" | openssl base64 -A ; echo ""
}

# --------------------
# Bootstrap (CSS and JS)
# --------------------

fetch_bootstrap() {
  local zipfile="${THIS_DIR}/bs.zip"

  echo "Fetching: ${B_URL}"
  curl -q --location "${B_URL}" --output "${zipfile}"

  echo "Extracting: bootstrap.min.css"
  unzip -joq "${zipfile}" "${DIST}/css/bootstrap.min.css" -d "${STATIC_DIR}/css"
  echo "Extracting: bootstrap.min.css.map"
  unzip -joq "${zipfile}" "${DIST}/css/bootstrap.min.css.map" -d "${STATIC_DIR}/css"
  echo "Extracting: bootstrap.bundle.min.js"
  unzip -joq "${zipfile}" "${DIST}/js/bootstrap.bundle.min.js" -d "${STATIC_DIR}/js"
  echo "Extracting: bootstrap.bundle.min.js.map"
  unzip -joq "${zipfile}" "${DIST}/js/bootstrap.bundle.min.js.map" -d "${STATIC_DIR}/js"
  rm -f "${zipfile}"

  echo ""
  echo "Modify templates with new integrity values:"
  echo "bootstrap.min.css:"
  integrity "${STATIC_DIR}/css/bootstrap.min.css"
  echo "bootstrap.bundle.min.js:"
  integrity "${STATIC_DIR}/js/bootstrap.bundle.min.js"
  echo ""
}

# --------------------
# Bootstrap icons
# --------------------

fetch_icons() {
  local zipfile="${THIS_DIR}/icons.zip"

  echo "Fetching: ${I_URL}"
  curl -q --location "${I_URL}" --output "${zipfile}"

  echo "Extracting: bootstrap-icons.css"
  unzip -joq "${zipfile}" "${I_DIST}/bootstrap-icons.css" -d "${STATIC_DIR}/css"

  echo "Extracting: fonts/bootstrap-icons.woff2"
  unzip -joq "${zipfile}" "${I_DIST}/fonts/bootstrap-icons.woff2" -d "${STATIC_DIR}/css/fonts"
  echo "Extracting: fonts/bootstrap-icons.woff"
  unzip -joq "${zipfile}" "${I_DIST}/fonts/bootstrap-icons.woff" -d "${STATIC_DIR}/css/fonts"
  rm -f "${zipfile}"
}

# --------------------
# Apache ECharts
# --------------------

fetch_echarts() {
  local out="${STATIC_DIR}/js/echarts.min.js"
  local tmp="${out}.tmp.$$"

  echo "Fetching: ${E_URL}"
  curl -fsSL --connect-timeout 30 --max-time 120 "${E_URL}" -o "${tmp}"

  # The download is refused rather than installed when it is not the
  # library: a mirror's error page is small, and the real file names
  # itself near the top (its license header comes first, so look past it).
  local size
  size="$(wc -c < "${tmp}" | tr -d ' ')"
  if [[ "${size}" -lt 100000 ]]; then
    echo "error: download too small (${size} bytes); refusing to install" >&2
    rm -f "${tmp}"
    exit 1
  fi
  if ! head -c 5000 "${tmp}" | grep -q 'echarts\|!function'; then
    echo "error: download does not look like echarts.min.js" >&2
    rm -f "${tmp}"
    exit 1
  fi
  mv -f "${tmp}" "${out}"
  echo "wrote ${out} (${size} bytes)"

  echo ""
  echo "Modify templates with the new integrity value:"
  echo "echarts.min.js:"
  integrity "${out}"
  echo ""
}

# --------------------

if [[ $# -eq 0 ]]; then
  set -- bootstrap icons echarts
fi
for what in "$@"; do
  case "${what}" in
    bootstrap) fetch_bootstrap ;;
    icons) fetch_icons ;;
    echarts) fetch_echarts ;;
    *) echo "error: unknown asset '${what}' (bootstrap, icons, echarts)" >&2; exit 1 ;;
  esac
done
