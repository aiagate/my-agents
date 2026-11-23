from __future__ import annotations

import argparse
import gc
import inspect
import json
import platform
import shutil
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from common import load_yaml_config, read_jsonl, sha256_file, sha256_text


def _ensure_eos_in_text(
    prompt_text: str,
    full_text: str,
    eos_token: str | None,
) -> str:
    if eos_token and eos_token not in full_text[len(prompt_text) :]:
        return full_text + eos_token
    return full_text


def build_supervised_features(
    messages: Sequence[dict[str, str]],
    tokenizer: Any,
    max_seq_length: int,
    chat_template_kwargs: dict[str, Any] | None = None,
) -> dict[str, list[int]]:
    if len(messages) < 2:
        raise ValueError("Each chat sample must contain prompt and assistant messages.")

    kwargs = dict(chat_template_kwargs or {})
    prompt_messages = list(messages[:-1])
    prompt_text = str(
        tokenizer.apply_chat_template(
            prompt_messages,
            tokenize=False,
            add_generation_prompt=True,
            **kwargs,
        )
    )
    full_text_raw = str(
        tokenizer.apply_chat_template(
            list(messages),
            tokenize=False,
            add_generation_prompt=False,
            **kwargs,
        )
    )
    eos_token = getattr(tokenizer, "eos_token", None)
    eos_token_id = getattr(tokenizer, "eos_token_id", None)
    full_text = _ensure_eos_in_text(
        prompt_text,
        full_text_raw,
        eos_token if isinstance(eos_token, str) else None,
    )

    prompt_ids = [
        int(tok)
        for tok in tokenizer(prompt_text, add_special_tokens=False)["input_ids"]
    ]
    full_ids = [
        int(tok) for tok in tokenizer(full_text, add_special_tokens=False)["input_ids"]
    ]

    if (
        isinstance(eos_token_id, int)
        and eos_token_id not in full_ids[len(prompt_ids) :]
    ):
        full_ids.append(eos_token_id)

    if len(full_ids) >= len(prompt_ids) and full_ids[: len(prompt_ids)] == prompt_ids:
        input_ids = full_ids
        labels = [-100] * len(prompt_ids) + full_ids[len(prompt_ids) :]
    else:
        if full_text.startswith(prompt_text):
            answer_text = full_text[len(prompt_text) :]
        else:
            answer_text = str(messages[-1]["content"])
            if isinstance(eos_token, str) and eos_token:
                answer_text += eos_token
        answer_ids = [
            int(tok)
            for tok in tokenizer(answer_text, add_special_tokens=False)["input_ids"]
        ]
        if isinstance(eos_token_id, int) and eos_token_id not in answer_ids:
            answer_ids.append(eos_token_id)
        input_ids = prompt_ids + answer_ids
        labels = [-100] * len(prompt_ids) + answer_ids

    if len(input_ids) > max_seq_length:
        input_ids = input_ids[:max_seq_length]
        labels = labels[:max_seq_length]

    attention_mask = [1] * len(input_ids)
    return {
        "input_ids": input_ids,
        "attention_mask": attention_mask,
        "labels": labels,
    }


@dataclass(slots=True)
class _PaddedCausalCollator:
    pad_token_id: int

    def __call__(self, features: list[dict[str, Any]]) -> dict[str, Any]:
        import torch

        max_len = max(len(item["input_ids"]) for item in features)
        input_ids_batch: list[list[int]] = []
        attention_batch: list[list[int]] = []
        labels_batch: list[list[int]] = []
        for item in features:
            pad_len = max_len - len(item["input_ids"])
            input_ids_batch.append(item["input_ids"] + [self.pad_token_id] * pad_len)
            attention_batch.append(item["attention_mask"] + [0] * pad_len)
            labels_batch.append(item["labels"] + [-100] * pad_len)
        return {
            "input_ids": torch.tensor(input_ids_batch, dtype=torch.long),
            "attention_mask": torch.tensor(attention_batch, dtype=torch.long),
            "labels": torch.tensor(labels_batch, dtype=torch.long),
        }


def _git_sha(repo_root: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() or None


def _log_sample_preview(
    first_row: dict[str, Any],
    first_features: dict[str, list[int]],
    tokenizer: Any,
    chat_template_kwargs: dict[str, Any],
) -> None:
    messages = first_row["messages"]
    rendered_prompt = str(
        tokenizer.apply_chat_template(
            messages[:-1],
            tokenize=False,
            add_generation_prompt=True,
            **chat_template_kwargs,
        )
    )
    rendered_full = str(
        tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=False,
            **chat_template_kwargs,
        )
    )
    prompt_toks = sum(1 for label in first_features["labels"] if label == -100)
    target_toks = sum(1 for label in first_features["labels"] if label != -100)
    print(f"Sample 0 token counts: prompt_masked={prompt_toks}, target={target_toks}")
    print("--- Rendered Prompt (truncated) ---")
    print(rendered_prompt[:400])
    print("--- Rendered Assistant Tail ---")
    print(rendered_full[len(rendered_prompt) :][:400])


