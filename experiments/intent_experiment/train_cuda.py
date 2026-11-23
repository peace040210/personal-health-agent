import argparse
import gc
import hashlib
import json
import math
import random
import time
from pathlib import Path

from health_agent.router import INTENT_CHOICES, INTENT_SYSTEM

from .cloud_data import CLOUD_DATA
from .compare import classification_metrics, evaluate_routes
from .dataset import payload, read_rows


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    temporary.replace(path)


def load_rows(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


class CudaIntent:
    def __init__(self, model, tokenizer, torch):
        self.model = model
        self.tokenizer = tokenizer
        self.torch = torch
        self.device = torch.device('cuda:0')
        self.pad = tokenizer.pad_token_id if tokenizer.pad_token_id is not None else tokenizer.eos_token_id
        answers = [tokenizer.encode(json.dumps({'choice': label}, separators=(',', ':')), add_special_tokens=False)
                   for label in INTENT_CHOICES]
        self.prefix = []
        for tokens in zip(*answers):
            if len(set(tokens)) != 1:
                break
            self.prefix.append(tokens[0])
        branches = [answer[len(self.prefix)] for answer in answers]
        if len(set(branches)) != len(INTENT_CHOICES):
            raise ValueError('The labels must have distinct tokens after the common prefix.')
        self.branches = torch.tensor(branches, device=self.device)

    def encode(self, system, body, choices):
        if tuple(choices) != INTENT_CHOICES:
            raise ValueError('Unexpected intent choices.')
        messages = [
            {'role': 'system', 'content': system + '\n可选 JSON 值：' + '、'.join(choices)},
            {'role': 'user', 'content': json.dumps(body, ensure_ascii=False)},
        ]
        tokens = self.tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True,
                                                    return_dict=False, enable_thinking=False) + self.prefix
        if len(tokens) > 512:
            raise ValueError('The intent prompt exceeds 512 tokens.')
        return tokens

    def scores(self, prompts):
        torch = self.torch
        length = max(map(len, prompts))
        positions = torch.tensor([len(ids) - 1 for ids in prompts], device=self.device)
        inputs = torch.tensor([ids + [self.pad] * (length - len(ids)) for ids in prompts], device=self.device)
        mask = (torch.arange(length, device=self.device)[None, :] <= positions[:, None]).long()
        base = self.model.get_base_model() if hasattr(self.model, 'get_base_model') else self.model
        hidden = base.model(input_ids=inputs, attention_mask=mask, use_cache=False).last_hidden_state
        decision = hidden[torch.arange(len(prompts), device=self.device), positions]
        return base.lm_head(decision).index_select(dim=1, index=self.branches).float()

    def choose(self, system, body, choices):
        with self.torch.inference_mode():
            scores = self.scores([self.encode(system, body, choices)])[0]
            return INTENT_CHOICES[scores.argmax().item()]


def evaluate(model, rows):
    model.model.eval()
    for row in rows[:3]:
        model.choose(INTENT_SYSTEM, payload(row), INTENT_CHOICES)
    predictions = []
    for row in rows:
        body = payload(row)
        start = time.perf_counter()
        prediction = model.choose(INTENT_SYSTEM, body, INTENT_CHOICES)
        predictions.append({'text': row['text'], 'recent': row['recent'], 'expected': row['choice'],
                            'predicted': prediction, 'ms': round((time.perf_counter() - start) * 1000, 3)})
    return {**classification_metrics(predictions), 'predictions': predictions}


def measure(model, sets):
    metrics = {name: evaluate(model, rows) for name, rows in sets.items()}
    for name, result in metrics.items():
        print(name, result['correct'], '/', result['total'], 'macro_f1', result['macro_f1'], flush=True)
    routing = evaluate_routes(model)
    print('routing', routing['correct'], '/', routing['total'], 'unexpected', routing['unexpected_actions'], flush=True)
    return {'classifiers': metrics, 'routing': routing}


