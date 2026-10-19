# 3. Use Gemini behind a provider interface

Date: 2026-10-19
Status: Accepted

## Context

The agent needs an LLM for ranking causes and writing cited summaries. A local model would compete with the cluster for RAM, and the project has no budget for paid APIs.

## Decision

Call a hosted **Gemini free-tier** model for reasoning, behind a small `LLMClient` interface so the provider can be replaced (Groq, Claude, a local Ollama model) without touching the rest of the agent.

## Alternatives considered

- **Local Ollama model**: no cost per call, but uses memory the cluster needs.
- **Paid API directly**: simpler, but costs money during evaluation runs that repeat each scenario many times.

## Consequences

- Free-tier rate limits apply, so prompt changes are evaluated by replaying saved evidence offline rather than re-breaking the cluster.
- The interface makes the LLM easy to mock in tests with recorded responses.
- Evidence sent to the provider is redacted first (see the threat model).
