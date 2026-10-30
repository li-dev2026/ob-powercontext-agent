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

"""Exhaustive acceptance-oriented checks for the csv-row Source challenge.

The suite uses nothing beyond the standard library, ``pytest`` and the product
itself: parsing, identity, projection, registry and transport contracts are all
verified through the real ``powercontext.sources`` surface, with no additional
dependency.

Two suites ship together:

* ``test_csv_rows_exhaustive.py`` - this file, driven by ``pytest``;
* ``csv_rows_stdlib_check.py`` - the same contracts as a plain script that runs
  on a bare interpreter with zero third-party packages installed.

Run:
    PYTHONPATH=<candidate>/src python -m pytest -q tests/challenges/source_01
    python tests/challenges/source_01/csv_rows_stdlib_check.py
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from powercontext.sources import (
    SourceDefinitionRegistry,
    TextEvidence,
    manifest_for_definition,
    project_source_for_transport,
)
from powercontext.sources.csv_rows import (
    CSV_ROW_SOURCE_DEFINITION,
    CSVDocumentInput,
    CSVRowInput,
    CSVRowSource,
    csv_row_source_id,
    parse_csv_document,
)
from powercontext.sources.models import SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY

REPO_ROOT = Path(__file__).resolve().parents[3]
_MODULE_FILE = Path(__file__).resolve().parents[3] / "src" / "powercontext" / "sources" / "csv_rows.py"


def _challenge_dir() -> Path:
    override = os.environ.get("POWERCONTEXT_CHALLENGE_DIR")
    if override:
        return Path(override)
    return REPO_ROOT / "competition" / "challenges" / "source-01-table-evidence"


def _workload_dir() -> Path:
    workload = _challenge_dir() / "workload" / "public"
    if not (workload / "airports.csv").is_file():
        pytest.skip(f"public workload not available under {workload}")
    return workload


def _expected() -> dict:
    return json.loads((_workload_dir() / "expected.json").read_text(encoding="utf-8"))


def _parse(document_id: str, primary_key: str, content: bytes) -> tuple[CSVRowInput, ...]:
    return parse_csv_document(CSVDocumentInput(document_id=document_id, primary_key=primary_key, content=content))


def _mapping(columns: tuple[str, ...] | list[str], values: tuple[str, ...] | list[str]) -> dict[str, str]:
    return dict(zip(columns, values, strict=True))


def _projection(row: CSVRowInput) -> TextEvidence:
    registry = SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))
    return TextEvidence.model_validate(registry.project(asyncio.run(registry.resolve(row)), TEXT_EVIDENCE_PROJECTION_KEY))


# --------------------------------------------------------------------------- #
# Public workload: 12 complete airport records.
# --------------------------------------------------------------------------- #


def test_public_workload_rows_match_expected_fields_without_conversion() -> None:
    expected = _expected()
    rows = _parse(
        expected["document_id"],
        expected["primary_key"],
        (_workload_dir() / "airports.csv").read_bytes(),
    )

    assert len(rows) == len(expected["rows"]) == 12
    columns = tuple(expected["columns"])
    for row, expected_row in zip(rows, expected["rows"], strict=True):
        assert row.document_id == expected["document_id"]
        assert row.primary_key == expected["primary_key"] == "id"
        assert row.columns == columns
        assert row.values == tuple(expected_row[column] for column in columns)
        assert row.primary_key_value == expected_row["id"]
        assert all(isinstance(value, str) for value in row.values)


def test_public_workload_projection_identity_and_read_back() -> None:
    expected = _expected()
    rows = _parse(
        expected["document_id"],
        expected["primary_key"],
        (_workload_dir() / "airports.csv").read_bytes(),
    )
    registry = SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))

    for row, expected_row in zip(rows, expected["rows"], strict=True):
        source = asyncio.run(registry.resolve(row))
        expected_id = csv_row_source_id(expected["document_id"], expected_row["id"])

        assert type(source) is CSVRowSource
        assert source.materialization is SourceMaterialization.CAPTURED
        assert source.definition_version == "1"
        assert source.source_id == source.name == expected_id
        assert asyncio.run(registry.read(source)) == row
        assert source.columns == tuple(expected["columns"])

        evidence = TextEvidence.model_validate(registry.project(source, TEXT_EVIDENCE_PROJECTION_KEY))
        expected_row_mapping = _mapping(expected["columns"], (expected_row[column] for column in expected["columns"]))
        assert evidence == TextEvidence(
            source_type="csv-row",
            source_id=expected_id,
            content=json.dumps(expected_row_mapping, ensure_ascii=False, separators=(",", ":")),
            metadata={
                "document_id": expected["document_id"],
                "primary_key": expected["primary_key"],
                "primary_key_value": expected_row["id"],
                "columns": expected["columns"],
                "row": expected_row_mapping,
            },
        )


def test_public_workload_transport_observation_carries_full_projection() -> None:
    expected = _expected()
    rows = _parse(
        expected["document_id"],
        expected["primary_key"],
        (_workload_dir() / "airports.csv").read_bytes(),
    )
    registry = SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))
    manifest = manifest_for_definition(CSV_ROW_SOURCE_DEFINITION)

    assert manifest.name == "csv-row"
    assert manifest.version == "1"
    assert manifest.fingerprint.startswith("sha256:")
    assert len(manifest.fingerprint) == len("sha256:") + 64

    observation = project_source_for_transport(registry, asyncio.run(registry.resolve(rows[0])))
    assert observation.source_type == "csv-row"
    assert observation.materialization is SourceMaterialization.CAPTURED
    assert observation.payload["columns"] == list(rows[0].columns)
    assert observation.payload["values"] == list(rows[0].values)
    assert observation.projection(TEXT_EVIDENCE_PROJECTION_KEY) == registry.project(
        asyncio.run(registry.resolve(rows[0])), TEXT_EVIDENCE_PROJECTION_KEY
    )


# --------------------------------------------------------------------------- #
# Parsing: fixed csv.reader semantics, BOM, line endings, blank physical records.
# --------------------------------------------------------------------------- #


def test_bom_multiline_quotes_and_spaced_header_are_preserved() -> None:
    content = b'\xef\xbb\xbf" id ",name,note\r\n001,"Airport, East","first line\r\nsecond ""quoted"" line"\r\n'
    rows = _parse("airports:control", " id ", content)

    assert len(rows) == 1
    assert rows[0].columns == (" id ", "name", "note")
    assert rows[0].values == ("001", "Airport, East", 'first line\r\nsecond "quoted" line')
    assert csv_row_source_id("airports:control", "001") == "csv:airports%3Acontrol:001"


@pytest.mark.parametrize(
    ("content", "expected_values"),
    [
        (b"id,name\n1,a\n", ("1", "a")),
        (b"id,name\r\n1,a\r\n", ("1", "a")),
        (b"id,name\r1,a\r", ("1", "a")),
        (b"id,name\n1,a", ("1", "a")),
        (b"id,name\r\n1,a", ("1", "a")),
        (b"id,name\n1,\"a\"", ("1", "a")),
    ],
    ids=["lf", "crlf", "cr", "no-trailing-lf", "no-trailing-crlf", "quoted-last-field"],
)
def test_line_endings_and_missing_final_newline(content: bytes, expected_values: tuple[str, str]) -> None:
    rows = _parse("doc", "id", content)
    assert len(rows) == 1
    assert rows[0].values == expected_values


@pytest.mark.parametrize(
    "content",
    [
        b"id,name\n\n",
        b"id,name\n\n\n",
        b"id,name\n\n1,a\n\n",
        b"id,name\r\n\r\n1,a\r\n\r\n",
        b"\xef\xbb\xbfid,name\n\n",
    ],
    ids=["header-only", "header-trailing-blanks", "blank-between", "crlf-blanks", "bom-blank"],
)
def test_header_only_and_blank_physical_records(content: bytes) -> None:
    rows = _parse("doc", "id", content)
    if b"1,a" in content:
        assert len(rows) == 1
        assert rows[0].values == ("1", "a")
    else:
        assert rows == ()


def test_quoted_fields_keep_raw_text_quotes_commas_newlines_tabs_and_unicode() -> None:
    content = (
        "id,text\n"
        '001,"a,b"\n'
        '002,"say ""hi"""\n'
        '003,"line1\nline2"\n'
        "004,  padded  \n"
        "005,\tTabbed\t\n"
        '006,"Naatsisʼáán"\n'
        '007,""\n'
        "008,007\n"
    ).encode("utf-8")
    rows = _parse("doc", "id", content)
    values = {row.primary_key_value: row.values[1] for row in rows}

    assert values["001"] == "a,b"
    assert values["002"] == 'say "hi"'
    assert values["003"] == "line1\nline2"
    assert values["004"] == "  padded  "
    assert values["005"] == "\tTabbed\t"
    assert values["006"] == "Naatsisʼáán"
    assert values["007"] == ""
    assert values["008"] == "007"