def _resolve_model_dtype_kwarg(
    transformers_version: str,
    compute_dtype: Any,
) -> dict[str, Any]:
    head = transformers_version.split(".", 1)[0]
    digits = "".join(ch for ch in head if ch.isdigit())
    major = int(digits) if digits else 4
    key = "dtype" if major >= 5 else "torch_dtype"
    return {key: compute_dtype}


def _resolve_device_map(
    *,
    cuda_available: bool,
    quantization: str,
) -> dict[str, int] | None:
    if not cuda_available:
        return None
    if quantization == "4bit":
        return {"": 0}
    return None


def _build_training_args_kwargs(
    training_args_cls: Any,
    *,
    checkpoints_dir: Path,
    train_cfg: dict[str, Any],
    bf16_supported: bool,
    cuda_available: bool,
    grad_ckpt: bool,
) -> dict[str, Any]:
    params = inspect.signature(training_args_cls.__init__).parameters
    kwargs: dict[str, Any] = {
        "output_dir": str(checkpoints_dir),
        "num_train_epochs": float(train_cfg.get("num_train_epochs", 3)),
        "learning_rate": float(train_cfg.get("learning_rate", 1e-4)),
        "lr_scheduler_type": str(train_cfg.get("lr_scheduler_type", "cosine")),
        "per_device_train_batch_size": int(
            train_cfg.get("per_device_train_batch_size", 4)
        ),
        "per_device_eval_batch_size": int(
            train_cfg.get("per_device_eval_batch_size", 4)
        ),
        "gradient_accumulation_steps": int(
            train_cfg.get("gradient_accumulation_steps", 2)
        ),
        "weight_decay": float(train_cfg.get("weight_decay", 0.01)),
        "eval_strategy": "steps",
        "eval_steps": int(train_cfg.get("eval_steps", 20)),
        "save_strategy": "steps",
        "save_steps": int(train_cfg.get("save_steps", 20)),
        "save_total_limit": 2,
        "logging_steps": int(train_cfg.get("logging_steps", 5)),
        "load_best_model_at_end": True,
        "metric_for_best_model": "eval_loss",
        "greater_is_better": False,
        "bf16": bf16_supported,
        "fp16": bool(cuda_available and not bf16_supported),
        "seed": int(train_cfg.get("seed", 42)),
        "gradient_checkpointing": grad_ckpt,
        "remove_unused_columns": False,
        "report_to": "none",
        "dataloader_pin_memory": cuda_available,
    }
    warmup_ratio = float(train_cfg.get("warmup_ratio", 0.1))
    if "warmup_ratio" in params:
        kwargs["warmup_ratio"] = warmup_ratio
    elif "warmup_steps" in params:
        kwargs["warmup_steps"] = warmup_ratio
    if "save_safetensors" in params:
        kwargs["save_safetensors"] = True
    return kwargs


def _resolve_local_model_dir(model_id: str) -> Path | None:
    local_candidate = Path(model_id).expanduser()
    if local_candidate.is_dir():
        return local_candidate
    try:
        from huggingface_hub import snapshot_download

        snapshot_dir = snapshot_download(model_id, local_files_only=True)
    except Exception:
        return None
    path = Path(snapshot_dir)
    return path if path.is_dir() else None


def _copy_remote_code_assets(
    model_id: str,
    dest_dir: Path,
    *,
    chat_template_override: str | None = None,
) -> None:
    src_dir = _resolve_local_model_dir(model_id)
    if src_dir is not None:
        for pattern in ("*.py", "tokenizer.jsonl", "chat_template.jinja"):
            for src_file in sorted(src_dir.glob(pattern)):
                if src_file.is_file():
                    dst_file = dest_dir / src_file.name
                    if not dst_file.exists():
                        shutil.copy2(src_file, dst_file)
    if chat_template_override is not None:
        (dest_dir / "chat_template.jinja").write_text(
            chat_template_override,
            encoding="utf-8",
        )


