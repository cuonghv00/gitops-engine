#!/usr/bin/env python3
"""
generator.py
============
The Python "Engine" for the Helm Monorepo Automation System.

Reads {project}-{env}.yaml (Single Source of Truth) from projects/{team}/{project}/ and for each app:
  1. Creates/updates the directory at projects/{team}/{project}/{project}-{env}/<app-name>/
  2. Generates Chart.yaml with a file:// dependency on common-lib
  3. Generates values.yaml mapping app config to common-lib value keys

Design principle: IDEMPOTENT — safe to run multiple times without side effects.
"""

import argparse
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Literal, Optional, Union

import yaml
from pydantic import BaseModel, Field, ConfigDict, model_validator

# ---------------------------------------------------------------------------
# Constants & Defaults
# ---------------------------------------------------------------------------
DEFAULT_REGISTRY = "registry.vn/platform"
DEFAULT_PULL_SECRET = "regcred"

REPO_ROOT = Path(__file__).parent.parent
COMMON_LIB_PATH = REPO_ROOT / "helm-templates" / "common-lib"

# Default Nginx Ingress annotations — merged with user-provided annotations.
# User values override these defaults.
_NGINX_DEFAULT_ANNOTATIONS: dict[str, str] = {
    "nginx.ingress.kubernetes.io/ssl-redirect": "false",
    "nginx.ingress.kubernetes.io/proxy-body-size": "8m",
}

# Default Pod-level security context applied to all workloads.
# These fields are valid ONLY at pod spec level (NOT container level):
#   - runAsNonRoot, runAsUser, runAsGroup: identity controls shared across containers
#   - fsGroup: sets GID ownership for mounted volumes (PVCs, secrets, etc.)
# Users can override any field via AppConfig.podSecurityContext.
_DEFAULT_POD_SEC_CTX: dict = {
    "runAsNonRoot": True,
    "runAsUser": 1000,
    "runAsGroup": 1000,
    "fsGroup": 1000,
}

# Default Container-level security context applied to main containers.
# Separate from pod-level context — these fields are valid ONLY at container level.
# Extracted as constant to avoid duplicate definitions (DRY).
_DEFAULT_CONTAINER_SEC_CTX: dict = {
    "readOnlyRootFilesystem": True,
    "allowPrivilegeEscalation": False,
    "runAsNonRoot": True,
    "runAsUser": 1000,
    "runAsGroup": 1000,
    "capabilities": {"drop": ["ALL"]},
}

# ---------------------------------------------------------------------------
# Validation Helpers
# ---------------------------------------------------------------------------

# K8s names: lowercase alphanumeric and dashes, must start/end with alphanumeric
_K8S_NAME_RE = re.compile(r'^[a-z0-9]([a-z0-9\-]*[a-z0-9])?$')


def _validate_k8s_name(name: str, label: str = "name") -> str:
    """Validate a name conforms to Kubernetes DNS naming rules.

    Rules: lowercase, alphanumeric and dashes, must start and end with alphanumeric.
    Max 63 chars (Helm truncates anyway, but good to catch early).
    """
    if not name:
        raise ValueError(f"{label} cannot be empty")
    if len(name) > 63:
        raise ValueError(f"{label} '{name}' exceeds 63 characters (len={len(name)})")
    if not _K8S_NAME_RE.match(name):
        raise ValueError(
            f"{label} '{name}' is not a valid Kubernetes name. "
            "Must be lowercase alphanumeric with dashes, e.g. 'my-app'."
        )
    return name


def _get_common_lib_rel_path(from_dir: Path) -> str:
    """Compute the relative path from a chart directory to the common-lib chart."""
    return os.path.relpath(COMMON_LIB_PATH, from_dir)


def _resolve_image(
    image: Optional[str],
    image_repo: str,
    name: str,
    image_tag_override: Optional[str],
    item_image_tag: Optional[str],
    project_image_tag: str,
) -> tuple[str, str]:
    """Resolve (image_name, tag) from a flexible image configuration.

    Priority chain: image_tag_override > embedded URI tag > item_image_tag > project_image_tag.

    Args:
        image:             Full image URI (may include embedded tag, e.g. 'redis:7.2-alpine')
        image_repo:        Resolved registry/repo prefix (e.g. 'registry.vn/platform')
        name:              Resource name — used for default URI when 'image' is unset
        image_tag_override: CLI --image-tag override (highest priority)
        item_image_tag:    App/container-level image_tag config
        project_image_tag: Project-level fallback image_tag

    Returns:
        (image_name, tag) tuple — e.g. ('registry.vn/platform/my-app', 'v1.2.3')
    """
    if image:
        last_segment = image.split("/")[-1]
        if ":" in last_segment:
            # URI contains embedded tag — split on last ':' in the final segment
            # e.g. 'gcr.io/project/app:v1.2' → ('gcr.io/project/app', 'v1.2')
            split_pos = image.rfind(":")
            image_name = image[:split_pos]
            tag = image_tag_override or image[split_pos + 1:]
        else:
            image_name = image
            tag = image_tag_override or item_image_tag or project_image_tag
    else:
        image_name = f"{image_repo}/{name}"
        tag = image_tag_override or item_image_tag or project_image_tag
    return image_name, tag


# ---------------------------------------------------------------------------
# Pydantic Models for Configuration Validation
# ---------------------------------------------------------------------------

class IngressPath(BaseModel):
    model_config = ConfigDict(extra='forbid')
    path: str = "/"
    pathType: str = "ImplementationSpecific"
    servicePort: Optional[Union[int, str]] = None


class IngressHost(BaseModel):
    model_config = ConfigDict(extra='forbid')
    host: str
    paths: list[IngressPath] = Field(default_factory=lambda: [IngressPath()])


class IngressConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = False
    host: Optional[str] = None
    path: Optional[str] = None  # Shortcut for single-path config
    hosts: Optional[list[IngressHost]] = None
    servicePort: Optional[Union[int, str]] = None
    annotations: dict[str, str] = Field(default_factory=dict)
    className: str = "nginx"
    tls: Optional[list[dict]] = None


class ProbeConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    path: Optional[str] = None
    port: Optional[Union[int, str]] = None
    grpc: Optional[dict] = None
    initialDelaySeconds: int = 10
    periodSeconds: int = 5
    timeoutSeconds: int = 2
    failureThreshold: int = 3


class HealthConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = False
    path: Optional[str] = None
    port: Optional[Union[int, str]] = None
    initialDelaySeconds: int = 10
    periodSeconds: int = 5
    timeoutSeconds: int = 2
    failureThreshold: int = 3
    liveness: Optional[Union[str, ProbeConfig]] = None
    readiness: Optional[Union[str, ProbeConfig]] = None
    startup: Optional[Union[str, ProbeConfig]] = None


class PVCConfig(BaseModel):
    """[DEPRECATED] Use project-level pvcs: and app-level volumes: instead."""
    model_config = ConfigDict(extra='forbid')
    enabled: bool = False
    size: str = "10Gi"
    storageClass: Optional[str] = None
    accessModes: list[str] = Field(default_factory=lambda: ["ReadWriteOnce"])
    mountPath: Optional[str] = None


