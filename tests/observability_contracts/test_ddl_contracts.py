from __future__ import annotations

import ast
import hashlib
import os
import re
import stat
from pathlib import Path

import pytest

from .contract_loader import repository_root


REQUIRE_IMPLEMENTATION = os.environ.get("OBS_REQUIRE_IMPLEMENTATION_DDL") == "1"


def _strip_sql_comments(sql: str) -> str:
    """Remove SQL comments while preserving quoted literals and line breaks."""

    result: list[str] = []
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(sql):
        char = sql[index]
        following = sql[index + 1] if index + 1 < len(sql) else ""
        if quote is not None:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\" and quote in {"'", '"'}:
                escaped = True
            elif char == quote:
                if following == quote:
                    result.append(following)
                    index += 1
                else:
                    quote = None
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            result.append(char)
            index += 1
            continue
        if char == "-" and following == "-":
            index += 2
            while index < len(sql) and sql[index] not in "\r\n":
                index += 1
            continue
        if char == "#":
            index += 1
            while index < len(sql) and sql[index] not in "\r\n":
                index += 1
            continue
        if char == "/" and following == "*":
            index += 2
            closed = False
            while index < len(sql):
                if sql[index] == "*" and index + 1 < len(sql) and sql[index + 1] == "/":
                    index += 2
                    closed = True
                    break
                result.append("\n" if sql[index] == "\n" else " ")
                index += 1
            if not closed:
                raise AssertionError("unterminated SQL block comment")
            continue
        result.append(char)
        index += 1
    if quote is not None:
        raise AssertionError("unterminated SQL quoted literal")
    return "".join(result)


