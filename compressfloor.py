#!/usr/bin/env python
"""Name what holds an enterprise geodatabase's compress floor, from exported system tables.

Compress on a traditionally versioned geodatabase exits 0 whether or not it got
anywhere. It removes states no version references and folds edits common to
every version into the base tables. One version that still points at an old
state stops that fold at the state where it forked from DEFAULT, every night,
with a success status. The version is often not a person's: a replica keeps
hidden SYNC_ system versions, and a replica that was unregistered can leave
them behind.

This tool reads the exported results of read-only SELECTs against the version,
state and state lineage tables, and optionally the replica items, the replica
log and a per-state count of delta rows. It reports the floor state, every
thing holding it, and the delta rows the compress cannot fold.

The obvious tools already do part of this. Esri Technical Article 000011719 is
the manual procedure for finding detached replica system versions. Scripts
such as StateLineageCheck.py in phillegard/ArcGIS_Maintenance count states
against a threshold through arcpy. Neither says which version or replica holds
the floor, and neither runs offline against exported rows.

    python compressfloor.py --self-test
    python compressfloor.py --sql sqlserver
    python compressfloor.py --versions v.csv --states s.csv --lineages l.csv
    python compressfloor.py --versions v.csv --states s.csv --lineages l.csv \\
        --replicas r.csv --replica-log log.csv --deltas d.csv --max-age-days 14
    python compressfloor.py ... --report floor.txt --apply

Nothing is written without --apply, and only the --report file is ever written.

Exit codes: 0 nothing in the exports holds the floor past the age limit, 1 at
least one blocker, 2 the tool could not read its input or write its report.
The exports hold versions, not state locks: a connected session's lock on an
old state can hold compress too, and this tool does not see it.
"""

from __future__ import print_function

import argparse
import bisect
import contextlib
import csv
import datetime
import io
import json
import math
import os
import re
import shutil
import sys
import tempfile

# =============================================================================
# CONFIGURATION. Deliberately not flags. Change here, not at the call site.
# =============================================================================

# A holder is a blocker once DEFAULT moved past its fork more than this many
# days ago. 30 days is long enough for an ordinary edit version to be
# reconciled and posted, and far shorter than the months a stalled replica
# goes unseen.
DEFAULT_MAX_AGE_DAYS = 30

# Replica system version names, from Esri Technical Article 000011719:
# SYNC_RECEIVE_<replica id>_<generation>, SYNC_RECEIVE_REC_<replica id>_<gen>
# and SYNC_SEND_<replica id>_<generation>.
SYNC_RE = re.compile(r"^SYNC_(SEND|RECEIVE_REC|RECEIVE)_(\d+)_(\d+)$", re.I)

DETACHED = "DETACHED_REPLICA_VERSION"
STALLED = "STALLED_REPLICA"
UNRESOLVED = "UNRESOLVED_REPLICA_VERSION"
ANCIENT = "ANCIENT_VERSION"
ORPHANED = "ORPHANED_STATES"
REPLICA = "REPLICA"
VERSION = "VERSION"

# Columns each export must carry. Headers are matched case-insensitively.
REQUIRED = {
    "versions": ("name", "owner", "state_id"),
    "states": ("state_id", "lineage_name", "creation_time"),
    "lineages": ("lineage_name", "lineage_id"),
    "replicas": ("objectid", "item_type", "id", "name"),
    "replica_log": ("replicaid", "logdate", "sourceendgen", "targetgen"),
    "deltas": ("state_id", "delta_rows"),
}

# Every shipped SELECT also returns COUNT(*) OVER () under this name: the
# number of rows the query produced, repeated on each row. An export file
# must carry it, and the tool refuses a file whose row count differs. A
# row limit, a spreadsheet's row cap or a paged copy loses rows silently,
# and a lost lineage or state row can turn a blocker into a clean exit 0.
ROW_COUNT = "row_count"

# The shipped versions SELECT also returns the database clock under this
# name. It is the default as-of time. Without it, ages would be measured
# from the newest state or replica log event, and on a geodatabase where
# editing has stopped every holder would look young for ever: a stalled
# replica would never cross the limit, and a nightly run would exit 0.
EXPORTED_AT = "exported_at"

# The only header names an error message may echo. A wrong file passed by
# mistake, such as a .env or a .pgpass, must never be printed to a job log.
KNOWN_COLUMNS = set(c for columns in REQUIRED.values() for c in columns)
KNOWN_COLUMNS.update((ROW_COUNT, EXPORTED_AT))

# The read-only SELECTs that produce each export, per DBMS. Table and column
# names are from Esri's geodatabase system table documentation; the GPReplica
# paths are Esri Technical Article 000011719's and the GPSyncReplica paths an
# Esri Community thread's. See the README for the links.
# A SQL Server geodatabase in the dbo schema needs dbo. in place of sde.
SQL = {
    "sqlserver": """\
-- versions.csv
SELECT name, owner, state_id,
       CONVERT(varchar(19), CURRENT_TIMESTAMP, 120) AS exported_at,
       COUNT(*) OVER () AS row_count
FROM sde.SDE_versions;

-- states.csv
SELECT state_id, lineage_name,
       CONVERT(varchar(19), creation_time, 120) AS creation_time,
       COUNT(*) OVER () AS row_count
FROM sde.SDE_states;

-- lineages.csv
SELECT lineage_name, lineage_id, COUNT(*) OVER () AS row_count
FROM sde.SDE_state_lineages;

-- replicas.csv (optional)
SET QUOTED_IDENTIFIER ON;
SELECT items.ObjectID AS objectid, itemtypes.Name AS item_type,
       COALESCE(items.Definition.value('(/GPReplica/ID)[1]', 'nvarchar(max)'),
                items.Definition.value('(/GPSyncReplica/ID)[1]', 'nvarchar(max)')) AS id,
       COALESCE(items.Definition.value('(/GPReplica/Name)[1]', 'nvarchar(max)'),
                items.Definition.value('(/GPSyncReplica/ReplicaName)[1]', 'nvarchar(max)')) AS name,
       COUNT(*) OVER () AS row_count
FROM sde.GDB_ITEMS AS items
INNER JOIN sde.GDB_ITEMTYPES AS itemtypes ON items.Type = itemtypes.UUID
WHERE itemtypes.Name IN ('Replica', 'Sync Replica');

-- replica_log.csv (optional, needs replicas.csv)
SELECT replicaid,
       CONVERT(varchar(19), logdate, 120) AS logdate,
       sourceendgen, targetgen, COUNT(*) OVER () AS row_count
FROM sde.GDB_REPLICALOG;

-- deltas.csv (optional), step 1: this SELECT writes one line per versioned table
SELECT 'UNION ALL SELECT sde_state_id, COUNT(*) FROM ' + r.owner + '.a'
       + CAST(r.registration_id AS varchar(12)) + ' GROUP BY sde_state_id'
       + ' UNION ALL SELECT deleted_at, COUNT(*) FROM ' + r.owner + '.d'
       + CAST(r.registration_id AS varchar(12)) + ' GROUP BY deleted_at'
FROM sde.SDE_table_registry AS r
WHERE r.registration_id IN (SELECT registration_id FROM sde.SDE_mvtables_modified);

-- deltas.csv, step 2: put every line from step 1 above the last line, then run it
SELECT state_id, delta_rows, COUNT(*) OVER () AS row_count FROM (
SELECT NULL AS state_id, 0 AS delta_rows WHERE 1 = 0
) t;
""",
    "postgresql": """\
-- versions.csv
SELECT name, owner, state_id,
       to_char(LOCALTIMESTAMP, 'YYYY-MM-DD HH24:MI:SS') AS exported_at,
       COUNT(*) OVER () AS row_count
FROM sde.sde_versions;

-- states.csv
SELECT state_id, lineage_name,
       to_char(creation_time, 'YYYY-MM-DD HH24:MI:SS') AS creation_time,
       COUNT(*) OVER () AS row_count
FROM sde.sde_states;

-- lineages.csv
SELECT lineage_name, lineage_id, COUNT(*) OVER () AS row_count
FROM sde.sde_state_lineages;

-- replicas.csv (optional)
SELECT items.objectid AS objectid, itemtypes.name AS item_type,
       COALESCE((xpath('/GPReplica/ID/text()', items.definition))[1]::text,
                (xpath('/GPSyncReplica/ID/text()', items.definition))[1]::text) AS id,
       COALESCE((xpath('/GPReplica/Name/text()', items.definition))[1]::text,
                (xpath('/GPSyncReplica/ReplicaName/text()', items.definition))[1]::text) AS name,
       COUNT(*) OVER () AS row_count
FROM sde.gdb_items AS items
INNER JOIN sde.gdb_itemtypes AS itemtypes ON items.type = itemtypes.uuid
WHERE itemtypes.name IN ('Replica', 'Sync Replica');

-- replica_log.csv (optional, needs replicas.csv)
SELECT replicaid,
       to_char(logdate, 'YYYY-MM-DD HH24:MI:SS') AS logdate,
       sourceendgen, targetgen, COUNT(*) OVER () AS row_count
FROM sde.gdb_replicalog;

-- deltas.csv (optional), step 1: this SELECT writes one line per versioned table
SELECT 'UNION ALL SELECT sde_state_id, COUNT(*) FROM ' || r.owner || '.a'
       || r.registration_id || ' GROUP BY sde_state_id'
       || ' UNION ALL SELECT deleted_at, COUNT(*) FROM ' || r.owner || '.d'
       || r.registration_id || ' GROUP BY deleted_at'
FROM sde.sde_table_registry AS r
WHERE r.registration_id IN (SELECT registration_id FROM sde.sde_mvtables_modified);

-- deltas.csv, step 2: put every line from step 1 above the last line, then run it
SELECT state_id, delta_rows, COUNT(*) OVER () AS row_count FROM (
SELECT NULL::bigint AS state_id, 0::bigint AS delta_rows WHERE 1 = 0
) t;
""",
    "oracle": """\
-- versions.csv
SELECT name, owner, state_id,
       TO_CHAR(SYSDATE, 'YYYY-MM-DD HH24:MI:SS') AS exported_at,
       COUNT(*) OVER () AS row_count
FROM sde.VERSIONS;

-- states.csv
SELECT state_id, lineage_name,
       TO_CHAR(creation_time, 'YYYY-MM-DD HH24:MI:SS') AS creation_time,
       COUNT(*) OVER () AS row_count
FROM sde.STATES;

-- lineages.csv
SELECT lineage_name, lineage_id, COUNT(*) OVER () AS row_count
FROM sde.STATE_LINEAGES;

-- replicas.csv (optional)
SELECT items.ObjectID AS objectid, itemtypes.Name AS item_type,
       COALESCE(EXTRACTVALUE(XMLType(items.Definition), '/GPReplica/ID'),
                EXTRACTVALUE(XMLType(items.Definition), '/GPSyncReplica/ID')) AS id,
       COALESCE(EXTRACTVALUE(XMLType(items.Definition), '/GPReplica/Name'),
                EXTRACTVALUE(XMLType(items.Definition), '/GPSyncReplica/ReplicaName')) AS name,
       COUNT(*) OVER () AS row_count
FROM sde.GDB_ITEMS_VW items
INNER JOIN sde.GDB_ITEMTYPES itemtypes ON items.Type = itemtypes.UUID
WHERE itemtypes.Name IN ('Replica', 'Sync Replica');

-- replica_log.csv (optional, needs replicas.csv)
SELECT replicaid,
       TO_CHAR(logdate, 'YYYY-MM-DD HH24:MI:SS') AS logdate,
       sourceendgen, targetgen, COUNT(*) OVER () AS row_count
FROM sde.GDB_REPLICALOG;

-- deltas.csv (optional), step 1: this SELECT writes one line per versioned table
SELECT 'UNION ALL SELECT sde_state_id, COUNT(*) FROM ' || r.owner || '.a'
       || r.registration_id || ' GROUP BY sde_state_id'
       || ' UNION ALL SELECT deleted_at, COUNT(*) FROM ' || r.owner || '.d'
       || r.registration_id || ' GROUP BY deleted_at'
FROM sde.TABLE_REGISTRY r
WHERE r.registration_id IN (SELECT registration_id FROM sde.MVTABLES_MODIFIED);

-- deltas.csv, step 2: put every line from step 1 above the last line, then run it
SELECT state_id, delta_rows, COUNT(*) OVER () AS row_count FROM (
SELECT NULL AS state_id, 0 AS delta_rows FROM dual WHERE 1 = 0
) t;
""",
}


class InputError(Exception):
    """An export the tool cannot reason over. Exit 2, never a finding."""


# --------------------------------------------------------------- decision core

# A date, then optionally the time, a fraction and a zone. Nothing else.
TIME_RE = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2})"
                     r"(?:[ T]([0-9]{2}:[0-9]{2}:[0-9]{2})(?:\.[0-9]+)?"
                     r"(?:Z|[+-][0-9]{2}(?::?[0-9]{2})?)?)?\Z")


def parse_time(text):
    """A timestamp from an export, or None for a blank cell.

    The shipped SQL formats every date as YYYY-MM-DD HH:MM:SS. A T separator,
    fractional seconds and a bare date are accepted too, because that is what
    a JSON export or a spreadsheet round trip produces. A Z or +hh[:mm] zone
    is ignored: every timestamp comes from the one database clock. Any other
    suffix raises ValueError. "03:00:00 PM" read as 03:00 moves a clock 12
    hours and can turn a blocker into a clean exit 0.
    """
    t = str(text).strip()
    if not t:
        return None
    m = TIME_RE.match(t)
    if not m:
        raise ValueError("not a YYYY-MM-DD HH:MM:SS timestamp")
    if m.group(2) is None:
        return datetime.datetime.strptime(m.group(1), "%Y-%m-%d")
    return datetime.datetime.strptime("%s %s" % m.groups(),
                                      "%Y-%m-%d %H:%M:%S")