class ProjectPVC(BaseModel):
    """Project-level PVC — lifecycle managed independently in project-shared chart."""
    model_config = ConfigDict(extra='forbid')
    name: str
    size: str = "10Gi"
    storageClass: Optional[str] = None
    accessModes: list[str] = Field(default_factory=lambda: ["ReadWriteOnce"])

    @model_validator(mode='after')
    def validate_name(self) -> 'ProjectPVC':
        _validate_k8s_name(self.name, "ProjectPVC.name")
        return self


class ServicePort(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = "http"
    port: int
    targetPort: Optional[int] = None
    protocol: str = "TCP"
    nodePort: Optional[int] = None


class ServiceAccountConfig(BaseModel):
    model_config = ConfigDict(extra='forbid', populate_by_name=True)
    create: bool = True
    # 'automountToken' is the user-facing field name in apps.yaml.
    # serialization_alias ensures model_dump(by_alias=True) emits 'automountServiceAccountToken'
    # which is the Kubernetes API field name used in both Pod spec and ServiceAccount resource.
    automountToken: bool = Field(False, serialization_alias="automountServiceAccountToken")  # Hardened default (RBAC Security)
    name: Optional[str] = None   # If set, overrides the auto-generated fullname


class ServiceConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = True
    type: Literal["ClusterIP", "NodePort", "LoadBalancer", "ExternalName"] = "ClusterIP"
    port: Optional[int] = None       # Shortcut: single-port service
    targetPort: Optional[int] = None # Shortcut: maps to container targetPort
    ports: list[ServicePort] = Field(default_factory=list)
    annotations: dict[str, str] = Field(default_factory=dict)


class EnvItem(BaseModel):
    """
    Flexible env configuration. Exactly one primary source key must be set.

    Supported patterns:
      Plain value:    name: KEY, value: VALUE
      valueFrom:      name: KEY, valueFrom: {fieldRef: ...}   # Downward API, resourceFieldRef
      Per-key secret: secretEnv: secret-name, vars: [KEY1, KEY2]
      envFrom CM:     configMap: configmap-name
      envFrom Secret: secret: secret-name
    """
    model_config = ConfigDict(extra='forbid')

    # Plain env var — name is the primary discriminator
    name: Optional[str] = None
    value: Optional[str] = None
    valueFrom: Optional[dict] = None  # Native K8s valueFrom (Downward API, resourceFieldRef, etc.)

    # Per-key secretKeyRef injection
    secretEnv: Optional[str] = None
    vars: Optional[list[str]] = None

    # envFrom sources
    configMap: Optional[str] = None
    secret: Optional[str] = None

    @model_validator(mode='after')
    def validate_source(self) -> 'EnvItem':
        # Count mutually-exclusive primary sources
        sources = sum([
            self.name is not None,
            self.secretEnv is not None,
            self.configMap is not None,
            self.secret is not None,
        ])
        if sources == 0:
            raise ValueError(
                "EnvItem must define exactly one source: "
                "name(/value|/valueFrom), secretEnv(+vars), configMap, or secret"
            )
        if sources > 1:
            raise ValueError(
                f"EnvItem has multiple sources defined — only one is allowed. "
                f"Found: name={self.name}, secretEnv={self.secretEnv}, "
                f"configMap={self.configMap}, secret={self.secret}"
            )
        # value and valueFrom are mutually exclusive on a name-based entry
        if self.value is not None and self.valueFrom is not None:
            raise ValueError(
                f"EnvItem '{self.name}': 'value' and 'valueFrom' cannot both be set. "
                "Use one or the other."
            )
        return self


class VolumeItem(BaseModel):
    """
    Unified volume config combining Volume source and VolumeMount in one entry.

    Mount options (readOnly, mountPropagation, recursiveReadOnly) are applied to the VolumeMount.

    Supported sources (exactly one must be set):
      Lớp 1 — Shorthand:  pvc, emptyDir, hostPath, configMap, secret
      Lớp 2 — Native:     nfs, csi, projected

    For other volume types (NFS raw, Gluster, etc.) use k8s.pod.volumes escape hatch.

    NOTE: emptyDir: {} is a valid empty dict (falsy in Python!) — always use
    'is not None' checks, never 'if item.emptyDir'.
    """
    model_config = ConfigDict(extra='forbid')

    # VolumeMount fields — REQUIRED for all volume types
    name: str
    mountPath: str
    readOnly: Optional[bool] = None
    mountPropagation: Optional[str] = None
    recursiveReadOnly: Optional[str] = None

    # Lớp 1 — Shorthand volume sources (exactly one must be set)
    pvc: Optional[str] = None                       # claimName
    emptyDir: Optional[dict] = None                 # {} or {medium: Memory}  ← may be falsy!
    hostPath: Optional[Union[str, dict]] = None     # "/path" or {path:, type:}
    configMap: Optional[Union[str, dict]] = None    # "cm-name" or {name:, items:}
    secret: Optional[Union[str, dict]] = None       # "sec-name" or {secretName:, items:}

    # Lớp 2 — Native K8s volume types
    nfs: Optional[dict] = None                      # {server: host, path: /exports}
    csi: Optional[dict] = None                      # {driver: ..., volumeHandle: ..., ...}
    projected: Optional[dict] = None                # {sources: [{secret: ...}, {configMap: ...}]}

    @model_validator(mode='after')
    def validate_source(self) -> 'VolumeItem':
        # NOTE: emptyDir may be {} (falsy), always use 'is not None'
        sources = sum([
            self.pvc is not None,
            self.emptyDir is not None,   # {} is valid — do NOT use `bool(self.emptyDir)`
            self.hostPath is not None,
            self.configMap is not None,
            self.secret is not None,
            self.nfs is not None,
            self.csi is not None,
            self.projected is not None,
        ])
        if sources == 0:
            raise ValueError(
                f"VolumeItem '{self.name}' must define exactly one source: "
                "pvc, emptyDir, hostPath, configMap, secret, nfs, csi, or projected"
            )
        if sources > 1:
            raise ValueError(
                f"VolumeItem '{self.name}' has multiple sources — only one is allowed"
            )
        return self


class ExtraContainerConfig(BaseModel):
    """Configuration for initContainers or sidecars."""
    model_config = ConfigDict(extra='forbid')

    name: str
    image: Optional[str] = None
    image_repo: Optional[str] = None
    image_tag: Optional[str] = None
    pullPolicy: Optional[str] = None

    command: Optional[Union[str, list[str]]] = None
    args: Optional[Union[str, list[str]]] = None

    envs: list[EnvItem] = Field(default_factory=list)
    volumes: list[VolumeItem] = Field(default_factory=list)

    resources: dict = Field(default_factory=dict)
    securityContext: dict = Field(default_factory=dict)
    health: HealthConfig = Field(default_factory=HealthConfig)

    # Primarily for native sidecars in initContainers (K8s 1.29+)
    # Set to 'Always' to make an initContainer a sidecar.
    restartPolicy: Optional[str] = None

    @model_validator(mode='after')
    def validate_container(self) -> 'ExtraContainerConfig':
        _validate_k8s_name(self.name, "Container name")
        if isinstance(self.command, str):
            self.command = shlex.split(self.command)
        if isinstance(self.args, str):
            self.args = shlex.split(self.args)
        return self


class JobConfig(BaseModel):
    """
    Kubernetes Job-specific configuration.
    Applied when AppConfig.type == 'job' or 'cronjob'.

    All fields are optional — K8s defaults apply when omitted.
    """
    model_config = ConfigDict(extra='allow')

    completions: Optional[int] = None             # Total successful pod completions required
    parallelism: Optional[int] = None             # Max pods running concurrently
    backoffLimit: Optional[int] = None            # Retry limit before marking the Job failed (K8s default: 6)
    activeDeadlineSeconds: Optional[int] = None   # Max job run time; Job is terminated if exceeded
    ttlSecondsAfterFinished: Optional[int] = None # Cleanup delay (TTL) after Job completion
    restartPolicy: str = "Never"                  # Pod restart policy: Never (default) or OnFailure


class CronJobConfig(BaseModel):
    """
    Kubernetes CronJob-specific configuration.
    Applied when AppConfig.type == 'cronjob'.

    schedule is REQUIRED when type == 'cronjob'.
    """
    model_config = ConfigDict(extra='allow')

    schedule: str                                         # Cron expression, e.g. "0 2 * * *"
    concurrencyPolicy: str = "Allow"                      # Allow | Forbid | Replace
    suspend: bool = False                                 # Pause scheduling
    successfulJobsHistoryLimit: int = 3                   # Retain N completed Job records
    failedJobsHistoryLimit: int = 1                       # Retain N failed Job records
    startingDeadlineSeconds: Optional[int] = None         # Deadline for starting a missed job


class HPAConfig(BaseModel):
    """
    Kubernetes HorizontalPodAutoscaler configuration.
    Applied when AppConfig.type == 'deployment' and hpa.enabled == True.

    Uses autoscaling/v2 API (stable from K8s 1.23+).

    The generator will automatically OMIT the 'replicas' field from the Deployment
    spec when HPA is enabled, preventing a control loop conflict where the Deployment
    controller and the HPA controller fight over the replica count.
    """
    model_config = ConfigDict(extra='forbid')

    enabled: bool = False
    minReplicas: int = 1
    maxReplicas: int = 5
    # CPU utilization target as a percentage of the container's request.
    targetCPUUtilizationPercentage: Optional[int] = None
    # Memory utilization target as a percentage of the container's request.
    targetMemoryUtilizationPercentage: Optional[int] = None
    behavior: dict = Field(default_factory=dict)


class K8sOverrides(BaseModel):
    """Native Kubernetes overrides applied as deep-merge after generation.

    Supports 3-layer config model:
      pod:           Pod spec level (hostAliases, dnsPolicy, dnsConfig, ...)
      deployment:    Deployment spec level (strategy, minReadySeconds, ...)
      mainContainer: Main container spec level (lifecycle, stdin, ...)
      service:       Service spec level
      ingress:       Ingress spec level
      job:           Job spec level (podFailurePolicy, ...)
      cronjob:       CronJob spec level (timeZone, ...)
    """
    model_config = ConfigDict(extra='forbid')
    pod: dict = Field(default_factory=dict)
    deployment: dict = Field(default_factory=dict)
    mainContainer: dict = Field(default_factory=dict)
    service: dict = Field(default_factory=dict)
    ingress: dict = Field(default_factory=dict)
    job: dict = Field(default_factory=dict)       # Merge into Job.spec (not jobTemplate.spec)
    cronjob: dict = Field(default_factory=dict)  # Merge into CronJob.spec


class AppConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')

    name: str
    type: Literal["deployment", "job", "cronjob"] = "deployment"

    port: Optional[int] = None
    ports: list[ServicePort] = Field(default_factory=list)

    replicas: int = 1
    image: Optional[str] = None
    image_repo: Optional[str] = None
    image_tag: Optional[str] = None
    pullPolicy: str = "IfNotPresent"
    imagePullSecrets: Optional[list[dict]] = None

    resources: dict = Field(default_factory=dict)
    securityContext: dict = Field(default_factory=dict)
    # Pod-level security context — valid pod spec fields only: fsGroup, supplementalGroups, etc.
    # Do NOT put container-level fields (readOnlyRootFilesystem, capabilities) here.
    podSecurityContext: dict = Field(default_factory=dict)
    auto_mount_tmp: bool = True

    # --- New flattened env/volume declarations ---
    envs: list[EnvItem] = Field(default_factory=list)
    volumes: list[VolumeItem] = Field(default_factory=list)

    # --- Legacy fields (deprecated but retained for backward compatibility) ---
    env: list[dict] = Field(default_factory=list)
    env_vars: list[str] = Field(default_factory=list)
    envFrom: list[dict] = Field(default_factory=list)
    mount_env_file: bool = False  # DEPRECATED: use volumes with configMap source
    pvc: PVCConfig = Field(default_factory=PVCConfig)  # DEPRECATED: use project pvcs + volumes

    service: Optional[Union[bool, ServiceConfig]] = None
    ingress: IngressConfig = Field(default_factory=IngressConfig)
    health: HealthConfig = Field(default_factory=HealthConfig)
    serviceAccount: ServiceAccountConfig = Field(default_factory=ServiceAccountConfig)

    strategy: Union[str, dict] = "RollingUpdate"
    affinity: dict = Field(default_factory=dict)
    tolerations: list = Field(default_factory=list)
    nodeSelector: dict = Field(default_factory=dict)        # Node label selectors
    podLabels: dict[str, str] = Field(default_factory=dict) # Extra labels for Pod metadata
    serviceAccountName: Optional[str] = None  # DEPRECATED: prefer serviceAccount.name
    genConfigMaps: bool = False
    podAnnotations: dict[str, str] = Field(default_factory=dict)

    # Init containers and sidecars
    initContainers: list[ExtraContainerConfig] = Field(default_factory=list)
    sidecars: list[ExtraContainerConfig] = Field(default_factory=list)

    # Entrypoint overrides for main container
    command: Optional[Union[str, list[str]]] = None
    args: Optional[Union[str, list[str]]] = None

    # Batch workload configs
    job: Optional[JobConfig] = None
    cronjob: Optional[CronJobConfig] = None

    # Autoscaling config (deployment only)
    hpa: HPAConfig = Field(default_factory=HPAConfig)

    # Native K8s Escape Hatch
    k8s: K8sOverrides = Field(default_factory=K8sOverrides)

    @model_validator(mode='after')
    def validate_app(self) -> 'AppConfig':
        # Validate K8s-compatible name
        _validate_k8s_name(self.name, "App name")

        if isinstance(self.command, str):
            self.command = shlex.split(self.command)
        if isinstance(self.args, str):
            self.args = shlex.split(self.args)

        # Validate port uniqueness across multi-port definitions
        names = [p.name for p in self.ports]
        if len(names) != len(set(names)):
            raise ValueError(f"Duplicate port names in app '{self.name}': {names}")
        port_nums = [p.port for p in self.ports]
        if len(port_nums) != len(set(port_nums)):
            raise ValueError(f"Duplicate port numbers in app '{self.name}': {port_nums}")

        # Validate ingress + service compatibility upfront
        if self.ingress.enabled and self.service is False:
            raise ValueError(
                f"App '{self.name}': ingress.enabled=true requires a Service backend. "
                "Remove 'service: false' or disable the ingress."
            )

        # Batch type validations
        if self.type == "cronjob":
            if self.cronjob is None:
                raise ValueError(
                    f"App '{self.name}': type='cronjob' requires a 'cronjob' configuration block "
                    "with at least a 'schedule' field."
                )
            if self.ingress.enabled:
                raise ValueError(
                    f"App '{self.name}': type='cronjob' cannot have ingress enabled. "
                    "CronJobs are batch workloads with no inbound traffic."
                )
        if self.type == "job":
            if self.ingress.enabled:
                raise ValueError(
                    f"App '{self.name}': type='job' cannot have ingress enabled. "
                    "Jobs are batch workloads with no inbound traffic."
                )

        # HPA validation: only valid for Deployment type
        if self.hpa.enabled and self.type != "deployment":
            raise ValueError(
                f"App '{self.name}': hpa.enabled=true is only supported for type='deployment'. "
                f"Current type is '{self.type}'. Jobs and CronJobs scale differently."
            )
        # HPA requires at least one metric target
        if self.hpa.enabled and (
            self.hpa.targetCPUUtilizationPercentage is None
            and self.hpa.targetMemoryUtilizationPercentage is None
        ):
            raise ValueError(
                f"App '{self.name}': hpa.enabled=true requires at least one metric target. "
                "Set 'targetCPUUtilizationPercentage' or 'targetMemoryUtilizationPercentage'."
            )

        return self


class ProjectDefinition(BaseModel):
    model_config = ConfigDict(extra='forbid')
    project: str
    common_version: str = "1.0.0"
    namespace: Optional[str] = None
    image_repo: Optional[str] = None
    image_tag: str = "latest"
    imagePullSecrets: Optional[list[dict]] = None
    pvcs: list[ProjectPVC] = Field(default_factory=list)
    apps: list[AppConfig] = Field(default_factory=list)

    @model_validator(mode='after')
    def validate_project(self) -> 'ProjectDefinition':
        _validate_k8s_name(self.project, "Project name")
        # Validate common_version follows semver (required by Helm dependency resolution).
        # Helm uses this to match the chart version in common-lib/Chart.yaml.
        if not re.match(r'^\d+\.\d+\.\d+', self.common_version):
            raise ValueError(
                f"common_version '{self.common_version}' must follow semver format "
                "(e.g. '1.0.0', '2.3.1'). Helm requires this for dependency resolution."
            )
        return self


# ---------------------------------------------------------------------------
# File & Dict Utilities
# ---------------------------------------------------------------------------

def deep_update(d: dict, u: dict) -> dict:
    """Recursively update a dictionary, mimicking K8s strategic merge.

    - dict values: merged recursively.
    - list values: extended with deduplication by 'key' or 'name' field.
    - scalar values: overwritten.
    """
    for k, v in u.items():
        if isinstance(v, dict) and k in d and isinstance(d[k], dict):
            deep_update(d[k], v)
        elif isinstance(v, list) and k in d and isinstance(d[k], list):
            # Dedup by 'key' or 'name' — avoids duplicate tolerations, hostAliases, etc.
            existing_ids = {
                e.get("key") or e.get("name")
                for e in d[k]
                if isinstance(e, dict) and (e.get("key") or e.get("name"))
            }
            for item in v:
                if not isinstance(item, dict):
                    d[k].append(item)
                else:
                    item_id = item.get("key") or item.get("name")
                    if item_id not in existing_ids:
                        d[k].append(item)
                        if item_id:
                            existing_ids.add(item_id)
        else:
            d[k] = v
    return d


def parse_env_file(env_path: Path) -> tuple[dict, list]:
    """Parse an .env file into (config_data, secret_keys).

    config_data: key=value pairs with literal values → becomes ConfigMap data.
    secret_keys: keys with ${...} placeholder values → expected in K8s Secret.
    """
    config_data: dict = {}
    secret_keys: list = []
    if not env_path.exists():
        return config_data, secret_keys

    pattern = re.compile(r"^\s*([\w.-]+)\s*=\s*(.*?)\s*$")
    with env_path.open() as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = pattern.match(line)
            if match:
                key, value = match.groups()
                # Strip matching outer quotes only (prevent stripping inner quotes)
                if len(value) >= 2 and (
                    (value[0] == '"' and value[-1] == '"')
                    or (value[0] == "'" and value[-1] == "'")
                ):
                    value = value[1:-1]
                if value.startswith("${") and value.endswith("}"):
                    secret_keys.append(key)
                else:
                    config_data[key] = value
    return config_data, secret_keys


def write_yaml(path: Path, data: dict, dry_run: bool = False) -> None:
    content = yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)
    if dry_run:
        print(f"  [DRY-RUN] Would write: {path}")
        print("  " + content.replace("\n", "\n  "))
    else:
        path.write_text(content)
        print(f"  [WRITE]   {path}")


