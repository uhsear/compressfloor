# compressfloor

Name what holds an enterprise geodatabase's compress floor, from exported system tables.

An enterprise geodatabase ran its compress every night. Every night the job exited 0 and the
compress log said it succeeded. It did this for months of nights, and in all that time the
geodatabase never got close to state 0. Two distributed collaboration replicas had never synced
past generation 0, so their hidden system versions still pointed at a state from months before.
A separate lineage of states that no version referenced held over a million delta rows. Over a
thousand versions had piled up. Nothing alerted, because nothing had failed.

Compress does not fail when something pins it. It removes the states that no version needs, it
folds the edits that every version shares into the base tables, and then it stops. The point
where it stops is the compress floor. Three kinds of version hold that floor: a replica that
never syncs, a replica that was unregistered but left its system versions behind, and a
forgotten edit version. Each holds it at the state where it forked from DEFAULT. Compress still
exits 0.

The only diagnosis was to run three system-table queries by hand and read the rows. This tool
reads the exported results of those queries and does the reading.

Three later additions use the same offline pattern. An `--exit-on` flag lets a daily monitor
tell a finding from a failure. A stranded sibling-state view lists, before a compress, the
states that hold rows no version reaches. A plan-only replica unregister orders the steps and
never runs one. Each has its own section below.

```
compressfloor self-test: no database, no network, no credentials
--------------------------------------------------------------------
PASS  the floor is state 17, where the oldest holder forks from DEFAULT
PASS  DEFAULT is read at state 9000
PASS  the as-of time is the database clock the versions export recorded, not the local clock
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
PASS  DEFAULT's delta rows at a version's fork state are not counted in its own-branch weight  <-- pinned defect
PASS  DEFAULT is found in a later row of the versions export, not taken from the first row  <-- pinned defect
PASS  the naive orphan count calls DEFAULT's own ancestor state 10 an orphan  <-- pinned defect
PASS  the lineage walk finds exactly the three unreferenced states
PASS  on a healthy tree the naive count finds an orphan and the walk finds none
...
PASS  a fork directly below DEFAULT's own old state is aged from that state, not held for 0 days  <-- pinned defect
PASS  an as-of time before the newest export timestamp is refused, never a negative age  <-- pinned defect
PASS  a fork state with no creation time is a blocker, never assumed young  <-- pinned defect
PASS  and the report says the age is unknown
PASS  a version made today from a DEFAULT idle for 80 days is not 80 days old  <-- pinned defect
PASS  a version made from state 0 forks at state 0 and holds the floor there  <-- pinned defect
PASS  a lineage export without its (lineage, 0) rows gives the same report  <-- pinned defect
...
PASS  only the unresolved replica versions are told to supply --replicas, and no finding is told not to delete  <-- pinned defect
PASS  a detached replica version at DEFAULT's own state is still a blocker  <-- pinned defect
PASS  and it is not said to hold a floor the header says nothing holds  <-- pinned defect
PASS  a replica with a fresh SEND and an old RECEIVE forks at the older state and is STALLED
PASS  and in the other row order too  <-- pinned defect
PASS  a replica whose SEND name reached generation 3 is not called stuck at 0 for its RECEIVE name  <-- pinned defect
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
PASS  with the export time, the same replica has held the floor for 269 days and is a blocker  <-- pinned defect
PASS  a blank exported_at is refused, never read as no export time  <-- pinned defect
...
PASS  a replica log is read at its highest generation, and a replica that synced past 0 is not called stuck at 0  <-- pinned defect
...
PASS  a clean run prints no empty findings section  <-- pinned defect
PASS  the clean verdict names the limit it used and claims only the exports  <-- pinned defect
...
PASS  a version name holding newlines and an ANSI escape cannot forge a VERDICT line or hide one  <-- pinned defect
...
PASS  a versions export without DEFAULT is refused  <-- pinned defect
...
PASS  a number past a float's range is a ValueError, not an OverflowError  <-- pinned defect
...
PASS  a 12-hour timestamp is refused, not read as 03:00 AM  <-- pinned defect
PASS  any other text after the seconds is refused  <-- pinned defect
PASS  a versions export with a 12-hour exported_at is refused, not a clock 12 hours early  <-- pinned defect
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
PASS  a versions export that lost OLD's row and repeats KEEP's is refused, though its row_count matches  <-- pinned defect
PASS  a lineages export that lost row (4, 2) and repeats another is refused, though its row_count matches  <-- pinned defect
PASS  a replicas export that lost replica 57's row and repeats 56's is refused, though its row_count matches  <-- pinned defect
...
PASS  a replicas export that lost a Replica row and repeats a Sync Replica row is refused by its objectid  <-- pinned defect
...
PASS  a deltas export with one row more than its row_count is refused, not summed twice  <-- pinned defect
PASS  blockers exit 1 under blocker and any, and 0 under never
PASS  a holder under the age limit exits 0 by default and 1 under any
...
PASS  an exit level the tool does not know is refused, not read as the default  <-- pinned defect
PASS  a state no version reaches, with a later state of the same parent and owner, is stranded  <-- pinned defect
...
PASS  no version points at a stranded state or has it as an ancestor, proved with the ancestor walk and not the fork walk
PASS  and the same proof finds the kept sibling, an ancestor and DEFAULT's state reached, so it is not vacuous
...
PASS  a lost state with no later sibling of its owner, or only an earlier one, is not stranded  <-- pinned defect
PASS  a state a version reaches as an ancestor is not stranded, whatever its siblings  <-- pinned defect
...
PASS  a version that points at the lower state keeps it from being stranded  <-- pinned defect
PASS  a version that points at a child of the lower state keeps it from being stranded  <-- pinned defect
PASS  a later state of another owner is not a sibling  <-- pinned defect
...
PASS  the stranded view without a deltas export is refused: it counts rows  <-- pinned defect
...
PASS  a state_parents export that repeats a state is refused  <-- pinned defect
...
PASS  a parent state that is in the states export but not in the state's lineage is refused  <-- pinned defect
...
PASS  a replica with a Replica item is REGISTERED, one with only SYNC_ versions is DETACHED  <-- pinned defect
PASS  a child version is dropped before its parent  <-- pinned defect
PASS  the plan predicts the floor moves from 17 to 50 when the three replicas' versions are gone
...
PASS  the drain wait states its poll rule and says the numbers are not Esri's
...
PASS  beside a Sync Replica item an unmatched SYNC_ id is REFUSED, and none of its versions is listed for dropping  <-- pinned defect
...
PASS  a replica version with an ordinary child version is kept, and the floor does not move  <-- pinned defect
...
PASS  a version whose child stays is kept, and so is every ancestor of it in the drop set  <-- pinned defect
PASS  drops are ordered deepest first across replicas, and each entry keeps that order  <-- pinned defect
...
PASS  the id list '' is refused  <-- pinned defect
PASS  the id list 'all' is refused  <-- pinned defect
PASS  the id list '56,' is refused  <-- pinned defect
PASS  the id list '56,x' is refused  <-- pinned defect
PASS  the id list '-1' is refused  <-- pinned defect
...
PASS  the id list '5.0' is refused  <-- pinned defect
PASS  the id list '\xb2' is refused  <-- pinned defect
PASS  the id list '\u0663' is refused  <-- pinned defect
PASS  the id list '56,56' is refused  <-- pinned defect
...
PASS  a version_tree export that repeats a version is refused  <-- pinned defect
...
PASS  parent links that loop are refused, not walked for ever  <-- pinned defect
...
PASS  the sqlserver replicas query sets QUOTED_IDENTIFIER ON before its XML call  <-- pinned defect
...
PASS  the disaster report is, line for line, the text the README quotes  <-- pinned defect
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
PASS  a header-only replicas export is read as no replicas, not exit 2  <-- pinned defect
...
PASS  a lineages export without row_count exits 2: a file that lost rows could not be told from a whole one  <-- pinned defect
PASS  a versions export without exported_at exits 2: on an idle geodatabase every holder would look young  <-- pinned defect
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
PASS  a unique prefix of --apply (--ap) is refused by the parser, not read as --apply  <-- pinned defect
...
PASS  a holder under the age limit exits 0 by default and under never, and 1 under any
...
PASS  --exit-on never does not turn a failure into exit 0  <-- pinned defect
...
PASS  the prefix --exit is refused by the parser, not read as a flag  <-- pinned defect
PASS  the prefix --unreg is refused by the parser, not read as a flag  <-- pinned defect
PASS  the prefix --state-p is refused by the parser, not read as a flag  <-- pinned defect
PASS  the prefix --version-t is refused by the parser, not read as a flag  <-- pinned defect
PASS  the prefix --rep is refused by the parser, not read as a flag  <-- pinned defect
PASS  the parser is built with allow_abbrev=False
PASS  --self-test --exit-on exits 2, not ignored  <-- pinned defect
PASS  --sql --exit-on exits 2, not ignored  <-- pinned defect
PASS  --self-test --unregister-plan exits 2, not ignored  <-- pinned defect
PASS  --sql --unregister-plan exits 2, not ignored  <-- pinned defect
PASS  --self-test --state-parents exits 2, not ignored  <-- pinned defect
PASS  --sql --state-parents exits 2, not ignored  <-- pinned defect
PASS  --self-test --version-tree exits 2, not ignored  <-- pinned defect
PASS  --sql --version-tree exits 2, not ignored  <-- pinned defect
PASS  a real process exits 1 on blockers, 0 under never, 0 on a holder under the limit and 2 on a failure
...
PASS  --state-parents without --deltas exits 2  <-- pinned defect
...
PASS  a header-only deltas export with the stranded view exits 2, not 0  <-- pinned defect
...
PASS  the plan wrote no file and loaded no arcpy, pyodbc or socket module  <-- pinned defect
...
PASS  --self-test takes no other flag
PASS  --self-test --max-age-days 5 exits 2, not ignored  <-- pinned defect
PASS  --self-test --max-age-days 30 exits 2, not ignored  <-- pinned defect
PASS  --self-test --as-of junk exits 2, not ignored  <-- pinned defect
PASS  --sql --as-of exits 2, not ignored  <-- pinned defect
...
PASS  check() and raises() really do record a failure  <-- pinned defect
PASS  outcome() turns a crash into a value a check can fail on
PASS  a run with a failure prints it and exits 1  <-- pinned defect
PASS  importing the file runs nothing and exposes the core
PASS  the import probe writes no bytecode beside the script  <-- pinned defect
--------------------------------------------------------------------
361 assertions, 0 failed
```