def to_int(value):
    """An integer cell. 17, "17" and "17.0" are 17; anything else raises.

    Only ValueError escapes. int(float("inf")) raises OverflowError, which no
    caller catches, and an uncaught error exits 1: the code for a blocker.
    """
    text = str(value).strip()
    try:
        return int(text)
    except ValueError:
        number = float(text)
        if not math.isfinite(number) or number != int(number):
            raise ValueError("not a whole number")
        return int(number)


def fold(kind, header):
    """A header's names, lowercased. Two that fold to one name are refused.

    A dict or a CSV row keeps only one of them, the last. Which one that is
    depends on column order, so the verdict would too.
    """
    names = [str(k).strip().lower() for k in header if k is not None]
    twice = set(n for n in names if names.count(n) > 1)
    if twice:
        raise InputError("%s export has two columns named %s. Keep one."
                         % (kind, ", ".join(sorted(twice & KNOWN_COLUMNS))
                            or "alike (not shown)"))
    return names


def check_columns(kind, header, counted=False):
    """Raise unless a header holds every column this export needs.

    counted also requires row_count, and exported_at in the versions
    export. An export file must carry them; rows handed to build() by
    another script need not.
    """
    header = set(fold(kind, header))
    needed = REQUIRED[kind] + ((ROW_COUNT,) if counted else ()) + (
        (EXPORTED_AT,) if counted and kind == "versions" else ())
    absent = [c for c in needed if c not in header]
    if absent:
        # Echo only column names the tool knows. Anything else may be the
        # first line of a credential file passed by mistake.
        known = sorted(header & KNOWN_COLUMNS)
        raise InputError("%s export has no column %s. It has %s and %d "
                         "other column(s), not shown. --sql prints the "
                         "SELECT that produces it."
                         % (kind, ", ".join(absent),
                            ", ".join(known) or "no known column",
                            len(header) - len(known)))


def normalise(rows, kind):
    """Lowercase every header and check the required columns are present.

    When the rows carry row_count, every row must hold the same count and
    it must equal the rows read. A lost lineage row can move a holder's
    age under the limit, and a lost state row can drop an orphaned
    lineage: both turn a blocker into a clean exit 0.
    """
    out = [dict(zip(fold(kind, row), [v for k, v in row.items()
                                      if k is not None])) for row in rows]
    if out:
        check_columns(kind, out[0])
        if ROW_COUNT in out[0]:
            # One set of raw cells, then one conversion: a per-row to_int
            # over millions of lineage rows costs seconds.
            counts = set(str(row.get(ROW_COUNT)).strip() for row in out)
            if len(counts) != 1 or cell(out[0], ROW_COUNT, kind, 0,
                                        to_int) != len(out):
                raise InputError(
                    "%s export holds %d row(s), and its row_count column "
                    "does not say %d on every row. Rows were lost or added "
                    "after the SELECT ran, by a row limit, a spreadsheet or "
                    "a paged copy. Export it again, whole."
                    % (kind, len(out), len(out)))
    return out


def text_of(row, column):
    """A text cell, stripped, with any unprintable character escaped.

    Names reach stdout, the --report file and error messages. A newline
    in a version name could forge a clean VERDICT line in the report, and
    an ANSI escape could hide the real one in a terminal.
    """
    text = str(row.get(column) or "").strip()
    return text if text.isprintable() else (
        text.encode("unicode_escape").decode("ascii"))


def cell(row, column, kind, index, convert):
    """One converted cell, or an InputError naming the export and the row.

    The message never holds the cell's value, nor the ValueError's text,
    which quotes it: a wrong file passed as an export may hold a secret.
    """
    try:
        return convert(row.get(column) if row.get(column) is not None else "")
    except ValueError:
        raise InputError("%s export row %d, column %s: the value is not %s"
                         % (kind, index + 1, column,
                            "a whole number" if convert is to_int
                            else "a YYYY-MM-DD HH:MM:SS time"))


def build(versions, states, lineages, replicas=None, replica_log=None,
          deltas=None):
    """Turn raw export rows into the model analyse() reasons over.

    Every inconsistency between the exports raises InputError rather than
    being guessed around. A version pointing at a state the states export
    does not hold means the exports were taken at different moments, and a
    floor computed from them would be a floor of neither.
    """
    versions = normalise(versions, "versions")
    states = normalise(states, "states")
    lineages = normalise(lineages, "lineages")

    model = {"versions": [], "states": {}, "lineages": {}, "replicas": None,
             "sync_replicas": 0, "log": None, "deltas": None,
             "exported_at": None}
    for i, row in enumerate(states):
        sid = cell(row, "state_id", "states", i, to_int)
        if sid in model["states"]:
            # Keeping either row would make the verdict depend on row order.
            raise InputError("states export row %d repeats state %d. Take "
                             "every export in one sitting." % (i + 1, sid))
        model["states"][sid] = {
            "lineage": cell(row, "lineage_name", "states", i, to_int),
            "created": cell(row, "creation_time", "states", i, parse_time)}
        if sid < 0:
            # Esri never writes one. A walk down a lineage would index past
            # its first id and wrap to its last: a false CLEAN. A negative
            # lineage is refused below, where the lineages export is read.
            raise InputError("states export row %d holds a negative state "
                             "id" % (i + 1))
    for i, row in enumerate(lineages):
        name = cell(row, "lineage_name", "lineages", i, to_int)
        lid = cell(row, "lineage_id", "lineages", i, to_int)
        if name < 0 or lid < 0:
            raise InputError("lineages export row %d holds a negative id"
                             % (i + 1))
        if lid in model["lineages"].get(name, ()):
            # The table cannot hold a pair twice. A copy that repeats one
            # row can lose another and keep the row_count: a false CLEAN.
            raise InputError("lineages export row %d repeats lineage %d, "
                             "state %d. Rows were copied twice, and others "
                             "may be lost. Export it again, whole."
                             % (i + 1, name, lid))
        model["lineages"].setdefault(name, set()).add(lid)
    seen = set()
    for i, row in enumerate(versions):
        v = {"name": text_of(row, "name"), "owner": text_of(row, "owner"),
             "state": cell(row, "state_id", "versions", i, to_int)}
        if (v["owner"], v["name"]) in seen:
            # The same false CLEAN: the repeat can stand in for a lost
            # holder's row.
            raise InputError("versions export row %d repeats version %s.%s. "
                             "Rows were copied twice, and others may be "
                             "lost. Export it again, whole."
                             % (i + 1, v["owner"], v["name"]))
        seen.add((v["owner"], v["name"]))
        if v["state"] not in model["states"]:
            raise InputError(
                "version %s.%s points at state %d, which the states export "
                "does not hold. Take every export in one sitting."
                % (v["owner"], v["name"], v["state"]))
        model["versions"].append(v)

    defaults = [v for v in model["versions"] if v["name"].upper() == "DEFAULT"]
    if len(defaults) != 1:
        raise InputError("the versions export holds %d DEFAULT row(s), not 1. "
                         "Is it the version table?" % len(defaults))
    if EXPORTED_AT in versions[0]:
        stamps = set(str(row.get(EXPORTED_AT)).strip() for row in versions)
        model["exported_at"] = cell(versions[0], EXPORTED_AT, "versions", 0,
                                    parse_time)
        if len(stamps) != 1 or model["exported_at"] is None:
            raise InputError("the versions export's exported_at is blank or "
                             "differs between rows. It is one clock reading "
                             "per export. Export it again.")
    if 0 not in model["states"]:
        raise InputError("the states export has no state 0, so it is not the "
                         "whole state table")
    for sid, state in model["states"].items():
        if state["lineage"] not in model["lineages"]:
            raise InputError("state %d is on lineage %d, which the lineages "
                             "export does not hold" % (sid, state["lineage"]))
        # A state is on its own lineage, and every lineage starts at state
        # 0. Esri writes both rows; adding them here means a walk down any
        # lineage always ends on DEFAULT's, with no fallback to get wrong.
        model["lineages"][state["lineage"]].update((sid, 0))
    for name in model["lineages"]:
        model["lineages"][name] = sorted(model["lineages"][name])

    if replicas is not None:
        model["replicas"] = {}
        objectids = set()
        for i, row in enumerate(normalise(replicas, "replicas")):
            item_type = str(row.get("item_type") or "").strip().lower()
            objectid = cell(row, "objectid", "replicas", i, to_int)
            if objectid in objectids:
                # GDB_ITEMS cannot hold an objectid twice. A copy that
                # repeats one row can lose a live replica's and keep the
                # row_count, and that replica's versions would be called
                # detached: TA 000011719's delete step, on a live replica.
                raise InputError("replicas export row %d repeats objectid %d. "
                                 "Rows were copied twice, and others may be "
                                 "lost. Export it again, whole."
                                 % (i + 1, objectid))
            objectids.add(objectid)
            if item_type == "sync replica":
                # A feature service replica: distributed collaboration or an
                # offline map. Esri does not document which number its SYNC_
                # versions carry, and its /GPSyncReplica/ID is often -1, so
                # its id is never matched. Only its presence is counted.
                model["sync_replicas"] += 1
                continue
            if item_type != "replica":
                raise InputError("replicas export row %d: item_type is not "
                                 "Replica or Sync Replica" % (i + 1))
            rid = cell(row, "id", "replicas", i, to_int)
            if rid in model["replicas"]:
                raise InputError("replicas export row %d repeats replica id "
                                 "%d. Export it again, whole." % (i + 1, rid))
            model["replicas"][rid] = {"objectid": objectid,
                                      "name": text_of(row, "name")}
    if replica_log is not None:
        model["log"] = {}
        for i, row in enumerate(normalise(replica_log, "replica_log")):
            model["log"].setdefault(
                cell(row, "replicaid", "replica_log", i, to_int), []).append((
                    cell(row, "logdate", "replica_log", i, parse_time),
                    max(cell(row, "sourceendgen", "replica_log", i, to_int),
                        cell(row, "targetgen", "replica_log", i, to_int))))
    if deltas is not None:
        model["deltas"] = {}
        for i, row in enumerate(normalise(deltas, "deltas")):
            sid = cell(row, "state_id", "deltas", i, to_int)
            model["deltas"][sid] = (model["deltas"].get(sid, 0)
                                    + cell(row, "delta_rows", "deltas", i,
                                           to_int))
    return model


def ancestors(model, state_id):
    """Every state on the path from state 0 to this one, itself included.

    Read from the state lineage table, which Esri documents as the index every
    version query traverses: a lineage holds its states in increasing id
    order, so the ancestors of a state are its lineage's ids up to its own.
    """
    lineage = model["lineages"][model["states"][state_id]["lineage"]]
    return set(lineage[:bisect.bisect_right(lineage, state_id)])


def fork_of(model, state_id, trunk):
    """Where a state's lineage meets the trunk, and the states before that.

    Walks down from the state until it reaches a state on the trunk, so the
    cost is the length of the branch, not of the whole lineage. A version
    forked from a long DEFAULT lineage costs a few steps, not a full scan.
    """
    lineage = model["lineages"][model["states"][state_id]["lineage"]]
    i = bisect.bisect_right(lineage, state_id) - 1
    private = set()
    while lineage[i] not in trunk:
        private.add(lineage[i])
        i -= 1
    return lineage[i], private


def weight(model, state_ids):
    """Delta rows held in these states, or None when no --deltas was given."""
    if model["deltas"] is None:
        return None
    return sum(model["deltas"].get(s, 0) for s in state_ids)


def held_since(model, fork, trunk, as_of):
    """How long DEFAULT has been past a holder's fork, or None when unknown.

    A version holds nothing while DEFAULT still sits at its fork. It starts
    to hold the floor when DEFAULT moves on, which is when the first state
    above the fork in DEFAULT's ancestry was created. A version made today
    from a DEFAULT idle for 80 days has held the floor since today.
    """
    i = bisect.bisect_right(trunk, fork)
    if i == len(trunk):
        return datetime.timedelta(0)
    return age_of(model, trunk[i], as_of)


def age_of(model, state_id, as_of):
    """How long before as_of a state was created, or None when unknown."""
    state = model["states"].get(state_id)
    if state is None or state["created"] is None:
        return None
    return as_of - state["created"]


def latest_time(model):
    """The newest timestamp in the exports.

    No age may be measured from before it. It is the as-of time only for
    rows with no exported_at, handed to build() by another script: on an
    idle geodatabase it makes every holder look young.
    """
    times = [s["created"] for s in model["states"].values() if s["created"]]
    for rows in (model["log"] or {}).values():
        times.extend(t for t, _ in rows if t)
    return max(times) if times else datetime.datetime(1970, 1, 1)


def subject_of(version):
    """Group a version under the replica it belongs to, or under itself."""
    match = SYNC_RE.match(version["name"])
    if match:
        return (REPLICA, int(match.group(2))), int(match.group(3))
    return (VERSION, "%s.%s" % (version["owner"], version["name"])), None


