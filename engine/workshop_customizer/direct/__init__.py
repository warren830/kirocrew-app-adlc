"""Direct mode: a pack's teaching loop straight on AgentCore, without the Workshop EC2.

The Guided Run (15 upstream scripts over SSM on the Workshop instance) gives the class verdict in about
42 minutes, plus about 17 minutes of cleanup per round. Direct mode runs the part a repair needs, from the
SA's machine, in minutes:

1. ``aws.Provisioner`` creates (or updates in place) the pack's resources under names of their own
   (``names.DirectNames``, never the Workshop's): a knowledge base with the release's own chunking
   (``knowledge-base/create_kb.py``), the tools Lambda and an MCP Gateway, a memory with the Workshop's two
   strategies, an execution role, and a Harness on the default (PUBLIC) network with its skills from S3.
2. ``run.DirectRun`` asks the first-conversation question and every practice question with the baseline
   prompt, switches the Harness to the candidate prompt in place, and asks them again, each question with a
   fresh session and memory actor, as 06 / 09 / 10 do.
3. Once the traces are complete for the evaluator, the Workshop's own judges score them locally: THELMA and
   Mind the Goal (the release's ``evaluators/`` code, ``templates/trace_judge_runner.py``) on the same session
   spans their Lambdas would get, L1 through ``l1.evaluate_run``, and the noise band by re-scoring the
   stability case three times.
4. The step records take the Guided Run's shapes, so ``rehearsal.build_rehearsal`` judges them unchanged; a
   direct run is never a class verdict (``readyForClass`` stays false: only the Guided Run gives one).
"""
