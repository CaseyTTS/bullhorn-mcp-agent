# Bullhorn MCP Agent

A Python Model Context Protocol (MCP) server for Bullhorn CRM, extended for agent-oriented recruiting workflows.

This project is based on the open-source [`osherai/bullhorn-mcp-python`](https://github.com/osherai/bullhorn-mcp-python) project and expands it with additional tools intended for AI agents, recruiting automation, candidate document workflows, and cross-system orchestration.

The goal is to provide a controlled MCP layer between an AI agent and Bullhorn rather than giving the model direct access to Bullhorn credentials or unrestricted API access.

---

## What It Does

The server exposes Bullhorn functionality as MCP tools that can be called by MCP-compatible AI clients such as Codex.

### Current MCP Tools

#### Connection

- `connection_status`
  - Checks whether required Bullhorn environment variables are configured.
  - Attempts to verify Bullhorn connectivity when credentials are present.
  - Does not expose credentials to the AI client.

#### Jobs

- `list_jobs`
  - List and filter Bullhorn JobOrders.
  - Supports Lucene search, status filtering, limits, and field selection.

- `get_job`
  - Retrieve a specific JobOrder by Bullhorn ID.

#### Candidates

- `list_candidates`
  - List and filter Bullhorn Candidates.
  - Supports Lucene search, status filtering, limits, and field selection.

- `get_candidate`
  - Retrieve a specific Candidate by Bullhorn ID.

- `get_candidate_files`
  - List files attached to an existing Bullhorn Candidate.

- `upload_candidate_resume`
  - Upload a local resume or document to an existing Bullhorn Candidate.

#### Placements

- `get_recent_placements`
  - Retrieve Bullhorn Placement records created within a specified number of days.
  - Includes bounded date and result limits for safer agent use.

#### Generic Bullhorn Access

- `search_entities`
  - Search Bullhorn entities using Lucene query syntax.

- `query_entities`
  - Query Bullhorn entities using Bullhorn's SQL-like/JPQL-style query syntax.

---

## Example Architecture

The MCP server acts as a controlled adapter between an AI agent and Bullhorn.

```text
                AI Agent / Codex
                       |
                       | MCP
                       v
              Bullhorn MCP Agent
                       |
                       | Bullhorn REST API
                       v
                    Bullhorn
