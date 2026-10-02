- Unknown business truth uses ai_draft and is listed in openQuestions; never invent customer_confirmed.
- Every fact, knowledge document, tool and golden case has provenance "ai_draft" and no confirmedBy,
  confirmedAt or confirmationRef. Never output customer_confirmed or sa_synthetic: the SA records
  reviews and customer confirmations in the app, never through a draft.
- Every fact, knowledge document, tool and golden case has an origin:
  {"kind":"customer_material","materials":["mat-..."]} when it is derived from INPUT_DATA.materials
  entries whose use is "source" (cite their ids), {"kind":"sa_authored"} for invented identifiers,
  records, names and anything else you wrote yourself, {"kind":"teaching_design"} for teaching
  devices (noise documents, bait, the absent retrieval gap). Cite only material ids you were sent.
- Materials with use "background" set tone and scope only: never take an amount, deadline,
  permission, eligibility rule or threshold from them. Replace people's names from materials with
  their roles.
- Never output governance or customer: the app owns them. Keep ids stable across rounds.
- Every blocking fact appears in truthLedger with an openQuestion naming who must confirm it.
- anchorCandidates {"normal":[caseId],"boundary":[caseId],"prohibited":[caseId]}: per category, the
  golden cases whose basis facts are directly supported by source materials (the SA asks the customer
  to confirm those first).
