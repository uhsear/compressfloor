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
    python compressfloor.py ... --deltas d.csv --state-parents sp.csv
    python compressfloor.py ... --replicas r.csv --version-tree vt.csv \\
        --unregister-plan 56,57
    python compressfloor.py ... --exit-on never
    python compressfloor.py ... --report floor.txt --apply

Nothing is written without --apply, and only the --report file is ever written.
The one exception is --self-test, which writes its synthetic fixture files into
a temporary folder and removes them. It never writes beside the script.
The stranded view (--state-parents) and the unregister plan (--unregister-plan)
only read exports and print text. The plan never runs UnregisterReplica, never
deletes a version and never connects to a database.

Exit codes: 0 no finding of the --exit-on level, 1 at least one, 2 the tool
could not read its input or write its report. By default (--exit-on blocker) a
finding is a blocker: a holder past the age limit, or a stranded state that
holds delta rows. --exit-on any also counts a holder under the limit, and
--exit-on never turns every finding into exit 0 and keeps exit 2.
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
import subprocess
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
STRANDED = "STRANDED_SIBLING_STATE"
REPLICA = "REPLICA"
VERSION = "VERSION"

# What --exit-on counts as a reason to exit 1. blocker is the default and the
# behaviour before the flag existed.
EXIT_ON = ("blocker", "any", "never")

# The unregister plan's drain wait. UnregisterReplica returns before the
# replica's system versions are all released, and the poll count is the only
# way to see the release end. These numbers came from one operator's run on
# one geodatabase. No Esri page gives them: tune them to your own estate.
DRAIN_POLL_SECONDS = 30
DRAIN_STABLE_POLLS = 3
DRAIN_TIMEOUT_SECONDS = 1800

# A plan entry's status.
REGISTERED = "REGISTERED"
DETACHED_ID = "DETACHED"
REFUSED = "REFUSED"
ABSENT = "ABSENT"

# Columns each export must carry. Headers are matched case-insensitively.
REQUIRED = {
    "versions": ("name", "owner", "state_id"),
    "states": ("state_id", "lineage_name", "creation_time"),
    "lineages": ("lineage_name", "lineage_id"),
    "replicas": ("objectid", "item_type", "id", "name"),
    "replica_log": ("replicaid", "logdate", "sourceendgen", "targetgen"),
    "deltas": ("state_id", "delta_rows"),
    "state_parents": ("state_id", "parent_state_id", "owner"),
    "version_tree": ("version_id", "parent_version_id", "owner", "name"),
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

-- state_parents.csv (optional, needs deltas.csv)
SELECT state_id, parent_state_id, owner, COUNT(*) OVER () AS row_count
FROM sde.SDE_states;

-- version_tree.csv (optional)
SELECT version_id, parent_version_id, owner, name,
       COUNT(*) OVER () AS row_count
FROM sde.SDE_versions;

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

-- state_parents.csv (optional, needs deltas.csv)
SELECT state_id, parent_state_id, owner, COUNT(*) OVER () AS row_count
FROM sde.sde_states;

-- version_tree.csv (optional)
SELECT version_id, parent_version_id, owner, name,
       COUNT(*) OVER () AS row_count
FROM sde.sde_versions;

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

-- state_parents.csv (optional, needs deltas.csv)
SELECT state_id, parent_state_id, owner, COUNT(*) OVER () AS row_count
FROM sde.STATES;

-- version_tree.csv (optional)
SELECT version_id, parent_version_id, owner, name,
       COUNT(*) OVER () AS row_count
FROM sde.VERSIONS;

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
          deltas=None, state_parents=None, version_tree=None):
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
             "exported_at": None, "parents": None, "tree": None}
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
    if state_parents is not None:
        read_parents(model, state_parents)
    if version_tree is not None:
        read_tree(model, version_tree)
    return model


def read_parents(model, rows):
    """model["parents"]: state id to (parent state id, owner).

    The export must hold exactly the states of the states export. A state
    missing here, or one only here, means the two were taken at different
    moments, and a sibling found across them is a sibling of neither.
    """
    model["parents"] = {}
    for i, row in enumerate(normalise(rows, "state_parents")):
        sid = cell(row, "state_id", "state_parents", i, to_int)
        if sid in model["parents"]:
            raise InputError("state_parents export row %d repeats state %d. "
                             "Rows were copied twice, and others may be "
                             "lost. Export it again, whole." % (i + 1, sid))
        model["parents"][sid] = (
            cell(row, "parent_state_id", "state_parents", i, to_int),
            text_of(row, "owner"))
    if set(model["parents"]) != set(model["states"]):
        raise InputError("the state_parents export and the states export do "
                         "not hold the same states. Take every export in one "
                         "sitting.")
    for sid, (parent, _) in model["parents"].items():
        # Esri does not say what state 0's own parent holds, so it is
        # converted to a number above but never used.
        if sid and parent not in model["states"]:
            raise InputError("state %d has parent %d, which the states export "
                             "does not hold" % (sid, parent))
        # Esri documents parent_state_id as redundant with the last value of
        # the state's lineage list, so the two must agree.
        lineage = model["lineages"][model["states"][sid]["lineage"]]
        if sid and parent != lineage[bisect.bisect_left(lineage, sid) - 1]:
            raise InputError("state %d has a parent that its lineage does "
                             "not show. Take every export in one sitting."
                             % sid)


def read_tree(model, rows):
    """model["tree"]: (owner, name) to the parent version's (owner, name).

    A root, DEFAULT in practice, maps to None. The export must hold exactly
    the versions of the versions export, and its parent links must end.
    """
    ids = {}
    parent_ids = {}
    for i, row in enumerate(normalise(rows, "version_tree")):
        key = (text_of(row, "owner"), text_of(row, "name"))
        vid = cell(row, "version_id", "version_tree", i, to_int)
        if key in parent_ids or vid in ids:
            raise InputError("version_tree export row %d repeats a version. "
                             "Rows were copied twice, and others may be "
                             "lost. Export it again, whole." % (i + 1))
        ids[vid] = key
        raw = row.get("parent_version_id")
        blank = raw is None or str(raw).strip() == ""
        parent_ids[key] = None if blank else cell(
            row, "parent_version_id", "version_tree", i, to_int)
    if set(parent_ids) != set((v["owner"], v["name"])
                              for v in model["versions"]):
        raise InputError("the version_tree export and the versions export do "
                         "not hold the same versions. Take every export in "
                         "one sitting.")
    model["tree"] = {}
    for key, pid in parent_ids.items():
        if pid is not None and pid not in ids:
            raise InputError("version %s.%s has a parent version id that the "
                             "version_tree export does not hold" % key)
        model["tree"][key] = None if pid is None else ids[pid]
    for key in model["tree"]:
        seen = set()
        while key is not None:
            if key in seen:
                raise InputError("the version_tree export's parent links "
                                 "loop back on themselves")
            seen.add(key)
            key = model["tree"][key]


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

    if model["parents"] is not None:
        findings.extend(stranded(model, set(model["states"]) - covered,
                                 covered, anc_default, as_of))

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


