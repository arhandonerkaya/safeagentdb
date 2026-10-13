"""Per-type round trip on SQLite production.
Row 1-7: canonical values written by SQLAlchemy. Row 11-17: same logical values
stored in formats another application could have written.
For each row: (a) no-op diff, (b) edit an unrelated column, (c) delete."""
import datetime as dt, decimal, warnings
from typing import Any
from _h import *
from sqlalchemy import (MetaData, Table, Column, Integer, String, Text, Date, DateTime,
                        Numeric, Boolean, LargeBinary, JSON, insert)
from safeagentdb import ShadowDB, SafeModel, SafeAgentDBError

class R(SafeModel):
    __table_name__ = "r"
    id: int; user_id: int; note: str
    d: Any; ts: Any; tstz: Any; amount: Any; flag: Any; blob: Any; s: Any; js: Any

DDL = ("CREATE TABLE r (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, note TEXT NOT NULL,"
       " d DATE, ts DATETIME, tstz DATETIME, amount NUMERIC(10,2), flag BOOLEAN,"
       " blob BLOB, s VARCHAR(20), js JSON)")
LONG = "x" * 1_000_000

def fresh():
    eng = prod([DDL])
    md = MetaData()
    t = Table("r", md, Column("id", Integer, primary_key=True), Column("user_id", Integer),
              Column("note", Text), Column("d", Date), Column("ts", DateTime),
              Column("tstz", DateTime(timezone=True)), Column("amount", Numeric(10, 2)),
              Column("flag", Boolean), Column("blob", LargeBinary), Column("s", String(20)),
              Column("js", JSON))
    base = dict(user_id=42, note="n", d=None, ts=None, tstz=None, amount=None, flag=None,
                blob=None, s=None, js=None)
    canon = [
        dict(id=1, d=dt.date(2024, 2, 29), ts=dt.datetime(2024, 1, 1, 10, 0, 0, 123456)),
        dict(id=2, tstz=dt.datetime(2024, 1, 1, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))),
        dict(id=3, amount=decimal.Decimal("1234.10"), flag=True),
        dict(id=4, blob=b"\x00\xff\x10", s=""),
        dict(id=5, s=LONG),
        dict(id=6, js={"a": [1, 2, {"b": None}], "c": "x"}),
        dict(id=7, js=[1, "two", None]),
    ]
    with eng.begin() as c:
        for r in canon:
            c.execute(insert(t).values({**base, **r}))
        # Same logical values as another app might store them (raw SQL, bypassing SQLAlchemy).
        c.exec_driver_sql("""INSERT INTO r (id,user_id,note,ts) VALUES (11,42,'n','2024-01-01 10:00:00')""")
        c.exec_driver_sql("""INSERT INTO r (id,user_id,note,ts) VALUES (12,42,'n','2024-01-01T10:00:00')""")
        c.exec_driver_sql("""INSERT INTO r (id,user_id,note,amount) VALUES (13,42,'n','1234.10')""")
        c.exec_driver_sql("""INSERT INTO r (id,user_id,note,flag) VALUES (14,42,'n','true')""")
        c.exec_driver_sql("""INSERT INTO r (id,user_id,note,js) VALUES (15,42,'n','"123"')""")
        c.exec_driver_sql("""INSERT INTO r (id,user_id,note,js) VALUES (16,42,'n','true')""")
        c.exec_driver_sql("""INSERT INTO r (id,user_id,note,js) VALUES (17,42,'n','null')""")
    return eng

def raw(eng, i):
    with eng.connect() as c:
        return c.exec_driver_sql(f"SELECT d,typeof(d),ts,typeof(ts),tstz,amount,typeof(amount),flag,typeof(flag),"
                                 f"hex(blob),length(s),js,typeof(js) FROM r WHERE id={i}").fetchone()

eng = fresh()
try:
    with ShadowDB(eng, tables=["r"], tenant_id=42) as sb:
        print("clone ok; noop diff empty:", sb.diff().is_empty)
        for i in (1,2,3,4,5,6,7,11,12,13,14,15,16,17):
            v = {k: (str(x)[:40] + ('...' if len(str(x)) > 40 else '')) for k, x in sb.query(f"SELECT * FROM r WHERE id={i}")[0].items()
                 if k not in ("id","user_id","note") and x is not None}
            print(f"  sandbox row {i}: {v}")
except Exception as e:
    print("CLONE FAILED:", type(e).__name__, e)

print("\n(b) unrelated column edited, per row:")
for i in (1,2,3,4,5,6,7,11,12,13,14,15,16,17):
    eng = fresh(); before = raw(eng, i)
    with ShadowDB(eng, tables=["r"], tenant_id=42) as sb:
        sb.execute(f"UPDATE r SET note='edited' WHERE id={i}")
        cs = sb.diff()
        ok, msg = cs.validate_all()[0][1:]
        try: res = f"wrote {sb.commit_to_production(changeset=cs)}"
        except SafeAgentDBError as e: res = f"{type(e).__name__}"
    after = raw(eng, i)
    print(f"  row {i}: changed={cs.diffs[0].changed_columns()} valid={ok} {res}; other columns identical: {before == after}"
          + ("" if before == after else f"\n     before {before}\n     after  {after}"))

print("\n(c) delete, per row:")
for i in (1,2,3,4,5,6,7,11,12,13,14,15,16,17):
    eng = fresh()
    with ShadowDB(eng, tables=["r"], tenant_id=42) as sb:
        sb.execute(f"DELETE FROM r WHERE id={i}")
        cs = sb.diff()
        try: res = f"wrote {sb.commit_to_production(changeset=cs)}"
        except SafeAgentDBError as e: res = f"{type(e).__name__}: {str(e)[:150]}"
    print(f"  row {i}: valid={cs.is_valid} {res}")
