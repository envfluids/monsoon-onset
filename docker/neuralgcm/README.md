# NeuralGCM Container Notes

## TPU Deployment

NeuralGCM runs on TPU VMs, not in containers. The typical deployment pattern is:

1. **TPU VM Creation**: Cloud Workflows creates a TPU VM with a startup script
2. **Code Download**: The startup script pulls code from GCS
3. **Execution**: Python runs directly on the TPU VM (not in a container)
4. **Cleanup**: VM is deleted after completion

## Why Not Containerized?

TPU VMs come with pre-installed, optimized JAX+TPU libraries that are tightly
coupled to the TPU hardware. Running JAX in a container on TPU is possible but:

- Requires matching JAX versions exactly to the TPU software
- Loses some TPU-specific optimizations
- Adds complexity without clear benefits

## This Container's Purpose

This Dockerfile is useful for:

1. **Local development/testing** (CPU mode)
2. **GPU fallback** if TPU is unavailable
3. **Packaging code** for upload to GCS

## Alternative: Direct VM Execution

For production, the recommended pattern is:

```bash
# Startup script on TPU VM
#!/bin/bash
pip install gcsfs google-cloud-storage

# Download application code
gsutil -m cp -r gs://bucket/neuralgcm/src /app/

# Download model weights
gsutil cp gs://bucket/weights/neuralgcm.pkl /app/weights/

# Run inference
cd /app && python -m src.main --region india --date 20250101T00
```

This approach uses the TPU VM's native JAX installation for optimal performance.
