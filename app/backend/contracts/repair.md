
REPAIR AND REGENERATE (this task's mode is ${mode}; see the first line and REPAIR_INPUT)
- REPAIR_INPUT.currentScenario is the pack as it is on disk; currentFiles holds its referenced files
  in the allowed scopes. ${merge_rule}
  Everything outside REPAIR_INPUT.allowedScopes is restored from disk by the app.
- Golden cases (repair and regenerate alike): never delete or rename a case, and never change its
  category or set, unless a finding or saInstructions names that case. One exception, set by the S1
  arithmetic, not by finding codes: when the finished set would have holdout below total/3 or over 7
  practice cases (e.g. once named holdout cases move to practice), MOVE practice cases that no
  phenomenon or stabilityCaseId uses to holdout (same category; their question appears in no file).
  Never delete cases to fix a count; fix a named case in place, and a case you must drop gets a
  replacement with the same category and set.
- Return the FULL scenario (every section, also the unchanged ones) and in files ONLY new or changed
  files, each with its complete content. Unchanged referenced files are carried over from disk. To
  delete a file, drop its reference; the app removes unreferenced files on apply.
- omittedFiles are read-only (too large to send): keep their references and never return them.
- Findings with "source":"rehearsal" (ids R1..Rn) come from the live rehearsal of the built release: the
  pack validated, but its teaching contrast did not reproduce in the Workshop environment. Change the
  named asset (path) the way the message says (messageEn is the same instruction in English); never
  weaken a validation rule to do it.
- lockedItems were reviewed by the SA. Keep them byte-identical unless a finding requires a change;
  any change resets that review.
- Keep every id stable. Record each change in "changes": [{"finding":"F3" or "sa-instruction",
  "target":"evaluation.goldenSet[case-id]","action":"what you changed, <=300 chars"}].
- Output shape: the same top-level object as above plus "mode":"${mode}" and "changes".
REPAIR SELF-CHECK (run it with the SELF-CHECK above, on the finished JSON)
C1 Every case id of currentScenario is still in goldenSet unless a finding or saInstructions named it.
C2 Recount (S1): no category count and no holdout count is lower than in currentScenario except through
   a change a finding names, and none is below the S1 floors.
${merge_check}
