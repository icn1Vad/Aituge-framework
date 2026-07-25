FROM ai-framework-core:base

RUN pip install --no-cache-dir \
    "langextract==1.6.0" \
    "psycopg[binary]>=3.2,<4" \
    "pydantic-settings>=2.4,<3" \
    "python-docx>=1.1,<2"
