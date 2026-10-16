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
# go to the volume. This image changes only when provision.sh or the vLLM
# version does.
#
#   docker build -t <registry>/llmao-vllm-box:<tag> hosting/runpod

# A pinned version, not :latest -- every pod created from the template
# would otherwise get whatever vLLM was newest that day.
FROM vllm/vllm-openai:v0.25.1

# supervisord is the one thing the stock image lacks: it restarts a vLLM
# server that dies without disturbing the others on the box.
RUN apt-get update \
 && apt-get install -y --no-install-recommends supervisor \
 && rm -rf /var/lib/apt/lists/*

COPY provision.sh /opt/llmao/provision.sh

# Replaces the stock ENTRYPOINT ["vllm", "serve"]. The template's start
# command must be left empty: anything there arrives as arguments to this.
ENTRYPOINT ["/opt/llmao/provision.sh"]
