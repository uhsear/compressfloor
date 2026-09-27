# compressfloor

Name what holds an enterprise geodatabase's compress floor, from exported system tables.

An enterprise geodatabase ran its compress every night. Every night the job exited 0 and the
compress log said it succeeded. It did this for about 255 nights, and in all that time the
geodatabase never got close to state 0. Two replicas had never synced past generation 0, so
their hidden system versions still pointed at a state from months before. A separate lineage of
states that no version referenced held 1.76 million delta rows. More than 1,500 versions had
piled up. Nothing alerted, because nothing had failed.

Compress does not fail when something pins it. It removes the states that no version needs, it
folds the edits that every version shares into the base tables, and then it stops. The point
where it stops is the compress floor. Three kinds of version hold that floor: a replica that
never syncs, a replica that was unregistered but left its system versions behind, and a
forgotten edit version. Each holds it at the state where it forked from DEFAULT. Compress still
exits 0.

The only diagnosis was to run three system-table queries by hand and read the rows. This tool
reads the exported results of those queries and does the reading.

```
$ python compressfloor.py --self-test
compressfloor self-test: no database, no network, no credentials
--------------------------------------------------------------------
PASS  the floor is state 17, where the oldest holder forks from DEFAULT
PASS  DEFAULT is read at state 9000
PASS  the as-of time is the newest timestamp in the exports, not the local clock
PASS  a registered replica forking at an old state is STALLED  <-- pinned defect
PASS  and it is named as holding the floor
PASS  only the holders at the floor state are named as holding it, not the ones above it  <-- pinned defect
PASS  its SEND and RECEIVE system versions are one replica, not two findings
PASS  a replica on its own branch forks where its branch leaves DEFAULT
PASS  a SYNC_ version whose replica id is not registered is DETACHED  <-- pinned defect
PASS  an ordinary version DEFAULT moved past 199 days ago is ANCIENT
PASS  a lineage no version references, untouched for months, is a blocker  <-- pinned defect
PASS  and it carries the 1,760,000 delta rows supplied for it
PASS  and it is reported forking from DEFAULT's lineage at state 208
...
PASS  the naive orphan count calls DEFAULT's own ancestor state 10 an orphan  <-- pinned defect
PASS  the lineage walk finds exactly the three unreferenced states
PASS  on a healthy tree the naive count finds an orphan and the walk finds none
...
PASS  an as-of time before the newest export timestamp is refused, never a negative age  <-- pinned defect
PASS  a fork state with no creation time is a blocker, never assumed young  <-- pinned defect
PASS  and the report says the age is unknown
PASS  a version made today from a DEFAULT idle for 80 days is not 80 days old  <-- pinned defect
PASS  a version made from state 0 forks at state 0 and holds the floor there  <-- pinned defect
PASS  a lineage export without its (lineage, 0) rows gives the same report  <-- pinned defect
...
PASS  a detached replica version at DEFAULT's own state is still a blocker  <-- pinned defect
PASS  and it is not said to hold a floor the header says nothing holds  <-- pinned defect
PASS  a replica with a fresh SEND and an old RECEIVE forks at the older state and is STALLED
PASS  and in the other row order too  <-- pinned defect
...
PASS  beside a Sync Replica item, an unmatched SYNC_ version is UNRESOLVED, not DETACHED  <-- pinned defect
PASS  and it is not sent to TA 000011719's delete step  <-- pinned defect
PASS  an offline map's <user>_<service>_<id> replica version is an ordinary version
PASS  a collaboration replica's versions at DEFAULT's state are not listed at all
PASS  a live collaboration replica a day behind is a holder, not a blocker  <-- pinned defect
...
PASS  a state above DEFAULT on DEFAULT's own lineage is not DEFAULT's ancestor: unreferenced, it is orphaned  <-- pinned defect
...
PASS  one undated state makes an orphaned lineage's age unknown, even beside a dated one  <-- pinned defect
PASS  an orphaned lineage says a compress must run before it is blamed on anything  <-- pinned defect
...
PASS  the clean verdict names the limit it used and claims only the exports  <-- pinned defect
...
PASS  a versions export without DEFAULT is refused  <-- pinned defect
...
PASS  a number past a float's range is a ValueError, not an OverflowError  <-- pinned defect
...
PASS  a state exported twice is refused, with the young row first  <-- pinned defect
PASS  a state exported twice is refused, with the old row first  <-- pinned defect
PASS  a negative state id (-1) is refused, not wrapped to the lineage's last id  <-- pinned defect
PASS  a negative state id (-3) is refused, not wrapped to the lineage's last id  <-- pinned defect
...
PASS  a row with both STATE_ID and state_id is refused, not read by key order  <-- pinned defect
...
PASS  a lineages export that lost a row is refused by its row_count, not read as a clean exit 0  <-- pinned defect
...
PASS  a states export that lost its last row is refused, not a clean exit 0 without the orphaned lineage  <-- pinned defect
...
PASS  the sqlserver replicas query sets QUOTED_IDENTIFIER ON before its XML call  <-- pinned defect
...
PASS  an --as-of before the exports exits 2, not a report of negative ages  <-- pinned defect
...
PASS  a UTF-8 BOM is stripped from the first header  <-- pinned defect
PASS  a missing export exits 2, not 1  <-- pinned defect
...
PASS  a JSON row with state_id twice (17,9000) exits 2, not a verdict picked by key order  <-- pinned defect
PASS  a JSON row with state_id twice (9000,17) exits 2, not a verdict picked by key order  <-- pinned defect
PASS  a JSON export after leading blank space is read as JSON, not as CSV
...
PASS  a zero-byte replicas export exits 2, it is not zero replicas  <-- pinned defect
PASS  a header-only export of the wrong kind exits 2  <-- pinned defect
...
PASS  a lineages export without row_count exits 2: a file that lost rows could not be told from a whole one  <-- pinned defect
...
PASS  a lineages CSV that lost its last row exits 2, not 0  <-- pinned defect
PASS  a CSV header with state_id twice exits 2  <-- pinned defect
PASS  a delta count of inf exits 2, not 1  <-- pinned defect
PASS  a JSON Infinity id exits 2
PASS  any other failure of the tool exits 2, not 1  <-- pinned defect
PASS  an age limit past what a timedelta holds exits 2  <-- pinned defect
PASS  a name outside cp1252 prints escaped to a cp1252 stdout, not a crash  <-- pinned defect
PASS  a replica log without the replicas export exits 2
PASS  --max-age-days 0 is accepted  <-- pinned defect
PASS  --max-age-days 999999999 is accepted  <-- pinned defect
PASS  a credential file passed as an export is not echoed  <-- pinned defect
PASS  a cell that is not a number is named, not echoed  <-- pinned defect
...
PASS  --report without --apply writes nothing  <-- pinned defect
...
PASS  check() and raises() really do record a failure  <-- pinned defect
PASS  outcome() turns a crash into a value a check can fail on
PASS  a run with a failure prints it and exits 1  <-- pinned defect
PASS  importing the file runs nothing and exposes the core
PASS  the import probe writes no bytecode beside the script  <-- pinned defect
--------------------------------------------------------------------
202 assertions, 0 failed
```

