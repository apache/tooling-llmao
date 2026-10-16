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

# The RunPod box: stock vLLM, plus supervisord and the boot script.
#
# Deliberately thin. The installer is NOT here: provision.sh fetches it on
# first boot, so a change to it needs no new image. Nor are weights; they
# go to the volume. This image changes only when provision.sh, supervisor
# or the vLLM version does.
#
# The final stage is COPY --link only, with no RUN. That is what keeps a
# build cheap: BuildKit never has to unpack the many-GB vLLM image to add a
# few hundred KB on top, so the runner needs neither the disk nor the time.
# A RUN in the final stage would undo that. Anything that needs a shell
# belongs in the vendor stage.
#
#   docker buildx build --push -t <registry>/llmao-vllm-box:<tag> hosting/runpod

# supervisor is pure Python with no dependencies, so it is installed here,
# in a small image, and copied across. Hash-pinned in requirements.txt.
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS vendor
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --no-deps --require-hashes --target /vendor -r /tmp/requirements.txt

# Pinned by digest, not :latest -- every pod created from the template would
# otherwise get whatever vLLM was newest that day. This is v0.25.1.
FROM vllm/vllm-openai:v0.25.1@sha256:e4f88a835143cd22aee2397a26ec6bb80b3a4a6fe0c882bcbc63822904766089

# supervisord restarts a vLLM server that dies without disturbing the others
# on the box. bin/ holds `supervisord` and `supervisorctl` commands that run
# the vendored copy, so both work as they would from a package.
COPY --link --from=vendor /vendor /opt/llmao/vendor
COPY --link --chmod=0755 bin/ /usr/local/bin/
COPY --link --chmod=0755 provision.sh /opt/llmao/provision.sh

# Replaces the stock ENTRYPOINT ["vllm", "serve"]. The template's start
# command must be left empty: anything there arrives as arguments to this.
ENTRYPOINT ["/opt/llmao/provision.sh"]
