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
"""What can go wrong talking to LiteLLM, as the pages see it.

Here rather than beside the code that raises them because that code is in
more than one module, and each would otherwise have to import another for
the sake of an exception.
"""


class BudgetExceededError(Exception):
    """Raised when a team is over budget (mirrors litellm proxy's 4xx)."""


class BackendUnavailableError(Exception):
    """Raised when the litellm admin API times out or can't be reached."""
