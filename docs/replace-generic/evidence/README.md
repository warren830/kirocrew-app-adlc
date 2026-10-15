# Recorded rehearsals (test inputs)

The tests replay these records through the engine (`tests/rehearsal_replay.py`, `tests/test_rehearsal_localized.py`,
`tests/test_teaching_policy.py`). They are the parts of the live acceptance runs those tests read: each scenario's final
`scenario.yaml` and the rehearsal documents the English-only engine wrote (`workshop-customizer/rehearsal/1`).

Every scenario is synthetic teaching data (invented companies, orders and people); the records carry no account,
resource names or credentials. Keep them as recorded: the tests check their verdicts and schema version.
