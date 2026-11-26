from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import common
import export_dataset_bundle
import numpy as np
import prepare_local_mlx
import pytest
import train_cuda_lora
from my_agents.domain.association_tree import ASSOCIATION_SYSTEM_PROMPT

CONFIGS_DIR = Path(__file__).resolve().parents[1] / "configs"


def _valid_tree_dict(root: str = "炊飯の水加減") -> dict[str, Any]:
    return {
        "root": root,
        "branches": [
            {
                "l1": "米の量",
                "cat": "計量",
                "l2": [
                    {
                        "term": "精米度",
                        "cat": "加工",
                        "l3": [
                            {
                                "term": "白米",
                                "cat": "精白",
                                "hint": "磨き上げる度合いの調整は文章の推敲レベルにも転用できる",
                            },
                            {
                                "term": "押麦",
                                "cat": "混穀",
                                "hint": "別素材を混ぜる発想は異業種チームの編成判断にも応用できる",
                            },
                        ],
                    },
                    {
                        "term": "指関節",
                        "cat": "目安",
                        "l3": [
                            {
                                "term": "計量器",
                                "cat": "道具",
                                "hint": "数値で測る正確さは家計簿の支出記録や体重管理にも使える",
                            },
                            {
                                "term": "目盛",
                                "cat": "表示",
                                "hint": "基準線の見える化は水槽の水位管理や作業工程にも応用できる",
                            },
                        ],
                    },
                ],
            },
            {
                "l1": "釜",
                "cat": "道具",
                "l2": [
                    {
                        "term": "炊き方",
                        "cat": "手順",
                        "l3": [
                            {
                                "term": "硬め",
                                "cat": "食感",
                                "hint": "仕上がりの強度選択はパスタの茹で加減や配合にも転用できる",
                            },
                            {
                                "term": "早炊き",
                                "cat": "時間",
                                "hint": "時間短縮モードの発想は急ぎの書類作成の優先度にも応用できる",
                            },
                        ],
                    },
                    {
                        "term": "蒸らし",
                        "cat": "待機",
                        "l3": [
                            {
                                "term": "余熱",
                                "cat": "温度",
                                "hint": "火を止めた後の力の残りは運動後の体温維持にも活用できる",
                            },
                            {
                                "term": "ふた",
                                "cat": "密閉",
                                "hint": "密閉して待つ発想は漬物の重石や書類の保管管理にも応用できる",
                            },
                        ],
                    },
                ],
            },
        ],
    }


def test_load_yaml_config_parses_all_checked_in_configs() -> None:
    expected = {
        "qwen25_3b_l4.yaml": ("Qwen/Qwen2.5-3B-Instruct", False, {}, 3),
        "qwen3_4b_l4.yaml": ("Qwen/Qwen3-4B", False, {"enable_thinking": False}, 3),
        "plamo3_translate_2b_l4.yaml": ("pfnet/plamo-3-translate-2b", True, {}, 3),
        "plamo3_08b_fin_l4.yaml": ("pfnet/plamo-3-0.8b-fin-instruct", True, {}, 5),
        "plamo3_nict_2b_l4.yaml": ("pfnet/plamo-3-nict-2b-base", True, {}, 5),
    }
    for filename, (model_id, trust_remote, chat_kwargs, epochs) in expected.items():
        cfg = common.load_yaml_config(CONFIGS_DIR / filename)
        assert cfg["model_id"] == model_id
        assert cfg["trust_remote_code"] is trust_remote
        assert cfg["chat_template_kwargs"] == chat_kwargs
        assert cfg["lora"]["target_modules"] == "all-linear"
        assert cfg["train"]["num_train_epochs"] == epochs


def test_resolve_model_dtype_kwarg_handles_v4_and_v5() -> None:
    assert train_cuda_lora._resolve_model_dtype_kwarg("4.57.6", "bf16") == {
        "torch_dtype": "bf16"
    }
    assert train_cuda_lora._resolve_model_dtype_kwarg("5.22.1", "bf16") == {
        "dtype": "bf16"
    }
    assert train_cuda_lora._resolve_model_dtype_kwarg("5.0.0rc1", "fp16") == {
        "dtype": "fp16"
    }


