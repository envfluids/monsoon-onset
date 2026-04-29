## Cloud Pipeline Architecture

This document describes the GCP-based cloud pipeline that replaces the original cron/HPC-based system.

```mermaid
graph TD
    Sched["Cloud Scheduler\n(per forecast region)"]
    Workflow["Cloud Workflows\nMain Pipeline"]

    subgraph "AIFS Branch"
        AIFSDownload["downloader\nCloud Run Job"]
        AIFSPostprocess["postprocess\nCloud Run Job"]
    end

    subgraph "NeuralGCM Branch"
        NGCMDownload["downloader\nCloud Run Job"]
        NGCMBatch["Cloud Batch GPU Job\nNeuralGCM Inference"]
        NGCMPostprocess["postprocess\nCloud Run Job"]
    end

    Blend["blend\nCloud Run Job"]
    Sync["sync\nCloud Run Job"]

    subgraph "Storage (GCS)"
        MainBucket["data bucket\nraw/ → intermediate/ → output/"]
        WeightsBucket["weights bucket\nNeuralGCM checkpoints"]
    end

    %% Orchestration
    Sched -- "HTTP POST\n(region arg)" --> Workflow
    Workflow --> AIFSDownload
    Workflow --> NGCMDownload

    %% AIFS branch
    AIFSDownload --> AIFSPostprocess

    %% NeuralGCM branch
    NGCMDownload --> NGCMBatch
    NGCMBatch --> NGCMPostprocess

    %% Converge
    AIFSPostprocess --> Blend
    NGCMPostprocess --> Blend
    Blend --> Sync

    %% Data flow
    AIFSDownload & NGCMDownload -- "write raw" --> MainBucket
    NGCMBatch -- "read weights" --> WeightsBucket
    NGCMBatch -- "write" --> MainBucket
    AIFSPostprocess & NGCMPostprocess -- "read / write" --> MainBucket
    Blend -- "read / write" --> MainBucket
    Sync -- "read" --> MainBucket

classDef storage fill:#34a853,color:#fff,stroke:#137333
classDef compute fill:#fbbc04,color:#000,stroke:#f9ab00
classDef orchestration fill:#ea4335,color:#fff,stroke:#c5221f

class Sched,Workflow orchestration
class AIFSDownload,AIFSPostprocess,NGCMDownload,NGCMBatch,NGCMPostprocess,Blend,Sync compute
class MainBucket,WeightsBucket storage
```
