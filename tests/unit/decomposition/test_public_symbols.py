"""Public symbols derived from the documents alone (author-evolution spec §2.1; the
user's derivation rule of 2026-10-04)."""

from __future__ import annotations

from orchestra.codeprojecteval.public_symbols import derive_public_symbols, parse_directory_tree

TREE = """\
├── pkg
│   ├── __init__.py
│   ├── database.py
│   ├── table.py
│   └── utils.py
"""

ARCH = """\
# Architecture Design

```
""" + TREE + """```

`__init__.py` : The public API; re-exports `TinyDB`.

`database.py` :

- `TinyDB`: The main class.

  - `table(name: str) -> Table`: Returns a table.
  - `_read_table()`: private helper.
  - The instance attribute `default_table_name` names the default table.

- `open_db(path)`: opens a database.

`utils.py`:

- `LRUCache`: a cache.
- `set` (from `pkg.table`): the set operation.
"""

UML = """\
```mermaid
classDiagram
    class Table {
        -_storage: Storage
        +insert(document) int
        +all() List
    }
    class Helper {
        +run()
    }
```
"""

PRD = "Users call `TinyDB.table` and `freeze` and `MIN_CACHE_NUM`."


def test_tree_parsing_reconstructs_paths() -> None:
    assert parse_directory_tree(TREE) == ["pkg/", "pkg/__init__.py", "pkg/database.py", "pkg/table.py", "pkg/utils.py"]


def test_architecture_symbols_are_attributed_to_their_heading_module() -> None:
    inv = derive_public_symbols({"architecture_design.md": ARCH, "UML.md": UML, "PRD.md": PRD})
    assert inv.module_of("TinyDB") == "pkg/database.py"
    assert inv.module_of("open_db") == "pkg/database.py"
    assert inv.module_of("TinyDB.table") == "pkg/database.py"
    assert inv.module_of("TinyDB.default_table_name") == "pkg/database.py"
    assert inv.module_of("LRUCache") == "pkg/utils.py"
    assert "TinyDB._read_table" not in inv.names()


def test_builtin_looking_name_counts_when_listed_explicitly_and_stated_module_wins() -> None:
    inv = derive_public_symbols({"architecture_design.md": ARCH})
    mods = {s.module for s in inv.symbols if s.name == "set"}
    assert "pkg/table.py" in mods


def test_uml_class_resolves_by_filename_and_members_follow_the_class() -> None:
    inv = derive_public_symbols({"architecture_design.md": ARCH, "UML.md": UML})
    assert inv.module_of("Table") == "pkg/table.py"
    assert inv.module_of("Table.insert") == "pkg/table.py"
    assert "Table._storage" not in inv.names()
    assert "Helper" in inv.unattributed()  # no file matches
    assert "Helper.run" in inv.names()


def test_strict_owned_versus_lenient_sets() -> None:
    inv = derive_public_symbols({"architecture_design.md": ARCH, "UML.md": UML, "PRD.md": PRD})
    own = inv.owned_by(["pkg/database.py"])
    assert {"TinyDB", "open_db", "TinyDB.table"} <= own
    assert "Table" not in own and "Helper" not in own
    lenient = inv.lenient(["pkg/database.py"], predecessor_paths=["pkg/table.py"])
    assert {"TinyDB", "Table", "insert", "Helper", "freeze"} <= lenient   # own + predecessor + unattributed
    assert "MIN_CACHE_NUM" in lenient                                      # backticked in the PRD, unattributed
    assert "LRUCache" not in lenient                                       # a later milestone's module


def test_focus_paths_rooted_differently_from_the_tree_still_match() -> None:
    tree_doc = ARCH.replace("├── pkg", "├── repo-name")
    inv = derive_public_symbols({"architecture_design.md": tree_doc})
    assert inv.module_of("TinyDB") == "repo-name/database.py"
    assert "TinyDB" in inv.owned_by(["pkg/database.py"])
