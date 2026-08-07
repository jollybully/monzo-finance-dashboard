# Finance Dashboard — TODO

## Bugs

### [BUG] `search_merchants` ignores `start`/`end` range parameters
- **Reported:** 2026-08-01
- **Symptom:** Calling `search_merchants` with explicit `start`/`end` (e.g. `2026-01-01` → `2026-08-01`) always returns results for the default current pay period only (`2026-07-31` → `2026-08-01`). The echoed request in the response shows the defaults, not the caller's range.
- **Impact:** Cannot find recurring merchants (e.g. gym membership "Crystal Palace Better") across historical periods; merchant-level analysis is limited to the current pay period.
- **Also observed:** `get_week_detail` raises `Unable to serialize unknown type: <class 'app.models.Transaction'>` — response serialization bug for week detail responses.
- **Suggested fix:** Respect `start`/`end` parameters in `search_merchants` (check argument passing/validation in the MCP server tool definitions); fix the `Transaction` model serialization in the week-detail response path (likely missing Pydantic `model_dump`/JSON-encoder for the MCP tool output).
- **Use case that motivated this:** locating Elliot's gym membership merchant (Crystal Palace Better) via recurring monthly payment.