def stranded(model, lost, covered, trunk, as_of):
    """Unreferenced states that have a later sibling: edits that were saved
    and then left out of the version.

    A sibling has the same parent state and the same owner. lost holds the
    states no version's lineage reaches, so no version points at them and none
    has them as an ancestor. covered holds the states a version does reach: a
    later sibling in it is the state the version kept. The cause is not
    proved: undone work can leave the same shape (see the README Limits).
    A compress removes unreferenced states, so rows still in one are the
    rows at risk. The weight is that state's own delta rows.
    """
    if model["deltas"] is None:
        raise InputError("the stranded view needs the deltas export: it "
                         "counts the rows a compress would remove")
    siblings = {}
    for sid, group in model["parents"].items():
        siblings.setdefault(group, []).append(sid)
    out = []
    for sid in sorted(lost):
        parent, owner = model["parents"][sid]
        later = sorted(x for x in siblings[(parent, owner)] if x > sid)
        if later:
            rows = weight(model, [sid])
            out.append({
                "kind": STRANDED, "blocker": rows > 0,
                "key": (STRANDED, sid), "states": [sid], "parent": parent,
                "owner": owner, "later": [(x, x in covered) for x in later],
                "fork": fork_of(model, sid, trunk)[0],
                "age": age_of(model, sid, as_of), "holds_floor": False,
                "weight": rows})
    if out and not model["deltas"]:
        # A header-only deltas file is what step 2 returns when the step 1
        # lines were left out. Read as "no rows anywhere" it would turn every
        # stranded blocker into a clean exit 0.
        raise InputError("the deltas export holds no rows, so the stranded "
                         "view cannot say which stranded states hold delta "
                         "rows. Check that step 1 of the deltas SQL returned "
                         "lines, then export again.")
    return out


def gate(report, exit_on="blocker"):
    """The exit code for a report. See --exit-on.

    blocker: 1 when any finding is a blocker. any: 1 when anything is
    listed, a holder under the age limit included. never: always 0. The
    codes are the tool's verdict only: exit 2 is not a verdict, and main()
    returns it for a failure whatever --exit-on says.
    """
    if exit_on not in EXIT_ON:
        raise ValueError("exit_on must be one of %s" % ", ".join(EXIT_ON))
    if exit_on == "never":
        return 0
    if exit_on == "any":
        return 1 if report["findings"] else 0
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
    out.append("  delta rows compress cannot fold: %s%s"
               % (number(report["unfoldable"]),
                  " (the deltas export holds no rows: check that step 1 of "
                  "the deltas SQL returned lines)"
                  if model["deltas"] is not None and not model["deltas"]
                  else ""))

    # A stranded state is also inside an ORPHANED_STATES lineage. The two
    # answer different questions, so each has its own section and count.
    strand = [f for f in report["findings"] if f["kind"] == STRANDED]
    rest = [f for f in report["findings"] if f["kind"] != STRANDED]
    for blocker, title in ((True, "BLOCKERS"),
                           (False, "holding state but under the %d day limit"
                            % report["max_age_days"])):
        group = [f for f in rest if f["blocker"] == blocker]
        if not group:
            continue
        out.append("")
        out.append("%s (%d)" % (title, len(group)))
        for f in group:
            out.extend(describe_finding(model, f))
    if strand:
        out.append("")
        out.append("STRANDED SIBLING STATES (%d)" % len(strand))
        for f in strand:
            out.extend(describe_finding(model, f))

    blockers = sum(1 for f in rest if f["blocker"])
    lost = sum(1 for f in strand if f["blocker"])
    out.append("")
    # An orphaned lineage pins nothing: a compress removes it, so only a
    # version-held blocker supports the "will not fold" prediction.
    if blockers and any(f["blocker"] and f["kind"] != ORPHANED for f in rest):
        out.append("VERDICT: %d blocker(s). Compress will keep exiting 0 and "
                   "will not fold past state %d." % (blockers, report["floor"]))
    elif blockers:
        out.append("VERDICT: %d blocker(s), all orphaned states. Nothing "
                   "pins the floor: the next compress removes them, so run "
                   "one and export again." % blockers)
    if lost:
        out.append("VERDICT: %d stranded state(s) hold delta rows that no "
                   "version reaches. The next compress removes states no "
                   "version references: extract the rows first." % lost)
    if not blockers and not lost:
        out.append("VERDICT: nothing in the exports holds the floor past the "
                   "%d day limit." % report["max_age_days"])
    return out


def describe_finding(model, f):
    """The lines for one finding."""
    kind, key = f["kind"], f["key"]
    if kind == STRANDED:
        head = ("state %d, owner %s, parent state %d"
                % (key[1], f["owner"], f["parent"]))
        body = ["no version points at state %d or reaches it through its "
                "lineage" % key[1],
                "later state(s) with the same parent and owner: %s"
                % ", ".join("%d (%s)" % (x, "a version reaches it" if r
                                         else "no version reaches it")
                            for x, r in f["later"]),
                "delta rows in this state: %s; created %s"
                % (number(f["weight"]), "at an unknown time"
                   if f["age"] is None else "%d day(s) before the as-of time"
                   % f["age"].days),
                "a compress removes states no version references"
                + (": extract these rows first" if f["blocker"] else
                   ", and this one holds no delta rows")]
        return ["  %-26s %s" % (kind, head)] + ["      " + b for b in body]
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


# --------------------------------------------------------- unregister plan

def parse_ids(text):
    """The --unregister-plan allowlist: replica ids, comma separated.

    There is no "all". ASCII digits only: str.isdigit() accepts characters
    that int() then reads as another number or refuses.
    """
    tokens = [t.strip() for t in text.split(",")]
    if not all(re.fullmatch(r"[0-9]+", t) for t in tokens):
        raise ValueError("an id list is digits separated by commas, such as "
                         "56,57")
    ids = [int(t) for t in tokens]
    if len(set(ids)) != len(ids):
        raise ValueError("an id appears twice in the list")
    return ids


def plan_unregister(model, report, ids):
    """An advisory plan: unregister these replicas, wait, drop what is left.

    Pure. It reads the model and the report and returns data. It runs
    nothing, and it writes nothing. Only the replica ids in the allowlist
    are planned, and each is one of four cases:

    REGISTERED: a Replica item has the id, so it is unregistered first.
    DETACHED: SYNC_ versions carry the id and no Replica item or Sync
        Replica item can own them (the TA 000011719 case).
    REFUSED: SYNC_ versions carry the id, no Replica item has it, and a Sync
        Replica item exists. Esri does not say which number those items'
        versions carry, so the versions may be live.
    ABSENT: nothing in the exports has the id.

    The versions of REGISTERED and DETACHED ids are the drop set. A version
    whose child version is not in the drop set stays: Esri says deleting a
    version as the geodatabase administrator deletes its descendants too.
    The rest are ordered children first, deepest first.
    """
    if model["replicas"] is None or model["tree"] is None:
        raise InputError("the unregister plan needs the replicas and "
                         "version_tree exports")
    tree = model["tree"]
    children = {}
    for key, parent in tree.items():
        if parent is not None:
            children.setdefault(parent, []).append(key)
    by_id = {}
    for v in model["versions"]:
        subject, _ = subject_of(v)
        if subject[0] == REPLICA:
            by_id.setdefault(subject[1], []).append((v["owner"], v["name"]))
    status = {}
    for rid in ids:
        if rid in model["replicas"]:
            status[rid] = REGISTERED
        elif not by_id.get(rid):
            status[rid] = ABSENT
        elif model["sync_replicas"]:
            status[rid] = REFUSED
        else:
            status[rid] = DETACHED_ID

    planned = set(k for rid in ids if status[rid] in (REGISTERED, DETACHED_ID)
                  for k in by_id.get(rid, ()))
    droppable = set(planned)
    changed = True
    while changed:
        changed = False
        for key in sorted(droppable):
            if any(c not in droppable for c in children.get(key, ())):
                droppable.discard(key)
                changed = True

    def depth(key):
        n = 0
        while tree[key] is not None:
            key, n = tree[key], n + 1
        return n

    order = sorted(droppable, key=lambda k: (-depth(k), k))
    entries = []
    for rid in ids:
        mine = by_id.get(rid, ())
        warning = None
        if status[rid] == REGISTERED:
            found = [f for f in report["findings"]
                     if f["key"] == (REPLICA, rid)]
            if not found:
                warning = ("its system versions hold nothing now: they sit "
                           "at DEFAULT's state, or it has none")
            elif not found[0]["blocker"]:
                warning = ("it is not a blocker: %s, within the %d day limit"
                           % (held(found[0]["age"]), report["max_age_days"]))
        entries.append({
            "id": rid, "status": status[rid], "warning": warning,
            "name": (model["replicas"].get(rid) or {}).get("name"),
            "order": [k for k in order if k in mine],
            "blocked": [(k, sorted(c for c in children.get(k, ())
                                   if c not in droppable)[0])
                        for k in sorted(mine)
                        if k in planned and k not in droppable]})

    after = analyse(dict(model, versions=[
        v for v in model["versions"]
        if (v["owner"], v["name"]) not in droppable]),
        report["max_age_days"], report["as_of"])
    return {"ids": list(ids), "entries": entries, "order": order,
            "sync": model["sync_replicas"],
            "floor_now": report["floor"], "floor_after": after["floor"],
            "states": dict(((v["owner"], v["name"]), v["state"])
                           for v in model["versions"])}


