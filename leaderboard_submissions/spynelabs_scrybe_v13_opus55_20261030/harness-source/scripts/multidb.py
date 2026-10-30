"""Read-only query helper for multi-database environments.   from multidb import q, dbs

q(db_name, query) -> pandas.DataFrame
  SQL (postgres / sqlite / duckdb): pass a SQL string.
  MongoDB: pass a dict/JSON string: {"collection": c, "filter": {...}, "projection": {...}, "limit": n}
           or {"collection": c, "pipeline": [...]}. No limit unless you give one.
dbs() -> {name: type} for the logical databases in the database config.

The database config is $SCRYBE_DB_CONFIG, else db_config.json in the working directory ($SCRYBE_WORKDIR, else the
current directory): {"databases": {<name>: {"type": "postgres", "dsn": ...} | {"type": "sqlite"|"duckdb", "path": ...}
| {"type": "mongo", "uri": ..., "db": ...}}}. It is available whenever the environment provides such a config; the
harness's other tools (dbq, dbschema, dbsnap) use this module.

From harness/dab-untuned-sage/scripts/dab.py (v6.3 hardening kept: the config is read on first use, a missing or
broken config is a clear error; an unknown database name or a malformed Mongo spec raises a ValueError that lists
what is valid; Mongo clients are closed after each query; pandas' "only supports SQLAlchemy" warning is suppressed;
SQLite text that is not valid UTF-8 is decoded with replacement characters). Read-only: Postgres sessions with
default_transaction_read_only, SQLite opened mode=ro, DuckDB read_only. Core addition: every query is bounded by the
harness deadline ($SCRYBE_DEADLINE): after it no query starts (TimeoutError), and a running query is stopped at it
(Postgres statement_timeout, MongoDB maxTimeMS, DuckDB interrupt, SQLite progress handler). Without a deadline the
behaviour is the DAB helper's.
"""
import json
import os
import threading
import time
import warnings

import pandas as pd

DEADLINE_MARGIN_S = 5


def config_path():
    p = os.environ.get("SCRYBE_DB_CONFIG")
    if p:
        return p
    return os.path.join(os.environ.get("SCRYBE_WORKDIR") or os.getcwd(), "db_config.json")


def results_dir():
    """Where dbq saves full results: <working directory>/results."""
    return os.path.join(os.environ.get("SCRYBE_WORKDIR") or os.getcwd(), "results")


class _LazyConfig(dict):
    """dict of logical databases, loaded from the database config on first access."""
    _loaded = False

    def _load(self):
        if not self._loaded:
            path = config_path()
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)["databases"]
            except FileNotFoundError:
                raise RuntimeError(f"database config not found: {path}") from None
            except (ValueError, KeyError, TypeError) as e:
                raise RuntimeError(f"database config {path} is not valid ({type(e).__name__}: {e})") from None
            dict.update(self, data)
            self._loaded = True

    def __getitem__(self, k):
        self._load()
        return dict.__getitem__(self, k)

    def __contains__(self, k):
        self._load()
        return dict.__contains__(self, k)

    def __iter__(self):
        self._load()
        return dict.__iter__(self)

    def __len__(self):
        self._load()
        return dict.__len__(self)

    def get(self, k, d=None):
        self._load()
        return dict.get(self, k, d)

    def keys(self):
        self._load()
        return dict.keys(self)

    def values(self):
        self._load()
        return dict.values(self)

    def items(self):
        self._load()
        return dict.items(self)


_CFG = _LazyConfig()


def dbs():
    return {k: v["type"] for k, v in _CFG.items()}


def _conf(db):
    if db not in _CFG:
        raise ValueError(f"unknown database {db!r}; available: {', '.join(sorted(_CFG)) or '(none)'}")
    return _CFG[db]


def time_left():
    """Seconds a query may still run (deadline minus a margin), None without a deadline. Raises TimeoutError when
    the deadline has been reached."""
    d = os.environ.get("SCRYBE_DEADLINE")
    try:
        left = float(d) - time.time() - DEADLINE_MARGIN_S if d else None
    except ValueError:
        return None
    if left is not None and left <= 0:
        raise TimeoutError("the harness deadline has been reached; no further database queries")
    return left


def _sqlite_connect(path):
    import sqlite3
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.text_factory = lambda b: b.decode("utf-8", "replace")
    return con


def _mongo_spec(query):
    if isinstance(query, str):
        try:
            spec = json.loads(query)
        except ValueError as e:
            raise ValueError(f"Mongo query must be a dict or a JSON object string ({e})") from None
    else:
        spec = query
    if not isinstance(spec, dict) or not spec.get("collection"):
        raise ValueError('Mongo query needs {"collection": ..., "filter": {...}} or {"collection": ..., "pipeline": [...]}')
    return spec


def _mongo_docs(c, spec):
    from pymongo import MongoClient
    left = time_left()
    mt = {"maxTimeMS": int(left * 1000)} if left else {}
    with MongoClient(c["uri"], serverSelectionTimeoutMS=10000) as cl:
        coll = cl[c["db"]][spec["collection"]]
        if "pipeline" in spec:
            docs = list(coll.aggregate(spec["pipeline"], allowDiskUse=True, **mt))
        else:
            cur = coll.find(spec.get("filter", {}), spec.get("projection"))
            if mt:
                cur = cur.max_time_ms(mt["maxTimeMS"])
            if spec.get("limit"):
                cur = cur.limit(int(spec["limit"]))
            docs = list(cur)
    for d in docs:
        if "_id" in d:
            d["_id"] = str(d["_id"])
    return docs


def q(db, query):
    c = _conf(db)
    t = c.get("type")
    left = time_left()
    if t == "postgres":
        import psycopg2
        opts = "-c default_transaction_read_only=on" + (f" -c statement_timeout={int(left * 1000)}" if left else "")
        con = psycopg2.connect(c["dsn"], options=opts)
        try:
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message=".*pandas only supports SQLAlchemy.*")
                return pd.read_sql_query(query, con)
        finally:
            con.close()
    if t == "sqlite":
        con = _sqlite_connect(c["path"])
        if left:
            stop = time.time() + left
            con.set_progress_handler(lambda: 1 if time.time() > stop else 0, 10000)
        try:
            return pd.read_sql_query(query, con)
        finally:
            con.close()
    if t == "duckdb":
        import duckdb
        con = duckdb.connect(c["path"], read_only=True)
        timer = None
        if left:
            timer = threading.Timer(left, con.interrupt)
            timer.daemon = True
            timer.start()
        try:
            return con.execute(query).fetchdf()
        finally:
            if timer:
                timer.cancel()
            con.close()
    if t in ("mongo", "mongodb"):
        return pd.DataFrame(_mongo_docs(c, _mongo_spec(query)))
    raise ValueError(f"unsupported db type {t!r} for {db!r}")
