# The model gateway and the trial egress proxy (#121, #123): one image, two services, standard library only.
# Build: GATEWAY_MODEL_KEY='' docker compose -f deploy/live/compose.yaml build
# By digest, the same as deploy/Dockerfile's, so a rebuild can't change the base image without a commit; Dependabot
# proposes the bumps (#183).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

RUN useradd --uid 10002 --no-create-home --shell /usr/sbin/nologin gateway \
    && useradd --uid 10003 --no-create-home --shell /usr/sbin/nologin egress \
    && install -d -o 10002 -g 10002 -m 0700 /var/lib/evalskills-gateway
# The named state volume copies this directory's owner on first mount, so the gateway can write its counters (#71).
COPY gateway/gateway.py gateway/egress_proxy.py /opt/gateway/
USER 10003
