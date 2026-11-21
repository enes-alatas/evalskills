# Live pilot services: model gateway and trial egress proxy (DRAFT, untested until step 0)

The internal pilot (#121) only. Nothing here serves the public board (ADR-0007). These two run on the **main**
Docker daemon, on their own network `evalskills-egress` (172.31.248.0/24), publish nothing, and run with
`ip_forward=0` (#137, ADR-0003 amended 2026-11-14):

| Service | Port | Holds | Reaches |
| --- | --- | --- | --- |
| `gateway` (`gateway/gateway.py`), `172.31.248.2` | 8443 | the model key, as a compose secret | the upstream in `gateway.json` (NVIDIA Build) |
| `egress-proxy` (`gateway/egress_proxy.py`), `172.31.248.3` | 3128 | nothing | the hostnames in `egress-proxy.json`, public addresses only |

The socket proxy attaches both to each trial's network, which is internal and has no address on the host, and names
them `evals-gateway` and `evals-proxy` in the trial. So a trial reaches these two and nothing else.

## Setup (as root, after `deploy/gvisor/host/` steps 1-8)
1. The key. In bash, paste it at the prompt, never on a command line, and never anywhere else:
   `install -d -m 0700 /etc/evalskills/secrets && (umask 077; read -rs K && printf '%s\n' "$K" > /etc/evalskills/secrets/model_key; unset K)`,
   then `chown 10002:10002 /etc/evalskills/secrets/model_key && chmod 0400 /etc/evalskills/secrets/model_key`.
   Compose mounts a file secret as-is, so the file is owned by the gateway's uid and by nobody else.
2. `sh deploy/gvisor/host/9-live.sh`, then again with `--yes`, with the socket proxy stopped:
   `docker compose up -d --build --force-recreate` on `/opt/evalskills/deploy/live/compose.yaml`.
   `docker logs evalskills-gateway` shows `listening`. Run it again after every `5-code.sh` that changes
   `gateway.json` or `egress-proxy.json`. Each service reads its file once, at start, and a running container keeps
   the file from before the checkout. So the step always recreates both, and fails unless each container reads the
   checked-out file (#234).
3. Trials reach them only through the socket proxy's attach. The host side reaches the gateway at `172.31.248.2`,
   which step 3 names `evals-gateway`.
4. Trials get the proxy from the trial user's Docker client config, `/var/lib/evals/.docker/config.json` (step 8).
   Docker then sets `HTTP(S)_PROXY` and `NO_PROXY` in every container that client creates. Without it, agent
   installs fail, and that is the safe failure.
5. A run token, for each run, copied to the runner's file without ever being printed:
   ```
   docker exec evalskills-gateway python3 -I /opt/gateway/gateway.py issue --config /etc/evalskills/gateway.json \
     --run RUN_ID --out /var/lib/evalskills-gateway/RUN_ID.token
   docker cp evalskills-gateway:/var/lib/evalskills-gateway/RUN_ID.token "$TIER3_GATEWAY_TOKEN_FILE"
   docker exec evalskills-gateway rm /var/lib/evalskills-gateway/RUN_ID.token
   ```
   `usage` shows each run's counts, and `revoke --run RUN_ID` ends a run.

## Step-0 checks
- From a trial container: the gateway answers `GET /v1/models` with a token, `https://registry.npmjs.org/` works
  through the proxy, and everything else (another host, the host's other ports, the LAN) fails.
- Only trials use the proxy: from the app container and from the host, `172.31.248.3:3128` is refused
  (`client not allowed`, or no route). The proxy serves `allowed_clients` (172.31.240.0/21, the trial networks) only. The gateway
  answers anyone who reaches it, but only with a valid run token.
- `docker logs evalskills-egress-proxy` lists every `denied` host. The allowlist is a first guess (Debian and Ubuntu
  mirrors, nodejs.org, raw.githubusercontent.com for nvm's install script, github.com for the nvm clone that
  script makes, registry.npmjs.org). Add a host only after reading why it was needed.
  For the #121 pilot only, `open_egress: true` lets any public hostname through, and every other rule still holds
  (#236, ADR-0003 amended 2026-11-20). Set it back to false before any run beyond the pilot.
- The egress proxy also refuses the host's own addresses (`--deny`), which `9-live.sh` lists with
  `deploy/live/host-addrs.sh`. If the host's public address is only on a NAT in front of it, not on an interface,
  set `EVALSKILLS_DENY_EXTRA=<address>` for `9-live.sh`. Without a list, the proxy won't start.
- Streamed completions arrive with a usage chunk. Calls without one show as `unmetered` in `usage`.
- A call's tokens are counted before the client gets the end of the reply, so the next request is admitted against
  them. A client that hangs up mid-stream is still counted: the gateway reads on to the usage chunk, for at most
  `drain_timeout_s` (120 s).
- A chat request without an output limit gets `max_tokens` set to `max_output_tokens` (4096), or to what the run has
  left if that's less. After `per_run_unmetered` (3) calls whose usage never arrived, the run is refused, so the token
  cap fails closed.

## Running the pilot (#121; after the step-0 checks pass)
The runner is `pipeline/live_pilot.py`. It runs the skills in `pilot/skills.tsv` one at a time, with the baseline
arm on. Each run gets its own token, which is revoked when the run ends, and the gateway's counts go into the run's
record. Records go to `reports/pilot/results.jsonl`, which is never committed: the board shows no pilot result
(ADR-0007), and the public build refuses any `mode: tier3` record.

Prerequisites on the host: `deploy/gvisor/host/` steps 1-9. That gives this repository at `/opt/evalskills`,
SkillEvaluator at the pinned version with its `tier3` extra at `/opt/skillevaluator` (`SKILLEVALUATOR_DIR`), and the
trial user's Docker client `proxies` config. Run it as root from `/opt/evalskills`. Start the socket proxy for the
pilot (`systemctl start evalskills-docker-proxy`), and stop it afterwards: `systemctl stop evalskills-docker-proxy`,
then check that `systemctl is-active evalskills-docker-proxy` says `inactive`. `5-code.sh` and `9-live.sh` refuse
while it runs, because it keeps serving the code it started with (#230). `SKILLEVALUATOR_DIR` must be set: the runner
reads the evaluator's version from `/opt/skillevaluator/.venv` before the first run, and stops if it isn't there.

```
python3 -I pipeline/live_pilot.py --dry-run          # the 16 planned runs; nothing starts
SKILLEVALUATOR_DIR=/opt/skillevaluator DOCKER_HOST=unix:///run/evalskills/docker.sock \
TIER3_GATEWAY_URL=http://evals-gateway:8443/v1 TIER3_AGENT_MODEL=nvidia/nemotron-3-super-120b-a12b \
SKILL_EVAL_LLM_MODEL=nvidia/nemotron-3-super-120b-a12b \
python3 -I pipeline/live_pilot.py --only obra__superpowers__skills__systematic-debugging   # one skill first
```

`tier3.py` refuses to start anywhere but the socket proxy with `--env-mode gvisor`, while the proxy reports the
trial isolation broken, with a real provider key in its environment, or without a token. SkillEvaluator runs as the
trial user (`TIER3_TRIAL_USER`, default `evals`) in a fresh folder under `TIER3_RUN_ROOT` (`/srv/evals/run`), and
root copies the report's plain files to `reports/` when the trial ends (ADR-0003, amended 2026-11-13).
A run that times out, writes no `lift.json` or doesn't succeed is recorded as `failed`, one with no comparable
cases as `inconclusive`, and one whose Dockerfile the proxy refuses as `unsupported`. None of them gets a score.

While a run goes, SkillEvaluator's output is shown on the terminal as it arrives, each line prefixed with the run's
ID, and the same output is kept in the report's `run.log` (#239). The text comes from the trial, so escape sequences
and control characters are removed, lines are cut to 200 characters, and at most 50 lines a second are shown; the
rest are counted and stay in `run.log`. SkillEvaluator prints a "still running" line every minute, so a terminal that
stays silent for longer means something stalled. `TIER3_PROGRESS=0` turns this off.

## Known limits
- Trial-to-gateway traffic is plain HTTP on the local bridge. The per-run token crosses it, and it is worthless
  once the run is revoked.
- Skill code can reach the allowlisted download hosts too. A trial holds no secret, and the skills are public
  (#123, item 6).

Undo: `docker compose -f deploy/live/compose.yaml down -v`, then `rm -rf /etc/evalskills/secrets`.
