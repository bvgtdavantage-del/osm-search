FROM debian:bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends osm2pgsql postgresql-client curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY database/ /sql/
COPY deploy/importer/import.sh /import.sh
RUN chmod +x /import.sh

WORKDIR /data
ENTRYPOINT ["/import.sh"]
