# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Standard-library-only acceptance check for ``powercontext.sources.csv_rows``.

This script needs nothing beyond a plain CPython 3.10+ interpreter: it installs
minimal stand-ins for the product's pydantic-backed source models, loads the real
``csv_rows.py`` from the checkout, and exercises the parsing, identity and
projection contracts asserted by the challenge.

Run from any directory::

    python tests/challenges/source_01/csv_rows_stdlib_check.py

Optional overrides:

* ``POWERCONTEXT_CHALLENGE_DIR`` - challenge directory holding ``workload/public``
* ``POWERCONTEXT_CSV_ROWS_MODULE`` - path to the module under test

Exit code is 0 only when every check passes.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import os
import sys
import types
from pathlib import Path
from urllib.parse import quote

REPO = Path(__file__).resolve().parents[3]
CHALLENGE = Path(
    os.environ.get("POWERCONTEXT_CHALLENGE_DIR")
    or REPO / "competition" / "challenges" / "source-01-table-evidence"
)
MODULE_PATH = Path(os.environ.get("POWERCONTEXT_CSV_ROWS_MODULE") or REPO / "src" / "powercontext" / "sources" / "csv_rows.py")

failures: list[str] = []
checks = 0


def check(label: str, condition: object, detail: object = "") -> None:
    global checks
    checks += 1
    if not condition:
        failures.append(f"{label}{': ' + repr(detail) if detail != '' else ''}")


def expect_value_error(label: str, call) -> None:
    global checks
    checks += 1
    try:
        call()
    except ValueError:
        return
    except BaseException as error:  # noqa: BLE001 - report the wrong exception type
        failures.append(f"{label}: raised {type(error).__name__} instead of ValueError")
        return
    failures.append(f"{label}: did not raise ValueError")


def install_stand_ins() -> None:
    """Provide the product modules csv_rows imports, backed by plain classes."""

    def module(name: str) -> types.ModuleType:
        created = types.ModuleType(name)
        created.__path__ = []  # mark as a package so submodules can be imported
        sys.modules[name] = created
        return created

    root = module("powercontext")
    sources = module("powercontext.sources")
    module("powercontext.sources.definitions")
    models = module("powercontext.sources.models")
    projections = module("powercontext.sources.projections")
    errors = module("powercontext.errors")
    limits = module("powercontext.limits")
    for package in (root, sources):
        package.__dict__.setdefault("__all__", [])

    errors.InvalidSourceReferenceError = type("InvalidSourceReferenceError", (ValueError,), {})
    errors.SourceError = ValueError
    limits.MAX_SOURCE_ID_LENGTH = 256
    limits.MAX_SOURCE_TYPE_LENGTH = 128

    class SourceMaterialization:
        CAPTURED = "captured"
        REFERENCED = "referenced"

    class Source:
        def __init__(self, **fields: object) -> None:
            for field, value in fields.items():
                setattr(self, field, value)
            hook = getattr(self, "model_post_init", None)
            if hook is not None:
                hook(None)

        def __repr__(self) -> str:
            fields = ", ".join(f"{key}={value!r}" for key, value in vars(self).items())
            return f"{type(self).__name__}({fields})"

    models.Source = Source
    models.SourceMaterialization = SourceMaterialization

    class TextEvidence:
        def __init__(self, source_type: str, source_id: str, content: str, metadata: dict) -> None:
            self.source_type = source_type
            self.source_id = source_id
            self.content = content
            self.metadata = metadata

        def __eq__(self, other: object) -> bool:
            return (
                isinstance(other, TextEvidence)
                and self.source_type == other.source_type
                and self.source_id == other.source_id
                and self.content == other.content
                and self.metadata == other.metadata
            )

    class ProjectionKey:
        name = "powercontext.text-evidence"
        version = "1"

    projections.TEXT_EVIDENCE_PROJECTION_KEY = ProjectionKey()
    projections.TextEvidence = TextEvidence

    class AdapterSourceDefinition:
        def __init__(self, adapter, version="1", projections=()):
            self.adapter = adapter
            self.version = version
            self.projections = projections

        @property
        def input_class(self):
            return self.adapter.input_class

        @property
        def name(self):
            return self.adapter.name

        @property
        def source_class(self):
            return self.adapter.source_class

        async def resolve(self, value, /):
            return await self.adapter.resolve(value)

        async def read(self, source, /):
            return await self.adapter.read(source)

    definitions = sys.modules["powercontext.sources.definitions"]
    definitions.AdapterSourceDefinition = AdapterSourceDefinition
    definitions.SourceDefinition = object
    definitions.SourceDefinitionRegistry = object
    definitions.SourceProjection = object


