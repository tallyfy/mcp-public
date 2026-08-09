FROM python:3.13-slim

WORKDIR /app

# Install the LOCK, not requirements.txt (#781).
#
# requirements.txt pins 17 direct packages and no transitive ones; the lock
# pins the full closure of 91. Installing the txt let pip re-resolve the other
# ~74 at build time, so two builds of the same commit a week apart could
# produce different images and nothing in the repo recorded which one shipped.
# The lock was already generated and committed on every deploy -- this line was
# the only thing that never read it.
#
# Both files are copied because the lock only means anything beside the file it
# was compiled from, and scripts/check_lock_current.py compares the pair.
#
# host/Dockerfile does exactly the same, deliberately. The pairing is not left
# to memory: tests/regression/test_lock_currency.py reads this line and
# requires the deploy workflow of any component that installs a `.lock` to run
# the staleness guard, so this file and .github/workflows/deploy-server.yml
# cannot drift apart silently.
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

COPY . .

EXPOSE 9000

CMD ["sh", "-c", "exec uvicorn server:app --host 0.0.0.0 --port ${PORT:-9000} --log-config logging.yaml"]