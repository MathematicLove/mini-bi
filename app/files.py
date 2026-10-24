import io
import json
import re
import sqlite3
from pathlib import Path

import pandas as pd

FILES_SOURCE = "uploaded_files"
FILES_SCHEMA = "uploaded_files"

SUPPORTED_TYPES = ["csv", "tsv", "txt", "xlsx", "xls", "ods", "json", "jsonl", "parquet", "xml"]

SQL_KEYWORDS = {
    "select", "from", "where", "group", "order", "by", "limit", "and", "or", "not", "as",
    "table", "index", "join", "on", "in", "is", "null", "like", "between", "case", "when",
    "then", "else", "end", "union", "all", "distinct", "having", "values", "insert", "update",
    "delete", "create", "drop", "primary", "key", "default", "check", "references", "count",
    "sum", "avg", "min", "max", "date", "time", "timestamp", "user",
}

class FileError(Exception):
    pass

def clean_name(raw, prefix):
    name = re.sub(r"\W+", "_", str(raw).strip().lower()).strip("_")
    if not name or name[0].isdigit():
        name = f"{prefix}_{name}" if name else prefix
    if name in SQL_KEYWORDS:
        name = f"{name}_"
    return name

def _dedupe(names):
    seen, out = {}, []
    for n in names:
        if n in seen:
            seen[n] += 1
            n = f"{n}_{seen[n]}"
        else:
            seen[n] = 0
        out.append(n)
    return out

def _prepare(df):
    df = df.copy()
    df.columns = _dedupe([clean_name(c, "col") for c in df.columns])
    for c in df.columns:
        if df[c].dtype == object:
            sample = df[c].dropna().head(50)
            if not sample.empty and sample.map(lambda v: isinstance(v, (dict, list))).any():
                df[c] = df[c].map(lambda v: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
    return df

def _read_text(data):
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise FileError("Could not decode the file (tried UTF-8 and Windows-1251).")

def _read_json(data):
    text = _read_text(data)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return pd.read_json(io.StringIO(text), lines=True)
    if isinstance(obj, dict):
        lists = {k: v for k, v in obj.items() if isinstance(v, list)}
        obj = next(iter(lists.values())) if len(lists) == 1 else [obj]
    return pd.json_normalize(obj)

def read_file(file_name, data):
    """Returns {table_name: DataFrame}; a workbook gives one table per sheet."""
    stem = clean_name(Path(file_name).stem, "file")
    ext = Path(file_name).suffix.lower().lstrip(".")
    try:
        if ext in ("csv", "txt"):
            frames = {stem: pd.read_csv(io.StringIO(_read_text(data)), sep=None, engine="python")}
        elif ext == "tsv":
            frames = {stem: pd.read_csv(io.StringIO(_read_text(data)), sep="\t")}
        elif ext in ("xlsx", "xls", "ods"):
            sheets = pd.read_excel(io.BytesIO(data), sheet_name=None)
            if len(sheets) == 1:
                frames = {stem: next(iter(sheets.values()))}
            else:
                frames = {f"{stem}_{clean_name(s, 'sheet')}": df for s, df in sheets.items()}
        elif ext == "json":
            frames = {stem: _read_json(data)}
        elif ext == "jsonl":
            frames = {stem: pd.read_json(io.StringIO(_read_text(data)), lines=True)}
        elif ext == "parquet":
            frames = {stem: pd.read_parquet(io.BytesIO(data))}
        elif ext == "xml":
            frames = {stem: pd.read_xml(io.BytesIO(data), parser="etree")}
        else:
            raise FileError(f"Unsupported file type .{ext}. Supported: {', '.join(SUPPORTED_TYPES)}.")
    except FileError:
        raise
    except Exception as exc:
        raise FileError(f"Could not read {file_name}: {exc}")

    frames = {name: _prepare(df) for name, df in frames.items() if not df.empty}
    if not frames:
        raise FileError(f"{file_name} has no rows.")
    return frames

def _data_type(series):
    if pd.api.types.is_bool_dtype(series):
        return "boolean"
    if pd.api.types.is_integer_dtype(series):
        return "bigint"
    if pd.api.types.is_float_dtype(series):
        return "double precision"
    if pd.api.types.is_datetime64_any_dtype(series):
        return "timestamp"
    return "text"

def catalog_rows(tables):
    """Rows in the same shape as backend.get_catalog: (source, schema, table, column, type, pk, fk)."""
    return [(FILES_SOURCE, FILES_SCHEMA, table, col, _data_type(df[col]), None, None)
            for table, df in tables.items() for col in df.columns]

def run_query(tables, sql_text):
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(f"ATTACH DATABASE ':memory:' AS {FILES_SCHEMA}")
        for name, df in tables.items():
            if re.search(rf"\b{FILES_SCHEMA}\.{re.escape(name)}\b", sql_text):
                df.to_sql(name, conn, index=False)
                conn.execute(f'CREATE TABLE {FILES_SCHEMA}."{name}" AS SELECT * FROM main."{name}"')
        return pd.read_sql_query(sql_text, conn)
    except Exception as exc:
        raise FileError(f"Query failed: {exc}")
    finally:
        conn.close()