def test_single_column_document_and_primary_key_not_trimmed() -> None:
    rows = _parse("doc", "id", b"id\n1\n2\n")
    assert [row.values for row in rows] == [("1",), ("2",)]

    spaced = _parse("doc", " id ", b" id ,id\n a ,b\n")
    assert spaced[0].columns == (" id ", "id")
    assert spaced[0].values == (" a ", "b")
    assert spaced[0].primary_key_value == " a "

    # The spaced column name and the plain one are distinct columns; selecting
    # "id" must land on the second column, never on the trimmed first one.
    plain = _parse("doc", "id", b" id ,id\n a ,b\n")
    assert plain[0].primary_key_value == "b"
    assert plain[0].values == (" a ", "b")

    with pytest.raises(ValueError):
        _parse("doc", "id", b" id ,name\n a ,b\n")


def test_wide_document_keeps_column_order() -> None:
    columns = [f"c{index}" for index in range(50)]
    values = [f"value-{index}" for index in range(50)]
    content = f"{','.join(columns)}\n{','.join(values)}\n"

    rows = _parse("doc", "c0", content.encode("utf-8"))
    assert rows[0].columns == tuple(columns)
    assert rows[0].values == tuple(values)
    assert list(_mapping(rows[0].columns, rows[0].values)) == columns


