from aioprometheus import Gauge, Histogram
from abc import ABC
from dataclasses import dataclass
from typing import Union, Optional, Dict, List
from vllm.core.scheduler import SchedulerOutputs

@dataclass
class SystemStats:
    """System Stats hold a snapshot of the system state at a given time."""
    num_total_gpu_blocks: int
    num_total_cpu_blocks: int
    num_free_gpu_blocks: int
    num_free_cpu_blocks: int
    num_running: int
    num_waiting: int
    num_swapped: int

@dataclass
class IterationStats:
    """IterationStats holds iteration level stats for logging_interval window."""
    num_prompt_tokens: List[int] = []
    num_generation_tokens: List[int] = []
    time_to_first_token: List[float] = []
    inter_token_latency: List[float] = []

    def update(self, now: float, scheduler_outputs: SchedulerOutputs) -> None:
        """Updates the Tracked Stats based on the SchedulerOutput."""
        # Update iteration timings for each SequenceGroup.
        timings = [
            seq_group.update_timings(
                now=now, 
                prompt_run=scheduler_outputs.prompt_run
            ) for seq_group in scheduler_outputs.scheduled_seq_groups
        ]

        # Update TrackedStats.
        if scheduler_outputs.prompt_run:
            # Prefill Related Stats.
            self.num_prompt_tokens.append(scheduler_outputs.num_batched_tokens)
            self.time_to_first_token.extend(timings)
        else:
            # Decode Related Stats.
            self.num_generation_tokens.append(scheduler_outputs.num_batched_tokens)
            self.inter_token_latency.extend(timings)

    def discard(self) -> None:
        """Discards Stats that are older than the logging_interval."""
        self.num_prompt_tokens = []
        self.num_generation_tokens = []
        self.time_to_first_token = []
        self.inter_token_latency = []

@dataclass
class Stats:
    system_stats: SystemStats
    iteration_stats: IterationStats

class PrometheusMetric(ABC):
    """Metric holds a Prometheus Metric and logic for converting Stats --> Metric"""    
    def log(self, labels: Dict) -> None:
        """Push metric to Prometheus client."""
        raise NotImplementedError

    def update(self, now: float, stats: Stats) -> None:
        """Update metric based on stats."""
        raise NotImplementedError
    
    def to_str(self) -> str:
        """Returns string representation for local logger."""
        raise NotImplementedError

class GaugeMetric(PrometheusMetric):
    def __init__(self, prometheus_metric: Gauge, labels: Dict[str,str]) -> None:
        self.gauge = prometheus_metric
        self.metric: Optional[Union[float, int]] = None
        self.labels = labels
        self.should_local_log = True
        super().__init__()
    
    def log(self) -> None:
        if self.metric is not None:
            self.gauge.set(self.labels, self.metric)
    
    def update(self, now: float, stats: Stats) -> None:
        raise NotImplementedError
    
    def to_str(self) -> str:
        raise NotImplementedError

class HistogramMetric(PrometheusMetric):
    def __init__(self, prometheus_metric: Histogram, labels: Dict[str,str]) -> None:
        self.histogram = prometheus_metric
        self.metrics: List[Union[float, int]] = []
        self.labels = labels
        self.should_local_log = False
        super().__init__()
    
    def log(self) -> None:
        for metric in self.metrics:
            self.histogram.observe(self.labels, metric)
    
    def update(self, now: float, stats: Stats) -> None:
        raise NotImplementedError
    
    def to_str(self) -> str:
        raise NotImplementedError
    