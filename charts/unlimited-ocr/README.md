# unlimited-ocr Helm chart

Runs [unlimited-ocr-container](https://github.com/fabceolin/unlimited-ocr-container) as a Kubernetes `Job` on k3s. Assumes a single-node cluster with `hostPath` storage and (for GPU) the `nvidia` `RuntimeClass`/containerd runtime already configured on the node.

All releases below live in a dedicated `ocr` namespace (not `default`) — add
`-n ocr --create-namespace` to every `helm install`/`upgrade` (shown inline below).

## 1. One-time: GPU device plugin

Only needed once per cluster, and only if `nvidia.com/gpu` isn't already schedulable (`kubectl describe node | grep nvidia.com/gpu`):

```bash
helm install ocr-gpu-infra ./charts/unlimited-ocr -n ocr --create-namespace \
  --set gpu.installDevicePlugin=true \
  --set job.enabled=false
```

### GPU partitioning with Ollama (this cluster)

`dev` also runs an [Ollama](https://ollama.com) deployment (`ollama` namespace, `ollama-helm/ollama`
chart) that permanently reserves the Tesla P40 + 1 RTX 3060 (index 0), outside of this chart's
device-plugin pool entirely. Ollama gets those 2 GPUs via `runtimeClassName: nvidia` +
`NVIDIA_VISIBLE_DEVICES=0,4` set directly on its container (no `nvidia.com/gpu` k8s resource
request at all) — validated empirically to restrict container GPU visibility at the CDI/cgroup
level, independent of whatever the app does internally.

This chart's device plugin is correspondingly restricted to the 3 *remaining* RTX 3060s:

```bash
helm install ocr-gpu-infra ./charts/unlimited-ocr -n ocr --create-namespace \
  --set gpu.installDevicePlugin=true \
  --set job.enabled=false \
  --set gpu.devicePlugin.visibleDevices="1\,2\,3"
```

Why not per-GPU-model resource splitting (e.g. `nvidia.com/gpu-p40` vs `nvidia.com/gpu-rtx3060`)
via the device plugin's own config? Tested directly against `nvcr.io/nvidia/k8s-device-plugin`
v0.15.0-rc.2 *and* v0.17.4 — the documented-looking `resources.gpus[].pattern` config is not
actually implemented (logs `"Customizing the 'resources' field is not yet supported in the
config. Ignoring..."` and silently falls back to one flat pool). The only genuinely working
resource-renaming path (`sharing.timeSlicing`) requires `replicas >= 2`, i.e. real GPU sharing/
oversubscription — not a plain 1:1 rename. Hence the explicit `visibleDevices` split above instead.

### Local registry mirror (this cluster)

`dev` runs a plain `registry:2` container (`docker run -d --name local-registry --restart=always
-p 5000:5000 -v registry-data:/var/lib/registry registry:2`) as a **persistent local cache**, not a
transparent proxy: `/etc/rancher/k3s/registries.yaml` configures `ghcr.io` with
`localhost:5000` as the first mirror endpoint and real `ghcr.io` as fallback, so:

- Any image already pushed to `localhost:5000/<same path as ghcr.io>` resolves instantly from disk
  (validated: a 23GB image pull went from re-downloading over the internet to 36ms).
- Anything not mirrored locally transparently falls through to the real `ghcr.io`.

This replaced the earlier workaround of `docker save | sudo k3s ctr images import -` for every
locally-built or re-pulled image, which had no persistence guarantee (containerd's image GC
evicted the 23GB OCR image mid-session, requiring a ~7min reimport). To mirror a new/updated
image locally:

```bash
docker tag ghcr.io/fabceolin/unlimited-ocr-container:gpu localhost:5000/fabceolin/unlimited-ocr-container:gpu
docker push localhost:5000/fabceolin/unlimited-ocr-container:gpu
```

The `*-api` images (`unlimited-ocr-api`, etc.) have no other home besides local builds, so on this
cluster they're additionally pushed as plain `localhost:5000/<name>:local` (no `ghcr.io` path to
mirror). The chart's `api.image.repository` default stays a portable bare local tag (works with
plain `docker build` + `k3s ctr images import`, no registry required) -- point it at the local
registry explicitly instead if you're on this cluster:

```bash
docker tag unlimited-ocr-api:local localhost:5000/unlimited-ocr-api:local
docker push localhost:5000/unlimited-ocr-api:local
helm upgrade ocr-api ./charts/unlimited-ocr -n ocr \
  --set api.enabled=true --set job.enabled=false \
  --set api.image.repository=localhost:5000/unlimited-ocr-api
```

### Unhealthy GPU workaround (this cluster)

`dev` has 4 physical GPUs; one of them (`0000:0C:00.0`, NVML index 3) intermittently fails with
`Unknown Error` (a correctable PCIe link error visible in `dmesg`). When that happens:

