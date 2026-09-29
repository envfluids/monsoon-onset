## Cloud Pipeline Architecture

This document describes the GCP-based cloud pipeline that replaces the original cron/HPC-based system.

```mermaid
graph TD
    Sched["Cloud Scheduler\n(per forecast region)"]
    Workflow["Cloud Workflows\nMain Pipeline"]

    subgraph "AIFS Branch"
        AIFSDownload["downloader\nCloud Run Job"]
        AIFSBatch["Cloud Batch GPU Job\nAIFS Inference + Model Post-processing"]
    end

    subgraph "NeuralGCM Branch"
        NGCMDownload["downloader\nCloud Run Job"]
        NGCMBatch["Cloud Batch GPU Job\nNeuralGCM Inference + Model Post-processing"]
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
    AIFSDownload --> AIFSBatch

    %% NeuralGCM branch
    NGCMDownload --> NGCMBatch

    %% Converge
    AIFSBatch --> Blend
    NGCMBatch --> Blend
    Blend --> Sync

    %% Data flow
    AIFSDownload & NGCMDownload -- "write raw" --> MainBucket
    AIFSBatch & NGCMBatch -- "read weights" --> WeightsBucket
    AIFSBatch & NGCMBatch -- "write" --> MainBucket
    Blend -- "read / write" --> MainBucket
    Sync -- "read" --> MainBucket

classDef storage fill:#34a853,color:#fff,stroke:#137333
classDef compute fill:#fbbc04,color:#000,stroke:#f9ab00
classDef orchestration fill:#ea4335,color:#fff,stroke:#c5221f

class Sched,Workflow orchestration
class AIFSDownload,AIFSBatch,NGCMDownload,NGCMBatch,Blend,Sync compute
class MainBucket,WeightsBucket storage
```