def ensure_dir(path: Path, dry_run: bool = False) -> None:
    if not path.exists():
        if not dry_run:
            path.mkdir(parents=True, exist_ok=True)
        print(f"  [MKDIR]   {path}")


# ---------------------------------------------------------------------------
# Probe Builder (module-level to avoid closure capture issues)
# ---------------------------------------------------------------------------

def _build_probe(
    cfg_union: Union[str, ProbeConfig, None],
    health_cfg: HealthConfig,
    default_port: Union[int, str],
) -> dict:
    """Build a K8s probe dict from a flexible probe configuration.

    Args:
        cfg_union:    None (use health defaults), str (HTTP path), or ProbeConfig.
        health_cfg:   The parent HealthConfig for fallback path/port/timing defaults.
        default_port: The container's primary port, used when no port is explicitly set.
    """
    if cfg_union is None:
        # Use parent HealthConfig as the probe spec
        path = health_cfg.path
        port = health_cfg.port or default_port
        init_delay = health_cfg.initialDelaySeconds
        period = health_cfg.periodSeconds
        timeout = health_cfg.timeoutSeconds
        failure = health_cfg.failureThreshold

    elif isinstance(cfg_union, str):
        # Short-form: just an HTTP path string
        path = cfg_union
        port = health_cfg.port or default_port
        init_delay = health_cfg.initialDelaySeconds
        period = health_cfg.periodSeconds
        timeout = health_cfg.timeoutSeconds
        failure = health_cfg.failureThreshold

    else:
        # Full ProbeConfig — use its values, falling back to health_cfg for missing fields
        path = cfg_union.path or health_cfg.path
        port = cfg_union.port or health_cfg.port or default_port
        init_delay = cfg_union.initialDelaySeconds
        period = cfg_union.periodSeconds
        timeout = cfg_union.timeoutSeconds
        failure = cfg_union.failureThreshold

    res: dict = {
        "initialDelaySeconds": init_delay,
        "periodSeconds": period,
        "timeoutSeconds": timeout,
        "failureThreshold": failure,
    }
    if getattr(cfg_union, 'grpc', None) is not None:
        res["grpc"] = getattr(cfg_union, 'grpc')
    elif path:
        res["httpGet"] = {"path": path, "port": port}
    else:
        res["tcpSocket"] = {"port": port}
    return res


