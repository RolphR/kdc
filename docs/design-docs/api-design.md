# kdc API Design

Status: Proposed

Based on [the high-level design](high-level-design.md). Names, defaults, and syntax below are proposed contracts for the first implementation, not existing functionality.

## API surface

kdc exposes a Kubernetes API and a packaging CLI. It does not need a separate HTTP service.

| Property | Proposed value |
| --- | --- |
| API group | `kdc.rolphr.dev` |
| Version | `v1alpha1` |
| Kind | `Resource` |
| Plural / short name | `resources` / `kdcr` |
| Scope | Namespaced |
| Subresource | `status` |
| Finalizer | `kdc.rolphr.dev/finalizer` |

The group is a naming proposal; control of its domain must be confirmed before release. Kubernetes discovery resolves API versions and kinds to resource endpoints. Targets must be namespaced and in the wrapper's namespace. References default to that namespace; cross-namespace references require admission-controller authorization. Cluster-scoped objects are rejected in this version.

## Resource specification

```yaml
apiVersion: kdc.rolphr.dev/v1alpha1
kind: Resource
metadata:
  name: application-settings
  namespace: infrastructure
spec:
  references:
    upstreamSettings:
      apiVersion: v1
      kind: ConfigMap
      name: upstream-settings
    upstream:
      apiVersion: apps/v1
      kind: Deployment
      name: upstream
  lifecycle:
    dependencies:
      - name: upstream-available
        reference: upstream
        readiness:
          observedGeneration: Require
          checks:
            - condition:
                type: Available
                status: 'True'
    reconcileInterval: 1m
    deletionPolicy: abandon
  target:
    apiVersion: v1
    kind: ConfigMap
    metadata:
      name: application-settings
      labels:
        app: example
    data:
      endpoint: '{{ .refs.upstreamSettings.data.endpoint }}'
      replicas: '3'
```

This example reads an endpoint from one reference and requires another reference's Deployment to be available. `lifecycle.targetReadiness` is omitted, so kdc looks for the target's `Ready` condition. A ConfigMap does not report that condition; its `TargetReady` remains `Unknown` unless an explicit readiness override is supplied.

| Field | Contract |
| --- | --- |
| `target` | Required complete target manifest, including literal identity and templated desired fields. |
| `references` | Optional map of aliases to resource identities, default `{}`. Shared by templates and dependencies. |
| `lifecycle` | Optional lifecycle settings, default `{}`; nested defaults apply when omitted. |
| `lifecycle.targetReadiness` | Optional readiness override for targets without the default `Ready` condition contract. |
| `lifecycle.dependencies` | Optional list of named readiness gates using reference aliases, default `[]`. |
| `lifecycle.reconcileInterval` | Duration string, default `1m`; range `1s` through `24h`. |
| `lifecycle.deletionPolicy` | `abandon` (default), `delete`, or `prevent-deletion`. |

Reference aliases and dependency names match `[A-Za-z_][A-Za-z0-9_]*`. Reference aliases are unique map keys; dependencies use CRD list-map semantics keyed by their unique `name`. Reject duplicate YAML keys before packaging. Ordering has no effect on reconciliation. Target readiness overrides, dependencies, reconciliation interval, and deletion policy are nested under `spec.lifecycle`; their former top-level fields are rejected. There are no wrapper-level `values` or `template` fields.

### Target identity and desired fields

`target.apiVersion`, `target.kind`, and `target.metadata.name` are required literals. `target.metadata.namespace` is optional; when present it must equal the wrapper namespace, otherwise the controller injects that namespace. Identity is immutable after wrapper creation, including the API version. A replacement requires a new wrapper and explicit handling of the previous target's deletion policy.

`target` contains arbitrary desired fields, such as `spec`, ConfigMap `data`, or Secret `stringData`, and user labels and annotations. Reject `status` and metadata fields other than `name`, `namespace`, `labels`, and `annotations`, including `generateName`, owner references, finalizers, UIDs, and managed fields. The `kdc.rolphr.dev/` metadata prefix is reserved. kdc does not permit a `Resource` as its own target.

