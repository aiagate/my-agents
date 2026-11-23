from __future__ import annotations

import argparse
import importlib.metadata
import os
import subprocess
import sys
from pathlib import Path


def _run_stream(
    cmd: list[str],
    *,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> int:
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="", flush=True)
    rc = proc.wait()
    if check and rc != 0:
        raise subprocess.CalledProcessError(rc, cmd)
    return rc


def _print_versions(pkgs: list[str]) -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for pkg in pkgs:
        try:
            ver = importlib.metadata.version(pkg)
        except importlib.metadata.PackageNotFoundError:
            ver = None
        versions[pkg] = ver
    print("Installed versions:", versions, flush=True)
    return versions


def _ensure_dependencies() -> None:
    pkgs = [
        "torch",
        "torchao",
        "transformers",
        "peft",
        "accelerate",
        "datasets",
        "safetensors",
        "pyyaml",
        "numba",
    ]
    versions = _print_versions(pkgs)
    try:
        from packaging.version import Version
    except ImportError:
        Version = None

    torchao_ver = versions.get("torchao")
    if torchao_ver is not None and (
        Version is None or Version(torchao_ver) < Version("0.16.0")
    ):
        print(
            f"Removing incompatible preinstalled torchao=={torchao_ver}",
            flush=True,
        )
        _run_stream([sys.executable, "-m", "pip", "uninstall", "-y", "torchao"])

    numba_ver = versions.get("numba")
    if numba_ver is None or (
        Version is not None and Version(numba_ver) < Version("0.60.0")
    ):
        print("Installing numba>=0.60.0...", flush=True)
        _run_stream([sys.executable, "-m", "pip", "install", "-q", "numba>=0.60.0"])
        _print_versions(["numba"])


def _probe_chat_template(
    model_id: str,
    *,
    trust_remote_code: bool,
    token: str | None,
) -> tuple[bool, str]:
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(
        model_id,
        trust_remote_code=trust_remote_code,
        token=token,
    )
    try:
        prompt_only = str(
            tokenizer.apply_chat_template(
                [
                    {"role": "system", "content": "s"},
                    {"role": "user", "content": "u"},
                ],
                tokenize=False,
                add_generation_prompt=True,
            )
        )
        full_turn = str(
            tokenizer.apply_chat_template(
                [
                    {"role": "system", "content": "s"},
                    {"role": "user", "content": "u"},
                    {"role": "assistant", "content": "a"},
                ],
                tokenize=False,
                add_generation_prompt=False,
            )
        )
    except Exception as exc:
        return False, f"built-in chat_template rejected system/user/assistant: {exc}"

    if "s" not in prompt_only or "u" not in prompt_only:
        return False, "built-in chat_template dropped system or user text"
    if "a" not in full_turn:
        return False, "built-in chat_template dropped assistant text"
    return True, f"built-in chat_template OK (prompt preview: {prompt_only!r})"


