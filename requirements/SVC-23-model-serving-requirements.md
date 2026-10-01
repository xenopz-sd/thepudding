# SVC-23 — Model Serving / Inference Endpoint Requirements

## 1. Overview

This service provides GPU-backed inference endpoints for machine-learning workloads: large language model (LLM) serving for ML-text projects and computer-vision (CV) model serving for ML-video/image projects. It is a Tier 1 core service for any project whose product surface includes a model that answers requests in real time (chat/completion APIs, vision pipelines processing drone or camera footage). Embedded/drone projects may also depend on it indirectly when on-prem inference (e.g., object detection on captured imagery) feeds back into fleet decision-making. Generic backend projects without an ML component do not need this service.

## 2. Scope

**In scope:**
- vLLM as the LLM inference server, OpenAI-compatible API surface.
- Triton Inference Server as the CV model inference server (ONNX/TensorRT/PyTorch backends).
- LocalAI as an optional, genuinely European alternative for lighter/CPU-bound inference where reducing non-EU footprint outweighs raw throughput.
- GPU passthrough/vGPU configuration on the underlying Proxmox VM.
- Exposure of inference endpoints via Traefik (SVC-09) with authentication via ZITADEL (SVC-06).
- Per-project dedicated inference endpoint provisioning pattern.

**Out of scope:**
- Model training/fine-tuning infrastructure (not covered by this catalog entry).
- Experiment tracking and model registry (SVC-24, MLflow).
- Dataset/annotation storage (SVC-26).
- GPU job scheduling for batch/offline workloads across multiple projects (SVC-27, Slurm) — this doc covers only always-on, request/response inference endpoints.

## 3. Technology selection

- **vLLM** (Apache-2.0) — default LLM serving engine, matching the user's existing usage. 🚩 Originated at UC Berkeley, US; the project and its primary maintainers remain US-based.
- **Triton Inference Server** (BSD-3-Clause) — default CV model serving engine. 🚩 NVIDIA Corporation, US.
- **No European alternative at comparable GPU-inference maturity was found for either workload.** This is flagged explicitly per the catalog's sourcing policy (catalog §3.5) rather than silently defaulting to a US vendor: the GPU-inference serving space (continuous batching, PagedAttention-style KV cache management, TensorRT backend integration) is currently dominated by vLLM and NVIDIA's own stack, and no EU-governed project matches their throughput/latency characteristics or hardware-vendor integration depth today.
- **LocalAI** (MIT license) — noted as a genuinely European alternative. Created by **Ettore Di Giacinto, Italy**. LocalAI is not the default because it is optimized for lighter, CPU-bound, or single-request inference rather than high-throughput concurrent GPU serving; it is called out here as the option to reach for on a project-by-project basis where reducing non-EU software footprint is a stated priority that outweighs raw throughput (e.g., a low-traffic internal tool, or a project with an explicit EU-sovereignty requirement).
- This selection and its rationale are carried directly from catalog §3.G (SVC-23) and are not re-litigated here.

## 4. Multi-tenancy model

**Dedicated per-project inference endpoint** — each project gets its own container (its own vLLM or Triton process) and its own GPU allocation or time-slice, rather than one shared multi-tenant server. This is a deliberate departure from the "shared platform service" default described in catalog §1, justified because:
- GPU memory is a hard-partitioned resource: two projects' models loaded into the same vLLM process would either contend for VRAM or require the operational complexity of dynamic model swapping, which harms both projects' latency predictability.
- Projects use different model versions, quantization formats, and serving configurations (`--max-model-len`, `--gpu-memory-utilization`, TensorRT engine builds specific to one model) that do not compose cleanly in a single shared process.
- Blast-radius containment: a crashed or misbehaving inference process (OOM, CUDA error) should not take down another project's inference capability.

Isolation is implemented as: one Compose stack per project (`inference-<slug>`), each bound to its own GPU or GPU time-slice (see §7), each with its own Traefik router rule (`inference-<slug>.internal.example.org` or a path prefix) and its own ZITADEL-registered OAuth2 client for endpoint auth. On a cluster with a single physical GPU, projects share the card via **vGPU** partitioning or **time-sliced** scheduling (documented in §7) rather than sharing a serving process — the isolation boundary stays at the container/process level even when the underlying silicon is shared.

## 5. Functional requirements

