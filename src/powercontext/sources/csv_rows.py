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

"""Row-level CSV Sources: strict offline parsing, stable identity and text evidence."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from urllib.parse import quote

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

_CSV_ROW_SOURCE_NAME = "csv-row"
_CSV_ROW_DEFINITION_VERSION = "1"
_CAPTURED_SOURCE_TYPE = "csv-row"
_IDENTITY_PREFIX = "csv"
_IDENTITY_SEPARATOR = ":"
_BYTE_ORDER_MARK = "\ufeff"


@dataclass(frozen=True, slots=True)
class CSVDocumentInput:
    """One complete offline CSV document offered to the row adapter."""

    document_id: str
    primary_key: str
    content: bytes


@dataclass(frozen=True, slots=True)
class CSVRowInput:
    """One parsed CSV data row addressed by document, header name and primary key value."""

    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "columns", tuple(self.columns))
        object.__setattr__(self, "values", tuple(self.values))


class CSVRowSource(Source):
    """The captured Source value for exactly one CSV row."""

    materialization: SourceMaterialization = SourceMaterialization.CAPTURED
    source_id: str
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]

    def model_post_init(self, __context: object) -> None:
        _validate_document_id(self.document_id)
        if not isinstance(self.primary_key, str) or not self.primary_key:
            raise ValueError("primary_key must be a non-empty string")
        _validate_primary_key_value(self.primary_key_value)
        if len(self.columns) != len(self.values):
            raise ValueError("columns and values must describe the same number of fields")
        expected_source_id = csv_row_source_id(self.document_id, self.primary_key_value)
        if self.source_id != expected_source_id:
            raise ValueError("source_id must be the csv-row identity of the described row")
        if self.name != self.source_id:
            raise ValueError("name must equal source_id")


class _CSVRowAdapter:
    """Resolve a parsed row into its captured Source and read it back unchanged."""

    input_class = CSVRowInput
    name = _CSV_ROW_SOURCE_NAME
    source_class = CSVRowSource

    async def resolve(self, value: CSVRowInput, /) -> CSVRowSource:
        if type(value) is not CSVRowInput:
            raise TypeError(f"CSVRowAdapter resolves CSVRowInput, not {type(value).__name__}")
        return _row_source(value)

    async def read(self, source: CSVRowSource, /) -> CSVRowInput:
        if type(source) is not CSVRowSource:
            raise TypeError(f"CSVRowAdapter reads CSVRowSource, not {type(source).__name__}")
        return CSVRowInput(
            document_id=source.document_id,
            primary_key=source.primary_key,
            primary_key_value=source.primary_key_value,
            columns=tuple(source.columns),
            values=tuple(source.values),
        )


class _TextEvidenceProjection:
    """Project one captured row into the standard textual evidence shape."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = CSVRowSource
    output_class = TextEvidence

    def project(self, source: CSVRowSource, /) -> TextEvidence:
        row = _row_mapping(source.columns, source.values)
        return TextEvidence(
            source_type=_CAPTURED_SOURCE_TYPE,
            source_id=source.source_id,
            content=json.dumps(row, ensure_ascii=False, separators=(",", ":")),
            metadata={
                "document_id": source.document_id,
                "primary_key": source.primary_key,
                "primary_key_value": source.primary_key_value,
                "columns": list(source.columns),
                "row": row,
            },
        )


CSV_ROW_SOURCE_DEFINITION: SourceDefinition[CSVRowInput, CSVRowSource, CSVRowInput] = AdapterSourceDefinition(
    adapter=_CSVRowAdapter(),
    version=_CSV_ROW_DEFINITION_VERSION,
    projections=(_TextEvidenceProjection(),),
)


def csv_row_source_id(document_id: str, primary_key_value: str) -> str:
    """Encode the stable row identity shared by the Source name and its projection."""

    _validate_document_id(document_id)
    _validate_primary_key_value(primary_key_value)
    source_id = (
        f"{_IDENTITY_PREFIX}{_IDENTITY_SEPARATOR}"
        f"{quote(document_id, safe='')}{_IDENTITY_SEPARATOR}"
        f"{quote(primary_key_value, safe='')}"
    )
    if len(source_id) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"csv-row identity exceeds {MAX_SOURCE_ID_LENGTH} characters")
    return source_id


