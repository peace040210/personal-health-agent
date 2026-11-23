import argparse
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

from health_agent.mlx_intent import load_model
from .dataset import DATA, ROOT, build


ARTIFACTS = ROOT / "artifacts"
ADAPTER = ARTIFACTS / "adapter"


def train(base: Path):
    import mlx.core as mx
    import numpy as np
    from mlx.utils import tree_flatten
    from mlx_lm.lora import CONFIG_DEFAULTS, train_model
    from mlx_lm.tuner.callbacks import TrainingCallback
    from mlx_lm.tuner.datasets import ChatDataset

    if (ADAPTER / "adapters.safetensors").exists():
        raise ValueError(f"Adapter already exists at {ADAPTER}.")
    build()
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    seed = 1370
    np.random.seed(seed)
    mx.random.seed(seed)
    model, tokenizer = load_model(base)
    train_rows, valid_rows = (
        [json.loads(row) for row in (DATA / f"{split}.jsonl").read_text(encoding="utf-8").splitlines()]
        for split in ("train", "valid")
    )
    for row in (*train_rows, *valid_rows):
        full = tokenizer.apply_chat_template(row["messages"], return_dict=False)
        prefix = tokenizer.apply_chat_template(row["messages"][:-1], add_generation_prompt=True, return_dict=False)
        if full[:len(prefix)] != prefix or not len(prefix) < len(full) <= 512:
            raise ValueError("Chat template prefix or sequence length invalid.")
    train_data = ChatDataset(train_rows, tokenizer, mask_prompt=True)
    valid_data = ChatDataset(valid_rows, tokenizer, mask_prompt=True)
    events = []

    class Recorder(TrainingCallback):
        def on_val_loss_report(self, info):
            events.append(info)
            (ARTIFACTS / "validation.json").write_text(json.dumps(events, indent=2), encoding="utf-8")

    config = {**CONFIG_DEFAULTS, "model": str(base), "data": str(DATA), "adapter_path": str(ADAPTER),
              "train": True, "test": False, "seed": seed, "mask_prompt": True,
              "fine_tune_type": "lora", "num_layers": 8, "optimizer": "adam",
              "lora_parameters": {"rank": 8, "scale": 16.0, "dropout": 0.0,
                                  "keys": ["self_attn.q_proj", "self_attn.v_proj"]},
              "batch_size": 4, "iters": 96, "learning_rate": 1e-4, "max_seq_length": 512,
              "steps_per_report": 12, "steps_per_eval": 32, "save_every": 96,
              "val_batches": -1, "report_to": None, "grad_checkpoint": False}
    start = time.monotonic()
    train_model(SimpleNamespace(**config), model, train_data, valid_data, training_callback=Recorder())
    weight_file = ADAPTER / "adapters.safetensors"
    summary = {
        "seconds": time.monotonic() - start,
        "trainable_parameters": sum(tensor.size for _, tensor in tree_flatten(model.trainable_parameters())),
        "total_parameters": sum(tensor.size for _, tensor in tree_flatten(model.parameters())),
        "peak_memory_gb": mx.get_peak_memory() / 1e9,
        "training_rows": len(train_rows), "validation_rows": len(valid_rows),
        "adapter_sha256": hashlib.sha256(weight_file.read_bytes()).hexdigest(),
        "settings": {name: config[name] for name in ("seed", "batch_size", "iters", "learning_rate", "num_layers", "lora_parameters", "mask_prompt")},
    }
    (ARTIFACTS / "training.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, required=True)
    train(parser.parse_args().base)
