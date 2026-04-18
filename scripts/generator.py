#!/usr/bin/env python3
"""
generator.py
============
The Python "Engine" for the Helm Monorepo Automation System.

Reads apps.{env}.yaml (Single Source of Truth) and for each app:
  1. Creates/updates the directory at projects/{env}/{project}/charts/<app-name>/
  2. Generates Chart.yaml with a file:// dependency on common-lib
  3. Generates values.yaml mapping app config to common-lib value keys

Design principle: IDEMPOTENT — safe to run multiple times without side effects.
"""

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Optional, Union

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
    model_config = ConfigDict(extra='forbid')
    create: bool = True
    automountToken: bool = False  # Hardened default (RBAC Security)
    name: Optional[str] = None   # If set, overrides the auto-generated fullname


class ServiceConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = True
    type: str = "ClusterIP"
    port: Optional[int] = None       # Shortcut: single-port service
    targetPort: Optional[int] = None # Shortcut: maps to container targetPort
    ports: list[ServicePort] = Field(default_factory=list)
    annotations: dict[str, str] = Field(default_factory=dict)


class EnvItem(BaseModel):
    """
    Flexible env configuration. Exactly one source key must be set.

    Supported patterns:
      Plain value:    name: KEY, value: VALUE
      Per-key secret: secretEnv: secret-name, vars: [KEY1, KEY2]
      envFrom CM:     configMap: configmap-name
      envFrom Secret: secret: secret-name
      Native K8s:     k8s: {name: ..., valueFrom: ...}  # Downward API, resourceFieldRef, etc.
    """
    model_config = ConfigDict(extra='forbid')

    # Plain env var
    name: Optional[str] = None
    value: Optional[str] = None

    # Per-key secretKeyRef injection (replaces legacy vault/env_vars logic)
    secretEnv: Optional[str] = None
    vars: Optional[list[str]] = None

    # envFrom sources
    configMap: Optional[str] = None
    secret: Optional[str] = None

    # Native K8s EnvVar manifest (escape hatch for Downward API, resourceFieldRef, etc.)
    k8s: Optional[dict] = None

    @model_validator(mode='after')
    def validate_source(self) -> 'EnvItem':
        sources = sum([
            self.name is not None,
            self.secretEnv is not None,
            self.configMap is not None,
            self.secret is not None,
            self.k8s is not None,
        ])
        if sources == 0:
            raise ValueError(
                "EnvItem must define exactly one source: "
                "name/value, secretEnv(+vars), configMap, secret, or k8s"
            )
        if sources > 1:
            raise ValueError(
                f"EnvItem has multiple sources defined — only one is allowed. "
                f"Found: name={self.name}, secretEnv={self.secretEnv}, "
                f"configMap={self.configMap}, secret={self.secret}, k8s={bool(self.k8s)}"
            )
        if self.secretEnv is not None and not self.vars:
            raise ValueError("'secretEnv' requires a non-empty 'vars' list")
        return self