# ---------------------------------------------------------------------------
# Env & Volume Builders
# ---------------------------------------------------------------------------

def build_env_items(envs: list[EnvItem]) -> tuple[list, list]:
    """Parse a list of EnvItem into (env_list, env_from_list) for K8s.

    Returns:
        env_list:      List of K8s EnvVar dicts → container.env
        env_from_list: List of K8s EnvFromSource dicts → container.envFrom
    """
    env_list: list = []
    env_from_list: list = []

    for item in envs:
        if item.secretEnv is not None:
            if item.vars:
                # Per-key secretKeyRef — inject each var individually
                for key in item.vars:
                    env_list.append({
                        "name": key,
                        "valueFrom": {
                            "secretKeyRef": {
                                "name": item.secretEnv,
                                "key": key,
                            }
                        },
                    })
            else:
                # No vars provided - inject the whole secret via envFrom
                env_from_list.append({"secretRef": {"name": item.secretEnv}})

        elif item.configMap is not None:
            # envFrom configMapRef — inject all keys from a ConfigMap
            env_from_list.append({"configMapRef": {"name": item.configMap}})

        elif item.secret is not None:
            # envFrom secretRef — inject all keys from a Secret
            env_from_list.append({"secretRef": {"name": item.secret}})

        elif item.name is not None:
            entry: dict = {"name": item.name}
            if item.valueFrom is not None:
                entry["valueFrom"] = item.valueFrom   # Downward API, resourceFieldRef, etc.
            elif item.value is not None:
                entry["value"] = item.value
            env_list.append(entry)

    return env_list, env_from_list