def describe_plan(plan):
    """The plan as lines of text."""
    out = ["UNREGISTER PLAN for replica(s) %s"
           % ", ".join(str(i) for i in plan["ids"]),
           "nothing was run and nothing is written: this is advice for a "
           "person to sign for",
           "compress floor now: state %d. If every version listed for "
           "dropping is dropped and a compress runs: state %d."
           % (plan["floor_now"], plan["floor_after"])]
    for e in plan["entries"]:
        name = " '%s'" % e["name"] if e["name"] else ""
        out.extend(["", "replica %d%s: %s" % (e["id"], name, e["status"])])
        if e["status"] == REFUSED:
            out.append("  not planned: no geodatabase replica has this id, "
                       "and %d sync replica(s) exist, whose version ids Esri "
                       "does not document. These SYNC_ versions may be live."
                       % plan["sync"])
            continue
        if e["status"] == ABSENT:
            out.append("  nothing to plan: no Replica item and no SYNC_ "
                       "version has this id")
            continue
        if e["warning"]:
            out.append("  warning: %s" % e["warning"])
        if e["status"] == REGISTERED:
            steps = [
                "Outside this tool: confirm the replica is abandoned and no "
                "partner still syncs with it. Esri says an unregistered "
                "replica cannot synchronize changes.",
                "Unregister it as the replica owner or the geodatabase "
                "administrator: UnregisterReplica(<geodatabase>, \"%s\"). "
                "Esri says a replica name must be fully qualified, such as "
                "owner.name, or you can pass the replica ID.%s"
                % (e["name"], "" if "." in e["name"] else
                   " This name has no owner part: check it first."),
                "Esri says the tool does not delete the replica's version. "
                "Wait for the release to end: poll the count of this "
                "replica's SYNC_ versions every %d s, go on when it is "
                "unchanged for %d polls in a row, and give up after %d s. "
                "Esri gives no such numbers."
                % (DRAIN_POLL_SECONDS, DRAIN_STABLE_POLLS,
                   DRAIN_TIMEOUT_SECONDS),
                "Export the tables again and run this plan again with the "
                "same list. A replica with no Replica item is DETACHED, and "
                "the versions it left are listed in drop order."]
        else:
            steps = ["Outside this tool: read Esri TA 000011719 and confirm "
                     "no live replica owns this id."]
        steps.append("Versions of this replica in the exports now are "
                     "numbered in the DROP ORDER list below. Drop only "
                     "those still there at the last export."
                     if e["order"] else
                     "No version of this replica can be dropped from the "
                     "exports now.")
        for n, text in enumerate(steps, 1):
            out.append("  %d. %s" % (n, text))
        for (owner, vname), (c_owner, c_name) in e["blocked"]:
            out.append("       kept: %s (owner %s) has child version %s.%s, "
                       "which is not dropped. Esri: deleting a version as the "
                       "geodatabase administrator deletes its descendant "
                       "versions too." % (vname, owner, c_owner, c_name))
    # One list for every replica: a version of one replica can be the child
    # of a version of another, so only the global order is leaf-first.
    if plan["order"]:
        owner_of = dict((k, e["id"]) for e in plan["entries"]
                        for k in e["order"])
        out.extend(["", "DROP ORDER (children first, deepest first, across "
                    "every replica above)"])
        for n, key in enumerate(plan["order"], 1):
            out.append("  drop %d: %s (owner %s, state %d, replica %d)"
                       % (n, key[1], key[0], plan["states"][key],
                          owner_of[key]))
    out.extend(["", "Esri documents Delete Version for traditional versions. "
                "Whether it can address a replica system version is not "
                "documented, and this tool did not test it."])
    return out


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

    only_orphan = build(**tree([("DEFAULT", "SDE", 5)], idle + [
        (7, 7, 5, "")], [], None))
    verdicts = [x for x in describe(only_orphan, analyse(
        only_orphan, 30, datetime.datetime(2026, 9, 1)))
        if x.startswith("VERDICT")]
    check(len(verdicts) == 1 and "all orphaned states" in verdicts[0]
          and "will not fold" not in verdicts[0],
          "a run whose only blocker is an orphaned lineage does not predict "
          "that compress will not fold  <-- pinned defect")

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
    check([l for l in text.splitlines() if l.startswith("VERDICT")]
          == ["VERDICT: nothing in the exports holds the floor past the 30 "
              "day limit."] and "holds no rows" not in text,
          "a clean report has that one VERDICT line and no blocker count  "
          "<-- pinned defect")
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

    # ---- --exit-on: what a daily monitor reads
    check(gate(report) == gate(report, "blocker") == 1
          and gate(report, "any") == 1 and gate(report, "never") == 0,
          "blockers exit 1 under blocker and any, and 0 under never")
    check(gate(young_sync) == 0 and gate(young_sync, "any") == 1
          and gate(young_sync, "never") == 0,
          "a holder under the age limit exits 0 by default and 1 under any")
    check(gate(clean, "any") == 0 and gate(clean, "never") == 0,
          "a clean run exits 0 under every level")
    raises(lambda: gate(report, "blockers"),
           "an exit level the tool does not know is refused, not read as "
           "the default  <-- pinned defect", ValueError)

    # ---- the stranded sibling-state view, on synthetic rows
    sx = strand_fixture()
    sm = build(**sx)
    sr = analyse(sm)
    strand = dict((f["key"][1], f) for f in sr["findings"]
                  if f["kind"] == STRANDED)

    def reached(model, sid):
        """True when a version points at sid or has it as an ancestor.
        It uses ancestors(), not the fork walk that analyse() uses."""
        return any(v["state"] == sid or sid in ancestors(model, v["state"])
                   for v in model["versions"])

    check(sorted(strand) == [7, 13],
          "a state no version reaches, with a later state of the same "
          "parent and owner, is stranded  <-- pinned defect")
    check(strand[7]["blocker"] and strand[7]["weight"] == 120
          and strand[7]["later"] == [(8, True)] and strand[7]["parent"] == 5
          and strand[7]["owner"] == "ED",
          "and it carries its 120 delta rows, its parent, its owner and the "
          "later sibling a version kept")
    check(all(not reached(sm, sid) for sid in strand),
          "no version points at a stranded state or has it as an ancestor, "
          "proved with the ancestor walk and not the fork walk")
    check(reached(sm, 8) and reached(sm, 16) and reached(sm, 5),
          "and the same proof finds the kept sibling, an ancestor and "
          "DEFAULT's state reached, so it is not vacuous")
    check(not strand[13]["blocker"] and strand[13]["weight"] == 0,
          "a stranded state with no delta rows is listed, not a blocker")
    check(strand[7]["age"].days == 4 and strand[7]["fork"] == 5,
          "a stranded state is aged from its own creation time and forks "
          "where its lineage leaves DEFAULT's")
    check(set(sid for f in sr["findings"] if f["kind"] == ORPHANED
              for sid in f["states"]) == set([7, 11, 13, 21]),
          "the four unreferenced states are still ORPHANED_STATES too: the "
          "view adds to the report and replaces nothing")
    check(11 not in strand and 21 not in strand,
          "a lost state with no later sibling of its owner, or only an "
          "earlier one, is not stranded  <-- pinned defect")
    check(16 not in strand and 5 not in strand,
          "a state a version reaches as an ancestor is not stranded, "
          "whatever its siblings  <-- pinned defect")
    s_lines = describe(sm, sr)
    s_text = "\n".join(s_lines)
    check("STRANDED SIBLING STATES (2)" in s_text
          and "state 7, owner ED, parent state 5" in s_text
          and "no version points at state 7 or reaches it through its "
          "lineage" in s_text
          and "later state(s) with the same parent and owner: 8 (a version "
          "reaches it)" in s_text
          and "delta rows in this state: 120; created 4 day(s) before the "
          "as-of time" in s_text
          and "extract these rows first" in s_text,
          "the report names the state, the owner, the parent, the later "
          "sibling and the rows")
    check("and this one holds no delta rows" in s_text
          and s_lines[-1].startswith("VERDICT: 1 stranded state(s)")
          and not any(line.startswith("VERDICT: nothing") for line in s_lines),
          "a stranded state with no rows says so, and the verdict counts "
          "only the one that holds rows")
    check([l for l in s_lines if l.startswith("VERDICT")]
          == ["VERDICT: 1 stranded state(s) hold delta rows that no version "
              "reaches. The next compress removes states no version "
              "references: extract the rows first."],
          "a report with stranded blockers only has no blocker-count "
          "VERDICT line  <-- pinned defect")
    check(gate(sr) == 1 and gate(sr, "never") == 0
          and gate(analyse(build(**dict(sx, deltas=[
              r for r in sx["deltas"] if r["STATE_ID"] != "7"])))) == 0,
          "stranded rows exit 1, and a stranded state with no rows exits 0")
    check([f["kind"] for f in analyse(build(**sx))["findings"]
           if f["blocker"]] == [STRANDED],
          "the view's only blocker here is the stranded state")
    kept_low = analyse(build(**strand_fixture(
        versions=STRAND_VERSIONS + [("ED_OLD", "ED", 7)])))
    check(sorted(f["key"][1] for f in kept_low["findings"]
                 if f["kind"] == STRANDED) == [13],
          "a version that points at the lower state keeps it from being "
          "stranded  <-- pinned defect")
    child = analyse(build(**strand_fixture(
        states=STRAND_STATES + [(10, 7, 7, "2026-09-10 08:01:00")],
        owners=dict(list(STRAND_OWNERS.items()) + [(10, "ED")]),
        versions=STRAND_VERSIONS + [("ED_CHILD", "ED", 10)])))
    check(sorted(f["key"][1] for f in child["findings"]
                 if f["kind"] == STRANDED) == [13],
          "a version that points at a child of the lower state keeps it "
          "from being stranded  <-- pinned defect")
    other = analyse(build(**strand_fixture(
        owners=dict(list(STRAND_OWNERS.items()) + [(8, "OTHER2")]))))
    check(sorted(f["key"][1] for f in other["findings"]
                 if f["kind"] == STRANDED) == [13],
          "a later state of another owner is not a sibling  <-- pinned "
          "defect")
    kept_lost = analyse(build(**strand_fixture(
        versions=[v for v in STRAND_VERSIONS if v[0] != "ED_WO"])))
    check([f["later"] for f in kept_lost["findings"]
           if f["kind"] == STRANDED and f["key"][1] == 7] == [[(8, False)]],
          "a later sibling that no version reaches is reported as such")
    check("no version reaches it)" in "\n".join(describe(
        build(**strand_fixture(
            versions=[v for v in STRAND_VERSIONS if v[0] != "ED_WO"])),
        kept_lost)),
          "and the report words it that way")
    undated = analyse(build(**strand_fixture(states=[
        (s, ln, p, "" if s == 7 else c) for s, ln, p, c in STRAND_STATES])))
    check([f["age"] for f in undated["findings"]
           if f["kind"] == STRANDED and f["key"][1] == 7] == [None]
          and "created at an unknown time" in "\n".join(describe(
              build(**strand_fixture(states=[
                  (s, ln, p, "" if s == 7 else c)
                  for s, ln, p, c in STRAND_STATES])), undated)),
          "a stranded state with no creation time says so")
    plain = fx_without(sx, "state_parents")
    check(not any(f["kind"] == STRANDED
                  for f in analyse(build(**plain))["findings"])
          and "STRANDED" not in "\n".join(describe(
              build(**plain), analyse(build(**plain)))),
          "without the state_parents export nothing is stranded and the "
          "report is the one it was")
    one_row = analyse(build(**dict(sx, deltas=[
        dict(r, DELTA_ROWS="1") if r["STATE_ID"] == "7" else r
        for r in sx["deltas"]])))
    check([f["blocker"] for f in one_row["findings"]
           if f["kind"] == STRANDED and f["key"][1] == 7] == [True]
          and gate(one_row) == 1,
          "a stranded state with exactly one delta row is a blocker  <-- "
          "pinned defect")
    raises(lambda: analyse(build(**dict(sx, deltas=[]))),
           "a header-only deltas export with stranded candidates is refused, "
           "not read as no rows  <-- pinned defect")
    raises(lambda: analyse(build(**fx_without(sx, "deltas"))),
           "the stranded view without a deltas export is refused: it counts "
           "rows  <-- pinned defect")
    raises(lambda: build(**dict(sx, state_parents=sx["state_parents"][1:])),
           "a state_parents export that lacks a state is refused")
    raises(lambda: build(**dict(sx, state_parents=sx["state_parents"]
                                + [dict(sx["state_parents"][0],
                                        STATE_ID="99")])),
           "a state_parents export that holds a state the states export "
           "does not is refused")
    raises(lambda: build(**dict(sx, state_parents=sx["state_parents"]
                                + sx["state_parents"][:1])),
           "a state_parents export that repeats a state is refused  <-- "
           "pinned defect")
    raises(lambda: build(**dict(sx, state_parents=[
        dict(r, PARENT_STATE_ID="404") if r["STATE_ID"] == "7" else r
        for r in sx["state_parents"]])),
           "a parent state that the states export does not hold is refused")
    raises(lambda: build(**dict(sx, state_parents=[
        dict(r, PARENT_STATE_ID="8") if r["STATE_ID"] == "7" else r
        for r in sx["state_parents"]])),
           "a parent state that is in the states export but not in the "
           "state's lineage is refused  <-- pinned defect")
    raises(lambda: build(**dict(sx, state_parents=[
        dict(r, PARENT_STATE_ID="x") if r["STATE_ID"] == "7" else r
        for r in sx["state_parents"]])),
           "a parent state id that is not a number is refused")
    check(len(build(**dict(sx, state_parents=[
        dict(r, PARENT_STATE_ID="-1") if r["STATE_ID"] == "0" else r
        for r in sx["state_parents"]]))["parents"]) == 13,
          "state 0's own parent is not read, because Esri does not say what "
          "it holds")
    check(build(**dict(sx, state_parents=[
        dict(r, OWNER="E\nD") for r in sx["state_parents"]]))["parents"][7]
          == (5, "E\\nD"),
          "an owner name from the export is escaped like any other name")

    # ---- the unregister plan, on synthetic rows
    px = plan_fixture()
    pm = build(**px)
    pr = analyse(pm)

    def plan_of(ids, fx=px):
        model = build(**fx)
        return plan_unregister(model, analyse(model), ids), model

    plan, _ = plan_of([56, 57, 48])
    st = dict((e["id"], e) for e in plan["entries"])
    check([e["status"] for e in plan["entries"]]
          == [REGISTERED, REGISTERED, DETACHED_ID],
          "a replica with a Replica item is REGISTERED, one with only SYNC_ "
          "versions is DETACHED  <-- pinned defect")
    check(st[56]["order"] == [("SDE", "SYNC_RECEIVE_56_0"),
                              ("SDE", "SYNC_SEND_56_0")],
          "a child version is dropped before its parent  <-- pinned defect")
    check(plan["order"] == [("SDE", "SYNC_RECEIVE_56_0"),
                            ("SDE", "SYNC_SEND_48_2"),
                            ("SDE", "SYNC_SEND_56_0"),
                            ("SDE", "SYNC_SEND_57_0")]
          and plan["floor_now"] == 17 and plan["floor_after"] == 50,
          "the plan predicts the floor moves from 17 to 50 when the three "
          "replicas' versions are gone")
    check(st[56]["warning"] is None and st[48]["warning"] is None,
          "a stalled replica and a detached one carry no warning")
    p_lines = describe_plan(plan)
    p_text = "\n".join(p_lines)
    check(p_lines[0] == "UNREGISTER PLAN for replica(s) 56, 57, 48"
          and "nothing was run and nothing is written" in p_text,
          "the plan says in its first lines that nothing was run")
    check("UnregisterReplica(<geodatabase>, \"parcels_to_web\")" in p_text
          and "Esri says a replica name must be fully qualified, such as "
          "owner.name, or you can pass the replica ID. "
          "This name has no owner part: check it first." in p_text,
          "the unregister step names the replica and warns about a name "
          "with no owner part")
    check("poll the count of this replica's SYNC_ versions every 30 s, go "
          "on when it is unchanged for 3 polls in a row, and give up after "
          "1800 s. Esri gives no such numbers." in p_text,
          "the drain wait states its poll rule and says the numbers are not "
          "Esri's")
    check([l for l in p_lines if l.strip().startswith("drop ")]
          == ["  drop 1: SYNC_RECEIVE_56_0 (owner SDE, state 17, replica 56)",
              "  drop 2: SYNC_SEND_48_2 (owner SDE, state 50, replica 48)",
              "  drop 3: SYNC_SEND_56_0 (owner SDE, state 17, replica 56)",
              "  drop 4: SYNC_SEND_57_0 (owner SDE, state 18, replica 57)"]
          and "Outside this tool: read Esri TA 000011719" in p_text,
          "the drops are one list numbered children first, and a detached "
          "replica is sent to TA 000011719")
    check(p_text.index("replica 56") < p_text.index("replica 57")
          < p_text.index("replica 48"),
          "the entries follow the order of the allowlist")
    check("Whether it can address a replica system version is not "
          "documented, and this tool did not test it." in p_lines[-1],
          "the plan ends by saying Delete Version on a replica system "
          "version is untested")
    check(all(line.isprintable() for line in p_lines),
          "the plan prints only printable characters")

    dotted = plan_of([56], dict(px, replicas=[
        dict(r, NAME="SDE." + r["NAME"]) for r in px["replicas"]]))[0]
    check("This name has no owner part" not in "\n".join(
        describe_plan(dotted)),
          "a replica name with an owner part gets no such warning")

    quiet70, _ = plan_of([70, 99, 1])
    check([e["status"] for e in quiet70["entries"]]
          == [REGISTERED, REGISTERED, ABSENT]
          and quiet70["entries"][0]["warning"]
          == "it is not a blocker: held for 0 day(s), within the 30 day "
          "limit"
          and quiet70["entries"][1]["warning"]
          == "its system versions hold nothing now: they sit at DEFAULT's "
          "state, or it has none",
          "a live replica and an empty one are planned with a warning, and "
          "an id nothing has is ABSENT")
    q_text = "\n".join(describe_plan(quiet70))
    check(q_text.count("  warning: ") == 2
          and "  warning: it is not a blocker: held for 0 day(s), within the "
          "30 day limit" in q_text
          and "  warning: its system versions hold nothing now: they sit at "
          "DEFAULT's state, or it has none" in q_text
          and "TA 000011719" not in q_text,
          "the warnings are printed, and an ABSENT replica gets no step "
          "list  <-- pinned defect")
    check("nothing to plan: no Replica item and no SYNC_ version has this "
          "id" in q_text
          and "No version of this replica can be dropped from the exports "
          "now." in q_text
          and quiet70["floor_after"] == quiet70["floor_now"] == 17,
          "ABSENT and empty entries list no drop, and the floor stays")

    sync_fx = dict(px, replicas=[
        dict(r, ITEM_TYPE="Sync Replica", ID="-1", NAME="collab")
        if r["ID"] == "56" else r for r in px["replicas"]])
    refused, _ = plan_of([56, 48, 70], sync_fx)
    check([e["status"] for e in refused["entries"]]
          == [REFUSED, REFUSED, REGISTERED]
          and refused["order"] == [("SDE", "SYNC_SEND_70_12")],
          "beside a Sync Replica item an unmatched SYNC_ id is REFUSED, and "
          "none of its versions is listed for dropping  <-- pinned defect")
    r_text = "\n".join(describe_plan(refused))
    check("not planned: no geodatabase replica has this id, and 1 sync "
          "replica(s) exist" in r_text and "may be live" in r_text
          and "TA 000011719" not in r_text,
          "and the plan says why, and a REFUSED replica gets no step list "
          "that leads to a drop  <-- pinned defect")

    # OLD_DESIGN (version 6) is a child of SYNC_SEND_48_2 (version 5).
    blocked, _ = plan_of([48], plan_fixture({"OLD_DESIGN": 5}))
    check(blocked["entries"][0]["order"] == []
          and blocked["entries"][0]["blocked"]
          == [(("SDE", "SYNC_SEND_48_2"), ("EDITOR1", "OLD_DESIGN"))]
          and blocked["floor_after"] == 17,
          "a replica version with an ordinary child version is kept, and the "
          "floor does not move  <-- pinned defect")
    b_text = "\n".join(describe_plan(blocked))
    check("kept: SYNC_SEND_48_2 (owner SDE) has child version "
          "EDITOR1.OLD_DESIGN, which is not dropped." in b_text
          and "deletes its descendant versions too" in b_text,
          "and the plan names the child and the Esri rule")
    # A chain: 56's SEND has the child RECEIVE (in the set) and OLD_DESIGN.
    chain, _ = plan_of([56], plan_fixture({"OLD_DESIGN": 2}))
    check(chain["order"] == [("SDE", "SYNC_RECEIVE_56_0")]
          and chain["entries"][0]["blocked"]
          == [(("SDE", "SYNC_SEND_56_0"), ("EDITOR1", "OLD_DESIGN"))],
          "a child version that can be dropped is, and its blocked parent "
          "is kept")
    trans, _ = plan_of([56, 57], plan_fixture({"SYNC_SEND_57_0": 3,
                                               "OLD_DESIGN": 4}))
    check(trans["order"] == [] and trans["floor_after"] == 17
          and len(trans["entries"][0]["blocked"]) == 2
          and len(trans["entries"][1]["blocked"]) == 1,
          "a version whose child stays is kept, and so is every ancestor "
          "of it in the drop set  <-- pinned defect")
    deep, _ = plan_of([56, 57], plan_fixture({"SYNC_SEND_57_0": 3}))
    check(deep["order"] == [("SDE", "SYNC_SEND_57_0"),
                            ("SDE", "SYNC_RECEIVE_56_0"),
                            ("SDE", "SYNC_SEND_56_0")]
          and deep["entries"][0]["order"] == [("SDE", "SYNC_RECEIVE_56_0"),
                                              ("SDE", "SYNC_SEND_56_0")],
          "drops are ordered deepest first across replicas, and each entry "
          "keeps that order  <-- pinned defect")
    check([l for l in describe_plan(deep) if l.strip().startswith("drop ")]
          == ["  drop 1: SYNC_SEND_57_0 (owner SDE, state 18, replica 57)",
              "  drop 2: SYNC_RECEIVE_56_0 (owner SDE, state 17, replica 56)",
              "  drop 3: SYNC_SEND_56_0 (owner SDE, state 17, replica 56)"],
          "the printed plan lists a later replica's child version before "
          "an earlier replica's parent version  <-- pinned defect")

    check(parse_ids("56, 57") == [56, 57] and parse_ids("0") == [0],
          "an id list is digits separated by commas")
    for bad in ("", "all", "56,", "56,x", "-1", "56,-1", "5.0", "\u00b2", "\u0663", "56,56"):
        raises(lambda: parse_ids(bad),
               "the id list %s is refused  <-- pinned defect" % ascii(bad),
               ValueError)
    raises(lambda: plan_unregister(build(**fx_without(px, "version_tree")),
                                   pr, [56]),
           "a plan without the version_tree export is refused")
    raises(lambda: plan_unregister(build(**fx_without(px, "replicas")),
                                   pr, [56]),
           "a plan without the replicas export is refused")

    # ---- the version_tree export
    check(pm["tree"][("SDE", "SYNC_RECEIVE_56_0")] == ("SDE", "SYNC_SEND_56_0")
          and pm["tree"][("SDE", "DEFAULT")] is None,
          "the tree maps a version to its parent, and DEFAULT to none")
    nulls = build(**dict(px, version_tree=[
        dict(r, PARENT_VERSION_ID=None) if r["VERSION_ID"] == "1" else r
        for r in px["version_tree"]]))
    check(nulls["tree"][("SDE", "DEFAULT")] is None,
          "a JSON null parent is a root")
    raises(lambda: build(**dict(px, version_tree=px["version_tree"][1:])),
           "a version_tree export that lacks a version is refused")
    raises(lambda: build(**dict(px, version_tree=px["version_tree"][:-1])),
           "a version_tree export that lacks a leaf version is refused, "
           "though every parent link is held  <-- pinned defect")
    raises(lambda: build(**dict(px, version_tree=px["version_tree"]
                                + px["version_tree"][:1])),
           "a version_tree export that repeats a version is refused  <-- "
           "pinned defect")
    raises(lambda: build(**dict(px, version_tree=px["version_tree"][:-1]
                                + [dict(px["version_tree"][-1],
                                        VERSION_ID="8")])),
           "a version_tree export that gives two versions one id is refused")
    raises(lambda: build(**dict(px, version_tree=[
        dict(r, PARENT_VERSION_ID="404") if r["VERSION_ID"] == "2" else r
        for r in px["version_tree"]])),
           "a parent version id that no row holds is refused")
    raises(lambda: build(**dict(px, version_tree=[
        dict(r, PARENT_VERSION_ID="x") if r["VERSION_ID"] == "2" else r
        for r in px["version_tree"]])),
           "a parent version id that is not a number is refused")
    raises(lambda: build(**dict(px, version_tree=[
        dict(r, PARENT_VERSION_ID="3") if r["VERSION_ID"] == "2" else r
        for r in px["version_tree"]])),
           "parent links that loop are refused, not walked for ever  <-- "
           "pinned defect")

    # ---- the SQL the README tells the user to run
    for dbms in ("sqlserver", "postgresql", "oracle"):
        sql = SQL[dbms]
        check(all(("-- %s.csv" % k) in sql for k in REQUIRED),
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
        check(sql.count("COUNT(*) OVER () AS row_count") == len(REQUIRED),
              "the %s SQL counts the rows of all %d exports, so a lost row "
              "is caught" % (dbms, len(REQUIRED)))

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
        for body in ("{}", "[1]"):
            with open(notlist, "w") as handle:
                handle.write(body)
            code, _, err = run(base[:4] + ["--lineages", notlist])
            check(code == 2 and "array of objects" in err,
                  "an empty JSON object or an array of non-objects exits 2 "
                  "(%s)  <-- pinned defect" % body)
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
              "a header-only replicas export is read as no replicas, not "
              "exit 2  <-- pinned defect")
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
        try:
            code, _, _ = run(full + ["--report", report_path, "--ap"])
        except SystemExit as exc:
            code = exc.code
        check(code == 2 and not os.path.exists(report_path),
              "a unique prefix of --apply (--ap) is refused by the parser, "
              "not read as --apply  <-- pinned defect")
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

        # ---- --exit-on, through the command line and through a real process
        code, out, _ = run(full + ["--exit-on", "never"])
        check(code == 0 and out.splitlines() == DISASTER_REPORT,
              "--exit-on never prints the same report and exits 0 with five "
              "blockers")
        check(run(full + ["--exit-on", "blocker"])[0] == 1
              and run(full + ["--exit-on", "any"])[0] == 1,
              "--exit-on blocker and any exit 1 on the disaster")
        solo = os.path.join(tmp, "solo")
        os.mkdir(solo)
        solo_fx = fx_without(stamped(tree(
            [("DEFAULT", "SDE", 5)],
            [(0, 0, 0, "2026-01-01 00:00:00"), (5, 0, 0, "2026-08-30 00:00:00"),
             (6, 0, 5, "2026-08-31 00:00:00"), (9, 0, 6, "2026-09-01 00:00:00")],
            [("SYNC_SEND_12_0", "SDE", 5)]), "2026-09-02 00:00:00"),
            "replicas")
        sp = write_fixture(solo, solo_fx)
        solo_args = ["--versions", sp["versions"], "--states", sp["states"],
                     "--lineages", sp["lineages"]]
        check([run(solo_args + ["--exit-on", level])[0]
               for level in ("blocker", "any", "never")] == [0, 1, 0]
              and run(solo_args)[0] == 0,
              "a holder under the age limit exits 0 by default and under "
              "never, and 1 under any")
        flat = os.path.join(tmp, "flat")
        os.mkdir(flat)
        flat_fx = stamped(tree(
            [("DEFAULT", "SDE", 5)],
            [(0, 0, 0, "2026-09-01 00:00:00"), (5, 0, 0, "2026-09-01 00:00:00")],
            [], None), "2026-09-02 00:00:00")
        fp = write_fixture(flat, fx_without(flat_fx, "replicas"))
        check([run(["--versions", fp["versions"], "--states", fp["states"],
                    "--lineages", fp["lineages"], "--exit-on", level])[0]
               for level in EXIT_ON] == [0, 0, 0],
              "a clean run exits 0 under every --exit-on level")
        code, out, err = run(base[:4] + ["--lineages", os.path.join(
            tmp, "absent.csv"), "--exit-on", "never"])
        check(code == 2 and out == "" and "absent.csv" in err,
              "--exit-on never does not turn a failure into exit 0  <-- "
              "pinned defect")
        try:
            code, _, _ = run(full + ["--exit-on", "bogus"])
        except SystemExit as exc:
            code = exc.code
        check(code == 2, "an unknown --exit-on level exits 2")
        for flag in (["--exit", "never"], ["--unreg", "56"],
                     ["--state-p", "x"], ["--version-t", "x"],
                     ["--rep", "x"]):
            try:
                code, _, _ = run(full + flag)
            except SystemExit as exc:
                code = exc.code
            check(code == 2,
                  "the prefix %s is refused by the parser, not read as a "
                  "flag  <-- pinned defect" % flag[0])
        check(_parse([])[0].allow_abbrev is False,
              "the parser is built with allow_abbrev=False")
        for extra in (["--exit-on", "any"], ["--unregister-plan", "56"],
                      ["--state-parents", "x"], ["--version-tree", "x"]):
            code, _, err = run(["--self-test"] + extra)
            check(code == 2 and "--self-test takes no other flag" in err,
                  "--self-test %s exits 2, not ignored  <-- pinned defect"
                  % extra[0])
            code, _, err = run(["--sql", "oracle"] + extra)
            check(code == 2 and "--sql takes no other flag" in err,
                  "--sql %s exits 2, not ignored  <-- pinned defect"
                  % extra[0])

        here = os.path.abspath(__file__)

        def process(args):
            """The exit code of a real run of this file."""
            return subprocess.run([sys.executable, "-B", here] + args,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE).returncode

        check([process(full), process(full + ["--exit-on", "any"]),
               process(full + ["--exit-on", "never"]), process(solo_args),
               process(base[:4] + ["--lineages", os.path.join(
                   tmp, "absent.csv")]),
               process(full + ["--exit-on", "bogus"])]
              == [1, 1, 0, 0, 2, 2],
              "a real process exits 1 on blockers, 0 under never, 0 on a "
              "holder under the limit and 2 on a failure")

        # ---- the stranded view, through the command line
        sdir = os.path.join(tmp, "strand")
        os.mkdir(sdir)
        spaths = write_fixture(sdir, sx)
        sargs = ["--versions", spaths["versions"], "--states", spaths["states"],
                 "--lineages", spaths["lineages"], "--deltas",
                 spaths["deltas"], "--state-parents", spaths["state_parents"]]
        code, out, _ = run(sargs)
        check(code == 1 and out.splitlines() == describe(sm, sr),
              "the stranded view end to end exits 1 and prints what the "
              "core describes")
        check(run(sargs + ["--exit-on", "never"])[0] == 0,
              "and exits 0 under --exit-on never")
        code, _, err = run(sargs[:6] + ["--state-parents",
                                        spaths["state_parents"]])
        check(code == 2 and "needs the deltas export" in err,
              "--state-parents without --deltas exits 2  <-- pinned defect")
        jspaths = write_fixture(sdir, sx, as_json=True)
        code, out, _ = run(["--versions", jspaths["versions"], "--states",
                            jspaths["states"], "--lineages",
                            jspaths["lineages"], "--deltas",
                            jspaths["deltas"], "--state-parents",
                            jspaths["state_parents"]])
        check(code == 1 and out.splitlines() == describe(sm, sr),
              "the same exports as JSON give the same stranded view")
        code, _, err = run(sargs[:8] + ["--state-parents", export(
            "sp_nocount.csv", "state_id,parent_state_id,owner\n0,0,SDE\n")])
        check(code == 2 and "state_parents export has no column row_count"
              in err,
              "a state_parents export without row_count exits 2")
        sdel = sargs[:7] + [export("deltas_header.csv",
                                   "state_id,delta_rows,row_count\n")]
        code, out, err = run(sdel + sargs[8:])
        check(code == 2 and "deltas export holds no rows" in err
              and "VERDICT" not in out,
              "a header-only deltas export with the stranded view exits 2, "
              "not 0  <-- pinned defect")
        code, out, err = run(sdel)
        check(code == 0 and "delta rows compress cannot fold: 0 (the deltas "
              "export holds no rows: check that step 1 of the deltas SQL "
              "returned lines)" in out,
              "and without --state-parents it is read as zero rows, and the "
              "report says the export held none  <-- pinned defect")

        # ---- the unregister plan, through the command line
        pdir = os.path.join(tmp, "plan")
        os.mkdir(pdir)
        ppaths = write_fixture(pdir, px)
        pargs = ["--versions", ppaths["versions"], "--states",
                 ppaths["states"], "--lineages", ppaths["lineages"],
                 "--replicas", ppaths["replicas"], "--replica-log",
                 ppaths["replica_log"], "--deltas", ppaths["deltas"],
                 "--version-tree", ppaths["version_tree"]]
        listing = sorted(os.listdir(pdir))
        loaded = set(sys.modules)
        code, out, _ = run(pargs + ["--unregister-plan", "56,57,48"])
        check(code == 1 and out.splitlines() == describe(pm, pr) + [""]
              + describe_plan(plan_unregister(pm, pr, [56, 57, 48])),
              "the plan end to end follows the report, and the exit code is "
              "still the report's")
        check(sorted(os.listdir(pdir)) == listing
              and not (set(sys.modules) - loaded) & set(
                  ["arcpy", "pyodbc", "socket"])
              and "arcpy" not in sys.modules and "pyodbc" not in sys.modules,
              "the plan wrote no file and loaded no arcpy, pyodbc or "
              "socket module  <-- pinned defect")
        check(run(pargs + ["--unregister-plan", "56", "--exit-on",
                           "never"])[0] == 0,
              "the plan honours --exit-on never")
        for flags, want in (
                (["--unregister-plan", "56"] + pargs[:-2],
                 "--unregister-plan needs --replicas and --version-tree"),
                (["--unregister-plan", "56"] + pargs[:6] + pargs[10:],
                 "--unregister-plan needs --replicas and --version-tree"),
                (["--unregister-plan", "all"] + pargs, "digits separated by "
                 "commas"),
                (["--unregister-plan", ""] + pargs, "digits separated by "
                 "commas"),
                (["--unregister-plan", "56,56"] + pargs, "appears twice")):
            code, out, err = run(flags)
            check(code == 2 and out == "" and want in err,
                  "--unregister-plan %r is refused with exit 2: %s"
                  % (flags[1], want))
        plan_report = os.path.join(pdir, "plan.txt")
        code, out, _ = run(pargs + ["--unregister-plan", "56", "--report",
                                    plan_report])
        check(not os.path.exists(plan_report) and "was not written" in out,
              "--report without --apply does not write the plan either")
        run(pargs + ["--unregister-plan", "56", "--report", plan_report,
                     "--apply"])
        with open(plan_report) as handle:
            written_plan = handle.read()
        check(sorted(os.listdir(pdir)) == sorted(listing + ["plan.txt"])
              and "UNREGISTER PLAN for replica(s) 56" in written_plan,
              "with --apply the plan goes into the report file, and that is "
              "the only file written")

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
                   "NAME": "assets_field"},
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


