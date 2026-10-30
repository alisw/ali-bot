#!/usr/bin/env python3

"""Strip away PCH instructions from a compile database to make it usable for header analysis with Clang-Tidy.

Usage: strip_pch.py build/compile_commands.json tidy-db/compile_commands.json
"""

import json
import os
import re
import sys

src, dst = sys.argv[1], sys.argv[2]

pch_flags = re.compile(
    r"\s-Xclang\s+-include-pch\s+-Xclang\s+\S+"
    r"|\s-Xclang\s+-include\s+-Xclang\s+\S*cmake_pch\S*"
    r"|\s-include\s+\S*cmake_pch\S*"
    r"|\s-Winvalid-pch"
)

with open(src) as f:
    db = json.load(f)

# 1. drop the entries that build the PCH itself
db = [e for e in db if "cmake_pch" not in e["file"]]

# 2. strip the PCH flags from the remaining commands
for e in db:
    if "command" in e:
        e["command"] = pch_flags.sub("", e["command"])

os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
with open(dst, "w") as f:
    json.dump(db, f, indent=1)
