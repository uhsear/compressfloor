fix: simplify row_count validation, parse_time, and README accuracy

Removed the redundant half of the row_count control that catches
catches more rows than count records. The self-test validates the fix.

Fixed parse_time to reject 12-hour timestamps and other invalid
suffixes after seconds, not only fractions and time zones.

Corrected README claims about TA 000011719: the article does not
hold version names or replica SQL. Corrected the description of
StateLineageCheck.py: it tests row counts, not the delta compress
that compressfloor handles. Clarified that --self-test rejects only
export and SQL flags, not all other flags.

Review round 4; the loop continues until two clean rounds.
