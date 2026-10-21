"""A complete, valid deploy env-file for the deploy/endpoint.sh tests (the required keys)."""

DIGEST = "@sha256:" + "c" * 64
REQUIRED = {
    "S3_ENDPOINT": "https://storage.eu-north1.nebius.cloud",
    "P2P_STATE_BUCKET": "p2p-state",
    "P2P_CONTROL_BUCKET": "p2p-bundles",
    "NEBIUS_SANDBOX_PROJECT_ID": "project-sandbox",
    "P2P_PROVISIONER_IMAGE": "cr.eu-north1.nebius.cloud/reg/p2p-provisioner" + DIGEST,
    "P2P_RUNNER_IMAGE": "cr.eu-north1.nebius.cloud/reg/p2p-runner" + DIGEST,
    "P2P_RUNNER_CPU_IMAGE": "cr.eu-north1.nebius.cloud/reg/p2p-runner-cpu" + DIGEST,
    "P2P_SUBNET_PROVISION": "vpcsubnet-provision",
    "P2P_SUBNET_EXEC": "vpcsubnet-exec",
    "P2P_GPU_PREEMPTIBLE": "true",
    "P2P_BUNDLE_KEY_ID": "kms-key-bundles",
}
ENV_LINES = "".join(f"{key}={value}\n" for key, value in REQUIRED.items())
