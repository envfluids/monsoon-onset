## Cloud Pipeline Architecture

This document describes the GCP-based cloud pipeline that replaces the original cron/HPC-based system.

```mermaid
graph TD
    subgraph Scheduling
        Sched["Cloud Scheduler\n(per forecast region)"]
    end

    subgraph Orchestration
        Sched -- "HTTP POST\n(region arg)" --> Workflow["Cloud Workflows\nMain Pipeline"]
        Workflow --> PubTrigger["Pub/Sub\npipeline-triggers"]
        PubComplete["Pub/Sub\npipeline-completions"] --> Workflow
    end

    subgraph "Compute"
        Workflow -- "1. Run Job" --> Downloader["downloader\nCloud Run Job"]
        Workflow -- "2. Create on-demand" --> TPU["TPU VM\nNeuralGCM JAX Inference"]
        TPU -- "done, delete VM" --> Workflow
        Workflow -- "3. Run Job" --> Postprocess["postprocess\nCloud Run Job"]
        Workflow -- "4. Run Job" --> Blend["blend\nCloud Run Job"]
        Workflow -- "5. Run Job" --> Sync["sync\nCloud Run Job"]
    end

    subgraph "Storage (GCS)"
        MainBucket["data bucket\nraw/ → intermediate/ → output/"]
        WeightsBucket["weights bucket\nNeuralGCM checkpoints"]
    end

    %% Data flow
    Downloader -- "write" --> MainBucket
    TPU -- "read weights" --> WeightsBucket
    TPU -- "write" --> MainBucket
    Postprocess -- "read / write" --> MainBucket
    Blend -- "read / write" --> MainBucket
    Sync -- "read" --> MainBucket

classDef storage fill:#34a853,color:#fff,stroke:#137333
classDef compute fill:#fbbc04,color:#000,stroke:#f9ab00
classDef orchestration fill:#ea4335,color:#fff,stroke:#c5221f

class Sched,Workflow,PubTrigger,PubComplete orchestration
class Downloader,Postprocess,Blend,Sync,TPU compute
class MainBucket,WeightsBucket storage
```