The full run prints all 361 assertions. The `...` lines are where this block is cut.

## What already exists

- **Esri Technical Article 000011719**, "How To: Determine if there are detached replica system
  versions in the geodatabase". It is the authoritative manual procedure. It names the replica
  system versions, gives the SQL that lists the registered replicas, and tells you to compare the
  two lists by eye. It is correct, and it covers one of the four things that hold a floor.
- **`StateLineageCheck.py` in [phillegard/ArcGIS_Maintenance](https://github.com/phillegard/ArcGIS_Maintenance)**.
  It counts the rows in the state table through `arcpy` and compares that count with warning and
  critical thresholds. It also prints the lineage row count and an "Orphaned States" count. A
  growing state count is a real early signal, and the script is easy to schedule. Its orphaned
  state count uses the query that "Why the obvious version is wrong" shows to be misleading.

- **Esri's Unregister Replica and Delete Version tools.** The
  [Unregister Replica](https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/unregister-replica.html)
  page says the tool does not delete the replica's version. The
  [Delete a traditional version](https://doc.esri.com/en/arcgis-pro/latest/help/data/geodatabases/overview/delete-a-traditional-version.html)
  page says that a geodatabase administrator who deletes a version deletes its descendant
  versions too. Both are accurate, and they do the work. They do not order a set of leftover
  versions, and they do not say where the floor will sit afterwards. The plan in this tool
  does that, and it runs neither tool.

A search of GitHub repositories on 2026-10-12 with `gh search repos` found no repository for
"UnregisterReplica arcpy", "geodatabase compress state_lineages", "arcgis replica version
cleanup script" or "enterprise geodatabase orphan states". A code search for `UnregisterReplica`
returned only wrappers of Esri's tool and unrelated projects. That says nothing about private
code, and it is not a claim that no such tool exists.

Neither of the first two says which version or replica holds the floor, and neither runs without a live
connection. For geodatabase replicas, this tool does the comparison that the article describes.
It adds the other three holders, and it runs offline against exported rows. For feature service
replicas, such as the distributed collaboration replicas in the story, it cannot do that
comparison. It reports their stuck system versions as unresolved blockers (see the worked
example).

## Requirements

Python 3.9 or newer and nothing else. No `arcpy`, no database driver, no third-party package, no
network. You run the SQL yourself, with the client you already use, and give the tool the
results as CSV or JSON.

The same 361 assertions pass everywhere they were run: Windows (Python 3.13.2), Ubuntu 24.04
(Python 3.12.3) and Windows (Python 3.9.25).

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
python compressfloor.py ... --deltas deltas.csv --state-parents state_parents.csv
python compressfloor.py ... --replicas replicas.csv --version-tree version_tree.csv --unregister-plan 56,57
python compressfloor.py ... --exit-on never
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
| `--state-parents` | off | The parent state and owner of each state. Needs `--deltas`. Adds the stranded sibling state view. |
| `--version-tree` | off | The parent version of each version. Orders the drops in `--unregister-plan`. |
| `--unregister-plan` | off | Replica ids, comma separated. Needs `--replicas` and `--version-tree`. Prints an advisory plan and runs nothing. |
| `--exit-on` | `blocker` | What makes the exit code 1: `blocker`, `any` or `never`. Exit 2 is a failure at every level. |
| `--max-age-days` | `30` | A holder that DEFAULT moved past more than this many days ago is a blocker. `0` makes a blocker of every holder and orphaned lineage older than the as-of time itself. |
| `--as-of` | the export time | Measure ages from this time instead of the `exported_at` time in the versions export. `YYYY-MM-DD` or `YYYY-MM-DD HH:MM:SS`, 24-hour. A time earlier than the newest timestamp in the exports exits 2, because every age would be negative. |
| `--report` | off | Write the report to this file. Needs `--apply`. |
| `--apply` | off | Write `--report`. Without it nothing is written. |
| `--sql` | off | Print the SELECTs for `sqlserver`, `postgresql` or `oracle`, and exit. Takes no other flag. |
| `--self-test` | off | Run the assertions and exit. Takes no other flag. |

The parser refuses an abbreviated flag, so `--ap` is not `--apply`. The self-test pins this for
`--ap`, `--exit`, `--unreg`, `--state-p`, `--version-t` and `--rep`. `--sql` and `--self-test`
refuse every other flag, the four added ones included.

A file whose first non-blank character is `[` or `{` is read as JSON, and it must be an array
of objects. Any other file is read as CSV. Column names are matched without regard to case, and
a UTF-8 byte order mark is removed.

Ages are measured from the database clock at the time of the export, not from the clock of the
machine that runs the tool. The versions query records that clock in its `exported_at` column.
The same exports therefore give the same report on any day. A versions export without
`exported_at` exits 2. Without it, the only clock would be the newest state or replica log
event. Take a geodatabase where editing stopped soon after a replica stalled. There, every
holder would look young for ever, and a nightly run would exit 0 each night. The self-test pins
this case.

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
| `STRANDED_SIBLING_STATE` | it holds delta rows | Only with `--state-parents`. An unreferenced state with a later state of the same parent and owner (see "Stranded sibling states"). |

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
compressfloor: 9 version(s), 14 state(s), 4 replica(s), as of 2026-09-20 02:05:00
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
  REPLICA                    replica 70 'assets_field'
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

The worked example uses registered geodatabase replicas, so it can show every class. The story's
two replicas were distributed collaboration replicas. The geodatabase stores those as `Sync
Replica` items, and the tool cannot tie a `SYNC_` version to one (see Limits). This is the same
fixture with replicas 56 and 57 exported as `Sync Replica` items, run through the command line:

```
$ python compressfloor.py --versions versions.csv --states states.csv --lineages lineages.csv --replicas replicas.csv --replica-log replica_log.csv --deltas deltas.csv
compressfloor: 9 version(s), 14 state(s), 2 replica(s) and 2 sync replica(s), as of 2026-09-20 02:05:00
DEFAULT (SDE) is at state 9000
compress floor: state 17, 253 day(s) old
  compress can fold edits into the base tables no further than state 17
  delta rows compress cannot fold: 1,809,012

BLOCKERS (5)
  UNRESOLVED_REPLICA_VERSION replica 56
      versions: SYNC_SEND_56_0 (state 17), SYNC_RECEIVE_56_0 (state 17)
      forks from DEFAULT at state 17, held for 231 day(s); this holds the floor
      highest sync generation in its version names: 0
      it has never advanced past sync generation 0
      delta rows on its own branch: 0
      no geodatabase replica has this id, and 2 sync replica(s) exist, whose version ids Esri does not document. It may be one of them: do not delete it on TA 000011719's word
  UNRESOLVED_REPLICA_VERSION replica 57
      versions: SYNC_SEND_57_0 (state 18)
      forks from DEFAULT at state 17, held for 231 day(s); this holds the floor
      highest sync generation in its version names: 0
      it has never advanced past sync generation 0
      delta rows on its own branch: 0
      no geodatabase replica has this id, and 2 sync replica(s) exist, whose version ids Esri does not document. It may be one of them: do not delete it on TA 000011719's word
  UNRESOLVED_REPLICA_VERSION replica 48
      versions: SYNC_SEND_48_2 (state 50)
      forks from DEFAULT at state 50, held for 199 day(s)
      highest sync generation in its version names: 2
      delta rows on its own branch: 0
      no geodatabase replica has this id, and 2 sync replica(s) exist, whose version ids Esri does not document. It may be one of them: do not delete it on TA 000011719's word
...
VERDICT: 5 blocker(s). Compress will keep exiting 0 and will not fold past state 17.
```

The run still exits 1, and it still says that each replica never advanced past generation 0.
It does not name the replicas, and it does not say whether they are stalled or detached. Replica
48 is `UNRESOLVED_REPLICA_VERSION` too, because a `Sync Replica` item exists. Replicas 70 and 99
are still `Replica` items, so the header counts 2 replicas and 2 sync replicas. The self-test
pins these three classifications and the header line.

## Exit codes for a daily monitor

| Code | Meaning |
|---|---|
| 0 | No finding at the `--exit-on` level. By default this means nothing in the exports holds the floor past the age limit. The exports do not show state locks, so this is not proof that nothing holds it (see Limits). |
| 1 | At least one finding at that level. By default, a finding is a blocker. |
| 2 | The tool could not do its job: an export could not be read or was a zero-byte file, the exports disagree with each other, a flag was wrong, `--as-of` is earlier than the exports, `--report` could not be written, or the tool itself hit an error. A header-only CSV or a JSON `[]` is read as zero rows, not as exit 2 (see Limits). |

Exit 2 is kept apart from exit 1 on purpose. A monitor that cannot read its input must never
look like a monitor that found nothing, and it must never look like a finding either. Exit 2
does not depend on `--exit-on`: a failure is exit 2 at every level.

`--exit-on` chooses what counts as a finding. Without the flag, the tool behaves as it did
before the flag existed.

| `--exit-on` | Exit 1 when | Use it when |
|---|---|---|
| `blocker` (default) | Any finding is a blocker. A stranded state that holds delta rows is a blocker. | The monitor must alert on a problem. |
| `any` | Anything is listed, a holder under the age limit included. | You want to see every version that is behind DEFAULT before it ages into a blocker. |
| `never` | Never. | The scheduler treats every non-zero exit as a failure, and you read the report for the findings. |

Under `never`, exit 0 means that the tool ran. It does not mean that the exports are clean. A
monitor that uses `never` must read the report. Its `VERDICT` line says what the run found.

A monitor that must tell a finding from a failure reads the exit code. This is a POSIX shell
version:

```sh
python3 compressfloor.py --versions versions.csv --states states.csv --lineages lineages.csv > floor.txt
case $? in
  0) echo "monitor: clean" ;;
  1) echo "monitor: findings, see floor.txt" ;;
  *) echo "monitor: compressfloor itself failed" ;;
esac
```

This is a PowerShell version:

```powershell
python compressfloor.py --versions versions.csv --states states.csv --lineages lineages.csv | Out-File -Encoding utf8 floor.txt
switch ($LASTEXITCODE) {
    0       { "monitor: clean" }
    1       { "monitor: findings, see floor.txt" }
    default { "monitor: compressfloor itself failed" }
}
```

Both were run on the self-test's disaster fixture, written out as CSV files. Each run added one
argument at the end of the `compressfloor.py` line. This is what each printed:

| Added argument | Windows, Git Bash `sh` | Windows PowerShell 5.1 | Ubuntu 24.04 `sh` |
|---|---|---|---|
| none | `monitor: findings, see floor.txt` | `monitor: findings, see floor.txt` | `monitor: findings, see floor.txt` |
| `--exit-on never` | `monitor: clean` | `monitor: clean` | `monitor: clean` |
| a `--lineages` file that does not exist | `monitor: compressfloor itself failed` | `monitor: compressfloor itself failed` | `monitor: compressfloor itself failed` |

The self-test also starts real processes of the tool. It checks that the process exit codes are
1 on blockers, 0 under `never`, 0 for a holder under the age limit, and 2 for a failure.

The exports are checked against each other before anything is reported. Each of these stops the
run with exit 2:

- a version that points at a state that is not in the states export
- a state on a lineage that is not in the lineages export
- a states export without state 0
- a versions export without exactly one DEFAULT row
- a state that appears twice in the states export, a version (owner and name) that appears
  twice in the versions export, or a lineage and state pair that appears twice in the lineages
  export
- an `objectid` that appears twice in the replicas export, or a replica `id` that two `Replica`
  rows share
- a versions export file without an `exported_at` column, or with an `exported_at` that is blank
  or differs between rows
- an `exported_at` time earlier than the newest timestamp in the exports
- two columns in one export whose names differ only in case, such as `STATE_ID` and `state_id`
- a JSON object that has one key twice, because a JSON reader keeps only the last value and the
  verdict would then depend on key order
- a negative state id, lineage name or lineage id, which Esri never writes
- an export file without a `row_count` column
- an export whose number of rows differs from its `row_count`, or whose rows do not all hold
  the same `row_count`
- a `state_parents` export that holds other states than the states export, repeats a state, or
  gives a state a parent that its lineage does not show
- a `version_tree` export that holds other versions than the versions export, repeats a version
  or a version id, names a parent version that it does not hold, or has parent links that loop
- the stranded view without a deltas export

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

A paged copy can also repeat one row and lose another, and the count then stays right. None of
the state, version, lineage and replica item tables can hold one key twice, so a repeated key in
those exports exits 2. In the replicas export the key is the item `objectid`, and for a
`Replica` item also its `id`. A lost `Replica` row is the dangerous case: its live replica's
system versions would be reported as detached, and TA 000011719 would send you to delete them.
The self-test pins a lost-and-repeated export for versions, lineages and replicas. The deltas
and replica log exports can repeat rows legitimately, so they are not checked this way.

## Stranded sibling states

An editor saves, and the version keeps a later state. The edit that was saved first stays in a
state that no version reaches. The next compress removes states that no version references. This
view lists those states before the compress runs.

It needs two more exports: `--deltas`, to count the rows, and `--state-parents`, which holds
each state's parent state and owner. A state is stranded when both of these are true:

1. No version points at the state, and no version has it as an ancestor.
2. Another state has the same parent state and the same owner, and a larger state id.

A stranded state that holds delta rows is a blocker, whatever its age. A compress can remove
it at any time, so its age does not matter. A stranded state with no delta rows is listed and
is not a blocker. Every stranded state is also inside an `ORPHANED_STATES` lineage. The two
answer different questions, so the report gives the stranded states their own section and count.
Without `--state-parents`, nothing in the report changes.

This is the synthetic fixture from the self-test, written out as CSV files. Its stranded states
are 7 and 13. State 11 and state 21 are unreferenced too, but each is the newest state of its
owner, so they are not stranded. State 16 has a later sibling, but a version reaches it through
its child 17. Only the end of the report is shown:

```
$ python compressfloor.py --versions versions.csv --states states.csv --lineages lineages.csv --deltas deltas.csv --state-parents state_parents.csv
...
STRANDED SIBLING STATES (2)
  STRANDED_SIBLING_STATE     state 7, owner ED, parent state 5
      no version points at state 7 or reaches it through its lineage
      later state(s) with the same parent and owner: 8 (a version reaches it)
      delta rows in this state: 120; created 4 day(s) before the as-of time
      a compress removes states no version references: extract these rows first
  STRANDED_SIBLING_STATE     state 13, owner MAP, parent state 5
      no version points at state 13 or reaches it through its lineage
      later state(s) with the same parent and owner: 14 (a version reaches it)
      delta rows in this state: 0; created 1 day(s) before the as-of time
      a compress removes states no version references, and this one holds no delta rows

VERDICT: 1 stranded state(s) hold delta rows that no version reaches. The next compress removes states no version references: extract the rows first.
```

Exit code 1. With `--exit-on never` the same run exits 0.

What was shown, and what was not:

- **Shown, on synthetic rows: no version points at a stranded state.** The self-test checks
  this clause for each stranded state with `ancestors()`, which reads the state lineage table.
  The tool finds the stranded states by a different walk, from each version down to DEFAULT's
  lineage. The self-test runs the same check on a kept sibling, an ancestor and DEFAULT's own
  state, and it finds all three reached, so the check is not empty. It also runs four cases
  that must not be stranded: a version that points at the lower state, a version that points
  at a child of the lower state, a later state of another owner, and a later state that has
  only an earlier sibling.
- **`UNVERIFIED`: that a lost save is why such a state exists.** The idea comes from one
  observation on one production geodatabase, which was not reproduced here. No Esri page that
  was read describes a save that ends in a sibling state. Undone work can leave the same
  shape, so read the list as candidates. The tool proves that no version reaches the state. It
  does not prove why.
- **`UNVERIFIED`: that a compress deletes the delta rows of an unreferenced state.** The
  Compress tool page says compress removes "states not referenced by a version and redundant
  rows". It does not say in so many words that the rows of an unreferenced state go with it.
  Extract the rows before you compress, as the report says.

The `state_parents` export is checked against the lineages. Esri documents `parent_state_id`
as "redundant with the last value of the lineage list". The tool therefore refuses a state
whose parent is not the id before it in its own lineage. It also refuses an export that holds a
different set of states than the states export does.

## Unregister plan

`--unregister-plan 56,57,48` prints an ordered plan. The plan unregisters the replicas with
those ids and then drops the system versions that they leave. The tool runs none of it. It
never calls `UnregisterReplica`, never deletes a version, never connects to a database, and
never imports `arcpy` or a database driver. A person signs for every step.

It needs `--replicas` and `--version-tree`. The id list is an allowlist: digits separated by
commas. There is no `all`. Only the versions of the ids in the list are planned. Each id gets
one of four statuses:

| Status | When | What the plan does |
|---|---|---|
| `REGISTERED` | A `Replica` item has the id. | Unregister it, wait, export again, then list its versions in drop order. |
| `DETACHED` | `SYNC_` versions carry the id. No `Replica` item has it, and no `Sync Replica` item exists. | Send the reader to TA 000011719, then list the versions in drop order. |
| `REFUSED` | `SYNC_` versions carry the id. No `Replica` item has it, and a `Sync Replica` item exists. | Plan nothing. Esri does not say which number a `Sync Replica` item's versions carry, so these versions may be live. |
| `ABSENT` | Nothing in the exports has the id. | Plan nothing. |

For a `REGISTERED` replica that is not a blocker in the report, the plan adds a warning. The
replica may be live. The allowlist is your decision, and the plan does not override it.

**Drop order.** The version tree comes from `--version-tree`. The plan ends with one
`DROP ORDER` list for every replica in the allowlist, children first, deepest first. One list
is needed because a version of one replica can be the child of a version of another. A version whose child version is not in the drop set stays, and
so does every ancestor of it in the set. Esri says that when the geodatabase administrator
deletes a version, its descendant versions are deleted too. A replica system version with an
ordinary version under it is therefore never listed for dropping, because the drop would remove
the ordinary version as well. The plan prints the child's name.

**Drain wait.** The Unregister Replica tool page says the tool does not delete the replica's
version. The plan therefore tells you to export again and to run the plan again. A replica that
has no `Replica` item is then `DETACHED`, and the versions that it left are listed in drop
order. Between the unregister and the re-export, the plan prints a poll rule: count the
replica's `SYNC_` versions every 30 seconds, go on when the count is unchanged for 3 polls, and
give up after 1,800 seconds. These three numbers are `UNVERIFIED`. They come from one
operator's runs on one geodatabase. No Esri page gives them. They are constants at the top of
the file, `DRAIN_POLL_SECONDS`, `DRAIN_STABLE_POLLS` and `DRAIN_TIMEOUT_SECONDS`. Change them
for your estate.

**Predicted floor.** The plan also says where the compress floor would sit if every listed
version were gone and a compress had run. It runs the same analysis on the exports with those
versions removed. It assumes that nothing else changes.

This is the fixture from the self-test, written out as CSV files, with the allowlist
`56,57,48`. The report comes first, and the first `...` stands for it. The long Esri notes in steps 1 to 4 are cut with `...` after the first words:

```
$ python compressfloor.py --versions versions.csv --states states.csv --lineages lineages.csv --replicas replicas.csv --replica-log replica_log.csv --deltas deltas.csv --version-tree version_tree.csv --unregister-plan 56,57,48
...
UNREGISTER PLAN for replica(s) 56, 57, 48
nothing was run and nothing is written: this is advice for a person to sign for
compress floor now: state 17. If every version listed for dropping is dropped and a compress runs: state 50.

replica 56 'parcels_to_web': REGISTERED
  1. Outside this tool: confirm the replica is abandoned and no partner still syncs with it. ...
  2. Unregister it as the replica owner or the geodatabase administrator: UnregisterReplica(<geodatabase>, "parcels_to_web"). ...
  3. Esri says the tool does not delete the replica's version. Wait for the release to end: ...
  4. Export the tables again and run this plan again with the same list. ...
  5. Versions of this replica in the exports now are numbered in the DROP ORDER list below. Drop only those still there at the last export.

replica 57 'roads_to_web': REGISTERED
  ...
  5. Versions of this replica in the exports now are numbered in the DROP ORDER list below. Drop only those still there at the last export.

replica 48: DETACHED
  1. Outside this tool: read Esri TA 000011719 and confirm no live replica owns this id.
  2. Versions of this replica in the exports now are numbered in the DROP ORDER list below. Drop only those still there at the last export.

DROP ORDER (children first, deepest first, across every replica above)
  drop 1: SYNC_RECEIVE_56_0 (owner SDE, state 17, replica 56)
  drop 2: SYNC_SEND_48_2 (owner SDE, state 50, replica 48)
  drop 3: SYNC_SEND_56_0 (owner SDE, state 17, replica 56)
  drop 4: SYNC_SEND_57_0 (owner SDE, state 18, replica 57)

Esri documents Delete Version for traditional versions. Whether it can address a replica system version is not documented, and this tool did not test it.
```

In the fixture, `SYNC_RECEIVE_56_0` is a child of `SYNC_SEND_56_0`. That nesting is synthetic.
Esri does not document how replica system versions nest, so the self-test builds the shape to
exercise the ordering. The plan reads the real nesting from your `version_tree` export.

The self-test runs the plan through the command line and checks that it wrote no file and
loaded no `arcpy`, `pyodbc` or `socket` module. With `--apply`, the plan goes into the
`--report` file, and that file is the only one written.

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
```

</details>

<details>
<summary>PostgreSQL</summary>

```sql
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
```

</details>

<details>
<summary>Oracle</summary>

```sql
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
```

The article warns that the replica query in Oracle needs EXTPROC configured.

</details>

### How the SQL was tested

All three dialects were run, exactly as printed by `--sql`, against a synthetic schema in a
throwaway container: PostgreSQL 16 with `psql`, SQL Server 2022 with `sqlcmd` and Oracle
Database Free with SQL\*Plus. The last run used the current `--sql` text, with `exported_at`,
and the current tool. Each query was saved in its own file and run with the procedure in the
tables above, and nothing else was done to the output. Each schema used the table and column
names above. It was filled with the self-test's fixture, including 1.8 million synthetic delta rows
across two versioned tables. It also held one `Sync Replica` item with a `<GPSyncReplica>`
definition whose `ID` is `-1`.

On each DBMS, every row of the versions export held the same `exported_at`, the container's
clock, and the tool exited 1. Its ages were measured from that clock. The run was then repeated
with `--as-of 2026-09-20 02:05:00`, the fixture's own export time. For each DBMS that report was
identical, line for line, to the tool's report on the same rows written directly as CSV. That
report is the first one above, with two differences, both caused by the `Sync Replica` item.
The header counts `4 replica(s) and 1 sync replica(s)`, and replica 48 is
`UNRESOLVED_REPLICA_VERSION`. Each export carried its `row_count`. When the last row of a live
lineages export was deleted, the run exited 2 on all three DBMSs.

The first live run found one defect. `sqlcmd` refused the replica query with Msg 1934, because `sqlcmd`
connects with `QUOTED_IDENTIFIER` off and SQL Server refuses an XML `.value()` call under that
setting. The query now sets `QUOTED_IDENTIFIER ON`, and an assertion pins it. Microsoft documents
that an XML method needs the setting on, and that the ODBC and OLE DB drivers turn it on. SSMS was not
part of the test, and no source is cited for its default.

These were synthetic schemas, not geodatabases that ArcGIS created. See Limits.

The two queries added later, `state_parents` and `version_tree`, were run on PostgreSQL only.
They ran with `psql --csv` in a throwaway PostgreSQL 16 container with no published port,
against synthetic `sde_versions`, `sde_states` and `sde_state_lineages` tables. The tool then
read the exports with a fixed `--as-of`. The stranded view and the unregister plan were
identical, line for line, to the same runs on CSV files written directly. The SQL Server and
Oracle versions of these two queries were not run. Each is the same SELECT on that DBMS's
table names, and each carries `row_count`.

## Why the obvious version is wrong

**"Compress exits 0" is not a check.** Compress succeeds whenever it removes what it is allowed to
remove, even if that is nothing. Its success status says that it ran, not that it got anywhere.

**A state count with a threshold alarms late and names nothing.** The count grows for months
before it crosses any threshold that a busy geodatabase would tolerate. When it does cross,
it says that there are too many states, not which version keeps them.

**Counting states that no version points at is wrong.** The tempting query is "the states whose
`state_id` is not any version's `state_id`". `StateLineageCheck.py` reports this count as
"Orphaned States":

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
- **One clock.** A `Z` or `+hh:mm` zone on a timestamp is ignored, because every timestamp comes
  from one database. Fractional seconds are dropped. Any other text after the seconds, such as a
  12-hour `PM`, exits 2. The PostgreSQL versions query reads
  `LOCALTIMESTAMP`, the time in the session's time zone. If that zone differs from the one the
  states were written in, every age shifts by the difference. An `exported_at` that is earlier
  than the newest state exits 2.
- **Rows without an export time.** A script that imports the tool and passes rows to `build()`
  directly may leave out `exported_at`. Ages are then measured from the newest timestamp in the
  rows. On a geodatabase where editing has stopped, that makes a stalled holder look young. The
  command line refuses a versions export without `exported_at` for this reason.
- **Everything is read into memory.** A synthetic export of 1,500 versions, 11,496 states and
  3.4 million lineage rows took 14.7 and 14.9 seconds in two runs (Python 3.13.2, Windows
  workstation), almost all of it spent reading the CSVs. The generator is not in this
  repository. The time grows with the number of rows.
- **Stranded states are candidates.** The tool proves that no version reaches the state. It
  does not prove that a lost save made it, and it does not prove that a compress deletes the
  state's delta rows. Both are `UNVERIFIED` (see "Stranded sibling states").
- **The unregister plan advises only.** It runs nothing. Its poll rule of 30 seconds, 3 polls
  and 1,800 seconds is not from Esri. Esri documents Delete Version for traditional versions,
  and whether it can address a replica system version is not documented. This tool did not
  test it. Esri also says that several replicas can share one replica version, and the plan
  matches a version to a replica by its `SYNC_` name only. A shared version under another name
  is not seen.
- **The version nesting in the plan's fixture is synthetic.** Esri does not document how
  replica system versions nest. The plan reads the real nesting from your `version_tree`
  export.
- **Nothing is fixed.** The tool names the holders. Deleting a version, re-syncing a replica or
  removing a detached replica version is a change that somebody signs for. Follow TA 000011719
  for a version the tool calls detached, and do not delete any other replica system version.
- **A CSV export needs its header row.** A zero-byte file exits 2, and so does a header that
  lacks the export's columns, `row_count` included. A correct header with no data rows is read
  as zero rows, and so is a JSON `[]`. No row then carries a `row_count`, so an export that lost
  every row cannot be told from an empty table. A `--replicas` export like that makes every
  `SYNC_` version detached, so check that the replica query really returned nothing before you
  act on it.
- **A deltas export with a header and no rows is not clean.** With `--state-parents`, the tool
  exits 2 when stranded states exist and the deltas export holds no rows, because it cannot
  tell which of them hold rows. Without `--state-parents`, a header-only deltas file is read as
  zero rows. The report then says `delta rows compress cannot fold: 0 (the deltas export
  holds no rows: check that step 1 of the deltas SQL returned lines)`. The exit code does not
  change.
- **A stale export is reported as it was.** The tool does not compare `exported_at` with the
  current time. If your export step fails and leaves last month's files, a daily monitor
  reports on the old data and can exit 0. Check the file dates in the monitor.
- **A stranded state is a candidate only.** Undone or abandoned work can leave the same shape
  as a lost save, and a stranded state is a blocker whatever its age. A nightly monitor can
  therefore exit 1 on candidate evidence. `--exit-on never` or `--exit-on any` lets you choose
  how loudly it alerts. Without `--state-parents`, the stranded view is absent: an orphaned
  state shows only as `ORPHANED_STATES`, and under the age limit it exits 0.
- **Exports must be UTF-8.** A UTF-16 file, which is what a Windows PowerShell 5.1 `>`
  redirect writes, or an ANSI file exits 2 with a decode error. Save with `-Encoding utf8` or
  the client's UTF-8 option. The error names a byte and a position, not the content.
- **`--report` overwrites the file it names.** With `--apply` it writes without asking, so
  never point it at an export. The tool does not compare the report path with the input paths.
- **A file that starts with `[` or `{` is read as JSON.** A CSV whose first header starts with
  one of those characters exits 2.
- **`--as-of ""` counts as not given.** An unset shell variable there silently uses the
  export's `exported_at`.
- **A numeric cell is read the way Python reads an integer.** A spreadsheet-edited value such
  as `1_0` is read as 10, non-ASCII digits are accepted, and so is a leading `+`. `SYNC_`
  names are matched with `\d`, which also matches non-ASCII digits. A negative `delta_rows` is
  accepted and summed. A SQL export never writes these.
- **A state 0 row with a blank or `NULL` `parent_state_id` exits 2.** The code does not use
  state 0's parent, but the cell is still converted to a number. Esri does not document the
  value. Write -1 or 0 in the export if your database holds `NULL` there.
- **A version named `default` in any case, owned by anyone but SDE, exits 2.** The tool counts
  exactly one `DEFAULT` version and refuses the export otherwise.
- **The plan prints a replica name as it is stored.** A quote character in the name is not
  escaped, so read the `UnregisterReplica` line before you paste it into a Python window.
  Step 1 of the deltas SQL joins table owner names into the generated query without quoting
  them, so read the generated lines before you run step 2.
- **A version that two replicas share is listed for dropping.** The plan matches a version to
  a replica by its `SYNC_` name, so unregistering the first replica lists a version that a
  second replica also uses.
- **The drain-wait numbers are a starting point, not a rule.** They come from one operator.
- **Some Esri Community claims were not re-read.** That most `GPSyncReplica` IDs are -1, and
  that `SYNC_` versions kept a compress from reaching state 0, rest on the two threads listed
  under Sources and are `UNVERIFIED` here.
- **Some code changes do not change a verdict.** Mutation testing found operator changes that
  the self-test cannot see. They affect only the row numbers in error messages, the
  state the orphan view uses as a tip (every state in that lineage forks the same way), the order of lines the report sorts
  again, and the numbering of plan steps.
- Only `--report` is ever written, and only with `--apply`. `--self-test` writes its fixture to
  a temporary folder and removes it, and it writes no bytecode cache beside the script. The
  tool opens no socket and imports no network module.
- **A wrong file is not echoed.** If you pass the wrong file as an export, such as a `.env`
  file, the error message names only the columns that the tool knows, and counts the rest. It
  names the export, row and column of a cell it cannot read, but never prints the cell's value.
  The self-test passes a synthetic `.env` line and a synthetic JSON value and checks that
  neither reaches stderr.
- **Names are printed escaped.** Version, owner and replica names come from the exports. If a
  name holds a character that cannot be printed, such as a newline or an ANSI escape, the whole
  name is printed with Python's `unicode_escape`, for example `\n` and `\x1b`. A name
  therefore cannot add a false `VERDICT` line to the report, or hide the real one in a
  terminal. The self-test pins this case.
- **A detached replica version at DEFAULT's own state is a blocker with an odd VERDICT.** The
  run exits 1. The VERDICT then says `will not fold past state N`, where N is DEFAULT's own
  state, and the report also says that nothing in the version table holds the floor. Read the
  finding, not the VERDICT, in this case.
- **A Replica item with a blank name is planned with an empty name.** The plan prints
  `UnregisterReplica(<geodatabase>, "")`. Pass the replica id instead.
- **The stranded view can be slow.** It scans every sibling of every unreferenced state. A
  service account that saved thousands of times from one parent state makes the run quadratic
  in that count.
- **A newest save is not stranded.** An unreferenced state that is its owner's newest save is
  not stranded. Recently deleted work that has delta rows is therefore not a blocker until the
  orphan age limit passes, and the VERDICT line does not say so.
- **A deleted version can look stranded.** A deleted version leaves the same shape before the
  next compress, when the same owner keeps a later version from the same parent state. Taken
  before a compress, that state is a blocker with delta rows and exits 1 under the default
  level. Export after the compress, or use `--exit-on never` for this view.
- **Exports taken seconds apart can exit 2.** On a busy geodatabase, a state that is created
  between two SELECTs makes the `exported_at` time earlier than the newest timestamp, and the
  run exits 2. The run fails safe, but a nightly monitor can show repeated exit 2 until the
  SELECTs run back to back.
- **Trailing empty CSV columns exit 2.** A CSV that Excel padded with empty columns exits 2
  with `two columns named alike (not shown)`. The run fails safe. Delete the empty columns.
- **A NULL that `sqlcmd` writes as the text `NULL` exits 2.** This holds for `logdate`,
  `sourceendgen`, `targetgen`, `creation_time` and `parent_version_id`. Only a blank cell is
  read as missing, and only `psql` writes one. Replace `NULL` with a blank cell or a value
  before the run. The same trap applies to the DEFAULT row of the `version_tree` export
  (see the next item).
- **The unregister plan needs a blank `parent_version_id` on the DEFAULT row.** Esri does not
  document what DEFAULT stores there. A value such as `-1`, or the text `NULL` that `sqlcmd -W`
  writes, makes the run exit 2. Blank that cell by hand. The plain report does not read this
  export and is not affected.
- **Report line endings differ by host.** `--report` on Windows writes CRLF line endings and
  on POSIX it writes LF, so the same report is not byte-identical across hosts.
- **A failed run keeps the old report.** The `--report` file is written in place and not
  atomically. A run that exits 2 leaves the previous report at that path, and a crash while
  writing can leave a truncated file. Read the exit code before the file.
- **Match the verdict from the start of the line.** A version name is printed as written, and
  only a newline and other unprintable characters are escaped. A name can hold the text
  `VERDICT:`. Use `grep '^VERDICT'`, not a search for the word anywhere.
- **Exports and reports hold account and version names.** The `.gitignore` does not exclude
  them. Keep them outside a git clone, or add them to your own ignore list.
- **On Windows PowerShell 5.1, a plain `Out-File` writes UTF-16.** The monitor example uses
  `-Encoding utf8` for that reason. Without it, `grep` sees a garbled report.
- **A device or a named pipe given as an export is read whole.** There is no size limit. Pass
  regular files.
- **The password prompt of SQL*Plus may land in the export.** `UNVERIFIED`: the recipe
  `@versions.sql > versions.csv` prompts for a password when the connect string has none, and
  this tool did not test where the prompt text goes. Use a wallet or an externally
  authenticated connect string.
- **An unreadable file is described by Python's error text.** `UnicodeDecodeError` names a
  byte and a position, and `json.JSONDecodeError` names a line and a column. Neither quotes
  the content of the file.
- **A self-test wrapper must read the footer.** `main()` can return `None` when the harness is
  wrong, and the self-test then prints nothing. A wrapper must treat exit 0 without the
  `0 failed` footer as a failure.
- **More mutations change no verdict.** The `if v is default: continue` line in `analyse()` is
  redundant. The `holds_floor` field on stranded findings is never read. The self-test cannot
  see them, and they change no verdict.

## Sources

- Esri Technical Article 000011719, "How To: Determine if there are detached replica system
  versions in the geodatabase". Its address, https://support.esri.com/en/technical-article/000011719,
  now leads to Esri's archive page. The copy read here is the
  [Wayback Machine copy of 5 August 2021](https://web.archive.org/web/20210805015432/https://support.esri.com/en/technical-article/000011719).
  It holds the version names, the replica SQL for SQL Server and Oracle and the warning against
  deleting any other replica system version.
- [Esri Technical Article 000010761](https://support.esri.com/en-us/knowledge-base/how-to-discover-what-state-locks-are-blocking-the-compr-000010761),
  "How To: Discover what state_locks are blocking the compress operation on Oracle": a
  connection takes a state lock, and compress cannot compress a locked state.
- [Geodatabase system tables in SQL Server](https://pro.arcgis.com/en/pro-app/latest/help/data/geodatabases/manage-sql-server/geodatabase-system-tables-sqlserver.htm),
  [in PostgreSQL](https://pro.arcgis.com/en/pro-app/latest/help/data/geodatabases/manage-postgresql/geodatabase-system-tables-postgresql.htm)
  and [in Oracle](https://pro.arcgis.com/en/pro-app/latest/help/data/geodatabases/manage-oracle/geodatabase-system-tables-oracle.htm):
  the table names for each DBMS.
- [System tables of a geodatabase in SQL Server (10.1)](https://resources.arcgis.com/en/help/main/10.1/002q/002q00000080000000.htm):
  the columns of `SDE_versions` (`version_id`, `parent_version_id`), `SDE_states` (`owner`,
  `parent_state_id`), `SDE_state_lineages`, `GDB_REPLICALOG`,
  `SDE_mvtables_modified` and `SDE_table_registry`.
- [ArcSDE versioning database schema (10.0)](https://help.arcgis.com/en/geodatabase/10.0/sdk/arcsde/concepts/versioning/dbschema/dbschema.htm):
  the same tables, and the rule that SQL Server and PostgreSQL add the `sde_` prefix. Its
  `STATES` table has `owner` and `parent_state_id`, and it says that `parent_state_id` is
  "redundant with the last value of the lineage list".
- [Versioned tables in a geodatabase in SQL Server (10.2)](https://resources.arcgis.com/en/help/main/10.2/002q/002q0000007v000000.htm):
  the delta tables `a<registration_id>` and `d<registration_id>`, and their `SDE_STATE_ID` and
  `DELETED_AT` columns.
- [XML column queries](https://desktop.arcgis.com/en/arcmap/latest/manage-data/using-sql-with-gdbs/xml-column-queries.htm)
  and [Example: Find domain owners using SQL](https://desktop.arcgis.com/en/arcmap/latest/manage-data/using-sql-with-gdbs/example-finding-domain-owners.htm):
  the `GDB_Items_vw` view in Oracle and the PostgreSQL `xpath()` form.
- [Unregister Replica (Data Management)](https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/unregister-replica.html):
  the tool must be run as the replica owner or the geodatabase administrator, takes a replica name
  (fully qualified) or a replica ID, does not delete the replica's version, and leaves a replica that
  cannot synchronize.
- [Delete a traditional version](https://doc.esri.com/en/arcgis-pro/latest/help/data/geodatabases/overview/delete-a-traditional-version.html)
  and [Delete Version (Data Management)](https://pro.arcgis.com/en/pro-app/latest/tool-reference/data-management/delete-version.htm):
  a geodatabase administrator who deletes a version deletes its descendant versions too.
- [Compress (Data Management)](https://pro.arcgis.com/en/pro-app/latest/tool-reference/data-management/compress.htm)
  and [Replication and versioning](https://pro.arcgis.com/en/pro-app/latest/help/data/geodatabases/overview/replication-and-versioning.htm):
  what compress removes ("states not referenced by a version and redundant rows"), why replica
  system versions need regular syncs for compress to be effective, and that several replicas
  can share one replica version.
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
  Its default pattern is `%SYNC%`, which matches
  the `SYNC_SEND_` and `SYNC_RECEIVE_` replica system versions, and it refuses those anchors by
  default (`--allow-replica-anchors` overrides that). That pairs with the unregister plan here.
  Never prune a replica system version by hand either: TA 000011719 warns that deleting one
  other than a detached one corrupts the replica.