def test_resolve_device_map() -> None:
    assert (
        train_cuda_lora._resolve_device_map(cuda_available=False, quantization="none")
        is None
    )
    assert (
        train_cuda_lora._resolve_device_map(cuda_available=False, quantization="4bit")
        is None
    )
    assert (
        train_cuda_lora._resolve_device_map(cuda_available=True, quantization="none")
        is None
    )
    assert train_cuda_lora._resolve_device_map(
        cuda_available=True, quantization="4bit"
    ) == {"": 0}


def test_build_training_args_kwargs_handles_v4_and_v5_signatures(
    tmp_path: Path,
) -> None:
    class _FakeV4TrainingArgs:
        def __init__(
            self,
            *,
            output_dir: str,
            num_train_epochs: float,
            learning_rate: float,
            warmup_ratio: float,
            lr_scheduler_type: str,
            per_device_train_batch_size: int,
            per_device_eval_batch_size: int,
            gradient_accumulation_steps: int,
            weight_decay: float,
            eval_strategy: str,
            eval_steps: int,
            save_strategy: str,
            save_steps: int,
            save_total_limit: int,
            logging_steps: int,
            load_best_model_at_end: bool,
            metric_for_best_model: str,
            greater_is_better: bool,
            bf16: bool,
            fp16: bool,
            seed: int,
            gradient_checkpointing: bool,
            remove_unused_columns: bool,
            save_safetensors: bool,
            report_to: str,
            dataloader_pin_memory: bool,
        ) -> None:
            pass

    class _FakeV5TrainingArgs:
        def __init__(
            self,
            *,
            output_dir: str,
            num_train_epochs: float,
            learning_rate: float,
            warmup_steps: float,
            lr_scheduler_type: str,
            per_device_train_batch_size: int,
            per_device_eval_batch_size: int,
            gradient_accumulation_steps: int,
            weight_decay: float,
            eval_strategy: str,
            eval_steps: int,
            save_strategy: str,
            save_steps: int,
            save_total_limit: int,
            logging_steps: int,
            load_best_model_at_end: bool,
            metric_for_best_model: str,
            greater_is_better: bool,
            bf16: bool,
            fp16: bool,
            seed: int,
            gradient_checkpointing: bool,
            remove_unused_columns: bool,
            report_to: str,
            dataloader_pin_memory: bool,
        ) -> None:
            pass

    train_cfg = {"num_train_epochs": 5, "warmup_ratio": 0.15}
    v4_kwargs = train_cuda_lora._build_training_args_kwargs(
        _FakeV4TrainingArgs,
        checkpoints_dir=tmp_path,
        train_cfg=train_cfg,
        bf16_supported=True,
        cuda_available=True,
        grad_ckpt=True,
    )
    assert v4_kwargs["warmup_ratio"] == 0.15
    assert "warmup_steps" not in v4_kwargs
    assert v4_kwargs["save_safetensors"] is True

    v5_kwargs = train_cuda_lora._build_training_args_kwargs(
        _FakeV5TrainingArgs,
        checkpoints_dir=tmp_path,
        train_cfg=train_cfg,
        bf16_supported=True,
        cuda_available=True,
        grad_ckpt=True,
    )
    assert v5_kwargs["warmup_steps"] == 0.15
    assert "warmup_ratio" not in v5_kwargs
    assert "save_safetensors" not in v5_kwargs


def test_evaluate_raw_association_json_accepts_valid_tree_and_fences() -> None:
    payload = json.dumps(_valid_tree_dict(), ensure_ascii=False)
    fenced = f"```json\n{payload}\n```"

    report = common.evaluate_raw_association_json(fenced, expected_root="炊飯の水加減")

    assert report.json_valid is True
    assert report.root_matches is True
    assert report.strict_valid is True
    assert report.branch_count == 2
    assert report.l2_count == 4
    assert report.l3_count == 8
    assert report.total_terms == 14
    assert report.unique_terms == 14
    assert report.duplicate_term_count == 0
    assert report.distinct_cats == 13
    assert report.cat_violation_count == 0
    assert report.valid_hint_count == 8
    assert report.first_error is None


