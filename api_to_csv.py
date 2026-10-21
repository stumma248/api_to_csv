#!/usr/bin/env python3
"""
Enterprise REST API -> flattened CSV ingestion utility.

Designed for Informatica PowerCenter Command Task integration.

Features
--------
* GET / POST / PUT / PATCH / DELETE
* Basic, Bearer and OAuth2 client-credentials authentication
* OAuth2 token caching and refresh
* Environment-variable secrets
* Query params / headers / JSON body
* Retry with exponential backoff and Retry-After
* Page-number, offset, cursor and next-link pagination
* Recursive JSON flattening
* Nested arrays -> rows
* Parent fields repeated on child rows
* Cartesian or zip handling for sibling arrays
* Schema discovery and schema-drift detection
* Optional schema contract
* Optional PII masking
* Optional raw response archive
* Audit/run-control records
* CSV UTF-8 output
* PowerCenter-friendly exit codes
* HTTP proxy / TLS verification / timeout
* Structured logs
* Atomic CSV completion
* Optional response-size guard

Exit codes
----------
0 = success
1 = functional/runtime failure
2 = configuration/dependency error
3 = schema drift / contract failure
4 = API/authentication failure
5 = output/audit failure
"""

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from itertools import product
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
import yaml


EXIT_SUCCESS = 0
EXIT_RUNTIME = 1
EXIT_CONFIG = 2
EXIT_SCHEMA = 3
EXIT_API = 4
EXIT_OUTPUT = 5


class ApiToCsvError(Exception):
    exit_code = EXIT_RUNTIME


class ConfigError(ApiToCsvError):
    exit_code = EXIT_CONFIG


class ApiError(ApiToCsvError):
    exit_code = EXIT_API


class SchemaDriftError(ApiToCsvError):
    exit_code = EXIT_SCHEMA


class OutputError(ApiToCsvError):
    exit_code = EXIT_OUTPUT


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def env_substitute(value):
    """Supports ${VAR} and ${VAR:-default} recursively."""
    if isinstance(value, str):
        pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")

        def repl(match):
            name = match.group(1)
            default = match.group(2)
            val = os.getenv(name)
            if val is None:
                if default is not None:
                    return default
                return ""
            return val

        return pattern.sub(repl, value)

    if isinstance(value, dict):
        return {k: env_substitute(v) for k, v in value.items()}

    if isinstance(value, list):
        return [env_substitute(v) for v in value]

    return value


def get_path(data, path):
    """Simple dot path. Supports array indexes: data.items.0.id"""
    if not path:
        return data

    current = data
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            idx = int(part)
            current = current[idx] if idx < len(current) else None
        else:
            return None
    return current


