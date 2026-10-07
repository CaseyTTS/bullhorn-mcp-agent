# Bullhorn MCP Agent

A Python Model Context Protocol (MCP) server for Bullhorn, extended into a recruiting-domain operating layer for AI agents and enterprise workflows.

This project began from the open-source `osherai/bullhorn-mcp-python` project and has been substantially expanded with tenant-aware schema mapping, per-user Bullhorn authentication, controlled writes, recruiting activity models, audit and approval controls, and agent-oriented workflow tooling.

The goal is to provide a governed layer between AI systems and Bullhorn rather than giving a model direct access to credentials, unrestricted REST endpoints, or tenant-specific business logic.

---

## Current Status

This project is currently in active **POC and test-stage development**.

It is not yet distributed for production use.

The current focus is validating:

- authentication and session isolation
- tenant configuration and mapping
- permission boundaries
- recruiting-domain semantics
- safe read/write patterns
- auditability
- security controls
- historical analytics architecture
- enterprise Workspace integration

Bullhorn remains the system of record.

---

## What This Project Is Becoming

This is no longer just a thin REST wrapper around Bullhorn.

The MCP is being developed as a recruiting-domain operating layer that sits between AI systems and Bullhorn.

Its responsibilities include:

- authenticating users
- isolating Bullhorn sessions
- enforcing permissions
- translating canonical recruiting concepts into tenant-specific Bullhorn fields
- validating workflows
- controlling writes
- auditing actions
- exposing structured recruiting data
- supporting analytics and historical intelligence
- keeping the model away from raw credentials and unrestricted API access

Conceptually:

```text
ChatGPT Workspace / AI Agent
            |
            | MCP
            v
Bullhorn Recruiting Orchestrator
    |
    |-- Authentication / session isolation
    |-- Permissions
    |-- Tenant mappings
    |-- Canonical recruiting schema
    |-- Safe reads
    |-- Controlled writes
    |-- Approval gates
    |-- Audit
    |-- Activity / analytics
    |
    | Bullhorn REST API
    v
Bullhorn