def test_evaluate_raw_association_json_detects_schema_and_constraint_errors() -> None:
    invalid_json = common.evaluate_raw_association_json(
        "not json at all",
        expected_root="炊飯の水加減",
    )
    assert invalid_json.json_valid is False
    assert invalid_json.strict_valid is False
    assert invalid_json.first_error == "invalid or missing JSON object"

    bad = _valid_tree_dict(root="違うルート")
    bad["branches"][0]["l1"] = "炊飯の水加減"  # duplicates expected_root
    bad["branches"][0]["l2"][0]["cat"] = "計量"  # repeats parent l1 cat
    bad["branches"][0]["l2"][0]["l3"][0]["term"] = "EnglishTerm"  # ASCII forbidden
    bad["branches"][0]["l2"][0]["l3"][1]["term"] = (
        "白米"  # duplicate term if white rice used; make duplicate of existing
    )
    bad["branches"][0]["l2"][0]["l3"][1]["term"] = (
        "押麦テスト長すぎる語彙十三字"  # > 12 chars
    )
    bad["branches"][0]["l2"][1]["cat"] = "CatAscii"  # ASCII in cat
    bad["branches"][0]["l2"][1]["l3"][0]["term"] = "釜"  # duplicates branch 1 l1
    bad["branches"][0]["l2"][1]["l3"][0]["hint"] = "短すぎ"  # < 15 chars
    bad["branches"][1]["cat"] = "計量"  # sibling l1 cat duplicate
    bad["branches"][1]["l2"][0]["l3"][1]["cat"] = "食感"  # sibling l3 cat duplicate

    report = common.evaluate_raw_association_json(
        json.dumps(bad, ensure_ascii=False),
        expected_root="炊飯の水加減",
    )

    assert report.json_valid is True
    assert report.root_matches is False
    assert report.strict_valid is False
    assert report.duplicate_term_count == 2
    assert report.cat_violation_count >= 3
    assert report.valid_hint_count == 7
    assert report.first_error is not None


def test_convert_peft_weights_to_mlx_transposes_and_sets_scale() -> None:
    peft_cfg = {
        "peft_type": "LORA",
        "r": 8,
        "lora_alpha": 16,
        "lora_dropout": 0.05,
        "use_dora": False,
        "use_rslora": False,
        "modules_to_save": None,
    }
    a_mat = np.arange(8 * 16, dtype=np.float32).reshape(8, 16)
    b_mat = np.arange(32 * 8, dtype=np.float32).reshape(32, 8)
    weights = {
        "base_model.model.model.layers.0.self_attn.q_proj.lora_A.weight": a_mat,
        "base_model.model.model.layers.0.self_attn.q_proj.lora_B.weight": b_mat,
        "base_model.model.model.layers.1.self_attn.q_proj.lora_A.weight": a_mat,
        "base_model.model.model.layers.1.self_attn.q_proj.lora_B.weight": b_mat,
    }

    mlx_cfg, mlx_weights = prepare_local_mlx.convert_peft_weights_to_mlx(
        peft_cfg,
        weights,
    )

    assert mlx_cfg == {
        "fine_tune_type": "lora",
        "num_layers": -1,
        "lora_parameters": {
            "rank": 8,
            "dropout": 0.05,
            "scale": 2.0,
            "keys": ["self_attn.q_proj"],
        },
    }
    q0_a = mlx_weights["model.layers.0.self_attn.q_proj.lora_a"]
    q0_b = mlx_weights["model.layers.0.self_attn.q_proj.lora_b"]
    assert q0_a.shape == (16, 8)
    assert q0_b.shape == (8, 32)
    assert np.array_equal(q0_a, a_mat.T)
    assert np.array_equal(q0_b, b_mat.T)
    assert q0_a.flags["C_CONTIGUOUS"]
    assert q0_b.flags["C_CONTIGUOUS"]


