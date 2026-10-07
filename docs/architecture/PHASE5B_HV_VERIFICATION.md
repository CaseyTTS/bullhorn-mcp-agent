# Phase 5B HV Verification: `/settings/commentActionList`

- **Status:** Builder-authored record for `PHASE5B_WORK_PACKAGE.md` §3 (HV-1), 2026-10-07.
- **Outcome:** HV-D1 **verified (documentation)**; HV-D2 **unresolved**; HV-D3 **unresolved**.
- **Effect:** `SETTINGS_ACTION_SOURCE_VERIFIED = False` in `src/bullhorn_mcp/notes/action_discovery.py`.
  The settings source is never called; discovery reports `source_unresolved: ["settings"]`.
- **Tenant verification:** not performed (it requires an administrator on a connected tenant, read-only,
  with no tenant values committed; D-5-22). No tenant values appear in this document.

## Sources (official reference: https://bullhorn.github.io/rest-api-docs/)

Fetched 2026-10-07 from the live page and compared with a cached copy (`apiref.html` and `entityref.html`)
downloaded on 2026-10-06. Both say the same thing.

**S1: REST API reference, "settings" section.** Verbatim:

> `GET /settings/setting1[,setting2…]`
>
> `curl https://rest{swimlane#}.bullhornstaffing.com/rest-services/e999/settings/allPrivateLabelIds,currencyFormat`
>
> Example Response
> ```
> {
>     "allPrivateLabelIds":  [ 1, 2, 3],
>     "currencyFormat": "USD"
> }
> ```
> Returns the value(s) of the specified system setting(s). The value type (Integer, String, Boolean, and so forth) depends on the specified setting name.

> `GET /settings`
>
> Example Response
> ```
> {
>   "data": [
>     ...
>     }, {
>       "name": "commentActionList",
>       "valueUrl": "http://rest{swimlane#}.bullhornstaffing.com/rest-services/e999/settings/commentActionList",
>       "valueType": "STRING",
>       "isArray": true
>     }
>     ...
>   ]
> }
> ```
> Returns a list of predefined setting names and their metadata.

**S2: Entity reference, Note, field `action`.** Verbatim:

> action | String (30) | Action type associated with Note. The list of values is configured in the private label attribute called commentActionList.

## Items

| ID | Question | Evidence | Result |
|---|---|---|---|
| HV-D1 | The `GET /settings/{name}` request form, and the response shape for `commentActionList` | S1 documents `GET /settings/{name1[,name2…]}` returning a JSON object keyed by setting name. In the same section, `GET /settings` lists `commentActionList` with `"valueType": "STRING", "isArray": true`. S1's array example (`allPrivateLabelIds`, also `isArray: true`) is a JSON list. The expected shape is therefore `{"commentActionList": [<string>, …]}`. | **VERIFIED (documentation).** No example response for `commentActionList` itself is shown. The shape comes from the documented metadata together with the documented general form. Anything else, such as a delimited string, a missing key, non-strings or an oversized body, is treated as `unverifiable`. |
| HV-D2 | Do the `commentActionList` values equal the exact strings accepted in `Note.action` (String(30))? | S2 says the `Note.action` value list "is configured in the private label attribute called commentActionList". The docs do not say: (a) that `GET /settings/commentActionList` returns that same private-label attribute for every private label (the setting is read in the API user's own context, and a tenant can have several private labels; see `allPrivateLabelIds`); (b) that the strings are byte-identical to what `Note.action` accepts (case, whitespace, trimming); or (c) that every entry fits String(30). | **UNRESOLVED.** Guard: the source is off. Adoption also rejects values longer than 30 characters (HV-B10). |
| HV-D3 | Permissions and error behaviour of `/settings` for non-admin API users | S1 says nothing about permissions, entitlements or error responses for `/settings`. | **UNRESOLVED.** Guard: the source is off. Even when it is on, any non-200, transport error, non-JSON, non-object, malformed, empty or oversized response is `unverifiable` with a bounded warning, and nothing is guessed. The response body is never echoed into errors. |

Under D-5B-1, the settings source is used **only if HV-D1..D3 are all verified**. With HV-D2 and HV-D3
unresolved, `SETTINGS_ACTION_SOURCE_VERIFIED` stays `False`.

## Effect in code

- `notes/action_discovery.py`: `settings_source()` returns `status: unresolved` without touching the
  client while the flag is `False`. `discover_schema` takes no tool argument and reads no state that could
  enable it. Tests patch the flag to `True` only to exercise the mocked verified shape (AC-6).
- Source 1 (`/meta/Note` `action` options, HV-A2/HV-B10) is unaffected. It is `verified` only when the
  snapshot holds a non-empty options list. Otherwise it is `unverifiable`.
- Source 3 (administrator entry with `set_value_mapping`) is unchanged.

**To enable the settings source later:** an administrator runs a read-only `GET /settings/commentActionList` on a
connected tenant. They record here, without values, the response shape, whether the strings match the tenant's
`Note.action` values exactly, and the non-admin behaviour. They then set the flag in the same reviewed change.

## P4B-8 (scrub before redact): **CLOSED** (Phase 5B, D-5B-7)

- A `BullhornAPIError` raised by `EntityWriter` for a non-200 response now carries only
  `API request failed: <status>` in `str()`, `repr()` and `args`. The raw body is kept in the private attribute
  `_bullhorn_raw_error_body`, which is never rendered.
- `safe_error_text(exc, scrub=…)` works in four steps. (1) It takes the raw body when present. (2) It scrubs the
  comment text: the whole text, each line of 8 or more characters, JSON-escaped variants, and any run containing
  an 8-character fragment of the comment. (3) It redacts secrets. (4) It bounds the result, then redacts again.
  With no raw body and no `scrub`, behaviour is unchanged.
- Tests: `tests/test_phase5b_p4b8.py` (AC-14, AC-15). The B-5 corpus (`tests/test_writes_review_fixes.py`)
  passes unmodified.
- `DEFERRED_DEBT.md` is not in the 5B allowlist, so the Architect should update its P4B-8 row to CLOSED, with a
  pointer here.
