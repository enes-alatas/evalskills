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

The runner never gets the daemon's socket. It talks to the Docker socket proxy, `gateway/docker_proxy.py`, which
runs as root next to the daemon. Its docstring lists the rules: allowlisted routes and fields, runsc forced, own
objects only, no builds or pulls, binds only from under `/srv/evals/run`, internal networks. The proxy also has a
firewall check, `GET /_evalskills/firewall`. Example config: `deploy/gvisor/docker-proxy.example.json`.

Builds: Compose never builds. When a task has a Dockerfile, `GvisorEnvironment` sends its build context to the
proxy's `POST /_evalskills/build` and gets back a tag, which becomes the trial's prebuilt image. The proxy builds only
allowlisted Dockerfiles (`deploy/gvisor/build/README.md`). A refused one fails the trial with "unsupported
environment", and the runner records it as unsupported, never as a score.

Not here yet:
- The runner's per-trial folders, the eval user, the disk cap, and the proxy's and the firewall's boot units
  (#137, PR C).

`pipeline/tier3.py` refuses to start a live run until those are in.

Accepted risk with binds (#137, host operator's condition 5):
- The skill can write into its own trial's log folders on the host, including symlinks.
- Harbor and SkillEvaluator then read those folders as the eval user and may follow such a symlink. For example,
  `verifier/reward.txt` could point at the per-run gateway token, which would then land in a private report.
- So the eval user must be able to read nothing else of value: no SkillEvaluator config holding keys, and no other
  runs' files. The token is capped and revoked after the run.
- The proxy accepts a bind from any trial folder under the root, not only the container's own trial's. Only the
  runner sets binds: SkillEvaluator refuses host binds in a skill's compose. PR C moves each finished trial's folder
  out of the root, so at most the trials running at the same time are within reach.
- The proxy checks bind sources before the daemon mounts them, so a window remains between its check and the
  mount. One fresh folder per trial, owned by the eval user, keeps that window narrow.

`--with-editable`, not `--with`: uv caches a `--with` path build, so later edits would not be picked up.
