"""Checks that came out sound (expected: every line OK)."""
from _h import *
import warnings
from safeagentdb import ShadowDB, SafeModel, SafeAgentDBError
which()
warnings.simplefilter("ignore")
class E(SafeModel):
    __table_name__ = "emp"; id: int; user_id: int; boss: int | None = None
class D(SafeModel):
    __table_name__ = "docs"; id: int; user_id: int; data: object = None; at: object = None

def check(label, cond, extra=""):
    print(("OK  " if cond else "FAIL") + f" {label} {extra}")

# Self-referencing chain inserted with descending keys, then deleted.
for action in ("", " ON DELETE CASCADE", " ON DELETE SET NULL"):
    eng = prod([f"CREATE TABLE emp (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, boss INTEGER REFERENCES emp(id){action})",
                "INSERT INTO emp VALUES (1, 42, NULL)"])
    with ShadowDB(eng, tables=["emp"], tenant_id=42) as sb:
        sb.execute("INSERT INTO emp VALUES (30, 42, 1), (20, 42, 30), (10, 42, 20)")
        n = sb.commit_to_production()
    check(f"self-ref insert chain{action}", n == 3, rows(eng, "SELECT * FROM emp ORDER BY id"))
    with ShadowDB(eng, tables=["emp"], tenant_id=42) as sb:
        if action:
            sb.execute("DELETE FROM emp WHERE id = 30")   # sandbox applies the action
        else:
            sb.execute("DELETE FROM emp WHERE id IN (10, 20, 30)")
        cs = sb.diff()
        try:
            n = sb.commit_to_production(changeset=cs); ok = True
        except SafeAgentDBError as e:
            ok, n = False, f"{type(e).__name__}: {e}"
    check(f"self-ref delete{action}", ok, f"{n} {rows(eng, 'SELECT * FROM emp ORDER BY id')}")

# Real row cycle refused in diff, production untouched.
eng = prod(["CREATE TABLE emp (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, boss INTEGER REFERENCES emp(id))",
            "INSERT INTO emp VALUES (1, 42, NULL)"])
with ShadowDB(eng, tables=["emp"], tenant_id=42) as sb:
    sb.execute("INSERT INTO emp VALUES (5, 42, NULL), (6, 42, 5)")
    sb.execute("UPDATE emp SET boss = 6 WHERE id = 5")
    cs = sb.diff()
    check("row cycle blocked in diff", not cs.is_valid, cs.blocking_errors[-1][:60])

# JSON scalars and datetime offsets copied with INSERT ... SELECT.
eng = prod(["CREATE TABLE docs (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, data JSON, at DATETIME)",
            """INSERT INTO docs VALUES (1, 42, '"123"', '2024-01-01 10:00:00+02:00'),
                                       (2, 42, 'true', '2024-01-01T10:00:00Z'),
                                       (3, 42, '"null"', '2024-01-01 10:00:00'),
                                       (4, 42, '{"a": 1, "b": [1, 2.5]}', NULL)"""])
with ShadowDB(eng, tables=["docs"], tenant_id=42) as sb:
    sb.execute("INSERT INTO docs SELECT id + 10, user_id, data, at FROM docs")
    sb.execute("""UPDATE docs SET data = '{"a": true, "b": [1, 2.5]}' WHERE id = 4""")
    sb.execute("""UPDATE docs SET data = '{"b":[1,2.5],"a":1}' WHERE id = 14""")  # reformat only
    cs = sb.diff()
    upd = {d.pk["id"]: d.changed_columns() for d in cs.diffs if d.diff_type.value == "UPDATE"}
    check("JSON 1->true is a change; reformat is not", upd == {4: ["data"]}, upd)
    sb.commit_to_production(changeset=cs)
got = dict((r[0], (r[1], r[2])) for r in rows(eng, "SELECT id, data, at FROM docs"))
check("JSON scalars keep type", [got[i][0] for i in (11, 12, 13)] == ['"123"', 'true', '"null"'], [got[i][0] for i in (11, 12, 13)])
check("1 -> true written", got[4][0].replace(" ", "") == '{"a":true,"b":[1,2.5]}', got[4][0])
check("offsets kept", got[11][1].endswith("+02:00") and got[12][1].endswith("+00:00"), (got[11][1], got[12][1]))

# DELETE of rows with offset / CURRENT_TIMESTAMP-format datetimes and text booleans (P2-1).
eng = prod(["CREATE TABLE docs (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, data JSON, at DATETIME)",
            "INSERT INTO docs VALUES (1, 42, NULL, '2024-01-01 10:00:00'), (2, 42, NULL, '2024-01-01T10:00:00Z')"])
with ShadowDB(eng, tables=["docs"], tenant_id=42) as sb:
    sb.execute("DELETE FROM docs")
    sb.execute("SELECT 1")
    check("delete datetime rows", sb.commit_to_production() == 2)

# Key-change log: changed and changed back is not reported; swap is.
eng = prod(["CREATE TABLE emp (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, boss INTEGER)",
            "INSERT INTO emp VALUES (1, 42, NULL), (2, 42, NULL)"])
with ShadowDB(eng, tables=["emp"], tenant_id=42) as sb:
    sb.execute("UPDATE emp SET id = 9 WHERE id = 1"); sb.execute("UPDATE emp SET id = 1 WHERE id = 9")
    check("key changed back: not reported", sb.diff().is_valid)
    sb.execute("UPDATE emp SET id = 9 WHERE id = 1"); sb.execute("UPDATE emp SET id = 1 WHERE id = 2")
    sb.execute("UPDATE emp SET id = 2 WHERE id = 9")
    cs = sb.diff()
    check("swap reported", len(cs.blocking_errors) == 2, len(cs.blocking_errors))
    sb.execute("SAVEPOINT s"); sb.execute("UPDATE emp SET id = 50 WHERE id = 1"); sb.execute("ROLLBACK TO s"); sb.execute("RELEASE s")
    check("rolled-back key change leaves no log", len(sb.diff().blocking_errors) == 2)

# Tracking tamper is detected; the agent cannot erase the log.
eng = prod(["CREATE TABLE emp (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, boss INTEGER)",
            "INSERT INTO emp VALUES (1, 42, NULL)"])
with ShadowDB(eng, tables=["emp"], tenant_id=42) as sb:
    # (erasing/forging log rows is covered by s2_log_tamper.py)
    sb.execute("DROP TRIGGER _safeagentdb_keys_0_t")
    try: sb.diff(); det = False
    except SafeAgentDBError as e: det = type(e).__name__ == "SchemaError"
    check("dropped trigger -> SchemaError", det)

# tenant_id normalisation for str/int.
eng = prod(["CREATE TABLE emp (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, boss INTEGER)",
            "INSERT INTO emp VALUES (1, 42, NULL), (2, 7, NULL)"])
for tid, ok in (("42", True), (" 42 ", True), ("42.0", False), (True, False)):
    try:
        with ShadowDB(eng, tables=["emp"], tenant_id=tid) as sb:
            sb.execute("UPDATE emp SET boss = 1 WHERE id = 1")
            res = sb.diff().is_valid and sb.query("SELECT count(*) n FROM emp")[0]["n"] == 1
    except SafeAgentDBError as e:
        res = False
    check(f"tenant_id={tid!r}", res == ok)