def build_volume_items(volumes: list[VolumeItem]) -> tuple[list, list]:
    """Parse a list of VolumeItem into (volume_specs, volume_mount_specs) for K8s.

    Returns:
        volume_specs:       List of K8s Volume dicts → pod.spec.volumes
        volume_mount_specs: List of K8s VolumeMount dicts → container.volumeMounts

    IMPORTANT: emptyDir may be {} (falsy in Python). Always use 'is not None' for checks.
    """
    volume_specs: list = []
    mount_specs: list = []

    for item in volumes:
        # --- Build VolumeMount ---
        vm: dict = {"name": item.name, "mountPath": item.mountPath}
        if item.readOnly is not None:
            vm["readOnly"] = item.readOnly
        if item.mountPropagation is not None:
            vm["mountPropagation"] = item.mountPropagation
        if item.recursiveReadOnly is not None:
            vm["recursiveReadOnly"] = item.recursiveReadOnly

        # --- Build Volume source ---
        vol: dict = {"name": item.name}

        if item.pvc is not None:
            vol["persistentVolumeClaim"] = {"claimName": item.pvc}

        elif item.emptyDir is not None:  # NOTE: {} is valid, use 'is not None'
            vol["emptyDir"] = item.emptyDir

        elif item.hostPath is not None:
            vol["hostPath"] = (
                {"path": item.hostPath}
                if isinstance(item.hostPath, str)
                else dict(item.hostPath)
            )

        elif item.configMap is not None:
            vol["configMap"] = (
                {"name": item.configMap}
                if isinstance(item.configMap, str)
                else dict(item.configMap)
            )

        elif item.secret is not None:
            vol["secret"] = (
                {"secretName": item.secret}
                if isinstance(item.secret, str)
                else dict(item.secret)
            )

        elif item.nfs is not None:
            vol["nfs"] = dict(item.nfs)

        elif item.csi is not None:
            vol["csi"] = dict(item.csi)

        elif item.projected is not None:
            vol["projected"] = dict(item.projected)

        volume_specs.append(vol)
        mount_specs.append(vm)

    return volume_specs, mount_specs


# ---------------------------------------------------------------------------
# Logic Builders
# ---------------------------------------------------------------------------

def _build_container_dict(
    name: str,
    container_cfg: Union[AppConfig, ExtraContainerConfig],
    project: ProjectDefinition,
    project_vars: tuple[dict, list],
    image_tag_override: Optional[str] = None,
    is_main: bool = False,
    allow_probes: bool = True,
    primary_port: Optional[int] = None,
) -> dict:
    """Build a K8s container dictionary.

    Handles image resolution, envs, volume mounts, probes, command/args, etc.

    Args:
        allow_probes: If False, probes are never rendered even if health.enabled=True.
                      Must be False for classic Init Containers (K8s will reject otherwise).
                      True for main container, native sidecars (restartPolicy=Always), and
                      traditional sidecars.
    """
    config_pool, secret_pool = project_vars
    project_name = project.project

    # 1. Image logic
    image_repo = container_cfg.image_repo or project.image_repo or DEFAULT_REGISTRY
    image_name, tag = _resolve_image(
        image=container_cfg.image,
        image_repo=image_repo,
        name=name,
        image_tag_override=image_tag_override,
        item_image_tag=container_cfg.image_tag,
        project_image_tag=project.image_tag,
    )

    # 2. Security Context
    # For the main container: apply the full hardened default policy, then merge user overrides.
    # For extra containers (init/sidecar): use a safe default (runAsNonRoot) unless fully overridden.
    # The user can still bypass this by explicitly providing {"runAsNonRoot": False}.
    if is_main:
        sec_ctx = {**_DEFAULT_CONTAINER_SEC_CTX, **container_cfg.securityContext}
    else:
        # Extra containers: use default runAsNonRoot: true if not explicitly set
        default_extra_sec_ctx: dict = {
            "runAsNonRoot": True
        }
        sec_ctx = {**default_extra_sec_ctx, **container_cfg.securityContext}

    # 3. Volumes & Mounts
    volume_mounts: list = []

    # Auto-mount /tmp emptyDir only for the main container (which has readOnlyRootFilesystem=true).
    # Extra containers manage their own write paths; force-mounting /tmp on a 3rd-party
    # image (busybox, envoy, etc.) is unnecessary and potentially incorrect.
    if is_main:
        user_mounts_tmp = any(
            (v.mountPath == "/tmp")
            for v in container_cfg.volumes
        )
        if sec_ctx.get("readOnlyRootFilesystem") and getattr(container_cfg, 'auto_mount_tmp', True) and not user_mounts_tmp:
            volume_mounts.append({"name": "tmp", "mountPath": "/tmp"})

    # Legacy support only for main container
    if is_main:
        app_cfg = container_cfg  # type: ignore
        if app_cfg.mount_env_file:
            volume_mounts.append({"name": "env-file", "mountPath": "/app/.env", "subPath": ".env"})
        if app_cfg.pvc.enabled and app_cfg.pvc.mountPath:
            volume_mounts.append({"name": "data-volume", "mountPath": app_cfg.pvc.mountPath})

    # Flattened volumes
    _, new_mounts = build_volume_items(container_cfg.volumes)
    volume_mounts.extend(new_mounts)

    # 4. Env Assembly
    env_list: list = []
    # a) Legacy main container envs
    if is_main:
        app_cfg = container_cfg  # type: ignore
        env_list.extend(list(app_cfg.env))

        for key in app_cfg.env_vars:
            if key not in secret_pool:
                print(
                    f"  [WARN] env_var '{key}' in container '{name}' is not declared in "
                    f"secret_pool (not found in .env as a ${{...}} placeholder). "
                    "Injecting anyway — ensure the key exists in the project Secret."
                )
            env_list.append({
                "name": key,
                "valueFrom": {"secretKeyRef": {"name": f"{project_name}-secret", "key": key}},
            })

    # b) New flattened envs
    new_env_list, new_env_from_list = build_env_items(container_cfg.envs)
    env_list.extend(new_env_list)

    # c) Env from sources
    env_from: list = []
    # Project-shared ConfigMap is ONLY injected into the main container.
    # Init/sidecar containers are self-contained — if they need a ConfigMap,
    # the user must explicitly declare it via envs: [{configMap: ...}].
    if is_main:
        app_cfg = container_cfg  # type: ignore
        if config_pool:
            env_from.append({"configMapRef": {"name": f"{project_name}-config"}})
        env_from.extend(app_cfg.envFrom)

    env_from.extend(new_env_from_list)

    # 5. Probes
    # Classic Init Containers (no restartPolicy) MUST NOT have probes — K8s will reject the manifest.
    # Native Sidecars (restartPolicy: Always) and traditional Sidecars support probes normally.
    # The caller controls this via the `allow_probes` parameter.
    probes: dict = {}
    if allow_probes and container_cfg.health.enabled:
        fallback_port = getattr(container_cfg, 'port', None) or primary_port
        if liveness := _build_probe(container_cfg.health.liveness, container_cfg.health, fallback_port):
            probes["livenessProbe"] = liveness
        if readiness := _build_probe(container_cfg.health.readiness, container_cfg.health, fallback_port):
            probes["readinessProbe"] = readiness
        if startup := _build_probe(container_cfg.health.startup, container_cfg.health, fallback_port):
            probes["startupProbe"] = startup

    # 6. Container Ports
    c_ports: list = []
    if is_main:
        # For main container, we collect ports from AppConfig.ports or AppConfig.port
        # but build_values_yaml handles this via all_ports list. We'll pass it in later.
        pass
    elif hasattr(container_cfg, 'port') and container_cfg.port: # Special case if extra container has a single port
        c_ports.append({"name": "http", "containerPort": container_cfg.port, "protocol": "TCP"})

    # 7. Final Spec
    res = {
        "name": name,
        "image": f"{image_name}:{tag}",
        "imagePullPolicy": container_cfg.pullPolicy or "IfNotPresent",
        "securityContext": sec_ctx,
        "env": env_list,
        "envFrom": env_from,
        "resources": container_cfg.resources,
        "volumeMounts": volume_mounts,
    }
    if container_cfg.command: res["command"] = container_cfg.command
    if container_cfg.args: res["args"] = container_cfg.args
    if c_ports: res["ports"] = c_ports
    res.update(probes)

    if hasattr(container_cfg, 'restartPolicy') and container_cfg.restartPolicy:
        res["restartPolicy"] = container_cfg.restartPolicy

    return res