def json_scalar(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return value


def flatten_scalar_object(obj, prefix="", sep="_"):
    """
    Flatten dictionaries but retain arrays as arrays.
    """
    result = {}
    if not isinstance(obj, dict):
        return result

    for key, value in obj.items():
        col = f"{prefix}{sep}{key}" if prefix else str(key)

        if isinstance(value, dict):
            result.update(flatten_scalar_object(value, col, sep))
        elif isinstance(value, list):
            result[col] = value
        else:
            result[col] = json_scalar(value)

    return result


def expand_node(node, array_mode="cartesian", prefix="", sep="_"):
    """
    Recursive hierarchical-to-relational conversion.

    Rules:
      object -> columns
      array  -> rows
      scalar -> column
    """
    if isinstance(node, list):
        rows = []
        for item in node:
            rows.extend(expand_node(item, array_mode, prefix, sep))
        return rows

    if not isinstance(node, dict):
        return [{prefix or "value": json_scalar(node)}]

    base = {}
    arrays = []

    for key, value in node.items():
        col = f"{prefix}{sep}{key}" if prefix else str(key)

        if isinstance(value, list):
            arrays.append((col, value))
        elif isinstance(value, dict):
            nested = expand_node(value, array_mode, col, sep)
            if len(nested) == 1:
                base.update(nested[0])
            else:
                arrays.append((col, nested))
        else:
            base[col] = json_scalar(value)

    if not arrays:
        return [base]

    groups = []
    for col, values in arrays:
        if isinstance(values, list):
            child_rows = []
            for item in values:
                if isinstance(item, dict):
                    child_rows.extend(expand_node(item, array_mode, col, sep))
                else:
                    child_rows.append({col: json_scalar(item)})
            groups.append(child_rows or [{col: ""}])
        else:
            groups.append(values or [{col: ""}])

    rows = []

    if array_mode == "zip":
        count = max(len(g) for g in groups)
        for i in range(count):
            row = dict(base)
            for group in groups:
                if i < len(group):
                    row.update(group[i])
            rows.append(row)
    else:
        for combo in product(*groups):
            row = dict(base)
            for child in combo:
                row.update(child)
            rows.append(row)

    return rows


def discover_columns(rows):
    columns = []
    seen = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                columns.append(key)
    return columns


def normalize_columns(columns, max_length=250):
    """
    Make dynamic API field names CSV/DB-friendly.
    Keeps names unique after normalization.
    """
    result = []
    used = {}

    for original in columns:
        name = str(original).strip()
        name = re.sub(r"[^A-Za-z0-9_]+", "_", name)
        name = re.sub(r"_+", "_", name).strip("_") or "COLUMN"
        if name[0].isdigit():
            name = "_" + name
        name = name[:max_length]

        base = name
        count = used.get(base, 0) + 1
        used[base] = count
        if count > 1:
            suffix = f"_{count}"
            name = (base[: max_length - len(suffix)] + suffix)

        result.append(name)

    return result


def apply_column_mapping(rows, original_columns, normalized_columns):
    mapping = dict(zip(original_columns, normalized_columns))
    return [
        {mapping[k]: v for k, v in row.items()}
        for row in rows
    ]


def hash_value(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def mask_rows(rows, columns, mask_cfg):
    if not mask_cfg:
        return rows

    mode = mask_cfg.get("mode", "partial")
    configured = set(mask_cfg.get("columns", []))

    for row in rows:
        for col in list(row):
            if col not in configured:
                continue

            value = row.get(col, "")
            if value in (None, ""):
                continue

            if mode == "hash":
                row[col] = hash_value(value)
            elif mode == "full":
                row[col] = "***MASKED***"
            else:
                s = str(value)
                keep = int(mask_cfg.get("keep_last", 4))
                row[col] = ("*" * max(0, len(s) - keep)) + s[-keep:]

    return rows


class OAuth2TokenProvider:
    def __init__(self, config):
        self.cfg = config
        self.token = None
        self.expires_at = 0

    def get_token(self):
        now = time.time()
        if self.token and now < self.expires_at - 60:
            return self.token

        token_url = self.cfg["token_url"]
        client_id = self.cfg["client_id"]
        client_secret = self.cfg["client_secret"]
        scope = self.cfg.get("scope")

        data = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
        if scope:
            data["scope"] = scope

        response = requests.post(
            token_url,
            data=data,
            timeout=int(self.cfg.get("timeout_seconds", 60)),
            verify=self.cfg.get("verify_tls", True),
            proxies=self.cfg.get("proxies")
        )

        if not response.ok:
            raise ApiError(
                f"OAuth2 token request failed: HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise ApiError("OAuth2 token endpoint did not return JSON") from exc

        token = payload.get("access_token")
        if not token:
            raise ApiError("OAuth2 response does not contain access_token")

        self.token = token
        self.expires_at = now + int(payload.get("expires_in", 3600))
        return self.token


class ApiClient:
    def __init__(self, config, run_id):
        self.config = config
        self.api = config["api"]
        self.run_id = run_id
        self.session = requests.Session()

        self.timeout = int(self.api.get("timeout_seconds", 120))
        self.retries = int(self.api.get("retries", 3))
        self.backoff = float(self.api.get("retry_backoff_seconds", 2))
        self.verify_tls = self.api.get("verify_tls", True)
        self.proxies = self.api.get("proxies")
        self.oauth = None

        auth = config.get("authentication", {}) or {}
        auth_type = auth.get("type", "none").lower()

        if auth_type == "oauth2_client_credentials":
            self.oauth = OAuth2TokenProvider(auth)

    def headers(self):
        headers = dict(self.api.get("headers", {}) or {})
        auth = self.config.get("authentication", {}) or {}
        auth_type = auth.get("type", "none").lower()

        if auth_type == "bearer":
            headers["Authorization"] = f"Bearer {auth.get('token', '')}"

        elif auth_type == "oauth2_client_credentials":
            headers["Authorization"] = f"Bearer {self.oauth.get_token()}"

        return headers

    def request(self, method, url=None, params=None, body=None):
        method = method.upper()
        url = url or self.api["url"]

        retry_statuses = set(
            self.api.get(
                "retry_statuses",
                [408, 425, 429, 500, 502, 503, 504]
            )
        )

        last_error = None

        for attempt in range(1, self.retries + 1):
            try:
                response = self.session.request(
                    method=method,
                    url=url,
                    headers=self.headers(),
                    params=params,
                    json=body if method in ("POST", "PUT", "PATCH") else None,
                    timeout=self.timeout,
                    verify=self.verify_tls,
                    proxies=self.proxies
                )

                logging.info(
                    "run_id=%s method=%s status=%s attempt=%s url=%s",
                    self.run_id, method, response.status_code, attempt, url
                )

                if response.status_code in retry_statuses:
                    if attempt < self.retries:
                        retry_after = response.headers.get("Retry-After")
                        try:
                            delay = float(retry_after) if retry_after else self.backoff * (2 ** (attempt - 1))
                        except ValueError:
                            delay = self.backoff * (2 ** (attempt - 1))

                        logging.warning(
                            "run_id=%s retryable HTTP %s; sleeping %.1fs",
                            self.run_id, response.status_code, delay
                        )
                        time.sleep(delay)
                        continue

                # OAuth token can expire between token acquisition and request.
                if response.status_code == 401 and self.oauth and attempt < self.retries:
                    self.oauth.token = None
                    self.oauth.expires_at = 0
                    time.sleep(self.backoff)
                    continue

                if not response.ok:
                    raise ApiError(
                        f"HTTP {response.status_code}: {response.text[:1000]}"
                    )

                return response

            except requests.RequestException as exc:
                last_error = exc
                if attempt < self.retries:
                    delay = self.backoff * (2 ** (attempt - 1))
                    logging.warning(
                        "run_id=%s request exception=%s retry_in=%.1fs",
                        self.run_id, exc, delay
                    )
                    time.sleep(delay)
                else:
                    break

        raise ApiError(f"API request failed after {self.retries} attempts: {last_error}")


def parse_response(response, max_bytes):
    content_length = response.headers.get("Content-Length")
    if content_length and int(content_length) > max_bytes:
        raise ApiError(
            f"Response exceeds max_response_bytes={max_bytes}"
        )

    raw = response.content
    if len(raw) > max_bytes:
        raise ApiError(
            f"Response exceeds max_response_bytes={max_bytes}"
        )

    try:
        return response.json()
    except ValueError as exc:
        raise ApiError(
            f"API response is not valid JSON. Content-Type="
            f"{response.headers.get('Content-Type', '')}"
        ) from exc


def archive_raw(payload, raw_dir, run_id, page_no):
    Path(raw_dir).mkdir(parents=True, exist_ok=True)
    path = Path(raw_dir) / f"{run_id}_page_{page_no:05d}.json"
    tmp = str(path) + ".tmp"

    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)

    os.replace(tmp, path)
    return str(path)


def extract_items(payload, data_path):
    data = get_path(payload, data_path) if data_path else payload
    return data


def fetch_all(config, client, raw_dir=None):
    pagination = config.get("pagination", {}) or {}
    mode = pagination.get("mode", "none").lower()
    page_count = 0
    all_payload_parts = []

    method = config["api"].get("method", "GET").upper()
    base_params = dict(config["api"].get("params", {}) or {})
    body = config["api"].get("body")

    if mode == "none":
        response = client.request(method, params=base_params, body=body)
        payload = parse_response(
            response,
            int(config["api"].get("max_response_bytes", 100 * 1024 * 1024))
        )
        page_count += 1
        if raw_dir:
            archive_raw(payload, raw_dir, client.run_id, page_count)
        return payload, page_count

    if mode == "page_number":
        page_param = pagination.get("page_param", "page")
        start_page = int(pagination.get("start_page", 1))
        max_pages = int(pagination.get("max_pages", 10000))
        page_size = pagination.get("page_size")
        all_items = []

        for page in range(start_page, start_page + max_pages):
            params = dict(base_params)
            params[page_param] = page

            response = client.request(method, params=params, body=body)
            payload = parse_response(
                response,
                int(config["api"].get("max_response_bytes", 100 * 1024 * 1024))
            )
            page_count += 1

            if raw_dir:
                archive_raw(payload, raw_dir, client.run_id, page_count)

            items = extract_items(payload, pagination.get("data_path"))

            if not isinstance(items, list):
                raise ApiError(
                    "page_number pagination requires data_path to point to an array"
                )

            all_items.extend(items)

            if not items or (page_size and len(items) < int(page_size)):
                break

        return all_items, page_count

    if mode == "offset":
        offset_param = pagination.get("offset_param", "offset")
        limit_param = pagination.get("limit_param", "limit")
        offset = int(pagination.get("start_offset", 0))
        page_size = int(pagination.get("page_size", 100))
        max_pages = int(pagination.get("max_pages", 10000))
        all_items = []

        for _ in range(max_pages):
            params = dict(base_params)
            params[offset_param] = offset
            params[limit_param] = page_size

            response = client.request(method, params=params, body=body)
            payload = parse_response(
                response,
                int(config["api"].get("max_response_bytes", 100 * 1024 * 1024))
            )
            page_count += 1

            if raw_dir:
                archive_raw(payload, raw_dir, client.run_id, page_count)

            items = extract_items(payload, pagination.get("data_path"))

            if not isinstance(items, list):
                raise ApiError(
                    "offset pagination requires data_path to point to an array"
                )

            all_items.extend(items)

            if len(items) < page_size:
                break

            offset += page_size

        return all_items, page_count

    if mode == "next_url":
        next_path = pagination.get("next_url_path")
        max_pages = int(pagination.get("max_pages", 10000))
        url = config["api"]["url"]
        all_items = []

        for _ in range(max_pages):
            response = client.request(method, url=url, params=base_params, body=body)
            payload = parse_response(
                response,
                int(config["api"].get("max_response_bytes", 100 * 1024 * 1024))
            )
            page_count += 1

            if raw_dir:
                archive_raw(payload, raw_dir, client.run_id, page_count)

            items = extract_items(payload, pagination.get("data_path"))
            if isinstance(items, list):
                all_items.extend(items)
            else:
                all_items.append(items)

            url = get_path(payload, next_path)
            if not url:
                break

        return all_items, page_count

    if mode == "cursor":
        cursor_param = pagination.get("cursor_param", "cursor")
        cursor_path = pagination.get("next_cursor_path")
        max_pages = int(pagination.get("max_pages", 10000))
        cursor = pagination.get("start_cursor")
        all_items = []

        for _ in range(max_pages):
            params = dict(base_params)
            if cursor not in (None, ""):
                params[cursor_param] = cursor

            response = client.request(method, params=params, body=body)
            payload = parse_response(
                response,
                int(config["api"].get("max_response_bytes", 100 * 1024 * 1024))
            )
            page_count += 1

            if raw_dir:
                archive_raw(payload, raw_dir, client.run_id, page_count)

            items = extract_items(payload, pagination.get("data_path"))
            if not isinstance(items, list):
                raise ApiError(
                    "cursor pagination requires data_path to point to an array"
                )

            all_items.extend(items)

            new_cursor = get_path(payload, cursor_path)
            if not new_cursor or new_cursor == cursor:
                break

            cursor = new_cursor

        return all_items, page_count

    raise ConfigError(f"Unsupported pagination.mode: {mode}")


def write_schema(schema_file, columns, metadata):
    path = Path(schema_file)
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "version": 1,
        "updated_at": utc_now(),
        "columns": columns,
        "metadata": metadata,
    }

    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def check_schema(config, columns):
    schema_cfg = config.get("schema", {}) or {}
    mode = schema_cfg.get("mode", "discover").lower()

    schema_file = schema_cfg.get("file")

    if mode == "discover":
        if schema_file:
            if os.path.exists(schema_file):
                with open(schema_file, encoding="utf-8") as fh:
                    old = json.load(fh).get("columns", [])
                if old and old != columns:
                    logging.warning(
                        "Schema changed. Previous=%s Current=%s",
                        old, columns
                    )

            write_schema(schema_file, columns, {"mode": "discover"})
        return

    if mode == "strict":
        expected = schema_cfg.get("columns", [])
        if not expected:
            raise ConfigError("schema.mode=strict requires schema.columns")

        if columns != expected:
            missing = [c for c in expected if c not in columns]
            added = [c for c in columns if c not in expected]
            raise SchemaDriftError(
                f"Schema drift detected. Missing={missing}; Added={added}"
            )

    elif mode == "allow_additive":
        if schema_file and os.path.exists(schema_file):
            with open(schema_file, encoding="utf-8") as fh:
                old = json.load(fh).get("columns", [])

            removed = [c for c in old if c not in columns]
            if removed:
                raise SchemaDriftError(
                    f"Schema breaking change. Removed columns={removed}"
                )

        if schema_file:
            write_schema(schema_file, columns, {"mode": "allow_additive"})

    else:
        raise ConfigError(f"Unsupported schema.mode: {mode}")


def atomic_write_csv(rows, output_file):
    path = Path(output_file)
    path.parent.mkdir(parents=True, exist_ok=True)

    if not rows:
        raise OutputError("No rows generated; CSV was not created.")

    columns = discover_columns(rows)

    fd, temp_path = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent)
    )
    os.close(fd)

    try:
        with open(temp_path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=columns,
                extrasaction="ignore",
                quoting=csv.QUOTE_MINIMAL
            )
            writer.writeheader()
            for row in rows:
                writer.writerow({
                    col: json_scalar(row.get(col, ""))
                    for col in columns
                })

        os.replace(temp_path, path)

    except Exception as exc:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise OutputError(f"Failed to create CSV: {exc}") from exc

    return columns