The full run prints all 202 assertions. The `...` lines are where this block is cut.

## What already exists

- **Esri Technical Article 000011719**, "How To: Determine if there are detached replica system
  versions in the geodatabase". It is the authoritative manual procedure. It names the replica
  system versions, gives the SQL that lists the registered replicas, and tells you to compare the
  two lists by eye. It is correct, and it covers one of the four things that hold a floor.
- **`StateLineageCheck.py` in [phillegard/ArcGIS_Maintenance](https://github.com/phillegard/ArcGIS_Maintenance)**.
  It counts the rows in the state and lineage tables through `arcpy` and compares the counts with
  warning and critical thresholds. A growing state count is a real early signal, and the script
  is easy to schedule.

Neither says which version or replica holds the floor, and neither runs without a live
connection. This tool does the comparison that the article describes, adds the other three
holders, and runs offline against exported rows.

## Requirements

Python 3.9 or newer and nothing else. No `arcpy`, no database driver, no third-party package, no
network. You run the SQL yourself, with the client you already use, and give the tool the
results as CSV or JSON.

The same 202 assertions pass everywhere they were run: Windows (Python 3.13.2), Ubuntu (Python
3.12.3) and Windows (Python 3.9.25).

```
git clone https://github.com/uhsear/compressfloor.git
```

## Usage

1. Print the SELECTs for your database. Run them as a user that can read the geodatabase
   administrator's schema, and save each result with its header row. "The SQL" below gives the
   exact client commands.
2. Give the tool the exports.

Take the exports after a compress has finished. A compress removes the states that no version
needs, so states left by a deleted version look orphaned until the next compress. If you
schedule this tool, run it after the nightly compress.

```
python compressfloor.py --sql sqlserver
python compressfloor.py --versions versions.csv --states states.csv --lineages lineages.csv
python compressfloor.py --versions versions.csv --states states.csv --lineages lineages.csv \
    --replicas replicas.csv --replica-log replica_log.csv --deltas deltas.csv
python compressfloor.py ... --max-age-days 14 --as-of 2026-09-21
python compressfloor.py ... --report floor.txt --apply
```

| Flag | Default | What it does |
|---|---|---|
| `--versions` | none | The version table export. Required. |
| `--states` | none | The state table export. Required. |
| `--lineages` | none | The state lineage table export. Required. |
| `--replicas` | off | The replica items export. Without it, a replica system version cannot be called detached or stalled. |
| `--replica-log` | off | The replica log export. Needs `--replicas`, because the log names a replica by its item `objectid`. |
| `--deltas` | off | Delta rows per state. Without it, every weight prints as `not supplied`. |
| `--max-age-days` | `30` | A holder that DEFAULT moved past more than this many days ago is a blocker. `0` makes a blocker of every holder and orphaned lineage older than the as-of time itself. |
| `--as-of` | newest export timestamp | Measure ages from this time instead. `YYYY-MM-DD` or `YYYY-MM-DD HH:MM:SS`. A time earlier than the newest export timestamp exits 2, because every age would be negative. |
| `--report` | off | Write the report to this file. Needs `--apply`. |
| `--apply` | off | Write `--report`. Without it nothing is written. |
| `--sql` | off | Print the SELECTs for `sqlserver`, `postgresql` or `oracle`, and exit. |
| `--self-test` | off | Run the assertions and exit. Takes no other flag. |

A file whose first non-blank character is `[` or `{` is read as JSON, and it must be an array
of objects. Any other file is read as CSV. Column names are matched without regard to case, and
a UTF-8 byte order mark is removed.

Ages are measured from the newest timestamp in the exports, not from the clock of the machine
that runs the tool. The same exports therefore give the same report on any day.

## What it checks

The tool walks the state lineage of every version. The state where a version's lineage leaves
DEFAULT's lineage is its fork. The oldest fork of all is the compress floor: compress can fold
edits into the base tables no further than that state. Every version whose fork is below
DEFAULT's own state is a holder. The exports hold versions, not state locks. A state lock can
stop compress sooner, and the tool does not see it (see Limits).

A holder starts to hold the floor when DEFAULT moves past its fork, not when the fork state was
made. The tool therefore ages a holder from the creation of the first state above the fork in
DEFAULT's own lineage. A version made today from a DEFAULT that sat idle for 80 days has held
the floor since today. A holder held for longer than `--max-age-days` is a blocker.

| Class | Blocker when | What it means |
|---|---|---|
| `STALLED_REPLICA` | held past the age limit | A registered replica whose `SYNC_` system versions have not moved. Its SEND and RECEIVE versions are one finding. |
| `DETACHED_REPLICA_VERSION` | always | A `SYNC_` version whose replica id has no `Replica` row in the replicas export, in a geodatabase with no `Sync Replica` items. TA 000011719 calls this detached. |
| `UNRESOLVED_REPLICA_VERSION` | held past the age limit | A `SYNC_` version that the tool cannot tie to a replica: no `--replicas` was given, or the geodatabase has `Sync Replica` items (see below). |
| `ANCIENT_VERSION` | held past the age limit | An ordinary version that forks from DEFAULT at an old state. |
| `ORPHANED_STATES` | its newest state is past the age limit | States that no version's lineage reaches, grouped by lineage. |

A replica system version is recognised by the names in TA 000011719: `SYNC_SEND_<id>_<gen>`,
`SYNC_RECEIVE_<id>_<gen>` and `SYNC_RECEIVE_REC_<id>_<gen>`. For each replica the report prints
the highest generation in those names and, with `--replica-log`, the last log event and the
highest generation it logged. A replica that has reached neither past generation 0 is described
in those words.

A holder under the age limit is listed in its own section and never fails the run. An edit
version that is ten days old is normal work, and a tool that failed on it would be switched off
within a week.

A detached replica version is a blocker at any age. Its replica no longer exists, so no sync
will ever move it.

A feature service replica is a different item, a `Sync Replica` item with a
`<GPSyncReplica>` definition. One Esri Community thread under Sources shows a distributed
collaboration that keeps `SYNC_SEND_` and `SYNC_RECEIVE_` versions. None of the Esri pages under Sources says which number those version names carry. In
the one listing of `GPSyncReplica` items published on Esri Community, most had the `ID` `-1`.
So when the replicas export holds any `Sync Replica`
row, a `SYNC_` version that matches no `Replica` row is `UNRESOLVED_REPLICA_VERSION`, never
detached. It is a blocker only when it has held the floor past the age limit. The report
tells you not to delete it on the article's word, because the article warns that deleting any
other replica system version can corrupt a replica.

This is the self-test's synthetic fixture, written out as CSV files and run through the command
line:

```
$ python compressfloor.py --versions versions.csv --states states.csv --lineages lineages.csv --replicas replicas.csv --replica-log replica_log.csv --deltas deltas.csv
compressfloor: 9 version(s), 14 state(s), 4 replica(s), as of 2026-09-20 02:00:00
DEFAULT (SDE) is at state 9000
compress floor: state 17, 253 day(s) old
  compress can fold edits into the base tables no further than state 17
  delta rows compress cannot fold: 1,809,012

BLOCKERS (5)
  STALLED_REPLICA            replica 56 'parcels_to_web'
      versions: SYNC_SEND_56_0 (state 17), SYNC_RECEIVE_56_0 (state 17)
      forks from DEFAULT at state 17, held for 231 day(s); this holds the floor
      highest sync generation in its version names: 0
      replica log: no event for this replica
      it has never advanced past sync generation 0
      delta rows on its own branch: 0
  STALLED_REPLICA            replica 57 'roads_to_web'
      versions: SYNC_SEND_57_0 (state 18)
      forks from DEFAULT at state 17, held for 231 day(s); this holds the floor
      highest sync generation in its version names: 0
      replica log: 1 event(s), last 2026-01-10 04:00:00, highest generation 0
      it has never advanced past sync generation 0
      delta rows on its own branch: 0
  DETACHED_REPLICA_VERSION   replica 48
      versions: SYNC_SEND_48_2 (state 50)
      forks from DEFAULT at state 50, held for 199 day(s)
      highest sync generation in its version names: 2
      delta rows on its own branch: 0
      no replica has this id: Esri TA 000011719 calls this a detached replica system version
  ANCIENT_VERSION            version EDITOR1.OLD_DESIGN
      versions: OLD_DESIGN (state 60)
      forks from DEFAULT at state 50, held for 199 day(s)
      delta rows on its own branch: 5,000
  ORPHANED_STATES            lineage 300: 2 state(s) no version references, 300 to 301
      newest state 197 day(s) old; forks from DEFAULT's lineage at state 208
      delta rows in these states: 1,760,000
      a compress removes states no version needs: if none has run since a version was deleted, run one and export again

holding state but under the 30 day limit (3)
  REPLICA                    replica 70 'hydrants_field'
      versions: SYNC_SEND_70_12 (state 901)
      forks from DEFAULT at state 900, held for 0 day(s)
      highest sync generation in its version names: 12
      replica log: 1 event(s), last 2026-09-19 03:00:00, highest generation 12
      delta rows on its own branch: 0
  VERSION                    version EDITOR2.WO_NEW
      versions: WO_NEW (state 905)
      forks from DEFAULT at state 900, held for 0 day(s)
      delta rows on its own branch: 0
  ORPHANED_STATES            lineage 9001: 1 state(s) no version references, 9001 to 9001
      newest state 0 day(s) old; forks from DEFAULT's lineage at state 9000
      delta rows in these states: 0
      a compress removes states no version needs: if none has run since a version was deleted, run one and export again

VERDICT: 5 blocker(s). Compress will keep exiting 0 and will not fold past state 17.
```

Exit code 1. The ages differ from the state ages on purpose. Replica 56 forks at state 17, made
253 days before the exports, but DEFAULT moved past state 17 only when it made state 50, 231
days before. The two stalled replicas hold the floor at state 17. The detached version and the
old edit version would hold it at state 50 if the replicas were fixed. The orphaned lineage
carries 1.76 million of the 1.8 million delta rows that compress cannot fold.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Nothing in the exports holds the floor past the age limit. The exports do not show state locks, so this is not proof that nothing holds it (see Limits). |
| 1 | At least one blocker. |
| 2 | The tool could not do its job: an export could not be read or was empty, the exports disagree with each other, a flag was wrong, `--as-of` is earlier than the exports, `--report` could not be written, or the tool itself hit an error. |

Exit 2 is kept apart from exit 1 on purpose. A monitor that cannot read its input must never
look like a monitor that found nothing, and it must never look like a finding either.

The exports are checked against each other before anything is reported. Each of these stops the
run with exit 2:

- a version that points at a state that is not in the states export
- a state on a lineage that is not in the lineages export
- a states export without state 0
- a versions export without exactly one DEFAULT row
- a state that appears twice in the states export
- two columns in one export whose names differ only in case, such as `STATE_ID` and `state_id`
- a JSON object that has one key twice, because a JSON reader keeps only the last value and the
  verdict would then depend on key order
- a negative state id, lineage name or lineage id, which Esri never writes
- an export file without a `row_count` column
- an export whose number of rows differs from its `row_count`, or whose rows do not all hold
  the same `row_count`

Exports taken at different moments disagree, and a floor computed from them is a floor of
neither.

An export that lost rows is worse, because nothing else catches it. When a lineage row is lost,
a holder can be aged from a later state and fall under the limit. When a state row is lost, an
orphaned lineage can disappear. Either way a blocker becomes a clean exit 0. The self-test
pins both cases. Rows are lost when a client returns only the first rows, such as "Select Top
1000 Rows" in SQL Server Management Studio. They are also lost when a spreadsheet saves only
its first 1,048,576 rows, or when a grid is copied one page at a time. Every shipped SELECT
therefore returns `COUNT(*) OVER () AS row_count`, the number of rows that the query produced,
on each row.

## The SQL

Every query is a read-only SELECT. `--sql` prints the same text that is shown below. Table and
column names come from Esri's system table documentation. The `GPReplica` paths come from TA
000011719, and the `GPSyncReplica` paths from an Esri Community thread (see Sources).

Save each result as a CSV with a header row, or as a JSON array of objects. A SQL Server
geodatabase in the `dbo` schema needs `dbo.` in place of `sde.`.

Put each query in its own file, such as `versions.sql`, and run it with the command for your
client. These are the exact procedures that were tested (see "How the SQL was tested"). Add
your own connection options.

| Client | Put at the top of each file | Run | Then |
|---|---|---|---|
| `psql` | nothing | `psql -v ON_ERROR_STOP=1 --csv -f versions.sql -o versions.csv` | nothing |
| `sqlcmd` | `SET NOCOUNT ON;` | `sqlcmd -b -W -s "," -i versions.sql -o versions.csv` | Delete the second line of the output, the dashes under the header. |
| SQL\*Plus | `SET MARKUP CSV ON`, `SET FEEDBACK OFF`, `SET PAGESIZE 50000` and `WHENEVER SQLERROR EXIT FAILURE`, one per line, and `EXIT` as the last line | `sqlplus -S <user>@<database> @versions.sql > versions.csv` | nothing |

Each of these settings matters. Without `SET NOCOUNT ON`, `sqlcmd` ends the file with a
`(14 rows affected)` line. Without `SET FEEDBACK OFF`, SQL\*Plus ends it with a
`14 rows selected.` line. The tool reads either line as a data row, and exits 2.

The delta counts take two steps. Step 1 is a SELECT that writes one line of SQL for each table
that has edits in a state. Its output must be those lines alone, with no header and no quotes:

| Client | Step 1 differs from the table above in this way |
|---|---|
| `psql` | Use `-A -t` in place of `--csv`. |
| `sqlcmd` | Add `-h -1`. There is then no header and no dashed line to delete. |
| SQL\*Plus | Use `SET MARKUP CSV ON QUOTE OFF` and add `SET HEADING OFF`. |

For step 2, make a file that holds the step 2 query. Put every line from step 1 above its last
line, `) t;`. Run that file as in the first table, and save the result as `deltas.csv`.

An export saved from SQL Server Management Studio was not tested. If you use it, turn on
"Include column headers when copying or saving the results" and "Quote strings containing list
separators when saving .csv results" before you save.

<details>
<summary>SQL Server</summary>

```sql
-- versions.csv
SELECT name, owner, state_id, COUNT(*) OVER () AS row_count
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
```

</details>

<details>
<summary>PostgreSQL</summary>

```sql
-- versions.csv
SELECT name, owner, state_id, COUNT(*) OVER () AS row_count
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
```

</details>

<details>
<summary>Oracle</summary>

```sql
-- versions.csv
SELECT name, owner, state_id, COUNT(*) OVER () AS row_count
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
```

The article warns that the replica query in Oracle needs EXTPROC configured.

</details>

### How the SQL was tested

All three dialects were run, exactly as printed by `--sql`, against a synthetic schema in a
throwaway container: PostgreSQL 16 with `psql`, SQL Server 2022 with `sqlcmd` and Oracle
Database Free with SQL\*Plus. Each query was saved in its own file and run with the procedure in
the tables above, and nothing else was done to the output. Each schema used the table and
column names above. It was filled with the self-test's fixture, including 1.8 million real
delta rows across two versioned tables, plus one `Sync Replica` item with a `<GPSyncReplica>`
definition whose `ID` is `-1`. For each DBMS, the tool's report on the exported CSVs was
identical, line for line, to its report on the same rows written directly as CSV. That report
is the one above, except that replica 48 is `UNRESOLVED_REPLICA_VERSION`, because a `Sync
Replica` item exists. Each export carried its `row_count`. When the last row of a live lineages
export was deleted, the run exited 2 on all three DBMSs.

The first live run found one defect. `sqlcmd` refused the replica query with Msg 1934, because `sqlcmd`
connects with `QUOTED_IDENTIFIER` off and SQL Server refuses an XML `.value()` call under that
setting. The query now sets `QUOTED_IDENTIFIER ON`, and an assertion pins it. Microsoft documents
that SQL Server Management Studio connects with the setting on. SSMS was not part of the test.

These were synthetic schemas, not geodatabases that ArcGIS created. See Limits.

## Why the obvious version is wrong

**"Compress exits 0" is not a check.** Compress succeeds whenever it removes what it is allowed to
remove, even if that is nothing. Its success status says that it ran, not that it got anywhere.

**A state count with a threshold alarms late and names nothing.** The count grows for months
before it crosses any threshold that a busy geodatabase would tolerate. When it does cross,
it says that there are too many states, not which version keeps them.

**Counting states that no version points at is wrong.** The tempting query is "the states whose
`state_id` is not any version's `state_id`":

```sql
SELECT COUNT(*) FROM sde.SDE_states s
WHERE NOT EXISTS (SELECT 1 FROM sde.SDE_versions v WHERE v.state_id = s.state_id);
```

A version points at one state, but it needs every state on its lineage back to state 0. That
query counts every ancestor of every live version as an orphan. In the fixture it calls state
10, an ancestor of DEFAULT itself, an orphan. The self-test also runs that naive count on a
healthy tree, DEFAULT alone at state 5. The naive count finds state 0, and the lineage walk
finds nothing. This tool walks the
state lineage table, which Esri documents as the index that every version query traverses, and
reports only the states that no version's lineage reaches.

**Looking only for detached replica versions misses the stalled ones.** A registered replica
that has not synced for months holds the floor exactly as hard as a detached one. TA
000011719's comparison passes it, because the replica is still registered.

## Limits

- **The SQL has not been run against a geodatabase that ArcGIS created.** It was run against
  synthetic schemas built from Esri's documentation (see above). The table and column names come
  from that documentation. The Oracle replica query reads `ObjectID` from `GDB_ITEMS_VW`. The
  article's Oracle query does not read that column, so its presence in the view is an
  assumption from the SQL Server and PostgreSQL `GDB_ITEMS` tables, not a documented fact.
- **The replica log is joined on `objectid`.** Esri documents that the log's `replicaid` is the
  replica's `objectid` in `GDB_ITEMS`. The `SYNC_` version names carry the `<ID>` from the
  replica's definition, as the article shows. The two numbers are exported separately, and the
  tool never assumes that they are equal.
- **The error code in the replica log is not read.** Esri documents that a successful event
  records a success code but does not name its value. The tool reports generations and dates,
  and it does not guess which events failed.
- **A holder's age depends on which state survives a compress.** The tool ages a holder from
  the oldest state above its fork in DEFAULT's lineage. Compress collapses an unreferenced run
  of states into one state, and Esri does not document which state and creation time remain.
  If the newest one remains, a holder can look younger than it is. The header line
  `compress floor: state N, D day(s) old` gives the fork state's own age, so compare the two.
- **Traditional versioning only.** Esri's Compress tool does not apply to branch versioning.
- **The floor comes from the version table.** An orphaned lineage is reported with its weight and
  the state where it leaves DEFAULT's lineage. Compress removes the states that no version
  references. Deleting a version leaves its states in place until the next compress, so check
  first that a compress has run since the last version was deleted. If one has, something is
  keeping these states. The exports do not show what, and the tool does not guess. The state
  lock table, `SDE_state_locks`, is a place to look next.
- **State locks are not read.** Esri's article on state locks (see Sources) says that when
  ArcGIS connects to an enterprise geodatabase, the connection takes a state lock on the state
  it reads. Compress cannot compress a locked state. A long-lived connection to an old DEFAULT
  state therefore holds compress back too. The SQL exports no lock table, so the tool does not
  see this. Exit 0 means that nothing in the exports holds the floor, not that nothing holds
  it. If compress still stops short after a clean run, follow that article and read
  `SDE_state_locks`.
- **Feature service replicas are only counted.** The replicas query reads `Sync Replica` items
  as well as `Replica` items, but the tool never matches a `SYNC_` version to a `Sync Replica`
  item, because no Esri page says which number ties them together. When any `Sync Replica`
  item exists, an unmatched `SYNC_` version is `UNRESOLVED_REPLICA_VERSION`, and a stalled
  collaboration replica cannot be told apart from a detached geodatabase replica. The
  replica log is joined to `Replica` items only.
- **A checkout replica's own version is an ordinary version here.** Its name is the replica's
  name, not a `SYNC_` pattern, so an old one is reported as `ANCIENT_VERSION`.
- **An offline map's replica version is an ordinary version here.** Esri names it
  `<user>_<service>_<ID>`, for example `Bob_NetFS_1404578882000`, with no `SYNC_` prefix. An
  old one is reported as `ANCIENT_VERSION`. Its device syncs through it, so do not delete it
  until that device has removed the map and the version is reconciled and posted.
- **Delta counts come from `SDE_mvtables_modified`.** Step 1 of the delta SQL lists only the
  tables that have a row there. A table with delta rows but no such row is not counted.
- **One clock.** A time zone suffix on a timestamp is ignored, because every timestamp comes from
  one database. Fractional seconds are dropped.
- **Everything is read into memory.** One synthetic export had 1,501 versions, 11,501 states and
  3,010,501 lineage rows. It took about 15 seconds on a Windows workstation, almost all of it
  spent reading the CSVs. The time grows with the number of rows.
- **Nothing is fixed.** The tool names the holders. Deleting a version, re-syncing a replica or
  removing a detached replica version is a change that somebody signs for. Follow TA 000011719
  for a version the tool calls detached, and do not delete any other replica system version.
- **A CSV export needs its header row.** A zero-byte file exits 2, and so does a header that
  lacks the export's columns, `row_count` included. A correct header with no data rows is read
  as zero rows, and so is a JSON `[]`. No row then carries a `row_count`, so an export that lost
  every row cannot be told from an empty table. A `--replicas` export like that makes every
  `SYNC_` version detached, so check that the replica query really returned nothing before you
  act on it.
- Only `--report` is ever written, and only with `--apply`. `--self-test` writes its fixture to
  a temporary folder and removes it, and it writes no bytecode cache beside the script. The
  tool opens no socket and imports no network module.
- **A wrong file is not echoed.** If you pass the wrong file as an export, such as a `.env`
  file, the error message names only the columns that the tool knows, and counts the rest. It
  names the export, row and column of a cell it cannot read, but never prints the cell's value.
  The self-test passes a synthetic `.env` line and a synthetic JSON value and checks that
  neither reaches stderr.

## Sources

- [Esri Technical Article 000011719](https://support.esri.com/en/technical-article/000011719),
  "How To: Determine if there are detached replica system versions in the geodatabase". The
  article is archived; the 2021 copy on the Wayback Machine is the one read here.
- [Esri Technical Article 000010761](https://support.esri.com/en-us/knowledge-base/how-to-discover-what-state-locks-are-blocking-the-compr-000010761),
  "How To: Discover what state_locks are blocking the compress operation on Oracle": a
  connection takes a state lock, and compress cannot compress a locked state.
- [Geodatabase system tables in SQL Server](https://pro.arcgis.com/en/pro-app/latest/help/data/geodatabases/manage-sql-server/geodatabase-system-tables-sqlserver.htm),
  [in PostgreSQL](https://pro.arcgis.com/en/pro-app/latest/help/data/geodatabases/manage-postgresql/geodatabase-system-tables-postgresql.htm)
  and [in Oracle](https://pro.arcgis.com/en/pro-app/latest/help/data/geodatabases/manage-oracle/geodatabase-system-tables-oracle.htm):
  the table names for each DBMS.
- [System tables of a geodatabase in SQL Server (10.1)](https://resources.arcgis.com/en/help/main/10.1/002q/002q00000080000000.htm):
  the columns of `SDE_versions`, `SDE_states`, `SDE_state_lineages`, `GDB_REPLICALOG`,
  `SDE_mvtables_modified` and `SDE_table_registry`.
- [ArcSDE versioning database schema (10.0)](https://help.arcgis.com/en/geodatabase/10.0/sdk/arcsde/concepts/versioning/dbschema/dbschema.htm):
  the same tables, and the rule that SQL Server and PostgreSQL add the `sde_` prefix.
- [Versioned tables in a geodatabase in SQL Server (10.2)](https://resources.arcgis.com/en/help/main/10.2/002q/002q0000007v000000.htm):
  the delta tables `a<registration_id>` and `d<registration_id>`, and their `SDE_STATE_ID` and
  `DELETED_AT` columns.
- [XML column queries](https://desktop.arcgis.com/en/arcmap/latest/manage-data/using-sql-with-gdbs/xml-column-queries.htm)
  and [Example: Find domain owners using SQL](https://desktop.arcgis.com/en/arcmap/latest/manage-data/using-sql-with-gdbs/example-finding-domain-owners.htm):
  the `GDB_Items_vw` view in Oracle and the PostgreSQL `xpath()` form.
- [Compress (Data Management)](https://pro.arcgis.com/en/pro-app/latest/tool-reference/data-management/compress.htm)
  and [Replication and versioning](https://pro.arcgis.com/en/pro-app/latest/help/data/geodatabases/overview/replication-and-versioning.htm):
  what compress removes, and why replica system versions need regular syncs for compress to be
  effective.
- [How to remove orphans sync replicas?](https://community.esri.com/t5/geodatabase-questions/how-to-remove-orphans-sync-replicas/td-p/846395)
  (Esri Community): the `Sync Replica` item type, the `/GPSyncReplica/ID` and
  `/GPSyncReplica/ReplicaName` paths, and a listing where most IDs are `-1`.
- [Issue with distributed collaboration SDE_versions and SDE Compression](https://community.esri.com/t5/arcgis-enterprise-questions/issue-with-distributed-collaboration-sde-versions/td-p/1546258)
  (Esri Community): a distributed collaboration with no geodatabase replicas whose `SYNC_SEND`
  and `SYNC_RECEIVE` versions kept a compress from reaching state 0.
- [Offline maps and traditional versioned data](https://enterprise.arcgis.com/en/server/latest/publish-services/windows/offline-maps-and-versioned-data.htm):
  an offline map's replica version is named `<user>_<service>_<ID>`, and it is deleted only
  after the map is removed from the device and the version is reconciled and posted.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [gdbprune](https://github.com/uhsear/gdbprune) - deletes the stale leaf versions whose names
  match a pattern, and prints a plan first. It does no reconcile, post or compress. After it
  deletes versions, run a compress, and then run compressfloor to see what still holds the
  floor. Before that compress, the deleted versions' states show here as `ORPHANED_STATES`.
  Its default pattern is `%SYNC%`, which
  also matches the `SYNC_SEND_` and `SYNC_RECEIVE_` replica system versions. Never prune those:
  TA 000011719 warns that deleting a replica system version other than a detached one corrupts
  the replica.