def analyse(model, max_age_days=DEFAULT_MAX_AGE_DAYS, as_of=None):
    """The floor, what holds it, and which holders are blockers."""
    newest = latest_time(model)
    # The export's own clock, not the local one: the same exports give the
    # same report on any day.
    as_of = as_of or model["exported_at"] or newest
    if as_of < newest:
        # Every age would be measured from before the state it ages, and a
        # negative age is never over the limit: a false CLEAN.
        raise InputError("the as-of time %s is earlier than the newest "
                         "timestamp in the exports, %s" % (as_of, newest))
    limit = datetime.timedelta(days=max_age_days)
    default = [v for v in model["versions"]
               if v["name"].upper() == "DEFAULT"][0]
    anc_default = ancestors(model, default["state"])
    trunk = sorted(anc_default)

    covered = set(anc_default)
    subjects = {}
    for v in model["versions"]:
        if v is default:
            continue
        fork, private = fork_of(model, v["state"], anc_default)
        covered |= private
        key, generation = subject_of(v)
        s = subjects.setdefault(key, {"versions": [], "fork": None,
                                      "private": set(), "generations": []})
        s["versions"].append(v)
        s["fork"] = fork if s["fork"] is None else min(s["fork"], fork)
        s["private"] |= private
        if generation is not None:
            s["generations"].append(generation)

    forks = [s["fork"] for s in subjects.values()]
    floor = min(forks) if forks else default["state"]

    findings = []
    for key, s in subjects.items():
        age = held_since(model, s["fork"], trunk, as_of)
        old = age is None or age > limit
        holds = s["fork"] < default["state"]
        kind = None
        if key[0] == REPLICA and model["replicas"] is not None:
            if key[1] in model["replicas"]:
                kind = STALLED if holds and old else None
            elif model["sync_replicas"]:
                # It may belong to a live feature service replica. Calling
                # it detached would send the reader to delete it.
                kind = UNRESOLVED if holds and old else None
            else:
                kind = DETACHED
        elif key[0] == REPLICA:
            kind = UNRESOLVED if holds and old else None
        elif holds and old:
            kind = ANCIENT
        if kind is None and not holds:
            continue
        findings.append({
            "kind": kind or key[0], "blocker": kind is not None,
            "key": key, "versions": s["versions"], "fork": s["fork"],
            "age": age, "holds_floor": holds and s["fork"] == floor,
            "generations": s["generations"],
            "weight": weight(model, s["private"])})

    orphans = {}
    for sid in set(model["states"]) - covered:
        orphans.setdefault(model["states"][sid]["lineage"], []).append(sid)
    for lineage, sids in orphans.items():
        tip = max(sids)
        created = [model["states"][x]["created"] for x in sids]
        age = None if None in created else as_of - max(created)
        findings.append({
            "kind": ORPHANED, "blocker": age is None or age > limit,
            "key": (ORPHANED, lineage), "states": sorted(sids),
            "fork": fork_of(model, tip, anc_default)[0], "age": age,
            "holds_floor": False, "weight": weight(model, sids)})

    findings.sort(key=lambda f: (not f["blocker"], f["fork"], str(f["key"])))
    return {"as_of": as_of, "max_age_days": max_age_days,
            "default": default, "floor": floor,
            "floor_age": age_of(model, floor, as_of),
            "findings": findings,
            "unfoldable": weight(model, set(model["states"]) - set(
                x for x in anc_default if x <= floor)),
            "counts": (len(model["versions"]), len(model["states"]),
                       None if model["replicas"] is None
                       else len(model["replicas"]))}


def gate(report):
    """Exit 1 when anything is a blocker, else 0."""
    return 1 if any(f["blocker"] for f in report["findings"]) else 0


def days(age):
    """A readable age."""
    return "age unknown" if age is None else "%d day(s) old" % age.days


def held(age):
    """How long a holder has held its fork."""
    return ("held for an unknown time" if age is None
            else "held for %d day(s)" % age.days)


def number(n):
    """Thousands separators, or a note that no delta counts were supplied."""
    return "not supplied" if n is None else format(n, ",d")


def replica_evidence(model, rid, generations):
    """What the version names and the replica log say about a replica."""
    lines = ["highest sync generation in its version names: %d"
             % max(generations)]
    logged = 0
    replica = (model["replicas"] or {}).get(rid)
    if model["log"] is not None and replica is not None:
        rows = model["log"].get(replica["objectid"], [])
        if rows:
            dates = [t for t, _ in rows if t]
            logged = max(g for _, g in rows)
            lines.append("replica log: %d event(s), last %s, highest "
                         "generation %d"
                         % (len(rows), max(dates) if dates else "undated",
                            logged))
        else:
            lines.append("replica log: no event for this replica")
    if max(generations) == 0 and logged == 0:
        lines.append("it has never advanced past sync generation 0")
    return lines


def describe(model, report):
    """The report as lines of text: stdout and the --report file."""
    nv, ns, nr = report["counts"]
    d = report["default"]
    if nr is None:
        replicas = "replicas not supplied"
    elif model["sync_replicas"]:
        replicas = "%d replica(s) and %d sync replica(s)" % (
            nr, model["sync_replicas"])
    else:
        replicas = "%d replica(s)" % nr
    out = ["compressfloor: %d version(s), %d state(s), %s, as of %s"
           % (nv, ns, replicas, report["as_of"]),
           "DEFAULT (%s) is at state %d" % (d["owner"], d["state"]),
           "compress floor: state %d, %s"
           % (report["floor"], days(report["floor_age"]))]
    if report["floor"] == d["state"]:
        out.append("  no version forks from DEFAULT below its own state, so "
                   "nothing in the version table holds the floor")
    else:
        out.append("  compress can fold edits into the base tables no "
                   "further than state %d" % report["floor"])
    out.append("  delta rows compress cannot fold: %s"
               % number(report["unfoldable"]))

    for blocker, title in ((True, "BLOCKERS"),
                           (False, "holding state but under the %d day limit"
                            % report["max_age_days"])):
        group = [f for f in report["findings"] if f["blocker"] == blocker]
        if not group:
            continue
        out.append("")
        out.append("%s (%d)" % (title, len(group)))
        for f in group:
            out.extend(describe_finding(model, f))

    blockers = sum(1 for f in report["findings"] if f["blocker"])
    out.append("")
    if blockers:
        out.append("VERDICT: %d blocker(s). Compress will keep exiting 0 and "
                   "will not fold past state %d." % (blockers, report["floor"]))
    else:
        out.append("VERDICT: nothing in the exports holds the floor past the "
                   "%d day limit." % report["max_age_days"])
    return out


def describe_finding(model, f):
    """The lines for one finding."""
    kind, key = f["kind"], f["key"]
    if kind == ORPHANED:
        head = ("lineage %d: %d state(s) no version references, %d to %d"
                % (key[1], len(f["states"]), f["states"][0], f["states"][-1]))
        body = ["newest state %s; forks from DEFAULT's lineage at state %d"
                % (days(f["age"]), f["fork"]),
                "delta rows in these states: %s" % number(f["weight"]),
                "a compress removes states no version needs: if none has run "
                "since a version was deleted, run one and export again"]
        return ["  %-26s %s" % (kind, head)] + ["      " + b for b in body]
    if key[0] == REPLICA:
        name = ((model["replicas"] or {}).get(key[1]) or {}).get("name")
        head = "replica %d%s" % (key[1], " '%s'" % name if name else "")
    else:
        head = "version %s" % key[1]
    body = ["versions: %s" % ", ".join(
        "%s (state %d)" % (v["name"], v["state"]) for v in f["versions"])]
    body.append("forks from DEFAULT at state %d, %s%s"
                % (f["fork"], held(f["age"]),
                   "; this holds the floor" if f["holds_floor"] else ""))
    if key[0] == REPLICA:
        body.extend(replica_evidence(model, key[1], f["generations"]))
    body.append("delta rows on its own branch: %s" % number(f["weight"]))
    if kind == DETACHED:
        body.append("no replica has this id: Esri TA 000011719 calls this a "
                    "detached replica system version")
    elif kind == UNRESOLVED and model["replicas"] is None:
        body.append("supply --replicas to tell a detached replica from a "
                    "stalled one")
    elif kind == UNRESOLVED:
        body.append("no geodatabase replica has this id, and %d sync "
                    "replica(s) exist, whose version ids Esri does not "
                    "document. It may be one of them: do not delete it on "
                    "TA 000011719's word" % model["sync_replicas"])
    return ["  %-26s %s" % (kind, head)] + ["      " + b for b in body]


# ------------------------------------------------------------------ self-test

