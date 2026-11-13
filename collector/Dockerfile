FROM alpine:latest

RUN apk add --no-cache docker-cli python3

COPY influx_push.py /app/influx_push.py
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

CMD ["/app/entrypoint.sh"]