def fx_without(fx, kind):
    """Canned rows with one export left out."""
    return dict((k, v) for k, v in fx.items() if k != kind)


def parents(states, owners):
    """Canned state_parents rows from tree()'s state tuples."""
    return [{"STATE_ID": str(s), "PARENT_STATE_ID": str(p),
             "OWNER": owners.get(s, "SDE")} for s, _, p, _ in states]


# Every state below is a child of state 5, DEFAULT's, except 17. Each editor
# saved once and a version kept the later state, so 7 and 13 are the earlier
# saves that no version reaches. 11 and 21 are unreached too, but each is the
# newest state of its owner. 16 is reached through its child 17.
STRAND_STATES = [
    (0, 0, 0, "2026-01-01 00:00:00"), (5, 0, 0, "2026-09-01 00:00:00"),
    (7, 7, 5, "2026-09-10 08:00:00"), (8, 8, 5, "2026-09-10 08:03:00"),
    (9, 9, 5, "2026-09-11 09:00:00"), (11, 11, 5, "2026-09-12 10:00:00"),
    (13, 13, 5, "2026-09-13 10:00:00"), (14, 14, 5, "2026-09-13 10:02:00"),
    (16, 16, 5, "2026-09-12 11:00:00"), (17, 16, 16, "2026-09-12 11:05:00"),
    (18, 18, 5, "2026-09-12 12:00:00"), (20, 20, 5, "2026-09-14 08:00:00"),
    (21, 21, 5, "2026-09-14 08:10:00")]