def build_values_yaml(
    app: AppConfig,
    project: ProjectDefinition,
    project_vars: tuple[dict, list],
    image_tag: Optional[str] = None,
    allow_latest: bool = True,
) -> dict:
    config_pool, secret_pool = project_vars
    project_name = project.project

    # 1. Resolve Ports
    all_ports = app.ports.copy()
    if app.port and not any(p.port == app.port for p in all_ports):
        # BUG-3 FIX: ensure the shorthand name is unique (user may already have 'http' in ports)
        existing_names = {p.name for p in all_ports}
        shorthand_name = "http" if "http" not in existing_names else "primary"
        all_ports.insert(0, ServicePort(name=shorthand_name, port=app.port))
    if not all_ports:
        # Absolute fallback — every deployment needs at least one port
        all_ports.append(ServicePort(name="http", port=80))

    primary_svc_port = all_ports[0].port
    primary_container_port = all_ports[0].targetPort or all_ports[0].port

    # 2. Image logic — delegate to _resolve_image() to avoid duplication
    image_repo = app.image_repo or project.image_repo or DEFAULT_REGISTRY
    image_name, tag = _resolve_image(
        image=app.image,
        image_repo=image_repo,
        name=app.name,
        image_tag_override=image_tag,
        item_image_tag=app.image_tag,
        project_image_tag=project.image_tag,
    )

    # Guard against non-deterministic 'latest' tag.
    # When allow_latest=False (CI production mode), raise to block the pipeline.
    if tag == "latest":
        if allow_latest:
            print(
                f"  [WARN] App '{app.name}' uses image tag 'latest' — "
                "consider pinning to a specific version for reproducible deployments."
            )
        else:
            raise ValueError(
                f"App '{app.name}' uses non-deterministic image tag 'latest'. "
                "Pin to a specific version, or pass --allow-latest to override."
            )

    # SEC-H3 FIX: Warn when resource limits are not defined.
    # Missing limits can cause noisy-neighbor issues and resource exhaustion in production.
    if not app.resources:
        print(
            f"  [WARN] App '{app.name}' has no resource limits defined. "
            "Consider setting resources.limits and resources.requests for production workloads."
        )

    # 3. Security Context & Base Volumes
    sec_ctx = {**_DEFAULT_CONTAINER_SEC_CTX, **app.securityContext}

    # 3. Probes (built inside _build_container_dict)
    # 4. Envs (built inside _build_container_dict)

    # 5. Volumes (Pod-level collection)
    pod_volumes: list = []

    # /tmp volume
    user_mounts_tmp_any = any(
        (v.mountPath == "/tmp")
        for v in app.volumes
    )
    # Check all init/sidecar volumes too for /tmp
    for c in app.initContainers + app.sidecars:
        if any((v.mountPath == "/tmp") for v in c.volumes):
            user_mounts_tmp_any = True
            break

    if sec_ctx.get("readOnlyRootFilesystem") and app.auto_mount_tmp and not user_mounts_tmp_any:
        pod_volumes.append({"name": "tmp", "emptyDir": {}})

    if app.mount_env_file:
        pod_volumes.append({"name": "env-file", "configMap": {"name": f"{project_name}-config"}})

    if app.pvc.enabled and app.pvc.mountPath:
        pod_volumes.append({
            "name": "data-volume",
            "persistentVolumeClaim": {"claimName": '{{ include "common-lib.fullname" . }}'},
        })

    # Collect all volume specs from all containers
    v_specs, _ = build_volume_items(app.volumes)
    pod_volumes.extend(v_specs)

    for c in app.initContainers + app.sidecars:
        c_v_specs, _ = build_volume_items(c.volumes)
        # Avoid duplicate volume names in Pod spec
        existing_v_names = {v["name"] for v in pod_volumes}
        for v in c_v_specs:
            if v["name"] not in existing_v_names:
                pod_volumes.append(v)

    # 6. Build Main Container
    main_container = _build_container_dict(
        name=app.name, # Fullname will be prepended by Helm, but we pass current name
        container_cfg=app,
        project=project,
        project_vars=project_vars,
        image_tag_override=image_tag,
        is_main=True,
        primary_port=primary_container_port,
    )
    # Main container ports are special because they come from AppConfig.ports or port
    main_container["name"] = '{{ include "common-lib.name" . }}'
    main_container["ports"] = [
        {"name": p.name, "containerPort": p.targetPort or p.port, "protocol": p.protocol}
        for p in all_ports
    ]
    # PY-H3 FIX: Use the already-resolved `image_name` variable instead of re-parsing
    # the composed "image:tag" string. The old `split(":")[0]` approach breaks for
    # registries with a port number (e.g. "registry:5000/app:v1" → split gives "registry").
    main_container.pop("image")  # Remove composed image:tag from container dict
    image_info = {
        "repository": image_name,
        "tag": tag,
        "pullPolicy": main_container.pop("imagePullPolicy")
    }
    
    deep_update(main_container, app.k8s.mainContainer)

    # 7. Build Init Containers
    # Classic Init Containers (no restartPolicy): probes are NOT allowed by K8s spec.
    # Native Sidecars (restartPolicy: Always): probes ARE allowed.
    init_containers = []
    for c in app.initContainers:
        is_native_sidecar = c.restartPolicy == "Always"
        init_containers.append(_build_container_dict(
            name=c.name,
            container_cfg=c,
            project=project,
            project_vars=project_vars,
            image_tag_override=image_tag,
            allow_probes=is_native_sidecar,  # Only native sidecars support probes
            primary_port=primary_container_port,
        ))

    # 8. Build Sidecar Containers (traditional — run alongside main container)
    # Traditional sidecars support the full lifecycle including probes.
    sidecar_containers = []
    for s in app.sidecars:
        sidecar_containers.append(_build_container_dict(
            name=s.name,
            container_cfg=s,
            project=project,
            project_vars=project_vars,
            image_tag_override=image_tag,
            allow_probes=True,
            primary_port=primary_container_port,
        ))

    # 9. Service Configuration
    svc_explicitly_disabled = app.service is False
    svc_explicitly_configured = isinstance(app.service, ServiceConfig)
    svc_enabled = not svc_explicitly_disabled and (svc_explicitly_configured or app.ingress.enabled)

    svc_values: dict = {"enabled": False}
    svc_ports: list = []
    if svc_enabled:
        s = app.service if isinstance(app.service, ServiceConfig) else ServiceConfig()
        source_ports = s.ports.copy()
        if s.port and not any(p.port == s.port for p in source_ports):
            source_ports.insert(0, ServicePort(name="http", port=s.port, targetPort=s.targetPort))
        for p in (source_ports if source_ports else all_ports):
            port_entry = {
                "name": p.name, "port": p.port,
                "targetPort": p.targetPort or p.port,
                "protocol": p.protocol,
            }
            if p.nodePort is not None:
                port_entry["nodePort"] = p.nodePort
            svc_ports.append(port_entry)
        svc_values = {"enabled": True, "type": s.type, "ports": svc_ports, "annotations": s.annotations}

    # 10. Ingress Configuration
    ing_values: dict = {"enabled": False}
    if app.ingress.enabled:
        ing_values = app.ingress.model_dump(exclude_none=True)
        if app.ingress.host and not app.ingress.hosts:
            ing_values["hosts"] = [{
                "host": app.ingress.host,
                "paths": [{"path": app.ingress.path or "/", "pathType": "ImplementationSpecific"}],
            }]

        if app.ingress.servicePort:
            default_svc_port: Union[int, str] = app.ingress.servicePort
        elif svc_enabled and svc_ports:
            default_svc_port = svc_ports[0]["port"]
        else:
            default_svc_port = primary_svc_port

        for h in ing_values.get("hosts", []):
            for p in h.get("paths", []):
                if not p.get("servicePort"):
                    p["servicePort"] = default_svc_port

        final_annotations: dict[str, str] = {}
        if (app.ingress.className or "nginx") == "nginx":
            final_annotations.update(_NGINX_DEFAULT_ANNOTATIONS)
        final_annotations.update(app.ingress.annotations)
        ing_values["annotations"] = final_annotations

    # 11. Assembly
    # KEY DECISION: When HPA is enabled, omit 'replicas' from the Deployment spec.
    # If 'replicas' is set, the Deployment controller will fight with HPA over the replica count,
    # resetting it to the static value on every reconcile. The HPA takes sole ownership.

    # strategy: accept both shorthand string and full object
    if isinstance(app.strategy, str):
        strategy_val: dict = {"type": app.strategy}
    else:
        strategy_val = app.strategy  # Full object: {type: RollingUpdate, rollingUpdate: {...}}

    deploy_dict = {
        "strategy": strategy_val,
        # podSecurityContext is POD-level only: runAsUser/Group, fsGroup, supplementalGroups.
        # Container-level fields (readOnlyRootFilesystem, capabilities) belong in securityContext.
        "podSecurityContext": {**_DEFAULT_POD_SEC_CTX, **app.podSecurityContext},
        "volumes": pod_volumes,
        "affinity": app.affinity,
        "tolerations": app.tolerations,
        "nodeSelector": app.nodeSelector,
        "podLabels": app.podLabels,
        "imagePullSecrets": (
            app.imagePullSecrets
            or project.imagePullSecrets
            or [{"name": DEFAULT_PULL_SECRET}]
        ),
        "podAnnotations": app.podAnnotations,
        "initContainers": init_containers,
        "sidecars": sidecar_containers, # This goes into the main containers array in template
    }
    # Only set static replicas when HPA is NOT managing the scaling
    if not app.hpa.enabled:
        deploy_dict["replicas"] = app.replicas
    # Main container fields are flat in deployment values
    deploy_dict.update(main_container)
    
    # Process k8s overrides: lists should be extended directly into deploy_dict to prevent duplicate keys
    k8s_pod = app.k8s.pod.copy()
    for list_key in ["volumes", "initContainers", "imagePullSecrets", "tolerations"]:
        if list_key in k8s_pod and isinstance(k8s_pod[list_key], list):
            deploy_dict[list_key].extend(k8s_pod.pop(list_key))
            
    deploy_dict["k8sPod"] = k8s_pod
    deploy_dict["k8sDeployment"] = app.k8s.deployment

    # Build serviceAccount conditionally.
    # by_alias=True: serialize 'automountToken' → 'automountServiceAccountToken'
    # to match the Kubernetes API field name expected by Helm templates.
    sa_dict = app.serviceAccount.model_dump(exclude_none=True, by_alias=True)
    sa_name = app.serviceAccount.name or app.serviceAccountName
    if sa_name and "name" not in sa_dict:
        sa_dict["name"] = sa_name

    # 12. Service & Ingress: disabled for batch workloads (Job/CronJob have no inbound traffic)
    is_batch = app.type in ("job", "cronjob")
    if is_batch:
        svc_values = {"enabled": False}
        ing_values = {"enabled": False}

    # 13. Job / CronJob config dicts — merge k8s.job / k8s.cronjob overrides last
    job_dict: dict = {}
    if app.job:
        job_dict = app.job.model_dump(exclude_none=True)
        if app.k8s.job:
            deep_update(job_dict, app.k8s.job)

    cronjob_dict: dict = {}
    if app.cronjob:
        cronjob_dict = app.cronjob.model_dump(exclude_none=True)
        if app.k8s.cronjob:
            deep_update(cronjob_dict, app.k8s.cronjob)

    # 14. HPA config
    hpa_dict = app.hpa.model_dump(exclude_none=True)

    # 15. Apply Native K8s Overrides to specific components
    deep_update(svc_values, app.k8s.service)
    deep_update(ing_values, app.k8s.ingress)

    return {
        "type": app.type,
        "image": image_info,
        "deployment": deploy_dict,
        "serviceAccount": sa_dict,
        "service": svc_values,
        "ingress": ing_values,
        "pvc": app.pvc.model_dump(exclude_none=True),
        "localConfig": {"enabled": app.genConfigMaps},
        "job": job_dict,
        "cronjob": cronjob_dict,
        "hpa": hpa_dict,
    }


