# source-01-table-evidence 提交说明

赛题：PowerContext CSV 行证据 Source（`powercontext.sources.csv_rows`）
基线：PowerContext v1.2.0，commit `9db1753434162c0aa7c9f1189a1722e617880a52`（公开测评分支 `2026-competition`）

## 1. 交付物

| 文件 | 规模 | 作用 |
|---|---|---|
| `src/powercontext/sources/csv_rows.py` | 284 行 / 10,565 字节 | 本题唯一产品实现：解析、行 Source、identity、text-evidence 投影、Definition |
| `tests/challenges/source_01/test_csv_rows_exhaustive.py` | 638 行 / 26,003 字节 | 验收测试套件（69 passed），29 个测试函数 |
| `tests/challenges/source_01/csv_rows_stdlib_check.py` | 20,989 字节 | 零依赖自查脚本（203 项），`py -3` 直接运行 |

产品实现**只用 Python 标准库**：`csv`、`io`、`json`、`collections.abc`、`dataclasses`、`urllib.parse`，
加上 4 个产品内部导入（`powercontext.limits`、`powercontext.sources.definitions`、
`powercontext.sources.models`、`powercontext.sources.projections`）。无新增依赖、无网络调用、无模型调用。

## 2. 契约覆盖矩阵

| 必测组 | 契约 | 实现位置 | 测试证据 |
|---|---|---|---|
| CSV 解析与原子校验 | 严格 UTF-8、开头 BOM 可选 | `_decode_document`（:216） | `test_bom_multiline_quotes_and_spaced_header_are_preserved` |
| | `csv.reader` strict 语义、`""` 转义、引号内 `\r\n` | `parse_csv_document`（:168，`io.StringIO(newline="")`） | 同上 + `test_quoted_fields_keep_raw_text_quotes_commas_newlines_tabs_and_unicode` |
| | 表头非空、非纯空白、唯一、原值精确比较 | `_validate_header`（:232） | `test_invalid_documents_raise_value_error`（26 组参数化） |
| | primary_key 非空且精确命中列名（不 trim） | `header.index(value.primary_key)`（:176） | `test_single_column_document_and_primary_key_not_trimmed` |
| | 行宽 = 表头宽度 | `parse_csv_document`（:189） | 参数化 + `test_wide_document_keeps_column_order` |
| | 主键值非空、非纯空白、文档内唯一 | `_validate_primary_key_value`（:251）+ 去重集合（:195） | `test_primary_key_uniqueness_is_scoped_to_one_document` |
| | 值原样字符串，不 trim / 不转类型 | 全程 `str` 直传，无类型转换 | `test_public_workload_rows_match_expected_fields_without_conversion`、`test_projection_preserves_empty_and_numeric_looking_values` |
| | 完全空物理记录忽略；只有表头 → `()` | `if not record: continue`（:187） | `test_header_only_and_blank_physical_records` |
| | 空文件 / 空表头 → ValueError | `_decode_document`（:225）、`_validate_header`（:233） | 参数化用例 |
| | 失败原子性：无部分结果 | 唯一 `return tuple(rows)` 在 EOF（:186） | `test_parse_returns_a_complete_tuple_and_never_a_partial_result` |
| 稳定 identity | `csv:{quote(doc,safe='')}:{quote(pk,safe='')}` | `csv_row_source_id`（:143） | `test_identity_format_and_encoding`、`test_identity_components_are_independent_and_delimiter_safe` |
| | ≤ 256 字符 | `csv_row_source_id`（:153） | `test_identity_length_boundary`（250 长值恰 256 通过 / 251 报错） |
| | `name == source_id`、不可篡改 | `CSVRowSource.model_post_init`（:74） | `test_source_identity_is_shared_by_name_and_rejects_tampering` |
| | 重排稳定 | identity 只由 document_id + 主键值决定 | `test_identity_survives_reordering_and_non_key_edits` |
| TextEvidence | `source_type=csv-row`、`source_id=identity` | `_TextEvidenceProjection.project`（:120） | `test_public_workload_projection_identity_and_read_back` |
| | content 按原列序、`ensure_ascii=False`、`(",",":")` | 同上（:125） | `test_projection_serialisation_is_compact_and_not_ascii_escaped` |
| | metadata 恰好 5 键，columns=list、row=dict | 同上（:126） | `test_projection_metadata_is_exactly_five_keys_and_typed` |
| | read 与 projection 保留列序与值 | `_CSVRowAdapter.read`（:100） | `test_source_dump_and_validate_round_trip` |
| Runtime 持久化与引用 | 经 registry resolve/read、manifest/observation | `CSV_ROW_SOURCE_DEFINITION`（:136） | `test_public_workload_transport_observation_carries_full_projection` |
| | 真实入库、重启后 projection 一致 | 使用产品既有 `open_builtin_runtime` / `open_builtin_contexts` | 官方 `test_public_rows_survive_runtime_restart_and_exact_memory_citation_lookup` |

