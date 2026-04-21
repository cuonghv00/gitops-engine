#!/usr/bin/env python3
"""
test_generator.py
=================
pytest-based test suite for generator.py.

Run with: python -m pytest scripts/test_generator.py -v
Or:        pytest scripts/test_generator.py -v --tb=short

Covers:
  - EnvItem / VolumeItem / ProjectPVC / AppConfig / HPAConfig validation
  - build_env_items / build_volume_items output
  - build_values_yaml integration (env, volumes, image, ingress, HPA, etc.)
  - _resolve_image helper
  - podSecurityContext (pod vs container level separation)
  - parse_env_file
  - Init Containers / Sidecars
  - Job / CronJob batch workloads
  - allow_latest flag
  - --env / semver validations
"""
import sys
import os
from pathlib import Path

import pytest

# Add repo root to path so 'scripts.generator' is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.generator import (
    ProjectDefinition,
    AppConfig,
    EnvItem,
    VolumeItem,
    ProjectPVC,
    ServicePort,
    ExtraContainerConfig,
    JobConfig,
    CronJobConfig,
    HPAConfig,
    build_values_yaml,
    build_env_items,
    build_volume_items,
    build_project_pvcs_yaml,
    parse_env_file,
    _validate_k8s_name,
    _resolve_image,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def base_project():
    return ProjectDefinition(project="test-proj", image_repo="registry.vn/test", image_tag="v1.0", apps=[])


@pytest.fixture
def batch_project():
    return ProjectDefinition(project="proj-batch", image_tag="v1", apps=[])


# ===========================================================================
# 1. EnvItem — Validation
# ===========================================================================

