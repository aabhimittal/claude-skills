# api-contract-guard rule catalog

Stable rule IDs and severities. `critical` > `high` > `medium` > `low` > `info`.
Default CI gate (`--fail-on`) is `high`. Additive (`info`) changes never fail CI.

## OpenAPI / Swagger (JSON)

| Rule | Severity | Change | Why it breaks clients |
| --- | --- | --- | --- |
| OAS001 | high | Removed path | Requests to it 404 |
| OAS002 | high | Removed operation (method on a path) | That call 404s / 405s |
| OAS003 | high | New **required** parameter | Existing callers that omit it are rejected |
| OAS004 | high | Parameter became required | Callers not sending it break |
| OAS005 | medium | Removed required parameter | May change server behavior for callers still sending it |
| OAS006 | high | New required request-body field | Existing request payloads are rejected |
| OAS100 | info | New path (additive) | Non-breaking |

## GraphQL (SDL)

| Rule | Severity | Change | Why it breaks clients |
| --- | --- | --- | --- |
| GQL001 | high | Removed type | Queries/fields referencing it fail |
| GQL002 | high | Type kind changed (e.g. `type`→`enum`) | Incompatible shape |
| GQL003 | high | Removed enum / union member | Clients sending or matching it break |
| GQL004 | high | Removed field | Selecting it is a validation error |
| GQL005 | high | Field type changed | Client deserialization / non-null expectations break |
| GQL006 | high | New **required** argument (non-null, no default) | Existing queries that omit it are rejected |
| GQL007 | medium | Removed argument | Queries passing it error |
| GQL008 | high | Argument became required | Queries not passing it break |
| GQL100 | info | New type (additive) | Non-breaking |

## The safe way to make these changes

- **Deprecate, then remove.** Mark the field/endpoint deprecated
  (`@deprecated` in GraphQL, `deprecated: true` in OpenAPI) for a release, then
  remove once clients have migrated.
- **Keep new inputs optional.** New parameters/arguments should be optional or
  carry a default; never introduce a required input on an existing operation.
- **Add, don't mutate.** To change a field's type or an endpoint's shape, add a
  new field/endpoint and migrate readers, rather than changing the existing one.
- **Version breaking changes.** When a break is unavoidable, expose it under a
  new path (`/v2/...`) or a new GraphQL field/type.

## Notes & limitations

- **Format detection is automatic:** both files parse as JSON with `openapi`/
  `swagger`/`paths` → OpenAPI; otherwise treated as GraphQL SDL. A mix errors.
- **OpenAPI:** JSON only (convert YAML first). Covers paths, operations,
  parameters, and request-body `required`. It does **not** deep-diff response
  schemas or follow `$ref` chains — a break hidden inside a referenced component
  schema may be missed.
- **GraphQL:** a lightweight SDL reader (no external parser). Standard type/
  input/interface/enum/union definitions are handled; highly unusual formatting
  or schema-extension (`extend type`) may be missed.
- Direction nuance on `GQL005`: output fields widening to non-null is
  technically safe, but the analyzer flags any type change for human review.