def write_audit(config, audit):
    audit_file = config.get("audit", {}).get(
        "file",
        "./audit/api_to_csv_audit.jsonl"
    )
    path = Path(audit_file)
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(audit, ensure_ascii=False) + "\n")


def configure_logging(config, run_id):
    cfg = config.get("logging", {}) or {}
    level = getattr(logging, cfg.get("level", "INFO").upper(), logging.INFO)

    handlers = [logging.StreamHandler()]

    if cfg.get("file"):
        Path(cfg["file"]).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(cfg["file"], encoding="utf-8"))

    logging.basicConfig(
        level=level,
        format=f"%(asctime)s %(levelname)s run_id={run_id} %(message)s",
        handlers=handlers
    )


def load_config(path):
    try:
        with open(path, encoding="utf-8") as fh:
            config = yaml.safe_load(fh)
    except Exception as exc:
        raise ConfigError(f"Cannot read configuration: {exc}") from exc

    config = env_substitute(config)

    if not isinstance(config, dict):
        raise ConfigError("Configuration must be a YAML object.")

    if "api" not in config or not config["api"].get("url"):
        raise ConfigError("api.url is required")

    return config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args()

    run_id = args.run_id or (
        datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        + "_"
        + uuid.uuid4().hex[:8]
    )

    start = time.time()
    audit = {
        "run_id": run_id,
        "start_time_utc": utc_now(),
        "status": "STARTED",
    }

    try:
        config = load_config(args.config)
        configure_logging(config, run_id)

        logging.info("Starting API-to-CSV ingestion")

        api_name = config["api"].get("name", "UNKNOWN")
        method = config["api"].get("method", "GET").upper()

        audit.update({
            "api_name": api_name,
            "method": method,
            "config_file": args.config,
        })

        client = ApiClient(config, run_id)

        raw_cfg = config.get("raw_archive", {}) or {}
        raw_dir = raw_cfg.get("directory") if raw_cfg.get("enabled", False) else None

        payload, pages = fetch_all(config, client, raw_dir)

        output_cfg = config.get("output", {}) or {}
        data_path = output_cfg.get("data_path")
        if data_path:
            payload = get_path(payload, data_path)

        rows = expand_node(
            payload,
            array_mode=output_cfg.get("array_mode", "cartesian"),
            sep=output_cfg.get("column_separator", "_")
        )

        original_columns = discover_columns(rows)
        normalized_columns = normalize_columns(
            original_columns,
            int(output_cfg.get("max_column_name_length", 250))
        )

        if original_columns != normalized_columns:
            rows = apply_column_mapping(
                rows,
                original_columns,
                normalized_columns
            )

        rows = mask_rows(
            rows,
            normalized_columns,
            config.get("masking")
        )

        columns = discover_columns(rows)

        check_schema(config, columns)

        output_file = output_cfg.get("file")
        if not output_file:
            raise ConfigError("output.file is required")

        final_columns = atomic_write_csv(rows, output_file)

        elapsed = round(time.time() - start, 3)

        audit.update({
            "status": "SUCCESS",
            "end_time_utc": utc_now(),
            "elapsed_seconds": elapsed,
            "pages": pages,
            "rows": len(rows),
            "columns": len(final_columns),
            "output_file": output_file,
        })

        write_audit(config, audit)

        print(
            f"SUCCESS|RUN_ID={run_id}|FILE={output_file}|"
            f"ROWS={len(rows)}|COLUMNS={len(final_columns)}|PAGES={pages}"
        )
        return EXIT_SUCCESS

    except ApiToCsvError as exc:
        logging.exception("Ingestion failed")
        audit.update({
            "status": "FAILED",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "end_time_utc": utc_now(),
            "elapsed_seconds": round(time.time() - start, 3),
        })

        try:
            # If config couldn't load, use a default audit location.
            cfg = locals().get("config")
            if cfg:
                write_audit(cfg, audit)
        except Exception:
            pass

        print(
            f"ERROR|RUN_ID={run_id}|CODE={exc.exit_code}|ERROR={exc}",
            file=sys.stderr
        )
        return exc.exit_code

    except Exception as exc:
        logging.exception("Unexpected ingestion failure")
        print(
            f"ERROR|RUN_ID={run_id}|CODE={EXIT_RUNTIME}|ERROR={exc}",
            file=sys.stderr
        )
        return EXIT_RUNTIME


if __name__ == "__main__":
    sys.exit(main())