STRAND_OWNERS = {7: "ED", 8: "ED", 9: "OTHER", 11: "OTHER", 13: "MAP",
                 14: "MAP", 16: "PX", 17: "PX", 18: "PX", 20: "QA",
                 21: "QA"}
STRAND_VERSIONS = [("ED_WO", "ED", 8), ("OTHER_V", "OTHER", 9),
                   ("MAP_V", "MAP", 14), ("CHILD_V", "PX", 17),
                   ("PX_NEW", "PX", 18), ("QA_V", "QA", 20)]


def strand_fixture(states=STRAND_STATES, owners=STRAND_OWNERS,
                   versions=STRAND_VERSIONS):
    """Synthetic rows for the stranded sibling-state view."""
    fx = fx_without(stamped(tree([("DEFAULT", "SDE", 5)], states,
                                 list(versions)), "2026-09-15 00:00:00"),
                    "replicas")
    fx["deltas"] = [{"STATE_ID": str(s), "DELTA_ROWS": str(n)} for s, n in (
        (7, 120), (8, 3), (9, 40), (11, 7), (13, 0), (14, 2), (17, 5),
        (21, 1))]
    fx["state_parents"] = parents(states, owners)
    return fx


# (owner, name, version id, parent version id) for fixture()'s nine versions.
# SYNC_RECEIVE_56_0 is a child of SYNC_SEND_56_0. The rest are children of
# DEFAULT. Real replica system versions may nest differently: this is a
# synthetic shape that exercises the ordering.
PLAN_TREE = [("SDE", "DEFAULT", 1, None), ("SDE", "SYNC_SEND_56_0", 2, 1),
             ("SDE", "SYNC_RECEIVE_56_0", 3, 2), ("SDE", "SYNC_SEND_57_0", 4, 1),
             ("SDE", "SYNC_SEND_48_2", 5, 1), ("EDITOR1", "OLD_DESIGN", 6, 1),
             ("SDE", "SYNC_SEND_70_12", 7, 1), ("EDITOR2", "WO_NEW", 8, 1),
             ("EDITOR3", "QA", 9, 1)]


