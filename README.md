# kdc

kdc is a Kubernetes Dependency Controller for coordinating infrastructure managed
through Kubernetes. Its proposed `Resource` API wraps a target manifest, resolves
fields from referenced objects, and waits for declared dependencies before
creating or updating the target. Cloud controllers such as Config Connector,
ACK, and Azure Service Operator handle the actual provisioning.

**Status:** design stage. This repository contains design documents, a Kubernetes
CRD, a JSON Schema, and cloud examples. The CLI, controller, Go module, container
image, and deployment manifests are not implemented yet. Installing the CRD lets
you store and validate wrappers; it does not provision their targets.

## Why kdc

I started kdc because I want to orchestrate infrastructure through Kubernetes,
with references and dependency management across objects and providers. A VM
needs a subnet, a database needs private networking, and connection settings need
the database endpoint. I want to express those relationships alongside the
resources themselves.

Terraform provides references and dependency management, but I don't want to
manage Terraform state files. I also want continuous reconciliation through
Kubernetes controllers. kdc is designed to bring those missing coordination
features into that workflow.

The design combines typed field references, explicit readiness checks, periodic
reconciliation, and deletion policies. A planned CLI packages YAML templates and
local values into wrappers for review and application through Kubernetes workflows.

## Installation

You can install the CRD today. Use a Kubernetes cluster running version 1.29 or
newer and a compatible `kubectl`, then run:

```sh
git clone https://github.com/RolphR/kdc.git
cd kdc
kubectl apply -f config/crd/bases/kdc.rolphr.dev_resources.yaml
kubectl wait --for=condition=Established --timeout=60s \
  crd/resources.kdc.rolphr.dev
```

The API is namespaced, with kind `Resource`, API version
`kdc.rolphr.dev/v1alpha1`, and short name `kdcr`. See
[schema documentation](docs/schema.md) for validation coverage and limitations.
There is currently no CLI or controller installation command.

## Examples

These short examples show the proposed API. Referenced objects are assumed to
exist in the wrapper's namespace; their provisioning is omitted.

### Read a field from another object

This wrapper copies an endpoint from an existing `upstream-settings` ConfigMap.
If the object or its `data.endpoint` field is missing, kdc waits before applying
the target.

```yaml
apiVersion: kdc.rolphr.dev/v1alpha1
kind: Resource
metadata:
  name: application-settings
  namespace: infrastructure
spec:
  references:
    settings:
      apiVersion: v1
      kind: ConfigMap
      name: upstream-settings
  lifecycle:
    targetReadiness:
      mode: None
  target:
    apiVersion: v1
    kind: ConfigMap
    metadata:
      name: application-settings
    data:
      endpoint: '{{ .refs.settings.data.endpoint }}'
```

`mode: None` disables target readiness checks for this ConfigMap, which has no
readiness condition. kdc can apply it, but wrapper readiness remains unknown.

### Use an AWS resource in a GCP firewall rule

This wrapper allows an AWS VM's private IP to reach tagged GCP VMs on TCP 5432.
It reads the address from an ACK Instance and waits until ACK reports the
instance synced and running before applying the Config Connector firewall.
An existing routed connection, such as an AWS-to-GCP VPN, and the GCP network
are prerequisites omitted here. This rule does not create that connection.

```yaml
apiVersion: kdc.rolphr.dev/v1alpha1
kind: Resource
metadata:
  name: allow-aws-postgres-client
  namespace: infrastructure
spec:
  references:
    awsVM:
      apiVersion: ec2.services.k8s.aws/v1alpha1
      kind: Instance
      name: aws-client
  lifecycle:
    dependencies:
      - name: aws_vm_running
        reference: awsVM
        readiness:
          checks:
            - condition:
                type: ACK.ResourceSynced
                status: 'True'
            - field:
                path: /status/state/name
                equals: running
  target:
    apiVersion: compute.cnrm.cloud.google.com/v1beta1
    kind: ComputeFirewall
    metadata:
      name: allow-aws-postgres-client
      annotations:
        cnrm.cloud.google.com/project-id: REPLACE_WITH_GCP_PROJECT_ID
    spec:
      networkRef:
        name: gcp-network
      direction: INGRESS
      sourceRanges:
        - '{{ .refs.awsVM.spec.privateIPAddress }}/32'
      targetTags:
        - postgres-server
      allow:
        - protocol: tcp
          ports: ['5432']
```

References supply template inputs; dependencies add readiness gates. Both
controllers must operate in `infrastructure`, and kdc must be permitted to read
the AWS Instance and manage the GCP firewall. Replace the project ID and resource
names, and assign `postgres-server` to the intended GCP VMs.

### Full cloud examples

Each cloud example describes a VM with a private address, private managed
PostgreSQL, and TCP 5432 access from the VM to the database.

| Directory | Required provider controllers | Configuration to replace |
| --- | --- | --- |
| [examples/gcp](examples/gcp/) | Config Connector | Project ID and unique Cloud SQL instance name |
| [examples/aws](examples/aws/) | ACK EC2 and RDS | Regional AMI ID |
| [examples/azure](examples/azure/) | Azure Service Operator | Unique PostgreSQL server name and SSH RSA public key |

Each directory contains an ordinary `settings.yaml` ConfigMap and three files of
already wrapped resources: `network.yaml`, `vm.yaml`, and `database.yaml`. These
fixtures do not require the planned `kdc wrap` command.

### Validate locally

From the repository root, install the Python validation dependencies in an
isolated environment and run the existing checks:

