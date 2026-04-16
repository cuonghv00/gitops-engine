# Walkthrough - Test Case & Convention Standardization

We have standardized the naming convention for application definitions and created a robust test suite to verify the generator's intelligence and flexibility.

## Changes Made

### 1. New Naming Convention
The generator script now defaults to looking for `apps.<env>.yaml`.
- **New Flag**: `--env` (Default: `dev`).
- **Dynamic Selection**: `python3 generator.py --project X` will now automatically look for `projects/X/apps.dev.yaml`.

### 2. Standardized Test Cases
We created two new projects to serve as benchmarks:

#### [single-app](file:///home/cuong/DevOps/k8s-helm-monorepo-automation/projects/single-app/apps.dev.yaml)
- **Objective**: Test absolute minimal configuration.
- **Content**: Only `name: "minimal-service"`.
- **Result**: Successfully generated a full Helm chart with smart defaults for all fields.

#### [multi-apps](file:///home/cuong/DevOps/k8s-helm-monorepo-automation/projects/multi-apps/apps.dev.yaml)
- **Objective**: Test different complexity levels in a single project.
- **Content**: 
    - `minimal-app`: Zero-config app.
    - `normal-app`: Standard web app (port + ingress).
    - `full-app`: Advanced app overriding resources, security, env, and volumes.
- **Result**: Successfully generated 3 distinct charts, each perfectly reflecting the intended complexity.

## Verification Results

### Environment Flexibility
The `--env` flag was verified to correctly load different files based on the environment name.

### Intelligence Validation
- **Smart Port Mapping**: Verified that `normal-app` used port `8080` for its health probes because that was the service port.
- **Image Construction**: Verified that the repository was correctly built as `registry.vn/test/<app_name>`.
- **Security Injection**: Verified that hardening defaults were injected into the minimal apps but allowed overrides in the `full-app`.

> [!TIP]
> You can now test your production setup by creating `apps.prod.yaml` and running with `--env prod`.
