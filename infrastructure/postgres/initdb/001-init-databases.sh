#!/bin/sh
# This script is mounted into a Linux container and must retain LF line endings.
set -eu

create_database() {
  database_name="$1"
  exists="$(psql --username "$POSTGRES_USER" --dbname postgres --tuples-only --no-align \
    --command "SELECT 1 FROM pg_database WHERE datname = '$database_name'")"
  if [ "$exists" != "1" ]; then
    psql --username "$POSTGRES_USER" --dbname postgres --set ON_ERROR_STOP=1 \
      --command "CREATE DATABASE \"$database_name\""
  fi
}

create_database aituge
create_database proofreading

for database_name in aituge proofreading; do
  psql --username "$POSTGRES_USER" --dbname "$database_name" --set ON_ERROR_STOP=1 \
    --command "CREATE EXTENSION IF NOT EXISTS vector"
done
