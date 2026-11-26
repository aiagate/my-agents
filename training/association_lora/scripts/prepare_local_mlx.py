from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np

_LAYER_KEY_RE = re.compile(r"^(?:model\.)?layers\.(\d+)\.(.+)$")


def convert_peft_weights_to_mlx(
    peft_config: dict[str, Any],
    peft_weights: dict[str, np.ndarray],
    *,
    total_layers: int | None = None,
) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    peft_type = peft_config.get("peft_type")
    if peft_type != "LORA":
        raise ValueError(
            f"Unsupported peft_type={peft_type!r}; only 'LORA' is supported."
        )
    if peft_config.get("use_dora"):
        raise ValueError(
            "DoRA adapters are not supported by peft-to-mlx; use merge+convert."
        )
    if peft_config.get("use_rslora"):
        raise ValueError(
            "rsLoRA adapters are not supported by peft-to-mlx; use merge+convert."
        )
    modules_to_save = peft_config.get("modules_to_save")
    if modules_to_save:
        raise ValueError(
            f"modules_to_save={modules_to_save!r} is not supported by peft-to-mlx; use merge+convert."
        )

    rank = int(peft_config["r"])
    lora_alpha = float(peft_config["lora_alpha"])
    lora_dropout = float(peft_config.get("lora_dropout", 0.0))
    scale = lora_alpha / rank

    mlx_weights: dict[str, np.ndarray] = {}
    layer_indices: set[int] = set()
    module_keys: set[str] = set()

    for raw_key in sorted(peft_weights):
        key = raw_key.removeprefix("base_model.model.")
        if key.endswith(".lora_A.weight"):
            base_key = key.removesuffix(".lora_A.weight")
            mlx_key = f"{base_key}.lora_a"
        elif key.endswith(".lora_A.default.weight"):
            base_key = key.removesuffix(".lora_A.default.weight")
            mlx_key = f"{base_key}.lora_a"
        elif key.endswith(".lora_B.weight"):
            base_key = key.removesuffix(".lora_B.weight")
            mlx_key = f"{base_key}.lora_b"
        elif key.endswith(".lora_B.default.weight"):
            base_key = key.removesuffix(".lora_B.default.weight")
            mlx_key = f"{base_key}.lora_b"
        else:
            raise ValueError(f"Unsupported PEFT LoRA parameter key: {raw_key!r}")

        weight = peft_weights[raw_key]
        if weight.ndim != 2:
            raise ValueError(
                f"Expected 2D weight for {raw_key!r}, got shape {weight.shape}."
            )
        mlx_weights[mlx_key] = np.ascontiguousarray(weight.T)

        match = _LAYER_KEY_RE.match(base_key)
        if match is not None:
            layer_indices.add(int(match.group(1)))
            module_keys.add(match.group(2))
        else:
            module_keys.add(base_key)

    if not mlx_weights:
        raise ValueError("No LoRA weights found in PEFT adapter.")

    if not layer_indices:
        num_layers = -1
    else:
        min_layer = min(layer_indices)
        max_layer = max(layer_indices)
        expected = set(range(min_layer, max_layer + 1))
        if layer_indices != expected:
            raise ValueError(
                f"PEFT adapter covers non-contiguous layers: {sorted(layer_indices)}"
            )
        if min_layer == 0:
            if total_layers is not None and max_layer + 1 != total_layers:
                raise ValueError(
                    f"Layer indices 0..{max_layer} do not match total_layers={total_layers}."
                )
            num_layers = -1
        else:
            if total_layers is None:
                raise ValueError(
                    "Pass --total-layers when PEFT adapter only covers trailing layers."
                )
            if max_layer + 1 != total_layers:
                raise ValueError(
                    f"Trailing layers {min_layer}..{max_layer} do not end at total_layers={total_layers}."
                )
            num_layers = max_layer - min_layer + 1

    mlx_config: dict[str, Any] = {
        "fine_tune_type": "lora",
        "num_layers": num_layers,
        "lora_parameters": {
            "rank": rank,
            "dropout": lora_dropout,
            "scale": scale,
            "keys": sorted(module_keys),
        },
    }
    return mlx_config, mlx_weights


def _run_mlx_convert(
    *,
    hf_path: str,
    mlx_path: Path,
    q_bits: int,
    q_group_size: int,
    trust_remote_code: bool,
) -> None:
    cmd = [
        sys.executable,
        "-m",
        "mlx_lm",
        "convert",
        "--hf-path",
        hf_path,
        "--mlx-path",
        str(mlx_path),
        "-q",
        "--q-bits",
        str(q_bits),
        "--q-group-size",
        str(q_group_size),
    ]
    if trust_remote_code:
        cmd.append("--trust-remote-code")
    subprocess.run(cmd, check=True)


def _prepare_output_dir(path: Path, *, force: bool) -> None:
    if path.exists():
        if not force:
            raise FileExistsError(
                f"Destination {path} already exists; pass --force to overwrite."
            )
        shutil.rmtree(path)


def _copy_sibling_manifest(hf_path: Path, mlx_path: Path) -> None:
    manifest_src = hf_path.parent / "run_manifest.json"
    if manifest_src.is_file():
        shutil.copy2(manifest_src, mlx_path / "training_manifest.json")


def _print_env_snippet(model_value: Path, adapter_value: Path | None) -> None:
    print("\n--- .env snippet ---")
    print(f"MLX_LORA_MODEL={model_value}")
    if adapter_value is None:
        print("# Keep MLX_LORA_ADAPTER_PATH unset for standalone merged models.")
    else:
        print(f"MLX_LORA_ADAPTER_PATH={adapter_value}")


