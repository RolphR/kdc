# Resource schemas

## Kubernetes CRD

[`config/crd/bases/kdc.rolphr.dev_resources.yaml`](../config/crd/bases/kdc.rolphr.dev_resources.yaml)
defines the namespaced `Resource` API at `kdc.rolphr.dev/v1alpha1`, with plural
`resources`, short name `kdcr`, and a status subresource. Install it on Kubernetes
1.29 or newer, where CEL validation is stable:

```sh
kubectl apply -f config/crd/bases/kdc.rolphr.dev_resources.yaml
kubectl wait --for=condition=Established --timeout=60s crd/resources.kdc.rolphr.dev
```

Installing the CRD registers the API; reconciliation still requires a kdc
controller, which is not implemented in this repository.

The structural OpenAPI schema validates known field types and readiness
predicates. Dependency lists are maps keyed by `name`, and condition lists are
maps keyed by `type`, preventing duplicate keys and allowing independent field
ownership. CEL validates reference alias syntax, declared dependency aliases,
readiness modes, durations between `1s` and `24h`, and immutable target API version,
kind, and name. It rejects kdc wrappers as targets and rejects target `status`,
including explicit null.

The target is an atomic embedded Kubernetes resource with arbitrary desired
fields preserved. Predicate `equals` preserves arbitrary JSON values, including
null, false, zero, arrays, and objects. Diagnostic entries remain open until the
API design specifies their structure.

Kubernetes inserts defaults for references, lifecycle settings, and dependency
generation policy. Omitted target readiness remains omitted. In `Predicates`
mode, an omitted target generation policy means `IfPresent` to the controller;
the CRD does not insert it, so `None` mode remains valid with only `mode`.

Kubernetes permits only limited schema constraints on embedded resource metadata,
and CEL cannot inspect its namespace, labels, or annotations. Admission and
controller validation must enforce target namespace equality, permitted metadata
fields, reserved metadata prefixes, target scope, templates, permissions,
ownership, and deletion-time changes. These boundaries follow the
[Kubernetes structural schema and CEL rules](https://kubernetes.io/docs/tasks/extend-kubernetes/custom-resources/custom-resource-definitions/).

Unknown fields outside preserved target and diagnostic data are pruned by
Kubernetes. Use strict field validation to reject them when submitting manifests:

```sh
kubectl apply --validate=strict -f examples/gcp/network.yaml
```

The CRD was checked with Kubernetes 1.34.1 validation libraries for structural
schema validity, default validity, CEL compilation and cost limits, and 88 resource
validation cases, including all 36 examples. No cluster installation was performed.

## Standalone JSON Schema

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
