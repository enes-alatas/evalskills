# Eval sandbox on the shared VPS: gVisor on the main Docker daemon (#137)

Live Tier 3 trials run on the host's **main** Docker daemon, every container under gVisor (`runsc`, systrap
platform, so no /dev/kvm is needed). The runner never gets Docker's socket: SkillEvaluator runs as the trial user
`evals`, whose only Docker access is the socket proxy (`gateway/docker_proxy.py`). The design and the host
operator's conditions are in #137 and ADR-0003 (amendments of 2026-11-12, 2026-11-13 and 2026-11-14). Trials are
isolated by Docker alone: there is no host firewall for them, and no step changes iptables.

Already done on the host (2026-11-12, announced): runsc installed and registered as an extra runtime
(`runsc install -- --platform=systrap`, `systemctl reload docker`). The default runtime stays runc.

| Item | Value |
| --- | --- |
| Trial user | `evals`, uid 10010, only in group `evalskills` (never `docker`); home `/var/lib/evals`, owned by root |
| Proxy socket | `/run/evalskills/docker.sock`, 0660 `root:evalskills` (`evalskills-docker-proxy.service`) |
| Trial folders | `/srv/evals/run` (the proxy's `bind_root`), a 5 GB ext4 image, `nodev,nosuid` |
| Trial networks | bridges from 172.31.240.0/21, one /28 each, created by the proxy: `internal`, masquerading off, and `inhibit_ipv4` (no address on the host). A trial reaches only the containers on its own network |
| Gateway and egress proxy | containers on their own network `evalskills-egress` (172.31.248.0/24): the gateway at `.2:8443`, the proxy at `.3:3128` (`deploy/live/`). Nothing published, `ip_forward=0` |
| How a trial reaches them | the proxy attaches both to each trial network at its first container create and detaches them before the network is removed. It names them `evals-gateway` and `evals-proxy` in the trial's `/etc/hosts`. On the host, step 3 names `172.31.248.2` `evals-gateway`, so one URL works in both places |
| Isolation check | the proxy's `GET /_evalskills/isolation` reads back every trial network's flags and the services' `ip_forward`, ports and networks; the runner refuses a trial unless it says ok |
| DNS | none for trials: the resolver is 127.0.0.1, both names are in `/etc/hosts`, the egress proxy resolves names |
| Base image | `evalskills/trial-base:d136a597`, built in CI (`build/README.md`), pinned by this host's image ID |

## Host steps

The host's operator runs each step, as root, in a window announced on #137 first (CONVENTIONS, shared hosts).
Each script is in `host/`, prints what it will do, and changes nothing without `--yes`. Each is safe to run twice,
and its `-undo.sh` removes only what that step added. Step 4 only reads, so it has no undo. Run them from a checkout of this repository, in this order:

| # | Script | What it does | Undo |
| --- | --- | --- | --- |
| 1 | `1-user.sh` | Group `evalskills`; system user `evals` (uid 10010, `nologin`, in `evalskills` only). `/var/lib/evals` root 0755, `~/.cache` the user's own (Harbor writes it by path); XDG config, data and state are per trial, in its trial folder (#186) | `1-user-undo.sh`: only that uid, only an empty group; refuses while the run disk exists or the user has processes |
| 2 | `2-run-disk.sh` | A 5 GB preallocated (`fallocate`, then `mkfs.ext4 -E nodiscard` so no holes are punched back) ext4 image `/var/lib/evals/run.img` (root 0600), mounted `loop,nodev,nosuid` at `/srv/evals/run` by `srv-evals-run.mount` (enabled); the mounted root `evals:evals` 0700. Never reformats an existing image, and refuses one that isn't fully allocated (#179) | `2-run-disk-undo.sh`: disables the mount, removes the unit and the image (every trial folder with it); refuses while the proxy runs |
| 3 | `3-hosts.sh` | Adds one `/etc/hosts` line, `172.31.248.2 evals-gateway # evalskills #137`, for the host-side judge | `3-hosts-undo.sh`: removes exactly that line |
| 4 | `4-check.sh` | Read-only: Docker Engine 28 or later, `runsc` registered, and no other Docker network on 172.31.240.0/21 or 172.31.248.0/24, and the run image still fully allocated (an `fstrim` through the loop mount can thin it, #179). Changes nothing | none needed |
| 5 | `5-code.sh COMMIT [REPO]` | This repository at `COMMIT` (refused unless on `origin/main`) at `/opt/evalskills`; SkillEvaluator at the pinned `d136a597` at `/opt/skillevaluator`, `uv sync --frozen --extra tier3` with `/opt/uv/bin/uv` (Python in `/opt/uv/python`, the cache in `/opt/uv/cache`, never under `/root`), and `gvisor_env` installed into its `.venv` non-editable. All root-owned, read-only to others, and it checks that `evals` can run the venv's Python. Needs uv: see below. **Downloads packages**: announce it as a network step. The repository is private, so `REPO` is usually a local path | `5-code-undo.sh`: removes both folders and `/opt/uv/python`, `/opt/uv/cache`; keeps uv; refuses while the proxy runs |
| 6 | `6-base-image.sh DIR` | Checks the CI artifact in `DIR` (first check the run on GitHub, as `build/README.md` says): `"loadable": true`, `"pinned": "committed"`, the tarball's sha256. Then `docker load`, and prints this host's image ID | `6-base-image-undo.sh`: `docker image rm` (no `-f`) |
| 7 | `7-docker-proxy.sh` | `/etc/evals/docker-proxy.json` (root 0640) from `host/docker-proxy.json`, with `builds.base_image_id` set to this host's ID of the base image; `evalskills-docker-proxy.service`. **Not enabled, not started** | `7-docker-proxy-undo.sh`: stops it, removes the unit and the config |
| 8 | `8-docker-client.sh` | `/var/lib/evals/.docker/config.json` (root 0644): `proxies` to `evals-proxy:3128`, so Docker sets `HTTP(S)_PROXY` in every trial container | `8-docker-client-undo.sh`: deletes it |
| 9 | `9-live.sh` | The gateway and the egress proxy (`deploy/live/compose.yaml`) on `evalskills-egress`. The model key goes in first, by hand (`deploy/live/README.md`, step 1); the script never reads it | `9-live-undo.sh`: `docker compose down` (volumes kept); refuses while the proxy runs |

A rerun of step 6 then step 7 re-pins a reloaded base image.

### uv on the host, before step 5

Step 5 runs uv from `/opt/uv/bin`, not from root's `PATH`. It refuses to start, before cloning anything, until uv is
there (#180). Install it once, as root, in an announced network window, from uv's standalone GitHub release, checked
against the SHA-256 committed here (not the release's own `.sha256`, which would change with the asset). To move to
a new version, take the hash from the release's asset digest and its `.sha256`, check that they agree, and commit it:

```sh
v=0.13.8 t=uv-x86_64-unknown-linux-gnu.tar.gz d=$(mktemp -d)
curl -fsSL -o "$d/$t" "https://github.com/astral-sh/uv/releases/download/$v/$t"
echo "5acfd9177cfecf0bf87578f059728215ccc884388988f55f7a65041b57387d91  $d/$t" | sha256sum -c - &&
  tar -xzf "$d/$t" -C "$d"
install -d -m 0755 /opt/uv /opt/uv/bin
install -m 0755 "$d/uv-x86_64-unknown-linux-gnu/uv" "$d/uv-x86_64-unknown-linux-gnu/uvx" /opt/uv/bin/
/opt/uv/bin/uv --version
```

Undo: `rm -rf /opt/uv`, only after `5-code-undo.sh`, since the venv's Python lives in `/opt/uv/python`. The host's
`python3 -m venv` is no alternative: the host has no `python3-venv`.

## Checks after the steps, before any trial

None of these spends API budget.
- With the proxy started (`systemctl start evalskills-docker-proxy`): `GET /_evalskills/isolation` on its socket
  says ok.
- `trial_env()` (`pipeline/tier3.py`) passes, as root, with the gateway URL and a token file set.
- As `evals`, `/var/run/docker.sock` is refused (`sudo -u evals docker -H unix:///var/run/docker.sock ps` fails with
  permission denied), and the proxy socket answers.
- Through the proxy: a runsc `hello-world` from the base image runs, and a create without runsc is refused.
- `df -h /srv/evals/run` shows the 5 GB disk, and `/` has more than `TIER3_MIN_FREE_ROOT` (8 GB) free: the runner
  refuses a trial below that, because trial images grow `/var/lib/docker` on `/`.

The first real trial is a separate step, with Enes's go-ahead. Keen wants the proxy's refused fields from that run.

## Known uncertainties

- **The network path** is proven by the `trial-network` workflow on every change to the proxy or the services:
  through the real proxy, with runsc, on Docker 29.6.1, before and after a dockerd restart. A trial reaches
  `evals-gateway:8443` and `evals-proxy:3128`, and not a trial on another network (both ways), the services' egress
  side, the host or the internet. An attacker with `NET_ADMIN` can't route through either service. On the host,
  check it once after step 9, before the first trial: the checks above, plus one runsc container on a proxy network
  that fetches `http://evals-gateway:8443/` and fails to reach `172.31.248.1`.
- The services sit on every live trial network at once. Trials still can't reach each other through them, because
  both run with `ip_forward=0` and the isolation check reads it back. A bug in either service's own code could
  bridge networks, as any shared service could.
- The services' egress (the gateway's upstream, the proxy's allowlist) is enforced in their code, not by a firewall.
- No IPv6: the proxy creates IPv4 networks only.
- The egress proxy's hostname allowlist (`deploy/live/egress-proxy.json`) is a first guess (#123).

## Clean-up on a host where the withdrawn second daemon was installed

The first design (a second dockerd, `docker-evals`) is withdrawn: on 2026-11-12 starting it took the main daemon's
networks down twice. Two dockerds in one network namespace aren't safe, whatever the config. Its install files are
gone from this folder. Where it was installed, the host's operator removes it in one announced step, as root:
1. `docker compose -f deploy/live/compose.yaml down`, if the gateway and proxy still listen on `br-evals`.
2. `sh deploy/gvisor/uninstall.sh`: a dry run that lists everything it will touch. Then `sh deploy/gvisor/uninstall.sh
   --yes`. It removes the unit, the EVALS rules (only those scoped to 172.31.240.0/20), `br-evals`, the eval
   daemon's directories and its group. It reports `dockremap` but doesn't remove it, and refuses while docker-evals
   runs or anything is mounted under its data root. It ends with an `iptables-save` diff and exits 2 if any rule
   outside the eval range changed. It leaves `runsc` installed.

Run it before step 4: its EVALS chains and `br-evals` are leftovers the new steps don't use.