def _copy_remote_code_assets(hf_path: Path, mlx_path: Path) -> None:
    for pattern in ("*.py", "tokenizer.jsonl", "chat_template.jinja"):
        for src_file in sorted(hf_path.glob(pattern)):
            if src_file.is_file():
                dst_file = mlx_path / src_file.name
                if not dst_file.exists():
                    shutil.copy2(src_file, dst_file)


def _cmd_convert(args: argparse.Namespace) -> int:
    hf_path = args.hf_path.expanduser()
    mlx_path = args.mlx_path.expanduser()
    if not hf_path.is_dir():
        raise FileNotFoundError(f"Merged HF model directory not found: {hf_path}")

    _prepare_output_dir(mlx_path, force=args.force)
    mlx_path.parent.mkdir(parents=True, exist_ok=True)
    _run_mlx_convert(
        hf_path=str(hf_path),
        mlx_path=mlx_path,
        q_bits=args.q_bits,
        q_group_size=args.q_group_size,
        trust_remote_code=args.trust_remote_code,
    )
    if args.trust_remote_code:
        _copy_remote_code_assets(hf_path, mlx_path)
    _copy_sibling_manifest(hf_path, mlx_path)
    _print_env_snippet(mlx_path, None)
    return 0


def _cmd_peft_to_mlx(args: argparse.Namespace) -> int:
    from safetensors.numpy import load_file, save_file

    peft_dir = args.peft_dir.expanduser()
    out_dir = args.out_dir.expanduser()
    config_path = peft_dir / "adapter_config.json"
    weights_path = peft_dir / "adapter_model.safetensors"
    if not config_path.is_file():
        raise FileNotFoundError(f"Missing PEFT adapter_config.json at {config_path}")
    if not weights_path.is_file():
        raise FileNotFoundError(
            f"Missing PEFT adapter_model.safetensors at {weights_path}"
        )

    peft_config = json.loads(config_path.read_text(encoding="utf-8"))
    peft_weights = load_file(str(weights_path))
    mlx_config, mlx_weights = convert_peft_weights_to_mlx(
        peft_config,
        peft_weights,
        total_layers=args.total_layers,
    )

    _prepare_output_dir(out_dir, force=args.force)
    out_dir.mkdir(parents=True, exist_ok=True)
    save_file(mlx_weights, str(out_dir / "adapters.safetensors"))
    (out_dir / "adapter_config.json").write_text(
        json.dumps(mlx_config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Saved MLX adapter to {out_dir}")

    if args.fuse_base and args.quantize_out:
        quantize_out = args.quantize_out.expanduser()
        _prepare_output_dir(quantize_out, force=args.force)
        quantize_out.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=str(quantize_out.parent)) as tmp_dir:
            fused_dir = Path(tmp_dir) / "fused"
            fuse_cmd = [
                sys.executable,
                "-m",
                "mlx_lm",
                "fuse",
                "--model",
                args.fuse_base,
                "--adapter-path",
                str(out_dir),
                "--save-path",
                str(fused_dir),
            ]
            if args.trust_remote_code:
                fuse_cmd.append("--trust-remote-code")
            subprocess.run(fuse_cmd, check=True)
            _run_mlx_convert(
                hf_path=str(fused_dir),
                mlx_path=quantize_out,
                q_bits=args.q_bits,
                q_group_size=args.q_group_size,
                trust_remote_code=args.trust_remote_code,
            )
        _copy_sibling_manifest(peft_dir, quantize_out)
        _print_env_snippet(quantize_out, None)
    elif args.fuse_base or args.quantize_out:
        raise ValueError("Specify both --fuse-base and --quantize-out together.")
    else:
        _copy_sibling_manifest(peft_dir, out_dir)
        print(f"MLX_LORA_ADAPTER_PATH={out_dir}")
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert CUDA-trained LoRA weights for local Apple Silicon MLX."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    convert_p = subparsers.add_parser(
        "convert",
        help="Quantize a merged HF/BF16 model directory into a standalone MLX model.",
    )
    convert_p.add_argument("--hf-path", type=Path, required=True)
    convert_p.add_argument("--mlx-path", type=Path, required=True)
    convert_p.add_argument("--q-bits", type=int, default=4)
    convert_p.add_argument("--q-group-size", type=int, default=64)
    convert_p.add_argument("--trust-remote-code", action="store_true")
    convert_p.add_argument("--force", action="store_true")

    peft_p = subparsers.add_parser(
        "peft-to-mlx",
        help="Convert a dense PEFT LoRA adapter into MLX adapter format.",
    )
    peft_p.add_argument("--peft-dir", type=Path, required=True)
    peft_p.add_argument("--out-dir", type=Path, required=True)
    peft_p.add_argument("--total-layers", type=int, default=None)
    peft_p.add_argument("--fuse-base", default=None)
    peft_p.add_argument("--quantize-out", type=Path, default=None)
    peft_p.add_argument("--q-bits", type=int, default=4)
    peft_p.add_argument("--q-group-size", type=int, default=64)
    peft_p.add_argument("--trust-remote-code", action="store_true")
    peft_p.add_argument("--force", action="store_true")

    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "convert":
        return _cmd_convert(args)
    if args.command == "peft-to-mlx":
        return _cmd_peft_to_mlx(args)
    raise ValueError(f"Unknown command: {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
