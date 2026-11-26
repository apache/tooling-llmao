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

"""Template data named after a dict method must be set as an item.

Page data is a Box, and Box refuses to set an attribute whose name is a
dict method -- `result.keys = rows` raises BoxKeyError. easydict allowed it,
so /keys and /keys/other broke on the move off easydict rather than on any
change to those pages. Nothing catches BoxKeyError, so the 500 handler
flashed "Something went wrong on that page" and bounced the person home.

`result["keys"] = rows` is the fix, and it is sound rather than a dodge:
ezt.Template.generate copies a dict-like straight into a plain object's
__dict__, so by the time the template resolves a name there is no Box left
to protect anything. Both halves of that are pinned below, because the fix
reads like a workaround and the obvious tidy-up -- putting the dot back --
would break the page again.

The protection is a scan rather than more rendering: these two pages have
render tests in test_keys_pages.py, and the scan covers the ones that do not.
"""

import io
import pathlib
import re

import ezt
import pytest
from box import Box

THIS_DIR = pathlib.Path(__file__).resolve().parent.parent

# Box derives this from dir(dict). Spelling it out keeps the test honest
# about what it checks; a dict method added to Python would be added here
# the first time it bit.
PROTECTED = frozenset(n for n in dir(dict) if not n.startswith("_"))

# Attribute assignment, which is the form Box refuses.
PY_ATTR_ASSIGN = re.compile(r"^\s*([A-Za-z_][\w.]*)\.(\w+)\s*=(?!=)", re.M)

# Names that hold template data. Scoped deliberately: a plain class may own
# an attribute called `keys` with no Box involved, as LiteLLMBackend.keys
# does for its KeyManager.
DATA_OBJECTS = frozenset({"result", "basic", "data", "row"})


def test_box_refuses_the_attribute_but_keeps_the_item():
    """The premise. Without this, the rule below is cargo cult."""
    box = Box()
    with pytest.raises(Exception, match="protected"):
        box.keys = ["a"]

    box["keys"] = ["a"]
    assert box["keys"] == ["a"]
    # Reading it back as an attribute still gives the method, which is why
    # nothing may read this name off the Box itself.
    assert callable(box.keys)


def test_ezt_flattens_top_level_data_so_an_item_named_keys_renders():
    """What makes `result["keys"]` correct and not merely quiet.

    generate() copies the mapping into a plain object, so the name reaches
    the template as ordinary instance state.
    """
    template = ezt.Template(compress_whitespace=0)
    template.parse("[for keys][keys.purpose],[end]")
    data = Box(title="t")
    data["keys"] = [Box(purpose="laptop"), Box(purpose="ci")]

    buf = io.StringIO()
    template.generate(buf, data)
    assert buf.getvalue() == "laptop,ci,"


def test_the_flattening_is_top_level_only():
    """A row field named after a dict method is still unreachable.

    Rows are read with getattr on the Box itself, so the escape hatch above
    does not extend to them. No row has such a field today, and if one were
    added it would fail at render rather than print a blank cell -- ezt gets
    the bound method and refuses to write it out. Worth pinning, because the
    top-level rule above might otherwise be read as general.
    """
    template = ezt.Template(compress_whitespace=0)
    template.parse("[for rows][rows.keys][end]")
    data = Box(title="t")
    row = Box(label="r")
    row["keys"] = "should not be reachable"
    data["rows"] = [row]

    buf = io.StringIO()
    with pytest.raises(TypeError, match="method"):
        template.generate(buf, data)
    assert "should not be reachable" not in buf.getvalue()


def test_no_page_sets_a_protected_name_as_an_attribute():
    offenders = []
    for path in sorted(THIS_DIR.glob("*.py")):
        text = path.read_text()
        for match in PY_ATTR_ASSIGN.finditer(text):
            target, attr = match.group(1), match.group(2)
            if attr in PROTECTED and target.split(".")[0] in DATA_OBJECTS:
                line = text[: match.start()].count("\n") + 1
                offenders.append(f'{path.name}:{line} {target}.{attr} = ...   (use {target}["{attr}"] = ...)')
    assert not offenders, "Box will raise BoxKeyError on these at runtime:\n  " + "\n  ".join(offenders)
