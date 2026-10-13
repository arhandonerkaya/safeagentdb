import os, sys, tempfile
from pathlib import Path

from sqlalchemy import create_engine, event

# docs/review-repros/b/_h.py -> repository root, so the local library is used.
# SAFEAGENTDB_ROOT=<path> runs another checkout instead.
_root = os.environ.get("SAFEAGENTDB_ROOT") or str(Path(__file__).resolve().parents[3])
sys.path.insert(0, os.path.abspath(_root))

def prod(ddl, fk=True):
    path = os.path.join(tempfile.mkdtemp(), "prod.db")
    eng = create_engine(f"sqlite:///{path}")
    if fk:
        @event.listens_for(eng, "connect")
        def _fk(c, _):
            c.execute("PRAGMA foreign_keys=ON")
    with eng.begin() as c:
        for s in ddl:
            c.exec_driver_sql(s)
    return eng

def prod_serial(ddl):
    """Production where `DEFAULT (nextval('...'))` stands in for a PostgreSQL
    serial: the sandbox cannot reproduce it, so the key becomes provisional."""
    path = os.path.join(tempfile.mkdtemp(), "prod.db")
    eng = create_engine(f"sqlite:///{path}")
    @event.listens_for(eng, "connect")
    def _c(c, _):
        c.execute("PRAGMA foreign_keys=ON")
        c.create_function("nextval", 1, lambda name: None)
    with eng.begin() as c:
        for s in ddl:
            c.exec_driver_sql(s)
    return eng

def rows(eng, sql):
    with eng.connect() as c:
        return [tuple(r) for r in c.exec_driver_sql(sql)]

def which():
    import safeagentdb
    print("library:", os.path.dirname(safeagentdb.__file__), safeagentdb.__version__)