## 3. 验证记录

1. **官方验收测试全量 17 passed**（含 runtime 持久化与精确 citation 回查）：

   ```
   PYTHONPATH="$CANDIDATE/src:$PUBLIC" python -m pytest -q -p no:cacheprovider \
     "$PUBLIC/challenges/source-01-table-evidence/acceptance"
   → 17 passed
   ```

   覆盖 `test_public_airport_rows_have_exact_source_identity_and_projection`、
   `test_bom_multiline_escaped_quotes_and_exact_header_matching`、
   `test_invalid_csv_documents_fail_without_partial_rows`（10 组）、
   `test_empty_records_are_ignored_and_header_only_documents_are_valid`、
   `test_source_identity_components_are_delimiter_safe_and_bounded`、
   `test_row_identity_survives_reordering_and_non_key_edits`、
   `test_primary_key_values_preserve_surrounding_spaces_but_duplicates_fail`、
   `test_public_rows_survive_runtime_restart_and_exact_memory_citation_lookup`。

2. **仓库内测试套件 69 passed**：`python -m pytest tests/challenges/source_01/test_csv_rows_exhaustive.py`。
3. **零依赖自查 203 项全过**：`py -3 tests/challenges/source_01/csv_rows_stdlib_check.py`。

> 说明：官方运行环境为 Linux + 固定依赖（`competition/requirements.txt` 的 pin 均带
> `platform_machine == 'x86_64' and sys_platform == 'linux'` 标记）。本地复核时，
> 依赖通过 `pip install --target` 临时落地到工作区外的目录，跑完即删除；
> 提交树中不含任何依赖安装残留，产品与测试源码零第三方导入。

## 4. 本地复现

```bash
# 零依赖自查（不需要安装任何第三方包）
py -3 tests/challenges/source_01/csv_rows_stdlib_check.py

# 仓库内套件（使用产品自身依赖）
PYTHONPATH="$CANDIDATE/src" python -m pytest -q tests/challenges/source_01/test_csv_rows_exhaustive.py
```

挑战目录可用环境变量 `POWERCONTEXT_CHALLENGE_DIR` 覆盖；`csv_rows_stdlib_check.py`
还支持 `POWERCONTEXT_CSV_ROWS_MODULE` 指向待检模块。

## 5. 提交步骤

```bash
git init -b main
git add -A
git commit -m "feat(sources): add CSV row evidence source for source-01-table-evidence"
git remote add origin <YOUR_FORK_HTTPS_URL>
git push -u origin main
git rev-parse HEAD      # 40 位 commit SHA
```

组织方需能匿名 clone 该仓库并检出上述 commit；判题只加载 `src/`，
`tests/`、本说明等附带文件不参与判分。

## 6. 实现决策与风险

| 项 | 决策 | 理由 |
|---|---|---|
| `CSVDocumentInput` / `CSVRowInput` 用 `@dataclass(frozen=True, slots=True)` 而非 `BaseModel` | 保持产品文件零第三方导入 | `SourceDefinitionRegistry` 只要求 `input_class` 是 `type`，行为等价；若要求逐字使用 `BaseModel`，改 2 行即可（会引入 pydantic 导入） |
| 末尾缺换行时补 `"\n"` | 见 `_decode_document`（:227） | `csv.reader` 在末条记录无换行终止时行为不稳定，补换行不改变字段内容，且与 `csv` 模块对「记录以换行结束」的语义一致 |
| 空物理记录用 `if not record: continue` | 见 :187 | `csv.reader` 以 `[]` 表示完全空行，符合「表头之后完全空物理记录忽略」 |
| 256 上限按 `len(str)` 计 | 见 :153 | 接口文档以「source_id 不得超过 256 个字符」表述 |
