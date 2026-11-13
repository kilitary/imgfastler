#!/bin/sh
# Collect Docker state into InfluxDB every 10 seconds.
# busybox crond only fires once per minute, so schedule the job 6 times a
# minute with `sleep 10` between runs; flock prevents overlapping instances
# when a snapshot takes longer than 10s.

cat > /etc/crontabs/root <<'CRON'
* * * * * for i in $(seq 0 5); do flock -n /tmp/collector.lock python3 -u /app/influx_push.py  >> /proc/1/fd/1 2>&1; sleep 10; done
CRON

exec crond -f -l 8
