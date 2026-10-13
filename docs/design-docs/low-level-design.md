# kdc Low-Level Design

Status: Proposed

This document defines the proposed Go implementation of the controller and CLI.
The repository currently contains designs, schemas, and examples, but no Go
implementation. Package names, interfaces, and operational settings below are
implementation proposals, not existing functionality.

The [API design](api-design.md) defines the behavioral contract and takes
precedence over earlier open decisions in the
[high-level design](high-level-design.md). The existing
[CRD and standalone schema](../schema.md) remain validation inputs. This design
does not add fields to the public Resource specification.

## Binaries and packages

Build two binaries from one Go module: `kdc` for offline packaging and validation,
and `kdc-controller` for reconciliation and admission. Share parsing, expression,
and validation packages so authoring and runtime semantics agree.

```text
cmd/
  kdc/main.go
  kdc-controller/main.go
api/v1alpha1/
  groupversion_info.go
  resource_types.go
  zz_generated.deepcopy.go
internal/
  cli/                 Commands, options, output, exit codes
  packaging/           File discovery, settings extraction, wrapper generation
  document/            Strict YAML/JSON decoding and source locations
  expression/          Lexer, parser, AST, typed evaluator, function registry
  validation/          Shared static and rendered-manifest validation
  controller/          Reconcile orchestration and outcome classification
  discovery/           GVK mapping, scope checks, refresh
  references/          Authorized, bounded input snapshots
  readiness/           JSON Pointer and generation-aware predicates
  relations/           Reverse indexes, target claims, cycle detection
  watches/             Metadata informer lifecycle and event routing
  target/              Ownership checks, create, apply, comparison, deletion
  policy/              Deployment policy and reference authorization
  admission/           Resource create/update validation
  status/              Conditions, safe diagnostics, status patching
  telemetry/           Metrics, logs, events
```

Use `sigs.k8s.io/controller-runtime` for manager lifecycle, the typed Resource
client, admission, leader election, and reconciliation. Use `k8s.io/client-go`
discovery and dynamic clients for arbitrary target and source kinds. Use
`k8s.io/apimachinery` metadata, runtime objects, and errors. Select and pin
compatible Kubernetes modules and controller-runtime together when creating
`go.mod`; do not infer a dependency version from the CRD validation version.

Keep Kubernetes I/O outside the expression and readiness packages. Pass
`context.Context` as the first argument at I/O and cancellable evaluation
boundaries, propagate request cancellation, and do not store contexts in service
structs. Inject clocks and scheduling randomness for deterministic tests. Library
packages return diagnostics rather than printing or exiting. The CLI command
runner accepts readers and writers for testing.

