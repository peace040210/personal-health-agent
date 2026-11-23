import json
from pathlib import Path


def load_model(base: Path, adapter: Path | None = None):
    from mlx_lm import load

    model, tokenizer = load(str(base), adapter_path=str(adapter) if adapter else None)
    tokenizer.chat_template = "{% set enable_thinking = false %}" + tokenizer.chat_template
    return model, tokenizer


class MlxIntent:
    """Run the Qwen3 intent classifier locally, optionally with a LoRA adapter."""

    def __init__(self, base: Path, adapter: Path | None = None, *, closed_choice: bool = False):
        self.model, self.tokenizer = load_model(base, adapter)
        self.closed_choice = closed_choice

    def choose(self, system: str, payload: dict, choices: tuple[str, ...]) -> str | None:
        messages = [
            {"role": "system", "content": system + "\n可选 JSON 值：" + "、".join(choices)},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        prompt = self.tokenizer.apply_chat_template(messages, add_generation_prompt=True, tokenize=True, return_dict=False)
        if self.closed_choice:
            return self._choose_closed(prompt, choices)

        from mlx_lm import stream_generate
        from mlx_lm.sample_utils import make_sampler

        answer = ""
        for chunk in stream_generate(self.model, self.tokenizer, prompt, max_tokens=24, sampler=make_sampler(temp=0)):
            answer += chunk.text
            if "}" in answer:
                break
        try:
            result = json.loads(answer.strip())
            if isinstance(result, dict) and set(result) == {"choice"} and result["choice"] in choices:
                return result["choice"]
        except (ValueError, TypeError):
            pass
        return None

    def _choose_closed(self, prompt: list[int], choices: tuple[str, ...]) -> str:
        import mlx.core as mx
        from mlx_lm.generate import generate_step

        answers = [self.tokenizer.encode(json.dumps({"choice": choice}, separators=(",", ":")),
                                         add_special_tokens=False) for choice in choices]
        prefix = []
        for tokens in zip(*answers):
            if len(set(tokens)) != 1:
                break
            prefix.append(tokens[0])
        branches = [answer[len(prefix)] for answer in answers]
        if len(set(branches)) != len(choices):
            raise ValueError("The five intent labels must have distinct tokens after the shared JSON prefix.")
        _, logprobs = next(generate_step(mx.array(prompt + prefix), self.model, max_tokens=1))
        return choices[mx.argmax(logprobs[mx.array(branches)]).item()]
