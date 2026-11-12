# gvisor_env: SkillEvaluator `--env-mode gvisor` (#137)

This adds an execution mode to SkillEvaluator through its `skillevaluator.env_modes` entry point. Trials run on
the host's **main** Docker daemon, with every container under gVisor (`runsc`). It's SkillEvaluator's hardened
Kata backend with a different runtime. See the docstring in `evalskills_gvisor/__init__.py` for the details.

Host prerequisite (done on the shared host on 2026-11-12, see #137): gVisor installed, then
`runsc install -- --platform=systrap` and `systemctl reload docker`. The default runtime stays runc.

Use it next to SkillEvaluator:

    cd $SKILLEVALUATOR_DIR
    uv run --python 3.13 --extra tier3 --with-editable <repo>/gvisor_env skillevaluator tier3 evaluate ... --env-mode gvisor

Tests: `tests/test_gvisor_env_mode.py` (it runs there; the plain test run skips it).

Not here yet: the socket proxy that refuses non-runsc containers at the API (#137), and the firewall's boot unit.
`pipeline/tier3.py` refuses to start a live run until those are in.

`--with-editable`, not `--with`: uv caches a `--with` path build, so later edits would not be picked up.