# --------------------------------------------------------------------------- #
# Atomic validation: every malformed input fails with ValueError, never partially.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("document_id", "primary_key", "content"),
    [
        ("doc", "id", b""),
        ("doc", "id", b"\xef\xbb\xbf"),
        ("doc", "id", b"\n"),
        ("doc", "id", b"\r\n"),
        ("doc", "id", b"   \n"),
        ("doc", "id", b"id\n\xff\n"),
        ("doc", "id", b"id\n\xc3\n"),
        ("doc", "id", b"id,id\n1,2\n"),
        ("doc", "id", b"id,\n1,\n"),
        ("doc", "id", b"id, \t\n1,2\n"),
        ("doc", "id", b"id,name,\n1,a,\n"),
        ("doc", "id", b"id,name\n1\n"),
        ("doc", "id", b"id,name\n1,a,extra\n"),
        ("doc", "id", b"id,name\n,a\n"),
        ("doc", "id", b"id,name\n  ,a\n"),
        ("doc", "id", b"id,name\n\t,a\n"),
        ("doc", "id", b"id,name\n1,a\n1,b\n"),
        ("doc", "id", b"id,name\n key ,a\n key ,b\n"),
        ("doc", "id", b'id,name\n1,"unclosed\n'),
        ("doc", "id", b'id,name\n1,"abc"def\n'),
        ("doc", "missing", b"id,name\n1,a\n"),
        ("doc", "", b"id,name\n1,a\n"),
        ("doc", " id ", b"id,name\n1,a\n"),
        ("", "id", b"id,name\n1,a\n"),
        (" doc ", "id", b"id,name\n1,a\n"),
        ("doc\n", "id", b"id,name\n1,a\n"),
    ],
    ids=[
        "empty-bytes",
        "bom-only",
        "newline-only",
        "crlf-only",
        "blank-lines-only",
        "invalid-utf8",
        "truncated-utf8",
        "duplicate-header",
        "empty-header-field",
        "blank-header-field",
        "trailing-empty-header",
        "too-few-fields",
        "too-many-fields",
        "empty-primary-value",
        "blank-primary-value",
        "tab-primary-value",
        "duplicate-primary-value",
        "duplicate-spaced-primary-value",
        "unclosed-quote",
        "quote-in-unquoted-field",
        "unknown-primary-key",
        "empty-primary-key",
        "unmatched-spaced-primary-key",
        "empty-document-id",
        "padded-document-id",
        "document-id-with-newline",
    ],
)
def test_invalid_documents_raise_value_error(document_id: str, primary_key: str, content: bytes) -> None:
    with pytest.raises(ValueError):
        _parse(document_id, primary_key, content)


