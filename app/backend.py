import os
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
import streamlit as st

NUMERIC_TYPES = {"smallint", "integer", "bigint", "numeric", "real", "double precision"}

REQUEST_TIMEOUT = 8

class BackendError(Exception):
    pass

def _read_env_file():
    path = Path(__file__).resolve().parents[1] / ".env"
    values = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    return values

_ENV = {**_read_env_file(), **{k: v for k, v in os.environ.items() if v}}

def _env(key):
    if key not in _ENV:
        raise BackendError(f"Missing setting {key} (see .env.example).")
    return _ENV[key]

def source_url_key(source_id):
    return f"SOURCE_{source_id.upper()}_URL"

def _internal(url):
    # Inside Docker the agents published on the host are reached through this alias.
    alias = _ENV.get("SOURCE_HOST_ALIAS")
    if not alias:
        return url
    return url.replace("://localhost", f"://{alias}").replace("://127.0.0.1", f"://{alias}")

def _load_api_urls():
    ids = [s.strip() for s in _ENV.get("SOURCES", "").split(",") if s.strip()]
    return {source_id: _internal(_env(source_url_key(source_id))) for source_id in ids}

API_URLS = _load_api_urls()
TEMPLATES_API_URL = _env("TEMPLATES_API_URL")
META_API_URL = _env("META_API_URL")

def all_sources():
    return list(API_URLS.keys())

@st.cache_data(ttl=60, show_spinner="Loading schema from source agents...")
def _fetch_metadata(source_id: str):
    url = f"{API_URLS[source_id]}/metadata"
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
    except requests.exceptions.RequestException:
        raise BackendError(f"Source {source_id} is unreachable ({url}).")
    if resp.status_code != 200:
        raise BackendError(f"Source {source_id} metadata request failed (HTTP {resp.status_code}).")
    return resp.json()["columns"]

def get_catalog():
    rows, errors = [], []
    for source_id in all_sources():
        try:
            for c in _fetch_metadata(source_id):
                rows.append((source_id, c["schema_name"], c["table_name"], c["column_name"],
                             c["data_type"], c["pk_key"], c["fk_key"]))
        except BackendError as exc:
            errors.append(str(exc))
    return rows, errors

def tables_for(source_id, catalog):
    return sorted({r[2] for r in catalog if r[0] == source_id})

def columns_for(catalog, source_id=None, table_name=None):
    rows = catalog
    if source_id:
        rows = [r for r in rows if r[0] == source_id]
        if table_name:
            rows = [r for r in rows if r[2] == table_name]
    return rows

def is_numeric(data_type):
    return data_type in NUMERIC_TYPES

COMPARISON_OPS = ["=", "<", ">", "<=", ">="]

def _filter_conditions(filters):
    conds = []
    for col, op, value in filters:
        if op == "LIKE":
            escaped = str(value).replace("'", "''")
            conds.append(f"{col} LIKE '%{escaped}%'")
        elif op in COMPARISON_OPS:
            conds.append(f"{col} {op} {value}")
        else:
            escaped = str(value).replace("'", "''")
            conds.append(f"{col} = '{escaped}'")
    return conds

def build_where(filters):
    conds = _filter_conditions(filters)
    return f"\nWHERE {' AND '.join(conds)}" if conds else ""

def build_sql(schema_name, table_name, chart_type, dim_col, measure_col, second_dim_col,
              agg_func, filters, row_limit):
    table = f"{schema_name}.{table_name}"

    if chart_type == "scatter":
        select, group_by = f"{dim_col}, {measure_col}", ""
    elif chart_type == "heatmap" and second_dim_col:
        select = f"{dim_col}, {second_dim_col}, {agg_func}({measure_col}) AS {measure_col}"
        group_by = f"\nGROUP BY {dim_col}, {second_dim_col}"
    elif agg_func == "COUNT" and not measure_col:
        select, group_by = f"{dim_col}, COUNT(*) AS count", f"\nGROUP BY {dim_col}"
    else:
        select = f"{dim_col}, {agg_func}({measure_col}) AS {measure_col}"
        group_by = f"\nGROUP BY {dim_col}"

    where = build_where(filters)
    limit = f"\nLIMIT {row_limit}" if row_limit else ""
    return f"SELECT {select}\nFROM {table}{where}{group_by}{limit}"