def _update_tied_weights_mapping(model_id: str, token: str | None) -> None:
    from huggingface_hub import hf_hub_download

    model_file = Path(hf_hub_download(model_id, "modeling_plamo.py", token=token))
    source = model_file.read_text(encoding="utf-8")
    legacy = '_tied_weights_keys = ["lm_head.weight"]'
    mapping = '_tied_weights_keys = {"lm_head.weight": "model.embed_tokens.weight"}'
    if mapping in source and "if isinstance(module, RotaryEmbedding):" in source:
        return
    if source.count(legacy) != 1 or "return self.model.embed_tokens" not in source:
        raise RuntimeError(
            "Unexpected PLaMo model source for Transformers 5 compatibility"
        )
    updated = model_file.with_suffix(".patched.py")
    source = source.replace(legacy, mapping, 1)
    import ast
    import textwrap

    tree = ast.parse(source)
    rotary = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "RotaryEmbedding"
    )
    rotary_methods = {
        node.name: node for node in rotary.body if isinstance(node, ast.FunctionDef)
    }
    base = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "Plamo3PreTrainedModel"
    )
    init_weights = next(
        node
        for node in base.body
        if isinstance(node, ast.FunctionDef) and node.name == "_init_weights"
    )
    if "_set_cos_sin_cache" in rotary_methods:
        args = [arg.arg for arg in rotary_methods["__init__"].args.args]
        if args[:5] != ["self", "dim", "max_position_embeddings", "base", "device"]:
            raise RuntimeError(f"Unexpected PLaMo RotaryEmbedding constructor: {args}")
        init_rotary = (
            "if isinstance(module, RotaryEmbedding):\n"
            "    rebuilt = RotaryEmbedding(module.dim, module.max_position_embeddings, module.base, module.inv_freq.device)\n"
            "    for name, buffer in rebuilt.named_buffers(recurse=False):\n"
            "        getattr(module, name).copy_(buffer)\n"
            "    return\n"
        )
    elif "_compute_buffers" in rotary_methods:
        init_rotary = (
            "if isinstance(module, RotaryEmbedding):\n"
            "    module._compute_buffers()\n"
            "    return\n"
        )
    else:
        raise RuntimeError("Unexpected PLaMo rotary buffer implementation")
    lines = source.splitlines(keepends=True)
    first_statement = init_weights.body[0]
    lines.insert(
        first_statement.lineno - 1,
        textwrap.indent(init_rotary, " " * first_statement.col_offset),
    )
    source = "".join(lines)
    compile(source, str(model_file), "exec")
    updated.write_text(source, encoding="utf-8")
    print(
        "Added reinitialization of PLaMo non-persistent rotary buffers for Transformers 5",
        flush=True,
    )
    updated.replace(model_file)
    print("Updated PLaMo tied-weight mapping for Transformers 5", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--fallback-template", type=Path, default=None)
    args = parser.parse_args()

    token_value = os.getenv("HF_TOKEN")

    print("=== GPU Info ===", flush=True)
    _run_stream(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total,driver_version",
            "--format=csv,noheader",
        ],
        check=False,
    )

    print("=== Checking Dependencies ===", flush=True)
    _ensure_dependencies()

    import yaml

    cfg = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    model_id = str(cfg["model_id"])
    trust_remote_code = bool(cfg.get("trust_remote_code", False))

    _update_tied_weights_mapping(model_id, token_value)

    use_builtin, probe_msg = _probe_chat_template(
        model_id,
        trust_remote_code=trust_remote_code,
        token=token_value,
    )
    print(f"=== Chat Template Probe ({model_id}) ===", flush=True)
    print(probe_msg, flush=True)

    args.output_dir.mkdir(parents=True, exist_ok=False)

    cmd = [
        sys.executable,
        "-u",
        str(Path(__file__).resolve().parents[1] / "scripts/train_cuda_lora.py"),
        "--config",
        str(args.config),
        "--dataset-dir",
        str(args.dataset_dir),
        "--output-dir",
        str(args.output_dir),
    ]
    if not use_builtin:
        if args.fallback_template is None or not args.fallback_template.is_file():
            raise FileNotFoundError(
                "Fallback chat template required but --fallback-template was missing."
            )
        print(
            f"Using fallback template: {args.fallback_template}",
            flush=True,
        )
        cmd.extend(["--chat-template-file", str(args.fallback_template)])

    child_env = dict(os.environ)
    if token_value is not None:
        child_env["HF_TOKEN"] = token_value
        child_env["HUGGING_FACE_HUB_TOKEN"] = token_value

    print("=== Running train_cuda_lora.py ===", flush=True)
    _run_stream(cmd, env=child_env)

    manifest_path = args.output_dir / "run_manifest.json"
    print("=== run_manifest.json ===", flush=True)
    print(manifest_path.read_text(encoding="utf-8"), flush=True)

    merged_dir = args.output_dir / "merged_bf16"
    print("=== merged_bf16 files ===", flush=True)
    if merged_dir.is_dir():
        for p in sorted(merged_dir.iterdir()):
            print(f"  {p.name}: {p.stat().st_size} bytes", flush=True)
    else:
        print("  (merged_bf16 not created)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
