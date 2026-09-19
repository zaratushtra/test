# SPINE collector.
#
# Two notes that are design decisions rather than Docker habits:
#
#   * There is no pip install, no requirements file and no build stage that
#     fetches anything. The project is stdlib-only, which means the image has no
#     dependency surface to audit and no supply chain to compromise.
#
#   * There is no secret handling, and no mechanism to add one. Under design
#     section 2.1's paper-only posture the venue endpoints are public and
#     spine/venue.py has no authentication path at all -- a secrets mount would
#     be a facility with no use and a liability the moment one appeared.
#
# Build:  docker build -t spine .
# Run:    docker run --rm -v spine-data:/data spine \
#           --interval 3600 --heartbeat /data/health.json \
#           -- --jurisdiction GB --db /data/spine.db --sweep

FROM python:3.11-slim AS runtime

# tini reaps zombies and forwards signals, so SIGTERM reaches serve.py rather
# than being swallowed by PID 1. Without it a container stop becomes SIGKILL
# ten seconds later, mid-pass.
RUN apt-get update \
 && apt-get install --no-install-recommends -y tini \
 && rm -rf /var/lib/apt/lists/*

# Unprivileged, and the code is not writable by the user running it.
RUN useradd --create-home --uid 10001 spine
WORKDIR /app
COPY --chown=root:root db/ ./db/
COPY --chown=root:root spine/ ./spine/
COPY --chown=root:root phase0/ ./phase0/
COPY --chown=root:root tests/ ./tests/
COPY --chown=root:root run_cycle.py serve.py run_tests.py ./
RUN chmod -R a-w /app

# Everything mutable lives here and nowhere else, so the container can run with
# --read-only and a tmpfs.
RUN mkdir -p /data && chown spine:spine /data
VOLUME ["/data"]

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER spine

# Fails the healthcheck when passes are failing, not merely when the process is
# gone. A process that is alive and failing every pass is the case worth
# catching, and the one a liveness probe misses.
HEALTHCHECK --interval=5m --timeout=10s --start-period=1m --retries=3 \
  CMD python3 -c "import json,sys,time; \
h=json.load(open('/data/health.json')); \
sys.exit(1 if h.get('state')=='failing' else 0)"

ENTRYPOINT ["/usr/bin/tini", "--", "python3", "/app/serve.py"]
CMD ["--interval", "3600", "--heartbeat", "/data/health.json"]