@pytest.mark.parametrize(
    "value",
    [
        CSVDocumentInput(document_id="doc", primary_key="id", content=b"id\n1\n"),
    ],
)
def test_parse_returns_a_complete_tuple_and_never_a_partial_result(value: CSVDocumentInput) -> None:
    parsed = parse_csv_document(value)
    assert isinstance(parsed, tuple)
    assert all(isinstance(row, CSVRowInput) for row in parsed)


def test_non_bytes_content_is_rejected() -> None:
    with pytest.raises(ValueError):
        parse_csv_document(CSVDocumentInput(document_id="doc", primary_key="id", content="id\n1\n"))  # type: ignore[arg-type]


def test_parse_is_pure_and_deterministic() -> None:
    content = b"id,name\n1,a\n2,b\n"
    first = _parse("doc", "id", content)
    second = _parse("doc", "id", content)
    assert first == second
    assert tuple(row.values for row in first) == (("1", "a"), ("2", "b"))


# --------------------------------------------------------------------------- #
# Identity: percent-encoded, delimiter safe, bounded and reorder stable.
# --------------------------------------------------------------------------- #


def test_identity_format_and_encoding() -> None:
    assert csv_row_source_id("doc:one", "key/one") == "csv:doc%3Aone:key%2Fone"
    assert csv_row_source_id("space-keys", " key ") == "csv:space-keys:%20key%20"
    assert csv_row_source_id("doc", "a%2Fb") == "csv:doc:a%252Fb"
    assert csv_row_source_id("doc", "中") == "csv:doc:%E4%B8%AD"
    assert csv_row_source_id("doc", "1") == csv_row_source_id("doc", "1")


def test_identity_components_are_independent_and_delimiter_safe() -> None:
    assert csv_row_source_id("a:b", "c") != csv_row_source_id("a", "b:c")
    assert csv_row_source_id("a", "bc") != csv_row_source_id("ab", "c")
    assert csv_row_source_id("doc", " key ") != csv_row_source_id("doc", "key ")
    assert csv_row_source_id("doc1", "1") != csv_row_source_id("doc2", "1")


def test_identity_length_boundary() -> None:
    assert len(csv_row_source_id("d", "x" * 250)) == 256
    with pytest.raises(ValueError):
        csv_row_source_id("d", "x" * 251)
    with pytest.raises(ValueError):
        csv_row_source_id("doc", "x" * 260)


@pytest.mark.parametrize(
    ("document_id", "primary_key_value"),
    [
        (" doc ", "key"),
        ("doc ", "key"),
        (" doc", "key"),
        ("", "key"),
        ("doc", "   "),
        ("doc", "\u3000"),
        ("doc", ""),
    ],
    ids=["padded-both", "trailing-space", "leading-space", "empty-document", "blank-key", "ideographic-space", "empty-key"],
)
def test_identity_rejects_invalid_components(document_id: str, primary_key_value: str) -> None:
    with pytest.raises(ValueError):
        csv_row_source_id(document_id, primary_key_value)


