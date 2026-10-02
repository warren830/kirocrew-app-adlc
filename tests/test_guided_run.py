from workshop_customizer.guided_run import GUIDE_STEPS, finish, initial_state, next_runnable, runnable, start


def test_guide_matches_upstream_and_excludes_cleanup():
    scripts = [s.script for s in GUIDE_STEPS]
    assert scripts[0] == "00-setup.sh" and scripts[-1] == "13-judge-stability.sh"
    assert "99-cleanup.sh" not in scripts
    assert len(scripts) == 15


def test_steps_run_sequentially_and_fail_closed():
    state = initial_state()
    assert state["setup"]["label"] == "Environment setup"
    assert state["judge-stability"]["label"] == "Judge stability"
    assert next_runnable(state) == "setup"
    start("setup", state, "t0")
    assert next_runnable(state) is None
    finish("setup", state, passed=True, at="t1", duration=1.2, outputs={"python": "ok"})
    assert next_runnable(state) == "infra"
    start("infra", state, "t2")
    finish("infra", state, passed=False, at="t3", duration=2, error="stack failed")
    assert state["infra"]["status"] == "failed"
    assert state["knowledge-base"]["status"] == "blocked"
    assert not runnable("knowledge-base", state)


def test_failed_step_can_resume_without_replaying_passed_steps():
    state = initial_state()
    start("setup", state, "t0")
    finish("setup", state, passed=True, at="t1", duration=1)
    start("infra", state, "t2")
    finish("infra", state, passed=False, at="t3", duration=1)
    assert runnable("infra", state)
    start("infra", state, "t4")
    finish("infra", state, passed=True, at="t5", duration=1)
    assert next_runnable(state) == "knowledge-base"
    assert state["setup"]["status"] == "passed"


def test_document_version_is_part_of_every_step_and_reset_on_start():
    state = initial_state()
    assert all(step["documentVersion"] is None for step in state.values())
    start("setup", state, "t0")
    state["setup"]["documentVersion"] = "4"
    finish("setup", state, passed=False, at="t1", duration=1)
    start("setup", state, "t2")
    assert state["setup"]["documentVersion"] is None