def load_module():
    install_stand_ins()
    spec = importlib.util.spec_from_file_location("powercontext.sources.csv_rows", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def check_import_hygiene() -> None:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    allowed = set(sys.stdlib_module_names) | {"powercontext"}
    check("module imports stay on the standard library", roots <= allowed, sorted(roots - allowed))


def check_definition_surface(csv_rows) -> None:
    definition = csv_rows.CSV_ROW_SOURCE_DEFINITION
    adapter = definition.adapter
    projection = definition.projections[0]

    check("definition name", definition.name == "csv-row", definition.name)
    check("definition version", definition.version == "1", definition.version)
    check("definition input_class", definition.input_class is csv_rows.CSVRowInput)
    check("definition source_class", definition.source_class is csv_rows.CSVRowSource)
    check("projection key", (projection.name, projection.version) == ("powercontext.text-evidence", "1"))
    check("projection source_class", projection.source_class is csv_rows.CSVRowSource)
    check(
        "projection output_class",
        projection.output_class is sys.modules["powercontext.sources.projections"].TextEvidence,
    )
    check("row input class is exported", hasattr(csv_rows, "CSVRowInput"))
    check("document input class is exported", hasattr(csv_rows, "CSVDocumentInput"))
    return definition, adapter, projection


def check_public_workload(csv_rows, adapter, projection) -> None:
    workload = CHALLENGE / "workload" / "public"
    if not (workload / "airports.csv").is_file():
        check(f"public workload present under {workload}", False, "missing")
        return
    expected = json.loads((workload / "expected.json").read_text(encoding="utf-8"))
    document_id, primary_key = expected["document_id"], expected["primary_key"]
    columns = tuple(expected["columns"])
    rows = csv_rows.parse_csv_document(
        csv_rows.CSVDocumentInput(document_id=document_id, primary_key=primary_key, content=(workload / "airports.csv").read_bytes())
    )

    check("public row count", len(rows) == 12 == len(expected["rows"]), len(rows))
    check("public columns", rows[0].columns == columns)

    for row, expected_row in zip(rows, expected["rows"], strict=True):
        label = expected_row[primary_key]
        check(f"public values {label}", row.values == tuple(expected_row[column] for column in columns))
        check(f"public primary_key_value {label}", row.primary_key_value == expected_row[primary_key])
        check(f"public document_id {label}", row.document_id == document_id)
        source = asyncio.run(adapter.resolve(row))
        expected_id = f"csv:{quote(document_id, safe='')}:{quote(row.primary_key_value, safe='')}"
        check(f"public source_id {label}", source.source_id == expected_id)
        check(f"public name {label}", source.name == source.source_id)
        check(f"public materialization {label}", source.materialization == "captured", source.materialization)
        check(f"public read round trip {label}", asyncio.run(adapter.read(source)) == row)
        evidence = projection.project(source)
        mapping = dict(zip(columns, row.values, strict=True))
        check(f"public content {label}", evidence.content == json.dumps(mapping, ensure_ascii=False, separators=(",", ":")))
        check(
            f"public metadata {label}",
            evidence.metadata
            == {
                "document_id": document_id,
                "primary_key": primary_key,
                "primary_key_value": expected_row[primary_key],
                "columns": list(columns),
                "row": mapping,
            },
        )

    unicode_row = next((row for row in rows if any(not value.isascii() for value in row.values)), None)
    check("public unicode row exists", unicode_row is not None)
    if unicode_row is not None:
        evidence = projection.project(asyncio.run(adapter.resolve(unicode_row)))
        check("public unicode text is not escaped", "\\u" not in evidence.content)
        check(
            "public unicode round trip",
            json.loads(evidence.content) == dict(zip(unicode_row.columns, unicode_row.values, strict=True)),
        )


def check_parsing(csv_rows, adapter, projection) -> None:
    def parse(document_id: str, primary_key: str, content: bytes):
        return csv_rows.parse_csv_document(csv_rows.CSVDocumentInput(document_id, primary_key, content))

    tricky = b'\xef\xbb\xbf" id ",name,note\r\n001,"Airport, East","first line\r\nsecond ""quoted"" line"\r\n'
    tricky_rows = parse("airports:control", " id ", tricky)
    check("bom column name kept", tricky_rows[0].columns == (" id ", "name", "note"))
    check("quoted content kept", tricky_rows[0].values == ("001", "Airport, East", 'first line\r\nsecond "quoted" line'), tricky_rows[0].values)
    check("identity for control row", csv_rows.csv_row_source_id("airports:control", "001") == "csv:airports%3Acontrol:001")

    for label, content in {
        "lf": b"id,name\n1,a\n",
        "crlf": b"id,name\r\n1,a\r\n",
        "cr": b"id,name\r1,a\r",
        "no trailing newline": b"id,name\n1,a",
        "quoted last field": b'id,name\n1,"a"',
    }.items():
        parsed = parse("doc", "id", content)
        check(f"line ending {label}", len(parsed) == 1 and parsed[0].values == ("1", "a"), parsed)

    check("header only is empty", parse("doc", "id", b"id,name\n\n") == ())
    check("blank physical records are skipped", len(parse("doc", "id", b"id,name\n\n1,a\n\n2,b\n\n")) == 2)
    check("blank record inside crlf document", len(parse("doc", "id", b"id,name\r\n\r\n1,a\r\n\r\n")) == 1)
    check("single column document", [row.values for row in parse("doc", "id", b"id\n1\n2\n")] == [("1",), ("2",)])

    spaced = parse("doc", " id ", b" id ,id\n a ,b\n")
    check("spaced header kept", spaced[0].columns == (" id ", "id"))
    check("spaced key value kept", spaced[0].values == (" a ", "b") and spaced[0].primary_key_value == " a ")
    plain = parse("doc", "id", b" id ,id\n a ,b\n")
    check("spaced header not trimmed when selecting", plain[0].primary_key_value == "b")

    columns = [f"c{index}" for index in range(50)]
    values = [f"value-{index}" for index in range(50)]
    wide = parse("doc", "c0", f"{','.join(columns)}\n{','.join(values)}\n".encode())
    check("wide document keeps order", wide[0].columns == tuple(columns) and wide[0].values == tuple(values))

    fidelity = parse("fidelity", "id", b"id,amount,flag\n 007 ,1.50,TRUE\n\t8\t,  ,false\n")
    check("fidelity keeps padding", fidelity[0].values == (" 007 ", "1.50", "TRUE"), fidelity[0].values)
    check("fidelity keeps tabs", fidelity[1].values == ("\t8\t", "  ", "false"), fidelity[1].values)

    quoted = parse("doc", "id", 'id,text\n001,"a,b"\n002,"say ""hi"""\n003,"line1\nline2"\n004,""\n'.encode())
    texts = {row.primary_key_value: row.values[1] for row in quoted}
    check("quoted comma", texts["001"] == "a,b")
    check("quoted quotes", texts["002"] == 'say "hi"')
    check("quoted newline", texts["003"] == "line1\nline2")
    check("quoted empty", texts["004"] == "")

    # Atomic validation: every malformed document fails with ValueError.
    invalid = {
        "empty bytes": ("doc", "id", b""),
        "bom only": ("doc", "id", b"\xef\xbb\xbf"),
        "newline only": ("doc", "id", b"\n"),
        "crlf only": ("doc", "id", b"\r\n"),
        "blank line only": ("doc", "id", b"   \n"),
        "invalid utf-8": ("doc", "id", b"id\n\xff\n"),
        "truncated utf-8": ("doc", "id", b"id\n\xc3\n"),
        "duplicate header": ("doc", "id", b"id,id\n1,2\n"),
        "empty header field": ("doc", "id", b"id,\n1,\n"),
        "blank header field": ("doc", "id", b"id, \t\n1,2\n"),
        "trailing empty header": ("doc", "id", b"id,name,\n1,a,\n"),
        "too few fields": ("doc", "id", b"id,name\n1\n"),
        "too many fields": ("doc", "id", b"id,name\n1,a,extra\n"),
        "empty primary value": ("doc", "id", b"id,name\n,a\n"),
        "blank primary value": ("doc", "id", b"id,name\n  ,a\n"),
        "tab primary value": ("doc", "id", b"id,name\n\t,a\n"),
        "duplicate primary value": ("doc", "id", b"id,name\n1,a\n1,b\n"),
        "duplicate spaced primary value": ("doc", "id", b"id,name\n key ,a\n key ,b\n"),
        "unclosed quote": ("doc", "id", b'id,name\n1,"unclosed\n'),
        "quote followed by data": ("doc", "id", b'id,name\n1,"abc"def\n'),
        "unknown primary key": ("doc", "missing", b"id,name\n1,a\n"),
        "empty primary key": ("doc", "", b"id,name\n1,a\n"),
        "unmatched spaced primary key": ("doc", "id", b" id ,name\n a ,b\n"),
        "empty document id": ("", "id", b"id,name\n1,a\n"),
        "padded document id": (" doc ", "id", b"id,name\n1,a\n"),
        "document id with newline": ("doc\n", "id", b"id,name\n1,a\n"),
    }
    for label, (document_id, primary_key, content) in invalid.items():
        expect_value_error(f"invalid document: {label}", lambda d=document_id, k=primary_key, c=content: parse(d, k, c))

    check("parse returns a tuple", isinstance(parse("doc", "id", b"id\n1\n"), tuple))
    expect_value_error(
        "non bytes content",
        lambda: csv_rows.parse_csv_document(csv_rows.CSVDocumentInput("doc", "id", "id\n1\n")),
    )


def check_identity(csv_rows) -> None:
    check("identity encodes both components", csv_rows.csv_row_source_id("doc:one", "key/one") == "csv:doc%3Aone:key%2Fone")
    check("identity percent is escaped", csv_rows.csv_row_source_id("doc", "a%2Fb") == "csv:doc:a%252Fb")
    check("identity unicode", csv_rows.csv_row_source_id("doc", "中") == "csv:doc:%E4%B8%AD")
    check("identity space key", csv_rows.csv_row_source_id("space-keys", " key ") == "csv:space-keys:%20key%20")
    check("identity stable", csv_rows.csv_row_source_id("doc", "1") == csv_rows.csv_row_source_id("doc", "1"))
    check("identity depends on document", csv_rows.csv_row_source_id("a", "1") != csv_rows.csv_row_source_id("b", "1"))
    check("identity depends on key", csv_rows.csv_row_source_id("a", "1") != csv_rows.csv_row_source_id("a", "2"))
    check("identity delimiter safe", csv_rows.csv_row_source_id("a:b", "c") != csv_rows.csv_row_source_id("a", "b:c"))
    check("identity trailing space differs", csv_rows.csv_row_source_id("doc", " key ") != csv_rows.csv_row_source_id("doc", "key "))
    check("identity max length accepted", len(csv_rows.csv_row_source_id("d", "x" * 250)) == 256)
    expect_value_error("identity too long", lambda: csv_rows.csv_row_source_id("doc", "x" * 260))
    expect_value_error("identity spaced document", lambda: csv_rows.csv_row_source_id(" doc ", "key"))
    expect_value_error("identity blank key", lambda: csv_rows.csv_row_source_id("doc", "  "))
    expect_value_error("identity empty key", lambda: csv_rows.csv_row_source_id("doc", ""))


def check_projection(csv_rows, adapter, projection) -> None:
    def evidence_for(content: bytes, document_id: str = "doc", primary_key: str = "id"):
        rows = csv_rows.parse_csv_document(csv_rows.CSVDocumentInput(document_id, primary_key, content))
        return rows, projection.project(asyncio.run(adapter.resolve(rows[0])))

    _, evidence = evidence_for('id,text\n001,"a,b ""c""\nNaatsisʼáán"\n'.encode())
    check("projection source_type", evidence.source_type == "csv-row")
    check("projection source_id is identity", evidence.source_id == csv_rows.csv_row_source_id("doc", "001"))
    check(
        "projection content compact json",
        evidence.content == json.dumps({"id": "001", "text": 'a,b "c"\nNaatsisʼáán'}, ensure_ascii=False, separators=(",", ":")),
    )
    check("projection uses compact separators", '", "' not in evidence.content and '": "' not in evidence.content)
    check("projection keeps unicode literal", "\\u" not in evidence.content and "Naatsisʼáán" in evidence.content)

    _, evidence = evidence_for(b"id,count,flag\n007,1.50,TRUE\n")
    check(
        "projection metadata keys",
        set(evidence.metadata) == {"document_id", "primary_key", "primary_key_value", "columns", "row"},
        sorted(evidence.metadata),
    )
    check("projection metadata columns is a list", evidence.metadata["columns"] == ["id", "count", "flag"])
    check("projection metadata row", evidence.metadata["row"] == {"id": "007", "count": "1.50", "flag": "TRUE"})
    check("projection metadata primary_key_value", evidence.metadata["primary_key_value"] == "007")
    check("projection content equals metadata row", json.loads(evidence.content) == evidence.metadata["row"])

    _, empty = evidence_for(b"id,a,b,c,d\n1,,0,0.0,false\n")
    check("projection keeps empty and numeric text", json.loads(empty.content) == {"id": "1", "a": "", "b": "0", "c": "0.0", "d": "false"})
    check("projection writes empty string as empty", '"a":""' in empty.content)


def check_reordering(csv_rows, adapter, projection) -> None:
    def parse(content: bytes):
        return csv_rows.parse_csv_document(csv_rows.CSVDocumentInput("reorder", "id", content))

    first, second = parse(b"id,name\n01,alpha\n02,beta\n"), parse(b"id,name\n02,beta\n01,alpha-edited\n")
    first_ids = {row.primary_key_value: csv_rows.csv_row_source_id(row.document_id, row.primary_key_value) for row in first}
    second_ids = {row.primary_key_value: csv_rows.csv_row_source_id(row.document_id, row.primary_key_value) for row in second}
    check("reordering keeps identities", first_ids == second_ids)

    before = projection.project(asyncio.run(adapter.resolve(first[0])))
    after = projection.project(asyncio.run(adapter.resolve(second[1])))
    check("editing a field keeps identity", before.source_id == after.source_id)
    check("editing a field changes content", before.content != after.content)

    check(
        "primary key scope is one document",
        csv_rows.csv_row_source_id("doc-a", "1") != csv_rows.csv_row_source_id("doc-b", "1"),
    )


def main() -> int:
    if not MODULE_PATH.is_file():
        print(f"module under test not found: {MODULE_PATH}")
        return 2

    csv_rows = load_module()
    check_import_hygiene()
    _, adapter, projection = check_definition_surface(csv_rows)
    check_public_workload(csv_rows, adapter, projection)
    check_parsing(csv_rows, adapter, projection)
    check_identity(csv_rows)
    check_projection(csv_rows, adapter, projection)
    check_reordering(csv_rows, adapter, projection)

    print(f"checks run: {checks}")
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