def train(args):
    import torch
    from peft import LoraConfig, get_peft_model, get_peft_model_state_dict, set_peft_model_state_dict
    from transformers import AutoModelForCausalLM, AutoTokenizer, get_cosine_schedule_with_warmup

    if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported():
        raise RuntimeError('This run requires a CUDA GPU with BF16 support.')
    manifest = json.loads((CLOUD_DATA / 'manifest.json').read_text(encoding='utf-8'))
    for name, report in manifest.items():
        if hashlib.sha256((CLOUD_DATA / f'{name}.jsonl').read_bytes()).hexdigest() != report['sha256']:
            raise ValueError(f'{name} split has changed.')
    args.out.mkdir(parents=True, exist_ok=False)
    seed = 1370
    rng = random.Random(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    start = time.monotonic()
    tokenizer = AutoTokenizer.from_pretrained(args.model, local_files_only=True)
    base = AutoModelForCausalLM.from_pretrained(args.model, dtype=torch.bfloat16, device_map={'': 0},
                                              local_files_only=True, attn_implementation='sdpa')
    classifier = CudaIntent(base, tokenizer, torch)
    train_rows = load_rows(CLOUD_DATA / 'train.jsonl')
    valid_rows = load_rows(CLOUD_DATA / 'valid.jsonl')
    test_sets = {'frozen_original': read_rows('test'), 'scenario_test': load_rows(CLOUD_DATA / 'test.jsonl')}
    examples = [(classifier.encode(INTENT_SYSTEM, payload(row), INTENT_CHOICES), INTENT_CHOICES.index(row['choice']))
                for row in train_rows]
    summary = {'model': 'Qwen/Qwen3-4B-Instruct-2507', 'precision': 'bfloat16', 'gpu': torch.cuda.get_device_name(),
               'datasets': manifest, 'seed': seed, 'learning_rate': args.lr, 'epochs': args.epochs,
               'batch_size': args.batch, 'gradient_accumulation': args.accumulate, 'objective': 'cross entropy over five intent logits',
               'system_prompt': INTENT_SYSTEM, 'original_test_sha256': hashlib.sha256(
                   (CLOUD_DATA.parent / 'data' / 'test_rows.jsonl').read_bytes()).hexdigest()}
    write_json(args.out / 'settings.json', summary)
    print('Base evaluation', flush=True)
    base_result = measure(classifier, test_sets)
    base_validation = evaluate(classifier, valid_rows)
    write_json(args.out / 'baseline.json', {**base_result, 'validation': base_validation})
    layer_count = base.config.num_hidden_layers
    config = LoraConfig(task_type='CAUSAL_LM', r=8, lora_alpha=16, lora_dropout=0.05,
                        target_modules=['q_proj', 'v_proj'], layers_to_transform=list(range(layer_count - 12, layer_count)))
    classifier.model = model = get_peft_model(base, config)
    model.print_trainable_parameters()
    params = [param for param in model.parameters() if param.requires_grad]
    optimizer = torch.optim.AdamW(params, lr=args.lr)
    examples_per_update = args.batch * args.accumulate
    updates_per_epoch = math.ceil(len(examples) / examples_per_update)
    scheduler = get_cosine_schedule_with_warmup(optimizer, max(1, updates_per_epoch // 10), updates_per_epoch * args.epochs)
    best_key, best_state = (-1.0, -1), None
    history = []
    for epoch in range(1, args.epochs + 1):
        model.train()
        rng.shuffle(examples)
        interval_loss = 0.0
        for offset in range(0, len(examples), examples_per_update):
            group = examples[offset:offset + examples_per_update]
            optimizer.zero_grad(set_to_none=True)
            for index in range(0, len(group), args.batch):
                prompts, labels = zip(*group[index:index + args.batch])
                logits = classifier.scores(prompts)
                loss = torch.nn.functional.cross_entropy(logits, torch.tensor(labels, device=classifier.device), reduction='sum') / len(group)
                loss.backward()
                interval_loss += loss.detach().item()
            torch.nn.utils.clip_grad_norm_(params, 1.0)
            optimizer.step()
            scheduler.step()
            update = offset // examples_per_update + 1
            if update % 20 == 0:
                print(f'epoch {epoch} update {update}/{updates_per_epoch} loss={interval_loss / 20:.4f}', flush=True)
                interval_loss = 0.0
        validation = evaluate(classifier, valid_rows)
        key = (validation['macro_f1'], validation['by_label']['unknown']['correct'])
        history.append({'epoch': epoch, **{name: value for name, value in validation.items() if name != 'predictions'}})
        write_json(args.out / 'validation.json', history)
        print('validation', epoch, validation['correct'], '/', validation['total'], 'macro_f1', key[0], flush=True)
        if key > best_key:
            best_key = key
            best_state = {name: value.detach().cpu().clone() for name, value in get_peft_model_state_dict(model).items()}
            model.save_pretrained(args.out / 'adapter', safe_serialization=True, save_embedding_layers=False)
            summary['selected_epoch'] = epoch
            write_json(args.out / 'settings.json', summary)
    set_peft_model_state_dict(model, best_state)
    gc.collect()
    torch.cuda.empty_cache()
    print('Adapter evaluation', flush=True)
    tuned = measure(classifier, test_sets)
    summary.update(seconds=round(time.monotonic() - start, 1), peak_gpu_gb=round(torch.cuda.max_memory_allocated() / 1e9, 2),
                   adapter_sha256=hashlib.sha256((args.out / 'adapter' / 'adapter_model.safetensors').read_bytes()).hexdigest())
    write_json(args.out / 'comparison.json', {'settings': summary, 'base': base_result, 'adapter': tuned})
    write_json(args.out / 'settings.json', summary)
    print('Experiment complete', args.out, flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--epochs', type=int, default=2)
    parser.add_argument('--batch', type=int, default=4)
    parser.add_argument('--accumulate', type=int, default=2)
    parser.add_argument('--lr', type=float, default=1e-4)
    args = parser.parse_args()
    if min(args.epochs, args.batch, args.accumulate) < 1 or args.lr <= 0:
        parser.error('The number of epochs, batch size, accumulation steps and learning rate must be positive.')
    train(args)