def test_source_identity_is_shared_by_name_and_rejects_tampering() -> None:
    row = _parse("doc", "id", b"id,name\n1,a\n")[0]
    registry = SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))
    source = asyncio.run(registry.resolve(row))

    assert source.source_id == source.name == "csv:doc:1"
    assert len(source.source_id) <= 256

    with pytest.raises(ValueError):
        CSVRowSource(
            name="tampered",
            materialization=SourceMaterialization.CAPTURED,
            source_id="csv:doc:1",
            document_id="doc",
            primary_key="id",
            primary_key_value="1",
            columns=("id", "name"),
            values=("1", "a"),
        )


# --------------------------------------------------------------------------- #
# Projection: JSON object in header order, exactly five metadata keys.
# --------------------------------------------------------------------------- #


def test_projection_serialisation_is_compact_and_not_ascii_escaped() -> None:
    row = _parse("doc", "id", 'id,text\n001,"a,b ""c""\nNaatsisʼáán"\n'.encode("utf-8"))[0]
    evidence = _projection(row)

    assert evidence.source_type == "csv-row"
    assert evidence.source_id == csv_row_source_id("doc", "001")
    assert evidence.content == json.dumps(
        {"id": "001", "text": 'a,b "c"\nNaatsisʼáán'}, ensure_ascii=False, separators=(",", ":")
    )
    assert '", "' not in evidence.content
    assert '": "' not in evidence.content
    assert "\\u" not in evidence.content
    assert "Naatsisʼáán" in evidence.content


def test_projection_metadata_is_exactly_five_keys_and_typed() -> None:
    row = _parse("doc", "id", b"id,count,flag\n007,1.50,TRUE\n")[0]
    evidence = _projection(row)

    assert set(evidence.metadata) == {"document_id", "primary_key", "primary_key_value", "columns", "row"}
    assert evidence.metadata["document_id"] == "doc"
    assert evidence.metadata["primary_key"] == "id"
    assert evidence.metadata["primary_key_value"] == "007"
    assert evidence.metadata["columns"] == ["id", "count", "flag"]
    assert isinstance(evidence.metadata["columns"], list)
    assert evidence.metadata["row"] == {"id": "007", "count": "1.50", "flag": "TRUE"}
    assert json.loads(evidence.content) == evidence.metadata["row"]


def test_projection_preserves_empty_and_numeric_looking_values() -> None:
    row = _parse("doc", "id", b"id,a,b,c,d\n1,,0,0.0,false\n")[0]
    evidence = _projection(row)

    assert json.loads(evidence.content) == {"id": "1", "a": "", "b": "0", "c": "0.0", "d": "false"}
    assert '"a":""' in evidence.content


def test_source_dump_and_validate_round_trip() -> None:
    row = _parse("doc", "id", b"id,name\n1,a\n")[0]
    registry = SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))
    source = asyncio.run(registry.resolve(row))

    payload = source.model_dump(mode="json")
    assert payload["columns"] == ["id", "name"]
    assert payload["values"] == ["1", "a"]
    assert payload["materialization"] == "captured"

    restored = CSVRowSource.model_validate(payload)
    assert asyncio.run(registry.read(restored)) == row
    assert registry.project(restored, TEXT_EVIDENCE_PROJECTION_KEY) == registry.project(
        source, TEXT_EVIDENCE_PROJECTION_KEY
    )


# --------------------------------------------------------------------------- #
# Reordering, duplication scope and edit stability.
# --------------------------------------------------------------------------- #


def test_identity_survives_reordering_and_non_key_edits() -> None:
    first = _parse("reorder", "id", b"id,name\n01,alpha\n02,beta\n")
    second = _parse("reorder", "id", b"id,name\n02,beta\n01,alpha-edited\n")

    first_ids = {row.primary_key_value: csv_row_source_id(row.document_id, row.primary_key_value) for row in first}
    second_ids = {row.primary_key_value: csv_row_source_id(row.document_id, row.primary_key_value) for row in second}
    assert first_ids == second_ids

    first_evidence = _projection(first[0])
    second_evidence = _projection(second[1])
    assert first_evidence.source_id == second_evidence.source_id
    assert first_evidence.content != second_evidence.content
    assert json.loads(second_evidence.content)["name"] == "alpha-edited"