def build_project_pvcs_yaml(pvcs: list[ProjectPVC]) -> str:
    """Generate a multi-document YAML string containing all project-level PVCs.

    PVCs are stateful infrastructure — they must NOT be deleted when Helm or ArgoCD
    uninstalls/prunes the chart. We therefore annotate every PVC with:
      - helm.sh/resource-policy: keep  (Helm will not delete on `helm uninstall`)
      - argocd.argoproj.io/sync-options: Prune=false  (ArgoCD will not prune)

    PVCs should only be deleted manually via `kubectl delete pvc <name>`.
    """
    docs = []
    for pvc in pvcs:
        spec: dict = {
            "accessModes": pvc.accessModes,
            "resources": {"requests": {"storage": pvc.size}},
        }
        # LOGIC-1 FIX: Use 'is not None' — empty string "" is a valid K8s value meaning
        # "no storage class" (forces PVC to match a PV without a class).
        if pvc.storageClass is not None:
            spec["storageClassName"] = pvc.storageClass
        doc = {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {
                "name": pvc.name,
                "annotations": {
                    # Prevent accidental deletion by Helm uninstall
                    "helm.sh/resource-policy": "keep",
                    # Prevent accidental pruning by ArgoCD auto-sync
                    "argocd.argoproj.io/sync-options": "Prune=false",
                },
            },
            "spec": spec,
        }
        docs.append(yaml.dump(doc, default_flow_style=False, sort_keys=False, allow_unicode=True))
    return "---\n" + "---\n".join(docs)


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