- **FR-1:** vLLM exposes an OpenAI-compatible REST API (`/v1/chat/completions`, `/v1/completions`, `/v1/models`) on port 8000 per project instance.
- **FR-2:** Triton exposes HTTP inference on port 8000, gRPC inference on port 8001, and a Prometheus metrics endpoint on port 8002 per project instance (Triton's three default ports); where a project runs both vLLM and Triton, port offsets are applied per-project to avoid collisions on the shared host.
- **FR-3:** Each inference endpoint is reachable only via Traefik (SVC-09), which terminates TLS and forwards to the internal Compose network — no direct external exposure of vLLM/Triton ports.
- **FR-4:** Every inference request is authenticated via a ZITADEL (SVC-06)-issued OAuth2/OIDC token, validated at the Traefik layer via `forwardAuth` middleware (or an API-gateway sidecar) before reaching the model container.
- **FR-5:** Model weights are loaded from a per-project volume populated from SVC-03 (Garage/S3-compatible object storage) at container startup, not baked into the image, so model updates do not require an image rebuild.
- **FR-6:** vLLM instances expose `--max-model-len` and `--gpu-memory-utilization` as Ansible-templated variables per project, set according to the model and available VRAM.
- **FR-7:** Triton model repositories follow the standard `models/<model_name>/<version>/model.*` + `config.pbtxt` layout, synced from SVC-03 or a project's Git repo via the CI pipeline.
- **FR-8:** LocalAI, where selected as the alternative for a lighter-weight project, exposes the same OpenAI-compatible API shape on port 8080 (LocalAI's default) so client code is portable between the vLLM and LocalAI backends.
- **FR-9:** Each endpoint exposes a `/health` or `/v2/health/ready` (Triton) liveness/readiness probe consumed by both Traefik health checks and Prometheus blackbox-style monitoring.
- **FR-10:** GPU allocation (whole-card passthrough, vGPU slice, or time-slice) is declared per project in Terraform and is visible in the project's Ansible inventory as a `gpu_allocation` variable.

## 6. Non-functional requirements

- **Availability target:** 95–99% depending on project criticality; inference endpoints are not expected to be five-nines given the small-cluster GPU budget, and a single-GPU host means a hardware failure is a real, accepted risk at this scale (mitigated by keeping model weights and configs reproducible via IaC, not by hot failover).
- **Performance/sizing** (small homelab/office cluster, realistically 1–2 physical GPUs shared across projects):
  - **vLLM**, per project, for an 8B-class open-weight LLM (e.g., Qwen3-8B, Llama-3.1-8B) in BF16: minimum 16–18 GB VRAM, practical comfortable minimum 24 GB VRAM (e.g., a single RTX 3090/4090-class card or vGPU slice) with room for KV cache and concurrent requests; 32 GB system RAM per instance; 50 GB disk for model weights + container image ([vLLM hardware requirements](https://localaimaster.com/blog/vllm-complete-setup-guide), [vLLM on a single consumer GPU](https://craftrigs.com/guides/vllm-single-gpu-consumer-setup-guide/)). Larger models (30B+) or 4-bit quantization change this math; each project's requirements doc should size against the specific model chosen.
  - **Triton**, per project, for typical CV models (ONNX/TensorRT object-detection or classification models): minimum 8 GB VRAM, 16 GB system RAM, 50 GB disk for the container image and model repository ([Triton prerequisites](https://docs.clore.ai/guides/mlops-and-deployment/triton-inference-server), [Triton system requirements](https://perlod.com/tutorials/ai-inference-with-triton-server/)).
  - **LocalAI**, for CPU-bound light inference: 4 vCPU, 8–16 GB RAM, no GPU required; sized per model as with any llama.cpp-based runtime.
- **Backup/DR:** model weights are reproducible from SVC-03/Git (source of truth), so the inference container itself is treated as ephemeral/stateless from a backup perspective; only the per-project Compose/Ansible config and any fine-tuned adapters are backed up via SVC-32.
- **Data retention:** inference request/response logs (if enabled for debugging) are retained 14 days by default in Loki (SVC-18) and are not treated as durable data unless a project explicitly needs request auditing.

## 7. Infrastructure architecture

- **Compute unit:** Proxmox **VM** (not LXC) per project inference endpoint — GPU passthrough/vGPU requires full kernel-level device access, which is only supported cleanly at the VM level per F2. This is a hard requirement, not a preference: LXC GPU sharing exists but lacks the driver-isolation guarantees needed for a dedicated per-project GPU allocation.
- **GPU passthrough options** (choice depends on GPU hardware and how many projects need concurrent access):
  - **Full PCIe passthrough (VFIO):** the entire physical GPU is bound to one VM via `vfio-pci`; requires IOMMU (Intel VT-d / AMD-Vi) enabled in BIOS, the GPU in its own IOMMU group, and a q35/OVMF (UEFI) VM configuration ([Proxmox PCI Passthrough docs](https://pve.proxmox.com/wiki/PCI_Passthrough)). Simplest and highest-performance option; the practical default for a homelab/office cluster with one GPU per demanding project, at the cost of that VM being unable to live-migrate and the host losing access to the card.
  - **NVIDIA vGPU:** since NVIDIA vGPU Software 18, Proxmox VE is an officially supported vGPU platform, allowing one supported datacenter/pro GPU (e.g., RTX A5000, RTX PRO 6000 Blackwell) to be split into multiple mediated devices assigned to separate VMs — the right choice when multiple projects must share one physical card concurrently ([NVIDIA vGPU on Proxmox VE](https://pve.proxmox.com/wiki/NVIDIA_vGPU_on_Proxmox_VE)). Requires a supported GPU from NVIDIA's vGPU hardware list and an NVIDIA vGPU license; consumer GeForce cards do not support vGPU.
  - **Default recommendation for this cluster:** full passthrough, since most homelab/office GPU inventory is consumer-grade (GeForce-class) hardware that does not support vGPU; vGPU is the documented upgrade path if a datacenter-class card (A-series/L-series or newer) is later procured and multiple simultaneous projects need to share it.
- **Minimum resource spec per project VM:** 4 vCPU, 32 GB RAM, 100 GB disk (local-zfs) plus the passed-through/allocated GPU, per the sizing in §6.
- **Network placement:** dedicated "ml-inference" VLAN; static IP/DHCP reservation per project VM. Only Traefik (SVC-09) and the observability VM (SVC-17/18, for scraping) may reach the inference VLAN directly; all external/application traffic goes through Traefik.
- **Storage:** local-zfs (fast NVMe preferred) for model weight caching; model weights fetched from SVC-03 (Garage) on provisioning and cached locally to avoid repeated network loads on restart.

## 8. Terraform scope

- **Module inputs:** `project_slug`, `vm_name`, `vlan_id` (ml-inference), `cpu_cores` (4), `memory_mb` (32768), `disk_gb` (100), `gpu_allocation` (`passthrough` | `vgpu-slice` | `none`), `gpu_pci_id` (for passthrough) or `mdev_type` (for vGPU), `serving_engine` (`vllm` | `triton` | `localai`).
- **Resources created:** one `proxmox_virtual_environment_vm` per project inference endpoint (via `bpg/proxmox`), with `hostpci` block configured for GPU passthrough or PCI resource-mapping reference for vGPU; firewall rules restricting inbound access to the Traefik VM and observability VM only.
- **Outputs:** `inference_vm_ip`, `inference_internal_port` (8000 for vLLM/Triton HTTP, 8080 for LocalAI), `traefik_router_hostname`, consumed by the Traefik dynamic configuration and by the project's application code (via ZITADEL-issued client credentials, not a hardcoded URL).

## 9. Ansible scope

- **Roles:** `gpu_host_prep` (installs NVIDIA host driver, configures VFIO or vGPU per `gpu_allocation`), `vllm_serving` (templates `docker-compose.yml` and vLLM launch flags), `triton_serving` (templates Triton's Compose stack and model-repository sync job), `localai_serving` (lighter CPU-only alternative role).
- **Idempotency:** GPU driver installation and VFIO/vGPU binding steps are guarded by fact checks (`nvidia-smi` output, `lspci -k` driver-in-use check) so re-runs do not attempt redundant driver installs; Compose stack redeploys only on a config or model-version hash change.
- **Config files templated:** vLLM launch command/environment (model name, `--max-model-len`, `--gpu-memory-utilization`, `--port`), Triton `config.pbtxt` per model, Traefik dynamic config snippet (router + service definition) per inference endpoint.
- **Secrets injected from SVC-07:** ZITADEL OAuth2 client secret for endpoint auth, SVC-03 (Garage) access key/secret for model-weight download, optional Hugging Face token for gated model downloads.

## 10. CI/CD pipeline

- **Stages:** `lint` (Dockerfile/Compose lint if a custom image wraps vLLM/Triton, `config.pbtxt` validation for Triton) → `plan` (Terraform plan for the project's inference VM) → `manual approve` (GPU allocation changes always require manual approval, given hardware scarcity) → `apply` → `configure` (Ansible) → `smoke test` (send a test completion request to vLLM's `/v1/completions` or a test inference request to Triton's `/v2/models/<model>/infer`, verify a 200 response and expected output shape).
- **Where it runs:** GitLab CI via GitLab Runner (SVC-13); GPU-dependent smoke tests run on a runner with access to the ml-inference VLAN.
- **State backend:** GitLab-managed Terraform state (F1), one state per project inference endpoint (consistent with the dedicated-per-project tenancy model).

## 11. Secrets & credentials

- **ZITADEL OAuth2 client credentials** for the inference endpoint — generated per project during SVC-06 client registration, stored in OpenBao at `secret/inference/<slug>/oauth-client`, rotated on a 180-day cadence.
- **SVC-03 (Garage) access key/secret** for model-weight bucket access — scoped read-only to the project's model bucket, stored in OpenBao.
- **Hugging Face token** (if gated models are used) — stored in OpenBao, injected as an environment variable at container start, never baked into the image.
- **NVIDIA vGPU license token** (only if vGPU is used) — stored in OpenBao, applied to the license server or the guest driver per NVIDIA's licensing flow.

## 12. Security & hardening baseline

- All inference traffic terminates TLS at Traefik (SVC-09); the vLLM/Triton container ports (8000/8001/8002 or 8080) are never directly internet-facing.
- Every request requires a valid ZITADEL-issued token, checked via Traefik `forwardAuth` middleware — no anonymous inference access.
- Least-privilege: inference containers run with only the GPU device(s) and model-weight volume mounted; no access to other projects' volumes or secrets.
- Firewall/VLAN rules: only Traefik and the observability VM may reach the ml-inference VLAN; inter-project traffic within the VLAN is denied by default.
- CVE scanning: `vllm/vllm-openai` and `nvcr.io/nvidia/tritonserver` images are pinned to specific version tags (not `:latest` or floating `-py3` tags), rescanned monthly via Harbor/Trivy (SVC-14); NVIDIA NGC images are mirrored into the internal Harbor registry rather than pulled directly from `nvcr.io` at deploy time, to keep a scanned, versioned copy of record.

## 13. Observability hooks

- vLLM exposes a Prometheus-compatible `/metrics` endpoint (request latency, token throughput, KV cache utilization, queue depth) scraped by SVC-17.
- Triton exposes its own Prometheus `/metrics` on port 8002 (inference count, latency percentiles, GPU utilization per model) scraped by SVC-17.
- `dcgm-exporter` (see SVC-17/18 doc) runs on every GPU-passthrough/vGPU host to provide card-level utilization, memory, temperature, and ECC-error metrics alongside the process-level metrics above.
- Inference request/error logs are shipped via Promtail to Loki (SVC-18) with `project` and `service=<vllm|triton|localai>` labels.
- Key alerts to define in Alertmanager: endpoint down, GPU memory utilization sustained >95% (risk of OOM-kill), request error rate >5% over 5 minutes, GPU ECC error nonzero, inference latency p95 above a per-project SLA threshold.

## 14. Acceptance criteria

- [ ] Project inference VM is provisioned with the correct GPU allocation (passthrough or vGPU slice) and `nvidia-smi` reports the GPU inside the guest.
- [ ] vLLM or Triton (or LocalAI, if selected) starts successfully, loads the project's model from SVC-03, and passes its health/readiness check.
- [ ] The endpoint is reachable only via Traefik with a valid TLS certificate and rejects requests without a valid ZITADEL token.
- [ ] A test inference request returns a correct, well-formed response within the project's expected latency budget.
- [ ] Prometheus is scraping the endpoint's `/metrics` and `dcgm-exporter` GPU metrics; a Grafana dashboard shows both.
- [ ] Terraform/Ansible pipeline runs idempotently with no drift on a second apply.

## 15. Open questions / assumptions

- Assumed the cluster currently has consumer-grade (GeForce-class) GPU hardware, making full PCIe passthrough the default; this should be revisited if a datacenter-class NVIDIA card is procured, at which point vGPU partitioning becomes viable for sharing across projects.
- Exact per-project VRAM/RAM sizing depends entirely on the chosen model and quantization — the figures in §6 are representative for common 8B-class open-weight models and must be re-derived per project.
- Assumed LocalAI is selected on a case-by-case, project-requested basis rather than being pre-provisioned platform-wide; a human should confirm this opt-in model versus making it a default lightweight tier.
- NVIDIA vGPU licensing cost and procurement is not addressed here and would need budget approval before that path is exercised.