```sh
python3 -m venv /tmp/kdc-example-validation
/tmp/kdc-example-validation/bin/pip install 'jsonschema[format]' PyYAML
/tmp/kdc-example-validation/bin/python examples/validate.py
```

The script checks all three clouds for valid wrapper schemas, stable names,
declared reference aliases, resolvable example references, and reference cycles.
It does not require a cluster or cloud credentials and does not validate live
provider behavior.

### Run a cloud example

Provisioning requires a compatible kdc controller, the provider controllers and
CRD versions used by the manifests, and configured cloud credentials. This
repository does not yet supply that kdc controller. Once those prerequisites
are available:

1. Choose `gcp`, `aws`, or `azure` and edit its `settings.yaml`, replacing marked
   values. Check regional images, sizes, and database versions.
2. Configure kdc permissions and its allowed target and source kinds for the
   example namespace.
3. Run the following from the repository root, changing `cloud` as needed:

   ```sh
   cloud=gcp
   namespace="kdc-example-$cloud"
   kubectl create namespace "$namespace"
   kubectl -n "$namespace" create secret generic database-credentials \
     --from-file=password=/path/to/password-file
   kubectl apply -f "examples/$cloud/settings.yaml"
   kubectl apply --validate=strict \
     -f "examples/$cloud/network.yaml" \
     -f "examples/$cloud/vm.yaml" \
     -f "examples/$cloud/database.yaml"
   kubectl -n "$namespace" get kdcr
   ```

Use a password file without a trailing newline. Provider controllers read the
Secret directly. When the dependency chain has reconciled, inspect the connection
output with:

```sh
kubectl -n "$namespace" get configmap "kdc-$cloud-connection" -o yaml
```

The output contains connection settings, without a password. Test connectivity
from the VM using a PostgreSQL client and an existing private administration
path or provider console. The examples install no guest applications and provide
no public IP or inbound SSH rule.

All example wrappers use `deletionPolicy: abandon`: deleting wrappers retains
their targets and cloud infrastructure. Plan cleanup explicitly. See
[the example guide](docs/examples.md) for provider prerequisites, networking,
readiness checks, and cleanup details.

## Local development with Minikube

### Start a cluster and check the CRD

Install [Minikube](https://minikube.sigs.k8s.io/docs/start/), `kubectl`, and a
supported container or VM driver. The following uses Docker and a separate
`kdc-dev` profile. Run from the repository root:

```sh
minikube start -p kdc-dev --driver=docker
kubectl --context=kdc-dev version
kubectl --context=kdc-dev apply \
  -f config/crd/bases/kdc.rolphr.dev_resources.yaml
kubectl --context=kdc-dev wait --for=condition=Established --timeout=60s \
  crd/resources.kdc.rolphr.dev
kubectl --context=kdc-dev create namespace kdc-example-gcp
kubectl --context=kdc-dev apply --dry-run=server --validate=strict \
  -f examples/gcp/network.yaml \
  -f examples/gcp/vm.yaml \
  -f examples/gcp/database.yaml
```

Confirm the cluster is Kubernetes 1.29 or newer. Minikube's
[`start` options](https://minikube.sigs.k8s.io/docs/commands/start/) include
`--kubernetes-version` if you need to select a version.

Server-side dry-run checks the wrappers against the installed CRD without storing
them. It needs no cloud controllers because it submits wrappers, rather than
their embedded targets. This verifies CRD admission, not runtime rendering,
provider schemas, or provisioning.

Stop the development cluster when finished:

```sh
minikube stop -p kdc-dev
```

### Build and test the planned Go implementation

The [low-level design](docs/design-docs/low-level-design.md) proposes two binaries:
`kdc` and `kdc-controller`. After `go.mod` and their packages exist, the intended
build and test commands are:

```sh
mkdir -p bin
go build -o bin/kdc ./cmd/kdc
go build -o bin/kdc-controller ./cmd/kdc-controller
go test ./...
go test -race ./...
```

These commands cannot run against the current repository. Today, use the Python
validator above. The design also calls for API-server integration tests through
envtest and provider acceptance tests; their tooling is not available yet.

### Run the planned controller locally

A runnable Minikube workflow still requires the controller implementation, RBAC,
deployment policy, and admission webhook setup. The design specifies installing
the CRD and RBAC first, then certificates, a Service, and ready webhook-serving
replicas, followed by the validating webhook configuration and user wrappers.
Controller flags and deployment commands have not been defined, so there is no
working local controller start command yet.

Once implemented, the CLI's proposed authoring workflow is:

```sh
./bin/kdc validate ./infrastructure --namespace infrastructure --values values.yaml
./bin/kdc wrap ./infrastructure --namespace infrastructure \
  --values values.yaml --output wrappers.yaml
./bin/kdc validate --wrappers wrappers.yaml
kubectl --context=kdc-dev apply -f wrappers.yaml
```

Here `infrastructure/` and `values.yaml` are your own inputs, and the
`infrastructure` namespace must already exist. For cloud examples, Minikube hosts
the controllers; the configured provider controllers provision resources in your
cloud account.

## Documentation

- [High-level design](docs/design-docs/high-level-design.md): purpose and architecture.
- [API design](docs/design-docs/api-design.md): references, readiness, templates, and lifecycle.
- [Low-level design](docs/design-docs/low-level-design.md): proposed implementation and verification.
- [Schemas](docs/schema.md): CRD and standalone validation.
- [Cloud examples](docs/examples.md): provider setup and networking details.
