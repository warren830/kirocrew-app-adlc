GUIDE NARRATIVE (optional; the Workshop Guides are generated from the scenario, defaults come from labs.teaching)
- labs.guide={"id":"guide-narrative","provenance":"ai_draft", tagline (<=300 chars), scenarioIntro (<=2000),
  memoryLesson (<=600), retrievalContrast (<=1200), stepNotes:{<step id>: text (<=1200)},
  facilitatorNotes:{<step id>: text (<=2000)}, designRationale (<=3000),
  experiments:[{"audience":"student"|"instructor","title" (<=120),"body" (<=1500)}] (at most 6),
  attribution (<=2000)}. Every field besides id and provenance is optional; leave labs.guide out
  instead of writing empty strings. It never carries origin or confirmation fields: provenance is
  always ai_draft and the SA reviews the narrative as one item.
- Step ids: prerequisites, setup, infra, knowledge-base, gateway, skills, agent, memory, conversation,
  eval-env, conversation-rerun, evaluators, baseline, optimize, cost-latency, models, judge-stability, cleanup.
- Teaching prose in the content language, never business truth (rules, amounts and deadlines stay in
  facts[] and the knowledge documents). No code fences or command lines, no Markdown headings, no HTML.
  Links only in attribution and the instructor fields (facilitatorNotes, designRationale, instructor
  experiments).
- Student fields (tagline, scenarioIntro, memoryLesson, retrievalContrast, stepNotes, student experiments,
  attribution) never name a holdout case, its id, question or label, and never the word holdout.
- labs.studentGuideFile / labs.instructorGuideFile only together with labs.guide, as full UTF-8
  Markdown files under guides/ that exist in files. The instructor supplement is instructor-only: it is
  never also a knowledge document, skill, prompt or the student supplement.