def test_convert_peft_weights_to_mlx_trailing_layers_and_rejections() -> None:
    peft_cfg = {
        "peft_type": "LORA",
        "r": 4,
        "lora_alpha": 8,
        "lora_dropout": 0.0,
    }
    a_mat = np.ones((4, 8), dtype=np.float32)
    b_mat = np.ones((8, 4), dtype=np.float32)
    trailing_weights = {
        "base_model.model.model.layers.2.mlp.down_proj.lora_A.weight": a_mat,
        "base_model.model.model.layers.2.mlp.down_proj.lora_B.weight": b_mat,
        "base_model.model.model.layers.3.mlp.down_proj.lora_A.weight": a_mat,
        "base_model.model.model.layers.3.mlp.down_proj.lora_B.weight": b_mat,
    }

    mlx_cfg, _ = prepare_local_mlx.convert_peft_weights_to_mlx(
        peft_cfg,
        trailing_weights,
        total_layers=4,
    )
    assert mlx_cfg["num_layers"] == 2

    with pytest.raises(ValueError, match="total-layers"):
        prepare_local_mlx.convert_peft_weights_to_mlx(peft_cfg, trailing_weights)

    non_contiguous = {
        "base_model.model.model.layers.0.mlp.down_proj.lora_A.weight": a_mat,
        "base_model.model.model.layers.2.mlp.down_proj.lora_A.weight": a_mat,
    }
    with pytest.raises(ValueError, match="non-contiguous"):
        prepare_local_mlx.convert_peft_weights_to_mlx(peft_cfg, non_contiguous)

    for bad_override in (
        {"use_dora": True},
        {"use_rslora": True},
        {"modules_to_save": ["embed_tokens"]},
        {"peft_type": "PREFIX_TUNING"},
    ):
        with pytest.raises(ValueError, match="not supported|Unsupported"):
            prepare_local_mlx.convert_peft_weights_to_mlx(
                {**peft_cfg, **bad_override},
                trailing_weights,
                total_layers=4,
            )


class _FakeTokenizer:
    def __init__(
        self,
        *,
        include_eos_in_template: bool = True,
        eos_token: str | None = "<|im_end|>",
        eos_token_id: int | None = 99,
        boundary_merge: bool = False,
    ) -> None:
        self.eos_token = eos_token
        self.eos_token_id = eos_token_id
        self.include_eos_in_template = include_eos_in_template
        self.boundary_merge = boundary_merge
        self.template_kwargs_seen: list[dict[str, Any]] = []

    def apply_chat_template(
        self,
        messages: list[dict[str, str]],
        *,
        tokenize: bool,
        add_generation_prompt: bool,
        **kwargs: Any,
    ) -> str:
        assert tokenize is False
        self.template_kwargs_seen.append(dict(kwargs))
        parts = [f"<{m['role']}>{m['content']}" for m in messages]
        joined = "|".join(parts)
        if add_generation_prompt:
            return joined + "|<assistant>"
        if self.include_eos_in_template and self.eos_token:
            return joined + self.eos_token + "\n"
        return joined

    def __call__(
        self, text: str, *, add_special_tokens: bool = True
    ) -> dict[str, list[int]]:
        assert add_special_tokens is False
        if text.endswith("|<assistant>"):
            return {"input_ids": [1, 2, 3]}
        if "<assistant>" in text:
            if self.boundary_merge:
                # Simulate a BPE merge changing the boundary token when full_text is tokenized.
                return {"input_ids": [1, 2, 777, 10, 11, 99]}
            ids = [1, 2, 3, 10, 11]
            if (
                self.eos_token
                and self.eos_token in text
                and self.eos_token_id is not None
            ):
                ids.append(self.eos_token_id)
            return {"input_ids": ids}
        ids = [10, 11]
        if self.eos_token and self.eos_token in text and self.eos_token_id is not None:
            ids.append(self.eos_token_id)
        return {"input_ids": ids}