def test_editing_the_key_changes_identity_and_landing_record() -> None:
    before = _parse("doc", "id", b"id,name\n1,a\n")
    after = _parse("doc", "id", b"id,name\n2,a\n")

    assert {row.primary_key_value for row in before} == {"1"}
    assert {row.primary_key_value for row in after} == {"2"}
    assert {csv_row_source_id(row.document_id, row.primary_key_value) for row in before} != {
        csv_row_source_id(row.document_id, row.primary_key_value) for row in after
    }


def test_primary_key_uniqueness_is_scoped_to_one_document() -> None:
    first = _parse("doc-a", "id", b"id,name\n1,a\n")
    second = _parse("doc-b", "id", b"id,name\n1,b\n")

    assert first[0].primary_key_value == second[0].primary_key_value == "1"
    assert csv_row_source_id("doc-a", "1") != csv_row_source_id("doc-b", "1")


# --------------------------------------------------------------------------- #
# Dependency hygiene: the module must stay on the standard library.
# --------------------------------------------------------------------------- #


def test_csv_rows_module_imports_only_stdlib_and_powercontext() -> None:
    tree = ast.parse(_MODULE_FILE.read_text(encoding="utf-8"))
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported_roots.add(node.module.split(".")[0])

    allowed = set(sys.stdlib_module_names) | {"powercontext"}
    assert imported_roots <= allowed, f"third-party imports found: {sorted(imported_roots - allowed)}"


# --------------------------------------------------------------------------- #
# Transport: the observation handed to ingestion replays the same projection.
# --------------------------------------------------------------------------- #


def test_source_observation_round_trips_through_transport_serialisation() -> None:
    row = _parse("doc", "id", b'id,name\n1,"a,b"\n')[0]
    registry = SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))
    source = asyncio.run(registry.resolve(row))
    observation = project_source_for_transport(registry, source)

    expected = TextEvidence.model_validate(registry.project(source, TEXT_EVIDENCE_PROJECTION_KEY))
    assert TextEvidence.model_validate(observation.projection(TEXT_EVIDENCE_PROJECTION_KEY)) == expected
    assert observation.source_type == "csv-row"
    assert observation.materialization is SourceMaterialization.CAPTURED

    restored = type(observation).model_validate(observation.model_dump(mode="json"))
    assert restored.projection(TEXT_EVIDENCE_PROJECTION_KEY) == observation.projection(TEXT_EVIDENCE_PROJECTION_KEY)
    assert restored.name == observation.name


def test_observation_payload_carries_every_row_field_for_replay() -> None:
    expected = _expected()
    rows = _parse(
        expected["document_id"],
        expected["primary_key"],
        (_workload_dir() / "airports.csv").read_bytes(),
    )
    registry = SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))

    for row, expected_row in zip(rows, expected["rows"], strict=True):
        observation = project_source_for_transport(registry, asyncio.run(registry.resolve(row)))
        payload = observation.payload

        assert payload["document_id"] == expected["document_id"]
        assert payload["primary_key"] == expected["primary_key"]
        assert payload["primary_key_value"] == expected_row["id"]
        assert payload["columns"] == list(expected["columns"])
        assert payload["values"] == [expected_row[column] for column in expected["columns"]]

        evidence = TextEvidence.model_validate(observation.projection(TEXT_EVIDENCE_PROJECTION_KEY))
        assert evidence.metadata["columns"] == list(expected["columns"])
        assert evidence.metadata["row"] == dict(zip(evidence.metadata["columns"], payload["values"], strict=True))


# --------------------------------------------------------------------------- #
# Suite hygiene: the checks themselves stay on the standard library.
# --------------------------------------------------------------------------- #

_SUITE_ROOTS = {"pytest", "powercontext"}


def _import_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_check_suite_imports_only_stdlib_and_product() -> None:
    allowed = set(sys.stdlib_module_names) | _SUITE_ROOTS
    candidates = [Path(__file__), Path(__file__).with_name("csv_rows_stdlib_check.py")]
    for path in candidates:
        if not path.is_file():
            continue
        roots = _import_roots(path)
        assert roots <= allowed, f"{path.name} imports outside stdlib/product: {sorted(roots - allowed)}"