- `nvidia-container-runtime` (mode `auto`, the k3s default) tries to regenerate its CDI spec on
  every container start by enumerating *all* physical GPUs, and hard-fails the whole spec if any
  one of them errors — this blocks **every** GPU pod on the node, not just this chart's Job.
- Fixed by switching `/etc/nvidia-container-runtime/config.toml` to `mode = "cdi"` (reads a static
  spec instead of live-generating), and installing `/usr/local/sbin/nvidia-cdi-generate-safe.sh` +
  `nvidia-cdi-generate-safe.service` (a oneshot systemd unit, `Before=k3s.service`, enabled) that
  regenerates `/etc/cdi/nvidia.yaml` at every boot restricted to whichever GPUs currently pass
  `nvidia-smi --query-gpu=index`. This self-heals across reboots in both directions: a GPU that
  comes back healthy is automatically re-included, no manual step needed.
- Separately, `NVIDIA_VISIBLE_DEVICES=all` on a container **bypasses** that CDI restriction and
  re-enumerates every physical GPU directly, hitting the same error. So while the unhealthy GPU
  persists, the device-plugin release also needs `gpu.devicePlugin.visibleDevices` pinned to the
  healthy list explicitly:

  ```bash
  helm install ocr-gpu-infra ./charts/unlimited-ocr -n ocr --create-namespace \
    --set gpu.installDevicePlugin=true \
    --set job.enabled=false \
    --set gpu.devicePlugin.visibleDevices="0\,1\,2\,4"
  ```

  Once GPU index 3 is confirmed stable again (`nvidia-smi` cleanly lists all 5), drop that last
  `--set` and reinstall to go back to the portable `all` default.

## 2. One-time: host directories

```bash
sudo mkdir -p /srv/unlimited-ocr/{data,outputs,log,models}
sudo chmod -R 777 /srv/unlimited-ocr
```

Copy input files into `/srv/unlimited-ocr/data` before each run.

## 3. Per document: run the OCR job

```bash
cp document.pdf /srv/unlimited-ocr/data/
helm install ocr-doc1 ./charts/unlimited-ocr -n ocr --create-namespace \
  --set ocr.input=document.pdf \
  --set ocr.mode=pdf

kubectl logs -f job/ocr-doc1-ocr -n ocr
```

Result lands in `/srv/unlimited-ocr/outputs/document.md`. Each document run should use its own Helm release name (`ocr-doc1`, `ocr-doc2`, ...); `helm uninstall` removes the finished Job (outputs on disk are untouched).

For image directories: `--set ocr.mode=image_dir --set ocr.input=my-images-subdir`.
For the CPU image: `--set image.variant=cpu` (also drops GPU-only args automatically).

See `values.yaml` for all knobs (concurrency, image_mode, model_dir, HF token, resource limits, etc).

## 4. Optional: HTTP API

A thin FastAPI front-end that accepts a PDF over HTTP, submits a Job identical in shape to the one
above per request, waits for it, and returns cleaned Markdown (reusing `scripts/grounding_to_markdown.py`,
the same converter `ocrpdf` uses locally). Each request gets its own Job/hostPath subdir
(`api-<request-id>`), so concurrent requests don't collide — the node's 5 GPUs bound how many run
at once; extras just queue as `Pending` pods until a GPU frees up.

Build and import the image once (no registry needed, same trick used for the main GPU image):

```bash
docker build -f api/Dockerfile -t unlimited-ocr-api:local .
docker save unlimited-ocr-api:local | sudo k3s ctr images import -
```

Install as its own release, GPU infra already installed separately (step 1), `job.enabled=false`
since this release only runs the API:

```bash
helm install ocr-api ./charts/unlimited-ocr -n ocr --create-namespace \
  --set api.enabled=true \
  --set job.enabled=false
```

Call it (from the node, or via `kubectl port-forward` from anywhere with cluster access):

```bash
kubectl port-forward -n ocr svc/ocr-api-api 8080:80 &
curl -F file=@document.pdf http://localhost:8080/v1/ocr -o result.md
```

Notes:
- `--set api.apiKey=<secret>` gates requests behind a required `X-API-Key` header.
- `?raw=true` returns the raw grounding output (`<|det|>...` tags) instead of cleaned Markdown.
- `--set api.ingress.enabled=true --set api.ingress.host=ocr.example.lan` exposes it through the
  Traefik ingress already running on this cluster instead of `port-forward`.
- Cold-start cost is unavoidable per request in this design: each call pays for a fresh SGLang
  server start (~70-90s with cached weights, per our test) plus inference time. For frequent calls,
  a persistent always-warm SGLang server is the better fit — see the chart's design notes.