def test_build_supervised_features_masks_prompt_and_preserves_eos() -> None:
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "usr"},
        {"role": "assistant", "content": "ans"},
    ]

    tok_with_eos = _FakeTokenizer(include_eos_in_template=True)
    feat = train_cuda_lora.build_supervised_features(
        messages,
        tok_with_eos,
        max_seq_length=32,
        chat_template_kwargs={"enable_thinking": False},
    )
    assert feat["input_ids"] == [1, 2, 3, 10, 11, 99]
    assert feat["labels"] == [-100, -100, -100, 10, 11, 99]
    assert feat["attention_mask"] == [1, 1, 1, 1, 1, 1]
    assert tok_with_eos.template_kwargs_seen == [
        {"enable_thinking": False},
        {"enable_thinking": False},
    ]

    tok_without_eos = _FakeTokenizer(include_eos_in_template=False)
    feat_appended = train_cuda_lora.build_supervised_features(
        messages,
        tok_without_eos,
        max_seq_length=32,
    )
    assert feat_appended["input_ids"] == [1, 2, 3, 10, 11, 99]
    assert feat_appended["labels"] == [-100, -100, -100, 10, 11, 99]

    tok_boundary = _FakeTokenizer(include_eos_in_template=True, boundary_merge=True)
    feat_fallback = train_cuda_lora.build_supervised_features(
        messages,
        tok_boundary,
        max_seq_length=5,
    )
    assert feat_fallback["input_ids"] == [1, 2, 3, 10, 11]
    assert feat_fallback["labels"] == [-100, -100, -100, 10, 11]
    assert feat_fallback["attention_mask"] == [1, 1, 1, 1, 1]


def test_export_dataset_bundle_verifies_prompt_and_writes_manifest(
    tmp_path: Path,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    matching_row = {
        "messages": [
            {"role": "system", "content": ASSOCIATION_SYSTEM_PROMPT},
            {"role": "user", "content": "トピック: テスト"},
            {"role": "assistant", "content": "{}"},
        ]
    }
    (src / "train.jsonl").write_text(
        json.dumps(matching_row, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (src / "valid.jsonl").write_text(
        json.dumps(matching_row, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (src / "metadata.json").write_text('{"valid_examples": 2}', encoding="utf-8")

    out_ok = tmp_path / "bundle_ok"
    manifest = export_dataset_bundle.export_bundle(src, out_ok)

    assert manifest["prompt_matches_domain"] is True
    assert manifest["train_rows"] == 1
    assert manifest["valid_rows"] == 1
    assert manifest["source_metadata"] == {"valid_examples": 2}
    assert (out_ok / "dataset_manifest.json").is_file()
    assert (out_ok / "train.jsonl").is_file()
    assert (out_ok / "valid.jsonl").is_file()

    src_mismatch = tmp_path / "src_mismatch"
    src_mismatch.mkdir()
    mismatched_row = {
        "messages": [
            {"role": "system", "content": "different prompt"},
            {"role": "user", "content": "トピック: テスト"},
            {"role": "assistant", "content": "{}"},
        ]
    }
    (src_mismatch / "train.jsonl").write_text(
        json.dumps(mismatched_row, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (src_mismatch / "valid.jsonl").write_text(
        json.dumps(mismatched_row, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="ASSOCIATION_SYSTEM_PROMPT"):
        export_dataset_bundle.export_bundle(src_mismatch, tmp_path / "bundle_fail")

    out_override = tmp_path / "bundle_override"
    override_manifest = export_dataset_bundle.export_bundle(
        src_mismatch,
        out_override,
        allow_prompt_mismatch=True,
    )
    assert override_manifest["prompt_matches_domain"] is False
    assert (out_override / "dataset_manifest.json").is_file()