class VolumeItem(BaseModel):
    """
    Unified volume config combining Volume source and VolumeMount in one entry.
    Supported sources: pvc, emptyDir, hostPath, configMap, secret, k8s.

    Mount options (readOnly, mountPropagation, recursiveReadOnly) are
    applied to the VolumeMount spec.

    NOTE: emptyDir: {} is a valid empty dict (falsy in Python!) — always use
    'is not None' checks, never 'if item.emptyDir'.

    For native K8s manifest (escape hatch for NFS, CSI, Projected, etc.):
      k8s:
        volume: {name: ..., <source_spec>: ...}
        mount:  {mountPath: ..., ...}
    """
    model_config = ConfigDict(extra='forbid')

    # VolumeMount fields (required for non-k8s)
    name: Optional[str] = None
    mountPath: Optional[str] = None
    readOnly: Optional[bool] = None
    mountPropagation: Optional[str] = None
    recursiveReadOnly: Optional[str] = None

    # Volume sources (exactly one must be set)
    pvc: Optional[str] = None                       # claimName
    emptyDir: Optional[dict] = None                 # {} or {medium: Memory}  ← may be falsy!
    hostPath: Optional[Union[str, dict]] = None     # "/path" or {path:, type:}
    configMap: Optional[Union[str, dict]] = None    # "cm-name" or {name:, items:}
    secret: Optional[Union[str, dict]] = None       # "sec-name" or {secretName:, items:}
    k8s: Optional[dict] = None                      # Native K8s: {volume: {...}, mount: {...}}

    @model_validator(mode='after')
    def validate_source(self) -> 'VolumeItem':
        # NOTE: emptyDir may be {} (falsy), use 'is not None' throughout
        sources = sum([
            self.pvc is not None,
            self.emptyDir is not None,   # {} is valid — do NOT use `bool(self.emptyDir)`
            self.hostPath is not None,
            self.configMap is not None,
            self.secret is not None,
            self.k8s is not None,
        ])
        if sources == 0:
            raise ValueError(
                "VolumeItem must define exactly one source: "
                "pvc, emptyDir, hostPath, configMap, secret, or k8s"
            )
        if sources > 1:
            raise ValueError("VolumeItem has multiple sources — only one is allowed")
        if self.k8s is None:
            if not self.name:
                raise ValueError("'name' is required for non-k8s volumes")
            if not self.mountPath:
                raise ValueError("'mountPath' is required for non-k8s volumes")
        return self


class AppConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')

    name: str
    type: str = "deployment"

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

    strategy: str = "RollingUpdate"
    affinity: dict = Field(default_factory=dict)
    tolerations: list = Field(default_factory=list)
    serviceAccountName: Optional[str] = None  # DEPRECATED: prefer serviceAccount.name
    genConfigMaps: bool = False
    podAnnotations: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode='after')
    def validate_app(self) -> 'AppConfig':
        # Validate K8s-compatible name
        _validate_k8s_name(self.name, "App name")

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
        return self


# ---------------------------------------------------------------------------
# File Utilities
# ---------------------------------------------------------------------------

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
    if path:
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
        if item.k8s is not None:
            # Native K8s EnvVar spec (Downward API, resourceFieldRef, etc.)
            env_list.append(item.k8s)

        elif item.secretEnv is not None:
            # Per-key secretKeyRef — inject each var individually
            for key in (item.vars or []):
                env_list.append({
                    "name": key,
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": item.secretEnv,
                            "key": key,
                        }
                    },
                })

        elif item.configMap is not None:
            # envFrom configMapRef — inject all keys from a ConfigMap
            env_from_list.append({"configMapRef": {"name": item.configMap}})

        elif item.secret is not None:
            # envFrom secretRef — inject all keys from a Secret
            env_from_list.append({"secretRef": {"name": item.secret}})

        elif item.name is not None:
            # Plain value
            entry: dict = {"name": item.name}
            if item.value is not None:
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
        if item.k8s is not None:
            # Native K8s Volume + VolumeMount spec (NFS, CSI, Projected, etc.)
            k8s_vol = dict(item.k8s.get("volume", {}))
            k8s_mount = dict(item.k8s.get("mount", {}))
            if not k8s_vol:
                raise ValueError("k8s volume item must have a non-empty 'volume' key")
            if "name" not in k8s_vol:
                raise ValueError("k8s volume 'volume' dict must include 'name'")
            k8s_mount["name"] = k8s_vol["name"]
            volume_specs.append(k8s_vol)
            mount_specs.append(k8s_mount)
            continue

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

        volume_specs.append(vol)
        mount_specs.append(vm)

    return volume_specs, mount_specs


# ---------------------------------------------------------------------------
# Logic Builders
# ---------------------------------------------------------------------------