def parse_csv_document(value: CSVDocumentInput) -> tuple[CSVRowInput, ...]:
    """Parse and fully validate one offline CSV document into captured rows."""

    if not isinstance(value, CSVDocumentInput):
        raise TypeError(f"parse_csv_document expects CSVDocumentInput, not {type(value).__name__}")
    _validate_document_id(value.document_id)
    if not isinstance(value.primary_key, str) or not value.primary_key:
        raise ValueError("primary_key must be a non-empty string")

    text = _decode_document(value.content)
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)

    header = _next_record(reader, described_as="header")
    if header is None:
        raise ValueError("CSV document must contain a header record")
    _validate_header(header)

    try:
        primary_key_index = header.index(value.primary_key)
    except ValueError:
        raise ValueError(f"primary_key {value.primary_key!r} does not name a header column") from None

    columns = tuple(header)
    rows: list[CSVRowInput] = []
    primary_key_values: set[str] = set()
    while True:
        record = _next_record(reader, described_as="data record")
        if record is None:
            return tuple(rows)
        if not record:
            continue
        if len(record) != len(columns):
            raise ValueError(
                f"CSV data record has {len(record)} fields while the header declares {len(columns)} columns"
            )
        primary_key_value = record[primary_key_index]
        _validate_primary_key_value(primary_key_value)
        if primary_key_value in primary_key_values:
            raise ValueError(f"duplicate primary key value {primary_key_value!r} in document {value.document_id!r}")
        primary_key_values.add(primary_key_value)
        rows.append(
            CSVRowInput(
                document_id=value.document_id,
                primary_key=value.primary_key,
                primary_key_value=primary_key_value,
                columns=columns,
                values=tuple(record),
            )
        )


def _next_record(reader: Iterator[list[str]], /, *, described_as: str) -> list[str] | None:
    try:
        return next(reader, None)
    except csv.Error as error:
        raise ValueError(f"malformed CSV {described_as}: {error}") from error


def _decode_document(content: object, /) -> str:
    if not isinstance(content, (bytes, bytearray, memoryview)):
        raise ValueError(f"CSV document content must be bytes, not {type(content).__name__}")
    try:
        text = bytes(content).decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"CSV document must be strict UTF-8: {error}") from error
    if text.startswith(_BYTE_ORDER_MARK):
        text = text[len(_BYTE_ORDER_MARK) :]
    if not text:
        raise ValueError("CSV document must not be empty")
    if not text.endswith(("\r", "\n")):
        text = f"{text}\n"
    return text


def _validate_header(header: Sequence[str], /) -> None:
    if not header:
        raise ValueError("CSV header must declare at least one column")
    declared: set[str] = set()
    for column in header:
        if not column or not column.strip():
            raise ValueError("CSV header columns must be non-empty and must not be blank")
        if column in declared:
            raise ValueError(f"duplicate CSV header column {column!r}")
        declared.add(column)


def _validate_document_id(document_id: object, /) -> None:
    if not isinstance(document_id, str) or not document_id:
        raise ValueError("document_id must be a non-empty string")
    if document_id != document_id.strip():
        raise ValueError("document_id must not contain leading or trailing whitespace")


def _validate_primary_key_value(primary_key_value: object, /) -> None:
    if not isinstance(primary_key_value, str):
        raise ValueError(f"primary_key_value must be a string, not {type(primary_key_value).__name__}")
    if not primary_key_value.strip():
        raise ValueError("primary_key_value must not be empty or blank")


def _row_mapping(columns: Sequence[str], values: Sequence[str], /) -> dict[str, str]:
    return dict(zip(columns, values, strict=True))


def _row_source(row: CSVRowInput, /) -> CSVRowSource:
    source_id = csv_row_source_id(row.document_id, row.primary_key_value)
    return CSVRowSource(
        name=source_id,
        definition_version=_CSV_ROW_DEFINITION_VERSION,
        materialization=SourceMaterialization.CAPTURED,
        source_id=source_id,
        document_id=row.document_id,
        primary_key=row.primary_key,
        primary_key_value=row.primary_key_value,
        columns=row.columns,
        values=row.values,
    )


__all__ = [
    "CSV_ROW_SOURCE_DEFINITION",
    "CSVDocumentInput",
    "CSVRowInput",
    "CSVRowSource",
    "csv_row_source_id",
    "parse_csv_document",
]