def _table_body(sql: str, table: str) -> str:
    sql = _strip_sql_comments(sql)
    identifier = rf"(?:`{re.escape(table)}`|\"{re.escape(table)}\"|{re.escape(table)})"
    marker = re.search(
        rf"create\s+table(?:\s+if\s+not\s+exists)?\s+{identifier}\s*\(",
        sql,
        re.IGNORECASE,
    )
    if marker is None:
        raise AssertionError(f"table definition missing: {table}")
    body_start = marker.end()
    depth = 1
    quote: str | None = None
    escaped = False
    index = body_start
    while index < len(sql):
        char = sql[index]
        if escaped:
            escaped = False
        elif char == "\\" and quote in {"'", '"'}:
            escaped = True
        elif quote is not None:
            if char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    index += 1
                else:
                    quote = None
        elif char in {"'", '"', "`"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return sql[body_start:index]
        index += 1
    raise AssertionError(f"unterminated table definition: {table}")


def _split_top_level_csv(body: str) -> list[str]:
    entries: list[str] = []
    depth = 0
    quote: str | None = None
    escaped = False
    start = 0
    index = 0
    while index < len(body):
        char = body[index]
        if escaped:
            escaped = False
        elif char == "\\" and quote in {"'", '"'}:
            escaped = True
        elif quote is not None:
            if char == quote:
                if index + 1 < len(body) and body[index + 1] == quote:
                    index += 1
                else:
                    quote = None
        elif char in {"'", '"', "`"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise AssertionError("unbalanced SQL table entry")
        elif char == "," and depth == 0:
            entry = body[start:index].strip()
            if entry:
                entries.append(entry)
            start = index + 1
        index += 1
    if quote is not None or depth != 0:
        raise AssertionError("unterminated SQL table entry")
    final = body[start:].strip()
    if final:
        entries.append(final)
    return entries


def _mysql_table_entries(sql: str, table: str) -> list[str]:
    return _split_top_level_csv(_table_body(sql, table))


def _mysql_table_columns(sql: str, table: str) -> dict[str, dict[str, object]]:
    columns: dict[str, dict[str, object]] = {}
    for entry in _mysql_table_entries(sql, table):
        match = re.match(r"`([A-Za-z0-9_]+)`\s+(.+)$", entry, re.DOTALL)
        if match is None:
            continue
        name, declaration = match.groups()
        constraints = re.split(
            r"\s+comment\s+", declaration, maxsplit=1, flags=re.IGNORECASE
        )[0]
        not_null = re.search(r"\bnot\s+null\b", constraints, re.IGNORECASE)
        default_null = re.search(r"\bdefault\s+null\b", constraints, re.IGNORECASE)
        columns[name.lower()] = {
            "declaration": " ".join(declaration.split()),
            "nullable": default_null is not None or not_null is None,
        }
    return columns


def _mysql_unique_column_sets(sql: str, table: str) -> set[tuple[str, ...]]:
    unique_sets: set[tuple[str, ...]] = set()
    for entry in _mysql_table_entries(sql, table):
        unique = re.match(
            r"(?:constraint\s+`[A-Za-z0-9_]+`\s+)?"
            r"unique(?:\s+(?:index|key))?(?:\s+`[A-Za-z0-9_]+`)?\s*"
            r"\((.+)\)$",
            entry,
            re.IGNORECASE | re.DOTALL,
        )
        if unique is not None:
            columns = tuple(
                name.lower()
                for name in re.findall(r"`([A-Za-z0-9_]+)`", unique.group(1))
            )
            if columns:
                unique_sets.add(columns)
            continue
        column = re.match(r"`([A-Za-z0-9_]+)`\s+(.+)$", entry, re.DOTALL)
        if column is not None and re.search(
            r"\bunique\b", column.group(2), re.IGNORECASE
        ):
            unique_sets.add((column.group(1).lower(),))
    return unique_sets


_SQL_IDENTIFIER = (
    r'(?:`[A-Za-z_][A-Za-z0-9_]*`|"[A-Za-z_][A-Za-z0-9_]*"|'
    r"[A-Za-z_][A-Za-z0-9_]*)"
)


def _unquote_identifier(identifier: str) -> str:
    return identifier.strip('`"').lower()


def _parenthesized_body(sql: str, body_start: int) -> tuple[str, int]:
    depth = 1
    quote: str | None = None
    escaped = False
    index = body_start
    while index < len(sql):
        char = sql[index]
        if escaped:
            escaped = False
        elif char == "\\" and quote in {"'", '"'}:
            escaped = True
        elif quote is not None:
            if char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    index += 1
                else:
                    quote = None
        elif char in {"'", '"', "`"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return sql[body_start:index], index + 1
        index += 1
    raise AssertionError("unterminated index column list")


def _index_column_sequence(body: str) -> tuple[str, ...]:
    columns: list[str] = []
    for term in _split_top_level_csv(body):
        match = re.fullmatch(
            rf"\s*({_SQL_IDENTIFIER})(?:\s+(?:asc|desc))?"
            rf"(?:\s+nulls\s+(?:first|last))?\s*",
            term,
            re.IGNORECASE,
        )
        if match is None:
            raise AssertionError(f"unsupported index expression: {term}")
        columns.append(_unquote_identifier(match.group(1)))
    assert columns, "index must contain at least one column"
    return tuple(columns)


def _standalone_index_sequences(sql: str) -> dict[tuple[str, str], tuple[str, ...]]:
    sql = _strip_sql_comments(sql)
    marker = re.compile(
        rf"create\s+(?:unique\s+)?index\s+(?:if\s+not\s+exists\s+)?"
        rf"(?P<index>{_SQL_IDENTIFIER})\s+on\s+"
        rf"(?P<table>{_SQL_IDENTIFIER})\s*\(",
        re.IGNORECASE,
    )
    indexes: dict[tuple[str, str], tuple[str, ...]] = {}
    for match in marker.finditer(sql):
        body, _ = _parenthesized_body(sql, match.end())
        key = (
            _unquote_identifier(match.group("table")),
            _unquote_identifier(match.group("index")),
        )
        assert key not in indexes, f"duplicate index definition: {key}"
        indexes[key] = _index_column_sequence(body)
    return indexes


def _mysql_inline_index_sequences(
    sql: str, table: str
) -> dict[tuple[str, str], tuple[str, ...]]:
    indexes: dict[tuple[str, str], tuple[str, ...]] = {}
    for entry in _mysql_table_entries(sql, table):
        match = re.match(
            rf"(?:unique\s+)?(?:index|key)\s+"
            rf"(?P<index>{_SQL_IDENTIFIER})\s*\(",
            entry,
            re.IGNORECASE,
        )
        if match is None:
            continue
        body, _ = _parenthesized_body(entry, match.end())
        key = (table.lower(), _unquote_identifier(match.group("index")))
        assert key not in indexes, f"duplicate index definition: {key}"
        indexes[key] = _index_column_sequence(body)
    return indexes


def _all_index_sequences(
    sql: str, mysql_tables: tuple[str, ...] = ()
) -> dict[tuple[str, str], tuple[str, ...]]:
    indexes = _standalone_index_sequences(sql)
    for table in mysql_tables:
        for key, columns in _mysql_inline_index_sequences(sql, table).items():
            assert key not in indexes, f"duplicate index definition: {key}"
            indexes[key] = columns
    return indexes


def _index_sequence_violations(
    indexes: dict[tuple[str, str], tuple[str, ...]],
    expected: dict[tuple[str, str], tuple[str, ...]],
) -> list[str]:
    violations: list[str] = []
    for key, wanted in expected.items():
        actual = indexes.get(key)
        if actual is None:
            violations.append(f"{key[0]}.{key[1]} missing expected={wanted}")
        elif actual != wanted:
            violations.append(f"{key[0]}.{key[1]} actual={actual} expected={wanted}")
    return violations


def _required_index_sequence_violations(
    indexes: dict[tuple[str, str], tuple[str, ...]],
    table: str,
    expected: set[tuple[str, ...]],
) -> list[str]:
    actual = {
        columns
        for (index_table, _), columns in indexes.items()
        if index_table == table.lower()
    }
    return [
        f"{table} index sequence missing expected={columns}"
        for columns in sorted(expected - actual)
    ]


def _postgres_unique_column_sets(sql: str, table: str) -> set[tuple[str, ...]]:
    unique_sets: set[tuple[str, ...]] = set()
    for entry in _split_top_level_csv(_table_body(sql, table)):
        match = re.match(
            rf"(?:constraint\s+{_SQL_IDENTIFIER}\s+)?unique\s*\(",
            entry,
            re.IGNORECASE,
        )
        if match is None:
            continue
        body, _ = _parenthesized_body(entry, match.end())
        unique_sets.add(_index_column_sequence(body))
    return unique_sets


def _literal_string_tuple(source: str, assignment: str) -> tuple[str, ...]:
    module = ast.parse(source)
    for node in module.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(
            isinstance(target, ast.Name) and target.id == assignment
            for target in node.targets
        ):
            value = ast.literal_eval(node.value)
            assert isinstance(value, tuple)
            assert all(isinstance(item, str) for item in value)
            return value
    raise AssertionError(f"string tuple assignment missing: {assignment}")


def _sqlmodel_index_sequences(
    source: str,
) -> dict[tuple[str, str], tuple[str, ...]]:
    indexes: dict[tuple[str, str], tuple[str, ...]] = {}
    for class_node in ast.parse(source).body:
        if not isinstance(class_node, ast.ClassDef):
            continue
        assignments: dict[str, ast.expr] = {}
        for statement in class_node.body:
            if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
                continue
            target = statement.targets[0]
            if isinstance(target, ast.Name):
                assignments[target.id] = statement.value
        table_node = assignments.get("__tablename__")
        table_args = assignments.get("__table_args__")
        if table_args is None:
            continue
        if not isinstance(table_node, ast.Constant) or not isinstance(
            table_node.value, str
        ):
            raise AssertionError(
                f"SQLModel table name is not literal: {class_node.name}"
            )
        if not isinstance(table_args, (ast.Tuple, ast.List)):
            raise AssertionError(
                f"SQLModel table args are not literal: {class_node.name}"
            )
        for item in table_args.elts:
            if not (
                isinstance(item, ast.Call)
                and isinstance(item.func, ast.Name)
                and item.func.id == "Index"
            ):
                continue
            values = [ast.literal_eval(argument) for argument in item.args]
            if len(values) < 2 or not all(isinstance(value, str) for value in values):
                raise AssertionError(
                    f"SQLModel index is not literal: {class_node.name}"
                )
            key = (table_node.value.lower(), values[0].lower())
            assert key not in indexes, f"duplicate SQLModel index definition: {key}"
            indexes[key] = tuple(value.lower() for value in values[1:])
    return indexes


def _migration_checksum_pair_paths(source: str) -> tuple[str, ...]:
    for class_node in ast.parse(source).body:
        if not isinstance(class_node, ast.ClassDef) or class_node.name != "Migration":
            continue
        for function_node in class_node.body:
            if not (
                isinstance(function_node, ast.FunctionDef)
                and function_node.name == "checksum"
            ):
                continue
            for loop in ast.walk(function_node):
                if not (
                    isinstance(loop, ast.For)
                    and isinstance(loop.target, ast.Name)
                    and isinstance(loop.iter, ast.Tuple)
                ):
                    continue
                target_name = loop.target.id
                paths: list[str] = []
                for item in loop.iter.elts:
                    if not (
                        isinstance(item, ast.Attribute)
                        and isinstance(item.value, ast.Name)
                        and item.value.id == "self"
                    ):
                        break
                    paths.append(item.attr)
                else:
                    reads_pair_bytes = any(
                        isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Attribute)
                        and isinstance(call.func.value, ast.Name)
                        and call.func.value.id == "digest"
                        and call.func.attr == "update"
                        and any(
                            isinstance(argument, ast.Call)
                            and isinstance(argument.func, ast.Attribute)
                            and isinstance(argument.func.value, ast.Name)
                            and argument.func.value.id == target_name
                            and argument.func.attr == "read_bytes"
                            and not argument.args
                            for argument in call.args
                        )
                        for call in ast.walk(loop)
                    )
                    if reads_pair_bytes:
                        return tuple(paths)
    raise AssertionError("migration checksum pair read loop missing")


def _postgres_table_columns(sql: str, table: str) -> dict[str, dict[str, object]]:
    columns: dict[str, dict[str, object]] = {}
    reserved = {"constraint", "primary", "unique", "check", "foreign", "exclude"}
    for entry in _split_top_level_csv(_table_body(sql, table)):
        match = re.match(r'"?([A-Za-z_][A-Za-z0-9_]*)"?\s+(.+)$', entry, re.DOTALL)
        if match is None or match.group(1).lower() in reserved:
            continue
        name, declaration = match.groups()
        is_required = re.search(
            r"\bnot\s+null\b|\bprimary\s+key\b", declaration, re.IGNORECASE
        )
        columns[name.lower()] = {
            "declaration": " ".join(declaration.split()),
            "nullable": is_required is None,
        }
    return columns


def _security_audit_nullability_violations(sql: str) -> list[str]:
    columns = _mysql_table_columns(sql, "sys_security_audit_event")
    violations: list[str] = []
    for name in ["audit_action_id", "audit_layer"]:
        if name not in columns:
            violations.append(f"{name} missing")
        elif columns[name]["nullable"] is True:
            violations.append(f"{name} actual={columns[name]['declaration']}")
    for name in ["access_session_id", "parent_audit_event_id"]:
        if name not in columns:
            violations.append(f"{name} missing")
        elif columns[name]["nullable"] is False:
            violations.append(f"{name} must remain nullable")
    return violations


def _assert_security_audit_nullability(sql: str) -> None:
    violations = _security_audit_nullability_violations(sql)
    assert not violations, (
        "sys_security_audit_event audit_action_id and audit_layer must be "
        f"NOT NULL; violations={'; '.join(violations)}"
    )


class ImplementationSourceError(RuntimeError):
    """Raised when an implementation source cannot be opened without path races."""


def _source_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


ALLOWED_IMPLEMENTATION_ROOTS = {
    "OBS_PAGE1_JAVA_WORKTREE": {
        "/home/aituge/worktrees/obs-java-ledger",
        "/home/aituge/worktrees/obs-integration/java",
        "/implementation/page1-java",
    },
    "OBS_PAGE2_PYTHON_WORKTREE": {
        "/home/aituge/worktrees/obs-python-model",
        "/home/aituge/worktrees/obs-integration/python",
        "/implementation/page2-python",
    },
    "OBS_PAGE3_PYTHON_WORKTREE": {
        "/home/aituge/worktrees/obs-python-task-security",
        "/home/aituge/worktrees/obs-integration/python",
        "/implementation/page3-python",
    },
}
ALLOWED_IMPLEMENTATION_MANIFESTS = {
    "OBS_PAGE1_JAVA_WORKTREE": "/implementation-manifests/page1-java.tsv",
    "OBS_PAGE2_PYTHON_WORKTREE": "/implementation-manifests/page2-python.tsv",
    "OBS_PAGE3_PYTHON_WORKTREE": "/implementation-manifests/page3-python.tsv",
}


def _validate_source_directory(metadata: os.stat_result, absolute: str) -> None:
    permissions = stat.S_IMODE(metadata.st_mode)
    if not stat.S_ISDIR(metadata.st_mode):
        raise ImplementationSourceError("implementation source directory invalid")
    if absolute == "/tmp":
        valid = metadata.st_uid == 0 and metadata.st_gid == 0 and permissions == 0o1777
    elif metadata.st_uid == 0:
        valid = metadata.st_gid == 0 and permissions & 0o022 == 0
    else:
        valid = (
            metadata.st_uid == os.getuid()
            and metadata.st_gid == os.getgid()
            and permissions & 0o002 == 0
        )
    if not valid:
        raise ImplementationSourceError("implementation source directory invalid")


def _open_source_root(root: Path) -> tuple[int, tuple[tuple[int, ...], ...]]:
    root_text = os.fspath(root)
    if (
        not root.is_absolute()
        or os.path.normpath(root_text) != root_text
        or root_text == "/"
    ):
        raise ImplementationSourceError("implementation source root invalid")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        current_fd = os.open("/", flags)
    except OSError as error:
        raise ImplementationSourceError("implementation source root invalid") from error
    identities: list[tuple[int, ...]] = []
    absolute = ""
    try:
        root_metadata = os.fstat(current_fd)
        _validate_source_directory(root_metadata, "/")
        identities.append(_source_identity(root_metadata))
        for component in root.parts[1:]:
            try:
                next_fd = os.open(component, flags, dir_fd=current_fd)
            except OSError as error:
                raise ImplementationSourceError(
                    "implementation source ancestor invalid"
                ) from error
            os.close(current_fd)
            current_fd = next_fd
            absolute += "/" + component
            metadata = os.fstat(current_fd)
            _validate_source_directory(metadata, absolute)
            identities.append(_source_identity(metadata))
        return current_fd, tuple(identities)
    except BaseException:
        os.close(current_fd)
        raise


def _open_relative_directories(
    root_fd: int, components: tuple[str, ...]
) -> tuple[int, tuple[tuple[int, ...], ...]]:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    current_fd = os.dup(root_fd)
    identities: list[tuple[int, ...]] = []
    try:
        for component in components:
            try:
                next_fd = os.open(component, flags, dir_fd=current_fd)
            except OSError as error:
                raise ImplementationSourceError(
                    "implementation source directory invalid"
                ) from error
            os.close(current_fd)
            current_fd = next_fd
            metadata = os.fstat(current_fd)
            _validate_source_directory(metadata, component)
            identities.append(_source_identity(metadata))
        return current_fd, tuple(identities)
    except BaseException:
        os.close(current_fd)
        raise


def _validate_source_file(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_gid != os.getgid()
        or metadata.st_nlink != 1
        or not 1 <= metadata.st_size <= 32 * 1024 * 1024
    ):
        raise ImplementationSourceError("implementation source file metadata invalid")


def _verify_source_path_binding(
    root: Path,
    directory_components: tuple[str, ...],
    file_name: str,
    root_identities: tuple[tuple[int, ...], ...],
    directory_identities: tuple[tuple[int, ...], ...],
    file_identity: tuple[int, ...],
) -> None:
    verify_root_fd, verify_root_identities = _open_source_root(root)
    try:
        if verify_root_identities != root_identities:
            raise ImplementationSourceError(
                "implementation source root identity changed"
            )
        verify_directory_fd, verify_directory_identities = _open_relative_directories(
            verify_root_fd, directory_components
        )
        try:
            if verify_directory_identities != directory_identities:
                raise ImplementationSourceError(
                    "implementation source directory identity changed"
                )
            try:
                verify_file_fd = os.open(
                    file_name,
                    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=verify_directory_fd,
                )
            except OSError as error:
                raise ImplementationSourceError(
                    "implementation source file invalid"
                ) from error
            try:
                metadata = os.fstat(verify_file_fd)
                _validate_source_file(metadata)
                if _source_identity(metadata) != file_identity:
                    raise ImplementationSourceError(
                        "implementation source file identity changed"
                    )
            finally:
                os.close(verify_file_fd)
        finally:
            os.close(verify_directory_fd)
    finally:
        os.close(verify_root_fd)


def _read_implementation_source(
    root: Path,
    relative_path: str,
    expected_root_identity: str | None = None,
    expected_sha256: str | None = None,
) -> str:
    relative = Path(relative_path)
    components = relative.parts
    if (
        relative.is_absolute()
        or not components
        or any(component in ("", ".", "..") for component in components)
    ):
        raise ImplementationSourceError("implementation source path invalid")

    root_fd, root_identities = _open_source_root(root)
    try:
        actual_root = os.fstat(root_fd)
        if expected_root_identity is not None and expected_root_identity != (
            f"{actual_root.st_dev}:{actual_root.st_ino}"
        ):
            raise ImplementationSourceError(
                "implementation source root binding invalid"
            )
        directory_fd, directory_identities = _open_relative_directories(
            root_fd, components[:-1]
        )
        try:
            try:
                source_fd = os.open(
                    components[-1],
                    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
            except OSError as error:
                raise ImplementationSourceError(
                    "implementation source file invalid"
                ) from error
            try:
                before = os.fstat(source_fd)
                _validate_source_file(before)
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = os.read(source_fd, 64 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > 32 * 1024 * 1024:
                        raise ImplementationSourceError(
                            "implementation source file size invalid"
                        )
                after = os.fstat(source_fd)
                if (
                    _source_identity(before) != _source_identity(after)
                    or total != before.st_size
                ):
                    raise ImplementationSourceError(
                        "implementation source file identity changed"
                    )
                _verify_source_path_binding(
                    root,
                    components[:-1],
                    components[-1],
                    root_identities,
                    directory_identities,
                    _source_identity(after),
                )
                try:
                    content = b"".join(chunks)
                    if expected_sha256 is not None and (
                        re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
                        or hashlib.sha256(content).hexdigest() != expected_sha256
                    ):
                        raise ImplementationSourceError(
                            "implementation source content binding invalid"
                        )
                    return content.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise ImplementationSourceError(
                        "implementation source encoding invalid"
                    ) from error
            finally:
                os.close(source_fd)
        finally:
            os.close(directory_fd)
    finally:
        os.close(root_fd)


def _load_implementation_manifest(
    env_name: str, configured_root: str, relative_path: str
) -> tuple[str, str]:
    expected_path = ALLOWED_IMPLEMENTATION_MANIFESTS.get(env_name)
    manifest_path_text = os.environ.get(f"{env_name}_MANIFEST", "")
    expected_manifest_sha256 = os.environ.get(f"{env_name}_MANIFEST_SHA256", "")
    commit_binding = os.environ.get(f"{env_name}_COMMIT", "")
    identity_binding = os.environ.get(f"{env_name}_ROOT_IDENTITY", "")
    if (
        expected_path is None
        or manifest_path_text != expected_path
        or re.fullmatch(r"[0-9a-f]{64}", expected_manifest_sha256) is None
        or re.fullmatch(r"[0-9a-f]{40}", commit_binding) is None
        or re.fullmatch(r"[0-9]+:[0-9]+", identity_binding) is None
    ):
        raise ImplementationSourceError(
            "implementation source provenance binding missing"
        )

    manifest_path = Path(manifest_path_text)
    manifest = _read_implementation_source(
        manifest_path.parent,
        manifest_path.name,
        expected_sha256=expected_manifest_sha256,
    )
    lines = manifest.splitlines()
    if not lines or lines[0] != "OBS70_IMPLEMENTATION_MANIFEST_V1":
        raise ImplementationSourceError("implementation source manifest invalid")

    scalar: dict[str, str] = {}
    files: dict[str, str] = {}
    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) == 2 and parts[0] in {
            "env",
            "mount",
            "branch",
            "commit",
            "head",
            "root_identity",
        }:
            if parts[0] in scalar or not parts[1]:
                raise ImplementationSourceError(
                    "implementation source manifest invalid"
                )
            scalar[parts[0]] = parts[1]
        elif len(parts) == 3 and parts[0] == "file":
            if (
                parts[1] in files
                or not parts[1]
                or Path(parts[1]).is_absolute()
                or any(item in ("", ".", "..") for item in Path(parts[1]).parts)
                or re.fullmatch(r"[0-9a-f]{64}", parts[2]) is None
            ):
                raise ImplementationSourceError(
                    "implementation source manifest invalid"
                )
            files[parts[1]] = parts[2]
        else:
            raise ImplementationSourceError("implementation source manifest invalid")

    if (
        set(scalar) != {"env", "mount", "branch", "commit", "head", "root_identity"}
        or scalar["env"] != env_name
        or scalar["mount"] != configured_root
        or scalar["commit"] != commit_binding
        or scalar["root_identity"] != identity_binding
        or re.fullmatch(r"[0-9a-f]{40}", scalar["head"]) is None
        or re.fullmatch(r"[0-9a-f]{40}", scalar["commit"]) is None
        or relative_path not in files
    ):
        raise ImplementationSourceError(
            "implementation source manifest binding invalid"
        )
    return identity_binding, files[relative_path]


def _text(env_name: str, relative_path: str) -> str:
    configured = os.environ.get(env_name)
    expected_root_identity: str | None = None
    expected_sha256: str | None = None
    try:
        if configured:
            if configured not in ALLOWED_IMPLEMENTATION_ROOTS.get(env_name, set()):
                raise ImplementationSourceError(
                    "implementation source root is not allowlisted"
                )
            expected_root_identity, expected_sha256 = _load_implementation_manifest(
                env_name, configured, relative_path
            )
            root = Path(configured)
        else:
            root = repository_root()
        return _read_implementation_source(
            root,
            relative_path,
            expected_root_identity=expected_root_identity,
            expected_sha256=expected_sha256,
        )
    except ImplementationSourceError as error:
        message = (
            f"{relative_path} is unavailable or unsafe; set {env_name} to the "
            "verified owning worktree or run after integration"
        )
        if REQUIRE_IMPLEMENTATION:
            pytest.fail(message, pytrace=False)
        pytest.skip(f"{message}: {error}")


@pytest.mark.parametrize("link_kind", ["file", "directory"])
def test_implementation_source_reader_rejects_internal_symlink(
    tmp_path: Path, link_kind: str
) -> None:
    root = tmp_path / "verified-worktree"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    fake_ddl = outside / "fake.sql"
    fake_ddl.write_text(
        "CREATE TABLE sys_security_audit_event (event_id varchar(64));",
        encoding="utf-8",
    )
    if link_kind == "file":
        migration_dir = root / "migrations"
        migration_dir.mkdir()
        (migration_dir / "contract.sql").symlink_to(fake_ddl)
    else:
        (root / "migrations").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ImplementationSourceError):
        _read_implementation_source(root, "migrations/contract.sql")


def test_implementation_source_reader_rejects_multiple_hardlinks(
    tmp_path: Path,
) -> None:
    root = tmp_path / "verified-worktree"
    root.mkdir()
    source = root / "contract.sql"
    source.write_text("CREATE TABLE safe_contract (id bigint);", encoding="utf-8")
    os.link(source, root / "contract-alias.sql")

    with pytest.raises(ImplementationSourceError):
        _read_implementation_source(root, "contract.sql")


def test_implementation_source_reader_rejects_root_ancestor_symlink(
    tmp_path: Path,
) -> None:
    actual_parent = tmp_path / "actual-parent"
    root = actual_parent / "verified-worktree"
    root.mkdir(parents=True)
    (root / "contract.sql").write_text(
        "CREATE TABLE outside_contract (id bigint);", encoding="utf-8"
    )
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(actual_parent, target_is_directory=True)

    with pytest.raises(ImplementationSourceError):
        _read_implementation_source(linked_parent / "verified-worktree", "contract.sql")


def test_implementation_source_reader_rejects_root_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "selected-worktree"
    alternate = tmp_path / "alternate-worktree"
    parked = tmp_path / "parked-worktree"
    root.mkdir()
    alternate.mkdir()
    (root / "contract.sql").write_text(
        "CREATE TABLE safe_contract (id bigint);", encoding="utf-8"
    )
    (alternate / "contract.sql").write_text(
        "CREATE TABLE foreign_contract (id bigint);", encoding="utf-8"
    )
    original_open = _open_source_root
    swapped = False

    def swap_after_open(path: Path) -> tuple[int, tuple[tuple[int, ...], ...]]:
        nonlocal swapped
        opened = original_open(path)
        if path == root and not swapped:
            root.rename(parked)
            alternate.rename(root)
            swapped = True
        return opened

    monkeypatch.setitem(globals(), "_open_source_root", swap_after_open)
    with pytest.raises(ImplementationSourceError, match="root identity changed"):
        _read_implementation_source(root, "contract.sql")


def test_text_rejects_arbitrary_configured_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "arbitrary-worktree"
    root.mkdir()
    (root / "contract.sql").write_text(
        "CREATE TABLE foreign_contract (id bigint);", encoding="utf-8"
    )
    monkeypatch.setenv("OBS_PAGE2_PYTHON_WORKTREE", os.fspath(root))
    monkeypatch.setitem(globals(), "REQUIRE_IMPLEMENTATION", False)

    with pytest.raises(pytest.skip.Exception):
        _text("OBS_PAGE2_PYTHON_WORKTREE", "contract.sql")


def _configure_test_implementation_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    env_name = "OBS_PAGE2_PYTHON_WORKTREE"
    root = tmp_path / "manifest-bound-worktree"
    root.mkdir(mode=0o700)
    source = root / "contract.sql"
    source.write_text("CREATE TABLE manifest_bound (id bigint);", encoding="utf-8")
    source.chmod(0o600)
    identity = f"{root.stat().st_dev}:{root.stat().st_ino}"
    commit = "a" * 40
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest_dir = tmp_path / "implementation-manifests"
    manifest_dir.mkdir(mode=0o700)
    manifest = manifest_dir / "page2-python.tsv"
    manifest.write_text(
        "\n".join(
            (
                "OBS70_IMPLEMENTATION_MANIFEST_V1",
                f"env\t{env_name}",
                f"mount\t{root}",
                "branch\tobs/20-python-model",
                f"commit\t{commit}",
                f"head\t{commit}",
                f"root_identity\t{identity}",
                f"file\tcontract.sql\t{source_sha256}",
                "",
            )
        ),
        encoding="utf-8",
    )
    manifest.chmod(0o600)
    manifest_sha256 = hashlib.sha256(manifest.read_bytes()).hexdigest()
    monkeypatch.setitem(ALLOWED_IMPLEMENTATION_ROOTS, env_name, {os.fspath(root)})
    monkeypatch.setitem(ALLOWED_IMPLEMENTATION_MANIFESTS, env_name, os.fspath(manifest))
    monkeypatch.setenv(env_name, os.fspath(root))
    monkeypatch.setenv(f"{env_name}_MANIFEST", os.fspath(manifest))
    monkeypatch.setenv(f"{env_name}_MANIFEST_SHA256", manifest_sha256)
    monkeypatch.setenv(f"{env_name}_COMMIT", commit)
    monkeypatch.setenv(f"{env_name}_ROOT_IDENTITY", identity)
    return root, source


def test_text_accepts_manifest_bound_private_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, source = _configure_test_implementation_manifest(tmp_path, monkeypatch)

    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(source.stat().st_mode) == 0o600
    assert _text("OBS_PAGE2_PYTHON_WORKTREE", "contract.sql") == (
        "CREATE TABLE manifest_bound (id bigint);"
    )


def test_implementation_manifest_rejects_wrong_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_test_implementation_manifest(tmp_path, monkeypatch)
    env_name = "OBS_PAGE2_PYTHON_WORKTREE"
    monkeypatch.setenv(f"{env_name}_COMMIT", "b" * 40)

    with pytest.raises(ImplementationSourceError, match="manifest binding invalid"):
        _load_implementation_manifest(env_name, os.environ[env_name], "contract.sql")


def test_text_rejects_manifest_bound_mount_file_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, source = _configure_test_implementation_manifest(tmp_path, monkeypatch)
    source.write_text("CREATE TABLE swapped_mount_file (id bigint);", encoding="utf-8")
    monkeypatch.setitem(globals(), "REQUIRE_IMPLEMENTATION", False)

    with pytest.raises(pytest.skip.Exception):
        _text("OBS_PAGE2_PYTHON_WORKTREE", "contract.sql")


@pytest.fixture
def nullable_security_audit_ddl() -> str:
    return """
    CREATE TABLE IF NOT EXISTS `sys_security_audit_event` (
      `event_id` varchar(64) NOT NULL,
      `audit_action_id` varchar(64) DEFAULT NULL,
      `access_session_id` varchar(64) DEFAULT NULL,
      `audit_layer` varchar(32) NULL,
      `parent_audit_event_id` varchar(64) DEFAULT NULL,
      PRIMARY KEY (`event_id`)
    );
    """


def test_mysql_column_parser_detects_nullable_required_columns(
    nullable_security_audit_ddl: str,
) -> None:
    columns = _mysql_table_columns(
        nullable_security_audit_ddl, "sys_security_audit_event"
    )
    assert columns["event_id"]["nullable"] is False
    assert columns["audit_action_id"]["nullable"] is True
    assert columns["audit_layer"]["nullable"] is True
    with pytest.raises(
        AssertionError, match="audit_action_id and audit_layer must be NOT NULL"
    ):
        _assert_security_audit_nullability(nullable_security_audit_ddl)


def test_mysql_parser_ignores_comment_hints_and_nonunique_indexes() -> None:
    weak_sql = """
    CREATE TABLE `weak_contract` (
      `nullable_hint` varchar(64) DEFAULT NULL COMMENT 'must be NOT NULL',
      `aggregate_type` varchar(64) NOT NULL,
      `aggregate_id` varchar(64) NOT NULL,
      INDEX `uk_looks_unique` (`aggregate_type`, `aggregate_id`)
    );
    """
    columns = _mysql_table_columns(weak_sql, "weak_contract")
    assert columns["nullable_hint"]["nullable"] is True
    assert _mysql_unique_column_sets(weak_sql, "weak_contract") == set()

    strict_sql = weak_sql.replace("INDEX `uk_looks_unique`", "UNIQUE INDEX `uk_real`")
    assert _mysql_unique_column_sets(strict_sql, "weak_contract") == {
        ("aggregate_type", "aggregate_id")
    }


def test_sql_comment_stripping_blocks_fake_tables_indexes_and_constraints() -> None:
    sql = """
    -- CREATE TABLE `commented_line` (`id` bigint NOT NULL);
    /* CREATE TABLE `commented_block` (`id` bigint NOT NULL); */
    CREATE TABLE `live_contract` (
      `tenant_id` varchar(64) NOT NULL,
      `occurred_at` timestamp NOT NULL,
      `event_id` varchar(80) NOT NULL,
      `nullable_hint` varchar(64) DEFAULT NULL
        COMMENT 'NOT NULL UNIQUE INDEX -- # /* literal */',
      `literal_markers` varchar(128) DEFAULT '-- # /* still literal */'
    );
    -- CREATE INDEX idx_fake_line ON live_contract (event_id);
    # CREATE INDEX idx_fake_hash ON live_contract (event_id);
    /* CREATE INDEX idx_fake_block ON live_contract (event_id); */
    CREATE INDEX idx_live_order
      ON live_contract (tenant_id, occurred_at DESC, event_id DESC);
    """
    stripped = _strip_sql_comments(sql)
    assert "'-- # /* still literal */'" in stripped
    for table in ["commented_line", "commented_block"]:
        with pytest.raises(AssertionError, match="table definition missing"):
            _table_body(sql, table)
    columns = _mysql_table_columns(sql, "live_contract")
    assert columns["nullable_hint"]["nullable"] is True
    indexes = _all_index_sequences(sql)
    assert indexes == {
        ("live_contract", "idx_live_order"): (
            "tenant_id",
            "occurred_at",
            "event_id",
        )
    }


def test_sql_comment_stripping_rejects_unterminated_block_comment() -> None:
    with pytest.raises(AssertionError, match="unterminated SQL block comment"):
        _strip_sql_comments("CREATE TABLE safe (id bigint); /* fake index")


def test_sqlmodel_index_parser_extracts_literal_column_order() -> None:
    source = """
class AuditRecord(SQLModel, table=True):
    __tablename__ = "audit_record"
    __table_args__ = (
        Index("idx_audit_tenant_time", "tenant_id", "occurred_at", "id"),
    )
"""

    assert _sqlmodel_index_sequences(source) == {
        ("audit_record", "idx_audit_tenant_time"): (
            "tenant_id",
            "occurred_at",
            "id",
        )
    }


def test_migration_checksum_parser_requires_both_source_files() -> None:
    source = """
class Migration:
    def checksum(self):
        digest = hashlib.sha256()
        for path in (self.up_path, self.down_path):
            digest.update(path.read_bytes())
        return digest.hexdigest()
"""

    assert _migration_checksum_pair_paths(source) == ("up_path", "down_path")


@pytest.mark.implementation
def test_java_business_event_and_outbox_ddl_contract() -> None:
    sql = _text(
        "OBS_PAGE1_JAVA_WORKTREE",
        "continew-server/src/main/resources/db/changelog/mysql/business/"
        "observability_ledger_1_5.sql",
    ).lower()
    tick = chr(96)

    for table in [
        "biz_business_event",
        "sys_security_audit_event",
        "integration_outbox",
        "integration_inbox",
    ]:
        assert re.search(rf"create table (if not exists )?{tick}{table}{tick}", sql)

    expected_unique_sets = {
        "biz_business_event": {
            ("event_id",),
            ("tenant_id", "idempotency_key"),
            (
                "tenant_id",
                "aggregate_type",
                "aggregate_id",
                "aggregate_version",
            ),
        },
        "sys_security_audit_event": {
            ("event_id",),
            ("scope_type", "tenant_scope_id", "idempotency_key"),
        },
        "integration_outbox": {
            ("command_id",),
            ("tenant_scope_id", "topic", "idempotency_key"),
            (
                "tenant_scope_id",
                "topic",
                "aggregate_type",
                "aggregate_id",
                "aggregate_version",
            ),
        },
        "integration_inbox": {
            ("source_service", "message_id"),
            ("source_service", "idempotency_key"),
        },
    }
    for table, expected in expected_unique_sets.items():
        assert expected <= _mysql_unique_column_sets(sql, table), table

    assert f"{tick}aggregate_version{tick}  bigint(20)    not null" in sql
    assert f"check ({tick}aggregate_version{tick} >= 1)" in sql
    expected_columns = ", ".join(
        f"{tick}{column}{tick}"
        for column in [
            "tenant_id",
            "aggregate_type",
            "aggregate_id",
            "aggregate_version",
        ]
    )
    assert expected_columns in sql
    security_violations = _security_audit_nullability_violations(sql)
    security_indexes = _all_index_sequences(
        sql, mysql_tables=("sys_security_audit_event",)
    )
    security_violations.extend(
        _required_index_sequence_violations(
            security_indexes,
            "sys_security_audit_event",
            {
                ("tenant_id", "occurred_at", "event_id"),
                ("audit_action_id", "occurred_at", "event_id"),
                ("access_session_id", "occurred_at", "event_id"),
                ("parent_audit_event_id",),
            },
        )
    )
    assert not security_violations, (
        "sys_security_audit_event DDL violations=" + "; ".join(security_violations)
    )
    assert f"{tick}source_ip_masked{tick}       varchar(80)" in sql
    for table in expected_unique_sets:
        assert re.search(
            rf"-- rollback\s+drop table if exists {tick}{table}{tick};", sql
        )
    assert re.search(
        rf"-- rollback alter table {tick}biz_contract_review_task{tick} .*"
        rf"drop column {tick}aggregate_version{tick};",
        sql,
    )


@pytest.mark.implementation
def test_python_model_ledger_up_and_down_contract() -> None:
    up = _text(
        "OBS_PAGE2_PYTHON_WORKTREE",
        "backend/model_observability/migrations/001_model_invocation_ledger.up.sql",
    ).lower()
    down = _text(
        "OBS_PAGE2_PYTHON_WORKTREE",
        "backend/model_observability/migrations/001_model_invocation_ledger.down.sql",
    ).lower()

    hardening_up = _text(
        "OBS_PAGE2_PYTHON_WORKTREE",
        "backend/model_observability/migrations/"
        "002_model_observability_hardening.up.sql",
    ).lower()
    hardening_down = _text(
        "OBS_PAGE2_PYTHON_WORKTREE",
        "backend/model_observability/migrations/"
        "002_model_observability_hardening.down.sql",
    ).lower()
    migration_runner_source = _text(
        "OBS_PAGE2_PYTHON_WORKTREE",
        "backend/model_observability/migrate.py",
    )
    migration_runner = migration_runner_source.lower()
    for table in [
        "tuge_model_invocation_event",
        "tuge_model_invocation_projection",
    ]:
        assert f"create table {table}" in up
        assert f"drop table if exists {table}" in down

    event_columns = _postgres_table_columns(up, "tuge_model_invocation_event")
    projection_columns = _postgres_table_columns(up, "tuge_model_invocation_projection")
    required_event_columns = {
        "event_id",
        "event_type",
        "schema_version",
        "logical_call_id",
        "invocation_id",
        "attempt_no",
        "dispatch_status",
        "occurred_at",
        "ingested_at",
        "tenant_id",
        "feature_code",
        "provider",
        "model_name",
        "privacy_mode",
        "route_type",
        "metadata_json",
    }
    required_projection_columns = {
        "invocation_id",
        "projection_version",
        "data_as_of",
        "logical_call_id",
        "attempt_no",
        "tenant_id",
        "feature_code",
        "started_at",
        "ingested_at",
        "lifecycle_status",
        "dispatch_status",
        "provider",
        "model_name",
        "privacy_mode",
        "route_type",
    }
    for name in required_event_columns:
        assert name in event_columns
        assert event_columns[name]["nullable"] is False, name
    for name in required_projection_columns:
        assert name in projection_columns
        assert projection_columns[name]["nullable"] is False, name

    for table, columns in {
        "tuge_model_invocation_event": event_columns,
        "tuge_model_invocation_projection": projection_columns,
    }.items():
        body = _table_body(up, table)
        for metric in [
            "input_token_count",
            "output_token_count",
            "latency_ms",
            "time_to_first_token_ms",
            "cost_amount",
        ]:
            declaration = str(columns[metric]["declaration"])
            assert re.search(
                rf"\bcheck\s*\(\s*{metric}\s+is\s+null\s+or\s+"
                rf"{metric}\s*>=\s*0\s*\)",
                declaration,
                re.DOTALL,
            ), (table, metric)
        currency_declaration = str(columns["cost_currency"]["declaration"])
        assert re.search(
            r"\bcheck\s*\(\s*cost_currency\s+is\s+null\s+or\s+"
            r"cost_currency\s*~\s*"
            r"'\^\[a-z\]\{3\}\$'\s*\)",
            currency_declaration,
            re.DOTALL,
        ), table
        assert "cost_metadata" in body

    indexes = _all_index_sequences(up)
    index_violations = _index_sequence_violations(
        indexes,
        {
            (
                "tuge_model_invocation_event",
                "uq_tuge_model_event_started",
            ): ("invocation_id",),
            (
                "tuge_model_invocation_event",
                "uq_tuge_model_event_logical_attempt_started",
            ): ("logical_call_id", "attempt_no"),
            (
                "tuge_model_invocation_event",
                "uq_tuge_model_event_dispatch_conclusion",
            ): ("invocation_id",),
            (
                "tuge_model_invocation_event",
                "uq_tuge_model_event_terminal",
            ): ("invocation_id",),
            (
                "tuge_model_invocation_projection",
                "idx_tuge_model_projection_tenant_started",
            ): ("tenant_id", "started_at", "invocation_id"),
            (
                "tuge_model_invocation_projection",
                "idx_tuge_model_projection_tenant_dispatch_started",
            ): ("tenant_id", "dispatch_status", "started_at", "invocation_id"),
        },
    )
    assert not index_violations, "; ".join(index_violations)
    assert (
        "logical_call_id",
        "attempt_no",
    ) in _postgres_unique_column_sets(up, "tuge_model_invocation_projection")

    clean_up = _strip_sql_comments(up)
    for index_name in [
        "uq_tuge_model_event_started",
        "uq_tuge_model_event_logical_attempt_started",
        "uq_tuge_model_event_dispatch_conclusion",
        "uq_tuge_model_event_terminal",
    ]:
        assert re.search(
            rf"create unique index {index_name}\s+on\s+"
            r"tuge_model_invocation_event\s*\(.+?\)\s*where\s+event_type",
            clean_up,
            re.DOTALL,
        ), index_name

    assert "ck_tuge_model_projection_terminal" in up
    assert "ck_tuge_model_projection_time_order" in up
    assert "ck_tuge_model_event_terminal_outcome" in up
    assert "ck_tuge_model_event_terminal_semantics" in up
    assert re.findall(r"drop table if exists\s+([a-z0-9_]+)\s*;", down) == [
        "tuge_model_invocation_projection",
        "tuge_model_invocation_event",
    ]

    for column in [
        "logical_call_id",
        "invocation_id",
        "attempt_no",
        "dispatch_status",
        "pricing_version",
        "cost_calculated_at",
    ]:
        assert column in up
    assert "attempt_no >= 1" in up
    assert "not_dispatched" in up
    assert "dispatched" in up
    assert "dispatch_unknown" in up
    assert "model_invocation_output_guardrail_rejected" in up
    assert "model_invocation_input_guardrail_rejected" not in up
    assert "cost_amount is null or cost_amount >= 0" in up
    assert "uq_tuge_model_projection_logical_attempt" in up
    assert "idx_tuge_model_projection_tenant_dispatch_started" in up
    hardening_compact = " ".join(_strip_sql_comments(hardening_up).split())
    assert "create table tuge_model_observability_sequence" in hardening_compact
    assert "sequence_value bigint not null default 0" in hardening_compact
    assert "add column server_sequence bigint" in hardening_compact
    assert "alter column server_sequence set not null" in hardening_compact
    assert "check (server_sequence >= 1)" in hardening_compact
    assert "uq_tuge_model_event_server_sequence" in hardening_up
    assert "select coalesce(max(server_sequence), 0)" in hardening_compact
    assert "where sequence_name = 'event'" in hardening_compact

    for constraint in [
        "ck_tuge_model_projection_started_sequence",
        "ck_tuge_model_projection_dispatch_sequence",
        "ck_tuge_model_projection_terminal_sequence",
        "ck_tuge_model_projection_causal_sequence",
    ]:
        assert constraint in hardening_up
    assert "terminal_sequence > dispatch_sequence" in hardening_compact
    assert "foreign key (fallback_from_invocation_id)" in hardening_compact
    assert "on delete restrict deferrable initially deferred" in hardening_compact

    assert "create trigger trg_tuge_model_event_append_only" in hardening_compact
    assert "before update or delete on tuge_model_invocation_event" in hardening_compact
    assert "tuge_model_invocation_event is append-only" in hardening_up
    assert "create trigger trg_tuge_model_event_validate_fallback" in hardening_compact
    for terminal_event in [
        "model_invocation_succeeded",
        "model_invocation_failed",
        "model_invocation_validation_failed",
        "model_invocation_output_guardrail_rejected",
        "model_invocation_outcome_unknown",
        "model_invocation_abandoned",
    ]:
        assert terminal_event in hardening_up
    assert "model_invocation_started_fact_missing" in hardening_up
    assert "model_invocation_dispatch_fact_invalid" in hardening_up
    assert "model_invocation_causal_sequence_invalid" in hardening_up

    snapshot_columns = _postgres_table_columns(
        hardening_up, "tuge_model_observability_query_snapshot"
    )
    for snapshot_column in [
        "high_watermark_handle",
        "query_snapshot_id",
        "scope_hash",
        "query_hash",
        "cursor_signing_key",
        "rows_json",
        "row_count",
        "storage_bytes",
        "snapshot_to",
        "expires_at",
        "snapshot_mode",
    ]:
        assert snapshot_column in snapshot_columns
        assert snapshot_columns[snapshot_column]["nullable"] is False
    assert "materialized_result_set" in hardening_up
    assert "append_only_high_watermark" in hardening_up
    hardening_indexes = _all_index_sequences(hardening_up)
    assert hardening_indexes[
        ("tuge_model_observability_query_snapshot", "idx_tuge_model_snapshot_reuse")
    ] == ("scope_hash", "query_hash", "created_at")
    assert hardening_indexes[
        ("tuge_model_observability_query_snapshot", "idx_tuge_model_snapshot_expiry")
    ] == ("expires_at",)

    rollback_compact = " ".join(_strip_sql_comments(hardening_down).split())
    for rollback_fragment in [
        "drop trigger if exists trg_tuge_model_event_validate_fallback",
        "drop function if exists tuge_validate_model_event_fallback()",
        "drop table if exists tuge_model_observability_query_snapshot",
        "drop trigger if exists trg_tuge_model_event_append_only",
        "drop function if exists tuge_reject_model_event_mutation()",
        "drop constraint if exists fk_tuge_model_projection_fallback",
        "drop constraint if exists ck_tuge_model_projection_causal_sequence",
        "drop column if exists terminal_sequence",
        "drop column if exists dispatch_sequence",
        "drop column if exists started_sequence",
        "drop index if exists uq_tuge_model_event_server_sequence",
        "drop column if exists server_sequence",
        "drop table if exists tuge_model_observability_sequence",
    ]:
        assert rollback_fragment in rollback_compact
    for hardening_sql in (hardening_up, hardening_down):
        assert (
            re.search(
                r"\b(?:add|drop)\s+constraint(?:\s+if\s+exists)?\s+"
                r"ck_tuge_model_projection_time_order\b",
                _strip_sql_comments(hardening_sql),
            )
            is None
        )

    checksum_section = migration_runner.split("def checksum", 1)[1].split(
        "def discover_migrations", 1
    )[0]
    assert "hashlib.sha256" in checksum_section
    assert _migration_checksum_pair_paths(migration_runner_source) == (
        "up_path",
        "down_path",
    )
    assert 'glob("*.up.sql")' in migration_runner
    assert migration_runner.count("migration.checksum") >= 3


@pytest.mark.implementation
def test_python_task_security_schema_and_rollback_contract() -> None:
    migration_source = _text(
        "OBS_PAGE3_PYTHON_WORKTREE",
        "backend/task_manager/observability_internal/migration.py",
    )
    migration = migration_source.lower()
    models_source = _text(
        "OBS_PAGE3_PYTHON_WORKTREE",
        "backend/task_manager/observability_internal/models.py",
    )
    models = models_source.lower()

    assert "tuge_security_audit_event" in migration
    assert "tuge_obs30_query_snapshot" in migration
    assert "alter column source_ip_masked type varchar(80)" in migration
    statements = _literal_string_tuple(migration_source, "INDEX_STATEMENTS")
    indexes = _all_index_sequences(";\n".join(statements))
    for key, columns in _sqlmodel_index_sequences(models_source).items():
        existing = indexes.get(key)
        assert existing is None or existing == columns, (
            f"conflicting index definition: {key} migration={existing} model={columns}"
        )
        indexes[key] = columns
    index_violations = _index_sequence_violations(
        indexes,
        {
            (
                "tuge_security_audit_event",
                "idx_obs30_security_tenant_occurred",
            ): ("tenant_id", "occurred_at", "id"),
            (
                "tuge_security_audit_event",
                "idx_obs30_security_action",
            ): ("audit_action_id", "occurred_at", "id"),
            (
                "tuge_security_audit_event",
                "idx_obs30_security_session",
            ): ("access_session_id", "occurred_at", "id"),
            (
                "tuge_security_audit_event",
                "idx_obs30_security_parent",
            ): ("parent_audit_event_id",),
        },
    )
    assert not index_violations, "; ".join(index_violations)
    assert "drop table if exists tuge_obs30_query_snapshot" in migration
    assert "drop table if exists tuge_security_audit_event" in migration

    assert re.search(
        r"audit_action_id:\s*str\s*=\s*field\(nullable=false",
        models,
    )
    assert re.search(
        r"audit_layer:\s*str\s*=\s*field\([^\n]*nullable=false",
        models,
    )
    assert "source_ip_masked: str | none = field(default=none, max_length=80)" in models