def build_values_yaml(
    app: AppConfig,
    project: ProjectDefinition,
    project_vars: tuple[dict, list],
    image_tag: str = None,
) -> dict:
    config_pool, secret_pool = project_vars
    project_name = project.project

    # 1. Resolve Ports
    all_ports = app.ports.copy()
    if app.port and not any(p.port == app.port for p in all_ports):
        # Legacy single-port shorthand — prepend as primary port
        all_ports.insert(0, ServicePort(name="http", port=app.port))
    if not all_ports:
        # Absolute fallback — every deployment needs at least one port
        all_ports.append(ServicePort(name="http", port=80))

    primary_svc_port = all_ports[0].port
    primary_container_port = all_ports[0].targetPort or all_ports[0].port

    # 2. Image logic
    image_repo = app.image_repo or project.image_repo or DEFAULT_REGISTRY
    tag = image_tag or app.image_tag or project.image_tag
    image_name = app.image if app.image else f"{image_repo}/{app.name}"

    # Warn on non-deterministic 'latest' tag
    if tag == "latest":
        print(
            f"  [WARN] App '{app.name}' uses image tag 'latest' — "
            "consider pinning to a specific version for reproducible deployments."
        )

    # 3. Security Context & Base Volumes
    default_sec_ctx: dict = {
        "readOnlyRootFilesystem": True, "allowPrivilegeEscalation": False,
        "runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000,
        "capabilities": {"drop": ["ALL"]}
    }
    sec_ctx = {**default_sec_ctx, **app.securityContext}

    volumes: list = []
    volume_mounts: list = []

    # Auto-mount /tmp emptyDir when readOnlyRootFilesystem is enabled
    if sec_ctx.get("readOnlyRootFilesystem") and app.auto_mount_tmp:
        volumes.append({"name": "tmp", "emptyDir": {}})
        volume_mounts.append({"name": "tmp", "mountPath": "/tmp"})

    # 4. [DEPRECATED] Legacy mount_env_file
    if app.mount_env_file:
        volumes.append({"name": "env-file", "configMap": {"name": f"{project_name}-config"}})
        volume_mounts.append({"name": "env-file", "mountPath": "/app/.env", "subPath": ".env"})

    # 5. [DEPRECATED] Legacy per-app pvc (creates PVC inside app chart)
    if app.pvc.enabled and app.pvc.mountPath:
        volumes.append({
            "name": "data-volume",
            "persistentVolumeClaim": {"claimName": '{{ include "common-lib.fullname" . }}'},
        })
        volume_mounts.append({"name": "data-volume", "mountPath": app.pvc.mountPath})

    # 6. New flattened volumes
    new_vols, new_mounts = build_volume_items(app.volumes)
    volumes.extend(new_vols)
    volume_mounts.extend(new_mounts)

    # 7. Probes
    liveness, readiness, startup = None, None, None
    if app.health.enabled:
        liveness = _build_probe(app.health.liveness, app.health, primary_container_port)
        readiness = _build_probe(app.health.readiness, app.health, primary_container_port)
        startup = _build_probe(app.health.startup, app.health, primary_container_port)

    # 8. Service Configuration
    #
    # Service is enabled when:
    #   - NOT explicitly disabled via `service: false`
    #   - AND either explicitly configured OR the ingress requires a backend
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
            svc_ports.append({
                "name": p.name, "port": p.port,
                "targetPort": p.targetPort or p.port,
                "protocol": p.protocol, "nodePort": p.nodePort,
            })
        svc_values = {"enabled": True, "type": s.type, "ports": svc_ports, "annotations": s.annotations}

    # 9. Ingress Configuration
    ing_values: dict = {"enabled": False}
    if app.ingress.enabled:
        ing_values = app.ingress.model_dump(exclude_none=True)

        # Expand shorthand host/path into hosts list
        if app.ingress.host and not app.ingress.hosts:
            ing_values["hosts"] = [{
                "host": app.ingress.host,
                "paths": [{"path": app.ingress.path or "/", "pathType": "ImplementationSpecific"}],
            }]

        # Resolve default servicePort per path (explicit precedence: path > ingress > service > container)
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

        # Merge Nginx default annotations with user-provided (user overrides defaults)
        final_annotations: dict[str, str] = {}
        if (app.ingress.className or "nginx") == "nginx":
            final_annotations.update(_NGINX_DEFAULT_ANNOTATIONS)
        final_annotations.update(app.ingress.annotations)  # User values win
        ing_values["annotations"] = final_annotations

    # 10. Env Assembly
    #
    # Merge order (later entries take precedence in K8s if duplicate names):
    #   Env vars:   legacy env[] → legacy env_vars → new envs[]
    #   Env from:   project ConfigMap (if exists) → legacy envFrom[] → new envs[] envFrom entries

    # a) Legacy env[] — raw K8s EnvVar list
    env_list: list = list(app.env)

    # b) Legacy env_vars — always inject keys into secretKeyRef from project secret.
    #    Warn if the key is not declared as a Vault placeholder in the .env file,
    #    but inject anyway because the K8s Secret may have been populated externally.
    for key in app.env_vars:
        if key not in secret_pool:
            print(
                f"  [WARN] env_var '{key}' in app '{app.name}' is not declared in "
                f"secret_pool (not found in .env as a ${{...}} placeholder). "
                "Injecting anyway — ensure the key exists in the project Secret."
            )
        env_list.append({
            "name": key,
            "valueFrom": {"secretKeyRef": {"name": f"{project_name}-secret", "key": key}},
        })

    # c) New envs[] — env vars from EnvItem list
    new_env_list, new_env_from_list = build_env_items(app.envs)
    env_list.extend(new_env_list)

    # d) Env from sources
    env_from: list = []
    # Only inject the project ConfigMap if it actually exists (i.e., .env file had config data)
    if config_pool:
        env_from.append({"configMapRef": {"name": f"{project_name}-config"}})
    env_from.extend(app.envFrom)          # Legacy envFrom[]
    env_from.extend(new_env_from_list)    # New envs[] envFrom entries

    # 11. Container Ports
    deploy_ports = [
        {"name": p.name, "containerPort": p.targetPort or p.port, "protocol": p.protocol}
        for p in all_ports
    ]

    return {
        "type": app.type,
        "image": {"repository": image_name, "tag": tag, "pullPolicy": app.pullPolicy},
        "deployment": {
            "replicas": app.replicas,
            "containerPort": primary_container_port,
            "ports": deploy_ports,
            "resources": app.resources,
            "strategy": {"type": app.strategy},
            "securityContext": sec_ctx,
            "envFrom": env_from,
            "env": env_list,
            "volumes": volumes,
            "volumeMounts": volume_mounts,
            "affinity": app.affinity,
            "livenessProbe": liveness,
            "readinessProbe": readiness,
            "startupProbe": startup,
            "imagePullSecrets": (
                app.imagePullSecrets
                or project.imagePullSecrets
                or [{"name": DEFAULT_PULL_SECRET}]
            ),
            "podAnnotations": app.podAnnotations,
        },
        "serviceAccount": {
            "create": app.serviceAccount.create,
            "name": app.serviceAccount.name or app.serviceAccountName,
            "automountServiceAccountToken": app.serviceAccount.automountToken,
        },
        "service": svc_values,
        "ingress": ing_values,
        "pvc": app.pvc.model_dump(exclude_none=True),
        "localConfig": {"enabled": app.genConfigMaps},
    }


def build_project_pvcs_yaml(pvcs: list[ProjectPVC]) -> str:
    """Generate a multi-document YAML string containing all project-level PVCs."""
    docs = []
    for pvc in pvcs:
        spec: dict = {
            "accessModes": pvc.accessModes,
            "resources": {"requests": {"storage": pvc.size}},
        }
        if pvc.storageClass:
            spec["storageClassName"] = pvc.storageClass
        doc = {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": pvc.name},
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
    parser.add_argument("--project", required=True)
    parser.add_argument("--env", default="dev")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--image-tag")
    args = parser.parse_args()

    # Paths
    project_dir = REPO_ROOT / "projects" / args.env / args.project
    definition_path = project_dir / f"apps.{args.env}.yaml"
    env_path = project_dir / f"apps.{args.env}.env"
    output_dir = project_dir / "charts"

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
    print(f"=== GitOps Engine Generator ===\nProject: {project_def.project} | Env: {args.env}")

    # --- Generate project-shared chart ---
    has_config = bool(config_pool) or bool(project_vars[1])
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
                build_values_yaml(app, project_def, project_vars, args.image_tag),
                False,
            )

    print(f"\n✅ Manifests generated in {output_dir}")


if __name__ == "__main__":
    main()
