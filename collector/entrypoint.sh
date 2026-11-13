#!/bin/sh
# Collect Docker state into InfluxDB every 10 seconds.
# busybox crond only fires once per minute, so schedule the job 6 times a
# minute with `sleep 10` between runs; flock prevents overlapping instances
# when a snapshot takes longer than 10s.
#
# Logs are appended to /logs/collector.log (mounted from the host) because the
# docker-log pipe proves unreliable for containers started with `docker start`
# after a Docker Desktop restart — the data write path is unaffected, but
# `docker logs` goes silent. The file always works and survives restarts.

LOG_DIR=${COLLECTOR_LOG_DIR:-/logs}
LOG_FILE="$LOG_DIR/collector.log"

mkdir -p "$LOG_DIR"
# Start each container run with a fresh log, but cap the file if it grew huge
# in a long-lived container (rotate at ~20 MiB).
if [ -f "$LOG_FILE" ]; then
    SIZE=$(wc -c < "$LOG_FILE")
    if [ "$SIZE" -gt 20971520 ]; then
        mv "$LOG_FILE" "$LOG_DIR/collector.log.1"
        echo "$(date -u +%FT%TZ) rotated previous log ($SIZE bytes) to collector.log.1" >> "$LOG_FILE"
    else
        : > "$LOG_FILE"
    fi
fi

cat > /etc/crontabs/root <<CRON
* * * * * for i in \$(seq 0 5); do flock -n /tmp/collector.lock python3 -u /app/influx_push.py >> $LOG_FILE 2>&1; sleep 10; done
CRON

echo "$(date -u +%FT%TZ) collector starting, logging to $LOG_FILE" >> "$LOG_FILE"
exec crond -f -l 8
