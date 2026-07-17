FROM pgvector/pgvector:0.8.5-pg16 AS pg-jieba-builder

ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG PG_JIEBA_COMMIT=d0ffac8028328b2566a889ff4db3d74ba63d1b42

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        cmake \
        git \
        postgresql-server-dev-16 \
    && rm -rf /var/lib/apt/lists/*

RUN git init /build/pg_jieba \
    && cd /build/pg_jieba \
    && git remote add origin https://github.com/jaiminpan/pg_jieba.git \
    && git fetch --depth 1 origin "${PG_JIEBA_COMMIT}" \
    && git checkout --detach FETCH_HEAD \
    && git submodule update --init --recursive --depth 1 \
    && cmake -S . -B build \
        -DCMAKE_BUILD_TYPE=Release \
        -DPostgreSQL_TYPE_INCLUDE_DIR=/usr/include/postgresql/16/server \
    && cmake --build build --parallel \
    && cmake --install build


FROM pgvector/pgvector:0.8.5-pg16

COPY --from=pg-jieba-builder /usr/lib/postgresql/16/lib/pg_jieba.so /usr/lib/postgresql/16/lib/pg_jieba.so
COPY --from=pg-jieba-builder /usr/share/postgresql/16/extension/pg_jieba.control /usr/share/postgresql/16/extension/pg_jieba.control
COPY --from=pg-jieba-builder /usr/share/postgresql/16/extension/pg_jieba--1.1.1.sql /usr/share/postgresql/16/extension/pg_jieba--1.1.1.sql
COPY --from=pg-jieba-builder /usr/share/postgresql/16/tsearch_data/jieba_base.dict /usr/share/postgresql/16/tsearch_data/jieba_base.dict
COPY --from=pg-jieba-builder /usr/share/postgresql/16/tsearch_data/jieba_hmm.model /usr/share/postgresql/16/tsearch_data/jieba_hmm.model
COPY --from=pg-jieba-builder /usr/share/postgresql/16/tsearch_data/jieba_user.dict /usr/share/postgresql/16/tsearch_data/jieba_user.dict
COPY --from=pg-jieba-builder /usr/share/postgresql/16/tsearch_data/jieba.stop /usr/share/postgresql/16/tsearch_data/jieba.stop
COPY --from=pg-jieba-builder /usr/share/postgresql/16/tsearch_data/jieba.idf /usr/share/postgresql/16/tsearch_data/jieba.idf