def plan_fixture(parent_ids=None):
    """The disaster fixture plus its version tree. parent_ids maps a version
    name to another parent version id."""
    fx = fixture()
    fx["version_tree"] = [
        {"OWNER": o, "NAME": n, "VERSION_ID": str(i),
         "PARENT_VERSION_ID": "" if p is None else str(
             (parent_ids or {}).get(n, p))} for o, n, i, p in PLAN_TREE]
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
    "  REPLICA                    replica 70 'assets_field'",
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
    # allow_abbrev=False: a unique prefix of --apply must not write the report.
    ap = argparse.ArgumentParser(
        prog="compressfloor.py", allow_abbrev=False,
        description="Name what holds an enterprise geodatabase's compress "
                    "floor, from exported system tables.",
        epilog="Exit 0 no finding of the --exit-on level, 1 a finding, 2 "
               "the input could not be read. Only --report is ever "
               "written, and only with --apply.")
    ap.add_argument("--versions", help="the version table export")
    ap.add_argument("--states", help="the state table export")
    ap.add_argument("--lineages", help="the state lineage table export")
    ap.add_argument("--replicas", help="the replica items export (optional)")
    ap.add_argument("--replica-log", dest="replica_log",
                    help="the replica log export (optional, needs "
                         "--replicas)")
    ap.add_argument("--deltas", help="delta rows per state (optional)")
    ap.add_argument("--state-parents", dest="state_parents",
                    help="the parent state and owner of each state (optional, "
                         "needs --deltas): adds the stranded sibling state "
                         "view")
    ap.add_argument("--version-tree", dest="version_tree",
                    help="the parent version of each version (optional): "
                         "orders the --unregister-plan drops")
    ap.add_argument("--unregister-plan", dest="unregister_plan",
                    help="replica ids, comma separated (needs --replicas and "
                         "--version-tree): print an advisory plan to "
                         "unregister them. Nothing is run.")
    ap.add_argument("--exit-on", dest="exit_on", choices=EXIT_ON,
                    help="what makes the exit code 1: blocker (default), any "
                         "listed holder, or never. A failure is exit 2 "
                         "either way.")
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
               "deltas", "state_parents", "version_tree")
    # Every flag but --self-test and --sql. An ignored flag reads as obeyed.
    others = [k for k in exports + ("report", "as_of", "max_age_days",
                                    "unregister_plan", "exit_on")
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
    if args.unregister_plan and not (args.replicas and args.version_tree):
        return fail("--unregister-plan needs --replicas and --version-tree: "
                    "the plan must tell a registered replica from a detached "
                    "one, and order the drops.")
    try:
        ids = parse_ids(args.unregister_plan) if (
            args.unregister_plan is not None) else None
    except ValueError as exc:
        return fail("--unregister-plan: %s." % exc)
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
    if ids:
        lines += [""] + describe_plan(plan_unregister(model, report, ids))
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
    return gate(report, args.exit_on or "blocker")


if __name__ == "__main__":
    sys.exit(main())
