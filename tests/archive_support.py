"""What a stored database says about a run's own groups (修复轮14).

The start and the end of a run are no longer stored state records: they are its run-level groups (source `_run`), the
end the one built at the end time its own record wrote into the run's row.  A run whose row has no end has no RUN_END.
"""

RUN_GROUPS = (
    "SELECT g.order_no,CASE WHEN g.source_id<>'_run' THEN g.kind WHEN g.built_wall=r.ended_wall THEN 'RUN_END' "
    "ELSE 'RUN_START' END AS kind FROM commit_group g LEFT JOIN run r USING (run_id) WHERE {where} ORDER BY {order}"
)


def groups_in_order(conn, order="g.committed_at,g.order_no", where="true", params=()) -> list[dict]:
    """(order_no, kind) of every stored group, the run's own named RUN_START / RUN_END, in the order asked for."""
    return conn.execute(RUN_GROUPS.format(where=where, order=order), params).fetchall()


SAMPLES = (
    "SELECT s.t,s.value,s.seg,s.item,s.ua_status,s.state,s.bucket,c.point_id,c.source_id,n.run_id FROM sample s "
    "JOIN channel c ON c.no=s.channel JOIN run_number n ON n.no=s.run"
)


def samples(conn, where="true", params=(), order="s.t,s.item") -> list[dict]:
    """The stored rows read back by name: point, source and run, and the state as the replay reads it (a dict-row
    connection, as `ArchiveStore.session` gives)."""
    from htsg.records import read_state

    rows = conn.execute(f"{SAMPLES} WHERE {where} ORDER BY {order}", params).fetchall()
    return [{**x, **read_state(x["state"]), "status_code": x["ua_status"] & 0xFFFFFFFF if x["ua_status"] else None} for x in rows]


def said_and_held(conn) -> tuple[int, int]:
    """The rows the stored groups say they wrote, and the rows the database holds in the tables taken away with a bucket.
    A row names no group, so they are counted as a whole: equal when every group is whole and no row is left over."""
    x = conn.execute(
        "SELECT (SELECT coalesce(sum(records),0) FROM commit_group) AS said,"
        "(SELECT count(*) FROM sample)+(SELECT count(*) FROM state_event) AS held"
    ).fetchone()
    return (x["said"], x["held"]) if isinstance(x, dict) else tuple(x)