Use a structural CRD schema for known fields and preserve unknown JSON fields beneath `target`. Treat `target` as atomic for wrapper field ownership and mark it as an embedded Kubernetes resource, since it includes `apiVersion`, `kind`, and `metadata`. Kubernetes supports selectively [preserving unknown fields](https://kubernetes.io/docs/tasks/extend-kubernetes/custom-resources/custom-resource-definitions/#field-pruning); admission and render-time validation supplement the schema for arbitrary desired fields.

## References

Each `references` entry identifies one Kubernetes resource by `apiVersion`, `kind`, and `name`, with optional `namespace`. The map key is its alias. References contain no field paths, extracted values, or readiness predicates. They identify the actual resource, including another wrapper's rendered target, rather than implicitly referring to its wrapper.

Omitted namespaces resolve to the wrapper namespace. A different namespace is allowed only when the admission controller authorizes the source namespace and resource kind for that wrapper namespace. Validate on creation and update; the controller also checks current policy before each read so revoked grants stop further use. Kubernetes RBAC must independently permit the controller's read. Cross-namespace references are denied when no applicable grant or admission enforcement is available.

The resolved resource object is available as `.refs.<alias>`. For example, `.refs.database.status.endpoint` selects an output, while `get .refs.settings.data "endpoint.url"` selects a key containing a dot. Dependencies use the same resolved objects and never repeat their identities. Resolve each alias once per reconciliation and share that snapshot between dependency checks and rendering.

All declared references must resolve before rendering, even if unused. A missing object leaves the wrapper waiting. A missing field accessed by a template also leaves it waiting with `FieldMissing`, identifying the alias and field path. Present null, false, zero, empty strings, and empty collections retain their JSON types and are distinct from absent fields. `has` can explicitly test optional map fields. Reference existence alone does not imply readiness; readiness gates are declared separately in `spec.lifecycle.dependencies`.

## Dependency and target readiness

Each entry in `spec.lifecycle.dependencies` requires `name`, `reference`, and `readiness`. `reference` must name an entry in `spec.references`; inline resource identities are rejected. `readiness.checks` is a nonempty list; every check must pass against that referenced object. Each check contains exactly one predicate:

```yaml
# A Kubernetes-style condition.
condition:
  type: Ready
  status: 'True' # Allowed: True, False, Unknown, expressed as strings.
```

```yaml
# An exact, typed comparison, including false, zero, and null.
field:
  path: /status/phase
  equals: Available
```

Predicate `field.path` is an absolute JSON Pointer relative to the referenced object. Decode `~1` as `/` and `~0` as `~`; array indices are supported. Wildcards, filters, and executable paths are excluded. Field selection belongs to the predicate or template, never the reference declaration.

Condition checks read `status.conditions`. Missing conditions, duplicate matching condition types, absent field paths, and unequal values are unsatisfied. `equals` must be present even when its value is null. JSON numbers compare numerically; other values compare structurally without coercion. Malformed source status produces a named diagnostic and blocks readiness.

`readiness.observedGeneration` is `IfPresent` (default), `Require`, or `Ignore`. For condition predicates, use the matching condition's `observedGeneration`, falling back to the object's `status.observedGeneration`. For field predicates, use `status.observedGeneration`. An available generation must equal `metadata.generation`. `Require` also blocks when generation is absent; `Ignore` skips the check. Providers using another generation layout require a future profile or an additional explicit field check where expressible.

`spec.lifecycle.targetReadiness` is optional. When omitted, inspect the target's `status.conditions` for a condition with `type: Ready` and use `observedGeneration: IfPresent` as defined above:

- A current `status: 'True'` sets `TargetReady=True`.
- `status: 'False'` or a stale observed generation sets `TargetReady=False`.
- `status: 'Unknown'` sets `TargetReady=Unknown`.
- An absent readiness condition sets `TargetReady=Unknown` with reason `ReadinessNotReported`. Malformed or duplicate `Ready` conditions set it to `Unknown` with reason `ReadinessInvalid`.

A missing target sets `TargetReady=False` with reason `TargetNotFound`; a read failure sets it to `Unknown`. Missing generation information alone does not block the default check.

For a target with a different readiness contract, supply an override. It replaces the default check and requires one of:

- `mode: Predicates`, with the same `checks` and `observedGeneration` fields as dependency readiness.
- `mode: None`, without checks. Application is observable, while target readiness remains `Unknown` with reason `ReadinessNotConfigured`.

For example, a target that reports an `Available` condition can use:

```yaml
lifecycle:
  targetReadiness:
    mode: Predicates
    observedGeneration: IfPresent
    checks:
      - condition:
          type: Available
          status: 'True'
```

Explicit predicates use the dependency predicate semantics above: every check must pass, and missing required fields or conditions leave `TargetReady=False`. No provider-specific condition is assumed beyond the default `Ready` contract. KCC, ACK, and ASO profiles are deferred until their resource-specific contracts are validated; users can declare overrides in this version.

The controller evaluates target readiness independently, including while inputs are blocked. If prerequisites become unsatisfied, retain the existing target and pause desired-field updates. When references still resolve, render and compare desired state even if dependencies are unready. Dependencies gate writes; they do not independently determine wrapper readiness.

## Template language

Use a restricted Go-template-style expression syntax over a parsed YAML/JSON tree. This is a kdc language, not full Helm or unrestricted `text/template` execution.

| Template node | Rendered result |
| --- | --- |
| `'{{ .refs.application.spec.replicas }}'` | The selected field's original type, such as a JSON number. |
| `'{{ .refs.settings.data }}'` | The selected field's original type, including an array or map. |
| `'https://{{ .refs.service.status.host }}/api'` | String interpolation; embedded results must be strings. |
| A scalar without expressions | The literal scalar, unchanged. |

A whole-scalar expression returns its evaluated value directly, without a text or YAML round trip. This requires a typed evaluator, rather than ordinary text-template execution. Surrounding literal text, including whitespace, makes the node an interpolated string. Expressions can appear in scalar values, never mapping keys or identity fields. A literal `{{` is written as `{{ "{{" }}`.

Controller expressions use only `.refs` for external inputs, plus quoted strings, numeric and boolean literals, function calls, parentheses, and pipelines. Pipeline results become the final argument of the next function. Undeclared aliases are configuration errors; missing fields on resolved objects produce waiting status. Blocks, loops, template definitions, includes, user methods, environment access, file access, shell execution, and network access are excluded. Collections are passed as typed values or constructed by functions. `.values` is a CLI-only input described below and must not remain in a wrapper.

### Initial function catalog

| Functions | Contract |
| --- | --- |
| `lower`, `upper`, `trim`, `replace` | String operations; `replace old new input` replaces every match. |
| `join`, `split` | `join separator strings` returns a string; `split separator string` returns a list. |
| `default` | `default fallback value` uses fallback only for null; preserves false, zero, and empty values. |
| `list`, `dict` | Construct a list or map; `dict` requires alternating string keys and values and rejects duplicate keys. |
| `get`, `has`, `length` | `get map key` fails if absent; `has map key` tests presence; `length` counts list/map entries or string Unicode code points. |
| `concat`, `merge` | Concatenate lists; shallow merge maps left to right, with later keys winning. |
| `toString`, `toNumber`, `toBool` | Explicit scalar conversions; reject invalid or lossy conversion. Booleans parse only `true` and `false`; numeric strings use JSON number syntax. |
| `toJson`, `fromJson`, `toYaml`, `fromYaml` | Deterministic serialization and strict parsing; reject duplicate keys and multiple YAML documents. |

`toString` accepts strings, booleans, and numbers; numbers use a canonical JSON representation. `toNumber` accepts numbers and numeric strings. `toBool` accepts booleans and the two supported strings. Collections and null cannot be converted by these functions. JSON/YAML parsers yield JSON-compatible values; YAML custom tags and non-string map keys are rejected. Encoders sort map keys.

No clock, randomness, or discovery functions are exposed. Evaluation errors identify the target pointer and function without echoing inputs. Proposed initial limits are 256 KiB of serialized target, 1,000 expression nodes, nesting depth 64, 100,000 evaluation operations, and 512 KiB of rendered JSON. Resolved reference snapshots are separately bounded to 1 MiB each and 8 MiB total per reconciliation; exceeding limits blocks application with `InputLimitExceeded`. These limits are enforced at admission where possible and otherwise before application, with bounded parsing and evaluation. They are controller-wide limits, not user overrides.

## Ownership and reconciliation

kdc uses [Server-Side Apply](https://kubernetes.io/docs/reference/using-api/server-side-apply/) with a field manager derived from the wrapper UID, such as `kdc/<uid>`, and never forces conflicts. It manages the desired fields supplied by the rendered target. Removing a desired field relinquishes ownership and follows Kubernetes apply semantics; status is never applied. Target schemas determine list and map ownership granularity, so providers with atomic fields may produce broader conflicts.

On first creation, include an annotation `kdc.rolphr.dev/wrapper-uid` and a label `kdc.rolphr.dev/wrapper-name`. Persist the returned target UID in status. Do not attach a garbage-collecting owner reference under any deletion policy. An existing target without the matching wrapper UID is rejected with `TargetAlreadyExists`. Automatic adoption and forced takeover are excluded.

Install the wrapper finalizer before creating a target. A create race must yield an ownership check, never an unconditional apply to the object that won the race. Before updates or deletion, verify the ownership marker and recorded target UID. Use resource-version preconditions for writes that require identity continuity and UID preconditions for deletion; re-read on conflict. API request mechanics must preserve these checks across create, apply, and restart recovery. Matching markers permit recovery when a crash occurred before status persisted the target UID.

If the managed target disappears, recreate it only when prerequisites are satisfied. A replacement without matching ownership blocks reconciliation. Two wrappers claiming the same identity produce an ownership conflict. API authorization, invalid target schema, or field conflicts block application with actionable reasons; no partial rendering is applied.

Reconcile on wrapper changes, referenced object changes, dependency changes, target changes, and interval expiry. Index relations by discovered resource identity, namespace, and name. Detect cycles among known wrappers by mapping referenced target identities back to their wrappers; mark participating wrappers `DependencyCycle`. External missing sources stay waiting. Watches reduce latency; periodic discovery and reconciliation recover from missed events or newly installed CRDs.

Compare the observed target's managed desired fields with the current render; skip unchanged applies while still detecting drift. Account for API-server defaulting and normalization, and exclude status, server metadata, and fields managed solely by other controllers. This comparison covers `spec` and other desired fields such as ConfigMap `data`. A successful apply alone does not prove convergence: verify the resulting target and its readiness before reporting wrapper readiness. Retry transient API failures with exponential backoff, bounded at five minutes, and add interval jitter of up to ten percent. Successful retries reset backoff. Reads are not a multi-object transaction; the next event or interval reconciles inputs that change during rendering.

## Status and diagnostics

Status contains identities and diagnostics, never rendered targets, resolved values, Secret contents, or value hashes.

| Status field | Meaning |
| --- | --- |
| `observedGeneration` | Latest wrapper generation evaluated, including blocked or failed evaluations. |
| `target` | Observed target API version, kind, namespace, name, and UID. |
| `lastSuccessfulReconcileTime` | Latest successful desired-state verification or application with satisfied prerequisites. |
| `lastAppliedTime` | Latest successful target create or apply. |
| `appliedGeneration` | Wrapper generation used in the latest successful desired-state verification or application. |
| `unresolvedReferences` | Named references with safe reason codes and pointers, without values. |
| `unreadyDependencies` | Named dependencies with safe reason codes and failed check indices. |
| `conditions` | Kubernetes-style conditions with type, status, reason, message, observed generation, and transition time. |

Always publish `InputsResolved`, `DependenciesReady`, `Applied`, `DesiredStateMatches`, `TargetReady`, and aggregate `Ready`. Empty references or dependencies satisfy their respective conditions. `Applied` reports successful application or desired-state verification for the current wrapper generation; it is diagnostic and does not alone establish readiness. Retain historical application timestamps when later attempts are blocked or fail. Generation changes alone never imply a new apply succeeded.

`DesiredStateMatches=True` means the observed managed target's desired fields match the current template render using current reference inputs. Set it to `False` for drift or a missing target, and `Unknown` when unresolved inputs, rendering errors, or read failures prevent comparison. Compare against the current render, never only the last applied render. An unrelated target cannot establish convergence.

For a non-deleting wrapper, `Ready=True` if and only if `DesiredStateMatches=True` and `TargetReady=True`. If either is false, set `Ready=False`; otherwise, if either is unknown, set `Ready=Unknown`. Input and dependency conditions explain reconciliation progress but are not additional readiness gates. Thus an unready dependency pauses updates without making a still-converged, ready target's wrapper unready. Missing inputs prevent a current render from being verified and therefore prevent `Ready=True`. While deleting, report `Ready=False` with reason `Deleting`.

An absent default readiness signal or explicit `mode: None` leaves target and wrapper readiness unknown unless desired-state mismatch already makes the wrapper unready. Generation-aware consumers must compare condition `observedGeneration` with wrapper `metadata.generation`. Target readiness checks use the configured generation policy, defaulting to `IfPresent`, so available stale generation information cannot establish readiness for a newly changed spec.

Reasons include `ReferenceNotFound`, `FieldMissing`, `DependencyNotReady`, `GenerationNotObserved`, `TemplateInvalid`, `RenderFailed`, `TargetInvalid`, `TargetAlreadyExists`, `OwnershipConflict`, `ApplyConflict`, `AccessDenied`, `DependencyCycle`, and `DeletionBlocked`. Waiting for missing inputs is normal and should not flood warning events. Messages identify the declaration or template path; provider errors and expression errors must be sanitized before status, events, or logs.

## Deletion contract

The finalizer coordinates all policies and is installed before target management starts.

| Policy | Finalizer behavior |
| --- | --- |
| `abandon` | Retain target, remove kdc ownership markers where permitted, and remove finalizer. Do not force field changes or delete the target. |
| `delete` | Verify ownership, request deletion with a UID precondition, and wait until that UID is gone. |
| `prevent-deletion` | Retain target and finalizer; report `DeletionBlocked`. |

If abandonment cannot remove markers, complete wrapper deletion and emit a sanitized diagnostic; stale markers do not authorize a different wrapper UID. A wrapper with no target can complete `abandon` or `delete` immediately. An unrelated replacement is never deleted. Ownership failures during `delete` block finalization until resolved or the policy changes to `abandon`.

After a deletion request, permit changing only `spec.lifecycle.deletionPolicy` within the spec. This allows recovery from `prevent-deletion` by selecting `abandon` or `delete`. No further desired-state applies occur while deleting. Direct target deletion is not blocked by this API, and removing the finalizer manually bypasses its protection. Infrastructure cleanup remains the target controller's responsibility.

## CLI authoring contract

```sh
kdc wrap ./infrastructure --namespace infrastructure --values values.yaml --output wrappers.yaml
kdc validate ./infrastructure --values values.yaml
kdc validate --wrappers wrappers.yaml
kubectl apply -f wrappers.yaml
```

`wrap` and `validate` operate locally and never require cluster dependencies. `wrap` emits manifests; it does not apply them. `validate` checks either a template folder or already wrapped resources, reports all diagnostics, and exits nonzero on errors. It cannot prove live readiness, permissions, installed target schemas, or ownership.

Input files are `.yaml` or `.yml` files visited recursively in sorted relative-path order. The file passed to `--values` and the output file are excluded. Multi-document files produce one wrapper per nonempty document. Ordinary manifests use the same path. Templates must be parseable YAML, so scalar expressions must be quoted.

Declare kdc settings in a reserved target annotation:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: application-settings
  annotations:
    kdc.rolphr.dev/config: |
      references:
        upstreamSettings:
          apiVersion: v1
          kind: ConfigMap
          name: upstream-settings
      lifecycle:
        deletionPolicy: abandon
data:
  endpoint: '{{ .refs.upstreamSettings.data.endpoint }}'
  replicas: '{{ toString .values.replicas }}'
```

With this CLI input:

```yaml
# values.yaml
replicas: 3
```

The annotation accepts `references` and `lifecycle`. The `lifecycle` object accepts optional `targetReadiness`, `dependencies`, `reconcileInterval`, and `deletionPolicy`; reject unknown settings and former top-level lifecycle fields. Omit `lifecycle.targetReadiness` to use the default `Ready` condition check; the CLI must preserve that omission. Extract settings into the matching wrapper spec fields, preserving lifecycle nesting, and preserve the complete manifest as `spec.target`, removing the reserved annotation. Reject prohibited fields rather than silently dropping them.

Choose namespace from the manifest or `--namespace`; reject conflicting values and require one to be available. Values files are literal JSON-compatible YAML mappings, never evaluated templates. Repeatable `--values` options shallow-merge maps left to right. The CLI substitutes `.values` expressions into the target before emitting wrappers; the example produces the literal string `"3"`. Values are never copied into a separate wrapper field. Changes to a values file require wrapping and applying again.

Packaging evaluates expressions that use `.values` and leaves `.refs` expressions intact for the controller. Reject individual expressions combining both roots; author separate nodes or interpolation segments instead. A scalar with separate `.values` and `.refs` segments is partially substituted while preserving its runtime string interpolation. Values-derived strings containing template delimiters are escaped as literal expression strings to prevent reinterpretation. Admission rejects wrappers retaining `.values` lookups. CLI substitutions may also set reference identities and predicate constants in the extracted settings, but may not read cluster resources.

At runtime, all external inputs come through declared references. Users can hardcode literals in `target`, substitute a local values file during packaging, or reference resource fields, including a ConfigMap containing shared configuration. Never place plaintext secrets in values files or source templates; read them through declared references when permitted.

Generate wrapper names as `kdcr-` plus the first 32 lowercase hexadecimal characters of SHA-256 over the canonical JSON array `[apiGroup, kind, namespace, targetName]`. Derive `apiGroup` from `apiVersion`, using the empty string for core resources. File paths, document order, and API version revisions do not affect naming. Reject duplicate target identities in one invocation. A version change retains the wrapper name but fails the immutable identity check, requiring an explicit migration.

Store only the relative source path and one-based document number in `kdc.rolphr.dev/source`. Diagnostics include file, document, line, and target pointer when available. Emit stable YAML ordering without timestamps or machine-specific absolute paths. Order wrapper spec fields as `references`, `lifecycle`, then `target` for readability.

## Validation and access policy

CRD validation enforces required fields, enums, name uniqueness, declared dependency aliases, and immutable target identity. Admission validates duration ranges, reserved fields, expression syntax, limits, and deletion-time spec changes. Controller validation repeats essential checks before writes and verifies discovered scope, deployment allowlists, ownership, and the complete rendered target against the API server. Missing CRDs remain waiting with `KindUnavailable`; malformed expressions fail validation.

Administrative deployment policy defines allowed target kinds, readable source kinds, namespaces, and Secret access. An omitted Secret-read grant denies it even if the service account has broader RBAC. Creating wrappers grants indirect use of the controller's permitted capabilities, so administrators must restrict wrapper writers accordingly; controller RBAC alone does not isolate tenants. Cross-namespace reads require the admission-controlled grants described above, in addition to deployment policy and RBAC. Targets remain in the wrapper namespace.

Treat all resolved inputs as potentially sensitive. Never persist them in status or diagnostic hashes, and never echo target apply payloads or unsanitized API errors. Rendering into a target intentionally persists the result there, so allowed source-to-target combinations must match the namespace's data access policy.

## Design acceptance checks

Before implementing or promoting this API, validate:

- Typed rendering for null, false, zero, empty values, lists, maps, interpolation, and deterministic functions.
- Missing references, predicate failure, stale generations, changed outputs, cycles, and readiness loss after application.
- Wrapper readiness for a converged ready target, a ready target with drift, a matching unready target, unknown target readiness, and dependency readiness loss while the target remains converged and ready.
- Omitted `lifecycle.targetReadiness` with true, false, unknown, absent, duplicate, and stale `Ready` conditions; overrides for alternative condition and field contracts; CLI preservation of an omitted override.
- Immutable identities, duplicate targets, apply conflicts, drift, target replacement races, and restart recovery between creation and status updates.
- Every deletion policy, policy changes during deletion, blocked provider cleanup, and orphaned markers.
- Local values substitution, rejection of runtime `.values`, shared reference aliases, multiple documents, stable identities after file moves, and source diagnostics.
- Same-namespace defaults, cross-namespace admission grants and revocation, kind policy, Secret access, bounded evaluation, and diagnostics that cannot leak resolved values.

Representative KCC, ACK, and ASO chains should establish whether the predicate API is sufficient before adding profiles. The API group, proposed limits, and defaults require review before a first release; cluster-scoped targets, adoption, and direct target deletion protection remain outside `v1alpha1`. Cross-namespace grant authoring and revocation must be validated with the admission-controller policy before release.
