from typing import Dict, List

from cacheflow.sequence import SequenceGroup


class StreamOutput:

    def __init__(
        self,
        request_id: int,
        token_id: int,
        done: bool = False,
    ) -> None:
        self.request_id = request_id
        self.token_id = token_id
        self.done = done

    @staticmethod
    def from_seq_group(seq_group: SequenceGroup) -> "StreamOutput":
        assert seq_group.num_seqs() == 1
        seq = seq_group.seqs[0]
        token_id = seq.get_last_token_id()
        done = seq_group.is_finished()
        return StreamOutput(seq_group.request_id, token_id, done)

    def __repr__(self) -> str:
        return (f"StreamOutput(request_id={self.request_id}, "
                f"token_id={self.token_id}, done={self.done})")


class CompletionOutput:

    def __init__(
        self,
        output: str,
        logprobs: List[Dict[int, float]],
    ) -> None:
        self.output = output
        self.logprobs = logprobs

    def __repr__(self) -> str:
        return (f"CompletionOutput(output={self.output!r}, "
                f"logprobs={self.logprobs})")


class RequestOutput:

    def __init__(
        self,
        request_id: int,
        prompt: str,
        outputs: List[CompletionOutput],
    ) -> None:
        self.request_id = request_id
        self.prompt = prompt
        self.outputs = outputs

    @staticmethod
    def from_seq_group(
        seq_group: SequenceGroup,
        tokenizer,
    ) -> "RequestOutput":
        assert seq_group.is_finished()

        outputs: List[CompletionOutput] = []
        seqs = seq_group.get_seqs()
        for seq in seqs:
            output_token_ids = seq.data.output_token_ids
            output_str = tokenizer.decode(output_token_ids,
                                          skip_special_tokens=True)
            logprobs = seq.output_logprobs

            if seq_group.sampling_params.logprobs == 0:
                # NOTE: We need to take care of this case because the sequence
                # always has the logprobs of the sampled tokens even if the
                # logprobs are not requested.
                output = CompletionOutput(output_str, logprobs={})
            else:
                # NOTE: The output has at most `logprobs + 1` number of tokens.
                output = CompletionOutput(output_str, logprobs)
            outputs.append(output)

        # Every sequence in the sequence group should have the same prompt.
        prompt = seqs[0].prompt
        return RequestOutput(seq_group.request_id, prompt, outputs)

    def __repr__(self) -> str:
        return (f"RequestOutput(request_id={self.request_id}, "
                f"prompt={self.prompt!r}, outputs={self.outputs})")
