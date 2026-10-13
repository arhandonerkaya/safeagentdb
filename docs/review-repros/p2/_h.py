import os, sys, tempfile, warnings
from pathlib import Path
# docs/review-repros/<piece>/_h.py -> repository root, so the local library is used.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from sqlalchemy import create_engine, event, text

def prod(ddl, fk=True):
    """A fresh SQLite file database standing in for production."""
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

def rows(eng, sql):
    with eng.connect() as c:
        return [tuple(r) for r in c.exec_driver_sql(sql)]

def banner(cs):
    return cs._render_plain().splitlines()[0]
