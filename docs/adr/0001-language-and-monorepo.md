# ADR-0001 — Python control plane and TypeScript Studio
Status: Accepted for bootstrap (2026-11-21)

Context: Agent orchestration, model SDK integration and YAML contracts favour Python. Interactive Studio needs a robust browser ecosystem. Rust/Go would improve some low-level worker characteristics but increase early development costs.

Decision: Python 3.11+ FastAPI/Pydantic modular monolith, React/TypeScript/Vite frontend, with an independently specified worker protocol. Avoid shared in-process worker APIs that prevent Go/Rust replacement later. No heavy Python orchestration framework until implementation benchmarks show a need.

Consequences: Fast iteration, common language with model integrations; execution isolation is achieved by sandboxing and service boundaries, not Python type safety. Future Go/Rust workers are possible without control plane rewrite.