def build_detail_sql(schema_name, table_name, columns, filters, row_limit):
    table = f"{schema_name}.{table_name}"
    where = build_where(filters)
    limit = f"\nLIMIT {row_limit}" if row_limit else "\nLIMIT 200"
    return f"SELECT {', '.join(columns)}\nFROM {table}{where}{limit}"

def filter_summary(filters) -> Optional[str]:
    conds = _filter_conditions(filters)
    return " AND ".join(conds) if conds else None

def run_query(source_id, sql_text) -> pd.DataFrame:
    url = f"{API_URLS[source_id]}/query"
    try:
        resp = requests.post(url, json={"sql_text": sql_text}, timeout=REQUEST_TIMEOUT)
    except requests.exceptions.RequestException:
        raise BackendError(f"Source {source_id} is unreachable ({url}).")

    body = resp.json()
    if resp.status_code != 200:
        raise BackendError(body.get("error", f"Query failed (HTTP {resp.status_code})."))
    return pd.DataFrame(body["rows"], columns=body["columns"])

def list_templates():
    url = f"{TEMPLATES_API_URL}/templates"
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
    except requests.exceptions.RequestException:
        raise BackendError(f"Templates service is unreachable ({url}).")
    if resp.status_code != 200:
        raise BackendError(resp.json().get("error", f"Could not load templates (HTTP {resp.status_code})."))
    return resp.json()["templates"]

def save_template(spec, sql_text, template_name=None):
    body = dict(
        sql_text=sql_text,
        source_id=spec["source_id"], schema_name=spec["schema_name"], table_name=spec["table_name"],
        chart_type=spec["chart_type"], dimension_col=spec["dim_col"], measure_col=spec["measure_col"],
        second_dim_col=spec["second_dim_col"], agg_func=spec["agg_func"],
        filter_summary=filter_summary(spec["filters"]), row_limit=spec["row_limit"],
        template_name=template_name or None,
    )
    url = f"{TEMPLATES_API_URL}/templates"
    try:
        resp = requests.post(url, json=body, timeout=REQUEST_TIMEOUT)
    except requests.exceptions.RequestException:
        raise BackendError(f"Templates service is unreachable ({url}).")
    if resp.status_code != 200:
        raise BackendError(resp.json().get("error", f"Could not save template (HTTP {resp.status_code})."))
    return resp.json()

def use_template(template_id):
    url = f"{TEMPLATES_API_URL}/templates/{template_id}/use"
    try:
        resp = requests.post(url, timeout=REQUEST_TIMEOUT)
    except requests.exceptions.RequestException:
        raise BackendError(f"Templates service is unreachable ({url}).")
    if resp.status_code != 200:
        raise BackendError(resp.json().get("error", f"Could not use template (HTTP {resp.status_code})."))
    return resp.json()

def delete_template(template_id):
    url = f"{TEMPLATES_API_URL}/templates/{template_id}"
    try:
        resp = requests.delete(url, timeout=REQUEST_TIMEOUT)
    except requests.exceptions.RequestException:
        raise BackendError(f"Templates service is unreachable ({url}).")
    if resp.status_code != 200:
        raise BackendError(resp.json().get("error", f"Could not delete template (HTTP {resp.status_code})."))
    return resp.json()

def get_diff(source_id):
    url = f"{META_API_URL}/diff"
    try:
        resp = requests.get(url, params={"source_id": source_id}, timeout=REQUEST_TIMEOUT)
    except requests.exceptions.RequestException:
        raise BackendError(f"Meta service is unreachable ({url}).")
    if resp.status_code != 200:
        raise BackendError(resp.json().get("error", f"Could not compute diff (HTTP {resp.status_code})."))
    return resp.json()

def sync_diff(source_id):
    url = f"{META_API_URL}/sync"
    try:
        resp = requests.post(url, params={"source_id": source_id}, timeout=REQUEST_TIMEOUT)
    except requests.exceptions.RequestException:
        raise BackendError(f"Meta service is unreachable ({url}).")
    if resp.status_code != 200:
        raise BackendError(resp.json().get("error", f"Could not sync (HTTP {resp.status_code})."))
    return resp.json()