def run_training(args: argparse.Namespace) -> int:
    import datasets
    import peft
    import torch
    import transformers
    from peft import (
        LoraConfig,
        PeftModel,
        TaskType,
        get_peft_model,
        prepare_model_for_kbit_training,
    )
    from transformers import (
        AutoModelForCausalLM,
        AutoTokenizer,
        BitsAndBytesConfig,
        Trainer,
        TrainingArguments,
    )

    config = load_yaml_config(args.config)
    if args.model_id:
        config["model_id"] = args.model_id
    if args.epochs is not None:
        config["train"]["num_train_epochs"] = args.epochs
    if args.lr is not None:
        config["train"]["learning_rate"] = args.lr
    if args.seed is not None:
        config["train"]["seed"] = args.seed
    if args.no_merge:
        config["merge_on_finish"] = False

    model_id: str = config["model_id"]
    trust_remote_code = bool(config.get("trust_remote_code", False))
    chat_template_kwargs: dict[str, Any] = dict(
        config.get("chat_template_kwargs") or {}
    )
    quantization = str(config.get("quantization", "none"))
    max_seq_length = int(config.get("max_seq_length", 1024))
    lora_cfg: dict[str, Any] = dict(config["lora"])
    train_cfg: dict[str, Any] = dict(config["train"])
    merge_on_finish = bool(config.get("merge_on_finish", True))

    train_path = args.dataset_dir / "train.jsonl"
    valid_path = args.dataset_dir / "valid.jsonl"
    if not train_path.is_file() or not valid_path.is_file():
        raise FileNotFoundError(
            f"Expected train.jsonl and valid.jsonl inside {args.dataset_dir}"
        )

    train_rows = read_jsonl(train_path)
    valid_rows = read_jsonl(valid_path)
    system_prompt = train_rows[0]["messages"][0]["content"]

    cuda_available = torch.cuda.is_available()
    bf16_supported = bool(cuda_available and torch.cuda.is_bf16_supported())
    if cuda_available:
        compute_dtype = torch.bfloat16 if bf16_supported else torch.float16
    else:
        compute_dtype = torch.float32

    print(
        f"Training config: model={model_id} quantization={quantization} "
        f"cuda={cuda_available} bf16={bf16_supported} "
        f"train_rows={len(train_rows)} valid_rows={len(valid_rows)}"
    )

    tokenizer = AutoTokenizer.from_pretrained(
        model_id,
        trust_remote_code=trust_remote_code,
    )
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token

    chat_template_override: str | None = None
    if args.chat_template_file is not None:
        chat_template_override = args.chat_template_file.read_text(encoding="utf-8")
    elif (
        isinstance(config.get("chat_template"), str)
        and str(config["chat_template"]).strip()
    ):
        chat_template_override = str(config["chat_template"])
    if chat_template_override is not None:
        tokenizer.chat_template = chat_template_override

    train_features = [
        build_supervised_features(
            row["messages"],
            tokenizer,
            max_seq_length,
            chat_template_kwargs,
        )
        for row in train_rows
    ]
    valid_features = [
        build_supervised_features(
            row["messages"],
            tokenizer,
            max_seq_length,
            chat_template_kwargs,
        )
        for row in valid_rows
    ]
    _log_sample_preview(
        train_rows[0],
        train_features[0],
        tokenizer,
        chat_template_kwargs,
    )

    train_ds = datasets.Dataset.from_list(train_features)
    valid_ds = datasets.Dataset.from_list(valid_features)

    bnb_config = None
    if quantization == "4bit":
        bnb_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_use_double_quant=True,
            bnb_4bit_compute_dtype=compute_dtype,
        )
    elif quantization != "none":
        raise ValueError(f"Unsupported quantization mode: {quantization!r}")

    dtype_kwargs = _resolve_model_dtype_kwarg(transformers.__version__, compute_dtype)
    model = AutoModelForCausalLM.from_pretrained(
        model_id,
        trust_remote_code=trust_remote_code,
        quantization_config=bnb_config,
        device_map=_resolve_device_map(
            cuda_available=cuda_available,
            quantization=quantization,
        ),
        **dtype_kwargs,
    )
    grad_ckpt = bool(train_cfg.get("gradient_checkpointing", True))
    if grad_ckpt:
        model.config.use_cache = False
    if quantization == "4bit":
        model = prepare_model_for_kbit_training(
            model,
            use_gradient_checkpointing=grad_ckpt,
        )
    elif grad_ckpt:
        model.gradient_checkpointing_enable()
        model.enable_input_require_grads()

    lora_config = LoraConfig(
        r=int(lora_cfg.get("r", 16)),
        lora_alpha=int(lora_cfg.get("lora_alpha", 32)),
        lora_dropout=float(lora_cfg.get("lora_dropout", 0.05)),
        target_modules=lora_cfg.get("target_modules", "all-linear"),
        bias="none",
        task_type=TaskType.CAUSAL_LM,
    )
    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()

    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoints_dir = output_dir / "checkpoints"

    training_args = TrainingArguments(
        **_build_training_args_kwargs(
            TrainingArguments,
            checkpoints_dir=checkpoints_dir,
            train_cfg=train_cfg,
            bf16_supported=bf16_supported,
            cuda_available=cuda_available,
            grad_ckpt=grad_ckpt,
        )
    )

    collator = _PaddedCausalCollator(pad_token_id=int(tokenizer.pad_token_id))
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_ds,
        eval_dataset=valid_ds,
        data_collator=collator,
    )

    started = time.perf_counter()
    train_output = trainer.train()
    eval_metrics = trainer.evaluate()
    runtime_s = round(time.perf_counter() - started, 2)

    peft_dir = output_dir / "peft_adapter"
    peft_dir.mkdir(parents=True, exist_ok=True)
    trainer.model.save_pretrained(str(peft_dir))
    tokenizer.save_pretrained(str(peft_dir))
    if trust_remote_code:
        _copy_remote_code_assets(
            model_id,
            peft_dir,
            chat_template_override=chat_template_override,
        )

    merged_dir: Path | None = None
    if merge_on_finish:
        merged_dir = output_dir / "merged_bf16"
        merged_dir.mkdir(parents=True, exist_ok=True)
        if quantization == "4bit":
            del trainer
            del model
            gc.collect()
            if cuda_available:
                torch.cuda.empty_cache()
            base_model = AutoModelForCausalLM.from_pretrained(
                model_id,
                trust_remote_code=trust_remote_code,
                device_map=None,
                **dtype_kwargs,
            )
            peft_loaded = PeftModel.from_pretrained(base_model, str(peft_dir))
            merged_model = peft_loaded.merge_and_unload()
        else:
            merged_model = trainer.model.merge_and_unload()
        merged_model.save_pretrained(str(merged_dir), safe_serialization=True)
        tokenizer.save_pretrained(str(merged_dir))
        if trust_remote_code:
            _copy_remote_code_assets(
                model_id,
                merged_dir,
                chat_template_override=chat_template_override,
            )
        best_ckpt = None
        best_metric = None
    else:
        best_ckpt = trainer.state.best_model_checkpoint
        best_metric = trainer.state.best_metric

    if merge_on_finish and quantization != "4bit":
        best_ckpt = trainer.state.best_model_checkpoint
        best_metric = trainer.state.best_metric
    elif merge_on_finish and quantization == "4bit":
        best_ckpt = None
        best_metric = eval_metrics.get("eval_loss")

    manifest: dict[str, Any] = {
        "created_at_utc": datetime.now(UTC).isoformat(),
        "git_sha": _git_sha(Path(__file__).resolve().parents[3]),
        "config": config,
        "dataset": {
            "dataset_dir": str(args.dataset_dir),
            "train_rows": len(train_rows),
            "valid_rows": len(valid_rows),
            "train_sha256": sha256_file(train_path),
            "valid_sha256": sha256_file(valid_path),
            "system_prompt_sha256": sha256_text(system_prompt),
        },
        "environment": {
            "python": platform.python_version(),
            "torch": torch.__version__,
            "transformers": transformers.__version__,
            "peft": peft.__version__,
            "cuda_available": cuda_available,
            "gpu_name": (torch.cuda.get_device_name(0) if cuda_available else None),
            "bf16_supported": bf16_supported,
        },
        "metrics": {
            "train_runtime_s": runtime_s,
            "train_loss": train_output.training_loss,
            "best_eval_loss": best_metric,
            "final_eval_loss": eval_metrics.get("eval_loss"),
            "best_model_checkpoint": best_ckpt,
        },
        "artifacts": {
            "peft_adapter_dir": str(peft_dir),
            "merged_bf16_dir": str(merged_dir) if merged_dir is not None else None,
        },
    }
    manifest_path = output_dir / "run_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Saved run manifest to {manifest_path}")
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Train association LoRA on CUDA GPU and export PEFT + merged weights."
    )
    parser.add_argument("--config", type=Path, required=True, help="YAML config path")
    parser.add_argument(
        "--dataset-dir",
        type=Path,
        required=True,
        help="Directory containing train.jsonl and valid.jsonl",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Output run directory (saves peft_adapter/, merged_bf16/, run_manifest.json)",
    )
    parser.add_argument("--model-id", default=None, help="Override config model_id")
    parser.add_argument("--epochs", type=float, default=None, help="Override epochs")
    parser.add_argument("--lr", type=float, default=None, help="Override learning_rate")
    parser.add_argument("--seed", type=int, default=None, help="Override random seed")
    parser.add_argument(
        "--chat-template-file",
        type=Path,
        default=None,
        help="Optional Jinja chat template file overriding tokenizer.chat_template",
    )
    parser.add_argument(
        "--no-merge",
        action="store_true",
        help="Skip merge_and_unload and export only peft_adapter/",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    return run_training(args)


if __name__ == "__main__":
    raise SystemExit(main())