Return concrete service types from constructors. Define a narrow interface in
the consuming package only when a concrete dependency must be substituted, such
as the controller's API reader or the CLI's filesystem writer; reuse existing
client and `io` interfaces where suitable. Pure expression and readiness logic
can be tested directly without mock interfaces. This follows
[Go interface guidance](https://go.dev/wiki/CodeReviewComments#interfaces).

## Go API model

`Resource` and `ResourceList` implement `runtime.Object`, register with the
manager scheme, and embed the normal Kubernetes type and object metadata.
Generate deepcopy methods. Keep the checked-in CRD authoritative until generated
schema output can reproduce its defaults, CEL rules, and preservation semantics.

| Go representation | JSON field and purpose |
| --- | --- |
| `map[string]Reference` | `spec.references`; literal GVK, name, optional namespace |
| `Lifecycle` | `spec.lifecycle`; dependencies, interval, policy, optional readiness |
| `*TargetReadiness` | `lifecycle.targetReadiness`; nil preserves the default behavior |
| `[]Dependency` | Named reference alias and `ReadinessSpec` |
| `Check` | Exactly one of `*ConditionCheck` or `*FieldCheck` |
| `json.RawMessage` | `spec.target`; preserve arbitrary target structure |
| `apiextensionsv1.JSON` | Predicate `equals`; preserve any JSON value |
| `[]metav1.Condition` | `status.conditions` |
| `*metav1.Time` | Optional application and reconciliation timestamps |
| `types.UID` | Observed target UID |

Represent the interval as its duration string and parse with `time.ParseDuration`
in shared validation; enforce `1s` through `24h`. Use typed string constants for
deletion policy, readiness mode, generation policy, and reason codes.

Presence is separate from value: a field predicate decoder checks whether
`equals` exists before decoding it. Explicit JSON null is valid; absence is not.
Likewise, reject prohibited fields such as target `status` even when null. Apply
semantic defaults on an in-memory copy so omitted lifecycle and readiness fields
do not unexpectedly appear in CLI output.

Decode target and reference values into a private JSON value tree using
`json.Number`. Keep exact numeric semantics during evaluation and equality,
including integers beyond float64 precision. Convert to Kubernetes unstructured
values only at the client boundary; reject unsupported or lossy representations
as `TargetInvalid` rather than silently rounding. Typed reference decoding must
also avoid silently losing precision before evaluation.

Proposed diagnostic entries are `{reference, reason, pointer}` for unresolved
references and `{name, reference, reason, failedChecks}` for dependencies.
Pointers are JSON Pointers and check indices are zero-based. These fit the
currently open diagnostic schemas; formalizing their public structure requires
an API design and schema update before release. Never include values or hashes.

## Shared parser and expression engine

### Document decoding

Parse YAML as nodes to retain line and column information. Walk nodes before
conversion to reject duplicate keys, non-string mapping keys, custom tags, and
unsupported values. Accept JSON-compatible scalars, lists, and maps. Expand YAML
aliases only with bounded depth and node counts; reject recursive aliases. Track
each converted node by file, one-based document number, and JSON Pointer.

JSON decoding also rejects duplicate keys and trailing documents. Every parse
boundary, including `fromJson`, `fromYaml`, values files, and the config
annotation, uses these strict rules. Stream source files and bound input bytes
and expanded nodes before allocating large trees. Packaging limits are shared
options with documented defaults to be selected during implementation; do not
load an unbounded directory into memory.

### Expression compilation

Compile only string scalar values containing delimiters. Mapping keys and target
identity fields must stay literal. The parser constructs explicit AST nodes for
root lookups, literals, calls, parenthesized expressions, and pipelines. Reject
blocks, loops, definitions, method invocation, unsupported roots, and unknown
functions. Use a dedicated parser and evaluator rather than executing
`text/template` and reparsing its output.

The compiled scalar distinguishes a whole expression from interpolation. Whole
expressions return typed values; interpolation accepts only strings. Any literal
text, including whitespace, makes the scalar interpolation. Record the target
pointer on each compiled scalar and reference alias on each lookup.

```go
type Environment struct {
    Refs   map[string]Value
    Values map[string]Value // Packaging only; absent at runtime.
}

type Engine struct {
    // Unexported registry and limits; immutable after construction.
}

// Proposed signatures; method bodies omitted.
func New() *Engine
func (e *Engine) Compile(target Value, mode CompileMode) (*Program, []Diagnostic)
func (e *Engine) Evaluate(ctx context.Context, program *Program, env Environment) (Value, error)
func (e *Engine) SubstituteValues(program *Program, values Value) (Value, []Diagnostic)
```

`Engine`, `Value`, `Program`, and `CompileMode` are exported identifiers in the
repository's internal packages, usable by both binaries but restricted by Go's
`internal` import rules. Keep value-tree and program representations unexported;
expose constructors, accessors, and mode constants rather than mutable AST fields.
Programs and inputs remain immutable; evaluation budgets and scratch data belong
to each call so one engine/program may be shared safely across reconciles.

Track map-field presence independently of null. Return typed errors for absent
alias, missing field, invalid function arguments, and exhausted budget. Classify
with `errors.Is`/`errors.As`, preserve safe causes with `%w`, and never match error
strings. `FieldMissing` waits; invalid configuration or incompatible types fail
without applying a partial tree. Evaluation checks cancellation during bounded
traversal and returns the context error without converting it into a template
failure. Never format function arguments into errors. See
[Go errors](https://pkg.go.dev/errors) and [context](https://pkg.go.dev/context).

Implement the complete initial function catalog in the API design through a
closed registry with argument count and type checks. Functions are pure. Compare
JSON numbers mathematically without string coercion; encoders sort map keys and
produce canonical numeric output. Reject duplicate `dict` keys and lossy scalar
conversions. Pipeline results become the final function argument.

Enforce the API's proposed runtime limits: 256 KiB serialized target, 1,000
expressions, depth 64, 100,000 evaluation operations, and 512 KiB rendered JSON.
Charge traversal and collection/string processing against the operation budget,
and bound function output before allocation. Reference snapshots are at most
1 MiB each and 8 MiB total. These constants require acceptance tests and release
review; users cannot raise them in a Resource.

## Controller startup and access policy

The controller loads validated administrative policy, registers the Resource
scheme, initializes discovery and dynamic clients, then starts admission and
the manager. Proposed deployment options cover allowed namespaces, target and
source kinds, Secret access, worker concurrency, Kubernetes client QPS/burst,
request deadlines, metrics, probes, leader election, and webhook TLS.

Use bounded worker concurrency and one active reconcile per wrapper key. Only
the elected leader performs reconciliation and watch-index maintenance. Admission
may run on every replica. Readiness indicates initialized policy, synchronized
Resource cache, and a serving webhook where enabled; unavailable provider kinds
block affected wrappers rather than all controller readiness.

`Policy.AuthorizeRead` checks source kind, source namespace, wrapper namespace,
Secret permissions, and any cross-namespace authorization. `AuthorizeTarget`
checks the target kind and wrapper namespace. RBAC remains an independent API
server check. Run authorization before every reference read and immediately
before target writes. Filter the watch registry through policy too.

The cross-namespace grant API is not defined by the existing designs. Provide an
authorization interface, but deny cross-namespace reads until a concrete
admission-enforced adapter is specified and tested. Re-check current grants on
every reconcile; revoked access must invalidate use of cached inputs. Policy
changes enqueue affected wrappers. Permit only configured source-to-target flows,
especially for Secrets.

## Discovery, watches, and dependency graph

Resolve each GVK through Kubernetes discovery and a resettable REST mapper to a
GVR and scope. Do not pluralize kinds heuristically. Reject cluster-scoped targets
and sources in this version. Missing mappings produce `KindUnavailable`; refresh
discovery periodically and after mapping failures so newly installed CRDs work.

Maintain three indexes under a short-held mutex:

- Source identity to wrappers declaring that source, including references without
  readiness gates.
- Target identity to its claiming wrappers and reverse target event subscribers.
- Wrapper key to its registered relations, enabling removal on update or deletion.

Watch keys use discovered GVR, namespace, and name. Canonical graph identities
use group/resource, namespace, and name so served API version aliases do not
hide cycles or duplicate target claims. Preserve the declared version for reads,
rendering, and immutable identity validation.

Create a `metadata.Interface` client and use
`metadatainformer.NewFilteredMetadataInformer` for authorized GVR/namespace
combinations with subscribers. Cache `PartialObjectMetadata`, never full dynamic
objects or Secret payloads; full object GETs occur only during resolution. Strip
unneeded annotations and managedFields in the cache transform, retaining identity,
resourceVersion, generation, deletion timestamp, and finalizers. Do not use watch
payloads as authorization evidence. If metadata negotiation or list/watch is
unsupported or forbidden but GET is permitted, record a safe diagnostic and use
periodic reconciliation. Do not fall back to a full-object informer. See the
[metadata informer API](https://pkg.go.dev/k8s.io/client-go/metadata/metadatainformer).

Handle add, delete, and deletion tombstones. For updates, enqueue source/target
subscribers whenever resourceVersion changes, including status-only changes;
generation-only predicates would miss provider readiness and outputs. Ignore
same-resourceVersion resync notifications. For a wrapper's own reconcile queue,
accept generation, deletion timestamp, finalizer, or relevant ownership metadata
changes; suppress its own status-only updates while still notifying wrappers
referencing its status. Enqueue keys without rendering in event callbacks.

Reference-count registrations. Proposed deployment setting `maxActiveWatches`
defaults to 128 GVR/namespace informers; excess subscriptions retain periodic
reconciliation and an observable fallback reason. Release slots when subscribers
disappear and retry eligible registrations without creating duplicate informers.
Each registration owns a cancellation context and completion signal. On last
subscriber removal, policy revocation, leader loss, or manager shutdown, cancel
list/watch and cache-sync waits, detach handlers, and join informer goroutines
before discarding state or reusing slots. Watch maintenance is a leader-elected
manager runnable; rebuild indexes and registrations on each new leadership term.

Build graph edges from every declared reference to known target claims. Run a
strongly connected component algorithm when relations change. Self-loops and
multi-wrapper components produce `DependencyCycle` and block writes for their
participants. Missing external objects are waiting inputs, not graph vertices
invented as wrappers. Multiple target claims block competing writes with an
ownership diagnostic. Rebuild all relations from wrappers at leader startup and
enqueue participants when a cycle appears or disappears. The informer/workqueue
separation follows the [client-go controller architecture](https://github.com/kubernetes/kubernetes/blob/master/staging/src/k8s.io/client-go/ARCHITECTURE.md).

## Reconciliation state machine

The reconciler coordinates narrow services rather than embedding parsing and
Kubernetes mutation in one method. Proposed boundaries are `Resolver.Resolve`,
`Readiness.Evaluate`, `Target.Observe`, `Target.Compare`, `Target.Ensure`, and
`Status.Publish`. Each result includes safe diagnostics and a classified outcome:
success, waiting, invalid configuration, conflict, or transient failure.

1. Read the wrapper through the manager cache for ordinary reconciliation. On
   NotFound, remove relations and watch subscriptions. Cached reads schedule work;
   they do not establish identity continuity before mutations.
2. Validate/default an in-memory spec, register relations, and check policy.
   If deleting, enter finalization immediately and perform no desired-state apply.
3. Install `kdc.rolphr.dev/finalizer` with a resource-version-guarded metadata
   patch. Return and re-read before managing the target.
4. Observe the target and evaluate target readiness even if inputs are blocked.
   Verify marker and persisted UID before treating it as managed.
5. Resolve all declared aliases once, in stable alias order, using authorized,
   bounded reads. Aliases identifying the same object may share one read. Freeze
   the snapshot for both rendering and dependency evaluation.
6. Evaluate every dependency predicate and cycle state. Continue evaluating other
   aliases/checks after a normal waiting result to publish useful diagnostics.
7. If inputs resolve, render, validate, and compare the complete target against
   current desired state, including when dependencies are unready. Otherwise
   set desired-state comparison to Unknown.
8. With valid render, resolved inputs, ready dependencies, allowed policy, and
   no cycle or ownership conflict, create or apply only if changes are needed.
9. Verify the returned object or fresh target read, compare current desired
   fields, and evaluate readiness again after writes. Reduce status and publish.
10. Schedule the next interval or classified retry.

Inject `mgr.GetAPIReader()` as the uncached typed reader for wrapper continuity
checks; never use `mgr.GetClient().Get` for those checks because manager reads may
come from a stale cache. Before each target mutation, finalizer patch, or status
publication, GET the wrapper through this reader. Compare UID, generation, and
deletion timestamp with the evaluated snapshot, and check the expected finalizer
state. On NotFound or changed identity/state, discard the computed action and
reconcile current state; a newly deleting wrapper must enter finalization. Use the
fresh wrapper resourceVersion to guard metadata and status patches. A read error
blocks the mutation and follows classified retry behavior. The distinction follows
the [controller-runtime API reader contract](https://pkg.go.dev/sigs.k8s.io/controller-runtime/pkg/manager#Manager).

Target ownership and continuity checks always use direct dynamic-client GETs,
including after AlreadyExists, before apply or marker cleanup, during restart UID
recovery, and before Delete. Verify the wrapper marker and recorded UID, then use
that GET's resourceVersion for conditional patches and its UID for Delete
preconditions. Informer metadata and earlier target observations cannot authorize
a mutation. After an optimistic concurrency conflict, fetch both wrapper and
target directly, revalidate identity, policy, and prerequisites, and recompute the
operation in a new reconciliation. Do not replay a stale payload or reuse its
resourceVersion.

No snapshot is a multi-object transaction. A later event or interval resolves
inputs changed during reconciliation. Kubernetes has no transaction spanning
wrapper and target writes; a deletion beginning after the uncached check can
overlap an in-flight target write. The installed finalizer ensures the next
reconciliation still executes the selected deletion policy.

### Scheduling and retry contract

Map outcomes to `(ctrl.Result, error)` after publishing available safe status:

| Outcome | Return and next action |
| --- | --- |
| Success, missing input, unready dependency/target, unavailable kind, or pending finalization | `ctrl.Result{RequeueAfter: intervalWithJitter}, nil`; relevant events may enqueue earlier |
| Invalid configuration/schema, policy denial/Forbidden, ownership conflict, or SSA field ownership conflict | Publish the diagnostic; return the same interval result with nil error so policy, grants, and external state can recover |
| Timeout, transport error, throttling, server failure, or unknown API failure | `ctrl.Result{}, retryErr`; the configured workqueue limiter schedules retry |
| Optimistic resourceVersion conflict or changed continuity guard | Discard the action; return `ctrl.Result{}, retryErr` and recompute using fresh direct reads in the next reconciliation |
| Wrapper NotFound after relation cleanup, or completed deletion with no remaining wrapper | `ctrl.Result{}, nil`; no periodic work remains |

`intervalWithJitter` is the configured lifecycle interval plus independently
sampled jitter in `[0, 0.1 * interval]`. Inject randomness for tests. Do not set
deprecated `Requeue`, return `TerminalError` for states requiring periodic
recovery, or combine a retry error with `RequeueAfter`: controller-runtime ignores
the latter when an ordinary non-nil error is returned. Use the controller's queue
for delayed work and backoff reset; do not maintain a second retry timer or retry
counter. These return semantics follow the
[reconcile contract](https://pkg.go.dev/sigs.k8s.io/controller-runtime/pkg/reconcile).

Classify structured Kubernetes status reasons and causes, not error message
strings. A 409 alone does not distinguish stale resourceVersion from SSA field
ownership: inspect field-manager conflict causes and the operation context.
Never force an SSA ownership conflict. An ambiguous conflict receives a bounded
retry with fresh reads and no ownership takeover. Errors returned to the queue
must be sanitized because controller-runtime logs them. A transient status-write
failure uses error retry even if the original outcome was waiting or permanent.

Configure the controller's rate limiter explicitly before controller construction.
Proposed deployment settings are exponential backoff per wrapper key starting at
one second and capped at five minutes, plus a process-wide retry token bucket.
Expose the base delay and bucket QPS/burst as deployment settings; validate a
positive base no greater than the five-minute cap. Successful nil-error processing
lets controller-runtime forget that key's retry history. The cap bounds the
exponential component; global saturation and worker availability can delay
execution further. Compose limiters using the supported
[client-go workqueue API](https://pkg.go.dev/k8s.io/client-go/util/workqueue).

The retry bucket does not throttle ordinary events or interval requeues. Configure
bounded worker concurrency and shared client request QPS/burst separately across
typed, dynamic, and discovery clients, avoiding independent budgets per client.
Deployment capacity verification selects their values. Events and stale delayed
queue entries remain possible, so every invocation re-observes current state;
never rely on a timer as authorization or proof that inputs are unchanged.

## Target ownership, application, and convergence

Use the dynamic client and `unstructured.Unstructured` at the target boundary.
Inject the wrapper namespace plus `kdc.rolphr.dev/wrapper-uid` annotation and
`kdc.rolphr.dev/wrapper-name` label. Never add an owner reference, target status,
or unrelated server metadata. The field manager is `kdc/<wrapper UID>`.

### Secret payload normalization

For the exact core `v1`, kind `Secret`, normalize a private copy of the complete
render before intent comparison, POST Create, dry-run SSA, or persisted apply.
Keep `stringData` valid in authored templates and packaged wrappers; never rewrite
the source wrapper. Kubernetes merges it into `data` with `stringData` precedence
and cautions against its use with SSA in the [Secret documentation](https://kubernetes.io/docs/concepts/configuration/secret/#constraints-on-secret-names-and-data).

Validate both maps before merging: reject invalid shapes, non-string entries,
invalid Secret keys, and invalid base64 in `data` as `TargetInvalid`, even when
`stringData` would override that key. Decode each `data` entry with Go
`encoding/base64.StdEncoding`, then re-encode its bytes canonically. Encode each
`stringData` string's UTF-8 bytes with the same encoder, overwriting corresponding
`data` entries. Remove `stringData` from the normalized copy. Apply rendered-size
bounds again after encoding; encoding is a representation change, not encryption.

Use this normalized tree for both requested-value verification and the SSA
payload. Thus the manager owns `data` keys, and removing a key from the next
render follows ordinary SSA ownership relinquishment. Preserve unrelated keys
owned by other managers. Keep only short-lived per-reconcile copies and report
safe field pointers without plaintext, base64 values, or hashes.

### Create and update mechanics

| Observed state | Action |
| --- | --- |
| Target absent, prerequisites satisfied | POST Create with the field manager and ownership markers |
| Create returns AlreadyExists | Fresh GET and ownership check before further processing |
| Matching marker, no recorded UID | Recover target UID in status before continuing |
| Matching marker and recorded UID | Compare and apply with current resourceVersion |
| Wrong marker or wrong recorded UID | Block; never adopt, update, or delete |

POST creation prevents apply from modifying a target that wins a creation race.
Include `FieldValidation: Strict`. Follow creation with the same manager's SSA
verification/application as needed, and test the transition from Create-managed
fields to Apply-managed fields. A crash before UID persistence is recoverable only
through the matching wrapper UID marker.

For existing targets, send `types.ApplyPatchType` with the latest observed
`metadata.resourceVersion`, the wrapper field manager, and `Force=false`.
Re-read and check identity again on stale-version conflicts. Do not use an
unconditional upsert for an existing object: the update must fail if it was
replaced between observation and mutation. Validate that guarantee against real
API-server behavior before release. Kubernetes documents apply ownership and
resource-version concurrency in [Server-Side Apply](https://kubernetes.io/docs/reference/using-api/server-side-apply/)
and [API concepts](https://kubernetes.io/docs/reference/using-api/api-concepts/).

### Comparison algorithm

Plain object equality would continually reapply defaults and provider status.
Comparison must account for the target schema and the wrapper manager's previous
field ownership, including fields removed from the latest render.

For a managed existing target, construct a resource-version-guarded dry-run SSA
request with `Force=false` and strict field validation. Its returned object is
the candidate normalized by the API server. Dry-run requests do not persist
changes, as defined in [Kubernetes API concepts](https://kubernetes.io/docs/reference/using-api/api-concepts/).
They may still require webhook execution and API permissions; classify failure
without falling back to an unsafe equality result.

Retain the current render as a separate intent tree before submitting dry-run.
A webhook-mutated candidate must never replace that intent. Use structural schema
and structured-merge field sets for two independent checks:

1. **Requested values:** compare explicitly requested fields against both the
   candidate and live object. Apply only documented, deterministic representation
   normalization, such as numeric equivalence and supported built-in field
   conversions. Never infer equivalence from a webhook's returned value. Match
   associative lists by schema keys and sets by element semantics; respect atomic
   collection boundaries and explicit empty collections. Ignore defaults added
   at omitted fields, including within requested objects, while still checking
   every supplied value. Include kdc ownership markers.
2. **Apply effects:** compare candidate and live desired data over the union of
   current intended fields and previous manager-owned fields. Exclude status,
   server metadata, resourceVersion, and fields owned solely by other managers.
   Compare data, not incidental managedFields timestamps. Removed fields require
   an apply to relinquish ownership even when another manager retains their values.

`DesiredStateMatches=True` requires verified managed identity, both requested-value
comparisons to pass, and no pending data or ownership changes. Candidate/live
equality alone is insufficient: if a webhook changes requested replicas from
`3` to `5` and the live object already has `5`, report False. A successful write
whose returned object violates intent also reports False; publish a safe field
pointer without values. Recheck on events or the normal interval rather than
immediately looping on successful writes that remain nonconvergent.

Cache discovered schemas with discovery invalidation. If normalization or field
ownership cannot be interpreted reliably, report comparison Unknown rather than
Ready=True. Core write-only fields such as Secret `stringData` require explicit
normalization as specified above; other unsupported write-only fields keep
comparison Unknown.

An unchanged, verified target skips persisted apply. Dry-run validation remains
API work, so expose its latency and request count. A dry-run conflict is
`ApplyConflict`, not proof of convergence.

## Readiness and status

Evaluate RFC 6901 pointers against immutable JSON values. Decode `~1` and `~0`,
support array indices, and reject invalid escapes, executable paths, and invalid
indices. Compare numbers numerically and all other values structurally without
coercion. Absent fields and unequal values are failed predicates.

Condition checks require a valid `status.conditions` array and exactly one
matching type. Apply the API's `IfPresent`, `Require`, or `Ignore` generation
rules separately for each check. Dependency checks all must pass; malformed
status produces safe diagnostics. Target overrides reuse the same evaluator.
Omitted target readiness uses the default `Ready` condition rules; `None` yields
Unknown with `ReadinessNotConfigured`.

Status reduction always emits `InputsResolved`, `DependenciesReady`, `Applied`,
`DesiredStateMatches`, `TargetReady`, and `Ready`. For non-deleting wrappers:

| DesiredStateMatches | TargetReady | Ready |
| --- | --- | --- |
| True | True | True |
| False | Any | False |
| Any | False | False |
| Otherwise | Otherwise | Unknown |

Dependencies gate writes, not aggregate readiness. A still-matching ready target
can keep Ready=True while a dependency is unready. `Applied=True` requires
successful application or verification for the current generation with satisfied
prerequisites. Preserve historical timestamps when later processing fails.
Advance `observedGeneration` for evaluated failures and waiting states too.

Update condition transition times only when condition status changes. Sort
diagnostics deterministically. Patch the status subresource only when its content
changes. Use the uncached wrapper continuity check and a resource-version-guarded
status patch so obsolete UID, generation, or deletion-state results are discarded.
On conflict, start a new reconciliation from direct reads rather than overwriting
newer status. Wrapper status-only updates must not trigger a self-reconcile loop,
but must still reach subscribers
that explicitly reference that wrapper's status.

## Finalization and admission

Finalization uses direct dynamic-client target identity reads and never
renders/applies desired fields. No target means `abandon` and `delete` can complete
immediately.

- `abandon`: remove only this wrapper's ownership markers with an identity-guarded
  patch where permitted, then remove the wrapper finalizer. If cleanup fails,
  emit a sanitized diagnostic and complete abandonment. Stale markers never
  authorize a new wrapper UID.
- `delete`: verify marker and recorded UID, issue Delete with UID preconditions,
  and keep the finalizer until that UID disappears. Wait through target finalizers.
  A replacement observed before verified disappearance blocks finalization; a
  replacement observed after the original UID is known gone is retained.
- `prevent-deletion`: retain the finalizer and target, reporting `DeletionBlocked`,
  including when no target exists, until policy changes.

Remove the wrapper finalizer using a resource-version-guarded patch that preserves
unrelated finalizers. Before each finalization mutation, use `mgr.GetAPIReader()`
to re-read wrapper identity, deletion state, and deletion policy. If generation or
policy changed, restart finalization using the current policy so recovery updates
are honored. A conflict triggers direct re-reads and a new reconciliation.

### Admission request contract

Register a validating webhook at `/validate-kdc-rolphr-dev-v1alpha1-resource`:

```yaml
admissionReviewVersions: [v1]
failurePolicy: Fail
matchPolicy: Equivalent
sideEffects: None
timeoutSeconds: 2
rules:
  - operations: [CREATE, UPDATE]
    apiGroups: [kdc.rolphr.dev]
    apiVersions: [v1alpha1]
    resources: [resources]
    scope: Namespaced
```

Match every wrapper namespace without bypass selectors. Do not match arbitrary
resources, `resources/status`, or DELETE. Status writes use CRD validation and
RBAC; DELETE proceeds to controller finalization. Metadata/finalizer UPDATEs
on the main resource do match: permit an unchanged spec without reauthorizing
source grants. Always enforce immutable identity and namespace on spec updates.
While deleting, compare old/new spec with only `lifecycle.deletionPolicy`
excluded and reject other spec changes. Permit a valid deletion-policy-only
recovery update even if an unchanged reference's grant has since been revoked.
Controller policy and identity checks still guard every target mutation.

CREATE and other spec-changing UPDATEs validate target metadata, literal
identities, supported expressions, declared aliases, limits, and reference access
policy. Use immutable snapshots from background-refreshed discovery and policy
caches; never render, read referenced objects, or run synchronous discovery in
admission. Reject denied cross-namespace access and unavailable, unsynchronized,
or expired authorization snapshots. Define the maximum snapshot age in deployment
policy and withdraw readiness when it expires. Cached scope must reject known
cluster-scoped kinds. Unknown/unavailable provider kinds may be accepted for
installation ordering; the controller must discover and verify scope before
reading references or writing targets. Request-context deadlines bound validation
below the two-second API timeout. Return sanitized diagnostics.

This follows Kubernetes guidance on narrow matching, short timeouts, and
side-effect-free validation in [webhook practices](https://kubernetes.io/docs/concepts/cluster-administration/admission-webhooks-good-practices/)
and [dynamic admission](https://kubernetes.io/docs/reference/access-authn-authz/extensible-admission-controllers/).
CRD and controller checks remain essential even with admission enabled.

### Availability and certificate lifecycle

Run at least two webhook-serving replicas behind a ClusterIP Service, with
topology spread and a disruption budget preserving one available replica.
Serve admission and refresh its caches on every replica independently of
reconcile leader election. Service readiness requires a valid serving certificate,
a listening handler, and synchronized, fresh policy snapshots; it must not depend
on becoming controller leader or on every provider kind being installed.
`failurePolicy: Fail` makes admission availability part of wrapper CREATE and
main-resource UPDATE availability, including finalizer and recovery updates.
Alert on failures, latency, endpoint availability, and certificate expiry.

Provision a certificate whose DNS SAN includes `<service>.<namespace>.svc` and
publish its issuing CA through `clientConfig.caBundle`. A deployment-managed
certificate reconciler renews the mounted Secret before expiry; the Go TLS
server atomically reloads valid key/certificate pairs on all replicas. During
CA rotation, first publish a bundle trusting old and new CAs, verify API-server
trust propagation, then switch serving certificates and verify all replicas.
Remove the old CA only after propagation and rollout complete. Keep webhook
configuration and certificate updates outside the webhook's resource rules.

Bootstrap in order: CRD and RBAC, certificates and Service, ready serving replicas,
then the Fail webhook configuration, then user wrappers. Keep cross-namespace
controller access disabled until admission enforcement is verified. Do not use
temporary Ignore behavior to admit wrappers during startup. Uninstall or replace
the webhook registration before removing its last serving endpoint.

## CLI implementation

### Commands and diagnostics

Implement `wrap <directory>` and `validate <directory>` with repeatable `--values`
and optional `--namespace`; `wrap` also accepts `--output`. Implement
`validate --wrappers <file>` as a mutually exclusive input mode that rejects
values substitution options. Neither command initializes a Kubernetes client.

`wrap` writes YAML to stdout by default. Send diagnostics to stderr. Proposed
exit codes are 0 for success, 1 for validation/processing failure, and 2 for
invalid command usage. Preserve file, document, line, and target pointer where
available; sort diagnostic output by source position and code. Print safe error
messages without rendered values or raw parser snippets that might contain them.

### Packaging pipeline

1. Resolve the input root, output path, and every values path. Walk recursively
   in sorted relative-path order for `.yaml` and `.yml`. Exclude all values files
   and the output file using canonical paths. Proposed symlink policy is to reject
   symlink inputs and avoid traversing directory symlinks; document this choice.
2. Parse values as literal JSON-compatible mappings. Shallow-merge files left to
   right and reject malformed or templated values files.
3. Parse each nonempty YAML document and retain source locations. Decode the
   reserved `kdc.rolphr.dev/config` annotation as one strict mapping permitting
   only `references` and `lifecycle`. Remove that annotation from the target;
   reject other prohibited fields rather than discarding them.
4. Compile expressions and classify each expression by root. Evaluate `.values`
   expressions locally, preserve `.refs` expressions, and reject expressions
   combining both roots. Evaluate pure literal/function expressions locally.
5. Substitute local values into target and extracted settings. References and
   predicate constants may use local values; reference identities and target
   identities must be literal after packaging. Reject runtime references in
   settings that must be literal. Resolve namespace from manifest/flag, require
   one, and reject disagreement.
6. Validate the resulting wrapper with shared semantic validation and the
   standalone schema. Reject unknown fields, duplicate dependency names,
   undeclared aliases, invalid expressions, and unsupported metadata. Preserve
   omitted target readiness. Offline validation cannot determine installed scope
   or provider schema, so defer those checks to admission/controller.
7. Compute the stable wrapper name, reject duplicate target identities across
   the entire invocation, and add the source annotation. Emit only after every
   document succeeds.

For mixed interpolation such as
`'https://{{ .values.prefix }}{{ .refs.service.status.host }}/api'`, replace the
local segment while preserving runtime string interpolation. A values-derived
string containing `{{` must be serialized as an escaped literal expression so
it cannot become executable runtime input. Traverse values-derived collections
too: nested delimiter-containing strings must remain literals. Do not reparse
values output as YAML or concatenate unchecked expression source.

Use SHA-256 over canonical JSON `[apiGroup, kind, namespace, targetName]`; take
the first 32 lowercase hexadecimal characters and prefix `kdcr-`. Core API group
is empty. Namespace defaults are resolved before hashing. Source paths, document
order, and API version revisions do not affect identity. Record relative path
and one-based document number in `kdc.rolphr.dev/source`, with a proposed encoding
of `<relative-path>#<document-number>`.

### Output and local validation

Write stable multi-document YAML in traversal order with wrapper spec fields
`references`, `lifecycle`, `target`. Sort arbitrary map keys, preserve JSON types,
and quote strings that YAML could reinterpret. Include no timestamps or absolute
paths. Buffer output within packaging limits; for `--output`, write a temporary
file in the destination directory and atomically rename only after successful
validation. Clean temporary files on failure. Never partially overwrite an
existing output with failed packaging.

Template-folder validation runs the same packaging pipeline without output.
Wrapped-resource validation runs strict decoding, schema, expression, and semantic
checks directly, rejects remaining `.values`, and never rewrites wrappers. Both
modes accumulate independent diagnostics and fail on any error. They establish
local syntax and structure, not live permissions, ownership, provider schemas,
or readiness.

## Observability and verification

Expose reconcile outcome counts, phase latency, reference wait counts, watch
health, apply conflicts, dry-run/persisted request counts, and queue depth. Use
bounded labels such as operation and reason; avoid wrapper names, aliases, field
values, and UIDs in metric labels. Logs may identify wrapper keys and declaration
pointers but never input objects or unsanitized Kubernetes errors. Rate-limit
events for waiting states. No rendered payloads, secrets, or value hashes enter
status, logs, events, or diagnostics.

### Capacity and workload benchmarks

All capacities and gates in this subsection are proposed acceptance targets, not
measurements or scalability claims. The 8 MiB reference snapshot limit is per
reconcile and excludes decoded-tree overhead, rendered trees, queued wrappers,
graph indexes, and metadata caches. Bound workers and account for the entire
process. Proposed deployment budgets are 10,000 tracked wrappers, 200,000 declared
reference relations, and 100,000 retained metadata objects, alongside the 128
active-watch cap. Enforce metadata budgets with bounded, paginated LIST handling
and incremental watch/cache accounting before unbounded allocation; stop an
over-budget informer and use polling. Monitor Resource-cache and graph budgets
too. If they cannot retain complete ownership/cycle indexes, withdraw controller
readiness and block target writes until scope or capacity is corrected; never
authorize writes from a truncated graph. Validate budgets against the deployment
memory limit before claiming a supported capacity.

Run this matrix against a real API server with declared machine resources,
Kubernetes/Go/module versions, API priority settings, controller client QPS/burst,
workers, namespace/GVR counts, and payload sizes recorded:

| Dimension | Proposed cases |
| --- | --- |
| Wrappers | 1,000; 10,000 |
| Distinct references per wrapper | 1; 5; 20 |
| Subscribers per changed source | 1; 100; 1,000; all wrappers |
| Serialized source sizes | 1 KiB; 256 KiB; near-limit fixtures within the 8 MiB aggregate cap |
| Interval | 60 seconds; 1 second saturation case |
| Watch distribution | Shared watches; more than 128 GVR/namespace combinations; metadata unsupported |
| Activity | Unchanged steady state; status changes; spec drift; source churn; restart/leader transfer |

For each steady-state case, warm caches for five minutes, measure for fifteen
minutes, and repeat three times. Separately measure bursts and recovery. Record
enqueue-to-worker p50/p95/p99 latency, end-to-end convergence latency, reconciles
per second, API requests by verb and dry-run/persisted operation, throttling and
errors, queue depth, active/fallback watches, RSS/heap/allocation profiles, and
goroutine counts. Measure performance without the race detector; use it in
separate correctness runs. Publish raw data and the tested capacity envelope.

Even unchanged reconciles issue dry-run PATCHes. For `N` wrappers, `R` distinct
references each, and `T` seconds between actual reconciles, the valid steady-state
path has a baseline of `N * (R + 2) / T` requests per second: reference GETs,
target GET, and dry-run PATCH. Wrapper mutation guards, status writes, discovery,
LIST/watch establishment, retries, and persisted writes add requests. At 10,000
wrappers, five references, and 60 seconds this baseline is about 1,167 requests
per second before those additions. This is arithmetic, not a measured result;
jitter changes the actual average interval and events add reconciles. Select
client/server budgets and intervals from measured capacity, accounting for API
users sharing the cluster.

Proposed release gates for a declared supported envelope: steady-state p99 queue
latency below 10% of the configured interval, no growing queue over the measured
window, RSS below 75% of the configured memory limit, no sustained throttling,
and observed request rate within the approved cluster API budget. After a burst,
return to the steady-state queue envelope within two intervals. Overload cases
must demonstrate bounded memory/watch counts and explicit degraded behavior;
passing them does not establish the latency gate. Unchanged targets must produce
zero persisted target applies after warm-up while dry-run counts remain visible.

Before implementation acceptance, cover these behaviors:

| Layer | Required coverage |
| --- | --- |
| Parser/evaluator unit and fuzz tests | Duplicate keys, aliases, limits, type preservation, missing/null distinction, numeric precision, function failures, delimiter escaping |
| Readiness unit tests | Default Ready behavior, overrides, malformed/duplicate conditions, stale generations, typed equality, JSON Pointer arrays and escapes |
| CLI golden and filesystem tests | Multiple documents, ordinary manifests, values merging, mixed segments, source locations, stable names, omitted readiness, deterministic output, atomic failure |
| Reconcile service tests | Input snapshots, dependency write gates, aggregate readiness, retries, grant revocation, cycles, uncached wrapper continuity and status guards |
| API-server integration tests | CRD defaults/CEL, admission, status subresource, strict target validation, SSA ownership/list semantics, requested-value verification across defaults and normalization, webhook overrides, resourceVersion races, UID deletion guards |
| Restart/concurrency tests | Crash before UID persistence, create races, target replacement, competing wrappers, leader index rebuild, policy changes during deletion, watch cancellation/join, registration churn |
| Provider acceptance tests | Representative KCC, ACK, ASO chains, changed outputs, drift, readiness loss, provider cleanup, Secret normalization |

Use a real API server through envtest for ownership and admission semantics;
fake clients cannot prove SSA, CEL, defaulting, or concurrency guarantees. Test a
mutating webhook that overrides a requested value with both a mismatching live
object and an already-equal candidate/live pair; neither may report convergence.
Also test omitted defaults, equivalent supported representations, schema-keyed
lists, field removal, and post-write mutation with no immediate reconcile loop.
Provider acceptance requires a cluster with the selected provider controllers. The existing
`examples/validate.py` remains a local fixture check, not end-to-end evidence.

Secret tests cover UTF-8 and empty strings, binary `data`, overlapping-key
precedence, malformed base64 and map values, unchanged source wrappers, and size
bounds after encoding. API-server tests verify create-to-apply ownership of `data`,
key removal with and without another owner's claim, preservation of unrelated
keys, drift detection, and no repeated persisted writes for unchanged secrets.
Diagnostic tests assert that neither plaintext nor encoded values escape.

Stale-cache tests leave the cached wrapper unchanged while the API wrapper starts
deleting, changes generation/deletion policy, or is replaced with another UID.
Verify uncached checks block obsolete target writes, status publication, and
finalizer patches, and that finalization honors the new policy. Inject read errors
and resourceVersion conflicts; assert no mutation uses a failed or cached guard.
API-server tests race target replacement and wrapper updates against conditional
apply, marker cleanup, status patches, and Delete. Verify conflict handling
re-reads and recomputes without touching an unrelated replacement.

Scheduling tests use an injected clock/random source and the controller queue to
verify outcome/result mapping, interval jitter bounds, exponential growth to the
five-minute cap, and reset after nil-error processing. Distinguish SSA ownership
conflicts from resourceVersion conflicts using structured API errors; verify no
forced apply, stale payload replay, or tight loop on waiting/permanent failures.
Cover transient status-write failures, queue deduplication, earlier events, shared
retry saturation, and completed-deletion cleanup without another periodic timer.

Admission tests verify CREATE/spec UPDATE enforcement, main-resource finalizer
updates, status bypass, DELETE finalization, and deletion-policy recovery after
grant revocation. Cover Equivalent matching, dry-run with no side effects,
unknown versus cluster-scoped kinds, stale/unsynchronized policy rejection, and
bounded timeout failures. Cluster deployment tests verify Fail behavior with no
endpoints, bootstrap ordering, service readiness on non-leader replicas,
single-replica loss, and leaf/CA rotation without interrupted admission.

Run `go test -race ./...` once Go code exists, including concurrent shared-engine
evaluation, relation changes, policy refresh, event routing, and shutdown. The
[Go race detector](https://go.dev/doc/articles/race_detector) detects only races
in executed paths, so include repeated watch create/remove, failed cache sync,
leader-transfer, and cancellation churn. Assert informer goroutines terminate,
slots are reclaimed, no events reach discarded queues, and goroutine/heap counts
return to an explained baseline after cleanup. Verify provider status-only events
enqueue subscribers and wrapper deletion/finalizer changes reconcile without a
generation change. Exercise watch and metadata budget overflow, unsupported
metadata, and graph saturation; verify polling or write blocking as specified
without retaining full Secret objects.

Implement shared types and parsing first, then offline CLI packaging, readiness
and status reduction, controller I/O and ownership, watches/graph, and admission
deployment. Release gates are tested conditional SSA updates, convergence across
schema/defaulting behavior, bounded sensitive-data handling, and an explicit
cross-namespace authorization decision. Automatic adoption, forced apply,
cluster-scoped resources, provider profiles, and direct target deletion protection
remain outside this version.
