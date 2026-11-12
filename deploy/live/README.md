# Live pilot services: model gateway and trial egress proxy (DRAFT, untested until step 0)

The internal pilot (#121) only. Nothing here serves the public board (ADR-0007). These two run on the **main**
Docker daemon, with host networking, listening on `br-evals` (172.31.248.1), the eval daemon's bridge:

| Service | Port | Holds | Reaches |
| --- | --- | --- | --- |
| `gateway` (`gateway/gateway.py`) | 8443 | the model key, as a compose secret | the upstream in `gateway.json` (NVIDIA Build) |
| `egress-proxy` (`gateway/egress_proxy.py`) | 3128 | nothing | the hostnames in `egress-proxy.json`, public addresses only |

Trials reach these two ports and nothing else (`deploy/gvisor/firewall.sh`).

## Setup (as root, after `deploy/gvisor/` steps 1-3)
1. The key. In bash, paste it at the prompt, never on a command line, and never anywhere else:
   `install -d -m 0700 /etc/evalskills/secrets && (umask 077; read -rs K && printf '%s\n' "$K" > /etc/evalskills/secrets/model_key; unset K)`,
   then `chown 10002:10002 /etc/evalskills/secrets/model_key && chmod 0400 /etc/evalskills/secrets/model_key`.
   Compose mounts a file secret as-is, so the file is owned by the gateway's uid and by nobody else.
2. `docker compose -f deploy/live/compose.yaml up -d --build`. `docker logs evalskills-gateway` shows `listening`.
3. In `/etc/docker-evals/firewall.env`, set `GATEWAY_IP=172.31.248.1`, `GATEWAY_PORT=8443`,
   `PROXY_IP=172.31.248.1` and `PROXY_PORT=3128`, then `systemctl restart docker-evals`.
4. Trials get the proxy from the Docker client config of the user that runs the pilot. In `~/.docker/config.json`:
   `{"proxies": {"default": {"httpProxy": "http://172.31.248.1:3128", "httpsProxy": "http://172.31.248.1:3128",
   "noProxy": "172.31.248.1"}}}`. Docker then sets `HTTP(S)_PROXY` and `NO_PROXY` in every container that client
   creates. Without it, agent installs fail, and that is the safe failure.
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
- Only trials use the proxy: from the app container and from an outside host, `172.31.248.1:3128` is refused
  (`client not allowed`, or no route). The proxy serves `allowed_clients` (172.31.240.0/20) only. The gateway
  answers anyone who reaches it, but only with a valid run token.
- `docker logs evalskills-egress-proxy` lists every `denied` host. The allowlist is a first guess (Debian and Ubuntu
  mirrors, nodejs.org, raw.githubusercontent.com for nvm, registry.npmjs.org). Add a host only after reading why it
  was needed.
- Streamed completions arrive with a usage chunk. Calls without one show as `unmetered` in `usage`.

## Running the pilot (#121; after the step-0 checks pass)
The runner is `pipeline/live_pilot.py`. It runs the skills in `pilot/skills.tsv` one at a time, with the baseline
arm on. Each run gets its own token, which is revoked when the run ends, and the gateway's counts go into the run's
record. Records go to `reports/pilot/results.jsonl`, which is never committed: the board shows no pilot result
(ADR-0007), and the public build refuses any `mode: tier3` record.

Prerequisites on the host: this repository, `uv`, and a SkillEvaluator checkout at the pinned version with its
`tier3` extra (`SKILLEVALUATOR_DIR`). The Docker client `proxies` config from step 4 must be in the runner user's
`~/.docker/config.json`.

```
python3 -I pipeline/live_pilot.py --dry-run          # the 16 planned runs; nothing starts
DOCKER_HOST=unix:///run/docker-evals.sock TIER3_GATEWAY_URL=http://172.31.248.1:8443/v1 \
TIER3_AGENT_MODEL=nvidia/nemotron-3-super-120b-a12b SKILL_EVAL_LLM_MODEL=nvidia/nemotron-3-super-120b-a12b \
python3 -I pipeline/live_pilot.py --only obra__superpowers__skills__systematic-debugging   # one skill first
```

`tier3.py` refuses to start on the main daemon, with a real provider key in its environment, or without a token.
A run that times out, writes no `lift.json` or doesn't succeed is recorded as `failed`, and one with no comparable
cases as `inconclusive`. Neither gets a score.

## Known limits
- Trial-to-gateway traffic is plain HTTP on the local bridge. The per-run token crosses it, and it is worthless
  once the run is revoked.
- Skill code can reach the allowlisted download hosts too. A trial holds no secret, and the skills are public
  (#123, item 6).

Undo: `docker compose -f deploy/live/compose.yaml down -v`, then `rm -rf /etc/evalskills/secrets`.