def self_test():
    """Assertions over the decision core. No database, no network."""
    passed = [0]
    failed = []

    def check(cond, label):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label)
        else:
            failed.append(label)
            print("FAIL  %s" % label)

    def outcome(fn):
        """fn's result, or the exception it raised, so a crash is a FAIL."""
        try:
            return fn()
        except Exception as exc:
            return exc

    def raises(fn, label, exc_type=InputError):
        try:
            fn()
        except exc_type:
            check(True, label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

    print("compressfloor self-test: no database, no network, no credentials")
    print("-" * 68)

    fx = fixture()
    model = build(**fx)
    report = analyse(model)
    by = dict((f["key"], f) for f in report["findings"])

    # ---- the disaster, in canned rows
    check(report["floor"] == 17,
          "the floor is state 17, where the oldest holder forks from DEFAULT")
    check(report["default"]["state"] == 9000, "DEFAULT is read at state 9000")
    check(report["as_of"] == datetime.datetime(2026, 9, 20, 2, 5, 0),
          "the as-of time is the database clock the versions export "
          "recorded, not the local clock")
    check(by[(REPLICA, 56)]["kind"] == STALLED,
          "a registered replica forking at an old state is STALLED"
          "  <-- pinned defect")
    check(by[(REPLICA, 56)]["holds_floor"],
          "and it is named as holding the floor")
    check(set(k for k, f in by.items() if f["holds_floor"])
          == set([(REPLICA, 56), (REPLICA, 57)]),
          "only the holders at the floor state are named as holding it, not "
          "the ones above it  <-- pinned defect")
    check(len(by[(REPLICA, 56)]["versions"]) == 2,
          "its SEND and RECEIVE system versions are one replica, not two "
          "findings")
    check(by[(REPLICA, 57)]["kind"] == STALLED
          and by[(REPLICA, 57)]["fork"] == 17,
          "a replica on its own branch forks where its branch leaves DEFAULT")
    check(by[(REPLICA, 48)]["kind"] == DETACHED,
          "a SYNC_ version whose replica id is not registered is DETACHED"
          "  <-- pinned defect")
    check(by[(VERSION, "EDITOR1.OLD_DESIGN")]["kind"] == ANCIENT
          and by[(VERSION, "EDITOR1.OLD_DESIGN")]["age"].days == 199,
          "an ordinary version DEFAULT moved past 199 days ago is ANCIENT")
    check(by[(ORPHANED, 300)]["blocker"],
          "a lineage no version references, untouched for months, is a "
          "blocker  <-- pinned defect")
    check(by[(ORPHANED, 300)]["weight"] == 1760000,
          "and it carries the 1,760,000 delta rows supplied for it")
    check(by[(ORPHANED, 300)]["fork"] == 208,
          "and it is reported forking from DEFAULT's lineage at state 208")
    check(not by[(ORPHANED, 9001)]["blocker"],
          "an unreferenced state minutes old is listed but not a blocker")
    check(not by[(REPLICA, 70)]["blocker"]
          and by[(REPLICA, 70)]["kind"] == REPLICA,
          "a replica that synced last week is a holder, not a blocker")
    check(not by[(VERSION, "EDITOR2.WO_NEW")]["blocker"],
          "a ten day old edit version is a holder, not a blocker")
    check(by[(REPLICA, 56)]["age"].days == 231,
          "a holder is aged from when DEFAULT moved past its fork, not from "
          "its fork state's creation")
    check((VERSION, "EDITOR3.QA") not in by,
          "a version at DEFAULT's own state holds nothing and is not listed")
    check(sum(1 for f in report["findings"] if f["blocker"]) == 5,
          "five blockers in the disaster fixture, no more and no fewer")
    check(gate(report) == 1, "blockers exit 1")
    check(report["unfoldable"] == 1809012,
          "the delta rows compress cannot fold sum every state above the "
          "floor")
    check(by[(VERSION, "EDITOR1.OLD_DESIGN")]["weight"] == 5000,
          "a version's own-branch weight counts only states DEFAULT lacks")
    forked = analyse(build(**dict(fx, deltas=fx["deltas"] + [
        {"STATE_ID": "50", "DELTA_ROWS": "9000"}])))
    check([f["weight"] for f in forked["findings"]
           if f["key"] == (VERSION, "EDITOR1.OLD_DESIGN")] == [5000],
          "DEFAULT's delta rows at a version's fork state are not counted in "
          "its own-branch weight  <-- pinned defect")

    # The shipped SELECTs have no ORDER BY, so DEFAULT can be any row.
    late = build(**tree([("OLD", "ED", 1)],
                        [(0, 0, 0, "2026-01-01 00:00:00"),
                         (1, 0, 0, "2026-01-01 00:00:00"),
                         (9, 0, 1, "2026-02-01 00:00:00")],
                        [("DEFAULT", "SDE", 9)]))
    lr = analyse(late, 30, datetime.datetime(2026, 9, 20))
    check(lr["default"]["name"] == "DEFAULT"
          and [(f["kind"], f["key"]) for f in lr["findings"]]
          == [(ANCIENT, (VERSION, "ED.OLD"))] and gate(lr) == 1,
          "DEFAULT is found in a later row of the versions export, not taken "
          "from the first row  <-- pinned defect")

    # ---- the obvious check, and why it is wrong
    def naive_orphans(fx):
        """StateLineageCheck-style: states no version's state_id names."""
        named = set(int(v["STATE_ID"]) for v in fx["versions"])
        return set(int(s["STATE_ID"]) for s in fx["states"]) - named

    real = set()
    for f in report["findings"]:
        real |= set(f.get("states", []))
    check(10 in naive_orphans(fx) and 10 not in real,
          "the naive orphan count calls DEFAULT's own ancestor state 10 an "
          "orphan  <-- pinned defect")
    check(real == set([300, 301, 9001]),
          "the lineage walk finds exactly the three unreferenced states")
    healthy = tree([("DEFAULT", "SDE", 5)],
                   [(0, 0, 0, "2026-09-01 00:00:00"),
                    (5, 0, 0, "2026-09-01 00:00:00")], [])
    check(naive_orphans(healthy) == set([0])
          and analyse(build(**healthy))["findings"] == [],
          "on a healthy tree the naive count finds an orphan and the walk "
          "finds none")

    # ---- the age limit at its boundary
    # OLD forks at state 1. DEFAULT moved past it when state 2 was made.
    def moved_past(created):
        return tree([("DEFAULT", "SDE", 5)],
                    [(0, 0, 0, "2026-01-01 00:00:00"),
                     (1, 0, 0, "2026-01-01 00:00:00"),
                     (2, 0, 1, created),
                     (5, 0, 2, "2026-09-01 00:00:00"),
                     (6, 6, 1, "2026-09-01 00:00:00")],
                    [("OLD", "ED", 6)])

    def one_old_version(created, max_age=30):
        return analyse(build(**moved_past(created)), max_age,
                       datetime.datetime(2026, 9, 1))

    exact = one_old_version("2026-08-02 00:00:00")
    check(exact["findings"][0]["kind"] == VERSION
          and not exact["findings"][0]["blocker"],
          "a fork exactly 30 days old is not a blocker, the limit is "
          "inclusive")
    past = one_old_version("2026-08-01 23:59:59")
    check(past["findings"][0]["kind"] == ANCIENT,
          "one second past 30 days is a blocker")
    check(one_old_version("2026-08-31 00:00:00", 0)["findings"][0]["kind"]
          == ANCIENT, "an age limit of 0 days makes a one day old holder a "
          "blocker")
    check(one_old_version("2026-09-01 00:00:00", 0)["findings"][0]["kind"]
          == VERSION, "at a 0 day limit, a holder DEFAULT moved past at the "
          "as-of time itself is not a blocker")
    # DEFAULT's own state, idle since March, is the first state above OLD's
    # fork: the fork has been held since DEFAULT's state was made.
    quiet = analyse(build(**tree(
        [("DEFAULT", "SDE", 5)],
        [(0, 0, 0, "2026-01-01 00:00:00"), (1, 0, 0, "2026-01-01 00:00:00"),
         (5, 0, 1, "2026-03-01 00:00:00"), (6, 6, 1, "2026-09-01 00:00:00")],
        [("OLD", "ED", 6)])), 30)
    check([(f["kind"], f["age"].days) for f in quiet["findings"]]
          == [(ANCIENT, 184)] and gate(quiet) == 1,
          "a fork directly below DEFAULT's own old state is aged from that "
          "state, not held for 0 days  <-- pinned defect")
    raises(lambda: analyse(build(**moved_past("2026-08-31 00:00:00")), 0,
                           datetime.datetime(2026, 8, 30)),
           "an as-of time before the newest export timestamp is refused, "
           "never a negative age  <-- pinned defect")
    unknown = one_old_version("")
    check(unknown["findings"][0]["blocker"]
          and unknown["findings"][0]["age"] is None,
          "a fork state with no creation time is a blocker, never assumed "
          "young  <-- pinned defect")
    check("held for an unknown time" in "\n".join(describe(build(
        **moved_past("")), unknown)),
          "and the report says the age is unknown")
    idle80 = analyse(build(**tree(
        [("DEFAULT", "SDE", 7)],
        [(0, 0, 0, "2025-01-01 00:00:00"), (5, 0, 0, "2026-07-01 00:00:00"),
         (6, 6, 5, "2026-09-19 09:00:00"), (7, 0, 5, "2026-09-19 10:00:00")],
        [("WO_NEW", "ED", 6)])))
    check(idle80["findings"][0]["kind"] == VERSION and gate(idle80) == 0,
          "a version made today from a DEFAULT idle for 80 days is not "
          "80 days old  <-- pinned defect")
    at_zero = tree([("DEFAULT", "SDE", 9000)],
                   [(0, 0, 0, "2026-01-01 00:00:00"),
                    (7, 7, 0, "2026-09-19 00:00:00"),
                    (9000, 0, 0, "2026-09-20 00:00:00")], [("OLD", "ED", 7)])
    zero = analyse(build(**at_zero))
    check(zero["floor"] == 0 and zero["findings"][0]["fork"] == 0,
          "a version made from state 0 forks at state 0 and holds the "
          "floor there  <-- pinned defect")
    bare_lineages = dict(at_zero, lineages=[
        r for r in at_zero["lineages"] if r["LINEAGE_ID"] != "0"])
    check(outcome(lambda: describe(build(**bare_lineages),
                                   analyse(build(**bare_lineages))))
          == describe(build(**at_zero), zero),
          "a lineage export without its (lineage, 0) rows gives the same "
          "report  <-- pinned defect")

    # ---- replicas without --replicas
    bare = dict(fx)
    bare["replicas"] = None
    bare["replica_log"] = None
    rb = analyse(build(**bare))
    kinds = dict((f["key"], f["kind"]) for f in rb["findings"])
    check(kinds[(REPLICA, 48)] == UNRESOLVED and kinds[(REPLICA, 56)]
          == UNRESOLVED,
          "without --replicas an old SYNC_ version is UNRESOLVED, still a "
          "blocker")
    check(kinds[(REPLICA, 70)] == REPLICA,
          "and a young one is still only a holder")
    check("replicas not supplied" in describe(build(**bare), rb)[0],
          "the header says the replicas export was not supplied")
    check(any("supply --replicas" in line
              for line in describe(build(**bare), rb)),
          "the finding says which export would resolve it")
    bm = build(**bare)
    check(all(("supply --replicas" in " ".join(describe_finding(bm, f)))
              == (f["kind"] == UNRESOLVED) for f in rb["findings"])
          and "do not delete it" not in "\n".join(describe(bm, rb)),
          "only the unresolved replica versions are told to supply "
          "--replicas, and no finding is told not to delete  <-- pinned "
          "defect")

    # ---- a detached replica is a blocker even when young
    young = tree([("DEFAULT", "SDE", 5)],
                 [(0, 0, 0, "2026-09-01 00:00:00"),
                  (5, 0, 0, "2026-09-01 00:00:00")],
                 [("SYNC_SEND_4_1", "SDE", 5)], replicas=[])
    ry = analyse(build(**young))
    check(ry["findings"][0]["kind"] == DETACHED and gate(ry) == 1,
          "a detached replica version at DEFAULT's own state is still a "
          "blocker  <-- pinned defect")
    text = "\n".join(describe(build(**young), ry))
    check("nothing in the version table holds the floor" in text
          and "this holds the floor" not in text
          and "at state 5, held for 0 day(s)" in text,
          "and it is not said to hold a floor the header says nothing holds"
          "  <-- pinned defect")

    # ---- rules the disaster fixture does not reach
    def small(default, states, extra, replicas=None):
        return analyse(build(**tree([("DEFAULT", "SDE", default)], states,
                                    extra, replicas)),
                       30, datetime.datetime(2026, 9, 1))

    three = [(0, 0, 0, "2026-01-01 00:00:00"), (5, 0, 0, "2026-01-01 00:00:00"),
             (6, 0, 5, "2026-01-02 00:00:00"), (9, 0, 6, "2026-09-01 00:00:00")]
    five = [{"OBJECTID": "1", "ITEM_TYPE": "Replica", "ID": "5",
             "NAME": "x"}]
    mixed = small(9, three, [("SYNC_SEND_5_3", "SDE", 9),
                             ("SYNC_RECEIVE_5_3", "SDE", 5)], five)
    check([(f["kind"], f["fork"]) for f in mixed["findings"]]
          == [(STALLED, 5)] and gate(mixed) == 1,
          "a replica with a fresh SEND and an old RECEIVE forks at the older "
          "state and is STALLED")
    flipped = small(9, three, [("SYNC_RECEIVE_5_3", "SDE", 5),
                               ("SYNC_SEND_5_3", "SDE", 9)], five)
    check([(f["kind"], f["fork"]) for f in flipped["findings"]]
          == [(STALLED, 5)] and gate(flipped) == 1,
          "and in the other row order too  <-- pinned defect")
    spread = build(**tree([("DEFAULT", "SDE", 9)], three,
                          [("SYNC_SEND_5_3", "SDE", 5),
                           ("SYNC_RECEIVE_5_0", "SDE", 5)], five))
    spread_text = describe(spread, analyse(spread, 30,
                                           datetime.datetime(2026, 9, 1)))
    check("      highest sync generation in its version names: 3"
          in spread_text
          and not any("never advanced" in line for line in spread_text),
          "a replica whose SEND name reached generation 3 is not called "
          "stuck at 0 for its RECEIVE name  <-- pinned defect")
    check(small(9, three, [("SYNC_RECEIVE_REC_5_0", "SDE", 5)],
                five)["findings"][0]["kind"] == STALLED,
          "a SYNC_RECEIVE_REC_ version belongs to its replica too")
    check(small(9, three, [("sync_send_5_0", "SDE", 5)],
                five)["findings"][0]["kind"] == STALLED,
          "a replica system version name is matched in any case")
    tail = small(9, three, [("SYNC_SEND_5_0_OLD", "SDE", 5)], five)
    check(tail["findings"][0]["kind"] == ANCIENT
          and tail["findings"][0]["key"] == (VERSION, "SDE.SYNC_SEND_5_0_OLD"),
          "a name with text after the generation is an ordinary version")
    sync = [{"OBJECTID": "2", "ITEM_TYPE": "Sync Replica", "ID": "-1",
             "NAME": "collab"}]
    old_sync = small(9, three, [("SYNC_SEND_12_0", "SDE", 5)], sync)
    check(old_sync["findings"][0]["kind"] == UNRESOLVED
          and gate(old_sync) == 1,
          "beside a Sync Replica item, an unmatched SYNC_ version is "
          "UNRESOLVED, not DETACHED  <-- pinned defect")
    text = "\n".join(describe(build(**tree(
        [("DEFAULT", "SDE", 9)], three, [("SYNC_SEND_12_0", "SDE", 5)],
        sync)), old_sync))
    check("do not delete it" in text and "calls this a detached" not in text,
          "and it is not sent to TA 000011719's delete step  <-- pinned "
          "defect")
    check([f["kind"] for f in small(9, three, [
        ("Bob_NetFS_1404578882000", "SDE", 5)], sync)["findings"]]
          == [ANCIENT],
          "an offline map's <user>_<service>_<id> replica version is an "
          "ordinary version")
    check(small(9, three, [("SYNC_SEND_12_0", "SDE", 9)], sync)
          ["findings"] == [],
          "a collaboration replica's versions at DEFAULT's state are not "
          "listed at all")
    young_sync = small(9, [(0, 0, 0, "2026-01-01 00:00:00"),
                           (5, 0, 0, "2026-08-30 00:00:00"),
                           (6, 0, 5, "2026-08-31 00:00:00"),
                           (9, 0, 6, "2026-09-01 00:00:00")],
                       [("SYNC_SEND_12_0", "SDE", 5),
                        ("SYNC_RECEIVE_12_0", "SDE", 5)], sync)
    check(young_sync["findings"][0]["kind"] == REPLICA
          and gate(young_sync) == 0,
          "a live collaboration replica a day behind is a holder, not a "
          "blocker  <-- pinned defect")
    other = outcome(lambda: build(**tree(
        [("DEFAULT", "SDE", 0)], [(0, 0, 0, "")], [], [{
            "OBJECTID": "1", "ITEM_TYPE": "Table", "ID": "1", "NAME": "x"}])))
    check(isinstance(other, InputError)
          and "item_type is not Replica or Sync Replica" in str(other),
          "a replicas row of any other item type is refused")
    idle = [(0, 0, 0, "2026-01-01 00:00:00"), (5, 0, 0, "2026-01-01 00:00:00")]
    at_default = [("QA", "ED", 5), ("SYNC_SEND_3_1", "SDE", 5)]
    check(small(5, idle, at_default)["findings"] == []
          and small(5, idle, at_default, [{
              "OBJECTID": "1", "ITEM_TYPE": "Replica", "ID": "3",
              "NAME": "x"}])["findings"] == [],
          "versions at DEFAULT's own state hold nothing, even when DEFAULT "
          "has been idle for months")

    def orphan(*states):
        return small(5, idle + list(states), [])["findings"][0]

    # A version made from DEFAULT that edits first puts its state on
    # DEFAULT's lineage, above DEFAULT.
    above = idle + [(6, 0, 5, "2026-01-02 00:00:00")]
    lost_above = small(5, above, [])
    check([(f["kind"], f["states"], f["blocker"])
           for f in lost_above["findings"]] == [(ORPHANED, [6], True)],
          "a state above DEFAULT on DEFAULT's own lineage is not DEFAULT's "
          "ancestor: unreferenced, it is orphaned  <-- pinned defect")
    kept_above = small(5, above, [("NEW", "ED", 6)])
    check(kept_above["findings"] == [] and kept_above["floor"] == 5,
          "referenced, it holds nothing and the floor stays at DEFAULT's "
          "state")

    lost = orphan((7, 7, 5, ""))
    check(lost["kind"] == ORPHANED and lost["blocker"] and lost["age"] is None,
          "an orphaned state with no creation time is a blocker, never "
          "assumed young")
    part = outcome(lambda: orphan((7, 7, 5, ""),
                                  (8, 7, 7, "2026-08-31 00:00:00")))
    check(isinstance(part, dict) and part["blocker"] and part["age"] is None,
          "one undated state makes an orphaned lineage's age unknown, even "
          "beside a dated one  <-- pinned defect")
    check(isinstance(part, dict)
          and "run one and export again" in describe_finding(None, part)[-1],
          "an orphaned lineage says a compress must run before it is "
          "blamed on anything  <-- pinned defect")
    check(not orphan((7, 7, 5, "2026-08-02 00:00:00"))["blocker"],
          "an orphaned lineage exactly 30 days old is not a blocker")
    split = orphan((7, 7, 5, "2026-01-02 00:00:00"),
                   (8, 7, 7, "2026-08-31 00:00:00"))
    check(not split["blocker"] and split["age"].days == 1,
          "an orphaned lineage is aged by its newest state, not its oldest")

    check(analyse(build(**dict(fx, deltas=fx["deltas"] + [
        {"STATE_ID": "17", "DELTA_ROWS": "100"}])))["unfoldable"] == 1809012,
          "delta rows on the floor state itself fold, so they are not "
          "counted as unfoldable")
    later = [{"REPLICAID": "1070", "LOGDATE": "2026-09-21 00:00:00",
              "SOURCEENDGEN": "13", "TARGETGEN": "12"}]
    unstamped = dict(fx, versions=[dict((k, v) for k, v in r.items()
                                        if k != "EXPORTED_AT")
                                   for r in fx["versions"]])
    check(analyse(build(**dict(unstamped, replica_log=fx["replica_log"]
                               + later)))["as_of"]
          == datetime.datetime(2026, 9, 21),
          "rows with no export time are aged from their newest timestamp, "
          "a replica log event included")
    raises(lambda: analyse(build(**dict(fx, replica_log=fx["replica_log"]
                                        + later))),
           "a replica log event after the export time is refused: the clocks "
           "disagree")

    # ---- a geodatabase where editing stopped
    # DEFAULT moved past replica 56's fork on 1 January, and nothing was
    # edited after 20 January. The exports were taken on 27 September.
    idle_gdb = tree([("DEFAULT", "SDE", 30)],
                    [(0, 0, 0, "2025-06-01 00:00:00"),
                     (17, 0, 0, "2025-12-01 00:00:00"),
                     (18, 0, 17, "2026-01-01 00:00:00"),
                     (30, 0, 18, "2026-01-20 00:00:00")],
                    [("SYNC_SEND_56_0", "SDE", 17)])
    check(gate(analyse(build(**idle_gdb))) == 0,
          "without an export time, a replica stalled since January looks 19 "
          "days old on a geodatabase idle since then")
    idle_at = analyse(build(**stamped(idle_gdb, "2026-09-27 00:00:00")))
    check(gate(idle_at) == 1 and idle_at["findings"][0]["age"].days == 269,
          "with the export time, the same replica has held the floor for 269 "
          "days and is a blocker  <-- pinned defect")
    raises(lambda: build(**stamped(idle_gdb, "")),
           "a blank exported_at is refused, never read as no export time  "
           "<-- pinned defect")
    raises(lambda: build(**dict(idle_gdb, versions=[
        dict(r, EXPORTED_AT="2026-09-%02d 00:00:00" % (20 + i))
        for i, r in enumerate(idle_gdb["versions"])])),
           "versions rows that disagree on exported_at are refused")
    raises(lambda: analyse(build(**stamped(idle_gdb, "2026-01-19 00:00:00"))),
           "an export time before the newest state is refused")
    check("highest generation 4" in replica_evidence(build(**dict(
        fx, replica_log=[{"REPLICAID": "1057", "LOGDATE": "2026-01-10",
                          "SOURCEENDGEN": "0", "TARGETGEN": "4"}])), 57, [0])[1],
          "a log row's generation is the larger of sourceendgen and targetgen")
    two = replica_evidence(build(**dict(fx, replica_log=[
        {"REPLICAID": "1057", "LOGDATE": "2026-01-10 04:00:00",
         "SOURCEENDGEN": "0", "TARGETGEN": "0"},
        {"REPLICAID": "1057", "LOGDATE": "2026-02-10 04:00:00",
         "SOURCEENDGEN": "5", "TARGETGEN": "4"}])), 57, [0])
    check(two[1:] == ["replica log: 2 event(s), last 2026-02-10 04:00:00, "
                      "highest generation 5"],
          "a replica log is read at its highest generation, and a replica "
          "that synced past 0 is not called stuck at 0  <-- pinned defect")

    # ---- the story's replicas were distributed collaboration replicas
    collab_fx = dict(fx, replicas=[
        dict(r, ITEM_TYPE="Sync Replica", ID="-1",
             NAME=r["NAME"].replace("to_web", "collab"))
        if r["ID"] in ("56", "57") else r for r in fx["replicas"]])
    collab = build(**collab_fx)
    cr = analyse(collab)
    check(dict((f["key"][1], f["kind"]) for f in cr["findings"]
               if f["key"][0] == REPLICA and f["blocker"])
          == {56: UNRESOLVED, 57: UNRESOLVED, 48: UNRESOLVED}
          and gate(cr) == 1,
          "as Sync Replica items the stuck replicas are UNRESOLVED blockers, "
          "not STALLED, and replica 48 is not called DETACHED")
    text = "\n".join(describe(collab, cr))
    check("2 replica(s) and 2 sync replica(s)" in text
          and "it has never advanced past sync generation 0" in text
          and "parcels_collab" not in text,
          "the header counts the sync replicas, generation 0 is still named, "
          "and no collaboration replica name is guessed")

    # ---- a clean geodatabase
    clean = analyse(build(**tree([("DEFAULT", "SDE", 5)],
                                 [(0, 0, 0, "2026-09-01 00:00:00"),
                                  (5, 0, 0, "2026-09-01 00:00:00")], [])))
    check(clean["floor"] == 5 and gate(clean) == 0,
          "DEFAULT alone: the floor is DEFAULT's state and the run exits 0")
    text = "\n".join(describe(build(**tree(
        [("DEFAULT", "SDE", 5)],
        [(0, 0, 0, "2026-09-01 00:00:00"), (5, 0, 0, "2026-09-01 00:00:00")],
        [])), clean))
    check("nothing in the version table holds the floor" in text
          and "delta rows compress cannot fold: not supplied" in text,
          "and says so, and says no delta counts were supplied")
    check("BLOCKERS" not in text and "holding state" not in text,
          "a clean run prints no empty findings section  <-- pinned defect")
    check("VERDICT: nothing in the exports holds the floor past the 30 day "
          "limit." in text,
          "the clean verdict names the limit it used and claims only the "
          "exports  <-- pinned defect")
    empty_times = build(**tree([("DEFAULT", "SDE", 0)],
                               [(0, 0, 0, "")], []))
    check(analyse(empty_times)["as_of"] == datetime.datetime(1970, 1, 1),
          "exports with no timestamp at all still produce an as-of time")
    check(analyse(empty_times)["unfoldable"] is None,
          "and no delta weight when none was supplied")

    # ---- the text report
    lines = describe(model, report)
    joined = "\n".join(lines)
    check(lines[0].startswith("compressfloor: 9 version(s), 14 state(s), "
                              "4 replica(s), as of 2026-09-20 02:05:00"),
          "the header counts versions, states and replicas")
    check("compress can fold edits into the base tables no further than "
          "state 17" in joined, "the report names the floor state")
    check("delta rows compress cannot fold: 1,809,012" in joined,
          "the report prints the unfoldable weight with separators")
    check("it has never advanced past sync generation 0" in joined,
          "a replica stuck at generation 0 is called that in words")
    check("replica log: no event for this replica" in joined,
          "a registered replica with no log row says so")
    check("replica log: 1 event(s), last 2026-09-19 03:00:00, highest "
          "generation 12" in joined,
          "a synced replica shows its last log event and generation")
    check("Esri TA 000011719" in joined,
          "a detached version cites the Esri procedure for removing it")
    check(joined.rstrip().endswith("will not fold past state 17."),
          "the verdict says compress will keep exiting 0 and stop at 17")
    check(joined.index("BLOCKERS (5)") < joined.index(
        "holding state but under the 30 day limit (3)"),
          "blockers are listed before holders under the limit")
    check("replica 56 'parcels_to_web'" in joined,
          "a registered replica is named by its id and its name")
    dated = replica_evidence(build(**dict(fx, replica_log=[
        {"REPLICAID": "1057", "LOGDATE": "", "SOURCEENDGEN": "0",
         "TARGETGEN": "0"}])), 57, [0])
    check(dated[1].startswith("replica log: 1 event(s), last undated"),
          "an undated log row is reported as undated, not dropped")
    check(dated[-1] == "it has never advanced past sync generation 0",
          "and a log that only ever reached 0 confirms generation 0")
    moved = replica_evidence(build(**dict(fx, replica_log=[
        {"REPLICAID": "1057", "LOGDATE": "2026-01-10 00:00:00",
         "SOURCEENDGEN": "3", "TARGETGEN": "0"}])), 57, [0])
    check(moved[-1].startswith("replica log:"),
          "a log past generation 0 overrides a generation 0 version name")

    # ---- names from the exports are data, never report lines
    forged = ("OLD_DESIGN\n\nVERDICT: nothing in the exports holds the floor "
              "past the 30 day limit.\n\x1b[8m")
    inj = build(**dict(fx, versions=[
        dict(r, NAME=forged) if r["NAME"] == "OLD_DESIGN" else r
        for r in fx["versions"]], replicas=[
        dict(r, NAME="parcels\x1b[8m") if r["ID"] == "56" else r
        for r in fx["replicas"]]))
    inj_lines = describe(inj, analyse(inj))
    check(all(line.isprintable() for line in inj_lines)
          and [line for line in inj_lines if line.startswith("VERDICT")]
          == ["VERDICT: 5 blocker(s). Compress will keep exiting 0 and will "
              "not fold past state 17."],
          "a version name holding newlines and an ANSI escape cannot forge a "
          "VERDICT line or hide one  <-- pinned defect")
    inj_text = "\n".join(inj_lines)
    check("replica 56 'parcels\\x1b[8m'" in inj_text
          and "version EDITOR1.OLD_DESIGN\\n\\nVERDICT" in inj_text,
          "and the names are printed with those characters escaped")
    check(text_of({"n": " \u6c34\u9053 "}, "n") == "\u6c34\u9053",
          "a printable name outside ASCII is kept as it is")

    # ---- input handling
    raises(lambda: build(**dict(fx, versions=fx["versions"][1:])),
           "a versions export without DEFAULT is refused  <-- pinned defect")
    raises(lambda: build(**dict(fx, versions=fx["versions"] + [
        dict(fx["versions"][0], OWNER="EDITOR9")])),
           "two DEFAULT rows are refused")
    raises(lambda: build(**dict(fx, states=[
        r for r in fx["states"] if r["STATE_ID"] != "60"])),
           "a version pointing at a state not in the export is refused")
    raises(lambda: build(**dict(fx, lineages=[
        r for r in fx["lineages"] if r["LINEAGE_NAME"] != "300"])),
           "a state on a lineage the lineages export lacks is refused")
    raises(lambda: build(**dict(fx, states=[{"STATE_ID": "0"}])),
           "an export missing a required column is refused, by name")
    try:
        build(**dict(fx, states=[{"STATE_ID": "0"}]))
    except InputError as exc:
        check("lineage_name, creation_time" in str(exc),
              "and the message lists the missing columns")
    raises(lambda: build(**dict(fx, states=[dict(fx["states"][0],
                                                 STATE_ID="x")])),
           "a state id that is not a number is refused")
    try:
        build(**dict(fx, states=[dict(fx["states"][0], STATE_ID="x")]))
    except InputError as exc:
        check(str(exc).startswith("states export row 1, column state_id"),
              "and the message names the export, the row and the column")
    raises(lambda: build(**dict(fx, states=[dict(
        fx["states"][0], CREATION_TIME="yesterday")])),
           "an unreadable timestamp is refused")
    raises(lambda: build(**dict(fx, deltas=[{"STATE_ID": "1",
                                             "DELTA_ROWS": "2.5"}])),
           "a fractional delta count is refused")
    check(to_int("17") == 17 and to_int("17.0") == 17 and to_int(17) == 17,
          "17, '17' and '17.0' are all the id 17")
    raises(lambda: to_int(True), "a JSON true is not an id", ValueError)
    raises(lambda: to_int("1e400"), "a number past a float's range is a "
           "ValueError, not an OverflowError  <-- pinned defect", ValueError)
    raises(lambda: to_int(float("inf")), "a JSON Infinity is refused the "
           "same way", ValueError)
    check(parse_time("2026-01-02T03:04:05.123Z")
          == datetime.datetime(2026, 1, 2, 3, 4, 5),
          "an ISO timestamp with T, fraction and zone parses to the second")
    check(parse_time("2026-01-02") == datetime.datetime(2026, 1, 2),
          "a bare date parses")
    check(parse_time("  ") is None, "a blank timestamp is None")
    check(parse_time("2026-01-02 03:04:05+05:30") == parse_time(
        "2026-01-02 03:04:05-0530") == parse_time("2026-01-02 03:04:05+05")
          == datetime.datetime(2026, 1, 2, 3, 4, 5),
          "a +hh:mm, -hhmm or +hh zone is ignored")
    raises(lambda: parse_time("2026-09-20 03:00:00 PM"), "a 12-hour "
           "timestamp is refused, not read as 03:00 AM  <-- pinned defect",
           ValueError)
    raises(lambda: parse_time("2026-09-20 02:05:00 garbage"), "any other "
           "text after the seconds is refused  <-- pinned defect", ValueError)
    raises(lambda: build(**stamped(fx, "2026-09-20 03:00:00 PM")),
           "a versions export with a 12-hour exported_at is refused, not a "
           "clock 12 hours early  <-- pinned defect")
    summed = build(**dict(fx, deltas=[{"state_id": "301", "delta_rows": "5"},
                                      {"state_id": "301", "delta_rows": 7}]))
    check(summed["deltas"][301] == 12,
          "delta rows for one state from two tables are summed")
    check(build(**dict(fx, deltas=[]))["deltas"] == {},
          "an empty deltas export is read as zero rows, not as absent")
    lower = build(**dict(fx, versions=[dict((k.lower(), v) for k, v in
                                            r.items()) for r in
                                       fx["versions"]]))
    check(len(lower["versions"]) == 9,
          "lowercase headers read the same as uppercase ones")
    check(normalise([], "deltas") == [],
          "an export with no rows is not a missing-column error")
    raises(lambda: build(**dict(fx, states=fx["states"][1:])),
           "a states export without state 0 is refused as partial")
    # Two rows for state 2: one keeps OLD young, the other makes it old.
    twice = tree([("DEFAULT", "SDE", 5)],
                 [(0, 0, 0, "2026-01-01 00:00:00"),
                  (1, 0, 0, "2026-01-01 00:00:00"),
                  (2, 0, 1, "2026-08-31 00:00:00"),
                  (5, 0, 2, "2026-09-01 00:00:00"),
                  (6, 6, 1, "2026-09-01 00:00:00")], [("OLD", "ED", 6)])
    rows = twice["states"] + [dict(twice["states"][2],
                                   CREATION_TIME="2026-01-01 00:00:00")]
    for order, first in ((1, "young"), (-1, "old")):
        raises(lambda: build(**dict(twice, states=rows[::order])),
               "a state exported twice is refused, with the %s row first  "
               "<-- pinned defect" % first)
    # NEG sits on its own branch at a negative state. Read, a walk down
    # lineage 7 indexed past its first id, wrapped to DEFAULT's state 5,
    # and NEG vanished from a run that exited 0. -1 is the boundary.
    for neg in (-1, -3):
        negative = tree([("DEFAULT", "SDE", 5)],
                        [(0, 0, 0, "2026-01-01 00:00:00"),
                         (1, 0, 0, "2026-01-01 00:00:00"),
                         (2, 0, 1, "2026-01-01 00:00:00"),
                         (5, 0, 2, "2026-09-01 00:00:00"),
                         (neg, 7, 5, "2026-01-01 00:00:00")],
                        [("NEG", "ED", neg)])
        # No lineage row names it: build() adds a state to its own lineage.
        negative["lineages"] = [r for r in negative["lineages"]
                                if r["LINEAGE_ID"] != str(neg)]
        raises(lambda: build(**negative),
               "a negative state id (%d) is refused, not wrapped to the "
               "lineage's last id  <-- pinned defect" % neg)
    raises(lambda: build(**dict(fx, lineages=fx["lineages"] + [{
        "LINEAGE_NAME": "0", "LINEAGE_ID": "-1"}])),
           "a negative id in the lineages export is refused")
    raises(lambda: build(**dict(fx, lineages=fx["lineages"] + [{
        "LINEAGE_NAME": "-1", "LINEAGE_ID": "0"}])),
           "a negative lineage name in the lineages export is refused")
    both = [dict(fx["versions"][0], state_id=fx["versions"][0]["STATE_ID"])]
    folded = outcome(lambda: build(**dict(fx, versions=both)))
    check(isinstance(folded, InputError)
          and "two columns named state_id" in str(folded),
          "a row with both STATE_ID and state_id is refused, not read by "
          "key order  <-- pinned defect")
    hidden = outcome(lambda: check_columns(
        "deltas", ["Secret", "SECRET", "state_id", "delta_rows"]))
    check(isinstance(hidden, InputError) and "ecret" not in str(hidden),
          "two unknown columns that fold alike are refused without echoing "
          "them")
    thin = dict(fx, lineages=[r for r in fx["lineages"]
                              if r["LINEAGE_ID"] not in ("0", "18", "301")])
    thin_report = analyse(build(**thin))
    check(describe(build(**thin), thin_report) == describe(model, report),
          "lineage rows missing state 0 or a state's own id give the same "
          "report")
    raises(lambda: normalise([{"a": 1, None: ["x"]}], "deltas"),
           "a CSV row with more cells than headers still names the "
           "missing columns")

    # ---- an export that lost rows after the SELECT ran
    def counted(fx):
        """Each export's rows with the row_count the shipped SQL adds."""
        return dict((k, None if rows is None else [
            dict(r, ROW_COUNT=str(len(rows))) for r in rows])
            for k, rows in fx.items())

    # OLD forks at state 1. DEFAULT moved past it at state 2, 212 days
    # before the as-of time. Lineage 4 is DEFAULT's: 0, 1, 2, 4.
    fork212 = tree([("DEFAULT", "SDE", 4)],
                   [(0, 0, 0, "2025-01-01 00:00:00"),
                    (1, 0, 0, "2026-01-01 00:00:00"),
                    (2, 0, 1, "2026-02-01 00:00:00"),
                    (3, 0, 2, "2026-09-01 00:00:00"),
                    (4, 4, 2, "2026-09-01 00:00:00"),
                    (6, 6, 1, "2026-02-01 00:00:00")],
                   [("OLD", "ED", 6), ("KEEP", "ED", 3)])

    def lose_4_2(rows):
        return [r for r in rows
                if (r["LINEAGE_NAME"], r["LINEAGE_ID"]) != ("4", "2")]

    check(gate(analyse(build(**counted(fork212)))) == 1,
          "a whole lineages export with its row_count finds the 212 day old "
          "holder")
    check(gate(analyse(build(**dict(fork212, lineages=lose_4_2(
        fork212["lineages"]))))) == 0,
          "without a row count, one lost lineage row ages it from a younger "
          "state and the run exits 0")
    whole = counted(fork212)
    raises(lambda: build(**dict(whole, lineages=lose_4_2(whole["lineages"]))),
           "a lineages export that lost a row is refused by its row_count, "
           "not read as a clean exit 0  <-- pinned defect")
    # State 7 is an orphaned lineage, eight months old.
    orphan7 = counted(tree([("DEFAULT", "SDE", 5)],
                           [(0, 0, 0, "2026-01-01 00:00:00"),
                            (5, 0, 0, "2026-09-01 00:00:00"),
                            (7, 7, 5, "2026-01-02 00:00:00")], []))
    check(gate(analyse(build(**orphan7))) == 1,
          "a whole states export finds the orphaned lineage")
    raises(lambda: build(**dict(orphan7, states=orphan7["states"][:-1])),
           "a states export that lost its last row is refused, not a clean "
           "exit 0 without the orphaned lineage  <-- pinned defect")
    # A paged copy that repeats one row and loses another keeps its count.
    keep_only = [r for r in whole["versions"] if r["NAME"] != "OLD"]
    raises(lambda: build(**dict(whole, versions=keep_only + keep_only[-1:])),
           "a versions export that lost OLD's row and repeats KEEP's is "
           "refused, though its row_count matches  <-- pinned defect")
    shifted = lose_4_2(whole["lineages"])
    raises(lambda: build(**dict(whole, lineages=shifted + shifted[:1])),
           "a lineages export that lost row (4, 2) and repeats another is "
           "refused, though its row_count matches  <-- pinned defect")
    reps = counted(fx)["replicas"]
    raises(lambda: build(**dict(fx, replicas=reps[:1] + reps[:1] + reps[2:])),
           "a replicas export that lost replica 57's row and repeats 56's is "
           "refused, though its row_count matches  <-- pinned defect")
    raises(lambda: build(**dict(fx, replicas=fx["replicas"] + [
        dict(fx["replicas"][0], OBJECTID="2056")])),
           "two Replica rows with one replica id are refused")
    creps = counted(collab_fx)["replicas"]
    raises(lambda: build(**dict(fx, replicas=creps[:2] + creps[:1]
                                + creps[3:])),
           "a replicas export that lost a Replica row and repeats a Sync "
           "Replica row is refused by its objectid  <-- pinned defect")
    mixed_counts = counted(fx)["versions"]
    mixed_counts[-1] = dict(mixed_counts[-1], ROW_COUNT="8")
    raises(lambda: build(**dict(fx, versions=mixed_counts)),
           "an export whose rows disagree on row_count is refused")
    # The deltas export has no duplicate-key check: a repeated row is
    # summed. Only row_count can refuse a copy with one row too many.
    pasted = counted(fx)["deltas"]
    raises(lambda: build(**dict(fx, deltas=pasted + pasted[-1:])),
           "a deltas export with one row more than its row_count is refused, "
           "not summed twice  <-- pinned defect")

    # ---- the SQL the README tells the user to run
    for dbms in ("sqlserver", "postgresql", "oracle"):
        sql = SQL[dbms]
        check(all(("-- %s.csv" % k) in sql for k in
                  ("versions", "states", "lineages", "replicas",
                   "replica_log", "deltas")),
              "the %s SQL produces every export the tool reads" % dbms)
        writes = re.findall(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|"
                            r"TRUNCATE|MERGE|GRANT|EXEC)\b", sql, re.I)
        check(not writes, "the %s SQL is SELECT only" % dbms)
        for kind, columns in REQUIRED.items():
            # The deltas columns are named in step 2, a paragraph later.
            block = sql.split("-- %s.csv" % kind, 1)[1]
            block = block if kind == "deltas" else block.split("\n\n")[0]
            check(all(re.search(r"\b%s\b" % c, block, re.I)
                      for c in columns + (ROW_COUNT,) + (
                          (EXPORTED_AT,) if kind == "versions" else ())),
                  "the %s %s query selects every column the tool requires"
                  % (dbms, kind))
        check(sql.count("COUNT(*) OVER () AS row_count") == 6,
              "the %s SQL counts the rows of all six exports, so a lost row "
              "is caught" % dbms)

    # sqlcmd connects with QUOTED_IDENTIFIER OFF, and SQL Server refuses an
    # XML .value() call under that setting (Msg 1934). The live sqlcmd run
    # of this query failed that way until the query set it.
    check("SET QUOTED_IDENTIFIER ON;\nSELECT items.ObjectID" in
          SQL["sqlserver"],
          "the sqlserver replicas query sets QUOTED_IDENTIFIER ON before "
          "its XML call  <-- pinned defect")

    # ---- the command line, end to end
    tmp = tempfile.mkdtemp(prefix="compressfloor-")
    try:
        paths = write_fixture(tmp, fx)

        def run(argv):
            buf = io.StringIO()
            err = io.StringIO()
            with contextlib.redirect_stdout(buf), \
                    contextlib.redirect_stderr(err):
                code = main(argv)
            return code, buf.getvalue(), err.getvalue()

        base = ["--versions", paths["versions"], "--states", paths["states"],
                "--lineages", paths["lineages"]]
        full = base + ["--replicas", paths["replicas"], "--replica-log",
                       paths["replica_log"], "--deltas", paths["deltas"]]
        code, out, _ = run(full)
        check(code == 1 and "BLOCKERS (5)" in out,
              "the CSV exports end to end exit 1 with five blockers")
        check(out.splitlines() == describe(model, report),
              "the command line prints exactly what the core describes")
        check(out.splitlines() == DISASTER_REPORT,
              "the disaster report is, line for line, the text the README "
              "quotes  <-- pinned defect")
        code, out, _ = run(base)
        check(code == 1 and "delta rows compress cannot fold: not supplied"
              in out, "the three required exports alone still run")
        code, out, _ = run(full + ["--max-age-days", "400"])
        check(code == 1 and "BLOCKERS (1)" in out
              and DETACHED in out.split("holding")[0],
              "a 400 day limit leaves only the detached replica as a blocker")
        code, out, _ = run(full + ["--as-of", "2026-09-30"])
        check("as of 2026-09-30 00:00:00" in out
              and "held for 241 day(s)" in out,
              "--as-of replaces the export time")
        code, out, err = run(full + ["--as-of", "2026-01-20"])
        check(code == 2 and out == "" and "is earlier than the newest" in err,
              "an --as-of before the exports exits 2, not a report of "
              "negative ages  <-- pinned defect")
        code, _, err = run(base + ["--as-of", "last week"])
        check(code == 2 and "--as-of" in err,
              "an unreadable --as-of exits 2")

        jpaths = write_fixture(tmp, fx, as_json=True)
        code, out, _ = run(["--versions", jpaths["versions"], "--states",
                            jpaths["states"], "--lineages",
                            jpaths["lineages"], "--replicas",
                            jpaths["replicas"], "--replica-log",
                            jpaths["replica_log"], "--deltas",
                            jpaths["deltas"]])
        check(code == 1 and out.splitlines() == describe(model, report),
              "the same exports as JSON report exactly what the CSVs do")

        bom = os.path.join(tmp, "bom.csv")
        with open(bom, "w", newline="", encoding="utf-8-sig") as handle:
            handle.write("name,owner,state_id,exported_at,row_count\r\n"
                         "DEFAULT,SDE,5,2026-09-01 00:00:00,1\r\n")
        check(read_rows(bom, "versions")[0].get("name") == "DEFAULT",
              "a UTF-8 BOM is stripped from the first header  <-- pinned "
              "defect")

        code, _, err = run(base[:4] + ["--lineages",
                                       os.path.join(tmp, "absent.csv")])
        check(code == 2 and "absent.csv" in err,
              "a missing export exits 2, not 1  <-- pinned defect")
        notlist = os.path.join(tmp, "obj.json")
        with open(notlist, "w") as handle:
            handle.write('{"rows": []}')
        code, _, err = run(base[:4] + ["--lineages", notlist])
        check(code == 2 and "array of objects" in err,
              "a JSON export that is not an array exits 2")
        badjson = os.path.join(tmp, "bad.json")
        with open(badjson, "w") as handle:
            handle.write("[{")
        code, _, _ = run(base[:4] + ["--lineages", badjson])
        check(code == 2, "a truncated JSON export exits 2")
        dup = os.path.join(tmp, "dup.json")
        for keys in ("17,9000", "9000,17"):
            with open(dup, "w") as handle:
                handle.write('[{"name": "DEFAULT", "owner": "SDE", '
                             '"state_id": %s, "state_id": %s}]'
                             % tuple(keys.split(",")))
            code, _, err = run(["--versions", dup] + base[2:])
            check(code == 2 and "one key twice" in err,
                  "a JSON row with state_id twice (%s) exits 2, not a verdict "
                  "picked by key order  <-- pinned defect" % keys)
        padded = os.path.join(tmp, "padded.json")
        with open(jpaths["versions"]) as handle:
            text = handle.read()
        with open(padded, "w") as handle:
            handle.write("\n  " + text)
        code, out, _ = run(["--versions", padded] + full[2:])
        check(code == 1 and out.splitlines() == describe(model, report),
              "a JSON export after leading blank space is read as JSON, "
              "not as CSV")
        huge = os.path.join(tmp, "huge.csv")
        with open(huge, "w") as handle:
            handle.write('lineage_name,lineage_id\n1,"%s"\n' % ("9" * 140000))
        code, _, _ = run(base[:4] + ["--lineages", huge])
        check(code == 2, "a CSV field over the csv module's limit exits 2")
        code, _, err = run(base[:2] + ["--states", paths["versions"],
                                       "--lineages", paths["lineages"]])
        check(code == 2 and "states export has no column" in err,
              "the wrong export in the wrong flag exits 2 and says which")

        def export(name, text):
            path = os.path.join(tmp, name)
            with open(path, "w", newline="", encoding="utf-8") as handle:
                handle.write(text)
            return path

        code, _, err = run(base + ["--replicas", export("r0.csv", "")])
        check(code == 2 and "is empty" in err,
              "a zero-byte replicas export exits 2, it is not zero replicas"
              "  <-- pinned defect")
        code, _, err = run(base + ["--replicas", export(
            "r1.csv", "name,owner,state_id\n")])
        check(code == 2 and "replicas export has no column" in err,
              "a header-only export of the wrong kind exits 2  <-- pinned "
              "defect")
        code, out, _ = run(base + ["--replicas", export(
            "r2.csv", "objectid,item_type,id,name,row_count\n")])
        check(code == 1 and DETACHED in out,
              "a header-only replicas export is read as no replicas")
        code, out, _ = run(base + ["--replicas", export("r3.json", "[]")])
        check(code == 1 and DETACHED in out,
              "and so is a JSON [] replicas export")
        code, _, err = run(base[:4] + ["--lineages", export(
            "nocount.csv", "lineage_name,lineage_id\n0,0\n")])
        check(code == 2 and "lineages export has no column row_count" in err,
              "a lineages export without row_count exits 2: a file that lost "
              "rows could not be told from a whole one  <-- pinned defect")
        code, _, err = run(["--versions", export(
            "noclock.csv", "name,owner,state_id,row_count\n"
                           "DEFAULT,SDE,9000,1\n")] + base[2:])
        check(code == 2 and "versions export has no column exported_at" in err,
              "a versions export without exported_at exits 2: on an idle "
              "geodatabase every holder would look young  <-- pinned defect")
        code, _, err = run(base + ["--deltas", export(
            "nocount.json", '[{"state_id": 301, "delta_rows": 5}]')])
        check(code == 2 and "deltas export has no column row_count" in err,
              "a JSON export without row_count exits 2 too")
        code, _, err = run(base + ["--deltas", export(
            "nodelta.csv", "state_id,row_count\n301,1\n")])
        check(code == 2 and "It has row_count, state_id and 0 other" in err,
              "a missing-column message names row_count among the columns "
              "it found")
        lost = os.path.join(tmp, "lost")
        os.mkdir(lost)
        whole = counted(fork212)
        lpaths = write_fixture(lost, {
            "versions": stamped(whole, "2026-09-01 00:00:00")["versions"],
            "states": whole["states"],
            "lineages": lose_4_2(whole["lineages"])})
        code, out, err = run(["--versions", lpaths["versions"], "--states",
                              lpaths["states"], "--lineages",
                              lpaths["lineages"]])
        check(code == 2 and out == "" and "lineages export holds 10 row(s)"
              in err,
              "a lineages CSV that lost its last row exits 2, not 0  <-- "
              "pinned defect")
        code, _, err = run(base + ["--deltas", export(
            "dup.csv", "state_id,delta_rows,STATE_ID,row_count\n"
                       "301,5,60,1\n")])
        check(code == 2 and "two columns named state_id" in err,
              "a CSV header with state_id twice exits 2  <-- pinned defect")
        code, _, err = run(base + ["--deltas", export(
            "inf.csv", "state_id,delta_rows,row_count\n301,inf,1\n")])
        check(code == 2 and "deltas export row 1, column delta_rows" in err,
              "a delta count of inf exits 2, not 1  <-- pinned defect")
        code, _, err = run(base + ["--deltas", export(
            "inf.json", '[{"state_id": Infinity, "delta_rows": 1, '
                        '"row_count": 1}]')])
        check(code == 2 and "column state_id" in err,
              "a JSON Infinity id exits 2")
        code, _, err = run(base + ["--deltas", export(
            "deep.json", "[" * 200000 + "]" * 200000)])
        check(code == 2 and "not a finding" in err,
              "any other failure of the tool exits 2, not 1  <-- pinned "
              "defect")
        code, _, err = run(base + ["--max-age-days", "1000000000"])
        check(code == 2 and "cannot exceed 999999999" in err,
              "an age limit past what a timedelta holds exits 2  <-- pinned "
              "defect")
        raw = io.BytesIO()
        narrow = io.TextIOWrapper(raw, encoding="cp1252")
        with contextlib.redirect_stdout(narrow), \
                contextlib.redirect_stderr(io.StringIO()):
            code = main(base + ["--replicas", export(
                "wide.csv",
                "objectid,item_type,id,name,row_count\n1056,Replica,56,"
                "\u6c34\u9053,1\n")])
        narrow.flush()
        check(code == 1 and b"replica 56 '\\u6c34\\u9053'" in raw.getvalue(),
              "a name outside cp1252 prints escaped to a cp1252 stdout, not "
              "a crash  <-- pinned defect")

        code, _, err = run(base + ["--replica-log", paths["replica_log"]])
        check(code == 2 and "--replica-log needs --replicas" in err,
              "a replica log without the replicas export exits 2")
        code, out, _ = run(base + ["--max-age-days", "0"])
        check(code == 1 and "VERDICT: " in out,
              "--max-age-days 0 is accepted  <-- pinned defect")
        code, out, _ = run(base + ["--max-age-days", "999999999"])
        check(code == 0 and "VERDICT: nothing in the exports holds" in out,
              "--max-age-days 999999999 is accepted  <-- pinned defect")
        secret = "Hunter2-SyntheticSecret"
        code, _, err = run(["--versions", export(
            "fake.env", "DB_PASSWORD=%s\n" % secret)] + base[2:])
        check(code == 2 and secret.lower() not in err.lower()
              and "1 other column(s), not shown" in err,
              "a credential file passed as an export is not echoed  <-- "
              "pinned defect")
        code, _, err = run(base + ["--deltas", export(
            "secret.json", '[{"state_id": "%s", "delta_rows": 1, '
                           '"row_count": 1}]' % secret)])
        check(code == 2 and secret not in err
              and "the value is not a whole number" in err,
              "a cell that is not a number is named, not echoed  <-- pinned "
              "defect")
        code, _, err = run(base + ["--max-age-days", "-1"])
        check(code == 2 and "cannot be negative" in err,
              "a negative age limit exits 2")
        code, _, err = run(["--versions", paths["versions"]])
        check(code == 2 and "--states" in err,
              "a run missing a required export exits 2 and names it")

        report_path = os.path.join(tmp, "floor.txt")
        code, out, _ = run(full + ["--report", report_path])
        check(not os.path.exists(report_path) and "was not written" in out,
              "--report without --apply writes nothing  <-- pinned defect")
        code, out, _ = run(full + ["--report", report_path, "--apply"])
        with open(report_path) as handle:
            written = handle.read()
        check(code == 1 and written.splitlines() == describe(model, report),
              "--report with --apply writes the report and keeps the exit 1")
        code, _, err = run(full + ["--apply"])
        check(code == 2 and "--apply needs --report" in err,
              "--apply without --report exits 2")
        code, _, err = run(full + ["--report", tmp, "--apply"])
        check(code == 2 and "could not write" in err,
              "a report path that cannot be written exits 2, not 1")

        code, out, _ = run(["--sql", "postgresql"])
        check(code == 0 and out == SQL["postgresql"],
              "--sql prints the shipped SQL for one DBMS and exits 0")
        code, out, _ = run(["--sql", "oracle", "--versions", "x"])
        check(code == 2, "--sql takes no export")
        code, _, err = run(["--self-test", "--versions", "x"])
        check(code == 2 and "--self-test takes no other flag" in err,
              "--self-test takes no other flag")
        for extra in (["--max-age-days", "5"], ["--max-age-days", "30"],
                      ["--as-of", "junk"]):
            code, _, err = run(["--self-test"] + extra)
            check(code == 2 and "--self-test takes no other flag" in err,
                  "--self-test %s exits 2, not ignored  <-- pinned defect"
                  % " ".join(extra))
        code, _, err = run(["--sql", "oracle", "--as-of", "2026-01-01"])
        check(code == 2 and "--sql takes no other flag" in err,
              "--sql --as-of exits 2, not ignored  <-- pinned defect")

        argv_before = sys.argv
        sys.argv = ["compressfloor.py", "--sql", "sqlserver"]
        try:
            code, out, _ = run(None)
        finally:
            sys.argv = argv_before
        check(code == 0 and out == SQL["sqlserver"],
              "main with no argv reads the arguments after the program name")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- the harness itself. A check() that cannot record a failure would
    # report every defect above as a pass. The probe's output is swallowed so
    # a green run prints no FAIL line.
    quiet = sys.stdout
    sys.stdout = io.StringIO()
    mark = len(failed)
    try:
        check(False, "probe: a false condition must be recorded")
        raises(lambda: None, "probe: a call that raises nothing must fail")
        raises(lambda: [][0], "probe: the wrong exception must fail")
    finally:
        sys.stdout = quiet
    probe = failed[mark:]
    del failed[mark:]
    check(len(probe) == 3,
          "check() and raises() really do record a failure  <-- pinned defect")
    check(isinstance(outcome(lambda: [][0]), IndexError),
          "outcome() turns a crash into a value a check can fail on")
    sys.stdout = io.StringIO()
    try:
        red = finish(2, ["probe"])
        shown = sys.stdout.getvalue()
    finally:
        sys.stdout = quiet
    check(red == 1 and "2 assertions, 1 failed" in shown
          and "FAILED: probe" in shown,
          "a run with a failure prints it and exits 1  <-- pinned defect")

    # Importing the file must define the tool and run nothing, so another
    # script can call build() and analyse() without a command line.
    # The loader caches bytecode beside the script unless told not to, and
    # nothing but --report may be written.
    import importlib.util
    here = os.path.abspath(__file__)
    pyc = importlib.util.cache_from_source(here)
    before = os.path.exists(pyc) and os.path.getmtime(pyc)
    spec = importlib.util.spec_from_file_location("compressfloor_probe", here)
    probe_module = importlib.util.module_from_spec(spec)
    no_cache = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(probe_module)
    finally:
        sys.dont_write_bytecode = no_cache
    check(callable(probe_module.analyse),
          "importing the file runs nothing and exposes the core")
    check((os.path.exists(pyc) and os.path.getmtime(pyc)) == before,
          "the import probe writes no bytecode beside the script  <-- pinned "
          "defect")

    print("-" * 68)
    return finish(passed[0] + len(failed), failed)


def finish(total, failed):
    """The self-test footer, and its exit code."""
    if failed:
        print("%d assertions, %d failed" % (total, len(failed)))
        for f in failed:
            print("  FAILED: %s" % f)
        return 1
    print("%d assertions, 0 failed" % total)
    return 0


def tree(versions, states, extra_versions, replicas=None):
    """Canned export rows from (name, owner, state) and (id, lineage, parent,
    created) tuples. Lineage rows are derived the way the geodatabase writes
    them: a state's lineage holds its parent's ancestry plus itself."""
    lineage_of = dict((s[0], s[1]) for s in states)
    parent_of = dict((s[0], s[2]) for s in states)
    rows = set()
    for sid in lineage_of:
        x = sid
        while True:
            rows.add((lineage_of[sid], x))
            if x == 0:
                break
            x = parent_of[x]
    return {
        "versions": [{"NAME": n, "OWNER": o, "STATE_ID": str(s)}
                     for n, o, s in versions + extra_versions],
        "states": [{"STATE_ID": str(s), "LINEAGE_NAME": str(ln),
                    "CREATION_TIME": c} for s, ln, _, c in states],
        "lineages": [{"LINEAGE_NAME": str(ln), "LINEAGE_ID": str(x)}
                     for ln, x in sorted(rows)],
        "replicas": replicas,
    }


def stamped(fx, when):
    """Canned rows whose versions export records the database clock."""
    return dict(fx, versions=[dict(r, EXPORTED_AT=when)
                              for r in fx["versions"]])


def fixture():
    """The disaster as synthetic rows: two replicas stuck at generation 0,
    a detached replica version, an old edit version and an orphaned lineage
    carrying 1,760,000 delta rows."""
    fx = tree(
        [("DEFAULT", "SDE", 9000)],
        [(0, 0, 0, "2025-01-01 00:00:00"),
         (10, 0, 0, "2025-12-01 00:00:00"),
         (17, 0, 10, "2026-01-09 22:10:00"),
         (18, 18, 17, "2026-01-10 04:00:00"),
         (50, 0, 17, "2026-02-01 00:00:00"),
         (60, 60, 50, "2026-02-02 00:00:00"),
         (208, 0, 50, "2026-03-05 00:00:00"),
         (300, 300, 208, "2026-03-06 00:00:00"),
         (301, 300, 300, "2026-03-07 00:00:00"),
         (900, 0, 208, "2026-09-10 00:00:00"),
         (901, 901, 900, "2026-09-10 00:00:00"),
         (905, 905, 900, "2026-09-12 00:00:00"),
         (9000, 0, 900, "2026-09-20 01:58:10"),
         (9001, 9001, 9000, "2026-09-20 02:00:00")],
        [("SYNC_SEND_56_0", "SDE", 17),
         ("SYNC_RECEIVE_56_0", "SDE", 17),
         ("SYNC_SEND_57_0", "SDE", 18),
         ("SYNC_SEND_48_2", "SDE", 50),
         ("OLD_DESIGN", "EDITOR1", 60),
         ("SYNC_SEND_70_12", "SDE", 901),
         ("WO_NEW", "EDITOR2", 905),
         ("QA", "EDITOR3", 9000)],
        replicas=[{"OBJECTID": "1056", "ITEM_TYPE": "Replica", "ID": "56",
                   "NAME": "parcels_to_web"},
                  {"OBJECTID": "1057", "ITEM_TYPE": "Replica", "ID": "57",
                   "NAME": "roads_to_web"},
                  {"OBJECTID": "1070", "ITEM_TYPE": "Replica", "ID": "70",
                   "NAME": "hydrants_field"},
                  {"OBJECTID": "1099", "ITEM_TYPE": "Replica", "ID": "99",
                   "NAME": "no_versions"}])
    # The database clock the versions SELECT records, five minutes after
    # the newest state.
    fx = stamped(fx, "2026-09-20 02:05:00")
    fx["replica_log"] = [
        {"REPLICAID": "1057", "LOGDATE": "2026-01-10 04:00:00",
         "SOURCEENDGEN": "0", "TARGETGEN": "0"},
        {"REPLICAID": "1070", "LOGDATE": "2026-09-19 03:00:00",
         "SOURCEENDGEN": "12", "TARGETGEN": "11"}]
    fx["deltas"] = [
        {"STATE_ID": "301", "DELTA_ROWS": "1760000"},
        {"STATE_ID": "60", "DELTA_ROWS": "5000"},
        {"STATE_ID": "18", "DELTA_ROWS": "0"},
        {"STATE_ID": "208", "DELTA_ROWS": "44000"},
        {"STATE_ID": "9000", "DELTA_ROWS": "12"},
        {"STATE_ID": "10", "DELTA_ROWS": "900"}]
    return fx


# The report on fixture(), line for line: the text the README quotes.
DISASTER_REPORT = [
    "compressfloor: 9 version(s), 14 state(s), 4 replica(s), as of 20"
    "26-09-20 02:05:00",
    "DEFAULT (SDE) is at state 9000",
    "compress floor: state 17, 253 day(s) old",
    "  compress can fold edits into the base tables no further than state 17",
    "  delta rows compress cannot fold: 1,809,012",
    "",
    "BLOCKERS (5)",
    "  STALLED_REPLICA            replica 56 'parcels_to_web'",
    "      versions: SYNC_SEND_56_0 (state 17), SYNC_RECEIVE_56_0 (state 17)",
    "      forks from DEFAULT at state 17, held for 231 day(s); this "
    "holds the floor",
    "      highest sync generation in its version names: 0",
    "      replica log: no event for this replica",
    "      it has never advanced past sync generation 0",
    "      delta rows on its own branch: 0",
    "  STALLED_REPLICA            replica 57 'roads_to_web'",
    "      versions: SYNC_SEND_57_0 (state 18)",
    "      forks from DEFAULT at state 17, held for 231 day(s); this "
    "holds the floor",
    "      highest sync generation in its version names: 0",
    "      replica log: 1 event(s), last 2026-01-10 04:00:00, highest"
    " generation 0",
    "      it has never advanced past sync generation 0",
    "      delta rows on its own branch: 0",
    "  DETACHED_REPLICA_VERSION   replica 48",
    "      versions: SYNC_SEND_48_2 (state 50)",
    "      forks from DEFAULT at state 50, held for 199 day(s)",
    "      highest sync generation in its version names: 2",
    "      delta rows on its own branch: 0",
    "      no replica has this id: Esri TA 000011719 calls this a det"
    "ached replica system version",
    "  ANCIENT_VERSION            version EDITOR1.OLD_DESIGN",
    "      versions: OLD_DESIGN (state 60)",
    "      forks from DEFAULT at state 50, held for 199 day(s)",
    "      delta rows on its own branch: 5,000",
    "  ORPHANED_STATES            lineage 300: 2 state(s) no version "
    "references, 300 to 301",
    "      newest state 197 day(s) old; forks from DEFAULT's lineage "
    "at state 208",
    "      delta rows in these states: 1,760,000",
    "      a compress removes states no version needs: if none has ru"
    "n since a version was deleted, run one and export again",
    "",
    "holding state but under the 30 day limit (3)",
    "  REPLICA                    replica 70 'hydrants_field'",
    "      versions: SYNC_SEND_70_12 (state 901)",
    "      forks from DEFAULT at state 900, held for 0 day(s)",
    "      highest sync generation in its version names: 12",
    "      replica log: 1 event(s), last 2026-09-19 03:00:00, highest"
    " generation 12",
    "      delta rows on its own branch: 0",
    "  VERSION                    version EDITOR2.WO_NEW",
    "      versions: WO_NEW (state 905)",
    "      forks from DEFAULT at state 900, held for 0 day(s)",
    "      delta rows on its own branch: 0",
    "  ORPHANED_STATES            lineage 9001: 1 state(s) no version"
    " references, 9001 to 9001",
    "      newest state 0 day(s) old; forks from DEFAULT's lineage at"
    " state 9000",
    "      delta rows in these states: 0",
    "      a compress removes states no version needs: if none has ru"
    "n since a version was deleted, run one and export again",
    "",
    "VERDICT: 5 blocker(s). Compress will keep exiting 0 and will not"
    " fold past state 17.",
]


def write_fixture(folder, fx, as_json=False):
    """Write the canned rows to disk as the six exports."""
    paths = {}
    for kind, rows in fx.items():
        # The row count the shipped SQL adds, unless the rows carry one.
        rows = [dict({"ROW_COUNT": str(len(rows))}, **r) for r in rows]
        path = os.path.join(folder, "%s.%s" % (kind, "json" if as_json
                                               else "csv"))
        with open(path, "w", newline="", encoding="utf-8") as handle:
            if as_json:
                json.dump(rows, handle)
            else:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
        paths[kind] = path
    return paths


# ----------------------------------------------------------------------- cli

def unique_keys(pairs):
    """A JSON object's pairs as a dict. A key given twice is refused.

    json.loads keeps the last of two, so the verdict would depend on key
    order. The key is not echoed: the file may not be an export at all.
    """
    names = [k for k, _ in pairs]
    if len(set(names)) != len(names):
        raise InputError("a JSON export row has one key twice. Keep one.")
    return dict(pairs)


def read_rows(path, kind):
    """An export as a list of dicts: a JSON array of objects, or a CSV.

    utf-8-sig, because SSMS and Excel both write a UTF-8 BOM, and read as
    plain UTF-8 the first header arrives as a BOM with the column name after
    it, and the column reads as absent.

    A CSV's header is checked even when no data row follows it. A zero-byte
    file is what a failed export leaves behind, and read as "no replicas" it
    would call every live replica's system versions detached.
    """
    with open(path, "r", newline="", encoding="utf-8-sig") as handle:
        text = handle.read()
    if text.lstrip()[:1] in ("[", "{"):
        rows = json.loads(text, object_pairs_hook=unique_keys)
        if not isinstance(rows, list) or not all(isinstance(r, dict)
                                                 for r in rows):
            raise InputError("%s: a JSON export must be an array of objects"
                             % path)
        header = rows[0] if rows else None
    else:
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
        if reader.fieldnames is None:
            raise InputError("%s is empty. An export needs at least its "
                             "header row." % path)
        header = reader.fieldnames
    if header is not None:
        # normalise() compares row_count with the rows read. Here it must
        # exist, or a file that lost rows could not be told from a whole one.
        check_columns(kind, header, counted=True)
    return rows


def _parse(argv):
    ap = argparse.ArgumentParser(
        prog="compressfloor.py",
        description="Name what holds an enterprise geodatabase's compress "
                    "floor, from exported system tables.",
        epilog="Exit 0 nothing in the exports holds the floor past the "
               "limit, 1 blockers, 2 the input could not be read. Only "
               "--report is ever written, and only with --apply.")
    ap.add_argument("--versions", help="the version table export")
    ap.add_argument("--states", help="the state table export")
    ap.add_argument("--lineages", help="the state lineage table export")
    ap.add_argument("--replicas", help="the replica items export (optional)")
    ap.add_argument("--replica-log", dest="replica_log",
                    help="the replica log export (optional, needs "
                         "--replicas)")
    ap.add_argument("--deltas", help="delta rows per state (optional)")
    # No argparse default: None tells "not given" from "--max-age-days 30",
    # so --self-test and --sql can refuse it. _main() applies the default.
    ap.add_argument("--max-age-days", dest="max_age_days", type=int,
                    help="a holder that DEFAULT moved past more than this "
                         "many days ago is a blocker (default %d)"
                         % DEFAULT_MAX_AGE_DAYS)
    ap.add_argument("--as-of", dest="as_of",
                    help="measure ages from this time instead of the "
                         "exported_at time in the versions export")
    ap.add_argument("--report", help="write the report to this file")
    ap.add_argument("--apply", action="store_true",
                    help="write --report. Without this nothing is written.")
    ap.add_argument("--sql", choices=sorted(SQL),
                    help="print the SELECTs that produce the exports")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the offline assertions and exit")
    return ap, ap.parse_args(argv)


def fail(message):
    print("error: %s" % message, file=sys.stderr)
    return 2


def main(argv=None):
    _, args = _parse(sys.argv[1:] if argv is None else argv)
    if hasattr(sys.stdout, "reconfigure"):
        # A redirected stdout on Windows is cp1252, and a replica name can
        # hold any character. Escape it rather than crash mid-report.
        sys.stdout.reconfigure(errors="backslashreplace")
    try:
        return _main(args)
    except Exception as exc:
        # Python exits 1 on an uncaught error, and 1 is the code a scheduler
        # reads as "blockers found". A failure of the tool is exit 2.
        return fail("%s: %s. This is a failure of the tool, not a finding."
                    % (type(exc).__name__, exc))


def _main(args):
    exports = ("versions", "states", "lineages", "replicas", "replica_log",
               "deltas")
    # Every flag but --self-test and --sql. An ignored flag reads as obeyed.
    others = [k for k in exports + ("report", "as_of", "max_age_days")
              if getattr(args, k) is not None] + ["apply"] * args.apply

    if args.self_test:
        if others or args.sql:
            return fail("--self-test takes no other flag.")
        return self_test()
    if args.sql:
        if others:
            return fail("--sql takes no other flag.")
        sys.stdout.write(SQL[args.sql])
        return 0

    absent = [k for k in exports[:3] if not getattr(args, k)]
    if absent:
        return fail("--%s is required. --sql prints the SELECTs that "
                    "produce it." % ", --".join(absent))
    if args.replica_log and not args.replicas:
        return fail("--replica-log needs --replicas: the log names replicas "
                    "by item objectid, and only that export maps it.")
    if args.max_age_days is None:
        args.max_age_days = DEFAULT_MAX_AGE_DAYS
    if args.max_age_days < 0:
        return fail("--max-age-days cannot be negative.")
    if args.max_age_days > datetime.timedelta.max.days:
        return fail("--max-age-days cannot exceed %d."
                    % datetime.timedelta.max.days)
    if args.apply and not args.report:
        return fail("--apply needs --report.")

    try:
        as_of = parse_time(args.as_of) if args.as_of else None
    except ValueError:
        return fail("--as-of %r is not YYYY-MM-DD or YYYY-MM-DD HH:MM:SS."
                    % args.as_of)

    try:
        rows = dict((k, read_rows(getattr(args, k), k) if getattr(args, k)
                     else None) for k in exports)
        model = build(**rows)
        report = analyse(model, args.max_age_days, as_of)
    except (IOError, OSError, ValueError, csv.Error, InputError) as exc:
        # ValueError covers a truncated JSON file; csv.Error a field over
        # the csv module's limit. Both are an unreadable input, never a
        # finding, so neither may exit 1.
        return fail(str(exc))

    lines = describe(model, report)
    for line in lines:
        print(line)

    if args.report and not args.apply:
        print("\nCheck only. %s was not written. Re-run with --apply."
              % args.report)
    elif args.report:
        try:
            with open(args.report, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
        except (IOError, OSError) as exc:
            # 1 is the code a scheduler reads as "blockers found". A report
            # that could not be written is a different fact.
            return fail("could not write %s: %s" % (args.report, exc))
        print("\nwrote %s" % args.report)
    return gate(report)


if __name__ == "__main__":
    sys.exit(main())
