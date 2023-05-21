from typing import Dict, List, Optional

from cacheflow.sequence import SequenceGroup, SequenceStatus


class CompletionOutput:

    def __init__(
        self,
        index: int,
        text: str,
        token_ids: List[int],
        cumulative_logprob: float,
        logprobs: List[Dict[int, float]],
        finish_reason: Optional[str] = None,
    ) -> None:
        self.index = index
        self.text = text
        self.token_ids = token_ids
        self.cumulative_logprob = cumulative_logprob
        self.logprobs = logprobs
        self.finish_reason = finish_reason

    def __repr__(self) -> str:
        return (f"CompletionOutput(index={self.index}, "
                f"text={self.text!r}, "
                f"token_ids={self.token_ids}, "
                f"cumulative_logprob={self.cumulative_logprob}, "
                f"logprobs={self.logprobs},"
                f"finish_reason={self.finish_reason})")


class RequestOutput:

    def __init__(
        self,
        request_id: int,
        prompt: str,
        prompt_token_ids: List[int],
        outputs: List[CompletionOutput],
    ) -> None:
        self.request_id = request_id
        self.prompt = prompt
        self.prompt_token_ids = prompt_token_ids
        self.outputs = outputs

    @staticmethod
    def from_seq_group(seq_group: SequenceGroup) -> "RequestOutput":
        # Get the top-n sequences.
        n = seq_group.sampling_params.n
        seqs = seq_group.get_seqs()
        assert n <= len(seqs)
        sorted_seqs = sorted(
            seqs, key=lambda seq: seq.get_cumulative_logprob(), reverse=True)
        top_n_seqs = sorted_seqs[:n]

        # Create the outputs.
        outputs: List[CompletionOutput] = []
        for seq in top_n_seqs:
            logprobs = seq.output_logprobs
            if seq_group.sampling_params.logprobs is None:
                # NOTE: We need to take care of this case because the sequence
                # always has the logprobs of the sampled tokens even if the
                # logprobs are not requested.
                logprobs = {}
            if seq.status == SequenceStatus.FINISHED_STOPPED:
                finish_reason = "stop"
            elif seq.status == SequenceStatus.FINISHED_LENGTH_CAPPED:
                finish_reason = "length"
            else:
                finish_reason = None
            output = CompletionOutput(seqs.index(seq), seq.output_text,
                                      seq.get_output_token_ids(),
                                      seq.get_cumulative_logprob(), logprobs,
                                      finish_reason)
            outputs.append(output)

        # Every sequence in the sequence group should have the same prompt.
        prompt = top_n_seqs[0].prompt
        prompt_token_ids = top_n_seqs[0].data.prompt_token_ids
        return RequestOutput(seq_group.request_id, prompt, prompt_token_ids,
                             outputs)

    def __repr__(self) -> str:
        return (f"RequestOutput(request_id={self.request_id}, "
                f"prompt={self.prompt!r}, "
                f"prompt_token_ids={self.prompt_token_ids}, "
                f"outputs={self.outputs})")

    def finished(self) -> bool:
        return all(output.finish_reason is not None for output in self.outputs)