# Dependency environments

The project root keeps only two daily entry points:

- `../requirements.txt`: API and business data Agent runtime.
- `../requirements-dev.txt`: runtime plus the verified test runner.

The optional `langchain-baseline.txt` profile contains the application runtime and the pinned LangChain packages used by `examples/langchain_sql_agent_demo.py`. Install it in a dedicated Python 3.12 virtual environment when comparing the hand-written Function Calling loop with LangChain tools.

Do not install the optional profile in the production API image. The Dockerfile intentionally installs only `requirements.txt`.