class TestEnvItemValidation:
    def test_plain_name_value(self):
        e = EnvItem(name="LOG", value="debug")
        assert e.name == "LOG"

    def test_secretenv_with_vars(self):
        e = EnvItem(secretEnv="my-secret", vars=["KEY1", "KEY2"])
        assert e.secretEnv == "my-secret"
        assert e.vars == ["KEY1", "KEY2"]

    def test_configmap_envfrom(self):
        e = EnvItem(configMap="global-cfg")
        assert e.configMap == "global-cfg"

    def test_secret_envfrom(self):
        e = EnvItem(secret="api-keys")
        assert e.secret == "api-keys"

    def test_k8s_native_env(self):
        e = EnvItem(k8s={"name": "POD_IP", "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}}})
        assert "name" in e.k8s

    def test_multiple_sources_raises(self):
        with pytest.raises(Exception):
            EnvItem(name="X", configMap="Y")

    def test_secretenv_without_vars_raises(self):
        with pytest.raises(Exception):
            EnvItem(secretEnv="sec")

    def test_empty_envitem_raises(self):
        with pytest.raises(Exception):
            EnvItem()


# ===========================================================================
# 2. build_env_items — Output
# ===========================================================================

class TestBuildEnvItems:
    @pytest.fixture(autouse=True)
    def _setup(self):
        envs = [
            EnvItem(name="LOG_LEVEL", value="debug"),
            EnvItem(secretEnv="proj-secret", vars=["DB_PASS", "API_KEY"]),
            EnvItem(configMap="global-cm"),
            EnvItem(secret="ext-secret"),
            EnvItem(k8s={"name": "POD_IP", "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}}}),
        ]
        self.env_list, self.env_from = build_env_items(envs)

    def test_plain_value_in_env_list(self):
        assert {"name": "LOG_LEVEL", "value": "debug"} in self.env_list

    def test_secretenv_db_pass_in_env_list(self):
        assert any(
            e.get("name") == "DB_PASS" and "secretKeyRef" in e.get("valueFrom", {})
            for e in self.env_list
        )

    def test_secretenv_api_key_in_env_list(self):
        assert any(e.get("name") == "API_KEY" for e in self.env_list)

    def test_configmap_in_env_from(self):
        assert {"configMapRef": {"name": "global-cm"}} in self.env_from

    def test_secret_in_env_from(self):
        assert {"secretRef": {"name": "ext-secret"}} in self.env_from

    def test_k8s_env_in_env_list(self):
        assert any(e.get("name") == "POD_IP" for e in self.env_list)


# ===========================================================================
# 3. VolumeItem — Validation
# ===========================================================================

class TestVolumeItemValidation:
    def test_pvc_with_mount_options(self):
        v = VolumeItem(
            name="data", mountPath="/data", pvc="my-claim",
            readOnly=True, mountPropagation="None", recursiveReadOnly="Enabled",
        )
        assert v.pvc == "my-claim"
        assert v.readOnly is True
        assert v.mountPropagation == "None"

    def test_emptydir_empty_dict(self):
        v = VolumeItem(name="tmp", mountPath="/tmp", emptyDir={})
        assert v.emptyDir == {}

    def test_emptydir_with_medium(self):
        v = VolumeItem(name="mem", mountPath="/cache", emptyDir={"medium": "Memory"})
        assert v.emptyDir["medium"] == "Memory"

    def test_hostpath_string_shorthand(self):
        v = VolumeItem(name="logs", mountPath="/var/log", hostPath="/var/log/nodes")
        assert v.hostPath == "/var/log/nodes"

    def test_configmap_string_shorthand(self):
        v = VolumeItem(name="cfg", mountPath="/etc/cfg", configMap="my-cm")
        assert v.configMap == "my-cm"

    def test_configmap_full_spec(self):
        v = VolumeItem(
            name="cfg2", mountPath="/etc/cfg2",
            configMap={"name": "my-cm", "items": [{"key": "a", "path": "a"}]},
        )
        assert isinstance(v.configMap, dict)

    def test_secret_string_shorthand(self):
        v = VolumeItem(name="certs", mountPath="/certs", secret="tls-cert")
        assert v.secret == "tls-cert"

    def test_secret_full_spec(self):
        v = VolumeItem(
            name="certs2", mountPath="/certs2",
            secret={"secretName": "tls-cert", "items": [{"key": "tls.crt", "path": "tls.crt"}]},
        )
        assert isinstance(v.secret, dict)

    def test_k8s_escape_hatch(self):
        v = VolumeItem(k8s={
            "volume": {"name": "nfs", "nfs": {"server": "nfs.example.com", "path": "/exports"}},
            "mount": {"mountPath": "/mnt/nfs", "readOnly": True},
        })
        assert "volume" in v.k8s

    def test_multiple_sources_raises(self):
        with pytest.raises(Exception):
            VolumeItem(name="x", mountPath="/x", pvc="y", emptyDir={})

    def test_no_source_raises(self):
        with pytest.raises(Exception):
            VolumeItem(name="x", mountPath="/x")


# ===========================================================================
# 4. build_volume_items — Output
# ===========================================================================

class TestBuildVolumeItems:
    @pytest.fixture(autouse=True)
    def _setup(self):
        volumes = [
            VolumeItem(name="data", mountPath="/data", pvc="my-pvc", readOnly=True, mountPropagation="None"),
            VolumeItem(name="cache", mountPath="/cache", emptyDir={}),
            VolumeItem(name="logs", mountPath="/var/log", hostPath="/var/log/nodes"),
            VolumeItem(name="cfg", mountPath="/etc/app", configMap="my-cm"),
            VolumeItem(name="certs", mountPath="/certs", secret="tls-secret"),
            VolumeItem(k8s={
                "volume": {"name": "nfs", "nfs": {"server": "nfs.example.com", "path": "/exports"}},
                "mount": {"mountPath": "/mnt/nfs"},
            }),
        ]
        self.vol_specs, self.mount_specs = build_volume_items(volumes)

    def test_pvc_volume_spec(self):
        assert any(
            v.get("persistentVolumeClaim", {}).get("claimName") == "my-pvc"
            for v in self.vol_specs
        )

    def test_pvc_mount_readonly(self):
        assert any(
            m.get("name") == "data" and m.get("readOnly") is True
            for m in self.mount_specs
        )

    def test_pvc_mount_propagation(self):
        assert any(
            m.get("name") == "data" and m.get("mountPropagation") == "None"
            for m in self.mount_specs
        )

    def test_emptydir_spec(self):
        assert any("emptyDir" in v and v["name"] == "cache" for v in self.vol_specs)

    def test_hostpath_string_to_dict(self):
        assert any(
            v.get("hostPath", {}).get("path") == "/var/log/nodes"
            for v in self.vol_specs
        )

    def test_configmap_string_to_dict(self):
        assert any(
            v.get("configMap", {}).get("name") == "my-cm"
            for v in self.vol_specs
        )

    def test_secret_string_to_dict(self):
        assert any(
            v.get("secret", {}).get("secretName") == "tls-secret"
            for v in self.vol_specs
        )

    def test_k8s_volume_preserved(self):
        assert any(v.get("name") == "nfs" and "nfs" in v for v in self.vol_specs)

    def test_k8s_mount_has_name(self):
        assert any(m.get("name") == "nfs" for m in self.mount_specs)


# ===========================================================================
# 5. ProjectDefinition — pvcs + semver validation
# ===========================================================================

class TestProjectDefinition:
    def test_pvcs_parsed(self):
        pd = ProjectDefinition(
            project="test-project",
            pvcs=[
                {"name": "shared-storage", "size": "50Gi", "storageClass": "nfs-client"},
                {"name": "db-data", "size": "20Gi"},
            ],
            apps=[],
        )
        assert len(pd.pvcs) == 2

    def test_pvc_name(self):
        pd = ProjectDefinition(project="test-project", pvcs=[{"name": "shared-storage", "size": "50Gi", "storageClass": "nfs-client"}], apps=[])
        assert pd.pvcs[0].name == "shared-storage"

    def test_pvc_storage_class(self):
        pd = ProjectDefinition(project="test-project", pvcs=[{"name": "shared-storage", "size": "50Gi", "storageClass": "nfs-client"}], apps=[])
        assert pd.pvcs[0].storageClass == "nfs-client"

    def test_pvc_default_access_modes(self):
        pd = ProjectDefinition(project="test-project", pvcs=[{"name": "db-data", "size": "20Gi"}], apps=[])
        assert pd.pvcs[0].accessModes == ["ReadWriteOnce"]

    def test_valid_semver_passes(self):
        pd = ProjectDefinition(project="test-project", common_version="1.2.3", apps=[])
        assert pd.common_version == "1.2.3"

    def test_valid_semver_with_patch_passes(self):
        pd = ProjectDefinition(project="test-project", common_version="2.10.0", apps=[])
        assert pd.common_version == "2.10.0"

    # Regex: ^\d+\.\d+\.\d+ — these strings do NOT match the prefix and should raise
    @pytest.mark.parametrize("bad_ver", ["v1.0.0", "1.0", "1", "latest", "v2.3.4"])
    def test_invalid_semver_raises(self, bad_ver):
        with pytest.raises(Exception, match="semver"):
            ProjectDefinition(project="test-project", common_version=bad_ver, apps=[])


# ===========================================================================
# 6. build_values_yaml — Integration
# ===========================================================================

class TestBuildValuesYamlIntegration:
    @pytest.fixture(autouse=True)
    def _setup(self, base_project):
        app = AppConfig(
            name="my-app",
            port=8080,
            envs=[
                EnvItem(name="ENV", value="prod"),
                EnvItem(secretEnv="test-proj-secret", vars=["DB_PASS"]),
                EnvItem(configMap="extra-cfg"),
                EnvItem(secret="extra-sec"),
            ],
            volumes=[
                VolumeItem(name="data", mountPath="/data", pvc="shared-pvc", readOnly=True),
                VolumeItem(name="cache", mountPath="/tmp/cache", emptyDir={}),
            ],
        )
        self.values = build_values_yaml(app, base_project, ({}, []))

    def test_plain_env_value(self):
        env = self.values["deployment"]["env"]
        assert any(e.get("name") == "ENV" and e.get("value") == "prod" for e in env)

    def test_secret_env_via_secretkeyref(self):
        env = self.values["deployment"]["env"]
        assert any(
            e.get("name") == "DB_PASS" and "secretKeyRef" in e.get("valueFrom", {})
            for e in env
        )

    def test_no_project_configmap_when_pool_empty(self):
        env_from = self.values["deployment"]["envFrom"]
        assert not any(
            e.get("configMapRef", {}).get("name") == "test-proj-config"
            for e in env_from
        )

    def test_extra_configmap_in_env_from(self):
        env_from = self.values["deployment"]["envFrom"]
        assert any(e.get("configMapRef", {}).get("name") == "extra-cfg" for e in env_from)

    def test_extra_secret_in_env_from(self):
        env_from = self.values["deployment"]["envFrom"]
        assert any(e.get("secretRef", {}).get("name") == "extra-sec" for e in env_from)

    def test_auto_tmp_emptydir_volume_present(self):
        vols = self.values["deployment"]["volumes"]
        assert any(v.get("name") == "tmp" for v in vols)

    def test_pvc_volume_ref(self):
        vols = self.values["deployment"]["volumes"]
        assert any(v.get("persistentVolumeClaim", {}).get("claimName") == "shared-pvc" for v in vols)

    def test_pvc_mount_readonly(self):
        mounts = self.values["deployment"]["volumeMounts"]
        assert any(m.get("name") == "data" and m.get("readOnly") is True for m in mounts)

    def test_emptydir_cache_volume(self):
        vols = self.values["deployment"]["volumes"]
        assert any("emptyDir" in v and v.get("name") == "cache" for v in vols)

    def test_image_repository(self):
        assert self.values["image"]["repository"] == "registry.vn/test/my-app"

    def test_image_tag(self):
        assert self.values["image"]["tag"] == "v1.0"


# ===========================================================================
# NEW: podSecurityContext vs securityContext separation
# ===========================================================================

class TestPodSecurityContext:
    """Verify pod-level and container-level security contexts are separate schemas."""

    def test_default_pod_sec_ctx_has_fsgroup(self, base_project):
        app = AppConfig(name="my-app", port=8080)
        values = build_values_yaml(app, base_project, ({}, []))
        pod_ctx = values["deployment"]["podSecurityContext"]
        assert pod_ctx.get("fsGroup") == 1000
        assert pod_ctx.get("runAsNonRoot") is True

    def test_user_pod_sec_ctx_merges_with_defaults(self, base_project):
        app = AppConfig(name="my-app", port=8080, podSecurityContext={"fsGroup": 2000, "supplementalGroups": [3000]})
        values = build_values_yaml(app, base_project, ({}, []))
        pod_ctx = values["deployment"]["podSecurityContext"]
        assert pod_ctx["fsGroup"] == 2000
        assert pod_ctx["supplementalGroups"] == [3000]
        assert pod_ctx["runAsNonRoot"] is True  # Default still applied

    def test_container_sec_ctx_has_readonly_filesystem(self, base_project):
        app = AppConfig(name="my-app", port=8080)
        values = build_values_yaml(app, base_project, ({}, []))
        container_ctx = values["deployment"]["securityContext"]
        assert container_ctx.get("readOnlyRootFilesystem") is True
        assert container_ctx.get("allowPrivilegeEscalation") is False

    def test_pod_ctx_does_not_have_readonly_filesystem(self, base_project):
        """readOnlyRootFilesystem is a container-level field, must NOT be in pod ctx."""
        app = AppConfig(name="my-app", port=8080)
        values = build_values_yaml(app, base_project, ({}, []))
        pod_ctx = values["deployment"]["podSecurityContext"]
        assert "readOnlyRootFilesystem" not in pod_ctx


# ===========================================================================
# NEW: ServiceAccount automountToken serialization
# ===========================================================================

class TestServiceAccountSerialization:
    def test_automount_token_serialized_as_k8s_field(self, base_project):
        app = AppConfig(name="my-app", port=8080)
        values = build_values_yaml(app, base_project, ({}, []))
        sa = values["serviceAccount"]
        # Must be 'automountServiceAccountToken' (K8s API name), NOT 'automountToken'
        assert "automountServiceAccountToken" in sa
        assert sa["automountServiceAccountToken"] is False


# ===========================================================================
# 7. BUG-1: env_vars — always inject even if not in secret_pool
# ===========================================================================

class TestLegacyEnvVars:
    def test_env_var_injected_when_not_in_pool(self):
        project = ProjectDefinition(project="proj-b1", image_tag="v1", apps=[])
        app = AppConfig(name="app-b1", port=8080, env_vars=["DB_URL", "API_KEY"])
        values = build_values_yaml(app, project, ({}, []))
        env = values["deployment"]["env"]
        assert any(
            e.get("name") == "DB_URL" and "secretKeyRef" in e.get("valueFrom", {})
            for e in env
        )

    def test_env_var_api_key_injected(self):
        project = ProjectDefinition(project="proj-b1", image_tag="v1", apps=[])
        app = AppConfig(name="app-b1", port=8080, env_vars=["DB_URL", "API_KEY"])
        values = build_values_yaml(app, project, ({}, []))
        env = values["deployment"]["env"]
        assert any(
            e.get("name") == "API_KEY" and "secretKeyRef" in e.get("valueFrom", {})
            for e in env
        )


# ===========================================================================
# 8. BUG-2: ingress + service=false → validation error
# ===========================================================================

class TestIngressServiceValidation:
    def test_ingress_with_service_false_raises(self):
        with pytest.raises(Exception, match="ingress.enabled=true requires a Service backend"):
            AppConfig(
                name="bad-app",
                port=8080,
                service=False,
                ingress={"enabled": True, "host": "example.com"},
            )


# ===========================================================================
# 9. LOGIC-2: Conditional project ConfigMap injection
# ===========================================================================

class TestConditionalConfigMap:
    def test_no_configmap_when_pool_empty(self):
        project = ProjectDefinition(project="proj-l2", image_tag="v1", apps=[])
        app = AppConfig(name="app-l2", port=8080)
        values = build_values_yaml(app, project, ({}, []))
        assert not any("configMapRef" in e for e in values["deployment"]["envFrom"])

    def test_configmap_injected_when_pool_has_data(self):
        project = ProjectDefinition(project="proj-l2", image_tag="v1", apps=[])
        app = AppConfig(name="app-l2", port=8080)
        values = build_values_yaml(app, project, ({"KEY": "val"}, []))
        assert any(
            e.get("configMapRef", {}).get("name") == "proj-l2-config"
            for e in values["deployment"]["envFrom"]
        )


# ===========================================================================
# 10. LOGIC-3: Nginx annotations merge
# ===========================================================================

class TestNginxAnnotations:
    @pytest.fixture
    def nginx_project(self):
        return ProjectDefinition(project="proj-l3", image_tag="v1", apps=[])

    def test_user_override_wins(self, nginx_project):
        app = AppConfig(
            name="app-l3", port=8080,
            ingress={
                "enabled": True,
                "host": "test.example.com",
                "className": "nginx",
                "annotations": {"nginx.ingress.kubernetes.io/ssl-redirect": "true"},
            },
        )
        values = build_values_yaml(app, nginx_project, ({}, []))
        assert values["ingress"]["annotations"].get("nginx.ingress.kubernetes.io/ssl-redirect") == "true"

    def test_default_proxy_body_size_present(self, nginx_project):
        app = AppConfig(
            name="app-l3", port=8080,
            ingress={
                "enabled": True,
                "host": "test.example.com",
                "className": "nginx",
                "annotations": {"nginx.ingress.kubernetes.io/ssl-redirect": "true"},
            },
        )
        values = build_values_yaml(app, nginx_project, ({}, []))
        assert "nginx.ingress.kubernetes.io/proxy-body-size" in values["ingress"]["annotations"]

    def test_non_nginx_no_default_annotations(self, nginx_project):
        app = AppConfig(
            name="traefik-app", port=8080,
            ingress={"enabled": True, "host": "t.example.com", "className": "traefik"},
        )
        values = build_values_yaml(app, nginx_project, ({}, []))
        assert "nginx.ingress.kubernetes.io/ssl-redirect" not in values["ingress"].get("annotations", {})


# ===========================================================================
# 11. K8s name validation
# ===========================================================================

class TestK8sNameValidation:
    def test_valid_name(self):
        assert _validate_k8s_name("my-app") == "my-app"

    def test_uppercase_raises(self):
        with pytest.raises(ValueError):
            _validate_k8s_name("MyApp")

    def test_underscore_raises(self):
        with pytest.raises(ValueError):
            _validate_k8s_name("my_app")

    def test_leading_dash_raises(self):
        with pytest.raises(ValueError):
            _validate_k8s_name("-myapp")

    def test_too_long_raises(self):
        with pytest.raises(ValueError):
            _validate_k8s_name("a" * 64)

    def test_appconfig_bad_name_raises(self):
        with pytest.raises(Exception):
            AppConfig(name="Bad_Name", port=8080)


# ===========================================================================
# 12. parse_env_file — quote handling
# ===========================================================================

class TestParseEnvFile:
    @pytest.fixture
    def env_file(self, tmp_path):
        f = tmp_path / "test.env"
        f.write_text(
            'DB_URL="postgres://user:pass@host:5432/db"\n'
            "API_SECRET='my-secret-value'\n"
            "PLAIN=hello=world\n"
            "VAULT_KEY=${vault:secret/data/key}\n"
            "# comment line\n"
        )
        return f

    def test_double_quoted_value(self, env_file):
        cfg, _ = parse_env_file(env_file)
        assert cfg.get("DB_URL") == "postgres://user:pass@host:5432/db"

    def test_single_quoted_value(self, env_file):
        cfg, _ = parse_env_file(env_file)
        assert cfg.get("API_SECRET") == "my-secret-value"

    def test_value_with_equals(self, env_file):
        cfg, _ = parse_env_file(env_file)
        assert cfg.get("PLAIN") == "hello=world"

    def test_vault_placeholder_is_secret(self, env_file):
        _, secrets = parse_env_file(env_file)
        assert "VAULT_KEY" in secrets

    def test_comment_ignored_and_count(self, env_file):
        cfg, _ = parse_env_file(env_file)
        assert "DB_URL" in cfg and len(cfg) == 3


# ===========================================================================
# NEW: _resolve_image helper
# ===========================================================================

class TestResolveImage:
    def test_simple_image_with_embedded_tag(self):
        name, tag = _resolve_image("redis:7.2-alpine", "registry.vn", "redis", None, None, "latest")
        assert name == "redis"
        assert tag == "7.2-alpine"

    def test_full_registry_path_with_tag(self):
        name, tag = _resolve_image(
            "registry.example.com/myteam/myapp:v1.2.3", "registry.vn", "myapp", None, None, "latest",
        )
        assert name == "registry.example.com/myteam/myapp"
        assert tag == "v1.2.3"

    def test_override_takes_priority_over_embedded(self):
        _, tag = _resolve_image("redis:7.0", "registry.vn", "redis", "8.0-override", None, "latest")
        assert tag == "8.0-override"

    def test_plain_image_uses_project_tag(self):
        _, tag = _resolve_image("my-registry/my-svc", "registry.vn", "my-svc", None, None, "v2.0")
        assert tag == "v2.0"

    def test_no_image_builds_from_repo_and_name(self):
        name, tag = _resolve_image(None, "registry.vn/platform", "my-app", None, None, "v1.0")
        assert name == "registry.vn/platform/my-app"
        assert tag == "v1.0"

    def test_item_tag_priority_over_project(self):
        _, tag = _resolve_image(None, "registry.vn", "app", None, "v1.2.3", "latest")
        assert tag == "v1.2.3"

    def test_override_always_wins(self):
        _, tag = _resolve_image(None, "registry.vn", "app", "override-tag", "item-tag", "proj-tag")
        assert tag == "override-tag"


# ===========================================================================
# 13. BUG-1: Image URI with embedded tag
# ===========================================================================

class TestImageURIWithEmbeddedTag:
    @pytest.fixture
    def latest_project(self):
        return ProjectDefinition(project="proj-img", image_tag="latest", apps=[])

    def test_simple_image_tag_repository(self, latest_project):
        app = AppConfig(name="my-cache", image="redis:7.2-alpine")
        v = build_values_yaml(app, latest_project, ({}, []), allow_latest=True)
        assert v["image"]["repository"] == "redis"

    def test_simple_image_tag_value(self, latest_project):
        app = AppConfig(name="my-cache", image="redis:7.2-alpine")
        v = build_values_yaml(app, latest_project, ({}, []), allow_latest=True)
        assert v["image"]["tag"] == "7.2-alpine"

    def test_full_path_image_repository(self, latest_project):
        app = AppConfig(name="my-app", image="registry.example.com/myteam/myapp:v1.2.3")
        v = build_values_yaml(app, latest_project, ({}, []))
        assert v["image"]["repository"] == "registry.example.com/myteam/myapp"

    def test_full_path_image_tag(self, latest_project):
        app = AppConfig(name="my-app", image="registry.example.com/myteam/myapp:v1.2.3")
        v = build_values_yaml(app, latest_project, ({}, []))
        assert v["image"]["tag"] == "v1.2.3"

    def test_cli_override_beats_embedded_tag(self, latest_project):
        app = AppConfig(name="my-app2", image="redis:7.0")
        v = build_values_yaml(app, latest_project, ({}, []), image_tag="8.0-override")
        assert v["image"]["tag"] == "8.0-override"

    def test_plain_image_uses_project_tag(self, latest_project):
        app = AppConfig(name="my-svc", image="my-registry/my-svc")
        v = build_values_yaml(app, latest_project, ({}, []), allow_latest=True)
        assert v["image"]["tag"] == "latest"

    def test_allow_latest_false_raises(self, latest_project):
        app = AppConfig(name="my-svc", image="my-registry/my-svc")
        with pytest.raises(ValueError, match="latest"):
            build_values_yaml(app, latest_project, ({}, []), allow_latest=False)

    def test_allow_latest_true_does_not_raise(self, latest_project):
        app = AppConfig(name="my-svc", image="my-registry/my-svc")
        v = build_values_yaml(app, latest_project, ({}, []), allow_latest=True)
        assert v["image"]["tag"] == "latest"


# ===========================================================================
# 14. BUG-2: auto /tmp mount deduplication
# ===========================================================================

class TestAutoMountTmpDedup:
    @pytest.fixture
    def tmp_project(self):
        return ProjectDefinition(project="proj-tmp", image_tag="v1", apps=[])

    def test_user_defined_tmp_no_duplicate_auto_volume(self, tmp_project):
        app = AppConfig(
            name="app-tmp", port=8080,
            volumes=[VolumeItem(name="ramdisk", mountPath="/tmp", emptyDir={"medium": "Memory"})],
        )
        v = build_values_yaml(app, tmp_project, ({}, []))
        names_at_tmp = [vol.get("name") for vol in v["deployment"]["volumes"] if vol.get("name") in ("tmp", "ramdisk")]
        assert len(names_at_tmp) == 1

    def test_user_defined_tmp_single_mount(self, tmp_project):
        app = AppConfig(
            name="app-tmp", port=8080,
            volumes=[VolumeItem(name="ramdisk", mountPath="/tmp", emptyDir={"medium": "Memory"})],
        )
        v = build_values_yaml(app, tmp_project, ({}, []))
        tmp_mounts = [m for m in v["deployment"]["volumeMounts"] if m.get("mountPath") == "/tmp"]
        assert len(tmp_mounts) == 1

    def test_user_defined_tmp_uses_custom_name(self, tmp_project):
        app = AppConfig(
            name="app-tmp", port=8080,
            volumes=[VolumeItem(name="ramdisk", mountPath="/tmp", emptyDir={"medium": "Memory"})],
        )
        v = build_values_yaml(app, tmp_project, ({}, []))
        tmp_mounts = [m for m in v["deployment"]["volumeMounts"] if m.get("mountPath") == "/tmp"]
        assert tmp_mounts[0]["name"] == "ramdisk"

    def test_no_user_tmp_auto_mount_added(self, tmp_project):
        app = AppConfig(name="app-notmp", port=8080)
        v = build_values_yaml(app, tmp_project, ({}, []))
        assert any(v.get("name") == "tmp" and "emptyDir" in v for v in v["deployment"]["volumes"])


# ===========================================================================
# 15. BUG-3: Port name uniqueness
# ===========================================================================

class TestPortNameUniqueness:
    @pytest.fixture
    def port_project(self):
        return ProjectDefinition(project="proj-port", image_tag="v1", apps=[])

    def test_port_names_all_unique(self, port_project):
        app = AppConfig(
            name="app-port",
            port=8080,
            ports=[ServicePort(name="http", port=9000), ServicePort(name="metrics", port=9100)],
        )
        v = build_values_yaml(app, port_project, ({}, []))
        names = [p["name"] for p in v["deployment"]["ports"]]
        assert len(names) == len(set(names))

    def test_shorthand_port_renamed_primary(self, port_project):
        app = AppConfig(
            name="app-port",
            port=8080,
            ports=[ServicePort(name="http", port=9000), ServicePort(name="metrics", port=9100)],
        )
        v = build_values_yaml(app, port_project, ({}, []))
        assert any(p["name"] == "primary" and p["containerPort"] == 8080 for p in v["deployment"]["ports"])

    def test_original_http_port_still_present(self, port_project):
        app = AppConfig(
            name="app-port",
            port=8080,
            ports=[ServicePort(name="http", port=9000), ServicePort(name="metrics", port=9100)],
        )
        v = build_values_yaml(app, port_project, ({}, []))
        assert any(p["name"] == "http" and p["containerPort"] == 9000 for p in v["deployment"]["ports"])


# ===========================================================================
# 16. LOGIC-1: storageClass empty string preserved
# ===========================================================================

class TestStorageClassEmptyString:
    def test_empty_string_renders_storageclass_key(self):
        pvc = ProjectPVC(name="no-class-pvc", size="5Gi", storageClass="")
        yaml_out = build_project_pvcs_yaml([pvc])
        assert "storageClassName" in yaml_out

    def test_empty_string_value_preserved(self):
        pvc = ProjectPVC(name="no-class-pvc", size="5Gi", storageClass="")
        yaml_out = build_project_pvcs_yaml([pvc])
        assert 'storageClassName: ""' in yaml_out or "storageClassName: ''" in yaml_out

    def test_none_storageclass_absent(self):
        pvc = ProjectPVC(name="default-class-pvc", size="5Gi")
        yaml_out = build_project_pvcs_yaml([pvc])
        assert "storageClassName" not in yaml_out


# ===========================================================================
# 17. LOGIC-2: No empty ConfigMap generation
# ===========================================================================

class TestNoEmptyConfigMap:
    def test_secrets_only_no_configmap_ref(self):
        project = ProjectDefinition(project="proj-l2b", image_tag="v1", apps=[])
        app = AppConfig(name="app-l2b", port=8080)
        values = build_values_yaml(app, project, ({}, ["VAULT_KEY"]))
        assert not any("configMapRef" in e for e in values["deployment"]["envFrom"])

    def test_config_and_secrets_has_configmap_ref(self):
        project = ProjectDefinition(project="proj-l2b", image_tag="v1", apps=[])
        app = AppConfig(name="app-l2b", port=8080)
        values = build_values_yaml(app, project, ({"APP_ENV": "prod"}, ["VAULT_KEY"]))
        assert any(
            e.get("configMapRef", {}).get("name") == "proj-l2b-config"
            for e in values["deployment"]["envFrom"]
        )


# ===========================================================================
# 18. Init Containers & Sidecars
# ===========================================================================

class TestInitContainersAndSidecars:
    @pytest.fixture(autouse=True)
    def _setup(self):
        project = ProjectDefinition(project="proj-extra", image_repo="my-registry", image_tag="v1", apps=[])
        app = AppConfig(
            name="my-app",
            port=8080,
            initContainers=[
                ExtraContainerConfig(
                    name="init-db",
                    image="busybox:1.36",
                    command=["sh", "-c", "echo waiting for db; sleep 2"],
                    envs=[EnvItem(name="DB_HOST", value="postgres")],
                ),
                ExtraContainerConfig(
                    name="native-sidecar",
                    image="log-exporter:latest",
                    restartPolicy="Always",
                ),
            ],
            sidecars=[
                ExtraContainerConfig(
                    name="proxy",
                    image_repo="envoyproxy",
                    image_tag="v1.25",
                    envs=[EnvItem(secretEnv="proxy-secret", vars=["API_KEY"])],
                )
            ],
        )
        v = build_values_yaml(app, project, ({"APP_ENV": "prod"}, ["API_KEY"]), allow_latest=True)
        self.init_c = v["deployment"]["initContainers"]
        self.side_c = v["deployment"]["sidecars"]

    def test_two_init_containers(self):
        assert len(self.init_c) == 2

    def test_init_db_correct_image(self):
        assert self.init_c[0]["image"] == "busybox:1.36"

    def test_init_db_correct_command(self):
        assert self.init_c[0]["command"] == ["sh", "-c", "echo waiting for db; sleep 2"]

    def test_init_db_has_explicit_env(self):
        assert self.init_c[0]["env"][0]["name"] == "DB_HOST"

    def test_init_db_no_liveness_probe(self):
        """CRITICAL: Classic init containers MUST NOT have probes (K8s rejects them)."""
        assert "livenessProbe" not in self.init_c[0]

    def test_init_db_no_readiness_probe(self):
        assert "readinessProbe" not in self.init_c[0]

    def test_init_db_no_startup_probe(self):
        assert "startupProbe" not in self.init_c[0]

    def test_init_db_no_auto_configmap_injection(self):
        """CRITICAL: Project ConfigMap must NOT be auto-injected into init containers."""
        assert not any("configMapRef" in e for e in self.init_c[0].get("envFrom", []))

    def test_init_db_no_forced_readonly_root_filesystem(self):
        """CRITICAL: Hardened container securityContext must NOT be forced onto init containers."""
        assert "readOnlyRootFilesystem" not in self.init_c[0].get("securityContext", {})

    def test_init_db_no_auto_tmp_mount(self):
        """CRITICAL: /tmp auto-mount must NOT be injected into init containers."""
        assert not any(m.get("mountPath") == "/tmp" for m in self.init_c[0].get("volumeMounts", []))

    def test_native_sidecar_restart_policy(self):
        assert self.init_c[1].get("restartPolicy") == "Always"

    def test_one_traditional_sidecar(self):
        assert len(self.side_c) == 1

    def test_proxy_sidecar_image(self):
        assert self.side_c[0]["image"] == "envoyproxy/proxy:v1.25"

    def test_proxy_sidecar_has_secret_env(self):
        assert any("secretKeyRef" in e.get("valueFrom", {}) for e in self.side_c[0]["env"])

    def test_proxy_sidecar_no_auto_configmap(self):
        assert not any("configMapRef" in e for e in self.side_c[0].get("envFrom", []))


# ===========================================================================
# 19. Job & CronJob
# ===========================================================================

class TestJobWorkload:
    @pytest.fixture
    def job_app(self):
        return AppConfig(
            name="db-migrate",
            type="job",
            image="myrepo/migrator:latest",
            job=JobConfig(backoffLimit=3, ttlSecondsAfterFinished=600, restartPolicy="OnFailure"),
            command=["/app/migrate", "--run"],
        )

    def test_job_type(self, batch_project, job_app):
        v = build_values_yaml(job_app, batch_project, ({}, []), allow_latest=True)
        assert v["type"] == "job"

    def test_job_backoff_limit(self, batch_project, job_app):
        v = build_values_yaml(job_app, batch_project, ({}, []), allow_latest=True)
        assert v["job"]["backoffLimit"] == 3

    def test_job_ttl_seconds(self, batch_project, job_app):
        v = build_values_yaml(job_app, batch_project, ({}, []), allow_latest=True)
        assert v["job"]["ttlSecondsAfterFinished"] == 600

    def test_job_restart_policy(self, batch_project, job_app):
        v = build_values_yaml(job_app, batch_project, ({}, []), allow_latest=True)
        assert v["job"]["restartPolicy"] == "OnFailure"

    def test_job_service_disabled(self, batch_project, job_app):
        v = build_values_yaml(job_app, batch_project, ({}, []), allow_latest=True)
        assert not v["service"]["enabled"]

    def test_job_ingress_disabled(self, batch_project, job_app):
        v = build_values_yaml(job_app, batch_project, ({}, []), allow_latest=True)
        assert not v["ingress"]["enabled"]

    def test_job_cronjob_dict_empty(self, batch_project, job_app):
        v = build_values_yaml(job_app, batch_project, ({}, []), allow_latest=True)
        assert v["cronjob"] == {}

    def test_job_with_ingress_raises(self):
        with pytest.raises(Exception):
            AppConfig(name="bad-job", type="job", image="img", ingress={"enabled": True, "host": "x.example.com"})


class TestCronJobWorkload:
    @pytest.fixture
    def cron_app(self):
        return AppConfig(
            name="backup-db",
            type="cronjob",
            image="myrepo/backup:v2",
            cronjob=CronJobConfig(schedule="0 2 * * *", concurrencyPolicy="Forbid", successfulJobsHistoryLimit=5, failedJobsHistoryLimit=2),
            job=JobConfig(backoffLimit=2),
        )

    def test_cronjob_type(self, batch_project, cron_app):
        v = build_values_yaml(cron_app, batch_project, ({}, []))
        assert v["type"] == "cronjob"

    def test_cronjob_schedule(self, batch_project, cron_app):
        v = build_values_yaml(cron_app, batch_project, ({}, []))
        assert v["cronjob"]["schedule"] == "0 2 * * *"

    def test_cronjob_concurrency_policy(self, batch_project, cron_app):
        v = build_values_yaml(cron_app, batch_project, ({}, []))
        assert v["cronjob"]["concurrencyPolicy"] == "Forbid"

    def test_cronjob_job_backoff_limit(self, batch_project, cron_app):
        v = build_values_yaml(cron_app, batch_project, ({}, []))
        assert v["job"]["backoffLimit"] == 2

    def test_cronjob_service_disabled(self, batch_project, cron_app):
        v = build_values_yaml(cron_app, batch_project, ({}, []))
        assert not v["service"]["enabled"]

    def test_cronjob_without_schedule_raises(self):
        with pytest.raises(Exception):
            AppConfig(name="bad-cron", type="cronjob", image="img")


# ===========================================================================
# 20. HorizontalPodAutoscaler (HPA)
# ===========================================================================

class TestHPA:
    @pytest.fixture
    def hpa_project(self):
        return ProjectDefinition(project="proj-hpa", image_tag="v1", apps=[])

    def test_hpa_disabled_by_default(self, hpa_project):
        app = AppConfig(name="no-hpa-svc", port=8080, image="myrepo/app:v1.0")
        v = build_values_yaml(app, hpa_project, ({}, []))
        assert not v["hpa"]["enabled"]

    def test_hpa_disabled_replicas_present_in_deployment(self, hpa_project):
        app = AppConfig(name="no-hpa-svc", port=8080, image="myrepo/app:v1.0")
        v = build_values_yaml(app, hpa_project, ({}, []))
        assert "replicas" in v["deployment"]

    def test_hpa_enabled_flag(self, hpa_project):
        app = AppConfig(
            name="api-svc", port=8080, image="myrepo/api:v2.0",
            hpa=HPAConfig(enabled=True, minReplicas=2, maxReplicas=10, targetCPUUtilizationPercentage=70),
        )
        v = build_values_yaml(app, hpa_project, ({}, []))
        assert v["hpa"]["enabled"] is True

    def test_hpa_min_replicas(self, hpa_project):
        app = AppConfig(
            name="api-svc", port=8080, image="myrepo/api:v2.0",
            hpa=HPAConfig(enabled=True, minReplicas=2, maxReplicas=10, targetCPUUtilizationPercentage=70),
        )
        v = build_values_yaml(app, hpa_project, ({}, []))
        assert v["hpa"]["minReplicas"] == 2

    def test_hpa_max_replicas(self, hpa_project):
        app = AppConfig(
            name="api-svc", port=8080, image="myrepo/api:v2.0",
            hpa=HPAConfig(enabled=True, minReplicas=2, maxReplicas=10, targetCPUUtilizationPercentage=70),
        )
        v = build_values_yaml(app, hpa_project, ({}, []))
        assert v["hpa"]["maxReplicas"] == 10

    def test_hpa_replicas_omitted_from_deployment_when_enabled(self, hpa_project):
        """CRITICAL: replicas must be absent from deployment dict when HPA is enabled."""
        app = AppConfig(
            name="api-svc", port=8080, image="myrepo/api:v2.0",
            hpa=HPAConfig(enabled=True, minReplicas=2, maxReplicas=10, targetCPUUtilizationPercentage=70),
        )
        v = build_values_yaml(app, hpa_project, ({}, []))
        assert "replicas" not in v["deployment"]

    def test_hpa_memory_target(self, hpa_project):
        app = AppConfig(
            name="worker-svc", port=8080, image="myrepo/worker:v1",
            hpa=HPAConfig(enabled=True, minReplicas=1, maxReplicas=5, targetMemoryUtilizationPercentage=80),
        )
        v = build_values_yaml(app, hpa_project, ({}, []))
        assert v["hpa"]["targetMemoryUtilizationPercentage"] == 80

    def test_hpa_on_job_type_raises(self):
        with pytest.raises(Exception):
            AppConfig(
                name="bad-job-hpa", type="job", image="img:v1",
                hpa=HPAConfig(enabled=True, targetCPUUtilizationPercentage=80),
            )

    def test_hpa_on_cronjob_type_raises(self):
        with pytest.raises(Exception):
            AppConfig(
                name="bad-cron-hpa", type="cronjob", image="img:v1",
                cronjob=CronJobConfig(schedule="0 2 * * *"),
                hpa=HPAConfig(enabled=True, targetCPUUtilizationPercentage=80),
            )

    def test_hpa_without_metric_target_raises(self):
        with pytest.raises(Exception):
            AppConfig(
                name="no-metric-hpa", port=8080, image="img:v1",
                hpa=HPAConfig(enabled=True),  # No CPU or Memory target
            )


# ===========================================================================
# 21. PY-H3 FIX: Registry with port number in image URI
# ===========================================================================

class TestImageRegistryWithPort:
    """Verify _resolve_image and build_values_yaml handle registries with port numbers."""

    def test_resolve_image_registry_with_port(self):
        """registry:5000/app:v1 must NOT be split as 'registry' (old bug)."""
        name, tag = _resolve_image(
            "registry:5000/my-app:v2.0", "fallback.io", "app", None, None, "latest",
        )
        assert name == "registry:5000/my-app"
        assert tag == "v2.0"

    def test_resolve_image_registry_with_port_no_tag(self):
        name, tag = _resolve_image(
            "myregistry:5000/team/service", "fallback.io", "app", None, None, "v1.0",
        )
        assert name == "myregistry:5000/team/service"
        assert tag == "v1.0"

    def test_resolve_image_registry_port_with_override(self):
        name, tag = _resolve_image(
            "registry:5000/my-app:v2.0", "fallback.io", "app", "override-tag", None, "latest",
        )
        assert name == "registry:5000/my-app"
        assert tag == "override-tag"

    def test_build_values_registry_port_repository(self):
        """End-to-end: build_values_yaml must produce correct image.repository."""
        project = ProjectDefinition(project="proj-rp", image_tag="v1", apps=[])
        app = AppConfig(name="svc-rp", image="my-registry:5000/team/my-svc:v3.0", port=8080)
        v = build_values_yaml(app, project, ({}, []))
        assert v["image"]["repository"] == "my-registry:5000/team/my-svc"
        assert v["image"]["tag"] == "v3.0"


# ===========================================================================
# 22. PY-H1 FIX: _DEFAULT_CONTAINER_SEC_CTX constant
# ===========================================================================

class TestDefaultContainerSecCtx:
    """Verify the extracted constant is used consistently."""

    def test_constant_imported(self):
        from scripts.generator import _DEFAULT_CONTAINER_SEC_CTX
        assert _DEFAULT_CONTAINER_SEC_CTX["readOnlyRootFilesystem"] is True
        assert _DEFAULT_CONTAINER_SEC_CTX["allowPrivilegeEscalation"] is False
        assert _DEFAULT_CONTAINER_SEC_CTX["capabilities"] == {"drop": ["ALL"]}

    def test_main_container_uses_constant(self, base_project):
        app = AppConfig(name="ctx-test", port=8080)
        v = build_values_yaml(app, base_project, ({}, []))
        ctx = v["deployment"]["securityContext"]
        assert ctx["readOnlyRootFilesystem"] is True
        assert ctx["allowPrivilegeEscalation"] is False
        assert ctx["runAsNonRoot"] is True

    def test_user_override_merges_with_constant(self, base_project):
        app = AppConfig(name="ctx-override", port=8080, securityContext={"runAsUser": 2000})
        v = build_values_yaml(app, base_project, ({}, []))
        ctx = v["deployment"]["securityContext"]
        assert ctx["runAsUser"] == 2000  # User override wins
        assert ctx["readOnlyRootFilesystem"] is True  # Default still applied


# ===========================================================================
# 23. SEC-H3 FIX: Resource limits warning
# ===========================================================================

class TestResourceLimitsWarning:
    """Verify warning is printed when resources are not defined."""

    def test_no_resources_prints_warning(self, base_project, capsys):
        app = AppConfig(name="no-res-app", port=8080)
        build_values_yaml(app, base_project, ({}, []))
        captured = capsys.readouterr()
        assert "no resource limits defined" in captured.out

    def test_with_resources_no_warning(self, base_project, capsys):
        app = AppConfig(
            name="res-app", port=8080,
            resources={"limits": {"cpu": "500m", "memory": "512Mi"}},
        )
        build_values_yaml(app, base_project, ({}, []))
        captured = capsys.readouterr()
        assert "no resource limits defined" not in captured.out
