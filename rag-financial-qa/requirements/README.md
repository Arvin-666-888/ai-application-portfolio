# Optional and reproducibility environments

The project root keeps only the two daily entry points:

- `../requirements.txt`: API and ordinary document worker runtime.
- `../requirements-dev.txt`: runtime plus the verified test runner.

Install exactly one optional profile in a separate virtual environment when needed:

| File | Purpose | Supported environment |
|---|---|---|
| `langchain-baseline.txt` | LangChain comparison demo and parent-retrieval baseline | Python 3.12 |
| `paddle-worker-windows-py312.txt` | Current PaddleOCR GPU worker, including the application runtime | Windows 11 AMD64, Python 3.12 |
| `locks/paddleocr-gpu-windows-py312.lock.txt` | Fully resolved transitive lock consumed by the Paddle worker profile | Windows 11 AMD64, Python 3.12 |
| `locks/task2-reproduction-windows-py312.lock.txt` | Historical Task 2 evaluation environment snapshot | Windows 11 AMD64, Python 3.12 |

Do not install both lock files into one environment. They intentionally preserve different NumPy, OpenAI, Paddle, Torch and LangChain dependency graphs.

The historical Task 2 lock is evidence for reproducing an earlier financial-document evaluation. It is not the installation entry point for the current ecommerce RAG API or Paddle worker.
