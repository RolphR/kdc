# Resource JSON Schema

[`schema/resource.schema.json`](../schema/resource.schema.json) validates a single
`kdc.rolphr.dev/v1alpha1` `Resource` using JSON Schema Draft 2020-12. It follows
the [API design](design-docs/api-design.md) and accepts arbitrary target kinds
and desired fields, including template strings. It also accepts optional wrapper
status and Kubernetes server metadata.

Parse YAML into JSON-compatible data before validation. Validate each nonempty
document separately and reject duplicate YAML keys during parsing. Configure your
validator to assert `date-time` formats. Schema defaults describe the API contract;
validation does not insert them.

The schema checks required fields, field types, enums, alias syntax, readiness
predicate structure, JSON Pointer escaping, literal identities, reserved target
metadata prefixes, and prohibited target fields. Unknown wrapper, spec, lifecycle,
reference, and predicate fields are rejected. Diagnostic entry fields remain open
because the design has not specified their exact structure.

Admission, CLI, or controller validation must additionally enforce:

- Unique dependency names and condition types, and declared dependency aliases.
- Target namespace equality, namespaced scope, and immutable target identities.
- Reconciliation durations between `1s` and `24h`; the schema checks syntax only.
- Template syntax, declared template aliases, absence of runtime `.values`
  lookups, literal mapping keys, and evaluation limits.
- Kubernetes naming rules, metadata rules, and the rendered target's own schema.
- Permissions, cross-namespace grants, ownership, and deletion-time restrictions.
- Safe status diagnostics and the controller's readiness and condition contracts.

This standalone JSON Schema is not a Kubernetes CRD structural schema.
