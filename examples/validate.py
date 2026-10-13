"""Validate the example wrappers, references, and dependency graphs locally."""

import hashlib
import json
import re
from pathlib import Path

import jsonschema
import yaml


class UniqueKeyLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in result:
                raise ValueError(f"Duplicate YAML key: {key}")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "schema/resource.schema.json").read_text())
jsonschema.Draft202012Validator.check_schema(SCHEMA)
VALIDATOR = jsonschema.Draft202012Validator(
    SCHEMA, format_checker=jsonschema.FormatChecker()
)


def identity(resource, namespace):
    metadata = resource.get("metadata", resource)
    return (
        resource["apiVersion"], resource["kind"],
        metadata.get("namespace", namespace), metadata["name"],
    )


def validate_cloud(cloud):
    directory = ROOT / "examples" / cloud
    namespace = "kdc-example-" + cloud
    resources = []
    for filename in ("network.yaml", "vm.yaml", "database.yaml"):
        resources.extend(yaml.load_all(
            (directory / filename).read_text(), Loader=UniqueKeyLoader
        ))
    settings = yaml.load(
        (directory / "settings.yaml").read_text(), Loader=UniqueKeyLoader
    )
    targets = {identity(settings, namespace): None}
    graph = {}
    wrapper_names = set()
    for wrapper in resources:
        VALIDATOR.validate(wrapper)
        assert wrapper["metadata"]["namespace"] == namespace
        target = wrapper["spec"]["target"]
        target_identity = identity(target, namespace)
        assert target_identity not in targets, f"Duplicate target: {target_identity}"
        api_group = target["apiVersion"].split("/")[0] if "/" in target["apiVersion"] else ""
        canonical = json.dumps(
            [api_group, target["kind"], namespace, target["metadata"]["name"]],
            separators=(",", ":"),
        )
        expected_name = "kdcr-" + hashlib.sha256(canonical.encode()).hexdigest()[:32]
        assert wrapper["metadata"]["name"] == expected_name
        assert expected_name not in wrapper_names
        wrapper_names.add(expected_name)
        targets[target_identity] = wrapper
    for target_identity, wrapper in targets.items():
        if wrapper is None:
            continue
        spec = wrapper["spec"]
        references = spec.get("references", {})
        dependencies = spec["lifecycle"].get("dependencies", [])
        names = [dependency["name"] for dependency in dependencies]
        assert len(names) == len(set(names)), "Duplicate dependency names"
        for dependency in dependencies:
            assert dependency["reference"] in references, "Undeclared dependency alias"
        aliases = set(re.findall(r"\.refs\.([A-Za-z_][A-Za-z0-9_]*)", json.dumps(spec["target"])))
        assert aliases <= references.keys(), f"Undeclared template aliases: {aliases - references.keys()}"
        assert ".values" not in json.dumps(spec), "Unsubstituted CLI values"
        graph[target_identity] = []
        for reference in references.values():
            referenced_identity = identity(reference, namespace)
            assert referenced_identity in targets, f"Missing reference: {referenced_identity}"
            graph[target_identity].append(referenced_identity)
    visited, visiting = set(), set()

    def visit(node):
        assert node not in visiting, f"Reference cycle: {node}"
        if node in visited:
            return
        visiting.add(node)
        for child in graph.get(node, []):
            visit(child)
        visiting.remove(node)
        visited.add(node)

    for node in graph:
        visit(node)
    print(f"{cloud}: {len(resources)} valid wrappers; references and graph checked")


if __name__ == "__main__":
    for cloud in ("gcp", "aws", "azure"):
        validate_cloud(cloud)