ALL_YAML_CONTENT = '{{/* Auto-generated by generator.py */}}\n{{ include "common-lib.main" . }}\n'


def main():
    parser = argparse.ArgumentParser(description="GitOps Engine Generator")
    parser.add_argument("--team", required=True, help="Team name (e.g. 'ops-team', 'backend')")
    parser.add_argument("--project", required=True)
    parser.add_argument("--env", default="dev")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--image-tag")
    parser.add_argument(
        "--allow-latest",
        action="store_true",
        help="Allow 'latest' image tag. Without this flag, 'latest' only generates a warning.",
    )
    args = parser.parse_args()

    # Validate --team, --env — must be valid lowercase Kubernetes-compatible identifiers.
    # This prevents path traversal, typos, and injection via shell expansion.
    for flag, value in (("--team", args.team), ("--env", args.env)):
        if not re.match(r'^[a-z0-9]([a-z0-9\-]*[a-z0-9])?$', value):
            print(
                f"ERROR: {flag} '{value}' must be a valid lowercase identifier "
                "(e.g. 'ops-team', 'dev'). No uppercase, underscores, or special chars."
            )
            sys.exit(1)

    # Paths
    # Structure: projects/<team>/<project>/<project>-<env>.yaml
    project_dir = REPO_ROOT / "projects" / args.team / args.project
    definition_path = project_dir / f"{args.project}-{args.env}.yaml"
    env_path = project_dir / f"{args.project}-{args.env}.env"
    output_dir = project_dir / f"{args.project}-{args.env}"

    if not definition_path.exists():
        print(f"ERROR: Definition not found at {definition_path}")
        sys.exit(1)

    with definition_path.open() as f:
        data = yaml.safe_load(f)
    try:
        project_def = ProjectDefinition(**data)
    except Exception as e:
        print(f"ERROR: Validation failed for {definition_path}:\n{e}")
        sys.exit(1)

    project_vars = parse_env_file(env_path)
    config_pool = project_vars[0]

    CHARTS_DIR = output_dir.resolve()
    print(f"=== GitOps Engine Generator ===\nTeam: {args.team} | Project: {project_def.project} | Env: {args.env}")

    # --- Generate project-shared chart ---
    # LOGIC-2 FIX: project-shared only needed when there is ACTUAL config data or PVCs.
    # secret_keys alone do NOT require a ConfigMap — they are Vault-injected into K8s Secrets.
    has_config = bool(config_pool)   # True only when .env has non-secret key=value pairs
    has_pvcs = bool(project_def.pvcs)
    if has_config or has_pvcs:
        shared_dir = CHARTS_DIR / "project-shared"
        ensure_dir(shared_dir, args.dry_run)
        ensure_dir(shared_dir / "templates", args.dry_run)
        if not args.dry_run:
            # project-shared does NOT depend on common-lib — it only contains
            # plain K8s resources (ConfigMap, PVCs) that need no helper templates.
            shared_chart = {
                "apiVersion": "v2",
                "name": f"{project_def.project}-shared",
                "description": "Shared project resources: ConfigMap, PVCs (lifecycle-independent)",
                "type": "application",
                "version": "0.1.0",
            }
            write_yaml(shared_dir / "Chart.yaml", shared_chart, False)
            # PY-M2 FIX: Generate empty values.yaml so Helm lint does not warn.
            write_yaml(shared_dir / "values.yaml", {}, False)

            # Only write ConfigMap when there is actual non-secret data.
            # An empty ConfigMap causes unnecessary resource churn and confuses operators.
            if has_config:
                cm = {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": f"{project_def.project}-config"},
                    "data": config_pool,
                }
                write_yaml(shared_dir / "templates" / "configmap.yaml", cm, False)

            if has_pvcs:
                pvcs_path = shared_dir / "templates" / "pvcs.yaml"
                pvcs_content = build_project_pvcs_yaml(project_def.pvcs)
                pvcs_path.write_text(pvcs_content)
                print(f"  [WRITE]   {pvcs_path}")

    # --- Generate App Charts ---
    for app in project_def.apps:
        chart_dir = CHARTS_DIR / app.name
        ensure_dir(chart_dir, args.dry_run)
        ensure_dir(chart_dir / "templates", args.dry_run)
        if not args.dry_run:
            (chart_dir / "templates" / "all.yaml").write_text(ALL_YAML_CONTENT)

            # Compute relative path from THIS chart's directory to common-lib dynamically.
            # This is more robust than a hardcoded constant if directory structure changes.
            common_lib_rel = _get_common_lib_rel_path(chart_dir)

            desc = {
                "apiVersion": "v2",
                "name": app.name,
                "description": f"Chart for {app.name}",
                "type": "application",
                "version": "0.1.0",
                "dependencies": [{
                    "name": "common-lib",
                    "version": project_def.common_version,
                    "repository": f"file://{common_lib_rel}",
                    "alias": "common-lib",
                }],
            }
            write_yaml(chart_dir / "Chart.yaml", desc, False)
            write_yaml(
                chart_dir / "values.yaml",
                build_values_yaml(app, project_def, project_vars, args.image_tag, args.allow_latest),
                False,
            )

    print(f"\n✅ Manifests generated in {output_dir}")


if __name__ == "__main__":
    main()
